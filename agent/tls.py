"""
The station's TLS material — which certificate am I, and how do I load it.

Track C (agent). Phase C5.

--------------------------------------------------------------------
WHERE THIS FITS

    csms/transport.py       Track A's context builders (they own it)
          |
    agent/tls.py         <- you are here. Picks THIS station's files and
          |                 asks Track A's builder for a context.
    agent/station.py        passes the context to connect()

--------------------------------------------------------------------
*** WHY THIS IMPORTS FROM csms/ ***

`csms/transport.py` contains `build_client_context()` -- explicitly the
STATION side, written by Track A and already used by their own fixture.
Writing a second SSL context builder in `agent/` would mean two places
that decide what `check_hostname` is, what verify mode applies, and how
a missing file is reported. They would agree today and drift by Stage 8.

This is the same reasoning the team has already applied twice: Track C
imports `StationView.is_recovered` rather than re-deriving the recovery
predicate, and Track B asked Track C to import `measure_certificate_sizes`
rather than recomputing byte counts. One definition, one source.

The import is READ-ONLY and one-directional. `agent/` imports from
`csms/`; nothing in `csms/` imports from `agent/`. Track C edits no file
under `csms/`.

If `csms.transport` is ever moved or renamed, this file fails loudly at
import with a message naming the cause, rather than silently falling
back to an unverified connection -- which is the failure mode that
matters, because a station that quietly stops checking certificates
still works perfectly right up until E5.

--------------------------------------------------------------------
WHAT THIS FILE IS **NOT**

It is not `agent/certificate.py`. That is Phase C8 and it is a different
job: requesting a certificate over `SignCertificate`, installing one over
`CertificateSigned`, rotating mid-transaction, and writing Contract 2's
migration fields. All of that is Track B's Stage 5 work meeting Track
C's, and none of it is here.

This file does one thing: given a station id and a certificate
directory, produce the SSL context that station connects with. That is
the whole of what C5 needs in order to run the `--tls` measurements
Track A asked for (their §9.5) before the joint session happens.

--------------------------------------------------------------------
FILE LAYOUT — FIXED BY TRACK B's bootstrap_pki.py

    certs/root.pem              the CA root, verified against by both
    certs/<station_id>.crt.pem  this station's certificate
    certs/<station_id>.key.pem  its private key
    certs/localhost.crt.pem     the CSMS's own identity (server side)

`python -m experiments.bootstrap_pki` writes all of these. `certs/` is
gitignored and regenerated, never committed.

--------------------------------------------------------------------
*** THE CN MUST MATCH THE STATION ID ***

Track A's Day 7 finding (their §6.2): mutual TLS alone does not bind a
station to its identity. A station announces itself twice -- in the
WebSocket path (`/CP001`) and in its certificate's Common Name -- and
nothing in TLS makes those agree. They demonstrated CP002 connecting as
CP001 using its own genuine certificate, with nothing forged.

The CSMS now checks CN against the path id. It is `warn` today and
becomes `enforce` before E5. So this file loads
`certs/<station_id>.crt.pem` and nothing else: the certificate whose CN
is the id the station connects with. Pointing a station at someone
else's certificate is possible -- `--cert` exists, and it is how the
attack gets demonstrated -- but it is never the default, and it is never
silent.
"""

from __future__ import annotations

import ssl
from pathlib import Path

from agent.logging_setup import get_logger

LOG = get_logger(__name__)

DEFAULT_CERT_DIR = "certs"
CA_FILENAME = "root.pem"

CERT_SUFFIX = ".crt.pem"
KEY_SUFFIX = ".key.pem"


class TlsMaterialError(RuntimeError):
    """
    This station cannot assemble the TLS material it needs.

    Raised at START-UP, never at first connection, for the same reason
    Track A raises on a missing server certificate at start-up: a
    station that launches with broken TLS and only fails when it dials
    looks like a server problem -- and during E2, with five hundred of
    them, it would look like a post-quantum problem.
    """


