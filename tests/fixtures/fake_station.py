"""
Throwaway station client, for Track A's own testing.

Track C owns the real agent (agent/client.py) and their own test double
of the CSMS (tests/fixtures/fake_csms.py). This is the mirror image and
it is Track A's: something that connects *to* the CSMS so the server can
be exercised without the full agent. Dev/test only, never imported by
shipped code.

A full charging session, which is the Stage 1 gate:

    connect
      -> BootNotification            (adopts the server's interval;
                                      STOPS if the server does not accept)
      -> StatusNotification Available
      -> Authorize                   (a driver taps a card)
      -> StatusNotification Occupied
      -> TransactionEvent Started    seq 0
      -> TransactionEvent Updated    seq 1..n, with meter values
      -> TransactionEvent Ended      seq n+1
      -> StatusNotification Available
    disconnect

Heartbeats run throughout at the interval the SERVER issued.

Day 8 -- the station now ANSWERS commands, so csms/dispatch.py can be
proven against a live connection instead of reviewed on paper:

    SetChargingProfile      caps the power it reports (0 W = SuspendedEVSE)
    ClearChargingProfile    removes the cap; "Unknown" if there was none
    RequestStopTransaction  ends the running transaction (RemoteStop)
    DataTransfer            always "UnknownVendorId" -- this fixture has
                            no post-quantum identity, so to the migration
                            orchestrator it is a charger that refuses the
                            upgrade. That is E3's injected failure for free.

Usage (the -m form is the repo convention):
    python -m tests.fixtures.fake_station CP0001
    python -m tests.fixtures.fake_station CP0001 CP0002 CP0003 --charge-for 40
    python -m tests.fixtures.fake_station CP0001 --token TAG-BLOCKED
    python -m tests.fixtures.fake_station CP0001 --charge-for 0 --power 11000
        ^ charges until the CSMS stops it (or Ctrl-C) -- for dispatch tests

Over mutual TLS -- certificates from `python -m experiments.bootstrap_pki`:
    python -m tests.fixtures.fake_station CP0001 --tls
    python -m tests.fixtures.fake_station CP0001 --tls --cert-as CP0002
        ^ connects as CP0001 while presenting CP0002's certificate, to prove
          the server's identity check catches one certificate holder
          claiming another station's identity.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from ocpp.routing import on
from ocpp.v201 import ChargePoint as CpBase
from ocpp.v201 import call, call_result

if __package__ in (None, ""):
    # Run as a plain script, Python puts only THIS directory on sys.path,
    # so `import csms` fails. The repo convention is
    # `python -m tests.fixtures.fake_station` from the root, which needs no
    # help -- this keeps the shorter `python tests/fixtures/fake_station.py`
    # working too. A dev-only fixture may do this; shipped code may not.
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from csms.transport import (
    DEFAULT_CERT_DIR,
    DEFAULT_SERVER_NAME,
    build_client_context,
)

DEFAULT_URL = "ws://localhost:9000"
DEFAULT_TOKEN = "TAG-0001"

STATUS_AVAILABLE = "Available"
STATUS_OCCUPIED = "Occupied"
CHARGING = "Charging"
SUSPENDED_EVSE = "SuspendedEVSE"
IDLE = "Idle"
"""Wire values from OCPP 2.0.1, sent as plain strings.

Deliberately not imported from ocpp.v201.enums: the enum CLASS names
have moved between releases of that library, and this fixture should
not be the thing that breaks when someone upgrades it. The library's
schema validator rejects a wrong value, so a typo fails loudly."""

DEFAULT_POWER_W = 7400.0
"""7.4 kW, matching agent/simulated_power.py's DEFAULT_LIMIT_W so that
Track A's fixture and Track C's agent report comparable figures. Now
only the DEFAULT for --power: a station whose power cannot move cannot
demonstrate curtailment, which is E5's whole mechanism."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meter_value(power_w: float, energy_wh: float) -> dict:
    """
    One OCPP MeterValue carrying instantaneous power and cumulative
    energy, with the measurand strings csms/metering.py recognises.
    """
    return {
        "timestamp": _now_iso(),
        "sampled_value": [
            {
                "value": power_w,
                "measurand": "Power.Active.Import",
                "unit_of_measure": {"unit": "W"},
            },
            {
                "value": energy_wh,
                "measurand": "Energy.Active.Import.Register",
                "unit_of_measure": {"unit": "Wh"},
            },
        ],
    }


