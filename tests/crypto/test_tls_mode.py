"""
Tests for crypto/tls_mode.py -- the three TLS crypto modes made real (Phase 4)
and one CSMS context serving a mixed fleet (Phase 4b).

The end-to-end test starts a real server PROCESS and one client PROCESS per
mode, each with its own OPENSSL_CONF (the mode is a per-process setting), using
Track A's real context builders from csms/transport.py. It asserts, from the
server's side, the key-exchange group and certificate type of every connection.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from crypto import tls_mode
from crypto.tls_mode import (
    EXPECTED_CERTIFICATE,
    EXPECTED_GROUP,
    MODE_CONFIG,
    PQ_SERVER_NAME,
    ROTATION_CONFIG,
    SERVER_CONFIG,
    check_process_mode,
    configured_groups,
    group_name,
    install_group_probe,
    negotiated_group,
    parse_server_hello_group,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NEEDS_OPENSSL_35 = pytest.mark.skipif(
    ssl.OPENSSL_VERSION_INFO < (3, 5),
    reason=f"post-quantum TLS needs OpenSSL >= 3.5; this Python has {ssl.OPENSSL_VERSION}",
)


# -- config files ---------------------------------------------------------

def test_every_mode_has_a_config_file_with_the_right_first_group():
    for mode, first in (("classical", "X25519"), ("hybrid", "X25519MLKEM768"), ("pqc", "MLKEM768")):
        groups = configured_groups({"OPENSSL_CONF": str(MODE_CONFIG[mode])})
        assert groups is not None and groups[0] == first, (mode, groups)


def test_server_config_offers_all_three_groups():
    groups = configured_groups({"OPENSSL_CONF": str(SERVER_CONFIG)})
    assert set(groups) == {"X25519", "X25519MLKEM768", "MLKEM768"}


def test_classical_and_hybrid_configs_restrict_signatures_to_ecdsa():
    for mode in ("classical", "hybrid"):
        text = MODE_CONFIG[mode].read_text()
        assert "SignatureAlgorithms = ECDSA+SHA256" in text, mode
    assert "SignatureAlgorithms" not in MODE_CONFIG["pqc"].read_text()


# -- check_process_mode ----------------------------------------------------

@NEEDS_OPENSSL_35
def test_matching_config_passes_the_check():
    for mode in tls_mode.MODES:
        assert check_process_mode(mode, env={"OPENSSL_CONF": str(MODE_CONFIG[mode])}) is None
    assert check_process_mode(None, server=True, env={"OPENSSL_CONF": str(SERVER_CONFIG)}) is None


@NEEDS_OPENSSL_35
def test_wrong_or_missing_config_is_reported():
    assert "MLKEM768" in check_process_mode("pqc", env={})
    assert check_process_mode("pqc", env={"OPENSSL_CONF": str(MODE_CONFIG["classical"])})
    assert check_process_mode(None, server=True, env={})
    assert check_process_mode(None, server=True, env={"OPENSSL_CONF": str(MODE_CONFIG["pqc"])})


def test_unknown_mode_is_not_checked():
    assert check_process_mode("label-only", env={}) is None or ssl.OPENSSL_VERSION_INFO < (3, 5)


# -- ServerHello parsing and the probe ----------------------------------------

def test_group_names():
    assert group_name(0x001D) == "x25519"
    assert group_name(0x11EC) == "X25519MLKEM768"
    assert group_name(0x0201) == "MLKEM768"
    assert group_name(0xBEEF) == "0xbeef"
    assert group_name(None) is None


def test_parse_rejects_garbage():
    assert parse_server_hello_group(b"") is None
    assert parse_server_hello_group(b"\x01\x00\x00\x00") is None   # not a ServerHello
    assert parse_server_hello_group(b"\x02\x00\x00\x10" + b"\x03") is None  # truncated


def _pki(tmp_path: Path) -> dict[str, Path]:
    """An ECDSA set in tmp/ec and an ML-DSA set in tmp/pq, bootstrap layout."""
    from crypto.ca import CertificateAuthority
    from crypto.classical import ClassicalProvider
    from crypto.pq import PQProvider
    from crypto.store import certificate_der_to_pem, save_certificate, save_private_key

    dirs = {}
    for tag, provider in (("ec", ClassicalProvider()), ("pq", PQProvider())):
        d = tmp_path / tag
        d.mkdir()
        ca = CertificateAuthority(provider)
        (d / "root.pem").write_bytes(certificate_der_to_pem(ca.root_certificate_der))
        server = ca.issue_server_certificate(
            san_names=["localhost", PQ_SERVER_NAME] if tag == "pq" else None)
        save_certificate(server.certificate_der, "localhost", directory=d)
        save_private_key(server.private_key_der, "localhost", directory=d)
        station = ca.issue_station_certificate_with_new_key("CP0001")
        save_certificate(station.certificate_der, "CP0001", directory=d)
        save_private_key(station.private_key_der, "CP0001", directory=d)
        dirs[tag] = d
    return dirs


def test_probe_reads_the_group_in_process(tmp_path):
    """Memory-BIO handshake in this process: the probe must report whatever
    group this process's OpenSSL negotiated, on both sides."""
    from csms.transport import build_client_context, build_server_context

    d = _pki(tmp_path)["ec"]
    s = build_server_context(d / "localhost.crt.pem", d / "localhost.key.pem", d / "root.pem")
    c = build_client_context(d / "CP0001.crt.pem", d / "CP0001.key.pem", d / "root.pem")
    install_group_probe(c)  # already installed by the builder: must be idempotent
    ci, co, si, so = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    cs = c.wrap_bio(ci, co, server_hostname="localhost")
    ss = s.wrap_bio(si, so, server_side=True)
    done_c = done_s = False
    for _ in range(20):
        if not done_c:
            try:
                cs.do_handshake()
                done_c = True
            except ssl.SSLWantReadError:
                pass
        data = co.read()
        if data:
            si.write(data)
        if not done_s:
            try:
                ss.do_handshake()
                done_s = True
            except ssl.SSLWantReadError:
                pass
        data = so.read()
        if data:
            ci.write(data)
        if done_c and done_s:
            break
    server_group, client_group = negotiated_group(ss), negotiated_group(cs)
    assert server_group is not None and server_group == client_group
    assert negotiated_group(ss) is None, "reading releases the entry"


