"""Tests for authoritative daily-lineup capture (app/cli.py + app/mlb.py).

The 2026-08-10 phantom-credit bug: `refresh-live` read the *current* lineup once
and kept each day's FIRST snapshot, so a tick landing before ESPN locked froze
the manager's pre-game intent. Bryan Baker was stored in slot 15 for 08-04 while
ESPN had him benched, `sim.load_settled_floor` credited his save, and the site
published Swamp Dragons SVHD 4 against ESPN's 3 — a lost category shown as a tie.

Covers:
  - mlb.scoring_period_for_date: the date -> ESPN scoringPeriodId mapping.
  - cli._authoritative_lineups: per-day fetch, failure isolation, empty-drop.
  - cli._replace_daily_lineups: full replace, incl. dropping stale rows.
  - the end-to-end effect on sim.load_settled_floor.
"""
import sqlite3
from datetime import date

import pytest

from app import cli, mlb, sim


# ───────────────────────── scoring-period mapping ─────────────────────────

def test_scoring_period_anchor_round_trips():
    assert mlb.scoring_period_for_date(mlb.SEASON_ANCHOR_SCORING_PERIOD_DATE) == \
        mlb.SEASON_ANCHOR_SCORING_PERIOD


def test_scoring_period_is_linear_in_days():
    # Verified against ESPN's own mRoster on these dates (2026-08-10).
    assert mlb.scoring_period_for_date(date(2026, 8, 4)) == 133
    assert mlb.scoring_period_for_date(date(2026, 8, 7)) == 136
    assert mlb.scoring_period_for_date(date(2026, 8, 8)) == 137


def test_scoring_period_handles_dates_before_the_anchor():
    assert mlb.scoring_period_for_date(date(2026, 5, 24)) == \
        mlb.SEASON_ANCHOR_SCORING_PERIOD - 1


# ───────────────────────── per-day authoritative fetch ─────────────────────────

def _row(team, pid, slot, name="P"):
    return {"fantasy_team_id": team, "player_id": pid,
            "lineup_slot_id": slot, "full_name": name}


def test_authoritative_lineups_asks_for_each_days_own_scoring_period(monkeypatch):
    """The core fix: one fetch per date, keyed by that date's SPID — not one
    current-day fetch smeared across every date."""
    seen = []

    def fake(spid=None):
        seen.append(spid)
        return [_row(3, 1, 15)]

    monkeypatch.setattr(cli.espn, "fetch_daily_lineups", fake)
    out = cli._authoritative_lineups(["2026-08-04", "2026-08-07"])

    assert seen == [133, 136]
    assert set(out) == {"2026-08-04", "2026-08-07"}


def test_authoritative_lineups_isolates_a_failing_day(monkeypatch):
    def fake(spid=None):
        if spid == 133:
            raise RuntimeError("ESPN auth hiccup")
        return [_row(3, 1, 15)]

    monkeypatch.setattr(cli.espn, "fetch_daily_lineups", fake)
    out = cli._authoritative_lineups(["2026-08-04", "2026-08-07"])

    # The bad day is dropped (its stored rows stay untouched); the good day lands.
    assert set(out) == {"2026-08-07"}


def test_authoritative_lineups_drops_an_empty_response(monkeypatch):
    # An empty result must never reach _replace_daily_lineups, which would
    # otherwise blank a day that we do have good data for.
    monkeypatch.setattr(cli.espn, "fetch_daily_lineups", lambda spid=None: [])
    assert cli._authoritative_lineups(["2026-08-04"]) == {}


# ───────────────────────── replace semantics ─────────────────────────

def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE daily_lineups (
        game_date TEXT NOT NULL, fantasy_team_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL, lineup_slot_id INTEGER, fetched_at TEXT NOT NULL,
        PRIMARY KEY (game_date, fantasy_team_id, player_id))""")
    return c


def test_replace_overwrites_a_pre_lock_slot():
    """The Baker case: a stored active slot must lose to ESPN's bench slot.
    Under the old INSERT OR IGNORE this was frozen forever."""
    c = _conn()
    c.execute("INSERT INTO daily_lineups VALUES ('2026-08-04',3,641329,15,'t0')")

    cli._replace_daily_lineups(c, "2026-08-04", [_row(3, 641329, 16)], "t1")

    assert c.execute("SELECT lineup_slot_id FROM daily_lineups").fetchone()[0] == 16


def test_replace_drops_a_row_espn_no_longer_lists():
    # A player dropped from the roster mid-week would otherwise keep a stale
    # active-slot row and stay creditable for that day.
    c = _conn()
    c.execute("INSERT INTO daily_lineups VALUES ('2026-08-04',3,111,15,'t0')")
    c.execute("INSERT INTO daily_lineups VALUES ('2026-08-04',3,222,15,'t0')")

    cli._replace_daily_lineups(c, "2026-08-04", [_row(3, 111, 15)], "t1")

    assert [r[0] for r in c.execute("SELECT player_id FROM daily_lineups")] == [111]


def test_replace_leaves_other_days_alone():
    c = _conn()
    c.execute("INSERT INTO daily_lineups VALUES ('2026-08-03',3,111,15,'t0')")

    cli._replace_daily_lineups(c, "2026-08-04", [_row(3, 111, 16)], "t1")

    slots = dict(c.execute("SELECT game_date, lineup_slot_id FROM daily_lineups"))
    assert slots == {"2026-08-03": 15, "2026-08-04": 16}


# ───────────────────── the credit that depended on it ─────────────────────

def _floor_conn(slot):
    """Minimal DB for load_settled_floor: one archived save on 2026-08-04."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE matchups (id INTEGER PRIMARY KEY, matchup_period_id INTEGER)")
    c.execute("INSERT INTO matchups VALUES (105, 18)")
    c.execute("CREATE TABLE players (id INTEGER PRIMARY KEY, full_name TEXT)")
    c.execute("INSERT INTO players VALUES (641329, 'Bryan Baker')")
    c.execute("""CREATE TABLE daily_lineups (game_date TEXT, fantasy_team_id INTEGER,
                 player_id INTEGER, lineup_slot_id INTEGER, fetched_at TEXT)""")
    c.execute("INSERT INTO daily_lineups VALUES ('2026-08-04',3,641329,?, 't0')", (slot,))
    c.execute("""CREATE TABLE pitcher_final_lines (game_pk INTEGER, name TEXT,
                 game_date TEXT, games_started INTEGER, outs INTEGER, er INTEGER,
                 sv INTEGER, hld INTEGER, final_at TEXT)""")
    c.execute("INSERT INTO pitcher_final_lines VALUES "
              "(824321,'Bryan Baker','2026-08-04',0,3,0,1,0,'2026-08-05T04:05:03+00:00')")
    return c


@pytest.mark.parametrize("slot,expected", [(15, 1), (16, 0)])
def test_settled_floor_follows_the_recorded_slot(slot, expected):
    """Why the stale row was expensive: the floor credits the save iff the slot
    is active, and publish takes max(scrape, floor) — so a wrong 15 becomes a
    phantom the scrape can never pull back down."""
    floor = sim.load_settled_floor(_floor_conn(slot), 105, 3, (sim.STAT_SVHD,),
                                   since_date="2026-08-10")
    assert floor[sim.STAT_SVHD] == expected
