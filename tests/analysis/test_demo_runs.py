"""
Runbook v1.3 (2026-10-14): one demo run analysed as one unit, the page's
bundle of views, and "All runs combined". Track C (tests).

    python -m analysis.run --run demo5
        -> only demo5's files (main + halt servers analysed apart),
           copied to evidence/demos/demo5/ (never a .log or .db),
           saved as analysis/output/runs/demo5.json -- REPLACED on a re-run,
           the page opens on demo5.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from analysis import collect, demo, store
from analysis import run as arun
from tests.analysis.fakes import T0, Diaries


def make_demo(logs: Path, name: str, tmp: Path, *, start: float = T0, halt: bool = True,
              legacy: bool = True, tls_refused: int = 0) -> None:
    """Write one demo run's files into `logs`, as the runbook names them."""
    main = Diaries(tmp / f"{name}_main")
    main.fleet_run(experiment=name, n=6, mode="hybrid", run_id=f"{name}a", start=start, migrate=True,
                   connected_lines=True)
    if legacy:
        main.fleet_run(experiment=f"{name}_legacy", n=2, mode="hybrid", run_id=f"{name}b",
                       first_id=8, start=start + 0.5, connected_lines=True)
    for k in range(tls_refused):
        main.server(start + 3 + k, "connection_failed", None, mode="hybrid", outcome="rejected",
                    reason="tls_rejected", tls_error="CERTIFICATE_VERIFY_FAILED",
                    detail="certificate not signed by our CA (forged, or from an old CA)", tls_detail="x")
    ev, mlogs = main.write()
    shutil.copyfile(ev, logs / f"{name}_events.jsonl")
    for p in mlogs.glob("*_n*_*.jsonl"):
        shutil.copyfile(p, logs / p.name)
    if halt:
        h = Diaries(tmp / f"{name}_halt")
        h.fleet_run(experiment=f"halt_{name}", n=4, mode="hybrid", run_id=f"{name}h", start=start + 1,
                    connected_lines=True)
        hev, hlogs = h.write()
        shutil.copyfile(hev, logs / f"{name}_halt_events.jsonl")
        for p in hlogs.glob("*_n*_*.jsonl"):
            shutil.copyfile(p, logs / p.name)


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


def test_only_the_named_runs_files_are_picked(tmp_path, logs):
    make_demo(logs, "demo5", tmp_path / "a")
    make_demo(logs, "demo50", tmp_path / "b", start=T0 + 500)      # a prefix twin
    (logs / "demo5_server.log").write_text("private key material (L04)")
    (logs / "demo5.db").write_bytes(b"sqlite")
    f = demo.find_run_files("demo5", logs)
    assert f.main_events.name == "demo5_events.jsonl"
    assert f.halt_events.name == "demo5_halt_events.jsonl"
    assert sorted(p.name for p in f.main_testers) == ["demo5_legacy_n2_hybrid.jsonl", "demo5_n6_hybrid.jsonl"]
    assert [p.name for p in f.halt_testers] == ["halt_demo5_n4_hybrid.jsonl"]
    assert all("demo50" not in p.name for p in f.all())


def test_a_bad_name_or_a_missing_run_is_a_clear_error(tmp_path, logs):
    with pytest.raises(ValueError):
        demo.check_name("demo_5")
    with pytest.raises(demo.RunNotFound):
        demo.locate("demo9", logs, tmp_path / "evidence")


def test_evidence_copy_replaces_the_old_copy_and_never_takes_logs_or_db(tmp_path, logs):
    make_demo(logs, "demo5", tmp_path / "a")
    (logs / "demo5_server.log").write_text("secret")
    ev = tmp_path / "evidence"
    stale = ev / "demo5" / "demo5_old_n9_hybrid.jsonl"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}\n")
    keep = ev / "demo5" / "README.txt"
    keep.write_text("notes")
    files, src = demo.locate("demo5", logs, ev)
    assert src == "logs"
    demo.save_evidence(files, ev)
    names = sorted(p.name for p in (ev / "demo5").iterdir())
    assert "demo5_old_n9_hybrid.jsonl" not in names and "README.txt" in names
    assert not any(n.endswith((".log", ".db")) for n in names)
    assert len([n for n in names if n.endswith(".jsonl")]) == 5


def test_main_and_halt_are_analysed_apart(tmp_path, logs):
    make_demo(logs, "demo5", tmp_path / "a", tls_refused=2)
    view = demo.analyse("demo5", logs, tmp_path / "evidence")
    servers = {view["slots"][k]["server"] for k in view["slot_order"]}
    assert servers == {"main", "halt"}
    assert "demo5|n8|hybrid|tls" in view["slots"]               # 6 + 2 legacy joined (L38)
    assert "halt_demo5|n4|hybrid|tls" in view["slots"]
    halt = view["slots"]["halt_demo5|n4|hybrid|tls"]
    assert halt["overview"]["stations"]["spawned"] == 4          # never mixed with the main fleet
    r = view["summary"]
    assert r["attacks"]["tls_refused"] == 2
    assert r["migration"]["hybrid"] is not None
    assert view["source"] == "logs" and (tmp_path / "evidence" / "demo5").is_dir()


