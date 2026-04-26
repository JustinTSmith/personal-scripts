#!/usr/bin/env bash
# ============================================================
# launchd_setup.sh — Install all OpenClaw cron jobs as macOS
# LaunchAgents. 100% local — zero Anthropic/cloud dependency.
#
# Usage:
#   ./launchd_setup.sh install   # Write plists + load all agents
#   ./launchd_setup.sh remove    # Unload + remove all plists
#   ./launchd_setup.sh status    # Print agent status table
#   ./launchd_setup.sh run <job> # Run one job right now (fire & wait)
#
# Logs: ~/Library/Logs/openclaw-cron/<job>.{log,err}
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/openclaw-cron"
PYTHON="$(command -v python3)"

mkdir -p "$AGENTS_DIR" "$LOG_DIR"

# ── Helpers ──────────────────────────────────────────────────────────────────

label_to_plist() { echo "${AGENTS_DIR}/${1}.plist"; }

# write_interval_plist <label> <interval_secs> <interval_flag> <timeout_secs> <command...>
write_interval_plist() {
    local label="$1" interval="$2" flag="$3" timeout="$4"
    shift 4
    local log="${LOG_DIR}/${label}"
    local -a cmd_args=()
    for arg in "$@"; do
        cmd_args+=("        <string>${arg}</string>")
    done

    cat > "$(label_to_plist "$label")" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>           <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/run_job.sh</string>
        <string>${label}</string>
        <string>--interval</string>  <string>${flag}</string>
        <string>--timeout</string>   <string>${timeout}</string>
        <string>--</string>
$(printf '%s\n' "${cmd_args[@]}")
    </array>
    <key>StartInterval</key>   <integer>${interval}</integer>
    <key>RunAtLoad</key>       <false/>
    <key>StandardOutPath</key> <string>${log}.log</string>
    <key>StandardErrorPath</key><string>${log}.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>  <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>  <string>${HOME}</string>
        <key>CRON_LOG_DB</key><string>${HOME}/.openclaw/cron_log.db</string>
        <key>TELEGRAM_BOT_TOKEN</key><string>${TELEGRAM_BOT_TOKEN}</string>
        <key>CRON_UPDATES_CHAT_ID</key><string>${CRON_UPDATES_CHAT_ID}</string>
    </dict>
    <key>WorkingDirectory</key><string>${REPO_DIR}</string>
</dict>
</plist>
PLIST
}

# write_calendar_plist <label> <flag> <timeout_secs> <cal_xml> <command...>
# cal_xml: the StartCalendarInterval dict contents as a string
write_calendar_plist() {
    local label="$1" flag="$2" timeout="$3" cal_xml="$4"
    shift 4
    local log="${LOG_DIR}/${label}"
    local -a cmd_args=()
    for arg in "$@"; do
        cmd_args+=("        <string>${arg}</string>")
    done

    cat > "$(label_to_plist "$label")" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>           <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/run_job.sh</string>
        <string>${label}</string>
        <string>--interval</string>  <string>${flag}</string>
        <string>--timeout</string>   <string>${timeout}</string>
        <string>--</string>
$(printf '%s\n' "${cmd_args[@]}")
    </array>
    <key>StartCalendarInterval</key>
    ${cal_xml}
    <key>RunAtLoad</key>       <false/>
    <key>StandardOutPath</key> <string>${log}.log</string>
    <key>StandardErrorPath</key><string>${log}.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>  <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>  <string>${HOME}</string>
        <key>CRON_LOG_DB</key><string>${HOME}/.openclaw/cron_log.db</string>
        <key>TELEGRAM_BOT_TOKEN</key><string>${TELEGRAM_BOT_TOKEN}</string>
        <key>CRON_UPDATES_CHAT_ID</key><string>${CRON_UPDATES_CHAT_ID}</string>
    </dict>
    <key>WorkingDirectory</key><string>${REPO_DIR}</string>
</dict>
</plist>
PLIST
}

# ── Load / unload helpers ─────────────────────────────────────────────────────

load_plist() {
    local label="$1"
    local plist; plist="$(label_to_plist "$label")"
    # Unload first (ignore errors) then reload
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load -w "$plist"
}

unload_plist() {
    local label="$1"
    local plist; plist="$(label_to_plist "$label")"
    [[ -f "$plist" ]] && launchctl unload -w "$plist" 2>/dev/null || true
}

# ── Job definitions ───────────────────────────────────────────────────────────
# All labels must match job names used in run_job.sh / cron_log.py

