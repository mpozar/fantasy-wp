"""Tail calibration — the measurement behind `scripts/tail_calibration.py`.

The failure modes worth guarding here are the ones that would silently invert a
conclusion rather than crash: a side/outcome mix-up (which would turn a
well-calibrated model into a broken one and vice versa), the reversed-stat
direction on ERA/WHIP, and the churn filter over- or under-excusing. The
statistics themselves are guarded by feeding them data whose answer is known by
construction.
"""
import json
import sqlite3

import pytest

from app import tail_calibration as tc

PERIOD = 10                       # 2026-06-01 .. 2026-06-07
FP = "2026-06-01T16:00:00+00:00"  # first_pitch fallback for a period with no
                                  # game_day_activity rows


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE category_state (matchup_id INT, team_id INT, "
                 "stat_id INT, score REAL, result TEXT, fetched_at TEXT, "
                 "PRIMARY KEY (matchup_id, team_id, stat_id, fetched_at))")
    conn.execute("CREATE TABLE wp_snapshots (matchup_id INT, computed_at TEXT, "
                 "home_wp REAL, away_wp REAL, model_version TEXT, "
                 "details_json TEXT, edited INT DEFAULT 0, "
                 "PRIMARY KEY (matchup_id, computed_at))")
    conn.execute("CREATE TABLE matchups (id INT PRIMARY KEY, matchup_period_id INT, "
                 "home_team_id INT, away_team_id INT, winner TEXT)")
    conn.execute("CREATE TABLE game_day_activity (matchup_period_id INT, "
                 "active_start TEXT)")
    conn.execute("INSERT INTO matchups VALUES (1,?,10,20,'HOME')", (PERIOD,))
    return conn


def _settle(conn, stats_home, stats_away, at="2026-06-08T00:00:00+00:00"):
    for tid, stats in ((10, stats_home), (20, stats_away)):
        for sid, sc in stats.items():
            conn.execute("INSERT INTO category_state VALUES (?,?,?,?,?,?)",
                         (1, tid, sid, sc, None, at))
    conn.commit()


def _snap(conn, at, cats, home_budget=(), away_budget=(), n=10000):
    """cats: {stat_id: (home_wins, away_wins, home_avg, away_avg)}"""
    d = {
        "n_sims": n,
        "category_wp": [
            {"stat_id": sid, "home_wins": hw, "away_wins": aw,
             "ties": n - hw - aw, "home_avg": ha, "away_avg": aa}
            for sid, (hw, aw, ha, aa) in cats.items()],
        "home_budgets": [{"name": nm, "role": rl} for nm, rl in home_budget],
        "away_budgets": [{"name": nm, "role": rl} for nm, rl in away_budget],
    }
    conn.execute("INSERT INTO wp_snapshots VALUES (1,?,0.5,0.5,'mc-v1',?,0)",
                 (at, json.dumps(d)))
    conn.commit()


# ── outcome derivation: the thing every other number is measured against ──

def _matchup(conn):
    return conn.execute("SELECT * FROM matchups WHERE id=1").fetchone()


def test_higher_is_better_and_lower_is_better_go_opposite_ways():
    conn = _mem_db()
    # home has more hits (wins H) and a HIGHER ERA (loses ERA).
    _settle(conn, {1: 60, 47: 4.10}, {1: 50, 47: 3.20})
    out = tc.outcome_by_stat(conn, _matchup(conn))
    assert out[1] == "HOME"
    assert out[47] == "AWAY"


def test_equal_scores_are_a_tie_not_a_win_for_either_side():
    conn = _mem_db()
    _settle(conn, {1: 55}, {1: 55})
    assert tc.outcome_by_stat(conn, _matchup(conn))[1] == "TIE"


def test_a_missing_counting_score_reads_as_zero():
    # Counting cats accumulate from zero, so "no row" is a real 0 and the side
    # with any production must win. Dropping the category instead would quietly
    # remove the most lopsided observations from the sample.
    conn = _mem_db()
    _settle(conn, {1: 4}, {})
    assert tc.outcome_by_stat(conn, _matchup(conn))[1] == "HOME"


def test_a_category_nobody_has_a_row_for_is_omitted():
    conn = _mem_db()
    _settle(conn, {1: 4}, {1: 2})
    assert 63 not in tc.outcome_by_stat(conn, _matchup(conn))


# ── the side/outcome mapping ──

def test_the_winning_side_is_scored_as_the_winner():
    conn = _mem_db()
    _settle(conn, {1: 60}, {1: 50})                    # home wins H
    _snap(conn, "2026-06-03T00:00:00+00:00", {1: (9000, 1000, 60.0, 50.0)})
    res = tc.collect(conn, first_period=PERIOD)
    assert res.units[(1, "home", 1)].won == 1
    assert res.units[(1, "away", 1)].won == 0
    # ...and each side's probability is its own, not the other's.
    assert res.units[(1, "home", 1)].min_p == pytest.approx(0.90)
    assert res.units[(1, "away", 1)].min_p == pytest.approx(0.10)


