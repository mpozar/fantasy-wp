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

# ESPN lineup slot IDs we care about.
BENCH_SLOT = 16
IL_SLOT = 17

# Hitter slot IDs ESPN exposes for MLB leagues. The set is intentionally
# broad — `lineupSlotCounts` from the league settings tells us which ones
# are actually configured for this league.
HITTER_SLOT_IDS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 19}

# Injury statuses for players still expected to play (per user choice — we
# treat DAY_TO_DAY / QUESTIONABLE / PROBABLE as playing through).
PLAYABLE_INJURY_STATUSES = {
    "", "ACTIVE", "NORMAL", "DAY_TO_DAY", "QUESTIONABLE", "PROBABLE",
}


def _is_playable(p: dict) -> bool:
    """Whether a roster entry can contribute production this week.

    Rules:
      - IL slot: never plays this week.
      - injury_status indicates definitely-out (IL, OUT, SUSPENDED, etc.):
        also skip.
      - Everything else (active slot, BE slot, day-to-day / questionable):
        playable. Pitchers cycle through bench naturally and hitters get
        slot-by-slot allocation downstream via the lineup optimizer.
    """
    if p.get("lineup_slot_id") == IL_SLOT:
        return False
    inj = (p.get("injury_status") or "").upper()
    return inj in PLAYABLE_INJURY_STATUSES

# Fallback RP appearance rate when ROS projection or team-total games are
# missing. Real per-player rates range ~0.1 (mop-up) to ~0.5 (workhorse
# closer), so this fallback is intentionally middle-of-the-pack.
RP_APPEARANCE_RATE = 0.40

# Cap on per-team-game SP start rate when estimating from ROS projections.
# Real MLB rotations top out near 1-start-per-5-team-games (20%); slight slack
# above that to allow for occasional spot starts when other arms are unavailable.
MAX_SP_RATE = 0.21


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


# ── In-progress game scaling ──────────────────────────────────────────
#
# An in-progress game has already produced some of its cat stats (already
# baked into the live cumulative state from ESPN). To avoid double-counting,
# we scale that game's *remaining* production for each role. Different
# roles consume innings differently:
#
#   - Hitters: production is spread across all 9 innings (cycle through
#     the lineup ~3-4 times per game).
#   - SPs: typically pulled around innings 5-6, so their remaining work
#     shrinks fast and hits zero past their expected exit.
#   - RPs: most of their work happens in the back of the game (innings
#     6-9), so their remaining stays near 1.0 until late innings.
#
# Anything other than `Final` and `In Progress` (e.g. Scheduled, Pre-Game,
# Warmup, Postponed) is treated as a full game ahead.

# Innings of a typical game in which RPs do their work. Used as the
# denominator for the RP "innings of bullpen work remaining" calculation.
RP_WORK_INNINGS = 4
RP_WORK_STARTS_AT = 6  # earliest inning we expect RP work


def _elapsed_innings(g: dict) -> float:
    """Best-effort elapsed-innings count for an in-progress game. Uses the
    half-inning state when available; falls back to mid-inning assumption."""
    cur = g.get("current_inning")
    if cur is None:
        return 0.0
    state = (g.get("inning_state") or "").lower()
    # "Top N":    inning N starting → (N-1) innings completed
    # "Middle N": top of N done, bottom not started → N-0.5
    # "Bottom N": bottom of N happening → N-0.5
    # "End N":    inning N fully done → N
    if state == "top":
        return float(cur) - 1.0
    if state == "end":
        return float(cur)
    # "Middle"/"Bottom" or unknown: assume top half completed
    return float(cur) - 0.5


def _hitter_factor(g: dict) -> float:
    status = g.get("game_status")
    if status == "Final":
        return 0.0
    if g.get("current_inning") is None:
        return 1.0
    elapsed = _elapsed_innings(g)
    return max(0.0, (9.0 - elapsed) / 9.0)


