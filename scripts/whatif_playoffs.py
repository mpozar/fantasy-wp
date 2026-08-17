"""What-if playoff odds: pin one undecided matchup to a result and re-price the season.

`app playoffs` answers "where do things stand". This answers "what does this one
game actually swing" — which is the question that gets asked out loud, and which
the odds table alone can't answer, because a result's value to a team is mostly
about which *rival* it eliminates rather than the win itself. Worked example
(2026-08-14, m115 Dragons @ Dawgs): a Dawgs win lifts them only +11.0pp while
costing the Dragons −34.5pp, and the biggest gainer is neither side — Big
Giraffes at +12.6pp, because they are the team actually contesting that spot.

    scripts/whatif_playoffs.py 115                  # both outcomes vs baseline
    scripts/whatif_playoffs.py 115 --winner "Desert Dawgs"
    scripts/whatif_playoffs.py "Desert Dawgs"       # their next undecided matchup

READ-ONLY, deliberately: it never writes `docs/playoffs.json` and never inserts
into `playoff_odds_runs`. A hypothetical must not enter the archive, because
`playoffs.load_odds_history` rebuilds the odds-over-time chart from exactly that
table — one stray what-if row would put a fictional kink in the published chart.

Method mirrors `cli.playoffs_cmd` (same records, same remaining-matchup WP draws,
same bracket samples) with one change: the target matchup's `home_wp` is pinned
to 1.0 / 0.0 instead of its snapshot WP.

Two seeding details make the deltas trustworthy, and both matter:
  * **Paired arms.** Every arm re-runs `simulate_odds` with a fresh
    `Random(seed)`. `simulate_odds` consumes exactly one `rng.random()` per
    remaining matchup regardless of the WP value, so pinning one matchup does
    not desynchronise the stream — the arms stay in lockstep and the ~±0.5pp of
    MC noise you would get from independent runs cancels instead of landing in
    the reported delta.
  * **Seeded bracket samples.** `sim.sample_team_totals` draws from the module
    global RNG with no seed hook, so without `random.seed()` the *baseline*
    moves ~0.5pp between invocations and two separate runs of this script are
    not comparable to each other. Seeded, they are.

So: deltas within one run are precise; absolute levels still carry normal MC
noise and should be read as ~±0.5pp.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, playoffs, sim  # noqa: E402
from app.cli import (LEAGUE_ID, SEASON_ID, _now_iso,  # noqa: E402
                     _current_matchup_period, _last_regular_season_period)

DEFAULT_SEED = 20260814


def _resolve_target(remaining: list[dict], teams: dict[int, dict],
                    target: str) -> dict:
    """Accept a matchup id or a (case-insensitive, partial) team name."""
    if target.isdigit():
        m = next((m for m in remaining if m["matchup_id"] == int(target)), None)
        if m is None:
            raise SystemExit(f"no UNDECIDED matchup with id {target}")
        return m
    hits = [t for t, v in teams.items()
            if target.lower() in (v.get("name") or "").lower()]
    if len(hits) != 1:
        names = ", ".join(sorted((v.get("name") or "") for v in teams.values()))
        raise SystemExit(f"team {target!r} matched {len(hits)} teams. Known: {names}")
    tid = hits[0]
    m = next((m for m in remaining
              if tid in (m["home"], m["away"])), None)
    if m is None:
        raise SystemExit(f"{teams[tid]['name']} has no undecided matchups left")
    return m


def _build_samples(conn, teams: dict[int, dict], team_ids: list[int],
                   current: int, last_reg: int, n_samples: int) -> dict[int, list]:
    ss = conn.execute(
        "SELECT * FROM scoring_settings WHERE league_id=? AND season_id=?",
        (LEAGUE_ID, SEASON_ID)).fetchone()
    if ss is None:
        raise SystemExit("missing league metadata — run `app fetch` first")
    lineup_slot_counts: dict[int, int] = {}
    if ss["lineup_slots_json"]:
        try:
            lineup_slot_counts = {int(k): int(v) for k, v in
                                  json.loads(ss["lineup_slots_json"]).items()}
        except (json.JSONDecodeError, ValueError, TypeError):
            lineup_slot_counts = {}
    ctx = sim.SimContext(
        team_total_ros_games=sim.load_total_remaining_games(conn, current),
        lineup_slot_counts=lineup_slot_counts,
        use_cadence=False)      # anchor is weeks stale by September — as compute --future
    rosters = {t: sim.load_team_roster(conn, current, t) for t in team_ids}
    now = _now_iso()
    out: dict[int, list] = {t: [] for t in team_ids}
    for period_id in range(last_reg + 1, last_reg + playoffs.NUM_PLAYOFF_PERIODS + 1):
        sched = sim.load_schedule_by_team(conn, period_id, now=now)
        if not sched:
            raise SystemExit(f"no team_schedule rows for playoff period {period_id} "
                             "— run `app refresh-schedule` first")
        for t in team_ids:
            budgets = sim.build_budgets(rosters[t], sched, ctx)
            out[t].append([playoffs.totals_to_values(c)
                           for c in sim.sample_team_totals(budgets, n_samples)])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="matchup id, or a team name (uses their next undecided matchup)")
    ap.add_argument("--winner", help="team name to force as winner; omit to show BOTH outcomes")
    ap.add_argument("--sims", type=int, default=playoffs.DEFAULT_SEASON_SIMS)
    ap.add_argument("--samples", type=int, default=playoffs.DEFAULT_TEAM_SAMPLES)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args()

    conn = db.connect()
    conn.row_factory = sqlite3.Row
    try:
        current = _current_matchup_period(conn)
        last_reg = _last_regular_season_period(conn)
        teams = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM teams")}
        team_ids = sorted(teams)
        wins, losses, h2h = playoffs.load_records(conn, team_ids)
        remaining = playoffs.load_remaining(conn)
        tgt = _resolve_target(remaining, teams, args.target)

        # Seeded so the BASELINE is reproducible across invocations, not just
        # across arms within one (see the module docstring).
        random.seed(args.seed)
        samples = _build_samples(conn, teams, team_ids, current, last_reg, args.samples)

        def run(rem):
            return playoffs.simulate_odds(team_ids, wins, h2h, rem, samples,
                                          n_sims=args.sims,
                                          rng=random.Random(args.seed))

        def pin(side):
            return [dict(m, home_wp=(1.0 if side == "home" else 0.0))
                    if m["matchup_id"] == tgt["matchup_id"] else m
                    for m in remaining]

        home_nm, away_nm = teams[tgt["home"]]["name"], teams[tgt["away"]]["name"]
        print(f"m{tgt['matchup_id']}  period {tgt['period']}   "
              f"{away_nm} (away) @ {home_nm} (home)")
        print(f"  current home_wp = {tgt['home_wp']:.3f}"
              f"{'' if tgt['had_snapshot'] else '  (NO SNAPSHOT — coin-flip fallback)'}")

        base = run(remaining)
        if args.winner:
            hits = [t for t in (tgt["home"], tgt["away"])
                    if args.winner.lower() in (teams[t]["name"] or "").lower()]
            if len(hits) != 1:
                raise SystemExit(f"--winner {args.winner!r} must match exactly one of "
                                 f"{home_nm!r} / {away_nm!r}")
            side = "home" if hits[0] == tgt["home"] else "away"
            arms = [(f"{teams[hits[0]]['name']} WINS", run(pin(side)))]
        else:
            arms = [(f"{home_nm} WINS", run(pin("home"))),
                    (f"{away_nm} WINS", run(pin("away")))]

        for metric, label in (("p_playoffs", "P(playoffs)"), ("p_champion", "P(champion)")):
            print(f"\n  {label}")
            hdr = f"    {'team':<26}{'rec':>6}{'base':>8}" + \
                  "".join(f"{('Δ ' + a[0])[:20]:>22}" for a in arms)
            print(hdr)
            print("    " + "-" * (len(hdr) - 4))
            for t in sorted(team_ids, key=lambda t: -base[t][metric]):
                if base[t][metric] < 0.0005 and all(a[1][t][metric] < 0.0005 for a in arms):
                    continue                      # eliminated and stays eliminated
                star = " *" if t in (tgt["home"], tgt["away"]) else "  "
                row = (f"    {teams[t]['name'][:24]:<24}{star}"
                       f"{wins[t]:>3}-{losses[t]:<2}{base[t][metric]*100:>7.1f}")
                for _, arm in arms:
                    d = (arm[t][metric] - base[t][metric]) * 100
                    row += f"{arm[t][metric]*100:>14.1f}{d:>+8.1f}"
                print(row)
        print(f"\n  * = a side of the pinned matchup. Teams at 0% in every arm omitted.")
        print(f"  {args.sims} season sims, {args.samples} team-week samples/round, "
              f"seed {args.seed}. Deltas are paired (see --help); levels ~±0.5pp.")
        print("  READ-ONLY: docs/playoffs.json and playoff_odds_runs are untouched.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
