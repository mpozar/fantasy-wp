#!/usr/bin/env python3
"""Compute league-average variance-to-mean ratios (VMR) per (stat_id, role)
from MLB statsapi game logs.

Pools every (player, game) observation across all active MLB rosters into
counter-stat distributions, then dumps mean/variance/VMR per (stat_id, role).
The output is intended to be pasted into sim.py as a VMR constant.

Run-time: ~1-2 min (one HTTP call per team for rosters, one per player for
game log). All data is public, unauthenticated.

Usage: .venv/bin/python scripts/analyze_variance.py
"""

from __future__ import annotations

import statistics
import sys
import time
from collections import defaultdict

import httpx

# Allow running from repo root
sys.path.insert(0, ".")
from app.teams import MLBAM_TO_ESPN

MLB_URL = "https://statsapi.mlb.com/api/v1"
SEASON = 2026
MIN_OBS_FOR_VMR = 50  # need at least this many observations to estimate VMR


# Our internal stat_id constants for the counters we sample in the sim.
# (Mirrors PITCHER_COUNTERS + HITTER_COUNTERS in app/sim.py.)
STAT = {
    "AB":   0, "H":     1,  "2B":  3,  "3B":   4,  "HR":   5,
    "BB":  10, "HBP":  12,  "SF": 13,  "R":   20,  "SB":  23,
    "OUTS": 34, "P_H": 37,  "P_BB": 39, "ER": 45, "K":   48,
    "QS":  63, "SVHD": 83,
}


def parse_ip(s: str | None) -> int:
    """Parse MLB's IP string (e.g. "5.2" = 5 innings + 2 outs = 17 outs)."""
    if not s:
        return 0
    parts = str(s).split(".")
    innings = int(parts[0]) if parts[0] else 0
    frac = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    return innings * 3 + frac


def fetch_rosters(client: httpx.Client) -> list[dict]:
    players = []
    for team_id in MLBAM_TO_ESPN:
        try:
            r = client.get(f"{MLB_URL}/teams/{team_id}/roster?rosterType=active",
                           timeout=30.0)
            r.raise_for_status()
            for p in r.json().get("roster", []):
                players.append({
                    "id": p["person"]["id"],
                    "name": p["person"]["fullName"],
                    "abbr": p["position"]["abbreviation"],
                })
        except Exception as e:
            print(f"  team {team_id} skipped: {e}", file=sys.stderr)
    return players


def fetch_pitching_log(client: httpx.Client, person_id: int) -> list[dict]:
    r = client.get(
        f"{MLB_URL}/people/{person_id}/stats",
        params={"stats": "gameLog", "season": SEASON, "group": "pitching"},
        timeout=30.0,
    )
    r.raise_for_status()
    stats_arr = r.json().get("stats", [])
    if not stats_arr:
        return []
    return stats_arr[0].get("splits", []) or []


def fetch_hitting_log(client: httpx.Client, person_id: int) -> list[dict]:
    r = client.get(
        f"{MLB_URL}/people/{person_id}/stats",
        params={"stats": "gameLog", "season": SEASON, "group": "hitting"},
        timeout=30.0,
    )
    r.raise_for_status()
    stats_arr = r.json().get("stats", [])
    if not stats_arr:
        return []
    return stats_arr[0].get("splits", []) or []


