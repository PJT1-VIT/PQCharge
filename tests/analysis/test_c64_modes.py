"""
C-P4 (C6.4) — the analysis reads Contract 7's security modes. Track C (tests).

Each test pins one rule:

    R1  E1 secure-ready = first challenge of a connection opened WITH a key
    R2  a migration's first challenge (no key at connect) is NOT secure-ready
    R3  E1 "ready" is like for like: classical = boot accepted, hybrid =
        boot + key check; the comparison reports the added milliseconds
    R4  E2 "recovered" in hybrid: key-checked chargers also need a PASSED
        key check after the restart (Contract 7 section 7.7)
    R5  E3 counts the migration's key checks only; boot checks apart
    R6  E3 counts chargers that made their own key (pq_enrolled)
    R7  saved keys at start: warn in a migration run, note otherwise
    R8  the tester's mode must match the server's
    R9  a hybrid run with keys but no boot checks is flagged
    R10 E5 splits key checks by trigger and counts chargers cut off
"""

from __future__ import annotations

import pytest

from analysis import check, collect, match, run
from analysis.measures import compare, e1_handshake, e2_storm, e3_migration, e5_security, overview
from tests.analysis.fakes import T0, Diaries


def _matched(d: Diaries, name: str):
    events, logs = d.write()
    diary = collect.read_server_diary(events)
    harness = collect.read_harness_file(logs / name).runs[0]
    return match.match_run(harness, diary)


def _h(info: dict, mode: str, n: int, experiment: str):
    return dict(run_id="run1", experiment=experiment, n=n, mode=mode, tls=True)


def add_ready_lines(d: Diaries, info: dict, *, mode: str, experiment: str, key_held: bool,
                    boot_ms: list[float], secure_ms: list[float] | None = None,
                    connection: int = 1) -> None:
    """station_connected (with pq_key_held), station_booted, station_authenticated."""
    n = len(info["ids"])
    H = _h(info, mode, n, experiment)
    for i, sid in enumerate(info["ids"]):
        t = T0 + 0.1 + i * 0.01 + 0.001 * connection
        d.harness(info["name"], t, "station_connected", station_id=sid, connect_ms=5.0,
                  connection=connection, attempt=connection, pq_key_held=key_held,
                  pq_key_id="k" * 16 if key_held else None, **H)
        d.harness(info["name"], t + 0.0001, "station_booted", station_id=sid,
                  connection=connection, since_connect_ms=boot_ms[i], **H)
        if secure_ms is not None:
            for k in (1, 2):   # the second challenge must never count
                d.harness(info["name"], t + 0.0002 * k, "station_authenticated", station_id=sid,
                          connection=connection, challenge_no=k,
                          since_connect_ms=secure_ms[i] * k, sign_ms=1.0 + i, key_id="k" * 16, **H)


# -- R1 / R3 ------------------------------------------------------------------


