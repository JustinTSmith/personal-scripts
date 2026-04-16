"""
AI-powered security analysis using Anthropic API.
Sends collected evidence to Claude for analysis from 4 perspectives:
  Red Team (offensive), Blue Team (defensive), Data Privacy, Operational Realism.
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import List

from ..config import ANTHROPIC_MODEL, MAX_OUTPUT_TOKENS, MAX_AI_FINDINGS_PER_PERSPECTIVE, MODELS_JSON

log = logging.getLogger("security_council.ai")

MODEL_OVERRIDE: str | None = None

# ── API key retrieval ────────────────────────────────────────────────────────

def _get_api_key() -> str | None:
    # 1. Environment
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key and len(key) > 20:
        return key
    # 2. OpenClaw models.json (operator agent)
    if MODELS_JSON.exists():
        try:
            data = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
            key = data.get("providers", {}).get("anthropic", {}).get("apiKey", "")
            if key and len(key) > 20:
                return key
        except (json.JSONDecodeError, OSError):
            pass
    return None


# ── Evidence aggregation ─────────────────────────────────────────────────────

def aggregate_evidence(check_results: List[dict]) -> str:
    """Combine evidence from all collectors into a structured briefing."""
    sections: dict = {}
    for r in check_results:
        section = r.get("section", "Unknown")
        if section not in sections:
            sections[section] = []
        evidence = r.get("evidence", "")
        detail = r.get("detail", "")
        label = r.get("label", "")
        status = r.get("status", "ok")
        severity = r.get("severity", "")

        if evidence:
            sections[section].append(evidence)
        elif detail and status in ("warn", "fail"):
            sections[section].append(f"[{severity or status}] {label}: {detail}")

    parts = []
    for section, items in sections.items():
        parts.append(f"## {section}")
        # Deduplicate
        seen = set()
        for item in items:
            if item not in seen:
                seen.add(item)
                parts.append(item)
        parts.append("")

    full = "\n".join(parts)
    return full[:8000]  # hard cap


# ── Perspective prompts ──────────────────────────────────────────────────────

PERSPECTIVES = {
    "Red Team": (
        "You are a red team security analyst auditing a personal AI assistant platform "
        "called OpenClaw running on macOS. It has agents with code execution access, "
        "Telegram integration, MCP servers, and manages API keys.\n\n"
        "Given the security evidence below, identify how an attacker could exploit this system. "
        "Focus on: credential theft, privilege escalation, prompt injection, data exfiltration, "
        "supply chain risks.\n\n"
        f"Return a JSON array of at most {MAX_AI_FINDINGS_PER_PERSPECTIVE} findings. "
        "Each finding must be: "
        '{\"id\": \"RT-N\", \"severity\": \"critical|high|medium|low\", '
        '\"title\": \"short title\", \"detail\": \"explanation with evidence\", '
        '\"remediation\": \"specific fix\"}\n\n'
        "Be specific — cite exact files, permissions, or patterns from the evidence. "
        "Return ONLY the JSON array, no markdown fencing."
    ),
    "Blue Team": (
        "You are a blue team security analyst evaluating the defenses of a personal AI assistant "
        "platform called OpenClaw.\n\n"
        "Assess: access controls, authentication strength, input validation, logging coverage, "
        "secret management, sandboxing, network exposure.\n\n"
        f"Return a JSON array of at most {MAX_AI_FINDINGS_PER_PERSPECTIVE} findings. "
        "Each finding must be: "
        '{\"id\": \"BT-N\", \"severity\": \"critical|high|medium|low\", '
        '\"title\": \"short title\", \"detail\": \"explanation\", '
        '\"remediation\": \"specific fix\"}\n\n'
        "Return ONLY the JSON array, no markdown fencing."
    ),
    "Data Privacy": (
        "You are a data privacy analyst reviewing a personal AI platform called OpenClaw.\n\n"
        "Identify: PII exposure in logs, unencrypted secrets at rest, data shared with "
        "third-party APIs, conversation data retention, credential leakage vectors.\n\n"
        f"Return a JSON array of at most {MAX_AI_FINDINGS_PER_PERSPECTIVE} findings. "
        "Each finding must be: "
        '{\"id\": \"DP-N\", \"severity\": \"critical|high|medium|low\", '
        '\"title\": \"short title\", \"detail\": \"explanation\", '
        '\"remediation\": \"specific fix\"}\n\n'
        "Return ONLY the JSON array, no markdown fencing."
    ),
    "Operational Realism": (
        "You are a security engineer reviewing operational risks of a personal AI platform "
        "called OpenClaw running 24/7 on a Mac Studio.\n\n"
        "Consider: what happens when tokens expire, gateway crashes, MCP servers are compromised, "
        "someone gains physical access, cron jobs fail silently, config drift introduces weaknesses.\n\n"
        f"Return a JSON array of at most {MAX_AI_FINDINGS_PER_PERSPECTIVE} findings. "
        "Each finding must be: "
        '{\"id\": \"OR-N\", \"severity\": \"critical|high|medium|low\", '
        '\"title\": \"short title\", \"detail\": \"explanation\", '
        '\"remediation\": \"specific fix\"}\n\n'
        "Return ONLY the JSON array, no markdown fencing."
    ),
}


def _parse_json_findings(text: str) -> List[dict]:
    """Extract JSON array from response, handling markdown fencing."""
    text = text.strip()
    # Remove markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        findings = json.loads(text)
        if isinstance(findings, list):
            return findings
    except json.JSONDecodeError:
        # Try to find JSON array in text
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return []


def analyze_perspective(evidence: str, perspective_name: str, system_prompt: str) -> List[dict]:
    """Call Anthropic API for one perspective. Returns list of finding dicts."""
    import anthropic

    api_key = _get_api_key()
    if not api_key:
        log.error("No Anthropic API key available")
        return [{
            "id": f"{perspective_name[:2].upper()}-ERR",
            "severity": "low",
            "title": f"AI analysis skipped: {perspective_name}",
            "detail": "No Anthropic API key found",
            "remediation": "Set ANTHROPIC_API_KEY env var or configure in models.json",
        }]

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL_OVERRIDE or ANTHROPIC_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"Security evidence from automated scan:\n\n{evidence}",
            }],
        )
        text = response.content[0].text
        findings = _parse_json_findings(text)
        log.info("AI %s: %d findings", perspective_name, len(findings))
        return findings[:MAX_AI_FINDINGS_PER_PERSPECTIVE]

    except Exception as e:
        log.exception("AI analysis failed for %s", perspective_name)
        return [{
            "id": f"{perspective_name[:2].upper()}-ERR",
            "severity": "low",
            "title": f"AI analysis error: {perspective_name}",
            "detail": str(e)[:200],
            "remediation": "Check API key and network connectivity",
        }]


def run_ai_analysis(check_results: List[dict]) -> List[dict]:
    """Run all 4 perspectives. Returns list of AI finding dicts with perspective field."""
    evidence = aggregate_evidence(check_results)
    if not evidence.strip():
        log.warning("No evidence collected — skipping AI analysis")
        return []

    all_findings = []
    for name, prompt in PERSPECTIVES.items():
        findings = analyze_perspective(evidence, name, prompt)
        for f in findings:
            f["perspective"] = name
        all_findings.extend(findings)

    log.info("AI analysis complete: %d total findings across %d perspectives",
             len(all_findings), len(PERSPECTIVES))
    return all_findings
