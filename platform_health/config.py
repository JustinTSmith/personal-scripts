from __future__ import annotations
"""
Central path and constant definitions for platform_health.
All checks import from here — no hardcoded paths elsewhere.
"""
from pathlib import Path
from datetime import datetime

# ── Root dirs ────────────────────────────────────────────────────────────────
HOME             = Path.home()
OPENCLAW_DIR     = HOME / ".openclaw"
WORKSPACE_DIR    = HOME / "Workspace"
SCRIPTS_DIR      = WORKSPACE_DIR / "scripts"
LAUNCHAGENTS_DIR = HOME / "Library" / "LaunchAgents"

# ── OpenClaw internals ────────────────────────────────────────────────────────
LOGS_DIR         = OPENCLAW_DIR / "logs"
GATEWAY_LOG      = LOGS_DIR / "gateway.log"
GATEWAY_ERR_LOG  = LOGS_DIR / "gateway.err.log"
GATEWAY_LOG_TMP  = Path(f"/tmp/openclaw/openclaw-{datetime.now().strftime('%Y-%m-%d')}.log")
OPENCLAW_JSON    = OPENCLAW_DIR / "openclaw.json"
AGENTS_DIR       = OPENCLAW_DIR / "agents"
SKILLS_DIR       = OPENCLAW_DIR / "skills"
FLOWS_DB         = OPENCLAW_DIR / "flows" / "registry.sqlite"
TASKS_DB         = OPENCLAW_DIR / "tasks" / "runs.sqlite"

# ── Git repos ─────────────────────────────────────────────────────────────────
OPENCLAW_REPO    = OPENCLAW_DIR          # ~/.openclaw  (backed up to GitHub)
WORKSPACE_REPO   = OPENCLAW_DIR / "workspace"

# ── Workspace docs ────────────────────────────────────────────────────────────
WORKSPACE_DOCS = {
    "AGENTS.md":   WORKSPACE_REPO / "AGENTS.md",
    "SOUL.md":     WORKSPACE_REPO / "SOUL.md",
    "USER.md":     WORKSPACE_REPO / "USER.md",
    "MEMORY.md":   WORKSPACE_REPO / "MEMORY.md",
    "IDENTITY.md": WORKSPACE_REPO / "IDENTITY.md",
    "TOOLS.md":    WORKSPACE_REPO / "TOOLS.md",
}

# ── Agent system prompts expected ─────────────────────────────────────────────
AGENTS_WITH_SYSTEM = ["operator", "accountability", "coach", "reasoning"]

# ── LaunchAgents to verify ────────────────────────────────────────────────────
CRITICAL_LAUNCHAGENTS = [
    "ai.openclaw.gateway",
    "com.justinsmith.open-brain-server",
    "com.justinsmith.obsidian-voice-processor",
    "com.justinsmith.ob1-cascade-check",
    "com.justinsmith.ob1-evidence-scan",
    "com.justinsmith.nerve",
    "com.justinsmith.gmail-automation",
]

# ── Gmail automation ──────────────────────────────────────────────────────────
GMAIL_AUTOMATION_DIR  = SCRIPTS_DIR / "gmail-automation"
GMAIL_LOG             = GMAIL_AUTOMATION_DIR / "gmail-automation.log"
GMAIL_ERR_LOG         = GMAIL_AUTOMATION_DIR / "gmail-automation-error.log"
GMAIL_STALE_HOURS     = 4  # alert if log hasn't been written in this many hours

# ── Coverage output ───────────────────────────────────────────────────────────
COVERAGE_DIR     = SCRIPTS_DIR / "platform_health" / "coverage"
COVERAGE_FILE    = COVERAGE_DIR / "baseline.json"

# ── Telegram ──────────────────────────────────────────────────────────────────
PLATFORM_HEALTH_CHAT_ID = "-5102137780"
TELEGRAM_MSG_LIMIT      = 4000   # chars per message (Telegram max is 4096)

# ── Health report log ─────────────────────────────────────────────────────────
HEALTH_LOG = Path("/tmp/openclaw/platform_health.log")
