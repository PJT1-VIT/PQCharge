"""
E4 — SIZE LIMITS: how big are the security artifacts, and do they fit?

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

Post-quantum keys and signatures are much larger than classical ones
(an ML-DSA-44 signature is about 2.4 KB; an ECDSA signature is about 70
bytes). OCPP 2.0.1 caps how large a certificate it will carry: 5,500
characters for one certificate and 10,000 for a certificate chain (the
limits the design document fixes for E4). This measures every artifact
our system ACTUALLY produces, with the project's own code:

    classical   certificates issued by Track B's real CA (station, server,
                root, and the station+root chain), in DER (binary) and PEM
                (the text form OCPP carries), plus an ECDSA key and signature
    post-quantum  ML-DSA-44 keys and signature, ML-KEM-768 key and
                ciphertext, from Track B's crypto/pq.py
    on the wire the three Option B messages exactly as the agent and the
                orchestrator exchange them (agent/pqc_messages.py): key
                install, challenge, signed answer

Nothing here depends on a run: sizes are properties of the algorithms and
of our encoding, identical on a laptop or the Raspberry Pi.

A NEGATIVE RESULT IS A VALID RESULT (design doc §13.1): under Option B no
ML-DSA certificate exists, so the certificate limits apply to the classical
certificates only; the post-quantum artifacts travel inside DataTransfer.
The table reports what exists, and marks which limit each row is checked
against -- nothing is invented to have something to exceed.

Missing post-quantum library -> those rows are skipped with a note, and the
classical rows still appear.
"""

from __future__ import annotations

from typing import Any

CERT_LIMIT = 5500
CHAIN_LIMIT = 10000


def _row(group: str, label: str, raw: int, pem: int | None = None,
         limit: int | None = None, algorithm: str | None = None) -> dict[str, Any]:
    checked = pem if pem is not None else raw
    return {
        "group": group,
        "label": label,
        "algorithm": algorithm,
        "bytes": raw,
        "pem_bytes": pem,
        "limit": limit,
        "within_limit": (checked <= limit) if limit else None,
        "headroom": (limit - checked) if limit else None,
    }


def measure() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    notes: list[str] = []

    # -- classical, through the real CA ------------------------------------
    try:
        from crypto.ca import CertificateAuthority
        from crypto.classical import ClassicalProvider
        from crypto.store import measure_certificate_sizes, measure_chain_sizes

        provider = ClassicalProvider()
        ca = CertificateAuthority(provider)
        station = ca.issue_station_certificate_with_new_key("CP0001")
        server = ca.issue_server_certificate()

        for label, der, limit in (
            ("Station certificate", station.certificate_der, CERT_LIMIT),
            ("Server certificate", server.certificate_der, CERT_LIMIT),
            ("Root CA certificate", ca.root_certificate_der, CERT_LIMIT),
        ):
            s = measure_certificate_sizes(der, label)
            rows.append(_row("classical", label, s.der_bytes, s.pem_bytes, limit, "ECDSA P-256"))

        chain = measure_chain_sizes([station.certificate_der, ca.root_certificate_der], "chain")
        rows.append(_row("classical", "Chain (station + root)", chain.der_bytes,
                         chain.pem_bytes, CHAIN_LIMIT, "ECDSA P-256"))

        priv, pub = provider.generate_keypair()
        rows.append(_row("classical", "Public key", len(pub), algorithm="ECDSA P-256"))
        rows.append(_row("classical", "Signature", len(provider.sign(priv, b"x" * 32)),
                         algorithm="ECDSA P-256"))
    except Exception as exc:  # noqa: BLE001 - report, never crash the analysis
        notes.append(f"classical sizes unavailable: {type(exc).__name__}: {exc}")

    # -- post-quantum, through Track B's provider --------------------------
    try:
        from crypto.pq import PQProvider

        pq = PQProvider()
        priv, pub = pq.generate_keypair()
        sig = pq.sign(priv, b"x" * 32)
        rows.append(_row("post-quantum", "Public key", len(pub), algorithm="ML-DSA-44"))
        rows.append(_row("post-quantum", "Private key", len(priv), algorithm="ML-DSA-44"))
        rows.append(_row("post-quantum", "Signature", len(sig), algorithm="ML-DSA-44"))
        kem_priv, kem_pub = pq.generate_kem_keypair()
        _secret, ciphertext = pq.encapsulate(kem_pub)
        rows.append(_row("post-quantum", "KEM public key", len(kem_pub), algorithm="ML-KEM-768"))
        rows.append(_row("post-quantum", "KEM ciphertext", len(ciphertext), algorithm="ML-KEM-768"))

        # -- the Option B messages, exactly as they cross the wire ---------
        from agent import pqc_messages as pqcm

        install = pqcm.build_install_message("CP0001", priv)
        challenge = pqcm.build_challenge_message(b"\x00" * 32)
        answer = pqcm.pack_signature(sig)
        for label, text in (
            ("InstallPQAuth payload", install.data),
            ("PQAuthChallenge payload", challenge.data),
            ("Signed answer payload", answer),
        ):
            rows.append(_row("on the wire", label, len(text.encode("utf-8")),
                             algorithm="DataTransfer (JSON + base64)"))
    except Exception as exc:  # noqa: BLE001
        notes.append(
            f"post-quantum sizes unavailable ({type(exc).__name__}: {exc}); "
            "install quantcrypt from requirements.txt to include them"
        )

    return {
        "limits": {"certificate": CERT_LIMIT, "chain": CHAIN_LIMIT},
        "rows": rows,
        "notes": notes,
    }
