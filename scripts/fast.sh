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

    "$APP" refresh-live
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

    log fast "done"
} >> "$LOGS/fast.log" 2>&1