def test_secure_ready_is_the_first_challenge_after_connecting_with_a_key(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e1", n=3, mode="hybrid")
    add_ready_lines(d, info, mode="hybrid", experiment="e1", key_held=True,
                    boot_ms=[20.0, 30.0, 40.0], secure_ms=[60.0, 70.0, 80.0])
    e1 = e1_handshake.measure(_matched(d, info["name"]))

    assert e1["secure_ready_ms"]["n"] == 3 and e1["secure_ready_ms"]["median"] == 70.0
    assert e1["boot_ready_ms"]["median"] == 30.0
    assert e1["ready_basis"] == "secure-ready (boot + key check)"
    assert e1["ready_ms"]["median"] == 70.0
    assert e1["sign_ms"]["n"] == 6                   # every signature, both challenges
    assert e1["chargers_with_key_at_connect"] == 3
    assert e1["secure_ready_reconnect_ms"]["n"] == 0


def test_a_classical_run_is_ready_at_boot_accepted(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e1", n=3, mode="classical")
    add_ready_lines(d, info, mode="classical", experiment="e1", key_held=False,
                    boot_ms=[20.0, 30.0, 40.0])
    e1 = e1_handshake.measure(_matched(d, info["name"]))
    assert e1["ready_basis"] == "boot accepted" and e1["ready_ms"]["median"] == 30.0
    assert e1["secure_ready_ms"] == {"n": 0}


def test_ready_is_compared_like_for_like(tmp_path):
    slots = []
    for mode, secure in (("classical", None), ("hybrid", [60.0, 70.0, 80.0])):
        d = Diaries(tmp_path / mode)
        info = d.fleet_run(experiment="e1", n=3, mode=mode)
        add_ready_lines(d, info, mode=mode, experiment="e1", key_held=secure is not None,
                        boot_ms=[20.0, 30.0, 40.0], secure_ms=secure)
        slots.append(run.analyse_run(_matched(d, info["name"])))
    cmp = compare.measure(slots)
    (row,) = cmp["ready_overhead_vs_classical"]
    assert row["mode"] == "hybrid" and row["basis"] == "secure-ready (boot + key check)"
    assert (row["median_ms"], row["classical_median_ms"], row["added_median_ms"]) == (70.0, 30.0, 40.0)
    assert set(cmp["ready_vs_n"]) == {"e1 · classical · TLS", "e1 · hybrid · TLS"}


# -- R2 -------------------------------------------------------------------------


def test_a_migrations_first_challenge_is_not_a_secure_ready_time(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=3, mode="hybrid")
    add_ready_lines(d, info, mode="hybrid", experiment="e3", key_held=False,
                    boot_ms=[20.0, 30.0, 40.0], secure_ms=[5000.0, 6000.0, 7000.0])
    e1 = e1_handshake.measure(_matched(d, info["name"]))
    assert e1["secure_ready_ms"] == {"n": 0}
    assert e1["ready_basis"] == "boot accepted"      # nothing secure-ready to report


def test_a_reconnection_with_a_key_is_a_secure_ready_reconnect(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e2", n=2, mode="hybrid")
    add_ready_lines(d, info, mode="hybrid", experiment="e2", key_held=True,
                    boot_ms=[20.0, 30.0], secure_ms=[60.0, 70.0])
    add_ready_lines(d, info, mode="hybrid", experiment="e2", key_held=True,
                    boot_ms=[25.0, 35.0], secure_ms=[90.0, 110.0], connection=2)
    e1 = e1_handshake.measure(_matched(d, info["name"]))
    assert e1["secure_ready_ms"]["median"] == 65.0
    assert e1["secure_ready_reconnect_ms"]["n"] == 2 and e1["secure_ready_reconnect_ms"]["median"] == 100.0


# -- R4 -------------------------------------------------------------------------


def boot_check(d: Diaries, t: float, sid: str, result: str = "success", run_id: str = "srv1",
               mode: str = "hybrid") -> None:
    """A boot key check exactly as Contract 7 section 7.6 shapes it."""
    d.server(t, "connection_attempt", None, run_id=run_id, mode=mode, transition="pq_auth",
             station=sid, result=result, detail="", duration_ms=12.0, algorithm="ML-DSA-44",
             key_id="k" * 16, trigger="boot", source="boot_verifier")


def test_hybrid_recovery_waits_for_the_key_check_after_boot(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e2", n=4, mode="hybrid",
                       storm={"kill_at": 4, "outage": 2, "recovery": [0.5, 0.6, 0.7, 0.8]})
    restart = info["storm_restart"]
    ids = info["ids"]
    # CP0001..3 are enrolled: checked at their first boot ...
    for i, sid in enumerate(ids[:3]):
        boot_check(d, T0 + 0.1 + i * 0.01 + 0.025, sid)
    # ... and after the restart: CP0001 passes 0.4 s AFTER its boot line,
    # CP0002 passes at once, CP0003 fails (wrong key) and never passes.
    boot_check(d, restart + 0.9, ids[0], run_id="srv2")
    boot_check(d, restart + 0.6, ids[1], run_id="srv2")
    boot_check(d, restart + 0.71, ids[2], result="rejected", run_id="srv2")
    # CP0004 is not enrolled: the boot alone counts (0.8 s).

    e2 = e2_storm.measure(_matched(d, info["name"]))
    assert e2["recovery_rule"] == "boot + key check" and e2["key_checked_population"] == 3
    assert e2["population"] == 4 and e2["recovered"] == 3 and e2["unrecovered"] == 1
    assert e2["booted_but_key_check_not_passed"] == [ids[2]]
    assert e2["boot_checks_failed_after_restart"] == 1
    assert e2["t100_s"] is None
    assert sorted(round(x, 3) for x in [e2["recovery_s"]["min"], e2["recovery_s"]["max"]]) == [0.6, 0.9]


def test_without_boot_checks_the_rule_is_unchanged(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e2", n=4, mode="classical",
                       storm={"kill_at": 4, "outage": 2, "recovery": [0.5, 0.6, 0.7, 0.8]})
    e2 = e2_storm.measure(_matched(d, info["name"]))
    assert e2["recovery_rule"] == "boot" and e2["recovered"] == 4
    assert e2["t100_s"] == pytest.approx(0.8)


# -- R5 / R6 / R7 -----------------------------------------------------------------


def test_boot_checks_never_inflate_the_migrations_numbers(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="hybrid", migrate=True, charge_s=8)
    boot_check(d, T0 + 0.5, info["ids"][0])
    boot_check(d, T0 + 0.6, info["ids"][1], result="rejected")
    e3 = e3_migration.measure(_matched(d, info["name"]))
    assert e3["pq_checks"]["total"] == 4             # the fake migration's 4, as before
    assert e3["boot_checks"] == {**e3["boot_checks"], "total": 2, "passed": 1, "rejected": 1}


def test_a_hybrid_run_with_only_boot_checks_has_no_migration(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e1", n=2, mode="hybrid")
    boot_check(d, T0 + 0.5, info["ids"][0])
    assert e3_migration.measure(_matched(d, info["name"])) is None


def test_keys_made_by_the_chargers_are_counted(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="hybrid", migrate=True, charge_s=8)
    for k, sid in enumerate(info["ids"][:4]):
        d.server(T0 + 1.3 + k * 0.01, "certificate_installed", None, run_id="srv1", mode="hybrid",
                 transition="pq_enrolled", station=sid, key_id=f"{k:016x}", algorithm="ML-DSA-44",
                 trigger="migration", source="orchestrator")
    e3 = e3_migration.measure(_matched(d, info["name"]))
    assert e3["pq_enrolled"]["count"] == 4
    assert e3["pq_enrolled"]["key_ids"]["CP0004"] == f"{3:016x}"


def _trust(m):
    ov = overview.measure(m)
    e1 = e1_handshake.measure(m)
    e3 = e3_migration.measure(m)
    return check.check_run(m, ov, e1, e3), e3


def _codes(trust):
    return {i["code"]: i["level"] for i in trust["issues"]}


def test_saved_keys_at_start_warn_in_a_migration_run(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="hybrid", migrate=True, charge_s=8)
    H = _h(info, "hybrid", 8, "e3")
    for i, sid in enumerate(info["ids"][:2]):
        d.harness(info["name"], T0 + 0.1 + i * 0.01 + 0.001, "station_connected", station_id=sid,
                  connect_ms=5.0, connection=1, attempt=1, pq_key_held=True, pq_key_id="k" * 16, **H)
    trust, e3 = _trust(_matched(d, info["name"]))
    assert _codes(trust)["started_with_saved_keys"] == "warn"
    assert e3["keys_at_start"] == {"known": True, "count": 2, "station_ids": ["CP0001", "CP0002"]}


def test_saved_keys_without_a_migration_are_only_a_note(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e1", n=2, mode="hybrid")
    add_ready_lines(d, info, mode="hybrid", experiment="e1", key_held=True,
                    boot_ms=[20.0, 30.0], secure_ms=[60.0, 70.0])
    boot_check(d, T0 + 0.5, info["ids"][0])
    trust, _ = _trust(_matched(d, info["name"]))
    assert _codes(trust)["started_with_saved_keys"] == "info"
    assert "hybrid_without_boot_checks" not in _codes(trust)


# -- R8 / R9 -----------------------------------------------------------------------


def test_the_tester_and_server_must_name_the_same_mode(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e1", n=2, mode="hybrid")
    for line in d.server_lines:
        line["crypto_mode"] = "classical"            # the server was started without --mode hybrid
    trust, _ = _trust(_matched(d, info["name"]))
    assert _codes(trust)["mode_mismatch"] == "warn"


def test_matching_modes_raise_nothing(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e1", n=2, mode="hybrid")
    trust, _ = _trust(_matched(d, info["name"]))
    assert "mode_mismatch" not in _codes(trust)


def test_a_hybrid_run_without_boot_checks_is_flagged(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e1", n=2, mode="hybrid")
    add_ready_lines(d, info, mode="hybrid", experiment="e1", key_held=True, boot_ms=[20.0, 30.0])
    trust, _ = _trust(_matched(d, info["name"]))
    assert _codes(trust)["hybrid_without_boot_checks"] == "warn"


# -- R10 -----------------------------------------------------------------------------


def test_e5_splits_checks_by_trigger_and_counts_chargers_cut_off(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e5", n=8, mode="hybrid", migrate=True, charge_s=8)
    boot_check(d, T0 + 0.5, info["ids"][0])
    boot_check(d, T0 + 0.6, info["ids"][1], result="rejected")
    d.server(T0 + 0.61, "connection_closed", info["ids"][1], run_id="srv1", mode="hybrid",
             outcome="failure", reason="pq_auth_failed")
    e5 = e5_security.measure(_matched(d, info["name"]))
    assert e5["pq_by_trigger"]["migration"] == {"checks": 4, "passed": 4, "rejected": 0}
    assert e5["pq_by_trigger"]["boot"] == {"checks": 2, "passed": 1, "rejected": 1}
    assert e5["pq_checks"] == 6 and e5["pq_rejected"] == 1
    assert e5["pq_cut_off"] == 1 and e5["pq_cut_off_ids"] == ["CP0002"]
    assert e5["pq_rejections"][0]["trigger"] == "boot"


def test_old_lines_without_a_trigger_count_as_migration(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    m = _matched(d, info["name"])
    assert len(m.pq_checks("migration")) == 4 and m.pq_checks("boot") == []
