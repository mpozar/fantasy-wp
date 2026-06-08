"""Weekly matchup FACTS — a neutral, reproducible data layer (no judgment).

This script does ONE thing well: emit the grounded, hard-to-hallucinate facts a
matchup write-up needs, in a perspective-explicit form, and leave ALL the
editorial work (which swings matter, who caused them, the narrative, the chart
labels) to the LLM that calls it. It deliberately does *not* label "who gained
ground", guess a single driving player, or build trend spans — those judgments
are exactly what the old `matchup_summary.py` got wrong (sign inversions,
double-attributed HRs, spans that disagreed with the events inside them).

Usage:
    .venv/bin/python scripts/matchup_facts.py <matchup_id>            # print facts
    .venv/bin/python scripts/matchup_facts.py <matchup_id> --write <authored.json>
        # bundle the LLM-authored {events, spans, writeup} into
        # docs/annotations/<matchup_id>.json, adding the deterministic result line
        # and validating the authored events/spans against real snapshots.

What it emits (facts mode):
  • result    — TIE-AWARE category tally (X-Y with Z ties), the actual ESPN
                winner, and the hits-tiebreaker comparison when cats are level.
  • categories— final away_avg vs home_avg per cat, with the raw
                home_wins/away_wins/ties sim counts and a `close` flag.
  • arc       — daily closes (BOTH home_wp and away_wp), peak, trough.
                Excludes hand-edited snapshots (`wp_snapshots.edited=1`).
  • swings    — every >=SWING tick, chronological, with BOTH deltas, the top
                category-win% movers across the tick, and the per-player budget
                diff (projection movers) for those cats. Skips edited rows.
  • boxscore  — for each swing day, the raw rostered-player box lines (HR/H/2B/3B
                for hitters; QS/SVHD/K/outs/ER for pitchers) per side, so the LLM
                can attribute a banked swing to the actual play. ALL contributors
                are listed (not a max), because the script can't know which swing
                a given HR caused — that's the LLM's call.

Sign convention for AUTHORED annotations (see --write validation):
  wp_delta is POSITIVE, expressed from the perspective of the team named.
  events:  side ∈ {away, home}      (away=orange curve, home=blue curve)
  spans:   dir  ∈ {up, down}        (up=away gained ground, down=home gained)
This matches the chart CSS, so a home-helping event reads "+12pp" in blue, never
a negative away-perspective delta under a home label.

Read-only except --write (which only writes docs/annotations/<id>.json).
"""
from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

from app import db, mlb, sim

NAMES = {20: "R", 5: "HR", 1: "H", 23: "SB", 18: "OPS",
         48: "K", 63: "QS", 47: "ERA", 41: "WHIP", 83: "SVHD"}
ORDER = [20, 5, 1, 23, 18, 48, 63, 47, 41, 83]
RATE = {18, 47, 41}
HITS = 1                       # tiebreaker category in this league
# driving stat_id -> the per-player budget field that explains a projection swing
DRIVER_EXP = {63: "exp_qs", 83: "exp_svhd", 48: "exp_k", 5: "exp_hr",
              20: "exp_r", 1: "exp_h", 23: "exp_sb", 18: "exp_ops",
              47: "exp_era", 41: "exp_whip"}
# box-score-attributable counting cats (per-player lines exist) -> batter field.
# R and SB are NOT in the box parse, so they're left for prose, not pinned.
BOX_BAT = {5: "hr", 1: "h"}
SWING = 0.07                   # |Δ wp| per tick to list as a candidate swing
BUDGET_MIN = 0.10              # min |Δ exp_<cat>| to report a projection mover
QS_OUTS, QS_MAX_ER = 18, 3


def _fmt(sid, v):
    return f"{v:.3f}" if sid in RATE else f"{v:.1f}"


def _cat_map(d):
    return {c["stat_id"]: c for c in d.get("category_wp", [])}


def _budgets_by_name(d):
    out = {}
    for b in d.get("home_budgets", []) + d.get("away_budgets", []):
        out[b["name"]] = b
    return out


def _winner_of(c):
    """Tie-aware category winner from sim counts: 'away' / 'home' / 'tie'."""
    if c["ties"] > c["away_wins"] and c["ties"] > c["home_wins"]:
        return "tie"
    return "away" if c["away_wins"] > c["home_wins"] else "home"


def _load_rows(conn, mid, include_edited=False):
    ws, we = None, None
    m = conn.execute("SELECT matchup_period_id FROM matchups WHERE id=?", (mid,)).fetchone()
    ws, we = mlb.matchup_period_window(m["matchup_period_id"])
    rows = conn.execute(
        "SELECT computed_at, home_wp, away_wp, details_json, edited FROM wp_snapshots "
        "WHERE matchup_id=? ORDER BY computed_at", (mid,)).fetchall()
    rows = [r for r in rows if r["computed_at"][:10] >= ws.isoformat()]
    if not include_edited:
        rows = [r for r in rows if not r["edited"]]
    return rows, ws, we


