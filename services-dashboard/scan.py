#!/usr/bin/env python3
"""
services-dashboard / scan.py

Scans ~/Library/LaunchAgents for all installed launchd services, cross-references
their runtime status from `launchctl list`, reads tail of their log files, and
generates a self-contained dashboard.html with embedded JSON.

Usage:
    python3 scan.py            # writes dashboard.html in this directory
    python3 scan.py --open     # also opens it in the default browser
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HOME = Path.home()
LAUNCH_AGENTS_DIR = HOME / "Library" / "LaunchAgents"
SCRIPT_DIR = Path(__file__).resolve().parent
DASHBOARD_HTML = SCRIPT_DIR / "dashboard.html"
EXPLANATION_CACHE = SCRIPT_DIR / "explanations.json"
ENV_FILE = HOME / ".config" / "ai" / ".env"

EXPLANATION_MODEL = "claude-haiku-4-5"  # cheap, fast, good enough for this
EXPLANATION_SOURCE_MAX = 30_000  # bytes of source to send to the LLM
EXPLANATION_MAX_TOKENS = 800

# Namespaces we consider "personal projects" — shown by default. Everything else
# (homebrew, google updaters, system stuff) is collapsed under "system" and
# hidden behind a filter toggle.
PERSONAL_NAMESPACES = {
    "com.justinsmith",
    "com.justinos",
    "com.openclaw",
    "ai.openclaw",
    "com.last30days",
    "com.paperclip",
    "com.mudrii",
    "com.user",
}

LOG_TAIL_LINES = 30
SCRIPT_MAX_BYTES = 200_000  # 200KB cap for embedded source

# Keyword → channel-label mapping for delivery-target detection.
# Order matters: more specific patterns first.
CHANNEL_PATTERNS = [
    ("Telegram",  re.compile(r"\b(telegram|tg_send|tg_bot|TelegramBot|api\.telegram\.org)\b", re.I)),
    ("Twilio/SMS", re.compile(r"\b(twilio|TwilioRestClient|\.messages\.create)\b", re.I)),
    ("Email",     re.compile(r"\b(smtp|smtplib|sendgrid|mailgun|gmail\.send|send_mail|send_email|service\.users\(\)\.messages\(\)\.send)\b", re.I)),
    ("Slack",     re.compile(r"\b(slack_webhook|slack_sdk|slack\.com/api|chat\.postMessage)\b", re.I)),
    ("Discord",   re.compile(r"\b(discord_webhook|discordapp\.com/api|discord\.com/api)\b", re.I)),
    ("Voice/TTS", re.compile(r"\b(pyaudio|elevenlabs|openai\.audio|coqui|piper_tts|say\s+\"|RealtimeAPI|gpt-4o-realtime)\b", re.I)),
    ("Reminders", re.compile(r"\b(EKReminder|EventKit|osascript.*Reminders|tell application \"Reminders\")\b", re.I)),
    ("Calendar",  re.compile(r"\b(GoogleCalendar|googleapiclient.*calendar|CalDAV|icalendar|EKEvent|events\(\)\.insert)\b", re.I)),
    ("Notion",    re.compile(r"\b(notion_client|api\.notion\.com|NOTION_TOKEN)\b", re.I)),
    ("Obsidian",  re.compile(r"\b(obsidian|VAULT_PATH|\.md['\"]?\s*[,)])", re.I)),
    ("Qdrant",    re.compile(r"\b(qdrant_client|qdrant\.upsert|QdrantClient)\b", re.I)),
    ("SQLite",    re.compile(r"\b(sqlite3|\.db['\"]?|better-sqlite3|SQLITE)\b", re.I)),
    ("Postgres",  re.compile(r"\b(psycopg|postgres|pg_connect)\b", re.I)),
    ("Webhook",   re.compile(r"\b(webhook_url|requests\.post.*webhook)\b", re.I)),
    ("Gmail API", re.compile(r"\b(gmail.*\.users\(\)|gmail_client|GMAIL_TOKEN|googleapiclient.*gmail)\b", re.I)),
    ("Apple Health", re.compile(r"\b(HealthKit|HKHealthStore|apple.health)\b", re.I)),
    ("LLM (cloud)", re.compile(r"\b(anthropic|openai\.ChatCompletion|openai\.completions|claude-3|claude-haiku|gpt-4)\b", re.I)),
    ("LLM (local)", re.compile(r"\b(ollama|deepseek|llama3|localhost:11434)\b", re.I)),
    ("Git",       re.compile(r"\bgit (push|commit|pull|fetch)\b", re.I)),
    ("Phone call", re.compile(r"\b(twilio.*\.calls\.create|client\.calls\.create|VoiceResponse)\b", re.I)),
]


def run(cmd: list[str], **kw) -> str:
    """Run a command, return stdout. Never raises — returns '' on failure."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, **kw
        )
        return out.stdout
    except Exception:
        return ""


def get_launchctl_state() -> dict[str, dict[str, Any]]:
    """
    Parse `launchctl list` output. Returns {label: {pid, last_exit_code}}.
    Format: PID\tStatus\tLabel
    """
    state: dict[str, dict[str, Any]] = {}
    out = run(["launchctl", "list"])
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_str, status_str, label = parts[0], parts[1], parts[2]
        pid = int(pid_str) if pid_str.isdigit() else None
        try:
            last_exit = int(status_str)
        except ValueError:
            last_exit = None
        state[label] = {"pid": pid, "last_exit_code": last_exit}
    return state


def parse_plist(path: Path) -> dict[str, Any] | None:
    """Parse a .plist file. Returns None if it can't be read."""
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return None


def classify_schedule(plist: dict[str, Any]) -> dict[str, Any]:
    """
    Determine the schedule type and a human-readable description.
    Returns {type, summary, raw}.
    """
    if plist.get("KeepAlive"):
        ka = plist["KeepAlive"]
        if ka is True:
            return {"type": "keepalive", "summary": "Always-on (auto-restart)", "raw": True}
        return {"type": "keepalive", "summary": "Conditional keepalive", "raw": ka}

    if "StartCalendarInterval" in plist:
        sci = plist["StartCalendarInterval"]
        # Can be a dict or a list of dicts
        intervals = sci if isinstance(sci, list) else [sci]
        summaries = []
        for iv in intervals:
            parts = []
            if "Weekday" in iv:
                wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][iv["Weekday"] % 7]
                parts.append(wd)
            if "Day" in iv:
                parts.append(f"day {iv['Day']}")
            h = iv.get("Hour")
            m = iv.get("Minute")
            if h is not None and m is not None:
                parts.append(f"{h:02d}:{m:02d}")
            elif h is not None:
                parts.append(f"{h:02d}:00")
            elif m is not None:
                parts.append(f":{m:02d} every hour")
            summaries.append(" ".join(parts) if parts else "scheduled")
        return {"type": "calendar", "summary": " · ".join(summaries), "raw": sci}

    if "StartInterval" in plist:
        secs = plist["StartInterval"]
        if secs >= 3600:
            summary = f"every {secs // 3600}h"
        elif secs >= 60:
            summary = f"every {secs // 60}m"
        else:
            summary = f"every {secs}s"
        return {"type": "interval", "summary": summary, "raw": secs}

    if plist.get("RunAtLoad"):
        return {"type": "ondemand", "summary": "Runs at load only", "raw": True}

    return {"type": "ondemand", "summary": "On-demand / triggered", "raw": None}


def status_for(state: dict[str, Any] | None, schedule: dict[str, Any], disabled: bool) -> str:
    """
    Compute a human-readable status:
      disabled   - .plist.disabled or Disabled key set
      running    - has an active PID
      error      - last exit code != 0 and not currently running
      scheduled  - registered, not running, scheduled to run
      loaded     - registered but not running and on-demand
      missing    - not in launchctl list at all
    """
    if disabled:
        return "disabled"
    if not state:
        return "missing"
    if state.get("pid"):
        return "running"
    exit_code = state.get("last_exit_code")
    if exit_code not in (0, None) and exit_code != 1:
        # Some scheduled jobs report code=1 when waiting; treat only clearly bad codes as error
        if exit_code > 1 or exit_code < 0:
            return "error"
    if schedule["type"] in {"calendar", "interval"}:
        return "scheduled"
    if schedule["type"] == "keepalive":
        # KeepAlive but not running = throttled or crashed
        return "error" if exit_code not in (0, None) else "loaded"
    return "loaded"


