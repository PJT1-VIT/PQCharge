"""
Throwaway station client, for Track A's own testing.

Track C owns the real agent (agent/client.py) and their own test double
of the CSMS (tests/fixtures/fake_csms.py). This is the mirror image and
it is Track A's: something that connects *to* the CSMS so the server can
be exercised before agent/client.py exists. It is dev/test only and is
never imported by shipped code.

Deliberately dumb. It opens a connection, holds it, and closes. Phase A1
is about the connection lifecycle and the registry, so anything that
sent OCPP messages would be testing Days 4-5 work that does not exist
yet.

Usage:
    python tests/fixtures/fake_station.py CP001
    python tests/fixtures/fake_station.py CP001 --hold 30
    python tests/fixtures/fake_station.py CP001 CP002 CP003 --hold 20
"""

from __future__ import annotations

import argparse
import asyncio

from websockets.asyncio.client import connect

DEFAULT_URL = "ws://localhost:9000"


async def hold_open(station_id: str, url: str, hold_s: float) -> None:
    """Connect as one station, stay connected, then close cleanly."""
    uri = f"{url}/{station_id}"
    async with connect(uri, subprotocols=["ocpp2.0.1"]) as ws:
        print(f"  [{station_id}] connected to {uri}")
        await asyncio.sleep(hold_s)
    print(f"  [{station_id}] disconnected")


async def main_async(station_ids: list[str], url: str, hold_s: float) -> None:
    await asyncio.gather(
        *(hold_open(sid, url, hold_s) for sid in station_ids)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="fake station(s) for CSMS testing")
    parser.add_argument("station_ids", nargs="+")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--hold", type=float, default=10.0,
                        help="seconds to stay connected")
    args = parser.parse_args()

    print(f"connecting {len(args.station_ids)} station(s), holding {args.hold}s")
    try:
        asyncio.run(main_async(args.station_ids, args.url, args.hold))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
