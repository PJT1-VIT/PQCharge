"""L07 (identity check enforced by default) and L14 (two event types)."""

import pytest

from csms import server, transport
from csms.events import EventType


# -- L07 -------------------------------------------------------------------

def test_identity_check_defaults_to_enforce():
    assert transport.DEFAULT_IDENTITY_CHECK == "enforce"


def test_server_flag_defaults_to_enforce():
    # The --tls-identity-check default and the CSMS constructor default
    # both use the constant server.py imports from csms/transport.py.
    assert server.DEFAULT_IDENTITY_CHECK == "enforce"


@pytest.fixture
def certificate_says(monkeypatch):
    """Make check_identity see a certificate with the given CN."""
    def _set(common_name):
        monkeypatch.setattr(transport, "peer_certificate", lambda conn: {"x": 1})
        monkeypatch.setattr(transport, "peer_common_name", lambda cert: common_name)
    return _set


def test_default_refuses_a_borrowed_identity(certificate_says):
    # F1 / E5: CP0002's genuine certificate presented on CP0001's path.
    certificate_says("CP0002")
    ok, cn = transport.check_identity("CP0001", object())
    assert ok is False and cn == "CP0002"


def test_default_accepts_a_matching_identity(certificate_says):
    certificate_says("CP0001")
    assert transport.check_identity("CP0001", object()) == (True, "CP0001")


def test_default_refuses_an_unreadable_identity(certificate_says):
    certificate_says(None)
    assert transport.check_identity("CP0001", object()) == (False, None)


def test_warn_is_still_available_explicitly(certificate_says):
    certificate_says("CP0002")
    assert transport.check_identity("CP0001", object(), mode="warn") == (True, "CP0002")


# -- L14 -------------------------------------------------------------------

def test_contract3_has_the_two_migration_event_types():
    # Exactly the strings Track B's orchestrator writes and Track C reads.
    assert EventType.STATION_DEFERRED.value == "station_deferred"
    assert EventType.MIGRATION_FAILED.value == "migration_failed"


def test_existing_event_type_strings_are_unchanged():
    # Renaming a member would invalidate every recorded log (Contract 3).
    for name, value in {
        "MIGRATION_STARTED": "migration_started",
        "WAVE_STARTED": "wave_started",
        "WAVE_COMPLETED": "wave_completed",
        "WAVE_ROLLED_BACK": "wave_rolled_back",
        "MIGRATION_COMPLETED": "migration_completed",
        "CONNECTION_ATTEMPT": "connection_attempt",
        "CONNECTION_CLOSED": "connection_closed",
        "CERTIFICATE_INSTALLED": "certificate_installed",
    }.items():
        assert EventType[name].value == value
