"""Unit tests for the invariant/anomaly checks in app/validate.py.

These assert the *output-level* properties that the unit tests for individual
functions missed — most notably the "rate components vanished → ERA projects
absurdly low" bug, encoded here as a permanent regression guard.
"""

import json
import sqlite3

from app import sim
from app import validate as v


def _view(**kw):
    base = dict(matchup_id=1, home_wp=0.5, away_wp=0.5, prev_home_wp=0.5,
                cat_avg={}, budgets=[], home_state={}, away_state={}, period_days=7)
    base.update(kw)
    return base


def _codes(findings):
    return {f.code for f in findings}


# ── the bug that escaped: rate components dropped ──

def test_rate_components_missing_flagged():
    # week underway (K banked) but no ER/OUTS in state → error, both sides
    view = _view(home_state={48: 20}, away_state={48: 18})
    f = v.check_rate_components(view)
    assert all(x.code == "INV_RATE_COMPONENTS_MISSING" and x.severity == "error" for x in f)
    assert len(f) == 2


def test_rate_components_present_ok():
    view = _view(home_state={48: 20, sim.STAT_ER: 5, sim.STAT_OUTS: 60},
                 away_state={48: 18, sim.STAT_ER: 4, sim.STAT_OUTS: 55})
    assert v.check_rate_components(view) == []


def test_rate_components_not_started_skipped():
    assert v.check_rate_components(_view()) == []   # nothing banked yet


# ── the idle-fetch drop: scored cats vanish from a partial-write read ──

# A complete underway state: all 10 scored cats + the raw rate components.
_FULL_STATE = {1: 30, 5: 5, 20: 18, 23: 4, 48: 30, 63: 2, 83: 3,
               18: 0.75, 47: 4.2, 41: 1.25, sim.STAT_ER: 14, sim.STAT_OUTS: 90}


def test_current_cats_missing_flagged():
    # OUTS banked (week underway) but the scored display cats were dropped —
    # only the raw rate components survive (the idle-fetch / single-MAX read bug).
    components_only = {sim.STAT_ER: 14, sim.STAT_OUTS: 90}
    view = _view(home_state=dict(components_only), away_state=dict(components_only))
    f = v.check_current_cats_present(view)
    assert {x.code for x in f} == {"INV_CURRENT_CATS_MISSING"}
    assert all(x.severity == "error" for x in f)
    assert len(f) == 2  # both sides

def test_current_cats_present_ok():
    view = _view(home_state=dict(_FULL_STATE), away_state=dict(_FULL_STATE))
    assert v.check_current_cats_present(view) == []

def test_current_cats_skipped_before_pitching():
    # no OUTS yet → not underway on the pitching side → nothing to expect
    assert v.check_current_cats_present(_view()) == []
    assert v.check_current_cats_present(_view(home_state={1: 5}, away_state={1: 3})) == []


def test_rate_divergence_catches_low_era():
    # the literal 8.37→3.76 smell, with a real sample banked
    view = _view(cat_avg={sim.STAT_ERA: (3.76, 4.0)},
                 home_state={sim.STAT_ERA: 8.37, sim.STAT_OUTS: 100})
    f = v.check_rate_divergence(view)
    assert any(x.code == "ANOM_RATE_DIVERGENCE" and "ERA" in x.detail for x in f)


def test_rate_divergence_quiet_on_small_sample():
    # only 6 IP banked → too early to flag a divergence
    view = _view(cat_avg={sim.STAT_ERA: (3.76, 4.0)},
                 home_state={sim.STAT_ERA: 8.37, sim.STAT_OUTS: 18})
    assert v.check_rate_divergence(view) == []


def test_rate_range_skips_current_off_tiny_sample():
    # Monday rollover: WHIP=inf at 0 IP, or an extreme rate off <3 IP, must NOT
    # flag the *current* rate (this is the early-week spam guard).
    view = _view(home_state={sim.STAT_WHIP: float("inf"), sim.STAT_OUTS: 0},
                 away_state={sim.STAT_WHIP: 9.0, sim.STAT_OUTS: 5})
    assert v.check_rate_ranges(view) == []


def test_rate_range_flags_current_once_innings_banked():
    # With real innings banked (10 IP), a WHIP of 9.0 is genuine garbage → flag.
    view = _view(home_state={sim.STAT_WHIP: 9.0, sim.STAT_OUTS: 30})
    assert any(x.code == "INV_RATE_RANGE" for x in v.check_rate_ranges(view))


