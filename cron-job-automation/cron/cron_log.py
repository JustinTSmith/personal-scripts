#!/usr/bin/env python3
"""
Central cron log database (SQLite).

CLI usage:
  python3 cron_log.py log-start  <job-name>
  python3 cron_log.py log-end    <run-id> <status> [summary]
  python3 cron_log.py should-run <job-name> [--interval daily|hourly|6h]
  python3 cron_log.py query      [--job NAME] [--status STATUS] [--since EPOCH] [--limit N]
  python3 cron_log.py check-failures <job-name> [--window 6] [--threshold 3]
  python3 cron_log.py cleanup-stale [--max-age 2]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from typing import Optional

DB_PATH = os.environ.get(
    "CRON_LOG_DB",
    os.path.expanduser("~/.openclaw/cron_log.db")
)


# ── Internal helpers ────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cron_runs (
                run_id       TEXT PRIMARY KEY,
                job_name     TEXT NOT NULL,
                started_at   REAL NOT NULL,
                ended_at     REAL,
                status       TEXT NOT NULL DEFAULT 'running',
                duration_sec REAL,
                summary      TEXT,
                host         TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job ON cron_runs(job_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON cron_runs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_started ON cron_runs(started_at)")


# ── Public API ───────────────────────────────────────────────────────────────

def log_start(job_name: str) -> str:
    """Record job start. Returns run_id. Auto-cleans stale jobs first."""
    _init()
    cleanup_stale()
    run_id = str(uuid.uuid4())
    try:
        host = os.uname().nodename
    except Exception:
        host = "unknown"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO cron_runs (run_id, job_name, started_at, host) VALUES (?,?,?,?)",
            (run_id, job_name, time.time(), host),
        )
    return run_id


def log_end(run_id: str, status: str, summary: str = "") -> None:
    """Record job completion. status: success | failure | skipped"""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """UPDATE cron_runs
               SET ended_at=?, status=?, duration_sec=(?-started_at), summary=?
               WHERE run_id=?""",
            (now, status, now, summary[:2000], run_id),
        )


def query(
    job_name: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int = 100,
) -> list[dict]:
    """Return filtered run history, newest first."""
    _init()
    clauses, params = [], []
    if job_name:
        clauses.append("job_name = ?"); params.append(job_name)
    if status:
        clauses.append("status = ?"); params.append(status)
    if since:
        clauses.append("started_at >= ?"); params.append(since)
    if until:
        clauses.append("started_at <= ?"); params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM cron_runs {where} ORDER BY started_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


def should_run(job_name: str, interval: str = "daily") -> bool:
    """
    Idempotency check. Returns True if the job should run.
    Interval: 'hourly' | 'daily' | '<N>h' | '<N>m'
    """
    _init()
    _map = {"hourly": 3600, "daily": 86400}
    if interval in _map:
        window = _map[interval]
    elif interval.endswith("h"):
        window = int(interval[:-1]) * 3600
    elif interval.endswith("m"):
        window = int(interval[:-1]) * 60
    else:
        window = 86400

    since = time.time() - window
    with _connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM cron_runs
               WHERE job_name=? AND status='success' AND started_at >= ?
               LIMIT 1""",
            (job_name, since),
        ).fetchone()
    return row is None


def cleanup_stale(max_age_hours: float = 2.0) -> int:
    """Mark jobs stuck in 'running' for >max_age_hours as failed. Returns count."""
    _init()
    cutoff = time.time() - (max_age_hours * 3600)
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE cron_runs
               SET status='failed', ended_at=?, summary='auto-marked stale (>2h in running state)'
               WHERE status='running' AND started_at < ?""",
            (time.time(), cutoff),
        )
        return cur.rowcount


def check_persistent_failures(
    job_name: str, window_hours: float = 6.0, threshold: int = 3
) -> bool:
    """Returns True if job failed >= threshold times in the past window_hours."""
    _init()
    since = time.time() - (window_hours * 3600)
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM cron_runs
               WHERE job_name=? AND status='failed' AND started_at >= ?""",
            (job_name, since),
        ).fetchone()
    return row["cnt"] >= threshold


# ── CLI ──────────────────────────────────────────────────────────────────────

def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_row(r: dict) -> str:
    dur = f"{r['duration_sec']:.1f}s" if r.get("duration_sec") else "-"
    ts = _ts(r["started_at"])
    return f"{r['run_id'][:8]}  {r['job_name']:<30} {r['status']:<10} {ts}  {dur}"


def main():
    parser = argparse.ArgumentParser(description="Cron log DB CLI")
    sub = parser.add_subparsers(dest="cmd")

    # log-start
    p = sub.add_parser("log-start")
    p.add_argument("job_name")

    # log-end
    p = sub.add_parser("log-end")
    p.add_argument("run_id")
    p.add_argument("status", choices=["success", "failure", "skipped"])
    p.add_argument("summary", nargs="?", default="")

    # should-run
    p = sub.add_parser("should-run")
    p.add_argument("job_name")
    p.add_argument("--interval", default="daily")

    # query
    p = sub.add_parser("query")
    p.add_argument("--job", default=None)
    p.add_argument("--status", default=None)
    p.add_argument("--since", type=float, default=None)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")

    # check-failures
    p = sub.add_parser("check-failures")
    p.add_argument("job_name")
    p.add_argument("--window", type=float, default=6.0)
    p.add_argument("--threshold", type=int, default=3)

    # cleanup-stale
    p = sub.add_parser("cleanup-stale")
    p.add_argument("--max-age", type=float, default=2.0)

    args = parser.parse_args()

    if args.cmd == "log-start":
        run_id = log_start(args.job_name)
        print(run_id)

    elif args.cmd == "log-end":
        log_end(args.run_id, args.status, args.summary)

    elif args.cmd == "should-run":
        result = should_run(args.job_name, args.interval)
        print("yes" if result else "no")
        sys.exit(0 if result else 1)

    elif args.cmd == "query":
        rows = query(job_name=args.job, status=args.status, since=args.since, limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2))
        else:
            print(f"{'RUN_ID':<8}  {'JOB':<30} {'STATUS':<10} {'STARTED':<19}  DUR")
            print("-" * 80)
            for r in rows:
                print(_fmt_row(r))

    elif args.cmd == "check-failures":
        triggered = check_persistent_failures(args.job_name, args.window, args.threshold)
        if triggered:
            print(f"ALERT: {args.job_name} failed {args.threshold}+ times in {args.window}h")
            sys.exit(2)

    elif args.cmd == "cleanup-stale":
        count = cleanup_stale(args.max_age)
        print(f"Marked {count} stale jobs as failed")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
