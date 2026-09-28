"""
Wiring Track B's migration orchestrator into the live CSMS.

Track A (csms). Day 9.

--------------------------------------------------------------------
WHAT THIS FILE IS

Track B built the migration engine (idmanager/orchestrator.py) and proved
it against fakes. Track C built the station side (agent/pqc_messages.py,
agent/pq_identity.py). Neither can run it for real, because the live
station connections, the dispatcher and the fleet registry all live in
this process. This file is the glue, and ONLY glue -- it edits nothing
in crypto/, idmanager/ or agent/:

    build_migration()           builds the real orchestrator with the real
                                CommandDispatcher, the real registry (via
                                Track B's FleetAdapter) and Track C's
                                build_install_message -- or, if the
                                post-quantum library is missing, a
                                DisabledController that says why.

    PersistentPQAuthenticator   Track B's PQAuthenticator, plus storage:
                                enrolled PUBLIC keys are written to SQLite
                                and reloaded at startup, so an E2 restart
                                does not silently un-migrate the fleet.

    load/apply_fleet_profile    which stations declare ML-DSA support.
                                Without it every station's
                                supported_algorithms is empty and the
                                orchestrator skips the whole fleet as
                                "incompatible".

    orchestrator_emitter        routes the orchestrator's migration events
                                into Contract 3, marked source="orchestrator".

--------------------------------------------------------------------
WHO OWNS WHAT (agreed Day 9)

  Track A  this wiring; the pq_enrolment table; the fleet-profile MECHANISM.
  Track B  the orchestrator's behaviour; the fleet-profile CONTENTS (which
           stations support what -- supported_algorithms is a Track B field
           under Contract 6); the post-quantum challenge check itself.
  Track C  the station's answer to InstallPQAuth / PQAuthChallenge.

WHAT "MIGRATED" MEANS UNTIL TRACK B'S CHALLENGE CHECK LANDS

The orchestrator marks a station MIGRATED when the station ACCEPTS its
InstallPQAuth. Nothing yet asks the station to prove it holds the key
(a PQAuthChallenge answered and verified). Until Track B adds that check,
results must say "key installed", not "authenticated".
"""

from __future__ import annotations

import functools
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from crypto.identity import MigrationState, StationIdentity
from crypto.pq_auth import PQAuthenticator
from idmanager.api import MigrationController, MigrationPhase, MigrationStatus
from idmanager.orchestrator import DEFAULT_FAILURE_THRESHOLD

from csms.handlers import _safe_payload

LOGGER = logging.getLogger("csms.migration")

MIGRATION_MODES = ("auto", "off")
DEFAULT_MIGRATION_MODE = "auto"
"""auto: build the real orchestrator if the post-quantum library imports,
otherwise run with migration disabled and say why. off: always disabled --
for classical baseline runs where nothing post-quantum should even load."""

SUPPORTED_TARGET_MODES = ("pqc",)
"""What /api/migration/start accepts today. The orchestrator enrols ML-DSA
keys whatever target it is given, so passing "classical" or "hybrid" would
record a run labelled with a mode it did not perform. Widen this when
Track B implements another target."""


# =====================================================================
# DISABLED CONTROLLER
# =====================================================================


