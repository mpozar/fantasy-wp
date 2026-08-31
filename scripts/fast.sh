#!/bin/zsh
# Fast tier: live matchup state → compute → publish → commit + push.
# Cron: every 5 minutes.

source "$(dirname "$0")/_common.sh"

{
    # Skip this tick if any other DB-writing job is in flight (another
    # fast.sh, medium.sh, daily.sh, or an interactive `app` invocation).
    # Better to drop a 5-min update than queue up and chain into the next
    # cron fire.
    if ! acquire_lock; then
        log fast "another app job holds the lock; skipping this tick"
        exit 0
    fi
    trap 'release_lock' EXIT

    log fast "start"

    timed fast refresh-live "$APP" refresh-live
    timed fast fetch        "$APP" fetch
    timed fast compute      "$APP" compute
    # Playoff odds normally ride medium.sh's 4-hourly cadence. On the LAST day of
    # a matchup period that's too coarse — six matchups resolve in a few hours and
    # the seeds/bye odds genuinely swing — so the fast tier offers a refresh every
    # tick and the command self-throttles to ~30 min via `--if-live-finale`
    # (`cli._finale_skip_reason`). A no-op (<50ms) on every other tick of the week.
    # Runs BEFORE the git step so the refreshed docs/playoffs.json ships in the
    # same commit. Non-fatal, same rationale as medium.sh: odds are derived, and a
    # hiccup must not cost the tick's data update.
    timed fast playoffs "$APP" playoffs --if-live-finale \
        || log fast "playoffs step errored (non-fatal)"
    timed fast publish      "$APP" publish

    git_start=$SECONDS
    # Pull first so a stale local main doesn't block the push
    git fetch --quiet origin main
    if ! git merge --ff-only --quiet origin/main; then
        log fast "fast-forward failed; aborting (manual reconcile required)"
        exit 1
    fi

    # publish writes data.json + per-week history files (docs/history/*.json).
    # Add first, then check the staged diff — `git diff` alone can't see a
    # brand-new (untracked) history file. Adding unchanged files stages nothing,
    # so the skip path still leaves the index untouched.
    git add docs/data.json docs/history docs/playoffs.json
    if git diff --cached --quiet docs/data.json docs/history docs/playoffs.json; then
        log fast "no data.json/history/playoffs changes; skipping commit"
    else
        git -c user.name="Mike Pozar" \
            -c user.email="mpozar@gmail.com" \
            commit -m "auto: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
        # Cron can't read the macOS keychain, so authenticate the push with a
        # GitHub token kept in ~/.zshenv. Falls back to whatever credential
        # helper git is configured with if the token isn't set (e.g., manual
        # runs from an authenticated shell).
        GH_TOKEN_VAL=$(read_zshenv_var GH_TOKEN)
        if [ -n "$GH_TOKEN_VAL" ]; then
            git -c credential.helper="" \
                -c credential.helper="!f() { echo username=oauth2; echo password=$GH_TOKEN_VAL; }; f" \
                push --quiet origin main
        else
            git push --quiet origin main
        fi
        log fast "pushed update"
    fi
    log fast "step git: $((SECONDS - git_start))s"

    # The published site is the one hop nothing else watches: the steps above can
    # all succeed while GitHub's Pages deploy is wedged, leaving docs/data.json
    # fresh on disk and the live site frozen — 2026-08-31 served 4-day-old data
    # with zero flags (`ANOM_SITE_STALE` reads the LOCAL artifact). Runs AFTER the
    # git step so it judges the state we just pushed. One API call on the healthy
    # path; only acts on a `waiting`/`queued` run older than 30 min, never on an
    # `in_progress` one (see app/pages.py). Non-fatal, like validate below.
    timed fast pages-guard "$APP" pages-guard \
        || log fast "pages-guard step errored (non-fatal)"

    # Invariant + anomaly checks over the just-computed current-period snapshots
    # (cheap, no sims). Records flags in validation_flags for later review via
    # `app validate --list`. Non-fatal: never let a check hiccup break the tick.
    timed fast validate "$APP" validate || log fast "validate step errored (non-fatal)"

    log fast "done"
} >> "$LOGS/fast.log" 2>&1
