#!/bin/zsh
# Daily tier: refresh MLB schedule + probable pitchers from statsapi.mlb.com,
# then force a full publish rebuild (no git push — the next fast tick pushes).
# Cron suggestion: once per day, e.g. 06:00 local time.

source "$(dirname "$0")/_common.sh"

{
    wait_lock
    trap 'release_lock' EXIT

    log daily "start"
    "$APP" refresh-schedule
    # Force a full publish rebuild once/day: refreshes the per-week block cache and
    # picks up any rare late stat correction to an already-settled week (whose
    # change-stamp wouldn't otherwise move). The next fast tick pushes the result.
    "$APP" publish --rebuild
    log daily "done"
} >> "$LOGS/daily.log" 2>&1
