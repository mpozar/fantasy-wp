#!/bin/zsh
# Fast tier: live matchup state → compute → publish → commit + push.
# Cron suggestion: every 15 minutes.

source "$(dirname "$0")/_common.sh"

{
    log fast "start"

    "$APP" fetch
    "$APP" compute
    "$APP" publish

    # Pull first so a stale local main doesn't block the push
    git fetch --quiet origin main
    if ! git merge --ff-only --quiet origin/main; then
        log fast "fast-forward failed; aborting (manual reconcile required)"
        exit 1
    fi

    if git diff --quiet docs/data.json && \
       git diff --cached --quiet docs/data.json; then
        log fast "no data.json changes; skipping commit"
    else
        git add docs/data.json
        git -c user.name="Mike Pozar" \
            -c user.email="mpozar@gmail.com" \
            commit -m "auto: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
        git push --quiet origin main
        log fast "pushed update"
    fi

    log fast "done"
} >> "$LOGS/fast.log" 2>&1
