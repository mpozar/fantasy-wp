"""SVHD rate blends ESPN's full-season projected rate toward actuals.

ESPN's ROS encoding of stat 83 is broken (it returns total GP for some
players), so the ROS value has always been rebuilt from a per-appearance rate.
What changed on 2026-08-10 is HOW: a hard 15-appearance cliff
(`MIN_ACT_GP_FOR_SVHD_RATE` — 100% ESPN's projection below it, 100% actuals at
it) became the same empirical-Bayes shrinkage the QS path uses. Measured
against this season's rostered relievers the shrinkage beats that cliff at
every sample size (-32% squared error over n=1..60, -36% over n=1..25), so
these tests pin the sample-size behavior and, above all, the CONTINUITY the
cliff did not have.
"""
import pytest

from app import espn
from app.espn import (SVHD_RATE_PRIOR_APPEARANCES, apply_svhd_rate_blend,
                      blend_svhd_rate)

K = SVHD_RATE_PRIOR_APPEARANCES


def test_blend_is_the_shrinkage_formula():
    # prior = 30/60 = .500; actual = 12/40 = .300
    # → (12 + K·.5) / (40 + K)
    got = blend_svhd_rate(proj_gp=60, proj_svhd=30, act_gp=40, act_svhd=12)
    assert got == pytest.approx((12 + K * 0.5) / (40 + K))


def test_pulls_an_inflated_projection_down_toward_actuals():
    # The real shape: ESPN says .500/appearance, the reliever has run .300.
    prior, actual = 0.500, 0.300
    got = blend_svhd_rate(proj_gp=60, proj_svhd=60 * prior,
                          act_gp=45, act_svhd=45 * actual)
    assert actual < got < prior          # strictly between
    assert got < (prior + actual) / 2    # and past the midpoint, toward truth


def test_large_sample_lands_close_to_actuals():
    got = blend_svhd_rate(proj_gp=60, proj_svhd=30, act_gp=400, act_svhd=120)
    assert got == pytest.approx(0.30, abs=0.02)


def test_tiny_sample_stays_near_espns_prior():
    # A 1-appearance callup: almost no information, so don't invent any. One
    # save-less outing may nudge the .500 prior, never overturn it toward the
    # raw 0/1 the actuals alone would say.
    got = blend_svhd_rate(proj_gp=60, proj_svhd=30, act_gp=1, act_svhd=0)
    assert got / 0.5 > 0.8


def test_weight_on_actuals_rises_monotonically_with_sample():
    prior, actual = 0.50, 0.10
    prev = prior
    for n in (1, 3, 6, 12, 25, 60, 200):
        got = blend_svhd_rate(proj_gp=60, proj_svhd=60 * prior,
                              act_gp=n, act_svhd=n * actual)
        assert got < prev            # each larger sample moves further down
        prev = got
    assert prev == pytest.approx(actual, abs=0.03)


# ── the cliff regression: the whole point of the change ──────────────────


def test_no_discontinuity_at_the_old_fifteen_appearance_cliff():
    """The defect being fixed. Under the cliff, 14 actual appearances gave
    ESPN's .500 and 15 gave the reliever's own .200 — a 0.3 jump from one
    outing. The blend must move smoothly through that boundary."""
    prior, actual = 0.500, 0.200
    rates = [blend_svhd_rate(proj_gp=60, proj_svhd=60 * prior,
                             act_gp=n, act_svhd=round(n * actual))
             for n in range(13, 18)]
    steps = [abs(b - a) for a, b in zip(rates, rates[1:])]
    assert max(steps) < 0.05
    assert all(r < prior for r in rates)          # already moving toward truth
    assert all(r > actual for r in rates)         # but not overcommitted


def test_a_credible_sub_cliff_sample_already_moves_the_rate():
    """A 12-appearance closer (Edwin Díaz's shape on 2026-08-10) used to be
    scored at 100% ESPN's projection. His own outings must now count."""
    prior = 0.613
    got = blend_svhd_rate(proj_gp=62, proj_svhd=62 * prior, act_gp=12, act_svhd=5)
    assert got == pytest.approx((5 + K * prior) / (12 + K))
    assert 5 / 12 < got < prior


# ── degrading one source at a time ───────────────────────────────────────


def test_no_actuals_falls_back_to_the_prior_alone():
    # The old sub-cliff behavior, preserved for a player with no appearances.
    assert blend_svhd_rate(60, 30, None, None) == pytest.approx(0.5)
    assert blend_svhd_rate(60, 30, 0, 0) == pytest.approx(0.5)


