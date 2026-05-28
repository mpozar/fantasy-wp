#!/bin/zsh
# Medium tier: refresh rosters + per-player ROS projections from ESPN.
# DB-only update (no git push). The next fast-tier run picks up the new data.
# Cron suggestion: every 4 hours.

source "$(dirname "$0")/_common.sh"

{
    # Slow job — wait for any fast.sh in flight rather than skipping, so
    # the every-4-hour projection refresh actually happens.
    wait_lock
    trap 'release_lock' EXIT

    log medium "start"
    "$APP" refresh-rosters
    # Recompute future-week WPs with the fresh projections. DB-only; the next
    # fast-tier publish picks them up.
    "$APP" compute --future
    log medium "done"
} >> "$LOGS/medium.log" 2>&1
