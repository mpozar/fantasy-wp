"""Does ESPN post a QS/SVHD credit promptly when the game earning it is the LAST
one running?

That single question is what still blocks deleting the QS/SVHD floor (Phase 2 of
the 2026-08-09 plan). Background: the DOM scrape banks a credit the instant a game
reads Final, but it only used to run while games were In Progress — so a credit
from the last game of the night sat un-banked until the ~07:00 UTC settle. The
`floor + box` reconstruction exists solely to bridge that window, and it measured
~8% over-counting against settled week 17. The closing scrape (cli._scrape_due,
2026-08-09) keeps scraping for 20 min after a Final, which should make the
reconstruction unnecessary — *if* ESPN actually posts the credit in that window.

Verifying that by hand needs a night where a rostered pitcher happens to earn a
late credit. 2026-08-09 had none (0 in the final hour, against 1-9 on each of the
five preceding nights), so the check silently didn't happen. This probe removes
the luck: both inputs are durable — `pitcher_final_lines` is a write-once archive
and `category_state` keeps per-tick history — so it can answer for ANY date after
the closing scrape shipped, and can be re-run over a range to accumulate evidence.

    .venv/bin/python scripts/late_credit_probe.py 2026-08-11
    .venv/bin/python scripts/late_credit_probe.py 2026-08-10 2026-08-16

Reads only. Every credit for the date is listed, not just the late ones: the
mid-slate credits are the control group — they bank within a tick or two while
other games are live, so if a LATE credit behaves the same way, the closing scrape
covers the gap and the floor can go.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, ingame, mlb, sim  # noqa: E402
from app.cli import CLOSING_SCRAPE_WINDOW_MIN  # noqa: E402
from app.names import norm_name  # noqa: E402

# A game counts as part of the closing batch when it finalized within this of the
# night's last Final — that's the batch that leaves nothing In Progress behind it,
# and therefore the only one the old code would have missed.
CLOSING_BATCH_MIN = 15


def _iso(t: datetime) -> str:
    return t.isoformat()


def credits_for(conn, game_date: str) -> list[dict]:
    """Every rostered + pitcher-slotted QS/SVHD earned on `game_date`."""
    period = mlb.period_for_date(datetime.fromisoformat(game_date).date())
    teams = [r["id"] for r in conn.execute("SELECT id FROM teams")]
    # One slot map per team for the day; the same gating load_settled_floor applies.
    slots = {}
    for tid in teams:
        slots[tid] = sim.load_active_slots(
            conn, tid, since_date=game_date,
            fallback_roster=sim.load_team_roster(conn, period, tid))
    out = []
    for r in conn.execute(
            "SELECT name, games_started, outs, er, sv, hld, final_at "
            "FROM pitcher_final_lines WHERE game_date=? ORDER BY final_at", (game_date,)):
        is_qs = bool(r["games_started"] and (r["outs"] or 0) >= ingame.QS_OUTS
                     and (r["er"] or 0) <= ingame.QS_MAX_ER)
        svhd = (r["sv"] or 0) + (r["hld"] or 0)
        if not (is_qs or svhd):
            continue
        nm = norm_name(r["name"])
        for tid in teams:
            if slots[tid].get(nm) in sim.PITCHER_SLOTS:
                out.append({"name": r["name"], "team_id": tid, "final_at": r["final_at"],
                            "stat_id": sim.STAT_QS if is_qs else sim.STAT_SVHD,
                            "kind": "QS" if is_qs else "SVHD"})
                break
    return out


def _matchup_for(conn, period: int, team_id: int) -> int | None:
    r = conn.execute(
        "SELECT id FROM matchups WHERE matchup_period_id=? AND (home_team_id=? OR away_team_id=?)",
        (period, team_id, team_id)).fetchone()
    return r["id"] if r else None


def landed_at(conn, matchup_id: int, team_id: int, stat_id: int, after: str) -> str | None:
    """First tick after `after` where this team's cat rose above its value *before*
    the game finalized.

    The baseline must be read at-or-BEFORE `after`: a credit can bank in the very
    same tick the game goes Final, so baselining on the first tick after it hides
    exactly the fast case we are trying to measure. Approximate when a team banks
    two of the same cat within a tick (reports the first increase either way) —
    fine, since we are measuring latency, not attributing a specific credit.

    Takes `matchup_id` rather than joining on period so both queries hit the full
    prefix of idx_category_state_recent. Joining matchups instead drops the leading
    index column and whole-scans a 2.6M-row table per credit (the probe took >2 min).
    """
    base = conn.execute(
        """SELECT score FROM category_state
           WHERE matchup_id=? AND team_id=? AND stat_id=? AND fetched_at <= ?
           ORDER BY fetched_at DESC LIMIT 1""",
        (matchup_id, team_id, stat_id, after)).fetchone()
    if base is None or base["score"] is None:
        return None
    row = conn.execute(
        """SELECT fetched_at FROM category_state
           WHERE matchup_id=? AND team_id=? AND stat_id=? AND fetched_at > ?
                 AND score > ?
           ORDER BY fetched_at LIMIT 1""",
        (matchup_id, team_id, stat_id, after, base["score"])).fetchone()
    return row["fetched_at"] if row else None


def probe(conn, game_date: str) -> None:
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM teams")}
    period = mlb.period_for_date(datetime.fromisoformat(game_date).date())
    # pitcher_final_lines is the write-once archive; team_schedule keeps only the
    # current + future weeks, so it reads empty for any past date.
    last = conn.execute(
        "SELECT MAX(final_at) m FROM pitcher_final_lines WHERE game_date=?",
        (game_date,)).fetchone()["m"]
    if not last:
        print(f"\n{game_date}: no archived Final lines"); return
    batch_start = _iso(datetime.fromisoformat(last) - timedelta(minutes=CLOSING_BATCH_MIN))

    creds = credits_for(conn, game_date)
    print(f"\n{game_date}  (period {period})  last Final {last[11:19]}Z  "
          f"— {len(creds)} rostered credit(s)")
    if not creds:
        print("   NOT EXERCISED — no rostered+slotted QS/SVHD earned this date")
        return
    verdicts = []
    for c in creds:
        mid = _matchup_for(conn, period, c["team_id"])
        land = landed_at(conn, mid, c["team_id"], c["stat_id"], c["final_at"]) if mid else None
        late = c["final_at"] >= batch_start
        if land:
            delay = (datetime.fromisoformat(land)
                     - datetime.fromisoformat(c["final_at"])).total_seconds() / 60
            tag = "LATE " if late else "  mid"
            ok = delay <= CLOSING_SCRAPE_WINDOW_MIN
            mark = "prompt" if ok else "SETTLE-ONLY"
            print(f"   {tag} {c['name']:<20} {c['kind']:<4} {names[c['team_id']][:18]:<18} "
                  f"final {c['final_at'][11:19]} → banked {land[11:19]}  "
                  f"+{delay:5.1f} min  {mark}")
            if late:
                verdicts.append(ok)
        else:
            print(f"   {'LATE ' if late else '  mid'} {c['name']:<20} {c['kind']:<4} "
                  f"{names[c['team_id']][:18]:<18} final {c['final_at'][11:19]} → NEVER BANKED")
            if late:
                verdicts.append(False)
    if not verdicts:
        print("   NOT EXERCISED — credits exist but none in the closing batch")
    elif all(verdicts):
        print(f"   PASS — every closing-batch credit banked within "
              f"{CLOSING_SCRAPE_WINDOW_MIN} min. ESPN posts promptly with nothing live; "
              f"the floor has no job left.")
    else:
        print("   FAIL — a closing-batch credit only appeared at the settle. ESPN does "
              "NOT post promptly once nothing is live; deleting the floor WOULD regress.")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    start = sys.argv[1]
    end = sys.argv[2] if len(sys.argv) > 2 else start
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    try:
        d, last = datetime.fromisoformat(start).date(), datetime.fromisoformat(end).date()
        while d <= last:
            probe(conn, d.isoformat())
            d += timedelta(days=1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
