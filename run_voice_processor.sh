#!/bin/bash
# Wrapper for obsidian_voice_processor.py — run by launchd
# Loads API keys from the shared .env and runs the script in the project venv.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_PATH="/Users/justinsmith/.config/ai/.env"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"
PROCESSOR="$SCRIPT_DIR/obsidian_voice_processor.py"

# Load environment variables
if [ -f "$ENV_PATH" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_PATH"
    set +a
else
    echo "ERROR: .env not found at $ENV_PATH" >&2
    exit 1
fi

# Validate the API key is present
if [ -z "$OPENAI_API_KEY" ]; then
    echo "ERROR: OPENAI_API_KEY not set in $ENV_PATH" >&2
    exit 1
fi

exec "$VENV_PYTHON" "$PROCESSOR"
