# Scripts Manifest
> Auto-maintained master list of all scripts in this directory.
> Run `python3 _generate_manifest.py` to regenerate from current state.
> Last updated: 2026-04-27

---

## Root Scripts

| Script | Type | Description |
|--------|------|-------------|
| `ai_watchdog.sh` | Shell | A bash watchdog daemon that monitors three critical local AI services and automatically restarts them via `launchctl` if |
| `benchmark.py` | Python | A performance benchmarking script for local Ollama models. Measures inference speed in tokens per second across a standa |
| `cloud_storage_cleanup_script.py` | Python | A duplicate file scanner and mover for Dropbox and Google Drive local sync folders. |
| `cowork-guide-screenshots-extra.js` | Node.js | — |
| `cowork-guide-screenshots.js` | Node.js | — |
| `decision_tracker.py` | Python | A stub / placeholder module for a decision tracking system. Not yet implemented. |
| `fix-openclaw-models.sh` | Shell | — |
| `followup_engine.py` | Python | A stub / placeholder module for a follow-up automation engine. Not yet implemented. |
| `identity_tracker.py` | Python | A stub / placeholder module for identity/persona tracking across the agent system. Not yet implemented. |
| `martell_classifier_llm.py` | Python | An LLM-powered classifier that categorizes messages into Dan Martell's leverage framework categories. |
| `mode_classifier_llm.py` | Python | An LLM-powered classifier that routes messages to the correct operating mode within the OpenClaw agent system. |
| `mode_logger.py` | Python | A stub / placeholder module for logging mode routing decisions. Not yet implemented. |
| `mode_router.py` | Python | The central message routing logic for the OpenClaw agent system. Classifies an incoming message and maps it to the appro |
| `obsidian_voice_processor.py` | Python | A voice note processing pipeline that monitors iCloud for new recordings, transcribes them, categorizes them with AI, an |
| `openclaw_backup.sh` | Shell | — |
| `platform_health_git_push.sh` | Shell | — |
| `podcast_video.py` | Python | — |
| `qdrant_indexer.py` | Python | A stub / placeholder module for indexing documents into the local Qdrant vector database. Not yet implemented. |
| `reputation.py` | Python | A stub / placeholder module for a reputation scoring system. Not yet implemented. |
| `route_models.py` | Python | A configuration file that maps operating modes to specific OpenClaw agent IDs and OpenAI model IDs. |
| `run_voice_processor.sh` | Shell | — |
| `start.sh` | Shell | The main JustinOS initialization script. Bootstraps the full personal AI stack on startup. |
| `sync_obsidian.py` | Python | A stub / placeholder module for syncing Obsidian vault content to external systems. Not yet implemented. |
| `tasks_to_reminders.py` | Python | A sync script that reads task Markdown files from your Obsidian Life OS and creates corresponding entries in Apple Remin |
| `telegram-router.js` | Node.js | A Node.js Telegram webhook server that receives messages from your Telegram bot and routes them to the appropriate OpenC |
| `telegram_send.py` | Python | A stub / placeholder module for sending messages to Telegram. Not yet implemented as a real sender. |
| `twilio_morning_call.py` | Python | A daily morning briefing system that calls your phone at 7:00 AM PT with an AI-generated audio briefing built from your  |

---

## Subdirectory Projects

| Directory | Description | Entry Point |
|-----------|-------------|-------------|
| `Applio/` | <h1 align="center"> | `python3 app.py` |
| `Playwright/` | A minimal Playwright browser automation script. Currently a starter/test file. | `—` |
| `anteage-crawler/` | A price monitoring scraper for anteage.com. Watches for significant price drops and sends email alerts. | `python3 crawler.py` |
| `anteage-monitor/` | — | `python3 crawler.py` |
| `audio_cache/` | — | `—` |
| `chatgpt_importer/` | A script that converts a ChatGPT conversation export into organized, AI-synthesized Markdown notes in your Obsidian vaul | `—` |
| `crm-followup/` | — | `—` |
| `cron/` | — | `—` |
| `cron-job-automation/` | — | `—` |
| `dan-realtime/` | A real-time voice conversation interface powered by the OpenAI Realtime API. Speaks as "Dan" — a high-agency operator pe | `—` |
| `gmail-automation/` | Version 3.0 – March 2026 | `python3 main.py` |
| `platform_health/` | — | `python3 main.py` |
| `qwen-voice/` | A push-to-talk voice assistant that runs entirely on local models. Hold Space to talk, release to get a spoken response. | `python3 voice_loop.py` |
| `qwen3-tts/` | A local text-to-speech server that implements the OpenAI-compatible `/v1/audio/speech` API endpoint. Uses Qwen3-TTS with | `python3 server.py` |
| `security_council/` | — | `python3 main.py` |
| `services-dashboard/` | — | `—` |
| `voice-finetune/` | A pipeline for fine-tuning a custom voice model from reference audio. Produces a voice clone usable in the Qwen3-TTS ser | `—` |
| `weekly-briefing/` | — | `—` |
| `yt-dlp/` | <!-- MANPAGE: BEGIN EXCLUDED SECTION --> | `—` |

---

## Adding a New Script

1. Create the script in this directory (or a subdirectory)
2. Create a companion `.md` README named after the script (e.g. `myscript.md` or `mydir/README.md`)
3. Run `python3 _generate_manifest.py` to update this file
4. The updated `SCRIPTS.md` is automatically reflected in OpenClaw agent memory
