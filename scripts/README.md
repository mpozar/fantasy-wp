# Cron automation

Three shell scripts that run the pipeline on different cadences. Live site at
https://mpozar.github.io/fantasy-wp/ only updates when **fast.sh** pushes a new
`docs/data.json`.

| Script        | What it does                                                    | Suggested cadence |
| ------------- | --------------------------------------------------------------- | ----------------- |
| `fast.sh`     | `fetch` (all periods) + `compute` (current week) + `publish` + push | every 5 min       |
| `medium.sh`   | `refresh-rosters` + `compute --future` (all remaining weeks)    | every 4 hours     |
| `daily.sh`    | `refresh-schedule` (MLB games + probable pitchers, full season) | once a day        |

Medium and daily only write to the local SQLite DB — the next fast-tier run
picks up the new data and pushes the result. So if `medium.sh` fails, the
public site keeps working with the previous projection snapshot.

Future-week WPs only change when projections or the MLB schedule change, so
they're computed on the medium tier (4 h). The current week's WP needs faster
turnaround (matchup state moves with every MLB game), so it stays on the fast
tier (15 min).

## Setup with crontab

The simplest path on macOS — works without any extra config in most cases.

```sh
crontab -e
```

Paste:

```
# fantasy-wp
*/5  *   * * *  /Users/mpozar/git/fantasy-wp/scripts/fast.sh
0    */4 * * *  /Users/mpozar/git/fantasy-wp/scripts/medium.sh
0    6   * * *  /Users/mpozar/git/fantasy-wp/scripts/daily.sh
```

The scripts log to `logs/{fast,medium,daily}.log` in the repo. Tail them to
verify the jobs are firing:

```sh
tail -f /Users/mpozar/git/fantasy-wp/logs/fast.log
```

## macOS permissions caveat

If cron jobs silently fail to read files in your home directory, you may need
to grant **Full Disk Access** to `/usr/sbin/cron` in
*System Settings → Privacy & Security → Full Disk Access*. Modern macOS
versions (Sequoia 15+) restrict cron's default access.

## git auth

Cron runs outside any logged-in shell, so it can't reach the macOS keychain
where `gh auth login` stashes its token. `fast.sh` works around this by reading
a GitHub token directly from `~/.zshenv`:

```sh
echo "export GH_TOKEN=$(gh auth token)" >> ~/.zshenv
chmod 600 ~/.zshenv
```

`fast.sh` reads that line and injects the token into a one-shot git credential
helper for the push. If `GH_TOKEN` isn't present it falls back to whatever
credential helper git is configured with (works fine for manual runs from a
shell that has keychain access).

If you rotate or invalidate the token (e.g. `gh auth refresh`), re-run the
command above to update `~/.zshenv`.

## Disabling

```sh
crontab -e               # delete the three lines
# or to wipe everything:
crontab -r
```