def _budget_movers(a, b, sid):
    """All players whose exp_<sid> moved >= BUDGET_MIN between snapshots a->b."""
    field = DRIVER_EXP.get(sid)
    if not field:
        return []
    ba, bb = _budgets_by_name(a), _budgets_by_name(b)
    out = []
    for name in set(ba) | set(bb):
        va = (ba.get(name) or {}).get(field)
        vb = (bb.get(name) or {}).get(field)
        if va is None or vb is None:
            continue
        dd = vb - va
        if abs(dd) >= BUDGET_MIN:
            out.append((name, field, dd))
    return sorted(out, key=lambda t: -abs(t[2]))


def _box_for_day(conn, period, side_team_id, date):
    """Raw rostered-player box lines for one fantasy side on one date.
    Returns (hitters, pitchers): hitters with hr/h/b2/b3; pitchers with the
    derived QS flag + svhd/k/outs/er. ALL contributors, no max()."""
    roster = {sim._norm_name(p["full_name"]): p["full_name"]
              for p in sim.load_team_roster(conn, period, side_team_id)}
    pks = [r[0] for r in conn.execute(
        "SELECT DISTINCT game_pk FROM team_schedule WHERE matchup_period_id=? AND game_date=?",
        (period, date))]
    hitters, pitchers = [], []
    for pk in pks:
        try:
            box = mlb.fetch_boxscore(pk)
        except Exception:
            continue
        for b in box["batters"]:
            if sim._norm_name(b["name"]) in roster and (b["h"] or b["hr"]):
                hitters.append({"name": b["name"], "h": b["h"], "hr": b["hr"],
                                "b2": b.get("b2", 0), "b3": b.get("b3", 0)})
        for p in box["pitchers"]:
            if sim._norm_name(p["name"]) not in roster:
                continue
            qs = int(p.get("games_started") and p["outs"] >= QS_OUTS and p["er"] <= QS_MAX_ER)
            svhd = (p.get("sv", 0) or 0) + (p.get("hld", 0) or 0)
            if qs or svhd or p["k"] or p["outs"]:
                pitchers.append({"name": p["name"], "qs": qs, "svhd": svhd,
                                 "k": p["k"], "outs": p["outs"], "er": p["er"]})
    return hitters, pitchers


