from __future__ import annotations
"""
Self-healing engine.

Two responsibilities:
1. ANALYSIS — scan check results and enrich them with heal_action / heal_cmd /
   drill_detail fields. Also runs error log pattern matching to generate
   specific patch suggestions. Runs on every health check.

2. EXECUTION — given an item number from drill_state.json, attempt to run
   the fix. Called only via `--heal N` flag. Returns (success, message).

Auto-executable fixes (safe to run without human in the loop):
  - Load a stopped LaunchAgent
  - Git commit + push staged changes in ~/.openclaw repos
  - Create missing directories

Everything else is advisory: heal_action describes what to do, heal_cmd
gives the exact shell command, but execution requires explicit `--heal N`.
"""
import re
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from ..config import OPENCLAW_DIR, LAUNCHAGENTS_DIR, GATEWAY_LOG, GATEWAY_LOG_TMP

log = logging.getLogger("platform_health.heal")

OPENCLAW_REPOS = [
    OPENCLAW_DIR,
    OPENCLAW_DIR / "workspace",
]

# ── Error log pattern → patch mapping ─────────────────────────────────────────
# Each entry: (compiled regex, fix_description, optional_shell_cmd)
ERROR_PATCH_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"Cannot find module '([^']+)'", re.I),
        "Missing npm module — reinstall dependencies",
        "cd ~/.openclaw && npm install",
    ),
    (
        re.compile(r"ECONNREFUSED.*:11434", re.I),
        "Ollama not running on :11434",
        "ollama serve",
    ),
    (
        re.compile(r"ECONNREFUSED.*:18789", re.I),
        "OpenClaw gateway unreachable",
        "launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist",
    ),
    (
        re.compile(r"(?:invalid|expired|missing).*(?:token|key|auth)", re.I),
        "Auth token invalid — check openclaw.json gateway.auth.token",
        "",
    ),
    (
        re.compile(r"SQLITE_BUSY", re.I),
        "SQLite WAL locked — force checkpoint",
        "python3 -c \"import sqlite3; c=sqlite3.connect('~/.openclaw/tasks/runs.sqlite'); c.execute('PRAGMA wal_checkpoint(FULL)'); c.close()\"",
    ),
    (
        re.compile(r"no space left", re.I),
        "Disk full — clear old logs",
        "find /tmp/openclaw -name '*.log' -mtime +7 -delete",
    ),
    (
        re.compile(r"out of memory", re.I),
        "OOM — check processes",
        "ps aux --sort=-%mem | head -10",
    ),
    (
        re.compile(r"ETIMEDOUT|EHOSTUNREACH", re.I),
        "Network timeout — check internet / VPN",
        "",
    ),
    (
        re.compile(r"Unhandled rejection|UnhandledPromiseRejection", re.I),
        "Unhandled Promise rejection — check gateway logs for stack trace",
        "",
    ),
    (
        re.compile(r"skipped server.*open-brain", re.I),
        "OB1 MCP server skipped at startup — check open-brain URL in openclaw.json",
        "",
    ),
    (
        re.compile(r"skipped server", re.I),
        "An MCP server was skipped — likely config or auth issue",
        "",
    ),
]

# ── LaunchAgent fix ────────────────────────────────────────────────────────────

def _launchagent_plist(label: str) -> Path | None:
    plist = LAUNCHAGENTS_DIR / f"{label}.plist"
    return plist if plist.exists() else None


def _heal_launchagent(label: str) -> Tuple[bool, str]:
    plist = _launchagent_plist(label)
    if plist is None:
        return False, f"plist not found for {label}"
    rc = subprocess.run(
        ["launchctl", "load", str(plist)],
        capture_output=True, text=True, timeout=10,
    )
    if rc.returncode == 0:
        return True, f"Loaded {label}"
    return False, f"launchctl load failed: {rc.stderr.strip()[:120]}"


# ── Git commit + push fix ──────────────────────────────────────────────────────

