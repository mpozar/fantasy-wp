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
    # Retries cover the transient morning network drops (see with_retries);
    # a still-failing fetch aborts the run (set -e) — stale rosters make the
    # compute below pointless, and the next 4h run catches up.
    with_retries medium refresh-rosters "$APP" refresh-rosters
    # Recompute future-week WPs with the fresh projections. DB-only; the next
    # fast-tier publish picks them up. Future weeks use fewer sims than the
    # current-week (fast.sh) compute: their ROS-share projections are inherently
    # fuzzy (no probables, rosters churn), so 10k's ~0.4pp MC precision is wasted
    # — 2,500 (~1pp SE) is invisible there and runs ~4x faster (measured: 72
    # matchups in ~30s vs multi-minute). Current week stays at 10k in fast.sh.
    "$APP" compute --future --sims 2500
    # Playoff odds ride the same cadence as the future-week WPs they consume.
    # Non-fatal: odds are a derived nicety — a hiccup (e.g. playoff schedule
    # rows missing before the next daily refresh) must not kill the roster/
    # compute work above. The next fast tick pushes the refreshed JSON.
    timed medium playoffs "$APP" playoffs || log medium "playoffs step errored (non-fatal)"
    log medium "done"
} >> "$LOGS/medium.log" 2>&1
