from __future__ import annotations
"""
Gateway checks:
- LaunchAgent status for all CRITICAL_LAUNCHAGENTS via launchctl list
- HTTP ping to OpenClaw gateway at localhost:18789/health
- Process uptime from launchctl
"""
import subprocess
import urllib.request
import urllib.error
import json
import time
from typing import List

from ..config import CRITICAL_LAUNCHAGENTS

GATEWAY_URL = "http://localhost:18789/health"
PING_TIMEOUT = 5  # seconds

# Cron-style LaunchAgents that legitimately stop between runs (exit 0, no PID is normal)
CRON_STYLE_AGENTS = {
    "com.justinsmith.ob1-cascade-check",
    "com.justinsmith.ob1-evidence-scan",
    "com.justinsmith.ob1-evidence-weekly",
}


def _launchctl_list() -> dict:
    """Return dict of {label: pid_or_None} from `launchctl list`."""
    try:
        out = subprocess.check_output(
            ["launchctl", "list"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return {}

    result = {}
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        pid_str, status_str, label = parts
        pid = None if pid_str == "-" else pid_str
        result[label] = {"pid": pid, "status": status_str}
    return result


def _ping_gateway() -> dict:
    """HTTP GET to /health. Returns {ok: bool, body: str, latency_ms: int}."""
    t0 = time.monotonic()
    try:
        req = urllib.request.urlopen(GATEWAY_URL, timeout=PING_TIMEOUT)
        body = req.read(512).decode("utf-8", errors="replace")
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "body": body, "latency_ms": latency_ms}
    except urllib.error.URLError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "body": str(e), "latency_ms": latency_ms}
    except Exception as e:
        return {"ok": False, "body": str(e), "latency_ms": 0}


def run() -> List[dict]:
    results = []
    launchctl = _launchctl_list()

    # ── LaunchAgent checks ───────────────────────────────────────────────────
    for label in CRITICAL_LAUNCHAGENTS:
        entry = launchctl.get(label)
        if entry is None:
            results.append({
                "section": "LaunchAgents",
                "status": "fail",
                "label": label,
                "detail": "not loaded",
            })
        elif entry["pid"] is None:
            # Loaded but not running — check exit status
            exit_code = entry.get("status", "?")
            # Cron-style agents with exit 0 are normal — they run on schedule
            if label in CRON_STYLE_AGENTS and exit_code in ("0", "-"):
                results.append({
                    "section": "LaunchAgents",
                    "status": "ok",
                    "label": label,
                    "detail": f"scheduled (exit {exit_code})",
                })
            else:
                status = "warn" if exit_code in ("0", "-") else "fail"
                results.append({
                    "section": "LaunchAgents",
                    "status": status,
                    "label": label,
                    "detail": f"stopped (exit {exit_code})",
                })
        else:
            results.append({
                "section": "LaunchAgents",
                "status": "ok",
                "label": label,
                "detail": f"PID {entry['pid']}",
            })

    # ── Gateway HTTP check ───────────────────────────────────────────────────
    ping = _ping_gateway()
    if ping["ok"]:
        # Parse JSON if possible
        try:
            body = json.loads(ping["body"])
            ok_flag = body.get("ok", False)
            if ok_flag:
                results.append({
                    "section": "Gateway",
                    "status": "ok",
                    "label": "HTTP /health",
                    "detail": f"{ping['latency_ms']}ms",
                })
            else:
                results.append({
                    "section": "Gateway",
                    "status": "warn",
                    "label": "HTTP /health",
                    "detail": f"responded but ok=false ({ping['latency_ms']}ms)",
                })
        except (json.JSONDecodeError, ValueError):
            results.append({
                "section": "Gateway",
                "status": "warn",
                "label": "HTTP /health",
                "detail": f"non-JSON response ({ping['latency_ms']}ms)",
            })
    else:
        results.append({
            "section": "Gateway",
            "status": "fail",
            "label": "HTTP /health",
            "detail": f"unreachable — {ping['body'][:80]}",
        })

    return results
