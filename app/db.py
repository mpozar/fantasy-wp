"""SQLite schema + helpers."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scoring_settings (
    league_id            INTEGER NOT NULL,
    season_id            INTEGER NOT NULL,
    name                 TEXT NOT NULL,
    size                 INTEGER NOT NULL,
    scoring_type         TEXT NOT NULL,
    tiebreaker_stat_id   INTEGER,
    categories_json      TEXT NOT NULL,
    lineup_slots_json    TEXT,
    fetched_at           TEXT NOT NULL,
    PRIMARY KEY (league_id, season_id)
);

CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    abbrev      TEXT,
    owner       TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matchups (
    id                INTEGER PRIMARY KEY,
    matchup_period_id INTEGER NOT NULL,
    home_team_id      INTEGER NOT NULL,
    away_team_id      INTEGER NOT NULL,
    winner            TEXT,
    fetched_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_state (
    matchup_id  INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    stat_id     INTEGER NOT NULL,
    score       REAL NOT NULL,
    result      TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (matchup_id, team_id, stat_id, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_category_state_recent
    ON category_state (matchup_id, team_id, stat_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS wp_snapshots (
    matchup_id     INTEGER NOT NULL,
    computed_at    TEXT NOT NULL,
    home_wp        REAL NOT NULL,
    away_wp        REAL NOT NULL,
    model_version  TEXT NOT NULL,
    details_json   TEXT,
    -- 1 = home_wp/away_wp were hand-edited (cosmetic graph smoothing over a data
    -- incident) and intentionally diverge from details_json's computed tally. Lets
    -- the WP↔details consistency check skip them instead of hardcoding date windows.
    edited         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (matchup_id, computed_at)
);

-- ── Player / roster / projection tables (used by the Monte Carlo model) ──

CREATE TABLE IF NOT EXISTS players (
    id                   INTEGER PRIMARY KEY,
    full_name            TEXT NOT NULL,
    pro_team_id          INTEGER,
    default_position_id  INTEGER,
    eligible_slots_json  TEXT,
    injury_status        TEXT,
    fetched_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_rosters (
    matchup_period_id  INTEGER NOT NULL,
    fantasy_team_id    INTEGER NOT NULL,
    player_id          INTEGER NOT NULL,
    lineup_slot_id     INTEGER NOT NULL,
    status             TEXT,
    fetched_at         TEXT NOT NULL,
    PRIMARY KEY (matchup_period_id, fantasy_team_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_rosters_period
    ON team_rosters (matchup_period_id, fantasy_team_id);

CREATE TABLE IF NOT EXISTS player_projections (
    player_id   INTEGER NOT NULL,
    stat_id     INTEGER NOT NULL,
    value       REAL,
    split_id    INTEGER NOT NULL,
    season_id   INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (player_id, stat_id, split_id, season_id)
);

-- ── MLB schedule (one row per game per team) ──
CREATE TABLE IF NOT EXISTS team_schedule (
    matchup_period_id          INTEGER NOT NULL,
    game_pk                    INTEGER NOT NULL,        -- MLB gamePk
    game_date                  TEXT NOT NULL,           -- YYYY-MM-DD
    pro_team_id                INTEGER NOT NULL,        -- ESPN proTeamId
    opponent_pro_team_id       INTEGER NOT NULL,
    is_home                    INTEGER NOT NULL,
    probable_pitcher_mlbam_id  INTEGER,
    probable_pitcher_name      TEXT,
    game_status                TEXT,
    current_inning             INTEGER,                 -- live inning for in-progress games
    inning_state               TEXT,                    -- "Top"/"Middle"/"Bottom"/"End" or null
    fetched_at                 TEXT NOT NULL,
    PRIMARY KEY (matchup_period_id, game_pk, pro_team_id)
);
CREATE INDEX IF NOT EXISTS idx_schedule_team
    ON team_schedule (matchup_period_id, pro_team_id);

-- ── Observed game activity per game-day (drives the chart's "Active" x-axis) ──
-- One row per (period, MLB official date). refresh-live stamps active_start the
-- first tick it sees a game In Progress and active_end once all that day's games
-- are Final. The chart collapses the dead time between these intervals.
CREATE TABLE IF NOT EXISTS game_day_activity (
    matchup_period_id  INTEGER NOT NULL,
    game_date          TEXT NOT NULL,   -- MLB official date, YYYY-MM-DD
    active_start       TEXT,            -- UTC ISO: first game seen In Progress
    active_end         TEXT,            -- UTC ISO: set once all games Final
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (matchup_period_id, game_date)
);

-- ── Live per-pitcher lines for in-progress games (in-game QS/SVHD model) ──
-- One row per pitcher who has appeared in a currently in-progress game.
-- Refreshed each fast tick; rows for a game are cleared when it's no longer
-- in progress. Matched to rostered players by normalized name.
CREATE TABLE IF NOT EXISTS live_pitchers (
    game_pk        INTEGER NOT NULL,
    mlbam_id       INTEGER NOT NULL,
    name           TEXT,
    pro_team_id    INTEGER,             -- ESPN proTeamId
    order_idx      INTEGER,             -- appearance order (0 = starter)
    is_last        INTEGER,             -- 1 if currently pitching for their team
    games_started  INTEGER,
    outs           INTEGER,
    er             INTEGER,
    k              INTEGER,
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (game_pk, mlbam_id)
);
CREATE INDEX IF NOT EXISTS idx_live_pitchers_team
    ON live_pitchers (pro_team_id);

-- ── Durable archive of Final starter/reliever lines (investigation telemetry) ──
-- live_pitchers is pruned the moment a game ages out of the unsettled window, so
-- the box line that earned (or missed) a QS/SVHD credit is gone after the fact —
-- which made the 2026-06-07 deGrom double-count hard to reconstruct. This keeps a
-- write-once copy (INSERT OR IGNORE, captured the first tick a game reads Final),
-- so "which exact line was credited / was it 17 or 18 outs" is answerable offline.
-- Bounded by games/day (~30 rows/day), not by the 5-min tick cadence.
CREATE TABLE IF NOT EXISTS pitcher_final_lines (
    game_pk        INTEGER NOT NULL,
    mlbam_id       INTEGER NOT NULL,
    name           TEXT,
    pro_team_id    INTEGER,
    game_date      TEXT,
    games_started  INTEGER,
    outs           INTEGER,
    er             INTEGER,
    k              INTEGER,
    p_h            INTEGER,
    p_bb           INTEGER,
    sv             INTEGER,
    hld            INTEGER,
    final_at       TEXT,             -- first tick this line was seen Final (≈ went-Final time)
    PRIMARY KEY (game_pk, mlbam_id)
);
CREATE INDEX IF NOT EXISTS idx_pitcher_final_lines_date
    ON pitcher_final_lines (game_date);

-- ── Per-week published-block cache (publish performance) ──
-- publish rebuilds data.json every fast tick, but only the *current* week's
-- content changes per tick (settled weeks are frozen; future weeks change only on
-- the 4-hourly medium run). Each week's rendered block is cached here keyed by a
-- cheap change-stamp (max wp_snapshots.computed_at + winners + state); a week whose
-- stamp is unchanged is reused verbatim instead of re-deriving it (which costs a
-- latest_category_state read per team). NB the stamp deliberately does NOT use
-- category_state.fetched_at — fetch re-writes settled weeks' state every tick with
-- identical values, which would defeat the cache; a rare late stat correction to an
-- already-settled week is caught by `publish --rebuild` (run daily from daily.sh).
CREATE TABLE IF NOT EXISTS published_week_cache (
    period_id  INTEGER PRIMARY KEY,
    stamp      TEXT,
    block_json TEXT
);

-- ── Per-reliever live appearance state (for in-game SVHD save/hold judging) ──
-- A save/hold is determined by the conditions WHEN the reliever entered and
-- exited — not the current score. `live_pitchers` is rewritten every tick and
-- carries no run margin, so this persists each reliever's *entry* margin (the
-- first tick we see him pitching) and *exit* margin (the first tick he's no
-- longer pitching) across ticks. That lets `ingame.project_svhd` LOCK an earned
-- hold from those conditions, instead of flickering off when the lead later
-- grows past a save (a blowout) or a *later* reliever coughs it up — the bug
-- behind the 2026-06-10 Melton case. Pruned with the game (see refresh-live).
CREATE TABLE IF NOT EXISTS reliever_appearances (
    game_pk      INTEGER NOT NULL,
    mlbam_id     INTEGER NOT NULL,
    name         TEXT,
    pro_team_id  INTEGER,
    entry_margin INTEGER,            -- team margin (runs − opp) the first tick seen pitching
    exit_margin  INTEGER,            -- team margin the first tick seen exited (NULL while in)
    entered_at   TEXT,
    exited_at    TEXT,
    PRIMARY KEY (game_pk, mlbam_id)
);

-- ── Live per-batter lines for the current week's games (live OPS components) ──
-- One row per batter who has appeared, for every game in the live window (both
-- in-progress and recently-Final, until ESPN's once-daily REST settle absorbs
-- it). Lets the sim reconstruct each fantasy team's banked OPS components
-- (AB/H/2B/3B/HR/BB/HBP/SF) live instead of waiting for the ~07:00 UTC settle.
-- Matched to rostered hitters by normalized name; attributed to a fantasy team
-- only if the hitter was in an active (non-bench) lineup slot that day. Mirrors
-- live_pitchers; empty when nothing is in the window, in which case the sim
-- behaves exactly as before.
CREATE TABLE IF NOT EXISTS live_batters (
    game_pk     INTEGER NOT NULL,
    mlbam_id    INTEGER NOT NULL,
    name        TEXT,
    pro_team_id INTEGER,             -- ESPN proTeamId
    ab  INTEGER, h INTEGER, b2 INTEGER, b3 INTEGER, hr INTEGER,
    bb  INTEGER, hbp INTEGER, sf INTEGER,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (game_pk, mlbam_id)
);
CREATE INDEX IF NOT EXISTS idx_live_batters_team
    ON live_batters (pro_team_id);

-- ── Daily fantasy-lineup snapshots (who counted on a given day) ──
-- One row per (game_date, fantasy_team, player) recording the player's ESPN
-- lineup_slot_id for that day. The source of truth for "did this player's stats
-- count for the team on day D" — a player contributes a day's box-score line
-- only if their slot that day is an active (scored) slot, not bench (16) / IL
-- (17). Needed for live component reconstruction (pitching + OPS) and usable to
-- replace projected lineups with actuals for elapsed days. Snapshotted forward
-- each live tick (lineups lock daily, so the in-day snapshot is authoritative).
CREATE TABLE IF NOT EXISTS daily_lineups (
    game_date       TEXT NOT NULL,   -- MLB official date, YYYY-MM-DD
    fantasy_team_id INTEGER NOT NULL,
    player_id       INTEGER NOT NULL,
    lineup_slot_id  INTEGER,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (game_date, fantasy_team_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_lineups_day
    ON daily_lineups (game_date, fantasy_team_id);

-- ── Per-pitcher start history (anchor for the rotation-cadence SP model) ──
-- One row per (pitcher, game) that the pitcher started, derived from Final
-- games' probable pitcher (the probable IS the actual starter once a game is
-- complete). Populated forward by refresh-schedule and seeded by
-- `app backfill-starts`. Matched to rostered players by normalized name
-- (there's no ESPN↔MLBAM player-id crosswalk), so pitcher_name is stored too.
CREATE TABLE IF NOT EXISTS pitcher_starts (
    mlbam_id     INTEGER NOT NULL,
    pitcher_name TEXT,
    game_pk      INTEGER NOT NULL,
    game_date    TEXT NOT NULL,        -- YYYY-MM-DD
    pro_team_id  INTEGER,              -- ESPN proTeamId
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (mlbam_id, game_pk)
);
CREATE INDEX IF NOT EXISTS idx_pitcher_starts_name
    ON pitcher_starts (pitcher_name, game_date);

-- ── Injury return dates (from ESPN's public injuries feed) ──
-- Real estimated activation dates per player, matched to rostered players by
-- normalized name. Overrides the fixed-days IL heuristic in sim._est_return_date.
-- Excludes Day-To-Day (those usually still play). Replaced wholesale each
-- refresh-rosters run.
CREATE TABLE IF NOT EXISTS player_injuries (
    norm_name    TEXT PRIMARY KEY,
    full_name    TEXT,
    return_date  TEXT,            -- YYYY-MM-DD, ESPN's estimated return
    fetched_at   TEXT NOT NULL
);

-- ── Validation / anomaly flags (app/validate.py) ──
-- Invariant violations ('error') and anomalies ('warn') from the cheap
-- post-compute checks. Deduped per (code, matchup_id, flag_date) so a recurring
-- condition bumps occurrences/last_seen rather than spamming a row every tick.
-- Review with `app validate --list`; investigate open flags in Claude Code.
CREATE TABLE IF NOT EXISTS validation_flags (
    code         TEXT NOT NULL,     -- e.g. INV_RATE_COMPONENTS_MISSING, ANOM_WP_SWING
    matchup_id   INTEGER,           -- NULL for league-wide
    flag_date    TEXT NOT NULL,     -- YYYY-MM-DD (dedup window)
    severity     TEXT NOT NULL,     -- 'error' | 'warn'
    detail       TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    occurrences  INTEGER NOT NULL DEFAULT 1,
    resolved     INTEGER NOT NULL DEFAULT 0,
    -- Resolution provenance: who/when/why a flag was triaged closed, so the
    -- reasoning survives the chat that did it (otherwise "resolved" is a bare bit
    -- and the next investigator re-derives — or can't recover — the conclusion).
    resolved_at     TEXT,
    resolved_by     TEXT,
    resolution_note TEXT,
    PRIMARY KEY (code, matchup_id, flag_date)
);
CREATE INDEX IF NOT EXISTS idx_validation_open
    ON validation_flags (resolved, last_seen DESC);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Multiple cron tiers touch this DB. WAL lets readers (publish) run while a
    # writer (fetch/compute) holds the lock, and busy_timeout makes a contending
    # writer wait rather than erroring out with "database is locked".
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Far-future ISO sentinel so an unbounded read can share the `fetched_at <= ?`
# query path (real timestamps all sort before it).
_MAX_TS = "9999-12-31T23:59:59+00:00"


def latest_category_state(conn: sqlite3.Connection, matchup_id: int, team_id: int,
                          *, as_of: str | None = None, rank: int = 1) -> dict[int, dict]:
    """Banked category_state for one (matchup, team) as {stat_id: {"score", "result"}}.

    The value is taken per *stat* at recency `rank` (1 = latest, 2 = second-latest),
    NOT at the matchup's single latest fetch: current-period state is split-sourced
    and written in partial subsets per tick (the scrape owns the display cats; an
    idle fetch writes only REST components at a fresh timestamp), so a global
    MAX(fetched_at) would drop any stat not touched that tick — the 2026-06-04
    idle-fetch collapse. `as_of` (ISO ts) restricts to rows at-or-before it, i.e.
    exactly what a publish stamped `generated_at=as_of` would have read.

    Single source for every current-state reader (sim.load_latest_state,
    cli._latest_score_rows, validate._state_as_of/_load_state_prev)."""
    if rank == 1:
        # Fast path (the overwhelmingly common one). `GROUP BY stat_id` with
        # `MAX(fetched_at)` returns the latest row per stat via SQLite's
        # bare-columns-take-the-max-row rule; `fetched_at` is unique per
        # (matchup,team,stat) (the PK), so it's exactly the rank-1 result — but it
        # seeks the per-stat max on idx_category_state_recent instead of
        # ROW_NUMBER()'s full-partition scan (~20× faster on the big partitions
        # category_state grows into: 317ms→14ms on a 75k-row partition).
        rows = conn.execute(
            """
            SELECT stat_id, score, result, MAX(fetched_at)
            FROM category_state
            WHERE matchup_id=? AND team_id=? AND fetched_at <= ?
            GROUP BY stat_id
            """,
            (matchup_id, team_id, as_of or _MAX_TS),
        ).fetchall()
    else:
        # rank>1 (e.g. second-latest, for the banked-regression check) needs the
        # window function — GROUP BY can only reach the max.
        rows = conn.execute(
            """
            SELECT stat_id, score, result FROM (
                SELECT stat_id, score, result,
                       ROW_NUMBER() OVER (PARTITION BY stat_id ORDER BY fetched_at DESC) rn
                FROM category_state
                WHERE matchup_id=? AND team_id=? AND fetched_at <= ?
            ) WHERE rn=?
            """,
            (matchup_id, team_id, as_of or _MAX_TS, rank),
        ).fetchall()
    return {r["stat_id"]: {"score": r["score"], "result": r["result"]} for r in rows}


def init() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Migrations for installed DBs that pre-date columns added later.
        for column_def in (
            ("team_schedule", "current_inning", "INTEGER"),
            ("team_schedule", "inning_state", "TEXT"),
            ("team_schedule", "team_runs", "INTEGER"),
            ("team_schedule", "opponent_runs", "INTEGER"),
            ("team_schedule", "became_final_at", "TEXT"),  # when a game first read Final (credit boundary)
            ("scoring_settings", "lineup_slots_json", "TEXT"),
            # Pitcher hits/walks allowed — added for live WHIP components.
            ("live_pitchers", "p_h", "INTEGER"),
            ("live_pitchers", "p_bb", "INTEGER"),
            ("live_pitchers", "sv", "INTEGER"),    # saves ┐ SVHD (stat 83) = SV + HLD
            ("live_pitchers", "hld", "INTEGER"),   # holds ┘ (blown saves not scored)
            ("wp_snapshots", "edited", "INTEGER NOT NULL DEFAULT 0"),
            ("validation_flags", "resolved_at", "TEXT"),
            ("validation_flags", "resolved_by", "TEXT"),
            ("validation_flags", "resolution_note", "TEXT"),
        ):
            table, col, type_ = column_def
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")
            except sqlite3.OperationalError:
                pass  # column already present
        conn.commit()
    finally:
        conn.close()
