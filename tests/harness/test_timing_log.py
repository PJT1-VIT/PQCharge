"""
Tests for harness/timing_log.py.

Track C (tests). Phase C5.

No network, no event loop. The timing log is the harness's only record
of a run, and a run is expensive — minutes of setup, one attempt — so
the properties tested here are the ones that decide whether a completed
run is readable afterwards.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from harness import timing_log as tl
from harness.timing_log import TimingLog, default_path


def make_log(tmp_path, **overrides) -> TimingLog:
    defaults = dict(
        run_id="testrun00001",
        experiment="unit",
        n_stations=3,
        crypto_mode="classical",
    )
    defaults.update(overrides)
    return TimingLog(tmp_path / "t.jsonl", **defaults)


# =====================================================================
# THE FILENAME — agreed with Track A
# =====================================================================


def test_the_filename_follows_the_agreed_convention():
    """
    logs/<experiment>_n<N>_<mode>.jsonl, agreed in Track A's §9.4.

    It matters because analysis/parse_events.py reads these by pattern
    at Stage 9. A run called `test2_final.jsonl` has to be identified by
    hand, weeks later, from its contents.
    """
    assert default_path("e2", 500, "pqc") .name == "e2_n500_pqc.jsonl"
    assert default_path("e1", 50, "classical", "logs").name == "e1_n50_classical.jsonl"


def test_an_awkward_experiment_name_cannot_produce_a_bad_path():
    """
    A slash in the name would write outside logs/, or fail. Sanitising
    is cheaper than a run that ends by not being able to save itself.
    """
    path = default_path("e2/storm run", 10, "classical")
    assert "/" not in path.name and " " not in path.name
    assert path.name.startswith("e2_storm_run_n10_")


# =====================================================================
# THE ENVELOPE
# =====================================================================


def test_every_record_is_self_describing(tmp_path):
    """
    The envelope repeats on every line on purpose. It buys the property
    that any line identifies its own run — so a file can be grepped,
    split, or concatenated with another run's and still be read.
    """
    with make_log(tmp_path) as log:
        log.emit(tl.STATION_SPAWNED, "CP0001")

    row = TimingLog.read(tmp_path / "t.jsonl")[0]
    for key in (
        "ts", "elapsed_ms", "run_id", "experiment",
        "n_stations", "crypto_mode", "tls", "event_type", "station_id",
    ):
        assert key in row, f"{key} missing from the envelope"
    assert row["run_id"] == "testrun00001"
    assert row["station_id"] == "CP0001"


def test_elapsed_ms_is_monotonic_not_wall_clock(tmp_path):
    """
    Contract 3's own rule: durations come from monotonic differences,
    never wall-clock subtraction. A 500-agent run takes minutes, which
    is long enough for an NTP correction to land inside it and silently
    move an interval.
    """
    with make_log(tmp_path) as log:
        log.emit(tl.PROGRESS)
        log.emit(tl.PROGRESS)

    rows = TimingLog.read(tmp_path / "t.jsonl")
    assert rows[0]["elapsed_ms"] >= 0
    assert rows[1]["elapsed_ms"] >= rows[0]["elapsed_ms"]


def test_the_wall_clock_stamp_is_timezone_aware(tmp_path):
    """Needed to line this file up against Track A's log. A naive
    timestamp would be read as local time at analysis."""
    with make_log(tmp_path) as log:
        log.emit(tl.RUN_STARTED)
    row = TimingLog.read(tmp_path / "t.jsonl")[0]
    assert datetime.fromisoformat(row["ts"]).tzinfo is not None


def test_caller_fields_are_merged_in(tmp_path):
    with make_log(tmp_path) as log:
        log.emit(tl.STATION_FINISHED, "CP0001", ok=True, reconnections=3)
    row = TimingLog.read(tmp_path / "t.jsonl")[0]
    assert row["ok"] is True and row["reconnections"] == 3


def test_a_nested_snapshot_survives_a_round_trip(tmp_path):
    """FLEET_SNAPSHOT stores Contract 6's whole payload verbatim. If it
    did not round-trip, recovery could not be computed at Stage 9."""
    snapshot = {"run_id": "abc", "stations": [{"station_id": "CP0001"}]}
    with make_log(tmp_path) as log:
        log.emit(tl.FLEET_SNAPSHOT, snapshot=snapshot)
    assert TimingLog.read(tmp_path / "t.jsonl")[0]["snapshot"] == snapshot


# =====================================================================
# NEVER BREAK THE RUN
# =====================================================================


def test_an_unserialisable_field_does_not_kill_the_run(tmp_path):
    """
    Instrumentation that can crash the thing it instruments is worse
    than no instrumentation: the run dies and the cause looks like a
    fault in the system under test.
    """
    class Odd:
        pass

    with make_log(tmp_path) as log:
        log.emit(tl.PROGRESS, thing=Odd())
        log.emit(tl.PROGRESS, after=True)

    rows = TimingLog.read(tmp_path / "t.jsonl")
    assert len(rows) == 2
    assert rows[-1]["after"] is True


def test_a_write_failure_is_counted_not_raised(tmp_path):
    log = make_log(tmp_path)
    log._handle.close()          # simulate the file going away mid-run
    log.emit(tl.PROGRESS)        # must not raise

    assert log.write_errors == 1
    assert log.records_written == 0


# =====================================================================
# READING BACK
# =====================================================================


def test_a_truncated_final_line_is_tolerated(tmp_path):
    """
    Not hypothetical. Ctrl-C on a long run, or the OS, can leave the
    last buffered write partial. Track A makes the same allowance for
    their log under the fsync experiment. A parser that raises on the
    last line makes an otherwise complete run unreadable.
    """
    path = tmp_path / "t.jsonl"
    with make_log(tmp_path) as log:
        log.emit(tl.RUN_STARTED)
        log.emit(tl.PROGRESS)

    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"event_type": "progress", "elapsed')  # killed mid-write

    rows = TimingLog.read(path)
    assert len(rows) == 2
    assert rows[0]["event_type"] == tl.RUN_STARTED


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    with make_log(tmp_path) as log:
        log.emit(tl.RUN_STARTED)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n\n")
    assert len(TimingLog.read(path)) == 1


# =====================================================================
# BUFFERING
# =====================================================================


def test_records_reach_disk_on_flush_not_only_on_close(tmp_path):
    """
    The harness flushes at phase boundaries — spawn complete, storm
    kill, storm restart — so the records either side of an interesting
    moment are durable before the next phase begins.
    """
    log = make_log(tmp_path, flush_every=1000, flush_interval_s=1000)
    log.emit(tl.STORM_KILL)
    log.flush()

    assert len(TimingLog.read(tmp_path / "t.jsonl")) == 1
    log.close()


def test_the_buffer_flushes_itself_after_enough_records(tmp_path):
    """A run must not hold thousands of records in memory waiting for a
    close that a hard kill will never deliver."""
    log = make_log(tmp_path, flush_every=5, flush_interval_s=1000)
    for _ in range(5):
        log.emit(tl.PROGRESS)

    assert len(TimingLog.read(tmp_path / "t.jsonl")) == 5
    log.close()


def test_closing_twice_is_harmless(tmp_path):
    """The context manager and an explicit close can both run on an
    error path."""
    log = make_log(tmp_path)
    log.close()
    log.close()


def test_the_directory_is_created_if_missing(tmp_path):
    """A run must not fail at the end because logs/ was not there."""
    log = TimingLog(
        tmp_path / "deep" / "deeper" / "t.jsonl",
        run_id="r", experiment="x", n_stations=1,
    )
    log.emit(tl.RUN_STARTED)
    log.close()
    assert (tmp_path / "deep" / "deeper" / "t.jsonl").is_file()


def test_appending_to_an_existing_file_keeps_both_runs(tmp_path):
    """
    Opened in append mode deliberately: two runs with the same name
    concatenate rather than one silently erasing the other. The envelope
    carries run_id, so the two are separable afterwards — losing a
    previous run's data is not.
    """
    with make_log(tmp_path, run_id="first0000001") as log:
        log.emit(tl.RUN_STARTED)
    with make_log(tmp_path, run_id="second000001") as log:
        log.emit(tl.RUN_STARTED)

    ids = {r["run_id"] for r in TimingLog.read(tmp_path / "t.jsonl")}
    assert ids == {"first0000001", "second000001"}


def test_event_type_constants_are_distinct():
    """Guards against a copy-paste duplicate collapsing two event kinds
    into one, which would be invisible until analysis."""
    names = [
        tl.RUN_STARTED, tl.RUN_FINISHED, tl.STATION_SPAWNED,
        tl.STATION_FINISHED, tl.STATION_CRASHED, tl.PROGRESS,
        tl.FLEET_SNAPSHOT, tl.STORM_KILL, tl.STORM_RESTART, tl.WATCHER_ERROR,
    ]
    assert len(set(names)) == len(names)
