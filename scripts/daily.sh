#!/bin/zsh
# Daily tier: refresh MLB schedule + probable pitchers from statsapi.mlb.com.
# DB-only update (no git push).
# Cron suggestion: once per day, e.g. 06:00 local time.

source "$(dirname "$0")/_common.sh"

{
    wait_lock
    trap 'release_lock' EXIT

    log daily "start"
    "$APP" refresh-schedule
    log daily "done"
} >> "$LOGS/daily.log" 2>&1
