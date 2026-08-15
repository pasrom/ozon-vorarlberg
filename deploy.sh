#!/bin/bash
#
# deploy.sh — Werte holen, Dashboard bauen, auf gh-pages schieben.
#
# Laeuft als LaunchDaemon io.ebs.agent.ozon unter dem Benutzer `agent`,
# dreimal pro Stunde. Ablauf:
#   1. Live-Werte holen, data.json bauen, Log auf 4 Tage kuerzen
#   2. _site/ zusammenstellen (Dashboard + JSON, ~240 KB)
#   3. gh-pages als einzelnen Commit neu schreiben und force-pushen
#
# Warum das Eigenlog das 72-h-Fenster allein tragen muss: das EEA-Archiv wird
# nur etwa EINMAL TAEGLICH neu geschrieben (gemessen am Last-Modified des
# Blobs). Sein Verzug schwankt daher zwischen rund 1 h direkt nach dem
# Schreibvorgang und rund 25 h davor. Als Fueller fuer die letzten Stunden ist
# es unbrauchbar — es liefert die Langzeitkennzahlen, den Verlauf loggen wir
# selbst.
#
# Konventionen des Agent-Servers, die hier umgesetzt sind
# (brain: tools-workflow/concepts/mac-mini-agent-server.md):
#
#   * Single-Instance-Lock ueber mkdir. flock(1) gibt es auf macOS nicht.
#     Der trap raeumt den Lock bei jedem Exit ab, auch bei Signal.
#   * Heartbeat-Datei nach jedem erfolgreichen Lauf, damit ein heartbeat-check
#     merkt, wenn der Job stillsteht.
#   * Telegram-Alarm nur bei Fehlschlag ueber ~/agents/bin/notify.sh.
#     Erfolgreiche Laeufe schweigen — sonst wird der Kanal zu Rauschen.
#   * Logs unter ~/agents/logs/ozon/, dort greift die newsyslog-Rotation.
#
# Der Push laeuft ueber einen SSH-Deploy-Key, nicht ueber den
# git-credential-Helper: launchd-Jobs haben keinen Schluesselbund-Zugriff,
# und dann haengt der Push still.

set -euo pipefail
cd "$(dirname "$0")"

REPO_DIR="$(pwd)"
SITE_DIR="$REPO_DIR/_site"
BRANCH="gh-pages"

JOB="ozon"
LOG_DIR="${OZON_LOG_DIR:-$HOME/agents/logs/$JOB}"
STATE_DIR="${OZON_STATE_DIR:-$HOME/agents/state/$JOB}"
SECRETS="${OZON_SECRETS:-$HOME/agents/secrets/telegram.env}"
NOTIFY="${OZON_NOTIFY:-$HOME/agents/bin/notify.sh}"
KEY="${OZON_DEPLOY_KEY:-$HOME/.ssh/id_ed25519_ozon}"
PY="${OZON_PYTHON:-$REPO_DIR/.venv/bin/python3}"

mkdir -p "$LOG_DIR" "$STATE_DIR"
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }
log()   { echo "[$(stamp)] $*" | tee -a "$LOG"; }

# Telegram nur im Fehlerfall. Schlaegt das Senden selbst fehl, darf das den
# Job nicht zusaetzlich abbrechen — der eigentliche Fehler ist wichtiger.
alarm() {
    [ -x "$NOTIFY" ] || return 0
    [ -r "$SECRETS" ] || return 0
    ( set -a; . "$SECRETS"; set +a; "$NOTIFY" "Ozon Vorarlberg: $1" ) \
        >/dev/null 2>&1 || true
}

fail() { log "FEHLER: $*"; alarm "$1"; exit 1; }

# --- Single-Instance-Lock ---------------------------------------------------
LOCK="$STATE_DIR/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    # Steht der Lock laenger als 30 min, haengt ein Vorlauf. Melden, aber
    # nicht selbst loeschen — das wuerde zwei parallele Laeufe erlauben.
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
        log "Lock ist aelter als 30 min — haengt der vorherige Lauf?"
        alarm "Lock haengt seit ueber 30 min ($LOCK)"
    else
        log "laeuft bereits, uebersprungen"
    fi
    exit 0
fi
cleanup() { rm -rf "${TMP:-}" 2>/dev/null || true; rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup EXIT

# --- 1) Daten holen ---------------------------------------------------------
[ -x "$PY" ] || fail "Python fehlt: $PY (mini/install.sh laufen lassen)"

log "Abruf vorarlberg-luft.at"
if ! "$PY" ozon_vorarlberg.py --log --strict --prune-history 4 \
        --out data.json --quiet >>"$LOG" 2>&1; then
    fail "Scraper fehlgeschlagen — Quelle offline oder Layout geaendert (siehe $LOG)"
fi
[ -s data.json ] || fail "data.json ist leer"

# --- 2) _site zusammenstellen ----------------------------------------------
rm -rf "$SITE_DIR"; mkdir -p "$SITE_DIR"
cp ozon_dashboard.html "$SITE_DIR/index.html"
cp data.json "$SITE_DIR/data.json"
[ -f archive.json ] && cp archive.json "$SITE_DIR/archive.json"
touch "$SITE_DIR/.nojekyll"      # sonst schluckt Pages Dateien mit Unterstrich
log "_site gebaut ($(du -sk "$SITE_DIR" | cut -f1) KB)"

# --- 3) Pushen --------------------------------------------------------------
REMOTE=$(git remote get-url origin 2>/dev/null || true)
[ -n "$REMOTE" ] || fail "kein origin-Remote gesetzt"

case "$REMOTE" in
    git@*|ssh://*)
        [ -f "$KEY" ] || fail "Deploy-Key $KEY fehlt"
        export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        ;;
    *)
        # GIT_SSH_COMMAND wirkt nur auf SSH-Remotes. Bei HTTPS griffe git zum
        # credential-Helper, der unter launchd keinen Schluesselbund hat.
        fail "origin ist HTTPS ($REMOTE) — unter launchd unbrauchbar, auf SSH umstellen"
        ;;
esac

TMP=$(mktemp -d)
git -C "$TMP" init -q
git -C "$TMP" remote add origin "$REMOTE"
git -C "$TMP" checkout -q --orphan "$BRANCH"
cp -R "$SITE_DIR"/. "$TMP"/
git -C "$TMP" add -A
git -C "$TMP" \
    -c user.name="ozon-vorarlberg deploy" \
    -c user.email="deploy@localhost" \
    commit -q -m "Stand $(date '+%Y-%m-%d %H:%M %Z')"

if ! git -C "$TMP" push -q --force origin "$BRANCH" 2>>"$LOG"; then
    fail "Push fehlgeschlagen — Deploy-Key ohne Schreibrecht? (siehe $LOG)"
fi

date -u +%s > "$STATE_DIR/heartbeat"
log "gh-pages aktualisiert"