def _rp_factor(g: dict) -> float:
    status = g.get("game_status")
    if status == "Final":
        return 0.0
    if g.get("current_inning") is None:
        return 1.0
    elapsed = _elapsed_innings(g)
    # RPs only start consuming "remaining" once the game enters the
    # bullpen window (~inning 6). Before that, full appearance ahead.
    rp_elapsed = max(0.0, elapsed - (RP_WORK_STARTS_AT - 1))
    return max(0.0, min(1.0, (RP_WORK_INNINGS - rp_elapsed) / RP_WORK_INNINGS))


def _sp_factor(g: dict, sp_exit_inning: float) -> float:
    status = g.get("game_status")
    if status == "Final":
        return 0.0
    if g.get("current_inning") is None:
        return 1.0
    elapsed = _elapsed_innings(g)
    if sp_exit_inning <= 0:
        return 0.0
    return max(0.0, (sp_exit_inning - elapsed) / sp_exit_inning)


def _hitter_remaining_units(team_id: int,
                            schedule_by_team: dict[int, list[dict]]) -> float:
    return sum(_hitter_factor(g) for g in schedule_by_team.get(team_id, []))


def _rp_remaining_units(team_id: int,
                        schedule_by_team: dict[int, list[dict]]) -> float:
    return sum(_rp_factor(g) for g in schedule_by_team.get(team_id, []))


def _remaining_team_games(team_id: int, schedule_by_team: dict[int, list[dict]]) -> int:
    """Integer count of non-Final games. Used by the future-week SP
    estimator where there's no inning data anyway."""
    games = schedule_by_team.get(team_id, [])
    return sum(1 for g in games if g.get("game_status") != "Final")


def _probable_starts_for(player_name: str, team_id: int,
                         schedule_by_team: dict[int, list[dict]],
                         sp_exit_inning: float) -> float:
    """Sum of SP factors over games where this pitcher is the probable
    starter. Returns a float in [0, n_probable_games] — partial credit for
    in-progress games that the SP is currently pitching."""
    target = _norm_name(player_name)
    if not target:
        return 0.0
    total = 0.0
    for g in schedule_by_team.get(team_id, []):
        if _norm_name(g.get("probable_pitcher_name")) == target:
            total += _sp_factor(g, sp_exit_inning)
    return total


def _has_pitcher_ros(ros: dict) -> bool:
    return (ros.get(STAT_GS, 0) or 0) > 0 or (ros.get(STAT_PITCH_GP, 0) or 0) > 0


def _has_hitter_ros(ros: dict) -> bool:
    return (ros.get(STAT_HIT_G, 0) or 0) > 0


def _is_two_way(p: dict) -> bool:
    ros = p.get("ros_stats") or {}
    return _has_pitcher_ros(ros) and _has_hitter_ros(ros)


def _is_probable_starter_on(p: dict, game_date: str,
                            schedule_by_team: dict[int, list[dict]]) -> bool:
    """Is this player the probable pitcher for one of their team's games on
    the given date? Used by the optimizer to block two-way players from
    being slotted as hitters on days they're scheduled to start."""
    target = _norm_name(p.get("full_name"))
    if not target:
        return False
    for g in schedule_by_team.get(p["pro_team_id"], []):
        if g.get("game_date") != game_date:
            continue
        if _norm_name(g.get("probable_pitcher_name")) == target:
            return True
    return False


def _hitter_per_game_impact(p: dict) -> float:
    """Crude one-number per-game impact for the lineup optimizer. Uses ROS
    rates so the comparison is on the same basis across hitters."""
    ros = p.get("ros_stats") or {}
    g = ros.get(STAT_HIT_G) or 0
    if g <= 0:
        return 0.0
    r = ros.get(STAT_R) or 0
    h = ros.get(STAT_H) or 0
    hr = ros.get(STAT_HR) or 0
    sb = ros.get(STAT_SB) or 0
    # Same shape as the front-end impactScore — R-heavy with some H/SB/HR.
    return (r + 0.6 * h + 0.3 * sb + 0.5 * hr) / g