def test_rate_range_projected_always_checked():
    # Projections are full-week, so an out-of-range projected rate flags regardless
    # of banked innings.
    view = _view(cat_avg={sim.STAT_WHIP: (9.0, 1.2)}, home_state={}, away_state={})
    assert any(x.code == "INV_RATE_RANGE" for x in v.check_rate_ranges(view))


# ── other invariants/anomalies ──

def test_proj_below_current_flagged():
    view = _view(cat_avg={48: (18.0, 30.0)}, home_state={48: 25})  # proj K 18 < current 25
    f = v.check_proj_vs_current(view)
    assert any(x.code == "INV_PROJ_LT_CURRENT" for x in f)


def test_wp_range_and_sum():
    assert any(x.code == "INV_WP_RANGE" for x in v.check_wp_range(_view(home_wp=1.3)))
    assert any(x.code == "INV_WP_SUM" for x in v.check_wp_range(_view(home_wp=0.8, away_wp=0.8)))


def test_sp_units_cap():
    # 2.6 starts in a normal 7-day week is impossible → flag
    view = _view(budgets=[{"name": "X", "role": "SP", "units": 2.6}])
    assert any(x.code == "INV_SP_UNITS_CAP" for x in v.check_units(view))


def test_sp_units_cap_scales_with_period_length():
    # 2.31 starts is fine in a 14-day (All-Star) period — must NOT flag
    view = _view(period_days=14, budgets=[{"name": "X", "role": "SP", "units": 2.31}])
    assert v.check_units(view) == []


def test_wp_swing_warn():
    assert any(x.code == "ANOM_WP_SWING" for x in v.check_wp_swing(_view(prev_home_wp=0.50, home_wp=0.70)))
    assert v.check_wp_swing(_view(prev_home_wp=0.50, home_wp=0.55)) == []


# ── flapping: WP oscillating back-and-forth (flaky source), not a one-way swing ──

def test_flapping_flagged():
    view = _view(wp_history=[0.30, 0.50, 0.30, 0.50])  # up, down, up = 2 reversals
    assert any(x.code == "ANOM_WP_FLAPPING" for x in v.check_wp_flapping(view))

def test_flapping_quiet_on_single_swing():
    view = _view(wp_history=[0.30, 0.30, 0.70, 0.72])  # one big move, then steady
    assert v.check_wp_flapping(view) == []

def test_flapping_quiet_on_swing_then_recover():
    # the incident shape: jump up, hold, drop back = 1 reversal, below threshold
    view = _view(wp_history=[0.04, 0.44, 0.44, 0.44, 0.04])
    assert v.check_wp_flapping(view) == []

def test_flapping_quiet_on_mc_noise():
    view = _view(wp_history=[0.500, 0.503, 0.498, 0.501, 0.499])  # jitter < 8pp
    assert v.check_wp_flapping(view) == []

def test_flapping_skipped_for_decided_week():
    view = _view(wp_history=[0.30, 0.50, 0.30, 0.50], winner="HOME")
    assert v.check_wp_flapping(view) == []


# ── rail flip: WP touching BOTH near-0 and near-100 (worst UX; phantom-stat smell) ──

def test_rail_flip_flagged():
    view = _view(wp_history=[0.97, 0.98, 0.04])   # near-100 then crashes to near-0
    assert any(x.code == "ANOM_WP_RAIL_FLIP" for x in v.check_wp_rail_flip(view))

def test_rail_flip_quiet_on_one_sided_resolution():
    view = _view(wp_history=[0.55, 0.72, 0.96])   # decisive, but never near the 0 rail
    assert v.check_wp_rail_flip(view) == []

def test_rail_flip_quiet_on_mc_noise():
    view = _view(wp_history=[0.50, 0.52, 0.48, 0.51])
    assert v.check_wp_rail_flip(view) == []

def test_rail_flip_skipped_for_decided_week():
    view = _view(wp_history=[0.98, 0.02], winner="HOME")
    assert v.check_wp_rail_flip(view) == []


def test_clean_view_no_findings():
    view = _view(home_state=dict(_FULL_STATE), away_state=dict(_FULL_STATE),
                 cat_avg={48: (31.0, 31.0), sim.STAT_ERA: (4.5, 4.6)})
    assert v.check_view(view) == []


# ── banked totals can't shrink ──

def test_banked_regressed_flagged():
    # H banked halved (64 → 33) — the exact incident shape
    view = _view(home_state={1: 33}, home_state_prev={1: 64})
    assert any(x.code == "INV_BANKED_REGRESSED" for x in v.check_banked_not_regressed(view))

