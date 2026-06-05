"""Publish-side rate derivation (cli._apply_derived_rates).

Regression guard for the stale-scraped-rate bug: the live DOM scrape freezes the
display rate (ERA/WHIP/OPS) when the slate goes idle, while REST keeps updating the
underlying components — so the published rate must be *derived* from the current
components, not read from the frozen scraped value. Mirrors the real 2026-06-05
Sox Teacher case (scraped ERA 2.821 while components implied 3.176).
"""

from app import cli, sim


def _cell(score):
    return {"score": score, "result": "WIN"}  # result is overwritten by derivation


def _pitch_comps(outs, er, p_h, p_bb):
    return {sim.STAT_OUTS: _cell(outs), sim.STAT_ER: _cell(er),
            sim.STAT_P_H: _cell(p_h), sim.STAT_P_BB: _cell(p_bb)}


def test_derived_rate_overwrites_stale_scrape():
    # Away (Sox): components imply ERA 3.176 / WHIP 0.882, but the scraped display
    # froze at 2.821 / 0.851. Home (Shih Tzus): scrape matches components (4.378 / 1.297).
    away = {**_pitch_comps(68, 8, 17, 3),
            47: _cell(2.821), 41: _cell(0.851), 48: _cell(23.0)}  # stale ERA/WHIP, K
    home = {**_pitch_comps(37, 6, 13, 3),
            47: _cell(4.378), 41: _cell(1.297), 48: _cell(15.0)}

    cli._apply_derived_rates(home, away)

    # Rates re-derived from components, not the stale scrape.
    assert away[47]["score"] == 3.176 and away[41]["score"] == 0.882
    assert home[47]["score"] == 4.378 and home[41]["score"] == 1.297
    # Lower ERA/WHIP wins (reversed cats): away beats home on both.
    assert away[47]["result"] == "WIN" and home[47]["result"] == "LOSS"
    assert away[41]["result"] == "WIN" and home[41]["result"] == "LOSS"
    # Counting cats are left alone.
    assert away[48]["score"] == 23.0 and home[48]["score"] == 15.0


def test_derived_ops_from_components():
    # OPS derived from batting components (obp + slg).
    bat = {sim.STAT_AB: _cell(90), sim.STAT_H: _cell(30), sim.STAT_2B: _cell(6),
           sim.STAT_3B: _cell(1), sim.STAT_HR: _cell(5), sim.STAT_B_BB: _cell(12),
           sim.STAT_HBP: _cell(1), sim.STAT_SF: _cell(2), 18: _cell(0.111)}  # stale OPS
    home = {**bat}
    away = {sim.STAT_AB: _cell(80), sim.STAT_H: _cell(18), sim.STAT_HR: _cell(1),
            sim.STAT_B_BB: _cell(5), 18: _cell(0.0)}

    cli._apply_derived_rates(home, away)

    expected = round(sim.derive_ops({s: c["score"] for s, c in bat.items()}), 4)
    assert home[18]["score"] == expected
    assert home[18]["result"] == "WIN"  # higher OPS wins (not reversed)
    assert away[18]["result"] == "LOSS"


def test_no_innings_keeps_scraped_fallback():
    # A team with no outs can't derive ERA (999 sentinel) — keep the scraped value
    # for display, but still resolve the result against the opponent.
    home = _pitch_comps(60, 5, 12, 2)            # has innings
    away = {47: _cell(0.0), 41: _cell(0.0)}      # no components, scraped 0.00

    cli._apply_derived_rates(home, away)

    assert away[47]["score"] == 0.0              # fell back to scraped, not 999
    assert home[47]["result"] == "WIN"           # home pitched, away didn't
    assert away[47]["result"] == "LOSS"
