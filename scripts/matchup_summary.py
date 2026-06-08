"""Weekly matchup summary — deterministic data layer.

Usage:  .venv/bin/python scripts/matchup_summary.py <matchup_id>

Emits the reproducible facts a matchup write-up needs: result + category score,
final per-category standings, the WP arc (daily closes / peak / trough), and the
top WP swings — each with its driving category and a per-player attribution from
the snapshot budgets (`details_json`). The `/matchup-summary` skill runs this,
adds box-score attribution for banked-hitting swings where needed, and writes the
prose. Read-only; safe to run anytime.

Attribution note: a *projection* swing (a player's exp_<cat> jumps — a QS/SVHD
locking in, a probable announced, an SP exiting) is named directly from the
budget diff. A *banked-counter* swing (a HR/hit that lands in category_state but
barely moves anyone's remaining projection) shows "(banked — check box score)" —
the skill resolves the hitter from the MLB box score.
"""
from __future__ import annotations

import json
import sys
from collections import OrderedDict

from app import db, mlb, sim

NAMES = {20: "R", 5: "HR", 1: "H", 23: "SB", 18: "OPS",
         48: "K", 63: "QS", 47: "ERA", 41: "WHIP", 83: "SVHD"}
ORDER = [20, 5, 1, 23, 18, 48, 63, 47, 41, 83]
RATE = {18, 47, 41}
# driving stat_id -> the per-player budget field that explains it
DRIVER_EXP = {63: "exp_qs", 83: "exp_svhd", 48: "exp_k", 5: "exp_hr",
              20: "exp_r", 1: "exp_h", 23: "exp_sb", 18: "exp_ops",
              47: "exp_era", 41: "exp_whip"}
SWING = 0.07            # |Δ away_wp| per tick to count as significant
ATTRIB_MIN = 0.15       # min |Δ exp_<cat>| to call it a projection (named) swing


def _fmt(sid, v):
    return f"{v:.3f}" if sid in RATE else f"{v:.1f}"


def _cat_map(d):
    return {c["stat_id"]: c for c in d.get("category_wp", [])}


def _budgets_by_name(d):
    out = {}
    for b in d.get("home_budgets", []) + d.get("away_budgets", []):
        out[b["name"]] = b
    return out


def _attribute(a, b, sid):
    """Name the player whose exp_<sid> moved most between snapshots a→b (or None
    if it's a banked-counter event with no clear projection mover)."""
    field = DRIVER_EXP.get(sid)
    if not field:
        return None
    ba, bb = _budgets_by_name(a), _budgets_by_name(b)
    best, bestd = None, 0.0
    for name in set(ba) | set(bb):
        va = (ba.get(name) or {}).get(field)
        vb = (bb.get(name) or {}).get(field)
        if va is None or vb is None:
            continue
        dd = vb - va
        if abs(dd) > abs(bestd):
            best, bestd = name, dd
    if best is not None and abs(bestd) >= ATTRIB_MIN:
        return f"{best} ({field} {bestd:+.2f})"
    return None


def _box_attribute(conn, period, team_id, date, sid):
    """Name rostered players on `team_id` who logged stat `sid` on `date`, from the
    MLB box score — for banked-counter hitting swings (HR/H/R/SB) the budget diff
    can't see. Returns a short label like 'Neto + Moreno' or None."""
    field = {5: "hr", 1: "h", 20: None, 23: None}.get(sid)  # HR/H from boxscore batting
    if not field:
        return None
    roster = {sim._norm_name(p["full_name"]): p["full_name"]
              for p in sim.load_team_roster(conn, period, team_id)}
    pks = [r[0] for r in conn.execute(
        "SELECT DISTINCT game_pk FROM team_schedule WHERE matchup_period_id=? AND game_date=?",
        (period, date))]
    hits = []
    for pk in pks:
        try:
            for b in mlb.fetch_boxscore(pk)["batters"]:
                if sim._norm_name(b["name"]) in roster and (b.get(field) or 0) > 0:
                    hits.append(b["name"].split()[-1])   # last name
        except Exception:
            pass
    return " + ".join(dict.fromkeys(hits)) if hits else None


