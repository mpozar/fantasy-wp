"""Monotonicity guard on current-period category_state writes
(`cli._write_category_score`).

Counting stats are cumulative within a scoring period, so a value below the
last-good one is a stale/partial read (laggy REST, mid-render scrape, a dropped
two-way line) and must be rejected. Rate stats (OPS/ERA/WHIP) move freely.
"""

import sqlite3

from app import sim
from app.cli import _write_category_score, _scrape_owns_display_cat, _RATE_STAT_IDS

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


# ── who owns a current-period display cat this tick (scrape vs REST) ──

def test_rate_cat_never_written_by_rest():
    # Rates are derived at publish from components, so REST always skips them.
    assert _scrape_owns_display_cat(sim.STAT_ERA, scrape_due=True) is True
    assert _scrape_owns_display_cat(sim.STAT_ERA, scrape_due=False) is True


def test_counting_cat_scrape_owned_while_live():
    # A scrape is due (games in progress) → it owns the counting display cats.
    assert _scrape_owns_display_cat(STAT_K, scrape_due=True) is True


def test_counting_cat_rest_reconciles_when_idle():
    # No scrape due → REST reconciles the final counting totals (the Ohtani
    # K 23→29 post-final fix).
    assert _scrape_owns_display_cat(STAT_K, scrape_due=False) is False


def test_counting_cat_ownership_follows_the_closing_scrape():
    """Ownership keys off `scrape_due`, not "games in progress", so the closing
    scrape (which runs with nothing In Progress) still owns the counting cats.
    Keying off in-progress here would hand REST ownership of the very cats that
    scrape is about to write, and the two would disagree."""
    # closing-scrape tick: nothing live, but a game just went Final
    assert _scrape_owns_display_cat(STAT_K, scrape_due=True) is True


# ── when a scrape is due (incl. the closing scrape) ──
# The scrape banks QS/SVHD the instant a game reads Final. The last games of a
# night finish together, so once none are In Progress the scrape used to stop and
# that final credit waited ~3h for the 07:00 settle — the window the QS/SVHD
# reconstruction existed to bridge (and got wrong ~8% of the time).

def _sched_db(rows):
    """rows: [(game_status, became_final_at)] for period 18."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE team_schedule (matchup_period_id INT, game_status TEXT, "
                 "became_final_at TEXT)")
    for st, fa in rows:
        conn.execute("INSERT INTO team_schedule VALUES (18, ?, ?)", (st, fa))
    conn.commit()
    return conn

NOW = "2026-08-09T04:20:00+00:00"

def test_scrape_due_while_games_are_live():
    from app.cli import _scrape_due
    conn = _sched_db([("In Progress", None), ("Final", "2026-08-09T01:00:00+00:00")])
    due, in_prog = _scrape_due(conn, 18, NOW)
    assert due is True and in_prog == 1

def test_closing_scrape_runs_just_after_the_last_game_finalizes():
    """THE fix: nothing In Progress, but a game went Final 15 min ago — the scrape
    must still run so ESPN banks that last credit now, not at 07:00."""
    from app.cli import _scrape_due
    conn = _sched_db([("Final", "2026-08-09T04:05:02+00:00")])   # 15 min before NOW
    due, in_prog = _scrape_due(conn, 18, NOW)
    assert due is True and in_prog == 0

def test_scrape_idle_once_the_closing_window_has_passed():
    """Outside the window the scrape stops — REST becomes the authoritative final
    source and reconciles the counting cats (the Ohtani K 23→29 path)."""
    from app.cli import _scrape_due
    from app.cli import CLOSING_SCRAPE_WINDOW_MIN
    conn = _sched_db([("Final", "2026-08-09T03:00:00+00:00")])   # 80 min before NOW
    assert CLOSING_SCRAPE_WINDOW_MIN < 80
    due, in_prog = _scrape_due(conn, 18, NOW)
    assert due is False and in_prog == 0

def test_scrape_not_due_before_the_slate_starts():
    from app.cli import _scrape_due
    conn = _sched_db([("Scheduled", None), ("Pre-Game", None)])
    assert _scrape_due(conn, 18, NOW) == (False, 0)

def test_scrape_due_falls_back_to_in_progress_on_a_bad_clock():
    """An unparseable `now` must not wedge the scrape off during live games."""
    from app.cli import _scrape_due
    conn = _sched_db([("In Progress", None)])
    assert _scrape_due(conn, 18, "not-a-timestamp") == (True, 1)
