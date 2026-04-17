from __future__ import annotations
"""
Permissions audit: LaunchAgent privileges, agent tool ACLs, MCP auth, file permissions.
"""
import json
import os
import plistlib
import stat
from pathlib import Path
from typing import List

from ..config import (
    LAUNCHAGENTS_DIR, OPENCLAW_JSON, AGENTS_DIR,
    OPENCLAW_DIR, MAX_EVIDENCE_CHARS,
)


def _scan_launchagents() -> List[dict]:
    results = []
    evidence_parts = []

    if not LAUNCHAGENTS_DIR.exists():
        return [{
            "section": "Permissions",
            "status": "warn",
            "label": "LaunchAgents",
            "detail": "directory not found",
        }]

    plists = sorted(LAUNCHAGENTS_DIR.glob("*.plist"))
    openclaw_plists = [
        p for p in plists
        if "openclaw" in p.name.lower() or "justinsmith" in p.name.lower()
    ]

    for plist_path in openclaw_plists:
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)

            label = data.get("Label", plist_path.stem)
            env_keys = list(data.get("EnvironmentVariables", {}).keys())
            umask = data.get("Umask")
            keep_alive = data.get("KeepAlive", False)

            issues = []
            if env_keys:
                sensitive_env = [k for k in env_keys if any(
                    s in k.lower() for s in ["token", "key", "secret", "password"]
                )]
                if sensitive_env:
                    issues.append(f"env secrets: {', '.join(sensitive_env)}")

            if umask is not None and umask < 63:  # 077 octal = 63 decimal
                issues.append(f"umask too permissive: {oct(umask)}")

            info = f"{label}: " + (", ".join(issues) if issues else "OK")
            evidence_parts.append(info)

            if issues:
                results.append({
                    "section": "Permissions",
                    "status": "warn",
                    "severity": "medium",
                    "label": f"LaunchAgent: {label}",
                    "detail": "; ".join(issues),
                })

        except Exception as e:
            evidence_parts.append(f"{plist_path.name}: parse error: {e}")

    if not any(r.get("section") == "Permissions" for r in results):
        results.append({
            "section": "Permissions",
            "status": "ok",
            "label": f"LaunchAgents ({len(openclaw_plists)} scanned)",
            "detail": "no privilege issues",
        })

    # Attach evidence
    if results:
        results[0]["evidence"] = "\n".join(evidence_parts)[:MAX_EVIDENCE_CHARS]

    return results


def _scan_agent_tool_acls() -> List[dict]:
    results = []
    evidence_parts = []

    if not OPENCLAW_JSON.exists():
        return []

    try:
        data = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    agents = data.get("agents", {}).get("list", [])
    for agent in agents:
        aid = agent.get("id", "?")
        tools = agent.get("tools", {})
        allow = tools.get("allow", [])
        deny = tools.get("deny", [])

        if "exec" in allow:
            results.append({
                "section": "Permissions",
                "status": "warn",
                "severity": "high",
                "label": f"agent:{aid} has exec",
                "detail": f"allow={allow}, deny={deny or 'none'}",
            })
            evidence_parts.append(f"RISK: {aid} allow={allow} deny={deny}")
        elif not tools:
            evidence_parts.append(f"UNRESTRICTED: {aid} (no tool config)")
        else:
            evidence_parts.append(f"OK: {aid} deny={deny}")

    # Check for agents with no tool config at all
    unrestricted = [a["id"] for a in agents if not a.get("tools")]
    if unrestricted:
        results.append({
            "section": "Permissions",
            "status": "warn",
            "severity": "medium",
            "label": "agents with no tool restrictions",
            "detail": ", ".join(unrestricted),
        })

    if not results:
        results.append({
            "section": "Permissions",
            "status": "ok",
            "label": f"agent tool ACLs ({len(agents)} agents)",
            "detail": "all have appropriate restrictions",
        })

    if results:
        results[0].setdefault("evidence", "\n".join(evidence_parts)[:MAX_EVIDENCE_CHARS])

    return results


def _scan_mcp_auth() -> List[dict]:
    results = []
    evidence_parts = []

    if not OPENCLAW_JSON.exists():
        return []

    try:
        data = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    mcp_servers = data.get("mcp", {}).get("servers", {})
    for name, config in mcp_servers.items():
        headers = config.get("headers", {})
        has_plaintext = any(
            len(str(v)) > 16 and not str(v).startswith("$")
            for v in headers.values()
        )
        if has_plaintext:
            evidence_parts.append(f"PLAINTEXT: {name} has inline auth headers")
            results.append({
                "section": "Permissions",
                "status": "warn",
                "severity": "medium",
                "label": f"MCP:{name} plaintext auth",
                "detail": "auth tokens stored inline in config (not env vars)",
            })
        else:
            evidence_parts.append(f"OK: {name}")

    if not results:
        results.append({
            "section": "Permissions",
            "status": "ok",
            "label": f"MCP auth ({len(mcp_servers)} servers)",
            "detail": "no plaintext auth issues",
        })

    if results:
        results[0].setdefault("evidence", "\n".join(evidence_parts)[:MAX_EVIDENCE_CHARS])

    return results


def _scan_sensitive_file_permissions() -> List[dict]:
    results = []
    sensitive_files = [
        OPENCLAW_JSON,
        OPENCLAW_DIR / ".env",
        OPENCLAW_DIR / "credentials" / "gcp-oauth.keys.json",
    ]
    # Also check auth files in agents
    for agent_dir in AGENTS_DIR.iterdir() if AGENTS_DIR.exists() else []:
        if agent_dir.is_dir():
            for auth_file in agent_dir.rglob("auth-*.json"):
                sensitive_files.append(auth_file)

    for sf in sensitive_files:
        if not sf.exists():
            continue
        st = sf.stat()
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:  # group or other can access
            results.append({
                "section": "Permissions",
                "status": "warn",
                "severity": "high" if "credential" in str(sf) or ".env" in str(sf) else "medium",
                "label": f"file perms: {sf}",
                "detail": f"mode {oct(mode)} (should be 0o600)",
            })

    return results


def run() -> List[dict]:
    results = []
    results.extend(_scan_launchagents())
    results.extend(_scan_agent_tool_acls())
    results.extend(_scan_mcp_auth())
    results.extend(_scan_sensitive_file_permissions())
    return results
