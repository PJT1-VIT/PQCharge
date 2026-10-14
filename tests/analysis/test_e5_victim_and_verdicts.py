"""
E5 after Track A's A-F7 (PR #46): the victim runs OUTSIDE the load generator,
and the results page must give the same verdicts as Track A's
`python -m experiments.e5_check` for the same server log.
Track C (tests; tracker step 8, T-1).
"""

from __future__ import annotations

from pathlib import Path

from analysis import collect, match
from analysis.measures import e5_security
from experiments import e5_check
from tests.analysis.fakes import T0, Diaries

VERDICT_WORD = {"STOPPED": "stopped", "DONE": "done", "not seen": "not_seen", "CHECK": "check"}


def _e5(d: Diaries, name: str) -> tuple[dict, Path]:
    events, logs = d.write()
    run = collect.read_harness_file(logs / name).runs[0]
    return e5_security.measure(match.match_run(run, collect.read_server_diary(events))), events


def _e5_check_verdicts(events: Path) -> dict:
    """Track A's tool, read from its printed report: the three '-> ...' lines, in order."""
    text = e5_check.report(e5_check.summarise(e5_check.load(events)))
    arrows = [line.split("->", 1)[1].strip() for line in text.splitlines() if "->" in line]
    words = [next(v for k, v in VERDICT_WORD.items() if a.startswith(k)) for a in arrows]
    return dict(zip(("stolen_certificate", "wrong_identity", "curtailment"), words))


def _stolen_certificate(d: Diaries, sid: str, times: int, *, at: float, closes: int | None = None) -> None:
    """The impostor: boot key check rejected, then closed with 1008, `times` times."""
    for k in range(times):
        t = at + k
        d.server(t, "connection_attempt", None, run_id="srv1", source="boot_verifier",
                 transition="pq_auth", station=sid, result="rejected", detail="Rejected",
                 duration_ms=5.0, algorithm="ML-DSA-44", key_id="k" * 16, trigger="boot")
        if closes is None or k < closes:
            d.server(t + 0.01, "connection_closed", sid, run_id="srv1", outcome="rejected",
                     reason="pq_auth_failed")


def test_the_victim_outside_the_load_generator_is_counted(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e5", n=2, mode="hybrid", charge_s=20)       # CP0001, CP0002
    _stolen_certificate(d, "CP0003", 3, at=T0 + 3)                              # the victim's window
    d.server(T0 + 8, "connection_attempt", "CP0003", run_id="srv1", outcome="rejected",
             transition="identity_check", certificate_common_name="CP0004")
    for sid in info["ids"]:
        d.server(T0 + 9, "message_sent", sid, run_id="srv1", outcome="success",
                 dispatched=True, action="SetChargingProfile")
        d.server(T0 + 10, "message_sent", sid, run_id="srv1", outcome="success",
                 dispatched=True, action="ClearChargingProfile")
    e5, _ = _e5(d, info["name"])
    assert (e5["pq_cut_off"], e5["identity_rejected"], e5["outside_ids"]) == (3, 1, ["CP0003"])
    assert e5["pq_by_trigger"]["boot"] == {"checks": 3, "passed": 0, "rejected": 3}
    assert e5["curtailment"] == {"SetChargingProfile": {"success": 2}, "ClearChargingProfile": {"success": 2}}
    assert e5["verdicts"] == {"stolen_certificate": "stopped", "wrong_identity": "stopped",
                              "curtailment": "done"}


def test_the_results_page_and_e5_check_agree(tmp_path):
    # Three logs: every attack seen; nothing seen; rejections without matching closes.
    cases = []
    d = Diaries(tmp_path / "all")
    info = d.fleet_run(experiment="e5", n=2, mode="hybrid", charge_s=20)
    _stolen_certificate(d, "CP0045", 4, at=T0 + 3)
    d.server(T0 + 8, "connection_attempt", "CP0003", run_id="srv1", outcome="rejected",
             transition="identity_check", certificate_common_name="CP0004")
    d.server(T0 + 9, "message_sent", "CP0001", run_id="srv1", outcome="success",
             dispatched=True, action="SetChargingProfile")
    cases.append((d, info))

    d = Diaries(tmp_path / "none")
    cases.append((d, d.fleet_run(experiment="e5", n=2, mode="hybrid", charge_s=20)))

    d = Diaries(tmp_path / "differ")
    info = d.fleet_run(experiment="e5", n=2, mode="hybrid", charge_s=20)
    _stolen_certificate(d, "CP0045", 3, at=T0 + 3, closes=1)                    # 3 rejected, 1 closed
    cases.append((d, info))

    seen = []
    for d, info in cases:
        e5, events = _e5(d, info["name"])
        assert e5["verdicts"] == _e5_check_verdicts(events)
        seen.append(e5["verdicts"]["stolen_certificate"])
    assert seen == ["stopped", "not_seen", "check"]
