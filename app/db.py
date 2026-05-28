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
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Migrations for installed DBs that pre-date columns added later.
        for column_def in (
            ("team_schedule", "current_inning", "INTEGER"),
            ("team_schedule", "inning_state", "TEXT"),
            ("scoring_settings", "lineup_slots_json", "TEXT"),
        ):
            table, col, type_ = column_def
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")
            except sqlite3.OperationalError:
                pass  # column already present
        conn.commit()
    finally:
        conn.close()
