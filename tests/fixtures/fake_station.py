"""
Throwaway station client, for Track A's own testing.

Track C owns the real agent (agent/client.py) and their own test double
of the CSMS (tests/fixtures/fake_csms.py). This is the mirror image and
it is Track A's: something that connects *to* the CSMS so the server can
be exercised before agent/client.py exists. Dev/test only, never
imported by shipped code.

Phase A3 behaviour — a full charging session, which is the Stage 1 gate:

    connect
      -> BootNotification            (adopts the server's interval)
      -> StatusNotification Available
      -> Authorize                   (a driver taps a card)
      -> StatusNotification Occupied
      -> TransactionEvent Started    seq 0
      -> TransactionEvent Updated    seq 1..n, with meter values
      -> TransactionEvent Ended      seq n+1
      -> StatusNotification Available
    disconnect

Heartbeats run throughout at the interval the SERVER issued.

The station stops after Authorize if the CSMS refuses the token, which
is the behaviour a real charger has and what makes --token TAG-BLOCKED
a useful demonstration rather than a crash.

Usage:
    python tests/fixtures/fake_station.py CP001
    python tests/fixtures/fake_station.py CP001 CP002 CP003 --hold 90
    python tests/fixtures/fake_station.py CP001 --token TAG-BLOCKED
    python tests/fixtures/fake_station.py CP001 --charge-for 40 --meter-every 5
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import datetime, timezone

from websockets.asyncio.client import connect

from ocpp.v201 import ChargePoint as CpBase
from ocpp.v201 import call

DEFAULT_URL = "ws://localhost:9000"
DEFAULT_TOKEN = "TAG-0001"

STATUS_AVAILABLE = "Available"
STATUS_OCCUPIED = "Occupied"
CHARGING = "Charging"
IDLE = "Idle"
"""Wire values from OCPP 2.0.1, sent as plain strings.

Deliberately not imported from ocpp.v201.enums: the enum CLASS names
have moved between releases of that library, and this fixture should
not be the thing that breaks when someone upgrades it. The wire values
are fixed by the specification and the library's schema validator
rejects a wrong one, so a typo fails loudly on the first message.
"""

POWER_W = 7400.0
"""7.4 kW, matching agent/simulated_power.py's DEFAULT_LIMIT_W so that
Track A's fixture and Track C's agent report comparable figures."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meter_value(power_w: float, energy_wh: float) -> dict:
    """
    One OCPP MeterValue carrying instantaneous power and cumulative
    energy.

    The measurand strings here are the ones csms/metering.py
    recognises. If Track C's agent sends different ones, the server
    logs an 'unrecognised measurand' warning naming them rather than
    silently reporting zero power -- which is the whole point of that
    module.
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


class FakeStation(CpBase):
    """Minimal station: boots, authorises, charges, reports, heartbeats."""

    def __init__(self, station_id: str, connection, token: str) -> None:
        super().__init__(station_id, connection)
        self.token = token
        self.transaction_id = uuid.uuid4().hex[:12]
        self.seq_no = 0
        self.energy_wh = 0.0

    def _next_seq(self) -> int:
        seq, self.seq_no = self.seq_no, self.seq_no + 1
        return seq

    async def boot(self) -> int:
        """Announce ourselves and adopt the interval the server issues."""
        response = await self.call(
            call.BootNotification(
                charging_station={
                    "model": "PQCharge-Fake",
                    "vendor_name": "PQCharge",
                },
                reason="PowerUp",
            )
        )
        interval = int(getattr(response, "interval", 0) or 0)
        print(f"  [{self.id}] boot -> {response.status}, interval={interval}s")
        return interval

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
        await self.call(
            call.TransactionEvent(
                event_type="Started",
                timestamp=_now_iso(),
                trigger_reason="Authorized",
                seq_no=self._next_seq(),
                transaction_info={
                    "transaction_id": self.transaction_id,
                    "charging_state": CHARGING,
                },
                evse={"id": 1, "connector_id": 1},
                id_token={"id_token": self.token, "type": "ISO14443"},
                meter_value=[_meter_value(POWER_W, self.energy_wh)],
            )
        )
        print(f"  [{self.id}] transaction started: {self.transaction_id}")

    async def transaction_updated(self, elapsed_s: float) -> None:
        self.energy_wh = POWER_W * (elapsed_s / 3600.0)
        await self.call(
            call.TransactionEvent(
                event_type="Updated",
                timestamp=_now_iso(),
                trigger_reason="MeterValuePeriodic",
                seq_no=self._next_seq(),
                transaction_info={
                    "transaction_id": self.transaction_id,
                    "charging_state": CHARGING,
                },
                evse={"id": 1, "connector_id": 1},
                meter_value=[_meter_value(POWER_W, self.energy_wh)],
            )
        )
        print(f"  [{self.id}] meter: {POWER_W}W, {self.energy_wh:.1f}Wh")

    async def transaction_ended(self) -> None:
        await self.call(
            call.TransactionEvent(
                event_type="Ended",
                timestamp=_now_iso(),
                trigger_reason="StopAuthorized",
                seq_no=self._next_seq(),
                transaction_info={
                    "transaction_id": self.transaction_id,
                    "charging_state": IDLE,
                },
                evse={"id": 1, "connector_id": 1},
                meter_value=[_meter_value(0.0, self.energy_wh)],
            )
        )
        print(f"  [{self.id}] transaction ended: {self.energy_wh:.1f}Wh total")

    async def heartbeat_loop(self, interval_s: int) -> None:
        """Heartbeat forever at the server's interval. Cancelled on exit."""
        if interval_s <= 0:
            return
        while True:
            await asyncio.sleep(interval_s)
            await self.call(call.Heartbeat())
            print(f"  [{self.id}] heartbeat")


