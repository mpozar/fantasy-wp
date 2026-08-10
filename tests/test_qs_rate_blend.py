"""QS rate blends ESPN's ROS projection toward season-to-date actuals.

ESPN's ROS QS rate is anchored to preseason talent and runs ~+37% above
realized rates (measured 2026-08-10: ESPN-implied .598 vs actual .438 across
the league's rostered starters), which is the level bias behind the +40.5%
start-of-week QS over-projection `scripts/calibration.py` found. The fix is an
empirical-Bayes shrinkage toward each pitcher's own starts, so these tests pin
the sample-size behavior — the property a hard threshold (the SVHD path's
15-GP cliff) would NOT have.
"""
import pytest

from app import espn
from app.espn import QS_RATE_PRIOR_STARTS, apply_qs_rate_blend, blend_qs_rate

K = QS_RATE_PRIOR_STARTS


def test_blend_is_the_shrinkage_formula():
    # prior = 6/10 = .600; actual = 8/20 = .400
    # → (8 + K·.6) / (20 + K)
    got = blend_qs_rate(ros_gs=10, ros_qs=6, act_gs=20, act_qs=8)
    assert got == pytest.approx((8 + K * 0.6) / (20 + K))


def test_pulls_an_inflated_projection_down_toward_actuals():
    # The real shape: ESPN says .600/start, the pitcher has actually run .400.
    prior, actual = 0.600, 0.400
    got = blend_qs_rate(ros_gs=10, ros_qs=6, act_gs=21, act_qs=21 * actual)
    assert actual < got < prior          # strictly between
    assert got < (prior + actual) / 2    # and past the midpoint, toward truth


def test_large_sample_lands_close_to_actuals():
    # 200 starts at .400 swamps a K-weight prior of .600.
    got = blend_qs_rate(ros_gs=10, ros_qs=6, act_gs=200, act_qs=80)
    assert got == pytest.approx(0.40, abs=0.02)


def test_tiny_sample_stays_near_espns_prior():
    # A 1-start callup: we have almost no information, so don't invent any —
    # one QS-less start may nudge the .600 prior but must not overturn it.
    got = blend_qs_rate(ros_gs=10, ros_qs=6, act_gs=1, act_qs=0)
    assert 0.6 - got < 0.1


def test_weight_on_actuals_rises_monotonically_with_sample():
    prior, actual = 0.60, 0.20
    prev = prior
    for n in (1, 3, 6, 12, 25, 60, 200):
        got = blend_qs_rate(ros_gs=10, ros_qs=10 * prior,
                            act_gs=n, act_qs=n * actual)
        assert got < prev            # each larger sample moves further down
        prev = got
    assert prev == pytest.approx(actual, abs=0.03)


@pytest.mark.parametrize("ros_gs,ros_qs,act_gs,act_qs", [
    (0, 0, 20, 8),        # pure reliever — no projected starts
    (None, None, 20, 8),  # missing ROS GS
    (10, 6, 0, 0),        # no actual starts yet
    (10, 6, None, None),  # no actuals block
    (10, 6, 20, None),    # actual GS present but QS missing — not the same as 0
])
def test_declines_when_it_has_nothing_to_blend(ros_gs, ros_qs, act_gs, act_qs):
    assert blend_qs_rate(ros_gs, ros_qs, act_gs, act_qs) is None


def test_rate_is_clamped_to_one_per_start():
    # QS is capped at 1 per start; a nonsense prior can't push past it.
    got = blend_qs_rate(ros_gs=2, ros_qs=99, act_gs=30, act_qs=30)
    assert 0.0 <= got <= 1.0


# ── the fetch-time write-back ────────────────────────────────────────────


def test_apply_writes_back_a_total_not_a_rate():
    # Stored shape must stay "ROS total" so sim recovers the rate as qs/gs.
    ros = {"33": 10.0, "63": 6.0}
    apply_qs_rate_blend(ros, {"33": 20.0, "63": 8.0})
    rate = (8 + K * 0.6) / (20 + K)
    assert ros["63"] == pytest.approx(rate * 10.0)
    assert ros["63"] / ros["33"] == pytest.approx(rate)   # what sim divides


def test_apply_is_a_noop_without_actuals():
    ros = {"33": 10.0, "63": 6.0}
    apply_qs_rate_blend(ros, None)
    assert ros["63"] == 6.0


def test_apply_leaves_a_relievers_projection_alone():
    ros = {"32": 60.0, "63": 0.4}      # no ROS GS at all
    apply_qs_rate_blend(ros, {"33": 1.0, "63": 0.0})
    assert ros["63"] == 0.4


def test_apply_tolerates_string_values_from_the_api():
    ros = {"33": "10", "63": "6"}
    apply_qs_rate_blend(ros, {"33": "20", "63": "8"})
    assert ros["63"] == pytest.approx(((8 + K * 0.6) / (20 + K)) * 10.0)


def test_prior_weight_constant_is_sane():
    # Guards a fat-fingered re-measure: K outside this range would either
    # ignore actuals entirely or trust a 2-start sample.
    assert 3.0 <= QS_RATE_PRIOR_STARTS <= 25.0
