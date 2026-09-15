"""
SQLite persistence — what the CSMS remembers across a restart.

Track A (csms). Phase A4, Day 6.

--------------------------------------------------------------------
WHY THIS EXISTS, AND WHAT IT IS NOT

It is NOT the measurement surface. Contract 3's event log is, and it
stays that way: append-only, fsynced, survives the server being killed
mid-write. Every number in the results chapter comes from there.

This is the answer to a different question -- "what did the CSMS know
about this fleet before it died?" Without it, a restarted CSMS reports
total_stations = 0 and counts up as stations reconnect, so E2 has no
recovery denominator: the server cannot say "47 of 500 recovered"
because it does not know there were 500. With it, the restarted server
reports the full fleet as disconnected and watches them come back.

The shape follows Day 6 Step 1. StationState was separated from
StationSession because a fact about a station outlives its socket --
and a fact that outlives a socket is exactly the fact worth writing to
disk. A connection cannot survive a process restart in any case.

--------------------------------------------------------------------
TWO TABLES FOR STATIONS, MIRRORING CONTRACT 6's OWNERSHIP SPLIT

  station_identity   Track B's fields: certificates, migration state
  station_state      Track A's fields: status, power, energy, sequence

Contract 6 says no field has two writers, which is why neither side
needs a lock. Keeping them in separate tables means that stays true on
disk as well as in memory, and a migration write can never clobber a
telemetry write by touching the same row.

The identity is stored as the JSON of Contract 2's own to_dict(), not
as unpacked columns. Contract 2 already defines that mapping; a second
column-by-column definition here would be a second thing to keep in
step with a frozen contract, and it would drift.

--------------------------------------------------------------------
WRITE-BEHIND, NOT WRITE-THROUGH

Every write is buffered and applied by flush(), in one transaction.

This is the fsync finding one layer up. At 500 stations, a synchronous
database write per meter value puts a disk round-trip on the same event
loop that is serving the WebSocket connections -- and the timings that
would inflate are the ones E2 exists to measure. Session opens are
buffered too, which is why open_session() mints its own identifier
rather than relying on the database to assign one: the connect path is
E2's hot path and must not block on disk.

THE COST, STATED PLAINLY: a hard kill loses whatever has not been
flushed -- up to flush_interval_s of state, and any meter values in the
buffer. That is acceptable precisely because this is not the
measurement surface. The event log has already fsynced those same
events. What is lost is a cache of current state, and the next
connection rebuilds it. Recorded in docs/limitations.md.

PRAGMA synchronous is configurable for the same reason it matters in
the event log: it is the knob that trades durability for speed, and the
Day 6 experiment should measure it rather than assume it.

--------------------------------------------------------------------
NullStore exists so the registry has no `if self._store is not None`
scattered through it. Persistence off is a store that discards, not a
branch at every call site.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from crypto.identity import StationIdentity

LOGGER = logging.getLogger("csms.persistence")

DEFAULT_DB_PATH = "logs/pqcharge.db"
DEFAULT_FLUSH_INTERVAL_S = 2.0
"""How often buffered writes reach disk. The upper bound on how much
current state a hard kill can lose."""

SYNCHRONOUS_MODES = ("off", "normal", "full")
DEFAULT_SYNCHRONOUS = "normal"
"""SQLite's durability setting. 'full' fsyncs every commit; 'normal'
fsyncs at checkpoints; 'off' leaves it to the operating system. Exposed
rather than fixed, because it is the same trade-off the Day 6 event-log
experiment is measuring and the two should be measured together."""

DEFAULT_JOURNAL_MODE = "wal"
"""Write-ahead logging. Chosen so a reader (a future dashboard, or a
person with sqlite3 open) cannot block the server's writes, and so an
interrupted write leaves a recoverable file rather than a corrupt one."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS station_identity (
    station_id  TEXT PRIMARY KEY,
    record      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_state (
    station_id            TEXT PRIMARY KEY,
    ocpp_status           TEXT,
    charging_state        TEXT,
    active_transaction_id TEXT,
    power_w               REAL,
    energy_wh             REAL,
    last_meter_at         TEXT,
    last_seq_no           INTEGER,
    last_seen_at          TEXT,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    station_id      TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    connected_at    TEXT NOT NULL,
    disconnected_at TEXT,
    handshake_ms    REAL,
    duration_ms     REAL,
    close_reason    TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    station_id     TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    id_token       TEXT,
    energy_wh      REAL
);

