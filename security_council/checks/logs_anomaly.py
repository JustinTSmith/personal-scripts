"""
Log anomaly detection: auth failures, injection attempts, error spikes, MCP disconnects.
"""
import re
from pathlib import Path
from typing import List

from ..config import GATEWAY_LOG, GATEWAY_ERR_LOG, GATEWAY_LOG_TMP, MAX_EVIDENCE_CHARS

TAIL_LINES = 500

# Security-specific patterns
AUTH_FAILURE = re.compile(r'(?i)(?:auth.*fail|unauthorized|forbidden|401|403|invalid.*token|denied)')
INJECTION_PATTERNS = [
    (re.compile(r'(?i)ignore\s+(?:previous|above|all)\s+(?:instructions?|prompts?|rules?)'), "prompt injection"),
    (re.compile(r'(?i)<\s*(?:system|admin|root)\s*>'), "XML injection marker"),
    (re.compile(r'(?i)ADMIN\s+OVERRIDE'), "admin override attempt"),
    (re.compile(r'\.\./', re.MULTILINE), "path traversal"),
    (re.compile(r';\s*(?:rm|cat|curl|wget)\s', re.I), "command injection"),
    (re.compile(r'`[^`]*(?:rm\s+-rf|curl\s+-d|wget\s+-O)[^`]*`'), "backtick injection"),
]
MCP_ISSUE = re.compile(r'(?i)(?:mcp.*(?:fail|error|disconnect|skipped)|skipped\s+server)')
ERROR_LINE = re.compile(r'(?i)\bERROR\b')


def _tail(path: Path, n: int) -> List[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-n:] if len(lines) > n else lines
    except OSError:
        return []


def _analyze_log(path: Path, label: str) -> List[dict]:
    results = []
    evidence_parts = []
    lines = _tail(path, TAIL_LINES)

    if not lines:
        return [{
            "section": "LogsAnomaly",
            "status": "ok",
            "label": f"{label}",
            "detail": "empty or missing",
        }]

    # Auth failures
    auth_fails = [l for l in lines if AUTH_FAILURE.search(l)]
    if auth_fails:
        results.append({
            "section": "LogsAnomaly",
            "status": "warn",
            "severity": "high" if len(auth_fails) > 5 else "medium",
            "label": f"{label}: auth failures",
            "detail": f"{len(auth_fails)} in last {TAIL_LINES} lines",
        })
        evidence_parts.append(f"Auth failures ({len(auth_fails)}):")
        for af in auth_fails[-3:]:  # last 3 examples
            evidence_parts.append(f"  {af.strip()[:120]}")

    # Injection attempts
    for pattern, name in INJECTION_PATTERNS:
        matches = [l for l in lines if pattern.search(l)]
        if matches:
            results.append({
                "section": "LogsAnomaly",
                "status": "fail",
                "severity": "critical",
                "label": f"{label}: {name} detected",
                "detail": f"{len(matches)} occurrence(s)",
            })
            evidence_parts.append(f"INJECTION: {name} ({len(matches)}x)")
            for m in matches[-2:]:
                evidence_parts.append(f"  {m.strip()[:120]}")

    # MCP issues
    mcp_issues = [l for l in lines if MCP_ISSUE.search(l)]
    if mcp_issues:
        results.append({
            "section": "LogsAnomaly",
            "status": "warn",
            "severity": "medium",
            "label": f"{label}: MCP issues",
            "detail": f"{len(mcp_issues)} failures/disconnects",
        })

    # Error rate spike: last 100 lines vs previous 400
    error_lines = [l for l in lines if ERROR_LINE.search(l)]
    if len(lines) >= 200:
        recent_errors = sum(1 for l in lines[-100:] if ERROR_LINE.search(l))
        baseline_errors = sum(1 for l in lines[:-100] if ERROR_LINE.search(l))
        baseline_rate = baseline_errors / max(len(lines) - 100, 1)
        recent_rate = recent_errors / 100

        if recent_rate > baseline_rate * 3 and recent_errors > 5:
            results.append({
                "section": "LogsAnomaly",
                "status": "warn",
                "severity": "high",
                "label": f"{label}: error spike",
                "detail": f"recent: {recent_errors}/100 lines vs baseline: {baseline_rate:.1%}",
            })

    if not results:
        results.append({
            "section": "LogsAnomaly",
            "status": "ok",
            "label": f"{label}",
            "detail": f"clean ({len(lines)} lines scanned)",
        })

    # Attach evidence to first result
    if results and evidence_parts:
        results[0]["evidence"] = "\n".join(evidence_parts)[:MAX_EVIDENCE_CHARS]

    return results


def run() -> List[dict]:
    results = []
    results.extend(_analyze_log(GATEWAY_LOG, "gateway.log"))
    results.extend(_analyze_log(GATEWAY_ERR_LOG, "gateway.err.log"))
    if GATEWAY_LOG_TMP.exists():
        results.extend(_analyze_log(GATEWAY_LOG_TMP, f"gateway-tmp"))
    return results
