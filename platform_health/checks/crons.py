from __future__ import annotations
"""
Cron health checks:
- Query ~/.openclaw/tasks/runs.sqlite for recent task execution history
- Detect failed runs, missed fires, and stale tasks
- Read-only SQLite access
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import List, Set
from pathlib import Path

from ..config import TASKS_DB, FLOWS_DB, OPENCLAW_DIR

# Tasks not seen in this many hours are considered stale
STALE_THRESHOLD_HOURS = 28  # bumped from 26 — gives evening daily jobs a 4h buffer

# Known weekly crons that only fire once per week — use 8-day threshold
WEEKLY_CRONS = {
    "mira-weekly-report",
    "apple-health-export-reminder",
}
WEEKLY_STALE_THRESHOLD_HOURS = 192  # 8 days

# How many recent runs to sample per task
RECENT_RUNS_SAMPLE = 10


def _load_active_cron_labels() -> Set[str]:
    """Load enabled cron job names from jobs.json. Returns set of active label names."""
    jobs_json = OPENCLAW_DIR / "cron" / "jobs.json"
    if not jobs_json.exists():
        return set()
    try:
        data = json.loads(jobs_json.read_text(encoding="utf-8"))
        return {
            j.get("name", "")
            for j in data.get("jobs", [])
            if j.get("name") and j.get("enabled", True)
        }
    except (json.JSONDecodeError, OSError):
        return set()


def _open_ro(path: Path):
    """Open SQLite database read-only. Returns connection or None."""
    if not path.exists():
        return None
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _check_task_runs(db_path: Path) -> List[dict]:
    results = []
    conn = _open_ro(db_path)

    if conn is None:
        if not db_path.exists():
            results.append({
                "section": "Crons",
                "status": "warn",
                "label": "tasks DB",
                "detail": "not found (no crons registered yet?)",
            })
        else:
            results.append({
                "section": "Crons",
                "status": "fail",
                "label": "tasks DB",
                "detail": f"could not open {db_path}",
            })
        return results

    try:
        cursor = conn.cursor()

        # Get table names to handle schema differences
        tables = {row[0] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

        if "task_runs" not in tables:
            results.append({
                "section": "Crons",
                "status": "warn",
                "label": "tasks DB",
                "detail": "task_runs table missing — DB may be uninitialized",
            })
            return results

        # Overall run counts in the last 48 hours
        # created_at is Unix timestamp in milliseconds
        cutoff_ms = int((time.time() - 48 * 3600) * 1000)
        row = cursor.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status IN ('failed', 'lost', 'timed_out') THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) as success
            FROM task_runs
            WHERE created_at > ?
            """,
            (cutoff_ms,),
        ).fetchone()

        total = row[0] or 0
        failed = row[1] or 0
        success = row[2] or 0

        if total == 0:
            results.append({
                "section": "Crons",
                "status": "warn",
                "label": "task runs (48h)",
                "detail": "no runs recorded",
            })
        elif failed > 0:
            results.append({
                "section": "Crons",
                "status": "warn" if failed < total * 0.5 else "fail",
                "label": "task runs (48h)",
                "detail": f"{success} ok, {failed} failed of {total} total",
            })
        else:
            results.append({
                "section": "Crons",
                "status": "ok",
                "label": "task runs (48h)",
                "detail": f"{success} ok, 0 failed",
            })

        # Per-task: find any tasks with consecutive failures
        task_rows = cursor.execute(
            """
            SELECT task_id, status, created_at
            FROM task_runs
            ORDER BY created_at DESC
            LIMIT 200
            """
        ).fetchall()

        # Group latest runs per task_id (each row is a sqlite3.Row)
        task_latest: dict = {}
        for row in task_rows:
            tid = str(row[0])  # task_id — use positional to avoid Row subscript ambiguity
            status_val = str(row[1])
            if tid not in task_latest:
                task_latest[tid] = []
            if len(task_latest[tid]) < RECENT_RUNS_SAMPLE:
                task_latest[tid].append(status_val)

        for tid, statuses in task_latest.items():
            if all(s in ("failed", "lost", "timed_out") for s in statuses[:3]) and len(statuses) >= 3:
                results.append({
                    "section": "Crons",
                    "status": "fail",
                    "label": f"task:{tid}",
                    "detail": f"last {min(3, len(statuses))} runs all failed",
                })

        # Stale check: only flag named crons that are still active in jobs.json
        active_labels = _load_active_cron_labels()
        stale_cutoff_ms = int((time.time() - STALE_THRESHOLD_HOURS * 3600) * 1000)
        stale_rows = cursor.execute(
            """
            SELECT label, MAX(created_at) as last_run_ms, COUNT(*) as run_count
            FROM task_runs
            WHERE label IS NOT NULL AND label != ''
            GROUP BY label
            HAVING last_run_ms < ?
            ORDER BY last_run_ms ASC
            LIMIT 10
            """,
            (stale_cutoff_ms,),
        ).fetchall()

        for row in stale_rows:
            label_val = str(row[0])
            last_ms = row[1]
            run_count = row[2]

            # Skip deleted crons (removed from jobs.json but still have DB history)
            if active_labels and label_val not in active_labels:
                continue

            # Skip weekly crons that have their own longer threshold
            if label_val in WEEKLY_CRONS:
                weekly_cutoff = int((time.time() - WEEKLY_STALE_THRESHOLD_HOURS * 3600) * 1000)
                if last_ms and last_ms > weekly_cutoff:
                    continue  # not actually stale for a weekly job

            if last_ms:
                last_dt = datetime.fromtimestamp(last_ms / 1000).strftime("%Y-%m-%d %H:%M")
            else:
                last_dt = "unknown"
            results.append({
                "section": "Crons",
                "status": "warn",
                "label": f"stale cron: {label_val[:40]}",
                "detail": f"last run: {last_dt} ({run_count} total runs)",
            })

    except sqlite3.Error as e:
        results.append({
            "section": "Crons",
            "status": "fail",
            "label": "tasks DB query",
            "detail": str(e)[:100],
        })
    finally:
        conn.close()

    return results


def _check_flows(db_path: Path) -> List[dict]:
    """Check flows DB for recent failures."""
    results = []
    conn = _open_ro(db_path)

    if conn is None:
        if not db_path.exists():
            return []  # Flows DB is optional
        results.append({
            "section": "Crons",
            "status": "warn",
            "label": "flows DB",
            "detail": "could not open",
        })
        return results

    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "flow_runs" not in tables:
            return []

        cutoff_ms = int((time.time() - 48 * 3600) * 1000)
        row = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status IN ('failed', 'error', 'cancelled') THEN 1 ELSE 0 END) as failed
            FROM flow_runs
            WHERE created_at > ?
            """,
            (cutoff_ms,),
        ).fetchone()

        total = row[0] or 0
        failed = row[1] or 0

        if failed > 0:
            results.append({
                "section": "Crons",
                "status": "warn",
                "label": "flows (48h)",
                "detail": f"{failed} failed/cancelled of {total} total",
            })
        elif total > 0:
            results.append({
                "section": "Crons",
                "status": "ok",
                "label": "flows (48h)",
                "detail": f"{total} flows, 0 failed",
            })

    except sqlite3.Error as e:
        results.append({
            "section": "Crons",
            "status": "warn",
            "label": "flows DB query",
            "detail": str(e)[:80],
        })
    finally:
        conn.close()

    return results


def run() -> List[dict]:
    results = []
    results.extend(_check_task_runs(TASKS_DB))
    results.extend(_check_flows(FLOWS_DB))
    return results
