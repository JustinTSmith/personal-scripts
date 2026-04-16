#!/usr/bin/env python3
"""
fix-openclaw-models.sh
Patches all agent models.json files after OpenClaw gateway start.
- Injects anthropic provider block (with correct api adapter) for strategist/writing/reasoning
- Injects xai provider block for grok-social
- Ensures qwen3 models have reasoning:false
Run: python3 ~/Workspace/scripts/fix-openclaw-models.sh
"""
import json, glob

import os
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
XAI_KEY = os.environ.get("XAI_API_KEY", "")

ANTHROPIC_PROVIDER = {
  "api": "anthropic-messages",
  "apiKey": ANTHROPIC_KEY,
  "models": [
    {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6", "reasoning": False,
     "input": ["text"], "cost": {"input": 3, "output": 15, "cacheRead": 0.3, "cacheWrite": 3.75},
     "contextWindow": 200000, "maxTokens": 8192, "api": "anthropic-messages"},
    {"id": "claude-opus-4-6", "name": "claude-opus-4-6", "reasoning": False,
     "input": ["text"], "cost": {"input": 15, "output": 75, "cacheRead": 1.5, "cacheWrite": 18.75},
     "contextWindow": 200000, "maxTokens": 8192, "api": "anthropic-messages"}
  ]
}

XAI_PROVIDER = {
  "api": "xai",
  "baseUrl": "https://api.x.ai/v1",
  "apiKey": XAI_KEY,
  "models": [
    {"id": "grok-3-mini", "name": "grok-3-mini", "reasoning": False,
     "input": ["text"], "cost": {"input": 0.3, "output": 0.5, "cacheRead": 0, "cacheWrite": 0},
     "contextWindow": 131072, "maxTokens": 16384}
  ]
}

# Which agents need which extra providers beyond ollama
AGENT_PROVIDERS = {
  "strategist": ["anthropic"],
  "writing":    ["anthropic"],
  "reasoning":  ["anthropic"],
  "grok-social": ["xai"],
}

for fpath in sorted(glob.glob('/Users/justinsmith/.openclaw/agents/*/agent/models.json')):
    agent = fpath.split('/agents/')[1].split('/agent/')[0]
    with open(fpath) as f:
        d = json.load(f)

    extra = AGENT_PROVIDERS.get(agent, [])
    changed = False

    # Inject or fix anthropic provider
    if "anthropic" in extra:
        if "anthropic" not in d.get("providers", {}):
            d.setdefault("providers", {})["anthropic"] = ANTHROPIC_PROVIDER
            changed = True
        else:
            ant = d["providers"]["anthropic"]
            if ant.get("api") != "anthropic-messages":
                ant["api"] = "anthropic-messages"
                changed = True
            if not ant.get("apiKey"):
                ant["apiKey"] = ANTHROPIC_KEY
                changed = True
            for m in ant.get("models", []):
                if m.get("api") != "anthropic-messages":
                    m["api"] = "anthropic-messages"
                    changed = True

    # Inject xai provider
    if "xai" in extra and "xai" not in d.get("providers", {}):
        d.setdefault("providers", {})["xai"] = XAI_PROVIDER
        changed = True

    # Ensure reasoning:false for qwen3 models (should already be correct in v2026.4.9)
    for pname, pdata in d.get("providers", {}).items():
        for m in pdata.get("models", []):
            if m.get("reasoning") is True and "qwen3" in m.get("id", ""):
                m["reasoning"] = False
                changed = True

    if changed:
        with open(fpath, 'w') as f:
            json.dump(d, f, indent=2)
        print(f"Patched: {agent}")
    else:
        print(f"OK:      {agent}")

print("Done.")
