"""
PQ-CSMS — OCPP 2.0.1 WebSocket server.

Track A (csms). Phase A1: transport, connection lifecycle, and the
Contract 6 HTTP surface.

--------------------------------------------------------------------
WHAT THIS IS AT STAGE 1

Plain ws://. No TLS, no certificates, no cryptography of any kind --
that is Day 7, jointly with Track B, and crypto/stub.py raises on every
call until then. Stage 1's job is to prove the protocol layer is
correct before anything cryptographic can hide a bug inside it.

Message handlers live in csms/handlers.py. As of Phase A2 that covers
BootNotification, Heartbeat and StatusNotification. Authorize and
TransactionEvent arrive in Phase A3 (Day 5); until then a station
sending either is answered with a protocol-level "not supported" by the
ocpp library, which is the correct behaviour for an unimplemented
action.

--------------------------------------------------------------------
ONE PORT, TWO PROTOCOLS

  ws://localhost:9000/{station_id}   OCPP 2.0.1, stations connect here
  http://localhost:9000/api/...      Contract 6, the dashboard polls here

The websockets library hands every incoming request to process_request
before deciding whether to upgrade it. Returning a Response there
answers it as plain HTTP; returning None lets the WebSocket handshake
proceed. So the two protocols share one listener with no second server,
no second port and no additional dependency.

--------------------------------------------------------------------
LIBRARY API

This targets websockets' asyncio server API (websockets >= 13), which
is what experiments/smoke_test.py already runs against: single-argument
connection handler, connection.request.path, and the
process_request(connection, request) signature. The import below is
from websockets.asyncio.server rather than the top-level package
precisely so that the API in use is unambiguous across versions --
top-level websockets.serve means different things in 13.x and 14.x.
--------------------------------------------------------------------

Usage:
    python -m csms.server
    python -m csms.server --port 9000 --mode classical
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.asyncio.server import serve

from csms.authorization import AUTH_MODES, AuthorizationPolicy
from csms.events import EventLog, EventType, Outcome
from csms.handlers import DEFAULT_HEARTBEAT_INTERVAL_S, CSMSHandlers
from csms.registry import SessionRegistry
from idmanager.stub import StubController

LOGGER = logging.getLogger("csms")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9000

SUBPROTOCOLS = ["ocpp2.0.1", "ocpp2.0.1+pqc"]
"""Both tags are accepted from the start.

ocpp2.0.1 is the standard identifier. ocpp2.0.1+pqc is the capability
signal proposed in PNNL-35760 for post-quantum-capable endpoints, and
the heterogeneous fleet in Stage 6 needs some way for a station to
declare what it can process. Accepting both now costs one list entry;
adding it to the handshake on Day 8 would mean reopening the connection
path after everything is built on top of it.

The server does not branch on which was negotiated. That is Stage 6's
work, and it belongs in the capability registry, not here.
"""

DEFAULT_WS_PING_INTERVAL_S: float | None = 20.0
DEFAULT_WS_PING_TIMEOUT_S: float | None = 20.0
"""WebSocket-level keepalive. NOT the OCPP Heartbeat.

Two different mechanisms, one layer apart, and they must not be
confused. OCPP Heartbeat is an application message a station sends on
the interval the CSMS issued. This is the WebSocket protocol's own
ping/pong: the server pings every open connection every
ws_ping_interval seconds and CLOSES any connection that fails to pong
within ws_ping_timeout.

WHY THIS IS EXPOSED AS A FLAG, AND WHY E2 SHOULD DISABLE IT.

E2 kills the CSMS and has the whole fleet reconnect at once, performing
post-quantum handshakes. That is a CPU spike across hundreds of agent
processes on one machine. An agent whose event loop is saturated may
pong late -- and the server would then drop a station that was
recovering perfectly well. That disconnection lands in the dataset as a
station that failed to recover, and the reported cost of post-quantum
migration would partly be the cost of our own keepalive.

OCPP Heartbeat already provides liveness at the application layer, so
the WebSocket ping adds nothing this system needs and can only add
noise to the one measurement the project is built on. Run E2 with
--ws-ping-interval 0.