# Patterns that indicate a genuine error (vs. routine stderr logging).
# Matched against the stderr tail. We use leading-anchor on most so that
# strings like "no errors" don't trigger.
ERROR_PATTERNS = [
    re.compile(r"\bTraceback\b"),
    re.compile(r"\b(?:Fatal|FATAL|fatal:)\b"),
    re.compile(r"\b(?:CRITICAL|Critical:)\b"),
    re.compile(r"^\s*(?:ERROR|Error)[:\s]", re.M),
    re.compile(r"\b\w*Error:"),                     # SyntaxError:, ValueError:, etc
    re.compile(r"\b\w*Exception\b(?!\s*=)"),         # NullPointerException etc
    re.compile(r"\bpanic:"),                         # Go
    re.compile(r"^\s*\[(?:error|err)\]", re.M | re.I),
    re.compile(r"\bUnhandledPromiseRejection\b"),
    re.compile(r"\bsegmentation fault\b", re.I),
    re.compile(r"\bcommand not found\b"),
    re.compile(r"\bNo such file or directory\b"),
    re.compile(r"\bPermission denied\b"),
    re.compile(r"\bAddress already in use\b"),
    re.compile(r"\bConnection refused\b"),
]


def diagnose_issue(
    *,
    status: str,
    last_exit_code: int | None,
    stderr_errors: list[str],
    stderr_mtime: str | None,
    schedule_type: str,
    disabled: bool,
    entry_script_exists: bool,
) -> dict | None:
    """
    Inspect the service's signals and return a diagnosed issue + recommended fix.
    Returns None if no actionable issue.

    Each issue dict has:
      type:    machine-readable issue id
      message: short human-readable summary (≤ 80 chars ideally)
      detail:  longer prose for tooltip/drawer
      fix:     {action, label, confirm} or None if not auto-fixable
    """
    # Disabled services aren't a problem to flag
    if disabled:
        return None

    err_text = "\n".join(stderr_errors) if stderr_errors else ""
    err_lower = err_text.lower()

    # --- 0. Stale stderr (check FIRST so old errors don't trigger specific
    #         categories that may already be resolved). For KeepAlive
    #         services, "old stderr" means "running fine without errors
    #         since"; for scheduled services, the next run will overwrite.
    if stderr_errors and stderr_mtime:
        try:
            age = dt.datetime.now() - dt.datetime.fromisoformat(stderr_mtime)
            age_h = age.total_seconds() / 3600
            if age_h > 24:
                age_str = (
                    f"{int(age_h / 24)}d ago" if age_h >= 48 else f"{int(age_h)}h ago"
                )
                return {
                    "type": "stale_stderr",
                    "message": f"Old errors in stderr ({age_str}) — likely already fixed",
                    "detail": "Errors are from a previous failed run. The next run will overwrite this; clearing the log silences the warning until then. The running service is not affected.",
                    "fix": {"action": "clear_stderr", "label": "Clear log",
                            "confirm": "Truncates the stderr log file (the error history is lost; service is not affected)."},
                }
        except Exception:
            pass

    # --- 1. Port conflict (most actionable signal first) -------------------
    if "address already in use" in err_lower:
        return {
            "type": "port_conflict",
            "message": "Port conflict — another process is bound to the same port",
            "detail": "Another process is already listening on this service's port. Unloading this service usually fixes it.",
            "fix": {"action": "unload", "label": "Unload service",
                    "confirm": "This will stop the service. The other process keeps running."},
        }

    # --- 2. Entry script missing on disk -----------------------------------
    if entry_script_exists is False or (
        last_exit_code == 127 and "No such file or directory" in err_text and "/bin/bash:" in err_text
    ):
        return {
            "type": "script_missing",
            "message": "Entry script doesn't exist on disk",
            "detail": "The script the plist points to has been deleted or moved. The service can never run as configured.",
            "fix": {"action": "disable", "label": "Disable service",
                    "confirm": "This renames the .plist to .plist.disabled and unloads it."},
        }

    # --- 3. Missing dependency (command not found) -------------------------
    m = re.search(r"([\w-]+): command not found", err_text)
    if m:
        cmd = m.group(1)
        return {
            "type": "missing_command",
            "message": f"Missing command: {cmd}",
            "detail": f"The script tried to run `{cmd}` but it's not on PATH. Install via brew, or check the script's environment.",
            "fix": None,
        }

    # --- 4. Script-path-wrong error from a Python interpreter --------------
    # Pattern: "/.../Python: can't open file '/path/script.py': [Errno 2] No such file"
    # Means the interpreter ran fine but the script path is bad. Usually
    # transient: plist was just edited, but stderr still shows the previous
    # run that used the old path.
    m = re.search(r"can't open file '([^']+)': \[Errno 2\] No such file", err_text)
    if m:
        return {
            "type": "wrong_script_path",
            "message": f"Last run targeted missing script: {m.group(1)}",
            "detail": "Either the plist's ProgramArguments still point at the old path, or the plist has been fixed and this stderr is from before the fix. Check the Command section in this drawer; if it's correct, just clear the log.",
            "fix": {"action": "clear_stderr", "label": "Clear log",
                    "confirm": "Truncates stderr. If the plist was actually updated, the next run will succeed and stderr will stay clean."},
        }

    # --- 5. Missing API key ------------------------------------------------
    m = re.search(r"((?:OPENAI|ANTHROPIC|GOOGLE|XAI|GROQ|TWILIO|TELEGRAM)_[A-Z_]*KEY)", err_text)
    if m and ("must be set" in err_text or "missing" in err_lower or "not set" in err_lower):
        return {
            "type": "missing_api_key",
            "message": f"Missing API key: {m.group(1)}",
            "detail": f"Set {m.group(1)} in ~/.config/ai/.env (or wherever the wrapper sources from), then restart the service.",
            "fix": None,
        }

    # --- 6. Permission denied ----------------------------------------------
    if "Permission denied" in err_text:
        return {
            "type": "permission_denied",
            "message": "Permission denied — likely macOS privacy gate",
            "detail": "Service tried to access something the OS hasn't approved (Reminders, Calendar, Files, Microphone, etc). Open System Settings → Privacy & Security and grant access to /opt/homebrew/bin/python3 or the relevant binary.",
            "fix": None,
        }

    # --- 7. Unhandled traceback / exception --------------------------------
    has_traceback = any("Traceback" in line for line in stderr_errors)

    if has_traceback:
        return {
            "type": "traceback",
            "message": "Unhandled exception in last run",
            "detail": "See the stderr section in this drawer for the traceback. Fix in code, then restart.",
            "fix": {"action": "restart", "label": "Restart service",
                    "confirm": "Unloads then reloads the service. Use after fixing the underlying code."},
        }

    # --- 9. Service registered but not loaded ------------------------------
    if status == "missing":
        return {
            "type": "not_loaded",
            "message": "Plist exists but service isn't loaded",
            "detail": "The .plist file is in ~/Library/LaunchAgents but launchd doesn't know about it.",
            "fix": {"action": "load", "label": "Load service",
                    "confirm": "Registers the service with launchd. It will start running per its schedule."},
        }

    # --- 10. KeepAlive that isn't running ----------------------------------
    if status == "loaded" and last_exit_code not in (0, None):
        return {
            "type": "keepalive_crashed",
            "message": "Always-on service has crashed",
            "detail": "Service is configured KeepAlive but isn't running and last exited with a non-zero code.",
            "fix": {"action": "restart", "label": "Restart service",
                    "confirm": "Unloads then reloads the service."},
        }

    return None


