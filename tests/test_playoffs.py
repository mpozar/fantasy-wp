"""Playoff odds: seeding tiebreak chain, bracket structure, value comparison,
and season-sim sanity. Pure-logic tests over app/playoffs.py."""
import random

import pytest

from app import playoffs
from app.playoffs import decide_values, seed_order, simulate_odds
from app.sim import CATEGORIES, TIEBREAKER_STAT_ID


def _h2h(team_ids, wins_pairs=()):
    g = {t: {u: 0 for u in team_ids} for t in team_ids}
    for w, l, n in wins_pairs:
        g[w][l] = n
    return g


# ── seed_order: record → H2H among tied (reset per seat) → coin flip ──

def test_seed_order_by_record():
    ids = [1, 2, 3]
    order = seed_order({1: 5, 2: 9, 3: 7}, _h2h(ids), random.Random(0))
    assert order == [2, 3, 1]


def test_seed_order_two_way_tie_h2h():
    ids = [1, 2, 3]
    # 1 and 2 tied; 2 swept the season series → 2 seeds ahead.
    order = seed_order({1: 8, 2: 8, 3: 2},
                       _h2h(ids, [(2, 1, 2)]), random.Random(0))
    assert order == [2, 1, 3]


def test_seed_order_three_way_tie_resets_chain():
    """ESPN seats the H2H winner, then RESTARTS the chain for the rest.

    A beat B twice; B beat C twice; C beat A twice — 2 group wins each, coin
    flip for the first seat. Seed the flip so A is seated: among the remaining
    {B, C}, B swept C, so B must seed ahead of C via the RESET H2H — an
    implementation that kept the original 3-team group wins (B=2, C=2) would
    coin-flip them instead.
    """
    ids = [1, 2, 3]
    g = _h2h(ids, [(1, 2, 2), (2, 3, 2), (3, 1, 2)])
    for seed in range(40):
        order = seed_order({1: 8, 2: 8, 3: 8}, g, random.Random(seed))
        if order[0] == 1:
            assert order == [1, 2, 3]
            break
    else:
        pytest.fail("no seed produced A first — coin flip not exercised")


def test_seed_order_coin_flip_covers_both():
    ids = [1, 2]
    got = {tuple(seed_order({1: 5, 2: 5}, _h2h(ids, [(1, 2, 1), (2, 1, 1)]),
                            random.Random(s)))
           for s in range(30)}
    assert got == {(1, 2), (2, 1)}   # tied H2H → pure coin flip, both orders occur


# ── decide_values: most cats, hits tiebreak, higher seed on dead heat ──

def _vals(overrides=None):
    """A flat value tuple with per-stat overrides, in CATEGORIES order."""
    base = {sid: 10.0 for sid, _ in CATEGORIES}
    base.update(overrides or {})
    return tuple(base[sid] for sid, _ in CATEGORIES)


def test_decide_values_most_cats_and_reversed():
    rev = next(sid for sid, r in CATEGORIES if r)          # e.g. ERA
    fwd = next(sid for sid, r in CATEGORIES if not r and sid != TIEBREAKER_STAT_ID)
    hi = _vals({rev: 5.0, fwd: 12.0})                    # wins both
    assert decide_values(hi, _vals()) is True
    assert decide_values(_vals(), hi) is False


def test_decide_values_hits_tiebreak_and_dead_heat():
    lo_hits = _vals({TIEBREAKER_STAT_ID: 9.0})
    assert decide_values(_vals(), lo_hits) is True         # cats tied, hits win
    assert decide_values(lo_hits, _vals()) is False
    assert decide_values(_vals(), _vals()) is True         # dead heat → higher seed


# ── simulate_odds: structure + degenerate cases ──

def _flat_samples(team_ids, strength=None):
    """One constant sample per round; `strength[t]` breaks every pairing."""
    strength = strength or {}
    return {t: [[_vals({TIEBREAKER_STAT_ID: 10.0 + strength.get(t, 0)})]
                for _ in range(playoffs.NUM_PLAYOFF_PERIODS)]
            for t in team_ids}


def test_simulate_odds_degenerate_dominant_team():
    """Team 1 wins every remaining matchup (wp=1) and every pairing → seed 1,
    bye, and championship with certainty; a 0-win team with no remaining
    matchups can't make the playoffs."""
    ids = list(range(1, 13))
    wins = {t: 12 - t for t in ids}                # strictly ordered records
    remaining = [{"home": 1, "away": 12, "home_wp": 1.0}]
    strength = {t: -t for t in ids}                # better record ⇒ stronger
    odds = simulate_odds(ids, wins, _h2h(ids), remaining,
                         _flat_samples(ids, strength), n_sims=200,
                         rng=random.Random(1))
    assert odds[1]["p_playoffs"] == odds[1]["p_bye"] == odds[1]["p_champion"] == 1.0
    assert odds[1]["seed_dist"][0] == 1.0
    assert odds[12]["p_playoffs"] == 0.0
    assert odds[7]["p_playoffs"] == 0.0            # seeds 7+ out in every sim