Set to None (via 0 on the command line) to disable entirely.
"""


def _json_response(status: HTTPStatus, payload: Any) -> Response:
    """
    Build a plain HTTP JSON response for the Contract 6 surface.

    default=str on the dump is a safety net, not the mechanism:
    FleetSnapshot.to_dict() already converts its datetimes. It is here
    so that an unconverted value added later degrades to a string in
    the dashboard rather than raising inside the connection handler and
    dropping the request.
    """
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = Headers(
        {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Connection": "close",
        }
    )
    return Response(status.value, status.phrase, headers, body)


def _station_id_from_path(path: str) -> str:
    """
    Extract the station identity from the connection path.

    Our own convention, frozen with Track C so that agent/client.py
    matches experiments/smoke_test.py:

        ws://host:port/{station_id}

    The LAST non-empty path segment is taken, not the whole stripped
    path, and this is deliberate. E6 connects a third-party OCPP client
    to this CSMS, and independent implementations commonly use a longer
    path -- /ocpp/{id}, or a full service route. Stripping slashes off
    the whole path would turn "/ocpp/CP001" into the station id
    "ocpp/CP001": not a crash, just a silently wrong identity flowing
    into the registry, the event log and the dashboard, discovered on
    Day 10 under time pressure. Taking the last segment accepts both
    shapes and costs nothing.

    The query string is discarded, so a station may append parameters
    without corrupting its own ID. Percent-encoding is decoded, because
    a conformant client is entitled to encode the path segment.
    """
    segments = [s for s in urlparse(path).path.split("/") if s]
    return unquote(segments[-1]) if segments else ""


class CSMS:
    """
    One CSMS process.

    Owns the event log, the session registry and the migration
    controller. Everything else in csms/ is handed these rather than
    constructing its own, so there is exactly one of each per process
    and no component can end up reading a different fleet than another.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        crypto_mode: str = "classical",
        log_path: str = "logs/events.jsonl",
        heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        log_messages: bool = False,
        ws_ping_interval: float | None = DEFAULT_WS_PING_INTERVAL_S,
        ws_ping_timeout: float | None = DEFAULT_WS_PING_TIMEOUT_S,
        auth_policy: AuthorizationPolicy | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.crypto_mode = crypto_mode
        self.heartbeat_interval_s = heartbeat_interval_s
        self.log_messages = log_messages
        self.ws_ping_interval = ws_ping_interval
        self.ws_ping_timeout = ws_ping_timeout
        self.auth_policy = auth_policy or AuthorizationPolicy()

        self.log = EventLog(path=log_path, crypto_mode=crypto_mode)
        """Contract 3. Created here and shared, because run_id must be
        identical across every event of one server run -- that is what
        separates a 50-node run from a 500-node run at analysis time."""

        self.controller = StubController()
        """Contract 4, Track B's stub until Days 8-12.

        get_migration_status() returns a real IDLE status, so the
        dashboard's migration panel works from today. start_migration()
        and rollback() raise NotImplementedError, which the HTTP layer
        turns into a 501 rather than a stack trace."""

        self.registry = SessionRegistry(
            event_log=self.log,
            crypto_mode=crypto_mode,
            run_id=self.log.run_id,
            migration_controller=self.controller,
        )

    # -- OCPP WebSocket side --------------------------------------------

    async def on_connect(self, websocket: Any) -> None:
        """
        Handle one station connection for its entire lifetime.

        The try/finally is the important part. Whatever ends this
        connection -- a clean close, a dropped network, an unhandled
        error in a handler, or the server being killed mid-run in E2 --
        the session is removed from the registry. A registry that leaks
        sessions reports a fleet larger than the one that exists, and
        every recovery figure derived from it is then wrong in a way
        that is invisible until analysis.
        """
        path = getattr(getattr(websocket, "request", None), "path", "") or ""
        station_id = _station_id_from_path(path)

        if not station_id:
            self.log.emit(
                EventType.CONNECTION_FAILED,
                None,
                outcome=Outcome.REJECTED,
                reason="no_station_id_in_path",
                path=path,
            )
            await websocket.close(code=1008, reason="station id required in path")
            return

        charge_point = CSMSHandlers(
            station_id,
            websocket,
            registry=self.registry,
            event_log=self.log,
            heartbeat_interval_s=self.heartbeat_interval_s,
            log_messages=self.log_messages,
            auth_policy=self.auth_policy,
        )
        self.registry.register(station_id, charge_point)
        LOGGER.info("station connected: %s", station_id)

        try:
            await charge_point.start()
        except ConnectionClosed:
            pass  # ordinary disconnect; the finally block does the work
        except Exception:
            LOGGER.exception("station %s: handler failed", station_id)
            self.log.emit(
                EventType.CONNECTION_FAILED,
                station_id,
                outcome=Outcome.FAILURE,
                reason="handler_exception",
            )
        finally:
            self.registry.deregister(station_id, charge_point)
            LOGGER.info("station disconnected: %s", station_id)

    # -- Contract 6 HTTP side -------------------------------------------

    def process_request(self, connection: Any, request: Any) -> Response | None:
        """
        Answer /api/ requests as HTTP; let everything else upgrade.

        Runs synchronously on the event loop for every incoming
        connection, so it must stay cheap. It is: every route below is a
        walk over in-memory state. Nothing here touches SQLite, and
        nothing here awaits.
        """
        parsed = urlparse(request.path)
        path = parsed.path

        if not path.startswith("/api/"):
            return None  # not ours — proceed with the WebSocket handshake

        query = parse_qs(parsed.query)

        try:
            return self._route(path, query)
        except Exception:
            LOGGER.exception("api: unhandled error on %s", path)
            return _json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}
            )

    def _route(self, path: str, query: dict[str, list[str]]) -> Response:
        if path == "/api/health":
            return _json_response(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "run_id": self.log.run_id,
                    "crypto_mode": self.crypto_mode,
                },
            )

        if path == "/api/fleet":
            return _json_response(HTTPStatus.OK, self.registry.snapshot().to_dict())

        if path.startswith("/api/fleet/"):
            station_id = path[len("/api/fleet/"):]
            view = self.registry.get_station(station_id)
            if view is None:
                return _json_response(
                    HTTPStatus.NOT_FOUND,
                    {"error": "unknown station", "station_id": station_id},
                )
            return _json_response(HTTPStatus.OK, view.to_dict())

        if path == "/api/migration":
            return _json_response(
                HTTPStatus.OK, self.controller.get_migration_status().to_dict()
            )

        if path == "/api/migration/start":
            return self._start_migration(query)

        if path == "/api/migration/rollback":
            return self._rollback(query)

        return _json_response(HTTPStatus.NOT_FOUND, {"error": "no such endpoint"})

    def _start_migration(self, query: dict[str, list[str]]) -> Response:
        """
        Forward to Contract 4's start_migration.

        Parameters arrive as query parameters rather than a JSON body
        because process_request is handed the request line and headers
        only and never reads a body. See Contract 6's note on this.
        """
        try:
            wave_size = int(query["wave_size"][0])
            canary_count = int(query["canary_count"][0])
            target_mode = query["target_mode"][0]
        except (KeyError, IndexError, ValueError):
            return _json_response(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "wave_size, canary_count and target_mode required",
                    "example": "/api/migration/start"
                               "?wave_size=10&canary_count=2&target_mode=pqc",
                },
            )

        try:
            migration_id = self.controller.start_migration(
                wave_size=wave_size,
                canary_count=canary_count,
                target_mode=target_mode,
            )
        except NotImplementedError:
            return _json_response(
                HTTPStatus.NOT_IMPLEMENTED,
                {"error": "migration orchestrator not implemented yet (Track B)"},
            )
        except RuntimeError as exc:
            # Contract 4: raised when a migration is already in progress.
            return _json_response(HTTPStatus.CONFLICT, {"error": str(exc)})

        self.log.emit(EventType.MIGRATION_STARTED, None, migration_id=migration_id)
        return _json_response(HTTPStatus.OK, {"migration_id": migration_id})

    def _rollback(self, query: dict[str, list[str]]) -> Response:
        try:
            wave_id = int(query["wave_id"][0])
        except (KeyError, IndexError, ValueError):
            return _json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "wave_id required",
                 "example": "/api/migration/rollback?wave_id=0"},
            )

        try:
            reverted = self.controller.rollback(wave_id)
        except NotImplementedError:
            return _json_response(
                HTTPStatus.NOT_IMPLEMENTED,
                {"error": "migration orchestrator not implemented yet (Track B)"},
            )

        self.log.emit(
            EventType.WAVE_ROLLED_BACK,
            None,
            wave_id=wave_id,
            outcome=Outcome.SUCCESS if reverted else Outcome.FAILURE,
        )
        return _json_response(HTTPStatus.OK, {"reverted": reverted})

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        """Serve until interrupted."""
        self.log.emit(
            EventType.SERVER_STARTED,
            None,
            host=self.host,
            port=self.port,
            subprotocols=SUBPROTOCOLS,
            heartbeat_interval_s=self.heartbeat_interval_s,
            log_messages=self.log_messages,
            ws_ping_interval=self.ws_ping_interval,
            ws_ping_timeout=self.ws_ping_timeout,
            **self.auth_policy.describe(),
        )
        # Every parameter that can affect a measurement is recorded on the
        # SERVER_STARTED event, so a run's configuration is recoverable from
        # its own log rather than from someone's memory of which flags they
        # typed. Section 18 asks for results reproducible from logged data.

        stop = asyncio.get_running_loop().create_future()
        self._install_signal_handlers(stop)

        async with serve(
            self.on_connect,
            self.host,
            self.port,
            subprotocols=SUBPROTOCOLS,
            process_request=self.process_request,
            ping_interval=self.ws_ping_interval,
            ping_timeout=self.ws_ping_timeout,
        ):
            LOGGER.info(
                "CSMS listening — ws://%s:%d/{station_id} · "
                "http://%s:%d/api/fleet · mode=%s · run_id=%s",
                self.host, self.port, self.host, self.port,
                self.crypto_mode, self.log.run_id,
            )
            await stop

        self.log.emit(EventType.SERVER_STOPPING, None)
        self.log.close()
        LOGGER.info("CSMS stopped")

    def _install_signal_handlers(self, stop: asyncio.Future) -> None:
        """
        Stop cleanly on SIGINT/SIGTERM so that SERVER_STOPPING is
        written and the log is fsynced before exit.

        E2 kills this process deliberately. A clean stop marks the
        before-picture; a kill -9 does not, and the analysis needs to be
        able to tell an intentional restart from a crash.

        add_signal_handler is not implemented on Windows, where two of
        the three machines run. Falling back rather than failing keeps
        the same code running on all three.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig, lambda: stop.done() or stop.set_result(None)
                )
            except (NotImplementedError, AttributeError):
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="PQ-CSMS (OCPP 2.0.1)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--mode",
        default="classical",
        choices=("classical", "hybrid", "pqc"),
        help="crypto mode recorded on every event; no crypto runs until Day 7",
    )
    parser.add_argument("--log", default="logs/events.jsonl")
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=DEFAULT_HEARTBEAT_INTERVAL_S,
        help="seconds; handed to each station in its BootNotification response",
    )
    parser.add_argument(
        "--log-messages",
        action="store_true",
        help="emit a MESSAGE_RECEIVED event per OCPP message; off by default "
             "because at fleet scale it dominates the event log",
    )
    parser.add_argument(
        "--ws-ping-interval",
        type=float,
        default=DEFAULT_WS_PING_INTERVAL_S,
        help="WebSocket keepalive ping interval in seconds; 0 disables. "
             "NOT the OCPP Heartbeat. Use 0 for E2 runs — see the module "
             "docstring for why",
    )
    parser.add_argument(
        "--ws-ping-timeout",
        type=float,
        default=DEFAULT_WS_PING_TIMEOUT_S,
        help="seconds to wait for a pong before closing the connection; "
             "0 disables",
    )
    parser.add_argument(
        "--auth-mode",
        default="allowlist",
        choices=AUTH_MODES,
        help="allowlist uses the seeded token list; accept-all authorises "
             "every token — the escape hatch for integrating with an agent "
             "whose tokens have not been agreed yet",
    )
    parser.add_argument(
        "--id-tokens",
        default=None,
        help='JSON file of {"TAG-0001": "Accepted", ...} replacing the '
             "seeded token list",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    auth_policy = (
        AuthorizationPolicy.from_file(args.id_tokens, mode=args.auth_mode)
        if args.id_tokens
        else AuthorizationPolicy(mode=args.auth_mode)
    )

    csms = CSMS(
        host=args.host,
        port=args.port,
        crypto_mode=args.mode,
        log_path=args.log,
        heartbeat_interval_s=args.heartbeat_interval,
        log_messages=args.log_messages,
        ws_ping_interval=args.ws_ping_interval or None,
        ws_ping_timeout=args.ws_ping_timeout or None,
        auth_policy=auth_policy,
    )
    try:
        asyncio.run(csms.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
