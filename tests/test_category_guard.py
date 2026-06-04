"""Monotonicity guard on current-period category_state writes
(`cli._write_category_score`).

Counting stats are cumulative within a scoring period, so a value below the
last-good one is a stale/partial read (laggy REST, mid-render scrape, a dropped
two-way line) and must be rejected. Rate stats (OPS/ERA/WHIP) move freely.
"""

import sqlite3

from app import sim
from app.cli import _write_category_score, _RATE_STAT_IDS

STAT_K = 48  # counting
STAT_OUTS = 34  # counting


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE category_state (matchup_id INT, team_id INT, "
              "stat_id INT, score REAL, result TEXT, fetched_at TEXT)")
    return c


def _latest(c, sid):
    r = c.execute("SELECT score FROM category_state WHERE stat_id=? "
                  "ORDER BY fetched_at DESC LIMIT 1", (sid,)).fetchone()
    return r["score"] if r else None


def test_counting_regression_rejected():
    c = _conn()
    lg = {(1, 1, STAT_K): 26.0}            # K last-good = 26
    assert _write_category_score(c, lg, 1, 1, STAT_K, 20.0, None, "t1") is False
    assert _latest(c, STAT_K) is None       # nothing written — kept last-good


def test_counting_increase_allowed():
    c = _conn()
    lg = {(1, 1, STAT_K): 26.0}
    assert _write_category_score(c, lg, 1, 1, STAT_K, 30.0, None, "t1") is True
    assert _latest(c, STAT_K) == 30.0


def test_no_baseline_writes_through():
    c = _conn()
    assert _write_category_score(c, {}, 1, 1, STAT_OUTS, 100.0, None, "t1") is True
    assert _latest(c, STAT_OUTS) == 100.0   # restored component, no prior value


def test_rate_stat_may_decrease():
    c = _conn()
    assert sim.STAT_ERA in _RATE_STAT_IDS
    lg = {(1, 1, sim.STAT_ERA): 6.0}
    assert _write_category_score(c, lg, 1, 1, sim.STAT_ERA, 3.5, None, "t1") is True
    assert _latest(c, sim.STAT_ERA) == 3.5  # rates can move either way
