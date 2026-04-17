#!/usr/bin/env python3
"""
twilio_morning_call.py
Twilio outbound voice call — Daily Morning Briefing
Fires at 7:00 AM PT (America/Los_Angeles) via macOS LaunchAgent.

Voice pipeline:
  1. Build briefing text from Obsidian vault
  2. Send to local Qwen3-TTS server (127.0.0.1:8100) → WAV audio
  3. Serve WAV from a temp HTTP server on a random port
  4. Open a Cloudflare quick tunnel → get public HTTPS URL (no account needed)
  5. Call Twilio with TwiML <Play url="..."/> pointing at the tunnel
  6. Poll until call completes, then shut down tunnel + server

Required environment variables:
  TWILIO_ACCOUNT_SID   — ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  TWILIO_AUTH_TOKEN    — from Twilio console dashboard
  TWILIO_PHONE_NUMBER  — your Twilio outbound number, e.g. +1xxxxxxxxxx
  TWILIO_TO_NUMBER     — destination (default: +14388213786)

Optional:
  TWILIO_DRY_RUN=true  — generate + save audio, skip real call
  QWEN_TTS_URL         — override TTS server (default: http://127.0.0.1:8100)
  CLOUDFLARED_BIN      — override cloudflared path (default: auto-detect)
"""