def _is_hitter_candidate(p: dict) -> bool:
    """A roster entry is a hitter candidate if they have a hitter default
    position OR a positive hitter ROS projection (the two-way case — Ohtani
    has default_position_id=10 plus pitcher stats, but his hitter side still
    needs to flow through the optimizer)."""
    pos = p.get("default_position_id")
    if pos not in (1, 11):
        return True
    return _has_hitter_ros(p.get("ros_stats") or {})


def _hitter_days_slotted(roster: list[dict],
                         schedule_by_team: dict[int, list[dict]],
                         lineup_slot_counts: dict[int, int]) -> dict[int, float]:
    """For each hitter, sum of in-progress factors across days they win a
    lineup slot. Greedy by per-game impact; honors slot eligibility and
    league-configured slot counts.

    Two-way players (e.g. Ohtani) are skipped as hitters on days they're
    listed as the probable starter for their team — they can't bat that day.

    The greedy is good enough at ~10-12 hitters × ~10 slots — exact bipartite
    matching would only differ by a few % and isn't worth the complexity.
    """
    units: dict[int, float] = {
        p["player_id"]: 0.0 for p in roster
        if _is_playable(p) and _is_hitter_candidate(p)
    }
    if not units:
        return units

    # Restrict to slots configured for this league and that hitters can fill.
    hitter_slot_counts = {
        slot: cnt for slot, cnt in lineup_slot_counts.items()
        if slot in HITTER_SLOT_IDS and cnt > 0
    }
    if not hitter_slot_counts:
        return units

    hitters = [
        p for p in roster
        if _is_playable(p) and _is_hitter_candidate(p)
    ]

    # All dates that appear in the team schedule (sorted for deterministic order).
    dates = sorted({
        g.get("game_date")
        for games in schedule_by_team.values()
        for g in games
        if g.get("game_date")
    })

    for date in dates:
        candidates = []
        for p in hitters:
            team_games_today = [
                g for g in schedule_by_team.get(p["pro_team_id"], [])
                if g.get("game_date") == date
            ]
            if not team_games_today:
                continue
            # Two-way players starting on the mound today can't bat.
            if _is_probable_starter_on(p, date, schedule_by_team):
                continue
            factor = max(_hitter_factor(g) for g in team_games_today)
            if factor <= 0:
                continue
            eligible = [s for s in (p.get("eligible_slots") or [])
                        if s in hitter_slot_counts]
            if not eligible:
                continue
            candidates.append({
                "player_id": p["player_id"],
                "factor": factor,
                "eligible": eligible,
                "impact": _hitter_per_game_impact(p),
            })

        candidates.sort(key=lambda c: -c["impact"])
        slots_remaining = dict(hitter_slot_counts)
        for c in candidates:
            for slot in c["eligible"]:
                if slots_remaining.get(slot, 0) > 0:
                    slots_remaining[slot] -= 1
                    units[c["player_id"]] += c["factor"]
                    break

    return units


