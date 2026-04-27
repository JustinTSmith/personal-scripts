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
import json
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
OPENCLAW_HOME = os.environ.get("OPENCLAW_HOME", os.path.expanduser("~/.openclaw"))
JOBS_FILE = os.path.join(OPENCLAW_HOME, "cron", "jobs.json")
REQUIRED_JOBS_FILE = os.path.join(OPENCLAW_HOME, "cron", "required_jobs.json")


def _icon(status: str) -> str:
    return {"success": "✅", "failed": "❌", "failure": "❌", "running": "🔄", "skipped": "⏭"}.get(
        status, "❓"
    )


def check_required_jobs(now: float) -> list[str]:
    """Alert on any required jobs missing from jobs.json or silent past their threshold."""
    if not os.path.exists(REQUIRED_JOBS_FILE):
        return []

    try:
        required = json.load(open(REQUIRED_JOBS_FILE)).get("required", [])
    except Exception as e:
        alert.send(f"⚠️ Could not read required_jobs.json: {e}")
        return []

    try:
        active_ids = {j["id"] for j in json.load(open(JOBS_FILE)).get("jobs", [])}
    except Exception as e:
        alert.send(f"🚨 Could not read cron/jobs.json: {e}")
        return [r["id"] for r in required]

    problems = []
    for req in required:
        job_id = req["id"]
        job_name = req["name"]
        desc = req.get("description", job_name)

        if job_id not in active_ids:
            msg = f"🚨 *Required job missing from jobs.json*\n`{job_name}` ({job_id})\n_{desc}_"
            alert.send(msg)
            problems.append(job_name)
            continue

        max_silence_h = req.get("maxSilenceHours")
        if max_silence_h:
            runs = cron_log.query(job_name=job_name, limit=1)
            if not runs:
                last_run_age_h = float("inf")
            else:
                last_run_age_h = (now - runs[0]["started_at"]) / 3600
            if last_run_age_h > max_silence_h:
                age_str = f"{last_run_age_h:.0f}h" if last_run_age_h != float("inf") else "never"
                msg = (
                    f"⏰ *Required job overdue*\n`{job_name}` — last ran {age_str} ago "
                    f"(threshold {max_silence_h}h)\n_{desc}_"
                )
                alert.send(msg)
                problems.append(job_name)

    return problems


def run(quiet: bool = False, filter_jobs: list[str] | None = None) -> dict:
    now = time.time()

    # 0. Enforce required-jobs manifest before anything else
    missing_or_overdue = check_required_jobs(now)

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

    result = {"jobs": job_results, "stale_cleaned": stale_count, "persistent_failures": persistent_failures, "missing_or_overdue": missing_or_overdue}
    print(f"[health_check] {len(job_results)} jobs checked, {len(persistent_failures)} persistent failures, {stale_count} stale cleaned, {len(missing_or_overdue)} required jobs missing/overdue")
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