import os
import sys
import re
import time
import signal
import logging
import datetime
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "twilio_morning_call.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── .env loader ───────────────────────────────────────────────────────────────
def _load_dotenv():
    candidates = [
        Path(__file__).parent / ".env",
        Path.home() / ".config" / "ai" / ".env",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val

_load_dotenv()

# ── Dan Martell rewriter ──────────────────────────────────────────────────────
DAN_SYSTEM_PROMPT = """\
You are Dan Martell — SaaS coach, founder of SaaS Academy, author of Buy Back Your Time.
Your job: take Justin's daily briefing data and deliver it as a punchy, direct coaching call.

Rules:
- Speak directly TO Justin in second person ("you", "your").
- Open with one sharp motivating line — no fluff.
- Cover: today's ONE most important focus, key schedule items, any reminders or goals to keep top of mind.
- Skip anything empty or irrelevant (e.g. "None", filler lines).
- Keep it under 200 words total — tight, energetic, actionable.
- Close with one sharp line to send him into the day.
- No bullet points, no headers — flowing speech only. This will be read aloud.
- Do NOT invent facts. Only use what's in the briefing.
"""

def _rewrite_as_dan(raw_briefing: str) -> str:
    """Use GPT-4o-mini to rewrite the briefing in Dan Martell's coaching voice."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        log.warning("No OPENAI_API_KEY — skipping Dan rewrite, using raw briefing.")
        return raw_briefing

    import json as _json
    import urllib.request as _req

    body = _json.dumps({
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": DAN_SYSTEM_PROMPT},
            {"role": "user", "content": f"Here is Justin's briefing data:\n\n{raw_briefing}"},
        ],
        "max_tokens": 400,
        "temperature": 0.7,
    }).encode()

    request = _req.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with _req.urlopen(request, timeout=30) as resp:
            result = _json.loads(resp.read())
            rewritten = result["choices"][0]["message"]["content"].strip()
            log.info("Dan rewrite (%d chars):\n%s", len(rewritten), rewritten)
            return rewritten
    except Exception as e:
        log.warning("Dan rewrite failed: %s — using raw briefing.", e)
        return raw_briefing


# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN", "")
FROM_NUMBER  = os.environ.get("TWILIO_PHONE_NUMBER", "")
TO_NUMBER    = os.environ.get("TWILIO_TO_NUMBER", "+14388213786")
DRY_RUN      = os.environ.get("TWILIO_DRY_RUN", "false").lower() == "true"
QWEN_TTS_URL = os.environ.get("QWEN_TTS_URL", "http://127.0.0.1:8100")
PREGENERATE  = "--pregenerate" in sys.argv or os.environ.get("PREGENERATE", "false").lower() == "true"

VAULT = Path(os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Main Vault/Life OS"
))
AUDIO_CACHE_ROOT = Path(__file__).parent / "audio_cache"

# ── Daily briefing content ────────────────────────────────────────────────────
DAILY_BRIEFING_JOB_ID = "db3baa0c-a0c9-4a8d-805c-7b33764e63a0"
OPENCLAW_RUNS_DIR = Path(os.path.expanduser(
    "~/Workspace/openclaw/cron/runs"
))

def _clean_for_speech(text: str) -> str:
    """Strip Telegram markdown and emoji for clean TTS output."""
    # Remove emoji
    text = re.sub(r'[^\x00-\x7F\u00C0-\u024F\u1E00-\u1EFF]', '', text)
    # Remove markdown bold/italic
    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)
    text = re.sub(r'_+([^_]+)_+', r'\1', text)
    # Remove bullet dashes/dots at line start
    text = re.sub(r'^\s*[•\-]\s*', '', text, flags=re.MULTILINE)
    # Collapse multiple spaces/newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    # Remove lines that are just dashes or empty headers
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l and not re.match(r'^[-=]+$', l)]
    return '\n'.join(lines).strip()


def _section_header_to_spoken(line: str) -> str:
    """Convert section headers like 'Goals' → natural spoken intro."""
    mapping = {
        "Identity":     "Identity check.",
        "Goals":        "Active goals.",
        "This Week":    "This week.",
        "Today":        "Today's focus.",
        "Today's Focus": "Today's focus.",
        "Schedule":     "Today's schedule.",
        "Reminders":    "Reminders.",
        "Urgent Email": "Urgent emails.",
        "FORGE":        "Meals and supplements.",
        "Meals":        "Meals and supplements.",
    }
    for key, spoken in mapping.items():
        if key.lower() in line.lower():
            return spoken
    return line


VAULT_ROOT = Path(os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Main Vault/Life OS"
))

MIRA_GCAL_VENV   = Path("/Users/justinsmith/Workspace/skills/health/mira-sleep-manager/scripts/venv/bin/python3")
MIRA_GCAL_TOKEN  = Path("/Users/justinsmith/Workspace/skills/health/mira-sleep-manager/scripts/gmail_token.json")
OPENCLAW_TASKS_DIR = Path(os.path.expanduser(
    "~/Workspace/openclaw/workspace-operator/log/tasks"
))
OPENCLAW_EXP_DIR = Path(os.path.expanduser(
    "~/Workspace/openclaw/workspace-operator/log/experiments"
))

def _read_apple_reminders(days_ahead: int = 7) -> list[dict]:
    """Return incomplete reminders due within days_ahead via Swift EventKit."""
    swift_code = f"""
import EventKit
import Foundation
let store = EKEventStore()
let sema = DispatchSemaphore(value: 0)
store.requestFullAccessToReminders {{ granted, _ in
    guard granted else {{ sema.signal(); return }}
    let deadline = Calendar.current.date(byAdding: .day, value: {days_ahead}, to: Date())!
    let pred = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: deadline, calendars: nil)
    store.fetchReminders(matching: pred) {{ reminders in
        for r in (reminders ?? []).sorted(by: {{ ($0.dueDateComponents?.date ?? .distantFuture) < ($1.dueDateComponents?.date ?? .distantFuture) }}) {{
            let due = r.dueDateComponents?.date.map {{ ISO8601DateFormatter().string(from: $0) }} ?? "none"
            print("\\(r.title ?? "")\\t\\(due)")
        }}
        sema.signal()
    }}
}}
sema.wait()
"""
    try:
        result = subprocess.run(["swift", "-"], input=swift_code, capture_output=True, text=True, timeout=20)
        reminders = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].strip():
                title, due_str = parts[0].strip(), parts[1].strip()
                due_dt = None
                if due_str != "none":
                    try:
                        due_dt = datetime.datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                reminders.append({"title": title, "due": due_dt})
        return reminders
    except Exception as e:
        log.warning("Could not read Apple Reminders: %s", e)
        return []

def _read_vault_goals() -> str:
    """Extract active goal names and daily execution actions + reminders due today."""
    import re as _re
    f = VAULT_ROOT / "Goals" / "active.md"
    lines = []
    if f.exists():
        text = f.read_text()
        seen_goals = set()
        for line in text.splitlines():
            if line.strip().lower().startswith("goal:"):
                goal = line.split(":", 1)[1].strip()
                if goal and goal not in seen_goals:
                    seen_goals.add(goal)
                    lines.append(f"Active goal: {goal}.")
        in_daily = False
        for line in text.splitlines():
            if "Daily Execution" in line:
                in_daily = True
                continue
            if in_daily:
                if line.startswith("#"):
                    break
                m = _re.match(r'\s*- \[ \] (.+)', line)
                if m:
                    lines.append(f"Daily action: {m.group(1).split('—')[0].strip()}.")

    # Apple Reminders due today
    today = datetime.date.today()
    reminders = _read_apple_reminders(days_ahead=1)
    for r in reminders:
        due = r["due"]
        if due and due.date() <= today:
            lines.append(f"Reminder due today: {r['title']}.")

    return "\n".join(lines)

def _read_vault_tasks() -> str:
    """Extract incomplete tasks from Tasks/this-week.md + Apple Reminders due this week."""
    import re as _re
    tasks = []

    f = VAULT_ROOT / "Tasks" / "this-week.md"
    if f.exists():
        for line in f.read_text().splitlines():
            m = _re.match(r'\s*- \[ \] (.+)', line)
            if m:
                task = _re.sub(r'\s*\([^)]+\)\s*$', '', m.group(1)).strip()
                if task:
                    tasks.append(f"Task: {task}.")

    # Apple Reminders due this week (with due date)
    today = datetime.date.today()
    week_end = today + datetime.timedelta(days=7)
    reminders = _read_apple_reminders(days_ahead=7)
    for r in reminders:
        due = r["due"]
        if due:
            due_date = due.date()
            if today < due_date <= week_end:
                due_label = due_date.strftime("%A %b %-d")
                tasks.append(f"Reminder due {due_label}: {r['title']}.")

    return "\n".join(tasks)

def _read_vault_weekly_intent() -> str:
    """Extract the weekly intent and top 3 outcomes from Planning/weekly.md."""
    f = VAULT_ROOT / "Planning" / "weekly.md"
    if not f.exists():
        return ""
    lines = []
    in_intent = False
    in_top3 = False
    for line in f.read_text().splitlines():
        if line.strip().startswith("## Intent"):
            in_intent = True
            in_top3 = False
            continue
        if line.strip().startswith("## Top 3"):
            in_top3 = True
            in_intent = False
            continue
        if line.startswith("##"):
            in_intent = False
            in_top3 = False
        if in_intent and line.strip():
            lines.append(f"This week's intent: {line.strip()}")
            in_intent = False  # one line only
        if in_top3 and line.strip() and line.strip()[0].isdigit():
            # Strip milestone refs like "(→ ...)"
            import re as _re
            item = _re.sub(r'\s*\(→[^)]+\)', '', line.strip())
            item = _re.sub(r'^\d+\.\s*', '', item).strip()
            if item:
                lines.append(f"Top outcome: {item}.")
    return "\n".join(lines)

def _read_google_calendar_today() -> str:
    """Fetch today's meaningful calendar events via the mira venv's google-api-python-client."""
    if not MIRA_GCAL_VENV.exists() or not MIRA_GCAL_TOKEN.exists():
        log.warning("Mira gcal venv or token not found — skipping calendar.")
        return ""
    script = f"""
