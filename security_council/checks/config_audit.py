"""
Config audit: openclaw.json structure, duplicate keys, gateway auth, MCP config.
"""
import json
import re
from pathlib import Path
from typing import List

from ..config import OPENCLAW_JSON, MAX_EVIDENCE_CHARS


def _deep_extract_values(obj, key_pattern: re.Pattern, path: str = "") -> List[tuple]:
    """Recursively extract (path, value) pairs matching key pattern."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            if key_pattern.search(k) and isinstance(v, str) and len(v) > 10:
                found.append((new_path, v))
            found.extend(_deep_extract_values(v, key_pattern, new_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_deep_extract_values(item, key_pattern, f"{path}[{i}]"))
    return found


def run() -> List[dict]:
    results = []
    evidence_parts = []

    if not OPENCLAW_JSON.exists():
        return [{
            "section": "ConfigAudit",
            "status": "fail",
            "severity": "critical",
            "label": "openclaw.json",
            "detail": "missing",
        }]

    try:
        raw = OPENCLAW_JSON.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return [{
            "section": "ConfigAudit",
            "status": "fail",
            "severity": "critical",
            "label": "openclaw.json",
            "detail": f"invalid JSON: {str(e)[:60]}",
        }]

    # 1. Gateway auth
    gateway = data.get("gateway", {})
    auth = gateway.get("auth", {})
    token = auth.get("token", "")
    if token:
        if len(token) < 32:
            results.append({
                "section": "ConfigAudit",
                "status": "warn",
                "severity": "high",
                "label": "gateway auth token",
                "detail": f"too short ({len(token)} chars, recommend ≥32)",
            })
        else:
            results.append({
                "section": "ConfigAudit",
                "status": "ok",
                "label": "gateway auth token",
                "detail": f"{len(token)} chars (adequate)",
            })
        evidence_parts.append(f"Gateway auth: mode={auth.get('mode','?')}, token_len={len(token)}")
    else:
        results.append({
            "section": "ConfigAudit",
            "status": "fail",
            "severity": "critical",
            "label": "gateway auth",
            "detail": "no auth token configured",
        })

    # 2. Duplicate secrets across services
    secret_pattern = re.compile(r'(?i)(apiKey|token|secret|password|key\b)')
    all_secrets = _deep_extract_values(data, secret_pattern)

    # Group by value to find duplicates
    value_to_paths: dict = {}
    for path, val in all_secrets:
        # Redact for evidence but track duplicates
        val_hash = val[:8] + "..." + val[-4:]  # fingerprint
        value_to_paths.setdefault(val_hash, []).append(path)

    duplicates = {k: v for k, v in value_to_paths.items() if len(v) > 1}
    if duplicates:
        dup_details = []
        for fingerprint, paths in duplicates.items():
            dup_details.append(f"shared across: {', '.join(p.split('.')[-1] for p in paths[:3])}")
        results.append({
            "section": "ConfigAudit",
            "status": "warn",
            "severity": "medium",
            "label": f"duplicate keys ({len(duplicates)} shared)",
            "detail": "; ".join(dup_details[:3]),
        })
        evidence_parts.append(f"Duplicate keys: {len(duplicates)} groups")
    else:
        results.append({
            "section": "ConfigAudit",
            "status": "ok",
            "label": "key uniqueness",
            "detail": "no duplicate secrets across services",
        })

    # 3. Agent security audit
    agents = data.get("agents", {}).get("list", [])
    no_tools_agents = [a["id"] for a in agents if not a.get("tools")]
    exec_agents = [
        a["id"] for a in agents
        if "exec" in a.get("tools", {}).get("allow", [])
    ]

    if exec_agents:
        results.append({
            "section": "ConfigAudit",
            "status": "warn",
            "severity": "high",
            "label": "agents with exec access",
            "detail": ", ".join(exec_agents),
        })
        evidence_parts.append(f"Exec agents: {', '.join(exec_agents)}")

    if no_tools_agents:
        evidence_parts.append(f"Unrestricted agents: {', '.join(no_tools_agents)}")

    # 4. MCP server transport security
    mcp_servers = data.get("mcp", {}).get("servers", {})
    for name, config in mcp_servers.items():
        transport = config.get("type", config.get("transport", "stdio"))
        url = config.get("url", "")
        if transport in ("sse", "streamable-http") and url.startswith("http://"):
            results.append({
                "section": "ConfigAudit",
                "status": "warn",
                "severity": "medium",
                "label": f"MCP:{name} unencrypted",
                "detail": f"uses HTTP (not HTTPS): {url[:60]}",
            })

    # 5. Config size sanity
    size_kb = len(raw) // 1024
    evidence_parts.append(f"Config: {size_kb}KB, {len(agents)} agents, {len(mcp_servers)} MCP servers")

    # Attach evidence
    if results:
        results[0].setdefault("evidence", "\n".join(evidence_parts)[:MAX_EVIDENCE_CHARS])

    return results
