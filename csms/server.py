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
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.asyncio.server import serve

from csms.authorization import AUTH_MODES, AuthorizationPolicy
from csms.dispatch import DEFAULT_DISPATCH_TIMEOUT_S, CommandDispatcher
from csms.events import EventLog, EventType, Outcome
from csms.handlers import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    HAS_ROUTE_MESSAGE,
    HAS_SEND,
    CSMSHandlers,
)
from csms.persistence import (
    DEFAULT_DB_PATH,
    DEFAULT_FLUSH_INTERVAL_S,
    DEFAULT_SYNCHRONOUS,
    SYNCHRONOUS_MODES,
    open_store,
)
from csms.registry import SessionRegistry
from csms.transport import (
    CLIENT_CERT_MODES,
    DEFAULT_CERT_DIR,
    DEFAULT_CLIENT_CERT_MODE,
    DEFAULT_IDENTITY_CHECK,
    DEFAULT_SERVER_NAME,
    IDENTITY_CHECK_MODES,
    TlsConfigError,
    build_server_context,
    check_identity,
    default_paths,
    describe_connection_security,
)
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


HANDSHAKE_SCOPE = "server_upgrade"
"""What this server's handshake_ms actually measures, stamped on every
connection event so no analysis can mistake it for the full figure.

THE SERVER CANNOT TIME ITS OWN TLS HANDSHAKE. By the time any of our
code runs on a connection, process_request has already been reached --
which is after the certificate exchange and after the HTTP request line
is parsed. The websockets library exposes no earlier hook.

So the server measures the WebSocket UPGRADE, from the first moment it
sees the connection to the moment the handler starts. That is a real
number and it belongs in the log, but it is NOT the cost of exchanging
post-quantum certificates, which is what E1 is about.

The full handshake is measured CLIENT-side, where one process owns the
whole sequence -- TCP connect, TLS handshake, WebSocket upgrade. Track C
already logs it, and their harness timing log is joined to this one on
station_id + run_id at analysis time (their plan §6.1). Their figure is
E1's headline; this one cross-checks it and covers the server's view.

Recording the scope in the data rather than only in a document means the
distinction survives someone reading the log six weeks from now.
"""