def facts(conn, mid):
    m = conn.execute("SELECT matchup_period_id, home_team_id, away_team_id, winner "
                     "FROM matchups WHERE id=?", (mid,)).fetchone()
    if not m:
        print(f"no matchup {mid}"); return
    period = m["matchup_period_id"]
    home = conn.execute("SELECT name FROM teams WHERE id=?", (m["home_team_id"],)).fetchone()["name"]
    away = conn.execute("SELECT name FROM teams WHERE id=?", (m["away_team_id"],)).fetchone()["name"]
    rows, ws, we = _load_rows(conn, mid)
    if not rows:
        print(f"no snapshots for matchup {mid}"); return
    last = json.loads(rows[-1]["details_json"] or "{}")
    cm = _cat_map(last)

    print(f"MATCHUP {mid} — period {period} ({ws}..{we})")
    print(f"  AWAY (orange): {away}")
    print(f"  HOME (blue):   {home}")
    print(f"  snapshots: {len(rows)} non-edited  ({rows[0]['computed_at'][:16]} .. {rows[-1]['computed_at'][:16]})")
    print(f"  final WP: away {rows[-1]['away_wp']*100:.1f}%  home {rows[-1]['home_wp']*100:.1f}%")

    # ── result (TIE-AWARE) ──
    aw = hw = tn = 0
    for sid in ORDER:
        c = cm.get(sid)
        if not c:
            continue
        w = _winner_of(c)
        aw += w == "away"; hw += w == "home"; tn += w == "tie"
    tiebreak = ""
    if aw == hw and HITS in cm:
        ah, hh = cm[HITS]["away_avg"], cm[HITS]["home_avg"]
        tb = away if ah > hh else (home if hh > ah else "level")
        tiebreak = f"  [cats level {aw}-{hw} → hits tiebreaker: {away} {ah:.0f} vs {home} {hh:.0f} → {tb}]"
    winner_name = away if m["winner"] == "AWAY" else (home if m["winner"] == "HOME" else m["winner"])
    print(f"\nRESULT: {away} {aw} - {hw} {home}" + (f" ({tn} tie)" if tn else "")
          + f"  | ESPN winner: {winner_name}" + tiebreak)

    # ── final category standings ──
    print("\nFINAL CATEGORIES (away_avg vs home_avg  |  sim away/home/tie):")
    for sid in ORDER:
        c = cm.get(sid)
        if not c:
            continue
        w = _winner_of(c)
        wlabel = {"away": away, "home": home, "tie": "TIE"}[w][:16]
        close = "  <<close" if 3500 <= c["away_wins"] <= 6500 and w != "tie" else ""
        print(f"  {NAMES[sid]:>5}: {_fmt(sid,c['away_avg']):>7} vs {_fmt(sid,c['home_avg']):>7}  "
              f"-> {wlabel:<16} (a{c['away_wins']/100:.0f}/h{c['home_wins']/100:.0f}/t{c['ties']/100:.0f}){close}")

    # ── WP arc (both perspectives) ──
    daily = OrderedDict()
    for r in rows:
        daily[r["computed_at"][:10]] = (r["away_wp"], r["home_wp"])
    peak = max(rows, key=lambda r: r["away_wp"])
    trough = min(rows, key=lambda r: r["away_wp"])
    print(f"\nWP ARC  (away peak {peak['away_wp']*100:.0f}% @ {peak['computed_at'][:16]} | "
          f"trough {trough['away_wp']*100:.0f}% @ {trough['computed_at'][:16]})")
    print(f"  {'date':<12} {'away%':>6} {'home%':>6}")
    for d, (a_wp, h_wp) in daily.items():
        print(f"  {d:<12} {a_wp*100:6.1f} {h_wp*100:6.1f}")

    # ── candidate swings (chronological; both deltas; movers; budget diff) ──
    swings = []
    for i in range(1, len(rows)):
        da = rows[i]["away_wp"] - rows[i - 1]["away_wp"]
        if abs(da) >= SWING:
            swings.append(i)
    print(f"\nCANDIDATE SWINGS (|Δaway_wp| ≥ {SWING*100:.0f}pp; {len(swings)} total, chronological):")
    swing_dates = set()
    for i in swings:
        a = json.loads(rows[i - 1]["details_json"] or "{}")
        b = json.loads(rows[i]["details_json"] or "{}")
        ca, cb = _cat_map(a), _cat_map(b)
        da = rows[i]["away_wp"] - rows[i - 1]["away_wp"]
        dh = rows[i]["home_wp"] - rows[i - 1]["home_wp"]
        movers = sorted((s for s in ca if s in cb),
                        key=lambda s: abs(cb[s]["away_wins"] - ca[s]["away_wins"]), reverse=True)[:3]
        when = rows[i]["computed_at"][:16]
        swing_dates.add(rows[i]["computed_at"][:10])
        print(f"\n  {when}  away {rows[i-1]['away_wp']*100:.0f}→{rows[i]['away_wp']*100:.0f}% "
              f"(Δaway {da*100:+.0f}pp / Δhome {dh*100:+.0f}pp)")
        for s in movers:
            bud = _budget_movers(a, b, s)
            budstr = "; ".join(f"{n} {f} {d:+.2f}" for n, f, d in bud[:4]) or "(no projection mover — likely banked counter)"
            print(f"      {NAMES[s]:>5}: catwin a{ca[s]['away_wins']/100:.0f}→{cb[s]['away_wins']/100:.0f}%  "
                  f"avg {away[:8]} {_fmt(s,ca[s]['away_avg'])}→{_fmt(s,cb[s]['away_avg'])} / "
                  f"{home[:8]} {_fmt(s,ca[s]['home_avg'])}→{_fmt(s,cb[s]['home_avg'])}")
            print(f"             {budstr}")

    # ── box scores for swing days (raw, all contributors, both sides) ──
    if swing_dates:
        print("\nBOX SCORES on swing days (rostered players only; for attributing banked swings):")
        for d in sorted(swing_dates):
            for label, tid in ((away, m["away_team_id"]), (home, m["home_team_id"])):
                hitters, pitchers = _box_for_day(conn, period, tid, d)
                if not hitters and not pitchers:
                    continue
                print(f"  {d}  {label}:")
                for h in sorted(hitters, key=lambda x: (-x["hr"], -x["h"])):
                    extra = " ".join(f"{k}={h[k]}" for k in ("hr", "b2", "b3") if h[k])
                    print(f"      BAT {h['name']:<22} H={h['h']}" + (f"  {extra}" if extra else ""))
                for p in sorted(pitchers, key=lambda x: (-x["qs"], -x["svhd"], -x["k"])):
                    tags = []
                    if p["qs"]: tags.append("QS")
                    if p["svhd"]: tags.append(f"SVHD={p['svhd']}")
                    print(f"      PIT {p['name']:<22} {p['outs']//3}.{p['outs']%3}ip ER={p['er']} K={p['k']} "
                          + " ".join(tags))


# ───────────────────────── writer (--write) ─────────────────────────

