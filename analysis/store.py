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

--------------------------------------------------------------------
SINCE RUNBOOK v1.3 (2026-10-14): ONE FILE PER VIEW, ONE PAGE FOR ALL

    analysis/output/runs/<name>.json   one demo run (analysis/demo.py),
                                       REPLACED when that run is analysed again
    analysis/output/experiments.json   the step-13 experiment runs (the
                                       slots described above; `write()`)
    analysis/output/results.json/.js   the page's bundle, rebuilt from the
                                       files above every time (format 4):
        current     the view the page opens on (the run analysed last)
        view_order  demo runs (newest first), "combined", "experiments"
        views       name -> view (a demo run or the experiments)
        combined    every demo run side by side + pooled (None without runs)

Nothing here ever creates a second file for the same run.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMAT_VERSION = 4
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_FILES = ("report.html", "charts.js", "vendor/echarts.min.js", "vendor/ECHARTS_LICENSE.txt")
RUNS_DIR = "runs"
EXPERIMENTS_FILE = "experiments.json"
COMBINED = "combined"
EXPERIMENTS = "experiments"


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


def _copy_web(out: Path) -> None:
    for name in WEB_FILES:
        src = WEB_DIR / name
        if src.exists():
            dst = out / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=str, allow_nan=False)


def write(results: dict[str, Any], out_dir: str | Path, make_current: bool = True) -> Path:
    """
    Save the EXPERIMENT results (run.build) as experiments.json and rebuild
    the page. make_current=False keeps the page opening on what it showed
    before (the load generator's automatic analysis must not take the page
    away from the demo run). Returns the page's results.json.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _atomic_write(out / EXPERIMENTS_FILE, _json(dict(results, kind=EXPERIMENTS, name=EXPERIMENTS)))
    return rebuild_bundle(out, current=EXPERIMENTS if make_current else None)


def write_demo(view: dict[str, Any], out_dir: str | Path) -> Path:
    """Save one demo run (runs/<name>.json, replacing an earlier analysis of
    it) and rebuild the page, opening on this run."""
    out = Path(out_dir)
    (out / RUNS_DIR).mkdir(parents=True, exist_ok=True)
    _atomic_write(out / RUNS_DIR / f"{view['name']}.json", _json(view))
    return rebuild_bundle(out, current=view["name"])


def _load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def rebuild_bundle(out_dir: str | Path, current: str | None = None) -> Path:
    """The page's results.json / results.js from every saved view."""
    from analysis import demo                     # here: demo imports run, run imports store

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    demos = [v for v in (_load(p) for p in sorted((out / RUNS_DIR).glob("*.json")))
             if v and v.get("kind") == "demo"]
    demos.sort(key=lambda v: ((v.get("summary") or {}).get("started_at") or "", v["name"]), reverse=True)
    experiments = _load(out / EXPERIMENTS_FILE)

    views: dict[str, Any] = {v["name"]: v for v in demos}
    order = [v["name"] for v in demos]
    if demos:
        order.append(COMBINED)
    if experiments:
        views[EXPERIMENTS] = experiments
        order.append(EXPERIMENTS)

    previous = _load(out / "results.json") or {}
    candidates = [current, previous.get("current"), order[0] if order else None]
    chosen = next((c for c in candidates if c and (c in views or c == COMBINED and demos)), None)

    bundle = {
        "format_version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current": chosen,
        "view_order": order,
        "views": views,
        "combined": demo.combine(list(reversed(demos))) if demos else None,
    }
    text = _json(bundle)
    _atomic_write(out / "results.json", text)
    _atomic_write(out / "results.js", f"window.PQCHARGE_RESULTS = {text};\n")
    _copy_web(out)
    return out / "results.json"


def read(out_dir: str | Path) -> dict[str, Any] | None:
    path = Path(out_dir) / "results.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
