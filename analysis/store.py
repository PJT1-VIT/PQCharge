"""
STEP 5 of the analysis — SAVE: the one results file.

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

All results live in ONE file: analysis/output/results.json.

Each test setup has ONE SLOT, named by what makes runs comparable:

    experiment | number of chargers | security mode | TLS on/off
    e.g.  "e1|n50|pqc|tls"

Running the same setup again REPLACES its slot -- the newest run wins, the
old one disappears from the results. Different setups sit side by side,
because the graphs compare them: "E1 classical" and "E1 post-quantum" must
both exist to draw the comparison.

The results file is rebuilt from the diaries every time, so it can never
drift from what was actually recorded. Deleting it loses nothing.

--------------------------------------------------------------------
WHAT GETS WRITTEN (analysis/output/, ignored by git)

    results.json   the results, for the dashboard (fetched over HTTP)
    results.js     the SAME data wrapped as `window.PQCHARGE_RESULTS = {...}`
                   -- browsers refuse to let a page opened from disk read a
                   .json file next to it, but they do load a script, so this
                   is what makes report.html work by double-click, no server
    report.html, charts.js, vendor/echarts.min.js
                   the results page, copied from analysis/web/

Both result files are written to a temporary name and then renamed into
place, so a page reading them never sees a half-written file.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

FORMAT_VERSION = 3
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_FILES = ("report.html", "charts.js", "vendor/echarts.min.js", "vendor/ECHARTS_LICENSE.txt")


def slot_key(experiment: str, n_stations: int, crypto_mode: str, tls: bool) -> str:
    return f"{experiment}|n{n_stations}|{crypto_mode}|{'tls' if tls else 'plain'}"


def latest_per_slot(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest run per slot wins. Returned in a stable, readable order."""
    best: dict[str, dict[str, Any]] = {}
    for s in slots:
        cur = best.get(s["key"])
        if cur is None or (s.get("started_at_epoch") or 0) > (cur.get("started_at_epoch") or 0):
            best[s["key"]] = s
    order = {"classical": 0, "hybrid": 1, "pqc": 2}
    return sorted(
        best.values(),
        key=lambda s: (s["experiment"], s["n_stations"], order.get(s["crypto_mode"], 9), s["tls"]),
    )


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write(results: dict[str, Any], out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    text = json.dumps(results, indent=1, default=str, allow_nan=False)
    _atomic_write(out / "results.json", text)
    _atomic_write(out / "results.js", f"window.PQCHARGE_RESULTS = {text};\n")

    for name in WEB_FILES:
        src = WEB_DIR / name
        if src.exists():
            dst = out / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    return out / "results.json"


def read(out_dir: str | Path) -> dict[str, Any] | None:
    path = Path(out_dir) / "results.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
