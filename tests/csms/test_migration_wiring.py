"""
Day 9 -- the wiring between the CSMS and Track B's orchestrator.

Everything here runs without a network. The last test runs Track B's REAL
orchestrator with REAL ML-DSA keys against the real registry and a fake
dispatcher; it is skipped where quantcrypt is not installed, so it never
reddens a teammate's suite.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from crypto.identity import MigrationState
from csms.migration import (
    DisabledController,
    PersistentPQAuthenticator,
    _expand_range,
    apply_fleet_profile,
    build_migration,
    load_fleet_profile,
    orchestrator_emitter,
)
from csms.persistence import SqliteStore
from csms.registry import SessionRegistry


# -- fleet profile ------------------------------------------------------


def test_range_expands_with_width_kept():
    assert _expand_range("CP0008-CP0011") == ["CP0008", "CP0009", "CP0010", "CP0011"]
    assert _expand_range("CP0100") == ["CP0100"]


@pytest.mark.parametrize("bad", ["CP0001-CP450", "CP0005-CP0001", "CP0001-XX0002"])
def test_bad_ranges_are_errors_not_different_fleets(bad):
    with pytest.raises(ValueError):
        _expand_range(bad)


def _write(tmp_path, text):
    path = tmp_path / "fleet.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_profile_loads_groups(tmp_path):
    path = _write(tmp_path, """{"groups": [
        {"stations": "CP0001-CP0003", "supported_algorithms": ["ECDSA-P256", "ML-DSA-44"]},
        {"stations": ["CP0004"], "supported_algorithms": ["ECDSA-P256"]}
    ]}""")
    profile = load_fleet_profile(path)
    assert sorted(profile) == ["CP0001", "CP0002", "CP0003", "CP0004"]
    assert profile["CP0002"] == ["ECDSA-P256", "ML-DSA-44"]
    assert profile["CP0004"] == ["ECDSA-P256"]


def test_profile_conflict_is_an_error(tmp_path):
    path = _write(tmp_path, """{"groups": [
        {"stations": "CP0001-CP0002", "supported_algorithms": ["ML-DSA-44"]},
        {"stations": "CP0002", "supported_algorithms": ["ECDSA-P256"]}
    ]}""")
    with pytest.raises(ValueError):
        load_fleet_profile(path)


@pytest.mark.parametrize("text", ["not json", "{}", '{"groups": [{"stations": "CP0001"}]}'])
def test_malformed_profile_is_an_error(tmp_path, text):
    with pytest.raises(ValueError):
        load_fleet_profile(_write(tmp_path, text))


def test_apply_profile_provisions_and_updates():
    registry = SessionRegistry()
    added, updated = apply_fleet_profile(registry, {"CP0001": ["ML-DSA-44"]})
    assert (added, updated) == (1, 0)
    assert registry.get_identity("CP0001").supported_algorithms == ["ML-DSA-44"]

    added, updated = apply_fleet_profile(registry, {"CP0001": ["ECDSA-P256"]})
    assert (added, updated) == (0, 1)
    assert registry.get_identity("CP0001").supported_algorithms == ["ECDSA-P256"]

    assert apply_fleet_profile(registry, {"CP0001": ["ECDSA-P256"]}) == (0, 0)


# -- enrolments survive a restart ----------------------------------------


class _FakeProvider:
    signature_algorithm = "ML-DSA-44"

    def verify(self, public_key, message, signature):
        return signature == b"signed:" + public_key + message


def test_enrolment_survives_a_restart(tmp_path):
    db = tmp_path / "pq.db"
    store = SqliteStore(db)
    auth = PersistentPQAuthenticator(_FakeProvider(), store, algorithm="ML-DSA-44")
    auth.enrol("CP0001", b"pk-1")
    auth.enrol("CP0002", b"pk-2")
    auth.unenrol("CP0002")
    store.close()                      # flushes, as a clean shutdown does

    store2 = SqliteStore(db)           # "restart"
    auth2 = PersistentPQAuthenticator(_FakeProvider(), store2, algorithm="ML-DSA-44")
    assert auth2.restore() == 1
    assert auth2.is_enrolled("CP0001")
    assert not auth2.is_enrolled("CP0002")

    # the restored key really verifies
    nonce = auth2.issue_challenge("CP0001")
    assert auth2.verify_response("CP0001", b"signed:" + b"pk-1" + nonce) is True
    store2.close()


def test_key_for_another_algorithm_is_not_restored(tmp_path):
    db = tmp_path / "pq.db"
    store = SqliteStore(db)
    PersistentPQAuthenticator(_FakeProvider(), store, algorithm="ML-DSA-65").enrol("CP0001", b"k")
    store.close()

    store2 = SqliteStore(db)
    auth = PersistentPQAuthenticator(_FakeProvider(), store2, algorithm="ML-DSA-44")
    assert auth.restore() == 0
    assert not auth.is_enrolled("CP0001")
    store2.close()


# -- events and the disabled path -----------------------------------------


class _FakeLog:
    def __init__(self):
        self.events = []

    def emit(self, event_type, station_id=None, **payload):
        self.events.append((event_type, station_id, payload))


def test_orchestrator_events_are_marked_and_shadow_safe():
    log = _FakeLog()
    emit = orchestrator_emitter(log)
    emit("wave_started", wave_id=0, is_canary=True, size=2)
    emit("wave_completed", wave_id=0, outcome="clash")   # would shadow emit()'s outcome
    (t1, sid1, p1), (t2, _, p2) = log.events
    assert (t1, sid1, p1["source"], p1["wave_id"]) == ("wave_started", None, "orchestrator", 0)
    assert "outcome" not in p2 and p2["payload_outcome"] == "clash"


def test_disabled_controller_reports_idle_and_says_why():
    controller = DisabledController("because")
    assert controller.get_migration_status().phase.value == "idle"
    with pytest.raises(NotImplementedError, match="because"):
        controller.start_migration(10, 2, "pqc")


def test_migration_off_never_loads_post_quantum_code():
    setup = build_migration(
        mode="off", registry=None, dispatcher=None, event_log=None, store=None
    )
    assert setup.enabled is False and isinstance(setup.controller, DisabledController)


# -- the real orchestrator, real ML-DSA, through our wiring ---------------


class _Result:
    def __init__(self, ok):
        self.ok = ok


class _FakeDispatcher:
    """Answers like a station: Accepted if it can do PQC, else refuses."""

    timeout_s = 1.0

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.sent = []

    async def send(self, station_id, request, *, timeout_s=None):
        self.sent.append((station_id, request.message_id))
        await asyncio.sleep(0)
        return _Result(ok=station_id not in self.refuse)


def _real_mldsa_unavailable() -> str | None:
    """Why real ML-DSA cannot run here, or None if it can.

    Importing quantcrypt is NOT enough: on an Intel Mac quantcrypt 1.0.0
    installs without its compiled binaries, imports fine, and only fails
    when a key is made. So the check makes one."""
    try:
        from crypto.pq import PQProvider
        PQProvider().generate_keypair()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


def test_missing_binaries_disable_migration_instead_of_crashing_later(monkeypatch):
    """The Day 9 Intel-Mac failure, reproduced: the provider constructs, but
    making a key fails. The server must start with migration DISABLED and
    say why -- not announce the orchestrator and crash in the first wave."""

    class _NoBinaries:
        signature_algorithm = "ML-DSA-44"

        def generate_keypair(self):
            raise ImportError("Failed to import clean binaries of the MLDSA_44 algorithm.")

    fake = types.ModuleType("crypto.pq")
    fake.PQProvider = _NoBinaries
    monkeypatch.setitem(sys.modules, "crypto.pq", fake)

    setup = build_migration(
        mode="auto", registry=None, dispatcher=None, event_log=None, store=None
    )
    assert setup.enabled is False
    assert isinstance(setup.controller, DisabledController)
    assert "binaries" in setup.reason and "qclib compile" in setup.reason


def test_real_orchestrator_through_the_wiring(tmp_path):
    why = _real_mldsa_unavailable()
    if why:
        pytest.skip(f"real ML-DSA unavailable on this machine ({why})")

    async def scenario():
        store = SqliteStore(tmp_path / "live.db")
        registry = SessionRegistry(store=store)
        apply_fleet_profile(registry, {
            "CP0001": ["ECDSA-P256", "ML-DSA-44"],
            "CP0002": ["ECDSA-P256", "ML-DSA-44"],
            "CP0003": ["ECDSA-P256"],                # legacy: skipped, not failed
        })
        log = _FakeLog()
        dispatcher = _FakeDispatcher()
        setup = build_migration(
            mode="auto", registry=registry, dispatcher=dispatcher,
            event_log=log, store=store,
        )
        assert setup.enabled, setup.reason
        registry.attach_migration_controller(setup.controller)

        setup.controller.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
        for _ in range(200):
            if setup.controller.get_migration_status().is_terminal:
                break
            await asyncio.sleep(0.01)
        return registry, setup, log, dispatcher, store

    registry, setup, log, dispatcher, store = asyncio.run(scenario())

    status = setup.controller.get_migration_status()
    assert status.phase.value == "completed"
    states = {sid: registry.get_identity(sid).migration_state for sid in ("CP0001", "CP0002", "CP0003")}
    assert states == {
        "CP0001": MigrationState.MIGRATED,
        "CP0002": MigrationState.MIGRATED,
        "CP0003": MigrationState.INCOMPATIBLE,
    }
    # the install message is Track C's DataTransfer, and the legacy station got none
    assert {sid for sid, _ in dispatcher.sent} == {"CP0001", "CP0002"}
    assert {msg for _, msg in dispatcher.sent} == {"InstallPQAuth"}
    # exactly ONE migration_started -- the server no longer adds its own
    assert [e[0] for e in log.events].count("migration_started") == 1
    # keys reached the database
    store.flush()
    assert set(store.load_enrolments()) == {"CP0001", "CP0002"}
    store.close()
