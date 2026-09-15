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


# ── load_remaining must never see playoff matchups (added 2026-09-07) ───────

def _remaining_conn():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE matchups (id INTEGER PRIMARY KEY, matchup_period_id INTEGER,
                 home_team_id INTEGER, away_team_id INTEGER, winner TEXT)""")
    c.execute("""CREATE TABLE wp_snapshots (matchup_id INTEGER, home_wp REAL,
                 computed_at TEXT)""")
    return c


def test_load_remaining_excludes_playoff_rounds():
    """Bracket games are also UNDECIDED. Before playoff matchups were stored the
    unfiltered query was accidentally right; once they are, counting them as
    remaining REGULAR-season games would inflate win totals and corrupt seeding.
    """
    from app import playoffs
    c = _remaining_conn()
    c.execute("INSERT INTO matchups VALUES (1, 22, 10, 11, 'UNDECIDED')")   # regular
    c.execute("INSERT INTO matchups VALUES (2, 23, 10, 12, 'UNDECIDED')")   # R1
    c.execute("INSERT INTO matchups VALUES (3, 24, 10, 13, 'UNDECIDED')")   # semi
    got = playoffs.load_remaining(c, last_regular_period=22)
    assert [m["matchup_id"] for m in got] == [1]


def test_load_remaining_requires_the_bound_explicitly():
    """Keyword-only and mandatory, so a caller cannot silently reintroduce the
    leak by forgetting it."""
    import pytest
    from app import playoffs
    with pytest.raises(TypeError):
        playoffs.load_remaining(_remaining_conn())


# ── the bracket must use REAL playoff matchups once seeded (added 2026-09-07) ──

def test_load_playoff_rounds_keys_by_round_and_skips_regular_and_one_sided():
    from app import playoffs
    c = _remaining_conn()
    c.execute("INSERT INTO matchups VALUES (1, 22, 10, 11, 'UNDECIDED')")     # regular
    c.execute("INSERT INTO matchups VALUES (2, 23, 10, 12, 'UNDECIDED')")     # R1
    c.execute("INSERT INTO matchups VALUES (3, 24, 10, 13, 'HOME')")          # semi, decided
    c.execute("INSERT INTO matchups VALUES (4, 23, 14, NULL, 'UNDECIDED')")   # bye placeholder
    c.execute("INSERT INTO wp_snapshots VALUES (2, 0.75, '2026-09-07T00:00')")
    got = playoffs.load_playoff_rounds(c, last_regular_period=22)
    assert set(got) == {0, 1}                      # round index, not period
    assert got[0][frozenset((10, 12))]["home_wp"] == 0.75
    assert got[1][frozenset((10, 13))]["winner"] == "HOME"
    assert all(frozenset((14,)) not in d for d in got.values())   # one-sided dropped


def test_bracket_follows_a_live_round_wp_instead_of_resampling():
    """The point of the change: during round 1 the odds must move with the real
    matchup. Team 12 is the weakest sample by far, so sampling would eliminate
    it immediately; a live WP of 1.0 must carry it through anyway."""
    ids = list(range(1, 13))
    wins = {t: 12 - t for t in ids}
    strength = {t: -t for t in ids}
    six = [1, 2, 3, 4, 5, 6]        # seeds by record; R1 is 3v6 and 4v5
    base = simulate_odds(ids, wins, _h2h(ids), [], _flat_samples(ids, strength),
                         n_sims=200, rng=random.Random(1))
    assert base[6]["p_final"] == 0.0        # weakest seed never survives sampling
    # Now say the real games are locks for the 6 seed: R1 (3v6) and then the
    # semi it feeds (2 vs the 3v6 winner). Both rounds must honour the override,
    # so the team sampling would have eliminated immediately reaches the final.
    ov = {0: {frozenset((3, 6)): {"home": 6, "away": 3,
                                  "winner": "UNDECIDED", "home_wp": 1.0}},
          1: {frozenset((2, 6)): {"home": 6, "away": 2,
                                  "winner": "UNDECIDED", "home_wp": 1.0}}}
    live = simulate_odds(ids, wins, _h2h(ids), [], _flat_samples(ids, strength),
                         n_sims=200, rng=random.Random(1), round_overrides=ov)
    assert live[6]["p_final"] == 1.0        # carried by the real WPs, not samples
    assert live[2]["p_final"] == 0.0        # the 2 seed loses the semi it lost
    assert six == [1, 2, 3, 4, 5, 6]        # seed order sanity


def test_a_decided_round_is_a_fact_not_a_coin_flip():
    ids = list(range(1, 13))
    wins = {t: 12 - t for t in ids}
    ov = {0: {frozenset((3, 6)): {"home": 3, "away": 6,
                                  "winner": "AWAY", "home_wp": 0.99}}}
    out = simulate_odds(ids, wins, _h2h(ids), [], _flat_samples(ids, {t: -t for t in ids}),
                        n_sims=100, rng=random.Random(1), round_overrides=ov)
    assert out[3]["p_final"] == 0.0         # lost, despite a 0.99 home_wp


# ── the bracket must use REAL pairings, not simulated seeding (2026-09-14) ──
# `play()` looks its override up by team-pair, so when a per-sim seeding
# coin-flip produced a pairing that never happened the lookup missed and the
# round was SAMPLED as a fictional game. On 2026-09-14 that had the already
# ELIMINATED Seattle Melonheads at 34.3% to reach the final and 19.5% to win it.

def test_load_records_excludes_playoff_results_from_seeding():
    """Seeding is the regular-season record. Counting a bracket result moved Jo
    Mamas to 15-8 and manufactured a tie with Melonheads that does not exist."""
    from app import playoffs
    c = _remaining_conn()
    c.execute("INSERT INTO matchups VALUES (1, 22, 10, 11, 'HOME')")   # regular
    c.execute("INSERT INTO matchups VALUES (2, 23, 10, 12, 'HOME')")   # playoff
    w, l, h = playoffs.load_records(c, [10, 11, 12], last_regular_period=22)
    assert w[10] == 1 and l[11] == 1        # regular season counted
    assert l[12] == 0 and w[10] == 1        # playoff result NOT counted


def test_real_bracket_picks_the_semis_and_drops_the_consolation():
    """ESPN also creates a game between the two round-1 LOSERS. It is not part
    of the championship bracket and must not feed the final."""
    from app import playoffs
    ro = {0: {frozenset((1, 11)): {"home": 11, "away": 1, "winner": "HOME", "home_wp": 1.0},
              frozenset((3, 20)): {"home": 20, "away": 3, "winner": "AWAY", "home_wp": 0.0}},
          1: {frozenset((11, 21)): {"home": 21, "away": 11, "winner": "UNDECIDED", "home_wp": 0.44},
              frozenset((3, 5)):   {"home": 5,  "away": 3,  "winner": "UNDECIDED", "home_wp": 0.43},
              frozenset((1, 20)):  {"home": 20, "away": 1,  "winner": "UNDECIDED", "home_wp": 0.67}}}
    r0, semis = playoffs.real_bracket(ro)
    assert len(r0) == 2
    pairs = {frozenset((s["home"], s["away"])) for s in semis}
    assert pairs == {frozenset((11, 21)), frozenset((3, 5))}
    assert frozenset((1, 20)) not in pairs      # the consolation, excluded


def test_real_bracket_returns_none_for_an_unseeded_round():
    from app import playoffs
    assert playoffs.real_bracket({})[1] is None
    assert playoffs.real_bracket(None)[1] is None


def test_eliminated_team_cannot_reach_the_final():
    """End-to-end guard on the actual symptom, with a seeding TIE present so the
    coin-flip would otherwise mis-pair (that is what exposed the bug)."""
    from app import playoffs
    ids = list(range(1, 13))
    wins = {t: 12 - t for t in ids}
    wins[4] = wins[3]                      # force a 3/4 seeding tie
    ro = {0: {frozenset((3, 6)): {"home": 3, "away": 6, "winner": "AWAY", "home_wp": 0.0},
              frozenset((4, 5)): {"home": 4, "away": 5, "winner": "HOME", "home_wp": 1.0}},
          1: {frozenset((1, 4)): {"home": 1, "away": 4, "winner": "UNDECIDED", "home_wp": 0.5},
              frozenset((2, 6)): {"home": 2, "away": 6, "winner": "UNDECIDED", "home_wp": 0.5},
              frozenset((3, 5)): {"home": 3, "away": 5, "winner": "UNDECIDED", "home_wp": 0.5}}}
    out = simulate_odds(ids, wins, _h2h(ids), [],
                        _flat_samples(ids, {t: -t for t in ids}),
                        n_sims=400, rng=random.Random(3), round_overrides=ro)
    assert out[3]["p_final"] == 0.0 and out[3]["p_champion"] == 0.0   # lost R1
    assert out[5]["p_final"] == 0.0 and out[5]["p_champion"] == 0.0   # lost R1
    assert out[4]["p_final"] + out[1]["p_final"] == 1.0               # one semi
    assert out[6]["p_final"] + out[2]["p_final"] == 1.0               # the other


# ── playoff rounds refresh on ANY live game, not just the last day (2026-09-15) ──

def _playoff_finale_db(statuses, last_reg=22, period=24):
    """Like _finale_db but with the metadata the playoff-round path needs."""
    import sqlite3, json as _json
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE team_schedule (matchup_period_id INT, game_date TEXT, "
                 "game_status TEXT)")
    conn.execute("CREATE TABLE playoff_odds_runs (computed_at TEXT PRIMARY KEY, "
                 "payload_json TEXT)")
    conn.execute("CREATE TABLE matchups (id INT, matchup_period_id INT)")
    conn.execute("CREATE TABLE scoring_settings (league_id INT, season_id INT, "
                 "last_regular_season_period INT)")
    from app.cli import LEAGUE_ID, SEASON_ID
    conn.execute("INSERT INTO scoring_settings VALUES (?,?,?)",
                 (LEAGUE_ID, SEASON_ID, last_reg))
    for gd, st in statuses:
        conn.execute("INSERT INTO team_schedule VALUES (?,?,?)", (period, gd, st))
    conn.commit()
    return conn


def _playoff_reason(conn, now, monkeypatch, period=24):
    import datetime as _dt
    from app import cli, mlb
    monkeypatch.setattr(mlb, "matchup_period_window",
                        lambda p: (_dt.date(2026, 9, 14), _dt.date(2026, 9, 20)))
    return cli._finale_skip_reason(conn, period, now)


def test_playoff_round_refreshes_on_a_midweek_game(monkeypatch):
    """A semifinal on TUESDAY must trigger the refresh. The bracket now consumes
    that round's live WP, so every game moves the championship odds — the
    last-day rule left the semis on the 4-hourly cadence while the Norsemen went
    from 41.6% to 63.2% with the published odds not following."""
    conn = _playoff_finale_db([("2026-09-15", "In Progress")])
    assert _playoff_reason(conn, "2026-09-15T20:00:00+00:00", monkeypatch) is None


def test_playoff_round_skips_when_nothing_is_live(monkeypatch):
    conn = _playoff_finale_db([("2026-09-15", "Final"), ("2026-09-16", "Scheduled")])
    r = _playoff_reason(conn, "2026-09-15T20:00:00+00:00", monkeypatch)
    assert r == "no in-progress games in this playoff round"


def test_regular_season_still_uses_the_last_day_rule(monkeypatch):
    """Unchanged where the old reasoning holds: mid-week regular-season games do
    not move seeds, so they must not trigger a refresh every tick."""
    conn = _playoff_finale_db([("2026-09-15", "In Progress")], last_reg=24, period=24)
    r = _playoff_reason(conn, "2026-09-15T20:00:00+00:00", monkeypatch)
    assert r is not None and "last day" in r
