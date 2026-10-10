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
    on the wire the Contract 7 messages exactly as the agent and the
                orchestrator exchange them (agent/pqc_messages.py): the
                enrolment request, the enrolment reply (public key + key_id),
                the challenge and the signed answer
    ML-DSA certificates  (C-F5, from Track B) real ML-DSA-44 X.509
                certificates built by crypto/pq_x509.py, when the installed
                `cryptography` can make them (version 50+). They are built
                for E4 only: the fleet itself still uses classical TLS
                certificates (Option B).

Nothing here depends on a run: sizes are properties of the algorithms and
of our encoding, identical on a laptop or the Raspberry Pi.

A NEGATIVE RESULT IS A VALID RESULT (design doc §13.1): the fleet's own
certificates are classical (Option B); the post-quantum artifacts travel
inside DataTransfer. The ML-DSA certificate rows answer "what WOULD the
OCPP limits do to post-quantum certificates" -- each row marks which limit
it is checked against, and nothing is invented to have something to exceed.

C-F5: the old "InstallPQAuth payload" row is gone. That message carried a
server-made PRIVATE key and is being removed (Contract 7 section 7.8,
A-F3 / B-F1b); the rows now show the messages actually used.

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

        request = pqcm.build_enrolment_request("CP0001")
        reply = pqcm.pack_enrolment_reply("ML-DSA-44", pub)
        challenge = pqcm.build_challenge_message(b"\x00" * 32, key_id=pqcm.key_id_for(pub))
        answer = pqcm.pack_signature(sig)
        for label, text in (
            ("RequestPQEnrolment payload", request.data),
            ("RequestPQEnrolment reply (public key)", reply),
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

    # -- ML-DSA certificates (C-F5; Track B's crypto/pq_x509.py) ------------
    # Rows come in this table's own format (_row). Any failure -- the module
    # not on this branch yet, or an older `cryptography` -- is a note, never
    # a crash, and the other rows still appear.
    try:
        from crypto.pq_x509 import available, size_rows
        if available():
            rows.extend(size_rows())
        else:
            notes.append("ML-DSA certificate rows need cryptography 50+")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"ML-DSA certificate rows unavailable: {type(exc).__name__}: {exc}")

    return {
        "limits": {"certificate": CERT_LIMIT, "chain": CHAIN_LIMIT},
        "rows": rows,
        "notes": notes,
    }