CREATE TABLE IF NOT EXISTS meter_values (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id     TEXT NOT NULL,
    transaction_id TEXT,
    run_id         TEXT NOT NULL,
    reading_at     TEXT,
    recorded_at    TEXT NOT NULL,
    seq_no         INTEGER,
    power_w        REAL,
    energy_wh      REAL,
    offline        INTEGER NOT NULL DEFAULT 0,
    applied        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_sessions_station  ON sessions(station_id);
CREATE INDEX IF NOT EXISTS idx_sessions_run      ON sessions(run_id);
CREATE INDEX IF NOT EXISTS idx_tx_station        ON transactions(station_id);
CREATE INDEX IF NOT EXISTS idx_tx_run            ON transactions(run_id);
CREATE INDEX IF NOT EXISTS idx_meter_tx          ON meter_values(transaction_id);
CREATE INDEX IF NOT EXISTS idx_meter_run         ON meter_values(run_id);
"""

STATE_COLUMNS = (
    "ocpp_status",
    "charging_state",
    "active_transaction_id",
    "power_w",
    "energy_wh",
    "last_meter_at",
    "last_seq_no",
    "last_seen_at",
)
"""The StationState fields that reach disk, named once.

Deliberately not derived from the dataclass by reflection: adding a
field to StationState should be a deliberate decision about whether it
belongs in the database, not something that silently changes the schema.
"""


def _iso(value: Any) -> str | None:
    """Datetime to ISO-8601 text, passing strings and None through."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) and value else None


class NullStore:
    """
    Persistence turned off. Every write is discarded, every read empty.

    Exists so that `--no-db` is a different object rather than a branch
    at every call site in the registry. The registry always has a store
    and never asks whether it has one.
    """

    enabled = False

    def load_identities(self) -> dict[str, dict[str, Any]]:
        return {}

    def load_states(self) -> dict[str, dict[str, Any]]:
        return {}

    def save_identity(self, station_id: str, record: dict[str, Any]) -> None:
        pass

    def save_state(self, station_id: str, fields: dict[str, Any]) -> None:
        pass

    def open_session(self, **kwargs: Any) -> str:
        return ""

    def close_session(self, session_id: str, **kwargs: Any) -> None:
        pass

    def start_transaction(self, **kwargs: Any) -> None:
        pass

    def end_transaction(self, **kwargs: Any) -> None:
        pass

    def add_meter_value(self, **kwargs: Any) -> None:
        pass

    def flush(self) -> int:
        return 0

    def close(self) -> None:
        pass


class SqliteStore:
    """
    The real store. One instance per CSMS process.

    Not thread-safe and does not need to be: the CSMS is a single
    asyncio event loop, the same constraint the registry documents. The
    connection is opened with check_same_thread left at its default so
    that assumption fails loudly rather than corrupting a file if it is
    ever broken.
    """

    enabled = True

    def __init__(
        self,
        path: str | Path = DEFAULT_DB_PATH,
        *,
        run_id: str = "",
        synchronous: str = DEFAULT_SYNCHRONOUS,
        journal_mode: str = DEFAULT_JOURNAL_MODE,
    ) -> None:
        if synchronous not in SYNCHRONOUS_MODES:
            raise ValueError(
                f"unknown synchronous mode {synchronous!r}; "
                f"expected one of {SYNCHRONOUS_MODES}"
            )

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id

        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA journal_mode={journal_mode}")
        self._db.execute(f"PRAGMA synchronous={synchronous}")
        self._db.executescript(SCHEMA)
        self._db.commit()

        # -- write-behind buffers --
        self._dirty_identities: dict[str, dict[str, Any]] = {}
        self._dirty_states: dict[str, dict[str, Any]] = {}
        self._session_opens: list[tuple] = []
        self._session_closes: list[tuple] = []
        self._tx_starts: list[tuple] = []
        self._tx_ends: list[tuple] = []
        self._meter_rows: list[tuple] = []

        LOGGER.info(
            "persistence open: %s (synchronous=%s, journal=%s)",
            self.path, synchronous, journal_mode,
        )

    # -- startup read ---------------------------------------------------

    def load_identities(self) -> dict[str, dict[str, Any]]:
        """
        Every Contract 2 record on disk, as to_dict() JSON.

        Returned as dicts rather than StationIdentity objects so this
        module stays a storage layer: it knows columns, the registry
        knows the domain. Contract 2's from_dict() does the rebuilding.
        """
        rows = self._db.execute(
            "SELECT station_id, record FROM station_identity"
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                out[row["station_id"]] = json.loads(row["record"])
            except json.JSONDecodeError:
                LOGGER.warning(
                    "station_identity row for %s is not valid JSON; skipping",
                    row["station_id"],
                )
        return out

    def load_states(self) -> dict[str, dict[str, Any]]:
        """Every station's last known state, keyed by station_id."""
        rows = self._db.execute(
            f"SELECT station_id, {', '.join(STATE_COLUMNS)} FROM station_state"
        ).fetchall()
        return {
            row["station_id"]: {c: row[c] for c in STATE_COLUMNS} for row in rows
        }

    # -- buffered writes -------------------------------------------------

    def save_identity(self, station_id: str, record: dict[str, Any]) -> None:
        """Queue a Contract 2 record. Last write before a flush wins."""
        self._dirty_identities[station_id] = record

    def save_state(self, station_id: str, fields: dict[str, Any]) -> None:
        """
        Queue a station's current state.

        Keyed by station, so a station reporting a meter value every ten
        seconds produces one row write per flush rather than one per
        reading. At 500 stations that is the difference between a
        handful of writes and thousands.
        """
        self._dirty_states[station_id] = fields

    def open_session(
        self,
        *,
        station_id: str,
        connected_at: Any,
        handshake_ms: float | None = None,
    ) -> str:
        """
        Record a connection opening. Returns the session identifier.

        The identifier is minted here rather than taken from SQLite's
        AUTOINCREMENT, because returning a database-assigned id would
        force a synchronous insert on the connect path -- and the
        connect path is precisely what E2 measures.
        """
        session_id = uuid.uuid4().hex
        self._session_opens.append(
            (
                session_id,
                station_id,
                self.run_id,
                _iso(connected_at),
                handshake_ms,
            )
        )
        return session_id

    def close_session(
        self,
        session_id: str,
        *,
        disconnected_at: Any,
        duration_ms: float | None = None,
        reason: str | None = None,
    ) -> None:
        if not session_id:
            return
        self._session_closes.append(
            (_iso(disconnected_at), duration_ms, reason, session_id)
        )

    def start_transaction(
        self,
        *,
        transaction_id: str,
        station_id: str,
        started_at: Any,
        id_token: str | None = None,
    ) -> None:
        self._tx_starts.append(
            (
                transaction_id,
                station_id,
                self.run_id,
                _iso(started_at),
                id_token,
            )
        )

    def end_transaction(
        self,
        *,
        transaction_id: str,
        ended_at: Any,
        energy_wh: float | None = None,
    ) -> None:
        self._tx_ends.append((_iso(ended_at), energy_wh, transaction_id))

    def add_meter_value(
        self,
        *,
        station_id: str,
        transaction_id: str | None,
        reading_at: Any,
        recorded_at: Any,
        seq_no: int | None = None,
        power_w: float | None = None,
        energy_wh: float | None = None,
        offline: bool = False,
        applied: bool = True,
    ) -> None:
        """
        Record one meter reading.

        Stale readings are stored too, marked applied=0. A replayed
        backlog that the registry refused for live state is still real
        history of that transaction, and discarding it would make the
        database disagree with the event log about what the station
        reported.
        """
        self._meter_rows.append(
            (
                station_id,
                transaction_id,
                self.run_id,
                _iso(reading_at),
                _iso(recorded_at),
                seq_no,
                power_w,
                energy_wh,
                1 if offline else 0,
                1 if applied else 0,
            )
        )

    # -- flush ------------------------------------------------------------

    def flush(self) -> int:
        """
        Apply every buffered write in one transaction.

        Returns:
            How many rows were written. Zero is the common case between
            events and costs one dictionary check.

        Ordering matters: sessions and transactions are opened before
        they are closed, so opens go in before closes within the same
        transaction. A close whose open is in the same batch therefore
        finds its row.
        """
        pending = (
            len(self._dirty_identities)
            + len(self._dirty_states)
            + len(self._session_opens)
            + len(self._session_closes)
            + len(self._tx_starts)
            + len(self._tx_ends)
            + len(self._meter_rows)
        )
        if pending == 0:
            return 0

        now = datetime.now().astimezone().isoformat()
        cur = self._db.cursor()
        try:
            if self._dirty_identities:
                cur.executemany(
                    "INSERT INTO station_identity (station_id, record, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(station_id) DO UPDATE SET "
                    "record=excluded.record, updated_at=excluded.updated_at",
                    [
                        (sid, json.dumps(rec), now)
                        for sid, rec in self._dirty_identities.items()
                    ],
                )

            if self._dirty_states:
                columns = ", ".join(STATE_COLUMNS)
                placeholders = ", ".join("?" for _ in STATE_COLUMNS)
                updates = ", ".join(f"{c}=excluded.{c}" for c in STATE_COLUMNS)
                cur.executemany(
                    f"INSERT INTO station_state (station_id, {columns}, updated_at) "
                    f"VALUES (?, {placeholders}, ?) "
                    f"ON CONFLICT(station_id) DO UPDATE SET "
                    f"{updates}, updated_at=excluded.updated_at",
                    [
                        (sid, *(_iso(f.get(c)) if c.endswith("_at") else f.get(c)
                                for c in STATE_COLUMNS), now)
                        for sid, f in self._dirty_states.items()
                    ],
                )

            if self._session_opens:
                cur.executemany(
                    "INSERT OR REPLACE INTO sessions "
                    "(session_id, station_id, run_id, connected_at, handshake_ms) "
                    "VALUES (?, ?, ?, ?, ?)",
                    self._session_opens,
                )
            if self._session_closes:
                cur.executemany(
                    "UPDATE sessions SET disconnected_at=?, duration_ms=?, "
                    "close_reason=? WHERE session_id=?",
                    self._session_closes,
                )

            if self._tx_starts:
                cur.executemany(
                    "INSERT OR IGNORE INTO transactions "
                    "(transaction_id, station_id, run_id, started_at, id_token) "
                    "VALUES (?, ?, ?, ?, ?)",
                    self._tx_starts,
                )
            if self._tx_ends:
                cur.executemany(
                    "UPDATE transactions SET ended_at=?, energy_wh=? "
                    "WHERE transaction_id=?",
                    self._tx_ends,
                )

            if self._meter_rows:
                cur.executemany(
                    "INSERT INTO meter_values "
                    "(station_id, transaction_id, run_id, reading_at, recorded_at, "
                    " seq_no, power_w, energy_wh, offline, applied) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._meter_rows,
                )

            self._db.commit()
        except sqlite3.Error:
            self._db.rollback()
            LOGGER.exception("persistence flush failed; buffered writes dropped")
            # Deliberately not re-raised. The CSMS must keep serving
            # stations if the database is unavailable -- this is a cache
            # of current state, not the measurement surface, and losing
            # it must never take a charging fleet offline.
        finally:
            self._dirty_identities.clear()
            self._dirty_states.clear()
            self._session_opens.clear()
            self._session_closes.clear()
            self._tx_starts.clear()
            self._tx_ends.clear()
            self._meter_rows.clear()

        return pending

    def close(self) -> None:
        """Flush and close. Called on clean shutdown."""
        self.flush()
        self._db.close()
        LOGGER.info("persistence closed: %s", self.path)


def open_store(
    path: str | Path | None,
    *,
    run_id: str = "",
    synchronous: str = DEFAULT_SYNCHRONOUS,
    journal_mode: str = DEFAULT_JOURNAL_MODE,
) -> NullStore | SqliteStore:
    """
    Build a store, or a NullStore when persistence is switched off.

    A database that cannot be opened returns a NullStore with a warning
    rather than preventing the server from starting. A CSMS that refuses
    to accept charging stations because a cache file is unwritable is
    worse than one that forgets what it knew.
    """
    if not path:
        return NullStore()
    try:
        return SqliteStore(
            path, run_id=run_id, synchronous=synchronous, journal_mode=journal_mode
        )
    except (sqlite3.Error, OSError):
        LOGGER.exception("could not open %s; continuing without persistence", path)
        return NullStore()
