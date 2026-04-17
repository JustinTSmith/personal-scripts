from __future__ import annotations
"""
Report formatting: builds the numbered digest Telegram message from check results.

Format:
  🔴 Platform Health — HH:MM

  [1] ❌ LaunchAgents / ob1-cascade-check: stopped (exit 0)  🔧
  [2] ⚠️ Crons / task runs (48h): 24 ok, 5 failed
  [3] ⚠️ Logs / gateway.log: ERROR×16
  ...

  ✅ 21 OK

  Reply N for details • "fix N" to auto-heal 🔧

Each result dict must have at minimum:
  { "status": "ok"|"warn"|"fail"|"skip", "section": str, "label": str, "detail": str }

Optional enrichment keys (added by heal.py):
  { "heal_action": str, "heal_cmd": str, "drill_detail": str }
"""
from datetime import datetime
from typing import List, Dict, Tuple

STATUS_EMOJI = {
    "ok":   "✅",
    "warn": "⚠️",
    "fail": "❌",
    "skip": "⏭️",
}

VERDICT_EMOJI = {
    "healthy":  "💚",
    "degraded": "🟡",
    "critical": "🔴",
}


def _overall_verdict(results: List[dict]) -> str:
    statuses = [r.get("status", "skip") for r in results]
    if any(s == "fail" for s in statuses):
        return "critical"
    if any(s == "warn" for s in statuses):
        return "degraded"
    return "healthy"


def build_digest(
    results: List[dict],
    *,
    title: str = "Platform Health",
    timestamp: datetime | None = None,
) -> Tuple[str, List[dict]]:
    """
    Build a numbered digest Telegram message.
    Returns (message_text, numbered_items) where numbered_items is the
    ordered list of warn/fail results (1-indexed) saved to drill_state.
    """
    if timestamp is None:
        timestamp = datetime.now()

    ts_str = timestamp.strftime("%Y-%m-%d %H:%M")
    verdict = _overall_verdict(results)
    verdict_emoji = VERDICT_EMOJI[verdict]

    # Separate actionable (warn/fail) from OK
    actionable = [r for r in results if r.get("status") in ("warn", "fail")]
    ok_results = [r for r in results if r.get("status") == "ok"]
    skip_results = [r for r in results if r.get("status") == "skip"]

    # Sort actionable: fail first, then warn; within each group keep original order
    actionable_sorted = sorted(actionable, key=lambda r: (0 if r.get("status") == "fail" else 1))

    lines = [
        f"{verdict_emoji} *{title}* — {ts_str}",
        "",
    ]

    if not actionable_sorted:
        lines.append("All systems nominal.")
    else:
        for i, r in enumerate(actionable_sorted, start=1):
            emoji = STATUS_EMOJI.get(r.get("status", "skip"), "❓")
            section = r.get("section", "")
            label = r.get("label", "Unknown")
            detail = r.get("detail", "")
            has_heal = bool(r.get("heal_cmd") or r.get("heal_action"))

            # Compact label: omit section prefix if label already contains it
            if section and not label.lower().startswith(section.lower()):
                display_label = f"{section} / {label}"
            else:
                display_label = label

            heal_badge = " 🔧" if has_heal else ""
            detail_str = f": {detail}" if detail else ""
            lines.append(f"[{i}] {emoji} {display_label}{detail_str}{heal_badge}")

    lines.append("")

    # OK summary (single line)
    ok_count = len(ok_results)
    if ok_count:
        lines.append(f"✅ _{ok_count} check{'s' if ok_count != 1 else ''} OK_")

    lines.append("")

    # Footer — only show if there are actionable items
    if actionable_sorted:
        has_any_heal = any(
            r.get("heal_cmd") or r.get("heal_action") for r in actionable_sorted
        )
        if has_any_heal:
            lines.append("_Reply N for details  •  \"fix N\" to auto-heal 🔧_")
        else:
            lines.append("_Reply N for details_")

    return "\n".join(lines), actionable_sorted


def chunk_message(text: str, limit: int = 4000) -> List[str]:
    """Split a long message into chunks at line boundaries, respecting limit."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current: List[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)

    if current:
        chunks.append("".join(current))

    return chunks