import sys, datetime, json
sys.path.insert(0, '/Users/justinsmith/Workspace/skills/health/mira-sleep-manager/scripts')
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

creds = Credentials.from_authorized_user_file('{MIRA_GCAL_TOKEN}', ['https://www.googleapis.com/auth/calendar'])
service = build('calendar', 'v3', credentials=creds)

tz_offset = datetime.timezone(datetime.timedelta(hours=-7))  # PDT
today = datetime.datetime.now(tz_offset).replace(hour=0, minute=0, second=0, microsecond=0)
tomorrow = today + datetime.timedelta(days=1)

# Meal/supplement keywords to filter out of the spoken briefing
NOISE = {{'M1','M2','M3','M4','supplement','Supplement','Pre-Bed','Morning Supplement','Midday Supplement','Evening Supplement','Pea Protein','Fresh Eggs','Bone Broth','Chicken + Carb'}}

events = service.events().list(
    calendarId='primary',
    timeMin=today.isoformat(),
    timeMax=tomorrow.isoformat(),
    maxResults=30, singleEvents=True, orderBy='startTime'
).execute().get('items', [])

out = []
for e in events:
    title = e.get('summary','')
    if any(n in title for n in NOISE): continue
    start = e['start'].get('dateTime', e['start'].get('date',''))
    try:
        dt = datetime.datetime.fromisoformat(start)
        time_str = dt.strftime('%-I:%M %p')
    except Exception:
        time_str = start
    out.append(f"{{time_str}}: {{title}}")
