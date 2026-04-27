#!/usr/bin/env bash
# Hourly git push — backs up critical repos plus restorable machine-state snapshots.
# Repos covered:
#   ~/.openclaw           → JustinTSmith/openclaw-config    (legacy root snapshot)
#   ~/.openclaw/workspace → JustinTSmith/openclaw-config    (workspace + system-state snapshots)
#   ~/Workspace/scripts   → JustinTSmith/personal-scripts   (automation scripts)
#
# Preferred snapshot source of truth:
#   ~/.openclaw/workspace/scripts/export-system-state.sh
#     → ~/.openclaw/workspace/snapshots/system-state/
#
# Legacy compatibility snapshot (still written for ~/.openclaw repo):
#   ~/.openclaw/system-config/
#
# Called by cron — exits 0 always (non-fatal design).

LOG="/tmp/openclaw/platform_health_git_push.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

# ── Snapshot system config into ~/.openclaw/system-config/ ───────────────────
snapshot_system_config() {
    local dest="$HOME/.openclaw/system-config"
    mkdir -p "$dest/LaunchAgents" "$dest/claude-skills"

    # 1. crontab
    crontab -l > "$dest/crontab.txt" 2>/dev/null || echo "# empty" > "$dest/crontab.txt"

    # 2. All LaunchAgents plists
    cp ~/Library/LaunchAgents/*.plist "$dest/LaunchAgents/" 2>/dev/null || true

    # 3. Personal skills (dirs without embedded .git repos = custom/personal)
    for skill_dir in ~/.claude/skills/*/; do
        skill_name=$(basename "$skill_dir")
        if [ ! -d "$skill_dir/.git" ] && [ "$skill_name" != "_library" ]; then
            rsync -a --delete --exclude='.git' --exclude='*/.git' "$skill_dir" "$dest/claude-skills/$skill_name/" 2>/dev/null || true
        fi
    done

    log "System config snapshot updated"
}

run_workspace_export() {
    local exporter="$HOME/.openclaw/workspace/scripts/export-system-state.sh"

    if [ -x "$exporter" ]; then
        if "$exporter" >>"$LOG" 2>&1; then
            log "Workspace system-state export updated"
        else
            log "WARN workspace system-state export failed (check $LOG)"
        fi
    else
        log "SKIP workspace export — $exporter is missing or not executable"
    fi
}

# ── Push a git repo ───────────────────────────────────────────────────────────
push_repo() {
    local repo="$1"
    local label="$2"

    if [ ! -d "$repo/.git" ] && [ ! -f "$repo/.git" ]; then
        log "SKIP $label — not a git repo"
        return 0
    fi

    cd "$repo"

    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        git add -A
        git commit -m "auto: hourly sync $(date '+%Y-%m-%d %H:%M')" --quiet || true
        log "Committed changes in $label"
    else
        log "No changes in $label"
    fi

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

# ── Main ──────────────────────────────────────────────────────────────────────
log "=== git push run start ==="

# Preferred workspace snapshot for restoreability
run_workspace_export

# Legacy root-level snapshot for compatibility with existing ~/.openclaw repo flow
snapshot_system_config

push_repo "$HOME/.openclaw/workspace"    "openclaw-workspace"
push_repo "$HOME/Workspace/scripts"      "personal-scripts"
push_repo "$HOME/.openclaw"              "openclaw-config"

log "=== git push run done ==="
