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

# Try to acquire the lock. Returns 0 on success, 1 if held by a live process.
acquire_lock() {
    if [ -e "$LOCKFILE" ]; then
        local pid
        pid=$(cat "$LOCKFILE" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 1
        fi
    fi
    echo $$ > "$LOCKFILE"
    return 0
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
