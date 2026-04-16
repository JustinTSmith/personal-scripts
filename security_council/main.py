"""
Security Council — main orchestrator.

Three-phase pipeline:
  Phase 1: Static collectors (parallel)
  Phase 2: AI analysis via Anthropic API (4 sequential perspective calls)
  Phase 3: Synthesis, enrichment, numbered digest, Telegram delivery

Usage:
  python3 -m security_council.main              # full run → Telegram
  python3 -m security_council.main --dry-run    # print digest to stdout
  python3 -m security_council.main --drill N    # detail for item N
  python3 -m security_council.main --heal N     # auto-fix item N
  python3 -m security_council.main --no-ai      # skip AI analysis (faster)
"""
import argparse
import json
import logging
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List

Path("/tmp/openclaw").mkdir(parents=True, exist_ok=True)

from .config import (
    OPENCLAW_JSON,
    SECURITY_COUNCIL_CHAT_ID,
    TELEGRAM_MSG_LIMIT,
    SECURITY_LOG,
    SEVERITY_ORDER,
)
from .lib.redact import redact
from .lib.report import build_security_digest, format_critical_alert, chunk_message
from .lib.heal import enrich_results, execute_heal
from .lib.drill import save_drill_state, get_item, format_drill_detail
from .lib.ai_analysis import run_ai_analysis

from .checks import secrets, permissions, code_exec, git_history, config_audit, logs_anomaly

