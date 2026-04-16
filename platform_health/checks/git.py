"""
Git health checks:
- ~/.openclaw repo: dirty working tree, ahead/behind remote, last commit age
- ~/.openclaw/workspace repo: same checks
- Detects if hourly push is working
"""
import subprocess
from datetime import datetime, timedelta
from typing import List
from pathlib import Path

from ..config import OPENCLAW_REPO, WORKSPACE_REPO

# If last commit is older than this, warn that git push may be broken
PUSH_STALE_HOURS = 3


def _git(repo: Path, *args, timeout: int = 10) -> tuple[int, str]:
    """Run a git command in `repo`. Returns (returncode, stdout)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists() or (path / ".git").is_file()


def _check_repo(path: Path, label: str) -> List[dict]:
    results = []

    if not path.exists():
        results.append({
            "section": "Git",
            "status": "warn",
            "label": label,
            "detail": "directory not found",
        })
        return results

    if not _is_git_repo(path):
        results.append({
            "section": "Git",
            "status": "warn",
            "label": label,
            "detail": "not a git repo",
        })
        return results

    # Dirty working tree
    rc, dirty = _git(path, "status", "--porcelain")
    if rc != 0:
        results.append({
            "section": "Git",
            "status": "warn",
            "label": label,
            "detail": "git status failed",
        })
    elif dirty:
        line_count = len(dirty.splitlines())
        results.append({
            "section": "Git",
            "status": "warn",
            "label": f"{label} working tree",
            "detail": f"{line_count} uncommitted change(s)",
        })
    else:
        results.append({
            "section": "Git",
            "status": "ok",
            "label": f"{label} working tree",
            "detail": "clean",
        })

    # Last commit age
    rc, log_out = _git(path, "log", "-1", "--format=%ct %s")
    if rc == 0 and log_out:
        parts = log_out.split(" ", 1)
        try:
            ts = int(parts[0])
            msg = parts[1][:50] if len(parts) > 1 else ""
            commit_dt = datetime.fromtimestamp(ts)
            age = datetime.now() - commit_dt
            age_hours = age.total_seconds() / 3600
            age_str = (
                f"{int(age.total_seconds() // 60)}m ago"
                if age_hours < 1
                else f"{age_hours:.1f}h ago"
            )

            if age_hours > PUSH_STALE_HOURS:
                results.append({
                    "section": "Git",
                    "status": "warn",
                    "label": f"{label} last commit",
                    "detail": f"{age_str} — {msg}",
                })
            else:
                results.append({
                    "section": "Git",
                    "status": "ok",
                    "label": f"{label} last commit",
                    "detail": f"{age_str} — {msg}",
                })
        except (ValueError, OSError):
            results.append({
                "section": "Git",
                "status": "warn",
                "label": f"{label} last commit",
                "detail": f"could not parse: {log_out[:50]}",
            })
    else:
        results.append({
            "section": "Git",
            "status": "warn",
            "label": f"{label} last commit",
            "detail": "no commits or git error",
        })

    # Ahead/behind remote
    rc, fetch_out = _git(path, "fetch", "--dry-run", timeout=5)
    rc2, ab = _git(path, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if rc2 == 0 and ab:
        parts = ab.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
            if ahead > 0 and behind > 0:
                results.append({
                    "section": "Git",
                    "status": "warn",
                    "label": f"{label} remote",
                    "detail": f"diverged: {ahead} ahead, {behind} behind",
                })
            elif ahead > 0:
                results.append({
                    "section": "Git",
                    "status": "warn",
                    "label": f"{label} remote",
                    "detail": f"{ahead} commit(s) not pushed",
                })
            elif behind > 0:
                results.append({
                    "section": "Git",
                    "status": "warn",
                    "label": f"{label} remote",
                    "detail": f"{behind} commit(s) behind remote",
                })
            else:
                results.append({
                    "section": "Git",
                    "status": "ok",
                    "label": f"{label} remote",
                    "detail": "in sync",
                })
    else:
        results.append({
            "section": "Git",
            "status": "skip",
            "label": f"{label} remote",
            "detail": "no upstream or offline",
        })

    return results


def run() -> List[dict]:
    results = []
    results.extend(_check_repo(OPENCLAW_REPO, "openclaw"))
    results.extend(_check_repo(WORKSPACE_REPO, "workspace"))
    return results
