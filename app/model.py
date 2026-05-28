"""Placeholder win-probability model.

For each scoring category, treat the current score ratio as the probability
that side wins that category by week's end. Convolve the per-category
Bernoullis into a poisson-binomial distribution over total category wins,
and roll up to overall WP. If category-count ties on the median, use the
tiebreaker stat's score ratio.

This is structurally the same shape as the real Monte Carlo will produce,
so the downstream UI and storage don't need to change when we swap models.
"""

from __future__ import annotations

from dataclasses import dataclass

MODEL_VERSION = "ratio-v0"


@dataclass(frozen=True)
class CatConfig:
    stat_id: int
    reversed: bool


def _per_cat_home_win_prob(home_score: float, away_score: float, reversed_: bool) -> float:
    total = home_score + away_score
    if total <= 0:
        return 0.5
    if reversed_:
        # lower is better → home wins when its score is the smaller share
        return away_score / total
    return home_score / total


def _convolve(probs: list[float]) -> list[float]:
    """Distribution over number of successes for independent non-identical Bernoullis."""
    n = len(probs)
    dist = [0.0] * (n + 1)
    dist[0] = 1.0
    for p in probs:
        new = [0.0] * (n + 1)
        for k, pk in enumerate(dist):
            if pk == 0.0:
                continue
            new[k] += pk * (1 - p)
            if k + 1 <= n:
                new[k + 1] += pk * p
        dist = new
    return dist


def compute_wp(
    home_scores: dict[int, float],
    away_scores: dict[int, float],
    categories: list[CatConfig],
    tiebreaker_stat_id: int | None,
) -> tuple[float, float, dict]:
    """Return (home_wp, away_wp, details)."""
    per_cat = []
    for cat in categories:
        h = home_scores.get(cat.stat_id, 0.0)
        a = away_scores.get(cat.stat_id, 0.0)
        p = _per_cat_home_win_prob(h, a, cat.reversed)
        per_cat.append({
            "stat_id": cat.stat_id,
            "home_score": h,
            "away_score": a,
            "p_home_wins_cat": p,
        })
    dist = _convolve([c["p_home_wins_cat"] for c in per_cat])
    n = len(per_cat)

    # P(home wins matchup) = sum over k of P(home wins k cats) * P(home wins overall | k cats won)
    # Simplification: assume ties (cat-result "TIE") split evenly, so home wins if k > n/2,
    # ties when k == n/2, loses if k < n/2.
    half = n / 2
    home_wp = 0.0
    for k, pk in enumerate(dist):
        if k > half:
            home_wp += pk
        elif k == half:  # only hits when n is even
            tb_p = 0.5
            if tiebreaker_stat_id is not None:
                tb_h = home_scores.get(tiebreaker_stat_id, 0.0)
                tb_a = away_scores.get(tiebreaker_stat_id, 0.0)
                if tb_h + tb_a > 0:
                    tb_p = tb_h / (tb_h + tb_a)
            home_wp += pk * tb_p

    away_wp = 1.0 - home_wp
    return home_wp, away_wp, {
        "model": MODEL_VERSION,
        "per_cat": per_cat,
        "cat_win_distribution": dist,
    }
