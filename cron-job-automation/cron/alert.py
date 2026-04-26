#!/usr/bin/env python3
"""
Telegram alerting for cron jobs → cron-updates group chat.

Config (in priority order):
  1. Env vars: TELEGRAM_BOT_TOKEN, CRON_UPDATES_CHAT_ID
  2. ~/.openclaw/openclaw.json → channels.telegram.{botToken, cronUpdatesChatId}

CLI usage:
  python3 alert.py started    <job-name> <run-id>
  python3 alert.py succeeded  <job-name> <run-id> [summary]
  python3 alert.py failed     <job-name> <run-id> [summary]
  python3 alert.py skipped    <job-name> [reason]
  python3 alert.py health     <line1> [line2 ...]
  python3 alert.py raw        <message>
"""
import json
import os
import sys
from typing import Optional

import requests


# ── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> tuple[Optional[str], Optional[str]]:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("CRON_UPDATES_CHAT_ID")

    if not (bot_token and chat_id):
        cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
        if os.path.exists(cfg_path):
            try:
                cfg = json.loads(open(cfg_path).read())
                tg = cfg.get("channels", {}).get("telegram", {})
                bot_token = bot_token or tg.get("botToken")
                chat_id = chat_id or tg.get("cronUpdatesChatId")
            except Exception:
                pass

    return bot_token, chat_id


# ── Core send ────────────────────────────────────────────────────────────────

def send(message: str, parse_mode: str = "Markdown") -> bool:
    """Send message to the cron-updates Telegram chat. Returns True on success."""
    bot_token, chat_id = _load_config()
    if not bot_token or not chat_id:
        print(f"[alert] Telegram not configured — message dropped: {message[:80]}", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": parse_mode},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[alert] Telegram send failed: {e}", file=sys.stderr)
        return False


# ── Typed helpers ────────────────────────────────────────────────────────────

def job_started(job_name: str, run_id: str) -> None:
    send(f"▶️ *{job_name}* started\n`run:{run_id[:8]}`")


def job_succeeded(job_name: str, run_id: str, duration_sec: Optional[float] = None, summary: str = "") -> None:
    dur = f" in {duration_sec:.1f}s" if duration_sec is not None else ""
    msg = f"✅ *{job_name}* succeeded{dur}"
    if summary:
        msg += f"\n_{summary[:300]}_"
    send(msg)


def job_failed(job_name: str, run_id: str, summary: str = "") -> None:
    msg = f"❌ *{job_name}* failed\n`run:{run_id[:8]}`"
    if summary:
        msg += f"\n_{summary[:300]}_"
    send(msg)


def job_skipped(job_name: str, reason: str = "") -> None:
    msg = f"⏭ *{job_name}* skipped (already ran)"
    if reason:
        msg += f" — {reason}"
    send(msg)


def persistent_failure_alert(job_name: str, count: int, window_hours: float) -> None:
    send(
        f"🚨 *PERSISTENT FAILURE — {job_name}*\n"
        f"Failed {count}+ times within {window_hours:.0f}h — manual intervention needed"
    )


def health_report(lines: list[str]) -> None:
    send("🏥 *Cron Health Check*\n" + "\n".join(lines))


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "started":
        job_started(sys.argv[2], sys.argv[3])
    elif cmd == "succeeded":
        job_succeeded(sys.argv[2], sys.argv[3], summary=" ".join(sys.argv[4:]))
    elif cmd == "failed":
        job_failed(sys.argv[2], sys.argv[3], summary=" ".join(sys.argv[4:]))
    elif cmd == "skipped":
        job_skipped(sys.argv[2], reason=" ".join(sys.argv[3:]))
    elif cmd == "health":
        health_report(sys.argv[2:])
    elif cmd == "raw":
        send(" ".join(sys.argv[2:]))
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
