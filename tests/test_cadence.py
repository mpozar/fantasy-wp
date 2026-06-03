"""Unit tests for the rotation-cadence SP start model.

Two layers:
  - `_cadence_extra_start_dist`: anchor + synthetic schedule → P(k extra starts),
    including the one- vs two-start-week split and the probable-exclusion rules.
  - `_simulate_team`: a Budget carrying an `extra_dist` injects the bimodal
    start-count variance (and couples the pitching categories), which the old
    smeared-mean path could not.
"""

import random
import statistics

import pytest

from app import sim
from app.sim import (
    Budget, STAT_K, STAT_GS, STAT_PITCH_GP, STAT_OUTS,
    build_budgets, _cadence_extra_start_dist, _simulate_team,
    _split_mean_to_dist,
)

TEAM = 100
PITCHER = "Test Starter"


@pytest.fixture(autouse=True)
def fixed_rest_weights(monkeypatch):
    """Pin the rotation distribution to a known fixture (modal 5, includes
    4-day rest) so these tests don't break when REST_DAY_WEIGHTS is re-measured
    from real data — they assert the cadence *mechanics*, not the live mix."""
    monkeypatch.setattr(sim, "REST_DAY_WEIGHTS", {4: 0.2, 5: 0.6, 6: 0.2})


def _game(game_date, probable=None, status="Scheduled", inning=None):
    return {
        "game_pk": hash(game_date) & 0xffff,
        "game_date": game_date,
        "game_status": status,
        "current_inning": inning,
        "inning_state": None,
        "probable_pitcher_name": probable,
        "is_home": 1, "opponent_pro_team_id": 200,
    }


def _daily(start_day, end_day, probable=None):
    """All-open daily games for 2026-06-`start_day` .. 2026-06-`end_day`."""
    return [_game(f"2026-06-{d:02d}", probable=probable)
            for d in range(start_day, end_day + 1)]


def _dist(last_start, games):
    sched = {TEAM: games}
    last = {sim._norm_name(PITCHER): last_start} if last_start else {}
    return _cadence_extra_start_dist(PITCHER, TEAM, sched, last, None)


def _ek(dist):
    return sum(i * p for i, p in enumerate(dist))


# ── _cadence_extra_start_dist ───────────────────────────────────────────────

def test_no_anchor_returns_none():
    # No recorded start and this pitcher isn't a probable anywhere → no anchor,
    # so the caller falls back to the flat ROS-share estimate.
    assert _dist(None, _daily(2, 7)) is None


def test_single_start_week():
    # Anchor on Mon 06-01; daily open games 06-02..06-07. One turn (~06-06) fits;
    # a second (~06-11) would fall outside the week.
    dist = _dist("2026-06-01", _daily(2, 7))
    assert dist is not None
    assert len(dist) <= 2            # P(2) is zero — no room for a second start
    assert 0.8 <= _ek(dist) <= 1.05


def test_two_start_week():
    # Anchor on Thu 05-28; daily open games all week → a real shot at two starts
    # (first ~06-01, second ~06-05/06).
    dist = _dist("2026-05-28", _daily(1, 7))
    assert dist is not None
    assert len(dist) == 3            # P(0), P(1), P(2)
    assert dist[2] > 0.3             # meaningful two-start probability
    assert dist[1] > 0.0
    assert _ek(dist) > 1.2


def test_two_start_more_likely_than_one_start_anchor():
    # Same schedule, earlier anchor → strictly more expected starts.
    early = _dist("2026-05-28", _daily(1, 7))
    late = _dist("2026-06-01", _daily(2, 7))
    assert _ek(early) > _ek(late)


def test_announced_probable_anchors_phase():
    # No recorded start, but this pitcher is the announced probable on 06-02.
    # That announced game anchors the phase (and is itself the fixed piece, not
    # credited here); the next open turn is projected from 06-02.
    games = [_game("2026-06-02", probable=PITCHER)] + _daily(3, 8)
    dist = _cadence_extra_start_dist(
        PITCHER, TEAM, {TEAM: games}, {}, None,
    )
    assert dist is not None
    assert _ek(dist) > 0.8           # ~one more start projected from 06-02


