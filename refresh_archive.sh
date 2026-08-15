#!/bin/bash
#
# refresh_archive.sh — pull the EEA archive forward (LaunchDaemon
# io.ebs.agent.ozon-archive).
#
# Runs daily at 04:17. --refresh is mandatory: without it the E2a container
# stays in the cache and the series ends wherever it ended on the first run.
# Downloads about 26 MB and takes roughly 20 seconds.
#
# Separate lock so a long archive run cannot block the 20-minute deploy; the
# two jobs write different files.

set -euo pipefail
cd "$(dirname "$0")"

JOB="ozon"
LOG_DIR="${OZON_LOG_DIR:-$HOME/agents/logs/$JOB}"
STATE_DIR="${OZON_STATE_DIR:-$HOME/agents/state/$JOB}"
SECRETS="${OZON_SECRETS:-$HOME/agents/secrets/telegram.env}"
NOTIFY="${OZON_NOTIFY:-$HOME/agents/bin/notify.sh}"
PY="${OZON_PYTHON:-$(pwd)/.venv/bin/python3}"

mkdir -p "$LOG_DIR" "$STATE_DIR"
LOG="$LOG_DIR/archive-$(date +%Y-%m-%d).log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

alarm() {
    [ -x "$NOTIFY" ] && [ -r "$SECRETS" ] || return 0
    ( set -a; . "$SECRETS"; set +a; "$NOTIFY" "Ozone Vorarlberg: $1" ) >/dev/null 2>&1 || true
}

LOCK="$STATE_DIR/archive.lock"
mkdir "$LOCK" 2>/dev/null || { log "already running, skipped"; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

log "pulling the EEA archive forward"
if ! "$PY" eea_archive.py --build --refresh --quiet >>"$LOG" 2>&1; then
    log "ERROR: archive run failed"
    alarm "archive run failed (see $LOG)"
    exit 1
fi

UP=$("$PY" -c 'import json;u=json.load(open("archive.json")).get("upstream",{});print(u.get("last_modified","?"),"->",u.get("newest_value","?"))' 2>/dev/null || echo "?")
date -u +%s > "$STATE_DIR/archive.heartbeat"
log "archive.json rebuilt  (container written: $UP)"
