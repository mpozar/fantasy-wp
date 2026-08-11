"""Publish display must match the WP's reconciled state on an idle/finished week.

Regression for 2026-07-27 m96: after Sunday's games finished the scrape froze the
scored display cats while REST kept settling the raw components, so the display
diverged from the (correct) WP — a re-derived OPS off time-mismatched components
flipped the OPS category, and an un-credited just-Final QS showed a stale low
count. Both made the scoreboard contradict its own 100% WP.

The QS/SVHD half is now handled at source by the closing scrape rather than by a
display-side floor — see `test_publish_does_not_adjust_qs_svhd`.
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


def test_publish_does_not_adjust_qs_svhd(monkeypatch):
    """The QS/SVHD half of the 2026-07-27 case, inverted.

    Back then an idle scrape could leave a just-Final QS un-banked for hours, so
    publish raised the display to a durable archive floor (`_apply_qs_svhd_floor`,
    `max(scrape, floor)`). The closing scrape (`cli._scrape_due`, 2026-08-09)
    removed the gap at its source — measured 2026-08-10, four credits from games
    that finalized with nothing else live all banked within ~8s — so on 2026-08-11
    the floor was deleted and QS/SVHD now come straight from `category_state`.

    This guards the new contract: publish must leave them EXACTLY as ESPN reports
    them. Re-introducing any display-side raise would put the scoreboard back on a
    number the WP doesn't share — the sim/display disagreement that cost the m105
    +16.4pp/−8.6pp swing pair.
    """
    assert not hasattr(cli, "_apply_qs_svhd_floor"), \
        "the display-side QS/SVHD raise was deleted 2026-08-11; see the docstring"
    assert not hasattr(sim, "load_settled_floor"), \
        "the QS/SVHD settled floor was deleted 2026-08-11; see the docstring"

    # A publish over a finished week must pass the scraped values through untouched.
    home, away = _state(_BEAR), _state(_OPP)
    before = (home[sim.STAT_QS]["score"], home[sim.STAT_SVHD]["score"])
    cli._apply_derived_rates(home, away, derived=False)
    cli._apply_counting_results(home, away)
    assert (home[sim.STAT_QS]["score"], home[sim.STAT_SVHD]["score"]) == before
    # …and the results still get decided, just from ESPN's own numbers:
    # Bear QS 4 vs 4 → TIE; SVHD 2 vs 6 → LOSS.
    assert home[sim.STAT_QS]["result"] == "TIE"
    assert home[sim.STAT_SVHD]["result"] == "LOSS"
