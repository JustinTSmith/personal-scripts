# obsidian_voice_processor.py

## What it is

A voice note processing pipeline that monitors iCloud for new recordings, transcribes them, categorizes them with AI, and creates organized Markdown notes in your Obsidian vault.

## What it does

Runs as a background watcher on a configured iCloud voice inbox folder. When a new audio file appears:

1. **Transcribes** — sends audio to OpenAI Whisper for speech-to-text
2. **Categorizes** — uses OpenAI to determine the note type (task, idea, journal, food diary, etc.)
3. **Routes to Obsidian** — creates a Markdown file in the appropriate vault subfolder based on category
4. **Special handling:**
   - **Mira mentions** — if the transcript references "Mira" (sleep/baby context), extracts sleep/nap data and writes to a structured sleep log
   - **Food diary** — routes food-related notes to a dedicated food diary folder
   - **Apple Reminders** — can create Reminders from task-type voice notes

Note files include the raw transcript, AI-generated summary/title, category tag, and timestamp.

## How to run

```bash
python3 obsidian_voice_processor.py
```

Runs continuously, watching the inbox folder. Typically managed as a LaunchAgent so it starts on login.

## Configuration

Edit the constants at the top of the script:

- `VOICE_INBOX` — iCloud folder to watch for new recordings
- `VAULT_PATH` — root of your Obsidian vault
- `OPENAI_API_KEY` — via environment or `.env`

## Dependencies

- Python 3
- `openai` (`pip install openai`) — for Whisper transcription and categorization
- `watchdog` (`pip install watchdog`) — folder monitoring
- macOS with iCloud Drive sync enabled
- `OPENAI_API_KEY` in environment or `.env`