def test_banked_regressed_ignores_small_correction():
    # a routine ESPN -1 stat correction must NOT cry wolf
    view = _view(home_state={1: 63}, home_state_prev={1: 64})
    assert v.check_banked_not_regressed(view) == []

def test_banked_regressed_quiet_when_growing():
    view = _view(home_state={1: 70}, home_state_prev={1: 64})
    assert v.check_banked_not_regressed(view) == []


# ── rate sanity bounds ──

def test_rate_range_flagged():
    # ERA 412 with real innings banked = a derivation blowup → flag. (At 0 IP it's
    # intentionally skipped now as early-week noise; see the tiny-sample tests above.)
    view = _view(home_state={47: 412.0, sim.STAT_OUTS: 60})
    assert any(x.code == "INV_RATE_RANGE" for x in v.check_rate_ranges(view))

def test_rate_range_ok():
    view = _view(home_state={47: 3.9, 41: 1.2, 18: 0.78})
    assert v.check_rate_ranges(view) == []


# ── sim accounting ──

def test_cat_sim_count_mismatch():
    view = _view(n_sims=10000, tally=(4000, 5000, 1000),
                 cat_counts=[{"stat_id": 1, "home_wins": 4000, "away_wins": 3000, "ties": 2000}])
    assert any(x.code == "INV_CAT_SIM_COUNT" for x in v.check_category_sim_counts(view))

def test_cat_sim_count_ok():
    view = _view(n_sims=10000, tally=(4000, 5000, 1000),
                 cat_counts=[{"stat_id": 1, "home_wins": 4000, "away_wins": 4000, "ties": 2000}])
    assert v.check_category_sim_counts(view) == []

def test_cat_sim_count_skipped_without_n():
    assert v.check_category_sim_counts(_view()) == []


# ── home_wp column vs details_json tally consistency ──

def test_wp_details_mismatch_flagged():
    # column says 0.30 but the tally (5000/10000) says 0.50 — they must agree
    view = _view(home_wp=0.30, away_wp=0.50, n_sims=10000, tally=(5000, 4000, 1000))
    f = v.check_wp_details_consistency(view)
    assert any(x.code == "INV_WP_DETAILS_MISMATCH" and "home" in x.detail for x in f)

def test_wp_details_consistency_ok():
    view = _view(home_wp=0.50, away_wp=0.40, n_sims=10000, tally=(5000, 4000, 1000))
    assert v.check_wp_details_consistency(view) == []

def test_wp_details_skips_edited_rows():
    # a hand-smoothed row diverges on purpose — edited=1 must suppress the check
    view = _view(home_wp=0.04, away_wp=0.95, n_sims=10000, tally=(4400, 5500, 100), edited=1)
    assert v.check_wp_details_consistency(view) == []

def test_wp_details_skipped_without_details():
    assert v.check_wp_details_consistency(_view(home_wp=0.5)) == []


# ── empty budgets (roster/projection fetch failed) ──

def test_empty_budgets_flagged():
    view = _view(home_budget_n=0, away_budget_n=12, home_state={1: 5})
    assert any(x.code == "INV_EMPTY_BUDGETS" for x in v.check_empty_budgets(view))

def test_empty_budgets_skipped_before_any_data():
    assert v.check_empty_budgets(_view(home_budget_n=0, away_budget_n=0)) == []

def test_empty_budgets_skipped_without_counts():
    assert v.check_empty_budgets(_view(home_state={1: 5})) == []

def test_empty_budgets_skipped_for_decided_week():
    # a completed week legitimately has no budgets — must NOT flag
    view = _view(home_budget_n=0, away_budget_n=0, home_state={1: 30}, winner="HOME")
    assert v.check_empty_budgets(view) == []

def test_empty_budgets_skipped_at_end_of_week_no_active_games():
    # roster IS fetched but all active players' games are Final (only IL/bench
    # left) → 0 to budget; UNDECIDED but decided-in-practice. Benign FP, must NOT flag.
    view = _view(home_budget_n=0, away_budget_n=12, home_state={1: 30},
                 home_roster_n=24, home_active_remaining=0,
                 away_roster_n=24, away_active_remaining=3)
    assert v.check_empty_budgets(view) == []

def test_empty_budgets_real_fetch_failure_still_flags():
    # empty budgets AND no roster fetched → real failure, must flag.
    view = _view(home_budget_n=0, away_budget_n=12, home_state={1: 5},
                 home_roster_n=0, home_active_remaining=0)
    assert any(x.code == "INV_EMPTY_BUDGETS" for x in v.check_empty_budgets(view))

