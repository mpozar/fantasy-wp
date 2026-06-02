"""Tests for the in-game QS/SVHD projection state machine (app/ingame.py).

These lock in the agreed state-table behavior with mock in-progress lines.
Run: .venv/bin/python -m pytest tests/ -q
"""

import pytest

from app.ingame import (
    StarterState,
    RelieverState,
    project_qs,
    project_svhd,
    game_script_gate,
)


# Sensible defaults for an established starter: ~16.5 outs/start (5.2 IP),
# ~0.13 ER/out (≈3.5 ER/9), 55% season QS rate.
def starter(**kw) -> StarterState:
    base = dict(
        game_status="In Progress", appeared=True, exited=False,
        outs=0, er=0, exp_outs_per_start=16.5, er_per_out=0.13,
        pregame_qs_rate=0.55,
    )
    base.update(kw)
    return StarterState(**base)


def reliever(**kw) -> RelieverState:
    base = dict(
        game_status="In Progress", appeared=True, exited=False,
        entered_save_situation=True, lead_intact=True, recorded_out=True,
        svhd_rate=0.45, game_script_gate=1.0, conversion_prob=0.85,
    )
    base.update(kw)
    return RelieverState(**base)


# ── QS: deterministic states ────────────────────────────────────────────────

def test_qs_final_is_banked_zero():
    # Even a clear quality start returns 0 remaining once Final (it's credited).
    assert project_qs(starter(game_status="Final", exited=True, outs=21, er=1)) == 0.0


def test_qs_not_pitched_uses_pregame_rate():
    s = starter(game_status="Scheduled", appeared=False, pregame_qs_rate=0.6)
    assert project_qs(s) == 0.6


def test_qs_exited_in_progress_qualified_is_one():
    # Pulled after 6 IP / 1 ER, game still going → we own the locked QS = 1.
    assert project_qs(starter(exited=True, outs=18, er=1)) == 1.0


def test_qs_exited_short_outing_is_zero():
    assert project_qs(starter(exited=True, outs=17, er=0)) == 0.0   # 5.2 IP


def test_qs_exited_blown_by_er_is_zero():
    assert project_qs(starter(exited=True, outs=21, er=4)) == 0.0


def test_qs_currently_pitching_over_er_cap_is_zero():
    # 5 ER while still in → QS impossible regardless of innings.
    assert project_qs(starter(outs=12, er=5)) == 0.0


# ── QS: in-progress probabilities (structure, not exact values) ───────────────

def test_qs_threshold_met_still_in_is_high_but_not_certain():
    # 6 IP / 1 ER but still pitching — could still allow >3 ER → high, < 1.
    p = project_qs(starter(outs=18, er=1, exp_outs_per_start=19))
    assert 0.7 < p < 1.0


def test_qs_more_outs_recorded_raises_prob():
    early = project_qs(starter(outs=9, er=0, exp_outs_per_start=18))
    late = project_qs(starter(outs=15, er=0, exp_outs_per_start=18))
    assert late > early


def test_qs_more_er_lowers_prob():
    clean = project_qs(starter(outs=15, er=0, exp_outs_per_start=18))
    dirty = project_qs(starter(outs=15, er=3, exp_outs_per_start=18))  # no headroom
    assert dirty < clean


def test_qs_inprogress_prob_in_unit_range():
    for o in (3, 9, 12, 15, 18):
        for e in (0, 1, 2, 3):
            p = project_qs(starter(outs=o, er=e))
            assert 0.0 <= p <= 1.0


# ── SVHD: deterministic states ────────────────────────────────────────────────

def test_svhd_final_is_banked_zero():
    assert project_svhd(reliever(game_status="Final", exited=True)) == 0.0


def test_svhd_exited_earned_is_one():
    # Entered a save spot, got an out, left with the lead, game still live → 1.
    assert project_svhd(reliever(exited=True)) == 1.0


def test_svhd_exited_blown_is_zero():
    assert project_svhd(reliever(exited=True, lead_intact=False)) == 0.0


def test_svhd_exited_non_save_spot_is_zero():
    assert project_svhd(reliever(exited=True, entered_save_situation=False)) == 0.0


def test_svhd_exited_no_out_is_zero():
    assert project_svhd(reliever(exited=True, recorded_out=False)) == 0.0


def test_svhd_currently_not_a_save_spot_is_zero():
    assert project_svhd(reliever(entered_save_situation=False)) == 0.0


def test_svhd_currently_blown_is_zero():
    assert project_svhd(reliever(lead_intact=False)) == 0.0


def test_svhd_currently_in_opp_uses_conversion_prob():
    assert project_svhd(reliever(conversion_prob=0.8)) == 0.8


# ── SVHD: not-yet-pitched forward estimate + game-script gate ─────────────────

def test_svhd_not_pitched_scales_rate_by_gate():
    s = reliever(appeared=False, svhd_rate=0.5, game_script_gate=0.3)
    assert project_svhd(s) == pytest.approx(0.15)


def test_game_script_gate_close_late_full():
    assert game_script_gate(team_margin=2, inning=8) == 1.0


def test_game_script_gate_blowout_lowered():
    assert game_script_gate(team_margin=7, inning=8) < 1.0


def test_game_script_gate_losing_big_near_zero():
    assert game_script_gate(team_margin=-5, inning=8) < 0.2


def test_game_script_gate_early_keeps_prior():
    assert game_script_gate(team_margin=-5, inning=3) == 1.0
