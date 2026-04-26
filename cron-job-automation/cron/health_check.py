#!/usr/bin/env python3
"""
Cron health check — runs every 30 min via scheduler.

What it does:
  1. Calls cleanup_stale() to mark stuck jobs as failed
  2. Scans recent run history per job
  3. Alerts on persistent failures (3+ failures in 6h)
  4. Posts a digest to the cron-updates Telegram chat
  5. Syncs latest state to Nerve dashboard

CLI:
  python3 health_check.py [--quiet] [--jobs JOB1,JOB2]
"""
import argparse
import os
import sys
import time
from datetime import datetime

# Ensure sibling modules are importable when run directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alert
import cron_log
import nerve_bridge


STALE_MAX_AGE_HOURS = 2.0
FAILURE_WINDOW_HOURS = 6.0
FAILURE_THRESHOLD = 3
RECENT_WINDOW_HOURS = 24.0


def _icon(status: str) -> str:
    return {"success": "✅", "failed": "❌", "failure": "❌", "running": "🔄", "skipped": "⏭"}.get(
        status, "❓"
    )


def run(quiet: bool = False, filter_jobs: list[str] | None = None) -> dict:
    now = time.time()

    # 1. Clean up stale jobs first
    stale_count = cron_log.cleanup_stale(STALE_MAX_AGE_HOURS)
    if stale_count:
        alert.send(f"⚠️ Auto-cleaned *{stale_count}* stale cron job(s) stuck in 'running' state")

    # 2. Get all job names with activity in the last 24h
    recent_runs = cron_log.query(since=now - RECENT_WINDOW_HOURS * 3600, limit=500)
    job_names = sorted(set(r["job_name"] for r in recent_runs))

    if filter_jobs:
        job_names = [j for j in job_names if j in filter_jobs]

    if not job_names:
        if not quiet:
            alert.send("🏥 *Cron Health Check* — no jobs in the last 24h")
        return {"jobs": {}, "stale_cleaned": stale_count}

    # 3. Build per-job status
    job_results = {}
    persistent_failures = []

    for job in job_names:
        runs = cron_log.query(job_name=job, limit=10)
        if not runs:
            continue

        last = runs[0]
        failure_count = sum(1 for r in runs if r["status"] in ("failed", "failure"))
        success_count = sum(1 for r in runs if r["status"] == "success")

        # Check persistent failure
        is_persistent_failure = cron_log.check_persistent_failures(
            job, FAILURE_WINDOW_HOURS, FAILURE_THRESHOLD
        )
        if is_persistent_failure:
            persistent_failures.append(job)

        job_results[job] = {
            "last_status": last["status"],
            "last_run": last["started_at"],
            "last_duration": last.get("duration_sec"),
            "failure_count_recent": failure_count,
            "success_count_recent": success_count,
            "persistent_failure": is_persistent_failure,
        }

    # 4. Send persistent failure alerts (separate urgent messages)
    for job in persistent_failures:
        recent_fails = cron_log.query(
            job_name=job,
            status="failed",
            since=now - FAILURE_WINDOW_HOURS * 3600,
            limit=20,
        )
        alert.persistent_failure_alert(job, len(recent_fails), FAILURE_WINDOW_HOURS)

    # 5. Build and send health digest
    if not quiet:
        lines = [f"📅 *{datetime.now().strftime('%Y-%m-%d %H:%M')}*"]
        for job, info in job_results.items():
            icon = _icon(info["last_status"])
            ts = datetime.fromtimestamp(info["last_run"]).strftime("%H:%M")
            dur = f"{info['last_duration']:.0f}s" if info.get("last_duration") else "—"
            pf_flag = " 🚨" if info["persistent_failure"] else ""
            lines.append(f"{icon} `{job}` — {ts}, {dur}{pf_flag}")

        if stale_count:
            lines.append(f"🧹 {stale_count} stale job(s) cleaned up")

        alert.health_report(lines)

    # 6. Sync to Nerve
    nerve_bridge.sync(limit=100)

    result = {"jobs": job_results, "stale_cleaned": stale_count, "persistent_failures": persistent_failures}
    print(f"[health_check] {len(job_results)} jobs checked, {len(persistent_failures)} persistent failures, {stale_count} stale cleaned")
    return result


def main():
    parser = argparse.ArgumentParser(description="Cron health check")
    parser.add_argument("--quiet", action="store_true", help="Skip Telegram digest (still sends failure alerts)")
    parser.add_argument("--jobs", default="", help="Comma-separated job names to check (default: all)")
    args = parser.parse_args()

    filter_jobs = [j.strip() for j in args.jobs.split(",") if j.strip()] or None
    run(quiet=args.quiet, filter_jobs=filter_jobs)


if __name__ == "__main__":
    main()