def _git(repo: Path, *args) -> Tuple[int, str]:
    r = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True, text=True, timeout=30,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def _heal_git_repo(repo: Path) -> Tuple[bool, str]:
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        return False, f"{repo} is not a git repo"

    dirty_rc, _ = _git(repo, "status", "--porcelain")
    _, dirty = _git(repo, "status", "--porcelain")

    messages = []
    if dirty:
        _git(repo, "add", "-A")
        rc, out = _git(
            repo, "commit", "-m",
            f"auto: platform health sync {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        )
        messages.append("committed" if rc == 0 else f"commit failed: {out[:80]}")

    # Push if remote exists
    rc, _ = _git(repo, "remote", "get-url", "origin")
    if rc == 0:
        rc, out = _git(repo, "push", "--quiet")
        messages.append("pushed" if rc == 0 else f"push failed: {out[:80]}")
    else:
        messages.append("no remote — skipping push")

    return True, "; ".join(messages) if messages else "nothing to do"


# ── Error log analysis → patch suggestions ────────────────────────────────────

def _tail_errors(path: Path, n: int = 200) -> List[str]:
    """Return last n lines containing ERROR/FATAL from a log file."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [l for l in lines if re.search(r"\b(ERROR|FATAL|UNCAUGHT)\b", l, re.I)][-n:]
    except OSError:
        return []


def analyze_error_logs() -> List[dict]:
    """
    Read error lines from gateway logs and return a list of patch suggestion
    dicts (compatible with check result format) with drill_detail and heal fields.
    """
    results = []
    error_lines = _tail_errors(GATEWAY_LOG) + _tail_errors(GATEWAY_LOG_TMP)

    if not error_lines:
        return []

    # Deduplicate by matching pattern
    seen_patterns: set = set()
    suggestions = []

    for line in error_lines[-50:]:  # most recent 50 error lines
        for pattern, fix_desc, fix_cmd in ERROR_PATCH_PATTERNS:
            m = pattern.search(line)
            if m and fix_desc not in seen_patterns:
                seen_patterns.add(fix_desc)
                suggestions.append({
                    "section": "ErrorPatterns",
                    "status": "warn",
                    "label": f"patch: {fix_desc[:50]}",
                    "detail": line.strip()[-120:],
                    "heal_action": fix_desc,
                    "heal_cmd": fix_cmd,
                    "drill_detail": f"Matched in log:\n`{line.strip()[-200:]}`",
                })
                break  # one pattern match per line

    return suggestions[:5]  # cap at 5 patch suggestions


# ── Result enrichment ──────────────────────────────────────────────────────────

def enrich_results(results: List[dict]) -> List[dict]:
    """
    Walk through check results and attach heal_action / heal_cmd / drill_detail
    to results that have known fixes. Returns enriched copy.
    Does NOT execute anything — analysis only.
    """
    enriched = []

    for r in results:
        r = dict(r)  # don't mutate original
        section = r.get("section", "")
        label = r.get("label", "")
        status = r.get("status", "ok")
        detail = r.get("detail", "")

        if status not in ("warn", "fail"):
            enriched.append(r)
            continue

        # LaunchAgent stopped
        if section == "LaunchAgents" and status in ("warn", "fail"):
            plist = _launchagent_plist(label)
            if plist:
                r["heal_action"] = f"Reload {label} LaunchAgent"
                r["heal_cmd"] = f"launchctl load {plist}"
                r["drill_detail"] = (
                    f"LaunchAgent: `{label}`\n"
                    f"Plist: `{plist}`\n"
                    f"Status: {detail}"
                )

        # Gateway HTTP unreachable
        elif section == "Gateway" and "unreachable" in detail:
            plist = _launchagent_plist("ai.openclaw.gateway")
            r["heal_action"] = "Restart OpenClaw gateway"
            r["heal_cmd"] = (
                f"launchctl unload {plist} && sleep 3 && launchctl load {plist}"
                if plist else "launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist"
            )
            r["drill_detail"] = f"Gateway at localhost:18789 is not responding.\n{detail}"

        # Git dirty working tree
        elif section == "Git" and "uncommitted" in detail:
            repo_label = label.split()[0] if label else "openclaw"
            repo_path = OPENCLAW_DIR if repo_label == "openclaw" else OPENCLAW_DIR / "workspace"
            r["heal_action"] = f"Auto-commit and push {repo_label} repo"
            r["heal_cmd"] = (
                f"git -C {repo_path} add -A && "
                f"git -C {repo_path} commit -m 'auto: platform health sync' && "
                f"git -C {repo_path} push"
            )

        # Git not pushed
        elif section == "Git" and "not pushed" in detail:
            repo_label = label.split()[0] if label else "openclaw"
            repo_path = OPENCLAW_DIR if repo_label == "openclaw" else OPENCLAW_DIR / "workspace"
            r["heal_action"] = f"Push {repo_label} to remote"
            r["heal_cmd"] = f"git -C {repo_path} push"

        # Cron failures — drill detail with task info
        elif section == "Crons" and "failed" in detail:
            r["drill_detail"] = (
                "Run:\n"
                "`sqlite3 ~/.openclaw/tasks/runs.sqlite "
                "\"SELECT task_id,label,status,error FROM task_runs "
                "WHERE status IN ('failed','lost','timed_out') "
                "ORDER BY created_at DESC LIMIT 10;\"`"
            )

        # Log errors — drill detail
        elif section == "Logs" and "ERROR" in detail:
            log_name = label
            r["drill_detail"] = (
                f"Scan: `grep -i ERROR ~/.openclaw/logs/{log_name} | tail -20`"
            )
            r["heal_action"] = "Review errors above (run log scan for root cause)"

        enriched.append(r)

    return enriched


# ── Execute a heal action ──────────────────────────────────────────────────────

def execute_heal(item: dict) -> Tuple[bool, str]:
    """
    Execute the heal_cmd for a drill item. Returns (success, message).
    Only auto-executes safe, pre-approved command classes:
      - launchctl load
      - git commit + push on known repos
    Anything else: returns the command for manual execution.
    """
    heal_cmd = item.get("heal_cmd", "")
    heal_action = item.get("heal_action", "")
    label = item.get("label", "")
    section = item.get("section", "")

    if not heal_cmd and not heal_action:
        return False, "No heal action defined for this item"

    log.info("Heal requested: section=%s label=%s", section, label)

    # ── LaunchAgent reload ────────────────────────────────────────────────────
    if section == "LaunchAgents":
        ok, msg = _heal_launchagent(label)
        log.info("LaunchAgent heal %s: %s", label, msg)
        return ok, msg

    # ── Git repo auto-commit + push ───────────────────────────────────────────
    if section == "Git":
        # Determine which repo
        repo_label = label.split()[0] if label else ""
        if "workspace" in repo_label:
            repo = OPENCLAW_DIR / "workspace"
        elif "openclaw" in repo_label:
            repo = OPENCLAW_DIR
        else:
            return False, f"Unknown repo from label: {label}"
        ok, msg = _heal_git_repo(repo)
        log.info("Git heal %s: %s", repo, msg)
        return ok, msg

    # ── Everything else: return the command for manual execution ─────────────
    if heal_cmd:
        log.info("Non-auto-executable heal for %s — returning command", label)
        return False, f"Run manually:\n`{heal_cmd}`"

    return False, f"No executable fix available. Suggested action: {heal_action}"