def detect_stderr_errors(stderr_tail: str) -> list[str]:
    """
    Return the (deduped, last-N) lines from stderr that look like real errors.
    Empty list = stderr is just routine logging.
    """
    if not stderr_tail:
        return []
    matches: list[str] = []
    seen: set[str] = set()
    for line in stderr_tail.splitlines():
        for pattern in ERROR_PATTERNS:
            if pattern.search(line):
                key = line.strip()
                if key and key not in seen:
                    seen.add(key)
                    matches.append(line.rstrip())
                break
    # Keep the most-recent 5 unique error lines
    return matches[-5:]


def tail_file(path: Path, n: int = LOG_TAIL_LINES) -> tuple[str, dict[str, Any]]:
    """Return (text, meta) for a log file. meta has size and mtime."""
    if not path or not path.exists():
        return "", {"size": 0, "mtime": None, "exists": False}
    try:
        size = path.stat().st_size
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        # Read last N lines efficiently via tail
        out = run(["tail", "-n", str(n), str(path)])
        return out, {"size": size, "mtime": mtime, "exists": True}
    except Exception:
        return "", {"size": 0, "mtime": None, "exists": False}


def derive_namespace(label: str) -> str:
    """com.justinsmith.foo -> com.justinsmith"""
    parts = label.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:2])
    return parts[0] if parts else "?"


def derive_name(label: str) -> str:
    """com.justinsmith.foo -> foo"""
    parts = label.split(".")
    return parts[-1] if parts else label


def find_entry_script(program_args: list[str]) -> Path | None:
    """
    Identify the 'meaningful' script being run.
    Skip wrappers like /bin/bash, /opt/homebrew/bin/python3, run_job.sh.
    Return the first existing .py / .sh / .ts / .js / executable path that
    isn't a known wrapper.
    """
    wrappers = {
        "/bin/bash", "/bin/sh", "/usr/bin/env",
    }
    interpreter_basenames = {"python3", "python", "node", "bash", "sh", "tsx", "deno"}
    skip_basenames = {"run_job.sh"}  # the openclaw cron wrapper — not meaningful
    candidates: list[Path] = []
    for arg in program_args or []:
        if not isinstance(arg, str) or not arg.startswith("/"):
            continue
        if arg in wrappers:
            continue
        p = Path(arg)
        if p.name in skip_basenames:
            continue
        # Skip plain interpreters
        if p.name in interpreter_basenames and p.suffix == "":
            continue
        if p.exists():
            candidates.append(p)
    # Prefer .py/.sh/.ts/.js over other paths
    preferred = [c for c in candidates if c.suffix in {".py", ".sh", ".ts", ".js", ".mjs"}]
    if preferred:
        return follow_wrapper(preferred[0])
    if candidates:
        return follow_wrapper(candidates[0])
    return None


def follow_wrapper(script: Path, depth: int = 0) -> Path:
    """
    If `script` is a small shell wrapper that ends with `exec <interpreter> <real-script>`,
    return the real script. Up to 2 hops.
    """
    if depth >= 2 or script.suffix not in {".sh", ".bash"}:
        return script
    try:
        if script.stat().st_size > 4096:  # too big to be a pure wrapper
            return script
        text = script.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return script

    # Join shell line-continuations (`\` at end of line) so multi-line exec
    # commands collapse into one logical line.
    joined = re.sub(r"\\\s*\n\s*", " ", text)

    # Parse simple VAR="value" assignments so we can substitute $VAR / ${VAR}
    # in the exec line. Scope: only handles assignments before the first exec.
    env: dict[str, str] = {}
    for m in re.finditer(r'^\s*([A-Z_][A-Z0-9_]*)=["\']?([^"\'\n]+)["\']?\s*$', joined, re.M):
        env[m.group(1)] = m.group(2)

    def expand(s: str) -> str:
        return re.sub(
            r"\$\{?([A-Z_][A-Z0-9_]*)\}?",
            lambda m: env.get(m.group(1), m.group(0)),
            s,
        )

    # Look for the last `exec ...` line — wrappers commonly end there
    exec_lines = re.findall(r'^\s*exec\s+(.+?)$', joined, re.M)
    if not exec_lines:
        return script
    last = expand(exec_lines[-1])
    # Where to resolve relative paths from: prefer the last `cd "..."` target,
    # else the wrapper's parent directory.
    cd_target = None
    cd_matches = re.findall(r'^\s*cd\s+["\']?([^"\'\n]+)["\']?\s*$', joined, re.M)
    if cd_matches:
        cd_target = expand(cd_matches[-1].strip())
    base_dir = Path(cd_target) if cd_target and Path(cd_target).is_dir() else script.parent

    # Pre-substitute common shell idioms that resolve to the script's dir:
    #   $(dirname "$0"), $(dirname "${BASH_SOURCE[0]}"), $SCRIPT_DIR, $DIR
    last = re.sub(r'\$\(dirname\s+"\$\{?(?:0|BASH_SOURCE\[0\])\}?"\)', str(script.parent), last)
    last = re.sub(r"\$\{?(?:SCRIPT_DIR|DIR|HERE)\}?", str(script.parent), last)

    # Extract path-like tokens that exist; prefer ones with .py/.ts/.js extension
    tokens = re.findall(r'(?:"([^"]+)"|\'([^\']+)\'|(\S+))', last)
    paths: list[Path] = []
    SCRIPT_EXTS = {".py", ".ts", ".js", ".mjs", ".sh", ".bash"}
    for tup in tokens:
        for tok in tup:
            if not tok:
                continue
            # Skip plain interpreters and shell flags / unresolved vars
            if tok.startswith("-") or "$" in tok:
                continue
            base = Path(tok).name
            if base in {"python3", "python", "node", "bash", "sh", "tsx", "deno"}:
                continue
            # Try as-is, then relative to base_dir, then wrapper's parent
            for candidate in [
                Path(tok) if tok.startswith("/") else None,
                base_dir / tok,
                script.parent / Path(tok).name,  # last-resort: file in same dir as wrapper
            ]:
                if candidate is None:
                    continue
                try:
                    if candidate.exists() and candidate.is_file():
                        # Only collect if it has a script-like extension OR is the
                        # only candidate (lets us still pick up odd binaries)
                        if candidate.suffix in SCRIPT_EXTS or not paths:
                            paths.append(candidate)
                            break
                except OSError:
                    pass
    if not paths:
        return script
    preferred = [p for p in paths if p.suffix in {".py", ".ts", ".js", ".mjs", ".sh", ".bash"}]
    target = preferred[0] if preferred else paths[0]
    if target == script:
        return script
    return follow_wrapper(target, depth + 1)


def extract_description_from_readme(project_dir: str | None) -> str | None:
    """Fallback: look for README.md in project_dir and grab the first prose paragraph."""
    if not project_dir:
        return None
    for name in ("README.md", "readme.md", "README", "README.txt", "CLAUDE.md", "AGENTS.md"):
        readme = Path(project_dir) / name
        if readme.exists():
            try:
                text = readme.read_text(encoding="utf-8", errors="replace")[:8000]
            except Exception:
                continue
            return _readme_first_prose(text)
    return None


def _readme_first_prose(text: str) -> str | None:
    """Return the first paragraph of actual prose, skipping headings, badges, HTML."""
    # Strip HTML tags but keep their content where useful
    text = re.sub(r"<!--[\s\S]*?-->", "", text)            # HTML comments
    text = re.sub(r"<[^>]+>", "", text)                    # any HTML tags
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text)]
    for p in paragraphs:
        if not p:
            continue
        # Drop image/badge-only paragraphs
        no_images = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", p)
        no_links = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", no_images)
        # Strip leading whitespace + punctuation/heading markers
        cleaned = no_links.strip()
        # Skip headings, list items, code fences, separators
        first_line = cleaned.split("\n", 1)[0].strip()
        if not first_line:
            continue
        if first_line.startswith(("#", ">", "```", "---", "===", "|", "* ", "- ", "1.", "[")):
            continue
        # Skip paragraphs that are mostly punctuation/links
        words = re.findall(r"[A-Za-z]{2,}", cleaned)
        if len(words) < 6:
            continue
        # Take just this paragraph, single-line
        result = re.sub(r"\s+", " ", cleaned).strip()
        if len(result) > 400:
            result = result[:400].rsplit(" ", 1)[0] + "…"
        return result
    return None


