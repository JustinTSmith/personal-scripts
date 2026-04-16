"""
CRM Follow-up Nudger
====================
Queries the personal-crm SQLite DB for keep contacts who haven't been
contacted in a while, then:
  1. Sends Justin a summary iMessage with who's overdue
  2. Opens a pre-drafted iMessage to each overdue contact (if phone number is known)

Config:
  contacts.json  — maps email -> phone number for iMessage drafts
  THRESHOLD_DAYS — contacts not seen in this many days are surfaced

Run:
  python3 followup.py [--dry-run]
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH         = Path.home() / "Workspace/projects/personal-crm/data/crm.db"
CONTACTS_JSON   = Path(__file__).parent / "contacts.json"
MY_NUMBER       = "+14388213786"
THRESHOLD_DAYS  = 30   # nudge if no interaction in this many days
DRY_RUN         = "--dry-run" in sys.argv

# ── iMessage helpers ──────────────────────────────────────────────────────────

def send_imessage(to: str, body: str):
    """Send an iMessage to a phone number or email."""
    if DRY_RUN:
        print(f"[dry-run] iMessage to {to}:\n{body}\n")
        return
    escaped = body.replace('"', '\\"').replace("\\n", "\\n")
    script = f'''
    tell application "Messages"
        send "{escaped}" to buddy "{to}" of (first service whose service type = iMessage)
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[warn] iMessage to {to} failed: {result.stderr.strip()}")


def open_imessage_draft(to: str, body: str):
    """Send Justin a draft message he can forward, then open Messages to the contact."""
    if DRY_RUN:
        print(f"[dry-run] Draft to {to}:\n{body}\n")
        return
    # Send Justin the draft text as a separate iMessage so he can copy and paste it
    draft_prompt = f"Draft for {to}:\n\n{body}\n\n(copy and paste to send)"
    send_imessage(MY_NUMBER, draft_prompt)
    time.sleep(0.5)


# ── CRM query ─────────────────────────────────────────────────────────────────

def get_overdue_contacts(threshold_days: int) -> list[dict]:
    """Return keep contacts not seen in threshold_days or more."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT email, name,
                   datetime(last_seen_at/1000, 'unixepoch') as last_seen,
                   CAST((unixepoch() - last_seen_at/1000) / 86400 AS INTEGER) as days_ago
            FROM contacts
            WHERE classification = 'keep'
              AND CAST((unixepoch() - last_seen_at/1000) / 86400 AS INTEGER) >= ?
            ORDER BY days_ago DESC
        """, (threshold_days,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_phone_map() -> dict[str, str]:
    if CONTACTS_JSON.exists():
        return json.loads(CONTACTS_JSON.read_text())
    return {}


# ── Draft templates ───────────────────────────────────────────────────────────

def draft_message(contact: dict) -> str:
    name = contact["name"].split()[0] if contact["name"] else "Hey"
    days = contact["days_ago"]
    if days > 180:
        return f"Hey {name}, it's been a while - hope you're doing well. What's new with you?"
    elif days > 90:
        return f"Hey {name}, been meaning to catch up. How are things going?"
    else:
        return f"Hey {name}, just thinking of you - hope all's good!"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    overdue = get_overdue_contacts(THRESHOLD_DAYS)

    if not overdue:
        print("No overdue contacts. Nothing to do.")
        return

    phone_map = load_phone_map()

    # Build summary message to Justin
    today = datetime.now().strftime("%a %b %-d")
    lines = [f"CRM follow-ups ({today}):"]
    for c in overdue:
        name = c["name"] or c["email"]
        lines.append(f"  - {name} — {c['days_ago']}d ago")
    lines.append("\nDraft messages follow...")
    summary = "\n".join(lines)

    send_imessage(MY_NUMBER, summary)
    time.sleep(1)

    # Open a draft in Messages for each contact with a known phone number
    for c in overdue:
        phone = phone_map.get(c["email"])
        if not phone or phone == "+1":
            print(f"[skip] No phone for {c['email']}")
            continue
        draft = draft_message(c)
        open_imessage_draft(phone, draft)

    print(f"Done. {len(overdue)} overdue contact(s) processed.")


if __name__ == "__main__":
    main()
