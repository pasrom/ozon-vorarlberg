#!/bin/bash
#
# refresh_archive.sh — EEA-Archiv nachziehen (LaunchDaemon io.ebs.agent.ozon-archive).
#
# Taeglich um 04:17. --refresh ist zwingend: ohne ihn bleibt der E2a-Container
# im Cache stehen und die Reihe endet dort, wo sie beim ersten Lauf endete.
# Laedt rund 26 MB und braucht etwa 20 Sekunden.
#
# Eigener Lock, damit ein langer Archivlauf den 20-Minuten-Deploy nicht
# blockiert; die beiden Jobs schreiben verschiedene Dateien.

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
    ( set -a; . "$SECRETS"; set +a; "$NOTIFY" "Ozon Vorarlberg: $1" ) >/dev/null 2>&1 || true
}

LOCK="$STATE_DIR/archive.lock"
mkdir "$LOCK" 2>/dev/null || { log "laeuft bereits, uebersprungen"; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

log "EEA-Archiv wird nachgezogen"
if ! "$PY" eea_archive.py --build --refresh --quiet >>"$LOG" 2>&1; then
    log "FEHLER: Archivlauf fehlgeschlagen"
    alarm "Archivlauf fehlgeschlagen (siehe $LOG)"
    exit 1
fi

UP=$("$PY" -c 'import json;u=json.load(open("archive.json")).get("upstream",{});print(u.get("last_modified","?"),"->",u.get("newest_value","?"))' 2>/dev/null || echo "?")
date -u +%s > "$STATE_DIR/archive.heartbeat"
log "archive.json neu gebaut  (Container geschrieben: $UP)"