print(json.dumps(out))
"""
    try:
        result = subprocess.run(
            [str(MIRA_GCAL_VENV), "-c", script],
            capture_output=True, text=True, timeout=15
        )
        import json as _json
        events = _json.loads(result.stdout.strip())
        if not events:
            return ""
        lines = ["Today's calendar:"] + [f"  {e}" for e in events]
        return "\n".join(lines)
    except Exception as e:
        log.warning("Could not read Google Calendar: %s", e)
        return ""

def _read_overnight_experiments() -> str:
    """Read task IDs from the morning-briefing run, then fetch real titles + verdicts from task/experiment files."""
    import json as _json, re as _re

    # Find task IDs mentioned in the latest morning-briefing run
    run_file = OPENCLAW_RUNS_DIR / f"{DAILY_BRIEFING_JOB_ID}.jsonl"
    task_ids = []
    if run_file.exists():
        try:
            last_summary = None
            for line in run_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                    if obj.get("status") == "ok" and obj.get("summary"):
                        last_summary = obj["summary"]
                except Exception:
                    continue
            if last_summary:
                task_ids = _re.findall(r'TASK-[\w-]+', last_summary)
                task_ids = list(dict.fromkeys(task_ids))  # dedupe, preserve order
        except Exception as e:
            log.warning("Could not read morning-briefing run file: %s", e)

    if not task_ids:
        return ""

    entries = []
    for tid in task_ids:
        # Real title from task file
        task_file = OPENCLAW_TASKS_DIR / f"{tid}.md"
        title = tid  # fallback
        if task_file.exists():
            for line in task_file.read_text().splitlines():
                m = _re.match(r'^#\s+' + re.escape(tid) + r'\s*[—-]\s*(.+)', line)
                if m:
                    title = m.group(1).strip()
                    break

        # Verdict from reasoning file
        verdict = ""
        reasoning_file = OPENCLAW_EXP_DIR / f"EXP-{tid[5:]}-reasoning.md"
        if reasoning_file.exists():
            text = reasoning_file.read_text()
            m = _re.search(r'##\s+Verdict\s*\n+\*\*([^*]+)\*\*\s*[–-]\s*(.+)', text)
            if m:
                verdict = f"{m.group(1).strip()} — {m.group(2).strip()[:120]}"

        entry = f"{tid}: {title}"
        if verdict:
            entry += f" → {verdict}"
        entries.append(entry)

    return "Overnight experiments:\n" + "\n".join(entries)

def build_briefing_text() -> str:
    """
    Build a comprehensive morning briefing from:
    1. Active goals + daily execution actions (vault)
    2. This week's tasks (vault)
    3. Weekly intent + top 3 outcomes (vault)
    4. Overnight experiment results (OpenClaw morning-briefing job)
    All passed to the Dan Martell rewriter.
    """
    today = datetime.date.today()
    sections = [f"Morning briefing for {today.strftime('%A, %B %-d')}."]

    goals = _read_vault_goals()
    if goals:
        sections.append(goals)
        log.info("Vault goals + reminders loaded.")

    weekly = _read_vault_weekly_intent()
    if weekly:
        sections.append(weekly)
        log.info("Vault weekly intent loaded.")

    calendar = _read_google_calendar_today()
    if calendar:
        sections.append(calendar)
        log.info("Google Calendar loaded.")

    tasks = _read_vault_tasks()
    if tasks:
        sections.append(tasks)
        log.info("Vault tasks + reminders loaded.")

    experiments = _read_overnight_experiments()
    if experiments:
        sections.append(experiments)
        log.info("Overnight experiment results loaded.")

    raw = "\n".join(sections)
    log.info("--- Raw briefing ---\n%s", raw)
    return _rewrite_as_dan(raw)


# ── Polly fallback call (no local TTS server required) ───────────────────────
def _polly_call(spoken_text: str) -> None:
    """
    Place a Twilio call using inline TwiML <Say voice="Polly.Matthew">.
    Used when the local Qwen3 TTS server is unavailable.
    No HTTP server or Cloudflare tunnel needed.
    """
    import xml.sax.saxutils as _sax
    import re as _re

    if not validate_credentials():
        log.error("Polly fallback: invalid Twilio credentials. Aborting.")
        return

    try:
        from twilio.rest import Client
    except ImportError:
        log.error("twilio not installed. Run: pip3 install twilio")
        return

    # Strip to clean spoken text, cap at ~4000 chars (TwiML limit)
    clean = _clean_for_speech(spoken_text)
    clean = _re.sub(r'\s+', ' ', clean).strip()[:4000]
    escaped = _sax.escape(clean)

    twiml = (
        '<Response>'
        '<Say voice="Polly.Matthew" language="en-US">'
        f'{escaped}'
        '</Say>'
        '</Response>'
    )

    client = Client(ACCOUNT_SID, AUTH_TOKEN)
    try:
        call = client.calls.create(twiml=twiml, to=TO_NUMBER, from_=FROM_NUMBER)
        log.info("Polly fallback call placed. SID: %s  Status: %s", call.sid, call.status)
        # Poll briefly so the log shows final status
        for _ in range(90):
            time.sleep(2)
            updated = client.calls(call.sid).fetch()
            log.info("Call status: %s", updated.status)
            if updated.status in ("completed", "failed", "busy", "no-answer", "canceled"):
                log.info("Polly call ended: %s", updated.status)
                break
    except Exception as e:
        log.error("Polly fallback call failed: %s", e)


# ── Qwen TTS ──────────────────────────────────────────────────────────────────
def _tts_chunk(text: str) -> bytes:
    """Call Qwen TTS for a single chunk of text. Returns WAV bytes."""
    import json as _json
    body = _json.dumps({
        "model": "qwen3-local",
        "input": text,
        "voice": "operator",
        "response_format": "wav",
    }).encode()
    req = urllib.request.Request(
        f"{QWEN_TTS_URL}/v1/audio/speech",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def _make_silence(duration_ms: int, sample_rate: int = 22050) -> bytes:
    """Generate a WAV file of pure silence for the given duration."""
    num_samples = int(sample_rate * duration_ms / 1000)
    import struct
    # Minimal WAV header + silence samples (16-bit PCM mono)
    data_size = num_samples * 2
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b'data', data_size)
    return header + b'\x00' * data_size


def _stitch_wavs(wav_bytes_list: list, silence_ms: int = 700) -> bytes:
    """Concatenate WAV audio chunks with silence gaps between them."""
    import subprocess as _sp
    import tempfile as _tf
    import os as _os

    tmp_files = []
    try:
        for i, wav_bytes in enumerate(wav_bytes_list):
            f = _tf.NamedTemporaryFile(suffix=f"_chunk{i}.wav", delete=False)
            f.write(wav_bytes)
            f.close()
            tmp_files.append(f.name)

            if i < len(wav_bytes_list) - 1:
                sf = _tf.NamedTemporaryFile(suffix=f"_silence{i}.wav", delete=False)
                sf.write(_make_silence(silence_ms))
                sf.close()
                tmp_files.append(sf.name)

        # Build ffmpeg concat list
        list_file = _tf.NamedTemporaryFile(mode='w', suffix=".txt", delete=False)
        for f in tmp_files:
            list_file.write(f"file '{f}'\n")
        list_file.close()
        tmp_files.append(list_file.name)

        out = _tf.NamedTemporaryFile(suffix="_stitched.wav", delete=False)
        out.close()
        tmp_files.append(out.name)

        _sp.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file.name, "-ar", "22050", "-ac", "1", out.name],
            check=True, capture_output=True,
        )
        return Path(out.name).read_bytes()
    finally:
        for f in tmp_files[:-1]:  # keep the stitched output until caller reads it
            try: _os.unlink(f)
            except Exception: pass


def generate_audio(text: str, out_path: Path) -> bool:
    """
    Split briefing into sections, synthesise each with Qwen TTS,
    stitch with 700ms silence gaps. Returns True on success.
    """
    log.info("Generating audio via Qwen TTS (%s)...", QWEN_TTS_URL)

    # Split on double-newlines or section headers to get natural pause points
    raw_sections = [s.strip() for s in text.split("\n\n") if s.strip()]
    # Further split long single-line sections on period-newline boundaries
    sections = []
    for s in raw_sections:
        lines = [l.strip() for l in s.splitlines() if l.strip()]
        if len(lines) == 1:
            sections.append(lines[0])
        else:
            # Group header + bullets together, split between groups
            sections.append(" ".join(lines))

    log.info("Synthesising %d sections...", len(sections))
    wav_chunks = []
    for i, section in enumerate(sections):
        if not section:
            continue
        log.info("  [%d/%d] %s...", i + 1, len(sections), section[:60])
        try:
            wav_chunks.append(_tts_chunk(section))
        except Exception as e:
            log.error("TTS chunk %d failed: %s", i, e)
            return False

    if not wav_chunks:
        log.error("No audio chunks generated.")
        return False

    log.info("Stitching %d chunks with 700ms pauses...", len(wav_chunks))
    try:
        stitched = _stitch_wavs(wav_chunks, silence_ms=700)
        out_path.write_bytes(stitched)
        log.info("Audio saved: %s  (%d KB)", out_path, len(stitched) // 1024)
        return True
    except Exception as e:
        log.error("Stitch failed: %s", e)
        return False


# ── Morning questions ─────────────────────────────────────────────────────────
QUESTIONS = [
    q.strip() for q in os.environ.get(
        "MORNING_QUESTIONS",
        "What are you absolutely going to focus on completing today?"
    ).split("|") if q.strip()
]

MORNING_CALLS_DIR = VAULT / "Journal"

# Shared state between webhook handler and main thread
_call_state: dict = {}   # {call_sid: {"answers": [(q, a), ...], "done": bool}}
_audio_dir: Path = None
_public_url: str = None


def _write_morning_answers_to_obsidian(answers: list):
    """Append Q&A answers to today's journal entry (or create one)."""
    if not answers:
        log.info("No answers to write to Obsidian.")
        return
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%I:%M %p").lstrip("0")

    block = f"\n\n## Morning Call — {timestamp}\n\n"
    for q, a in answers:
        block += f"**{q}**\n{a}\n\n"
    block = block.rstrip() + "\n"

    # Find today's note or create one
    MORNING_CALLS_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(MORNING_CALLS_DIR.glob(f"{today}-*.md"))
    if existing:
        note_path = existing[-1]
        with open(note_path, "a") as f:
            f.write(block)
        log.info("Answers appended to %s", note_path)
    else:
        note_path = MORNING_CALLS_DIR / f"{today}-morning-call.md"
        with open(note_path, "w") as f:
            f.write(f"# Morning Call — {now.strftime('%A, %B %-d')}\n{block}")
        log.info("Answers written to new note: %s", note_path)


