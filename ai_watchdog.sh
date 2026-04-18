#!/bin/bash
# ai_watchdog.sh — checks critical services; generous timeout to avoid killing mid-warmup
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
USER_ID=$(id -u)

check() {  # args: url, name
    local url="$1" name="$2"
    for i in 1 2 3; do
        if curl -sf --max-time 15 "$url" > /dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    return 1
}

# 1. Ollama
check "http://localhost:11434/api/tags" "Ollama" || {
    echo "$(date): Ollama down. Restarting..."
    launchctl kickstart -k gui/${USER_ID}/com.ollama.ollama 2>/dev/null || brew services restart ollama
}

# 2. Qdrant
check "http://localhost:6333/healthz" "Qdrant" || {
    echo "$(date): Qdrant down. Restarting..."
    launchctl kickstart -k gui/${USER_ID}/com.justinos.qdrant 2>/dev/null
}

# 3. OpenClaw gateway — now with 45s cumulative grace (3x15s retries)
check "http://localhost:18789/health" "OpenClaw gateway" || {
    echo "$(date): Gateway down after 45s retries. Restarting..."
    launchctl kickstart -k gui/${USER_ID}/ai.openclaw.gateway 2>/dev/null
}

echo "$(date): Watchdog check complete."
