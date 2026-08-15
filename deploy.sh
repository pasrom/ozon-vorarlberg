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
# The data branch keeps a real commit history, one commit per hourly reading.
# An earlier version force-pushed a single orphan commit, justified with an
# estimate of "roughly half a gigabyte of git objects per year". That estimate
# was wrong by a factor of eleven: measured with realistic changes, the
# marginal cost settles at ~1.8 KB per commit, i.e. about 46 MB per year. Git
# delta-compresses shifting JSON far better than assumed.
#
# The history is worth having: it records what the page actually showed at any
# point in time. That is not the same as the measurements — those live at the
# EEA — and it cannot be reconstructed from anywhere else if the source ever
# emits nonsense or the scraper has a bug.
#
# Only genuinely new readings are committed. The source updates hourly while
# this job runs three times an hour, so two of three runs would otherwise
# produce a commit in which nothing but the fetch timestamp changed.
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

# Shallow clone: the history grows by design, and a full fetch would get
# slower every day for no benefit. --depth 1 keeps every run equally cheap.
TMP=$(mktemp -d)
if ! git clone -q --depth 1 --branch "$BRANCH" "$REMOTE" "$TMP" 2>>"$LOG"; then
    # Branch does not exist yet: start it.
    log "branch $BRANCH not found, creating it"
    git -C "$TMP" init -q
    git -C "$TMP" remote add origin "$REMOTE"
    git -C "$TMP" checkout -q -b "$BRANCH"
fi

# Is this actually a new reading? The source updates hourly while this job
# runs three times an hour. Without this check two of three runs would commit
# nothing but a changed fetch timestamp.
SRC_NEW=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("source_time") or "")' \
          "$SITE_DIR/data.json" 2>/dev/null || echo "")
SRC_OLD=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("source_time") or "")' \
          "$TMP/data.json" 2>/dev/null || echo "")

cp -R "$SITE_DIR"/. "$TMP"/
git -C "$TMP" add -A

ARCHIVE_CHANGED=0
git -C "$TMP" diff --cached --quiet -- archive.json || ARCHIVE_CHANGED=1

if [ -n "$SRC_OLD" ] && [ "$SRC_NEW" = "$SRC_OLD" ] && [ "$ARCHIVE_CHANGED" = "0" ]; then
    date -u +%s > "$STATE_DIR/heartbeat"
    log "source unchanged ($SRC_NEW), nothing to commit"
    exit 0
fi

if git -C "$TMP" diff --cached --quiet; then
    date -u +%s > "$STATE_DIR/heartbeat"
    log "no change, nothing to commit"
    exit 0
fi

git -C "$TMP" \
    -c user.name="ozon-vorarlberg deploy" \
    -c user.email="deploy@localhost" \
    commit -q -m "data ${SRC_NEW:-$(date '+%Y-%m-%d %H:%M')}"

# One retry: should a push ever be rejected as non-fast-forward, refetch and
# replay on top rather than reaching for --force.
if ! git -C "$TMP" push -q origin "$BRANCH" 2>>"$LOG"; then
    log "push rejected, refetching and retrying once"
    git -C "$TMP" fetch -q --depth 1 origin "$BRANCH" 2>>"$LOG" || true
    git -C "$TMP" reset -q --soft FETCH_HEAD 2>>"$LOG" || true
    git -C "$TMP" \
        -c user.name="ozon-vorarlberg deploy" \
        -c user.email="deploy@localhost" \
        commit -q -m "data ${SRC_NEW:-$(date '+%Y-%m-%d %H:%M')}" 2>>"$LOG" || true
    git -C "$TMP" push -q origin "$BRANCH" 2>>"$LOG" \
        || fail "push failed — deploy key without write access? (see $LOG)"
fi

date -u +%s > "$STATE_DIR/heartbeat"
log "data branch updated ($SRC_NEW)"