class DisabledController(MigrationController):
    """
    Contract 4 with migration switched off -- and the reason attached.

    Replaces idmanager/stub.py's StubController in the CSMS, so Track A no
    longer imports Track B's stub and Track B can delete it whenever the
    coordinated stub removal happens. Same behaviour: IDLE status (the
    dashboard's migration panel still works), and the two mutating calls
    raise NotImplementedError, which the HTTP layer turns into a 501 that
    carries the reason.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def start_migration(self, wave_size: int, canary_count: int, target_mode: Any) -> str:
        raise NotImplementedError(self.reason)

    def rollback(self, wave_id: int) -> bool:
        raise NotImplementedError(self.reason)

    def get_migration_status(self) -> MigrationStatus:
        return MigrationStatus(
            migration_id="",
            target_mode="classical",
            phase=MigrationPhase.IDLE,
        )


# =====================================================================
# ENROLMENTS THAT SURVIVE A RESTART
# =====================================================================


class PersistentPQAuthenticator(PQAuthenticator):
    """
    Track B's PQAuthenticator, with its enrolments written to SQLite.

    The problem it solves: PQAuthenticator keeps enrolled public keys in a
    dictionary. Kill the CSMS -- which E2 does on purpose -- and every key
    is gone, while the station_identity table still says "migrated". The
    first challenge after the restart would then fail for every migrated
    station, and E2 would measure our own amnesia as post-quantum cost.

    A subclass rather than an edit: Track B's file is untouched, and every
    method they wrote behaves exactly as before. This class only ADDS a
    write after enrol/unenrol and a restore() at startup.

    Only PUBLIC keys are stored. The private half is generated by the
    orchestrator, sent once to the station inside InstallPQAuth, and never
    written anywhere by the CSMS.
    """

    def __init__(self, provider: Any, store: Any, *, algorithm: str, **kwargs: Any) -> None:
        super().__init__(provider, **kwargs)
        self._store = store
        self._algorithm = algorithm

    def enrol(self, station_id: str, public_key: bytes) -> None:
        super().enrol(station_id, public_key)
        self._store.save_enrolment(station_id, self._algorithm, bytes(public_key))

    def unenrol(self, station_id: str) -> None:
        """
        Remove a station's enrolment (a rolled-back wave, a failed install).

        Track B's plan describes unenrol() but PQAuthenticator does not
        define it yet; the orchestrator falls back to editing the private
        dictionary itself. Defining it here makes that fallback unnecessary
        and adds the database delete. If Track B later adds their own
        unenrol(), theirs runs first.
        """
        parent = getattr(super(), "unenrol", None)
        if callable(parent):
            parent(station_id)
        else:
            self._enrolled.pop(station_id, None)
            self._outstanding.pop(station_id, None)
        self._store.delete_enrolment(station_id)

    def restore(self) -> int:
        """
        Reload every stored enrolment. Called once, at startup, before any
        station can reconnect -- the same moment registry.load() restores
        the fleet. Returns how many keys were restored.

        A key stored under a different algorithm than the one this CSMS
        now runs is skipped with a warning rather than enrolled: verifying
        an ML-DSA-65 key with an ML-DSA-44 provider would fail every
        challenge and look like an attack.
        """
        restored = 0
        for station_id, (algorithm, public_key) in self._store.load_enrolments().items():
            if algorithm != self._algorithm:
                LOGGER.warning(
                    "stored enrolment for %s is %s, this CSMS runs %s; not restored",
                    station_id, algorithm, self._algorithm,
                )
                continue
            PQAuthenticator.enrol(self, station_id, public_key)  # no re-write
            restored += 1
        return restored

    @property
    def enrolled_ids(self) -> list[str]:
        return sorted(self._enrolled)


# =====================================================================
# FLEET PROFILE -- which stations can do what
# =====================================================================

_ID_PARTS = re.compile(r"^(.*?)(\d+)$")


def _expand_range(spec: str) -> list[str]:
    """
    "CP0001-CP0450" -> ["CP0001", ..., "CP0450"]. A plain id is returned
    as itself. Both ends must share the prefix and the digit width, so a
    typo like "CP0001-CP450" is an error rather than a silently different
    fleet.
    """
    if "-" not in spec[1:]:
        return [spec]
    start, _, end = spec.partition("-")
    m1, m2 = _ID_PARTS.match(start), _ID_PARTS.match(end)
    if not m1 or not m2:
        raise ValueError(f"range {spec!r}: both ends must end in digits")
    (p1, d1), (p2, d2) = m1.groups(), m2.groups()
    if p1 != p2 or len(d1) != len(d2):
        raise ValueError(f"range {spec!r}: ends differ in prefix or digit width")
    lo, hi = int(d1), int(d2)
    if hi < lo:
        raise ValueError(f"range {spec!r}: end is before start")
    return [f"{p1}{n:0{len(d1)}d}" for n in range(lo, hi + 1)]


def load_fleet_profile(path: str | Path) -> dict[str, list[str]]:
    """
    Read a fleet profile: which stations declare which algorithms.

    Format (the CONTENTS are Track B's to decide; this is only the shape):

        {
          "description": "E3 mixed fleet: 45 capable, 5 legacy",
          "groups": [
            {"stations": "CP0001-CP0045",
             "supported_algorithms": ["ECDSA-P256", "ML-DSA-44"]},
            {"stations": ["CP0046", "CP0047-CP0050"],
             "supported_algorithms": ["ECDSA-P256"]}
          ]
        }

    A station listed twice with different algorithms is an error -- two
    answers to "can this charger do ML-DSA" would make E3 unrepeatable.
    Raises ValueError on anything malformed, so a bad file stops the
    server at startup rather than mid-experiment.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"fleet profile {path}: {exc}") from exc

    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValueError(f"fleet profile {path}: needs a non-empty 'groups' list")

    profile: dict[str, list[str]] = {}
    for index, group in enumerate(groups):
        algorithms = group.get("supported_algorithms") if isinstance(group, dict) else None
        if not isinstance(algorithms, list) or not all(isinstance(a, str) for a in algorithms):
            raise ValueError(f"fleet profile group {index}: 'supported_algorithms' must be a list of strings")
        stations = group.get("stations")
        specs = [stations] if isinstance(stations, str) else stations
        if not isinstance(specs, list) or not specs or not all(isinstance(s, str) and s for s in specs):
            raise ValueError(f"fleet profile group {index}: 'stations' must be an id, a range or a list of them")
        for spec in specs:
            for station_id in _expand_range(spec):
                if station_id in profile and profile[station_id] != algorithms:
                    raise ValueError(
                        f"fleet profile: {station_id} listed twice with "
                        f"different algorithms"
                    )
                profile[station_id] = list(algorithms)
    return profile


