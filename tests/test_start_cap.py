"""Physical min-rest cap on projected SP starts.

A pitcher can't start more often than the rotation allows, so the speculative
(cadence) extra-start piece is clipped such that announced starts + extra never
exceed what's physically possible in the remaining window — while never clipping
the announced starts themselves. Backstop for transient impossible-back-to-back
projections (2026-06-26 Rasmussen: a cadence Jun-27 turn colliding with a
soon-to-be-announced Jun-28 start briefly projected 2 starts in 2 days).
"""
from datetime import date

import pytest

from app import sim
from app.sim import (
    _max_remaining_starts, _cap_extra_dist, build_budgets,
    STAT_GS, STAT_PITCH_GP, STAT_OUTS, STAT_K,
)

D = date.fromisoformat
TEAM = 100
NAME = "Cap Test SP"


def test_max_remaining_starts():
    # Rasmussen case: last start Jun 22, window ends Jun 28 → only one more turn fits.
    assert _max_remaining_starts(D("2026-06-22"), D("2026-06-28"), 5) == 1
    # Legit two-start week: last start Jun 18 → Jun 23 + Jun 28 both fit.
    assert _max_remaining_starts(D("2026-06-18"), D("2026-06-28"), 5) == 2
    # No room before window end.
    assert _max_remaining_starts(D("2026-06-26"), D("2026-06-28"), 5) == 0
    # Unknown bounds → no cap.
    assert _max_remaining_starts(None, D("2026-06-28"), 5) is None
    assert _max_remaining_starts(D("2026-06-22"), None, 5) is None


def test_cap_extra_dist():
    assert _cap_extra_dist([0.1, 0.5, 0.4], 0) == [1.0]              # no extra allowed
    capped = _cap_extra_dist([0.1, 0.5, 0.4], 1)                     # fold P(2) into P(1)
    assert capped[0] == 0.1 and capped[1] == pytest.approx(0.9)
    assert sum(capped) == pytest.approx(1.0)
    assert _cap_extra_dist([0.3, 0.7], 1) == [0.3, 0.7]             # already within → no-op
    assert _cap_extra_dist([0.3, 0.7], 5) == [0.3, 0.7]


def _sp():
    return {"player_id": 1, "full_name": NAME, "pro_team_id": TEAM,
            "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 15,
            "ros_stats": {STAT_GS: 30, STAT_PITCH_GP: 30, STAT_OUTS: 540, STAT_K: 180}}


def _g(d, probable=None, status="Scheduled"):
    return {"game_pk": hash(d) & 0xffff, "game_date": d, "game_status": status,
            "current_inning": None, "inning_state": None,
            "probable_pitcher_name": probable, "is_home": 1, "opponent_pro_team_id": 200}


def _sp_budget(sched, last):
    bs = build_budgets([_sp()], sched, team_total_ros_games={TEAM: 60},
                       last_start_by_pitcher={sim._norm_name(NAME): last})
    return next(b for b in bs if b.role == "SP")


def test_cap_clips_collision_but_respects_the_announced_start():
    # Anchor (last recorded start) Jun 22 + an announced Jun-22 probable (= 1 fixed
    # start) + an OPEN Jun-27 game the cadence would project as an extra. Only one
    # start physically fits Jun 22→27, so the extra must be clipped — and the one
    # announced start is kept. (Without the cap this projects ~1.34 starts.)
    b = _sp_budget({TEAM: [_g("2026-06-22", probable=NAME), _g("2026-06-27")]},
                   "2026-06-22")
    assert b.units == pytest.approx(1.0, abs=1e-9)   # announced start kept, extra clipped
    # extra collapsed to "0 extra starts" (single bucket → E[extra] = 0).
    assert b.extra_dist is None or len(b.extra_dist) == 1
    assert sim._expected_extra_starts(b.extra_dist) == pytest.approx(0.0, abs=1e-9)


def test_cap_keeps_a_real_two_start_week():
    # Two announced starts 5 days apart (Jun 23 + Jun 28) — a genuine two-start week.
    # The cap must NOT clip either; physical max over the window is 2.
    b = _sp_budget({TEAM: [_g("2026-06-23", probable=NAME), _g("2026-06-28", probable=NAME)]},
                   "2026-06-18")
    assert b.units == pytest.approx(2.0, abs=1e-9)   # both announced starts survive
