#!/usr/bin/env python
"""Offline tool: are extreme per-category win probabilities honest?

Read-only. Companion to `scripts/calibration.py`, which asks whether a projected
*total* is the right size. This one asks whether the *probabilities* built from
those totals hold up — specifically in the tail, where a category priced under
5% is the model claiming a matter is settled.

Why it is a separate tool: a level bias cancels head-to-head when both teams
carry similar volume and does not when they do not, so the same bias can read as
a benign +40% on the level metric while flipping the *sign* of a projected
margin. The 2026-08-10 tail investigation found exactly that on the pitching
counters — reliability was fine everywhere above 5% and 2.8x off below it.

Roster churn is separated rather than assumed away. Projections condition on the
current roster, so a mid-week streaming add is a documented limitation, not a
modelling error; every table reports the churn-free residual alongside the raw
number. The filter is generous (it excuses a forecast whether or not the
newcomer produced), so the churn-free column is a LOWER bound on model error.

Three measurements, three different defects — read them together:
  * reliability     — is a stated 1% actually a 1%?
  * margin slope    — < 1 means the model claims a bigger gap than materialises
                      (over-differentiates teams; the between-matchup defect)
  * dispersion      — > 1 means the simulated week is too narrow relative to the
                      errors the model actually makes (the within-week defect)

Caveats printed with the output so they cannot be lost:
  * Scores the model AS IT RAN. Periods 10-18 predate the 2026-08-10 QS/SVHD
    rate blends, and the weeks cannot be re-simmed (`player_projections` has no
    period key). Read QS/SVHD as pessimistic and K as current.
  * The reliability curve is snapshot-weighted, so a single long-lived tail
    state contributes hundreds of forecasts. The unit table underneath it is the
    de-duplicated restatement; trust the CIs, which are clustered by matchup.

Usage:
    .venv/bin/python scripts/tail_calibration.py [--db data.db] [--reps 2000]
                                                 [--from-period 10] [--to-period N]
Takes ~35s over periods 10-18: it parses every snapshot's details_json once.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict

from app import stats as appstats
from app import tail_calibration as tc

# Display order: batting then pitching, matching the scoreboard.
ORDER = [1, 20, 5, 23, 18, 48, 63, 47, 41, 83]


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_ci(ci) -> str:
    return f"[{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "—"


def reliability_curve(res: tc.Result) -> None:
    print("\n  Reliability — every in-week forecast, all ten categories")
    print(f"  {'band':>15} {'n':>9} {'predicted':>10} {'observed':>9} "
          f"{'obs/pred':>9} {'excess wins':>12}")
    for i in range(len(tc.EDGES) - 1):
        b = res.bins.get((None, tc.ALL, i))
        if not b or not b[0]:
            continue
        n, sp, w = b
        pred, obs = sp / n, w / n
        ratio = obs / pred if pred > 0 else float("nan")
        print(f"  {tc.EDGES[i]:>6.3f}-{tc.EDGES[i+1]:<8.3f} {n:>9,} {pred:>10.4f} "
              f"{obs:>9.4f} {ratio:>9.2f} {w - sp:>+12.0f}")


def tail_bands(res: tc.Result, reps: int) -> None:
    print("\n  Low tail by band — CI is 90%, bootstrap clustered by matchup")
    print(f"  {'band':>15} {'n':>9} {'exp':>8} {'obs':>7} {'ratio':>7} "
          f"{'90% CI':>16}   {'churn-free ratio':>17}")
    for i in range(len(tc.EDGES) - 1):
        if tc.EDGES[i] >= 0.20:
            break
        rows = []
        for scope in (tc.ALL, tc.CLEAN):
            cl = res.clusters.get((None, scope, i))
            rows.append(tc.cluster_ratio(cl) if cl else None)
        if not rows[0]:
            continue
        ratio, sp, sw = rows[0]
        ci = tc.cluster_ratio_ci(res.clusters[(None, tc.ALL, i)], reps=reps)
        clean = f"{rows[1][0]:.2f}" if rows[1] else "—"
        n = res.bins[(None, tc.ALL, i)][0]
        print(f"  {tc.EDGES[i]:>6.3f}-{tc.EDGES[i+1]:<8.3f} {n:>9,} {sp:>8.1f} "
              f"{sw:>7.0f} {ratio:>7.2f} {_fmt_ci(ci):>16}   {clean:>17}")
    for scope, label in ((tc.ALL, "all forecasts"), (tc.CLEAN, "churn-free")):
        cl = res.clusters.get((None, scope, "tail"))
        if not cl:
            continue
        ratio, sp, sw = tc.cluster_ratio(cl)
        ci = tc.cluster_ratio_ci(cl, reps=reps)
        print(f"  {'p < 0.05':>15} {label:>9}  exp={sp:>8.1f} obs={sw:>6.0f} "
              f"ratio={ratio:>5.2f}  90% CI {_fmt_ci(ci)}")


def tail_by_category(res: tc.Result, reps: int) -> None:
    print("\n  Low tail (p < 0.05) by category")
    print(f"  {'cat':>5} {'n':>8} {'exp':>7} {'obs':>6} {'ratio':>7} "
          f"{'churn-free':>11} {'90% CI (churn-free)':>21}")
    for sid in ORDER:
        a = res.clusters.get((sid, tc.ALL, "tail"))
        c = res.clusters.get((sid, tc.CLEAN, "tail"))
        if not a:
            continue
        ra = tc.cluster_ratio(a)
        rc = tc.cluster_ratio(c) if c else None
        n = sum(res.bins[(sid, tc.ALL, i)][0] for i in range(len(tc.EDGES) - 1)
                if tc.EDGES[i] < tc.TAIL_HI and (sid, tc.ALL, i) in res.bins)
        ci = tc.cluster_ratio_ci(c, reps=reps) if c else None
        print(f"  {appstats.name(sid):>5} {n:>8,} {ra[1]:>7.1f} {ra[2]:>6.0f} "
              f"{ra[0]:>7.2f} {(f'{rc[0]:.2f}' if rc else '—'):>11} "
              f"{_fmt_ci(ci):>21}")


def margin_slope(res: tc.Result, reps: int) -> None:
    print("\n  Margin slope — settled margin regressed on projected margin,")
    print("  through the origin, churn-free checkpoints only.")
    print("  1.00 = the projected gap is the right size. 0.60 = the model")
    print("  routinely claims a gap ~40% bigger than the one that arrives.")
    pairs: dict[int, list] = defaultdict(list)
    for r in res.checkpoints:
        if r["churn_free"]:
            pairs[r["stat"]].append((r["proj_margin"], r["actual_margin"]))
    print(f"\n  {'cat':>5} {'n':>4} {'slope':>7} {'90% CI':>16} "
          f"{'mean |proj gap|':>16} {'gap / team total':>17}")
    for sid in ORDER:
        d = pairs.get(sid) or []
        s = tc.slope_through_origin(d)
        if s is None or len(d) < 10:
            continue
        ci = tc.slope_ci(d, reps=reps)
        mg = statistics.fmean(abs(p) for p, _ in d)
        tot, tn = res.totals.get(sid, [0.0, 0])
        gap, gn = res.gaps.get(sid, [0.0, 0])
        rel = (f"{(gap/gn)/(tot/tn):>16.0%}" if tn and gn and tot else
               f"{'—':>16}")
        print(f"  {appstats.name(sid):>5} {len(d):>4} {s:>7.2f} {_fmt_ci(ci):>16} "
              f"{mg:>16.2f} {rel:>17}")
    print("\n  'gap / team total' is why the level bias cancels for some cats and")
    print("  not others: a shared multiplicative bias cancels head-to-head only")
    print("  when both teams carry similar volume.")


def dispersion(res: tc.Result) -> None:
    print("\n  Dispersion — how wide the errors really are, in units of the")
    print("  sim's OWN sigma. 1.00 = the simulated week is the right width;")
    print(f"  above 1 = too narrow. Body only ({tc.BODY_LO:.2f} < p < "
          f"{tc.BODY_HI:.2f}), all three checkpoints pooled.")
    z: dict[tuple, list] = defaultdict(list)
    for r in res.checkpoints:
        p = r["p_home"]
        if not (tc.BODY_LO < p < tc.BODY_HI):
            continue
        sigma = tc.implied_sigma(p, r["proj_margin"])
        if sigma is None:
            continue
        val = (r["actual_margin"] - r["proj_margin"]) / sigma
        z[(tc.ALL, r["stat"])].append(val)
        if r["churn_free"]:
            z[(tc.CLEAN, r["stat"])].append(val)
    print("  'typical' is robust (median |z| / 0.6745) — the width of the")
    print("  middle. '|z|>2' is the share of forecasts that missed by more than")
    print("  two of the sim's own sigmas; a correct model shows 4.6%. typical ~ 1")
    print("  with |z|>2 well above 4.6% is a heavy TAIL rather than a wide")
    print("  middle, and the tail is what breaks extreme probabilities.")
    print(f"\n  {'':>5} {'--- all forecasts ---':>21}   "
          f"{'--- churn-free ---':>20}")
    print(f"  {'cat':>5} {'n':>4} {'typical':>8} {'|z|>2':>7}   "
          f"{'n':>4} {'typical':>8} {'|z|>2':>7}")
    for sid in ORDER:
        cells = []
        for scope in (tc.ALL, tc.CLEAN):
            d = z.get((scope, sid)) or []
            rob = tc.robust_z_scale(d)
            exc = (sum(1 for v in d if abs(v) > 2) / len(d)
                   if len(d) >= 8 else None)
            cells.append((len(d), rob, exc))
        if cells[0][1] is None and cells[1][1] is None:
            continue
        row = f"  {appstats.name(sid):>5}"
        for i, (n, rob, exc) in enumerate(cells):
            row += (f" {n:>4} {(f'{rob:.2f}' if rob else '—'):>8} "
                    f"{(f'{exc:.1%}' if exc is not None else '—'):>7}")
            row += "  " if i == 0 else ""
        print(row)


def comebacks(res: tc.Result) -> None:
    units = res.units
    dipped = [k for k, u in units.items() if u.min_p < tc.TAIL_HI]
    won = [k for k in dipped if units[k].won]
    clean = [k for k in won if units[k].churn_free]
    print(f"\n  Units (matchup x side x category): {len(units)} total, "
          f"{len(dipped)} priced under 5% at some point,")
    print(f"  {len(won)} of those won the category anyway — of which "
          f"{len(clean)} with no later roster addition.")
    if not won:
        return
    print(f"\n  {'m':>5} {'side':>5} {'cat':>5} {'min p':>7} "
          f"{'tail snaps':>11} {'last <5% at':>21}  churn")
    for k in sorted(won, key=lambda k: units[k].min_p):
        mid, side, sid = k
        u = units[k]
        print(f"  {mid:>5} {side:>5} {appstats.name(sid):>5} {u.min_p:>7.4f} "
              f"{u.tail_snapshots:>11,} {str(u.last_tail_at):>21}  "
              f"{'—' if u.churn_free else 'roster add'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data.db")
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--from-period", type=int, default=tc.FIRST_PERIOD)
    ap.add_argument("--to-period", type=int, default=None)
    args = ap.parse_args()

    conn = _connect(args.db)
    res = tc.collect(conn, first_period=args.from_period,
                     last_period=args.to_period)
    if not res.n_forecasts:
        print("No observations — no settled period has in-week snapshots.")
        return

    matchups = {k[0] for k in res.units}
    print("Tail calibration of per-category win probabilities")
    print(f"  periods {res.periods[0]}..{res.periods[-1]} "
          f"({len(res.periods)} weeks), {len(matchups)} settled matchups")
    print(f"  {res.n_forecasts:,} in-week forecasts over {len(res.units)} units; "
          f"{res.n_churn_free/res.n_forecasts:.0%} churn-free")
    print("  outcome = one home-vs-away comparison of the settled scores")

    reliability_curve(res)
    tail_bands(res, args.reps)
    tail_by_category(res, args.reps)
    margin_slope(res, args.reps)
    dispersion(res)
    comebacks(res)

    print("\n  Caveats: scores the model AS IT RAN — periods 10-18 predate the")
    print("  2026-08-10 QS/SVHD rate blends and cannot be re-simmed, so read")
    print("  QS/SVHD as pessimistic and K as current. The reliability curve is")
    print("  snapshot-weighted (one long-lived tail state = hundreds of rows);")
    print("  the unit table is the de-duplicated restatement and the CIs are")
    print("  clustered by matchup. Churn-free columns are a LOWER bound on model")
    print("  error — the filter excuses a forecast whether or not the newcomer")
    print("  produced, and an IL activation reads as a roster addition.")


if __name__ == "__main__":
    main()
