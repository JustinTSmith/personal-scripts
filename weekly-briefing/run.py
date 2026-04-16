#!/usr/bin/env python3
"""
Weekly Newsletter Briefing Generator
Pipeline: Gmail fetch → qwen3.5:35b-a3b (analysis) → claude-opus-4-6 (writing) → Gmail draft

Runs every Sunday at 8am via LaunchAgent.
"""

import os
import sys
import json
import datetime
import subprocess
import urllib.request
import urllib.error

LOG_FILE = os.path.expanduser("~/Workspace/scripts/weekly-briefing/briefing.log")
ENV_FILE = os.path.expanduser("~/.config/ai/.env")
OLLAMA_URL = "http://localhost:11434/api/generate"
ANALYSIS_MODEL = "qwen3.5:35b-a3b"
WRITING_MODEL = "claude-opus-4-6"
RECIPIENT = "justintsmith@gmail.com"


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    return env


def fetch_newsletters_via_claude():
    """Use the claude CLI (which has Gmail MCP) to fetch newsletter content."""
    today = datetime.date.today()
    week_ago = today - datetime.timedelta(days=7)
    date_str = week_ago.strftime("%Y/%m/%d")

    prompt = f"""Search Gmail for newsletter emails received in the past 7 days (after:{date_str}).

Search for emails from these sources (use multiple searches):
1. from:(lennyrachitsky.com OR lennys.com) - Lenny's Newsletter
2. from:garymarcus.net OR subject:"Marcus on AI" - Gary Marcus
3. subject:"Nate's Newsletter" OR from:natesfyi - Nate's Newsletter
4. subject:"Sandhill" OR from:sandhilleast - Sandhill East
5. Also search: label:newsletters newer_than:7d

For each email found, output:
SOURCE: [newsletter name]
DATE: [date received]
SUBJECT: [subject line]
CONTENT:
[full plain text body of the email]
---END---

Output ALL found newsletters. Do not summarize — output the raw content. If no emails found for a source, skip it."""

    log("Fetching newsletters via claude CLI...")
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "HOME": os.path.expanduser("~")}
    )

    if result.returncode != 0:
        log(f"claude CLI error: {result.stderr[:500]}")
        return None

    content = result.stdout.strip()
    if len(content) < 200:
        log(f"Suspiciously short newsletter content ({len(content)} chars), output: {content[:200]}")
        return None

    log(f"Fetched {len(content)} chars of newsletter content")
    return content


def run_qwen_analysis(newsletter_content):
    """Send newsletter content to qwen3.5:35b-a3b for structured analysis."""
    today = datetime.date.today().strftime("%b %d, %Y")

    prompt = f"""You are an expert technology analyst. Analyze the following newsletter content from the past week and extract structured intelligence.

## NEWSLETTER CONTENT

{newsletter_content}

---

## YOUR TASK

Extract and synthesize intelligence across ALL sources. Return a JSON object with exactly these keys:

{{
  "signal": {{
    "title": "short punchy title for the dominant cross-source theme",
    "body": "3-4 paragraphs of deep analysis connecting multiple sources. Personal, direct. Address 'Justin' directly. Include specific mechanism, not vague trends. End with a 'why this week specifically' paragraph and a cross-source synthesis paragraph."
  }},
  "opportunities": [
    {{
      "name": "product name",
      "icp": "specific buyer with context",
      "conviction": "act" or "watch",
      "why_now": "specific trigger from this week's news",
      "path": "path to $10K MRR with $ figures and timeline"
    }}
  ],
  "trends": [
    {{
      "stat": "specific number or metric",
      "context": "what it means, one sentence"
    }}
  ]
}}

Rules:
- opportunities: exactly 5 items
- trends: 8-10 items, all must include specific numbers (%, $, counts, multipliers)
- signal body must reference at least 3 different newsletter sources
- conviction "act" = build now, window closing; "watch" = real but early
- Output ONLY valid JSON. No markdown fences, no preamble."""

    log(f"Running qwen analysis ({ANALYSIS_MODEL})...")
    payload = json.dumps({
        "model": ANALYSIS_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 5000}
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read())
            raw = result.get("response", "")
    except Exception as e:
        log(f"Ollama error: {e}")
        return None

    # Strip any markdown fences if model added them
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("`").strip()

    try:
        analysis = json.loads(raw)
        log(f"qwen analysis complete: {len(analysis.get('opportunities', []))} opportunities, {len(analysis.get('trends', []))} trends")
        return analysis
    except json.JSONDecodeError as e:
        log(f"JSON parse error: {e}\nRaw (first 500): {raw[:500]}")
        return None


