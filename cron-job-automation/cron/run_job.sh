#!/usr/bin/env bash
# ============================================================
# run_job.sh — Cron wrapper with lockfile, signal traps, timeout
#
# Usage:
#   run_job.sh <job-name> [--timeout <secs>] [--interval <interval>] -- <command...>
#
# Options:
#   --timeout  N     Kill job after N seconds
#   --interval I     Idempotency interval: daily | hourly | 6h | 30m (default: none)
#   --no-notify      Skip Telegram alerts
#
# Exit codes:
#   0  success
#   1  failure
#   2  skipped (already ran this interval)
#   3  lock held by another PID
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_LOG="${SCRIPT_DIR}/cron_log.py"
ALERT="${SCRIPT_DIR}/alert.py"
NERVE="${SCRIPT_DIR}/nerve_bridge.py"
LOCK_DIR="${TMPDIR:-/tmp}"

# ── Parse args ──────────────────────────────────────────────────────────────
JOB_NAME="${1:?Usage: run_job.sh <job-name> [options] -- <command...>}"
shift

TIMEOUT_SECS=""
INTERVAL=""
NOTIFY=true
CMD_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --timeout)   TIMEOUT_SECS="$2"; shift 2 ;;
        --interval)  INTERVAL="$2";     shift 2 ;;
        --no-notify) NOTIFY=false;       shift   ;;
        --)          shift; CMD_ARGS=("$@"); break ;;
        *)           CMD_ARGS+=("$1");   shift   ;;
    esac
done

if [[ ${#CMD_ARGS[@]} -eq 0 ]]; then
    echo "[run_job] No command provided for $JOB_NAME" >&2
    exit 1
fi

# ── Idempotency check ────────────────────────────────────────────────────────
if [[ -n "$INTERVAL" ]]; then
    result=$(python3 "$CRON_LOG" should-run "$JOB_NAME" --interval "$INTERVAL" 2>/dev/null || true)
    if [[ "$result" == "no" ]]; then
        echo "[run_job] $JOB_NAME already ran (interval=$INTERVAL), skipping"
        $NOTIFY && python3 "$ALERT" skipped "$JOB_NAME" "already ran this $INTERVAL" 2>/dev/null || true
        exit 2
    fi
fi

# ── PID lockfile ─────────────────────────────────────────────────────────────
SAFE_NAME="${JOB_NAME//[^a-zA-Z0-9_-]/-}"
LOCK_FILE="${LOCK_DIR}/cron-${SAFE_NAME}.pid"

if [[ -f "$LOCK_FILE" ]]; then
    OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[run_job] $JOB_NAME is already running (PID $OLD_PID)" >&2
        exit 3
    else
        echo "[run_job] Removing stale lockfile for $JOB_NAME (PID $OLD_PID)" >&2
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"

# ── Log start ────────────────────────────────────────────────────────────────
RUN_ID=$(python3 "$CRON_LOG" log-start "$JOB_NAME")
echo "[run_job] Started $JOB_NAME run_id=${RUN_ID:0:8}"
$NOTIFY && python3 "$ALERT" started "$JOB_NAME" "$RUN_ID" 2>/dev/null || true

# ── Cleanup handler ──────────────────────────────────────────────────────────
_cleanup() {
    local exit_code="$1"
    local signal="${2:-}"

    rm -f "$LOCK_FILE"

    if [[ $exit_code -eq 0 ]]; then
        local summary="Completed normally"
        [[ -n "$signal" ]] && summary="Exited via $signal"
        python3 "$CRON_LOG" log-end "$RUN_ID" success "$summary" 2>/dev/null || true
        $NOTIFY && python3 "$ALERT" succeeded "$JOB_NAME" "$RUN_ID" "$summary" 2>/dev/null || true
    elif [[ $exit_code -eq 124 ]]; then
        # timeout(1) exits 124 on timeout
        local summary="Timed out after ${TIMEOUT_SECS}s"
        python3 "$CRON_LOG" log-end "$RUN_ID" failure "$summary" 2>/dev/null || true
        $NOTIFY && python3 "$ALERT" failed "$JOB_NAME" "$RUN_ID" "$summary" 2>/dev/null || true
    else
        local summary="Exit code $exit_code"
        [[ -n "$signal" ]] && summary="Killed by $signal (exit $exit_code)"
        python3 "$CRON_LOG" log-end "$RUN_ID" failure "$summary" 2>/dev/null || true
        $NOTIFY && python3 "$ALERT" failed "$JOB_NAME" "$RUN_ID" "$summary" 2>/dev/null || true
    fi

    # Persistent failure detection (alert if 3+ failures in 6h)
    python3 "$CRON_LOG" check-failures "$JOB_NAME" 2>/dev/null || \
        $NOTIFY && python3 "$ALERT" raw "🚨 *PERSISTENT FAILURE — ${JOB_NAME}*\nFailed 3+ times in 6h" 2>/dev/null || true

    # Sync to Nerve dashboard
    python3 "$NERVE" push "$RUN_ID" 2>/dev/null || true
}

# Trap signals for clean shutdown
trap '_cleanup 130 SIGINT;  exit 130' INT
trap '_cleanup 143 SIGTERM; exit 143' TERM
trap '_cleanup 129 SIGHUP;  exit 129' HUP

# ── Execute ──────────────────────────────────────────────────────────────────
EXIT_CODE=0

if [[ -n "$TIMEOUT_SECS" ]]; then
    timeout "$TIMEOUT_SECS" "${CMD_ARGS[@]}" || EXIT_CODE=$?
else
    "${CMD_ARGS[@]}" || EXIT_CODE=$?
fi

_cleanup "$EXIT_CODE"
exit "$EXIT_CODE"