# -- the real thing: one mixed server process, one client process per mode -----

_SERVER = textwrap.dedent("""
    import json, socket, sys
    from csms.transport import build_server_context
    from crypto.tls_mode import negotiated_group, certificate_key_type
    ec, pq, port_file, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    ctx = build_server_context(f"{ec}/localhost.crt.pem", f"{ec}/localhost.key.pem", f"{ec}/root.pem",
                               extra_identities=((f"{pq}/localhost.crt.pem", f"{pq}/localhost.key.pem"),),
                               extra_cafiles=(f"{pq}/root.pem",))
    listener = socket.create_server(("127.0.0.1", 0))
    open(port_file, "w").write(str(listener.getsockname()[1]))
    for _ in range(n):
        conn, _ = listener.accept()
        try:
            tls = ctx.wrap_socket(conn, server_side=True)
            cn = dict(x[0] for x in tls.getpeercert()["subject"])["commonName"]
            print(json.dumps({"cn": cn, "group": negotiated_group(tls),
                              "client_key": certificate_key_type(tls.getpeercert(True))}), flush=True)
            tls.sendall(b"S"); tls.recv(1); tls.close()
        except Exception as e:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
""")

_CLIENT = textwrap.dedent("""
    import json, socket, sys
    from csms.transport import build_client_context
    from crypto.tls_mode import negotiated_group, certificate_key_type, pin_classical_key_exchange
    d, port, server_name, ca, pin = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
    ctx = build_client_context(f"{d}/CP0001.crt.pem", f"{d}/CP0001.key.pem", ca)
    if pin == "pin":
        pin_classical_key_exchange(ctx)
    tls = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=10), server_hostname=server_name)
    tls.recv(1); tls.sendall(b"C")
    print(json.dumps({"group": negotiated_group(tls),
                      "server_key": certificate_key_type(tls.getpeercert(True))}))
""")


