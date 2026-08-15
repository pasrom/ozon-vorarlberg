#!/bin/bash
#
# deploy.sh — fetch readings and push them to the `data` branch.
#
# Runs as LaunchDaemon io.ebs.agent.ozon under the `agent` user, three times
# per hour:
#   1. fetch live readings, build data.json, prune the log to 4 days
#   2. rewrite the `data` branch as a single commit and force-push it
#
# Branch layout (same shape as the wastewater project):
#
#   main   code and the site itself — GitHub Pages serves from here
#   data   data.json + archive.json, nothing else
#
# The dashboard reads the data branch at runtime via raw.githubusercontent.com.
# That keeps the two apart: the logger pushes three times an hour without ever
# touching the site or triggering a Pages rebuild.
#
# The data branch is force-pushed as a single orphan commit. At this cadence a
# real history would mean ~72 commits per day and roughly half a gigabyte of
# git objects per year — for numbers that the EEA archives permanently anyway.
#
# Why the local log has to carry the whole 72 h window: the EEA archive is
# rewritten only about ONCE PER DAY (measured from the blob's Last-Modified).
# Its lag therefore swings between roughly 1 h right after a write and roughly
# 25 h just before the next one. That makes it useless for filling in the last
# few hours — it supplies the long-term metrics, we log the recent curve
# ourselves.
#
# Agent-server conventions implemented here
# (brain: tools-workflow/concepts/mac-mini-agent-server.md):
#
#   * Single-instance lock via mkdir. macOS has no flock(1). The trap clears
#     the lock on every exit, including on a signal.
#   * Heartbeat file after every successful run, so a heartbeat check can tell
#     when the job has gone quiet.
#   * Telegram alert on failure only, via ~/agents/bin/notify.sh. Successful
#     runs stay silent — otherwise the channel turns into noise.
#   * Logs under ~/agents/logs/ozon/, where newsyslog rotation applies.
#
# The push uses an SSH deploy key rather than the git credential helper:
# launchd jobs have no keychain access, and the push would hang silently.

set -euo pipefail
cd "$(dirname "$0")"

REPO_DIR="$(pwd)"
SITE_DIR="$REPO_DIR/_data"
BRANCH="data"

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

# Telegram on failure only. If sending itself fails, that must not abort the
# job on top of the original error — the original error is what matters.
alarm() {
    [ -x "$NOTIFY" ] || return 0
    [ -r "$SECRETS" ] || return 0
    ( set -a; . "$SECRETS"; set +a; "$NOTIFY" "Ozone Vorarlberg: $1" ) \
        >/dev/null 2>&1 || true
}

fail() { log "ERROR: $*"; alarm "$1"; exit 1; }

# --- single-instance lock ---------------------------------------------------
LOCK="$STATE_DIR/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    # A lock older than 30 min means a previous run is stuck. Report it, but
    # do not remove it here — that would allow two runs in parallel.
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
        log "lock is older than 30 min — is the previous run stuck?"
        alarm "lock stuck for over 30 min ($LOCK)"
    else
        log "already running, skipped"
    fi
    exit 0
fi
cleanup() { rm -rf "${TMP:-}" 2>/dev/null || true; rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup EXIT

# --- 1) fetch readings ------------------------------------------------------
[ -x "$PY" ] || fail "python missing: $PY (run mini/install.sh)"

log "fetching vorarlberg-luft.at"
if ! "$PY" ozon_vorarlberg.py --log --strict --prune-history 4 \
        --out data.json --quiet >>"$LOG" 2>&1; then
    fail "scraper failed — source offline or layout changed (see $LOG)"
fi
[ -s data.json ] || fail "data.json is empty"

# --- 2) assemble the payload ------------------------------------------------
rm -rf "$SITE_DIR"; mkdir -p "$SITE_DIR"
cp data.json "$SITE_DIR/data.json"
[ -f archive.json ] && cp archive.json "$SITE_DIR/archive.json"
log "payload built ($(du -sk "$SITE_DIR" | cut -f1) KB)"

# --- 3) push ----------------------------------------------------------------
REMOTE=$(git remote get-url origin 2>/dev/null || true)
[ -n "$REMOTE" ] || fail "no origin remote configured"

case "$REMOTE" in
    git@*|ssh://*)
        [ -f "$KEY" ] || fail "deploy key $KEY missing"
        export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        ;;
    *)
        # GIT_SSH_COMMAND only applies to SSH remotes. Over HTTPS git would
        # reach for the credential helper, which has no keychain under launchd.
        fail "origin is HTTPS ($REMOTE) — unusable under launchd, switch to SSH"
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
    commit -q -m "data as of $(date '+%Y-%m-%d %H:%M %Z')"

if ! git -C "$TMP" push -q --force origin "$BRANCH" 2>>"$LOG"; then
    fail "push failed — deploy key without write access? (see $LOG)"
fi

date -u +%s > "$STATE_DIR/heartbeat"
log "data branch updated"
