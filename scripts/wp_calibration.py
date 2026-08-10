#!/usr/bin/env python
"""Offline tool: is the published win probability CALIBRATED?

`calibration.py` measures the model's *inputs* (projected vs actual category
totals). This measures its *output*: when the site says 70%, does that side win
70% of the time?

Two levels, deliberately, because they have very different statistical power:

  MATCHUP level  — n≈54. One observation per settled matchup (home side only:
                   away_wp ≈ 1 − home_wp, so scoring both sides double-counts
                   and shrinks every CI by √2 for free). Enough to detect gross
                   miscalibration, not subtle. Reported with honest CIs.

  CATEGORY level — n≈540 (10 cats × 54 matchups), clustered within matchup.
                   ~10× the power, and it localises miscalibration to specific
                   categories. This is where the useful signal is.

The headline is not the Brier score — a Brier score alone is meaningless without
a baseline, so it's reported as a SKILL SCORE against always-saying-50%, plus
`sharpness` (does the model commit at all?) and a calibration slope.

Reading the slope (regression of outcome on predicted probability):
    slope ≈ 1   calibrated
    slope < 1   OVERCONFIDENT — predictions are too extreme, shrink them
    slope > 1   UNDER-DISPERSED — too timid, the model knows more than it says

That last case is the open question from the rate-category measurement: OPS/ERA/
WHIP projections vary at only 11-18% of the outcome's spread, which is either an
honestly weak forecast (fine) or a homogenised one (fixable). A slope > 1 on the
rate cats is the evidence that distinguishes them.

Read-only.

    .venv/bin/python scripts/wp_calibration.py [--db data.db] [--reps 4000]
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
from collections import defaultdict

from app import calibration as calib
from app import stats as appstats


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ── data ─────────────────────────────────────────────────────────────────

def collect(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """(matchup-level, category-level) pre-play forecast/outcome pairs."""
    from app import db as appdb

    matchup_rows: list[dict] = []
    cat_rows: list[dict] = []
    periods = [r["p"] for r in conn.execute(
        """SELECT DISTINCT matchup_period_id p FROM matchups
           WHERE winner IN ('HOME','AWAY') AND matchup_period_id >= ?
           ORDER BY p""", (calib.FIRST_PERIOD,))]

    for period in periods:
        fp = calib.first_pitch(conn, period)
        for m in conn.execute(
            """SELECT id, home_team_id, away_team_id, winner FROM matchups
               WHERE matchup_period_id=? ORDER BY id""", (period,)):
            snap = calib.preplay_snapshot(conn, m["id"], fp)
            if snap is None:
                continue
            row = conn.execute(
                "SELECT home_wp FROM wp_snapshots WHERE matchup_id=? AND computed_at=?",
                (m["id"], snap["computed_at"])).fetchone()
            if row is None:
                continue
            matchup_rows.append({
                "period": period, "matchup_id": m["id"],
                "p": float(row["home_wp"]),
                "y": 1.0 if m["winner"] == "HOME" else 0.0,
            })

            d = json.loads(snap["details_json"])
            n_sims = d.get("n_sims") or 0
            home_state = appdb.latest_category_state(conn, m["id"], m["home_team_id"])
            for c in d.get("category_wp") or []:
                sid = c["stat_id"]
                res = (home_state.get(sid) or {}).get("result")
                if res not in ("WIN", "LOSS", "TIE") or not n_sims:
                    continue
                # Ties get half credit on BOTH sides of the comparison so the
                # forecast and the outcome are coded on the same scale.
                p = (c.get("home_wins", 0) + 0.5 * c.get("ties", 0)) / n_sims
                y = {"WIN": 1.0, "LOSS": 0.0, "TIE": 0.5}[res]
                cat_rows.append({"period": period, "matchup_id": m["id"],
                                 "stat": sid, "p": p, "y": y})
    return matchup_rows, cat_rows


# ── metrics ──────────────────────────────────────────────────────────────

def brier(rows: list[dict]) -> float:
    return statistics.fmean((r["p"] - r["y"]) ** 2 for r in rows)


def skill(rows: list[dict]) -> float:
    """Brier skill score vs always predicting 0.5. >0 beats a coin flip."""
    ref = statistics.fmean((0.5 - r["y"]) ** 2 for r in rows)
    return 1.0 - brier(rows) / ref if ref > 0 else 0.0


def sharpness(rows: list[dict]) -> float:
    """Mean distance from 50% — does the forecast commit? A perfectly calibrated
    model that always says 50% has sharpness 0 and is useless."""
    return statistics.fmean(abs(r["p"] - 0.5) for r in rows)


def slope_intercept(rows: list[dict]) -> tuple[float, float] | None:
    """OLS of outcome on predicted probability. slope<1 overconfident,
    slope>1 under-dispersed (too timid)."""
    xs = [r["p"] for r in rows]
    ys = [r["y"] for r in rows]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return b, my - b * mx


def auc(rows: list[dict]) -> float | None:
    """Rank-based concordance, ties at half credit. Does a higher forecast
    actually pick the winner more often? Separates 'ranks fine but
    miscalibrated' from 'no signal at all'."""
    pos = [r["p"] for r in rows if r["y"] > 0.5]
    neg = [r["p"] for r in rows if r["y"] < 0.5]
    if not pos or not neg:
        return None
    wins = sum((1.0 if a > b else 0.5 if a == b else 0.0)
               for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def cluster_boot(rows: list[dict], fn, reps: int, seed: int = 11,
                 level: float = 0.90) -> tuple[float, float] | None:
    """Bootstrap CI resampling MATCHUPS — the clustering unit, since the 10
    categories inside a matchup are strongly correlated."""
    by_m: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_m[r["matchup_id"]].append(r)
    keys = sorted(by_m)
    if len(keys) < 5:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        draw = [x for _ in keys for x in by_m[rng.choice(keys)]]
        try:
            v = fn(draw)
        except (ZeroDivisionError, statistics.StatisticsError):
            v = None
        if v is not None:
            vals.append(v)
    if len(vals) < reps // 2:
        return None
    vals.sort()
    return (vals[int((1 - level) / 2 * len(vals))],
            vals[int((1 + level) / 2 * len(vals)) - 1])


def reliability(rows: list[dict], edges=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0)):
    out = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [r for r in rows if lo <= r["p"] < hi or (hi == 1.0 and r["p"] == 1.0)]
        if sel:
            out.append((lo, hi, len(sel), statistics.fmean(r["p"] for r in sel),
                        statistics.fmean(r["y"] for r in sel)))
    return out


