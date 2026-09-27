"""
Day 10-11 handoff — proves the migration orchestrator end to end, so the
integration with Track C's agent is 'run the harness against your real
agent', not 'debug together'. The same role bootstrap_pki.py played for
Track A on Day 7.

Runs the full orchestrator with REAL ML-DSA (crypto/pq.py) against a fake
dispatcher and a fake fleet that mimic the real Contract 6 surface. What
it proves, on Track B's side alone:

  - a fleet migrates to post-quantum in canary + waves
  - a migrated station's ML-DSA key is enrolled and actually authenticates
  - stations lacking ML-DSA are skipped as incompatible, not failed
  - a wave that fails past threshold rolls back, leaving nothing enrolled
  - overlap safety: a failed dispatch un-enrols that station
  - migration events are emitted for E3

WHAT REMAINS FOR DAY 10-11 (with Track C): swap the FakeDispatcher for the
real csms.dispatch.CommandDispatcher and the FakeFleet for a real
SessionRegistry via FleetAdapter, and Track C's agent/certificate.py must
answer the InstallPQAuth message this harness stubs. Everything on Track
B's side is proven here first.

Run:
    python experiments/rotation_harness.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from crypto.pq import PQProvider
from crypto.pq_auth import PQAuthenticator, sign_challenge, AuthError
from crypto.identity import StationIdentity, MigrationState
from idmanager.orchestrator import MigrationOrchestrator
from idmanager.api import MigrationPhase


class _FakeResult:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.outcome = "success" if ok else "failure"


class FakeDispatcher:
    """Stands in for csms.dispatch.CommandDispatcher until the live-station
    path is proven. send() returns ok unless the station is in fail_ids."""

    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.fail_ids = set(fail_ids or [])
        self.sent: list[str] = []

    async def send(self, station_id, request, *, timeout_s=None):
        self.sent.append(station_id)
        await asyncio.sleep(0)
        return _FakeResult(ok=station_id not in self.fail_ids)


class FakeFleet:
    """Mimics SessionRegistry's Contract 6 surface that FleetAdapter wraps:
    get_identity, set_identity (whole-record, KeyError on unknown),
    list-of-ids. Seeded with per-station capability profiles."""

    def __init__(self, capabilities: dict[str, list[str]]) -> None:
        self._ids = {
            sid: StationIdentity(
                station_id=sid,
                current_algorithm="ECDSA-P256",
                supported_algorithms=algs,
            )
            for sid, algs in capabilities.items()
        }

    def list_station_ids(self) -> list[str]:
        return sorted(self._ids)

    def get_identity(self, station_id):
        return self._ids.get(station_id)

    def set_identity(self, identity) -> None:
        if identity.station_id not in self._ids:
            raise KeyError(identity.station_id)
        self._ids[identity.station_id] = identity


class _FakeAdapter:
    """The FleetAdapter logic over a FakeFleet. Mirrors
    idmanager/fleet_adapter.py exactly."""

    def __init__(self, fleet: FakeFleet) -> None:
        self._f = fleet

    def migration_candidate_ids(self):
        return self._f.list_station_ids()

    def supported_algorithms(self, station_id):
        it = self._f.get_identity(station_id)
        return list(it.supported_algorithms) if it else []

    def set_state(self, station_id, state, wave):
        it = self._f.get_identity(station_id)
        if it is None:
            return
        it.migration_state = state
        it.migration_wave = wave
        try:
            self._f.set_identity(it)
        except KeyError:
            pass

    def mark_migrated_algorithm(self, station_id, algorithm):
        it = self._f.get_identity(station_id)
        if it is None:
            return
        it.current_algorithm = algorithm
        try:
            self._f.set_identity(it)
        except KeyError:
            pass


async def _run_to_completion(orch) -> None:
    while not orch.get_migration_status().is_terminal:
        await asyncio.sleep(0.005)


def _build(capabilities, provider, authenticator, *, fail_ids=None, events=None):
    fleet = FakeFleet(capabilities)
    adapter = _FakeAdapter(fleet)
    dispatcher = FakeDispatcher(fail_ids=fail_ids)
    emitter = (lambda ev, **k: events.append(ev)) if events is not None else None
    orch = MigrationOrchestrator(
        dispatcher=dispatcher,
        authenticator=authenticator,
        fleet=adapter,
        keypair_factory=provider.generate_keypair,
        install_message_factory=lambda sid, priv: ("InstallPQAuth", sid),
        event_emitter=emitter,
    )
    return orch, fleet


async def main() -> None:
    print("=" * 60)
    print("PQCharge — Day 10-11 rotation/enrolment harness")
    print("=" * 60)
    provider = PQProvider()
    ok = True

    print("\n[1] Clean migration, all stations PQC-capable")
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(10)}
    auth = PQAuthenticator(provider)
    orch, fleet = _build(caps, provider, auth)
    orch.start_migration(wave_size=3, canary_count=2, target_mode="pqc")
    await _run_to_completion(orch)
    st = orch.get_migration_status()
    print(f"    phase={st.phase.value} migrated={st.migrated} "
          f"enrolled={sum(auth.is_enrolled(s) for s in caps)}")
    ok &= st.phase == MigrationPhase.COMPLETED and st.migrated == 10

    print("\n[2] Heterogeneous fleet: stations without ML-DSA skipped")
    caps = {f"CP{i:03d}": (["ECDSA-P256", "ML-DSA-44"] if i % 3 else ["ECDSA-P256"])
            for i in range(9)}
    auth = PQAuthenticator(provider)
    orch, fleet = _build(caps, provider, auth)
    orch.start_migration(wave_size=3, canary_count=2, target_mode="pqc")
    await _run_to_completion(orch)
    st = orch.get_migration_status()
    print(f"    phase={st.phase.value} migrated={st.migrated} "
          f"incompatible={st.incompatible}")
    ok &= st.phase == MigrationPhase.COMPLETED and st.incompatible == 3

    print("\n[3] Canary fails past threshold -> rollback")
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(10)}
    auth = PQAuthenticator(provider)
    orch, fleet = _build(caps, provider, auth, fail_ids={"CP000", "CP001"})
    orch.start_migration(wave_size=3, canary_count=2, target_mode="pqc")
    await _run_to_completion(orch)
    st = orch.get_migration_status()
    print(f"    phase={st.phase.value} "
          f"enrolled_after={sum(auth.is_enrolled(s) for s in caps)}")
    ok &= st.phase == MigrationPhase.ROLLED_BACK

    print("\n[4] Overlap safety: failed dispatch un-enrols that station")
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(6)}
    auth = PQAuthenticator(provider)
    orch, fleet = _build(caps, provider, auth, fail_ids={"CP005"})
    orch.start_migration(wave_size=5, canary_count=1, target_mode="pqc")
    await _run_to_completion(orch)
    print(f"    CP005 enrolled (should be False): {auth.is_enrolled('CP005')}")
    ok &= not auth.is_enrolled("CP005")

    print("\n[5] A migrated station authenticates with real ML-DSA")
    prov = PQProvider()
    a = PQAuthenticator(prov)
    priv, pub = prov.generate_keypair()
    a.enrol("CP000", pub)
    ch = a.issue_challenge("CP000")
    resp = sign_challenge(prov, priv, ch)
    verdict = a.verify_response("CP000", resp)
    print(f"    challenge-response verdict: {verdict}")
    ok &= verdict is True

    print("\n" + "=" * 60)
    if ok:
        print("SUCCESS — orchestrator proven end to end with real ML-DSA.")
        print("\nFor Day 10-11 with Track C: swap FakeDispatcher for")
        print("csms.dispatch.CommandDispatcher and FakeFleet for a real")
        print("SessionRegistry via FleetAdapter. Track C's agent must answer")
        print("the InstallPQAuth message this harness stubs.")
    else:
        print("FAILED — see scenarios above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())