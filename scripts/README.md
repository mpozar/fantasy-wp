# Cron automation

Three shell scripts that run the pipeline on different cadences. Live site at
https://mpozar.github.io/fantasy-wp/ only updates when **fast.sh** pushes a new
`docs/data.json`.

| Script        | What it does                                        | Suggested cadence |
| ------------- | --------------------------------------------------- | ----------------- |
| `fast.sh`     | `fetch` + `compute` + `publish` + commit + push     | every 15 min      |
| `medium.sh`   | `refresh-rosters` (ESPN rosters + ROS projections)  | every 4 hours     |
| `daily.sh`    | `refresh-schedule` (MLB games + probable pitchers)  | once a day        |

Medium and daily only write to the local SQLite DB — the next fast-tier run
picks up the new data and pushes the result. So if `medium.sh` fails, the
public site keeps working with the previous projection snapshot.

## Setup with crontab

The simplest path on macOS — works without any extra config in most cases.

```sh
crontab -e
```

Paste:

```
# fantasy-wp
*/15 *  * * *  /Users/mpozar/git/fantasy-wp/scripts/fast.sh
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

`fast.sh` pushes over HTTPS using whatever credential helper git is configured
with. `gh auth login` (already done) puts a token in the macOS keychain, which
the credential helper reads. If pushes start failing from cron — usually a
keychain-unlock issue — the workaround is to switch the remote to SSH:

```sh
git remote set-url origin git@github.com:mpozar/fantasy-wp.git
ssh-add ~/.ssh/id_ed25519        # add key to ssh-agent once per login
```

## Disabling

```sh
crontab -e               # delete the three lines
# or to wipe everything:
crontab -r
```
