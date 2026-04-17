from __future__ import annotations
"""
Drill state persistence for Security Council.
Separate state file from platform_health.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import List

from ..config import DRILL_STATE_FILE


def save_drill_state(items: List[dict], run_ts: str) -> None:
    state = {
        "generated_at": run_ts,
        "items": {str(i + 1): item for i, item in enumerate(items)},
        "count": len(items),
    }
    DRILL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DRILL_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_drill_state() -> dict:
    if not DRILL_STATE_FILE.exists():
        return {}
    try:
        return json.loads(DRILL_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def get_item(n: int) -> dict | None:
    state = load_drill_state()
    return state.get("items", {}).get(str(n))


def format_drill_detail(item: dict) -> str:
    label = item.get("label", "Unknown")
    section = item.get("section", "")
    status = item.get("status", "?")
    severity = item.get("severity", "")
    detail = item.get("detail", "")
    drill_detail = item.get("drill_detail", "")
    heal_action = item.get("heal_action", "")
    heal_cmd = item.get("heal_cmd", "")

    status_map = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}
    sev_map = {"critical": "🚨", "high": "⚠️", "medium": "📋", "low": "💡"}
    emoji = sev_map.get(severity, status_map.get(status, "❓"))

    lines = [
        f"{emoji} *{section} / {label}*",
        f"Severity: {severity.upper()}" if severity else "",
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
