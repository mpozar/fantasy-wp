"""Publish display must match the WP's reconciled state on an idle/finished week.

Regression for 2026-07-27 m96: after Sunday's games finished the scrape froze the
scored display cats while REST kept settling the raw components, so the display
diverged from the (correct) WP — a re-derived OPS off time-mismatched components
flipped the OPS category, and an un-credited just-Final QS showed a stale low
count. Both made the scoreboard contradict its own 100% WP.
"""
from app import cli, sim


def _state(scores):
    return {sid: {"score": v, "result": None} for sid, v in scores.items()}


# Real m96 Bear Nation component counters (verified to derive_ops → 0.8335),
# but the atomically-scraped OPS (stat 18) is 0.7044. The opponent scrapes 0.7091.
_BEAR = {0: 177, 1: 52, 3: 6, 4: 1, 5: 9, 10: 13, 12: 1, 13: 2, 18: 0.7044,
         20: 34, 23: 2, 34: 119, 37: 25, 39: 5, 41: 0.756, 45: 12, 47: 2.723,
         48: 39, 57: 2, 60: 0, 63: 4, 83: 2}
# Opponent: minimal components that derive to a low OPS, scraped OPS 0.7091.
_OPP = {0: 100, 1: 10, 3: 1, 4: 0, 5: 1, 10: 5, 12: 0, 13: 0, 18: 0.7091,
        34: 120, 47: 4.277, 41: 1.411, 48: 81, 63: 4, 83: 6}


def test_derived_true_reproduces_the_bogus_ops_flip():
    # derived=True (the pre-fix / live-fold path): OPS from components → Bear's
    # 0.8335 wins the category — the bug when components are stale/mismatched.
    h, a = _state(_BEAR), _state(_OPP)
    cli._apply_derived_rates(h, a, derived=True)
    assert h[18]["result"] == "WIN"


def test_derived_false_trusts_scraped_ops():
    # derived=False (idle/finished week — no live fold): trust the scraped OPS.
    # 0.7044 < 0.7091 → Bear LOSES OPS (correct, matches the WP).
    h, a = _state(_BEAR), _state(_OPP)
    cli._apply_derived_rates(h, a, derived=False)
    assert h[18]["result"] == "LOSS"
    assert abs(h[18]["score"] - 0.7044) < 1e-6
    assert a[18]["result"] == "WIN"


def test_qs_svhd_floor_raises_a_lagging_scrape(monkeypatch):
    # The scrape banked only QS=4, but the durable archive floor is 5 (a just-Final
    # QS the idle scrape hasn't picked up). The floor credit raises the display to 5;
    # an already-ahead SVHD (6 ≥ floor 2) is never lowered.
    monkeypatch.setattr(sim, "load_settled_floor",
                        lambda *a, **k: {sim.STAT_QS: 5, sim.STAT_SVHD: 2})
    home = {sim.STAT_QS: {"score": 4.0, "result": "TIE"},
            sim.STAT_SVHD: {"score": 6.0, "result": "WIN"}}
    away = {sim.STAT_QS: {"score": 4.0, "result": "TIE"},
            sim.STAT_SVHD: {"score": 2.0, "result": "LOSS"}}
    cli._apply_qs_svhd_floor(None, 96, 1, 2, home, away)
    assert home[sim.STAT_QS]["score"] == 5      # 4 → 5 (floor)
    assert home[sim.STAT_SVHD]["score"] == 6    # 6 ≥ 2 → unchanged (never lowers)