def test_a_tie_counts_as_a_loss_for_both_sides():
    # `p` is P(win), which excludes ties by construction, so the outcome it is
    # scored against must exclude them too.
    conn = _mem_db()
    _settle(conn, {1: 50}, {1: 50})
    _snap(conn, "2026-06-03T00:00:00+00:00", {1: (4000, 4000, 50.0, 50.0)})
    res = tc.collect(conn, first_period=PERIOD)
    assert res.units[(1, "home", 1)].won == 0
    assert res.units[(1, "away", 1)].won == 0


def test_hand_edited_snapshots_are_excluded():
    # Smoothed WP rows are deliberately not model output (2026-06-04 repair).
    conn = _mem_db()
    _settle(conn, {1: 60}, {1: 50})
    _snap(conn, "2026-06-03T00:00:00+00:00", {1: (9000, 1000, 60.0, 50.0)})
    conn.execute("UPDATE wp_snapshots SET edited=1")
    conn.commit()
    assert tc.collect(conn, first_period=PERIOD).n_forecasts == 0


def test_snapshots_before_first_pitch_are_not_forecasts_under_test():
    conn = _mem_db()
    _settle(conn, {1: 60}, {1: 50})
    _snap(conn, "2026-05-20T00:00:00+00:00", {1: (9000, 1000, 60.0, 50.0)})
    assert tc.collect(conn, first_period=PERIOD).n_forecasts == 0


# ── churn: the filter the whole "is it the model's fault" question rests on ──

HIT_A = (("Hitter A", "HIT"),)
HIT_AB = (("Hitter A", "HIT"), ("Hitter B", "HIT"))
ARM_A = (("Arm A", "SP"),)
ARM_AB = (("Arm A", "SP"), ("Arm B", "RP"))


def test_a_later_addition_marks_only_forecasts_made_before_it():
    conn = _mem_db()
    _settle(conn, {63: 4}, {63: 3})
    _snap(conn, "2026-06-02T00:00:00+00:00", {63: (9000, 1000, 4.0, 3.0)},
          home_budget=ARM_A, away_budget=ARM_A)
    _snap(conn, "2026-06-05T00:00:00+00:00", {63: (9000, 1000, 4.0, 3.0)},
          home_budget=ARM_AB, away_budget=ARM_A)
    res = tc.collect(conn, first_period=PERIOD)
    # The home side gained an arm on the 5th. Its forecast from the 2nd was made
    # without knowing that, so it is excused; the one made on the 5th knew, so
    # it is not. The away side never changed and is clean throughout.
    assert res.n_forecasts == 4                     # 2 snapshots x 2 sides
    assert res.n_churn_free == 3


def test_churn_is_scoped_to_the_side_and_to_the_relevant_player_type():
    conn = _mem_db()
    _settle(conn, {1: 60, 63: 4}, {1: 50, 63: 3})
    cats = {1: (9000, 1000, 60.0, 50.0), 63: (9000, 1000, 4.0, 3.0)}
    _snap(conn, "2026-06-02T00:00:00+00:00", cats,
          home_budget=HIT_A + ARM_A, away_budget=HIT_A + ARM_A)
    _snap(conn, "2026-06-05T00:00:00+00:00", cats,
          home_budget=HIT_A + ARM_AB, away_budget=HIT_A + ARM_A)
    res = tc.collect(conn, first_period=PERIOD)
    # A pitcher joining the home side must not excuse a HITTING forecast, nor
    # anything on the away side. Exactly one of the eight is exposed.
    assert res.n_forecasts == 8       # 2 snapshots x 2 cats x 2 sides
    assert res.n_churn_free == 7
    p90 = tc.bin_index(0.9)           # the home side's probability in both cats
    assert res.bins[(1, tc.CLEAN, p90)][0] == 2    # both home H forecasts clean
    assert res.bins[(63, tc.CLEAN, p90)][0] == 1   # home QS on the 2nd excused


def test_a_player_who_leaves_the_budget_and_returns_is_not_churn():
    # Budgets legitimately drop a player once his games are done or while he is
    # on the IL and re-add him later. Scoring that as a roster addition would
    # excuse the model for its own errors — the filter must key on names never
    # seen before, not on absence from the previous snapshot.
    conn = _mem_db()
    _settle(conn, {63: 4}, {63: 3})
    cats = {63: (9000, 1000, 4.0, 3.0)}
    _snap(conn, "2026-06-02T00:00:00+00:00", cats,
          home_budget=ARM_AB, away_budget=ARM_A)
    _snap(conn, "2026-06-03T00:00:00+00:00", cats,
          home_budget=ARM_A, away_budget=ARM_A)      # Arm B drops out
    _snap(conn, "2026-06-05T00:00:00+00:00", cats,
          home_budget=ARM_AB, away_budget=ARM_A)     # ...and comes back
    res = tc.collect(conn, first_period=PERIOD)
    assert res.n_churn_free == res.n_forecasts