def _limit_from_profile(charging_profile: Any) -> float | None:
    """
    The power cap in watts carried by a SetChargingProfile, or None if
    the profile is not one this fixture understands.

    Deliberately narrow: first schedule, first period, unit W. That is
    exactly the shape csms/dispatch.py sends (rule 3 -- watts, never
    amps), so anything else reaching this fixture is a dispatch bug
    worth refusing loudly rather than guessing at.
    """
    try:
        schedules = charging_profile["charging_schedule"]
        schedule = schedules[0] if isinstance(schedules, list) else schedules
        if str(schedule.get("charging_rate_unit")) != "W":
            return None
        limit = float(schedule["charging_schedule_period"][0]["limit"])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    if not math.isfinite(limit) or limit < 0:
        return None
    return limit


class FakeStation(CpBase):
    """Minimal station: boots, authorises, charges, reports, obeys."""

    def __init__(
        self,
        station_id: str,
        connection,
        token: str,
        *,
        power_w: float = DEFAULT_POWER_W,
    ) -> None:
        super().__init__(station_id, connection)
        self.token = token
        self.transaction_id = uuid.uuid4().hex[:12]
        self.seq_no = 0
        self.energy_wh = 0.0

        self.max_power_w = power_w
        """What the car would draw if nothing limited it (--power)."""

        self.limit_w: float | None = None
        """The cap the CSMS set with SetChargingProfile, or None."""

        self.transaction_open = False
        self.stop_requested = asyncio.Event()
        """Set by RequestStopTransaction. The charging loop watches it.

        The handler must NOT end the transaction itself: ending it means
        sending TransactionEvent, which awaits a reply that only this
        connection's receive loop can read -- and inside a handler that
        loop is the one blocked. The same rule 1 csms/dispatch.py
        enforces on the server side, from the other end of the wire."""

    # -- what the station is drawing right now ---------------------------

    @property
    def power_w(self) -> float:
        if self.limit_w is None:
            return self.max_power_w
        return min(self.max_power_w, self.limit_w)

    @property
    def charging_state(self) -> str:
        return SUSPENDED_EVSE if self.power_w <= 0 else CHARGING

    def _next_seq(self) -> int:
        seq, self.seq_no = self.seq_no, self.seq_no + 1
        return seq

    # -- station-initiated messages ------------------------------------

    async def boot(self) -> tuple[str, int]:
        """Announce ourselves. Returns (status, interval)."""
        response = await self.call(
            call.BootNotification(
                charging_station={
                    "model": "PQCharge-Fake",
                    "vendor_name": "PQCharge",
                },
                reason="PowerUp",
            )
        )
        status = str(response.status)
        interval = int(getattr(response, "interval", 0) or 0)
        print(f"  [{self.id}] boot -> {status}, interval={interval}s")
        return status, interval

    async def send_status(self, status: str) -> None:
        await self.call(
            call.StatusNotification(
                timestamp=_now_iso(),
                connector_status=status,
                evse_id=1,
                connector_id=1,
            )
        )
        print(f"  [{self.id}] status -> {status}")

    async def authorize(self) -> bool:
        """Present the driver's token. False means do not charge."""
        response = await self.call(
            call.Authorize(id_token={"id_token": self.token, "type": "ISO14443"})
        )
        info = getattr(response, "id_token_info", {}) or {}
        status = info.get("status") if isinstance(info, dict) else str(info)
        accepted = str(status) == "Accepted"
        print(f"  [{self.id}] authorize {self.token} -> {status}")
        return accepted

    async def transaction_started(self) -> None:
        self.transaction_open = True
        await self.call(
            call.TransactionEvent(
                event_type="Started",
                timestamp=_now_iso(),
                trigger_reason="Authorized",
                seq_no=self._next_seq(),
                transaction_info={
                    "transaction_id": self.transaction_id,
                    "charging_state": self.charging_state,
                },
                evse={"id": 1, "connector_id": 1},
                id_token={"id_token": self.token, "type": "ISO14443"},
                meter_value=[_meter_value(self.power_w, self.energy_wh)],
            )
        )
        print(f"  [{self.id}] transaction started: {self.transaction_id}")

    async def transaction_updated(self, trigger: str = "MeterValuePeriodic") -> None:
        if not self.transaction_open:
            return
        await self.call(
            call.TransactionEvent(
                event_type="Updated",
                timestamp=_now_iso(),
                trigger_reason=trigger,
                seq_no=self._next_seq(),
                transaction_info={
                    "transaction_id": self.transaction_id,
                    "charging_state": self.charging_state,
                },
                evse={"id": 1, "connector_id": 1},
                meter_value=[_meter_value(self.power_w, self.energy_wh)],
            )
        )
        print(
            f"  [{self.id}] meter: {self.power_w:.0f}W "
            f"({self.charging_state}), {self.energy_wh:.1f}Wh"
        )

    async def transaction_ended(self, *, remote: bool = False) -> None:
        self.transaction_open = False
        info = {"transaction_id": self.transaction_id, "charging_state": IDLE}
        if remote:
            info["stopped_reason"] = "Remote"
        await self.call(
            call.TransactionEvent(
                event_type="Ended",
                timestamp=_now_iso(),
                trigger_reason="RemoteStop" if remote else "StopAuthorized",
                seq_no=self._next_seq(),
                transaction_info=info,
                evse={"id": 1, "connector_id": 1},
                meter_value=[_meter_value(0.0, self.energy_wh)],
            )
        )
        how = "stopped by the CSMS" if remote else "ended"
        print(f"  [{self.id}] transaction {how}: {self.energy_wh:.1f}Wh total")

    async def heartbeat_loop(self, interval_s: int) -> None:
        """Heartbeat forever at the server's interval. Cancelled on exit."""
        if interval_s <= 0:
            return
        while True:
            await asyncio.sleep(interval_s)
            await self.call(call.Heartbeat())
            print(f"  [{self.id}] heartbeat")

    def _report_soon(self, trigger: str) -> None:
        """
        Send a meter update from OUTSIDE the handler that caused it.

        A new power cap should be visible on /api/fleet at once, not at
        the next periodic reading. Scheduling it as its own task lets the
        handler return first, so the receive loop is free to read the
        CSMS's reply to this TransactionEvent.
        """
        if self.transaction_open:
            asyncio.ensure_future(self.transaction_updated(trigger))

    # -- CSMS-initiated commands (Day 8) --------------------------------

    @on("SetChargingProfile")
    async def on_set_charging_profile(
        self, evse_id: Any = None, charging_profile: Any = None, **kwargs: Any
    ):
        limit = _limit_from_profile(charging_profile)
        if limit is None:
            print(f"  [{self.id}] SetChargingProfile REJECTED: unreadable "
                  f"profile {charging_profile!r}")
            return call_result.SetChargingProfile(status="Rejected")
        self.limit_w = limit
        print(f"  [{self.id}] SetChargingProfile: cap {limit:.0f}W "
              f"-> now drawing {self.power_w:.0f}W")
        self._report_soon("ChargingRateChanged")
        return call_result.SetChargingProfile(status="Accepted")

    @on("ClearChargingProfile")
    async def on_clear_charging_profile(self, **kwargs: Any):
        if self.limit_w is None:
            # "Unknown" = no matching profile existed. Correct, not an error
            # -- dispatch.py's rule 4 scores it as success.
            print(f"  [{self.id}] ClearChargingProfile: nothing to clear")
            return call_result.ClearChargingProfile(status="Unknown")
        self.limit_w = None
        print(f"  [{self.id}] ClearChargingProfile: cap removed "
              f"-> drawing {self.power_w:.0f}W")
        self._report_soon("ChargingRateChanged")
        return call_result.ClearChargingProfile(status="Accepted")

    @on("RequestStopTransaction")
    async def on_request_stop_transaction(
        self, transaction_id: str | None = None, **kwargs: Any
    ):
        if not self.transaction_open or transaction_id != self.transaction_id:
            print(f"  [{self.id}] RequestStopTransaction REJECTED: "
                  f"no open transaction {transaction_id!r}")
            return call_result.RequestStopTransaction(status="Rejected")
        print(f"  [{self.id}] RequestStopTransaction accepted")
        self.stop_requested.set()
        return call_result.RequestStopTransaction(status="Accepted")

    @on("DataTransfer")
    async def on_data_transfer(self, vendor_id: str | None = None, **kwargs: Any):
        # No post-quantum identity here, on purpose: this fixture is the
        # "older charger" of the heterogeneous fleet. UnknownVendorId is the
        # standard answer a stock third-party client gives too (E6).
        print(f"  [{self.id}] DataTransfer from vendor {vendor_id!r}: "
              f"UnknownVendorId")
        return call_result.DataTransfer(status="UnknownVendorId")


