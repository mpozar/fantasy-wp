"""Publish-side helper for attaching per-point category history (live week)."""

import json

from app.cli import _slim_category_wp


def _details(cwp, n=10000):
    return json.dumps({"n_sims": n, "category_wp": cwp,
                       "home_budgets": [], "away_budgets": []})


def test_slim_keeps_shape_and_rounds_avgs():
    cwp = [{"stat_id": 48, "home_wins": 8000, "away_wins": 1500, "ties": 500,
            "home_avg": 58.7953, "away_avg": 28.36461}]
    slim, n = _slim_category_wp(_details(cwp))
    assert n == 10000
    e = slim[0]
    # same shape renderCategoryWP expects, avgs rounded to 3 dp
    assert e == {"stat_id": 48, "home_wins": 8000, "away_wins": 1500, "ties": 500,
                 "home_avg": 58.795, "away_avg": 28.365}


def test_slim_empty_category_wp_returns_none_list():
    slim, n = _slim_category_wp(_details([]))
    assert slim is None and n == 10000


def test_slim_bad_json_returns_none_none():
    assert _slim_category_wp("not json{") == (None, None)
    assert _slim_category_wp(None) == (None, None)
