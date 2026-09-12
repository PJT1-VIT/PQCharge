"""
Agent configuration — every setting the charger agent needs, in one place.

Track C (agent). Phase C1.

--------------------------------------------------------------------
WHY THIS FILE EXISTS

Without it, settings end up scattered: a URL typed into client.py, a
token buried in a test, a power limit passed positionally three calls
deep. That is how an experiment run ends up misconfigured in a way
nobody notices until the numbers look odd in Stage 9.

One object, constructed once at start-up, passed down. Every run logs
what it was configured with (see describe()), so a run's parameters are
recoverable from its own log rather than from memory of which flags
were typed. Track A does the same thing server-side by writing its
configuration into the SERVER_STARTED event.

--------------------------------------------------------------------
TWO WAYS TO BUILD ONE, AND WHY BOTH ARE NEEDED

    AgentConfig(station_id="CP001")      # in code
    AgentConfig.from_args()              # from the command line

The command-line path is for running a single agent by hand. The
in-code path is what harness/load_generator.py (Phase C5) uses when it
creates five hundred of these in a loop. A config object that could
only be built from sys.argv would force the harness to invent a second
configuration system, so this class never touches sys.argv except
inside from_args().

--------------------------------------------------------------------
WHAT IS DELIBERATELY *NOT* A SETTING HERE

The heartbeat interval. It is tempting to add it, and it would be
wrong: OCPP has the CSMS issue the interval in its reply to
BootNotification, and the station must use what it is told. A
hardcoded local value would work in testing and quietly diverge from
the server under load. agent/client.py reads it from the response.

--------------------------------------------------------------------
DEPENDENCY RULE

This module must NOT import agent.logging_setup, and logging_setup must
not import this. They are both foundation modules; if either imports
the other, the first log line emitted from inside config creates a
circular import. Whoever starts the program wires them together:
read the config, then pass config.log_level into configure_logging().
--------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# Contract 1 owns the list of legal cryptographic modes. Importing it
# rather than re-typing the three strings means this file cannot drift
# from Track B's definition. Reading another track's contract is
# expected; editing their module is not.
#
# crypto/provider.py is a pure abstract base class -- it imports only
# abc and typing, and pulls in no cryptographic library -- so this
# import stays cheap and cannot fail because liboqs is missing.
from crypto.provider import VALID_MODES, CryptoMode

# The simulated power backend's own default, imported for the same
# reason: one source of truth for "what does a station draw".
from agent.simulated_power import DEFAULT_LIMIT_W

# Our connection convention, matching csms/server.py.
DEFAULT_CSMS_URL = "ws://localhost:9000"

# Verified accepted by csms/authorization.py. TAG-BLOCKED is the one
# the server deliberately refuses, which is what makes a demonstration
# of authorisation show anything at all.
DEFAULT_ID_TOKEN = "TAG-0001"

# csms/server.py accepts both of these. "ocpp2.0.1" is the standard
# identifier; "ocpp2.0.1+pqc" is the capability marker for later
# post-quantum work. Stage 1 uses the plain one.
SUBPROTOCOL_STANDARD = "ocpp2.0.1"
SUBPROTOCOL_PQC = "ocpp2.0.1+pqc"


@dataclass
class AgentConfig:
    """
    Settings for one charging-station agent.

    A dataclass rather than a dict so that a typo in a field name is a
    TypeError at construction instead of a silently ignored key that
    leaves the agent running on a default nobody intended.
    """

    # -- identity and connection ---------------------------------------

    station_id: str = "CP001"
    """
    This station's OCPP identity.

    It becomes the last segment of the WebSocket path, and that is how
    the CSMS knows who connected -- csms/server.py takes the last
    non-empty path segment. It is also the primary key in the fleet
    view, the event log and the dashboard table, so it must be unique
    across a run.
    """

    csms_url: str = DEFAULT_CSMS_URL
    """
    Base address of the CSMS, without a station id.

    The station id is appended by the ws_url property below. Kept
    separate so the harness can vary one and hold the other fixed.
    """

    subprotocol: str = SUBPROTOCOL_STANDARD
    """WebSocket subprotocol offered during the handshake."""

    # -- driver and session --------------------------------------------

    id_token: str = DEFAULT_ID_TOKEN
    """
    The driver token this station presents in Authorize.

    Set it to TAG-BLOCKED to exercise the refusal path: the server
    answers Blocked and no transaction should begin. Anything not in
    the server's list comes back Invalid.
    """

    charge_for_s: float = 40.0
    """How long a simulated charging session lasts, in seconds."""

    meter_every_s: float = 10.0
    """Seconds between TransactionEvent Updated messages."""

    # -- cryptography (Stage 3 onward; inert today) ---------------------

    crypto_mode: CryptoMode = "classical"
    """
    Which cryptographic configuration this station runs.

    Does almost nothing today: Track B's real backends do not exist and
    crypto/stub.py raises on every call. It is here from the start
    anyway because it is the grouping variable for every comparison
    chart in the results -- retrofitting it later would mean touching
    every file that logs or reports.
    """

    supported_algorithms: list[str] = field(default_factory=list)
    """
    What this station's firmware claims it can process, per Contract 2.

    This is what makes the heterogeneous-fleet story work. A station
    whose list excludes the migration target is marked INCOMPATIBLE and
    skipped by the orchestrator, rather than counted as a failure. Left
    empty by default because inventing algorithm names before Track B
    publishes them would put fiction in the results table.
    """

    # -- physical model --------------------------------------------------

    max_power_w: float = DEFAULT_LIMIT_W
    """Ceiling passed to SimulatedPower. 7.4 kW is a typical AC charger."""

    # -- reconnection (used from Phase C4) -------------------------------

    reconnect_base_delay_s: float = 1.0
    """First retry delay. Doubles on each subsequent failure."""

    reconnect_max_delay_s: float = 30.0
    """Ceiling on the doubling, so a long outage does not back off forever."""

    reconnect_jitter: float = 0.3
    """
    Random fraction added to or subtracted from each delay, 0.0-1.0.

    Not decoration. Experiment E2 kills the CSMS with hundreds of
    agents attached; without jitter they all retry on the same tick,
    and the measurement becomes the cost of our own thundering herd
    rather than the cost of post-quantum handshakes.
    """

    reconnect_max_attempts: int = 0
    """0 means retry indefinitely, which is what an E2 run wants."""

    # -- observability ---------------------------------------------------

    log_level: str = "INFO"
    """
    DEBUG, INFO, WARNING or ERROR.

    Passed to configure_logging() by the entry point. The harness
    lowers this to WARNING for the agents it spawns: five hundred
    agents each logging at INFO floods the terminal and competes with
    the event loop, which is the same class of problem as logging cost
    distorting the very timings being measured.
    """

    log_to_file: bool = False
    """Also write to logs/agent_<station_id>.log."""

    log_dir: str = "logs"
    """
    Where per-agent log files go.

    .gitignore already ignores everything under logs/ except .gitkeep,
    so run output cannot be committed by accident.
    """

    run_id: str | None = None
    """
    Identifier shared by every agent in one experiment run.

    None means "generate one". The harness sets this explicitly so that
    all five hundred agents and the harness timing log carry the same
    value, which is what lets analysis join client-side timings to the
    server's event log later.

    Note this is Track C's own run id for its own logs. The CSMS
    generates its own run_id for the Contract 3 event log; the two are
    correlated at analysis time, not shared at runtime.
    """

    # -- validation ------------------------------------------------------

    def __post_init__(self) -> None:
        """
        Reject nonsense at construction rather than at first use.

        A bad station id or crypto mode discovered here is one clear
        error message. The same mistake discovered three layers into an
        async call stack, twenty seconds into a run, is an afternoon.
        """
        if not self.station_id or not self.station_id.strip():
            raise ValueError("station_id must be a non-empty string")

        # Slashes would break the URL path convention: the CSMS takes
        # the LAST path segment, so "CP/001" would arrive as "001".
        if "/" in self.station_id:
            raise ValueError(
                f"station_id must not contain '/': {self.station_id!r}. "
                "The CSMS reads the last path segment as the identity, so a "
                "slash would silently truncate it."
            )

        if self.crypto_mode not in VALID_MODES:
            raise ValueError(
                f"unknown crypto_mode {self.crypto_mode!r}; "
                f"expected one of {VALID_MODES}"
            )

        if self.subprotocol not in (SUBPROTOCOL_STANDARD, SUBPROTOCOL_PQC):
            raise ValueError(
                f"unknown subprotocol {self.subprotocol!r}; csms/server.py "
                f"accepts {SUBPROTOCOL_STANDARD!r} and {SUBPROTOCOL_PQC!r}"
            )

        if self.max_power_w < 0:
            raise ValueError(f"max_power_w must be >= 0, got {self.max_power_w}")

        if self.charge_for_s < 0:
            raise ValueError(f"charge_for_s must be >= 0, got {self.charge_for_s}")

        if self.meter_every_s <= 0:
            raise ValueError(
                f"meter_every_s must be > 0, got {self.meter_every_s}; "
                "zero would spin the meter loop without sleeping"
            )

        if self.reconnect_base_delay_s <= 0:
            raise ValueError("reconnect_base_delay_s must be > 0")

        if self.reconnect_max_delay_s < self.reconnect_base_delay_s:
            raise ValueError(
                "reconnect_max_delay_s must be >= reconnect_base_delay_s"
            )

        if not 0.0 <= self.reconnect_jitter <= 1.0:
            raise ValueError(
                f"reconnect_jitter must be between 0.0 and 1.0, "
                f"got {self.reconnect_jitter}"
            )

        # Normalise the level string so "info" and "INFO" both work.
        self.log_level = self.log_level.upper()
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"unknown log_level {self.log_level!r}")

        # A run id is always present after construction, so nothing
        # downstream has to handle None.
        if self.run_id is None:
            self.run_id = uuid.uuid4().hex[:12]

    # -- derived values ---------------------------------------------------

    @property
    def ws_url(self) -> str:
        """
        The full address this agent connects to.

        Composed here, once, rather than by string-joining at the call
        site -- which is how a double slash or a missing one ends up
        producing a station id of "" and a confusing 1008 close.
        """
        return f"{self.csms_url.rstrip('/')}/{self.station_id}"

    @property
    def log_file_path(self) -> Path:
        """Where this agent's own diagnostic log goes, if enabled."""
        return Path(self.log_dir) / f"agent_{self.station_id}.log"

    # -- serialisation ----------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """
        A flat, JSON-safe summary for the one start-up log line.

        Section 18 of the design document asks for results reproducible
        from logged data. That starts with being able to tell, from a
        run's own log, how the run was configured.
        """
        d = asdict(self)
        d["ws_url"] = self.ws_url
        return d

    def describe_compact(self) -> str:
        """
        One-line human-readable form, for the start-up INFO message.

        Only the fields that change a measurement. The full picture is
        in describe(); this is what a person reads while watching a run.
        """
        return (
            f"station={self.station_id} url={self.ws_url} "
            f"mode={self.crypto_mode} token={self.id_token} "
            f"run_id={self.run_id} log_level={self.log_level}"
        )

    def to_json(self, indent: int = 2) -> str:
        """Full configuration as JSON, for saving alongside a run."""
        return json.dumps(self.describe(), indent=indent, sort_keys=True)

    # -- construction from outside ----------------------------------------

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """
        Register this config's flags on an existing parser.

        Split out from from_args() so the load generator can build one
        parser carrying both its own options (how many agents, storm
        timing) and every per-agent option, without duplicating the
        flag definitions.
        """
        parser.add_argument(
            "--station-id", default=cls.station_id,
            help="OCPP identity; becomes the last path segment of the URL",
        )
        parser.add_argument(
            "--csms-url", default=cls.csms_url,
            help="CSMS base address without the station id",
        )
        parser.add_argument(
            "--subprotocol", default=cls.subprotocol,
            choices=[SUBPROTOCOL_STANDARD, SUBPROTOCOL_PQC],
        )
        parser.add_argument(
            "--token", dest="id_token", default=cls.id_token,
            help="driver token for Authorize; TAG-BLOCKED to see a refusal",
        )
        parser.add_argument(
            "--charge-for", dest="charge_for_s", type=float,
            default=cls.charge_for_s,
            help="seconds to charge before ending the transaction",
        )
        parser.add_argument(
            "--meter-every", dest="meter_every_s", type=float,
            default=cls.meter_every_s,
            help="seconds between TransactionEvent Updated messages",
        )
        parser.add_argument(
            "--crypto-mode", dest="crypto_mode", default=cls.crypto_mode,
            choices=list(VALID_MODES),
        )
        parser.add_argument(
            "--supported-algorithms", dest="supported_algorithms",
            default="", metavar="A,B,C",
            help="comma-separated capability profile for Contract 2",
        )
        parser.add_argument(
            "--max-power", dest="max_power_w", type=float,
            default=cls.max_power_w, help="watts",
        )
        parser.add_argument(
            "--reconnect-base-delay", dest="reconnect_base_delay_s",
            type=float, default=cls.reconnect_base_delay_s,
        )
        parser.add_argument(
            "--reconnect-max-delay", dest="reconnect_max_delay_s",
            type=float, default=cls.reconnect_max_delay_s,
        )
        parser.add_argument(
            "--reconnect-jitter", dest="reconnect_jitter", type=float,
            default=cls.reconnect_jitter,
            help="0.0-1.0; keep non-zero for E2 or agents retry in lockstep",
        )
        parser.add_argument(
            "--reconnect-max-attempts", dest="reconnect_max_attempts",
            type=int, default=cls.reconnect_max_attempts,
            help="0 for unlimited",
        )
        parser.add_argument(
            "--log-level", dest="log_level", default=cls.log_level,
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        )
        parser.add_argument(
            "--log-to-file", dest="log_to_file", action="store_true",
            help="also write logs/agent_<station_id>.log",
        )
        parser.add_argument(
            "--log-dir", dest="log_dir", default=cls.log_dir,
        )
        parser.add_argument(
            "--run-id", dest="run_id", default=None,
            help="shared across every agent in one experiment run",
        )

    @classmethod
    def from_namespace(cls, ns: argparse.Namespace) -> AgentConfig:
        """
        Build a config from parsed arguments.

        Kept separate from from_args() so the harness, which parses its
        own richer namespace, can reuse this conversion instead of
        reimplementing it.
        """
        # The only field needing a type change: a comma-separated string
        # on the command line becomes a list on the dataclass.
        raw = getattr(ns, "supported_algorithms", "") or ""
        algorithms = [a.strip() for a in raw.split(",") if a.strip()]

        return cls(
            station_id=ns.station_id,
            csms_url=ns.csms_url,
            subprotocol=ns.subprotocol,
            id_token=ns.id_token,
            charge_for_s=ns.charge_for_s,
            meter_every_s=ns.meter_every_s,
            crypto_mode=ns.crypto_mode,
            supported_algorithms=algorithms,
            max_power_w=ns.max_power_w,
            reconnect_base_delay_s=ns.reconnect_base_delay_s,
            reconnect_max_delay_s=ns.reconnect_max_delay_s,
            reconnect_jitter=ns.reconnect_jitter,
            reconnect_max_attempts=ns.reconnect_max_attempts,
            log_level=ns.log_level,
            log_to_file=ns.log_to_file,
            log_dir=ns.log_dir,
            run_id=ns.run_id,
        )

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> AgentConfig:
        """
        Build a config from the command line.

        argv is a parameter rather than read straight from sys.argv so
        that tests can drive it without touching global state.
        """
        parser = argparse.ArgumentParser(
            description="PQCharge station agent configuration",
        )
        cls.add_arguments(parser)
        return cls.from_namespace(parser.parse_args(argv))

    @classmethod
    def from_json_file(cls, path: str | Path) -> AgentConfig:
        """
        Build a config from a JSON file.

        Useful for pinning an experiment's parameters in the repo so a
        run can be repeated exactly months later. Unknown keys are
        rejected rather than ignored, because a silently dropped key is
        a setting you think you changed and did not.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))

        # describe() adds ws_url, which is derived, not a field. Drop it
        # so a file produced by to_json() can be read back unchanged.
        data.pop("ws_url", None)

        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                f"unknown configuration keys in {path}: {sorted(unknown)}"
            )
        return cls(**data)