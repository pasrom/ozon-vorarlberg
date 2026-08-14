#!/bin/bash
#
# setup_mini.sh — Logger auf einem immer laufenden Mac einrichten.
#
# Einmal interaktiv auf dem Mac mini ausfuehren:
#     ./setup_mini.sh
#
# Prueft die Voraussetzungen, legt einen SSH-Deploy-Key an, haengt ihn ans
# GitHub-Repo, installiert zwei launchd-Jobs und macht einen Testlauf.
#
# Idempotent: mehrfaches Ausfuehren ist unschaedlich. Zum Entfernen:
#     ./setup_mini.sh --uninstall

set -euo pipefail
cd "$(dirname "$0")"
REPO_DIR="$(pwd)"

LABEL_MAIN="io.ebs.ozon-vorarlberg"
LABEL_ARC="io.ebs.ozon-vorarlberg-archive"
AGENTS="$HOME/Library/LaunchAgents"
KEY="$HOME/.ssh/ozon_deploy"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$*"; }
step() { printf "\n\033[1m%s\033[0m\n" "$*"; }
die()  { bad "$*"; exit 1; }

# ---------------------------------------------------------------- uninstall --
if [ "${1:-}" = "--uninstall" ]; then
    step "Entfernen"
    for L in "$LABEL_MAIN" "$LABEL_ARC"; do
        if launchctl list "$L" >/dev/null 2>&1; then
            launchctl bootout "gui/$(id -u)/$L" 2>/dev/null \
                || launchctl unload "$AGENTS/$L.plist" 2>/dev/null || true
            ok "$L gestoppt"
        fi
        rm -f "$AGENTS/$L.plist" && ok "$L.plist entfernt"
    done
    warn "Deploy-Key $KEY bleibt liegen — bei Bedarf selbst loeschen"
    warn "Der gh-pages-Branch auf GitHub bleibt bestehen"
    exit 0
fi

echo "Ozon Vorarlberg — Einrichtung des Loggers"
echo "Repo: $REPO_DIR"

# ------------------------------------------------------------ 1) Grundlagen --
step "1. Voraussetzungen"

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$c" ] || continue
    if "$c" -c 'import requests, bs4, lxml, pyarrow' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    bad "Kein python3 mit allen Abhaengigkeiten gefunden."
    echo "     Nachinstallieren, z. B.:"
    echo "       /usr/bin/python3 -m pip install --user -r requirements.txt"
    exit 1
fi
ok "python3: $PY"
"$PY" -c 'import sys; print("     Version:", sys.version.split()[0])'

command -v git >/dev/null || die "git fehlt"
ok "git: $(git --version | awk '{print $3}')"

command -v gh >/dev/null || die "gh CLI fehlt (brew install gh)"
gh auth status >/dev/null 2>&1 || die "gh ist nicht angemeldet — 'gh auth login' ausfuehren"
GH_USER=$(gh api user --jq .login)
ok "gh angemeldet als: $GH_USER"

# ---------------------------------------------------- 2) Schlaf-Einstellung --
step "2. Bleibt die Maschine wach?"
SLEEP=$(pmset -g | awk '/^ *sleep/ {print $2; exit}')
if [ "${SLEEP:-1}" = "0" ]; then
    ok "Ruhezustand ist aus"
else
    warn "Ruhezustand nach ${SLEEP} min — der Logger schweigt dann."
    echo "     Auf einem Mac mini abschalten mit:"
    echo "       sudo pmset -a sleep 0 disksleep 0"
    echo "     (Display darf schlafen, das stoert nicht.)"
fi
if pmset -g | grep -q "womp.*1"; then ok "Wake on LAN aktiv"; fi

# --------------------------------------------------------------- 3) Git/Repo --
step "3. Git-Repo und Remote"
if [ ! -d .git ]; then
    git init -q && ok "git init"
else
    ok "Repo vorhanden"
fi
git symbolic-ref -q HEAD >/dev/null || git checkout -q -b main
BRANCH_NOW=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)

if ! git remote get-url origin >/dev/null 2>&1; then
    DEFAULT_NAME="ozon-vorarlberg"
    read -r -p "  Repo-Name auf GitHub [$DEFAULT_NAME]: " RNAME
    RNAME="${RNAME:-$DEFAULT_NAME}"
    if gh repo view "$GH_USER/$RNAME" >/dev/null 2>&1; then
        ok "Repo $GH_USER/$RNAME existiert schon"
        git remote add origin "git@github.com:$GH_USER/$RNAME.git"
    else
        echo
        warn "GitHub Pages braucht bei kostenlosen Konten ein OEFFENTLICHES Repo."
        warn "Code, Verlaufsdaten und Dashboard sind dann fuer jeden einsehbar."
        read -r -p "  Oeffentliches Repo $GH_USER/$RNAME anlegen? [j/N] " YN
        [[ "$YN" =~ ^[jJyY] ]] || die "Abgebrochen. Ohne Repo kein Deploy."
        gh repo create "$GH_USER/$RNAME" --public \
            --description "Ozonwerte Vorarlberg: Trainingsampel mit Messhistorie seit 1988" \
            --source . --remote origin --push
        ok "Repo angelegt und $BRANCH_NOW gepusht"
    fi
else
    ok "origin: $(git remote get-url origin)"
fi