def _client_tls(station_id: str, cert_dir: str):
    """
    This station's TLS material. Each station presents its OWN
    certificate -- certs/<id>.crt.pem -- because Security Profile 3
    authenticates the station, not the fleet.
    """
    directory = Path(cert_dir)
    return build_client_context(
        directory / f"{station_id}.crt.pem",
        directory / f"{station_id}.key.pem",
        directory / "root.pem",
    )


async def _charge(station: FakeStation, charge_for_s: float, meter_every_s: float) -> bool:
    """
    Charge until charge_for_s elapses (<= 0 means forever) or the CSMS
    sends RequestStopTransaction. Returns True if the CSMS stopped it.

    Energy is integrated from the power actually drawn in each interval,
    so a curtailed station's energy_wh bends when its cap changes --
    which is what the dashboard and E5's timeline should show.
    """
    forever = charge_for_s <= 0
    elapsed = 0.0
    loop = asyncio.get_running_loop()
    while forever or elapsed < charge_for_s:
        step = meter_every_s if forever else min(meter_every_s, charge_for_s - elapsed)
        started = loop.time()
        power_during = station.power_w
        try:
            await asyncio.wait_for(station.stop_requested.wait(), timeout=step)
        except asyncio.TimeoutError:
            pass
        dt = loop.time() - started
        elapsed += dt
        station.energy_wh += power_during * dt / 3600.0
        if station.stop_requested.is_set():
            return True
        await station.transaction_updated()
    return False


