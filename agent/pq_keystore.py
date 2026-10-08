"""
Where a charger keeps its own post-quantum key. Track C (agent). C-P2.

Contract 7, section 7.3 (PQCharge_Interface_Contracts.md).

--------------------------------------------------------------------
WHY THIS FILE EXISTS

Before Contract 7 the server made every charger's private key, sent it
over the network (where the ocpp library logged it: L04), and the
charger held it in memory only, so a restart lost it (L01). Now the
charger makes its own key pair, keeps the private key on its own disk,
and sends only the public key. This module is that disk.

One JSON file per charger, `<pq_key_dir>/<station_id>.json`:

    {"algorithm": "ML-DSA-44",
     "current":  {"key_id", "public_key", "private_key", "created_at"},
     "previous": {...} or absent}

`previous` is kept for one rotation, so a challenge that names the old
key_id can still be answered while the server switches over.

RULES (Contract 7 section 7.3):
  * Written atomically: a temporary file in the same folder, then
    os.replace over the real one. A crash mid-write leaves the old file
    intact, never half a key.
  * On Linux and the Pi the file is 0600 (owner read/write only).
    Windows cannot enforce that (L19); accepted for simulated chargers.
  * The private key is NEVER logged. Logs may show key_id.
  * A corrupt or unreadable file is reported loudly (ERROR) and treated
    as "no key". The charger then starts classical; the server's boot
    check will refuse it, which is visible. It does not crash the run.
--------------------------------------------------------------------
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.logging_setup import get_logger

FORMAT_VERSION = 1


@dataclass(frozen=True)
class StoredKey:
    """One ML-DSA key pair as the charger keeps it."""

    key_id: str
    public_key: bytes
    private_key: bytes
    created_at: str

    def to_json(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "public_key": base64.b64encode(self.public_key).decode("ascii"),
            "private_key": base64.b64encode(self.private_key).decode("ascii"),
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, obj: Any) -> StoredKey:
        if not isinstance(obj, dict):
            raise ValueError("key entry is not an object")
        try:
            public = base64.b64decode(obj["public_key"], validate=True)
            private = base64.b64decode(obj["private_key"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"key entry has a missing or invalid key: {exc}") from exc
        key_id = obj.get("key_id")
        if not public or not private or not isinstance(key_id, str) or not key_id:
            raise ValueError("key entry is incomplete")
        return cls(key_id=key_id, public_key=public, private_key=private,
                   created_at=str(obj.get("created_at") or ""))

    def __repr__(self) -> str:  # never let a private key reach a log via repr()
        return f"StoredKey(key_id={self.key_id!r}, created_at={self.created_at!r})"


@dataclass(frozen=True)
class KeyRecord:
    """What is on disk for one charger."""

    algorithm: str
    current: StoredKey
    previous: StoredKey | None = None


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class PQKeyStore:
    """
    The key file of ONE charger. No asyncio; small synchronous file I/O,
    done at charger start (load) and at enrolment (save) only, never in
    the middle of a measured exchange.
    """

    def __init__(self, key_dir: str | os.PathLike[str], station_id: str) -> None:
        if not station_id or any(c in station_id for c in '/\\:*?"<>|') or station_id in (".", ".."):
            raise ValueError(f"station_id {station_id!r} cannot be used as a file name")
        self.station_id = station_id
        self.dir = Path(key_dir)
        self.path = self.dir / f"{station_id}.json"
        self.log = get_logger(__name__, station_id=station_id)

    # -- reading -----------------------------------------------------------

    def load(self) -> KeyRecord | None:
        """
        The stored record, or None when there is none.

        A corrupt file is logged at ERROR and returns None (see the module
        docstring). It is left in place: the next enrolment overwrites it
        atomically, and until then a person can inspect it.
        """
        if not self.path.exists():
            return None
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("file is not a JSON object")
            algorithm = obj.get("algorithm")
            if not isinstance(algorithm, str) or not algorithm:
                raise ValueError("missing 'algorithm'")
            current = StoredKey.from_json(obj.get("current"))
            prev_obj = obj.get("previous")
            previous = StoredKey.from_json(prev_obj) if prev_obj else None
        except (OSError, ValueError) as exc:
            self.log.error(
                "post-quantum key file %s is unreadable (%s); starting with NO key. "
                "The server's boot check will refuse this charger until it is "
                "enrolled again.", self.path, exc,
            )
            return None
        return KeyRecord(algorithm=algorithm, current=current, previous=previous)

    # -- writing -----------------------------------------------------------

    def save(self, record: KeyRecord) -> None:
        """Write the record atomically, 0600 where the OS supports it."""
        self.dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "format": FORMAT_VERSION,
            "station_id": self.station_id,
            "algorithm": record.algorithm,
            "current": record.current.to_json(),
        }
        if record.previous is not None:
            payload["previous"] = record.previous.to_json()

        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.station_id}.", suffix=".tmp", dir=self.dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            if os.name == "posix":
                os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        except BaseException:
            # Never leave a half-written temporary file holding key bytes.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self.log.info("post-quantum key saved (key_id %s) to %s", record.current.key_id, self.path)
