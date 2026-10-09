"""
Log redaction -- keep private keys out of the server's output log (L04).

Track A (csms). A-P5 of the Contract 7 priority list.

--------------------------------------------------------------------
WHY THIS EXISTS

The ocpp library logs every message it sends and receives at INFO level
("<id>: send [2, ...]"). While the deprecated InstallPQAuth path still
exists (Contract 7 section 7.8), a message carrying a station's ML-DSA
PRIVATE key can pass through that logger, and the key then lands in the
server's output log in clear text. GitGuardian flagged such copies once
they were committed under evidence/ (L04).

The root fix is removing InstallPQAuth (A-P6 / B-P5). Until then -- and
as a safety net afterwards -- every log record is passed through
PrivateKeyRedactingFilter before it is written.

--------------------------------------------------------------------
WHAT IT MATCHES

The key travels as a JSON field, "private_key": "<base64>". In a logged
OCPP frame it sits inside DataTransfer.data, which is itself a JSON
STRING, so its quotes appear escaped:

    "data": "{\\"algorithm\\": \\"ML-DSA-44\\", \\"private_key\\": \\"AbC...==\\"}"

The pattern therefore accepts zero or more backslashes before each
quote. The value is standard base64 (agent/pqc_messages.py encodes it
with base64.b64encode), so it is matched as [A-Za-z0-9+/=]* -- which
also tells the pattern exactly where the key ends. Only the value is
replaced; the field name stays, so a reader can see a key WAS there.

What it does NOT touch: the Contract 3 event log (csms/events.py). No
code path writes message bodies there -- only action names, statuses
and timings -- so there is nothing to redact.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"
"""What replaces a private key's value in a log line."""

_PRIVATE_KEY_VALUE = re.compile(
    r'(private_key\\*"\s*:\s*\\*")[A-Za-z0-9+/=]*'
)
"""Group 1 is everything up to and including the opening quote of the
value; the base64 run after it is the key."""


def redact_private_keys(text: str) -> str:
    """Return text with the value of every private_key field replaced."""
    if "private_key" not in text:  # fast path: almost every log line
        return text
    return _PRIVATE_KEY_VALUE.sub(lambda m: m.group(1) + REDACTED, text)


class PrivateKeyRedactingFilter(logging.Filter):
    """
    A logging filter that blanks private-key values in a record.

    Attached to HANDLERS (not loggers): a handler sees every record that
    reaches it, including records propagated from the ocpp library's own
    logger, whereas a filter on one logger only sees that logger's
    records.

    Never drops a record -- filter() always returns True. A filter that
    raised would lose the log line it was meant to clean, so any failure
    here leaves the record unchanged rather than raising.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            cleaned = redact_private_keys(message)
            if cleaned != message:
                # Freeze the cleaned text: later formatting must not
                # re-insert the original arguments.
                record.msg = cleaned
                record.args = None
            if record.exc_info and not record.exc_text:
                # Render the traceback now so its text can be cleaned too;
                # logging.Formatter reuses exc_text when it is set.
                record.exc_text = logging.Formatter().formatException(
                    record.exc_info
                )
            if record.exc_text:
                record.exc_text = redact_private_keys(record.exc_text)
        except Exception:  # noqa: BLE001 - never lose the log line
            pass
        return True


def install_private_key_redaction(
    logger: logging.Logger | None = None,
) -> PrivateKeyRedactingFilter:
    """
    Attach one PrivateKeyRedactingFilter to every handler of `logger`
    (default: the root logger). Call after logging.basicConfig().

    Idempotent: a handler that already has the filter is skipped, so
    calling this twice does not redact twice. Returns the filter.
    """
    target = logger if logger is not None else logging.getLogger()
    redactor = PrivateKeyRedactingFilter()
    for handler in target.handlers:
        if not any(isinstance(f, PrivateKeyRedactingFilter) for f in handler.filters):
            handler.addFilter(redactor)
    return redactor
