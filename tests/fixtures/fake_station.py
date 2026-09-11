"""
Throwaway station client, for Track A's own testing.

Track C owns the real agent (agent/client.py) and their own test double
of the CSMS (tests/fixtures/fake_csms.py). This is the mirror image and
it is Track A's: something that connects *to* the CSMS so the server can
be exercised before agent/client.py exists. Dev/test only, never
imported by shipped code.

Phase A2 behaviour, per station:
    connect -> BootNotification -> StatusNotification("Available")
            -> Heartbeat at the interval the SERVER issued
            -> optional status change -> disconnect

The heartbeat interval is taken from the BootNotification response, not
from a constant here. That is the point of the test: it proves the
server's interval actually reaches the station and is obeyed, rather
than both sides happening to agree on a hardcoded number.

Usage:
    python tests/fixtures/fake_station.py CP001
    python tests/fixtures/fake_station.py CP001 CP002 CP003 --hold 60
    python tests/fixtures/fake_station.py CP001 --hold 60 --occupy-after 10
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from websockets.asyncio.client import connect

from ocpp.v201 import ChargePoint as CpBase
from ocpp.v201 import call

DEFAULT_URL = "ws://localhost:9000"

STATUS_AVAILABLE = "Available"
STATUS_OCCUPIED = "Occupied"
"""Sent as plain strings, which is what the OCPP 2.0.1 schema expects.

Deliberately not imported from ocpp.v201.enums: the enum class name has
moved between releases of the library, and this fixture should not be
the thing that breaks when someone upgrades it. The server stores
whatever string arrives, so the wire value is the contract.
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeStation(CpBase):
    """Minimal station: boots, reports status, heartbeats."""

    async def boot(self) -> int:
        """
        Announce ourselves and adopt the interval the server hands back.

        Returns:
            The heartbeat interval in seconds, as issued by the CSMS.
        """
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

    async def heartbeat_loop(self, interval_s: int) -> None:
        """Heartbeat forever at the server's interval. Cancelled on exit."""
        if interval_s <= 0:
            return
        while True:
            await asyncio.sleep(interval_s)
            await self.call(call.Heartbeat())
            print(f"  [{self.id}] heartbeat")


async def run_station(
    station_id: str, url: str, hold_s: float, occupy_after: float | None
) -> None:
    """One station's whole life: connect, boot, report, heartbeat, close."""
    uri = f"{url}/{station_id}"
    async with connect(uri, subprotocols=["ocpp2.0.1"]) as ws:
        station = FakeStation(station_id, ws)
        print(f"  [{station_id}] connected to {uri}")

        reader = asyncio.ensure_future(station.start())
        """The ocpp library's receive loop. It must be running before
        any call() is made, because call() awaits a response that this
        loop is what actually reads off the socket."""

        beats = None
        try:
            interval = await station.boot()
            await station.send_status(STATUS_AVAILABLE)
            beats = asyncio.ensure_future(station.heartbeat_loop(interval))

            if occupy_after is not None and occupy_after < hold_s:
                await asyncio.sleep(occupy_after)
                await station.send_status(STATUS_OCCUPIED)
                await asyncio.sleep(hold_s - occupy_after)
            else:
                await asyncio.sleep(hold_s)
        finally:
            for task in (beats, reader):
                if task is not None:
                    task.cancel()

    print(f"  [{station_id}] disconnected")


async def main_async(
    station_ids: list[str], url: str, hold_s: float, occupy_after: float | None
) -> None:
    await asyncio.gather(
        *(run_station(sid, url, hold_s, occupy_after) for sid in station_ids)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="fake station(s) for CSMS testing")
    parser.add_argument("station_ids", nargs="+")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--hold", type=float, default=60.0,
                        help="seconds to stay connected")
    parser.add_argument("--occupy-after", type=float, default=None,
                        help="seconds after boot to report Occupied, "
                             "to exercise the STATE_CHANGED path")
    args = parser.parse_args()

    print(f"connecting {len(args.station_ids)} station(s), holding {args.hold}s")
    try:
        asyncio.run(
            main_async(args.station_ids, args.url, args.hold, args.occupy_after)
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
