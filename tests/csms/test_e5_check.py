"""A-F7: experiments/e5_check reads the server log the way the live run wrote it."""

from experiments.e5_check import report, summarise
from csms.events import EventLog


def test_three_attacks_are_reported(tmp_path):
    log = EventLog(tmp_path / "e.jsonl")
    for _ in range(3):   # (a) impostor: rejected boot check + one close, each time
        log.emit("connection_attempt", None, transition="pq_auth", station="CP0003",
                 result="rejected", trigger="boot", source="boot_verifier")
        log.emit("connection_closed", "CP0003", outcome="rejected", reason="pq_auth_failed")
    log.emit("connection_attempt", None, transition="pq_auth", station="CP0001",
             result="success", trigger="boot", source="boot_verifier")
    # (b) wrong identity
    log.emit("connection_attempt", "CP0003", outcome="rejected", transition="identity_check",
             certificate_common_name="CP0004", identity_matches=False, identity_check="enforce")
    # (c) curtailment
    for action in ("SetChargingProfile", "ClearChargingProfile"):
        log.emit("message_sent", "CP0001", outcome="success", dispatched=True,
                 action=action, status="Accepted")
    log.close()

    from experiments.e5_check import load
    s = summarise(load(tmp_path / "e.jsonl"))
    assert s["cut_off"] == {"CP0003": 3}
    assert s["identity_rejected"] == [("CP0003", "CP0004")]
    text = report(s)
    assert "3 rejected, 1 passed" in text
    assert text.count("-> STOPPED") == 2 and "-> DONE" in text

    only_victim = summarise(load(tmp_path / "e.jsonl"), station="CP0003")
    assert sum(n for (_sid, r), n in only_victim["boot_checks"].items() if r == "success") == 0
