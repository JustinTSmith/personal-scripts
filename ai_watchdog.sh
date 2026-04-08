#!/bin/bash
# ai_watchdog.sh — checks critical services and restarts them if down
# Runs every 2 minutes via com.justinsmith.ai-watchdog LaunchAgent

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

USER_ID=$(id -u)

# 1. Check if Ollama is running
if ! curl -s --max-time 5 http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "$(date): Ollama is down. Restarting via launchctl..."
    launchctl kickstart -k gui/${USER_ID}/com.ollama.ollama 2>/dev/null || brew services restart ollama
fi

# 2. Check if Qdrant is running (native binary, managed by launchd)
if ! curl -s --max-time 5 http://localhost:6333/healthz > /dev/null 2>&1; then
    echo "$(date): Qdrant is down. Restarting via launchctl..."
    launchctl kickstart -k gui/${USER_ID}/com.justinos.qdrant 2>/dev/null
fi

# 3. Check if OpenClaw gateway is running
if ! curl -s --max-time 5 http://localhost:18789/health > /dev/null 2>&1; then
    echo "$(date): OpenClaw gateway is down. Restarting via launchctl..."
    launchctl kickstart -k gui/${USER_ID}/ai.openclaw.gateway 2>/dev/null
fi

echo "$(date): Watchdog check complete."