def test_pre_week_budgets_seed_the_churn_baseline():
    # A player present in the pre-week projection is not new to the team just
    # because the in-week series happens to start without him.
    conn = _mem_db()
    _settle(conn, {63: 4}, {63: 3})
    cats = {63: (9000, 1000, 4.0, 3.0)}
    _snap(conn, "2026-05-20T00:00:00+00:00", cats,
          home_budget=ARM_AB, away_budget=ARM_A)     # before first pitch
    _snap(conn, "2026-06-02T00:00:00+00:00", cats,
          home_budget=ARM_A, away_budget=ARM_A)
    _snap(conn, "2026-06-05T00:00:00+00:00", cats,
          home_budget=ARM_AB, away_budget=ARM_A)
    res = tc.collect(conn, first_period=PERIOD)
    assert res.n_forecasts == 4                      # 2 in-week snaps x 2 sides
    assert res.n_churn_free == res.n_forecasts


# ── the statistics, on data whose answer is known by construction ──

def test_bin_index_is_half_open_and_clamps_at_one():
    assert tc.bin_index(0.0) == 0
    assert tc.bin_index(0.001) == 1                  # lower edge belongs above
    assert tc.bin_index(0.9999) == len(tc.EDGES) - 2
    assert tc.bin_index(1.0) == len(tc.EDGES) - 2


def test_slope_recovers_a_known_shrinkage():
    # Forecasts that overstate every gap by 1/0.6 must measure as slope 0.6.
    pairs = [(m / 0.6, m) for m in (-5, -3, -1, 1, 2, 4, 6, 9)]
    assert tc.slope_through_origin(pairs) == pytest.approx(0.6)


def test_slope_is_one_when_the_forecast_is_unbiased():
    pairs = [(m, m + e) for m, e in
             [(-4, .5), (-2, -.5), (1, .5), (3, -.5), (5, .5), (7, -.5)]]
    assert tc.slope_through_origin(pairs) == pytest.approx(1.0, abs=0.05)


def test_cluster_ratio_is_one_for_a_calibrated_set():
    # Ten clusters, each predicting 2.0 wins and delivering 2.
    clusters = {i: [2.0, 2] for i in range(10)}
    ratio, pred, obs = tc.cluster_ratio(clusters)
    assert ratio == pytest.approx(1.0)
    assert (pred, obs) == (20.0, 20)
    lo, hi = tc.cluster_ratio_ci(clusters, reps=400)
    assert lo == pytest.approx(1.0) and hi == pytest.approx(1.0)


def test_cluster_ci_widens_when_one_cluster_carries_the_signal():
    # Nine quiet clusters and one that supplies every surprise: the interval has
    # to admit that resampling might miss it entirely.
    clusters = {i: [2.0, 2] for i in range(9)}
    clusters[9] = [2.0, 20]
    lo, hi = tc.cluster_ratio_ci(clusters, reps=1000)
    assert lo < 1.5 < hi
    assert hi - lo > 1.0


def test_implied_sigma_recovers_the_sigma_that_produced_the_probability():
    from statistics import NormalDist
    sigma, margin = 3.0, 4.5
    p = NormalDist().cdf(margin / sigma)
    assert tc.implied_sigma(p, margin) == pytest.approx(sigma, rel=1e-6)


@pytest.mark.parametrize("p,margin", [(0.0, 2.0), (1.0, 2.0), (0.5, 2.0),
                                      (0.9, 0.0), (0.2, 2.0)])
def test_implied_sigma_refuses_the_cases_it_cannot_answer(p, margin):
    # Rails, a zero margin, a coin-flip probability, and a *negative* implied
    # sigma (p below .5 with a positive margin) all have to return None rather
    # than a number that would silently distort the dispersion table.
    assert tc.implied_sigma(p, margin) is None


def test_robust_z_scale_reads_one_on_unit_noise_and_two_on_doubled():
    import random
    rng = random.Random(4)
    unit = [rng.gauss(0, 1) for _ in range(4000)]
    assert tc.robust_z_scale(unit) == pytest.approx(1.0, abs=0.06)
    assert tc.robust_z_scale([2 * z for z in unit]) == pytest.approx(2.0, abs=0.12)


def test_robust_z_scale_ignores_a_single_wild_outlier():
    # One tiny-sigma forecast must not be able to set the width of the middle.
    import random
    rng = random.Random(5)
    zs = [rng.gauss(0, 1) for _ in range(400)] + [1e6]
    assert tc.robust_z_scale(zs) == pytest.approx(1.0, abs=0.12)


def test_robust_z_scale_declines_a_sample_too_small_to_mean_anything():
    assert tc.robust_z_scale([0.3, -0.2, 1.1]) is None