def build_annotations(conn, mid, *, event_pp=0.08, span_pp=0.12):
    """Build {events, spans} for the WP chart. Events = acute swings (merged within
    15 min), each attributed to a player; spans = day-level trends labeled by their
    dominant categories. Reuses the same swing/attribution logic as the write-up."""
    m = conn.execute("SELECT matchup_period_id, home_team_id, away_team_id FROM matchups WHERE id=?",
                     (mid,)).fetchone()
    period = m["matchup_period_id"]
    ws, we = mlb.matchup_period_window(period)
    rows = [r for r in conn.execute(
        "SELECT computed_at, away_wp, details_json FROM wp_snapshots WHERE matchup_id=? ORDER BY computed_at",
        (mid,)).fetchall() if r["computed_at"][:10] >= ws.isoformat()]
    away_name = conn.execute("SELECT name FROM teams WHERE id=?", (m["away_team_id"],)).fetchone()["name"]
    home_name = conn.execute("SELECT name FROM teams WHERE id=?", (m["home_team_id"],)).fetchone()["name"]

    # ── acute events: merge adjacent same-direction big ticks into one ──
    raw = []
    for i in range(1, len(rows)):
        d = rows[i]["away_wp"] - rows[i - 1]["away_wp"]
        if abs(d) >= event_pp:
            raw.append((i, d))
    events = []
    for i, d in raw:
        # merge into the previous event if within 15 min and same sign
        if events and (events[-1]["sign"] == (d > 0)) and \
           (_t(rows[i]["computed_at"]) - _t(rows[events[-1]["_j"]]["computed_at"]) <= 900):
            ev = events[-1]; ev["delta"] += d; ev["_i0"] = min(ev["_i0"], i - 1)
            if abs(d) > ev["_peakabs"]:
                ev["_peakabs"] = abs(d); ev["_j"] = i
            continue
        events.append({"_i0": i - 1, "_j": i, "delta": d, "sign": d > 0, "_peakabs": abs(d)})
    out_events = []
    for ev in events:
        a = json.loads(rows[ev["_i0"]]["details_json"] or "{}")
        b = json.loads(rows[ev["_j"]]["details_json"] or "{}")
        ca, cb = _cat_map(a), _cat_map(b)
        drv = max((s for s in ca if s in cb),
                  key=lambda s: abs(cb[s]["away_wins"] - ca[s]["away_wins"]), default=None)
        if drv is None:
            continue
        gained_away = ev["delta"] > 0           # which side this swing helped
        side_id = m["away_team_id"] if gained_away else m["home_team_id"]
        date = rows[ev["_j"]]["computed_at"][:10]
        who = _attribute(a, b, drv)
        player = who.split(" (")[0] if who else _box_attribute(conn, period, side_id, date, drv)
        cat = NAMES[drv]
        label = f"{player} {cat}" if player else f"{cat} swing"
        out_events.append({
            "at": rows[ev["_j"]]["computed_at"],
            "label": label, "cat": cat,
            "side": "away" if gained_away else "home",
            "wp_delta": round(ev["delta"], 3),
        })
    out_events.sort(key=lambda e: abs(e["wp_delta"]), reverse=True)
    out_events = sorted(out_events[:6], key=lambda e: e["at"])

    # ── day-level trend spans ──
    from collections import OrderedDict
    byday = OrderedDict()
    for r in rows:
        byday.setdefault(r["computed_at"][:10], []).append(r)
    days = list(byday)
    spans = []
    for d in days:
        drows = byday[d]
        net = drows[-1]["away_wp"] - drows[0]["away_wp"]
        if abs(net) < span_pp:
            continue
        a = json.loads(drows[0]["details_json"] or "{}"); b = json.loads(drows[-1]["details_json"] or "{}")
        ca, cb = _cat_map(a), _cat_map(b)
        movers = sorted((s for s in ca if s in cb),
                        key=lambda s: abs(cb[s]["away_wins"] - ca[s]["away_wins"]), reverse=True)
        top = [NAMES[s] for s in movers[:2]]
        team = away_name if net > 0 else home_name
        verb = "gains" if net > 0 else "loses ground"
        spans.append({"start": drows[0]["computed_at"], "end": drows[-1]["computed_at"],
                      "label": f"{team.split()[0]} {verb}: {', '.join(top)}",
                      "dir": "up" if net > 0 else "down", "wp_delta": round(net, 3)})
    spans.sort(key=lambda s: abs(s["wp_delta"]), reverse=True)
    spans = sorted(spans[:4], key=lambda s: s["start"])

    return {"matchup_id": mid, "period": period,
            "generated_at": rows[-1]["computed_at"], "model_version": "mc-v1",
            "away": away_name, "home": home_name,
            "events": out_events, "spans": spans}