define_jobs() {
    # ── Every 10 min ─────────────────────────────────────────────────────────
    write_interval_plist \
        "com.openclaw.obsidian-voice-processor" 600 "10m" 120 \
        "$PYTHON" "${REPO_DIR}/obsidian_voice_processor.py"

    write_interval_plist \
        "com.openclaw.tasks-to-reminders" 600 "10m" 60 \
        "$PYTHON" "${REPO_DIR}/tasks_to_reminders.py"

    # ── Every 30 min (health check) ───────────────────────────────────────────
    write_interval_plist \
        "com.openclaw.cron-health-check" 1800 "30m" 60 \
        "$PYTHON" "${SCRIPT_DIR}/health_check.py"

    # ── Daily midnight — Obsidian → Qdrant reindex ───────────────────────────
    write_calendar_plist \
        "com.openclaw.obsidian-index" "daily" 600 \
        "<dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>0</integer></dict>" \
        "$PYTHON" "${REPO_DIR}/qdrant_indexer.py"

    # ── Monday 9am — model usage weekly report ────────────────────────────────
    write_calendar_plist \
        "com.openclaw.model-usage-weekly" "daily" 120 \
        "<dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>" \
        "$PYTHON" "${REPO_DIR}/benchmark.py"

    # ── Sunday 8pm — Gmail weekly digest ─────────────────────────────────────
    write_calendar_plist \
        "com.openclaw.gmail-weekly-digest" "daily" 300 \
        "<dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>" \
        "$PYTHON" "${REPO_DIR}/gmail-automation/main.py"

    # ── 1st of month 3am — iMessage relationship analysis ────────────────────
    write_calendar_plist \
        "com.openclaw.imessage-relationships-monthly" "daily" 600 \
        "<dict><key>Day</key><integer>1</integer><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>" \
        "$PYTHON" "${REPO_DIR}/crm-followup/followup.py"

    # ── Sunday 2am — Dropbox/Drive dedupe ────────────────────────────────────
    write_calendar_plist \
        "com.openclaw.dropbox-drive-dedupe" "daily" 900 \
        "<dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer></dict>" \
        "$PYTHON" "${REPO_DIR}/cloud_storage_cleanup_script.py"

    # ── Sunday 9pm — weekly task archive ─────────────────────────────────────
    write_calendar_plist \
        "com.openclaw.tasks-weekly-archive" "daily" 120 \
        "<dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>" \
        "$PYTHON" "${REPO_DIR}/tasks_to_reminders.py"
}

ALL_LABELS=(
    com.openclaw.obsidian-voice-processor
    com.openclaw.tasks-to-reminders
    com.openclaw.cron-health-check
    com.openclaw.obsidian-index
    com.openclaw.model-usage-weekly
    com.openclaw.gmail-weekly-digest
    com.openclaw.imessage-relationships-monthly
    com.openclaw.dropbox-drive-dedupe
    com.openclaw.tasks-weekly-archive
)

# ── Commands ──────────────────────────────────────────────────────────────────

do_install() {
    # Load env vars so they get baked into the plist EnvironmentVariables blocks
    local env_file="$HOME/.config/ai/.env"
    if [[ -f "$env_file" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$env_file"
        set +a
        echo "Loaded env from $env_file"
    fi

    echo "Checking dependencies ..."
    "$PYTHON" -c "import requests" 2>/dev/null || {
        echo "  Installing requests ..."
        "$PYTHON" -m pip install --quiet requests
    }
    echo "  ✅ requests"

    echo ""
    echo "Writing LaunchAgent plists → $AGENTS_DIR"
    define_jobs

    echo "Loading agents ..."
    for label in "${ALL_LABELS[@]}"; do
        load_plist "$label"
        echo "  ✅ $label"
    done

    echo ""
    echo "Done. Logs: $LOG_DIR"
    echo "Run './launchd_setup.sh status' to verify."
}

do_remove() {
    echo "Unloading and removing LaunchAgent plists ..."
    for label in "${ALL_LABELS[@]}"; do
        unload_plist "$label"
        rm -f "$(label_to_plist "$label")"
        echo "  🗑  $label"
    done
    echo "Done."
}

do_status() {
    printf "%-50s %-6s %-10s\n" "LABEL" "PID" "LAST_EXIT"
    printf '%s\n' "$(printf '─%.0s' {1..70})"
    for label in "${ALL_LABELS[@]}"; do
        local plist; plist="$(label_to_plist "$label")"
        if [[ ! -f "$plist" ]]; then
            printf "%-50s %-6s %-10s\n" "$label" "-" "NOT INSTALLED"
            continue
        fi
        local info; info=$(launchctl list "$label" 2>/dev/null || echo "- - -")
        local pid;  pid=$(echo "$info"  | awk 'NR==1{print $1}')
        local exit; exit=$(echo "$info" | awk 'NR==1{print $2}')
        printf "%-50s %-6s %-10s\n" "$label" "${pid:--}" "${exit:--}"
    done
    echo ""
    echo "Logs: $LOG_DIR"
    ls -lht "$LOG_DIR" 2>/dev/null | head -12 || echo "  (no logs yet)"
}

do_run() {
    local job="${1:?Usage: $0 run <job-label>}"
    # Allow short name (e.g. "health-check") or full label
    [[ "$job" != com.openclaw.* ]] && job="com.openclaw.${job}"
    local plist; plist="$(label_to_plist "$job")"
    [[ -f "$plist" ]] || { echo "Not installed: $job"; exit 1; }
    echo "Running $job ..."
    launchctl start "$job"
}

# ── Main ──────────────────────────────────────────────────────────────────────

CMD="${1:-help}"
case "$CMD" in
    install) do_install         ;;
    remove)  do_remove          ;;
    status)  do_status          ;;
    run)     do_run "${2:-}"    ;;
    *)
        echo "Usage: $0 install | remove | status | run <job>"
        echo ""
        echo "Jobs:"
        for l in "${ALL_LABELS[@]}"; do echo "  $l"; done
        exit 1
        ;;
esac