# (label, OPENSSL_CONF, cert set, SNI, pin X25519?, expected group, expected
#  charger cert, expected SERVER cert). The rotation rows are one charger
#  process before and after its switch (plan Phase 5): classical until then --
#  including the server certificate it is shown -- and post-quantum after.
_CASES = [
    ("classical", MODE_CONFIG["classical"], "ec", "localhost", False, "x25519", "ECDSA-P256", "ECDSA-P256"),
    ("hybrid", MODE_CONFIG["hybrid"], "ec", "localhost", False, "X25519MLKEM768", "ECDSA-P256", "ECDSA-P256"),
    ("pqc", MODE_CONFIG["pqc"], "pq", PQ_SERVER_NAME, False, "MLKEM768", "ML-DSA-44", "ML-DSA-44"),
    ("rotation-before", ROTATION_CONFIG, "ec", "localhost", True, "x25519", "ECDSA-P256", "ECDSA-P256"),
    ("rotation-after", ROTATION_CONFIG, "pq", PQ_SERVER_NAME, False, "MLKEM768", "ML-DSA-44", "ML-DSA-44"),
]


@NEEDS_OPENSSL_35
def test_one_mixed_server_negotiates_each_charger_mode(tmp_path):
    dirs = _pki(tmp_path)
    bundle = tmp_path / "roots_all.pem"
    bundle.write_bytes((dirs["ec"] / "root.pem").read_bytes() + (dirs["pq"] / "root.pem").read_bytes())
    port_file = tmp_path / "port"
    env_base = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    server = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(dirs["ec"]), str(dirs["pq"]), str(port_file), str(len(_CASES))],
        cwd=PROJECT_ROOT, env={**env_base, "OPENSSL_CONF": str(SERVER_CONFIG)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import time
        for _ in range(100):
            if port_file.exists() and port_file.read_text():
                break
            if server.poll() is not None:
                pytest.fail(f"server died: {server.stderr.read()}")
            time.sleep(0.1)
        port = port_file.read_text()
        clients = {}
        for label, conf, cert_set, sni, pin, *_ in _CASES:
            # rotation processes trust both roots, as a rotating charger must
            ca = bundle if label.startswith("rotation") else dirs[cert_set] / "root.pem"
            out = subprocess.run(
                [sys.executable, "-c", _CLIENT, str(dirs[cert_set]), port, sni, str(ca),
                 "pin" if pin else "-"],
                cwd=PROJECT_ROOT, env={**env_base, "OPENSSL_CONF": str(conf)},
                capture_output=True, text=True, timeout=60)
            assert out.returncode == 0, f"{label} client failed: {out.stderr}"
            clients[label] = json.loads(out.stdout.strip().splitlines()[-1])
        server_out, server_err = server.communicate(timeout=30)
    finally:
        if server.poll() is None:
            server.kill()
    seen = [json.loads(line) for line in server_out.strip().splitlines()]
    assert len(seen) == len(_CASES), (seen, server_err)
    for (label, _c, _s, _n, _p, group, client_key, server_key), row in zip(_CASES, seen):
        assert "error" not in row, (label, row)
        assert row["cn"] == "CP0001"
        assert row["group"] == group, (label, row)
        assert row["client_key"] == client_key, (label, row)
        assert clients[label]["group"] == group, (label, clients[label])
        assert clients[label]["server_key"] == server_key, (label, clients[label])


def test_expected_tables_match_the_cases():
    for mode in ("classical", "hybrid", "pqc"):
        row = next(c for c in _CASES if c[0] == mode)
        assert row[5] == EXPECTED_GROUP[mode] and row[6] == EXPECTED_CERTIFICATE[mode]
