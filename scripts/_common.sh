# Sourced by every tier script. Sets up the environment + paths.
# Not executable directly.

set -euo pipefail

# cron starts with a minimal PATH and no shell rc files, so set explicitly
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export LANG="${LANG:-en_US.UTF-8}"
export HOME="${HOME:-/Users/mpozar}"

# Keep the Mac awake for the duration of this tick — a running tick shouldn't sleep
# or throttle under itself (dark-wake on battery had thrashed ticks: a 28s sim took
# 189s; mid-tick fetches hit DNS errors). Re-exec once under caffeinate.
#   *** Use ${ZSH_ARGZERO:-$0}, NOT $0. *** This file is *sourced*, and the tiers run
#   under zsh (their shebang). In a sourced file under zsh, $0 is THIS file
#   (_common.sh) — re-exec'ing that tried to execute the non-executable _common.sh,
#   gave "Permission denied" (exit 126), and silently killed every cron tick (the
#   2026-06-13 outage). ZSH_ARGZERO is the invoked script (fast.sh) under zsh; $0 is
#   already correct under bash. Verified by running a tier cron-style (env -i, zsh).
# The real lid-closed-on-AC fix is `sudo pmset -c disablesleep 1`; this is a backstop,
# mainly for the battery case (where -s is a no-op but -i still helps a running tick).
if [ -z "${FWP_CAFFEINATED:-}" ] && [ -x /usr/bin/caffeinate ]; then
    export FWP_CAFFEINATED=1
    exec /usr/bin/caffeinate -ims "${ZSH_ARGZERO:-$0}" "$@"
fi

REPO="/Users/mpozar/git/fantasy-wp"
LOGS="$REPO/logs"
APP="$REPO/.venv/bin/app"

mkdir -p "$LOGS"
cd "$REPO"

log() {
    local tier="$1"
    local msg="$2"
    printf '[%s] [%s] %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tier" "$msg"
}

# Run a command and log its wall-clock duration (whole seconds), so we can see
# which step dominates a slow tick. Returns the command's exit status; the caller
# decides fatal vs non-fatal (a bare `timed ...` aborts the tick under `set -e`,
# `timed ... || handler` doesn't). The `|| rc=$?` makes the rc-capture and the
# duration log run even on failure, instead of relying on errexit being
# suppressed inside a function called in a `||` context.
timed() {
    local tier="$1" label="$2"; shift 2
    local start=$SECONDS rc=0
    "$@" || rc=$?
    log "$tier" "step ${label}: $((SECONDS - start))s"
    return $rc
}

# Run a command with bounded retries — for the network-fetch steps, which hit
# transient DNS/connect failures (the laptop regularly drops off Wi-Fi around
# 06:00-07:00 UTC; most drops last one tick, some the whole hour). Backoff
# totals ~7 min worst case so a lock-holding job stays far under MAX_LOCK_AGE.
# A full-hour outage still fails after the last attempt — deliberate: better
# to give up and let the next scheduled run catch up than wedge the lock.
RETRY_DELAYS=(60 120 240)
with_retries() {
    local tier="$1" label="$2"; shift 2
    local rc delay
    for delay in "${RETRY_DELAYS[@]}" 0; do
        rc=0
        "$@" || rc=$?
        [ "$rc" -eq 0 ] && return 0
        if [ "$delay" -eq 0 ]; then break; fi
        log "$tier" "step ${label} failed (rc=$rc); retrying in ${delay}s"
        sleep "$delay"
    done
    log "$tier" "step ${label} failed after $((${#RETRY_DELAYS[@]} + 1)) attempts (rc=$rc)"
    return $rc
}

# Read `export NAME=...` value from ~/.zshenv (same pattern as espn.py).
# Cron can't reach the keychain, so secrets live in this file.
read_zshenv_var() {
    grep -m1 "^export $1=" "$HOME/.zshenv" \
        | sed -E "s/^export $1=//; s/^['\"]//; s/['\"]\$//"
}

# Shared lock used by every script that writes to data.db. SQLite serializes
# writes itself, but its default behavior is to error on contention rather
# than wait — and the lockfile is a clearer signal anyway. Fast jobs should
# skip on contention; slow jobs should wait their turn.
LOCKFILE="$REPO/.app.lock"

# A tick should never legitimately hold the lock this long — medium.sh (the
# slowest job) runs ~1-2 min, up to ~9 min if with_retries rides out a network
# blip. Past this we treat the
# holder as wedged (a network call hung beyond its own timeout, or a process
# killed without its EXIT trap firing) and steal the lock, so one stuck tick
# can't freeze the whole pipeline until someone clears it by hand.
MAX_LOCK_AGE=1200   # 20 minutes

# Try to acquire the lock. Returns 0 on success, 1 if held by a live, recent
# process. Steals the lock from a dead holder OR one wedged past MAX_LOCK_AGE.
acquire_lock() {
    if [ -e "$LOCKFILE" ]; then
        local pid age
        pid=$(cat "$LOCKFILE" 2>/dev/null || true)
        # File mtime age in seconds (BSD stat on macOS; treat as 0 if it vanished).
        age=$(( $(date +%s) - $(stat -f %m "$LOCKFILE" 2>/dev/null || date +%s) ))
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && [ "$age" -lt "$MAX_LOCK_AGE" ]; then
            return 1   # held by a live, not-yet-wedged process
        fi
        # Stale: dead holder, or alive but wedged past MAX_LOCK_AGE. If still
        # alive, kill it first so it can't wake up and double-write, then steal.
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            log lock "stealing wedged lock from pid $pid (held ${age}s); killing it"
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$LOCKFILE"
    fi
    # Atomic create — noclobber makes the redirect fail if another process
    # created the file first, closing the check-then-write race that let two
    # tiers run (and write the DB) concurrently.
    if ( set -o noclobber; echo $$ > "$LOCKFILE" ) 2>/dev/null; then
        return 0
    fi
    return 1
}

# Block until the lock can be acquired.
wait_lock() {
    while ! acquire_lock; do
        sleep 5
    done
}

release_lock() {
    rm -f "$LOCKFILE"
}