def write_briefing_with_opus(analysis, env):
    """Feed qwen's structured analysis to Claude Opus for final prose writing."""
    today = datetime.date.today().strftime("%b %d, %Y")
    api_key = env.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log("No ANTHROPIC_API_KEY found")
        return None

    signal = analysis.get("signal", {})
    opportunities = analysis.get("opportunities", [])
    trends = analysis.get("trends", [])

    # Format opportunities for the prompt
    opp_text = ""
    for i, opp in enumerate(opportunities, 1):
        opp_text += f"""
**{i}. {opp.get('name', '')}**
*ICP: {opp.get('icp', '')}*
*Conviction: [{opp.get('conviction', 'watch')}]*
*Why now:* {opp.get('why_now', '')}
*Path:* {opp.get('path', '')}
"""

    trend_text = "\n".join(f"- **{t.get('stat', '')}** — {t.get('context', '')}" for t in trends)

    prompt = f"""You are writing a weekly intelligence briefing for Justin — a builder and investor tracking AI, venture, and emerging tech.

Today is {today}.

Here is the structured analysis from this week's newsletters. Your job is to write the final briefing in Justin's exact preferred format. Write with authority, specificity, and personal directness. No hedging, no filler.

## SIGNAL TO EXPAND
Title: {signal.get('title', '')}
Analysis: {signal.get('body', '')}

## OPPORTUNITIES (already structured, just format them cleanly)
{opp_text}

## TRENDS (already structured, just format them cleanly)
{trend_text}

---

Write the complete briefing in this EXACT format:

# 📬 Weekly Briefing — {today}

Hey Justin,

[1-2 sentence setup paragraph. Tease the dominant theme. Create forward pull into the signal section.]

---

## 🎯 This Week's Signal

**[Signal Title]**

[Expand the signal analysis into flowing prose. 4-5 paragraphs. Start with the specific evidence from this week's newsletters. Build to the mechanism. Make the personal implication for Justin explicit. End with a "why this week specifically" paragraph and a cross-source synthesis. No bullet points in this section.]

---

## 🏗 Build Opportunity Scan

[Format each opportunity exactly like this:]
**[N]. [Name]**
*ICP: [specific buyer]*
*Conviction: [act/watch]*
**Why now:** [specific this-week trigger]
**Path:** [specific $ path to $10K MRR]

[Repeat for all 5]

---

## 📈 Trend Velocity

[Bullet list, each line: **[stat]** — [one sentence context]]

---

*Sources: [list the actual newsletter sources used this week]*

Output the briefing only. No preamble."""

    log(f"Writing briefing with {WRITING_MODEL}...")
    payload = json.dumps({
        "model": WRITING_MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            briefing = result["content"][0]["text"]
            log(f"Opus writing complete: {len(briefing)} chars")
            return briefing
    except Exception as e:
        log(f"Anthropic API error: {e}")
        return None


def create_gmail_draft(briefing):
    """Use claude CLI to create Gmail draft with the briefing."""
    today = datetime.date.today().strftime("%b %-d, %Y")
    subject = f"📬 Weekly Briefing — {today}"

    prompt = f"""Create a Gmail draft with:
To: {RECIPIENT}
Subject: {subject}

Body (HTML — preserve all markdown formatting as proper HTML: ## headings as <h2>, **bold** as <strong>, bullet lists as <ul><li>, --- as <hr>, *italic* as <em>, `code` as <code>):

{briefing}

Create the draft now."""

    log("Creating Gmail draft via claude CLI...")
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HOME": os.path.expanduser("~")}
    )

    if result.returncode != 0:
        log(f"Gmail draft error: {result.stderr[:300]}")
        return False

    log(f"Gmail draft created: {result.stdout[:200]}")
    return True


def save_local_copy(briefing):
    """Save a local markdown copy for reference."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    path = os.path.expanduser(f"~/Workspace/scripts/weekly-briefing/briefings/{today}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(briefing)
    log(f"Saved local copy: {path}")


def main():
    log("=== Weekly Briefing Generator starting ===")
    env = load_env()

    # Step 1: Fetch newsletters
    newsletters = fetch_newsletters_via_claude()
    if not newsletters:
        log("ABORT: Could not fetch newsletter content")
        sys.exit(1)

    # Step 2: qwen analysis
    analysis = run_qwen_analysis(newsletters)
    if not analysis:
        log("ABORT: qwen analysis failed")
        sys.exit(1)

    # Step 3: Opus writing
    briefing = write_briefing_with_opus(analysis, env)
    if not briefing:
        log("ABORT: Opus writing failed")
        sys.exit(1)

    # Step 4: Save local copy
    save_local_copy(briefing)

    # Step 5: Create Gmail draft
    ok = create_gmail_draft(briefing)
    if not ok:
        log("WARNING: Gmail draft creation failed — briefing saved locally only")
        sys.exit(1)

    log("=== Weekly Briefing Generator complete ===")


if __name__ == "__main__":
    main()