# ── Webhook HTTP server ────────────────────────────────────────────────────────
class _WebhookHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/start":
            self._handle_start()
        elif path.startswith("/audio/"):
            self._serve_audio(path[len("/audio/"):])
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/start":
            self._handle_start()
        elif path.startswith("/answer/"):
            self._handle_answer(int(path.split("/")[-1]))
        else:
            self.send_response(404); self.end_headers()

    def _parse_body(self):
        import urllib.parse as _up
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        return dict(_up.parse_qsl(body))

    def _serve_twiml(self, xml: str):
        data = xml.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_audio(self, filename: str):
        audio_path = _audio_dir / filename
        if not audio_path.exists():
            log.warning("Audio not found: %s", filename)
            self.send_response(404); self.end_headers()
            return
        data = audio_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_start(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
            f'  <Play>{_public_url}/audio/briefing.wav</Play>\n'
            f'  <Gather input="speech" action="{_public_url}/answer/0" '
            f'speechTimeout="3" language="en-US">\n'
            f'    <Play>{_public_url}/audio/q0.wav</Play>\n'
            f'  </Gather>\n'
            f'  <Redirect>{_public_url}/answer/0</Redirect>\n'
            '</Response>'
        )
        self._serve_twiml(xml)

    def _handle_answer(self, q_index: int):
        params = self._parse_body()
        speech = params.get("SpeechResult", "").strip()
        call_sid = params.get("CallSid", "unknown")

        if call_sid not in _call_state:
            _call_state[call_sid] = {"answers": [], "done": False}
        if speech and q_index < len(QUESTIONS):
            _call_state[call_sid]["answers"].append((QUESTIONS[q_index], speech))
            log.info("Answer [%d] %s → %s", q_index, QUESTIONS[q_index][:40], speech[:80])

        next_q = q_index + 1
        if next_q < len(QUESTIONS):
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
                f'  <Gather input="speech" action="{_public_url}/answer/{next_q}" '
                f'speechTimeout="3" language="en-US">\n'
                f'    <Play>{_public_url}/audio/q{next_q}.wav</Play>\n'
                f'  </Gather>\n'
                f'  <Redirect>{_public_url}/answer/{next_q}</Redirect>\n'
                '</Response>'
            )
        else:
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
                f'  <Play>{_public_url}/audio/outro.wav</Play>\n'
                '</Response>'
            )
            _call_state[call_sid]["done"] = True
            threading.Timer(1.0, lambda: _write_morning_answers_to_obsidian(
                _call_state.get(call_sid, {}).get("answers", [])
            )).start()

        self._serve_twiml(xml)

    def log_message(self, fmt, *args):
        log.info("HTTP: " + fmt, *args)