def extract_description(script_path: Path | None) -> str | None:
    """
    Pull a 1-3 sentence description from the script:
      - Python: module docstring (first triple-quoted block)
      - Shell: leading `#` comments after the shebang
      - JS/TS: leading `//` or `/* */` comments
    """
    if not script_path or not script_path.exists():
        return None
    try:
        text = script_path.read_text(encoding="utf-8", errors="replace")[:8000]
    except Exception:
        return None

    if script_path.suffix == ".py":
        # Match a module-level docstring (must appear before any code)
        m = re.match(r'\s*(?:#![^\n]*\n)?(?:#[^\n]*\n)*\s*(?:"""|\'\'\')([\s\S]*?)(?:"""|\'\'\')', text)
        if m:
            return _clean_description(m.group(1))
    if script_path.suffix in {".sh", ".bash"}:
        # Header block: contiguous # comments after shebang, before first code line
        lines = text.splitlines()
        out = []
        started = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#!") and not started:
                continue  # skip shebang
            if stripped.startswith("#"):
                started = True
                # Strip leading # and one optional space
                comment = stripped.lstrip("#").lstrip()
                if comment and not re.match(r"^[-=─]{3,}", comment):  # skip separator lines
                    out.append(comment)
            elif started:
                break
            elif stripped == "":
                continue
            else:
                break
        if out:
            return _clean_description("\n".join(out))
    if script_path.suffix in {".ts", ".js", ".mjs"}:
        # Block comment at top
        m = re.match(r"\s*/\*\*?([\s\S]*?)\*/", text)
        if m:
            return _clean_description(re.sub(r"^\s*\*\s?", "", m.group(1), flags=re.M))
        # Or contiguous // comments
        lines = text.splitlines()
        out = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("//"):
                out.append(stripped[2:].lstrip())
            elif stripped == "":
                if out:
                    break
                continue
            else:
                break
        if out:
            return _clean_description("\n".join(out))
    return None


def _clean_description(raw: str) -> str | None:
    """Trim a docstring/comment block to a useful 1-3 sentence summary."""
    raw = raw.strip()
    if not raw:
        return None
    # Drop frontmatter-style lines and license boilerplate
    lines = [
        l for l in raw.splitlines()
        if not re.match(r"^(Copyright|License|SPDX|Author|Version|Usage:|Example:|Maintainer)", l.strip(), re.I)
    ]
    cleaned = "\n".join(lines).strip()
    # Take first paragraph (split on blank line)
    first_para = re.split(r"\n\s*\n", cleaned)[0].strip()
    # Limit length
    if len(first_para) > 400:
        first_para = first_para[:400].rsplit(" ", 1)[0] + "…"
    return first_para or None


def detect_channels(script_path: Path | None) -> list[str]:
    """
    Scan script for evidence of where output is delivered (Email, Telegram, etc.).
    Returns a list of channel labels, ordered by appearance.
    """
    if not script_path or not script_path.exists():
        return []
    try:
        text = script_path.read_text(encoding="utf-8", errors="replace")[:SCRIPT_MAX_BYTES]
    except Exception:
        return []
    found: list[str] = []
    for label, pattern in CHANNEL_PATTERNS:
        if pattern.search(text):
            if label not in found:
                found.append(label)
    return found


def read_script_source(script_path: Path | None) -> dict[str, Any]:
    """Read script source for embedding in dashboard. Returns {path, language, content, truncated}."""
    if not script_path or not script_path.exists():
        return {"path": None, "language": "", "content": "", "truncated": False, "size": 0}
    try:
        size = script_path.stat().st_size
        try:
            text = script_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return {"path": str(script_path), "language": "", "content": "(binary file)", "truncated": False, "size": size}
        truncated = False
        if len(text) > SCRIPT_MAX_BYTES:
            text = text[:SCRIPT_MAX_BYTES]
            truncated = True
        ext_to_lang = {".py": "python", ".sh": "bash", ".bash": "bash",
                       ".ts": "typescript", ".js": "javascript", ".mjs": "javascript"}
        return {
            "path": str(script_path),
            "language": ext_to_lang.get(script_path.suffix, "text"),
            "content": text,
            "truncated": truncated,
            "size": size,
        }
    except Exception:
        return {"path": str(script_path), "language": "", "content": "", "truncated": False, "size": 0}


def project_dir_from_args(program_args: list[str], working_directory: str | None) -> str | None:
    """
    Best-effort: infer the project directory the service is running from.
    Look at the longest path-like argument that exists on disk.
    """
    if working_directory and Path(working_directory).is_dir():
        return working_directory
    candidates = []
    for arg in program_args or []:
        if isinstance(arg, str) and arg.startswith("/"):
            p = Path(arg)
            # Walk up parents to find an existing directory containing the file
            if p.exists():
                if p.is_dir():
                    candidates.append(str(p))
                else:
                    candidates.append(str(p.parent))
    if candidates:
        # Prefer paths under ~/Workspace
        ws = str(HOME / "Workspace")
        ws_paths = [c for c in candidates if c.startswith(ws)]
        if ws_paths:
            return max(ws_paths, key=len)
        return max(candidates, key=len)
    return None


# ── Natural-language explanation generation (Claude Haiku, with cache) ─────


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (does not overwrite)."""
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'").strip('"')
            # Only set if not already populated. Treat empty strings as unset
            # (pre-existing env can have KEY="" which would otherwise block us).
            if key and not os.environ.get(key):
                os.environ[key] = value
    except Exception:
        pass


def load_cache() -> dict[str, Any]:
    if EXPLANATION_CACHE.exists():
        try:
            return json.loads(EXPLANATION_CACHE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache: dict[str, Any]) -> None:
    tmp = EXPLANATION_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(EXPLANATION_CACHE)


def _hash_source(path: str, content: str) -> str:
    h = hashlib.sha256()
    h.update(path.encode("utf-8"))
    h.update(b"\0")
    h.update(content.encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]


def generate_explanation(
    source: dict[str, Any],
    label: str,
    schedule_summary: str,
    cache: dict[str, Any],
) -> str | None:
    """
    Returns a markdown explanation of how the script works. Uses cache keyed
    by SHA-256 of (path + content). Calls Claude Haiku on cache miss.
    Returns None if the source is empty or the API call fails.
    """
    path = source.get("path")
    content = source.get("content") or ""
    if not path or not content.strip():
        return None

    key = _hash_source(path, content)
    if key in cache:
        return cache[key].get("explanation")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    print(f"  → Generating explanation for {label}…", file=sys.stderr, flush=True)

    # Truncate to keep token usage sane
    truncated = content[:EXPLANATION_SOURCE_MAX]
    truncated_note = (
        f"\n\n[NOTE: source truncated to first {EXPLANATION_SOURCE_MAX} bytes of "
        f"{len(content)}]" if len(content) > EXPLANATION_SOURCE_MAX else ""
    )

    prompt = f"""You are explaining a script to a developer browsing a services dashboard.

The script runs as a macOS launchd service named **{label}**.
Schedule: {schedule_summary or "—"}.
Path: `{path}`
Language: {source.get("language", "?")}

Write a short, concrete explanation in markdown using exactly these sections:

## Purpose
One sentence: what this script accomplishes.

## How it works
3–6 short bullets. Reference specific function names, key APIs, files, env vars from the code. Skip generic statements ("imports modules", "defines functions") — describe the actual flow.

## Inputs
What it reads or receives (env vars, files, API endpoints, command-line args).

## Outputs
What it produces and where it sends it (files, databases, emails, APIs, stdout).

## External dependencies
Bulleted list of services, APIs, or libraries it depends on.

Keep the entire response under 350 words. Be specific to this code, not generic.