def apply_fleet_profile(registry: Any, profile: dict[str, list[str]]) -> tuple[int, int]:
    """
    Provision the listed stations and set their declared algorithms.

    Returns (added, updated). A station not yet known is provisioned, so
    it appears on /api/fleet as never_seen before anything connects. A
    known station whose algorithms differ is updated through
    set_identity, the Contract 6 write path. Stations the profile does not
    mention are left alone: an unlisted station has declared nothing, and
    the orchestrator correctly treats it as incompatible.
    """
    added = updated = 0
    for station_id, algorithms in sorted(profile.items()):
        identity = registry.get_identity(station_id)
        if identity is None:
            registry.provision(
                StationIdentity(
                    station_id=station_id,
                    current_algorithm="",
                    supported_algorithms=list(algorithms),
                )
            )
            added += 1
        elif list(identity.supported_algorithms) != list(algorithms):
            identity.supported_algorithms = list(algorithms)
            registry.set_identity(identity)
            updated += 1
    return added, updated


# =====================================================================
# EVENTS
# =====================================================================


def orchestrator_emitter(event_log: Any) -> Callable[..., Any]:
    """
    The event_emitter the orchestrator is built with.

    The orchestrator calls emit("wave_started", wave_id=0, ...). Those
    names are already Contract 3 EventType values, so they are written
    as-is, with station_id None (a wave is not one station) and
    source="orchestrator" so a reader can tell these from the server's
    own manual-rollback line. Payload keys go through the same
    _safe_payload guard as the handlers: a key that shadowed an emit()
    parameter would raise inside the migration task and silently drop
    the event -- the A3 bug, in a new place.
    """

    def emit(event_type: str, **payload: Any) -> Any:
        return event_log.emit(
            event_type, None, source="orchestrator", **_safe_payload(payload)
        )

    return emit


