#!/usr/bin/env python3
"""
Nerve dashboard bridge — pushes cron job status to http://localhost:3080/

Nerve (github.com/evilsocket/nerve) exposes a REST API on its web UI port.
This module syncs cron_log runs to Nerve so they appear in the dashboard.

Config:
  NERVE_URL          Base URL (default: http://localhost:3080)
  NERVE_API_TOKEN    Bearer token if auth is enabled

CLI:
  python3 nerve_bridge.py sync [--limit 50]
  python3 nerve_bridge.py push <run_id>
  python3 nerve_bridge.py status
"""
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

import requests

NERVE_BASE = os.environ.get("NERVE_URL", "http://localhost:3080").rstrip("/")
TIMEOUT = 5


def _headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.environ.get("NERVE_API_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _is_available() -> bool:
    try:
        r = requests.get(f"{NERVE_BASE}/", headers=_headers(), timeout=TIMEOUT)
        return r.status_code < 500
    except Exception:
        return False


def _push_run(run: dict) -> bool:
    """
    Push one cron run to Nerve. Tries POST first, falls back to PUT on conflict.
    Nerve task payload maps cron concepts to agent task fields.
    """
    status_map = {
        "success": "completed",
        "failure": "failed",
        "running": "running",
        "skipped": "skipped",
        "failed": "failed",
    }

    payload = {
        "id": run["run_id"],
        "name": f"cron/{run['job_name']}",
        "status": status_map.get(run["status"], run["status"]),
        "started_at": datetime.fromtimestamp(run["started_at"]).isoformat(),
        "ended_at": datetime.fromtimestamp(run["ended_at"]).isoformat() if run.get("ended_at") else None,
        "duration_sec": run.get("duration_sec"),
        "output": run.get("summary", ""),
        "tags": ["cron"],
        "source": "cron-log",
    }

    # Attempt POST (create)
    try:
        r = requests.post(
            f"{NERVE_BASE}/api/v1/task",
            json=payload,
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if r.ok:
            return True
        if r.status_code == 409:
            # Already exists — update via PUT
            r2 = requests.put(
                f"{NERVE_BASE}/api/v1/task/{run['run_id']}",
                json=payload,
                headers=_headers(),
                timeout=TIMEOUT,
            )
            return r2.ok
        # Some Nerve versions use /api/tasks (no version prefix)
        r = requests.post(
            f"{NERVE_BASE}/api/tasks",
            json=payload,
            headers=_headers(),
            timeout=TIMEOUT,
        )
        return r.ok
    except requests.RequestException as e:
        print(f"[nerve] push failed for {run['run_id'][:8]}: {e}", file=sys.stderr)
        return False


def sync(limit: int = 50) -> int:
    """Sync recent cron log entries to Nerve. Returns number synced."""
    if not _is_available():
        print(f"[nerve] Dashboard not reachable at {NERVE_BASE}", file=sys.stderr)
        return 0

    # Import here to allow this module to be used standalone
    sys.path.insert(0, os.path.dirname(__file__))
    import cron_log

    runs = cron_log.query(limit=limit)
    synced = 0
    for run in runs:
        if _push_run(run):
            synced += 1
    print(f"[nerve] Synced {synced}/{len(runs)} runs to {NERVE_BASE}")
    return synced


def push_run_id(run_id: str) -> bool:
    """Push a single run by ID."""
    sys.path.insert(0, os.path.dirname(__file__))
    import cron_log

    rows = cron_log.query(limit=1000)
    for run in rows:
        if run["run_id"] == run_id or run["run_id"].startswith(run_id):
            return _push_run(run)
    print(f"[nerve] run_id not found: {run_id}", file=sys.stderr)
    return False


def status() -> None:
    available = _is_available()
    print(f"Nerve dashboard: {NERVE_BASE}")
    print(f"Status: {'reachable' if available else 'unreachable'}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "sync":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        count = sync(limit)
        sys.exit(0 if count >= 0 else 1)

    elif cmd == "push":
        if len(sys.argv) < 3:
            print("Usage: nerve_bridge.py push <run_id>", file=sys.stderr)
            sys.exit(1)
        ok = push_run_id(sys.argv[2])
        sys.exit(0 if ok else 1)

    elif cmd == "status":
        status()

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
