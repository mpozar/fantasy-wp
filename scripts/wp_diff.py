"""WP-change decomposition — the mandatory FIRST STEP of any "what caused this
WP move?" investigation (see .claude/skills/wp-investigate/SKILL.md).

Why this exists: a run of misdiagnoses (2026-07-09, INCIDENTS/CLAUDE.md
"Investigation discipline") all had the same shape — attributing a WP move to
the first *salient* change instead of decomposing every simultaneous delta.
Each decomposition step is mechanical, so this script does all of them, every
time, with the canonical stat labels. The investigator's job starts AFTER this
output is on the table: weigh the candidates (a category that FLIPS leaders
dominates a within-lean shift), verify mechanics in code, and label anything
unconfirmed as a hypothesis.

Usage:
    .venv/bin/python scripts/wp_diff.py <matchup_id|team-name> <start> <end> [--utc]

    <start>/<end> — "2026-07-09 14:00" or ISO. NAIVE times are read as
    Europe/Oslo local (how the owner phrases questions; CEST in summer) and
    converted; pass --utc to read them as UTC instead. Timestamps with an
    explicit offset are used as-is. The header echoes the interpretation —
    check it before reasoning about times.

What it emits, in order:
  window     — both-timezone echo of the window + compute-gap warnings (a >15
               min snapshot gap = laptop slept; a slate's changes lump onto the
               post-wake tick) + hand-edited-row warnings.
  series     — every snapshot tick in the window with |Δ| ≥ 1pp (±1pp at
               p≈0.5 is Monte Carlo noise, ~0.4pp SE at n=10k).
  categories — per-cat sim win share before vs after (boundary snapshots),
               sorted by |Δ|, LEADER-FLIPS marked. Rate-cat avgs are the
               internal scale, direction-only (playbook #11).
  budgets    — per-side player diff: added / removed / changed exp_* lines,
               with provenance flags (promoted, benched-live-drop, …).
  banked     — category_state as-of each boundary, per team, deltas only.
  schedule   — games that went Final inside the window (became_final_at) +
               a current-state caveat (historical statuses are overwritten).
  live_recon — QS/SVHD scrape/floor/box decisions + rate verdicts, both ticks.
  flags      — validation_flags overlapping the window (incl. league-wide).
  published  — which of the window's points survived the ~200-point
               downsample into docs/data.json (what the owner could SEE).

Read-only (opens data.db in ro mode); safe to run mid-slate.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, mlb, stats  # noqa: E402

OSLO = ZoneInfo("Europe/Oslo")
DOCS_DATA_JSON = Path(__file__).resolve().parent.parent / "docs" / "data.json"
GAP_MIN = 15           # snapshot gap (minutes) worth flagging
SERIES_PP = 0.01       # |Δ home_wp| per tick to list in the series
BUDGET_MIN = 0.10      # |Δ| in units / exp_* to report a budget mover
MAX_MOVERS = 12        # per side, per boundary diff

# budget fields worth diffing, roughly ordered by how often they decide cats
EXP_FIELDS = ["units", "exp_qs", "exp_svhd", "exp_k", "exp_outs", "exp_era",
              "exp_whip", "exp_h", "exp_hr", "exp_r", "exp_sb", "exp_ops"]


def _parse_when(raw: str, assume_utc: bool) -> datetime:
    t = datetime.fromisoformat(raw)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc if assume_utc else OSLO)
    return t.astimezone(timezone.utc)


def _fmt_both(t: datetime) -> str:
    return f"{t.isoformat()} UTC = {t.astimezone(OSLO).strftime('%Y-%m-%d %H:%M')} Oslo"


def _snap_t(row) -> datetime:
    t = datetime.fromisoformat(row["computed_at"])
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _connect_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_matchup(conn, target: str, start: datetime, end: datetime):
    """target = matchup id or a team-name/abbrev/owner substring."""
    if target.isdigit():
        m = conn.execute("SELECT * FROM matchups WHERE id=?", (int(target),)).fetchone()
        if not m:
            sys.exit(f"no matchup id {target}")
        return m
    q = f"%{target.lower()}%"
    teams = conn.execute(
        "SELECT id, name FROM teams WHERE lower(name) LIKE ? OR lower(abbrev) LIKE ? "
        "OR lower(owner) LIKE ?", (q, q, q)).fetchall()
    if len(teams) != 1:
        names = ", ".join(t["name"] for t in conn.execute("SELECT name FROM teams"))
        sys.exit(f"team '{target}' matched {len(teams)} teams — teams: {names}")
    team = teams[0]
    for d in (start, end):
        period = mlb.period_for_date(d.date())
        m = conn.execute(
            "SELECT * FROM matchups WHERE matchup_period_id=? AND "
            "(home_team_id=? OR away_team_id=?)", (period, team["id"], team["id"])).fetchone()
        if m:
            return m
    sys.exit(f"no matchup for {team['name']} in the period(s) covering the window")


def _team_names(conn, m) -> tuple[str, str]:
    n = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM teams")}
    return n.get(m["home_team_id"], "home"), n.get(m["away_team_id"], "away")


def _details(row) -> dict | None:
    if row is None or not row["details_json"]:
        return None
    try:
        return json.loads(row["details_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def _cat_rows(d: dict) -> dict[int, dict]:
    return {c["stat_id"]: c for c in (d or {}).get("category_wp", [])}


def _print_series(rows, start, end):
    print("\n== series (ticks with |Δ| ≥ 1pp; all times UTC, Oslo in parens) ==")
    prev = None
    gaps, edited = 0, 0
    for r in rows:
        t = _snap_t(r)
        if r["edited"]:
            edited += 1
        line = None
        if prev is not None:
            dt_min = (t - _snap_t(prev)) / timedelta(minutes=1)
            d = r["home_wp"] - prev["home_wp"]
            if dt_min > GAP_MIN:
                gaps += 1
                print(f"  ⚠ GAP {dt_min:.0f} min → next tick lumps the backlog "
                      f"(laptop sleep / offline; see CLAUDE.md full-day-offline note)")
            if abs(d) >= SERIES_PP or r is rows[-1]:
                line = f"{d:+.1%}"
        if prev is None or line is not None:
            osl = t.astimezone(OSLO).strftime("%H:%M")
            mark = "  [EDITED — hand-smoothed, don't diff details]" if r["edited"] else ""
            print(f"  {r['computed_at']}  ({osl} Oslo)  home_wp={r['home_wp']:.1%}"
                  f"{'  Δ' + line if line else ''}{mark}")
        prev = r
    if edited:
        print(f"  ⚠ {edited} hand-edited snapshot(s) in window — see INCIDENTS.md")
    if not gaps:
        print("  (no compute gaps > 15 min)")


def _print_categories(before_d, after_d, home_name, away_name):
    print(f"\n== categories (home = {home_name}; sorted by |Δ win share|) ==")
    if not (before_d and after_d):
        print("  details_json missing on a boundary snapshot — can't decompose")
        return
    cb, ca = _cat_rows(before_d), _cat_rows(after_d)
    nb, na = before_d.get("n_sims") or 1, after_d.get("n_sims") or 1
    rows = []
    for sid in ca:
        if sid not in cb:
            continue
        b, a = cb[sid], ca[sid]
        sb, sa = b["home_wins"] / nb, a["home_wins"] / na
        lead_b = (b["home_wins"] > b["away_wins"]) - (b["home_wins"] < b["away_wins"])
        lead_a = (a["home_wins"] > a["away_wins"]) - (a["home_wins"] < a["away_wins"])
        rows.append((abs(sa - sb), sid, sb, sa, lead_b != lead_a, b, a))
    rows.sort(reverse=True)
    print(f"  {'cat':5} {'before':>7} {'after':>7} {'Δpp':>7}   avgs (home | away)  "
          f"[rate-cat avgs = internal scale, direction only]")
    for mag, sid, sb, sa, flipped, b, a in rows:
        flip = "  ← LEADER FLIPPED (dominant candidate)" if flipped else ""
        print(f"  {stats.name(sid):5} {sb:6.1%} {sa:6.1%} {sa - sb:+6.1%}   "
              f"{b['home_avg']:.2f}→{a['home_avg']:.2f} | "
              f"{b['away_avg']:.2f}→{a['away_avg']:.2f}{flip}")
    if not any(r[4] for r in rows):
        print("  (no leader flips — the move is within-lean shifts; weigh accordingly)")


def _budget_diff(before_d, after_d, side, label):
    print(f"\n-- budgets: {label} ({side}) --")
    if not (before_d and after_d):
        print("  (missing details on a boundary)")
        return
    bb = {b["name"]: b for b in before_d.get(f"{side}_budgets", [])}
    ba = {b["name"]: b for b in after_d.get(f"{side}_budgets", [])}
    lines = []
    for name in sorted(set(bb) | set(ba)):
        b, a = bb.get(name), ba.get(name)
        if b is None or a is None:
            rec = a or b
            what = "ADDED" if b is None else "REMOVED"
            key = ", ".join(f"{f}={rec[f]}" for f in EXP_FIELDS
                            if isinstance(rec.get(f), (int, float)) and abs(rec[f]) >= BUDGET_MIN)
            lines.append((99, f"  {what:8} {name} [{rec.get('role')}] {key}"
                              f"{'  flags=' + ','.join(rec['flags']) if rec.get('flags') else ''}"))
            continue
        deltas = []
        for f in EXP_FIELDS:
            vb, va = b.get(f), a.get(f)
            if isinstance(vb, (int, float)) and isinstance(va, (int, float)) \
                    and abs(va - vb) >= BUDGET_MIN:
                deltas.append(f"{f} {vb}→{va}")
        fl_b, fl_a = set(b.get("flags") or []), set(a.get("flags") or [])
        if fl_b != fl_a:
            deltas.append(f"flags {sorted(fl_b)}→{sorted(fl_a)}")
        if deltas:
            mag = max(abs((a.get(f) or 0) - (b.get(f) or 0)) for f in EXP_FIELDS
                      if isinstance(b.get(f), (int, float)) and isinstance(a.get(f), (int, float)))
            lines.append((mag, f"  changed  {name} [{a.get('role')}] " + "; ".join(deltas)))
    lines.sort(key=lambda x: -x[0])
    for _, line in lines[:MAX_MOVERS]:
        print(line)
    if len(lines) > MAX_MOVERS:
        print(f"  … {len(lines) - MAX_MOVERS} more (rerun with a tighter window)")
    if not lines:
        print("  (no budget changes ≥ threshold — roster/projections steady)")


def _banked_asof(conn, mid, when: datetime):
    iso = when.isoformat()
    return {(r["team_id"], r["stat_id"]): r["score"] for r in conn.execute(
        """SELECT cs.team_id, cs.stat_id, cs.score FROM category_state cs
           WHERE cs.matchup_id=? AND cs.fetched_at=(
             SELECT MAX(fetched_at) FROM category_state
             WHERE matchup_id=cs.matchup_id AND team_id=cs.team_id
               AND stat_id=cs.stat_id AND fetched_at<=?)""", (mid, iso))}


def _print_banked(conn, m, t_before, t_after, home_name, away_name):
    print("\n== banked category_state (as-of each boundary; deltas only) ==")
    b, a = _banked_asof(conn, m["id"], t_before), _banked_asof(conn, m["id"], t_after)
    names = {m["home_team_id"]: home_name, m["away_team_id"]: away_name}
    any_delta = False
    for (tid, sid), va in sorted(a.items()):
        vb = b.get((tid, sid))
        if vb is not None and abs(va - vb) > 1e-9:
            any_delta = True
            print(f"  {names.get(tid, tid):20} {stats.name(sid):5} {vb:g} → {va:g}")
    if not any_delta:
        print("  (flat — banked stats did NOT move; a WP move here is a projection/"
              "roster/schedule change, not live play. Playbook #14.)")


def _print_schedule(conn, m, start, end):
    print("\n== schedule: games gone Final inside the window ==")
    rows = conn.execute(
        """SELECT game_pk, game_date, MIN(became_final_at) f,
                  GROUP_CONCAT(probable_pitcher_name, ' / ') probables
           FROM team_schedule WHERE matchup_period_id=? AND became_final_at>=?
           AND became_final_at<=? GROUP BY game_pk ORDER BY f""",
        (m["matchup_period_id"], start.isoformat(), end.isoformat())).fetchall()
    for r in rows:
        print(f"  {r['f']}  game {r['game_pk']} ({r['game_date']})  "
              f"probables: {r['probables'] or '—'}")
    if not rows:
        print("  (none)")
    print("  NB: team_schedule is CURRENT-state — historical statuses/dates are "
          "overwritten, so a past tick's schedule inputs can't be reconstructed.")


def _print_live_recon(before_d, after_d):
    print("\n== live_recon (QS/SVHD scrape/floor/box + rate verdicts) ==")
    shown = False
    for label, d in (("before", before_d), ("after", after_d)):
        recon = (d or {}).get("live_recon")
        if recon:
            shown = True
            print(f"  {label}: {json.dumps(recon, separators=(',', ':'))[:600]}")
    if not shown:
        print("  (no live_recon on either boundary — pre-telemetry snapshot or no live week)")


def _print_flags(conn, m, start, end):
    print("\n== validation_flags overlapping the window (incl. league-wide) ==")
    rows = conn.execute(
        """SELECT code, severity, matchup_id, detail, first_seen, last_seen,
                  occurrences, resolved FROM validation_flags
           WHERE (matchup_id=? OR matchup_id IS NULL) AND last_seen>=? AND first_seen<=?
           ORDER BY severity, first_seen""",
        (m["id"], start.isoformat(), end.isoformat())).fetchall()
    for r in rows:
        scope = f"m{r['matchup_id']}" if r["matchup_id"] else "league"
        state = "resolved" if r["resolved"] else "OPEN"
        print(f"  [{r['severity']}] {r['code']} ({scope}, ×{r['occurrences']}, {state}) "
              f"{(r['detail'] or '')[:120]}")
    if not rows:
        print("  (none — but absence of flags is not proof of health; see 2026-06-04)")


def _print_published(mid, start, end):
    print("\n== published site (what the owner could actually SEE) ==")
    try:
        data = json.loads(DOCS_DATA_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        print("  docs/data.json unreadable")
        return
    # History is split out of data.json into per-week files (see cli.publish);
    # find the matchup's week in data.json, then read its history file.
    period = None
    for wk in data.get("weeks", []):
        for mm in wk.get("matchups", []):
            if mm.get("matchup_id") == mid:
                period = wk["matchup_period_id"]
    if period is None:
        print("  matchup not in data.json")
        return
    try:
        hist_file = json.loads(
            (DOCS_DATA_JSON.parent / "history" / f"{period}.json").read_text())
        hist = hist_file.get("history", {}).get(str(mid)) or []
    except (OSError, json.JSONDecodeError):
        print(f"  docs/history/{period}.json unreadable")
        return
    pts = [p for p in hist
           if start <= datetime.fromisoformat(p["computed_at"]) <= end]
    print(f"  {len(pts)} downsampled point(s) fell inside the window "
          f"(chart keeps ~200/matchup — a brief raw-DB blip may not be one of them):")
    for p in pts[:20]:
        print(f"    {p['computed_at']}  home_wp={p['home_wp']:.1%}")
    if len(pts) > 20:
        print(f"    … {len(pts) - 20} more")
    if not pts:
        print("  → the chart shows NOTHING inside this window; whatever the owner saw "
              "is the nearest surviving points outside it. Don't explain a raw-only blip.")


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--utc"]
    assume_utc = "--utc" in sys.argv
    if len(args) != 3:
        sys.exit("usage: wp_diff.py <matchup_id|team-name> <start> <end> [--utc]")
    start, end = _parse_when(args[1], assume_utc), _parse_when(args[2], assume_utc)
    if end <= start:
        sys.exit("end must be after start")

    conn = _connect_ro()
    m = _resolve_matchup(conn, args[0], start, end)
    home_name, away_name = _team_names(conn, m)

    print(f"== window ==")
    print(f"  matchup {m['id']} period {m['matchup_period_id']}: "
          f"{away_name} (away) @ {home_name} (home)  winner={m['winner'] or 'UNDECIDED'}")
    tz_note = "UTC (--utc)" if assume_utc else "Europe/Oslo local (default; pass --utc to change)"
    print(f"  naive inputs read as {tz_note}")
    print(f"  start: {_fmt_both(start)}\n  end:   {_fmt_both(end)}")

    snaps = conn.execute(
        "SELECT computed_at, home_wp, away_wp, details_json, edited FROM wp_snapshots "
        "WHERE matchup_id=? ORDER BY computed_at", (m["id"],)).fetchall()
    before = None
    for r in snaps:
        if _snap_t(r) <= start:
            before = r
    in_window = [r for r in snaps if start <= _snap_t(r) <= end]
    if before is None:
        if not in_window:
            sys.exit("no snapshots at or before the window — wrong week or window?")
        before = in_window[0]
        print("  ⚠ no snapshot at/before start; using first in-window tick as 'before'")
    after = in_window[-1] if in_window else before
    series = ([before] if before not in in_window else []) + in_window

    _print_series(series, start, end)

    # biggest single tick inside the window — often the real event boundary
    biggest = None
    for prev, cur in zip(series, series[1:]):
        d = abs(cur["home_wp"] - prev["home_wp"])
        if biggest is None or d > biggest[0]:
            biggest = (d, prev, cur)
    if biggest and biggest[0] >= 0.02:
        print(f"\n  biggest tick: {biggest[1]['computed_at']} → {biggest[2]['computed_at']} "
              f"(Δ {biggest[2]['home_wp'] - biggest[1]['home_wp']:+.1%}); decomposition "
              f"below spans the whole window — rerun on just that tick to isolate it")

    before_d, after_d = _details(before), _details(after)
    print(f"\n== decomposition: {before['computed_at']} → {after['computed_at']} ==")
    print(f"  home_wp {before['home_wp']:.1%} → {after['home_wp']:.1%} "
          f"({after['home_wp'] - before['home_wp']:+.1%})")
    _print_categories(before_d, after_d, home_name, away_name)
    _budget_diff(before_d, after_d, "home", home_name)
    _budget_diff(before_d, after_d, "away", away_name)
    _print_banked(conn, m, _snap_t(before), _snap_t(after), home_name, away_name)
    _print_schedule(conn, m, start, end)
    _print_live_recon(before_d, after_d)
    _print_flags(conn, m, start, end)
    _print_published(m["id"], start, end)

    print("""
== before you attribute (the contract) ==
  • Weigh EVERY candidate above; a LEADER-FLIPPED category dominates within-lean shifts.
  • Salient ≠ causal. A removed player's impact is MARGINAL (optimizer backfills).
  • Mechanics claims (slots, roles, optimizer) → verify in code, cite the line.
  • Anything not confirmed by data above = say "hypothesis:", explicitly.
  • ±1pp/tick at p≈0.5 is Monte Carlo noise. Log the outcome in INVESTIGATIONS.md.""")


if __name__ == "__main__":
    main()
