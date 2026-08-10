#!/bin/zsh
# Daily tier: refresh MLB schedule + probable pitchers from statsapi.mlb.com,
# then force a full publish rebuild (no git push — the next fast tick pushes).
# Cron suggestion: once per day, e.g. 06:00 local time.

source "$(dirname "$0")/_common.sh"

{
    wait_lock
    trap 'release_lock' EXIT

    log daily "start"
    with_retries daily refresh-schedule "$APP" refresh-schedule
    # Force a full publish rebuild once/day: refreshes the per-week block cache and
    # picks up any rare late stat correction to an already-settled week (whose
    # change-stamp wouldn't otherwise move). The next fast tick pushes the result.
    "$APP" publish --rebuild
    # Retrospective calibration check (projected vs settled actual, per category).
    # Daily rather than per-tick: its answer only moves when a week settles, and
    # it reads details_json across every settled week. Non-fatal — it's a
    # monitor, and a failure here must not cost the schedule refresh above.
    "$APP" validate --calibration || log daily "validate --calibration failed (non-fatal)"
    log daily "done"
} >> "$LOGS/daily.log" 2>&1