def main() -> None:
    obs: dict[tuple[int, str], list[float]] = defaultdict(list)

    with httpx.Client() as client:
        print("Fetching active MLB rosters...", file=sys.stderr)
        players = fetch_rosters(client)
        print(f"  got {len(players)} players", file=sys.stderr)

        pitchers = [p for p in players if p["abbr"] in ("P", "TWP")]
        hitters = [p for p in players if p["abbr"] != "P"]

        # ── Pitcher game logs ──
        print(f"Fetching pitcher game logs ({len(pitchers)} pitchers)...",
              file=sys.stderr)
        for i, p in enumerate(pitchers):
            try:
                splits = fetch_pitching_log(client, p["id"])
            except Exception:
                continue
            if len(splits) < 3:
                continue
            # Classify as SP vs RP from GS/G ratio across the season
            total_games = len(splits)
            total_starts = sum(1 for s in splits if (s.get("stat") or {}).get("gamesStarted", 0))
            is_sp = (total_starts / total_games) > 0.5
            role = "SP" if is_sp else "RP"

            for s in splits:
                stat = s.get("stat") or {}
                # For SPs, only count *starts* (their relief appearances are noise)
                # For RPs, only count *appearances* (relief outings)
                gs = stat.get("gamesStarted", 0)
                if is_sp and not gs:
                    continue
                if not is_sp and gs:
                    continue

                outs = parse_ip(stat.get("inningsPitched"))
                obs[(STAT["OUTS"], role)].append(outs)
                obs[(STAT["K"],    role)].append(stat.get("strikeOuts", 0))
                obs[(STAT["ER"],   role)].append(stat.get("earnedRuns", 0))
                obs[(STAT["P_H"],  role)].append(stat.get("hits", 0))
                obs[(STAT["P_BB"], role)].append(stat.get("baseOnBalls", 0))

                if is_sp:
                    # QS: 6+ IP (18+ outs) AND <= 3 ER
                    qs = 1 if (outs >= 18 and stat.get("earnedRuns", 0) <= 3) else 0
                    obs[(STAT["QS"], "SP")].append(qs)

                sv = stat.get("saves", 0)
                hd = stat.get("holds", 0)
                obs[(STAT["SVHD"], role)].append(sv + hd)

            if (i + 1) % 30 == 0:
                print(f"  pitcher {i+1}/{len(pitchers)}", file=sys.stderr)

        # ── Hitter game logs ──
        print(f"Fetching hitter game logs ({len(hitters)} hitters)...",
              file=sys.stderr)
        for i, p in enumerate(hitters):
            try:
                splits = fetch_hitting_log(client, p["id"])
            except Exception:
                continue
            if len(splits) < 10:
                continue

            for s in splits:
                stat = s.get("stat") or {}
                ab = stat.get("atBats", 0)
                if ab == 0:
                    continue  # didn't bat
                obs[(STAT["AB"],   "HIT")].append(ab)
                obs[(STAT["H"],    "HIT")].append(stat.get("hits", 0))
                obs[(STAT["2B"],   "HIT")].append(stat.get("doubles", 0))
                obs[(STAT["3B"],   "HIT")].append(stat.get("triples", 0))
                obs[(STAT["HR"],   "HIT")].append(stat.get("homeRuns", 0))
                obs[(STAT["BB"],   "HIT")].append(stat.get("baseOnBalls", 0))
                obs[(STAT["HBP"],  "HIT")].append(stat.get("hitByPitch", 0))
                obs[(STAT["SF"],   "HIT")].append(stat.get("sacFlies", 0))
                obs[(STAT["R"],    "HIT")].append(stat.get("runs", 0))
                obs[(STAT["SB"],   "HIT")].append(stat.get("stolenBases", 0))

            if (i + 1) % 50 == 0:
                print(f"  hitter {i+1}/{len(hitters)}", file=sys.stderr)

    # ── Compute + print VMR table ──
    print(f"\n# Auto-generated by scripts/analyze_variance.py")
    print(f"# Season {SEASON}, MLB statsapi game logs as of {time.strftime('%Y-%m-%d')}")
    print(f"# variance-to-mean ratio per (stat_id, role)")
    print("VMR = {")
    name_by_id = {v: k for k, v in STAT.items()}
    for key in sorted(obs.keys()):
        stat_id, role = key
        values = obs[key]
        if len(values) < MIN_OBS_FOR_VMR:
            continue
        mean = statistics.mean(values)
        if mean <= 0:
            continue
        var = statistics.variance(values)
        vmr = var / mean
        comment = f"# {name_by_id.get(stat_id, stat_id):<5} {role:<3} n={len(values):>5} μ={mean:.2f} σ²={var:.2f}"
        print(f"    ({stat_id}, {role!r:>5}): {vmr:5.2f},  {comment}")
    print("}")


if __name__ == "__main__":
    main()