```{source.get("language", "")}
{truncated}{truncated_note}
```"""

    try:
        import anthropic  # noqa: WPS433
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=EXPLANATION_MODEL,
            max_tokens=EXPLANATION_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in msg.content if hasattr(block, "text"))
        cache[key] = {
            "path": path,
            "explanation": text,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "model": EXPLANATION_MODEL,
        }
        return text
    except Exception as e:  # noqa: BLE001
        print(f"  ! Claude call failed for {label}: {e}", file=sys.stderr)
        return None


def scan() -> dict[str, Any]:
    load_env_file(ENV_FILE)
    cache = load_cache()
    cache_size_before = len(cache)
    plists = sorted(LAUNCH_AGENTS_DIR.glob("*.plist*"))  # include .disabled
    state = get_launchctl_state()
    services = []

    for path in plists:
        # Skip backup files
        if any(suffix in path.name for suffix in (".bak.", ".superseded.")):
            continue
        disabled = path.suffix == ".disabled" or ".disabled" in path.name

        plist = parse_plist(path) if not disabled else parse_plist(path)
        if not plist:
            continue

        label = plist.get("Label") or path.stem.replace(".plist", "")
        namespace = derive_namespace(label)
        name = derive_name(label)

        program_args = plist.get("ProgramArguments") or (
            [plist["Program"]] if "Program" in plist else []
        )
        working_directory = plist.get("WorkingDirectory")
        project_dir = project_dir_from_args(program_args, working_directory)

        entry_script = find_entry_script(program_args)
        description = extract_description(entry_script)
        if not description:
            description = extract_description_from_readme(project_dir)
        channels = detect_channels(entry_script)
        source = read_script_source(entry_script)
        explanation = generate_explanation(
            source, label, classify_schedule(plist).get("summary", ""), cache,
        )

        schedule = classify_schedule(plist)

        runtime = state.get(label)
        status = status_for(runtime, schedule, disabled)

        stdout_path = plist.get("StandardOutPath")
        stderr_path = plist.get("StandardErrorPath")
        stdout_tail, stdout_meta = tail_file(Path(stdout_path)) if stdout_path else ("", {"exists": False})
        stderr_tail, stderr_meta = tail_file(Path(stderr_path)) if stderr_path else ("", {"exists": False})

        # Activity heuristic: most recent log mtime
        activity = None
        for meta in (stdout_meta, stderr_meta):
            if meta.get("mtime"):
                if not activity or meta["mtime"] > activity:
                    activity = meta["mtime"]

        services.append({
            "label": label,
            "name": name,
            "namespace": namespace,
            "is_personal": namespace in PERSONAL_NAMESPACES,
            "plist_path": str(path),
            "disabled": disabled,
            "status": status,
            "pid": runtime.get("pid") if runtime else None,
            "last_exit_code": runtime.get("last_exit_code") if runtime else None,
            "schedule": schedule,
            "program_args": program_args,
            "working_directory": working_directory,
            "project_dir": project_dir,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "stdout_meta": stdout_meta,
            "stderr_meta": stderr_meta,
            "last_activity": activity,
            "environment": plist.get("EnvironmentVariables") or {},
            "entry_script": str(entry_script) if entry_script else None,
            "description": description,
            "channels": channels,
            "source": source,
            "stderr_errors": detect_stderr_errors(stderr_tail),
            "explanation": explanation,
            "issue": diagnose_issue(
                status=status,
                last_exit_code=runtime.get("last_exit_code") if runtime else None,
                stderr_errors=detect_stderr_errors(stderr_tail),
                stderr_mtime=stderr_meta.get("mtime"),
                schedule_type=schedule["type"],
                disabled=disabled,
                entry_script_exists=bool(entry_script and Path(entry_script).exists()) if entry_script else None,
            ),
        })

    # Persist newly-generated explanations to disk
    if len(cache) > cache_size_before:
        save_cache(cache)
        print(f"  Cached {len(cache) - cache_size_before} new explanations")

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "host": os.uname().nodename,
        "user": os.environ.get("USER", "?"),
        "launch_agents_dir": str(LAUNCH_AGENTS_DIR),
        "services": services,
    }


# ── HTML rendering ──────────────────────────────────────────────────────────


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Services Dashboard — __HOST__</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161922;
    --panel-2: #1d212c;
    --border: #2a2f3d;
    --text: #e7eaf0;
    --muted: #8a93a6;
    --accent: #6ea8fe;
    --green: #4ade80;
    --amber: #f59e0b;
    --red: #ef4444;
    --gray: #6b7280;
    --blue: #3b82f6;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f6f7fb;
      --panel: #ffffff;
      --panel-2: #f0f2f8;
      --border: #dfe3ec;
      --text: #1a1d24;
      --muted: #6b7280;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
  header { padding: 20px 28px; border-bottom: 1px solid var(--border); display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 12px; }
  header .stats { margin-left: auto; display: flex; gap: 14px; font-size: 12px; color: var(--muted); }
  header .stats b { color: var(--text); font-weight: 600; }
  .toolbar { display: flex; gap: 8px; padding: 14px 28px; border-bottom: 1px solid var(--border); flex-wrap: wrap; align-items: center; background: var(--panel); }
  .toolbar input[type=search] { flex: 1; min-width: 200px; padding: 8px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font: inherit; }
  .toolbar button, .chip {
    padding: 6px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 999px;
    color: var(--text); font: inherit; font-size: 12px; cursor: pointer; user-select: none;
  }
  .toolbar button:hover, .chip:hover { border-color: var(--accent); }
  .chip.active { background: var(--accent); color: #000; border-color: var(--accent); }
  .chip .count { opacity: 0.6; margin-left: 6px; font-variant-numeric: tabular-nums; }
  main { padding: 18px 28px 80px; }
  .group { margin-bottom: 28px; }
  .group h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 10px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px;
    cursor: pointer; transition: transform 0.08s ease, border-color 0.08s ease;
  }
  .card:hover { border-color: var(--accent); }
  .card .top { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
  .card .name { font-weight: 600; font-size: 15px; word-break: break-all; }
  .card .label { color: var(--muted); font-size: 11px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
  }
  .badge::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  .badge.running { color: var(--green); background: rgba(74,222,128,0.12); }
  .badge.scheduled { color: var(--blue); background: rgba(59,130,246,0.12); }
  .badge.loaded { color: var(--gray); background: rgba(107,114,128,0.18); }
  .badge.error { color: var(--red); background: rgba(239,68,68,0.14); }
  .badge.disabled { color: var(--muted); background: rgba(139,147,166,0.14); }
  .badge.missing { color: var(--amber); background: rgba(245,158,11,0.14); }
  .card .desc { margin-top: 10px; font-size: 12.5px; color: var(--text); opacity: 0.85; line-height: 1.45;
                display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .card .meta { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; font-size: 12px; color: var(--muted); }
  .card .meta span { display: inline-flex; align-items: center; gap: 4px; }
  .card .meta b { color: var(--text); font-weight: 500; }
  .channels { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
  .ch {
    display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10.5px; font-weight: 500;
    background: rgba(110,168,254,0.14); color: var(--accent); border: 1px solid rgba(110,168,254,0.25);
  }
  .ch.delivery { background: rgba(74,222,128,0.13); color: var(--green); border-color: rgba(74,222,128,0.25); }
  .activity { font-size: 11px; color: var(--muted); margin-top: 8px; font-variant-numeric: tabular-nums; }
  /* Issue panel on cards */
  .issue {
    margin-top: 10px; padding: 10px 12px;
    background: rgba(239,68,68,0.07); border: 1px solid rgba(239,68,68,0.25);
    border-left: 3px solid var(--red); border-radius: 6px;
    font-size: 12.5px; line-height: 1.4;
  }
  .issue.warn   { background: rgba(245,158,11,0.07); border-color: rgba(245,158,11,0.25); border-left-color: var(--amber); }
  .issue.info   { background: rgba(110,168,254,0.07); border-color: rgba(110,168,254,0.25); border-left-color: var(--accent); }
  .issue .msg   { color: var(--text); font-weight: 500; }
  .issue .det   { color: var(--muted); margin-top: 4px; font-size: 11.5px; }
  .issue .actions { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .fix-btn {
    background: var(--accent); color: #fff; border: 0; padding: 5px 12px; border-radius: 5px;
    font: 600 11.5px/1 inherit; cursor: pointer; letter-spacing: 0.01em;
  }
  .fix-btn:hover { filter: brightness(1.1); }
  .fix-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .fix-btn.danger { background: var(--red); }
  .fix-btn.muted  { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  /* Confirmation modal */
  .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: none; z-index: 100; align-items: center; justify-content: center; }
  .modal-bg.open { display: flex; }
  .modal { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 24px; width: min(440px, 92vw); }
  .modal h3 { margin: 0 0 8px; font-size: 16px; }
  .modal p  { margin: 0 0 16px; color: var(--muted); font-size: 13px; line-height: 1.5; }
  .modal .actions { display: flex; gap: 8px; justify-content: flex-end; }
  /* Toast */
  .toast {
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    background: var(--panel); border: 1px solid var(--border); border-left: 3px solid var(--green);
    padding: 12px 18px; border-radius: 8px; font-size: 13px; z-index: 200;
    box-shadow: 0 4px 16px rgba(0,0,0,0.3); max-width: 90vw;
    opacity: 0; transition: opacity 0.2s ease, transform 0.2s ease;
  }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(-4px); }
  .toast.error { border-left-color: var(--red); }
  /* Server status banner */
  .server-banner {
    background: rgba(245,158,11,0.10); border-bottom: 1px solid rgba(245,158,11,0.3);
    padding: 10px 28px; font-size: 12.5px; color: var(--text);
  }
  .server-banner code { background: var(--panel-2); padding: 2px 6px; border-radius: 3px; }
  .last-active {
    display: inline-flex; align-items: center; gap: 6px;
    margin-top: 10px; padding: 4px 10px; border-radius: 6px;
    font-size: 11.5px; font-variant-numeric: tabular-nums;
    background: var(--panel-2); border: 1px solid var(--border);
  }
  .last-active::before { content: '●'; font-size: 10px; }
  .last-active.fresh::before { color: var(--green); }
  .last-active.recent::before { color: var(--blue); }
  .last-active.stale::before { color: var(--amber); }
  .last-active.cold::before { color: var(--gray); }
  .last-active.never::before { color: var(--muted); opacity: 0.5; }
  /* Explanation (markdown rendered) */
  .explanation { font-size: 13.5px; line-height: 1.55; }
  .explanation h4, .explanation h5 { margin: 16px 0 6px; font-size: 12px; font-weight: 600; color: var(--accent); text-transform: uppercase; letter-spacing: 0.04em; }
  .explanation h4:first-child, .explanation h5:first-child { margin-top: 0; }
  .explanation p { margin: 0 0 10px; }
  .explanation ul { margin: 4px 0 12px; padding-left: 22px; }
  .explanation li { margin-bottom: 4px; }
  .explanation code { background: var(--panel-2); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  .explanation b { color: var(--text); }
  .source-toggle { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 6px 12px; border-radius: 6px; font: inherit; font-size: 12px; cursor: pointer; }
  .source-toggle:hover { border-color: var(--accent); color: var(--text); }
  .explanation-loading { color: var(--muted); font-style: italic; padding: 12px 0; }
  /* Source code view */
  .source {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px;
    font: 11.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    max-height: 480px; overflow: auto; padding: 12px; white-space: pre; tab-size: 4;
  }
  .source .ln { display: inline-block; width: 38px; color: var(--muted); opacity: 0.5; user-select: none; padding-right: 12px; text-align: right; }
  .src-toolbar { display: flex; align-items: center; gap: 8px; margin: 0 0 6px; font-size: 11px; color: var(--muted); }
  .src-toolbar code { background: var(--panel-2); padding: 1px 6px; border-radius: 3px; }
  .src-toolbar a { color: var(--accent); text-decoration: none; cursor: pointer; }
  .src-toolbar a:hover { text-decoration: underline; }
  .src-trunc { background: rgba(245,158,11,0.14); color: var(--amber); padding: 2px 6px; border-radius: 3px; font-size: 10.5px; }
  /* Drawer */
  .drawer-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: none; z-index: 50; }
  .drawer-bg.open { display: block; }
  .drawer {
    position: fixed; top: 0; right: 0; bottom: 0; width: min(720px, 96vw);
    background: var(--panel); border-left: 1px solid var(--border); z-index: 51;
    transform: translateX(100%); transition: transform 0.18s ease; overflow-y: auto;
  }
  .drawer.open { transform: translateX(0); }
  .drawer header { position: sticky; top: 0; background: var(--panel); }
  .drawer h3 { margin: 18px 28px 6px; font-size: 12px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.06em; }
  .drawer .section { padding: 0 28px 14px; }
  .drawer pre {
    background: var(--panel-2); padding: 12px; border-radius: 6px; border: 1px solid var(--border);
    font: 11.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; overflow-x: auto;
    max-height: 360px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  }
  .drawer .kv { display: grid; grid-template-columns: 140px 1fr; gap: 6px 14px; font-size: 13px; }
  .drawer .kv dt { color: var(--muted); }
  .drawer .kv dd { margin: 0; word-break: break-all; }
  .close { background: transparent; border: 0; color: var(--text); font-size: 24px; cursor: pointer; padding: 0 8px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .empty { padding: 40px 0; text-align: center; color: var(--muted); }
  .ago { font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<div class="server-banner" id="server-banner" style="display:none">
  ⚠️ Auto-fix is disabled — open this page from <code>http://127.0.0.1:8765/</code> to enable fix buttons.
  Start the server with: <code>python3 ~/Workspace/scripts/services-dashboard/server.py</code>
</div>

<header>
  <h1>Services Dashboard</h1>
  <span class="meta" id="generated"></span>
  <div class="stats" id="stats"></div>
</header>

<div class="toolbar">
  <input type="search" id="search" placeholder="Search by name, label, project path…" autofocus>
  <span class="chip active" data-status="all">All</span>
  <span class="chip" data-status="running">Running</span>
  <span class="chip" data-status="scheduled">Scheduled</span>
  <span class="chip" data-status="error">Error</span>
  <span class="chip" data-status="disabled">Disabled</span>
  <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
  <span class="chip active" data-scope="personal">Personal</span>
  <span class="chip" data-scope="all">+ System</span>
</div>

<main id="main"></main>

<div class="modal-bg" id="modal-bg">
  <div class="modal">
    <h3 id="modal-title">Confirm fix</h3>
    <p id="modal-body"></p>
    <div class="actions">
      <button class="fix-btn muted" id="modal-cancel">Cancel</button>
      <button class="fix-btn" id="modal-confirm">Apply</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<div class="drawer-bg" id="drawer-bg"></div>
<aside class="drawer" id="drawer">
  <header>
    <h1 id="drawer-title">—</h1>
    <button class="close" id="drawer-close">×</button>
  </header>
  <div id="drawer-body"></div>
</aside>

<script>
const DATA = __DATA__;

const STATE = {
  search: '',
  status: 'all',
  scope: 'personal',
};

function timeAgo(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  const s = Math.floor(diff / 1000);
  if (s < 60) return s + 's ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 48) return h + 'h ago';
  const d = Math.floor(h / 24);
  return d + 'd ago';
}

function freshnessClass(iso) {
  if (!iso) return 'never';
  const hoursAgo = (Date.now() - new Date(iso).getTime()) / (1000 * 3600);
  if (hoursAgo < 1) return 'fresh';
  if (hoursAgo < 24) return 'recent';
  if (hoursAgo < 24 * 7) return 'stale';
  return 'cold';
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

// Tiny markdown renderer — handles headings, bullets, paragraphs, **bold**, *em*, `code`.
function renderMd(md) {
  if (!md) return '';
  const blocks = md.split(/\n\s*\n/);
  return blocks.map(block => {
    block = block.trim();
    if (!block) return '';
    // Heading
    let m = block.match(/^(#{1,6})\s+(.+)$/);
    if (m) {
      const level = Math.min(6, Math.max(4, m[1].length + 3));
      return `<h${level}>${inlineMd(m[2])}</h${level}>`;
    }
    // Bulleted list
    if (/^[-*]\s+/.test(block)) {
      const items = block.split(/\n/).filter(l => /^[-*]\s+/.test(l.trim()))
        .map(l => `<li>${inlineMd(l.replace(/^[-*]\s+/, ''))}</li>`).join('');
      return `<ul>${items}</ul>`;
    }
    return `<p>${inlineMd(block.replace(/\n/g, ' '))}</p>`;
  }).join('');
}

function inlineMd(s) {
  s = escapeHtml(s);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  s = s.replace(/(^|[\s(])\*([^*]+)\*/g, '$1<i>$2</i>');
  return s;
}

function fmtBytes(n) {
  if (!n) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  return (n/1024/1024).toFixed(1) + ' MB';
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderHeader() {
  document.getElementById('generated').textContent =
    `Generated ${timeAgo(DATA.generated_at)} · ${DATA.host} · ${DATA.services.length} services`;

  const counts = { running: 0, scheduled: 0, error: 0, loaded: 0, disabled: 0, missing: 0 };
  for (const s of DATA.services) counts[s.status] = (counts[s.status] || 0) + 1;
  document.getElementById('stats').innerHTML = `
    <span><b>${counts.running}</b> running</span>
    <span><b>${counts.scheduled}</b> scheduled</span>
    <span><b>${counts.error || 0}</b> errors</span>
    <span><b>${counts.disabled}</b> disabled</span>
  `;

  // Update chip counts
  for (const chip of document.querySelectorAll('.chip[data-status]')) {
    const status = chip.dataset.status;
    const count = status === 'all' ? DATA.services.length : (counts[status] || 0);
    if (!chip.querySelector('.count')) {
      chip.insertAdjacentHTML('beforeend', `<span class="count">${count}</span>`);
    }
  }
}

function filtered() {
  const q = STATE.search.trim().toLowerCase();
  return DATA.services.filter(s => {
    if (STATE.scope === 'personal' && !s.is_personal) return false;
    if (STATE.status !== 'all' && s.status !== STATE.status) return false;
    if (q) {
      const hay = (s.label + ' ' + s.name + ' ' + (s.project_dir || '') + ' ' + (s.program_args || []).join(' ')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function render() {
  const main = document.getElementById('main');
  const items = filtered();
  if (!items.length) {
    main.innerHTML = `<div class="empty">No services match.</div>`;
    return;
  }

  // Group by namespace
  const byNs = {};
  for (const s of items) (byNs[s.namespace] ||= []).push(s);
  const order = Object.keys(byNs).sort((a, b) => byNs[b].length - byNs[a].length);

  main.innerHTML = order.map(ns => `
    <section class="group">
      <h2>${escapeHtml(ns)} <span style="opacity:0.5">· ${byNs[ns].length}</span></h2>
      <div class="grid">
        ${byNs[ns].map(card).join('')}
      </div>
    </section>
  `).join('');

  for (const el of main.querySelectorAll('.card')) {
    // Click anywhere except a fix button opens the drawer
    el.addEventListener('click', (e) => {
      if (e.target.closest('.fix-btn')) return;
      openDrawer(el.dataset.label);
    });
  }

  // Wire fix buttons
  for (const btn of main.querySelectorAll('.fix-btn[data-fix]')) {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      requestFix(btn);
    });
  }

  // Disable fix buttons if server isn't reachable
  if (!SERVER_AVAILABLE) {
    for (const btn of main.querySelectorAll('.fix-btn[data-fix]')) {
      btn.disabled = true;
      btn.title = 'Auto-fix server not reachable. See banner at top of page.';
    }
  }
}

// ── Server detection + fix request flow ─────────────────────────────────

let SERVER_AVAILABLE = false;
async function checkServer() {
  if (window.location.protocol !== 'http:') return false;
  try {
    const r = await fetch('/api/health', { method: 'GET' });
    if (!r.ok) return false;
    const j = await r.json();
    return !!j.ok;
  } catch (e) { return false; }
}

function showToast(msg, isError = false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 4000);
}

let _pendingFix = null;
function requestFix(btn) {
  const action = btn.dataset.fix;
  const label = btn.dataset.label;
  const fixLabel = btn.dataset.fixLabel;
  const confirm = btn.dataset.fixConfirm;
  _pendingFix = { action, label, fixLabel, btn };
  document.getElementById('modal-title').textContent = `${fixLabel}: ${label}`;
  document.getElementById('modal-body').textContent = confirm || 'Are you sure you want to apply this fix?';
  document.getElementById('modal-bg').classList.add('open');
}

document.getElementById('modal-cancel').addEventListener('click', () => {
  document.getElementById('modal-bg').classList.remove('open');
  _pendingFix = null;
});
document.getElementById('modal-confirm').addEventListener('click', async () => {
  if (!_pendingFix) return;
  const { action, label, btn } = _pendingFix;
  document.getElementById('modal-bg').classList.remove('open');
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const r = await fetch('/api/fix', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, label }),
    });
    const j = await r.json();
    if (j.ok) {
      showToast('✓ ' + (j.message || 'Done'));
      // Pull fresh data and re-render
      await loadFreshData();
    } else {
      showToast('✗ ' + (j.message || 'Fix failed'), true);
      btn.disabled = false;
      btn.textContent = _pendingFix.fixLabel;
    }
  } catch (e) {
    showToast('✗ ' + e.message, true);
    btn.disabled = false;
    btn.textContent = _pendingFix.fixLabel;
  }
  _pendingFix = null;
});

async function loadFreshData() {
  if (!SERVER_AVAILABLE) return;
  try {
    const r = await fetch('/api/data');
    if (r.ok) {
      const fresh = await r.json();
      DATA.services = fresh.services;
      DATA.generated_at = fresh.generated_at;
      renderHeader();
      render();
    }
  } catch (e) { /* ignore */ }
}

// Channels considered "delivery" (where output is actually sent) vs storage/runtime.
const DELIVERY_CHANNELS = new Set([
  'Email', 'Telegram', 'Slack', 'Discord', 'Twilio/SMS', 'Phone call',
  'Voice/TTS', 'Reminders', 'Calendar', 'Notion', 'Obsidian', 'Webhook'
]);

function channelChips(channels) {
  if (!channels || !channels.length) return '';
  return `<div class="channels">` + channels.slice(0, 6).map(c =>
    `<span class="ch ${DELIVERY_CHANNELS.has(c) ? 'delivery' : ''}">${escapeHtml(c)}</span>`
  ).join('') + `</div>`;
}

// Issue types we treat as warnings vs hard errors (visual only)
const WARN_ISSUES = new Set(['stale_stderr', 'missing_command', 'permission_denied']);
const INFO_ISSUES = new Set(['not_loaded']);

function issueBlock(s) {
  if (!s.issue) return '';
  const issue = s.issue;
  const severity = WARN_ISSUES.has(issue.type) ? 'warn'
                 : INFO_ISSUES.has(issue.type) ? 'info' : '';
  const fix = issue.fix;
  return `
    <div class="issue ${severity}">
      <div class="msg">${escapeHtml(issue.message)}</div>
      ${issue.detail ? `<div class="det">${escapeHtml(issue.detail)}</div>` : ''}
      <div class="actions">
        ${fix ? `<button class="fix-btn ${fix.action === 'disable' ? 'danger' : ''}"
                       data-fix="${escapeHtml(fix.action)}"
                       data-label="${escapeHtml(s.label)}"
                       data-fix-label="${escapeHtml(fix.label)}"
                       data-fix-confirm="${escapeHtml(fix.confirm || '')}">
                  ${escapeHtml(fix.label)}
                </button>` : ''}
        ${!fix ? '<span style="color:var(--muted);font-size:11.5px;font-style:italic">Manual action required</span>' : ''}
      </div>
    </div>
  `;
}

function card(s) {
  const sched = s.schedule || {};
  const errCount = (s.stderr_errors || []).length;
  const errTip = errCount ? escapeHtml(s.stderr_errors.slice(-3).join('\n')) : '';
  return `
    <div class="card" data-label="${escapeHtml(s.label)}">
      <div class="top">
        <div>
          <div class="name">${escapeHtml(s.name)}</div>
          <div class="label">${escapeHtml(s.label)}</div>
        </div>
        <span class="badge ${s.status}">${s.status}${s.pid ? ' · ' + s.pid : ''}</span>
      </div>
      <div class="desc">${s.description ? escapeHtml(s.description) : '<span style="opacity:0.5">— no description found in script —</span>'}</div>
      ${channelChips(s.channels)}
      ${issueBlock(s)}
      <div class="meta">
        <span>📅 <b>${escapeHtml(sched.summary || '')}</b></span>
        ${errCount && s.status !== 'disabled' && !s.issue ? `<span style="color:var(--red)" title="${errTip}">⚠️ ${errCount} error${errCount>1?'s':''} in stderr</span>` : ''}
      </div>
      <div class="last-active ${freshnessClass(s.last_activity)}" title="${fmtDate(s.last_activity)}">
        Last active: ${timeAgo(s.last_activity)}
      </div>
    </div>
  `;
}

function openDrawer(label) {
  const s = DATA.services.find(x => x.label === label);
  if (!s) return;
  document.getElementById('drawer-title').textContent = s.name;
  const sched = s.schedule || {};
  const src = s.source || {};
  const linesOfSource = src.content ? src.content.split('\n') : [];
  const numbered = linesOfSource.map((ln, i) =>
    `<span class="ln">${i+1}</span>${escapeHtml(ln)}`
  ).join('\n');

  document.getElementById('drawer-body').innerHTML = `
    <div class="section">
      <span class="badge ${s.status}">${s.status}${s.pid ? ' · pid ' + s.pid : ''}</span>
    </div>

    ${s.description ? `
      <h3>What it does</h3>
      <div class="section" style="white-space:pre-wrap;line-height:1.55">${escapeHtml(s.description)}</div>
    ` : ''}

    ${(s.channels && s.channels.length) ? `
      <h3>Detected delivery / dependencies</h3>
      <div class="section">${channelChips(s.channels)}</div>
    ` : ''}

    ${(s.stderr_errors && s.stderr_errors.length) ? `
      <h3 style="color:var(--red)">⚠️ Errors detected in stderr</h3>
      <div class="section">
        <pre style="border-color:rgba(239,68,68,0.4);background:rgba(239,68,68,0.06)">${escapeHtml(s.stderr_errors.join('\n'))}</pre>
      </div>
    ` : ''}

    <h3>Identity</h3>
    <div class="section">
      <dl class="kv">
        <dt>Label</dt><dd><code>${escapeHtml(s.label)}</code></dd>
        <dt>Namespace</dt><dd>${escapeHtml(s.namespace)}</dd>
        <dt>Plist</dt><dd><code>${escapeHtml(s.plist_path)}</code></dd>
        ${s.project_dir ? `<dt>Project dir</dt><dd><code>${escapeHtml(s.project_dir)}</code></dd>` : ''}
        ${s.entry_script ? `<dt>Entry script</dt><dd><code>${escapeHtml(s.entry_script)}</code></dd>` : ''}
      </dl>
    </div>

    <h3>Schedule</h3>
    <div class="section">
      <dl class="kv">
        <dt>Type</dt><dd>${escapeHtml(sched.type || '—')}</dd>
        <dt>Summary</dt><dd>${escapeHtml(sched.summary || '—')}</dd>
        <dt>Last exit code</dt><dd>${s.last_exit_code == null ? '—' : s.last_exit_code}</dd>
      </dl>
    </div>

    <h3>Command</h3>
    <div class="section">
      <pre>${escapeHtml((s.program_args || []).join(' \\\n  '))}</pre>
    </div>

    ${src.path ? `
      <h3>How it works</h3>
      <div class="section">
        ${s.explanation ? `<div class="explanation">${renderMd(s.explanation)}</div>`
          : `<div class="explanation-loading">No explanation generated yet — Claude will produce one on the next refresh (requires ANTHROPIC_API_KEY).</div>`}
      </div>

      <h3>Source</h3>
      <div class="section">
        <div class="src-toolbar">
          <span><code>${escapeHtml(src.path)}</code></span>
          <span><b>${src.language || 'text'}</b> · ${fmtBytes(src.size || 0)}</span>
          ${src.truncated ? `<span class="src-trunc">truncated</span>` : ''}
          <a onclick="navigator.clipboard.writeText(${JSON.stringify(src.content || '')})">copy</a>
          <a onclick="openInEditor(${JSON.stringify(src.path)})">open in editor</a>
          <button class="source-toggle" id="toggle-src">Show source</button>
        </div>
        <div class="source" id="src-block" style="display:none">${numbered || '(empty)'}</div>
      </div>
    ` : ''}

    ${s.stdout_path ? `
      <h3>stdout — <code>${escapeHtml(s.stdout_path)}</code> · ${fmtBytes(s.stdout_meta?.size || 0)} · ${timeAgo(s.stdout_meta?.mtime)}</h3>
      <div class="section">
        <pre>${escapeHtml(s.stdout_tail || '(empty)')}</pre>
      </div>
    ` : ''}

    ${s.stderr_path ? `
      <h3>stderr — <code>${escapeHtml(s.stderr_path)}</code> · ${fmtBytes(s.stderr_meta?.size || 0)} · ${timeAgo(s.stderr_meta?.mtime)}</h3>
      <div class="section">
        <pre>${escapeHtml(s.stderr_tail || '(empty)')}</pre>
      </div>
    ` : ''}

    ${Object.keys(s.environment || {}).length ? `
      <h3>Environment</h3>
      <div class="section">
        <pre>${escapeHtml(Object.entries(s.environment).map(([k,v]) => k + '=' + v).join('\n'))}</pre>
      </div>
    ` : ''}
  `;
  // Wire the "Show source" toggle (re-bound each time the drawer opens)
  const toggleBtn = document.getElementById('toggle-src');
  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      const block = document.getElementById('src-block');
      const showing = block.style.display !== 'none';
      block.style.display = showing ? 'none' : 'block';
      toggleBtn.textContent = showing ? 'Show source' : 'Hide source';
    });
  }

  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-bg').classList.add('open');
}

function openInEditor(path) {
  // Try VS Code first, fall back to default app via file://
  window.open('vscode://file' + path, '_blank');
}

document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.getElementById('drawer-bg').addEventListener('click', closeDrawer);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-bg').classList.remove('open');
}

document.getElementById('search').addEventListener('input', e => {
  STATE.search = e.target.value; render();
});

for (const chip of document.querySelectorAll('.chip[data-status]')) {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip[data-status]').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    STATE.status = chip.dataset.status; render();
  });
}
for (const chip of document.querySelectorAll('.chip[data-scope]')) {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip[data-scope]').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    STATE.scope = chip.dataset.scope; render();
  });
}

(async function boot() {
  SERVER_AVAILABLE = await checkServer();
  if (!SERVER_AVAILABLE) {
    document.getElementById('server-banner').style.display = 'block';
  }
  renderHeader();
  render();
})();
</script>
</body>
</html>
"""