def _result_line(conn, mid):
    """Deterministic, tie-aware result string for the annotations header."""
    m = conn.execute("SELECT home_team_id, away_team_id, winner FROM matchups WHERE id=?", (mid,)).fetchone()
    home = conn.execute("SELECT name FROM teams WHERE id=?", (m["home_team_id"],)).fetchone()["name"]
    away = conn.execute("SELECT name FROM teams WHERE id=?", (m["away_team_id"],)).fetchone()["name"]
    rows, _, _ = _load_rows(conn, mid)
    cm = _cat_map(json.loads(rows[-1]["details_json"] or "{}"))
    aw = sum(_winner_of(cm[s]) == "away" for s in ORDER if s in cm)
    hw = sum(_winner_of(cm[s]) == "home" for s in ORDER if s in cm)
    tn = sum(_winner_of(cm[s]) == "tie" for s in ORDER if s in cm)
    winner = away if m["winner"] == "AWAY" else (home if m["winner"] == "HOME" else None)
    loser = home if winner == away else away
    hi, lo = (aw, hw) if winner == away else (hw, aw)
    tie_note = f" ({tn} tie)" if tn else ""
    if winner:
        return f"{winner} def. {loser} {hi}–{lo}{tie_note}"
    return f"{away} {aw}–{hw} {home}{tie_note}"


def write_annotations(conn, mid, authored_path):
    """Bundle LLM-authored {events, spans, writeup} into docs/annotations/<id>.json.
    Validates the authored shape + that event/span timestamps fall in-window."""
    authored = json.loads(Path(authored_path).read_text())
    rows, ws, we = _load_rows(conn, mid, include_edited=True)
    valid_ts = {r["computed_at"] for r in rows}
    lo, hi = rows[0]["computed_at"], rows[-1]["computed_at"]
    m = conn.execute("SELECT home_team_id, away_team_id, matchup_period_id FROM matchups WHERE id=?", (mid,)).fetchone()
    away = conn.execute("SELECT name FROM teams WHERE id=?", (m["away_team_id"],)).fetchone()["name"]
    home = conn.execute("SELECT name FROM teams WHERE id=?", (m["home_team_id"],)).fetchone()["name"]

    errs = []
    events = authored.get("events", [])
    spans = authored.get("spans", [])
    for i, e in enumerate(events):
        for k in ("at", "label", "side", "wp_delta"):
            if k not in e:
                errs.append(f"event[{i}] missing '{k}'")
        if e.get("side") not in ("away", "home"):
            errs.append(f"event[{i}] side must be away|home, got {e.get('side')!r}")
        if not (lo <= e.get("at", "") <= hi):
            errs.append(f"event[{i}] at={e.get('at')!r} outside window {lo[:16]}..{hi[:16]}")
        if e.get("wp_delta", 0) < 0:
            errs.append(f"event[{i}] wp_delta must be positive (perspective of `side`), got {e.get('wp_delta')}")
    for i, s in enumerate(spans):
        for k in ("start", "end", "label", "dir", "wp_delta"):
            if k not in s:
                errs.append(f"span[{i}] missing '{k}'")
        if s.get("dir") not in ("up", "down"):
            errs.append(f"span[{i}] dir must be up|down, got {s.get('dir')!r}")
        for k in ("start", "end"):
            if not (lo <= s.get(k, "") <= hi):
                errs.append(f"span[{i}] {k}={s.get(k)!r} outside window {lo[:16]}..{hi[:16]}")
        if s.get("wp_delta", 0) < 0:
            errs.append(f"span[{i}] wp_delta must be positive, got {s.get('wp_delta')}")
    if errs:
        print("AUTHORED ANNOTATION VALIDATION FAILED:")
        for e in errs:
            print("  •", e)
        sys.exit(1)

    out = {"matchup_id": mid, "period": m["matchup_period_id"], "model_version": "mc-v1",
           "generated_at": rows[-1]["computed_at"], "away": away, "home": home,
           "result": _result_line(conn, mid),
           "events": events, "spans": spans}
    if "writeup" in authored:
        out["writeup"] = authored["writeup"]
    dest = Path(__file__).resolve().parent.parent / "docs" / "annotations"
    dest.mkdir(exist_ok=True)
    path = dest / f"{mid}.json"
    path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {path}  ({len(events)} events, {len(spans)} spans"
          f"{', +writeup' if 'writeup' in out else ''})")
    print(f"result line: {out['result']}")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__); sys.exit(0 if args else 1)
    conn = db.connect()
    if "--write" in args:
        authored = args[args.index("--write") + 1]
        mid = int(next(a for a in args if not a.startswith("-") and a != authored))
        write_annotations(conn, mid, authored)
    else:
        mid = int(next(a for a in args if not a.startswith("-")))
        facts(conn, mid)
    conn.close()


if __name__ == "__main__":
    main()