logging.basicConfig(
    filename=str(SECURITY_LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("security_council")


# ── Telegram ──────────────────────────────────────────────────────────────────

def _get_bot_token() -> str | None:
    try:
        data = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
        return data.get("channels", {}).get("telegram", {}).get("botToken")
    except Exception:
        return None


def send_telegram(text: str, chat_id: str, bot_token: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        body = e.read(200).decode("utf-8", errors="replace")
        log.error("Telegram HTTP %s: %s", e.code, body)
        return False
    except Exception as e:
        log.error("Telegram send error: %s", e)
        return False


# ── Phase 1: Static collectors ────────────────────────────────────────────────

CHECK_MODULES = [
    ("Secrets", secrets),
    ("Permissions", permissions),
    ("CodeExec", code_exec),
    ("GitHistory", git_history),
    ("ConfigAudit", config_audit),
    ("LogsAnomaly", logs_anomaly),
]


def run_all_checks_parallel() -> List[dict]:
    section_results: dict[str, List[dict]] = {}
    with ThreadPoolExecutor(max_workers=len(CHECK_MODULES)) as executor:
        futures = {
            executor.submit(module.run): name
            for name, module in CHECK_MODULES
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results = future.result(timeout=30)
                section_results[name] = results
                log.info("Check %s: %d results", name, len(results))
            except Exception as e:
                log.exception("Check %s crashed", name)
                section_results[name] = [{
                    "section": name,
                    "status": "fail",
                    "severity": "high",
                    "label": f"{name} check",
                    "detail": f"check crashed: {str(e)[:80]}",
                }]

    # Reassemble in original order
    all_results = []
    for name, _ in CHECK_MODULES:
        all_results.extend(section_results.get(name, []))
    return all_results


# ── Phase 3: Synthesis ────────────────────────────────────────────────────────

def synthesize_findings(check_results: List[dict], ai_findings: List[dict]) -> List[dict]:
    """Convert AI findings to check result format and merge with static results."""
    all_results = list(check_results)

    status_map = {"critical": "fail", "high": "fail", "medium": "warn", "low": "warn"}

    for f in ai_findings:
        perspective = f.get("perspective", "AI")
        severity = f.get("severity", "medium")
        result = {
            "section": f"AI/{perspective}",
            "status": status_map.get(severity, "warn"),
            "severity": severity,
            "label": f.get("title", "untitled"),
            "detail": f.get("detail", "")[:200],
            "drill_detail": (
                f.get("detail", "") + "\n\n"
                f"*Remediation:* {f.get('remediation', 'N/A')}"
            ),
        }
        all_results.append(result)

    return all_results


# ── Drill mode ────────────────────────────────────────────────────────────────

def cmd_drill(n: int) -> int:
    item = get_item(n)
    if item is None:
        print(f"No item [{n}] in current drill state. Run the security council first.")
        return 1
    print(redact(format_drill_detail(item)))
    return 0


# ── Heal mode ─────────────────────────────────────────────────────────────────

def cmd_heal(target: str, dry_run: bool = False, bot_token: str | None = None) -> int:
    from .lib.drill import load_drill_state

    state = load_drill_state()
    items_map = state.get("items", {})
    if not items_map:
        print("No drill state found. Run the security council first.")
        return 1

    if target == "all":
        targets = [(int(k), v) for k, v in items_map.items()
                   if v.get("heal_cmd") or v.get("heal_action")]
    else:
        try:
            n = int(target)
        except ValueError:
            print(f"Invalid target '{target}' — use a number or 'all'")
            return 1
        item = items_map.get(str(n))
        if item is None:
            print(f"No item [{n}] found.")
            return 1
        targets = [(n, item)]

    if not targets:
        print("No healable items found.")
        return 0

    report_lines = [f"🔧 *Security Fix Report* — {datetime.now().strftime('%H:%M')}", ""]
    overall_ok = True

    for n, item in targets:
        label = item.get("label", "?")
        if dry_run:
            print(f"[{n}] DRY-RUN: {label}")
            print(f"     Action: {item.get('heal_action', 'none')}")
            if item.get("heal_cmd"):
                print(f"     Cmd:    {item['heal_cmd']}")
            continue

        ok, msg = execute_heal(item)
        log.info("Heal [%d] %s: ok=%s msg=%s", n, label, ok, msg)
        emoji = "✅" if ok else "⚠️"
        report_lines.append(f"{emoji} [{n}] {label}: {msg}")
        if not ok:
            overall_ok = False

    if not dry_run:
        report_text = redact("\n".join(report_lines))
        print(report_text)
        if bot_token:
            send_telegram(report_text, SECURITY_COUNCIL_CHAT_ID, bot_token)

    return 0 if overall_ok else 1


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False, skip_ai: bool = False, model_override: str | None = None) -> int:
    start = datetime.now()
    log.info("Security Council run started")

    if model_override:
        from .lib import ai_analysis as _ai
        _ai.MODEL_OVERRIDE = model_override
        log.info("Model override: %s", model_override)

    # Phase 1: Static collectors (parallel)
    check_results = run_all_checks_parallel()
    log.info("Phase 1 complete: %d static results", len(check_results))

    # Phase 2: AI analysis (sequential, 4 API calls)
    ai_findings = []
    if not skip_ai:
        log.info("Phase 2: AI analysis starting")
        ai_findings = run_ai_analysis(check_results)
        log.info("Phase 2 complete: %d AI findings", len(ai_findings))
    else:
        log.info("Phase 2: AI analysis skipped (--no-ai)")

    # Phase 3: Synthesis
    synthesized = synthesize_findings(check_results, ai_findings)
    synthesized = enrich_results(synthesized)

    # Phase 4: Auto-heal — fix everything that's safe to fix autonomously
    auto_fixed = []
    still_open = []
    for item in synthesized:
        if item.get("status") not in ("warn", "fail"):
            still_open.append(item)
            continue
        if not item.get("heal_cmd"):
            still_open.append(item)
            continue
        # Try to auto-fix
        ok, msg = execute_heal(item)
        if ok:
            log.info("AUTO-FIX: %s → %s", item.get("label", "?"), msg)
            auto_fixed.append({**item, "_fix_result": msg})
        else:
            # Fix failed or advisory-only — keep as open finding
            still_open.append(item)

    log.info("Phase 4: auto-fixed %d items, %d remain open", len(auto_fixed), len(still_open))

    # Build the digest from remaining open items only
    digest_text, numbered_items = build_security_digest(
        still_open, title="Security Council", timestamp=start
    )

    # Append auto-fix summary to digest
    if auto_fixed:
        fix_lines = [
            "",
            f"🔧 *Auto-Fixed ({len(auto_fixed)})*",
        ]
        for item in auto_fixed:
            label = item.get("label", "?")
            result = item.get("_fix_result", "done")
            fix_lines.append(f"  ✅ {label}: {result}")
        digest_text += "\n".join(fix_lines)

    save_drill_state(numbered_items, start.isoformat())
    log.info("Drill state saved: %d actionable items", len(numbered_items))

    digest_text = redact(digest_text)

    elapsed = (datetime.now() - start).total_seconds()
    log.info("All phases complete in %.1fs", elapsed)

    if dry_run:
        print(digest_text)
        return 0

    # Send to Telegram
    bot_token = _get_bot_token()
    if not bot_token:
        log.error("No bot token — aborting send")
        print("[ERROR] No bot token. Use --dry-run.", file=sys.stderr)
        return 1

    # Critical alert first (only for unfixed critical items)
    critical_items = [i for i in numbered_items if i.get("severity") == "critical"]
    if critical_items:
        alert = format_critical_alert(critical_items)
        send_telegram(redact(alert), SECURITY_COUNCIL_CHAT_ID, bot_token)

    # Full digest
    chunks = chunk_message(digest_text, limit=TELEGRAM_MSG_LIMIT)
    success = True
    for i, chunk in enumerate(chunks):
        ok = send_telegram(chunk, SECURITY_COUNCIL_CHAT_ID, bot_token)
        if not ok:
            log.error("Failed to send chunk %d/%d", i + 1, len(chunks))
            success = False

    log.info("Security Council complete in %.1fs, success=%s", elapsed, success)
    return 0 if success else 1


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Security Council — AI-powered nightly auditor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print to stdout instead of sending to Telegram")
    parser.add_argument("--drill", metavar="N", type=int,
                        help="Print full detail for drill item N")
    parser.add_argument("--heal", metavar="N",
                        help="Execute auto-fix for item N (or 'all')")
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip AI analysis (faster, static checks only)")
    parser.add_argument("--model", metavar="MODEL",
                        help="Override Anthropic model (e.g. claude-opus-4-6)")
    args = parser.parse_args()

    if args.drill is not None:
        sys.exit(cmd_drill(args.drill))
    elif args.heal is not None:
        bot_token = None if args.dry_run else _get_bot_token()
        sys.exit(cmd_heal(args.heal, dry_run=args.dry_run, bot_token=bot_token))
    else:
        sys.exit(main(dry_run=args.dry_run, skip_ai=args.no_ai, model_override=args.model))
