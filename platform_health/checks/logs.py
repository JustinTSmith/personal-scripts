"""
Log analysis checks:
- Scan last N lines of gateway.log and gateway.err.log for error patterns
- Scan today's /tmp OpenClaw log for errors and anomalies
- Detect crash loops (rapid restarts), MCP failures, auth errors
- Check gmail-automation log freshness and scan for errors
"""
import re
from pathlib import Path
from typing import List
from datetime import datetime

from ..config import GATEWAY_LOG, GATEWAY_ERR_LOG, GATEWAY_LOG_TMP, GMAIL_LOG, GMAIL_ERR_LOG, GMAIL_STALE_HOURS

# How many lines to tail from each log file
TAIL_LINES = 500

# Error patterns and their severity
ERROR_PATTERNS = [
    # (regex, label, severity)
    (re.compile(r'(?i)\bERROR\b'), "ERROR", "warn"),
    (re.compile(r'(?i)\bFATAL\b'), "FATAL", "fail"),
    (re.compile(r'(?i)\bUNCAUGHT\b'), "UNCAUGHT", "fail"),
    (re.compile(r'(?i)\bCRASH\b'), "CRASH", "fail"),
    (re.compile(r'(?i)mcp.*(?:fail|error|disconnect)'), "MCP failure", "warn"),
    (re.compile(r'(?i)auth.*(?:fail|denied|invalid)'), "Auth failure", "warn"),
    (re.compile(r'(?i)(?:ECONNREFUSED|ENOTFOUND|ETIMEDOUT)'), "Connection error", "warn"),
    (re.compile(r'(?i)skipped server'), "MCP server skipped", "warn"),
    (re.compile(r'(?i)out of memory'), "OOM", "fail"),
    (re.compile(r'(?i)segfault|signal 11'), "Segfault", "fail"),
]

# Restart detection: match actual gateway-level restarts only (not session starts)
# OpenClaw logs many "starting" events (MCP connections, agent sessions) — be specific
RESTART_PATTERN = re.compile(r'(?i)(?:gateway\s+(?:started|listening|ready)|port\s+18789|openclaw.*gateway.*start)')
RESTART_THRESHOLD = 10  # more than this in TAIL_LINES = crash loop

# Lines matching these patterns are noise — don't count them as errors
IGNORED_PATTERNS = [
    re.compile(r'(?i)unsupported file'),           # agent requesting non-supported file types
    re.compile(r'(?i)sendChatAction failed'),       # Telegram typing indicator, cosmetic
    re.compile(r'(?i)CHAT_PATH_LINKS'),             # known unsupported file request
    re.compile(r'(?i)already listening.*EADDRINUSE'),# stale "port in use" from prior restart
]


def _tail(path: Path, n: int) -> List[str]:
    """Return last n lines of a file. Empty list if file missing."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-n:] if len(lines) > n else lines
    except OSError:
        return []


def _analyze_lines(lines: List[str]) -> dict:
    """Return counts of matched error patterns."""
    counts: dict = {}
    restart_count = 0

    for line in lines:
        if RESTART_PATTERN.search(line):
            restart_count += 1

        # Skip lines matching known noise patterns
        if any(ip.search(line) for ip in IGNORED_PATTERNS):
            continue

        for pattern, label, severity in ERROR_PATTERNS:
            if pattern.search(line):
                key = (label, severity)
                counts[key] = counts.get(key, 0) + 1

    return {"patterns": counts, "restarts": restart_count}


def _log_check(path: Path, log_name: str, section: str = "Logs") -> List[dict]:
    results = []

    if not path.exists():
        results.append({
            "section": section,
            "status": "warn",
            "label": log_name,
            "detail": "file not found",
        })
        return results

    # Check file age — if > 2 days old, may be stale
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        age_hours = (datetime.now() - mtime).total_seconds() / 3600
        if age_hours > 48:
            results.append({
                "section": section,
                "status": "warn",
                "label": log_name,
                "detail": f"not written in {age_hours:.0f}h",
            })
            return results
    except OSError:
        pass

    lines = _tail(path, TAIL_LINES)
    if not lines:
        results.append({
            "section": section,
            "status": "warn",
            "label": log_name,
            "detail": "empty",
        })
        return results

    analysis = _analyze_lines(lines)

    # Crash loop detection
    if analysis["restarts"] > RESTART_THRESHOLD:
        results.append({
            "section": section,
            "status": "fail",
            "label": log_name,
            "detail": f"possible crash loop: {analysis['restarts']} restarts in last {TAIL_LINES} lines",
        })

    # Error pattern reporting — sort by severity (fail first), then count desc
    worst_severity = None
    detail_parts = []
    severity_rank = {"fail": 0, "warn": 1}
    for (label, severity), count in sorted(
        analysis["patterns"].items(),
        key=lambda x: (severity_rank.get(x[0][1], 2), -x[1])
    ):
        detail_parts.append(f"{label}×{count}")
        if worst_severity is None or (severity == "fail"):
            worst_severity = severity

    if detail_parts:
        results.append({
            "section": section,
            "status": worst_severity or "warn",
            "label": log_name,
            "detail": ", ".join(detail_parts[:5]),  # cap at 5 types
        })
    else:
        results.append({
            "section": section,
            "status": "ok",
            "label": log_name,
            "detail": f"clean ({len(lines)} lines scanned)",
        })

    return results


def _gmail_check() -> List[dict]:
    """Check gmail-automation log freshness and scan for errors."""
    results = []
    section = "Gmail Automation"

    if not GMAIL_LOG.exists():
        results.append({
            "section": section,
            "status": "fail",
            "label": "gmail-automation.log",
            "detail": "log file missing — service may never have run",
        })
        return results

    try:
        mtime = datetime.fromtimestamp(GMAIL_LOG.stat().st_mtime)
        age_hours = (datetime.now() - mtime).total_seconds() / 3600
    except OSError:
        results.append({
            "section": section,
            "status": "warn",
            "label": "gmail-automation.log",
            "detail": "could not stat log file",
        })
        return results

    if age_hours > GMAIL_STALE_HOURS:
        results.append({
            "section": section,
            "status": "fail",
            "label": "gmail-automation.log",
            "detail": f"stale: last write {age_hours:.1f}h ago (threshold {GMAIL_STALE_HOURS}h)",
        })
    else:
        results.append({
            "section": section,
            "status": "ok",
            "label": "gmail-automation.log",
            "detail": f"active: last write {age_hours:.1f}h ago",
        })

    # Scan error log for recent problems
    if GMAIL_ERR_LOG.exists():
        results.extend(_log_check(GMAIL_ERR_LOG, "gmail-automation-error.log", section=section))

    return results


def run() -> List[dict]:
    results = []

    # Persistent log files
    results.extend(_log_check(GATEWAY_LOG, "gateway.log"))
    results.extend(_log_check(GATEWAY_ERR_LOG, "gateway.err.log"))

    # Today's /tmp log
    if GATEWAY_LOG_TMP.exists():
        results.extend(_log_check(GATEWAY_LOG_TMP, f"gateway-tmp ({GATEWAY_LOG_TMP.name})"))
    else:
        results.append({
            "section": "Logs",
            "status": "warn",
            "label": "gateway-tmp",
            "detail": f"no log for today at {GATEWAY_LOG_TMP}",
        })

    # Gmail automation
    results.extend(_gmail_check())

    return results