def _start_http_server():
    """Start the webhook HTTP server on a random port. Returns (server, port)."""
    server = HTTPServer(("127.0.0.1", 0), _WebhookHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Webhook server started on port %d", port)
    return server, port


# ── Cloudflare quick tunnel ───────────────────────────────────────────────────
def _find_cloudflared() -> str:
    override = os.environ.get("CLOUDFLARED_BIN", "")
    if override and Path(override).exists():
        return override
    for candidate in [
        "/opt/homebrew/bin/cloudflared",
        "/usr/local/bin/cloudflared",
        "/usr/bin/cloudflared",
    ]:
        if Path(candidate).exists():
            return candidate
    # Try PATH
    result = subprocess.run(["which", "cloudflared"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def start_tunnel(port: int):
    """
    Start a Cloudflare quick tunnel. Returns (process, public_url).
    No account or login required — uses trycloudflare.com.
    """
    cf = _find_cloudflared()
    if not cf:
        raise RuntimeError("cloudflared not found. Install with: brew install cloudflare/cloudflare/cloudflared")

    log.info("Starting Cloudflare tunnel → localhost:%d ...", port)
    proc = subprocess.Popen(
        [cf, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # cloudflared prints the public URL to stderr
    public_url = None
    deadline = time.time() + 30
    url_pattern = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")

    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"cloudflared exited early: {proc.returncode}")
            time.sleep(0.1)
            continue
        log.debug("cloudflared: %s", line.strip())
        m = url_pattern.search(line)
        if m:
            public_url = m.group(0)
            log.info("Tunnel URL: %s", public_url)
            break

    if not public_url:
        proc.terminate()
        raise RuntimeError("Timed out waiting for Cloudflare tunnel URL")

    return proc, public_url


# ── Credential validation ─────────────────────────────────────────────────────
def validate_credentials() -> bool:
    missing = []
    if not ACCOUNT_SID or not ACCOUNT_SID.startswith("AC"):
        missing.append("TWILIO_ACCOUNT_SID")
    if not AUTH_TOKEN:
        missing.append("TWILIO_AUTH_TOKEN")
    if not FROM_NUMBER or not FROM_NUMBER.startswith("+"):
        missing.append("TWILIO_PHONE_NUMBER")
    if missing:
        log.error("Missing Twilio credentials: %s", ", ".join(missing))
        return False
    return True


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _audio_dir, _public_url

    TODAY = datetime.date.today().strftime("%Y-%m-%d")
    _cache_dir = AUDIO_CACHE_ROOT / TODAY
    _using_cache = False

    log.info("=== Twilio Morning Call (two-way) starting ===")
    log.info("DRY_RUN=%s  TO=%s  Questions=%d  PREGENERATE=%s",
             DRY_RUN, TO_NUMBER, len(QUESTIONS), PREGENERATE)

    if PREGENERATE:
        # Pre-generation mode: synthesise all audio and save to dated cache dir.
        # Invoked at 6 AM by LaunchAgent; the 7:10 AM call picks up the cache.
        _cache_dir.mkdir(parents=True, exist_ok=True)
        log.info("Pre-generating audio to %s ...", _cache_dir)
        spoken_text = build_briefing_text()
        log.info("--- Briefing ---\n%s", spoken_text)
        if not generate_audio(spoken_text, _cache_dir / "briefing.wav"):
            log.warning("Qwen TTS unavailable during pre-generation — cache not built. Will use Polly fallback at call time.")
            return
        log.info("Generating question + outro audio...")
        for i, q in enumerate(QUESTIONS):
            (_cache_dir / f"q{i}.wav").write_bytes(_tts_chunk(q))
            log.info("  q%d: %s", i, q[:60])
        outro_text = "Perfect. I've noted that. Now go make it happen. Make it count today."
        (_cache_dir / "outro.wav").write_bytes(_tts_chunk(outro_text))
        log.info("Pre-generation complete. Audio cached in %s", _cache_dir)
        return

    spoken_text = None

    # Check for pre-generated audio cache (built by 6 AM LaunchAgent)
    if _cache_dir.exists() and (_cache_dir / "briefing.wav").exists():
        log.info("Using pre-generated audio from %s", _cache_dir)
        _audio_dir = _cache_dir
        _using_cache = True
    else:
        # Live TTS generation fallback
        spoken_text = build_briefing_text()
        log.info("--- Briefing ---\n%s", spoken_text)
        _audio_dir = Path(tempfile.mkdtemp(prefix="morning_call_"))
        log.info("Audio dir: %s", _audio_dir)
        if not generate_audio(spoken_text, _audio_dir / "briefing.wav"):
            log.warning("Qwen TTS unavailable — falling back to Polly <Say> call.")
            if not DRY_RUN:
                _polly_call(spoken_text)
            else:
                log.info("DRY_RUN — would have placed Polly fallback call.")
            return
        log.info("Generating question audio (%d questions)...", len(QUESTIONS))
        for i, q in enumerate(QUESTIONS):
            q_path = _audio_dir / f"q{i}.wav"
            q_path.write_bytes(_tts_chunk(q))
            log.info("  q%d: %s", i, q[:60])
        outro_text = "Perfect. I've noted that. Now go make it happen. Make it count today."
        (_audio_dir / "outro.wav").write_bytes(_tts_chunk(outro_text))

    if DRY_RUN:
        log.info("DRY_RUN — audio in %s. No call placed.", _audio_dir)
        print(f"\n[DRY RUN] Audio dir: {_audio_dir}")
        if spoken_text:
            print(f"[DRY RUN] Briefing:\n{spoken_text}")
        print(f"[DRY RUN] Questions: {QUESTIONS}")
        return

    if not validate_credentials():
        sys.exit(1)

    try:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException
    except ImportError:
        log.error("twilio not installed. Run: pip3 install twilio")
        sys.exit(1)

    http_server = None
    tunnel_proc = None

    try:
        # 3. Start webhook server
        http_server, port = _start_http_server()

        # 4. Open Cloudflare tunnel
        tunnel_proc, public_url = start_tunnel(port)
        _public_url = public_url
        log.info("Webhook URL: %s", _public_url)

        time.sleep(2)

        # 5. Place call pointing to /start webhook
        client = Client(ACCOUNT_SID, AUTH_TOKEN)
        call = client.calls.create(
            url=f"{_public_url}/start",
            to=TO_NUMBER,
            from_=FROM_NUMBER,
        )
        log.info("Call placed. SID: %s  Status: %s", call.sid, call.status)
        print(f"\nCALL SID: {call.sid}\nStatus:   {call.status}\n")

        # 6. Poll until call completes
        log.info("Waiting for call to complete...")
        for _ in range(180):  # up to 6 minutes (longer — two-way takes more time)
            time.sleep(2)
            updated = client.calls(call.sid).fetch()
            log.info("Call status: %s", updated.status)
            if updated.status in ("completed", "failed", "busy", "no-answer", "canceled"):
                log.info("Call ended: %s", updated.status)
                break

        # Give Obsidian writer time to finish
        time.sleep(3)

    except TwilioRestException as e:
        log.error("Twilio API error: %s (code=%s)", e.msg, e.code)
        sys.exit(1)
    except Exception as e:
        log.error("Error: %s", e)
        sys.exit(1)
    finally:
        if http_server:
            http_server.shutdown()
            log.info("HTTP server stopped.")
        if tunnel_proc:
            tunnel_proc.terminate()
            log.info("Cloudflare tunnel closed.")
        if not _using_cache:
            import shutil
            try:
                shutil.rmtree(_audio_dir)
            except Exception:
                pass

    log.info("=== Twilio Morning Call complete ===")


if __name__ == "__main__":
    main()