def build_budgets(roster: list[dict],
                  schedule_by_team: dict[int, list[dict]],
                  estimate_sp_starts: bool = False,
                  team_total_ros_games: dict[int, int] | None = None,
                  lineup_slot_counts: dict[int, int] | None = None,
                  ) -> list[Budget]:
    """Convert a roster + schedule into per-player production budgets.

    Inclusion rules:
      - IL slot or definitely-out injury status → skipped.
      - All other rostered pitchers (BE included) → considered. Their
        per-week units come from probable pitchers (current week SPs) or
        the ROS-rate estimator (future-week SPs and all RPs).
      - Hitters → run through the per-day lineup optimizer; their units
        are the sum of days they win a slot.

    When `estimate_sp_starts` is True (future weeks: no probable pitchers),
    SP starts are estimated as `ros_gs * (week_games / total_ros_games)`.
    """
    team_total_ros_games = team_total_ros_games or {}
    lineup_slot_counts = lineup_slot_counts or {}
    hitter_units = _hitter_days_slotted(roster, schedule_by_team, lineup_slot_counts)

    out: list[Budget] = []
    for p in roster:
        if not _is_playable(p):
            continue
        ros = p["ros_stats"]
        pos = p["default_position_id"]
        team_id = p["pro_team_id"]

        # ── Pitcher budget ─────────────────────────────────────────────
        if _has_pitcher_ros(ros):
            # Classify SP vs RP by projected usage, not ESPN's
            # defaultPositionId — handles RP-eligible swingmen and two-way
            # players (Ohtani has pos=10 but gs/gp=1.0 → SP).
            gs_ros = ros.get(STAT_GS) or 0
            gp_ros = ros.get(STAT_PITCH_GP) or 0
            if gp_ros > 0:
                is_sp = (gs_ros / gp_ros) > 0.5
            else:
                is_sp = (pos == 1)

            if is_sp:
                if estimate_sp_starts:
                    team_games = _remaining_team_games(team_id, schedule_by_team)
                    total_ros = team_total_ros_games.get(team_id, 0)
                    if total_ros > 0 and gs_ros > 0 and team_games > 0:
                        rate = min(gs_ros / total_ros, MAX_SP_RATE)
                        sp_units = rate * team_games
                    else:
                        sp_units = 0
                else:
                    if gs_ros > 0:
                        avg_outs_per_start = (ros.get(STAT_OUTS, 0) or 0) / gs_ros
                        sp_exit_inning = max(1.0, avg_outs_per_start / 3.0 + 1.0)
                    else:
                        sp_exit_inning = 6.0
                    sp_units = _probable_starts_for(
                        p["full_name"], team_id, schedule_by_team, sp_exit_inning,
                    )
                denom_p = gs_ros
                role_p = "SP"
                units_p = sp_units
            else:
                rp_remaining = _rp_remaining_units(team_id, schedule_by_team)
                total_ros = team_total_ros_games.get(team_id, 0)
                if total_ros > 0 and gp_ros > 0:
                    units_p = (gp_ros / total_ros) * rp_remaining
                else:
                    units_p = rp_remaining * RP_APPEARANCE_RATE
                denom_p = gp_ros
                role_p = "RP"

            budget = _make_budget(p, ros, units_p, denom_p, PITCHER_COUNTERS, role_p)
            if budget:
                out.append(budget)
        else:
            sp_units = 0  # for the two-way hitter-day adjustment below

        # ── Hitter budget ──────────────────────────────────────────────
        if _has_hitter_ros(ros):
            units_h = hitter_units.get(p["player_id"], 0.0)
            # Two-way players in future weeks: the optimizer slotted them
            # every day (no probable pitchers to block their pitching
            # days), so subtract expected SP days here to avoid double-
            # counting. Current-week two-ways already had their start days
            # filtered out inside the optimizer, so no adjustment needed.
            if estimate_sp_starts and _has_pitcher_ros(ros):
                units_h = max(0.0, units_h - sp_units)
            denom_h = ros.get(STAT_HIT_G) or 0
            budget = _make_budget(p, ros, units_h, denom_h, HITTER_COUNTERS, "HIT")
            if budget:
                out.append(budget)

    return out


def _make_budget(p: dict, ros: dict, units: float, denom: float,
                 counters: list[int], role: str) -> Budget | None:
    if denom <= 0 or units <= 0:
        return None
    expected: dict[int, float] = {}
    for stat_id in counters:
        ros_v = ros.get(stat_id)
        if ros_v is None or ros_v <= 0:
            continue
        expected[stat_id] = (ros_v / denom) * units
    if not expected:
        return None
    return Budget(
        player_id=p["player_id"],
        name=p["full_name"],
        role=role,
        units=units,
        expected=expected,
    )


# ── Score a single simulated matchup ──

