"""Investigation telemetry added after the 2026-06-07 deGrom double-count:
  - live-recon decisions persisted per snapshot (`_live_recon_block`),
  - Final pitcher lines archived past the live_pitchers prune (`_archive_final_lines`),
  - `team_schedule.became_final_at` stamped once at the Final transition
    (`_TEAM_SCHEDULE_UPSERT`).
These were the three things missing when reconstructing that incident.
"""
import sqlite3

from app import cli


# ── #1 live-recon block (persisted in details_json) ──

def test_live_recon_block_none_when_no_live_games():
    assert cli._live_recon_block("2026-06-07", [], []) is None

def test_live_recon_block_carries_scrape_floor_box():
    hdec = [{"group": "qs", "scraped": 3, "floor": 2, "qs_added": 1, "result": 3}]
    b = cli._live_recon_block("2026-06-07", hdec, [])
    assert b == {"since_date": "2026-06-07", "home": hdec, "away": []}


# ── #2 Final-line archive ──

def _arch_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE pitcher_final_lines (game_pk INT, mlbam_id INT, name TEXT,
           pro_team_id INT, game_date TEXT, games_started INT, outs INT, er INT,
           k INT, p_h INT, p_bb INT, sv INT, hld INT, final_at TEXT,
           PRIMARY KEY (game_pk, mlbam_id))"""
    )
    return conn

def _line(pk, mlbam, outs, er, **kw):
    return {"game_pk": pk, "mlbam_id": mlbam, "name": kw.get("name", "P"),
            "espn_team_id": 26, "games_started": kw.get("gs", 1), "outs": outs,
            "er": er, "k": 5, "p_h": 4, "p_bb": 1,
            "sv": kw.get("sv", 0), "hld": kw.get("hld", 0)}

def test_archive_only_final_games():
    conn = _arch_db()
    lines = [_line(1, 101, 18, 0), _line(2, 102, 15, 2)]   # game1 Final, game2 live
    status = {1: {"Final"}, 2: {"In Progress"}}
    dates = {1: "2026-06-07", 2: "2026-06-07"}
    n = cli._archive_final_lines(conn, lines, status, dates, "t0")
    assert n == 1
    rows = conn.execute("SELECT mlbam_id, outs FROM pitcher_final_lines").fetchall()
    assert [(r["mlbam_id"], r["outs"]) for r in rows] == [(101, 18)]   # only the Final one

def test_archive_write_once_keeps_first_final_at():
    conn = _arch_db()
    line, status, dates = [_line(1, 101, 18, 0)], {1: {"Final"}}, {1: "2026-06-07"}
    assert cli._archive_final_lines(conn, line, status, dates, "t0") == 1
    assert cli._archive_final_lines(conn, line, status, dates, "t1") == 0   # already archived
    assert conn.execute("SELECT final_at FROM pitcher_final_lines").fetchone()[0] == "t0"


# ── #3 became_final_at stamping (via the shared upsert SQL) ──

def _sched_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE team_schedule (matchup_period_id INT, game_pk INT, game_date TEXT,
           pro_team_id INT, opponent_pro_team_id INT, is_home INT,
           probable_pitcher_mlbam_id INT, probable_pitcher_name TEXT, game_status TEXT,
           current_inning INT, inning_state TEXT, team_runs INT, opponent_runs INT,
           became_final_at TEXT, fetched_at TEXT,
           PRIMARY KEY (matchup_period_id, game_pk, pro_team_id))"""
    )
    return conn

_FINAL = {"Final", "Game Over", "Completed Early"}

def _upsert(conn, status, at):
    final_at = at if status in _FINAL else None   # mirrors refresh_live's compute
    conn.execute(cli._TEAM_SCHEDULE_UPSERT,
                 (10, 500, "2026-06-07", 26, 27, 1, None, None, status,
                  None, None, None, None, final_at, at))
    conn.commit()

def _stamp(conn):
    return conn.execute("SELECT became_final_at FROM team_schedule").fetchone()[0]

