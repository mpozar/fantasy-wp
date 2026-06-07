"""Publish-side category-result symmetry (cli._apply_counting_results).

Regression guard for the asymmetric W-L-T records bug (observed ~08:00 CET
2026-06-06): the per-team stored `result` is stamped independently per (team, stat)
and read per-stat-latest, so a category lead that flips between the two teams' last
writes (e.g. mid overnight stat-reconciliation) left the stored results
non-complementary, and `_team_block` summed them into NON-mirrored records — Desert
Dawgs 9-1-0 next to Bear Nation 2-7-1, which is impossible in head-to-head category
scoring. Results must derive from a single home-vs-away comparison so the two sides
always mirror.
"""
from collections import Counter

from app import cli

H, R, HR, SB, K, QS, SVHD = 1, 20, 5, 23, 48, 63, 83
ALL = [H, R, HR, SB, K, QS, SVHD]
_COMP = {"WIN": "LOSS", "LOSS": "WIN", "TIE": "TIE"}


def _cell(score, result):
    return {"score": score, "result": result}


def _record(state):
    c = Counter(s["result"] for s in state.values())
    return (c["WIN"], c["LOSS"], c["TIE"])


def test_skewed_counting_results_made_symmetric():
    # Stored results are inconsistent: home K marked WIN though 38 < 42 (stale skew),
    # and QS stored WIN/LOSS though tied — exactly the temporal-skew shape.
    home = {H: _cell(55, "WIN"), K: _cell(38, "WIN"), QS: _cell(3, "WIN")}
    away = {H: _cell(33, "WIN"), K: _cell(42, "LOSS"), QS: _cell(3, "LOSS")}
    cli._apply_counting_results(home, away)
    assert home[H]["result"] == "WIN" and away[H]["result"] == "LOSS"   # 55 > 33
    assert home[K]["result"] == "LOSS" and away[K]["result"] == "WIN"   # 38 < 42 (corrected)
    assert home[QS]["result"] == "TIE" and away[QS]["result"] == "TIE"  # equal (corrected)


def test_results_always_complementary_and_record_mirrors():
    # Every cat stored as WIN for both sides (the impossible state the bug produces).
    home = {c: _cell(v, "WIN") for c, v in zip(ALL, [55, 34, 8, 4, 38, 3, 1])}
    away = {c: _cell(v, "WIN") for c, v in zip(ALL, [33, 20, 10, 3, 42, 3, 0])}
    cli._apply_counting_results(home, away)
    for c in ALL:
        assert away[c]["result"] == _COMP[home[c]["result"]], c
    hw, hl, ht = _record(home)
    aw, al, at = _record(away)
    assert (hw, hl, ht) == (al, aw, at)   # mirror images


def test_missing_counting_score_treated_as_zero_keeps_mirror():
    # One side missing a counting cat (e.g. SVHD) → treat as 0 so both still compare
    # and the record stays mirrored.
    home = {SVHD: _cell(2, "WIN")}
    away = {}
    cli._apply_counting_results(home, away)
    assert home[SVHD]["result"] == "WIN" and away[SVHD]["result"] == "LOSS"


def test_scores_are_preserved():
    home = {H: _cell(55, "LOSS")}   # wrong stored result, correct score
    away = {H: _cell(33, "WIN")}
    cli._apply_counting_results(home, away)
    assert home[H]["score"] == 55 and away[H]["score"] == 33   # scores untouched
    assert home[H]["result"] == "WIN" and away[H]["result"] == "LOSS"
