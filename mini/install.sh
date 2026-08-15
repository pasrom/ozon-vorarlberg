#!/bin/bash
#
# mini/install.sh — auf dem Agent-Server als Benutzer `agent` ausfuehren.
#
# Richtet alles ein, was ohne root geht: venv, Abhaengigkeiten, Deploy-Key,
# Archiv, Testlauf. Die beiden LaunchDaemons liegen in /Library/LaunchDaemons
# und brauchen sudo — der Befehl dafuer wird am Ende ausgegeben.
#
#     ssh agent@mac-mini
#     cd ~/git/ozon-vorarlberg && ./mini/install.sh

set -euo pipefail
cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"
KEY="$HOME/.ssh/id_ed25519_ozon"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
step() { printf "\n\033[1m%s\033[0m\n" "$*"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$*"; exit 1; }

[ "$(whoami)" = "agent" ] || warn "laeuft als $(whoami), erwartet war 'agent'"

step "1. Python-Umgebung"
# Homebrew-Python ist nicht verlinkt; System-Python 3.9 hat die
# Abhaengigkeiten nicht und ist extern verwaltet. Also eigenes venv.
BREW_PY=$(ls -1 /opt/homebrew/opt/python@3.1*/bin/python3.1* 2>/dev/null | grep -v config | sort -V | tail -1)
[ -n "$BREW_PY" ] || die "kein Homebrew-Python gefunden (brew install python@3.12)"
ok "Basis: $BREW_PY ($("$BREW_PY" -V 2>&1))"
[ -x .venv/bin/python3 ] || "$BREW_PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python3 -c 'import requests,bs4,lxml,pyarrow' || die "Abhaengigkeiten fehlen"
ok "venv fertig: $REPO_DIR/.venv"

step "2. Verzeichnisse nach Serverkonvention"
mkdir -p "$HOME/agents/logs/ozon" "$HOME/agents/state/ozon"
ok "logs: ~/agents/logs/ozon  (newsyslog-Rotation greift dort)"
ok "state: ~/agents/state/ozon"

step "3. Deploy-Key"
if [ -f "$KEY" ]; then
    ok "vorhanden: $KEY"
else
    ssh-keygen -t ed25519 -N "" -C "ozon-vorarlberg deploy (agent@$(hostname -s))" -f "$KEY" -q
    ok "erzeugt: $KEY"
fi
chmod 600 "$KEY"
echo
echo "  Oeffentlicher Teil (muss als Deploy-Key mit Schreibrecht am Repo haengen):"
sed 's/^/    /' "$KEY.pub"

step "4. Remote auf SSH"
CUR=$(git remote get-url origin)
SLUG=$(echo "$CUR" | sed -E 's#.*[:/]([^/]+/[^/]+)(\.git)?$#\1#; s#\.git$##')
case "$CUR" in
    git@*|ssh://*) ok "schon SSH: $CUR" ;;
    *) git remote set-url origin "git@github.com:$SLUG.git"; ok "umgestellt auf git@github.com:$SLUG.git" ;;
esac
git config user.name  "$(git config user.name  || echo 'ozon deploy')" >/dev/null 2>&1 || true

step "5. Push-Zugriff testen"
if GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
   git ls-remote origin >/dev/null 2>&1; then
    ok "SSH-Zugriff funktioniert"
else
    die "SSH-Zugriff scheitert — haengt der Key oben mit Schreibrecht am Repo?"
fi

step "6. EEA-Archiv"
if [ -s archive.json ]; then
    ok "archive.json vorhanden ($(du -h archive.json | cut -f1))"
else
    echo "  Laedt die Messhistorie seit 1988 (einmalig, ~26 MB)…"
    ./refresh_archive.sh
fi

step "7. Testlauf"
./deploy.sh && ok "Deploy erfolgreich"

step "8. Jetzt fehlt nur noch root"
cat <<CMD

  Die LaunchDaemons brauchen sudo. Diesen Block als Benutzer mit Adminrechten
  ausfuehren (z. B. per 'ssh mac-mini'):

    sudo cp $REPO_DIR/mini/io.ebs.agent.ozon.plist \\
            $REPO_DIR/mini/io.ebs.agent.ozon-archive.plist \\
            /Library/LaunchDaemons/
    sudo chown root:wheel /Library/LaunchDaemons/io.ebs.agent.ozon*.plist
    sudo chmod 644        /Library/LaunchDaemons/io.ebs.agent.ozon*.plist
    for L in io.ebs.agent.ozon io.ebs.agent.ozon-archive; do
      sudo launchctl bootstrap system /Library/LaunchDaemons/\$L.plist
      sudo launchctl enable system/\$L
    done
    launchctl list | grep io.ebs.agent.ozon

  Danach sofort einmal feuern:
    sudo launchctl kickstart -k system/io.ebs.agent.ozon

CMD