def test_became_final_at_null_while_in_progress():
    conn = _sched_db()
    _upsert(conn, "In Progress", "t0")
    assert _stamp(conn) is None

def test_became_final_at_stamps_at_transition_and_holds():
    conn = _sched_db()
    _upsert(conn, "In Progress", "t0")
    _upsert(conn, "Final", "t1")          # transition → stamped t1
    assert _stamp(conn) == "t1"
    _upsert(conn, "Final", "t2")          # later ticks keep the FIRST stamp
    assert _stamp(conn) == "t1"

def test_became_final_at_stamps_on_insert_if_already_final():
    # a game first seen already Final (e.g. backfill) → stamped on the insert
    conn = _sched_db()
    _upsert(conn, "Final", "t5")
    assert _stamp(conn) == "t5"


# ── reliever entry/exit margin tracking (for the in-game SVHD save/hold fix) ──

def _ra_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE reliever_appearances (game_pk INT, mlbam_id INT, name TEXT,
           pro_team_id INT, entry_margin INT, exit_margin INT, entered_at TEXT,
           exited_at TEXT, PRIMARY KEY (game_pk, mlbam_id))"""
    )
    return conn

def _rp(mlbam, is_last, gs=0, team=26):
    return {"game_pk": 1, "mlbam_id": mlbam, "name": f"R{mlbam}", "espn_team_id": team,
            "is_last": is_last, "games_started": gs, "outs": 3}

def _row(conn):
    return conn.execute("SELECT entry_margin, exit_margin FROM reliever_appearances").fetchone()

def test_track_entry_recorded_once_then_exit_recorded_once():
    conn = _ra_db()
    cli._track_reliever_appearances(conn, [_rp(50, is_last=1)], {(1, 26): 2}, "t0")
    assert tuple(_row(conn)) == (2, None)                      # entry stamped
    cli._track_reliever_appearances(conn, [_rp(50, is_last=1)], {(1, 26): 5}, "t1")
    assert _row(conn)["entry_margin"] == 2                     # entry NOT overwritten
    cli._track_reliever_appearances(conn, [_rp(50, is_last=0)], {(1, 26): 8}, "t2")
    assert tuple(_row(conn)) == (2, 8)                         # exit stamped (blowout margin)
    cli._track_reliever_appearances(conn, [_rp(50, is_last=0)], {(1, 26): 9}, "t3")
    assert _row(conn)["exit_margin"] == 8                      # exit NOT overwritten

def test_track_skips_starters():
    conn = _ra_db()
    cli._track_reliever_appearances(conn, [_rp(60, is_last=1, gs=1)], {(1, 26): 2}, "t0")
    assert conn.execute("SELECT COUNT(*) FROM reliever_appearances").fetchone()[0] == 0

def test_track_skips_when_margin_unknown():
    conn = _ra_db()
    cli._track_reliever_appearances(conn, [_rp(50, is_last=1)], {}, "t0")   # no margin
    assert conn.execute("SELECT COUNT(*) FROM reliever_appearances").fetchone()[0] == 0


# ── migration-drift safeguard: the CLI group ensures schema before any subcommand ──

def test_cli_group_ensures_schema_before_subcommand(tmp_path, monkeypatch):
    """A schema-touching code change must not crash a cron tick that runs before the
    migration (the 2026-06-10 reliever_appearances window). The group callback runs
    db.init() first, so a missing table is (re)created on any invocation."""
    from pathlib import Path
    from click.testing import CliRunner
    from app import db
    from app.cli import cli
    dbfile = Path(tmp_path / "drift.db")
    monkeypatch.setattr(db, "DB_PATH", dbfile)

    def _has_table():
        return sqlite3.connect(str(dbfile)).execute(
            "SELECT count(*) FROM sqlite_master WHERE name='reliever_appearances'"
        ).fetchone()[0]

    assert _has_table() == 0                      # fresh DB, no tables
    CliRunner().invoke(cli, ["validate"])         # any subcommand → group callback inits
    assert _has_table() == 1                      # schema ensured
