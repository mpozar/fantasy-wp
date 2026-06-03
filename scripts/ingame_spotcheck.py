"""Spot-check the in-game QS/SVHD projections during live games.

Read-only (safe to run anytime, even while cron is writing — WAL). For every
rostered pitcher currently in a live game, prints their live line, the game
context, and the model's *in-game* projection for THIS appearance — i.e. what
`ingame.project_qs` / `project_svhd` returns for their current state (not the
week total, which also includes other starts and obscures the in-game value).

    .venv/bin/python scripts/ingame_spotcheck.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, ingame
from app.sim import (
    _norm_name, STAT_GS, STAT_PITCH_GP, STAT_OUTS, STAT_ER, STAT_QS, STAT_SVHD,
    load_team_roster, MAX_SVHD_RATE,
)


def _ip(outs: int) -> str:
    return f"{outs // 3}.{outs % 3}"


def main() -> None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT matchup_period_id FROM team_rosters "
            "GROUP BY matchup_period_id ORDER BY MAX(fetched_at) DESC LIMIT 1"
        ).fetchone()
        period = row["matchup_period_id"] if row else None

        live = conn.execute("SELECT * FROM live_pitchers").fetchall()
        if not live:
            print("No games in progress right now — nothing to spot-check.")
            return

        games = {}
        for g in conn.execute(
            "SELECT game_pk, current_inning, inning_state, team_runs, opponent_runs "
            "FROM team_schedule WHERE matchup_period_id=? AND game_status='In Progress'",
            (period,),
        ).fetchall():
            games[g["game_pk"]] = dict(g)

        teams = {t["id"]: t["name"] for t in conn.execute("SELECT id, name FROM teams")}
        team_ids = set()
        for m in conn.execute(
            "SELECT home_team_id, away_team_id FROM matchups WHERE matchup_period_id=?",
            (period,),
        ).fetchall():
            team_ids.add(m["home_team_id"]); team_ids.add(m["away_team_id"])

        # norm name -> (fantasy team name, player dict w/ ros_stats)
        roster_by_name = {}
        for tid in team_ids:
            for p in load_team_roster(conn, period, tid):
                roster_by_name[_norm_name(p["full_name"])] = (teams.get(tid), p)
    finally:
        conn.close()

    def project(lp, g):
        team, p = roster_by_name[_norm_name(lp["name"])]
        ros = p["ros_stats"]
        gs = ros.get(STAT_GS) or 0
        gp = ros.get(STAT_PITCH_GP) or 0
        is_sp = (gs / gp > 0.5) if gp else (p.get("default_position_id") == 1)
        margin = (g.get("team_runs") or 0) - (g.get("opponent_runs") or 0)
        if is_sp and gs > 0:
            outs_tot = ros.get(STAT_OUTS) or 0
            state = ingame.StarterState(
                game_status="In Progress", appeared=True, exited=not lp["is_last"],
                outs=lp["outs"], er=lp["er"],
                exp_outs_per_start=outs_tot / gs if gs else 18.0,
                er_per_out=(ros.get(STAT_ER) or 0) / outs_tot if outs_tot else 0.13,
                pregame_qs_rate=(ros.get(STAT_QS) or 0) / gs,
            )
            return team, "SP", f"QS {ingame.project_qs(state):.2f}", margin
        svhd_rate = min((ros.get(STAT_SVHD) or 0) / gp, MAX_SVHD_RATE) if gp else 0.0
        state = ingame.RelieverState(
            game_status="In Progress", appeared=True, exited=not lp["is_last"],
            entered_save_situation=1 <= margin <= 3, lead_intact=margin > 0,
            recorded_out=lp["outs"] >= 1, svhd_rate=svhd_rate,
        )
        return team, "RP", f"SVHD {ingame.project_svhd(state):.2f}", margin

    rows = [(lp, games.get(lp["game_pk"], {})) for lp in live
            if _norm_name(lp["name"]) in roster_by_name]

    print(f"\n=== Live in-game pitcher projections (period {period}) ===")
    print(f"{len(live)} pitchers in live games, {len(rows)} rostered. "
          f"'proj' = the model's projection for THIS appearance.\n")
    if not rows:
        print("  (no rostered pitchers currently in a live game)")
        return
    for lp, g in sorted(rows, key=lambda r: project(r[0], r[1])[0] or ""):
        team, role, metric, margin = project(lp, g)
        status = "IN " if lp["is_last"] else "OUT"
        line = f"{_ip(lp['outs'])} IP, {lp['er']} ER, {lp['k']} K"
        ctx = f"inn {g.get('current_inning')} {g.get('inning_state') or ''}, margin {margin:+d}"
        print(f"  [{team:<20}] {lp['name']:<22} {role} {status}  "
              f"{line:<20} | {ctx:<24} | proj {metric}")
    print()


if __name__ == "__main__":
    main()