def _fmt_ci(ci) -> str:
    return f"[{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else "—"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data.db")
    ap.add_argument("--reps", type=int, default=4000)
    ap.add_argument("--split-at", type=int, default=14, metavar="PERIOD",
                    help="Also report an early-vs-late era split at this period. "
                         "Default 14 (Jun 29): the QS/SVHD correctness fixes "
                         "cluster before it — Binomial-not-Poisson 06-07, QS "
                         "max-not-add 06-08, SVHD entry/exit margins + "
                         "spot-starter skip 06-10, relief-SVHD smear 07-03.")
    args = ap.parse_args()

    conn = _connect(args.db)
    mrows, crows = collect(conn)
    if not mrows:
        print("No settled matchup has a pre-play snapshot.")
        return

    print("Win-probability calibration — pre-play forecast vs settled outcome")
    print(f"  forecast = last snapshot before first pitch; CIs 90%, "
          f"bootstrap clustered by matchup ({args.reps} reps)\n")

    # ── matchup level ────────────────────────────────────────────────────
    weeks = sorted({r["period"] for r in mrows})
    print(f"MATCHUP level — n={len(mrows)} (home side only), periods "
          f"{weeks[0]}..{weeks[-1]}")
    si = slope_intercept(mrows)
    print(f"  base rate (home win)   {statistics.fmean(r['y'] for r in mrows):.3f}")
    print(f"  mean forecast          {statistics.fmean(r['p'] for r in mrows):.3f}")
    print(f"  Brier                  {brier(mrows):.4f}   "
          f"(always-50% = 0.2500)")
    print(f"  skill vs 50%           {skill(mrows):+.3f}   "
          f"CI {_fmt_ci(cluster_boot(mrows, skill, args.reps))}")
    print(f"  AUC                    {(auc(mrows) or 0):.3f}   "
          f"CI {_fmt_ci(cluster_boot(mrows, auc, args.reps))}")
    print(f"  sharpness              {sharpness(mrows):.3f}   "
          f"(0 = never commits, .5 = always 0/100)")
    if si:
        print(f"  calib slope / intercept {si[0]:+.3f} / {si[1]:+.3f}   "
              f"(slope<1 overconfident, >1 too timid)")
    print("  reliability:")
    for lo, hi, n, mp, my in reliability(mrows):
        print(f"    forecast {lo:.0%}-{hi:.0%}  n={n:>3}  mean pred {mp:.3f}  "
              f"observed {my:.3f}")

    # ── category level ───────────────────────────────────────────────────
    print(f"\nCATEGORY level — n={len(crows)} ({len(crows)//10 if crows else 0} "
          f"matchups × 10 cats), clustered by matchup")
    si = slope_intercept(crows)
    print(f"  Brier                  {brier(crows):.4f}")
    print(f"  skill vs 50%           {skill(crows):+.3f}   "
          f"CI {_fmt_ci(cluster_boot(crows, skill, args.reps))}")
    print(f"  AUC                    {(auc(crows) or 0):.3f}")
    print(f"  sharpness              {sharpness(crows):.3f}")
    if si:
        print(f"  calib slope / intercept {si[0]:+.3f} / {si[1]:+.3f}")
    print("  reliability:")
    for lo, hi, n, mp, my in reliability(crows):
        print(f"    forecast {lo:.0%}-{hi:.0%}  n={n:>3}  mean pred {mp:.3f}  "
              f"observed {my:.3f}")

    # Counting vs rate — the split that answers the under-dispersion question.
    print("\n  by category (skill vs 50%, and the slope that says which way "
          "it's wrong):")
    print(f"    {'cat':>5} {'n':>4} {'sharp':>6} {'skill':>7} {'slope':>7} "
          f"{'AUC':>6}  verdict")
    by_stat: dict[int, list[dict]] = defaultdict(list)
    for r in crows:
        by_stat[r["stat"]].append(r)
    for stat in calib.COUNTING_CATS + calib.RATE_CATS:
        rows = by_stat.get(stat) or []
        if len(rows) < 10:
            continue
        si = slope_intercept(rows)
        sl = si[0] if si else float("nan")
        verdict = ("too timid" if sl > 1.25 else
                   "overconfident" if sl < 0.75 else "ok")
        kind = "rate" if stat in calib.RATE_CATS else ""
        print(f"    {appstats.name(stat):>5} {len(rows):>4} "
              f"{sharpness(rows):>6.3f} {skill(rows):>+7.3f} {sl:>+7.2f} "
              f"{(auc(rows) or 0):>6.3f}  {verdict} {kind}")

    for label, sids in (("counting", calib.COUNTING_CATS),
                        ("rate    ", calib.RATE_CATS)):
        rows = [r for r in crows if r["stat"] in sids]
        if not rows:
            continue
        si = slope_intercept(rows)
        ci = cluster_boot(rows, lambda rr: (slope_intercept(rr) or (None,))[0],
                          args.reps)
        print(f"    {label} cats pooled: n={len(rows)}, "
              f"sharpness {sharpness(rows):.3f}, skill {skill(rows):+.3f}, "
              f"slope {(si[0] if si else 0):+.2f} CI {_fmt_ci(ci)}")

    # ── era split: is a bad category actually a bad ERA? ────────────────
    # Several correctness fixes landed INSIDE this window, and the ones that
    # produced confidently-wrong output cluster early (see --split-at help). A
    # category whose skill is dominated by the buggy era is not evidence about
    # the model as it stands today. LONG_MATCHUPS excluded: not comparable.
    from app.mlb import LONG_MATCHUPS
    early = [r for r in crows
             if r["period"] < args.split_at and r["period"] not in LONG_MATCHUPS]
    late = [r for r in crows
            if r["period"] >= args.split_at and r["period"] not in LONG_MATCHUPS]
    if early and late:
        print(f"\n  ERA SPLIT at period {args.split_at} "
              f"(pre-fix n={len(early)} / post n={len(late)}) — a slope near 0 "
              f"means\n  the forecast carried no information at all:")
        print(f"    {'cat':>5} {'sharp≺':>7} {'sharp≻':>7} {'skill≺':>8} "
              f"{'skill≻':>8} {'slope≺':>7} {'slope≻':>7}")
        for stat in calib.COUNTING_CATS + calib.RATE_CATS:
            e = [r for r in early if r["stat"] == stat]
            l = [r for r in late if r["stat"] == stat]
            if len(e) < 8 or len(l) < 8:
                continue
            se = slope_intercept(e)
            sl = slope_intercept(l)
            print(f"    {appstats.name(stat):>5} {sharpness(e):>7.3f} "
                  f"{sharpness(l):>7.3f} {skill(e):>+8.3f} {skill(l):>+8.3f} "
                  f"{(se[0] if se else 0):>+7.2f} {(sl[0] if sl else 0):>+7.2f}")
        for label, rows in (("pre ", early), ("post", late)):
            si = slope_intercept(rows)
            ci = cluster_boot(rows,
                              lambda rr: (slope_intercept(rr) or (None,))[0],
                              args.reps)
            print(f"    pooled {label}: skill {skill(rows):+.3f}, "
                  f"slope {(si[0] if si else 0):+.2f} CI {_fmt_ci(ci)}")
        print("    ≺ = before the split period, ≻ = from it onward. n per cell is")
        print("    ~24, so read these as directional, not conclusive.")

    print("\n  Caveat: scores the model AS IT RAN. Weeks 10-18 predate the "
          "2026-08-10 fixes")
    print("  (RP denominator, QS rate, SVHD rate) entirely — even the 'post' era "
          "above is")
    print("  pre-those — and it cannot be re-simmed: player_projections had no "
          "period key.")
    print("  `ros_projection_archive` fixes that from period 19 forward.")


if __name__ == "__main__":
    main()
