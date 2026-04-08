# ai_watchdog.sh

## What it is

A bash watchdog daemon that monitors three critical local AI services and automatically restarts them via `launchctl` if they go down.

## What it does

Runs in an infinite loop, polling every 2 minutes. For each service it checks:

- **Ollama** — local LLM inference server
- **Qdrant** — local vector database
- **OpenClaw gateway** — the personal agent routing layer

If a service is found to be unresponsive, the script restarts it using `launchctl kickstart` (macOS LaunchAgent / LaunchDaemon mechanism). Each check and restart event is logged.

## How to run

```bash
bash ai_watchdog.sh
```

It's designed to run as a background process, typically managed by a macOS LaunchAgent so it starts on login and stays alive. To run manually in the foreground:

```bash
bash /Users/justinsmith/Workspace/scripts/ai_watchdog.sh &
```

To wire it up as a LaunchAgent, create a plist in `~/Library/LaunchAgents/` pointing at the script and load it with:

```bash
launchctl load ~/Library/LaunchAgents/com.justinos.ai_watchdog.plist
```

## Dependencies

- macOS (uses `launchctl`)
- Ollama, Qdrant, and OpenClaw gateway installed and registered as LaunchAgents/Daemons
