#!/usr/bin/env bash
# Weekly backup of ~/.openclaw to ~/Workspace/backups/
# Excludes node_modules, .git internals, sessions, logs, and WAL files.
# Rotates to keep the last 7 backups.

set -euo pipefail

BACKUP_DIR="$HOME/Workspace/backups"
SOURCE_DIR="$HOME/.openclaw"
TIMESTAMP=$(date '+%Y-%m-%d_%H%M')
BACKUP_FILE="$BACKUP_DIR/openclaw-backup-$TIMESTAMP.tar.gz"
KEEP=7

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting OpenClaw backup..."

tar czf "$BACKUP_FILE" \
    -C "$HOME" \
    --exclude='.openclaw/node_modules' \
    --exclude='.openclaw/.git/objects' \
    --exclude='.openclaw/workspace/.git/objects' \
    --exclude='.openclaw/agents/*/sessions' \
    --exclude='.openclaw/logs' \
    --exclude='.openclaw/delivery-queue' \
    --exclude='.openclaw/qqbot' \
    --exclude='.openclaw/telegram' \
    --exclude='.openclaw/dashboard/.token-usage-cache.json' \
    --exclude='*.sqlite-wal' \
    --exclude='*.sqlite-shm' \
    --exclude='*.sqlite-journal' \
    --exclude='.DS_Store' \
    .openclaw/ 2>&1

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(date)] Backup complete: $BACKUP_FILE ($SIZE)"

# Rotate old backups — keep the newest $KEEP files
cd "$BACKUP_DIR"
TOTAL=$(ls -1 openclaw-backup-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
if [ "$TOTAL" -gt "$KEEP" ]; then
    REMOVE=$((TOTAL - KEEP))
    ls -1t openclaw-backup-*.tar.gz | tail -"$REMOVE" | while read -r f; do
        echo "[$(date)] Removing old backup: $f"
        rm -f "$f"
    done
fi

echo "[$(date)] Done. $TOTAL backups, keeping newest $KEEP."
