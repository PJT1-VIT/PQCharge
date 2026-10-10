"""
The experiment measures (analysis/measures/). Track C, Phase C6.

Each test builds a run whose answer is known in advance (tests/analysis/
fakes.py) and checks the analysis reproduces it exactly.
"""

from __future__ import annotations

import pytest

from analysis import collect, match
from analysis.measures import (
    e1_handshake, e2_storm, e3_migration, e4_sizes, e5_security, e6_nodes, overview,
)
from tests.analysis.fakes import T0, Diaries


def _matched(tmp_path, *, experiment="e1", n=5, mode="classical", **kw):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment=experiment, n=n, mode=mode, **kw)
    events, logs = d.write()
    diary = collect.read_server_diary(events)
    run = collect.read_harness_file(logs / info["name"]).runs[0]
    return match.match_run(run, diary), diary, d


# -- OVERVIEW -------------------------------------------------------------------


def test_overview_counts_sessions_energy_and_readings(tmp_path):
    m, _, _ = _matched(tmp_path, n=4, charge_s=5)
    ov = overview.measure(m)
    assert ov["stations"]["spawned"] == 4 and ov["stations"]["finished_ok"] == 4
    assert ov["sessions"]["stations_with_completed_session"] == 4
    # 4 chargers x 7400 W x 5 s = 41.11 Wh
    assert abs(ov["sessions"]["energy_wh_total"] - 4 * 7400 * 5 / 3600) < 1e-3
    # started + 5 updates + ended, per charger
    assert ov["meter"]["readings"] == 4 * 7
    assert ov["meter"]["missing_events"] == 0


def test_fleet_power_peaks_at_every_charger_at_full_power(tmp_path):
    m, _, _ = _matched(tmp_path, n=4)
    peak = max(v for _, v in overview.measure(m)["timeline"]["power_w"])
    assert peak == 4 * 7400.0


def test_the_live_picture_empties_the_moment_the_server_is_killed(tmp_path):
    m, _, _ = _matched(tmp_path, experiment="e2", n=4, charge_s=10,
                       storm={"kill_at": 4, "outage": 2, "recovery": [0.5, 0.6, 0.7, 0.8]})
    tl = overview.measure(m)["timeline"]
    during = [v for x, v in tl["connected"] if 4.2 <= x <= 5.8]
    assert during and max(during) == 0, "nobody is connected to a dead server"


# -- E1 -------------------------------------------------------------------------


def test_e1_uses_the_first_connection_and_keeps_reconnections_apart(tmp_path):
    times = [11.0, 12.0, 13.0, 14.0, 15.0]
    m, _, _ = _matched(tmp_path, experiment="e2", n=5, connect_ms=times, charge_s=10,
                       storm={"kill_at": 4, "outage": 2, "recovery": [0.5] * 5})
    e1 = e1_handshake.measure(m)
    assert e1["station_side_available"]
    assert e1["station_connect_ms"]["n"] == 5
    assert e1["station_connect_ms"]["median"] == 13.0
    assert e1["reconnect_connect_ms"]["median"] == 39.0     # fakes: 3x on reconnect
    assert e1["server_scope"] == "server_upgrade"
    assert e1["tls"]["version"] == "TLSv1.3"
    assert e1["bytes_per_connection"]["n"] == 5


# -- E2 -------------------------------------------------------------------------


def test_e2_recovery_times_are_measured_from_the_restart(tmp_path):
    recovery = [0.1 * (i + 1) for i in range(19)] + [None]   # 20 chargers, one never back
    m, _, _ = _matched(tmp_path, experiment="e2", n=20, charge_s=12,
                       storm={"kill_at": 4, "outage": 3, "recovery": recovery})
    e2 = e2_storm.measure(m)
    assert e2 is not None
    assert e2["population"] == 20 and e2["recovered"] == 19 and e2["unrecovered"] == 1
    assert e2["unrecovered_ids"] == ["CP0020"]
    assert e2["t50_s"] == pytest.approx(1.0, abs=1e-3)       # 10th of 20
    assert e2["t95_s"] == pytest.approx(1.9, abs=1e-3)       # 19th of 20
    assert e2["t100_s"] is None                              # never reached
    assert e2["outage_s"] == pytest.approx(3.0, abs=1e-3)
    assert e2["restart_detected_by"] == "server_started"
    assert e2["curve"][0] == [0.0, 0.0] and e2["curve"][-1][1] == pytest.approx(95.0)


def test_e2_splits_lost_readings_by_where_they_were_lost(tmp_path):
    m, _, _ = _matched(tmp_path, experiment="e2", n=3, charge_s=10,
                       storm={"kill_at": 4, "outage": 2, "recovery": [0.2, 0.3, 0.4]})
    g = e2_storm.measure(m)["integrity"]
    assert g["events_lost_in_transit"] == 2
    assert g["events_dropped_by_agent_queue"] == 0
    assert g["sessions_in_flight_at_kill"] == 3
    assert g["sessions_resumed_after_restart"] == 3
    assert g["readings_rejected_as_stale"] == 3


def test_a_run_without_a_storm_has_no_e2_result(tmp_path):
    m, _, _ = _matched(tmp_path)
    assert e2_storm.measure(m) is None


