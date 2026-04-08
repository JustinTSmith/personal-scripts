#!/usr/bin/env python3
"""
_generate_manifest.py
Regenerates SCRIPTS.md from the current state of the scripts directory.

Reads the first meaningful description line from each script's companion
README (.md file named after the script, or README.md for subdirectories).

Usage:
    python3 _generate_manifest.py
"""

import os
import re
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
OUTPUT = SCRIPTS_DIR / "SCRIPTS.md"

IGNORED = {
    "_generate_manifest.py", "SCRIPTS.md", ".DS_Store", ".env", ".envrc",
    "automation.log", "state.db", "token.json", "credentials.json",
    "opportunity_log.jsonl", "prices.json",
}
IGNORED_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    "logs", "assets", "voices",
}
SCRIPT_EXTS = {".py", ".sh", ".js", ".ts", ".rb"}


def first_description(readme_path: Path) -> str:
    """Extract the first non-header, non-empty line from a README."""
    if not readme_path.exists():
        return ""
    try:
        text = readme_path.read_text(errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("|") or line.startswith("---"):
                continue
            # Strip markdown bold/italic
            line = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", line)
            return line[:120]
    except Exception:
        return ""
    return ""


def collect_root_scripts():
    scripts = []
    for f in sorted(SCRIPTS_DIR.iterdir()):
        if f.is_file() and f.suffix in SCRIPT_EXTS and f.name not in IGNORED:
            readme = SCRIPTS_DIR / (f.stem + ".md")
            desc = first_description(readme) or "—"
            scripts.append((f.name, f.suffix, desc))
    return scripts


def collect_subdirs():
    subdirs = []
    for d in sorted(SCRIPTS_DIR.iterdir()):
        if d.is_dir() and d.name not in IGNORED_DIRS and not d.name.startswith("."):
            readme = d / "README.md"
            desc = first_description(readme) or "—"
            # Find likely entry point
            entry = "—"
            for candidate in ["main.py", "server.py", "app.py", "index.js",
                               "voice_loop.py", "crawler.py"]:
                if (d / candidate).exists():
                    entry = f"python3 {candidate}" if candidate.endswith(".py") else f"node {candidate}"
                    break
            subdirs.append((d.name, desc, entry))
    return subdirs


def build_manifest():
    root_scripts = collect_root_scripts()
    subdirs = collect_subdirs()
    today = date.today().isoformat()

    lines = [
        "# Scripts Manifest",
        "> Auto-maintained master list of all scripts in this directory.",
        "> Run `python3 _generate_manifest.py` to regenerate from current state.",
        f"> Last updated: {today}",
        "",
        "---",
        "",
        "## Root Scripts",
        "",
        "| Script | Type | Description |",
        "|--------|------|-------------|",
    ]
    for name, ext, desc in root_scripts:
        type_label = {"py": "Python", "sh": "Shell", "js": "Node.js", "ts": "TypeScript"}.get(ext.lstrip("."), ext)
        lines.append(f"| `{name}` | {type_label} | {desc} |")

    lines += [
        "",
        "---",
        "",
        "## Subdirectory Projects",
        "",
        "| Directory | Description | Entry Point |",
        "|-----------|-------------|-------------|",
    ]
    for name, desc, entry in subdirs:
        lines.append(f"| `{name}/` | {desc} | `{entry}` |")

    lines += [
        "",
        "---",
        "",
        "## Adding a New Script",
        "",
        "1. Create the script in this directory (or a subdirectory)",
        "2. Create a companion `.md` README named after the script (e.g. `myscript.md` or `mydir/README.md`)",
        "3. Run `python3 _generate_manifest.py` to update this file",
        "4. The updated `SCRIPTS.md` is automatically reflected in OpenClaw agent memory",
    ]

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    content = build_manifest()
    OUTPUT.write_text(content)
    print(f"✓ Updated {OUTPUT}")
