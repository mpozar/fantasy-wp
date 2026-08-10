#!/usr/bin/env python
"""Offline tool: how accurate are START-OF-WEEK category projections?

Read-only. For every settled matchup period that has a genuine pre-play
snapshot, compares the projected end-of-week total for each COUNTING category
against the settled actual, and reports per-category bias.

Why this measurement and not WP calibration: only ~54 settled matchups have a
start-of-week forecast (snapshots begin 2026-05-28), so binary WP calibration
has very little power. Projected-vs-actual totals give ~108 continuous
observations per category and localize *which* category is biased — the class
of defect that the 2026-08-10 RP-appearance inflation belonged to.

Headline metric is the AGGREGATE ratio Σprojected / Σactual per category, not
the mean of per-observation percent errors: QS/SVHD/SB actuals are legitimately
0 in some team-weeks, which makes a per-observation percentage undefined or
explosive. Confidence intervals come from a bootstrap CLUSTERED BY WEEK — the
correlation that matters is a league-wide hot/cold week, and with 9 weeks the
week-clustered interval is the honest (wide) one.

Two structural caveats, printed with the output so they can't be lost:
  * The historical sample scores the model *as it actually ran*. Weeks 10-18
    span several since-fixed bugs (doubleheader units 07-11, phantom-schedule
    guard 06-18/06-24, promoted starters 06-28, relief SVHD 07-03, and the RP
    denominator 08-10). It is a pessimistic read on today's model, and it
    cannot be re-simmed: `player_projections` has no period key, so the ROS
    inputs from those weeks are gone.
  * Rate categories (OPS/ERA/WHIP) are EXCLUDED. `category_wp[].{home,away}_avg`
    is an internal derived scale for them, not the displayed rate.

Usage:
    .venv/bin/python scripts/calibration.py [--db data.db] [--reps 4000]
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
from collections import defaultdict

from app import db as appdb
from app import stats as appstats
from app.mlb import matchup_period_window

# Counting cats only — rate cats' stored avg is an internal scale (playbook #11).
COUNTING_CATS = [1, 20, 5, 23, 48, 63, 83]      # H, R, HR, SB, K, QS, SVHD
HITTER_CATS = {1, 20, 5, 23}
LONG_PERIODS = {15}                              # All-Star: 2 weeks, not 1

# Weeks 1-9 have no usable pre-play snapshot (snapshots start 2026-05-28,
# mid-period-9), so the sample opens at period 10.
FIRST_PERIOD = 10


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _first_pitch(conn: sqlite3.Connection, period: int) -> str | None:
    """Earliest observed first pitch of the period, from game_day_activity.
    Falls back to Monday 16:00 UTC (before any MLB game) if untracked."""
    row = conn.execute(
        "SELECT MIN(active_start) a FROM game_day_activity WHERE matchup_period_id=?",
        (period,),
    ).fetchone()
    if row and row["a"]:
        return row["a"]
    return f"{matchup_period_window(period)[0].isoformat()}T16:00:00+00:00"


def _preplay_snapshot(conn: sqlite3.Connection, matchup_id: int,
                      first_pitch: str) -> sqlite3.Row | None:
    """Last snapshot strictly before the week's first pitch — the most-informed
    forecast that still saw zero play. Hand-edited rows are skipped."""
    return conn.execute(
        """SELECT computed_at, details_json FROM wp_snapshots
           WHERE matchup_id=? AND computed_at < ? AND details_json IS NOT NULL
             AND edited=0
           ORDER BY computed_at DESC LIMIT 1""",
        (matchup_id, first_pitch),
    ).fetchone()


def collect(conn: sqlite3.Connection) -> list[dict]:
    """One row per (period, matchup, side, stat): projected vs actual total."""
    obs: list[dict] = []
    periods = [r["p"] for r in conn.execute(
        """SELECT DISTINCT matchup_period_id p FROM matchups
           WHERE winner IN ('HOME','AWAY') AND matchup_period_id >= ?
           ORDER BY p""", (FIRST_PERIOD,))]

    for period in periods:
        fp = _first_pitch(conn, period)
        for m in conn.execute(
            """SELECT id, home_team_id, away_team_id FROM matchups
               WHERE matchup_period_id=? ORDER BY id""", (period,)):
            snap = _preplay_snapshot(conn, m["id"], fp)
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
                    proj = c.get(f"{side}_avg")
                    act = actual[stat].get("score")
                    if proj is None or act is None:
                        continue
                    obs.append({
                        "period": period, "matchup_id": m["id"], "side": side,
                        "team_id": team_id, "stat": stat,
                        "proj": float(proj), "actual": float(act),
                        "snap": snap["computed_at"],
                    })
    return obs


def _ratio(rows: list[dict]) -> float | None:
    """Aggregate Σproj / Σactual — robust to zero actuals."""
    sa = sum(r["actual"] for r in rows)
    if sa <= 0:
        return None
    return sum(r["proj"] for r in rows) / sa


def _boot_ci(rows: list[dict], reps: int, seed: int = 12345,
             level: float = 0.90) -> tuple[float, float] | None:
    """Bootstrap CI on the aggregate ratio, resampling WEEKS (clusters)."""
    by_week: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_week[r["period"]].append(r)
    weeks = sorted(by_week)
    if len(weeks) < 3:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        draw: list[dict] = []
        for _ in weeks:
            draw.extend(by_week[rng.choice(weeks)])
        v = _ratio(draw)
        if v is not None:
            vals.append(v)
    if len(vals) < reps // 2:
        return None
    vals.sort()
    lo = vals[int((1 - level) / 2 * len(vals))]
    hi = vals[int((1 + level) / 2 * len(vals)) - 1]
    return lo, hi


def _trend(rows: list[dict]) -> float | None:
    """OLS slope of per-week aggregate ratio on week number, in ratio-points
    per week. A structural span/denominator bug grows over the season; a rate
    bias is flat. This is the shape that discriminates them."""
    per_week: list[tuple[float, float]] = []
    by_week: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_week[r["period"]].append(r)
    for wk in sorted(by_week):
        v = _ratio(by_week[wk])
        if v is not None:
            per_week.append((float(wk), v))
    if len(per_week) < 3:
        return None
    xs = [p[0] for p in per_week]
    ys = [p[1] for p in per_week]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data.db")
    ap.add_argument("--reps", type=int, default=4000)
    args = ap.parse_args()

    conn = _connect(args.db)
    obs = collect(conn)
    if not obs:
        print("No observations — no settled period has a pre-play snapshot.")
        return

    weeks = sorted({o["period"] for o in obs})
    matchups = {o["matchup_id"] for o in obs}
    print(f"Start-of-week category projection accuracy")
    print(f"  periods {weeks[0]}..{weeks[-1]} ({len(weeks)} weeks), "
          f"{len(matchups)} matchups, {len(obs)} observations")
    print(f"  forecast = last snapshot before the week's first pitch")
    print(f"  CI = 90%, bootstrap clustered by week ({args.reps} reps)\n")

    by_stat: dict[int, list[dict]] = defaultdict(list)
    for o in obs:
        by_stat[o["stat"]].append(o)

    print(f"  {'cat':>5} {'n':>4} {'Σproj':>8} {'Σact':>8} {'bias':>8} "
          f"{'90% CI':>16} {'MAE':>6} {'trend/wk':>9}")
    for stat in COUNTING_CATS:
        rows = by_stat.get(stat) or []
        if not rows:
            continue
        ratio = _ratio(rows)
        ci = _boot_ci(rows, args.reps)
        mae = statistics.fmean(abs(r["proj"] - r["actual"]) for r in rows)
        tr = _trend(rows)
        sp = sum(r["proj"] for r in rows)
        sa = sum(r["actual"] for r in rows)
        ci_s = f"[{(ci[0]-1):+.1%},{(ci[1]-1):+.1%}]" if ci else "—"
        print(f"  {appstats.name(stat):>5} {len(rows):>4} {sp:>8.0f} {sa:>8.0f} "
              f"{(ratio-1):>+7.1%} {ci_s:>16} {mae:>6.1f} "
              f"{(f'{tr:+.3f}' if tr is not None else '—'):>9}")

    # Per-week detail, so a regime shift stays visible rather than averaged away.
    print("\n  per-week bias (Σproj/Σact − 1):")
    hdr = "  " + "week ".rjust(7) + "".join(f"{appstats.name(s):>7}" for s in COUNTING_CATS)
    print(hdr)
    for wk in weeks:
        cells = []
        for stat in COUNTING_CATS:
            rows = [o for o in by_stat.get(stat, []) if o["period"] == wk]
            v = _ratio(rows)
            cells.append(f"{(v-1):>+6.0%}" + " " if v is not None else "     — ")
        tag = "*" if wk in LONG_PERIODS else " "
        print(f"  {wk:>5}{tag} " + "".join(c.rjust(7) for c in cells))
    if any(w in LONG_PERIODS for w in weeks):
        print("   * 2-week All-Star period (different length/variance)")

    # Unit-free ratio test: a hitter's lineup-days feed H/R/HR/SB identically,
    # so a common bias is UNITS (lineup optimism) while a per-cat deviation is
    # that cat's RATE. Ratios to H cancel units without needing units actuals.
    print("\n  hitter rate check (unit-free — cancels lineup-days):")
    for stat in (20, 5, 23):
        num = by_stat.get(stat) or []
        den = by_stat.get(1) or []
        key = lambda r: (r["matchup_id"], r["side"])
        dmap = {key(r): r for r in den}
        pr = ar = 0.0
        for r in num:
            d = dmap.get(key(r))
            if d and d["actual"] > 0 and d["proj"] > 0:
                pr += r["proj"] / d["proj"]
                ar += r["actual"] / d["actual"]
        if ar > 0:
            print(f"    {appstats.name(stat)}/H projected vs actual: "
                  f"{(pr/ar - 1):+.1%}")

    print("\n  Caveats: scores the model AS IT RAN (weeks 10-18 include several")
    print("  since-fixed bugs incl. the 08-10 RP denominator); cannot be re-simmed")
    print("  (player_projections has no period key). Rate cats excluded.")


if __name__ == "__main__":
    main()
