from __future__ import annotations
"""
Security Council — constants and paths.
"""
from datetime import datetime
from pathlib import Path

HOME = Path.home()
OPENCLAW_DIR = HOME / ".openclaw"
WORKSPACE_DIR = HOME / "Workspace"
SCRIPTS_DIR = WORKSPACE_DIR / "scripts"
LAUNCHAGENTS_DIR = HOME / "Library" / "LaunchAgents"

# ── OpenClaw internals ────────────────────────────────────────────────────────
LOGS_DIR = OPENCLAW_DIR / "logs"
GATEWAY_LOG = LOGS_DIR / "gateway.log"
GATEWAY_ERR_LOG = LOGS_DIR / "gateway.err.log"
GATEWAY_LOG_TMP = Path(f"/tmp/openclaw/openclaw-{datetime.now().strftime('%Y-%m-%d')}.log")
OPENCLAW_JSON = OPENCLAW_DIR / "openclaw.json"
AGENTS_DIR = OPENCLAW_DIR / "agents"
SKILLS_DIR = OPENCLAW_DIR / "skills"
WORKSPACE_SKILLS_DIR = WORKSPACE_DIR / "skills"
CREDENTIALS_DIR = OPENCLAW_DIR / "credentials"
CRON_JOBS_JSON = OPENCLAW_DIR / "cron" / "jobs.json"
MODELS_JSON = AGENTS_DIR / "operator" / "agent" / "models.json"

# ── Git repos ────────────────────────────────────────────────────────────────
OPENCLAW_REPO = OPENCLAW_DIR
WORKSPACE_REPO = OPENCLAW_DIR / "workspace"

# ── Scan scope ────────────────────────────────────────────────────────────────
ENV_SCAN_DIRS = [OPENCLAW_DIR, WORKSPACE_DIR]
GITIGNORE_REQUIRED = [".env", "credentials/", "logs/", "exec-approvals.json"]

# ── Telegram ──────────────────────────────────────────────────────────────────
SECURITY_COUNCIL_CHAT_ID = "-5170780877"
TELEGRAM_MSG_LIMIT = 4000

# ── Logging ───────────────────────────────────────────────────────────────────
SECURITY_LOG = Path("/tmp/openclaw/security_council.log")

# ── Drill state (separate from platform_health) ──────────────────────────────
DRILL_STATE_FILE = Path("/tmp/openclaw/security_drill_state.json")

# ── AI analysis ───────────────────────────────────────────────────────────────
ANTHROPIC_MODEL = "claude-sonnet-4-6"
MAX_EVIDENCE_CHARS = 2000  # per collector
MAX_OUTPUT_TOKENS = 4096   # per perspective call
MAX_AI_FINDINGS_PER_PERSPECTIVE = 5

# ── Severity ordering ────────────────────────────────────────────────────────
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
