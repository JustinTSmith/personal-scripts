from __future__ import annotations
"""
Coverage baseline:
- Generate / update a JSON coverage report tracking platform health metrics
- Agents with system prompts (%)
- Skills with SKILL.md (%)
- Cron success rate (last 48h)
- Workspace docs presence (%)
Saves to coverage/baseline.json and returns a summary result.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from ..config import (
    AGENTS_DIR,
    AGENTS_WITH_SYSTEM,
    SKILLS_DIR,
    TASKS_DB,
    WORKSPACE_DOCS,
    COVERAGE_FILE,
    COVERAGE_DIR,
)

# Workspace skills fallback
WORKSPACE_SKILLS = Path.home() / "Workspace" / "skills"


def _agent_coverage() -> dict:
    if not AGENTS_DIR.exists():
        return {"total": 0, "with_system": 0, "pct": 0.0}

    total = 0
    with_system = 0

    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        sub = agent_dir / "agent"
        if not sub.exists():
            continue
        total += 1
        if agent_dir.name in AGENTS_WITH_SYSTEM:
            system_md = sub / "system.md"
            if system_md.exists() and system_md.stat().st_size >= 50:
                with_system += 1

    pct = (with_system / len(AGENTS_WITH_SYSTEM) * 100) if AGENTS_WITH_SYSTEM else 100.0
    return {"total": total, "required": len(AGENTS_WITH_SYSTEM), "with_system": with_system, "pct": round(pct, 1)}


def _skills_coverage() -> dict:
    skills_base = SKILLS_DIR if SKILLS_DIR.exists() else WORKSPACE_SKILLS
    if SKILLS_DIR.is_symlink():
        skills_base = SKILLS_DIR.resolve()

    if not skills_base.exists():
        return {"total": 0, "with_skill_md": 0, "pct": 0.0}

    total = 0
    with_md = 0
    SKIP_DIRS = {".git", ".claude", ".github", "_library", "__pycache__"}

    for d in skills_base.iterdir():
        if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
            continue
        total += 1
        skill_md = d / "SKILL.md"
        if skill_md.exists() and skill_md.stat().st_size >= 50:
            with_md += 1

    pct = (with_md / total * 100) if total > 0 else 0.0
    return {"total": total, "with_skill_md": with_md, "pct": round(pct, 1)}


def _cron_success_rate() -> dict:
    if not TASKS_DB.exists():
        return {"total": 0, "success": 0, "failed": 0, "pct": None}

    try:
        uri = f"file:{TASKS_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row

        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

        if "task_runs" not in tables:
            conn.close()
            return {"total": 0, "success": 0, "failed": 0, "pct": None}

        import time as _time
        cutoff_ms = int((_time.time() - 48 * 3600) * 1000)
        row = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status IN ('failed','lost','timed_out') THEN 1 ELSE 0 END) as failed
            FROM task_runs WHERE created_at > ?
            """,
            (cutoff_ms,),
        ).fetchone()

        conn.close()
        total = row[0] or 0
        success = row[1] or 0
        failed = row[2] or 0
        pct = round(success / total * 100, 1) if total > 0 else None
        return {"total": total, "success": success, "failed": failed, "pct": pct}

    except (sqlite3.Error, Exception):
        return {"total": 0, "success": 0, "failed": 0, "pct": None}


def _docs_coverage() -> dict:
    total = len(WORKSPACE_DOCS)
    present = sum(1 for p in WORKSPACE_DOCS.values() if p.exists() and p.stat().st_size >= 50)
    pct = round(present / total * 100, 1) if total > 0 else 0.0
    return {"total": total, "present": present, "pct": pct}


def _save_baseline(data: dict) -> None:
    COVERAGE_DIR.mkdir(parents=True, exist_ok=True)
    COVERAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_previous() -> dict:
    if COVERAGE_FILE.exists():
        try:
            return json.loads(COVERAGE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def run() -> List[dict]:
    results = []
    now = datetime.now().isoformat()

    agents = _agent_coverage()
    skills = _skills_coverage()
    crons = _cron_success_rate()
    docs = _docs_coverage()

    current = {
        "generated_at": now,
        "agents": agents,
        "skills": skills,
        "crons": crons,
        "docs": docs,
    }

    # Compare to previous baseline
    previous = _load_previous()
    regressions = []

    if previous:
        if agents["pct"] < previous.get("agents", {}).get("pct", 100):
            regressions.append(f"agents: {previous['agents']['pct']}% → {agents['pct']}%")
        if skills["pct"] < previous.get("skills", {}).get("pct", 100):
            regressions.append(f"skills: {previous['skills']['pct']}% → {skills['pct']}%")

    # Save updated baseline
    try:
        _save_baseline(current)
        save_ok = True
    except OSError:
        save_ok = False

    # Agent coverage result
    pct = agents["pct"]
    results.append({
        "section": "Coverage",
        "status": "ok" if pct >= 100 else ("warn" if pct >= 75 else "fail"),
        "label": "agent system prompts",
        "detail": f"{agents['with_system']}/{agents['required']} ({pct}%)",
    })

    # Skills coverage result
    spct = skills["pct"]
    results.append({
        "section": "Coverage",
        "status": "ok" if spct >= 80 else ("warn" if spct >= 50 else "fail"),
        "label": "skill SKILL.md",
        "detail": f"{skills['with_skill_md']}/{skills['total']} ({spct}%)",
    })

    # Cron success rate
    if crons["pct"] is not None:
        cpct = crons["pct"]
        results.append({
            "section": "Coverage",
            "status": "ok" if cpct >= 90 else ("warn" if cpct >= 70 else "fail"),
            "label": "cron success rate (48h)",
            "detail": f"{cpct}% ({crons['success']}/{crons['total']} runs)",
        })

    # Docs coverage
    dpct = docs["pct"]
    results.append({
        "section": "Coverage",
        "status": "ok" if dpct >= 100 else "warn",
        "label": "workspace docs",
        "detail": f"{docs['present']}/{docs['total']} ({dpct}%)",
    })

    # Regression warnings
    for r in regressions:
        results.append({
            "section": "Coverage",
            "status": "warn",
            "label": "regression",
            "detail": r,
        })

    # Baseline save status
    if not save_ok:
        results.append({
            "section": "Coverage",
            "status": "warn",
            "label": "baseline save",
            "detail": f"could not write to {COVERAGE_FILE}",
        })

    return results
