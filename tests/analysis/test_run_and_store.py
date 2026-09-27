"""
The whole pipeline and the one results file (analysis/run.py, store.py). Track C, Phase C6.

Guards the promises made to the team: one command, one results file,
re-running a setup replaces it, the page is written next to it, and the
automatic run after the load generator can never fail a run.
"""

from __future__ import annotations

import json

from analysis import run, store
from harness.load_generator import build_parser
from tests.analysis.fakes import T0, Diaries


def _build(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=4, mode="classical", run_id="c4", server_run_id="s1")
    d.fleet_run(experiment="e1", n=4, mode="pqc", run_id="p4", server_run_id="s2",
                start=T0 + 100, connect_ms=[30.0, 31.0, 32.0, 33.0])
    d.fleet_run(experiment="e1", n=8, mode="classical", run_id="c8", server_run_id="s3",
                start=T0 + 200)
    return d


def test_one_command_builds_every_slot_and_the_comparisons(tmp_path):
    events, logs = _build(tmp_path).write()
    results = run.build(events, logs)
    assert results["slot_order"] == ["e1|n4|classical|tls", "e1|n4|pqc|tls", "e1|n8|classical|tls"]
    assert results["comparisons"]["e1_vs_n"]["e1 · classical · TLS"][1]["n"] == 8
    over = results["comparisons"]["overhead_vs_classical"]
    assert over and over[0]["mode"] == "pqc" and over[0]["median_ratio"] > 1


def test_rerunning_a_setup_replaces_its_slot(tmp_path):
    d = _build(tmp_path)
    # the same setup again, later, and faster
    d.fleet_run(experiment="e1", n=4, mode="pqc", run_id="p4-again", server_run_id="s9",
                start=T0 + 300, connect_ms=[5.0, 5.0, 5.0, 5.0])
    events, logs = d.write()
    results = run.build(events, logs)
    slot = results["slots"]["e1|n4|pqc|tls"]
    assert slot["harness_run_id"] == "p4-again"
    assert slot["e1"]["station_connect_ms"]["median"] == 5.0
    assert results["sources"]["runs_found"] == 4 and results["sources"]["runs_kept"] == 3


def test_the_results_and_the_page_are_written_side_by_side(tmp_path):
    events, logs = _build(tmp_path).write()
    out = tmp_path / "out"
    path = store.write(run.build(events, logs), out)
    data = json.loads(path.read_text())          # strict JSON: no NaN anywhere
    assert data["format_version"] == store.FORMAT_VERSION
    js = (out / "results.js").read_text()
    assert js.startswith("window.PQCHARGE_RESULTS = {")
    for name in ("report.html", "charts.js", "vendor/echarts.min.js"):
        assert (out / name).exists(), name
    assert not list(out.glob("*.tmp")), "temporary files are renamed into place"


def test_the_command_line_runs_end_to_end(tmp_path, capsys):
    events, logs = _build(tmp_path).write()
    out = tmp_path / "out"
    code = run.main(["--events", str(events), "--logs", str(logs), "--out", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "e1|n4|pqc|tls" in printed and "report.html" in printed


def test_the_automatic_analysis_after_a_run_never_raises(tmp_path, capsys):
    run.analyse_after_run(tmp_path / "missing" / "e1_n1_classical.jsonl",
                          events=tmp_path / "missing.jsonl", out=tmp_path / "out")
    # a missing diary is reported in the results, not raised
    assert (tmp_path / "out" / "results.json").exists()


def test_the_load_generator_analyses_by_default_and_can_be_told_not_to():
    parser = build_parser()
    assert parser.parse_args(["--n", "1"]).no_analyse is False
    assert parser.parse_args(["--n", "1", "--no-analyse"]).no_analyse is True