def test_a_missing_actual_svhd_alongside_appearances_means_zero():
    """ESPN omits stat 83 from the actuals block rather than sending 0 (83 of
    133 rostered pitchers with appearances, 2026-08-10). Reading that as
    "unknown" parks every save-less arm on ESPN's prior: Kyle Leahy, 0-for-22,
    jumped from a projected 0.00 to 3.91 ROS SVHD before this was caught."""
    absent = blend_svhd_rate(proj_gp=46, proj_svhd=20, act_gp=22, act_svhd=None)
    explicit = blend_svhd_rate(proj_gp=46, proj_svhd=20, act_gp=22, act_svhd=0)
    assert absent == explicit
    assert absent == pytest.approx((K * (20 / 46)) / (22 + K))
    assert absent < 0.5 * (20 / 46)     # well below the untouched prior


def test_no_prior_falls_back_to_the_players_own_rate():
    assert blend_svhd_rate(None, None, 40, 12) == pytest.approx(0.3)
    assert blend_svhd_rate(0, 0, 40, 12) == pytest.approx(0.3)


def test_declines_when_it_has_neither_source():
    assert blend_svhd_rate(None, None, None, None) is None
    assert blend_svhd_rate(0, 0, 0, 0) is None


def test_rate_is_clamped_to_one_per_appearance():
    # A save and a hold can't both be earned in one outing.
    assert 0.0 <= blend_svhd_rate(2, 99, 30, 30) <= 1.0
    assert 0.0 <= blend_svhd_rate(60, 30, 5, 40) <= 1.0


# ── the fetch-time write-back ────────────────────────────────────────────


def test_apply_writes_back_a_total_not_a_rate():
    # Stored shape must stay "ROS total" so sim recovers the rate as svhd/gp.
    ros = {"32": 20.0, "83": 999.0}          # 999 = the broken ROS encoding
    apply_svhd_rate_blend(ros, {"32": 40.0, "83": 12.0}, {"32": 60.0, "83": 30.0})
    rate = (12 + K * 0.5) / (40 + K)
    assert ros["83"] == pytest.approx(rate * 20.0)
    assert ros["83"] / ros["32"] == pytest.approx(rate)   # what sim divides


def test_apply_leaves_espns_value_alone_with_nothing_to_go_on():
    ros = {"32": 20.0, "83": 999.0}
    apply_svhd_rate_blend(ros, None, None)
    assert ros["83"] == 999.0


def test_apply_is_a_noop_without_projected_appearances():
    ros = {"32": 0.0, "83": 5.0}
    apply_svhd_rate_blend(ros, {"32": 40.0, "83": 12.0}, {"32": 60.0, "83": 30.0})
    assert ros["83"] == 5.0


def test_apply_tolerates_string_values_from_the_api():
    ros = {"32": "20", "83": "999"}
    apply_svhd_rate_blend(ros, {"32": "40", "83": "12"}, {"32": "60", "83": "30"})
    assert ros["83"] == pytest.approx(((12 + K * 0.5) / (40 + K)) * 20.0)


def test_apply_leaves_a_starters_projection_at_zero():
    # A starter has actual appearances but no saves/holds, and ESPN projects
    # none either — the blend must not manufacture any.
    ros = {"32": 8.0, "83": 8.0}             # broken: ROS 83 == ROS GP
    apply_svhd_rate_blend(ros, {"32": 22.0, "83": 0.0}, {"32": 31.0, "83": 0.0})
    assert ros["83"] == pytest.approx(0.0)


def test_blended_rate_stays_under_the_sims_cap_for_real_inputs():
    """`sim.MAX_SVHD_RATE` (0.80) is a backstop against the broken ROS
    encoding, not a routine clamp: no plausible blend should reach it."""
    from app.sim import MAX_SVHD_RATE
    # The league's most extreme real reliever shape this season.
    got = blend_svhd_rate(proj_gp=65, proj_svhd=37, act_gp=41, act_svhd=27)
    assert got < MAX_SVHD_RATE


def test_prior_weight_constant_is_sane():
    # Guards a fat-fingered re-measure: K outside this range would either
    # ignore actuals entirely or trust a 2-appearance sample.
    assert 5.0 <= SVHD_RATE_PRIOR_APPEARANCES <= 30.0


def test_the_old_cliff_constant_is_gone():
    # Its removal is the change; a reintroduced threshold would silently
    # restore the discontinuity the blend exists to remove.
    assert not hasattr(espn, "MIN_ACT_GP_FOR_SVHD_RATE")
