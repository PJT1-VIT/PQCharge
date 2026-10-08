"""
Phase 6 — the A+B+C integration sessions, re-run in every crypto mode and with
live certificate rotation, on one machine. Produces the green-flag evidence.

Track B (experiments). Copy only (exp/pqc-py314) until the green flag.

Re-creates the 2026-10-02 sessions (evidence/integration_2026-10-02/) with the
same fleets, profiles and expected results, and checks them from the CSMS's own
Contract 3 event log and /api:

    run                fleet                                   expected (of total)
    s1                 3 agents + CP0004 legacy, ws://          3 migrated / 1 incompatible, pq_auth 3/3
    s1b-<mode>         same, mutual TLS, mode = classical |     3 / 1, pq_auth 3/3, every connection in
                       hybrid | pqc                             the mode's group and certificate type
    stage6-<mode>      45 agents + 5 refusers (fake stations),  phase rolled_back, 40 / 5 incompatible /
                       N=50, canary 5, waves of 10              5 rolled back / 0 pending, pq_auth 40/40
    stage6-rotation    same fleet, migration = LIVE CERTIFICATE same counts; 40/40 rotations confirmed;
                       ROTATION (plan Phase 5)                  rotated chargers end on MLKEM768/ML-DSA-44;
                                                                0 sessions split; no private key at the CSMS

Each run gets a fresh --db and --log, --ws-ping-interval 0, and waits for every
started station to be connected before starting the migration (handoff §14).
Server and fleet processes each get their own OPENSSL_CONF (crypto/tls_mode.py).

    python -m experiments.integration_matrix                  # everything (~6-8 min)
    python -m experiments.integration_matrix --runs s1 stage6-rotation
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crypto.tls_mode import EXPECTED_CERTIFICATE, EXPECTED_GROUP, MODE_CONFIG, ROTATION_CONFIG, SERVER_CONFIG  # noqa: E402

MODES = ("classical", "hybrid", "pqc")
S1_PROFILE = ROOT / "tests" / "fixtures" / "fleet_profile_day9_clean.json"
STAGE6_PROFILE = ROOT / "tests" / "fixtures" / "fleet_profile_stage6_n50.json"


@dataclass
class Run:
    name: str
    tls: bool
    mode: str                     # fleet crypto mode ("classical" for the rotation run)
    method: str                   # enrolment | rotation
    agents: int                   # Track C agents CP0001..CP<agents>
    refusers: list[str]           # Track A fake stations (refuse the migration)
    profile: Path
    wave_size: int
    canary: int
    charge_for: int
    expect: dict = field(default_factory=dict)


def _runs() -> list[Run]:
    s1_expect = dict(phase="completed", total=4, migrated=3, incompatible=1, rolled_back=0, pending=0, auth=3)
    st6_expect = dict(phase="rolled_back", total=50, migrated=40, incompatible=5, rolled_back=5, pending=0, auth=40)
    runs = [Run("s1", False, "classical", "enrolment", 3, [], S1_PROFILE, 3, 1, 60, s1_expect)]
    runs += [Run(f"s1b-{m}", True, m, "enrolment", 3, [], S1_PROFILE, 3, 1, 60, s1_expect) for m in MODES]
    refusers = [f"CP{i:04d}" for i in range(46, 51)]
    runs += [Run(f"stage6-{m}", True, m, "enrolment", 45, refusers, STAGE6_PROFILE, 10, 5, 150, st6_expect)
             for m in MODES]
    runs.append(Run("stage6-rotation", True, "classical", "rotation", 45, refusers, STAGE6_PROFILE, 10, 5,
                    150, st6_expect))
    return runs


# -- helpers ---------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(conf: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "OPENSSL_CONF"}
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    if conf is not None:
        env["OPENSSL_CONF"] = str(conf)
    return env


def _api(base: str, path: str, tls: bool) -> dict:
    ctx = None
    if tls:
        ctx = ssl.create_default_context(cafile=str(ROOT / "certs" / "root.pem"))
        ctx.load_cert_chain(str(ROOT / "certs" / "CP0001.crt.pem"), str(ROOT / "certs" / "CP0001.key.pem"))
    with urllib.request.urlopen(base + path, context=ctx, timeout=10) as r:
        return json.loads(r.read())


def _bootstrap(py: str, out: Path) -> None:
    for extra in ([], ["--algorithm", "pqc"]):
        log = out / f"bootstrap{'_pqc' if extra else ''}.txt"
        with open(log, "w", encoding="utf-8") as f:
            rc = subprocess.run([py, "-m", "experiments.bootstrap_pki", "--count", "50", *extra],
                                cwd=ROOT, env=_env(None), stdout=f, stderr=subprocess.STDOUT).returncode
        if rc != 0 or "SUCCESS" not in log.read_text(encoding="utf-8", errors="replace"):
            raise SystemExit(f"bootstrap_pki {' '.join(extra)} failed -- see {log}")


def _stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    deadline = time.time() + 10
    for p in procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()


# -- one run ---------------------------------------------------------------

def run_one(run: Run, py: str, out: Path) -> dict:
    d = out / run.name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    port = _free_port()
    scheme = "wss" if run.tls else "ws"
    base = f"{'https' if run.tls else 'http'}://localhost:{port}"
    fleet_conf = None
    if run.tls:
        fleet_conf = ROTATION_CONFIG if run.method == "rotation" else MODE_CONFIG[run.mode]
    cert_dir = "certs/pqc" if (run.tls and run.mode == "pqc") else "certs"

    server_cmd = [py, "-m", "csms.server", "--port", str(port), "--mode", run.mode,
                  "--db", str(d / "csms.db"), "--log", str(d / "events.jsonl"),
                  "--ws-ping-interval", "0", "--fleet-profile", str(run.profile),
                  "--migration-method", run.method]
    if run.tls:
        server_cmd += ["--tls", "--cert-dir", "certs", "--pq-cert-dir", "certs/pqc",
                       "--tls-identity-check", "enforce"]
    procs: list[subprocess.Popen] = []
    files = []

    def spawn(cmd, conf, name):
        f = open(d / name, "w", encoding="utf-8")
        files.append(f)
        p = subprocess.Popen(cmd, cwd=ROOT, env=_env(conf), stdout=f, stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    result: dict = {"run": run.name}
    try:
        server = spawn(server_cmd, SERVER_CONFIG if run.tls else None, "server.txt")
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("CSMS exited at start-up -- see server.txt")
            try:
                _api(base, "/api/health", run.tls)
                break
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.2)

        fleet_cmd = [py, "-m", "harness.load_generator", "--n", str(run.agents), "--experiment", run.name,
                     "--csms-url", f"{scheme}://localhost:{port}", "--crypto-mode", run.mode,
                     "--charge-for", str(run.charge_for), "--meter-every", "2",
                     "--timing-log", str(d / "timing.jsonl"), "--no-analyse", "--seed", "1"]
        if run.tls:
            fleet_cmd += ["--cert-dir", cert_dir]
        if run.method == "rotation":
            fleet_cmd += ["--rotation-dir", str(d / "rotated")]
        spawn(fleet_cmd, fleet_conf, "fleet.txt")
        if run.refusers:
            fake_cmd = [py, "-m", "tests.fixtures.fake_station", *run.refusers,
                        "--url", f"{scheme}://localhost:{port}", "--charge-for", str(run.charge_for),
                        "--meter-every", "5"]
            if run.tls:
                pq_refusers = run.mode == "pqc"
                fake_cmd += ["--tls", "--cert-dir", "certs/pqc" if pq_refusers else "certs"]
                if pq_refusers:
                    fake_cmd += ["--server-name", "pq.localhost"]
            spawn(fake_cmd, fleet_conf, "refusers.txt")

        expected_connected = run.agents + len(run.refusers)
        connected = 0
        for _ in range(300):
            connected = _api(base, "/api/fleet", run.tls).get("connected_count", 0)
            if connected >= expected_connected:
                break
            time.sleep(0.2)
        result["connected_before_migration"] = connected
        time.sleep(3)  # every charger past boot and into its transaction

        _api(base, f"/api/migration/start?wave_size={run.wave_size}&canary_count={run.canary}"
                   f"&target_mode=pqc", run.tls)
        status = {}
        started = time.time()
        for _ in range(900):
            status = _api(base, "/api/migration", run.tls)
            if status.get("phase") in ("completed", "rolled_back", "failed"):
                break
            time.sleep(0.2)
        result["migration_seconds"] = round(time.time() - started, 1)
        result["status"] = {k: status.get(k) for k in
                            ("phase", "total_stations", "migrated", "incompatible", "rolled_back", "pending")}
        time.sleep(3)  # let rotated chargers settle on their new connection
    finally:
        _stop(procs)
        for f in files:
            f.close()

    result.update(_analyse(run, d))
    result["pass"], result["failures"] = _verdict(run, result)
    (d / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _analyse(run: Run, d: Path) -> dict:
    events = [json.loads(l) for l in (d / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    last_conn: dict[str, tuple] = {}
    groups = collections.Counter()
    auth = collections.Counter()
    rotations = collections.Counter()
    rotation_ms = []
    tx_ids = collections.defaultdict(set)
    cert_bytes = []
    agents = {f"CP{i:04d}" for i in range(1, run.agents + 1)}
    for e in events:
        p = e.get("payload") or {}
        t = e["event_type"]
        if t == "connection_established":
            last_conn[e["station_id"]] = (p.get("tls_group"), p.get("peer_key_type"))
            if e["station_id"] in agents:
                groups[(p.get("tls_group"), p.get("peer_key_type"))] += 1
                if p.get("peer_cert_bytes") is not None:
                    cert_bytes.append(p["peer_cert_bytes"])
        elif t == "connection_attempt" and p.get("transition") == "pq_auth":
            auth[p.get("result")] += 1
        elif t == "connection_attempt" and p.get("transition") == "cert_rotation":
            rotations[p.get("result")] += 1
            if p.get("result") == "success" and p.get("duration_ms") is not None:
                rotation_ms.append(p["duration_ms"])
        elif t.startswith("transaction_") and e["station_id"]:
            tx_ids[e["station_id"]].add(p.get("transaction_id"))
    text = (d / "events.jsonl").read_text(encoding="utf-8") + (d / "server.txt").read_text(
        encoding="utf-8", errors="replace")
    rotation_ms.sort()
    # Client-side connect time (TCP + TLS + WebSocket upgrade), from the
    # harness timing log: the CSMS's own handshake_ms starts AFTER TLS, so it
    # cannot show the cost of a post-quantum handshake. First connections only.
    connect_ms = []
    timing = d / "timing.jsonl"
    if timing.is_file():
        for line in timing.read_text(encoding="utf-8").splitlines():
            t = json.loads(line) if line.strip() else {}
            if t.get("event_type") == "station_connected" and t.get("connection") == 1 and "connect_ms" in t:
                connect_ms.append(t["connect_ms"])
    connect_ms.sort()
    return {
        "client_connect_ms_median": round(connect_ms[len(connect_ms) // 2], 1) if connect_ms else None,
        "charger_cert_bytes": sorted(set(cert_bytes)),
        "agent_connections": {f"{g}/{k}": n for (g, k), n in groups.items()},
        "final_connection": {s: f"{g}/{k}" for s, (g, k) in sorted(last_conn.items())},
        "pq_auth": dict(auth),
        "rotations": dict(rotations),
        "rotation_ms_median": rotation_ms[len(rotation_ms) // 2] if rotation_ms else None,
        "rotation_ms_max": rotation_ms[-1] if rotation_ms else None,
        "sessions_split": sorted(s for s, ids in tx_ids.items() if len(ids) > 1),
        "private_key_at_csms": "PRIVATE KEY" in text,
    }


def _verdict(run: Run, r: dict) -> tuple[bool, list[str]]:
    x, s, fails = run.expect, r.get("status", {}), []
    for key, api_key in (("phase", "phase"), ("total", "total_stations"), ("migrated", "migrated"),
                         ("incompatible", "incompatible"), ("rolled_back", "rolled_back"), ("pending", "pending")):
        if s.get(api_key) != x[key]:
            fails.append(f"{api_key}={s.get(api_key)} (expected {x[key]})")
    if r.get("sessions_split"):
        fails.append(f"sessions split by the migration: {r['sessions_split']}")
    if run.method == "enrolment":
        if r["pq_auth"].get("success", 0) != x["auth"]:
            fails.append(f"pq_auth passed {r['pq_auth'].get('success', 0)} (expected {x['auth']})")
        if run.tls:
            want = f"{EXPECTED_GROUP[run.mode]}/{EXPECTED_CERTIFICATE[run.mode]}"
            wrong = {k: n for k, n in r["agent_connections"].items() if k != want}
            if wrong:
                fails.append(f"agent connections not {want}: {wrong}")
    else:
        if r["rotations"].get("success", 0) != x["migrated"]:
            fails.append(f"rotations confirmed {r['rotations'].get('success', 0)} (expected {x['migrated']})")
        rotated_ok = sum(1 for v in r["final_connection"].values() if v == "MLKEM768/ML-DSA-44")
        if rotated_ok != x["migrated"]:
            fails.append(f"{rotated_ok} chargers ended on MLKEM768/ML-DSA-44 (expected {x['migrated']})")
        if r["private_key_at_csms"]:
            fails.append("a private key appeared on the CSMS side")
    return not fails, fails


def main(argv: list[str] | None = None) -> int:
    runs = _runs()
    ap = argparse.ArgumentParser(description="Phase 6 integration matrix")
    ap.add_argument("--runs", nargs="*", choices=[r.name for r in runs], help="default: all")
    ap.add_argument("--out", default=str(ROOT / "logs" / "phase6"))
    ap.add_argument("--skip-bootstrap", action="store_true", help="reuse certs/ (50 + 50 certificates)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    selected = [r for r in runs if not args.runs or r.name in args.runs]

    if not args.skip_bootstrap:
        print("bootstrapping PKI (50 ECDSA + 50 ML-DSA chargers)...", flush=True)
        _bootstrap(py, out)
    results = []
    for run in selected:
        print(f"-- {run.name} ...", flush=True)
        r = run_one(run, py, out)
        results.append(r)
        s = r.get("status", {})
        extra = (f"pq_auth {r['pq_auth'].get('success', 0)}/{r['pq_auth'].get('success', 0) + r['pq_auth'].get('rejected', 0)}"
                 if run.method == "enrolment" else
                 f"rotations {r['rotations'].get('success', 0)} ok / {r['rotations'].get('rejected', 0)} rejected, "
                 f"median {r['rotation_ms_median'] and round(r['rotation_ms_median'], 1)} ms")
        print(f"   {'PASS' if r['pass'] else 'FAIL'}  {s.get('phase')}  migrated {s.get('migrated')} / "
              f"incompatible {s.get('incompatible')} / rolled back {s.get('rolled_back')} / pending "
              f"{s.get('pending')} of {s.get('total_stations')}  |  {extra}  |  connections "
              f"{r['agent_connections']}  |  split sessions {len(r['sessions_split'])}", flush=True)
        for f in r["failures"]:
            print(f"      ! {f}", flush=True)
    (out / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\nper run: client connect time (TCP+TLS+WebSocket, median of first connections) | charger cert DER bytes")
    print("   (an E1 preview only: one machine, server and fleet competing for the same CPU)")
    for r in results:
        print(f"   {r['run']:16s} {str(r.get('client_connect_ms_median')):>7s} ms | {r.get('charger_cert_bytes')}")
    passed = sum(r["pass"] for r in results)
    print(f"\nPHASE 6: {passed}/{len(results)} runs passed  (details: {out / 'summary.json'})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())