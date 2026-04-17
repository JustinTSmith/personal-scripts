from __future__ import annotations
"""
Security Council — heal actions.
Auto-executable: chmod 600, gitignore append.
Everything else: advisory with exact commands.
"""
import os
import subprocess
import logging
from pathlib import Path
from typing import List, Tuple

log = logging.getLogger("security_council.heal")


def enrich_results(results: List[dict]) -> List[dict]:
    enriched = []
    for r in results:
        r = dict(r)
        section = r.get("section", "")
        label = r.get("label", "")
        detail = r.get("detail", "")
        status = r.get("status", "ok")

        if status not in ("warn", "fail"):
            enriched.append(r)
            continue

        # File permissions
        if "permissions" in detail.lower() and ("644" in detail or "world" in detail.lower()):
            # Extract path from label or detail
            path = _extract_path(label, detail)
            if path:
                r["heal_action"] = f"Fix permissions to 600 on {path}"
                r["heal_cmd"] = f"chmod 600 {path}"
                r["drill_detail"] = (
                    f"File `{path}` has insecure permissions.\n"
                    f"Current: {detail}\n"
                    f"Required: 600 (owner read/write only)"
                )

        # Missing gitignore entries
        elif "gitignore" in label.lower() and "missing" in detail.lower():
            r["heal_action"] = "Add missing entry to .gitignore"
            r["drill_detail"] = f"The .gitignore is missing a critical entry:\n`{detail}`"

        # Plaintext secrets
        elif "plaintext" in detail.lower() or "plaintext" in label.lower():
            r["heal_action"] = "Move secret to environment variable or encrypted store"
            r["drill_detail"] = (
                f"Secret stored in plaintext: {label}\n"
                f"Recommended: use environment variables or a secrets manager"
            )

        # Exec/eval patterns
        elif section in ("CodeExec", "AI/Red Team") and ("exec" in detail.lower() or "eval" in detail.lower()):
            r["heal_action"] = "Review and sandbox or remove dangerous code execution patterns"
            r["drill_detail"] = detail

        # Agent tool restrictions
        elif "no tool restrictions" in detail.lower() or "unrestricted" in detail.lower():
            r["heal_action"] = "Add tool deny list for this agent in openclaw.json"
            r["drill_detail"] = (
                f"Agent has no tool restrictions: {label}\n"
                f"Add: \"tools\": {{\"deny\": [\"code_execution\"]}} to the agent config"
            )

        # Key rotation
        elif "rotate" in detail.lower() or "rotation" in detail.lower() or "shared" in detail.lower():
            r["heal_action"] = "Rotate this key and use unique keys per service"
            r["drill_detail"] = detail

        enriched.append(r)
    return enriched


def execute_heal(item: dict) -> Tuple[bool, str]:
    heal_cmd = item.get("heal_cmd", "")
    heal_action = item.get("heal_action", "")

    if not heal_cmd and not heal_action:
        return False, "No heal action defined for this item"

    # chmod — auto-executable
    if heal_cmd and heal_cmd.startswith("chmod 600 "):
        path = heal_cmd.split("chmod 600 ", 1)[1].strip()
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            try:
                os.chmod(expanded, 0o600)
                log.info("Fixed permissions on %s", expanded)
                return True, f"Set permissions to 600 on {path}"
            except OSError as e:
                return False, f"chmod failed: {e}"
        return False, f"File not found: {path}"

    # gitignore append — auto-executable
    if heal_cmd and heal_cmd.startswith("echo ") and ".gitignore" in heal_cmd:
        try:
            result = subprocess.run(
                heal_cmd, shell=True, capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return True, "Added entry to .gitignore"
            return False, f"gitignore append failed: {result.stderr[:80]}"
        except Exception as e:
            return False, str(e)

    # Everything else: advisory
    if heal_cmd:
        return False, f"Run manually:\n`{heal_cmd}`"
    return False, f"Manual action required: {heal_action}"


def _extract_path(label: str, detail: str) -> str | None:
    """Try to extract a file path from label or detail."""
    for text in [label, detail]:
        for word in text.split():
            if "/" in word and not word.startswith("("):
                clean = word.strip(":`'\"")
                if clean.startswith("/") or clean.startswith("~"):
                    return clean
    return None
