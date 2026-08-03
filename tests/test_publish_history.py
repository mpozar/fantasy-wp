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


def _gaps_min(rows):
    from datetime import datetime
    return {int((datetime.fromisoformat(rows[i + 1]["computed_at"])
                 - datetime.fromisoformat(rows[i]["computed_at"])).total_seconds() / 60)
            for i in range(len(rows) - 1)}


def test_downsample_thins_to_max_without_recent():
    h = _hist(1000)
    out = _downsample_history(h, max_points=200, recent_hours=0, grid_minutes=0)
    assert len(out) == 200
    assert out[0] == h[0] and out[-1] == h[-1]   # endpoints preserved


def test_downsample_keeps_recent_window_at_full_resolution():
    # 1000 5-min points; keep everything on/after the cutoff un-thinned.
    h = _hist(1000)
    cutoff = h[900]["computed_at"]               # last 100 points are "recent"
    out = _downsample_history(h, max_points=200, recent_since=cutoff, grid_minutes=0)
    recent = [p for p in out if p["computed_at"] >= cutoff]
    older = [p for p in out if p["computed_at"] < cutoff]
    assert len(recent) == 100                    # recent kept in full (5-min)
    assert len(older) <= 200                      # older thinned
    assert out == sorted(out, key=lambda p: p["computed_at"])
    # consecutive recent points stay 5 min apart (granular hover)
    assert _gaps_min(recent) == {5}


# The 2026-08-03 regressions: week 17's −78pp settle cliff vanished from the
# published chart because a finished week thinned its WHOLE series to 200
# evenly-spaced points (~55-min steps, a value nobody chose).

def test_downsample_recent_window_is_derived_from_the_series_not_now():
    # A week that ended long ago (no `recent_since` passed) still keeps its own
    # final 24h at raw 5-min resolution — that's where settle cliffs live.
    from datetime import datetime, timedelta
    h = _hist(2016, start="2026-07-27T00:00:00+00:00")     # a full 7-day week
    out = _downsample_history(h)
    cutoff = datetime.fromisoformat(h[-1]["computed_at"]) - timedelta(hours=24)
    tail = [p for p in out if datetime.fromisoformat(p["computed_at"]) >= cutoff]
    assert _gaps_min(tail) == {5}
    assert len(tail) == 24 * 12 + 1      # 24h of five-minute points, inclusive
    assert out[-1] == h[-1]


def test_downsample_older_history_lands_on_a_round_grid():
    # Older points snap to a round wall-clock cadence, not span/N (the ~55-min
    # artifact): every gap is a multiple of the grid, and none is 55 min.
    from datetime import datetime, timedelta
    h = _hist(2016, start="2026-07-27T00:00:00+00:00")
    out = _downsample_history(h, grid_minutes=15)
    cutoff = datetime.fromisoformat(h[-1]["computed_at"]) - timedelta(hours=24)
    older = [p for p in out if datetime.fromisoformat(p["computed_at"]) < cutoff]
    assert _gaps_min(older) == {15}
    assert all(datetime.fromisoformat(p["computed_at"]).minute % 15 == 0 for p in older)


def test_downsample_grid_is_stable_across_republishes():
    # publish --rebuild runs daily over every week; the same input must give the
    # same output (a wall-clock window would slide and re-thin the detail).
    h = _hist(2016, start="2026-07-27T00:00:00+00:00")
    assert _downsample_history(h) == _downsample_history(list(h))


def test_downsample_caps_category_wp_but_not_the_wp_line():
    h = _hist(2016, start="2026-07-27T00:00:00+00:00")
    for p in h:
        p["category_wp"] = [{"stat_id": 48, "home_wins": 1, "away_wins": 1, "ties": 0}]
        p["n_sims"] = 10000
    out = _downsample_history(h, max_cat_points=50)
    with_cat = [p for p in out if "category_wp" in p]
    assert len(out) > 500                      # WP line stays fine-grained
    assert len(with_cat) == 50                 # category history stays bounded
    assert "n_sims" not in out[1] or "category_wp" in out[1]   # stripped together
    assert "category_wp" in out[-1]            # newest state always kept
