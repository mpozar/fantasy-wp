"""Start-of-week projection accuracy: projected vs settled actual, per category.

One implementation, two consumers — `scripts/calibration.py` (the human-facing
report, with CIs and trends) and `validate.check_calibration` (the recurring
alarm). Keeping the measurement here is deliberate: `INV_SITE_QS_OVERCREDIT`
became a 1830-false-positive generator precisely because it was a *second*
implementation of publish's rule that didn't gain a path publish had gained.

Only COUNTING categories. The rate cats' `category_wp[].{home,away}_avg` is an
internal derived scale, not the displayed rate (playbook #11), so a
projected-vs-actual comparison on them is meaningless without reconstructing
from components first.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from app.mlb import LONG_MATCHUPS, matchup_period_window

# H, R, HR, SB, K, QS, SVHD — see app/stats.py for the canonical map.
COUNTING_CATS = [1, 20, 5, 23, 48, 63, 83]

# OPS, ERA, WHIP. Measurable after all (verified 2026-08-10): playbook #11 said
# `category_wp[].{home,away}_avg` is "an internal/derived scale (OPS showed
# ~1.0-1.6, not ~.800)" for these, which is why they were excluded from every
# measurement. It is NOT true of the current model — checked against the settled
# value at each decided week's FINAL snapshot, where the projection must equal the
# actual, the ratio is 1.0000 (sd 0.001, n=24 per cat), and pre-play values sit in
# plausible display ranges (OPS .750-.805, ERA 3.36-3.88, WHIP 1.14-1.29). Treat
# the old note as another unverified premise that carried a decision.
#
# They need DIFFERENT aggregation from counting cats: Σproj/Σactual is meaningless
# for a ratio, so `rate_summary` reports level bias, MAE and — the finding that
# matters here — DISCRIMINATION.
RATE_CATS = [18, 47, 41]
REVERSED_RATE_CATS = {47, 41}       # ERA/WHIP: lower is better

# Weeks 1-9 have no usable pre-play snapshot (wp_snapshots start 2026-05-28,
# mid-period-9), so every consumer's sample opens at period 10.
FIRST_PERIOD = 10


def first_pitch(conn: sqlite3.Connection, period: int) -> str:
    """Earliest observed first pitch of the period. Falls back to Monday 16:00
    UTC — before any MLB game — when `game_day_activity` never tracked it."""
    row = conn.execute(
        "SELECT MIN(active_start) a FROM game_day_activity WHERE matchup_period_id=?",
        (period,)).fetchone()
    if row and row["a"]:
        return row["a"]
    return f"{matchup_period_window(period)[0].isoformat()}T16:00:00+00:00"


def preplay_snapshot(conn: sqlite3.Connection, matchup_id: int,
                     fp: str) -> sqlite3.Row | None:
    """The last snapshot strictly before first pitch — the most-informed forecast
    that still saw zero play (announced probables in, cadence on, nothing banked).
    Hand-edited rows are excluded; their WP columns are deliberately smoothed."""
    return conn.execute(
        """SELECT computed_at, details_json FROM wp_snapshots
           WHERE matchup_id=? AND computed_at < ? AND details_json IS NOT NULL
             AND edited=0
           ORDER BY computed_at DESC LIMIT 1""", (matchup_id, fp)).fetchone()


def collect(conn: sqlite3.Connection, *, first_period: int = FIRST_PERIOD,
            periods: list[int] | None = None,
            skip_long: bool = False,
            stats: list[int] | None = None) -> list[dict]:
    """One row per (period, matchup, side, stat): projected vs settled actual.

    Restricted to periods with a decided winner — an unsettled week's "actual"
    is a partial total and would read as a huge over-projection.

    `skip_long` drops `LONG_MATCHUPS` (the 2-week All-Star period). Not
    cosmetic: a fortnight is not comparable to a week in a week-over-week
    series — twice the games, different variance, and a *known* +44% rotation
    artifact (both rotation models walk straight through the break's ~3 gameless
    days). Left IN for the human report, where it's flagged and informative, and
    taken OUT for the recurring jump check, where it fired twice on exactly that
    known artifact.
    """
    from app import db as appdb

    wanted = COUNTING_CATS if stats is None else stats
    if periods is None:
        periods = [r["p"] for r in conn.execute(
            """SELECT DISTINCT matchup_period_id p FROM matchups
               WHERE winner IN ('HOME','AWAY') AND matchup_period_id >= ?
               ORDER BY p""", (first_period,))]
    obs: list[dict] = []
    for period in periods:
        if skip_long and period in LONG_MATCHUPS:
            continue
        fp = first_pitch(conn, period)
        for m in conn.execute(
            """SELECT id, home_team_id, away_team_id FROM matchups
               WHERE matchup_period_id=? ORDER BY id""", (period,)):
            snap = preplay_snapshot(conn, m["id"], fp)
            if snap is None:
                continue
            cw = {c["stat_id"]: c for c in
                  json.loads(snap["details_json"]).get("category_wp") or []}
            for side in ("home", "away"):
                team_id = m[f"{side}_team_id"]
                actual = appdb.latest_category_state(conn, m["id"], team_id)
                for stat in wanted:
                    c = cw.get(stat)
                    if c is None or stat not in actual:
                        continue
                    proj, act = c.get(f"{side}_avg"), actual[stat].get("score")
                    if proj is None or act is None:
                        continue
                    obs.append({"period": period, "matchup_id": m["id"],
                                "side": side, "team_id": team_id, "stat": stat,
                                "proj": float(proj), "actual": float(act)})
    return obs


def ratio(rows: list[dict]) -> float | None:
    """Aggregate Σproj / Σactual. Deliberately not the mean of per-observation
    percent errors: QS/SVHD/SB actuals are legitimately 0 in some team-weeks,
    which makes a per-observation percentage undefined or explosive."""
    sa = sum(r["actual"] for r in rows)
    if sa <= 0:
        return None
    return sum(r["proj"] for r in rows) / sa


def bias(rows: list[dict]) -> float | None:
    """`ratio` expressed as a signed bias (0.0 = perfectly calibrated)."""
    r = ratio(rows)
    return None if r is None else r - 1.0


def by_stat(rows: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["stat"]].append(r)
    return out


def rate_summary(rows: list[dict]) -> dict | None:
    """Accuracy of one RATE category. A ratio can't be summed, so instead of
    Σproj/Σactual this reports:

      * `level` — mean projected minus mean actual, in the category's own units
        (ERA points, OPS points). The level bias.
      * `mae` — mean absolute error, i.e. how far a single team-week's forecast
        typically lands.
      * `sd_proj` / `sd_actual` — DISCRIMINATION, and the interesting number here.
        A projection whose spread is far below the outcome's spread is heavily
        regressed to the mean: unbiased on average yet unable to tell a good
        pitching week from a bad one, which is invisible to any bias metric.
      * `corr` — Pearson correlation of projected vs actual across team-weeks.
        The direct measure of whether the forecast ranks teams correctly at all.

    Team-weeks with a zero/absent actual are dropped: an ERA of 0.00 over 0 outs
    is not a good week, it is a team that hasn't pitched.
    """
    pairs = [(r["proj"], r["actual"]) for r in rows
             if r["actual"] and r["actual"] > 0 and r["proj"] and r["proj"] > 0]
    if len(pairs) < 3:
        return None
    import statistics
    p = [x[0] for x in pairs]
    a = [x[1] for x in pairs]
    mp, ma = statistics.fmean(p), statistics.fmean(a)
    sp, sa = statistics.pstdev(p), statistics.pstdev(a)
    cov = statistics.fmean((pi - mp) * (ai - ma) for pi, ai in pairs)
    corr = (cov / (sp * sa)) if sp > 0 and sa > 0 else None
    return {
        "n": len(pairs), "mean_proj": mp, "mean_actual": ma,
        "level": mp - ma, "rel": (mp / ma - 1.0) if ma else None,
        "mae": statistics.fmean(abs(pi - ai) for pi, ai in pairs),
        "sd_proj": sp, "sd_actual": sa,
        "spread_ratio": (sp / sa) if sa > 0 else None, "corr": corr,
    }


def weekly_bias(rows: list[dict]) -> dict[int, dict[int, float]]:
    """{stat_id: {period: signed bias}} — the per-week series both the report's
    trend column and the recurring jump check read."""
    out: dict[int, dict[int, float]] = {}
    for stat, srows in by_stat(rows).items():
        per_week: dict[int, float] = {}
        buckets: dict[int, list[dict]] = defaultdict(list)
        for r in srows:
            buckets[r["period"]].append(r)
        for wk in sorted(buckets):
            b = bias(buckets[wk])
            if b is not None:
                per_week[wk] = b
        out[stat] = per_week
    return out
