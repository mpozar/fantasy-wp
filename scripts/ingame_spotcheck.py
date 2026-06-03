"""Spot-check the in-game QS/SVHD projections during live games.

Read-only (safe to run anytime, even while cron is writing — WAL). For every
rostered pitcher currently in a live game, prints their live line, the game
context, and what the model is projecting right now (QS for starters, SVHD for
relievers) so you can eyeball whether it's sane.

    .venv/bin/python scripts/ingame_spotcheck.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.sim import _norm_name


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

        # game_pk -> live game context (score/inning), current period.
        games = {}
        for g in conn.execute(
            "SELECT game_pk, current_inning, inning_state, team_runs, opponent_runs "
            "FROM team_schedule WHERE matchup_period_id=? AND game_status='In Progress'",
            (period,),
        ).fetchall():
            games[g["game_pk"]] = g

        # normalized name -> {fantasy team, role, exp_qs, exp_svhd} from the
        # latest snapshot's budgets (these carry the in-game-overridden values).
        teams = {t["id"]: t["name"] for t in conn.execute("SELECT id, name FROM teams")}
        proj, snap_at = {}, None
        for m in conn.execute(
            "SELECT * FROM matchups WHERE matchup_period_id=?", (period,)
        ).fetchall():
            s = conn.execute(
                "SELECT computed_at, details_json FROM wp_snapshots "
                "WHERE matchup_id=? ORDER BY computed_at DESC LIMIT 1",
                (m["id"],),
            ).fetchone()
            if not s or not s["details_json"]:
                continue
            snap_at = max(snap_at or s["computed_at"], s["computed_at"])
            d = json.loads(s["details_json"])
            for side, team_id in (("home_budgets", m["home_team_id"]),
                                  ("away_budgets", m["away_team_id"])):
                for b in d.get(side, []):
                    proj[_norm_name(b["name"])] = {
                        "team": teams.get(team_id), "role": b["role"],
                        "exp_qs": b.get("exp_qs"), "exp_svhd": b.get("exp_svhd"),
                    }
    finally:
        conn.close()

    rows = []
    for lp in live:
        p = proj.get(_norm_name(lp["name"]))
        if not p:
            continue  # pitcher not on any roster — skip
        g = games.get(lp["game_pk"], {})
        margin = (g.get("team_runs") or 0) - (g.get("opponent_runs") or 0)
        rows.append((p["team"], lp["name"], p["role"], lp,
                     g.get("current_inning"), g.get("inning_state"), margin, p))

    print(f"\n=== Live in-game pitcher projections (period {period}, "
          f"as of {snap_at}) ===")
    print(f"{len(live)} pitchers in live games, {len(rows)} rostered:\n")
    if not rows:
        print("  (no rostered pitchers currently in a live game)")
        return
    for team, name, role, lp, inning, state, margin, p in sorted(rows, key=lambda r: r[0] or ""):
        status = "IN " if lp["is_last"] else "OUT"
        ctx = f"inn {inning} {state or ''}, margin {margin:+d}"
        line = f"{_ip(lp['outs'])} IP, {lp['er']} ER, {lp['k']} K"
        if role == "SP":
            metric = f"proj QS {p['exp_qs']}"
        else:
            metric = f"proj SVHD {p['exp_svhd']}"
        print(f"  [{team:<18}] {name:<22} {role} {status}  {line:<20} | "
              f"{ctx:<26} | {metric}")
    print()


if __name__ == "__main__":
    main()