async def run_station(
    station_id: str,
    url: str,
    token: str,
    charge_for_s: float,
    meter_every_s: float,
    *,
    power_w: float = DEFAULT_POWER_W,
    tls: bool = False,
    cert_dir: str = DEFAULT_CERT_DIR,
    cert_as: str | None = None,
    server_name: str = DEFAULT_SERVER_NAME,
) -> None:
    """One station's whole life: connect, boot, charge, close."""
    uri = f"{url}/{station_id}"
    kwargs = {}
    if tls:
        kwargs["ssl"] = _client_tls(cert_as or station_id, cert_dir)
        kwargs["server_hostname"] = server_name

    async with connect(uri, subprotocols=["ocpp2.0.1"], **kwargs) as ws:
        station = FakeStation(station_id, ws, token, power_w=power_w)
        print(f"  [{station_id}] connected to {uri}")

        reader = asyncio.ensure_future(station.start())
        """The ocpp library's receive loop. It must be running before any
        call() is made, because call() awaits a response this loop reads."""

        beats = None
        try:
            status, interval = await station.boot()
            if status != "Accepted":
                # A charger may not start a transaction until the CSMS has
                # accepted it. Before Day 8 this fixture ignored a Rejected
                # boot and charged anyway (Track C found it).
                print(f"  [{station_id}] boot not accepted ({status}) — "
                      f"not charging")
                return
            beats = asyncio.ensure_future(station.heartbeat_loop(interval))

            await station.send_status(STATUS_AVAILABLE)

            if not await station.authorize():
                print(f"  [{station_id}] not authorised — no transaction")
                await asyncio.sleep(10.0 if charge_for_s <= 0 else min(charge_for_s, 10.0))
                return

            await station.send_status(STATUS_OCCUPIED)
            await station.transaction_started()

            stopped_remotely = await _charge(station, charge_for_s, meter_every_s)

            await station.transaction_ended(remote=stopped_remotely)
            await station.send_status(STATUS_AVAILABLE)
        finally:
            for task in (beats, reader):
                if task is not None:
                    task.cancel()

    print(f"  [{station_id}] disconnected")