async def run_station(
    station_id: str,
    url: str,
    token: str,
    charge_for_s: float,
    meter_every_s: float,
) -> None:
    """One station's whole life: connect, boot, charge, close."""
    uri = f"{url}/{station_id}"
    async with connect(uri, subprotocols=["ocpp2.0.1"]) as ws:
        station = FakeStation(station_id, ws, token)
        print(f"  [{station_id}] connected to {uri}")

        reader = asyncio.ensure_future(station.start())
        """The ocpp library's receive loop. It must be running before
        any call() is made, because call() awaits a response that this
        loop is what actually reads off the socket."""

        beats = None
        try:
            interval = await station.boot()
            beats = asyncio.ensure_future(station.heartbeat_loop(interval))

            await station.send_status(STATUS_AVAILABLE)

            if not await station.authorize():
                print(f"  [{station_id}] not authorised — no transaction")
                await asyncio.sleep(min(charge_for_s, 10.0))
                return

            await station.send_status(STATUS_OCCUPIED)
            await station.transaction_started()

            elapsed = 0.0
            while elapsed < charge_for_s:
                await asyncio.sleep(min(meter_every_s, charge_for_s - elapsed))
                elapsed += meter_every_s
                await station.transaction_updated(elapsed)

            await station.transaction_ended()
            await station.send_status(STATUS_AVAILABLE)
        finally:
            for task in (beats, reader):
                if task is not None:
                    task.cancel()

    print(f"  [{station_id}] disconnected")


async def main_async(args) -> None:
    await asyncio.gather(
        *(
            run_station(
                sid, args.url, args.token, args.charge_for, args.meter_every
            )
            for sid in args.station_ids
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="fake station(s) for CSMS testing")
    parser.add_argument("station_ids", nargs="+")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN,
                        help="driver token to present; TAG-BLOCKED to see a "
                             "refusal, or any unlisted string for Invalid")
    parser.add_argument("--charge-for", type=float, default=40.0,
                        help="seconds to charge before ending the transaction")
    parser.add_argument("--meter-every", type=float, default=10.0,
                        help="seconds between TransactionEvent Updated")
    args = parser.parse_args()

    print(
        f"connecting {len(args.station_ids)} station(s), "
        f"token={args.token}, charging {args.charge_for}s"
    )
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
