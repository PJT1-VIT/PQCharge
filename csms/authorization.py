"""
Authorization policy — which driver tokens may charge.

Track A (csms). Phase A3.

--------------------------------------------------------------------
Before a transaction begins, a station sends Authorize carrying the
token a driver presented, and the CSMS answers. This module holds the
answer, out of the handler, so that changing the token list is an edit
to a list rather than an edit to protocol logic.

PROVISIONAL. Track C has not yet supplied the tokens their agents will
present, so Track A chose a set. If their agents arrive on Day 5 with
different tokens, every transaction would be rejected -- which is why
`--auth-mode accept-all` exists: one flag unblocks the integration
immediately while the list is reconciled, instead of losing a day.
Recorded in claude/TrackA_Dev_Plan.md.

Two rejection statuses rather than one, because they mean different
things and the demo is better for showing both: a token the operator
has barred is Blocked; a token nobody has heard of is Invalid.

Status strings are sent as plain strings rather than imported from the
ocpp library's enums. The enum CLASS names have moved between releases
of that library; the wire values are fixed by OCPP 2.0.1 and cannot.
The schema validator in the ocpp library rejects a wrong value, so a
typo here fails loudly at the first Authorize rather than silently.
--------------------------------------------------------------------
"""

from __future__ import annotations

import json
from pathlib import Path

STATUS_ACCEPTED = "Accepted"
STATUS_BLOCKED = "Blocked"
STATUS_INVALID = "Invalid"

DEFAULT_ID_TOKENS: dict[str, str] = {
    "TAG-0001": STATUS_ACCEPTED,
    "TAG-0002": STATUS_ACCEPTED,
    "TAG-0003": STATUS_ACCEPTED,
    "TAG-0004": STATUS_ACCEPTED,
    "TAG-BLOCKED": STATUS_BLOCKED,
}
"""The seeded token list.

TAG-BLOCKED is deliberate: a demonstration in which every driver is
always authorised shows nothing about authorisation. One barred token
gives the walkthrough a visible refusal, and gives E5 a contrast --
a rejection that is a policy decision, next to a rejection that is a
failed signature.
"""

UNKNOWN_TOKEN_STATUS = STATUS_INVALID
"""What an unlisted token gets. Not Accepted -- a CSMS that authorises
anything it has never seen is not modelling authorisation at all."""

AUTH_MODES = ("allowlist", "accept-all")


class AuthorizationPolicy:
    """
    Decides Authorize outcomes. One instance per CSMS process.

    Deliberately not a database lookup. Contract 2 and §14 give SQLite
    the stations, sessions, transactions and meter values; driver
    tokens are not fleet state and do not belong in the same store. If
    a persisted token list is ever needed, --id-tokens already reads
    one from a file.
    """

    def __init__(
        self,
        tokens: dict[str, str] | None = None,
        *,
        mode: str = "allowlist",
    ) -> None:
        if mode not in AUTH_MODES:
            raise ValueError(f"unknown auth mode {mode!r}; expected {AUTH_MODES}")
        self.mode = mode
        self.tokens = dict(DEFAULT_ID_TOKENS if tokens is None else tokens)

    @classmethod
    def from_file(cls, path: str | Path, *, mode: str = "allowlist") -> "AuthorizationPolicy":
        """
        Load a token list from JSON: {"TAG-0001": "Accepted", ...}.

        Lets the list be swapped to match Track C's agents without a
        code change or a redeploy.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object of token -> status")
        return cls({str(k): str(v) for k, v in data.items()}, mode=mode)

    def authorize(self, token_id: str | None) -> tuple[str, bool]:
        """
        Decide one Authorize request.

        Args:
            token_id: the idToken string the station presented, or None
                if the payload carried no usable token.

        Returns:
            (status, known) -- status is an OCPP AuthorizationStatus
            value; known says whether the token was in the list, so the
            caller can log an unknown token by name without inferring
            it from the status.
        """
        if token_id is None or token_id == "":
            return STATUS_INVALID, False

        if self.mode == "accept-all":
            return STATUS_ACCEPTED, token_id in self.tokens

        status = self.tokens.get(token_id)
        if status is None:
            return UNKNOWN_TOKEN_STATUS, False
        return status, True

    def describe(self) -> dict[str, object]:
        """Configuration summary, recorded on the SERVER_STARTED event
        so a run's authorisation behaviour is recoverable from its own
        log rather than from memory of which flags were typed."""
        return {
            "auth_mode": self.mode,
            "token_count": len(self.tokens),
            "accepted_tokens": sorted(
                t for t, s in self.tokens.items() if s == STATUS_ACCEPTED
            ),
        }
