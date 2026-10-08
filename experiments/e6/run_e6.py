"""
E6 again, on the Python 3.14 stack: does the third-party charger still work?

Green-flag criterion 8 (handoff §12): "Third-party charger (E6) still connects
in classical/hybrid server configs." Runs the SAP e-mobility simulator (the same
external OCPP 2.0.1 client and template as Track A's E6, experiments/e6/README.md)
against the CSMS from this copy, in three server configurations:

    ws             plain ws:// -- the configuration of the recorded 2026-09-29 PASS
    tls-classical  mutual TLS; CSMS offers X25519 only, ECDSA identity only
    tls-hybrid     mutual TLS; the MIXED-FLEET CSMS of Phase 4b: every group
                   offered (tls/server.cnf), ECDSA + ML-DSA identities (SNI)

For TLS the simulator presents its own client certificate (CN = E6-SAP-01), so
the CSMS's identity check runs on 'enforce'. The certificate goes in through the
simulator's documented template field `wsOptions` (passed to Node's TLS client
as cert / key / ca) -- the simulator's code is not changed.

Each run: fresh --db/--log, ~3 minutes of the simulator's automatic
transactions, then Track A's own checker (check_e6.py) gives the verdict, plus
the key-exchange group and certificate type the CSMS recorded for the charger.

The simulator's dist/assets/config.json and our station template are written
for each run, and its cached station configurations (dist/assets/configurations,
which keep the previous run's server address even with persistState false) are
set aside per run; everything is restored afterwards (backups *.pqcharge-bak).

    python -m experiments.e6.run_e6 --sim D:\\e6-sap-sim
    python -m experiments.e6.run_e6 --sim D:\\e6-sap-sim --variants tls-hybrid
    python -m experiments.e6.run_e6 --sim D:\\e6-sap-sim --node D:\\node-v22.23.3-win-x64\\node.exe

--node picks the Node.js that runs the simulator (default: `node` on PATH). The
simulator's key exchange depends on the OpenSSL bundled in that Node: OpenSSL
3.0 (Node <= 22.19) can only do X25519; OpenSSL 3.5 (Node >= 22.20) offers
X25519MLKEM768 first, so the third-party charger itself goes post-quantum --
with no change to the simulator. The Node and OpenSSL versions are recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAP = ROOT / "experiments" / "e6" / "sap"
TEMPLATE_NAME = "pqcharge-e6.station-template.json"
STATION = "E6-SAP-01"
VARIANTS = ("ws", "tls-classical", "tls-hybrid")


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


def _bootstrap(py: str, out: Path) -> None:
    """Fresh PKI: classical set incl. E6-SAP-01, plus the ML-DSA set the
    mixed-fleet CSMS presents to post-quantum chargers."""
    for extra in (["--count", "1", "--also", STATION], ["--algorithm", "pqc", "--count", "1"]):
        log = out / ("bootstrap_pqc.txt" if "pqc" in extra else "bootstrap.txt")
        with open(log, "w", encoding="utf-8") as f:
            subprocess.run([py, "-m", "experiments.bootstrap_pki", *extra], cwd=ROOT, env=_env(None),
                           stdout=f, stderr=subprocess.STDOUT, check=False)
        if "SUCCESS" not in log.read_text(encoding="utf-8", errors="replace"):
            raise SystemExit(f"bootstrap_pki failed -- see {log}")


def _write_assets(assets: Path, url: str, tls: bool) -> None:
    config = json.loads((SAP / "config.json").read_text(encoding="utf-8"))
    config["supervisionUrls"] = [url]
    (assets / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    template = json.loads((SAP / TEMPLATE_NAME).read_text(encoding="utf-8"))
    template["supervisionUrls"] = [url]
    if tls:
        certs = ROOT / "certs"
        template["wsOptions"] = {
            "cert": (certs / f"{STATION}.crt.pem").read_text(encoding="ascii"),
            "key": (certs / f"{STATION}.key.pem").read_text(encoding="ascii"),
            "ca": (certs / "root.pem").read_text(encoding="ascii"),
        }
    (assets / "station-templates").mkdir(exist_ok=True)
    (assets / "station-templates" / TEMPLATE_NAME).write_text(json.dumps(template, indent=2), encoding="utf-8")
    shutil.copy2(SAP / "pqcharge-idtags.json", assets / "pqcharge-idtags.json")


def node_versions(node: str) -> str:
    try:
        return subprocess.run([node, "-p", "process.version + ' OpenSSL ' + process.versions.openssl"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except OSError as exc:
        raise SystemExit(f"cannot run Node.js at {node!r}: {exc}")


def run_variant(variant: str, sim: Path, py: str, out: Path, seconds: int, node: str = "node") -> dict:
    d = out / variant
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    # The simulator caches each station's configuration -- including the
    # server address (CentralSystemAddress) -- and reuses it over the template.
    cache = sim / "dist" / "assets" / "configurations"
    shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir()
    port = _free_port()
    tls = variant != "ws"
    url = f"{'wss' if tls else 'ws'}://localhost:{port}"
    _write_assets(sim / "dist" / "assets", url, tls)

    server_cmd = [py, "-m", "csms.server", "--port", str(port), "--db", str(d / "csms.db"),
                  "--log", str(d / "events.jsonl"), "--migration", "off", "--verbose"]
    conf = None
    if tls:
        server_cmd += ["--tls", "--cert-dir", "certs", "--tls-identity-check", "enforce"]
        if variant == "tls-hybrid":
            server_cmd += ["--pq-cert-dir", "certs/pqc"]
            conf = ROOT / "tls" / "server.cnf"
        else:
            conf = ROOT / "tls" / "classical.cnf"

    server_log = open(d / "server.log", "w", encoding="utf-8")
    sim_log = open(d / "simulator.txt", "w", encoding="utf-8")
    server = subprocess.Popen(server_cmd, cwd=ROOT, env=_env(conf), stdout=server_log, stderr=subprocess.STDOUT)
    simulator = None
    try:
        time.sleep(3)
        if server.poll() is not None:
            raise RuntimeError(f"CSMS exited at start-up -- see {d / 'server.log'}")
        sim_env = {k: v for k, v in os.environ.items() if k != "OPENSSL_CONF"}
        sim_env["NODE_ENV"] = "production"
        simulator = subprocess.Popen([node, "dist/start.js"], cwd=sim, env=sim_env,
                                     stdout=sim_log, stderr=subprocess.STDOUT)
        print(f"   simulator running against {url} for {seconds}s ...", flush=True)
        time.sleep(seconds)
    finally:
        for proc in (simulator, server):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        server_log.close()
        sim_log.close()

    check = subprocess.run(
        [py, str(ROOT / "experiments" / "e6" / "check_e6.py"), "--events", str(d / "events.jsonl"),
         "--server-log", str(d / "server.log")],
        cwd=ROOT, env=_env(None), capture_output=True, text=True, encoding="utf-8", errors="replace")
    (d / "verdict.txt").write_text(check.stdout + check.stderr, encoding="utf-8")

    tls_facts = []
    for line in (d / "events.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line) if line.strip() else {}
        p = e.get("payload") or {}
        if e.get("event_type") == "connection_established" and e.get("station_id") == STATION \
                and p.get("tls_version"):
            tls_facts.append(f"{p.get('tls_version')} {p.get('tls_group')} {p.get('peer_key_type')}")
        if e.get("event_type") == "connection_attempt" and e.get("station_id") == STATION \
                and p.get("transition") == "identity_check":
            tls_facts.append(f"identity check {e.get('outcome')} (CN {p.get('certificate_common_name')})")
    return {"variant": variant, "pass": check.returncode == 0, "verdict": check.stdout.strip(),
            "tls": tls_facts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E6 on the Python 3.14 stack (green-flag criterion 8)")
    ap.add_argument("--sim", required=True, help="the SAP simulator folder, built (dist/start.js)")
    ap.add_argument("--variants", nargs="*", choices=VARIANTS, default=list(VARIANTS))
    ap.add_argument("--seconds", type=int, default=200, help="how long each run lets the simulator charge")
    ap.add_argument("--out", default=str(ROOT / "logs" / "e6"))
    ap.add_argument("--node", default="node", help="Node.js executable that runs the simulator")
    args = ap.parse_args(argv)

    sim = Path(args.sim)
    assets = sim / "dist" / "assets"
    if not (sim / "dist" / "start.js").is_file() or not assets.is_dir():
        raise SystemExit(f"{sim} is not a built simulator (no dist/start.js) -- see experiments/e6/README.md")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    backups, created = [], []
    for rel in ("config.json", f"station-templates/{TEMPLATE_NAME}", "pqcharge-idtags.json"):
        src = assets / rel
        if src.is_file():
            bak = src.with_name(src.name + ".pqcharge-bak")
            shutil.copy2(src, bak)
            backups.append((bak, src))
        else:
            created.append(src)
    cache = assets / "configurations"
    cache_bak = assets / "configurations.pqcharge-bak"
    if cache.is_dir():
        shutil.rmtree(cache_bak, ignore_errors=True)
        shutil.move(str(cache), str(cache_bak))

    results = []
    try:
        node_info = node_versions(args.node)
        print(f"simulator runs on Node {node_info} ({args.node})", flush=True)
        print("bootstrapping PKI (incl. E6-SAP-01) ...", flush=True)
        _bootstrap(py, out)
        for variant in args.variants:
            print(f"-- {variant}", flush=True)
            r = run_variant(variant, sim, py, out, args.seconds, args.node)
            r["node"] = node_info
            results.append(r)
            print(f"   {'PASS' if r['pass'] else 'FAIL'}  |  TLS: {'; '.join(r['tls']) or 'none (ws://)'}", flush=True)
            for line in r["verdict"].splitlines():
                if line.strip().startswith(("[", "energy", "actions", "E6")) or "none" == line.strip():
                    print(f"      {line.strip()}", flush=True)
    finally:
        for bak, src in backups:
            shutil.move(str(bak), str(src))
        for src in created:
            src.unlink(missing_ok=True)
        shutil.rmtree(cache, ignore_errors=True)
        if cache_bak.is_dir():
            shutil.move(str(cache_bak), str(cache))
        print("simulator assets restored", flush=True)

    (out / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(r["pass"] for r in results)
    print(f"\nE6 (criterion 8): {passed}/{len(results)} configurations passed  (details: {out})")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())