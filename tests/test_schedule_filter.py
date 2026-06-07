"""load_schedule_by_team must exclude phantom games that aren't really part of
the week — postponed games whose date has drifted outside the period window (or
whose status is Postponed/Suspended/Cancelled). Regression for the 2026-06-06
bug where a postponed game (kept in period 10 by the PK, re-dated to August)
made Ranger Suarez project 2.0 starts and inflated a teammate RP's appearances.
"""
import sqlite3

from app import sim


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE team_schedule (
            matchup_period_id INT, game_pk INT, game_date TEXT, pro_team_id INT,
            opponent_pro_team_id INT, is_home INT, probable_pitcher_mlbam_id INT,
            probable_pitcher_name TEXT, game_status TEXT, current_inning INT,
            inning_state TEXT, team_runs INT, opponent_runs INT,
            PRIMARY KEY (matchup_period_id, game_pk, pro_team_id))"""
    )
    return conn


def _row(conn, *, pk, date, status, probable="Ranger Suarez", team=2):
    conn.execute(
        "INSERT INTO team_schedule VALUES (10,?,?,?,99,1,1,?,?,NULL,NULL,NULL,NULL)",
        (pk, date, team, probable, status),
    )


def test_postponed_out_of_window_game_excluded():
    conn = _mem_db()
    # period 10 window is 2026-06-01..06-07.
    _row(conn, pk=823537, date="2026-06-07", status="Scheduled")     # real
    _row(conn, pk=823539, date="2026-08-29", status="Postponed")     # phantom
    conn.commit()
    sched = sim.load_schedule_by_team(conn, 10)
    games = sched.get(2, [])
    assert len(games) == 1                       # phantom dropped
    assert games[0]["game_pk"] == 823537
    # → exactly one probable start for Suarez, not two.
    assert sim._probable_starts_for("Ranger Suarez", 2, sched, 6.0) == 1.0


def test_in_window_postponed_status_excluded():
    conn = _mem_db()
    _row(conn, pk=1, date="2026-06-07", status="Scheduled")
    _row(conn, pk=2, date="2026-06-06", status="Postponed")          # in-window but postponed
    conn.commit()
    games = sim.load_schedule_by_team(conn, 10).get(2, [])
    assert [g["game_pk"] for g in games] == [1]


def test_normal_schedule_unaffected():
    conn = _mem_db()
    _row(conn, pk=1, date="2026-06-05", status="Final")
    _row(conn, pk=2, date="2026-06-07", status="Scheduled")
    conn.commit()
    games = sim.load_schedule_by_team(conn, 10).get(2, [])
    assert {g["game_pk"] for g in games} == {1, 2}