def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, default=str)
    # Defang sequences that would break the embedding <script>...</script> tag.
    # JSON parses "\/" identically to "/", so escaping "</" → "<\/" is safe and
    # prevents browsers from terminating the script block when an embedded
    # source file contains literal "</script>".
    payload = payload.replace("</", "<\\/")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = HTML_TEMPLATE.replace("__HOST__", data.get("host", ""))
    html = html.replace("__DATA__", payload)
    return html


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--open", action="store_true", help="Open dashboard.html after generating")
    p.add_argument("--json-only", action="store_true", help="Print JSON to stdout, don't write HTML")
    args = p.parse_args()

    data = scan()

    if args.json_only:
        print(json.dumps(data, indent=2, default=str))
        return 0

    DASHBOARD_HTML.write_text(render_html(data))
    print(f"Wrote {DASHBOARD_HTML}")
    print(f"Services: {len(data['services'])} "
          f"({sum(1 for s in data['services'] if s['status']=='running')} running, "
          f"{sum(1 for s in data['services'] if s['status']=='scheduled')} scheduled, "
          f"{sum(1 for s in data['services'] if s['status']=='error')} errors)")

    if args.open:
        subprocess.run(["open", str(DASHBOARD_HTML)])

    return 0


if __name__ == "__main__":
    sys.exit(main())