async def _run_with_retries(sid: str, args) -> None:
    """
    One station, reconnected up to --retries times if its connection
    fails. A retry is a fresh session (new boot, new transaction) -- this
    is a load fixture, not the agent's offline queue.
    """
    attempt = 0
    while True:
        try:
            await run_station(
                sid,
                args.url,
                args.token,
                args.charge_for,
                args.meter_every,
                power_w=args.power,
                tls=args.tls,
                cert_dir=args.cert_dir,
                cert_as=args.cert_as,
                server_name=args.server_name,
            )
            return
        except Exception as exc:  # noqa: BLE001 - refused, reset, 1008 close...
            if attempt >= args.retries:
                raise
            attempt += 1
            reason = f"{type(exc).__name__}: {exc}"
        print(f"  [{sid}] connection failed ({reason}); "
              f"retry {attempt}/{args.retries} in {args.retry_delay}s")
        await asyncio.sleep(args.retry_delay)


async def main_async(args) -> int:
    """
    Run every station concurrently. One station failing does NOT take
    the others down (return_exceptions=True) -- at N = 50 a single
    refused connection used to abort the whole run, which made the fsync
    experiment impossible. Returns the number of stations that failed.
    """
    results = await asyncio.gather(
        *(_run_with_retries(sid, args) for sid in args.station_ids),
        return_exceptions=True,
    )
    failed = 0
    for sid, result in zip(args.station_ids, results):
        if isinstance(result, BaseException):
            failed += 1
            print(f"  [{sid}] FAILED: {type(result).__name__}: {result}")
    ok = len(args.station_ids) - failed
    print(f"done: {ok} station(s) completed, {failed} failed")
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description="fake station(s) for CSMS testing")
    parser.add_argument("station_ids", nargs="+")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN,
                        help="driver token to present; TAG-BLOCKED to see a "
                             "refusal, or any unlisted string for Invalid")
    parser.add_argument("--charge-for", type=float, default=40.0,
                        help="seconds to charge before ending the transaction; "
                             "0 = charge until the CSMS stops it or Ctrl-C")
    parser.add_argument("--meter-every", type=float, default=10.0,
                        help="seconds between TransactionEvent Updated")
    parser.add_argument("--power", type=float, default=DEFAULT_POWER_W,
                        help="watts the car draws when nothing limits it")
    parser.add_argument("--retries", type=int, default=0,
                        help="reconnect attempts per station if the "
                             "connection fails; 0 = fail immediately")
    parser.add_argument("--retry-delay", type=float, default=1.0,
                        help="seconds between reconnect attempts")
    parser.add_argument("--tls", action="store_true",
                        help="connect over wss:// with a client certificate")
    parser.add_argument("--cert-dir", default=DEFAULT_CERT_DIR,
                        help="directory bootstrap_pki wrote the PKI into")
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME,
                        help="name to verify the server certificate against; "
                             "must appear in its Subject Alternative Name")
    parser.add_argument("--cert-as", default=None,
                        help="present ANOTHER station's certificate while "
                             "connecting under this station's id, to test the "
                             "server's identity check")
    args = parser.parse_args()
    if args.power < 0 or not math.isfinite(args.power):
        parser.error("--power must be a non-negative number of watts")
    if args.retries < 0:
        parser.error("--retries must be 0 or more")
    if args.tls and args.url.startswith("ws://"):
        args.url = "wss://" + args.url[len("ws://"):]

    duration = "until stopped" if args.charge_for <= 0 else f"{args.charge_for:g}s"
    print(
        f"connecting {len(args.station_ids)} station(s), token={args.token}, "
        f"power={args.power:.0f}W, charging {duration}"
    )
    try:
        failed = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
