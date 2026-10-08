"""
Live certificate rotation — rotate, then reconnect (Phase 5 of the plan).

Track B (idmanager). Restores the original plan's Stage 5: each charger moves
from its classical (ECDSA) certificate to a post-quantum (ML-DSA-44) one DURING
a live session, using OCPP 2.0.1's own certificate flow, without losing its
charging transaction.

--------------------------------------------------------------------
THE FLOW (per charger; plan §11 Phase 5, steps 1-7)

  1 Request  CSMS -> TriggerMessage(SignChargingStationCertificate).
             The charger generates an ML-DSA-44 key pair LOCALLY and answers
             with SignCertificate(csr). The private key never leaves it.
  2 Issue    crypto/ca.py checks the CSR (signature = proof of possession,
             CN = this station, key = ML-DSA-44) and signs it with the same
             ML-DSA root the CSMS's post-quantum identity chains to.
             CSMS -> CertificateSigned(chain).
  3 Store    (charger) saves the new certificate and key persistently and
             keeps its old ECDSA pair as the fallback.
  4 Switch   the CSMS closes the socket (plan: "CSMS triggers a reconnect");
             the charger reconnects with the ML-DSA certificate (MLKEM768).
  5 Confirm  the CSMS sees a new connection presenting the NEW serial
             -> MIGRATED; Contract 2's previous_certificate_serial is filled.
  6 Continuity  the charger's open transaction resumes on the new connection
             (the agent's existing reconnect/resume path; same transaction id).
  7 Rollback the new handshake fails, times out, or the charger comes back on
             its OLD certificate -> ROLLED_BACK, and the new serial is refused
             from then on. A wave that fails its threshold rolls back its
             already-rotated stations the same way: their new serials are
             refused and their connections closed, so each falls back to its
             ECDSA certificate. Wave-threshold logic is unchanged.

--------------------------------------------------------------------
SPLIT OF RESPONSIBILITIES

  RotationBroker              the meeting point between the CSMS's connection
                              and message handlers (Track A calls on_* methods)
                              and the driver below. Pure bookkeeping, no I/O.
  CertificateRotationDriver   the per-station sequence above. Everything it
                              needs from Track A (dispatcher, how to close a
                              connection, how to build the two OCPP calls) is
                              injected, exactly like the orchestrator's other
                              collaborators, so it is unit-testable with fakes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from crypto.ca import CertificateAuthority, CSRRejected
from crypto.csr import certificate_serial_hex

LOGGER = logging.getLogger("idmanager.rotation")

DEFAULT_STEP_TIMEOUT_S = 15.0
DEFAULT_RECONNECT_TIMEOUT_S = 30.0

CLOSE_CODE_ROTATE = 1012
"""WebSocket 'service restart': the CSMS asks the charger to reconnect."""
CLOSE_REASON_ROTATE = "certificate rotated: reconnect with the new certificate"
CLOSE_CODE_REFUSED = 1008
"""WebSocket 'policy violation': this certificate is no longer accepted."""
CLOSE_REASON_REFUSED = "certificate refused: rolled back"


class RotationBroker:
    """
    Bookkeeping shared by the CSMS and the rotation driver.

    Track A's connection handler calls on_connected() for every TLS station
    connection, and its SignCertificate handler calls on_sign_certificate().
    The driver registers what it is waiting for. All calls happen on the
    CSMS's one event loop, so no locking is needed.
    """

    def __init__(self) -> None:
        self._csr_waiters: dict[str, asyncio.Future] = {}
        self._connect_waiters: dict[str, asyncio.Future] = {}
        self._current_serial: dict[str, str] = {}
        self._refused: set[tuple[str, str]] = set()

    # -- called by the CSMS (Track A) ------------------------------------

    def on_sign_certificate(self, station_id: str, csr: str) -> bool:
        """A SignCertificate arrived. Returns True if a rotation was waiting
        for it (the CSMS answers Accepted), False otherwise (Rejected)."""
        waiter = self._csr_waiters.pop(station_id, None)
        if waiter is None or waiter.done():
            return False
        waiter.set_result(csr)
        return True

    def on_connected(self, station_id: str, certificate_der: bytes | None) -> bool:
        """
        A station completed TLS. Returns False when the certificate it
        presented has been refused (rolled back) -- the CSMS then closes the
        connection with CLOSE_CODE_REFUSED and the charger falls back.
        """
        serial = certificate_serial_hex(certificate_der)
        if serial is None:
            return True
        if (station_id, serial) in self._refused:
            return False
        self._current_serial[station_id] = serial
        waiter = self._connect_waiters.pop(station_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(serial)
        return True

    # -- used by the driver ----------------------------------------------

    def current_serial(self, station_id: str) -> str | None:
        return self._current_serial.get(station_id)

    def expect_csr(self, station_id: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._csr_waiters[station_id] = future
        return future

    def expect_connection(self, station_id: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._connect_waiters[station_id] = future
        return future

    def forget_waiters(self, station_id: str) -> None:
        for table in (self._csr_waiters, self._connect_waiters):
            waiter = table.pop(station_id, None)
            if waiter is not None and not waiter.done():
                waiter.cancel()

    def refuse(self, station_id: str, serial: str | None) -> None:
        if serial:
            self._refused.add((station_id, serial))

    def is_refused(self, station_id: str, serial: str | None) -> bool:
        return serial is not None and (station_id, serial) in self._refused


@dataclass
class RotationOutcome:
    ok: bool
    detail: str
    duration_ms: float
    new_serial: str | None = None
    previous_serial: str | None = None
    step: str = ""
    """Where it ended: trigger, csr, issue, install, reconnect, confirmed."""


class CertificateRotationDriver:
    """
    Runs the rotate-then-reconnect sequence for one station at a time.
    Never raises for a station-level failure: every outcome is returned.
    """

    def __init__(
        self,
        *,
        dispatcher: Any,
        broker: RotationBroker,
        ca: CertificateAuthority,
        trigger_message_factory: Callable[[], Any],
        certificate_signed_factory: Callable[[str], Any],
        close_connection: Callable[[str, int, str], Awaitable[None]],
        step_timeout_s: float = DEFAULT_STEP_TIMEOUT_S,
        reconnect_timeout_s: float = DEFAULT_RECONNECT_TIMEOUT_S,
    ) -> None:
        if ca.mode != "pqc":
            raise ValueError("rotation issues post-quantum certificates: the CA must be a pqc CA")
        self._dispatch = dispatcher
        self._broker = broker
        self._ca = ca
        self._make_trigger = trigger_message_factory
        self._make_signed = certificate_signed_factory
        self._close = close_connection
        self._step_timeout_s = step_timeout_s
        self._reconnect_timeout_s = reconnect_timeout_s

    async def rotate(self, station_id: str) -> RotationOutcome:
        started = time.perf_counter()
        previous = self._broker.current_serial(station_id)

        def done(ok: bool, detail: str, step: str, new_serial: str | None = None) -> RotationOutcome:
            if not ok:
                self._broker.forget_waiters(station_id)
            return RotationOutcome(ok, detail, (time.perf_counter() - started) * 1000.0,
                                   new_serial, previous, step)

        # 1. Request: the charger makes its own key and sends a CSR.
        csr_future = self._broker.expect_csr(station_id)
        result = await self._dispatch.send(station_id, self._make_trigger(),
                                           timeout_s=self._step_timeout_s)
        if not getattr(result, "ok", False):
            return done(False, f"TriggerMessage not accepted (outcome={getattr(result, 'outcome', None)}, "
                               f"status={getattr(result, 'status', None)})", "trigger")
        try:
            csr = await asyncio.wait_for(csr_future, self._step_timeout_s)
        except asyncio.TimeoutError:
            return done(False, "no SignCertificate (CSR) arrived", "csr")

        # 2. Issue.
        try:
            issued = self._ca.issue_certificate_from_csr(csr, station_id)
        except CSRRejected as exc:
            return done(False, f"CSR refused: {exc}", "issue")
        from crypto.store import certificate_der_to_pem

        chain_pem = certificate_der_to_pem(issued.certificate_der).decode("ascii")
        new_serial = issued.serial

        # Register for the reconnect BEFORE anything can trigger it.
        connect_future = self._broker.expect_connection(station_id)
        result = await self._dispatch.send(station_id, self._make_signed(chain_pem),
                                           timeout_s=self._step_timeout_s)
        if not getattr(result, "ok", False):
            self._broker.refuse(station_id, new_serial)
            return done(False, f"charger did not install the certificate "
                               f"(status={getattr(result, 'status', None)})", "install", new_serial)

        # 4. Switch: the CSMS closes the socket; the charger reconnects.
        try:
            await self._close(station_id, CLOSE_CODE_ROTATE, CLOSE_REASON_ROTATE)
        except Exception as exc:  # noqa: BLE001 - a dead socket also forces a reconnect
            LOGGER.debug("closing %s for rotation: %s", station_id, exc)

        # 5. Confirm: the next connection must present the NEW certificate.
        try:
            serial = await asyncio.wait_for(connect_future, self._reconnect_timeout_s)
        except asyncio.TimeoutError:
            self._broker.refuse(station_id, new_serial)
            return done(False, "charger did not reconnect in time", "reconnect", new_serial)
        if serial != new_serial:
            # 7. It fell back to its old certificate: the new one must never
            # be accepted later behind the orchestrator's back.
            self._broker.refuse(station_id, new_serial)
            return done(False, "charger reconnected with its previous certificate (fell back)",
                        "reconnect", new_serial)
        return done(True, "reconnected with the new ML-DSA-44 certificate", "confirmed", new_serial)

    def revert_soon(self, station_id: str, new_serial: str | None) -> None:
        """
        Wave rollback for an already-rotated station: refuse its new serial
        from now on and close its connection, so it reconnects and falls back
        to its ECDSA certificate. Synchronous (the orchestrator's rollback is),
        so the close is scheduled.
        """
        self._broker.refuse(station_id, new_serial)
        try:
            asyncio.get_running_loop().create_task(
                self._close(station_id, CLOSE_CODE_REFUSED, CLOSE_REASON_REFUSED)
            )
        except RuntimeError:  # no running loop (synchronous caller in a test)
            pass
