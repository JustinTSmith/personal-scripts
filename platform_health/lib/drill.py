from __future__ import annotations
"""
Drill state: persist numbered digest items so the OpenClaw agent can look up
full context when Justin replies with a number or "fix N" in Telegram.

State lives at /tmp/openclaw/drill_state.json — ephemeral, refreshed each run.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import List

DRILL_STATE_FILE = Path("/tmp/openclaw/drill_state.json")


def save_drill_state(items: List[dict], run_ts: str) -> None:
    """
    Persist numbered items. Each item is a check result dict + optional fields:
      heal_action: str — human-readable fix description
      heal_cmd:    str — shell command to execute (if auto-fixable)
      drill_detail: str — extended info to show on drill-down
    """
    state = {
        "generated_at": run_ts,
        "items": {str(i + 1): item for i, item in enumerate(items)},
        "count": len(items),
    }
    DRILL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DRILL_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_drill_state() -> dict:
    """Return the most recent drill state dict, or {} if not found."""
    if not DRILL_STATE_FILE.exists():
        return {}
    try:
        return json.loads(DRILL_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def get_item(n: int) -> dict | None:
    """Return drill item N (1-indexed), or None if not found."""
    state = load_drill_state()
    return state.get("items", {}).get(str(n))


def format_drill_detail(item: dict) -> str:
    """Format full drill detail for a single numbered item."""
    label = item.get("label", "Unknown")
    section = item.get("section", "")
    status = item.get("status", "?")
    detail = item.get("detail", "")
    drill_detail = item.get("drill_detail", "")
    heal_action = item.get("heal_action", "")
    heal_cmd = item.get("heal_cmd", "")

    status_map = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}
    emoji = status_map.get(status, "❓")

    lines = [
        f"{emoji} *{section} / {label}*",
        f"`{detail}`" if detail else "",
        "",
    ]

    if drill_detail:
        lines += ["*Details:*", drill_detail, ""]

    if heal_action:
        lines.append(f"💊 *Suggested fix:* {heal_action}")
    if heal_cmd:
        lines.append(f"`{heal_cmd}`")

    return "\n".join(l for l in lines if l is not None)
