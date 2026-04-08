# tasks_to_reminders.py

## What it is

A sync script that reads task Markdown files from your Obsidian Life OS and creates corresponding entries in Apple Reminders.

## What it does

1. Scans a configured tasks folder in your Obsidian vault (e.g. `Life OS/Tasks/`)
2. Parses Markdown task files, extracting task names, due dates, and metadata
3. Creates Apple Reminders entries via AppleScript
4. **Archiving** — once a task file is processed, optionally moves it to a weekly archive folder (e.g. `Life OS/Tasks/Archive/2026-W14/`) to keep the active task list clean

Designed to be the bridge between a plain-text task system in Obsidian and native macOS Reminders (which syncs to iPhone).

## How to run

```bash
python3 tasks_to_reminders.py
```

Can be run manually or on a schedule via cron/LaunchAgent.

To enable archiving, ensure the archive path constant at the top of the script is set to your preferred location.

## Dependencies

- Python 3
- macOS (uses AppleScript via `osascript` for Reminders integration)
- Obsidian vault with a `Life OS/Tasks/` folder structure (or equivalent — configure path in script)