def test_simulate_odds_probabilities_consistent():
    ids = list(range(1, 13))
    wins = {t: 8 for t in ids}
    remaining = [{"home": a, "away": b, "home_wp": 0.5}
                 for a in ids for b in ids if a < b][:20]
    odds = simulate_odds(ids, wins, _h2h(ids), remaining,
                         _flat_samples(ids), n_sims=500,
                         rng=random.Random(7))
    assert abs(sum(o["p_champion"] for o in odds.values()) - 1.0) < 1e-9
    assert abs(sum(o["p_playoffs"] for o in odds.values())
               - playoffs.PLAYOFF_TEAM_COUNT) < 1e-9
    assert abs(sum(o["p_bye"] for o in odds.values()) - playoffs.BYE_SEEDS) < 1e-9
    assert abs(sum(o["p_final"] for o in odds.values()) - 2.0) < 1e-9
    for o in odds.values():                        # every seed dist sums to 1
        assert abs(sum(o["seed_dist"]) - 1.0) < 1e-9
        assert o["p_bye"] <= o["p_playoffs"] + 1e-9
        assert o["p_champion"] <= o["p_final"] + 1e-9


# ── load_odds_history: chronological slim series from the runs archive ──

def test_load_odds_history_reads_archive():
    import json as _json
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE playoff_odds_runs (computed_at TEXT PRIMARY KEY, payload_json TEXT)")
    for i, ts in enumerate(["2026-07-20T06:00:00+00:00", "2026-07-20T10:00:00+00:00"]):
        conn.execute("INSERT INTO playoff_odds_runs VALUES (?,?)", (ts, _json.dumps({
            "generated_at": ts,
            "teams": [{"team_id": 5, "p_playoffs": 0.9, "p_bye": 0.5 + i / 10,
                       "p_final": 0.4, "p_champion": 0.3}],
        })))
    conn.execute("INSERT INTO playoff_odds_runs VALUES ('2026-07-20T11:00:00+00:00', 'garbage')")
    hist = playoffs.load_odds_history(conn)
    assert [h["t"] for h in hist] == ["2026-07-20T06:00:00+00:00", "2026-07-20T10:00:00+00:00"]
    assert hist[0]["teams"]["5"] == [0.9, 0.5, 0.3]      # [playoffs, bye, champion]
    assert hist[1]["teams"]["5"][1] == 0.6               # garbage row skipped, order kept


# ── live-finale refresh gate (cli._finale_skip_reason) ──
# Playoff odds ride medium.sh's 4-hourly cadence all week, but the LAST day of a
# matchup period resolves six matchups in a few hours, so the fast tier offers a
# refresh every tick and the command self-throttles to ~30 min.

def _finale_db(period_end="2026-08-09", statuses=(("2026-08-09", "In Progress"),),
               last_run=None):
    import sqlite3, json as _json
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE team_schedule (matchup_period_id INT, game_date TEXT, "
                 "game_status TEXT)")
    conn.execute("CREATE TABLE playoff_odds_runs (computed_at TEXT PRIMARY KEY, "
                 "payload_json TEXT)")
    for gd, st in statuses:
        conn.execute("INSERT INTO team_schedule VALUES (18, ?, ?)", (gd, st))
    if last_run:
        conn.execute("INSERT INTO playoff_odds_runs VALUES (?, ?)",
                     (last_run, _json.dumps({"teams": []})))
    conn.commit()
    return conn

def _reason(conn, now, monkeypatch, period_end="2026-08-09"):
    import datetime as _dt
    from app import cli, mlb
    monkeypatch.setattr(mlb, "matchup_period_window",
                        lambda p: (_dt.date(2026, 8, 3), _dt.date.fromisoformat(period_end)))
    return cli._finale_skip_reason(conn, 18, now)

def test_finale_refresh_due_when_last_day_game_is_live(monkeypatch):
    conn = _finale_db()
    assert _reason(conn, "2026-08-09T23:30:00+00:00", monkeypatch) is None

def test_finale_refresh_skipped_when_nothing_live_on_the_last_day(monkeypatch):
    """A live game on an EARLIER day of the period is the ordinary mid-week case."""
    conn = _finale_db(statuses=(("2026-08-06", "In Progress"), ("2026-08-09", "Scheduled")))
    r = _reason(conn, "2026-08-06T23:30:00+00:00", monkeypatch)
    assert r and "last day" in r

def test_finale_refresh_survives_the_utc_rollover(monkeypatch):
    """THE case a wall-clock 'is today the last day' test would get wrong: Sunday's
    West-Coast games are still in progress at 02:00 UTC Monday — still the finale."""
    conn = _finale_db()
    assert _reason(conn, "2026-08-10T02:00:00+00:00", monkeypatch) is None

def test_finale_refresh_throttled_within_the_interval(monkeypatch):
    from app.cli import PLAYOFF_LIVE_INTERVAL_MIN
    conn = _finale_db(last_run="2026-08-09T23:15:00+00:00")
    r = _reason(conn, "2026-08-09T23:30:00+00:00", monkeypatch)   # 15 min later
    assert r and str(PLAYOFF_LIVE_INTERVAL_MIN) in r

def test_finale_refresh_due_once_the_interval_elapses(monkeypatch):
    conn = _finale_db(last_run="2026-08-09T23:00:00+00:00")
    assert _reason(conn, "2026-08-09T23:31:00+00:00", monkeypatch) is None   # 31 min

def test_finale_refresh_not_blocked_by_an_unparseable_stamp(monkeypatch):
    """A bad archive stamp must not wedge the refresh off permanently."""
    conn = _finale_db(last_run="not-a-timestamp")
    assert _reason(conn, "2026-08-09T23:30:00+00:00", monkeypatch) is None