# =====================================================================
# BUILD
# =====================================================================


@dataclass
class MigrationSetup:
    """What build_migration produced, for the server and its startup log."""

    controller: MigrationController
    enabled: bool
    reason: str
    authenticator: PersistentPQAuthenticator | None = None
    algorithm: str | None = None
    restored_enrolments: int = 0
    failure_threshold: float | None = None


def build_migration(
    *,
    mode: str,
    registry: Any,
    dispatcher: Any,
    event_log: Any,
    store: Any,
    failure_threshold: float = DEFAULT_FAILURE_THRESHOLD,
) -> MigrationSetup:
    """
    Build the live migration controller, or a disabled one with a reason.

    Never raises for a missing post-quantum library: a CSMS that refuses
    to start because quantcrypt is absent would block every classical
    run -- E1's and E2's classical baselines need no post-quantum code at
    all. It logs loudly instead, and /api/health reports why.
    """
    if mode == "off":
        reason = "migration disabled (--migration off)"
        return MigrationSetup(DisabledController(reason), False, reason)

    try:
        from crypto.pq import PQProvider
        from agent.pqc_messages import build_install_message
        from idmanager.fleet_adapter import FleetAdapter
        from idmanager.orchestrator import MigrationOrchestrator

        provider = PQProvider()
        # PQProvider() only imports quantcrypt's PYTHON classes. The compiled
        # PQClean binaries that do the maths load the first time a key is
        # made -- so make one now, at startup. Found on Day 9: on an Intel Mac
        # quantcrypt 1.0.0 installs without its binaries, PQProvider() still
        # succeeds, and without this probe the server announced "orchestrator
        # active" and then crashed inside the first migration wave.
        provider.generate_keypair()
    except Exception as exc:  # noqa: BLE001 - any failure means "not available"
        reason = (
            f"post-quantum stack unavailable ({type(exc).__name__}: {exc}). "
            f"Fix: pip install -r requirements.txt; if quantcrypt says its "
            f"binaries are missing: pip install 'quantcrypt[compiler]==1.0.0' "
            f"&& qclib compile"
        )
        LOGGER.warning("migration disabled: %s", reason)
        return MigrationSetup(DisabledController(reason), False, reason)

    algorithm = provider.signature_algorithm
    authenticator = PersistentPQAuthenticator(provider, store, algorithm=algorithm)
    restored = authenticator.restore()

    controller = MigrationOrchestrator(
        dispatcher=dispatcher,
        authenticator=authenticator,
        fleet=FleetAdapter(registry),
        keypair_factory=provider.generate_keypair,
        install_message_factory=functools.partial(
            build_install_message, algorithm=algorithm
        ),
        event_emitter=orchestrator_emitter(event_log),
        failure_threshold=failure_threshold,
        dispatch_timeout_s=dispatcher.timeout_s,
        target_algorithm=algorithm,
    )

    # Consistency check: a station the database calls MIGRATED but for
    # which no key was restored cannot pass a challenge. Most likely a
    # database from before Day 9, or a kill inside the flush window.
    orphans = [
        v.station_id for v in registry.list_stations()
        if (registry.get_identity(v.station_id) or StationIdentity(v.station_id, "")).migration_state
        == MigrationState.MIGRATED
        and v.station_id not in authenticator.enrolled_ids
    ]
    if orphans:
        LOGGER.warning(
            "%d station(s) marked migrated with NO enrolled key (e.g. %s). "
            "They will fail any post-quantum challenge. Start from a fresh "
            "--db, or re-run the migration.",
            len(orphans), ", ".join(orphans[:5]),
        )

    reason = f"orchestrator active ({algorithm}, {restored} key(s) restored)"
    LOGGER.info("migration: %s", reason)
    return MigrationSetup(
        controller=controller,
        enabled=True,
        reason=reason,
        authenticator=authenticator,
        algorithm=algorithm,
        restored_enrolments=restored,
        failure_threshold=failure_threshold,
    )
