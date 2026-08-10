"""batter_final_lines — the hitter analogue of pitcher_final_lines.

`live_batters` is pruned once a game ages out of the unsettled window, so before
this there was no record of what a hitter actually did. That gap is why hitter
accuracy could only be inferred with the unit-free ratio trick (HR/H, which
cancels games-played): a hitter's actual games-played had nothing to compare
against, so the ~+8% lineup-days over-projection measured 2026-08-10 is an
inference, not a direct reading.

Two properties matter: only FINAL games are archived (an in-progress line still
moves), and the first Final capture WINS (a later tick must not overwrite it).
"""
import sqlite3

from app import cli, db

FINAL = next(iter(cli._FINAL_GAME_STATES))


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def _line(**kw):
    base = dict(game_pk=1, mlbam_id=100, name="Test Hitter", espn_team_id=19,
                ab=4, h=2, b2=1, b3=0, hr=1, bb=1, hbp=0, sf=0, r=2, sb=1)
    base.update(kw)
    return base


def _rows(conn):
    return conn.execute("SELECT * FROM batter_final_lines ORDER BY mlbam_id").fetchall()


def test_archives_a_final_line_with_every_component():
    conn = _db()
    n = cli._archive_final_batter_lines(
        conn, [_line()], {1: {FINAL}}, {1: "2026-08-09"}, "t0")
    conn.commit()
    assert n == 1
    r = _rows(conn)[0]
    # Full OPS component set + the scored counting cats.
    assert (r["ab"], r["h"], r["b2"], r["b3"], r["hr"]) == (4, 2, 1, 0, 1)
    assert (r["bb"], r["hbp"], r["sf"], r["r"], r["sb"]) == (1, 0, 0, 2, 1)
    assert r["game_date"] == "2026-08-09"
    assert r["pro_team_id"] == 19


def test_skips_a_game_still_in_progress():
    conn = _db()
    n = cli._archive_final_batter_lines(
        conn, [_line()], {1: {"In Progress"}}, {1: "2026-08-09"}, "t0")
    conn.commit()
    assert n == 0 and _rows(conn) == []


def test_first_final_capture_wins():
    conn = _db()
    cli._archive_final_batter_lines(
        conn, [_line(h=2, hr=1)], {1: {FINAL}}, {1: "2026-08-09"}, "t0")
    # A later tick re-reads the same game with different (e.g. corrected) numbers.
    cli._archive_final_batter_lines(
        conn, [_line(h=9, hr=9)], {1: {FINAL}}, {1: "2026-08-09"}, "t1")
    conn.commit()
    rows = _rows(conn)
    assert len(rows) == 1
    assert (rows[0]["h"], rows[0]["hr"], rows[0]["final_at"]) == (2, 1, "t0")


def test_separate_players_and_games_do_not_collide():
    conn = _db()
    cli._archive_final_batter_lines(
        conn, [_line(mlbam_id=100), _line(mlbam_id=200, h=0)],
        {1: {FINAL}}, {1: "2026-08-09"}, "t0")
    cli._archive_final_batter_lines(
        conn, [_line(game_pk=2, mlbam_id=100, h=3)],
        {2: {FINAL}}, {2: "2026-08-10"}, "t0")
    conn.commit()
    assert len(conn.execute("SELECT 1 FROM batter_final_lines").fetchall()) == 3


def test_missing_r_and_sb_default_to_zero():
    # Older/partial box lines may omit them; they must not become NULL.
    conn = _db()
    line = _line()
    del line["r"], line["sb"]
    cli._archive_final_batter_lines(conn, [line], {1: {FINAL}}, {1: "d"}, "t0")
    conn.commit()
    r = _rows(conn)[0]
    assert r["r"] == 0 and r["sb"] == 0


def test_schema_reapplication_keeps_rows():
    conn = _db()
    cli._archive_final_batter_lines(conn, [_line()], {1: {FINAL}}, {1: "d"}, "t0")
    conn.commit()
    conn.executescript(db.SCHEMA)      # db.init() runs before every subcommand
    assert len(_rows(conn)) == 1
