from __future__ import annotations
"""
Config file health checks:
- Workspace docs existence and non-emptiness (AGENTS.md, SOUL.md, USER.md, etc.)
- openclaw.json JSON validity
- Agent system prompt presence for required agents
- Agent agentDir existence
"""
import json
from pathlib import Path
from typing import List

from ..config import (
    OPENCLAW_JSON,
    WORKSPACE_DOCS,
    AGENTS_DIR,
    AGENTS_WITH_SYSTEM,
)

MIN_DOC_SIZE = 100  # bytes


def _check_workspace_docs() -> List[dict]:
    results = []

    for doc_name, doc_path in WORKSPACE_DOCS.items():
        if not doc_path.exists():
            results.append({
                "section": "Configs",
                "status": "warn",
                "label": doc_name,
                "detail": f"missing at {doc_path}",
            })
        elif doc_path.stat().st_size < MIN_DOC_SIZE:
            results.append({
                "section": "Configs",
                "status": "warn",
                "label": doc_name,
                "detail": f"suspiciously small ({doc_path.stat().st_size}B)",
            })
        else:
            results.append({
                "section": "Configs",
                "status": "ok",
                "label": doc_name,
                "detail": f"{doc_path.stat().st_size // 1024}KB",
            })

    return results


def _check_openclaw_json() -> List[dict]:
    results = []

    if not OPENCLAW_JSON.exists():
        results.append({
            "section": "Configs",
            "status": "fail",
            "label": "openclaw.json",
            "detail": "missing",
        })
        return results

    try:
        raw = OPENCLAW_JSON.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        results.append({
            "section": "Configs",
            "status": "fail",
            "label": "openclaw.json",
            "detail": f"invalid JSON: {str(e)[:60]}",
        })
        return results
    except OSError as e:
        results.append({
            "section": "Configs",
            "status": "fail",
            "label": "openclaw.json",
            "detail": str(e)[:60],
        })
        return results

    size_kb = len(raw) // 1024
    agent_count = len(data.get("agents", {}).get("list", []))
    results.append({
        "section": "Configs",
        "status": "ok",
        "label": "openclaw.json",
        "detail": f"valid JSON, {size_kb}KB, {agent_count} agents",
    })

    # Check MCP servers configured
    mcp_servers = list(data.get("mcp", {}).get("servers", {}).keys())
    if mcp_servers:
        results.append({
            "section": "Configs",
            "status": "ok",
            "label": "MCP servers",
            "detail": ", ".join(mcp_servers),
        })
    else:
        results.append({
            "section": "Configs",
            "status": "warn",
            "label": "MCP servers",
            "detail": "none configured",
        })

    # Check Telegram channels
    telegram_directs = list(
        data.get("channels", {})
            .get("telegram", {})
            .get("direct", {})
            .keys()
    )
    results.append({
        "section": "Configs",
        "status": "ok",
        "label": "Telegram channels",
        "detail": f"{len(telegram_directs)} configured",
    })

    return results


def _check_agent_dirs() -> List[dict]:
    results = []

    if not AGENTS_DIR.exists():
        results.append({
            "section": "Configs",
            "status": "fail",
            "label": "agents dir",
            "detail": f"missing at {AGENTS_DIR}",
        })
        return results

    agent_dirs = sorted([d for d in AGENTS_DIR.iterdir() if d.is_dir()])
    ok_count = 0
    missing_system = []

    for agent_dir in agent_dirs:
        agent_name = agent_dir.name
        agent_subdir = agent_dir / "agent"

        if not agent_subdir.exists():
            continue  # Not a standard agent dir structure

        # Check system prompt for required agents
        if agent_name in AGENTS_WITH_SYSTEM:
            system_md = agent_subdir / "system.md"
            if not system_md.exists() or system_md.stat().st_size < 50:
                missing_system.append(agent_name)
            else:
                ok_count += 1
        else:
            ok_count += 1

    if missing_system:
        results.append({
            "section": "Configs",
            "status": "warn",
            "label": "agent system prompts",
            "detail": f"missing for: {', '.join(missing_system)}",
        })
    else:
        results.append({
            "section": "Configs",
            "status": "ok",
            "label": f"agents ({len(agent_dirs)} total)",
            "detail": f"all required system prompts present",
        })

    return results


def run() -> List[dict]:
    results = []
    results.extend(_check_openclaw_json())
    results.extend(_check_workspace_docs())
    results.extend(_check_agent_dirs())
    return results
