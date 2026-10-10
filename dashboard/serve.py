"""
Serve the live dashboard — against the real CSMS, or against the mock.

Track C (dashboard). Phase C-F6 (FINAL 2-DAY PLAN).

--------------------------------------------------------------------
IN PLAIN WORDS

The dashboard is three static files in dashboard/static/. Once Track A's
A-F4 is on `main`, the CSMS serves them itself at http://<server>:9000/dashboard/
and nothing here is needed. Until then -- and for rehearsals -- run:

    python -m dashboard.serve --mock
        a pretend fleet (dashboard/mock.py): no server, no chargers needed.

    python -m dashboard.serve --api http://localhost:9000
        the real CSMS. Every /api/... request is passed through to it
        unchanged (GET only, like the CSMS itself).

    then open  http://localhost:8080/dashboard/

The page only ever asks for /api/...; it never knows which of the three is
answering. So what works here works unchanged when the CSMS serves the page.

For a CSMS running with --tls, give an https:// --api and --cert-dir: the
pass-through then presents a charger certificate to the server (the same
stand-in the fleet watcher uses; limitations.md L08).

Python's built-in HTTP server only (Track C decision C4): no new package.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dashboard.mock import MockFleet

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


def static_file(path: str) -> tuple[bytes, str] | None:
    """(body, content type) for /dashboard/<path>, or None. Never leaves STATIC_DIR."""
    rel = path[len("/dashboard/"):] if path.startswith("/dashboard/") else ""
    rel = rel or "index.html"
    target = (STATIC_DIR / rel).resolve()
    if STATIC_DIR not in target.parents and target != STATIC_DIR:
        return None
    if not target.is_file():
        return None
    ctype = CONTENT_TYPES.get(target.suffix) or mimetypes.guess_type(target.name)[0] \
        or "application/octet-stream"
    return target.read_bytes(), ctype


def _q(query: dict[str, list[str]], name: str, default: Any, cast=int) -> Any:
    try:
        return cast(query[name][0])
    except (KeyError, IndexError, ValueError, TypeError):
        return default


def mock_api(mock: MockFleet, path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
    """Answer one /api request from the mock. (status, JSON body)."""
    if path == "/api/health":
        return 200, mock.health()
    if path == "/api/fleet":
        return 200, mock.fleet()
    if path.startswith("/api/fleet/") and path not in ("/api/fleet/limit", "/api/fleet/clear-limit"):
        sid = path[len("/api/fleet/"):]
        for st in mock.fleet()["stations"]:
            if st["station_id"] == sid:
                return 200, st
        return 404, {"error": "unknown station", "station_id": sid}
    if path == "/api/migration":
        return 200, mock.migration()
    if path == "/api/events":
        return 200, mock.events_after(_q(query, "after", 0), _q(query, "limit", 500))
    if path == "/api/migration/start":
        return mock.start(_q(query, "wave_size", 10), _q(query, "canary_count", 5),
                          _q(query, "target_mode", "hybrid", str))
    if path == "/api/migration/rotate":
        return mock.rotate(_q(query, "wave_size", 10), _q(query, "canary_count", 5))
    if path == "/api/migration/rollback":
        return mock.rollback(_q(query, "wave_id", -1))
    if path == "/api/fleet/limit":
        watts = _q(query, "watts", None, float)
        if watts is None:
            return 400, {"error": "watts is required"}
        return mock.set_limit(watts)
    if path == "/api/fleet/clear-limit":
        return mock.set_limit(None)
    if path == "/api/mock/storm":
        return mock.storm(_q(query, "outage_s", 3.0, float))
    if path == "/api/mock/impostor":
        return mock.impostor(_q(query, "station_id", "CP0003", str))
    return 404, {"error": "unknown path", "path": path}


def make_handler(*, mock: MockFleet | None, api: str | None,
                 ssl_context: ssl.SSLContext | None = None, timeout_s: float = 5.0):
    """The request handler class, bound to one back end (mock or real CSMS)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "PQChargeDashboard/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet: polled 3x a second
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj, default=str).encode("utf-8"), CONTENT_TYPES[".json"])

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            if path in ("/", "/dashboard"):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/dashboard/")
                self.end_headers()
                return
            if path.startswith("/dashboard/"):
                found = static_file(path)
                if found is None:
                    self._json(404, {"error": "not found", "path": path})
                else:
                    self._send(200, *found)
                return
            if not path.startswith("/api/"):
                self._json(404, {"error": "not found", "path": path})
                return
            if mock is not None:
                status, body = mock_api(mock, path, urllib.parse.parse_qs(url.query))
                self._json(status, body)
                return
            self._proxy(self.path)

        def _proxy(self, path_and_query: str) -> None:
            target = api.rstrip("/") + path_and_query
            try:
                with urllib.request.urlopen(urllib.request.Request(target, method="GET"),
                                            timeout=timeout_s, context=ssl_context) as resp:
                    self._send(resp.status, resp.read(),
                               resp.headers.get("Content-Type", CONTENT_TYPES[".json"]))
            except urllib.error.HTTPError as exc:      # the CSMS answered with an error: pass it on
                self._send(exc.code, exc.read(), exc.headers.get("Content-Type", CONTENT_TYPES[".json"]))
            except (urllib.error.URLError, OSError) as exc:
                self._json(502, {"error": "CSMS not reachable", "api": api,
                                 "detail": f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"})

    return Handler


def _ssl_for(api: str, cert_dir: str, cert_station: str) -> ssl.SSLContext | None:
    if not api.lower().startswith("https://"):
        return None
    from agent.config import AgentConfig  # noqa: PLC0415
    from harness.load_generator import _watch_ssl_context  # noqa: PLC0415

    host = api.split("://", 1)[1].rstrip("/")
    return _watch_ssl_context(AgentConfig(station_id=cert_station, csms_url=f"wss://{host}",
                                          cert_dir=cert_dir), cert_station)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m dashboard.serve",
                                description="Serve the PQCharge live dashboard.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--mock", action="store_true", help="a pretend fleet (no server needed)")
    src.add_argument("--api", help="the CSMS base address, e.g. http://localhost:9000")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--mock-n", type=int, default=49, help="simulated chargers (plus CP-PI01)")
    p.add_argument("--mock-mode", choices=["classical", "hybrid"], default="hybrid")
    p.add_argument("--mock-no-tls", action="store_true")
    p.add_argument("--mock-halt", action="store_true",
                   help="CP0021-CP0025 refuse their key: the wave rolls back and the migration halts")
    p.add_argument("--cert-dir", default="certs", help="TLS --api only")
    p.add_argument("--cert-station", default="CP0001", help="TLS --api only (L08 stand-in)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mock = None
    if args.mock:
        mock = MockFleet(n=args.mock_n, mode=args.mock_mode, tls=not args.mock_no_tls,
                         halt=args.mock_halt)
    ssl_context = None if args.mock else _ssl_for(args.api, args.cert_dir, args.cert_station)
    handler = make_handler(mock=mock, api=args.api, ssl_context=ssl_context)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    source = "MOCK fleet (made-up data)" if args.mock else f"CSMS at {args.api}"
    print(f"PQCharge dashboard: http://{args.host}:{args.port}/dashboard/   data: {source}")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