# Remote auf SSH normalisieren. launchd-Jobs haben oft keinen Zugriff auf den
# Schluesselbund; ueber HTTPS haengt der Push dann still. Der Deploy-Key ist
# der verlaessliche Weg, und der wirkt nur bei SSH-Remotes.
CURRENT=$(git remote get-url origin)
REPO_SLUG=$(echo "$CURRENT" | sed -E 's#.*[:/]([^/]+/[^/]+)(\.git)?$#\1#; s#\.git$##')
case "$CURRENT" in
    git@*|ssh://*) ok "Remote ist SSH" ;;
    *) git remote set-url origin "git@github.com:$REPO_SLUG.git"
       ok "Remote von HTTPS auf SSH umgestellt" ;;
esac

# ----------------------------------------------------------- 4) Deploy-Key --
step "4. SSH-Deploy-Key"
# Bewusst SSH statt git-credential-Helper: launchd-Jobs haben oft keinen
# Zugriff auf den Schluesselbund, und dann haengt der Push still.
if [ -f "$KEY" ]; then
    ok "Key vorhanden: $KEY"
else
    ssh-keygen -t ed25519 -N "" -C "ozon-vorarlberg deploy ($(hostname -s))" -f "$KEY" -q
    ok "Key erzeugt: $KEY"
fi
chmod 600 "$KEY"

KEY_TITLE="ozon-deploy-$(hostname -s)"
if gh repo deploy-key list --repo "$REPO_SLUG" 2>/dev/null | grep -q "$KEY_TITLE"; then
    ok "Deploy-Key haengt am Repo"
else
    if gh repo deploy-key add "$KEY.pub" --repo "$REPO_SLUG" \
           --title "$KEY_TITLE" --allow-write 2>/dev/null; then
        ok "Deploy-Key mit Schreibrecht hinzugefuegt"
    else
        bad "Konnte den Deploy-Key nicht automatisch hinzufuegen."
        echo "     Diesen Text unter Settings > Deploy keys eintragen"
        echo "     (Schreibrecht aktivieren):"
        echo; cat "$KEY.pub"; echo
        read -r -p "  Erledigt? [Enter] " _
    fi
fi

step "5. SSH-Verbindung testen"
if GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
   git ls-remote origin >/dev/null 2>&1; then
    ok "Push-Zugriff funktioniert"
else
    die "SSH-Zugriff schlaegt fehl. Hat der Deploy-Key Schreibrecht?"
fi

# ------------------------------------------------------------- 6) Archiv --
step "6. EEA-Archiv"
if [ -s archive.json ]; then
    ok "archive.json vorhanden ($(du -h archive.json | cut -f1))"
else
    echo "  Lade die Messhistorie (einmalig, ~26 MB)…"
    "$PY" eea_archive.py --build --quiet && ok "archive.json gebaut"
fi

# ------------------------------------------------------------ 7) launchd --
step "7. launchd-Jobs"
mkdir -p logs "$AGENTS"
for L in "$LABEL_MAIN" "$LABEL_ARC"; do
    sed -e "s|__REPO__|$REPO_DIR|g" -e "s|__PYTHON__|$PY|g" -e "s|__KEY__|$KEY|g" \
        "launchd/$L.plist" > "$AGENTS/$L.plist"
    plutil -lint "$AGENTS/$L.plist" >/dev/null || die "$L.plist ist ungueltig"
    launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$L.plist" 2>/dev/null \
        || launchctl load "$AGENTS/$L.plist"
    ok "$L installiert"
done

step "8. Testlauf"
if bash deploy.sh; then
    ok "Deploy erfolgreich"
else
    die "Deploy fehlgeschlagen — Ausgabe oben lesen"
fi

# --------------------------------------------------------------- 9) Pages --
step "9. GitHub Pages"
if gh api "repos/$REPO_SLUG/pages" >/dev/null 2>&1; then
    URL=$(gh api "repos/$REPO_SLUG/pages" --jq .html_url)
    ok "Pages aktiv: $URL"
else
    if gh api -X POST "repos/$REPO_SLUG/pages" -f "source[branch]=gh-pages" \
         -f "source[path]=/" >/dev/null 2>&1; then
        ok "Pages aktiviert (erste Veroeffentlichung dauert 1-2 Minuten)"
        URL="https://$(echo "$REPO_SLUG" | cut -d/ -f1).github.io/$(echo "$REPO_SLUG" | cut -d/ -f2)/"
    else
        warn "Pages konnte nicht automatisch aktiviert werden."
        echo "     Settings > Pages > Source: Branch 'gh-pages', Ordner '/'"
        URL="(nach Aktivierung sichtbar)"
    fi
fi

cat <<EOF

$(printf "\033[1mFertig.\033[0m")

  Dashboard    $URL
  Logger       alle 20 min   ($LABEL_MAIN)
  Archiv       montags 04:17 ($LABEL_ARC)

  Logs         tail -f $REPO_DIR/logs/deploy.err
  Status       launchctl list | grep ozon
  Sofort       launchctl kickstart -k gui/$(id -u)/$LABEL_MAIN
  Entfernen    ./setup_mini.sh --uninstall

  Die Seite zeigt selbst an, wenn die Daten alt werden — wenn oben ein
  Banner "Daten sind alt" steht, schweigt der Logger.
EOF
