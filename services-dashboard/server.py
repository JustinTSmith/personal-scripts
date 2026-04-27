#!/usr/bin/env python3
"""
server.py — local HTTP server for the services dashboard.

Runs on 127.0.0.1:8765 only. Periodically regenerates the dashboard data and
serves an /api/fix endpoint backed by fixes.py.

Usage:
    python3 server.py            # foreground
    python3 server.py --port N   # alt port
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fixes
import scan

SCRIPT_DIR = Path(__file__).resolve().parent
DASHBOARD_HTML = SCRIPT_DIR / "dashboard.html"
DEFAULT_PORT = 8765
RESCAN_INTERVAL_S = 300  # 5 minutes

# Latest scan snapshot, refreshed by background thread
_latest_data: dict | None = None
_latest_lock = threading.Lock()


def _refresh_data() -> None:
    """Run the scanner and overwrite both dashboard.html and the in-memory snapshot."""
    global _latest_data
    data = scan.scan()
    DASHBOARD_HTML.write_text(scan.render_html(data))
    with _latest_lock:
        _latest_data = data
    print(f"[refresh] {len(data['services'])} services scanned", file=sys.stderr, flush=True)


def _refresh_loop() -> None:
    while True:
        try:
            _refresh_data()
        except Exception as e:  # noqa: BLE001
            print(f"[refresh] error: {e}", file=sys.stderr, flush=True)
        time.sleep(RESCAN_INTERVAL_S)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        # Quieter logging
        sys.stderr.write(f"[{self.address_string()}] {format % args}\n")
        sys.stderr.flush()

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._json(404, {"error": "not found"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html", "/dashboard.html"):
            self._file(DASHBOARD_HTML, "text/html; charset=utf-8")
            return
        if self.path == "/api/health":
            self._json(200, {"ok": True, "service": "services-dashboard"})
            return
        if self.path == "/api/data":
            with _latest_lock:
                payload = _latest_data
            if payload is None:
                self._json(503, {"error": "scan not ready"})
            else:
                self._json(200, payload)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/refresh":
            try:
                _refresh_data()
                self._json(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/fix":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(body or "{}")
                action = payload.get("action", "")
                label = payload.get("label", "")
            except Exception as e:  # noqa: BLE001
                self._json(400, {"ok": False, "message": f"Bad request: {e}"})
                return

            result = fixes.apply(action, label)
            # Re-scan after a successful fix so the UI sees fresh state
            if result.get("ok"):
                try:
                    _refresh_data()
                except Exception as e:  # noqa: BLE001
                    print(f"[refresh] post-fix error: {e}", file=sys.stderr, flush=True)
            self._json(200, result)
            return

        self._json(404, {"error": "not found"})


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args()

    # Initial scan before opening the socket so / serves real data immediately
    print("[boot] running initial scan…", file=sys.stderr, flush=True)
    _refresh_data()

    # Background scanner
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()

    # 127.0.0.1 only — no external access
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[boot] listening on http://127.0.0.1:{args.port}/", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[shutdown] bye", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