def station_cert_paths(
    station_id: str,
    cert_dir: str | Path = DEFAULT_CERT_DIR,
    *,
    cert: str | Path | None = None,
    key: str | Path | None = None,
    ca: str | Path | None = None,
) -> tuple[Path, Path, Path]:
    """
    Which three files this station presents and verifies against.

    Explicit overrides win, so a single station can be pointed at
    another's certificate to demonstrate the identity-binding attack --
    the client-side counterpart of Track A's `--cert-as` on their
    fixture. Otherwise the id decides, which is what keeps five hundred
    agents correct without five hundred command-line arguments.
    """
    directory = Path(cert_dir)
    return (
        Path(cert) if cert else directory / f"{station_id}{CERT_SUFFIX}",
        Path(key) if key else directory / f"{station_id}{KEY_SUFFIX}",
        Path(ca) if ca else directory / CA_FILENAME,
    )


def build_station_context(
    station_id: str,
    cert_dir: str | Path = DEFAULT_CERT_DIR,
    *,
    cert: str | Path | None = None,
    key: str | Path | None = None,
    ca: str | Path | None = None,
    check_hostname: bool = True,
) -> ssl.SSLContext:
    """
    This station's TLS context, built by Track A's builder.

    Raises:
        TlsMaterialError: a file is missing, the material does not load,
            or `csms.transport` could not be imported. All three are
            start-up problems with the same consequence, so they are one
            exception type with a message that names which happened.
    """
    try:
        # Imported inside the function, not at module top level, so that
        # `import agent.tls` costs nothing and cannot fail for a station
        # that is not using TLS at all. Five hundred agents import this
        # module; only the TLS ones should pay for it or be broken by it.
        from csms.transport import TlsConfigError, build_client_context
    except ImportError as exc:  # pragma: no cover - a repo-layout fault
        raise TlsMaterialError(
            "could not import csms.transport, which owns the station TLS "
            f"context builder ({exc}). Run from the repository root as "
            "`python -m agent.station` / `python -m harness.load_generator`."
        ) from exc

    cert_path, key_path, ca_path = station_cert_paths(
        station_id, cert_dir, cert=cert, key=key, ca=ca
    )

    missing = [str(p) for p in (cert_path, key_path, ca_path) if not p.is_file()]
    if missing:
        # Named individually, with the fix, because the single most
        # likely cause is that nobody has run the bootstrap yet.
        raise TlsMaterialError(
            f"station {station_id}: TLS material not found: {', '.join(missing)}. "
            "Run `python -m experiments.bootstrap_pki` to generate the PKI, and "
            "check that a certificate exists for this station id."
        )

    try:
        context = build_client_context(
            cert_path, key_path, ca_path, check_hostname=check_hostname
        )
    except TlsConfigError as exc:
        raise TlsMaterialError(f"station {station_id}: {exc}") from exc

    if not check_hostname:
        # Loud, because switching this off is how a Raspberry Pi SAN
        # problem gets "fixed" at the demo and then silently weakens
        # every figure taken afterwards.
        LOG.warning(
            "station %s: server hostname checking is OFF. The station is no "
            "longer verifying that it is talking to the CSMS it thinks it is. "
            "Acceptable only for a deliberate experiment, and it belongs in "
            "docs/limitations.md if any recorded run uses it.",
            station_id,
        )

    LOG.info(
        "station %s: TLS material loaded (cert=%s ca=%s hostname_check=%s)",
        station_id, cert_path, ca_path, check_hostname,
    )
    return context


def describe_material(
    station_id: str,
    cert_dir: str | Path = DEFAULT_CERT_DIR,
) -> str:
    """One line for a start-up log, whether or not the files exist."""
    cert_path, key_path, ca_path = station_cert_paths(station_id, cert_dir)
    present = all(p.is_file() for p in (cert_path, key_path, ca_path))
    return f"tls material {'present' if present else 'MISSING'}: {cert_path}, {ca_path}"