CONNECTION_START_ATTR = "_pqcharge_seen_at_ns"
"""Monotonic reading stamped on the connection in process_request, read
back in on_connect. An attribute on the library's object rather than a
dictionary keyed by connection, so it cannot leak if a connection is
dropped between the two points."""


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
        db_path: str | None = DEFAULT_DB_PATH,
        db_flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        db_synchronous: str = DEFAULT_SYNCHRONOUS,
        ssl_context: Any | None = None,
        identity_check: str = DEFAULT_IDENTITY_CHECK,
        dispatch_timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S,
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

        self.store = open_store(
            db_path,
            run_id=self.log.run_id,
            synchronous=db_synchronous,
        )
        """Station state that survives a restart. A NullStore when
        --no-db is passed, or when the file cannot be opened -- a CSMS
        that refuses to accept charging stations because a cache file is
        unwritable is worse than one that forgets what it knew."""

        self.db_flush_interval_s = db_flush_interval_s

        self.ssl_context = ssl_context
        """The Security Profile 3 context, or None for plain ws://.
        Built in main() so a bad certificate path stops the server at
        startup rather than at the first connection."""

        self.identity_check = identity_check
        """How strictly the certificate's Common Name must match the
        station id from the path. See csms/transport.py -- without this
        check, any holder of a valid certificate can claim any station's
        identity, which is the impersonation E5 demonstrates."""

        self.registry = SessionRegistry(
            event_log=self.log,
            crypto_mode=crypto_mode,
            run_id=self.log.run_id,
            migration_controller=self.controller,
            store=self.store,
        )
        self.dispatcher = CommandDispatcher(
            self.registry, self.log, timeout_s=dispatch_timeout_s
        )
        """Sends OCPP commands down to stations.

        Handed to Track B's migration orchestrator when it lands: its
        certificate messages go through the same generic send() as the
        charging commands, so Stage 5 rotation is not a second path
        built under time pressure."""

        restored = self.registry.load()
        if restored:
            LOGGER.info(
                "restored %d station(s) from %s -- fleet known before anyone "
                "reconnects", restored, db_path,
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

        if self.ssl_context is not None:
            ok, common_name = check_identity(
                station_id, websocket, mode=self.identity_check
            )
            self.log.emit(
                EventType.CONNECTION_ATTEMPT,
                station_id,
                outcome=Outcome.SUCCESS if ok else Outcome.REJECTED,
                transition="identity_check",
                certificate_common_name=common_name,
                identity_matches=common_name == station_id,
                identity_check=self.identity_check,
            )
            if not ok:
                await websocket.close(
                    code=1008, reason="certificate identity mismatch"
                )
                return

        seen_at_ns = getattr(websocket, CONNECTION_START_ATTR, None)
        setup_ms = (
            (time.monotonic_ns() - seen_at_ns) / 1e6
            if seen_at_ns is not None
            else None
        )
        security = (
            describe_connection_security(websocket)
            if self.ssl_context is not None
            else {}
        )

        charge_point = CSMSHandlers(
            station_id,
            websocket,
            registry=self.registry,
            event_log=self.log,
            heartbeat_interval_s=self.heartbeat_interval_s,
            log_messages=self.log_messages,
            auth_policy=self.auth_policy,
        )
        self.registry.register(
            station_id,
            charge_point,
            handshake_ms=setup_ms,
            security={k: v for k, v in security.items()
                      if k != "peer_common_name"},
            extra={"handshake_scope": HANDSHAKE_SCOPE},
        )
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
        # First moment this server sees the connection. Stamped for every
        # request, WebSocket upgrade or /api call alike, because the cost
        # of one monotonic read is nothing and branching here would mean
        # the OCPP path -- the one being measured -- carried the branch.
        try:
            setattr(connection, CONNECTION_START_ATTR, time.monotonic_ns())
        except (AttributeError, TypeError):  # pragma: no cover
            pass

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
            db_enabled=self.store.enabled,
            db_flush_interval_s=self.db_flush_interval_s,
            tls=self.ssl_context is not None,
            identity_check=self.identity_check,
            handshake_scope=HANDSHAKE_SCOPE,
            byte_counting_available=HAS_ROUTE_MESSAGE and HAS_SEND,
            dispatch_timeout_s=dispatch_timeout_s,
        )
        # Every parameter that can affect a measurement is recorded on the
        # SERVER_STARTED event, so a run's configuration is recoverable from
        # its own log rather than from someone's memory of which flags they
        # typed. Section 18 asks for results reproducible from logged data.

        stop = asyncio.get_running_loop().create_future()
        self._install_signal_handlers(stop)
        flusher = asyncio.ensure_future(self._flush_loop())

        async with serve(
            self.on_connect,
            self.host,
            self.port,
            subprotocols=SUBPROTOCOLS,
            ssl=self.ssl_context,
            process_request=self.process_request,
            ping_interval=self.ws_ping_interval,
            ping_timeout=self.ws_ping_timeout,
        ):
            scheme_ws = "wss" if self.ssl_context is not None else "ws"
            scheme_http = "https" if self.ssl_context is not None else "http"
            LOGGER.info(
                "CSMS listening — %s://%s:%d/{station_id} · "
                "%s://%s:%d/api/fleet · mode=%s · run_id=%s",
                scheme_ws, self.host, self.port,
                scheme_http, self.host, self.port,
                self.crypto_mode, self.log.run_id,
            )
            if self.ssl_context is not None:
                LOGGER.info(
                    "the /api surface is behind the same TLS socket: use "
                    "--cacert %s, and a client certificate if client certs "
                    "are required",
                    "certs/root.pem",
                )
            await stop

        flusher.cancel()
        self.registry.close()
        self.log.emit(EventType.SERVER_STOPPING, None)
        self.log.close()
        LOGGER.info("CSMS stopped")

    async def _flush_loop(self) -> None:
        """
        Push buffered station state to disk on a timer.

        Separate from the connection handlers on purpose: a disk write
        on the path that accepts a connection is a disk write inside the
        measurement E2 is taking. This task is the only thing that
        touches the database during a run, and cancelling it costs at
        most one interval of state.
        """
        if self.db_flush_interval_s <= 0:
            return
        while True:
            await asyncio.sleep(self.db_flush_interval_s)
            written = self.registry.flush()
            if written:
                LOGGER.debug("persistence flush: %d row(s)", written)

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
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help="SQLite file holding station state across restarts",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="run without persistence; a restarted CSMS then starts with an "
             "empty fleet and E2 has no recovery denominator",
    )
    parser.add_argument(
        "--db-flush-interval",
        type=float,
        default=DEFAULT_FLUSH_INTERVAL_S,
        help="seconds between buffered writes reaching disk; also the upper "
             "bound on how much state a hard kill can lose",
    )
    parser.add_argument(
        "--db-synchronous",
        default=DEFAULT_SYNCHRONOUS,
        choices=SYNCHRONOUS_MODES,
        help="SQLite durability. Exposed because it is the same "
             "durability-versus-speed trade-off the Day 6 event-log "
             "experiment measures, and the two should be measured together",
    )
    parser.add_argument(
        "--tls",
        action="store_true",
        help="enable OCPP Security Profile 3 (mutual TLS). Certificates come "
             "from `python -m experiments.bootstrap_pki`",
    )
    parser.add_argument(
        "--cert-dir",
        default=DEFAULT_CERT_DIR,
        help="directory bootstrap_pki wrote the PKI into",
    )
    parser.add_argument("--cert", default=None, help="server certificate PEM")
    parser.add_argument("--key", default=None, help="server private key PEM")
    parser.add_argument("--ca", default=None, help="CA root PEM")
    parser.add_argument(
        "--tls-client-certs",
        default=DEFAULT_CLIENT_CERT_MODE,
        choices=CLIENT_CERT_MODES,
        help="'required' is Security Profile 3. Anything else is a documented "
             "deviation — and note every caller of /api, including the "
             "dashboard, then needs a client certificate too",
    )
    parser.add_argument(
        "--tls-identity-check",
        default=DEFAULT_IDENTITY_CHECK,
        choices=IDENTITY_CHECK_MODES,
        help="compare the certificate Common Name against the station id in "
             "the path. 'enforce' refuses a mismatch — without it any valid "
             "certificate can claim any station's identity",
    )
    parser.add_argument(
        "--dispatch-timeout",
        type=float,
        default=DEFAULT_DISPATCH_TIMEOUT_S,
        help="seconds to wait for a station to answer a command. Shorter "
             "than the ocpp library's own 30 s, which is what a dead "
             "connection blocks for; it lands in E3's migration duration",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    ssl_context = None
    if args.tls:
        cert, key, ca = default_paths(args.cert_dir, DEFAULT_SERVER_NAME)
        try:
            ssl_context = build_server_context(
                args.cert or cert,
                args.key or key,
                args.ca or ca,
                client_certs=args.tls_client_certs,
            )
        except TlsConfigError as exc:
            parser.error(str(exc))

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
        db_path=None if args.no_db else args.db,
        db_flush_interval_s=args.db_flush_interval,
        db_synchronous=args.db_synchronous,
        ssl_context=ssl_context,
        identity_check=args.tls_identity_check,
        dispatch_timeout_s=args.dispatch_timeout,
    )
    try:
        asyncio.run(csms.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
