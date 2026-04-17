from __future__ import annotations
"""
Git history analysis: recent security-relevant commits and gitignore integrity.
"""
import subprocess
from pathlib import Path
from typing import List

from ..config import OPENCLAW_REPO, WORKSPACE_REPO, GITIGNORE_REQUIRED, MAX_EVIDENCE_CHARS


def _git(repo: Path, *args, timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


def _check_gitignore(repo: Path) -> List[dict]:
    results = []
    gi_path = repo / ".gitignore"
    if not gi_path.exists():
        results.append({
            "section": "GitHistory",
            "status": "fail",
            "severity": "high",
            "label": f"{repo.name}/.gitignore",
            "detail": "missing — secrets may be committed",
        })
        return results

    try:
        content = gi_path.read_text(encoding="utf-8")
    except OSError:
        return results

    missing = []
    for entry in GITIGNORE_REQUIRED:
        if entry not in content:
            missing.append(entry)

    if missing:
        results.append({
            "section": "GitHistory",
            "status": "warn",
            "severity": "high",
            "label": f"{repo.name}/.gitignore missing entries",
            "detail": ", ".join(missing),
        })
    else:
        results.append({
            "section": "GitHistory",
            "status": "ok",
            "label": f"{repo.name}/.gitignore",
            "detail": "all critical entries present",
        })

    return results


def _check_recent_commits(repo: Path, label: str) -> List[dict]:
    results = []
    evidence_parts = []

    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        return []

    # Recent commits (last 7 days)
    rc, log_out = _git(repo, "log", "--oneline", "--since=7 days ago", "--all")
    if rc != 0:
        return []

    commits = log_out.splitlines() if log_out else []
    evidence_parts.append(f"{label}: {len(commits)} commits in last 7 days")

    # Security-relevant commits (touching auth, secrets, config)
    security_keywords = ["auth", "token", "secret", "key", ".env", "credential", "password"]
    sec_commits = []
    for commit in commits:
        if any(kw in commit.lower() for kw in security_keywords):
            sec_commits.append(commit[:80])

    if sec_commits:
        results.append({
            "section": "GitHistory",
            "status": "warn",
            "severity": "medium",
            "label": f"{label}: security-relevant commits",
            "detail": f"{len(sec_commits)} commits touching auth/secrets",
            "evidence": "\n".join(sec_commits)[:MAX_EVIDENCE_CHARS],
        })

    # Check for gitignore changes (potential weakening)
    rc, gi_log = _git(repo, "log", "--oneline", "--since=7 days ago", "--all", "--", ".gitignore")
    if rc == 0 and gi_log.strip():
        gi_commits = gi_log.strip().splitlines()
        results.append({
            "section": "GitHistory",
            "status": "warn",
            "severity": "medium",
            "label": f"{label}: .gitignore modified",
            "detail": f"{len(gi_commits)} recent change(s) — verify no entries removed",
        })

    # Check for newly added executable files
    rc, new_execs = _git(repo, "log", "--since=7 days ago", "--all",
                         "--diff-filter=A", "--name-only", "--pretty=format:", "--",
                         "*.sh", "*.py")
    if rc == 0 and new_execs.strip():
        new_files = [f for f in new_execs.strip().splitlines() if f.strip()]
        if new_files:
            evidence_parts.append(f"New executables: {', '.join(new_files[:10])}")

    if not results:
        results.append({
            "section": "GitHistory",
            "status": "ok",
            "label": f"{label}: recent commits",
            "detail": f"{len(commits)} commits, no security concerns",
        })

    return results


def run() -> List[dict]:
    results = []
    results.extend(_check_gitignore(OPENCLAW_REPO))
    results.extend(_check_recent_commits(OPENCLAW_REPO, "openclaw"))
    results.extend(_check_recent_commits(WORKSPACE_REPO, "workspace"))
    return results
