# start.sh

## What it is

The main JustinOS initialization script. Bootstraps the full personal AI stack on startup.

## What it does

Runs a sequenced startup procedure:

1. **Loads environment** — sources `.env` and `~/.config/ai/.env` for API keys and config
2. **Generates auth profiles** — prepares any credential files needed downstream
3. **Starts OpenClaw gateway** — the central agent routing service
4. **Validates Telegram bot** — confirms the Telegram webhook and bot token are live
5. **Checks voice systems** — verifies Ollama, Qwen3-TTS, and Whisper are reachable
6. **Runs time orchestrator** — kicks off any scheduled time-based automation tasks

If a component fails its health check, the script logs the failure and continues rather than halting.

## How to run

```bash
bash start.sh
```

Typically invoked automatically on login via a macOS LaunchAgent, or manually after a reboot to bring the full stack online.

## Dependencies

- Ollama running locally (`ollama serve`)
- Qwen3-TTS server (`qwen3-tts/server.py`)
- OpenClaw gateway configured and installed
- Telegram bot token in environment (`TELEGRAM_BOT_TOKEN`)
- `.env` or `~/.config/ai/.env` with required API keys
