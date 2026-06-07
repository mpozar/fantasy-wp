"""QS/SVHD are per-event-capped (≤1 per start / per appearance), so the sim must
draw them as Binomial, not Poisson — otherwise a single in-progress start can
"earn" 2+ quality starts and spuriously win a category that's actually locked.

Regression for the 2026-06-07 Bus-vs-Mamas case: Bus had 2 banked QS + one
in-progress starter (deGrom, exp_qs≈0.91); Mamas had 3 banked + a starter to
come. Bus's reachable max is 3 and Mamas's floor is 3, so Bus can *at most tie*
— P(Bus wins QS) must be ~0, but Poisson sampling reported ~16%.
"""
import random

from app import sim


def setup_function(_):
    random.seed(12345)   # deterministic draws


# ── the sampler itself ──

def test_binomial_from_mean_never_exceeds_ceil():
    # exp_qs ≈ 0.91 from one start → only 0 or 1, never 2+.
    draws = [sim._binomial_from_mean(0.91) for _ in range(20000)]
    assert max(draws) == 1
    assert min(draws) == 0
    assert abs(sum(draws) / len(draws) - 0.91) < 0.02      # mean preserved

    # A two-start week (mean 1.5) caps at 2, never 3.
    draws2 = [sim._binomial_from_mean(1.5) for _ in range(20000)]
    assert max(draws2) == 2
    assert abs(sum(draws2) / len(draws2) - 1.5) < 0.03

    assert sim._binomial_from_mean(0.0) == 0


def test_binomial_from_mean_is_underdispersed_vs_poisson():
    # Binomial variance < mean (Poisson variance) — matches the empirical note.
    draws = [sim._binomial_from_mean(0.9) for _ in range(50000)]
    mean = sum(draws) / len(draws)
    var = sum((d - mean) ** 2 for d in draws) / len(draws)
    assert var < mean                                       # under-dispersed


# ── the category outcome ──

def _sp_budget(name, exp_qs):
    return sim.Budget(player_id=hash(name) % 10000, name=name, role="SP",
                      units=1.0, expected={sim.STAT_QS: exp_qs})


def test_bus_cannot_win_locked_qs_category():
    # Bus: 2 banked + deGrom in-progress (0.91). Mamas: 3 banked + Soriano (0.63).
    bus_state, bus_buds = {sim.STAT_QS: 2.0}, [_sp_budget("deGrom", 0.91)]
    mamas_state, mamas_buds = {sim.STAT_QS: 3.0}, [_sp_budget("Soriano", 0.63)]
    n = 20000
    bus_wins = ties = bus_over_3 = 0
    for _ in range(n):
        bus = sim._simulate_team(bus_state, bus_buds)[sim.STAT_QS]
        mam = sim._simulate_team(mamas_state, mamas_buds)[sim.STAT_QS]
        if bus > 3:
            bus_over_3 += 1            # would be impossible — must never happen
        if bus > mam:
            bus_wins += 1
        elif bus == mam:
            ties += 1
    assert bus_over_3 == 0             # Bus QS can never exceed 2 banked + 1 start
    assert bus_wins == 0              # cannot win outright — at best a tie
    assert ties > 0                   # the 3-3 tie is reachable (deGrom QS, Soriano none)
