"""
Stage 0 smoke test.

Proves the toolchain end to end: the ocpp library, the websockets
transport, and Python's asyncio all work together on this machine.
One BootNotification crosses a real WebSocket; the CSMS answers; the
station reads the answer.

Run it, see "SMOKE TEST PASSED", and Stage 0 is cleared. This file is
throwaway scaffolding -- the real CSMS and agent replace it from Day 3.

Usage:
    python experiments/smoke_test.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import websockets
from ocpp.routing import on
from ocpp.v201 import ChargePoint as CpBase
from ocpp.v201 import call, call_result
from ocpp.v201.enums import RegistrationStatusEnumType

PORT = 9000
STATION_ID = "SMOKE001"


class CentralSystem(CpBase):
    """Minimal CSMS side: answers a BootNotification."""

    @on("BootNotification")
    async def on_boot(self, charging_station, reason, **kwargs):
        print(f"  [server] BootNotification from "
              f"{charging_station['model']} / {charging_station['vendor_name']}")
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=10,
            status=RegistrationStatusEnumType.accepted,
        )


class Station(CpBase):
    """Minimal station side: sends a BootNotification."""

    async def send_boot(self):
        request = call.BootNotification(
            charging_station={"model": "SmokeModel", "vendor_name": "PQCharge"},
            reason="PowerUp",
        )
        print("  [station] sending BootNotification")
        response = await self.call(request)
        print(f"  [station] response status: {response.status}")
        return response.status


async def on_connect(websocket):
    """Server-side handler for an incoming station connection."""
    path = getattr(getattr(websocket, "request", None), "path", "/") or "/"
    station_id = path.strip("/") or STATION_ID
    cp = CentralSystem(station_id, websocket)
    try:
        await cp.start()
    except websockets.exceptions.ConnectionClosed:
        pass  # client hung up normally — expected in this smoke test


async def run_server():
    return await websockets.serve(
        on_connect, "localhost", PORT, subprotocols=["ocpp2.0.1"]
    )


async def run_station():
    uri = f"ws://localhost:{PORT}/{STATION_ID}"
    async with websockets.connect(uri, subprotocols=["ocpp2.0.1"]) as ws:
        station = Station(STATION_ID, ws)
        task = asyncio.ensure_future(station.start())
        status = await station.send_boot()
        task.cancel()
        return status


async def main():
    print("Stage 0 smoke test")
    print("-" * 40)
    server = await run_server()
    print(f"  [server] listening on ws://localhost:{PORT}")
    try:
        status = await run_station()
    finally:
        server.close()
        await server.wait_closed()
    print("-" * 40)
    if str(status) == "RegistrationStatusEnumType.accepted" or "accepted" in str(status).lower():
        print("SMOKE TEST PASSED")
    else:
        print(f"SMOKE TEST FAILED — unexpected status: {status}")


if __name__ == "__main__":
    asyncio.run(main())