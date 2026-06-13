# Sourced by every tier script. Sets up the environment + paths.
# Not executable directly.

set -euo pipefail

# cron starts with a minimal PATH and no shell rc files, so set explicitly
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export LANG="${LANG:-en_US.UTF-8}"
export HOME="${HOME:-/Users/mpozar}"

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
# which step dominates a slow tick. Preserves the command's exit status.
timed() {
    local tier="$1" label="$2"; shift 2
    local start=$SECONDS
    "$@"
    local rc=$?
    log "$tier" "step ${label}: $((SECONDS - start))s"
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

# A tick should never legitimately hold the lock this long — medium.sh's
# `compute --future` (the slowest job) runs ~3-5 min. Past this we treat the
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