def test_empty_budgets_roster_present_with_games_remaining_still_flags():
    # roster fetched and active games remain but produced no budgets → real
    # projection failure, must flag.
    view = _view(home_budget_n=0, away_budget_n=12, home_state={1: 5},
                 home_roster_n=24, home_active_remaining=4)
    assert any(x.code == "INV_EMPTY_BUDGETS" for x in v.check_empty_budgets(view))


# ── league-level: correlated swing (the systemic fingerprint) ──

def test_correlated_swing_flagged():
    views = [_view(matchup_id=i, prev_home_wp=0.40, home_wp=0.70) for i in (1, 2, 3)]
    f = v.check_correlated_swing(views)
    assert len(f) == 1 and f[0].code == "ANOM_CORRELATED_SWING" and f[0].severity == "error"
    assert f[0].matchup_id is None  # league-wide

def test_correlated_swing_quiet_below_threshold():
    views = [_view(matchup_id=1, prev_home_wp=0.40, home_wp=0.70),
             _view(matchup_id=2, prev_home_wp=0.50, home_wp=0.52)]  # only 1 swung
    assert v.check_correlated_swing(views) == []

def test_correlated_swing_ignores_decided_weeks():
    # 3 completed matchups whose final snapshot snapped to 100/0 must NOT re-fire
    views = [_view(matchup_id=i, prev_home_wp=0.70, home_wp=1.0, winner="HOME")
             for i in (1, 2, 3)]
    assert v.check_correlated_swing(views) == []


# ── pipeline freshness + published site (the output the user sees) ──

def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE category_state (matchup_id INT, team_id INT, stat_id INT, "
                 "score REAL, result TEXT, fetched_at TEXT, "
                 "PRIMARY KEY (matchup_id, team_id, stat_id, fetched_at))")
    conn.execute("CREATE TABLE wp_snapshots (matchup_id INT, computed_at TEXT, home_wp REAL, "
                 "away_wp REAL, model_version TEXT, details_json TEXT, "
                 "PRIMARY KEY (matchup_id, computed_at))")
    return conn

def _put_state(conn, mid, tid, stats, at):
    for sid, sc in stats.items():
        conn.execute("INSERT INTO category_state VALUES (?,?,?,?,?,?)", (mid, tid, sid, sc, None, at))
    conn.commit()

def test_pipeline_freshness_flags_stale():
    conn = _mem_db()
    conn.execute("INSERT INTO wp_snapshots VALUES (1,?,0.5,0.5,'mc-v1','{}')",
                 ("2026-06-04T21:00:00+00:00",))
    _put_state(conn, 1, 20, {1: 30}, "2026-06-04T21:00:00+00:00")
    f = v.check_pipeline_freshness(conn, "2026-06-04T21:30:00+00:00")  # 30 min later
    assert {x.code for x in f} == {"ANOM_STALE_SNAPSHOTS", "ANOM_STALE_FETCH"}

def test_pipeline_freshness_quiet_when_fresh():
    conn = _mem_db()
    conn.execute("INSERT INTO wp_snapshots VALUES (1,?,0.5,0.5,'mc-v1','{}')",
                 ("2026-06-04T21:28:00+00:00",))
    _put_state(conn, 1, 20, {1: 30}, "2026-06-04T21:29:00+00:00")
    assert v.check_pipeline_freshness(conn, "2026-06-04T21:30:00+00:00") == []

def test_pipeline_freshness_skipped_without_now():
    assert v.check_pipeline_freshness(_mem_db(), None) == []


_FULL_BAT = [{"stat_id": s, "score": 1.0} for s in (1, 5, 20, 23, 18)]
_FULL_PIT = [{"stat_id": s, "score": 1.0} for s in (48, 63, 47, 41, 83)]
_FULL_BLK = {"batting": _FULL_BAT, "pitching": _FULL_PIT}

def _write_site(tmp_path, weeks, generated_at="2026-06-04T21:30:00+00:00"):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"generated_at": generated_at, "weeks": weeks}))
    return str(p)

def test_site_missing_scores_flagged(tmp_path):
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [
        {"matchup_id": 1, "home": _FULL_BLK, "away": _FULL_BLK},
        {"matchup_id": 2, "home": {"batting": [], "pitching": []},
                          "away": {"batting": [], "pitching": []}},
    ]}]
    f = v.check_published_site(_write_site(tmp_path, weeks), "2026-06-04T21:31:00+00:00")
    miss = [x for x in f if x.code == "INV_SITE_MISSING_SCORES"]
    assert miss and all(x.matchup_id == 2 for x in miss)  # only the empty one flags

