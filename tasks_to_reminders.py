#!/usr/bin/env python3
"""
tasks_to_reminders.py
Syncs Life OS task .md files → Apple Reminders via osascript.

Usage:
    python3 tasks_to_reminders.py          # process new tasks
    python3 tasks_to_reminders.py archive  # move processed tasks to weekly archive
"""

import os
import re
import sys
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
VAULT_BASE = Path(
    os.path.expanduser(
        "~/Library/Mobile Documents/com~apple~CloudDocs/Main Vault"
    )
)
TASKS_DIR   = VAULT_BASE / "Life OS" / "Tasks"
ARCHIVE_DIR = TASKS_DIR / "archive"
REMINDERS_LIST = "Life OS"   # Apple Reminders list name

# ── Frontmatter helpers ──────────────────────────────────────────────────────
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

def parse_frontmatter(text: str) -> dict:
    m = FM_RE.match(text)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm

def set_frontmatter_key(text: str, key: str, value: str) -> str:
    """Add or update a single key in frontmatter."""
    m = FM_RE.match(text)
    if not m:
        # no frontmatter yet — prepend one
        return f"---\n{key}: {value}\n---\n\n{text}"
    body = m.group(1)
    # remove existing key if present
    lines = [l for l in body.splitlines() if not l.startswith(f"{key}:")]
    lines.append(f"{key}: {value}")
    new_fm = "\n".join(lines)
    return text[: m.start(1)] + new_fm + text[m.end(1):]

# ── Apple Reminders (via osascript) ─────────────────────────────────────────
def ensure_reminders_list(list_name: str):
    """Create the Reminders list if it doesn't exist."""
    script = f'''
tell application "Reminders"
    if not (exists list "{list_name}") then
        make new list with properties {{name:"{list_name}"}}
    end if
end tell
'''
    subprocess.run(["osascript", "-e", script], capture_output=True)


def create_reminder(title: str, list_name: str, notes: str = "") -> bool:
    """Create a reminder via AppleScript. Returns True on success."""
    # Escape double quotes in title/notes for AppleScript
    safe_title = title.replace('"', '\\"')
    safe_notes = notes.replace('"', '\\"')

    script = f'''
tell application "Reminders"
    tell list "{list_name}"
        make new reminder with properties {{name:"{safe_title}", body:"{safe_notes}"}}
    end tell
end tell
'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ⚠️  osascript error: {result.stderr.strip()}")
        return False
    return True


# ── Archive helper ───────────────────────────────────────────────────────────
def get_week_folder() -> Path:
    """Returns archive/YYYY-WNN path for the current week."""
    today = datetime.today()
    year, week_num, _ = today.isocalendar()
    return ARCHIVE_DIR / f"{year}-W{week_num:02d}"


# ── Main modes ───────────────────────────────────────────────────────────────
def process_new():
    """Find unprocessed task files and create Apple Reminders for them."""
    if not TASKS_DIR.exists():
        print(f"Tasks dir not found: {TASKS_DIR}")
        sys.exit(1)

    ensure_reminders_list(REMINDERS_LIST)

    task_files = sorted(
        f for f in TASKS_DIR.glob("*.md")
        if f.is_file()
    )

    if not task_files:
        print("No task files found.")
        return

    created = 0
    skipped = 0

    for path in task_files:
        text = path.read_text(encoding="utf-8")
        fm   = parse_frontmatter(text)

        # Skip if already synced
        if fm.get("reminder_created", "").lower() == "true":
            skipped += 1
            continue

        title = fm.get("title") or path.stem.replace("-", " ").title()
        notes = f"From: {path.name}\nCategory: {fm.get('category','')}"

        print(f"  Creating reminder: {title!r}")
        ok = create_reminder(title, REMINDERS_LIST, notes)
        if ok:
            # Mark file as synced
            updated = set_frontmatter_key(text, "reminder_created", "true")
            path.write_text(updated, encoding="utf-8")
            created += 1
        else:
            print(f"  ❌ Failed to create reminder for: {path.name}")

    print(f"\nDone. Created {created} reminder(s), skipped {skipped} already-synced.")


def archive_processed():
    """Move files with reminder_created: true into archive/YYYY-WNN/."""
    if not TASKS_DIR.exists():
        print(f"Tasks dir not found: {TASKS_DIR}")
        sys.exit(1)

    week_dir = get_week_folder()

    task_files = sorted(
        f for f in TASKS_DIR.glob("*.md")
        if f.is_file()
    )

    moved = 0
    for path in task_files:
        text = path.read_text(encoding="utf-8")
        fm   = parse_frontmatter(text)
        if fm.get("reminder_created", "").lower() == "true":
            week_dir.mkdir(parents=True, exist_ok=True)
            dest = week_dir / path.name
            shutil.move(str(path), str(dest))
            print(f"  Archived: {path.name} → {week_dir.name}/")
            moved += 1

    print(f"\nArchived {moved} processed task(s).")


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "process"

    if mode == "archive":
        archive_processed()
    else:
        process_new()
