"""
Platform Health Report — main orchestrator.

Modes:
  python3 -m platform_health.main              # run checks, send digest to Telegram
  python3 -m platform_health.main --dry-run    # print digest to stdout
  python3 -m platform_health.main --drill N    # print full detail for item N
  python3 -m platform_health.main --heal N     # execute auto-fix for item N
  python3 -m platform_health.main --heal all   # auto-fix all healable items

Parallel execution: all check modules run concurrently via ThreadPoolExecutor.
Self-healing: heal.py enriches results with fix actions; --heal N executes them.
Drill state: numbered items saved to /tmp/openclaw/drill_state.json for agent lookup.
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

# ── Bootstrap log dir ─────────────────────────────────────────────────────────
Path("/tmp/openclaw").mkdir(parents=True, exist_ok=True)

from .config import (
    OPENCLAW_JSON,
    PLATFORM_HEALTH_CHAT_ID,
    TELEGRAM_MSG_LIMIT,
    HEALTH_LOG,
)
from .lib.redact import redact
from .lib.report import build_digest, chunk_message
from .lib.heal import enrich_results, analyze_error_logs, execute_heal
from .lib.drill import save_drill_state, get_item, format_drill_detail

from .checks import gateway, crons, logs, git, skills, backups, configs, coverage

logging.basicConfig(
    filename=str(HEALTH_LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("platform_health")


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


# ── Parallel check runner ─────────────────────────────────────────────────────

CHECK_MODULES = [
    ("Gateway",  gateway),
    ("Crons",    crons),
    ("Logs",     logs),
    ("Git",      git),
    ("Skills",   skills),
    ("Backups",  backups),
    ("Configs",  configs),
    ("Coverage", coverage),
]


def run_all_checks_parallel() -> List[dict]:
    """Run all check modules concurrently. Returns merged result list."""
    # Map future → module name for error attribution
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
                log.exception("Check %s raised exception", name)
                section_results[name] = [{
                    "section": name,
                    "status": "fail",
                    "label": f"{name} check",
                    "detail": f"check crashed: {str(e)[:80]}",
                }]

    # Reassemble in original order (parallel completion is non-deterministic)
    all_results = []
    for name, _ in CHECK_MODULES:
        all_results.extend(section_results.get(name, []))

    return all_results


# ── Drill mode ────────────────────────────────────────────────────────────────

def cmd_drill(n: int) -> int:
    item = get_item(n)
    if item is None:
        print(f"No item [{n}] in current drill state. Run the health check first.")
        return 1
    detail_text = format_drill_detail(item)
    print(redact(detail_text))
    return 0


# ── Heal mode ─────────────────────────────────────────────────────────────────

def cmd_heal(target: str, dry_run: bool = False, bot_token: str | None = None) -> int:
    from .lib.drill import load_drill_state

    state = load_drill_state()
    items_map = state.get("items", {})

    if not items_map:
        print("No drill state found. Run the health check first.")
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

    report_lines = [f"🔧 *Auto-Heal Report* — {datetime.now().strftime('%H:%M')}"]
    report_lines.append("")

    overall_ok = True
    for n, item in targets:
        label = item.get("label", "?")
        if dry_run:
            heal_cmd = item.get("heal_cmd", "")
            heal_action = item.get("heal_action", "no action defined")
            print(f"[{n}] DRY-RUN: {label}")
            print(f"     Action: {heal_action}")
            if heal_cmd:
                print(f"     Cmd:    {heal_cmd}")
            continue

        ok, msg = execute_heal(item)
        log.info("Heal [%d] %s: ok=%s msg=%s", n, label, ok, msg)
        status_emoji = "✅" if ok else "⚠️"
        report_lines.append(f"{status_emoji} [{n}] {label}: {msg}")
        if not ok:
            overall_ok = False

    if not dry_run:
        report_text = redact("\n".join(report_lines))
        print(report_text)

        if bot_token and PLATFORM_HEALTH_CHAT_ID:
            send_telegram(report_text, PLATFORM_HEALTH_CHAT_ID, bot_token)

    return 0 if overall_ok else 1


# ── Main report ───────────────────────────────────────────────────────────────

def main(dry_run: bool = False) -> int:
    start = datetime.now()
    log.info("Platform health run started (parallel)")

    # 1. Run all checks in parallel
    results = run_all_checks_parallel()

    # 2. Run error log analysis for patch suggestions (serial — fast, reads files)
    patch_suggestions = analyze_error_logs()
    results.extend(patch_suggestions)

    # 3. Enrich results with heal actions
    results = enrich_results(results)

    # 4. Auto-heal — fix everything that's safe to fix autonomously
    auto_fixed = []
    still_open = []
    for item in results:
        if item.get("status") not in ("warn", "fail"):
            still_open.append(item)
            continue
        if not item.get("heal_cmd"):
            still_open.append(item)
            continue
        ok, msg = execute_heal(item)
        if ok:
            log.info("AUTO-FIX: %s → %s", item.get("label", "?"), msg)
            auto_fixed.append({**item, "_fix_result": msg})
        else:
            still_open.append(item)

    log.info("Auto-fixed %d items, %d remain open", len(auto_fixed), len(still_open))

    # 5. Build numbered digest from remaining open items
    digest_text, numbered_items = build_digest(
        still_open, title="Platform Health", timestamp=start
    )

    # Append auto-fix summary
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

    # 6. Save drill state for agent lookup
    run_ts = start.isoformat()
    save_drill_state(numbered_items, run_ts)
    log.info("Drill state saved: %d actionable items", len(numbered_items))

    # 7. Redact before any output
    digest_text = redact(digest_text)

    elapsed = (datetime.now() - start).total_seconds()
    log.info("Checks complete in %.1fs", elapsed)

    if dry_run:
        print(digest_text)
        return 0

    # 7. Send to Telegram
    bot_token = _get_bot_token()
    if not bot_token:
        log.error("No bot token — aborting send")
        print("[ERROR] No bot token. Use --dry-run.", file=sys.stderr)
        return 1

    chunks = chunk_message(digest_text, limit=TELEGRAM_MSG_LIMIT)
    success = True
    for i, chunk in enumerate(chunks):
        ok = send_telegram(chunk, PLATFORM_HEALTH_CHAT_ID, bot_token)
        if not ok:
            log.error("Failed to send chunk %d/%d", i + 1, len(chunks))
            success = False

    log.info("Platform health complete in %.1fs, success=%s", elapsed, success)
    return 0 if success else 1


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Platform health report")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print to stdout instead of sending to Telegram")
    parser.add_argument("--drill", metavar="N", type=int,
                        help="Print full detail for drill item N")
    parser.add_argument("--heal", metavar="N",
                        help="Execute auto-fix for item N (or 'all')")
    args = parser.parse_args()

    if args.drill is not None:
        sys.exit(cmd_drill(args.drill))
    elif args.heal is not None:
        bot_token = None if args.dry_run else _get_bot_token()
        sys.exit(cmd_heal(args.heal, dry_run=args.dry_run, bot_token=bot_token))
    else:
        sys.exit(main(dry_run=args.dry_run))
