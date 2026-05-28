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
