"""
experiments/bootstrap_pki.py: every run replaces the certificates of the
previous run (2026-10-14). Old certificate files are deleted, the chargers'
ML-DSA key folder (certs/pq) and anything else in certs/ is kept.
Track C asked for it (tracker step 8); the file is Track B's.
"""

from __future__ import annotations

from experiments import bootstrap_pki as bp


def _touch(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_old_certificates_are_deleted_and_key_folders_are_kept(tmp_path):
    for name in ("root.pem", "localhost.crt.pem", "localhost.key.pem",
                 "CP0400.crt.pem", "CP0400.key.pem", "CP001.crt.pem"):
        _touch(tmp_path / name)
    pq_key = _touch(tmp_path / "pq" / "CP0001.json", "{}")
    archived = _touch(tmp_path / "pq_old_1750" / "CP0002.json", "{}")
    nested_pem = _touch(tmp_path / "pq" / "stray.crt.pem")
    other = _touch(tmp_path / "README.txt")

    removed = bp.clear_old_certificates(tmp_path)

    assert removed == sorted(["root.pem", "localhost.crt.pem", "localhost.key.pem",
                              "CP0400.crt.pem", "CP0400.key.pem", "CP001.crt.pem"])
    assert not list(tmp_path.glob("*.pem"))
    assert pq_key.exists() and archived.exists() and nested_pem.exists() and other.exists()


def test_a_smaller_run_leaves_no_certificate_from_the_bigger_one(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "CERT_DIR", tmp_path)
    bp.build_pki(bp.station_ids(5, ["CP0000"]))
    first_root = (tmp_path / "root.pem").read_bytes()
    pq_key = _touch(tmp_path / "pq" / "CP0001.json", "{}")

    bp.build_pki(bp.station_ids(2))                                 # no --also CP0000 this time
    names = sorted(p.name for p in tmp_path.glob("*.pem"))
    assert names == ["CP0001.crt.pem", "CP0001.key.pem", "CP0002.crt.pem", "CP0002.key.pem",
                     "localhost.crt.pem", "localhost.key.pem", "root.pem"]
    assert (tmp_path / "root.pem").read_bytes() != first_root       # a new root CA
    assert pq_key.exists()


def test_a_missing_folder_is_not_an_error(tmp_path):
    assert bp.clear_old_certificates(tmp_path / "nothing-here") == []