def _decide(home_counters: dict,
            away_counters: dict) -> tuple[str, dict[int, str]]:
    """Return (matchup_winner, per_cat) where per_cat maps stat_id to
    'HOME' | 'AWAY' | 'TIE'."""
    per_cat: dict[int, str] = {}
    home_cats = 0
    away_cats = 0
    for stat_id, reversed_ in CATEGORIES:
        h = _cat_value(home_counters, stat_id)
        a = _cat_value(away_counters, stat_id)
        if h == a:
            per_cat[stat_id] = "TIE"
            continue
        home_better = (h < a) if reversed_ else (h > a)
        if home_better:
            per_cat[stat_id] = "HOME"
            home_cats += 1
        else:
            per_cat[stat_id] = "AWAY"
            away_cats += 1
    if home_cats > away_cats:
        return "HOME", per_cat
    if away_cats > home_cats:
        return "AWAY", per_cat
    # Categories tied — tiebreaker on hits
    h_tb = _cat_value(home_counters, TIEBREAKER_STAT_ID)
    a_tb = _cat_value(away_counters, TIEBREAKER_STAT_ID)
    if h_tb > a_tb:
        return "HOME", per_cat
    if a_tb > h_tb:
        return "AWAY", per_cat
    return "TIE", per_cat


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
             n_sims: int = DEFAULT_SIMS,
             estimate_sp_starts: bool = False,
             team_total_ros_games: dict[int, int] | None = None,
             lineup_slot_counts: dict[int, int] | None = None,
             ) -> tuple[float, float, dict]:
    home_budgets = build_budgets(
        inputs.home_roster, schedule_by_team,
        estimate_sp_starts=estimate_sp_starts,
        team_total_ros_games=team_total_ros_games,
        lineup_slot_counts=lineup_slot_counts,
    )
    away_budgets = build_budgets(
        inputs.away_roster, schedule_by_team,
        estimate_sp_starts=estimate_sp_starts,
        team_total_ros_games=team_total_ros_games,
        lineup_slot_counts=lineup_slot_counts,
    )

    home_wins = 0
    away_wins = 0
    ties = 0
    cat_counts: dict[int, dict[str, int]] = {
        stat_id: {"HOME": 0, "AWAY": 0, "TIE": 0} for stat_id, _ in CATEGORIES
    }
    # Sum each side's underlying counters across sims so we can report the
    # expected end-of-matchup value per category. For rate stats (OPS/ERA/
    # WHIP) we use ratio-of-expectations (derive from averaged counters)
    # rather than expectation-of-ratios — the latter explodes when any sim
    # has near-zero innings.
    counter_sums_h: dict[int, float] = {}
    counter_sums_a: dict[int, float] = {}
    for _ in range(n_sims):
        h = _simulate_team(inputs.home_state, home_budgets)
        a = _simulate_team(inputs.away_state, away_budgets)
        w, per_cat = _decide(h, a)
        if w == "HOME":
            home_wins += 1
        elif w == "AWAY":
            away_wins += 1
        else:
            ties += 1
        for stat_id, outcome in per_cat.items():
            cat_counts[stat_id][outcome] += 1
        for sid, v in h.items():
            counter_sums_h[sid] = counter_sums_h.get(sid, 0.0) + v
        for sid, v in a.items():
            counter_sums_a[sid] = counter_sums_a.get(sid, 0.0) + v

    home_wp = home_wins / n_sims
    away_wp = away_wins / n_sims

    def budget_summary(bs: list[Budget]) -> list[dict]:
        out = []
        for b in bs:
            rec = {
                "player_id": b.player_id,
                "name": b.name,
                "role": b.role,
                "units": round(b.units, 2),
            }
            if b.role == "HIT":
                exp_ab = b.expected.get(STAT_AB, 0)
                rec.update({
                    "exp_h":   round(b.expected.get(STAT_H, 0), 1),
                    "exp_hr":  round(b.expected.get(STAT_HR, 0), 2),
                    "exp_r":   round(b.expected.get(STAT_R, 0), 1),
                    "exp_sb":  round(b.expected.get(STAT_SB, 0), 2),
                    # Per-batter OPS only meaningful with a real AB budget;
                    # null otherwise so the UI can hide it.
                    "exp_ops": round(derive_ops(b.expected), 3) if exp_ab >= 1 else None,
                })
            else:  # SP or RP
                exp_outs = b.expected.get(STAT_OUTS, 0)
                rec.update({
                    "exp_k":    round(b.expected.get(STAT_K, 0), 1),
                    "exp_outs": round(exp_outs, 1),
                    "exp_qs":   round(b.expected.get(STAT_QS, 0), 2),
                    "exp_svhd": round(b.expected.get(STAT_SVHD, 0), 2),
                })
                # ERA/WHIP need at least ~1 IP of expected production to be
                # informative — otherwise it's noise from a 1-out projection.
                if exp_outs >= 3:
                    rec["exp_era"]  = round(derive_era(b.expected), 2)
                    rec["exp_whip"] = round(derive_whip(b.expected), 2)
                else:
                    rec["exp_era"]  = None
                    rec["exp_whip"] = None
            out.append(rec)
        return out

    avg_h = {sid: s / n_sims for sid, s in counter_sums_h.items()}
    avg_a = {sid: s / n_sims for sid, s in counter_sums_a.items()}
    category_wp = [
        {
            "stat_id": stat_id,
            "home_wins": cat_counts[stat_id]["HOME"],
            "away_wins": cat_counts[stat_id]["AWAY"],
            "ties": cat_counts[stat_id]["TIE"],
            "home_avg": _cat_value(avg_h, stat_id),
            "away_avg": _cat_value(avg_a, stat_id),
        }
        for stat_id, _ in CATEGORIES
    ]

    details = {
        "model": MODEL_VERSION,
        "n_sims": n_sims,
        "home_wins": home_wins,
        "away_wins": away_wins,
        "ties": ties,
        "category_wp": category_wp,
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
               p.full_name, p.pro_team_id, p.default_position_id,
               p.eligible_slots_json, p.injury_status
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
        eligible: list[int] = []
        if r["eligible_slots_json"]:
            try:
                parsed = json.loads(r["eligible_slots_json"])
                eligible = [int(s) for s in parsed]
            except (json.JSONDecodeError, ValueError, TypeError):
                eligible = []
        roster.append({
            "player_id": r["player_id"],
            "lineup_slot_id": r["lineup_slot_id"],
            "status": r["status"],
            "full_name": r["full_name"],
            "pro_team_id": r["pro_team_id"],
            "default_position_id": r["default_position_id"],
            "eligible_slots": eligible,
            "injury_status": r["injury_status"],
            "ros_stats": {row["stat_id"]: row["value"] for row in ros},
        })
    return roster


def load_total_remaining_games(conn: sqlite3.Connection,
                               from_period_id: int,
                               to_period_id: int) -> dict[int, int]:
    """Total scheduled games per pro team across an inclusive range of
    matchup periods. Used by future-week sims to estimate per-SP weekly
    starts as a share of season-remaining games."""
    rows = conn.execute(
        """
        SELECT pro_team_id, COUNT(*) AS n
        FROM team_schedule
        WHERE matchup_period_id BETWEEN ? AND ?
        GROUP BY pro_team_id
        """,
        (from_period_id, to_period_id),
    ).fetchall()
    return {r["pro_team_id"]: r["n"] for r in rows}


def load_schedule_by_team(conn: sqlite3.Connection,
                          matchup_period_id: int) -> dict[int, list[dict]]:
    rows = conn.execute(
        """
        SELECT pro_team_id, game_date, opponent_pro_team_id, is_home,
               probable_pitcher_mlbam_id, probable_pitcher_name, game_status,
               current_inning, inning_state
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
