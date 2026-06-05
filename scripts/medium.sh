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
    # fast-tier publish picks them up. Future weeks use fewer sims than the
    # current-week (fast.sh) compute: their ROS-share projections are inherently
    # fuzzy (no probables, rosters churn), so 10k's ~0.4pp MC precision is wasted
    # — 2,500 (~1pp SE) is invisible there and runs ~4x faster (measured: 72
    # matchups in ~30s vs multi-minute). Current week stays at 10k in fast.sh.
    "$APP" compute --future --sims 2500
    log medium "done"
} >> "$LOGS/medium.log" 2>&1
