"""A-P5 (L04): private keys never reach the server's output log."""

import base64
import inspect
import io
import json
import logging

import pytest

from agent.pqc_messages import build_install_message
from csms import server
from csms.log_redaction import (
    REDACTED,
    PrivateKeyRedactingFilter,
    install_private_key_redaction,
    redact_private_keys,
)

# A realistic ML-DSA-44 private key is 2,560 bytes; any bytes will do here.
FAKE_KEY = bytes(range(256)) * 10
FAKE_KEY_B64 = base64.b64encode(FAKE_KEY).decode("ascii")


def _ocpp_send_frame() -> str:
    """The exact text the ocpp library logs for an InstallPQAuth send:
    "<id>: send [2, <uid>, "DataTransfer", {...}]", where DataTransfer.data
    is itself a JSON string -- so the key's quotes appear escaped."""
    request = build_install_message("CP0001", FAKE_KEY)
    payload = {"vendorId": request.vendor_id, "messageId": request.message_id,
               "data": request.data}
    return json.dumps([2, "uid-1", "DataTransfer", payload],
                      separators=(",", ":"))


@pytest.fixture
def captured():
    """A logger with one handler writing to a buffer, redaction installed."""
    logger = logging.getLogger("test.redaction")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    buffer = io.StringIO()
    logger.addHandler(logging.StreamHandler(buffer))
    install_private_key_redaction(logger)
    yield logger, buffer
    logger.handlers.clear()


def test_the_real_install_frame_really_contains_the_key():
    # Guard for the test itself: if the frame stopped containing the key,
    # every test below would pass for the wrong reason.
    assert FAKE_KEY_B64 in _ocpp_send_frame()


def test_ocpp_style_send_line_is_redacted(captured):
    logger, buffer = captured
    logger.info("%s: send %s", "CP0001", _ocpp_send_frame())
    out = buffer.getvalue()
    assert FAKE_KEY_B64 not in out
    assert FAKE_KEY_B64[:40] not in out          # not even a prefix survives
    assert "private_key" in out and REDACTED in out
    assert "CP0001: send" in out                 # the rest of the line is kept
    assert "ML-DSA-44" in out


def test_plain_json_form_is_redacted():
    text = json.dumps({"algorithm": "ML-DSA-44", "private_key": FAKE_KEY_B64})
    cleaned = redact_private_keys(text)
    assert FAKE_KEY_B64 not in cleaned
    assert json.loads(cleaned)["private_key"] == REDACTED


def test_several_keys_on_one_line_are_all_redacted():
    text = f'"private_key": "{FAKE_KEY_B64}" and "private_key":"{FAKE_KEY_B64}"'
    cleaned = redact_private_keys(text)
    assert FAKE_KEY_B64 not in cleaned
    assert cleaned.count(REDACTED) == 2


def test_lines_without_a_key_are_untouched(captured):
    logger, buffer = captured
    line = 'CP0001: receive [3,"uid-1",{"status":"Accepted","public_key":"QUJD"}]'
    logger.info(line)
    assert buffer.getvalue().strip() == line
    assert redact_private_keys(line) is line      # fast path, no copy


def test_public_keys_are_not_redacted():
    text = '{"public_key": "QUJDRA==", "key_id": "0123456789abcdef"}'
    assert redact_private_keys(text) == text


def test_key_inside_a_traceback_is_redacted(captured):
    logger, buffer = captured
    try:
        raise ValueError(f'bad payload {{"private_key": "{FAKE_KEY_B64}"}}')
    except ValueError:
        logger.exception("dispatch failed")
    out = buffer.getvalue()
    assert "Traceback" in out and "dispatch failed" in out
    assert FAKE_KEY_B64 not in out


def test_filter_never_drops_a_record():
    record = logging.LogRecord("x", logging.INFO, __file__, 1,
                               "%s %s", ("only", "one"), None)
    record.args = ("too", "many", "args")       # getMessage() will raise
    assert PrivateKeyRedactingFilter().filter(record) is True


def test_install_is_idempotent(captured):
    logger, buffer = captured
    install_private_key_redaction(logger)
    install_private_key_redaction(logger)
    # pytest attaches its own capture handlers to loggers during a test,
    # so check every handler rather than assuming there is exactly one.
    assert logger.handlers
    for handler in logger.handlers:
        assert sum(isinstance(f, PrivateKeyRedactingFilter)
                   for f in handler.filters) == 1


def test_records_from_the_ocpp_library_logger_are_redacted():
    """The filter sits on the ROOT logger's handlers, so a record from the
    ocpp library's own logger (propagated upwards) is cleaned too."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    buffer = io.StringIO()
    root.handlers = [logging.StreamHandler(buffer)]
    root.setLevel(logging.INFO)
    try:
        install_private_key_redaction()
        logging.getLogger("ocpp").info("%s: send %s", "CP0001",
                                       _ocpp_send_frame())
    finally:
        root.handlers, root.level = saved_handlers, saved_level
    out = buffer.getvalue()
    assert "CP0001: send" in out
    assert FAKE_KEY_B64 not in out


def test_server_main_installs_the_redaction():
    # Static guard: main() must install the filter after basicConfig, or
    # the ocpp library's INFO lines go out unfiltered.
    source = inspect.getsource(server.main)
    assert "install_private_key_redaction()" in source
    assert source.index("logging.basicConfig(") < source.index(
        "install_private_key_redaction()"
    )
