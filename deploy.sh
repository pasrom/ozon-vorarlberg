#!/bin/bash
#
# deploy.sh — Dashboard bauen und auf den gh-pages-Branch schieben.
#
# Wird vom launchd-Job auf dem Mac mini aufgerufen. Ablauf:
#   1. Live-Werte holen, data.json bauen, Log auf 4 Tage kuerzen
#   2. _site/ zusammenstellen (Dashboard + JSON, ~230 KB)
#   3. gh-pages als Waise neu schreiben und force-pushen
#
# Der Push laeuft ueber einen SSH-Deploy-Key, NICHT ueber den
# git-credential-Helper: launchd-Jobs haben oft keinen Zugriff auf den
# Schluesselbund, und dann haengt der Push still. Der Key liegt in
# ~/.ssh/ozon_deploy und wird von setup_mini.sh angelegt.
#
# gh-pages wird bei jedem Lauf als einzelner Commit neu geschrieben (force).
# Bei einem Lauf alle 20 Minuten waeren es sonst 72 Commits pro Tag, die
# niemand je liest.
#
# Warum das Eigenlog das 72-h-Fenster allein tragen muss: das EEA-Archiv wird
# nur etwa EINMAL TAeGLICH neu geschrieben (gemessen am Last-Modified des
# Blobs). Sein Verzug schwankt daher zwischen rund 1 h direkt nach dem
# Schreibvorgang und rund 25 h davor. Als Fueller fuer die letzten Stunden ist
# es damit unbrauchbar - es liefert die Langzeitkennzahlen, den Verlauf loggen
# wir selbst.

set -euo pipefail

cd "$(dirname "$0")"

REPO_DIR="$(pwd)"
SITE_DIR="$REPO_DIR/_site"
BRANCH="gh-pages"
KEY="${OZON_DEPLOY_KEY:-$HOME/.ssh/ozon_deploy}"
PY="${OZON_PYTHON:-/usr/bin/python3}"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

log() { echo "$LOG_PREFIX $*"; }
fail() { echo "$LOG_PREFIX FEHLER: $*" >&2; exit 1; }

# --- 1) Daten holen ---------------------------------------------------------
log "Abruf vorarlberg-luft.at"
if ! "$PY" ozon_vorarlberg.py \
        --log --strict --prune-history 4 \
        --out data.json --quiet; then
    fail "Scraper fehlgeschlagen — Layout der Quelle geaendert oder offline?"
fi

[ -s data.json ] || fail "data.json ist leer"

# --- 2) _site zusammenstellen ----------------------------------------------
rm -rf "$SITE_DIR"
mkdir -p "$SITE_DIR"
cp ozon_dashboard.html "$SITE_DIR/index.html"
cp data.json "$SITE_DIR/data.json"
[ -f archive.json ] && cp archive.json "$SITE_DIR/archive.json"
# Jekyll ueberspringen: sonst schluckt GitHub Pages Dateien mit Unterstrich.
touch "$SITE_DIR/.nojekyll"

SIZE=$(du -sk "$SITE_DIR" | cut -f1)
log "_site gebaut (${SIZE} KB)"

# --- 3) Pushen --------------------------------------------------------------
REMOTE=$(git remote get-url origin 2>/dev/null || true)
[ -n "$REMOTE" ] || fail "kein origin-Remote gesetzt — setup_mini.sh laufen lassen"

if [ ! -f "$KEY" ]; then
    fail "Deploy-Key $KEY fehlt — setup_mini.sh laufen lassen"
fi
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

# In einem temporaeren Klon arbeiten, damit der Arbeitsbaum unberuehrt bleibt.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

git -C "$TMP" init -q
git -C "$TMP" remote add origin "$REMOTE"
git -C "$TMP" checkout -q --orphan "$BRANCH"
cp -R "$SITE_DIR"/. "$TMP"/
git -C "$TMP" add -A
git -C "$TMP" \
    -c user.name="ozon-vorarlberg deploy" \
    -c user.email="deploy@localhost" \
    commit -q -m "Stand $(date '+%Y-%m-%d %H:%M %Z')"

if git -C "$TMP" push -q --force origin "$BRANCH"; then
    log "gh-pages aktualisiert"
else
    fail "Push fehlgeschlagen — Deploy-Key ohne Schreibrecht?"
fi
