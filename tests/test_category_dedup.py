"""Dedup guard on non-current-period category_state writes
(`cli._write_noncurrent_score`).

Settled past weeks never change and future weeks are all-zero, but `fetch` runs
every 5 min and used to re-INSERT an identical snapshot of every non-current
matchup each tick — the duplicate writes that grew category_state to ~21M rows.
The guard skips a write whose (score, result) is unchanged from the latest stored
value, while still letting a rare late ESPN correction through (the value differs).
"""

import sqlite3

from app.cli import _write_noncurrent_score

STAT_H = 1     # counting
STAT_ERA = 47  # rate


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE category_state (matchup_id INT, team_id INT, "
              "stat_id INT, score REAL, result TEXT, fetched_at TEXT)")
    return c


def _count(c):
    return c.execute("SELECT COUNT(*) FROM category_state").fetchone()[0]


def test_new_cell_is_written():
    c = _conn()
    prev: dict = {}
    assert _write_noncurrent_score(c, prev, 1, 1, STAT_H, 12.0, "WIN", "t1") is True
    assert _count(c) == 1


def test_unchanged_value_is_skipped():
    c = _conn()
    prev = {(1, 1, STAT_H): (12.0, "WIN")}
    assert _write_noncurrent_score(c, prev, 1, 1, STAT_H, 12.0, "WIN", "t2") is False
    assert _count(c) == 0  # no duplicate row appended


def test_changed_score_is_written():
    c = _conn()
    prev = {(1, 1, STAT_H): (12.0, "WIN")}
    # A late ESPN correction to a settled week — score moved, must land.
    assert _write_noncurrent_score(c, prev, 1, 1, STAT_H, 13.0, "WIN", "t2") is True
    assert _count(c) == 1


def test_changed_result_is_written():
    c = _conn()
    prev = {(1, 1, STAT_H): (12.0, "LOSS")}
    # Same score but the head-to-head result flipped (opponent's stat corrected).
    assert _write_noncurrent_score(c, prev, 1, 1, STAT_H, 12.0, "WIN", "t2") is True
    assert _count(c) == 1


def test_rate_cell_unchanged_skipped():
    c = _conn()
    prev = {(1, 1, STAT_ERA): (3.27, "WIN")}
    assert _write_noncurrent_score(c, prev, 1, 1, STAT_ERA, 3.27, "WIN", "t2") is False
    assert _count(c) == 0


def test_distinct_cells_independent():
    c = _conn()
    prev = {(1, 1, STAT_H): (12.0, "WIN")}
    # Same matchup/team, different stat → no prev entry → written.
    assert _write_noncurrent_score(c, prev, 1, 1, STAT_ERA, 3.27, "WIN", "t2") is True
    # Different team, same stat → no prev entry → written.
    assert _write_noncurrent_score(c, prev, 1, 2, STAT_H, 9.0, "LOSS", "t2") is True
    assert _count(c) == 2
