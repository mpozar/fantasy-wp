"""The ROS-projection archive: first write per period wins.

`player_projections` has no period key and every fetch overwrites it, so the
inputs a past week was projected from are gone — which is why today's model can
never be scored against history (`scripts/calibration.py` can only score the
model *as it ran*). This archive fixes that going forward, but only if a later
refresh within the same week does NOT overwrite the first capture: refresh-rosters
runs 4-hourly, and it's the FIRST capture (before first pitch) that corresponds
to the start-of-week forecast. An overwrite would silently turn the archive into
a record of mid-week values, which looks identical in the schema and is useless
for backtesting.
"""
import sqlite3

from app import db, sim


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def _archive(conn, period, player, stat, value, when):
    conn.execute(
        """INSERT OR IGNORE INTO ros_projection_archive
           (matchup_period_id, player_id, stat_id, value, season_id, captured_at)
           VALUES (?,?,?,?,2026,?)""", (period, player, stat, value, when))
    conn.commit()


def _get(conn, period, player, stat):
    r = conn.execute(
        "SELECT value, captured_at FROM ros_projection_archive WHERE "
        "matchup_period_id=? AND player_id=? AND stat_id=?",
        (period, player, stat)).fetchone()
    return (r["value"], r["captured_at"]) if r else None


def test_first_write_wins_within_a_period():
    conn = _db()
    _archive(conn, 19, 1, 63, 6.0, "2026-08-10T06:00:00+00:00")   # pre-play
    _archive(conn, 19, 1, 63, 3.1, "2026-08-13T18:00:00+00:00")   # mid-week
    assert _get(conn, 19, 1, 63) == (6.0, "2026-08-10T06:00:00+00:00")


def test_each_period_keeps_its_own_capture():
    conn = _db()
    _archive(conn, 19, 1, 63, 6.0, "2026-08-10T06:00:00+00:00")
    _archive(conn, 20, 1, 63, 3.1, "2026-08-17T06:00:00+00:00")
    assert _get(conn, 19, 1, 63)[0] == 6.0
    assert _get(conn, 20, 1, 63)[0] == 3.1


def test_stats_and_players_do_not_collide():
    conn = _db()
    _archive(conn, 19, 1, 63, 6.0, "t")
    _archive(conn, 19, 1, 48, 70.0, "t")
    _archive(conn, 19, 2, 63, 4.0, "t")
    assert _get(conn, 19, 1, 63)[0] == 6.0
    assert _get(conn, 19, 1, 48)[0] == 70.0
    assert _get(conn, 19, 2, 63)[0] == 4.0


def test_schema_is_idempotent():
    # db.init() runs before every subcommand (the editable-install drift guard),
    # so re-applying the schema must not error or wipe rows.
    conn = _db()
    _archive(conn, 19, 1, 63, 6.0, "t")
    conn.executescript(db.SCHEMA)
    assert _get(conn, 19, 1, 63)[0] == 6.0


def test_only_ros_split_is_archived():
    # The archive exists to preserve ROS (split=6) inputs; full-season and
    # last-7-day blocks are not what the sim projects from.
    assert sim.ROS_SPLIT_ID == 6