def _t(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


def main(mid):
    conn = db.connect()
    m = conn.execute("SELECT matchup_period_id, home_team_id, away_team_id, winner "
                     "FROM matchups WHERE id=?", (mid,)).fetchone()
    if not m:
        print(f"no matchup {mid}"); return
    home = conn.execute("SELECT name FROM teams WHERE id=?", (m["home_team_id"],)).fetchone()["name"]
    away = conn.execute("SELECT name FROM teams WHERE id=?", (m["away_team_id"],)).fetchone()["name"]
    ws, we = mlb.matchup_period_window(m["matchup_period_id"])
    rows = conn.execute("SELECT computed_at, home_wp, away_wp, details_json FROM wp_snapshots "
                        "WHERE matchup_id=? ORDER BY computed_at", (mid,)).fetchall()
    # Clip to the matchup week (drop pre-week `compute --future` projections); keep
    # the tail through the Mon settle. The pre-week rows are a flat projection, not
    # part of the live story.
    rows = [r for r in rows if r["computed_at"][:10] >= ws.isoformat()]
    if not rows:
        print(f"no snapshots for matchup {mid}"); return
    last = json.loads(rows[-1]["details_json"] or "{}")

    print(f"MATCHUP {mid} — period {m['matchup_period_id']} ({ws}..{we})")
    print(f"  HOME: {home}   AWAY: {away}")
    print(f"  snapshots: {len(rows)}  ({rows[0]['computed_at'][:16]} .. {rows[-1]['computed_at'][:16]})")

    # ── result + final category standings ──
    cm = _cat_map(last)
    aw = away_cats = home_cats = ties = 0
    print(f"\nFINAL  away({away}) WP = {rows[-1]['away_wp']*100:.1f}%")
    lines = []
    for sid in ORDER:
        c = cm.get(sid)
        if not c:
            continue
        ta, tz = c["away_avg"], c["home_avg"]
        aw = c["away_wins"]
        win = away if aw > 5200 else (home if aw < 4800 else "~tie")
        if win == away: away_cats += 1
        elif win == home: home_cats += 1
        else: ties += 1
        close = "  <<close" if 3500 <= aw <= 6500 else ""
        lines.append(f"  {NAMES[sid]:>5}: {away[:12]:>12} {_fmt(sid,ta):>7}  vs {_fmt(sid,tz):>7} {home[:12]:<12}  -> {win}{close}")
    print(f"CATEGORY SCORE: {away} {away_cats} - {home_cats} {home}" + (f" ({ties} tie)" if ties else ""))
    print("\n".join(lines))

    # ── WP arc ──
    daily = OrderedDict()
    for r in rows:
        daily[r["computed_at"][:10]] = r["away_wp"]
    peak = max(rows, key=lambda r: r["away_wp"])
    trough = min(rows, key=lambda r: r["away_wp"])
    print(f"\nWP ARC (away={away}):  peak {peak['away_wp']*100:.0f}% @ {peak['computed_at'][:16]}  |  "
          f"trough {trough['away_wp']*100:.0f}% @ {trough['computed_at'][:16]}")
    for d, wp in daily.items():
        print(f"  {d} close: {wp*100:5.1f}%")

    # ── top swings, with driver + attribution ──
    swings = []
    for i in range(1, len(rows)):
        d = rows[i]["away_wp"] - rows[i - 1]["away_wp"]
        if abs(d) >= SWING:
            swings.append((abs(d), i, d))
    swings.sort(reverse=True)
    print(f"\nTOP SWINGS (|Δ away_wp| ≥ {SWING*100:.0f}pp; {len(swings)} total, showing up to 8):")
    for _, i, d in sorted(swings[:8], key=lambda x: x[1]):  # chronological
        a = json.loads(rows[i - 1]["details_json"] or "{}")
        b = json.loads(rows[i]["details_json"] or "{}")
        ca, cb = _cat_map(a), _cat_map(b)
        drv = max((s for s in ca if s in cb),
                  key=lambda s: abs(cb[s]["away_wins"] - ca[s]["away_wins"]), default=None)
        when = rows[i]["computed_at"][5:16]
        head = (f"  {when}  away {rows[i-1]['away_wp']*100:.0f}%->{rows[i]['away_wp']*100:.0f}% "
                f"(Δ{d*100:+.0f}pp)")
        if drv is None:
            print(head); continue
        cda = ca[drv]["away_wins"] / 100; cdb = cb[drv]["away_wins"] / 100
        a_av0, a_av1 = ca[drv]["away_avg"], cb[drv]["away_avg"]
        h_av0, h_av1 = ca[drv]["home_avg"], cb[drv]["home_avg"]
        who = _attribute(a, b, drv) or "(banked — check box score)"
        print(f"{head}  driver={NAMES[drv]} (cat win {cda:.0f}%->{cdb:.0f}%; "
              f"avg {away[:8]} {a_av0:.2f}->{a_av1:.2f} / {home[:8]} {h_av0:.2f}->{h_av1:.2f})  by: {who}")

    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__); sys.exit(0 if args else 1)
    annotate = "--annotate" in args
    mid = int(next(a for a in args if not a.startswith("-")))
    if annotate:
        from pathlib import Path
        conn = db.connect()
        ann = build_annotations(conn, mid)
        conn.close()
        out = Path(__file__).resolve().parent.parent / "docs" / "annotations"
        out.mkdir(exist_ok=True)
        path = out / f"{mid}.json"
        path.write_text(json.dumps(ann, separators=(",", ":")))
        print(f"wrote {path}  ({len(ann['events'])} events, {len(ann['spans'])} spans)")
    else:
        main(mid)