def test_site_ok_full(tmp_path):
    weeks = [{"matchup_period_id": 10, "state": "live",
              "matchups": [{"matchup_id": 1, "home": _FULL_BLK, "away": _FULL_BLK}]}]
    assert v.check_published_site(_write_site(tmp_path, weeks), "2026-06-04T21:31:00+00:00") == []

def test_site_upcoming_week_not_flagged(tmp_path):
    weeks = [{"matchup_period_id": 11, "state": "upcoming", "matchups": [
        {"matchup_id": 9, "home": {"batting": [], "pitching": []},
                          "away": {"batting": [], "pitching": []}}]}]
    assert v.check_published_site(_write_site(tmp_path, weeks), "2026-06-04T21:31:00+00:00") == []

def test_site_stale_generated_at(tmp_path):
    path = _write_site(tmp_path, [], generated_at="2026-06-04T20:00:00+00:00")
    f = v.check_published_site(path, "2026-06-04T21:00:00+00:00")  # 60 min old
    assert any(x.code == "ANOM_SITE_STALE" for x in f)

def test_site_missing_file(tmp_path):
    f = v.check_published_site(str(tmp_path / "nope.json"), "2026-06-04T21:00:00+00:00")
    assert any(x.code == "INV_SITE_MISSING" for x in f)

def test_site_skipped_without_path():
    assert v.check_published_site(None, "2026-06-04T21:00:00+00:00") == []

def test_site_asymmetric_record_flagged(tmp_path):
    bad = {"matchup_id": 1, "home": dict(_FULL_BLK, record={"W": 9, "L": 1, "T": 0}),
           "away": dict(_FULL_BLK, record={"W": 2, "L": 7, "T": 1})}  # not mirrored
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [bad]}]
    f = v.check_published_site(_write_site(tmp_path, weeks), "2026-06-04T21:31:00+00:00")
    assert any(x.code == "INV_SITE_RECORD_ASYMMETRIC" for x in f)

def test_site_mirrored_record_ok(tmp_path):
    good = {"matchup_id": 1, "home": dict(_FULL_BLK, record={"W": 6, "L": 3, "T": 1}),
            "away": dict(_FULL_BLK, record={"W": 3, "L": 6, "T": 1})}
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [good]}]
    f = v.check_published_site(_write_site(tmp_path, weeks), "2026-06-04T21:31:00+00:00")
    assert not any(x.code == "INV_SITE_RECORD_ASYMMETRIC" for x in f)


# ── cross-source: published scores must match the DB (as of generated_at) ──

def _live_site_block(team_id, scores):
    """A data.json team block: scores is {stat_id: value}."""
    bat = [{"stat_id": s, "score": scores[s]} for s in (1, 5, 20, 23, 18)]
    pit = [{"stat_id": s, "score": scores[s]} for s in (48, 63, 47, 41, 83)]
    return {"team_id": team_id, "batting": bat, "pitching": pit}

_ALL_TEN = {1: 30, 5: 5, 20: 18, 23: 4, 48: 30, 63: 2, 83: 3, 18: 0.75, 47: 4.2, 41: 1.25}

def test_site_db_mismatch_flagged(tmp_path):
    conn = _mem_db()
    _put_state(conn, 1, 20, _ALL_TEN, "2026-06-04T21:30:00+00:00")
    site_scores = dict(_ALL_TEN); site_scores[1] = 19   # site shows H=19, DB has 30
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [
        {"matchup_id": 1, "home": _live_site_block(20, site_scores),
         "away": _live_site_block(21, _ALL_TEN)}]}]
    # team 21 absent from DB → no rows to compare for away; only home H mismatches
    f = v.check_published_site(_write_site(tmp_path, weeks), "2026-06-04T21:31:00+00:00", conn=conn)
    mm = [x for x in f if x.code == "INV_SITE_DB_MISMATCH"]
    assert len(mm) == 1 and "H site=19 vs DB=30" in mm[0].detail

def test_site_db_agreement_ok(tmp_path):
    conn = _mem_db()
    _put_state(conn, 1, 20, _ALL_TEN, "2026-06-04T21:30:00+00:00")
    _put_state(conn, 1, 21, _ALL_TEN, "2026-06-04T21:30:00+00:00")
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [
        {"matchup_id": 1, "home": _live_site_block(20, _ALL_TEN),
         "away": _live_site_block(21, _ALL_TEN)}]}]
    f = v.check_published_site(_write_site(tmp_path, weeks), "2026-06-04T21:31:00+00:00", conn=conn)
    assert [x for x in f if x.code == "INV_SITE_DB_MISMATCH"] == []

