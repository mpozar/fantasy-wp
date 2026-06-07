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


# ── _downsample_history: thin older history, keep the recent window full-res ──

from app.cli import _downsample_history


def _hist(n, start="2026-06-01T00:00:00+00:00", step_min=5, ver="mc-v1"):
    from datetime import datetime, timedelta
    t0 = datetime.fromisoformat(start)
    return [{"computed_at": (t0 + timedelta(minutes=i * step_min)).isoformat(),
             "home_wp": 0.5, "away_wp": 0.5, "model_version": ver} for i in range(n)]


def test_downsample_thins_to_max_without_recent():
    h = _hist(1000)
    out = _downsample_history(h, max_points=200)
    assert len(out) == 200
    assert out[0] == h[0] and out[-1] == h[-1]   # endpoints preserved


def test_downsample_keeps_recent_window_at_full_resolution():
    # 1000 5-min points; keep everything on/after the cutoff un-thinned.
    h = _hist(1000)
    cutoff = h[900]["computed_at"]               # last 100 points are "recent"
    out = _downsample_history(h, max_points=200, recent_since=cutoff)
    recent = [p for p in out if p["computed_at"] >= cutoff]
    older = [p for p in out if p["computed_at"] < cutoff]
    assert len(recent) == 100                    # recent kept in full (5-min)
    assert len(older) <= 200                      # older thinned
    assert out == sorted(out, key=lambda p: p["computed_at"])
    # consecutive recent points stay 5 min apart (granular hover)
    from datetime import datetime
    gaps = {int((datetime.fromisoformat(recent[i + 1]["computed_at"])
                 - datetime.fromisoformat(recent[i]["computed_at"])).total_seconds())
            for i in range(len(recent) - 1)}
    assert gaps == {300}
