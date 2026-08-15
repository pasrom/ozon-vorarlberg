#!/bin/bash
#
# mini/install.sh — run this on the agent server as the `agent` user.
#
# Sets up everything that works without root: venv, dependencies, deploy key,
# archive, test run. The two LaunchDaemons live in /Library/LaunchDaemons and
# need sudo — the command for that is printed at the end.
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

[ "$(whoami)" = "agent" ] || warn "running as $(whoami), expected 'agent'"

step "1. Python environment"
# Homebrew python is not symlinked, and the system python 3.9 lacks the
# dependencies and is externally managed. So: a venv of our own.
BREW_PY=$(ls -1 /opt/homebrew/opt/python@3.1*/bin/python3.1* 2>/dev/null | grep -v config | sort -V | tail -1)
[ -n "$BREW_PY" ] || die "no Homebrew python found (brew install python@3.12)"
ok "base: $BREW_PY ($("$BREW_PY" -V 2>&1))"
[ -x .venv/bin/python3 ] || "$BREW_PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python3 -c 'import requests,bs4,lxml,pyarrow' || die "dependencies missing"
ok "venv ready: $REPO_DIR/.venv"

step "2. Directories, per server convention"
mkdir -p "$HOME/agents/logs/ozon" "$HOME/agents/state/ozon"
ok "logs:  ~/agents/logs/ozon  (newsyslog rotation applies there)"
ok "state: ~/agents/state/ozon"

step "3. Deploy key"
if [ -f "$KEY" ]; then
    ok "present: $KEY"
else
    ssh-keygen -t ed25519 -N "" -C "ozon-vorarlberg deploy (agent@$(hostname -s))" -f "$KEY" -q
    ok "generated: $KEY"
fi
chmod 600 "$KEY"
echo
echo "  Public half (must be attached to the repo as a deploy key with write"
echo "  access):"
sed 's/^/    /' "$KEY.pub"

step "4. Remote on SSH"
# launchd jobs have no keychain access; over HTTPS the push hangs silently.
# The deploy key is the reliable route, and it only applies to SSH remotes.
CUR=$(git remote get-url origin)
SLUG=$(echo "$CUR" | sed -E 's#.*[:/]([^/]+/[^/]+)(\.git)?$#\1#; s#\.git$##')
case "$CUR" in
    git@*|ssh://*) ok "already SSH: $CUR" ;;
    *) git remote set-url origin "git@github.com:$SLUG.git"
       ok "switched from HTTPS to git@github.com:$SLUG.git" ;;
esac

step "5. Test push access"
if GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
   git ls-remote origin >/dev/null 2>&1; then
    ok "push access works"
else
    die "SSH access fails — is the key above attached with write permission?"
fi

step "6. EEA archive"
if [ -s archive.json ]; then
    ok "archive.json present ($(du -h archive.json | cut -f1))"
else
    echo "  Fetching the measurement history (one-off, ~26 MB)…"
    ./refresh_archive.sh
fi

step "7. Test run"
./deploy.sh && ok "deploy succeeded"

step "8. Only root is left"
cat <<CMD

  The LaunchDaemons need sudo. Run this block as a user with admin rights
  (e.g. via 'ssh -t mac-mini'):

    sudo cp $REPO_DIR/mini/io.ebs.agent.ozon.plist \\
            $REPO_DIR/mini/io.ebs.agent.ozon-archive.plist \\
            /Library/LaunchDaemons/
    sudo chown root:wheel /Library/LaunchDaemons/io.ebs.agent.ozon*.plist
    sudo chmod 644        /Library/LaunchDaemons/io.ebs.agent.ozon*.plist
    for L in io.ebs.agent.ozon io.ebs.agent.ozon-archive; do
      sudo launchctl bootstrap system /Library/LaunchDaemons/\$L.plist
      sudo launchctl enable system/\$L
    done

  Then check — note that plain 'launchctl list' only shows the user domain and
  will NOT find system daemons:

    launchctl print system/io.ebs.agent.ozon | grep -E "state|runs|last exit"
    sudo launchctl kickstart -k system/io.ebs.agent.ozon   # fire immediately

CMD
