"""
Security Council report formatting.
Groups findings by severity (critical → high → medium → low).
"""
from datetime import datetime
from typing import List, Dict, Tuple

from ..config import SEVERITY_ORDER

SEVERITY_EMOJI = {
    "critical": "🚨",
    "high": "⚠️",
    "medium": "📋",
    "low": "💡",
}

SEVERITY_HEADER = {
    "critical": "🚨 *CRITICAL*",
    "high": "⚠️ *HIGH*",
    "medium": "📋 *MEDIUM*",
    "low": "💡 *LOW*",
}

STATUS_TO_SEVERITY = {
    "fail": "high",
    "warn": "medium",
    "ok": None,
    "skip": None,
}

VERDICT_EMOJI = {
    "secure": "🛡️",
    "concerns": "⚠️",
    "critical": "🚨",
}


def _get_severity(item: dict) -> str:
    return item.get("severity", STATUS_TO_SEVERITY.get(item.get("status", "ok"), "low") or "low")


def _overall_verdict(items: List[dict]) -> str:
    severities = [_get_severity(i) for i in items]
    if "critical" in severities:
        return "critical"
    if "high" in severities:
        return "concerns"
    return "secure"


def build_security_digest(
    results: List[dict],
    *,
    title: str = "Security Council",
    timestamp: datetime | None = None,
) -> Tuple[str, List[dict]]:
    if timestamp is None:
        timestamp = datetime.now()

    ts_str = timestamp.strftime("%Y-%m-%d %H:%M")

    # Separate actionable from OK
    actionable = [r for r in results if r.get("status") in ("warn", "fail") or r.get("severity")]
    # Remove items with severity=None or status=ok that slipped in
    actionable = [r for r in actionable if _get_severity(r) is not None]
    ok_results = [r for r in results if r.get("status") == "ok" and not r.get("severity")]

    # Sort by severity
    actionable_sorted = sorted(actionable, key=lambda r: SEVERITY_ORDER.get(_get_severity(r), 99))

    verdict = _overall_verdict(actionable_sorted) if actionable_sorted else "secure"
    verdict_emoji = VERDICT_EMOJI[verdict]

    lines = [
        f"{verdict_emoji} *{title}* — {ts_str}",
        "",
    ]

    if not actionable_sorted:
        lines.append("All clear — no security findings.")
    else:
        # Group by severity with headers
        current_sev = None
        n = 0
        for r in actionable_sorted:
            sev = _get_severity(r)
            if sev != current_sev:
                if current_sev is not None:
                    lines.append("")
                lines.append(SEVERITY_HEADER.get(sev, f"*{sev.upper()}*"))
                current_sev = sev

            n += 1
            section = r.get("section", "")
            label = r.get("label", "Unknown")
            detail = r.get("detail", "")
            has_heal = bool(r.get("heal_cmd") or r.get("heal_action"))

            if section and not label.lower().startswith(section.lower()):
                display_label = f"{section} / {label}"
            else:
                display_label = label

            heal_badge = " 🔧" if has_heal else ""
            emoji = SEVERITY_EMOJI.get(sev, "❓")
            detail_str = f": {detail}" if detail else ""
            lines.append(f"[{n}] {emoji} {display_label}{detail_str}{heal_badge}")

    lines.append("")
    ok_count = len(ok_results)
    if ok_count:
        lines.append(f"✅ _{ok_count} check{'s' if ok_count != 1 else ''} OK_")
    lines.append("")

    if actionable_sorted:
        has_any_heal = any(r.get("heal_cmd") or r.get("heal_action") for r in actionable_sorted)
        if has_any_heal:
            lines.append("_Reply N for details • \"fix N\" to auto-heal 🔧_")
        else:
            lines.append("_Reply N for details_")

    return "\n".join(lines), actionable_sorted


def format_critical_alert(critical_items: List[dict]) -> str:
    lines = [
        f"🚨 *SECURITY ALERT* — {datetime.now().strftime('%H:%M')}",
        "",
        f"{len(critical_items)} critical finding{'s' if len(critical_items) != 1 else ''} require immediate attention:",
        "",
    ]
    for i, item in enumerate(critical_items, 1):
        label = item.get("label", "?")
        detail = item.get("detail", "")
        lines.append(f"[{i}] ❌ {label}: {detail}" if detail else f"[{i}] ❌ {label}")
    lines.append("")
    lines.append("_Full digest follows._")
    return "\n".join(lines)


def chunk_message(text: str, limit: int = 4000) -> List[str]:
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
