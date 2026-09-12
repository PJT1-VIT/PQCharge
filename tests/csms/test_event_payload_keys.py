"""
Regression test — no emit() call may shadow an EventLog parameter.

Track A. Added after a real bug: csms/handlers.py passed event_type= as a
payload keyword while also passing it positionally, which raised TypeError
inside the handler. The ocpp library turns a handler exception into a
CALLError back to the station, so the connection survived, the station
carried on charging, and the ONLY casualty was the event that was never
written to the log.

That is the worst failure shape in this project: a silent hole in the
data, discovered during Stage 9 analysis when the runs are over and the
numbers are missing. Section 14 of the design document warns that
instrumentation errors stay invisible until analysis. This is that.

The check is static — it parses the source rather than running the
server — so it needs neither the ocpp library nor a live CSMS, and it
covers every handler including ones not exercised by the fixtures.
Command dispatch (Days 8-9) will add more emit() calls; this test
watches them too, for free.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from csms.events import EventLog

CSMS_DIR = pathlib.Path(__file__).resolve().parents[2] / "csms"


def _reserved_parameter_order() -> list[str]:
    """EventLog.emit's named parameters, in declaration order."""
    return [
        name
        for name, param in inspect.signature(EventLog.emit).parameters.items()
        if name != "self" and param.kind is not param.VAR_KEYWORD
    ]


def _emit_calls():
    """Yield (path, lineno, positional_count, keyword_names) per emit call."""
    for path in sorted(CSMS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "emit"
            ):
                keywords = {kw.arg for kw in node.keywords if kw.arg}
                yield path, node.lineno, len(node.args), keywords


def test_no_emit_call_shadows_a_named_parameter():
    order = _reserved_parameter_order()
    offenders = []

    for path, lineno, positional_count, keywords in _emit_calls():
        already_positional = set(order[:positional_count])
        clash = sorted(keywords & already_positional)
        if clash:
            offenders.append(f"{path.name}:{lineno} passes {clash} twice")

    assert not offenders, (
        "emit() called with a keyword that was also supplied positionally. "
        "Rename the payload key (e.g. event_type -> tx_event_type):\n  "
        + "\n  ".join(offenders)
    )
