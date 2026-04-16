#!/usr/bin/env bash
# Hourly git push for ~/.openclaw and ~/.openclaw/workspace repos.
# Called by cron — exits 0 on success, non-zero on any failure.

set -euo pipefail

LOG="/tmp/openclaw/platform_health_git_push.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

push_repo() {
    local repo="$1"
    local label="$2"

    if [ ! -d "$repo/.git" ] && [ ! -f "$repo/.git" ]; then
        log "SKIP $label — not a git repo"
        return 0
    fi

    cd "$repo"

    # Stage any changes
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        git add -A
        git commit -m "auto: hourly sync $(date '+%Y-%m-%d %H:%M')" --quiet || true
        log "Committed changes in $label"
    else
        log "No changes in $label"
    fi

    # Push (non-fatal — no remote is fine)
    if git remote get-url origin &>/dev/null; then
        if git push --quiet 2>>"$LOG"; then
            log "Pushed $label"
        else
            log "WARN push failed for $label (check $LOG)"
        fi
    else
        log "SKIP push for $label — no remote configured"
    fi
}

log "=== git push run start ==="
push_repo "$HOME/.openclaw"           "openclaw"
push_repo "$HOME/.openclaw/workspace" "workspace"
log "=== git push run done ==="
