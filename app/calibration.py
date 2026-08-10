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
            skip_long: bool = False) -> list[dict]:
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
                for stat in COUNTING_CATS:
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
