from __future__ import annotations
"""
Secrets scanner: .env permissions, config secrets, env vars, git history.
"""
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import List

from ..config import (
    OPENCLAW_DIR, WORKSPACE_DIR, OPENCLAW_JSON,
    ENV_SCAN_DIRS, MAX_EVIDENCE_CHARS,
)

# API key env var names to check
SENSITIVE_ENV_VARS = [
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "PERPLEXITY_API_KEY", "XAI_API_KEY", "TELEGRAM_BOT_TOKEN",
    "OPENCLAW_GATEWAY_TOKEN", "SUPABASE_KEY", "GITHUB_TOKEN",
]

# Patterns in config files that indicate plaintext secrets
SECRET_KEY_PATTERNS = re.compile(
    r'(?i)(apiKey|token|secret|password|auth\b.*key|botToken|x-brain-key|x-access-key)',
)


def _find_env_files() -> List[Path]:
    found = []
    for base in ENV_SCAN_DIRS:
        if not base.exists():
            continue
        for env_file in base.rglob(".env"):
            if "node_modules" in str(env_file) or ".git" in str(env_file):
                continue
            found.append(env_file)
        for env_file in base.rglob(".env.*"):
            if "node_modules" in str(env_file) or ".git" in str(env_file):
                continue
            found.append(env_file)
    return found[:20]  # cap


def _check_file_permissions(path: Path) -> dict | None:
    if not path.exists():
        return None
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    octal = oct(mode)
    is_secure = (mode & 0o077) == 0  # no group/other access
    return {"path": str(path), "mode": octal, "secure": is_secure}


def _extract_env_key_names(path: Path) -> List[str]:
    """Extract variable names (not values) from .env file."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        keys = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key = line.split("=", 1)[0].strip()
                if key:
                    keys.append(key)
        return keys
    except OSError:
        return []


def _check_config_secrets(path: Path) -> List[str]:
    """Find keys in JSON config that look like secret storage."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        matches = []
        for i, line in enumerate(text.splitlines(), 1):
            if SECRET_KEY_PATTERNS.search(line):
                # Extract just the key name, not the value
                clean = re.sub(r'"[^"]{16,}"', '"[REDACTED]"', line)
                matches.append(f"L{i}: {clean.strip()[:100]}")
        return matches[:15]
    except OSError:
        return []


def _git_secret_scan(repo: Path) -> List[str]:
    """Scan git history for committed secrets."""
    if not (repo / ".git").exists():
        return []
    findings = []
    try:
        # Check if .env was ever tracked
        r = subprocess.run(
            ["git", "-C", str(repo), "log", "--all", "--diff-filter=A",
             "--name-only", "--pretty=format:", "--", "*.env", ".env*"],
            capture_output=True, text=True, timeout=10,
        )
        env_files = [f for f in r.stdout.strip().splitlines() if f.strip()]
        if env_files:
            findings.append(f".env committed to history: {', '.join(env_files[:5])}")

        # Check for secret patterns in recent diffs (last 50 commits)
        for pattern in ["sk-ant-api", "sk-proj-", "xai-", "botToken"]:
            r = subprocess.run(
                ["git", "-C", str(repo), "log", "-50", "--all", "-S", pattern,
                 "--pretty=format:%h %s", "--"],
                capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip():
                lines = r.stdout.strip().splitlines()[:3]
                findings.append(f"Pattern '{pattern}' in commits: {'; '.join(lines)}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return findings[:5]


def run() -> List[dict]:
    results = []
    evidence_parts = []

    # 1. .env file permissions
    env_files = _find_env_files()
    insecure_envs = []
    for ef in env_files:
        # Skip .env.example / .env.template — these are intentionally 644
        if any(ef.name.endswith(s) for s in (".example", ".template", ".sample", ".jest.example")):
            continue

        perm = _check_file_permissions(ef)
        if perm and not perm["secure"]:
            insecure_envs.append(perm)
            results.append({
                "section": "Secrets",
                "status": "fail",
                "severity": "critical",
                "label": f".env: {ef}",
                "detail": f"permissions {perm['mode']} (should be 0o600)",
            })
            evidence_parts.append(f".env INSECURE: {ef} mode={perm['mode']}")

    if not insecure_envs and env_files:
        results.append({
            "section": "Secrets",
            "status": "ok",
            "label": f".env permissions ({len(env_files)} files)",
            "detail": "all secure (600)",
        })

    # 2. .env key names (not values)
    all_env_keys = set()
    for ef in env_files:
        keys = _extract_env_key_names(ef)
        all_env_keys.update(keys)
        if keys:
            evidence_parts.append(f"{ef.name} keys: {', '.join(keys[:10])}")

    # 3. Config file secrets
    config_secrets = _check_config_secrets(OPENCLAW_JSON)
    if config_secrets:
        results.append({
            "section": "Secrets",
            "status": "warn",
            "severity": "medium",
            "label": "openclaw.json plaintext secrets",
            "detail": f"{len(config_secrets)} secret-like keys found",
        })
        evidence_parts.append("openclaw.json secrets:\n" + "\n".join(config_secrets[:8]))
    else:
        results.append({
            "section": "Secrets",
            "status": "ok",
            "label": "openclaw.json secrets",
            "detail": "no plaintext secrets detected",
        })

    # 4. Environment variables
    exposed_vars = []
    for var in SENSITIVE_ENV_VARS:
        val = os.environ.get(var, "")
        if val and len(val) > 10:
            exposed_vars.append(var)
    if exposed_vars:
        results.append({
            "section": "Secrets",
            "status": "warn",
            "severity": "medium",
            "label": "active env vars with secrets",
            "detail": ", ".join(exposed_vars),
        })
        evidence_parts.append(f"Env vars with secrets: {', '.join(exposed_vars)}")
    else:
        results.append({
            "section": "Secrets",
            "status": "ok",
            "label": "environment variables",
            "detail": "no sensitive vars exposed in current env",
        })

    # 5. Git history scan
    git_findings = _git_secret_scan(OPENCLAW_DIR)
    if git_findings:
        results.append({
            "section": "Secrets",
            "status": "warn",
            "severity": "high",
            "label": "git history secrets",
            "detail": "; ".join(git_findings)[:200],
        })
        evidence_parts.append("Git secrets:\n" + "\n".join(git_findings))

    # Attach combined evidence to first result
    evidence = "\n".join(evidence_parts)[:MAX_EVIDENCE_CHARS]
    for r in results:
        r["evidence"] = evidence
        break  # only first

    return results