def test_re_analysing_a_run_replaces_its_file_and_the_page_opens_on_it(tmp_path, logs):
    make_demo(logs, "demo5", tmp_path / "a")
    make_demo(logs, "demo6", tmp_path / "b", start=T0 + 3600)
    out, ev = tmp_path / "out", tmp_path / "evidence"
    assert arun.main(["--run", "demo5", "--logs", str(logs), "--evidence", str(ev), "--out", str(out), "--quiet"]) == 0
    assert arun.main(["--run", "demo6", "--logs", str(logs), "--evidence", str(ev), "--out", str(out), "--quiet"]) == 0
    before = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert arun.main(["--run", "demo5", "--logs", str(logs), "--evidence", str(ev), "--out", str(out), "--quiet"]) == 0
    after = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert before == after                                       # nothing new was created
    assert sorted(p.name for p in (out / "runs").iterdir()) == ["demo5.json", "demo6.json"]
    bundle = json.loads((out / "results.json").read_text())
    assert bundle["format_version"] == store.FORMAT_VERSION == 4
    assert bundle["current"] == "demo5"                          # the run analysed last
    assert bundle["view_order"] == ["demo6", "demo5", "combined"]  # newest run first
    assert (out / "results.js").read_text().startswith("window.PQCHARGE_RESULTS = {")


def test_a_run_only_in_evidence_is_analysed_from_there(tmp_path, logs):
    make_demo(logs, "demo5", tmp_path / "a")
    ev = tmp_path / "evidence"
    demo.save_evidence(demo.locate("demo5", logs, ev)[0], ev)
    empty_logs = tmp_path / "other_laptop_logs"
    empty_logs.mkdir()
    view = demo.analyse("demo5", empty_logs, ev)
    assert view["source"] == "evidence"
    out = tmp_path / "out"
    assert arun.main(["--all-runs", "--logs", str(empty_logs), "--evidence", str(ev),
                      "--out", str(out), "--quiet"]) == 0
    assert (out / "runs" / "demo5.json").exists()


def test_the_experiments_analysis_does_not_take_the_page_off_the_demo_run(tmp_path, logs):
    make_demo(logs, "demo5", tmp_path / "a")
    out = tmp_path / "out"
    arun.main(["--run", "demo5", "--logs", str(logs), "--evidence", str(tmp_path / "ev"), "--out", str(out), "--quiet"])
    e = Diaries(tmp_path / "exp")
    e.fleet_run(experiment="e1", n=3, mode="classical")
    events, elogs = e.write()
    arun.analyse_after_run(elogs / "e1_n3_classical.jsonl", events=[str(events)], out=out)   # automatic
    bundle = json.loads((out / "results.json").read_text())
    assert bundle["current"] == "demo5" and "experiments" in bundle["view_order"]
    arun.main(["--events", str(events), "--logs", str(elogs), "--out", str(out), "--quiet"])  # by hand
    assert json.loads((out / "results.json").read_text())["current"] == "experiments"


def test_combined_puts_runs_side_by_side_and_pools_their_samples(tmp_path, logs):
    make_demo(logs, "demo5", tmp_path / "a", tls_refused=1)
    make_demo(logs, "demo6", tmp_path / "b", start=T0 + 3600, tls_refused=2)
    v5 = demo.analyse("demo5", logs, tmp_path / "ev")
    v6 = demo.analyse("demo6", logs, tmp_path / "ev")
    c = demo.combine([v5, v6])
    assert [r["name"] for r in c["runs"]] == ["demo5", "demo6"]
    assert c["totals"]["runs"] == 2 and c["totals"]["tls_refused"] == 3
    n5, n6 = len(v5["samples"]["connect_ms"]), len(v6["samples"]["connect_ms"])
    assert n5 == 12 and c["pooled"]["connect_ms"]["n"] == n5 + n6     # 6 + 2 + 4 chargers each
    mig = v5["samples"]["key_check_ms"]["migration"] + v6["samples"]["key_check_ms"]["migration"]
    assert c["pooled"]["key_check_ms"]["migration"]["n"] == len(mig)
    assert sum(b[1] for b in c["pooled"]["key_check_hist"]["migration"]) == len(mig)


def test_a_part_started_twice_is_not_chained_into_one_run(tmp_path):
    """Real demo2 halt logs: CP0026-50 started in two windows at once."""
    d = Diaries(tmp_path)
    d.fleet_run(experiment="halt_demo2", n=20, mode="hybrid", run_id="a", first_id=1, charge_s=20)
    d.fleet_run(experiment="halt_demo2", n=25, mode="hybrid", run_id="b", first_id=26, start=T0 + 1, charge_s=10)
    d.fleet_run(experiment="halt_demo2", n=25, mode="hybrid", run_id="c", first_id=26, start=T0 + 6, charge_s=4)
    _events, logs = d.write()
    files = [collect.read_harness_file(p) for p in collect.find_harness_files(logs)]
    runs = collect.join_split_runs(collect.all_harness_runs(files))
    sizes = sorted(r.n_stations for r in runs)
    assert sizes == [25, 45]                                     # never 70
    joined = next(r for r in runs if r.n_stations == 45)
    assert joined.run_id == "a+b"                                # the copy that ran longer alongside


def test_the_page_has_the_view_bar_and_no_s1():
    web = Path(store.WEB_DIR)
    html = (web / "report.html").read_text(encoding="utf-8")
    js = (web / "charts.js").read_text(encoding="utf-8")
    assert 'href="#s1"' not in html and "renderS1" not in js and '"S1"' not in js
    for code in ("function renderViewBar(", "function renderCombined(", "function renderDemoSummary(",
                 "function asBundle(", "Refused at TLS", "view="):
        assert code in js, code


def test_the_page_script_parses():
    import shutil as sh
    import subprocess

    node = sh.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine")
    r = subprocess.run([node, "--check", str(Path(store.WEB_DIR) / "charts.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
