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

EXPLANATION_MODEL = "claude-sonnet-4-6"
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


def sanitize_environment(env: dict[str, Any]) -> dict[str, str]:
    secret_key_markers = (
        "KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "AUTH", "PRIVATE",
        "SESSION", "COOKIE", "CREDENTIAL", "API_",
    )
    redacted: dict[str, str] = {}
    for key, value in (env or {}).items():
        key_str = str(key)
        value_str = str(value)
        upper_key = key_str.upper()
        if any(marker in upper_key for marker in secret_key_markers):
            redacted[key_str] = "[redacted]"
        else:
            redacted[key_str] = value_str
    return redacted


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
    cache: dict[str, Any] | None = None,
    description: str = "",
) -> str | None:
    """
    Returns a markdown explanation of how the script works. Uses cache keyed
    by SHA-256 of (path + content). Calls Claude on cache miss.
    When cache is None, loads from and saves to disk automatically.
    Returns None if the source is empty or the API call fails.
    """
    path = source.get("path")
    content = source.get("content") or ""
    if not path or not content.strip():
        return None

    _own_cache = cache is None
    if _own_cache:
        cache = load_cache()

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

    desc_line = f"\nContext: {description}" if description else ""

    prompt = f"""You are explaining a script to a developer browsing a services dashboard.

The script runs as a macOS launchd service named **{label}**.
Schedule: {schedule_summary or "—"}.
Path: `{path}`
Language: {source.get("language", "?")}{desc_line}

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
        if _own_cache:
            save_cache(cache)
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
            "environment": sanitize_environment(plist.get("EnvironmentVariables") or {}),
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

TEMPLATE_FILE = SCRIPT_DIR / "template.html"


def _load_template() -> str:
    """Read template.html fresh on each render so edits show up without restarting."""
    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(
            f"template.html not found at {TEMPLATE_FILE}. "
            "It must live next to scan.py."
        )
    return TEMPLATE_FILE.read_text(encoding="utf-8")


def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, default=str)
    # Defang sequences that would break the embedding <script>...</script> tag.
    payload = payload.replace("</", "<\\/")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = _load_template()
    html = html.replace("__HOST__", data.get("host", ""))
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
