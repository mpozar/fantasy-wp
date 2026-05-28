"""SQLite schema + helpers."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scoring_settings (
    league_id           INTEGER NOT NULL,
    season_id           INTEGER NOT NULL,
    name                TEXT NOT NULL,
    size                INTEGER NOT NULL,
    scoring_type        TEXT NOT NULL,
    tiebreaker_stat_id  INTEGER,
    categories_json     TEXT NOT NULL,
    fetched_at          TEXT NOT NULL,
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
        conn.commit()
    finally:
        conn.close()
