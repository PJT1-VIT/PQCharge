"""
Contract 7, section 7.3 — the charger makes, stores and loads its own key. Track C (tests). C-P2.

Fake provider, no quantcrypt: these run on every machine. The real ML-DSA
end-to-end checks are in test_pqc_enrolment_c7.py.

What these guard:
  * enrol() saves the key BEFORE answering; a save failure changes nothing.
  * The private key never appears in the reply, the logs or repr().
  * A restart (a new PQIdentity on the same folder) loads the same key and
    builds the provider at start (L10). No key file = no provider built.
  * Rotation keeps exactly one previous key; a challenge picks its key by
    key_id; an unknown key_id is refused.
  * The key file is written atomically, 0600 on POSIX; a corrupt file is
    reported and treated as "no key", never a crash.
  * Config: AgentConfig() keeps keys in memory (tests); the command line
    defaults to certs/pq; 'none' turns persistence off.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import stat

import pytest

from agent import pqc_messages as pqcm
from agent.config import AgentConfig, DEFAULT_PQ_KEY_DIR
from agent.pq_identity import PQIdentity
from agent.pq_keystore import PQKeyStore


class FakeProvider:
    """Deterministic stand-in for crypto.pq.PQProvider: 'signature' = sha256(private + message)."""

    signature_algorithm = "ML-DSA-44"

    def __init__(self) -> None:
        self.keypairs_made = 0

    def generate_keypair(self):
        self.keypairs_made += 1
        private = os.urandom(64)
        public = hashlib.sha256(b"pub" + private).digest() * 2
        return private, public

    def sign(self, private_key: bytes, message: bytes) -> bytes:
        return hashlib.sha256(private_key + message).digest()


def ident(tmp_path, station="CP0001", **kw):
    built = []

    def factory():
        p = FakeProvider()
        built.append(p)
        return p

    store = PQKeyStore(tmp_path, station) if tmp_path is not None else None
    identity = PQIdentity(station, key_store=store, provider_factory=factory, **kw)
    return identity, built


# -- enrol ------------------------------------------------------------------------


def test_enrol_saves_the_key_and_returns_only_public_material(tmp_path):
    pq, built = ident(tmp_path)
    public_key, key_id = pq.enrol("ML-DSA-44")

    assert pq.is_migrated and pq.key_id == key_id == pqcm.key_id_for(public_key)
    assert pq.algorithm == "ML-DSA-44" and pq.installs == 1

    on_disk = json.loads((tmp_path / "CP0001.json").read_text())
    assert on_disk["algorithm"] == "ML-DSA-44"
    assert on_disk["current"]["key_id"] == key_id
    assert base64.b64decode(on_disk["current"]["public_key"]) == public_key
    assert "previous" not in on_disk

    reply = json.loads(pqcm.pack_enrolment_reply("ML-DSA-44", public_key))
    assert on_disk["current"]["private_key"] not in json.dumps(reply)


def test_enrol_refuses_an_unsupported_algorithm(tmp_path):
    pq, _ = ident(tmp_path)
    with pytest.raises(ValueError):
        pq.enrol("ML-DSA-87")
    assert not pq.is_migrated and not (tmp_path / "CP0001.json").exists()


def test_a_legacy_capability_list_refuses_enrolment(tmp_path):
    pq, _ = ident(tmp_path, supported_algorithms=["ECDSA-P256"])
    assert pq.supports("ML-DSA-44") is False
    with pytest.raises(ValueError):
        pq.enrol("ML-DSA-44")


def test_if_the_key_cannot_be_saved_nothing_changes(tmp_path, monkeypatch):
    pq, _ = ident(tmp_path)
    pq.enrol("ML-DSA-44")
    before = pq.key_id

    def broken_save(record):
        raise OSError("disk full")

    monkeypatch.setattr(pq._key_store, "save", broken_save)
    with pytest.raises(OSError):
        pq.enrol("ML-DSA-44")
    assert pq.key_id == before and pq.previous_key_id is None


# -- restart, warm-up ------------------------------------------------------------------


def test_a_restarted_charger_loads_its_key_and_builds_the_provider_at_start(tmp_path):
    first, _ = ident(tmp_path)
    _pub, key_id = first.enrol("ML-DSA-44")
    nonce = os.urandom(32)
    expected = first.answer_challenge(nonce)

    again, built = ident(tmp_path)
    assert built == []                       # nothing built before load()
    assert again.load() is True
    assert len(built) == 1                   # L10: provider built at start, not at the first challenge
    assert again.key_id == key_id and again.is_migrated
    assert again.answer_challenge(nonce) == expected   # same key, same signature


def test_no_key_file_means_no_provider_is_built(tmp_path):
    pq, built = ident(tmp_path)
    assert pq.load() is False
    assert built == [] and not pq.is_migrated


def test_memory_only_identity_never_writes(tmp_path):
    pq, _ = ident(None)
    pq.enrol("ML-DSA-44")
    assert pq.is_migrated and list(tmp_path.iterdir()) == []


# -- rotation and key_id -------------------------------------------------------------------


def test_rotation_keeps_one_previous_key_and_challenges_pick_by_key_id(tmp_path):
    pq, _ = ident(tmp_path)
    _p1, k1 = pq.enrol("ML-DSA-44")
    _p2, k2 = pq.enrol("ML-DSA-44")
    _p3, k3 = pq.enrol("ML-DSA-44")
    assert (pq.key_id, pq.previous_key_id) == (k3, k2)    # k1 is gone

    on_disk = json.loads((tmp_path / "CP0001.json").read_text())
    assert (on_disk["current"]["key_id"], on_disk["previous"]["key_id"]) == (k3, k2)

    nonce = os.urandom(32)
    by_current = pq.answer_challenge(nonce, k3)
    by_previous = pq.answer_challenge(nonce, k2)
    assert by_current == pq.answer_challenge(nonce)       # no key_id = current
    assert by_current != by_previous
    with pytest.raises(LookupError):
        pq.answer_challenge(nonce, k1)


def test_the_deprecated_install_still_works_and_is_not_saved(tmp_path):
    pq, _ = ident(tmp_path)
    pq.install(os.urandom(64), "ML-DSA-44")
    assert pq.is_migrated and pq.key_id is None
    assert not (tmp_path / "CP0001.json").exists()


# -- the file itself ----------------------------------------------------------------------


def test_the_file_is_written_atomically_and_privately(tmp_path):
    pq, _ = ident(tmp_path)
    pq.enrol("ML-DSA-44")
    pq.enrol("ML-DSA-44")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "CP0001.json"]
    assert leftovers == []                                 # no temporary files left
    if os.name == "posix":
        mode = stat.S_IMODE((tmp_path / "CP0001.json").stat().st_mode)
        assert mode == 0o600


def test_a_corrupt_file_is_reported_and_treated_as_no_key(tmp_path, caplog):
    (tmp_path / "CP0001.json").write_text("{not json")
    pq, built = ident(tmp_path)
    with caplog.at_level(logging.ERROR):
        assert pq.load() is False
    assert not pq.is_migrated and built == []
    assert any("unreadable" in r.getMessage() for r in caplog.records)
    # the next enrolment replaces it
    pq.enrol("ML-DSA-44")
    assert json.loads((tmp_path / "CP0001.json").read_text())["current"]["key_id"] == pq.key_id


def test_the_private_key_never_reaches_the_log(tmp_path, caplog):
    pq, _ = ident(tmp_path)
    with caplog.at_level(logging.DEBUG):
        pq.enrol("ML-DSA-44")
        pq.answer_challenge(os.urandom(32))
        again, _ = ident(tmp_path)
        again.load()
    private_b64 = json.loads((tmp_path / "CP0001.json").read_text())["current"]["private_key"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert private_b64 not in text
    assert private_b64 not in repr(again._current)


@pytest.mark.parametrize("bad", ["", "../CP0001", "a/b", "a\\b", "..", "x:y"])
def test_a_station_id_that_is_not_a_safe_file_name_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        PQKeyStore(tmp_path, bad)


# -- configuration -----------------------------------------------------------------


def test_config_in_code_keeps_keys_in_memory():
    assert AgentConfig(station_id="CP0001").pq_key_dir is None


def _ns(*argv):
    parser = argparse.ArgumentParser()
    AgentConfig.add_arguments(parser)
    return AgentConfig.from_namespace(parser.parse_args(list(argv)))


def test_the_command_line_defaults_to_certs_pq_and_none_turns_it_off():
    assert DEFAULT_PQ_KEY_DIR == "certs/pq"
    assert _ns().pq_key_dir == "certs/pq"
    assert _ns("--pq-key-dir", "none").pq_key_dir is None
    assert _ns("--pq-key-dir", "keys/x").pq_key_dir == "keys/x"
