#!/bin/bash

# Resolve script's own directory so it runs correctly from anywhere
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Starting JustinOS..."

OPENCLAW_HOME="$HOME/Workspace/openclaw"
ENV_PATH="/Users/justinsmith/.config/ai/.env"

# -----------------------------
# 1. Load ENV (robust)
# -----------------------------
if [ -f "$ENV_PATH" ]; then
  export $(grep -v '^#' "$ENV_PATH" | xargs)
  echo "✅ .env loaded from $ENV_PATH"
else
  echo "❌ Missing .env at $ENV_PATH"
  exit 1
fi

# -----------------------------
# 1.5 Generate auth-profiles.json
# -----------------------------
AUTH_PATH="$OPENCLAW_HOME/agents/main/agent/auth-profiles.json"

mkdir -p "$(dirname "$AUTH_PATH")"

cat > "$AUTH_PATH" <<EOF
{
  "version": 1,
  "profiles": {
    "openai:default": {
      "type": "api_key",
      "provider": "openai",
      "key": "$OPENAI_API_KEY"
    },
    "anthropic:default": {
      "type": "api_key",
      "provider": "anthropic",
      "key": "$ANTHROPIC_API_KEY"
    },
    "grok:default": {
      "type": "api_key",
      "provider": "grok",
      "key": "$XAI_API_KEY"
    },
    "ollama:default": {
      "type": "api_key",
      "provider": "ollama",
      "key": "ollama-local"
    }
  },
  "lastGood": {
    "openai": "openai:default",
    "anthropic": "anthropic:default",
    "grok": "grok:default",
    "ollama": "ollama:default"
  }
}
EOF

echo "✅ auth-profiles.json generated"

# -----------------------------
# 1.6 Propagate auth to agents
# -----------------------------
for agent in operator main coach strategist writing grok-social coding reasoning; do
  mkdir -p "$OPENCLAW_HOME/agents/$agent/agent"
  cp "$AUTH_PATH" "$OPENCLAW_HOME/agents/$agent/agent/auth-profiles.json"
done

echo "✅ auth propagated to agents"


# -----------------------------
# 1.7 Inject env into config
# -----------------------------
CONFIG_PATH="$OPENCLAW_HOME/openclaw.json"

# Create temp config (cleaned up on exit)
TMP_CONFIG="$OPENCLAW_HOME/openclaw.runtime.json"
PLIST="$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist"
trap 'rm -f "$TMP_CONFIG"; launchctl bootstrap gui/$UID "$PLIST" 2>/dev/null; echo ""; echo "🛑 JustinOS stopped. LaunchAgent restored."' EXIT

cp "$CONFIG_PATH" "$TMP_CONFIG"

# Replace placeholders
sed -i '' "s|__TELEGRAM_BOT_TOKEN__|$TELEGRAM_BOT_TOKEN|g" "$TMP_CONFIG"
sed -i '' "s|__BRAVE_API_KEY__|$BRAVE_API_KEY|g" "$TMP_CONFIG"

echo "✅ Injected env into config"

# -----------------------------
# 2. Validate critical vars
# -----------------------------
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "❌ TELEGRAM_BOT_TOKEN missing"
  exit 1
fi

# -----------------------------
# 3. Check Ollama
# -----------------------------
if ! command -v ollama &> /dev/null; then
  echo "❌ Ollama not installed"
  exit 1
fi

echo "🧠 Checking Ollama models..."

ollama list | grep -q "llama3.2" || ollama pull llama3.2:3b
ollama list | grep -q "qwen2.5-coder" || ollama pull qwen2.5-coder:latest
ollama list | grep -q "deepseek-r1" || ollama pull deepseek-r1:latest
ollama list | grep -q "nomic-embed-text" || ollama pull nomic-embed-text:latest

echo "✅ Models ready"

# -----------------------------
# 4. Stop existing OpenClaw
# -----------------------------
# Stop the supervised LaunchAgent first (pkill alone won't work — launchd restarts it)
launchctl bootout gui/$UID/ai.openclaw.gateway 2>/dev/null || true
pkill -f openclaw 2>/dev/null || true
sleep 3

# -----------------------------
# 5. Start OpenClaw gateway
# -----------------------------
echo "🧠 Starting OpenClaw gateway..."

OPENCLAW_STATE_DIR="$OPENCLAW_HOME" OPENCLAW_CONFIG_PATH="$TMP_CONFIG" openclaw gateway &
GATEWAY_PID=$!
sleep 30

# -----------------------------
# 6. Health check (gateway)
# -----------------------------
if lsof -i :18789 >/dev/null; then
  echo "✅ Gateway running on port 18789"
else
  echo "❌ Gateway failed to start"
  echo "👉 Run: openclaw gateway"
  exit 1
fi

# -----------------------------
# 7. Telegram status check
# -----------------------------
echo "📲 Checking Telegram bot..."

if curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe" | grep -q '"ok":true'; then
  echo "✅ Telegram bot connected"
else
  echo "⚠️ Telegram bot check failed"
  echo "👉 Possible issues:"
  echo "   - invalid token"
  echo "   - webhook conflict"
fi

# Optional: clear webhook (safe)
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/deleteWebhook" > /dev/null

# -----------------------------
# 8. Voice system check
# -----------------------------
echo "🔊 Checking voice system..."

if [ ! -z "$OPENAI_API_KEY" ]; then
  echo "✅ OpenAI TTS available"
else
  echo "⚠️ No OpenAI API key (voice disabled)"
fi

if [ ! -z "$ELEVENLABS_API_KEY" ]; then
  echo "✅ ElevenLabs available"
fi

# -----------------------------
# 9. Time Orchestrator
# -----------------------------
python3 ~/Workspace/scripts/time_orchestrator.py
echo "Time protection is on"

# -----------------------------
# 9. Final status
# -----------------------------
echo ""
echo "🔥 JustinOS is LIVE"
echo ""
echo "→ Send a message to your Telegram bot"
echo "→ Visit http://127.0.0.1:18789"
echo "→ Press Ctrl+C to stop"
echo ""

# Keep the window open — wait for the gateway to exit
wait $GATEWAY_PID