# -- E3 -------------------------------------------------------------------------


def test_e3_follows_the_canary_the_waves_and_the_rollback(tmp_path):
    m, _, _ = _matched(tmp_path, experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    e3 = e3_migration.measure(m)
    assert e3 is not None
    assert e3["final_counts"]["migrated"] == 4
    assert e3["final_counts"]["rolled_back"] == 4
    assert e3["rollbacks"] == 1
    # The real orchestrator HALTS on a rollback (no migration_completed), so
    # the migration ends at the wave_rolled_back line, 3 s after it began.
    assert e3["duration_s"] == pytest.approx(3.0, abs=1e-3)
    assert e3["phase"] == "rolled_back"
    assert e3["pq_checks"]["passed"] == 4 and e3["verification"] == "authenticated"
    assert [w["is_canary"] for w in e3["waves"]] == [True, False, False]
    assert e3["charging"]["sessions_running_at_start"] == 8
    assert e3["charging"]["sessions_disturbed"] == 0
    total = [sum(e3["state_timeline"][s][i][1] for s in e3_migration.STATES)
             for i in range(len(e3["state_timeline"]["pending"]))]
    assert set(total) == {8}, "every charger is in exactly one state at every moment"


def test_a_run_without_a_migration_has_no_e3_result(tmp_path):
    m, _, _ = _matched(tmp_path)
    assert e3_migration.measure(m) is None


def test_e3_counts_a_session_disturbed_when_its_charger_drops_mid_migration(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    d.server(T0 + 2.5, "connection_closed", "CP0003", run_id="srv1", outcome="failure")
    events, logs = d.write()
    run = collect.read_harness_file(logs / info["name"]).runs[0]
    m = match.match_run(run, collect.read_server_diary(events))
    c = e3_migration.measure(m)["charging"]
    assert c["sessions_disturbed"] == 1 and c["disturbed_station_ids"] == ["CP0003"]


# -- E5 -------------------------------------------------------------------------


def test_e5_counts_a_rejected_identity(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e5", n=3, mode="classical")
    d.server(T0 + 1.0, "connection_attempt", "CP0002", run_id="srv1", outcome="rejected",
             transition="identity_check", certificate_common_name="CP0003", identity_matches=False)
    events, logs = d.write()
    run = collect.read_harness_file(logs / info["name"]).runs[0]
    e5 = e5_security.measure(match.match_run(run, collect.read_server_diary(events)))
    assert e5["identity_checks"] == 1 and e5["identity_rejected"] == 1
    assert e5["identity_rejections"][0]["certificate_name"] == "CP0003"


# -- E6 / hardware ----------------------------------------------------------------


def test_the_pi_is_found_as_the_hardware_node_and_tester_chargers_are_not(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=3, mode="classical")
    d.server(T0 + 1, "connection_established", "CP0100", run_id="hw", outcome="success")
    d.server(T0 + 1.1, "state_changed", "CP0100", run_id="hw", transition="booted")
    d.server(T0 + 9, "transaction_ended", "CP0100", run_id="hw", energy_wh=0.25)
    d.server(T0 + 1, "connection_established", "EXT01", run_id="hw", outcome="success")
    events, _ = d.write()
    nodes = e6_nodes.measure(collect.read_server_diary(events), ["CP0001", "CP0002", "CP0003"])
    by_id = {n["station_id"]: n for n in nodes}
    assert set(by_id) == {"CP0100", "EXT01"}
    assert by_id["CP0100"]["kind"] == "hardware"
    assert by_id["CP0100"]["completed_session"] and by_id["CP0100"]["energy_wh"] == 0.25
    assert by_id["EXT01"]["kind"] == "external" and not by_id["EXT01"]["accepted"]


# -- E4 -------------------------------------------------------------------------


def test_e4_measures_the_real_classical_certificates_against_the_limits():
    e4 = e4_sizes.measure()
    classical = {r["label"]: r for r in e4["rows"] if r["group"] == "classical"}
    cert = classical["Station certificate"]
    assert cert["limit"] == 5500 and cert["within_limit"] is True
    assert cert["pem_bytes"] > cert["bytes"], "PEM is base64 text: larger than DER"
    assert classical["Chain (station + root)"]["limit"] == 10000


def test_e4_measures_the_post_quantum_artifacts_and_the_wire_messages():
    pytest.importorskip("quantcrypt", reason="PQ backend; see requirements.txt")
    rows = {(r["group"], r["label"]): r for r in e4_sizes.measure()["rows"]}
    assert rows[("post-quantum", "Signature")]["bytes"] == 2420      # ML-DSA-44
    assert rows[("post-quantum", "Public key")]["bytes"] == 1312
    # C-F5: Contract 7's messages; InstallPQAuth (a server-made private key) is gone.
    reply = rows[("on the wire", "RequestPQEnrolment reply (public key)")]["bytes"]
    assert reply > 1312 * 4 // 3, "base64 of the 1312-byte public key, inside JSON"
    assert ("on the wire", "InstallPQAuth payload") not in rows
    assert rows[("on the wire", "RequestPQEnrolment payload")]["bytes"] < 100