def test_probable_games_not_credited():
    # All games already have *another* pitcher announced → no open games → the
    # cadence credits zero extra starts (they're someone else's, counted there).
    games = _daily(2, 7, probable="Someone Else")
    dist = _dist("2026-06-01", games)
    assert dist == [1.0]             # P(0 extra) = 1


# ── _simulate_team: extra-start sampling ────────────────────────────────────

def _sample_k_counter(budget, n=40000):
    random.seed(0)
    return [_simulate_team({}, [budget]).get(STAT_K, 0) for _ in range(n)]


def test_extra_dist_injects_count_variance():
    # A 60% chance of one extra start worth 6 K. Mean should be ~0.6×6 = 3.6,
    # but variance must far exceed a deterministic Poisson(3.6) — the all-or-
    # nothing second start is exactly the spread the old smeared mean dropped.
    rate = 6.0
    sampled = Budget(player_id=1, name=PITCHER, role="SP", units=1.0,
                     expected={}, extra_dist=[0.4, 0.6],
                     extra_per_start={STAT_K: rate})
    smeared = Budget(player_id=2, name="Smear", role="SP", units=1.0,
                     expected={STAT_K: 0.6 * rate})  # old: deterministic mean

    s_vals = _sample_k_counter(sampled)
    m_vals = _sample_k_counter(smeared)

    # Means agree (both ≈ 3.6).
    assert abs(statistics.mean(s_vals) - 0.6 * rate) < 0.15
    assert abs(statistics.mean(m_vals) - 0.6 * rate) < 0.15

    # Variance strictly (and substantially) higher for the sampled count.
    # Analytic: Var = E[6k] + 36·Var(k) = 3.6 + 36·0.24 ≈ 12.2 vs Poisson 3.6.
    assert statistics.pvariance(s_vals) > 2 * statistics.pvariance(m_vals)


def test_split_mean_to_dist():
    assert _split_mean_to_dist(0) == [1.0]
    d = _split_mean_to_dist(1.6)
    assert d == [0.0, pytest.approx(0.4), pytest.approx(0.6)]
    assert sum(d) == pytest.approx(1.0)
    assert sum(i * p for i, p in enumerate(d)) == pytest.approx(1.6)   # mean preserved
    # Integer mean → degenerate at that integer.
    di = _split_mean_to_dist(2.0)
    assert sum(i * p for i, p in enumerate(di)) == pytest.approx(2.0)


def test_use_cadence_flag_gates_the_model():
    # Both modes sample the extra-start *count* (so it varies per sim), but only
    # the cadence model is turn-aware: its dist depends on the anchor, while the
    # far-future flat fallback splits the ROS-share mean and ignores the anchor.
    sp = {
        "player_id": 1, "full_name": PITCHER, "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 15,
        "ros_stats": {STAT_GS: 20, STAT_PITCH_GP: 20, STAT_OUTS: 360, STAT_K: 200},
    }
    sched = {TEAM: _daily(2, 7)}            # all-open week
    early = {sim._norm_name(PITCHER): "2026-05-28"}
    late = {sim._norm_name(PITCHER): "2026-06-01"}

    def sp_budget(use_cadence, last):
        bs = build_budgets([sp], sched, use_cadence=use_cadence,
                           team_total_ros_games={TEAM: 60}, last_start_by_pitcher=last)
        return next(b for b in bs if b.role == "SP")

    # Cadence ON → turn-aware: a different anchor gives a different distribution.
    on_early, on_late = sp_budget(True, early), sp_budget(True, late)
    assert on_early.extra_dist is not None
    assert on_early.extra_dist != on_late.extra_dist

    # Cadence OFF → still samples the count, but anchor-independent (flat).
    off_early, off_late = sp_budget(False, early), sp_budget(False, late)
    assert off_early.extra_dist is not None
    assert off_early.extra_dist == off_late.extra_dist
    assert off_early.units > 0


def test_zero_extra_dist_is_noop():
    # extra_dist = [1.0] (P(0 extra) = 1) adds nothing.
    b = Budget(player_id=1, name=PITCHER, role="SP", units=1.0,
               expected={STAT_K: 4.0}, extra_dist=[1.0],
               extra_per_start={STAT_K: 6.0})
    random.seed(0)
    vals = [_simulate_team({}, [b]).get(STAT_K, 0) for _ in range(20000)]
    assert abs(statistics.mean(vals) - 4.0) < 0.15   # only the fixed piece
