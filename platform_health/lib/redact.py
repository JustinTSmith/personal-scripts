from __future__ import annotations
"""
Security hardening: redact tokens, API keys, and secrets from report text
before sending to Telegram. All check output must pass through redact() before
being included in any message.
"""
import re
import json
from pathlib import Path

# ── Patterns to redact ────────────────────────────────────────────────────────
_PATTERNS = [
    # Telegram bot token (digits:alphanumeric)
    (re.compile(r'\b\d{8,10}:[A-Za-z0-9_-]{35}\b'), '[BOT_TOKEN]'),
    # Anthropic API keys
    (re.compile(r'\bsk-ant-api[0-9A-Za-z_-]{40,}\b'), '[ANTHROPIC_KEY]'),
    # OpenAI API keys
    (re.compile(r'\bsk-[A-Za-z0-9]{40,}\b'), '[OPENAI_KEY]'),
    # XAI / Grok keys
    (re.compile(r'\bxai-[A-Za-z0-9]{40,}\b'), '[XAI_KEY]'),
    # Generic "token": "..." patterns (JSON-style) — use non-lookbehind form
    (re.compile(r'("token"\s*:\s*")[^"]{16,}("?)'), r'\1[TOKEN]\2'),
    # x-brain-key values (OB1 Supabase)
    (re.compile(r'\bb[0-9a-f]{46}\b'), '[BRAIN_KEY]'),
    # OpenClaw gateway auth tokens (hex-like 48-char)
    (re.compile(r'\b[0-9a-f]{48}\b'), '[GATEWAY_TOKEN]'),
    # Generic hex secrets >= 32 chars
    (re.compile(r'\b[0-9a-f]{32,}\b'), '[SECRET]'),
    # Bearer / Authorization header values
    (re.compile(r'(?i)(?:bearer|token)\s+([A-Za-z0-9_\-\.]{20,})'), r'[AUTH_HEADER]'),
    # Password-like key=value in URLs or env
    (re.compile(r'(?i)(?:password|passwd|secret|apikey|api_key)=[^\s&"\']{8,}'), '[CREDENTIAL]'),
]

# ── Path fragments to abbreviate (not redact, just shorten for readability) ──
_PATH_ABBREVS = [
    (re.compile(r'/Users/[^/]+'), '~'),
]


def redact(text: str) -> str:
    """Apply all redaction patterns to text. Returns sanitized string."""
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _PATH_ABBREVS:
        text = pattern.sub(replacement, text)
    return text


def redact_dict(d: dict) -> dict:
    """Recursively redact all string values in a dict."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, list):
            out[k] = [redact(i) if isinstance(i, str) else i for i in v]
        elif isinstance(v, str):
            out[k] = redact(v)
        else:
            out[k] = v
    return out


def load_openclaw_json_safe(path: Path) -> dict:
    """Load openclaw.json and immediately redact sensitive fields before
    returning. Use this instead of raw json.load() in any check that touches
    the config."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return redact_dict(raw)
    except Exception as e:
        return {"_error": str(e)}
