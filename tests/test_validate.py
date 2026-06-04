"""Unit tests for the invariant/anomaly checks in app/validate.py.

These assert the *output-level* properties that the unit tests for individual
functions missed — most notably the "rate components vanished → ERA projects
absurdly low" bug, encoded here as a permanent regression guard.
"""

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


def test_clean_view_no_findings():
    view = _view(home_state=dict(_FULL_STATE), away_state=dict(_FULL_STATE),
                 cat_avg={48: (31.0, 31.0), sim.STAT_ERA: (4.5, 4.6)})
    assert v.check_view(view) == []
