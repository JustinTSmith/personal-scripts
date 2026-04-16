#!/usr/bin/env bash
# Hourly git push — backs up all critical repos and system config snapshots.
# Repos covered:
#   ~/.openclaw           → JustinTSmith/openclaw-config      (agents, config, watchdog)
#   ~/.openclaw/workspace → JustinTSmith/openclaw-workspace   (operator memory, state)
#   ~/Workspace/scripts   → JustinTSmith/personal-scripts     (automation scripts)
# Snapshots (written into ~/.openclaw/system-config/, committed with openclaw repo):
#   crontab               → system-config/crontab.txt
#   LaunchAgents          → system-config/LaunchAgents/
#   personal skills       → system-config/claude-skills/
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
            rsync -a --delete "$skill_dir" "$dest/claude-skills/$skill_name/" 2>/dev/null || true
        fi
    done

    log "System config snapshot updated"
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

# Snapshot crontab, LaunchAgents, and personal skills before committing openclaw
snapshot_system_config

push_repo "$HOME/.openclaw"              "openclaw-config"
push_repo "$HOME/.openclaw/workspace"    "openclaw-workspace"
push_repo "$HOME/Workspace/scripts"      "personal-scripts"

log "=== git push run done ==="
