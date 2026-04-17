from __future__ import annotations
"""
Skills health checks:
- Iterate all skills directories
- Verify SKILL.md exists and is non-empty for each skill
- Detect skills with empty or missing metadata
"""
import os
from pathlib import Path
from typing import List

from ..config import SKILLS_DIR, OPENCLAW_DIR

# Canonical skills dir (Workspace)
WORKSPACE_SKILLS_DIR = Path.home() / "Workspace" / "skills"

# Minimum SKILL.md size in bytes to be considered non-empty
MIN_SKILL_MD_BYTES = 50


def _scan_skills_dir(base: Path, label_prefix: str) -> List[dict]:
    results = []

    if not base.exists():
        results.append({
            "section": "Skills",
            "status": "warn",
            "label": f"{label_prefix} dir",
            "detail": f"not found at {base}",
        })
        return results

    # Each subdirectory is a skill — skip hidden dirs and known non-skill dirs
    SKIP_DIRS = {".git", ".claude", ".github", "_library", "__pycache__", ".DS_Store"}
    skill_dirs = sorted([
        d for d in base.iterdir()
        if d.is_dir() and d.name not in SKIP_DIRS and not d.name.startswith(".")
    ])

    if not skill_dirs:
        results.append({
            "section": "Skills",
            "status": "warn",
            "label": f"{label_prefix}",
            "detail": "no skill directories found",
        })
        return results

    ok_count = 0
    missing_skill_md = []
    empty_skill_md = []

    for skill_dir in skill_dirs:
        skill_name = skill_dir.name
        skill_md = skill_dir / "SKILL.md"

        if not skill_md.exists():
            missing_skill_md.append(skill_name)
        elif skill_md.stat().st_size < MIN_SKILL_MD_BYTES:
            empty_skill_md.append(skill_name)
        else:
            ok_count += 1

    total = len(skill_dirs)

    # Summary result
    if missing_skill_md or empty_skill_md:
        issues = []
        if missing_skill_md:
            issues.append(f"{len(missing_skill_md)} missing SKILL.md")
        if empty_skill_md:
            issues.append(f"{len(empty_skill_md)} empty SKILL.md")

        status = "warn" if ok_count > 0 else "fail"
        results.append({
            "section": "Skills",
            "status": status,
            "label": f"{label_prefix} ({total} total)",
            "detail": f"{ok_count} ok, {', '.join(issues)}",
        })

        # List the worst offenders (limit to 5)
        bad_skills = (missing_skill_md + empty_skill_md)[:5]
        for s in bad_skills:
            results.append({
                "section": "Skills",
                "status": "warn",
                "label": f"  {label_prefix}/{s}",
                "detail": "no SKILL.md" if s in missing_skill_md else "SKILL.md too small",
            })
    else:
        results.append({
            "section": "Skills",
            "status": "ok",
            "label": f"{label_prefix} ({total} total)",
            "detail": f"all {ok_count} have SKILL.md",
        })

    return results


def run() -> List[dict]:
    results = []

    # Check the OpenClaw skills dir (may be a symlink to Workspace/skills)
    if SKILLS_DIR.exists():
        if SKILLS_DIR.is_symlink():
            target = SKILLS_DIR.resolve()
            results.append({
                "section": "Skills",
                "status": "ok",
                "label": "openclaw/skills symlink",
                "detail": f"→ {target}",
            })
            results.extend(_scan_skills_dir(target, "skills"))
        else:
            results.extend(_scan_skills_dir(SKILLS_DIR, "openclaw/skills"))
    elif WORKSPACE_SKILLS_DIR.exists():
        # Fall back to workspace skills
        results.extend(_scan_skills_dir(WORKSPACE_SKILLS_DIR, "workspace/skills"))
    else:
        results.append({
            "section": "Skills",
            "status": "warn",
            "label": "skills",
            "detail": "neither openclaw/skills nor workspace/skills found",
        })

    return results
