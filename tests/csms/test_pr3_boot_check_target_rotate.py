"""
Track A PR 3:
  A-F1 (A-P3)  the boot key check switched on in --mode hybrid (Contract 7 7.5)
  A-F2 (A-P7)  /api/migration/start takes target_mode=hybrid; pqc refused (L28)
  A-F5 (A-P8)  /api/migration/rotate starts Track B's key rotation (B-F2)
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
from websockets.asyncio.server import serve

from csms.migration import (
    BOOT_CHECK_FAILED,
    PQC_NOT_BUILT,
    MigrationSetup,
    install_boot_check,
)
from csms.registry import SessionRegistry
from csms.server import CSMS, SUBPROTOCOLS


def _csms(tmp_path, **kwargs):
    kwargs.setdefault("migration_mode", "off")
    return CSMS(host="127.0.0.1", port=0, log_path=str(tmp_path / "e.jsonl"),
                db_path=None, ws_ping_interval=None, **kwargs)


def _get(csms, path_and_query):
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(path_and_query)
    response = csms._route(parsed.path, parse_qs(parsed.query))
    return response.status_code, json.loads(response.body)


# =====================================================================
# A-F2: target_mode=hybrid
# =====================================================================

def test_start_accepts_hybrid_and_refuses_pqc(tmp_path):
    csms = _csms(tmp_path)          # migration off -> DisabledController
    code, body = _get(csms, "/api/migration/start?wave_size=2&canary_count=1&target_mode=pqc")
    assert code == 400 and body["error"] == PQC_NOT_BUILT
    # hybrid passes validation and reaches the controller (501: migration off)
    code, body = _get(csms, "/api/migration/start?wave_size=2&canary_count=1&target_mode=hybrid")
    assert code == 501
    code, _ = _get(csms, "/api/migration/start?wave_size=2&canary_count=1&target_mode=classical")
    assert code == 400
    csms.log.close()


# =====================================================================
# A-F5: /api/migration/rotate
# =====================================================================

class _Controller:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def start_rotation(self, wave_size, canary_count):
        self.calls.append((wave_size, canary_count))
        if self.error:
            raise self.error
        return "rot-1"


def test_rotate_endpoint(tmp_path):
    csms = _csms(tmp_path)
    assert _get(csms, "/api/migration/rotate")[0] == 400                    # params missing
    assert _get(csms, "/api/migration/rotate?wave_size=2&canary_count=1")[0] == 501  # off

    csms.controller = _Controller()
    code, body = _get(csms, "/api/migration/rotate?wave_size=3&canary_count=1")
    assert (code, body) == (200, {"migration_id": "rot-1"})
    assert csms.controller.calls == [(3, 1)]

    csms.controller = _Controller(RuntimeError("a migration or rotation is already in progress"))
    assert _get(csms, "/api/migration/rotate?wave_size=3&canary_count=1")[0] == 409
    csms.log.close()


# =====================================================================
# A-F1: refuse to start hybrid without migration
# =====================================================================

def test_hybrid_without_migration_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError, match="--mode hybrid needs post-quantum migration"):
        _csms(tmp_path, crypto_mode="hybrid", migration_mode="off")


def test_classical_mode_registers_no_boot_check(tmp_path):
    csms = _csms(tmp_path, crypto_mode="classical")
    assert csms.boot_verifier is None and csms.registry._boot_listeners == []
    csms.log.close()


# =====================================================================
# A-F1: the listener -- pass, fail + close, and L36 (stale result)
# =====================================================================

class _WebSocket:
    def __init__(self):
        self.closed_with = None

    async def close(self, code, reason=""):
        self.closed_with = (code, reason)


class _ChargePoint:
    def __init__(self):
        self._connection = _WebSocket()


class _Log:
    def __init__(self):
        self.events = []

    def emit(self, event_type, station_id=None, **kw):
        self.events.append((getattr(event_type, "value", event_type), station_id, kw))


class _Dispatcher:
    timeout_s = 1.0


def _wired(monkeypatch, verdicts):
    """A registry with the boot check installed; verify_on_boot is replaced by
    a fake that awaits `verdicts[station]` (a Future) so a test can decide
    WHEN the check ends."""
    from idmanager import boot_verifier

    async def fake_verify(self, station_id):
        return await verdicts[station_id]

    monkeypatch.setattr(boot_verifier.BootVerifier, "verify_on_boot", fake_verify)
    log = _Log()
    registry = SessionRegistry(event_log=log, crypto_mode="hybrid")
    setup = MigrationSetup(controller=None, enabled=True, reason="test",
                           authenticator=object(), algorithm="ML-DSA-44")
    install_boot_check(setup, registry=registry, dispatcher=_Dispatcher(), event_log=log)
    return registry, log


def _boot(registry, sid):
    cp = _ChargePoint()
    session = registry.register(sid, cp)
    registry.mark_boot_accepted(sid)
    return cp, session, registry.notify_boot(sid, cp)


def test_passed_boot_check_marks_that_connection(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        verdicts = {"CP0001": loop.create_future()}
        registry, _log = _wired(monkeypatch, verdicts)
        cp, session, tasks = _boot(registry, "CP0001")
        verdicts["CP0001"].set_result(True)
        await asyncio.gather(*tasks)
        return session, cp

    session, cp = asyncio.run(scenario())
    assert session.pq_verified is True and cp._connection.closed_with is None


def test_failed_boot_check_closes_1008_with_one_logged_line(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        verdicts = {"CP0001": loop.create_future()}
        registry, log = _wired(monkeypatch, verdicts)
        cp, session, tasks = _boot(registry, "CP0001")
        verdicts["CP0001"].set_result(False)
        await asyncio.gather(*tasks)
        registry.deregister("CP0001", cp)      # what on_connect's finally does
        return session, cp, log

    session, cp, log = asyncio.run(scenario())
    assert cp._connection.closed_with == (1008, BOOT_CHECK_FAILED)
    assert session.pq_verified is False
    closes = [e for e in log.events if e[0] == "connection_closed"]
    assert len(closes) == 1
    (_t, sid, payload), = closes
    assert sid == "CP0001" and payload["reason"] == "pq_auth_failed"
    assert payload["outcome"].value == "rejected"


@pytest.mark.parametrize("verdict", [True, False])
def test_late_verdict_never_touches_the_new_connection(monkeypatch, verdict):
    """L36: the charger reconnects while its boot check is still running."""
    async def scenario():
        loop = asyncio.get_running_loop()
        verdicts = {"CP0001": loop.create_future()}
        registry, log = _wired(monkeypatch, verdicts)
        old_cp, _old, tasks = _boot(registry, "CP0001")
        new_cp = _ChargePoint()
        new_session = registry.register("CP0001", new_cp)      # reconnected
        verdicts["CP0001"].set_result(verdict)                 # old check ends
        await asyncio.gather(*tasks)
        return old_cp, new_cp, new_session

    old_cp, new_cp, new_session = asyncio.run(scenario())
    assert new_session.pq_verified is False            # True did not leak over
    assert new_cp._connection.closed_with is None      # False did not close it
    assert old_cp._connection.closed_with is None      # old one is already gone


# =====================================================================
# A-F1 live: real server, real BootVerifier, real ML-DSA, a charger with
# the wrong key -> refused and closed 1008
# =====================================================================

def _real_mldsa_unavailable():
    try:
        from crypto.pq import PQProvider
        PQProvider().generate_keypair()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_live_enrolled_charger_without_its_key_is_closed_1008(tmp_path):
    why = _real_mldsa_unavailable()
    if why:
        pytest.skip(f"real ML-DSA unavailable here ({why})")
    from crypto.pq import PQProvider
    from tests.fixtures.fake_station import run_station

    port = _free_port()
    log_path = tmp_path / "e.jsonl"

    async def scenario():
        csms = CSMS(host="127.0.0.1", port=port, log_path=str(log_path), db_path=None,
                    ws_ping_interval=None, crypto_mode="hybrid", migration_mode="auto")
        # CP0001 is enrolled with a key it does not hold (the E5 impostor):
        # the fake station answers the challenge with UnknownVendorId.
        _priv, pub = PQProvider().generate_keypair()
        csms.migration.authenticator.enrol("CP0001", pub)
        closed_codes = []

        async def station():
            try:
                await run_station("CP0001", f"ws://127.0.0.1:{port}", "TAG-0001",
                                  charge_for_s=30.0, meter_every_s=1.0)
            except Exception as exc:  # noqa: BLE001 - the close is expected
                closed_codes.append(str(exc))

        async with serve(csms.on_connect, "127.0.0.1", port,
                         subprotocols=SUBPROTOCOLS, process_request=csms.process_request):
            task = asyncio.ensure_future(station())
            # Wait for the SERVER's side: the session is gone once the 1008
            # close has been logged. (The fake station itself may sit in an
            # unanswered call for its own 30 s timeout, so it is not awaited.)
            for _ in range(200):
                await asyncio.sleep(0.05)
                if csms.registry.get_session("CP0001") is None and \
                        "CP0001" in csms.registry._ever_connected:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        csms.log.close()
        return closed_codes

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    checks = [e["payload"] for e in events if e["event_type"] == "connection_attempt"
              and e["payload"].get("transition") == "pq_auth"]
    assert [(c["trigger"], c["result"], c["source"]) for c in checks] == [
        ("boot", "rejected", "boot_verifier")]
    closes = [e for e in events if e["event_type"] == "connection_closed"]
    assert len(closes) == 1 and closes[0]["payload"]["reason"] == "pq_auth_failed"
    assert closes[0]["station_id"] == "CP0001"
