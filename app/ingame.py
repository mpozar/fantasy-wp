"""In-game projection of threshold/context pitcher stats (QS, SVHD).

These stats aren't accumulating counters — they're decided by game state, so
the linear `_sp_factor`/`_rp_factor` time-scaling in `sim.py` projects them
badly mid-game. This module computes the *remaining* QS/SVHD contribution for a
pitcher from their observed in-game state, per the agreed state machine.

Pure and dependency-free on purpose: every input is a plain field on a small
dataclass, so the whole thing is exercised by `tests/test_ingame.py` and
`scripts/ingame_scenarios.py` with mock data — no DB, no live fetch. Wiring
these into `build_budgets` (with real boxscore extraction) is a later phase;
nothing here touches the running pipeline yet.

Key principle: an earned QS/SVHD is "banked" into the live cumulative totals
only when the game is **Final** (that's when it's credited and scraped). Until
then — even after the pitcher has exited with the outcome locked — we own it and
return it as remaining contribution. So:

  - game Final            → 0  (credited; already in the totals we sim from)
  - not yet pitched       → the forward estimate
  - currently pitching    → probability of earning it from here
  - exited, game still live→ the locked 0/1, supplied by us
"""

from __future__ import annotations

import math
from dataclasses import dataclass

QS_OUTS = 18      # 6.0 IP
QS_MAX_ER = 3     # quality start allows at most 3 earned runs

# Default fixed conversion probability for a reliever who is mid-appearance in
# an unresolved save/hold situation with the lead intact. Tuning knob.
DEFAULT_SVHD_CONVERSION = 0.85


def _poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam). k < 0 → 0."""
    if k < 0:
        return 0.0
    if lam <= 0:
        return 1.0
    term = math.exp(-lam)
    total = term
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


# ── Quality starts ──────────────────────────────────────────────────────────

@dataclass
class StarterState:
    """Observed in-game state of a rostered *starting* pitcher.

    `appeared`/`exited` come from the boxscore pitcher order (a starter has
    exited once a later pitcher from their team appears). `outs`/`er` are the
    live running line. `exp_outs_per_start` and `er_per_out` come from the
    pitcher's ROS projection and drive the simple pull/ER model.
    """
    game_status: str            # "Scheduled" | "In Progress" | "Final"
    appeared: bool
    exited: bool
    outs: int
    er: int
    exp_outs_per_start: float   # avg outs/start (ros_outs / ros_gs)
    er_per_out: float           # ros_er / ros_outs
    pregame_qs_rate: float      # season QS probability for this start


def _qs_inprogress_prob(outs: int, er: int,
                        exp_outs_per_start: float, er_per_out: float) -> float:
    """QS probability for a starter still pitching (caller guarantees er <= 3).

    P(reaches 18 outs before being pulled) × P(allows <= 3-er more ER). Both
    use the pitcher's expected remaining outs as the exposure. This is the
    deliberately-simple first cut; the pull model (how many more outs before the
    hook) is the main thing to refine — it ignores pitch count and that a
    cruising pitcher tends to go past their average.
    """
    exp_remaining = max(0.0, exp_outs_per_start - outs)
    outs_needed = max(0, QS_OUTS - outs)

    if outs_needed == 0:
        p_reach = 1.0
    else:
        # P(records >= outs_needed more outs) with remaining outs ~ Poisson.
        p_reach = 1.0 - _poisson_cdf(outs_needed - 1, exp_remaining)

    er_headroom = QS_MAX_ER - er          # additional ER allowed (>= 0 here)
    p_er_ok = _poisson_cdf(er_headroom, er_per_out * exp_remaining)

    return p_reach * p_er_ok


def project_qs(s: StarterState) -> float:
    """Remaining QS contribution (0..1) for a starter — see module docstring."""
    if s.game_status == "Final":
        return 0.0                                  # credited → in live totals
    if not s.appeared:
        return s.pregame_qs_rate                    # game hasn't started
    if s.exited:                                    # locked, not yet in totals
        return 1.0 if (s.outs >= QS_OUTS and s.er <= QS_MAX_ER) else 0.0
    if s.er > QS_MAX_ER:                            # currently pitching
        return 0.0                                  # impossible (ER only rises)
    return _qs_inprogress_prob(s.outs, s.er, s.exp_outs_per_start, s.er_per_out)


# ── Saves + holds ───────────────────────────────────────────────────────────

@dataclass
class RelieverState:
    """Observed in-game state of a rostered *relief* pitcher.

    The save/hold situational flags (`entered_save_situation`, `lead_intact`,
    `recorded_out`) are what we have to derive ourselves from score + entry
    context — the feed doesn't credit SV/HD live. For the not-yet-pitched case
    `game_script_gate` (0..1) scales the season rate by how plausible a SV/HD
    chance is given the live score/inning.
    """
    game_status: str            # "Scheduled" | "In Progress" | "Final"
    appeared: bool
    exited: bool
    entered_save_situation: bool
    lead_intact: bool           # hasn't surrendered the protected lead
    recorded_out: bool
    svhd_rate: float            # season SVHD per appearance (role prior)
    game_script_gate: float = 1.0
    conversion_prob: float = DEFAULT_SVHD_CONVERSION


def project_svhd(s: RelieverState) -> float:
    """Remaining SVHD contribution (0..1) for a reliever — see module docstring."""
    if s.game_status == "Final":
        return 0.0                                  # credited → in live totals
    if not s.appeared:
        # Forward estimate: season rate, gated by how live the save chance is.
        return s.svhd_rate * s.game_script_gate
    if s.exited:                                    # locked, not yet in totals
        earned = (s.entered_save_situation and s.recorded_out and s.lead_intact)
        return 1.0 if earned else 0.0
    # Currently pitching.
    if not s.entered_save_situation:
        return 0.0                                  # not a save/hold spot
    if not s.lead_intact:
        return 0.0                                  # blew it
    return s.conversion_prob


def game_script_gate(team_margin: int, inning: int) -> float:
    """Crude 0..1 gate on a not-yet-pitched reliever's SVHD chance from the live
    score (team_margin = this team's runs − opponent's) and inning.

    A save/hold needs a close, winnable game late. Early on we don't know yet
    (keep the full prior); late blowouts or large deficits kill the chance.
    This is the fuzziest piece and the main tuning target.
    """
    if inning < 6:
        return 1.0                       # too early to tell — keep the prior
    if team_margin <= -4:
        return 0.1                       # losing big late — no save chance
    if abs(team_margin) >= 6:
        return 0.3                       # blowout — low-leverage, closer rests
    return 1.0
