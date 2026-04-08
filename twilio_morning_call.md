# twilio_morning_call.py

## What it is

A daily morning briefing system that calls your phone at 7:00 AM PT with an AI-generated audio briefing built from your Obsidian vault.

## What it does

Full pipeline, triggered each morning:

1. **Builds briefing text** — reads relevant notes and tasks from your Obsidian vault
2. **Rewrites in Dan Martell style** — passes the content through an OpenAI prompt to frame it as a high-leverage operator briefing
3. **Synthesizes audio** — sends the text to the local Qwen3-TTS server (`127.0.0.1:8100`) and gets back a WAV file
4. **Serves audio publicly** — spins up a temporary HTTP server on a random local port, then opens a Cloudflare quick tunnel to get a public HTTPS URL (no Cloudflare account required)
5. **Places Twilio call** — calls Twilio API with TwiML `<Play>` pointing at the tunnel URL
6. **Monitors and cleans up** — polls until the call completes, then tears down the tunnel and HTTP server

Logs to `logs/twilio_morning_call.log`.

## How to run

```bash
python3 twilio_morning_call.py
```

Designed to be triggered by a macOS LaunchAgent at 7:00 AM PT daily.

### Dry run (generate audio, skip the actual call)

```bash
TWILIO_DRY_RUN=true python3 twilio_morning_call.py
```

## Environment variables

Required:
- `TWILIO_ACCOUNT_SID` — from Twilio console
- `TWILIO_AUTH_TOKEN` — from Twilio console
- `TWILIO_PHONE_NUMBER` — your Twilio outbound number (e.g. `+1xxxxxxxxxx`)
- `TWILIO_TO_NUMBER` — destination number (defaults to `+14388213786`)

Optional:
- `TWILIO_DRY_RUN=true` — skip real call, just generate audio
- `QWEN_TTS_URL` — override TTS server URL (default: `http://127.0.0.1:8100`)
- `CLOUDFLARED_BIN` — override path to `cloudflared` binary

Set in `.env` or `~/.config/ai/.env`.

## Dependencies

- Python 3
- `twilio` (`pip install twilio`)
- `cloudflared` binary installed and on PATH
- Qwen3-TTS server running at port 8100 (`cd qwen3-tts && uvicorn server:app --port 8100`)
- Obsidian vault accessible at the configured path
