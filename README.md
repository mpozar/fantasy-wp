# fantasy-wp

Win probability estimator for a single ESPN head-to-head fantasy baseball league
(Quintonia Baseball, leagueId 71455).

## How it works

Local Python pulls matchup state from ESPN, stores it in SQLite, computes a
placeholder win probability per matchup, and writes a static `docs/data.json`
that a tiny HTML page renders. The static site can be served by GitHub Pages.

```
ESPN  ──(fetch)──▶  SQLite  ──(compute)──▶  wp_snapshots  ──(publish)──▶  docs/data.json  ──▶  docs/index.html
```

## Setup

ESPN private-league cookies must be in `~/.zshenv`:

```sh
export ESPN_SWID="{...}"
export ESPN_S2="..."
```

Install:

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Usage

```sh
app init-db          # one-time: create SQLite schema
app fetch            # pull current matchup state from ESPN
app compute          # compute WP for all matchups in current period
app publish          # write docs/data.json
```

Or chain them:

```sh
app fetch && app compute && app publish
```

Open `docs/index.html` in a browser to see the current state.

## Status

Skeleton. The WP model is a placeholder (per-category score ratios convolved
into a poisson-binomial); the real Monte Carlo model is not yet implemented.