def test_site_db_compare_ignores_later_fetch(tmp_path):
    # DB updated AFTER generated_at must not create a phantom mismatch
    conn = _mem_db()
    _put_state(conn, 1, 20, _ALL_TEN, "2026-06-04T21:30:00+00:00")               # what publish saw
    _put_state(conn, 1, 20, {**_ALL_TEN, 1: 41}, "2026-06-04T21:40:00+00:00")     # later fetch
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [
        {"matchup_id": 1, "home": _live_site_block(20, _ALL_TEN),
         "away": _live_site_block(21, _ALL_TEN)}]}]
    f = v.check_published_site(_write_site(tmp_path, weeks, generated_at="2026-06-04T21:35:00+00:00"),
                               "2026-06-04T21:41:00+00:00", conn=conn)
    assert [x for x in f if x.code == "INV_SITE_DB_MISMATCH"] == []  # compared as-of 21:35, not 21:40


def test_site_db_check_skips_reconstructed_cats_compares_counting(tmp_path):
    # Rate cats (ERA/WHIP/OPS) and QS/SVHD are derived in publish from the live
    # box-score reconstruction (so the scoreboard matches the projection), not from
    # raw category_state — so the cross-source check SKIPS them. Counting cats
    # (H/HR/R/SB/K) come straight from category_state and are still compared.
    conn = _mem_db()
    comp = {1: 30, 5: 5, 20: 18, 23: 4, 48: 30, 63: 2, 83: 3,
            sim.STAT_OUTS: 68, sim.STAT_ER: 8, sim.STAT_P_H: 17, sim.STAT_P_BB: 3,
            47: 4.2, 41: 1.25, 18: 0.75}
    _put_state(conn, 1, 20, comp, "2026-06-04T21:30:00+00:00")
    _put_state(conn, 1, 21, comp, "2026-06-04T21:30:00+00:00")
    base = {s: comp[s] for s in (1, 5, 20, 23, 48, 63, 83, 18, 47, 41)}
    # Home publishes a wildly-off ERA (47) — reconstruction-derived, must be SKIPPED —
    # AND a wrong K (48) — a counting cat, must be FLAGGED.
    blk = {**base, 47: 99.9, 48: 1}
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [
        {"matchup_id": 1, "home": _live_site_block(20, blk),
         "away": _live_site_block(21, base)}]}]
    f = v.check_published_site(_write_site(tmp_path, weeks, "2026-06-04T21:30:00+00:00"),
                              "2026-06-04T21:31:00+00:00", conn=conn)
    mm = [x for x in f if x.code == "INV_SITE_DB_MISMATCH"]
    assert any("K" in x.detail for x in mm)            # counting cat flagged
    assert all("ERA" not in x.detail for x in mm)      # reconstructed rate skipped


# ── QS/SVHD over-credit guard: site must not exceed max(scrape, floor + box) ──

