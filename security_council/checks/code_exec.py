from __future__ import annotations
"""
Code execution scanner: finds exec/eval/subprocess patterns in skills and workspace.
"""
import re
from pathlib import Path
from typing import List

from ..config import SKILLS_DIR, WORKSPACE_SKILLS_DIR, WORKSPACE_DIR, MAX_EVIDENCE_CHARS

DANGEROUS_PATTERNS = [
    (re.compile(r'\bexec\s*\('), "exec()"),
    (re.compile(r'\beval\s*\('), "eval()"),
    (re.compile(r'\bsubprocess\.(?:run|Popen|call|check_output)\s*\('), "subprocess"),
    (re.compile(r'\bos\.system\s*\('), "os.system()"),
    (re.compile(r'\b__import__\s*\('), "__import__()"),
    (re.compile(r'\bimportlib\.import_module\s*\('), "importlib"),
]

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "site-packages", ".tox", ".eggs",
}


def _scan_directory(base: Path) -> List[dict]:
    """Scan all .py files under base for dangerous patterns."""
    findings = []
    if not base.exists():
        return []

    py_files = []
    for py_file in base.rglob("*.py"):
        # Skip excluded dirs
        parts = py_file.parts
        if any(skip in parts for skip in SKIP_DIRS):
            continue
        py_files.append(py_file)

    for py_file in py_files[:200]:  # cap at 200 files
        try:
            lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern, label in DANGEROUS_PATTERNS:
                if pattern.search(stripped):
                    findings.append({
                        "file": str(py_file),
                        "line": i,
                        "pattern": label,
                        "code": stripped[:120],
                    })

    return findings


def run() -> List[dict]:
    results = []
    evidence_parts = []

    # Scan skills directories
    all_findings = []
    for scan_dir, label in [
        (SKILLS_DIR, "openclaw/skills"),
        (WORKSPACE_SKILLS_DIR, "workspace/skills"),
    ]:
        if scan_dir.exists():
            findings = _scan_directory(scan_dir)
            all_findings.extend(findings)
            for f in findings[:10]:
                short_path = f["file"].replace(str(scan_dir), label)
                evidence_parts.append(f"{short_path}:{f['line']} — {f['pattern']}: {f['code'][:80]}")

    # Scan workspace scripts
    scripts_findings = _scan_directory(WORKSPACE_DIR / "scripts")
    all_findings.extend(scripts_findings)

    # Group by pattern type
    pattern_counts: dict = {}
    for f in all_findings:
        p = f["pattern"]
        pattern_counts[p] = pattern_counts.get(p, 0) + 1

    if all_findings:
        # Summary result
        summary = ", ".join(f"{p}×{c}" for p, c in sorted(pattern_counts.items(), key=lambda x: -x[1]))
        severity = "high" if pattern_counts.get("exec()", 0) > 0 or pattern_counts.get("eval()", 0) > 0 else "medium"
        results.append({
            "section": "CodeExec",
            "status": "warn",
            "severity": severity,
            "label": f"dangerous patterns ({len(all_findings)} matches)",
            "detail": summary,
            "evidence": "\n".join(evidence_parts)[:MAX_EVIDENCE_CHARS],
        })

        # Top 5 individual findings for drill detail
        for f in all_findings[:5]:
            results.append({
                "section": "CodeExec",
                "status": "warn",
                "severity": "medium",
                "label": f"  {Path(f['file']).name}:{f['line']}",
                "detail": f"{f['pattern']}: {f['code'][:80]}",
            })
    else:
        results.append({
            "section": "CodeExec",
            "status": "ok",
            "label": "code execution patterns",
            "detail": "no exec/eval/subprocess found in skills",
        })

    return results
