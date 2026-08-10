#!/usr/bin/env python
"""Offline tool: decompose the SP start-count over-projection.

Read-only. Answers "are we projecting too many starts, and if so why" by
splitting the gap into the two mechanisms that produce it:

    projected units ──▶ starts those pitchers actually MADE ──▶ starts CREDITED
                        (rotation/cadence model error)          (slot attribution:
                                                                 bench/IL that day)

The second gap is NOT a bug — it is the deliberate modelling stance (owner call,
2026-08-10): the model assumes every manager activates a benched starter when he
should, so a bench/IL-slotted pitcher is projected at full weight and the
dashboard never penalises a team for a neglectful owner. Measured at ~10.6% of
real starts. It is reported here so it stays visible and separable, not so it
gets "fixed".

`CREDITED` is the accounting that matches `scripts/calibration.py`: ESPN's banked
totals only include players in an active pitching slot that day, so a category's
projected-vs-actual bias should be read against *credited* starts.

Coverage starts at period 11 (`pitcher_final_lines` from 2026-06-08,
`daily_lineups` from 2026-06-06).

    .venv/bin/python scripts/analyze_starts.py
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
from collections import defaultdict

from app import sim
from app.mlb import LONG_MATCHUPS, matchup_period_window
from app.names import norm_name

FIRST_PERIOD = 11


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _first_pitch(conn: sqlite3.Connection, period: int) -> str:
    r = conn.execute("SELECT MIN(active_start) a FROM game_day_activity "
                     "WHERE matchup_period_id=?", (period,)).fetchone()
    if r and r["a"]:
        return r["a"]
    return f"{matchup_period_window(period)[0].isoformat()}T16:00:00+00:00"


def collect(conn: sqlite3.Connection) -> list[dict]:
    out: list[dict] = []
    periods = [r["p"] for r in conn.execute(
        "SELECT DISTINCT matchup_period_id p FROM matchups WHERE winner IN "
        "('HOME','AWAY') AND matchup_period_id >= ? ORDER BY p", (FIRST_PERIOD,))]
    for period in periods:
        lo, hi = matchup_period_window(period)
        lo, hi = lo.isoformat(), hi.isoformat()
        fp = _first_pitch(conn, period)
        starts_by_name: dict[str, list[str]] = defaultdict(list)
        for r in conn.execute("SELECT game_date, name FROM pitcher_final_lines "
                              "WHERE game_date BETWEEN ? AND ? AND games_started=1",
                              (lo, hi)):
            starts_by_name[norm_name(r["name"])].append(r["game_date"])

        for m in conn.execute("SELECT id, home_team_id, away_team_id FROM matchups "
                              "WHERE matchup_period_id=? ORDER BY id", (period,)):
            snap = conn.execute(
                """SELECT details_json FROM wp_snapshots WHERE matchup_id=?
                   AND computed_at < ? AND details_json IS NOT NULL AND edited=0
                   ORDER BY computed_at DESC LIMIT 1""", (m["id"], fp)).fetchone()
            if not snap:
                continue
            d = json.loads(snap["details_json"])
            for side in ("home", "away"):
                team_id = m[f"{side}_team_id"]
                slots = {}
                for r in conn.execute(
                    "SELECT dl.game_date, p.full_name, dl.lineup_slot_id "
                    "FROM daily_lineups dl JOIN players p ON p.id=dl.player_id "
                    "WHERE dl.fantasy_team_id=? AND dl.game_date BETWEEN ? AND ?",
                        (team_id, lo, hi)):
                    slots[(r["game_date"], norm_name(r["full_name"]))] = r["lineup_slot_id"]

                proj = made = credited = 0.0
                for b in d[f"{side}_budgets"]:
                    if b.get("role") != "SP":
                        continue
                    proj += float(b.get("units") or 0)
                    nn = norm_name(b["name"])
                    for gd in starts_by_name.get(nn, []):
                        made += 1
                        if slots.get((gd, nn)) in sim.PITCHER_SLOTS:
                            credited += 1
                out.append({"period": period, "team_id": team_id, "proj": proj,
                            "made": made, "credited": credited})
    return out


def _ratio_ci(rows: list[dict], num: str, den: str, reps: int,
              seed: int = 7) -> tuple[float, float] | None:
    by_week: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_week[r["period"]].append(r)
    wks = sorted(by_week)
    if len(wks) < 3:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        draw = [x for _ in wks for x in by_week[rng.choice(wks)]]
        d = sum(x[den] for x in draw)
        if d:
            vals.append(sum(x[num] for x in draw) / d)
    if not vals:
        return None
    vals.sort()
    return vals[int(0.05 * len(vals))], vals[int(0.95 * len(vals)) - 1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data.db")
    ap.add_argument("--reps", type=int, default=4000)
    args = ap.parse_args()

    conn = _connect(args.db)
    rows = collect(conn)
    if not rows:
        print("No observations.")
        return

    P = sum(r["proj"] for r in rows)
    M = sum(r["made"] for r in rows)
    C = sum(r["credited"] for r in rows)
    weeks = sorted({r["period"] for r in rows})
    print(f"SP start counts — periods {weeks[0]}..{weeks[-1]}, {len(rows)} team-weeks\n")
    print(f"  projected (Σ SP budget units)          {P:8.1f}")
    print(f"  starts those pitchers actually made    {M:8.0f}   ({P/M-1:+.1%})")
    print(f"  of those, credited (active slot)       {C:8.0f}   ({P/C-1:+.1%})\n")
    gap = P - C
    print(f"  rotation model (never started):        {P-M:7.1f}  ({(P-M)/gap:5.1%} of gap)")
    print(f"  slot attribution (bench/IL that day):  {M-C:7.1f}  ({(M-C)/gap:5.1%} of gap)"
          f"  <- DELIBERATE")

    normal = [r for r in rows if r["period"] not in LONG_MATCHUPS]
    long_ = [r for r in rows if r["period"] in LONG_MATCHUPS]
    if normal:
        p, m = sum(r["proj"] for r in normal), sum(r["made"] for r in normal)
        ci = _ratio_ci(normal, "proj", "made", args.reps)
        ci_s = f"  90% CI [{ci[0]-1:+.1%}, {ci[1]-1:+.1%}]" if ci else ""
        print(f"\n  rotation error, NORMAL weeks only:     {p/m-1:+.1%}{ci_s}")
    if long_:
        p, m = sum(r["proj"] for r in long_), sum(r["made"] for r in long_)
        print(f"  rotation error, LONG periods {sorted(LONG_MATCHUPS)}:      "
              f"{p/m-1:+.1%}   <- All-Star break; see known limitations")

    print(f"\n  {'wk':>3} {'days':>5} {'g/team':>7} {'proj':>7} {'made':>7} {'cred':>7} "
          f"{'p/made':>8} {'p/cred':>8}")
    byw: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for r in rows:
        byw[r["period"]][0] += r["proj"]
        byw[r["period"]][1] += r["made"]
        byw[r["period"]][2] += r["credited"]
    for wk in weeks:
        p, m, c = byw[wk]
        lo, hi = matchup_period_window(wk)
        days = (hi - lo).days + 1
        g = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT pro_team_id) t FROM team_schedule "
            "WHERE matchup_period_id=? AND (game_status IS NULL OR game_status NOT IN "
            "('Postponed','Suspended','Cancelled','Canceled'))", (wk,)).fetchone()
        per_team = (g["n"] / g["t"]) if g["t"] else 0.0
        tag = "*" if wk in LONG_MATCHUPS else " "
        print(f"  {wk:>3}{tag}{days:>5} {per_team:>7.1f} {p:>7.1f} {m:>7.0f} {c:>7.0f} "
              f"{(p/m-1 if m else 0):>+7.1%} {(p/c-1 if c else 0):>+7.1%}")
    if any(w in LONG_MATCHUPS for w in weeks):
        print("   * multi-week period (All-Star). Note games/team per calendar day "
              "drops to ~0.68 vs ~0.90 —")
        print("     the break is ~3 gameless days the rotation models walk straight "
              "through.")


if __name__ == "__main__":
    main()
