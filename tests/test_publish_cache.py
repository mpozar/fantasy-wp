"""Publish performance: the latest_category_state fast path + the per-week block cache.

- `latest_category_state` rank-1 GROUP BY must match the window read (and respect as_of).
- `_week_stamp` must move exactly on the inputs that change a week's rendered block.
- publish must reuse unchanged weeks, rebuild changed ones, honor --rebuild, and emit
  byte-identical output whether a week came from cache or a fresh build.
"""
import datetime
import json
import pathlib
import sqlite3

from app import cli, db, mlb


# ── latest_category_state: GROUP BY rank-1 fast path ──

def _cs_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE category_state (matchup_id INT, team_id INT, stat_id INT,
           score REAL, result TEXT, fetched_at TEXT,
           PRIMARY KEY (matchup_id, team_id, stat_id, fetched_at))"""
    )
    return conn

def _put(conn, sid, score, result, at):
    conn.execute("INSERT INTO category_state VALUES (1,20,?,?,?,?)", (sid, score, result, at))

def test_latest_rank1_returns_latest_per_stat():
    conn = _cs_db()
    _put(conn, 1, 30, "LOSS", "2026-06-10T10:00")
    _put(conn, 1, 31, "TIE", "2026-06-10T10:30")
    _put(conn, 1, 33, "WIN", "2026-06-10T11:00")     # latest for stat 1
    _put(conn, 48, 28, "WIN", "2026-06-10T10:00")
    _put(conn, 48, 30, "LOSS", "2026-06-10T11:00")   # latest for stat 48
    conn.commit()
    got = db.latest_category_state(conn, 1, 20)
    assert got[1] == {"score": 33, "result": "WIN"}
    assert got[48] == {"score": 30, "result": "LOSS"}

def test_latest_rank1_respects_as_of_and_rank2_is_second_latest():
    conn = _cs_db()
    _put(conn, 1, 30, "LOSS", "2026-06-10T10:00")
    _put(conn, 1, 33, "WIN", "2026-06-10T11:00")
    conn.commit()
    # as_of before the second tick → the first value
    assert db.latest_category_state(conn, 1, 20, as_of="2026-06-10T10:30")[1] == {"score": 30, "result": "LOSS"}
    # rank=2 (window path) → second-latest
    assert db.latest_category_state(conn, 1, 20, rank=2)[1] == {"score": 30, "result": "LOSS"}


# ── _week_stamp ──

def _stamp_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE wp_snapshots (matchup_id INT, computed_at TEXT, edited INT)")
    return conn

def test_week_stamp_moves_on_each_relevant_input():
    conn = _stamp_db()
    conn.execute("INSERT INTO wp_snapshots VALUES (101,'2026-06-10T12:00',0)")
    ms = [{"id": 101, "winner": "UNDECIDED"}]
    base = cli._week_stamp(conn, 11, ms, "live")
    assert cli._week_stamp(conn, 11, ms, "live") == base          # stable on identical inputs
    conn.execute("INSERT INTO wp_snapshots VALUES (101,'2026-06-10T12:05',0)")
    assert cli._week_stamp(conn, 11, ms, "live") != base          # new compute
    assert cli._week_stamp(conn, 11, [{"id": 101, "winner": "HOME"}], "live") \
        != cli._week_stamp(conn, 11, ms, "live")                  # winner change
    assert cli._week_stamp(conn, 11, ms, "final") != cli._week_stamp(conn, 11, ms, "live")  # state change

def test_week_stamp_moves_on_edited_flag():
    conn = _stamp_db()
    conn.execute("INSERT INTO wp_snapshots VALUES (101,'2026-06-10T12:00',0)")
    ms = [{"id": 101, "winner": "HOME"}]
    base = cli._week_stamp(conn, 11, ms, "final")
    conn.execute("UPDATE wp_snapshots SET edited=1 WHERE matchup_id=101")
    assert cli._week_stamp(conn, 11, ms, "final") != base         # a hand-smoothing edit forces rebuild


# ── publish cache end-to-end (heavy renderers stubbed; cache logic exercised) ──

def _pub_setup(tmp_path, monkeypatch):
    dbfile = pathlib.Path(tmp_path / "pub.db")
    monkeypatch.setattr(db, "DB_PATH", dbfile)
    db.init()
    c = sqlite3.connect(str(dbfile))
    c.execute("INSERT INTO scoring_settings (league_id,season_id,name,size,scoring_type,"
              "tiebreaker_stat_id,categories_json,fetched_at) VALUES (?,?,?,?,?,?,?,?)",
              (cli.LEAGUE_ID, cli.SEASON_ID, "Test", 12, "H2H_CATEGORY", 1,
               json.dumps([{"stat_id": 1}]), "t"))
    for tid in (10, 11, 12, 13):
        c.execute("INSERT INTO teams (id,name,fetched_at) VALUES (?,?,?)", (tid, f"T{tid}", "t"))
    c.execute("INSERT INTO matchups VALUES (101,10,10,11,'HOME','t')")        # settled week
    c.execute("INSERT INTO matchups VALUES (111,11,12,13,'UNDECIDED','t')")   # current week
    c.execute("INSERT INTO wp_snapshots VALUES (101,'2026-06-01T00:00',0.6,0.4,'mc-v1','{}',0)")
    c.execute("INSERT INTO wp_snapshots VALUES (111,'2026-06-10T12:00',0.5,0.5,'mc-v1','{}',0)")
    c.commit(); c.close()

    rendered = []   # matchup_period_ids whose blocks got (re)built this run
    monkeypatch.setattr(cli, "_matchup_block",
                        lambda conn, teams, m, *, started, live: rendered.append(m["matchup_period_id"]) or {"matchup_id": m["id"]})
    monkeypatch.setattr(cli, "_week_state", lambda conn, pid: "final" if pid == 10 else "live")
    monkeypatch.setattr(cli, "_active_intervals", lambda conn, pid, now: [])
    monkeypatch.setattr(cli, "_current_matchup_period", lambda conn: 11)
    monkeypatch.setattr(cli, "_last_regular_season_period", lambda conn: 11)
    monkeypatch.setattr(cli, "_now_iso", lambda: "2026-06-10T12:00:00+00:00")
    monkeypatch.setattr(mlb, "matchup_period_window",
                        lambda pid: (datetime.date(2026, 6, 1), datetime.date(2026, 6, 7)))
    written = {}
    monkeypatch.setattr(pathlib.Path, "write_text",
                        lambda self, txt, *a, **k: written.__setitem__("json", txt))
    monkeypatch.setattr(pathlib.Path, "stat", lambda self, *a, **k: type("S", (), {"st_size": 0})())
    return dbfile, rendered, written

def test_publish_cache_reuse_rebuild_identical(tmp_path, monkeypatch):
    dbfile, rendered, written = _pub_setup(tmp_path, monkeypatch)

    cli.publish.callback(rebuild=False)                # run 1: cold cache → both weeks built
    assert sorted(rendered) == [10, 11]
    first = written["json"]

    rendered.clear()
    cli.publish.callback(rebuild=False)                # run 2: nothing changed → 0 rebuilds
    assert rendered == []                              # all served from cache
    assert written["json"] == first                    # byte-identical output

    # change only the current week (new compute) → only week 11 rebuilds
    c = sqlite3.connect(str(dbfile))
    c.execute("INSERT INTO wp_snapshots VALUES (111,'2026-06-10T12:05',0.55,0.45,'mc-v1','{}',0)")
    c.commit(); c.close()
    rendered.clear()
    cli.publish.callback(rebuild=False)
    assert rendered == [11]                            # settled week 10 still cached

    rendered.clear()
    cli.publish.callback(rebuild=True)                 # --rebuild → everything
    assert sorted(rendered) == [10, 11]