def _mem_db_qs(tmp_path):
    """A DB with everything the independent QS recompute needs."""
    conn = _mem_db()  # category_state + wp_snapshots
    conn.executescript(
        """
        CREATE TABLE team_schedule (matchup_period_id INT, game_pk INT, game_date TEXT,
            pro_team_id INT, game_status TEXT);
        CREATE TABLE live_pitchers (game_pk INT, mlbam_id INT, name TEXT, pro_team_id INT,
            order_idx INT, is_last INT, games_started INT, outs INT, er INT, k INT,
            p_h INT, p_bb INT, sv INT, hld INT, fetched_at TEXT);
        CREATE TABLE live_batters (game_pk INT, mlbam_id INT, name TEXT, pro_team_id INT,
            ab INT, h INT, b2 INT, b3 INT, hr INT, bb INT, hbp INT, sf INT, fetched_at TEXT);
        CREATE TABLE daily_lineups (game_date TEXT, fantasy_team_id INT, player_id INT,
            lineup_slot_id INT, fetched_at TEXT);
        CREATE TABLE players (id INT PRIMARY KEY, full_name TEXT, pro_team_id INT,
            default_position_id INT, eligible_slots_json TEXT, injury_status TEXT);
        CREATE TABLE team_rosters (matchup_period_id INT, fantasy_team_id INT,
            player_id INT, lineup_slot_id INT, status TEXT);
        CREATE TABLE player_injuries (norm_name TEXT, return_date TEXT);
        CREATE TABLE player_projections (player_id INT, split_id INT, stat_id INT, value REAL);
        """
    )
    # deGrom: a Final QS start (6 IP, 0 ER) for fantasy team 20, slotted SP.
    conn.execute("INSERT INTO team_schedule VALUES (10, 9001, '2026-06-07', 26, 'Final')")
    conn.execute("INSERT INTO live_pitchers VALUES "
                 "(9001, 1, 'Jacob deGrom', 26, 0, 1, 1, 18, 0, 7, 4, 1, 0, 0, 't')")
    conn.execute("INSERT INTO players VALUES (101, 'Jacob deGrom', 26, 13, '[]', NULL)")
    conn.execute("INSERT INTO team_rosters VALUES (10, 20, 101, 13, NULL)")
    conn.execute("INSERT INTO daily_lineups VALUES ('2026-06-07', 20, 101, 13, 't')")
    # category_state QS for team 20: settled floor 2 in the morning, scrape banks
    # deGrom (→3) at night. SVHD steady at 3. Away team 21: QS 2 / SVHD 3 (no live).
    _put_state(conn, 1, 20, {63: 2.0, 83: 3.0}, "2026-06-07T08:00:00+00:00")
    _put_state(conn, 1, 20, {63: 3.0, 83: 3.0}, "2026-06-07T21:15:00+00:00")
    _put_state(conn, 1, 21, {63: 2.0, 83: 3.0}, "2026-06-07T21:15:00+00:00")
    return conn

_GEN_QS = "2026-06-07T22:00:00+00:00"

def _qs_block(team_id, qs, svhd):
    s = {1: 30, 5: 5, 20: 18, 23: 4, 18: 0.75, 48: 30, 47: 4.2, 41: 1.25, 63: qs, 83: svhd}
    return _live_site_block(team_id, s)

def test_site_qs_overcredit_flagged(tmp_path):
    # The deGrom shape: scrape banked the QS (weekly →3), but publish *also* added
    # the box-score credit → site shows 4. max(scrape 3, floor 2 + box 1) = 3, so 4
    # is a phantom double-count → flag.
    conn = _mem_db_qs(tmp_path)
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [
        {"matchup_id": 1, "home": _qs_block(20, qs=4, svhd=3),
         "away": _qs_block(21, qs=2, svhd=3)}]}]
    f = v.check_published_site(_write_site(tmp_path, weeks, _GEN_QS),
                               "2026-06-07T22:01:00+00:00", conn=conn)
    oc = [x for x in f if x.code == "INV_SITE_QS_OVERCREDIT"]
    assert len(oc) == 1 and oc[0].matchup_id == 1 and "QS" in oc[0].detail

def test_site_qs_no_overcredit_when_correct(tmp_path):
    # Post-fix value: site shows 3 = max(3, 2+1). No flag. (SVHD 3 also fine.)
    conn = _mem_db_qs(tmp_path)
    weeks = [{"matchup_period_id": 10, "state": "live", "matchups": [
        {"matchup_id": 1, "home": _qs_block(20, qs=3, svhd=3),
         "away": _qs_block(21, qs=2, svhd=3)}]}]
    f = v.check_published_site(_write_site(tmp_path, weeks, _GEN_QS),
                               "2026-06-07T22:01:00+00:00", conn=conn)
    assert [x for x in f if x.code == "INV_SITE_QS_OVERCREDIT"] == []


# ── read-fix regression guard: an idle partial write must NOT drop banked cats ──

def test_read_survives_idle_partial_write():
    """The 2026-06-04 evening bug: an idle fetch wrote only components at a fresh
    timestamp, and a single-MAX read dropped all scored cats. load_latest_state now
    reads per-stat, so the scored cats persist from the last full (scrape) tick."""
    conn = _mem_db()
    scored = {1: 30, 5: 5, 20: 18, 23: 4, 48: 30, 63: 2, 83: 3, 18: 0.75, 47: 4.2, 41: 1.25}
    components = {sim.STAT_ER: 14, sim.STAT_OUTS: 90, sim.STAT_P_H: 80, sim.STAT_AB: 200}
    _put_state(conn, 1, 20, {**scored, **components}, "2026-06-04T21:30:00+00:00")  # live scrape
    _put_state(conn, 1, 20, components, "2026-06-04T21:35:00+00:00")                # idle fetch
    state = sim.load_latest_state(conn, 1, 20)
    for sid in scored:
        assert sid in state, f"scored cat {sid} dropped by the read"
    assert state[1] == 30 and state[sim.STAT_OUTS] == 90  # banked values intact
    # and the guard check sees a complete state → silent
    assert v.check_current_cats_present(_view(home_state=state, away_state={})) == []

