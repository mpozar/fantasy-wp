"""Monte Carlo simulator (model `mc-v1`).

Given:
  - the live matchup state (per-team cat-by-cat counters from `category_state`)
  - each team's roster + ROS projections (`team_rosters`, `players`,
    `player_projections`)
  - the MLB schedule + probable pitchers for the remaining games in the
    matchup period (`team_schedule`)

…simulates the rest of the matchup N times and returns each team's win
probability. Rate stats (OPS, ERA, WHIP) are aggregated from their
underlying counters — never averaged across players.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import string
from dataclasses import dataclass

MODEL_VERSION = "mc-v1"
DEFAULT_SIMS = 10_000

ROS_SPLIT_ID = 6

# ── ESPN stat IDs ──
STAT_AB         = 0
STAT_H          = 1
STAT_2B         = 3
STAT_3B         = 4
STAT_HR         = 5
STAT_B_BB       = 10
STAT_HBP        = 12
STAT_SF         = 13
STAT_OPS        = 18  # presentational only — we derive from counters
STAT_R          = 20
STAT_SB         = 23
STAT_PITCH_GP   = 32  # pitcher games played
STAT_GS         = 33  # games started
STAT_OUTS       = 34
STAT_P_H        = 37
STAT_P_BB       = 39
STAT_WHIP       = 41  # derived
STAT_ER         = 45
STAT_ERA        = 47  # derived
STAT_K          = 48
STAT_QS         = 63
STAT_HIT_G      = 81  # hitter games played
STAT_SVHD       = 83

HITTER_COUNTERS = [
    STAT_AB, STAT_H, STAT_2B, STAT_3B, STAT_HR,
    STAT_B_BB, STAT_HBP, STAT_SF, STAT_R, STAT_SB,
]
PITCHER_COUNTERS = [
    STAT_OUTS, STAT_P_H, STAT_P_BB, STAT_ER,
    STAT_K, STAT_QS, STAT_SVHD,
]

# Scoring categories: (stat_id, reversed?)
CATEGORIES = [
    (STAT_H,    False),
    (STAT_HR,   False),
    (STAT_R,    False),
    (STAT_SB,   False),
    (STAT_OPS,  False),
    (STAT_K,    False),
    (STAT_QS,   False),
    (STAT_ERA,  True),
    (STAT_WHIP, True),
    (STAT_SVHD, False),
]
TIEBREAKER_STAT_ID = STAT_H

# Slots we exclude from production
EXCLUDED_SLOTS = {16, 17}  # BE, IL

# RP appearance rate as a fraction of team games (rough constant for v1)
RP_APPEARANCE_RATE = 0.60


# ── name matching for probable pitchers ──

def _norm_name(s: str | None) -> str:
    if not s:
        return ""
    return "".join(c for c in s.lower() if c.isalnum())


# ── Poisson sampler ──

def _poisson(lam: float) -> int:
    if lam <= 0:
        return 0
    if lam < 30:
        # Knuth's algorithm
        L = math.exp(-lam)
        k = 0
        p = 1.0
        while True:
            k += 1
            p *= random.random()
            if p <= L:
                return k - 1
    # Normal approximation for large lambda
    return max(0, round(random.gauss(lam, math.sqrt(lam))))


# ── Rate-stat derivation from counters ──

def derive_ops(c: dict[int, float]) -> float:
    AB = c.get(STAT_AB, 0)
    H = c.get(STAT_H, 0)
    BB = c.get(STAT_B_BB, 0)
    HBP = c.get(STAT_HBP, 0)
    SF = c.get(STAT_SF, 0)
    HR = c.get(STAT_HR, 0)
    DB = c.get(STAT_2B, 0)
    TR = c.get(STAT_3B, 0)
    obp_den = AB + BB + HBP + SF
    obp = (H + BB + HBP) / obp_den if obp_den > 0 else 0.0
    slg = (H + DB + 2 * TR + 3 * HR) / AB if AB > 0 else 0.0
    return obp + slg


def derive_era(c: dict[int, float]) -> float:
    ER = c.get(STAT_ER, 0)
    OUTS = c.get(STAT_OUTS, 0)
    if OUTS <= 0:
        # No innings pitched → "infinitely bad" ERA in the comparison sense;
        # use a large number so any opponent with OUTS wins ERA.
        return 999.0
    return ER * 27.0 / OUTS


def derive_whip(c: dict[int, float]) -> float:
    PH = c.get(STAT_P_H, 0)
    PBB = c.get(STAT_P_BB, 0)
    OUTS = c.get(STAT_OUTS, 0)
    if OUTS <= 0:
        return 999.0
    return (PH + PBB) * 3.0 / OUTS


def _cat_value(c: dict[int, float], stat_id: int) -> float:
    if stat_id == STAT_OPS:
        return derive_ops(c)
    if stat_id == STAT_ERA:
        return derive_era(c)
    if stat_id == STAT_WHIP:
        return derive_whip(c)
    return c.get(stat_id, 0)


# ── Per-player budgets ──

@dataclass
class Budget:
    """Expected matchup-remainder production for one player."""
    player_id: int
    name: str
    role: str                              # 'HIT' | 'SP' | 'RP'
    units: float                           # games / starts / appearances remaining
    expected: dict[int, float]             # stat_id → expected counter value


def _remaining_team_games(team_id: int, schedule_by_team: dict[int, list[dict]]) -> int:
    games = schedule_by_team.get(team_id, [])
    return sum(1 for g in games if g.get("game_status") != "Final")


def _probable_starts_for(player_name: str, team_id: int,
                         schedule_by_team: dict[int, list[dict]]) -> int:
    target = _norm_name(player_name)
    if not target:
        return 0
    starts = 0
    for g in schedule_by_team.get(team_id, []):
        if g.get("game_status") == "Final":
            continue
        if _norm_name(g.get("probable_pitcher_name")) == target:
            starts += 1
    return starts


def build_budgets(roster: list[dict],
                  schedule_by_team: dict[int, list[dict]]) -> list[Budget]:
    """Convert a roster + schedule into per-player production budgets."""
    out: list[Budget] = []
    for p in roster:
        if p["lineup_slot_id"] in EXCLUDED_SLOTS:
            continue
        # Skip injured (TEN_DAY_DL, FIFTEEN_DAY_DL, etc.) — unless they're in
        # an active slot anyway, which can happen; the slot decision overrides.
        ros = p["ros_stats"]
        pos = p["default_position_id"]
        team_id = p["pro_team_id"]

        if pos == 1:  # SP
            units = _probable_starts_for(p["full_name"], team_id, schedule_by_team)
            denom = ros.get(STAT_GS) or 0
            counters = PITCHER_COUNTERS
            role = "SP"
        elif pos == 11:  # RP
            team_games = _remaining_team_games(team_id, schedule_by_team)
            units = team_games * RP_APPEARANCE_RATE
            denom = ros.get(STAT_PITCH_GP) or 0
            counters = PITCHER_COUNTERS
            role = "RP"
        else:  # Hitter
            units = _remaining_team_games(team_id, schedule_by_team)
            denom = ros.get(STAT_HIT_G) or 0
            counters = HITTER_COUNTERS
            role = "HIT"

        if denom <= 0 or units <= 0:
            continue

        expected: dict[int, float] = {}
        for stat_id in counters:
            ros_v = ros.get(stat_id)
            if ros_v is None or ros_v <= 0:
                continue
            expected[stat_id] = (ros_v / denom) * units
        if not expected:
            continue

        out.append(Budget(
            player_id=p["player_id"],
            name=p["full_name"],
            role=role,
            units=units,
            expected=expected,
        ))
    return out


# ── Score a single simulated matchup ──

def _decide(home_counters: dict, away_counters: dict) -> str:
    """Return 'HOME', 'AWAY', or 'TIE' (which never happens since we always
    break with hits, but keep for safety)."""
    home_cats = 0
    away_cats = 0
    for stat_id, reversed_ in CATEGORIES:
        h = _cat_value(home_counters, stat_id)
        a = _cat_value(away_counters, stat_id)
        if h == a:
            continue
        home_better = (h < a) if reversed_ else (h > a)
        if home_better:
            home_cats += 1
        else:
            away_cats += 1
    if home_cats > away_cats:
        return "HOME"
    if away_cats > home_cats:
        return "AWAY"
    # Categories tied — tiebreaker on hits
    h_tb = _cat_value(home_counters, TIEBREAKER_STAT_ID)
    a_tb = _cat_value(away_counters, TIEBREAKER_STAT_ID)
    if h_tb > a_tb:
        return "HOME"
    if a_tb > h_tb:
        return "AWAY"
    return "TIE"


# ── Per-sim team-totals draw ──

def _simulate_team(current_state: dict[int, float],
                   budgets: list[Budget]) -> dict[int, float]:
    counters = dict(current_state)
    for b in budgets:
        for stat_id, exp in b.expected.items():
            counters[stat_id] = counters.get(stat_id, 0) + _poisson(exp)
    return counters


# ── Top-level entrypoint ──

@dataclass
class MatchupInputs:
    matchup_id: int
    home_state: dict[int, float]
    away_state: dict[int, float]
    home_roster: list[dict]
    away_roster: list[dict]


def simulate(inputs: MatchupInputs,
             schedule_by_team: dict[int, list[dict]],
             n_sims: int = DEFAULT_SIMS) -> tuple[float, float, dict]:
    home_budgets = build_budgets(inputs.home_roster, schedule_by_team)
    away_budgets = build_budgets(inputs.away_roster, schedule_by_team)

    home_wins = 0
    away_wins = 0
    ties = 0
    for _ in range(n_sims):
        h = _simulate_team(inputs.home_state, home_budgets)
        a = _simulate_team(inputs.away_state, away_budgets)
        w = _decide(h, a)
        if w == "HOME":
            home_wins += 1
        elif w == "AWAY":
            away_wins += 1
        else:
            ties += 1

    home_wp = home_wins / n_sims
    away_wp = away_wins / n_sims

    def budget_summary(bs: list[Budget]) -> list[dict]:
        return [{
            "player_id": b.player_id,
            "name": b.name,
            "role": b.role,
            "units": round(b.units, 2),
            "exp_h": round(b.expected.get(STAT_H, 0), 1),
            "exp_hr": round(b.expected.get(STAT_HR, 0), 2),
            "exp_r": round(b.expected.get(STAT_R, 0), 1),
            "exp_k": round(b.expected.get(STAT_K, 0), 1),
            "exp_outs": round(b.expected.get(STAT_OUTS, 0), 1),
            "exp_qs": round(b.expected.get(STAT_QS, 0), 2),
        } for b in bs]

    details = {
        "model": MODEL_VERSION,
        "n_sims": n_sims,
        "home_wins": home_wins,
        "away_wins": away_wins,
        "ties": ties,
        "home_budgets": budget_summary(home_budgets),
        "away_budgets": budget_summary(away_budgets),
    }
    return home_wp, away_wp, details


# ── DB-loading helpers ──

def load_team_roster(conn: sqlite3.Connection, matchup_period_id: int,
                     fantasy_team_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT tr.player_id, tr.lineup_slot_id, tr.status,
               p.full_name, p.pro_team_id, p.default_position_id, p.injury_status
        FROM team_rosters tr
        JOIN players p ON p.id = tr.player_id
        WHERE tr.matchup_period_id = ? AND tr.fantasy_team_id = ?
        """,
        (matchup_period_id, fantasy_team_id),
    ).fetchall()

    roster = []
    for r in rows:
        ros = conn.execute(
            """
            SELECT stat_id, value
            FROM player_projections
            WHERE player_id = ? AND split_id = ?
            """,
            (r["player_id"], ROS_SPLIT_ID),
        ).fetchall()
        roster.append({
            "player_id": r["player_id"],
            "lineup_slot_id": r["lineup_slot_id"],
            "status": r["status"],
            "full_name": r["full_name"],
            "pro_team_id": r["pro_team_id"],
            "default_position_id": r["default_position_id"],
            "injury_status": r["injury_status"],
            "ros_stats": {row["stat_id"]: row["value"] for row in ros},
        })
    return roster


def load_schedule_by_team(conn: sqlite3.Connection,
                          matchup_period_id: int) -> dict[int, list[dict]]:
    rows = conn.execute(
        """
        SELECT pro_team_id, game_date, opponent_pro_team_id, is_home,
               probable_pitcher_mlbam_id, probable_pitcher_name, game_status
        FROM team_schedule
        WHERE matchup_period_id = ?
        ORDER BY game_date
        """,
        (matchup_period_id,),
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["pro_team_id"], []).append(dict(r))
    return out


def load_latest_state(conn: sqlite3.Connection, matchup_id: int,
                      team_id: int) -> dict[int, float]:
    rows = conn.execute(
        """
        SELECT stat_id, score
        FROM category_state
        WHERE matchup_id=? AND team_id=?
          AND fetched_at = (
              SELECT MAX(fetched_at) FROM category_state
              WHERE matchup_id=? AND team_id=?
          )
        """,
        (matchup_id, team_id, matchup_id, team_id),
    ).fetchall()
    return {r["stat_id"]: r["score"] for r in rows}
