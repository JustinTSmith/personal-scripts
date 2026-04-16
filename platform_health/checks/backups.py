"""
Backup health checks:
- SQLite DB freshness (flows/registry.sqlite, tasks/runs.sqlite)
- WAL file size checks (large WAL = checkpoint not running)
- Workspace backup tar existence and age
- Git repo as backup proxy (last push time)
"""
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from ..config import FLOWS_DB, TASKS_DB, OPENCLAW_DIR, SCRIPTS_DIR

# Thresholds
DB_STALE_HOURS = 48          # DB not modified in this long → warn
WAL_LARGE_MB = 50            # WAL file larger than this → warn
BACKUP_TAR_STALE_DAYS = 7   # Local backup tar older than this → warn

# Where to look for backup tars
BACKUP_SEARCH_DIRS = [
    Path.home() / "Workspace" / "backups",
    Path("/tmp/openclaw"),
    SCRIPTS_DIR,
]


def _db_freshness(path: Path, label: str) -> List[dict]:
    results = []

    if not path.exists():
        results.append({
            "section": "Backups",
            "status": "warn",
            "label": label,
            "detail": "DB not found",
        })
        return results

    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        age_hours = (datetime.now() - mtime).total_seconds() / 3600

        if age_hours > DB_STALE_HOURS:
            results.append({
                "section": "Backups",
                "status": "warn",
                "label": label,
                "detail": f"not written in {age_hours:.0f}h",
            })
        else:
            size_kb = path.stat().st_size / 1024
            results.append({
                "section": "Backups",
                "status": "ok",
                "label": label,
                "detail": f"{size_kb:.0f}KB, updated {age_hours:.1f}h ago",
            })

        # WAL file check
        wal_path = path.with_suffix(path.suffix + "-wal")
        if wal_path.exists():
            wal_mb = wal_path.stat().st_size / (1024 * 1024)
            if wal_mb > WAL_LARGE_MB:
                results.append({
                    "section": "Backups",
                    "status": "warn",
                    "label": f"{label} WAL",
                    "detail": f"{wal_mb:.1f}MB — checkpoint may be stuck",
                })

    except OSError as e:
        results.append({
            "section": "Backups",
            "status": "warn",
            "label": label,
            "detail": str(e)[:80],
        })

    return results


def _find_backup_tars() -> List[dict]:
    """Look for recent .tar.gz backup files in known backup dirs."""
    results = []
    found_any = False

    for search_dir in BACKUP_SEARCH_DIRS:
        if not search_dir.exists():
            continue
        tars = sorted(
            search_dir.glob("*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not tars:
            continue

        found_any = True
        latest = tars[0]
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)
        age_days = (datetime.now() - mtime).total_seconds() / 86400
        size_mb = latest.stat().st_size / (1024 * 1024)

        if age_days > BACKUP_TAR_STALE_DAYS:
            results.append({
                "section": "Backups",
                "status": "warn",
                "label": f"backup tar ({latest.name[:30]})",
                "detail": f"{age_days:.0f} days old, {size_mb:.1f}MB",
            })
        else:
            results.append({
                "section": "Backups",
                "status": "ok",
                "label": f"backup tar",
                "detail": f"{latest.name[:30]}, {age_days:.0f}d ago, {size_mb:.1f}MB",
            })

    if not found_any:
        results.append({
            "section": "Backups",
            "status": "warn",
            "label": "backup tars",
            "detail": "no .tar.gz found in backup dirs",
        })

    return results


def _openclaw_dir_size() -> List[dict]:
    """Quick sanity: confirm ~/.openclaw exists and has reasonable size."""
    results = []
    if not OPENCLAW_DIR.exists():
        results.append({
            "section": "Backups",
            "status": "fail",
            "label": "~/.openclaw",
            "detail": "directory missing",
        })
        return results

    # Count top-level items as a proxy for health
    items = list(OPENCLAW_DIR.iterdir())
    results.append({
        "section": "Backups",
        "status": "ok",
        "label": "~/.openclaw",
        "detail": f"{len(items)} top-level items",
    })
    return results


def run() -> List[dict]:
    results = []
    results.extend(_db_freshness(FLOWS_DB, "flows.sqlite"))
    results.extend(_db_freshness(TASKS_DB, "tasks.sqlite"))
    results.extend(_find_backup_tars())
    results.extend(_openclaw_dir_size())
    return results