def test_load_state_prev_per_stat():
    conn = _mem_db()
    _put_state(conn, 1, 20, {1: 30, 48: 20, sim.STAT_OUTS: 60}, "2026-06-04T21:30:00+00:00")
    _put_state(conn, 1, 20, {1: 33, 48: 22, sim.STAT_OUTS: 66}, "2026-06-04T21:35:00+00:00")
    prev = v._load_state_prev(conn, 1, 20)
    assert prev == {1: 30, 48: 20, sim.STAT_OUTS: 60}


# ── fetch-time scrape health (the silent live-scrape failure) ──

def test_scrape_health_flags_empty_during_live_games():
    f = v.check_scrape_health(in_progress=3, scraped_cells=0)
    assert len(f) == 1 and f[0].code == "ANOM_SCRAPE_EMPTY" and f[0].severity == "warn"

def test_scrape_health_ok_when_scrape_produced_cells():
    assert v.check_scrape_health(in_progress=3, scraped_cells=120) == []

def test_scrape_health_quiet_when_idle():
    assert v.check_scrape_health(in_progress=0, scraped_cells=0) == []


def test_persist_upserts_and_dedups():
    conn = _mem_db()
    conn.execute("CREATE TABLE validation_flags (code TEXT, matchup_id INT, flag_date TEXT, "
                 "severity TEXT, detail TEXT, first_seen TEXT, last_seen TEXT, "
                 "occurrences INT, resolved INT, PRIMARY KEY (code, matchup_id, flag_date))")
    finding = v.Finding("ANOM_SCRAPE_EMPTY", "warn", None, "scrape empty")
    v.persist(conn, [finding], "2026-06-04T21:00:00+00:00")
    v.persist(conn, [finding], "2026-06-04T21:05:00+00:00")  # same day → bump occurrences
    row = conn.execute("SELECT matchup_id, occurrences, resolved FROM validation_flags").fetchone()
    assert row["matchup_id"] == -1 and row["occurrences"] == 2 and row["resolved"] == 0


def _flags_db():
    conn = _mem_db()
    conn.execute("CREATE TABLE validation_flags (code TEXT, matchup_id INT, flag_date TEXT, "
                 "severity TEXT, detail TEXT, first_seen TEXT, last_seen TEXT, occurrences INT, "
                 "resolved INT, resolved_at TEXT, resolved_by TEXT, resolution_note TEXT, "
                 "PRIMARY KEY (code, matchup_id, flag_date))")
    return conn

def test_resolve_stamps_provenance():
    conn = _flags_db()
    v.persist(conn, [v.Finding("ANOM_WP_SWING", "warn", 58, "swing")], "2026-06-05T07:55:00+00:00")
    n = v.resolve(conn, "ANOM_WP_SWING", now="2026-06-05T08:00:00+00:00",
                  by="mpozar", note="benign roster downgrade")
    assert n == 1
    r = conn.execute("SELECT resolved, resolved_at, resolved_by, resolution_note "
                     "FROM validation_flags").fetchone()
    assert r["resolved"] == 1 and r["resolved_by"] == "mpozar"
    assert r["resolved_at"] == "2026-06-05T08:00:00+00:00"
    assert r["resolution_note"] == "benign roster downgrade"

def test_resolve_all_codes():
    conn = _flags_db()
    v.persist(conn, [v.Finding("ANOM_WP_SWING", "warn", 1, "a"),
                     v.Finding("INV_RATE_RANGE", "error", 2, "b")], "2026-06-05T07:00:00+00:00")
    assert v.resolve(conn, "all", now="2026-06-05T08:00:00+00:00", by="x", note=None) == 2

def test_resolve_only_touches_open():
    conn = _flags_db()
    v.persist(conn, [v.Finding("ANOM_WP_SWING", "warn", 1, "a")], "2026-06-05T07:00:00+00:00")
    v.resolve(conn, "ANOM_WP_SWING", now="2026-06-05T08:00:00+00:00", by="x", note="first")
    # second resolve finds nothing open → 0, and must not overwrite the original note
    assert v.resolve(conn, "ANOM_WP_SWING", now="2026-06-05T09:00:00+00:00", by="y", note="second") == 0
    assert conn.execute("SELECT resolution_note FROM validation_flags").fetchone()[0] == "first"
