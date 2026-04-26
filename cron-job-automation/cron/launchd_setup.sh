#!/usr/bin/env bash
# ============================================================
# launchd_setup.sh — Install/remove macOS LaunchAgent plists
# for local cron jobs that need access to local resources.
#
# Usage:
#   ./launchd_setup.sh install   # Write plists + load agents
#   ./launchd_setup.sh remove    # Unload + remove plists
#   ./launchd_setup.sh status    # Show agent status
#
# These jobs run locally (access Obsidian, Apple Reminders, etc.)
# High-frequency jobs that can't use remote CCR triggers.
# ============================================================
set -euo pipefail

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRON_DIR="${SCRIPTS_DIR}/cron"
LOG_DIR="$HOME/Library/Logs/openclaw-cron"

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

# ── Plist generator ──────────────────────────────────────────────────────────

write_plist() {
    local label="$1"
    local interval_secs="$2"    # StartInterval (seconds)
    local script="$3"
    local log_name="${label//./-}"

    cat > "${LAUNCH_AGENTS_DIR}/${label}.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CRON_DIR}/run_job.sh</string>
        <string>${label}</string>
        <string>--interval</string>
        <string>${4:-}</string>
        <string>--timeout</string>
        <string>${5:-120}</string>
        <string>--no-notify</string>
        <string>--</string>
        <string>/usr/bin/python3</string>
        <string>${script}</string>
    </array>
    <key>StartInterval</key>
    <integer>${interval_secs}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${log_name}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${log_name}.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>${SCRIPTS_DIR}</string>
</dict>
</plist>
EOF
}

write_calendar_plist() {
    local label="$1"
    local minute="$2"
    local hour="$3"
    local weekday="$4"   # empty = daily, 0-6 = weekly
    local script="$5"
    local log_name="${label//./-}"

    local weekday_xml=""
    if [[ -n "$weekday" ]]; then
        weekday_xml="<key>Weekday</key><integer>${weekday}</integer>"
    fi

    cat > "${LAUNCH_AGENTS_DIR}/${label}.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CRON_DIR}/run_job.sh</string>
        <string>${label}</string>
        <string>--interval</string>
        <string>daily</string>
        <string>--timeout</string>
        <string>600</string>
        <string>--</string>
        <string>/usr/bin/python3</string>
        <string>${script}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>${hour}</integer>
        <key>Minute</key><integer>${minute}</integer>
        ${weekday_xml}
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${log_name}.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${log_name}.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>${SCRIPTS_DIR}</string>
</dict>
</plist>
EOF
}

# ── Job definitions ──────────────────────────────────────────────────────────

JOBS=(
    # label | interval_secs | script | interval_flag | timeout
    "com.openclaw.obsidian-voice-processor|600|${SCRIPTS_DIR}/obsidian_voice_processor.py|10m|120"
    "com.openclaw.tasks-to-reminders|600|${SCRIPTS_DIR}/tasks_to_reminders.py|10m|60"
    "com.openclaw.cron-health-check|1800|${CRON_DIR}/health_check.py|30m|60"
)

CALENDAR_JOBS=(
    # label | minute | hour | weekday | script
    "com.openclaw.obsidian-index|0|0||${SCRIPTS_DIR}/qdrant_indexer.py"
    "com.openclaw.tasks-weekly-archive|0|21|0|${SCRIPTS_DIR}/tasks_to_reminders.py"
)

# ── Install ──────────────────────────────────────────────────────────────────

do_install() {
    echo "Installing LaunchAgent plists to $LAUNCH_AGENTS_DIR ..."

    for entry in "${JOBS[@]}"; do
        IFS='|' read -r label interval script interval_flag timeout <<< "$entry"
        write_plist "$label" "$interval" "$script" "$interval_flag" "$timeout"
        launchctl load -w "${LAUNCH_AGENTS_DIR}/${label}.plist" 2>/dev/null || \
            launchctl unload "${LAUNCH_AGENTS_DIR}/${label}.plist" 2>/dev/null && \
            launchctl load -w "${LAUNCH_AGENTS_DIR}/${label}.plist"
        echo "  ✅ $label (every ${interval}s)"
    done

    for entry in "${CALENDAR_JOBS[@]}"; do
        IFS='|' read -r label minute hour weekday script <<< "$entry"
        write_calendar_plist "$label" "$minute" "$hour" "$weekday" "$script"
        launchctl load -w "${LAUNCH_AGENTS_DIR}/${label}.plist" 2>/dev/null || \
            launchctl unload "${LAUNCH_AGENTS_DIR}/${label}.plist" 2>/dev/null && \
            launchctl load -w "${LAUNCH_AGENTS_DIR}/${label}.plist"
        echo "  ✅ $label (calendar)"
    done

    echo ""
    echo "Logs: $LOG_DIR"
}

# ── Remove ───────────────────────────────────────────────────────────────────

do_remove() {
    echo "Removing LaunchAgent plists ..."
    all_jobs=("${JOBS[@]}" "${CALENDAR_JOBS[@]}")
    for entry in "${all_jobs[@]}"; do
        label="$(echo "$entry" | cut -d'|' -f1)"
        plist="${LAUNCH_AGENTS_DIR}/${label}.plist"
        if [[ -f "$plist" ]]; then
            launchctl unload -w "$plist" 2>/dev/null || true
            rm -f "$plist"
            echo "  🗑  $label removed"
        fi
    done
}

# ── Status ───────────────────────────────────────────────────────────────────

do_status() {
    echo "LaunchAgent status:"
    all_jobs=("${JOBS[@]}" "${CALENDAR_JOBS[@]}")
    for entry in "${all_jobs[@]}"; do
        label="$(echo "$entry" | cut -d'|' -f1)"
        plist="${LAUNCH_AGENTS_DIR}/${label}.plist"
        if [[ -f "$plist" ]]; then
            pid=$(launchctl list "$label" 2>/dev/null | awk 'NR==1{print $1}')
            status=$(launchctl list "$label" 2>/dev/null | awk 'NR==1{print $2}')
            echo "  $label  PID=$pid  LastExit=$status"
        else
            echo "  $label  [not installed]"
        fi
    done
    echo ""
    echo "Recent logs in $LOG_DIR:"
    ls -lht "$LOG_DIR" 2>/dev/null | head -10 || echo "  (no logs yet)"
}

# ── Main ─────────────────────────────────────────────────────────────────────

CMD="${1:-help}"
case "$CMD" in
    install) do_install ;;
    remove)  do_remove  ;;
    status)  do_status  ;;
    *)
        echo "Usage: $0 install | remove | status"
        exit 1
        ;;
esac
