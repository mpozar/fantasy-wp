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
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app import db, ingame

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

# IL statuses that imply a recoverable return. The number is "days from today
# until estimated return" — we can't know exactly when a player was placed
# on IL (ESPN doesn't expose it), so this is a floor estimate that gets
# refined naturally as the cron re-fetches each day.
IL_RETURN_DAYS = {
    "TEN_DAY_IL":      7,
    "TEN_DAY_DL":      7,
    "FIFTEEN_DAY_IL":  10,
    "FIFTEEN_DAY_DL":  10,
    "SIXTY_DAY_IL":    30,
    "SIXTY_DAY_DL":    30,
}


def _utc_today() -> date:
    """Reference 'today' for the projection, in **UTC** — never the host's local
    date. The sim must behave identically regardless of the machine's timezone, so
    we never call `date.today()` (which rolls at the host's local midnight and once
    dropped a day of still-unplayed US games at 00:00 CEST — see INCIDENTS.md). The
    common path keys off game *status* and needs no clock at all; this is only the
    reference for the IL-return heuristic, which is ±days fuzzy anyway."""
    return datetime.now(timezone.utc).date()


def _est_return_date(p: dict, today: date) -> date | None:
    """Estimated date the player can next contribute.

      - today (or earlier) → playable now
      - future date → returning from an IL stint
      - None → indefinitely out (e.g. OUT, INJURY_RESERVE, unknown status)

    ESPN's real return date (`injury_return_override`, from the public injuries
    feed) wins over the fixed-days heuristic when present — it's an actual
    estimated activation date rather than a floor guess, and it also catches IL
    moves/activations the fantasy `injury_status` hasn't reflected yet.
    """
    override = p.get("injury_return_override")
    if override is not None:
        return override
    inj = (p.get("injury_status") or "").upper()
    if inj in PLAYABLE_INJURY_STATUSES:
        return today
    days = IL_RETURN_DAYS.get(inj)
    if days is not None:
        return today + timedelta(days=days)
    return None


def _is_playable(p: dict, as_of: date | None = None) -> bool:
    """Whether this player can contribute at some point in the projection
    window.

      - Active slot: use injury_status. Healthy and DAY_TO_DAY/QUESTIONABLE/
        PROBABLE → playable. IL types → playable with an estimated return
        date (filtered per-game downstream). OUT/INJURY_RESERVE → excluded.
      - IL slot: only include when the status explicitly maps to an IL
        return estimate. A manager-stashed player in IL slot with ACTIVE
        status is treated as the manager intends — out for the period.
    """
    inj = (p.get("injury_status") or "").upper()
    if p.get("lineup_slot_id") == IL_SLOT:
        return inj in IL_RETURN_DAYS
    return _est_return_date(p, as_of or _utc_today()) is not None

# Fallback RP appearance rate when ROS projection or team-total games are
# missing. Real per-player rates range ~0.1 (mop-up) to ~0.5 (workhorse
# closer), so this fallback is intentionally middle-of-the-pack.
RP_APPEARANCE_RATE = 0.40

# Cap on per-team-game SP start rate when estimating from ROS projections.
# Real MLB rotations top out near 1-start-per-5-team-games (20%); slight slack
# above that to allow for occasional spot starts when other arms are unavailable.
# Used only by the flat-rate fallback (when the cadence model has no anchor).
MAX_SP_RATE = 0.21

# Rotation rest-day distribution: P(calendar days between a SP's consecutive
# starts), used by the cadence model to project a pitcher's remaining turns from
# his last/announced start. Measured by scripts/analyze_cadence.py from MLB game
# logs — re-run yearly and paste the result here (like ER_VMR_BY_ROLE). Note the
# 2026 mix is modal *6* days (mean ~5.8): six-man rotations / load management
# have all but eliminated 4-day rest, so two-start weeks are correspondingly rare.
#
# Auto-generated 2026-06-03: 135 SPs, 1181 gaps, mean rest = 5.79 days.
REST_DAY_WEIGHTS = {5: 0.340, 6: 0.544, 7: 0.101, 8: 0.014}

# Most extra (un-probabled) starts the cadence model will project into one week.
# A SP gets at most ~2 starts in a 7-day scoring period.
MAX_EXTRA_STARTS = 2

# MLB statsapi detailedState values that mean a game is over.
_GAME_FINISHED = {"Final", "Game Over", "Completed Early"}

# Cap on per-appearance SV+HLD rate. The ROS SVHD value is derived from the
# player's actual season-to-date rate (with a fallback to ESPN's full-season
# projection rate when sample size is small), so this cap only guards against
# extreme cases — e.g. a hot reliever whose actual rate is > 80% in a small
# sample. Realistic elite high-leverage RPs top out near 0.75-0.80.
MAX_SVHD_RATE = 0.80

# Per-(stat_id, role) variance-to-mean ratios, measured from this season's
# MLB game logs via scripts/analyze_variance.py. Most counter stats are
# essentially Poisson (VMR ≈ 1), so we only override here for the one stat
# that shows meaningful overdispersion: earned runs. Blowup innings push the
# ER distribution above Poisson, by 60% for SPs and 83% for RPs.
ER_VMR_BY_ROLE = {"SP": 1.60, "RP": 1.83}


# ── name matching for probable pitchers ──

def _norm_name(s: str | None) -> str:
    if not s:
        return ""
    # Strip diacritics first so accented spellings match ASCII ones — MLB's
    # probable-pitcher feed uses accents ("Cristopher Sánchez") while ESPN's
    # roster names often don't ("Cristopher Sanchez"). Without this they fail
    # to match and the SP loses credit for a confirmed start.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


# ── Samplers ──

def _neg_binom(mean: float, vmr: float) -> int:
    """Negative-Binomial draw with the given mean and variance-to-mean ratio.

    Implemented as a Gamma-Poisson mixture: λ ~ Gamma(k, mean/k), then
    Poisson(λ). Variance = mean × vmr. Falls back to Poisson for vmr ≤ 1.
    """
    if mean <= 0:
        return 0
    if vmr <= 1.0:
        return _poisson(mean)
    k = mean / (vmr - 1.0)
    rate = random.gammavariate(k, mean / k)
    return _poisson(rate)


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


def _sample_dist(weights: list[float]) -> int:
    """Sample an index from a list of non-negative weights (cumulative draw).
    Used to draw a SP's integer extra-start count from its cadence dist."""
    total = sum(weights)
    if total <= 0:
        return 0
    r = random.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if r < acc:
            return i
    return len(weights) - 1


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


# stat_id → deriver for the three rate cats. Single source of truth: the win
# comparison (`cat_value`), the publish display (`cli._apply_derived_rates`), and
# validate's cross-source check all route rate cats through this.
RATE_DERIVERS = {STAT_OPS: derive_ops, STAT_ERA: derive_era, STAT_WHIP: derive_whip}


def cat_value(c: dict[int, float], stat_id: int) -> float:
    """Category value for the win comparison: rate cats derived from their
    component counters, counting cats read directly."""
    deriver = RATE_DERIVERS.get(stat_id)
    return deriver(c) if deriver else c.get(stat_id, 0)


# ── Per-player budgets ──

@dataclass
class Budget:
    """Expected matchup-remainder production for one player."""
    player_id: int
    name: str
    role: str                              # 'HIT' | 'SP' | 'RP'
    units: float                           # games / starts / appearances remaining
    expected: dict[int, float]             # stat_id → expected counter value
    # SP-only (None otherwise): the stochastic extra-start piece sampled per sim.
    # `expected` above holds only the *fixed* piece (announced probables + any
    # live start); the cadence model's uncertain extra starts are drawn from
    # `extra_dist` (P(k extra starts)) and scaled by `extra_per_start` (per-start
    # stat rates) so all pitching categories move together with the drawn count.
    extra_dist: list[float] | None = None
    extra_per_start: dict[int, float] | None = None


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


def _game_after_return(g: dict, return_date: date | None) -> bool:
    """True if this game falls on or after the player's estimated return.
    If return_date is None or in the past, no filter is applied."""
    if return_date is None:
        return True
    game_d = g.get("game_date")
    if not game_d:
        return True
    try:
        return date.fromisoformat(game_d) >= return_date
    except ValueError:
        return True


def _hitter_remaining_units(team_id: int,
                            schedule_by_team: dict[int, list[dict]],
                            return_date: date | None = None) -> float:
    return sum(_hitter_factor(g)
               for g in schedule_by_team.get(team_id, [])
               if _game_after_return(g, return_date))


def _rp_remaining_units(team_id: int,
                        schedule_by_team: dict[int, list[dict]],
                        return_date: date | None = None) -> float:
    return sum(_rp_factor(g)
               for g in schedule_by_team.get(team_id, [])
               if _game_after_return(g, return_date))


def _probable_starts_for(player_name: str, team_id: int,
                         schedule_by_team: dict[int, list[dict]],
                         sp_exit_inning: float,
                         return_date: date | None = None) -> float:
    """Sum of SP factors over games where this pitcher is the probable
    starter and the game is on/after their estimated return date."""
    target = _norm_name(player_name)
    if not target:
        return 0.0
    total = 0.0
    for g in schedule_by_team.get(team_id, []):
        if not _game_after_return(g, return_date):
            continue
        if _norm_name(g.get("probable_pitcher_name")) == target:
            total += _sp_factor(g, sp_exit_inning)
    return total


def _open_sp_game_weight(team_id: int,
                         schedule_by_team: dict[int, list[dict]],
                         sp_exit_inning: float,
                         return_date: date | None = None) -> float:
    """SP-factor weight of this team's games with **no probable announced yet**
    (and on/after the player's return date).

    These are games a rostered SP might start but MLB hasn't posted a probable
    for — the part of the week beyond the ~3-day probable horizon. Used to
    estimate starts there via the pitcher's ROS-share. Games that already have
    a probable are excluded (that start is counted by `_probable_starts_for`,
    so no double-count); Final games contribute 0 through `_sp_factor`. For a
    future week (no probables at all) this is just every remaining game."""
    total = 0.0
    for g in schedule_by_team.get(team_id, []):
        if not _game_after_return(g, return_date):
            continue
        if g.get("probable_pitcher_name"):
            continue
        total += _sp_factor(g, sp_exit_inning)
    return total


def _cadence_extra_start_dist(player_name: str, team_id: int,
                              schedule_by_team: dict[int, list[dict]],
                              last_start_by_pitcher: dict[str, str],
                              return_date: date | None = None,
                              ) -> list[float] | None:
    """Distribution over the number of *extra* (un-probabled, open) starts this
    pitcher gets in the period, from a rotation-turn projection.

    Returns `[P(0), P(1), ...]`, or `None` when there's no usable anchor (no
    recorded last start and no announced probable) — the caller then falls back
    to the flat ROS-share estimate, so behavior never regresses below today's.

    Model: anchor on the later of the pitcher's last recorded start and his
    latest *announced* probable in this period (an arm slated for Tuesday
    projects his next turn from Tuesday). From the anchor, project forward turns
    by sampling rest-days (`REST_DAY_WEIGHTS`), snapping each projected date to
    this team's next **open** game (no probable announced yet) on/after it.
    Announced-probable starts are the 'fixed' piece counted by
    `_probable_starts_for`, so they're never credited here (no double-count) —
    they only set the rotation phase. Aggregated over all rest-day scenarios
    into a discrete distribution, capped at `MAX_EXTRA_STARTS`.
    """
    target = _norm_name(player_name)
    if not target:
        return None
    sched = [g for g in schedule_by_team.get(team_id, [])
             if _game_after_return(g, return_date)]

    # Anchor: last recorded start, raised to the latest announced probable for
    # this pitcher in-period (announced games chain the phase into the open tail).
    anchor: date | None = None
    last = last_start_by_pitcher.get(target)
    if last:
        try:
            anchor = date.fromisoformat(last)
        except ValueError:
            anchor = None
    for g in sched:
        if _norm_name(g.get("probable_pitcher_name")) == target:
            try:
                gd = date.fromisoformat(g["game_date"])
            except (ValueError, KeyError):
                continue
            if anchor is None or gd > anchor:
                anchor = gd
    if anchor is None:
        return None  # no usable anchor → caller uses flat-rate fallback

    # Candidate open games: no probable yet, not finished, not in progress
    # (an in-progress game already has a starter). Future games only.
    open_dates: list[date] = []
    for g in sched:
        if g.get("probable_pitcher_name"):
            continue
        if g.get("game_status") in _GAME_FINISHED:
            continue
        if g.get("current_inning") is not None:
            continue
        try:
            open_dates.append(date.fromisoformat(g["game_date"]))
        except (ValueError, KeyError):
            continue
    open_dates.sort()

    dist: dict[int, float] = {}

    def walk(cur: date, count: int, prob: float) -> None:
        if count >= MAX_EXTRA_STARTS:
            dist[count] = dist.get(count, 0.0) + prob
            return
        for r_days, w in REST_DAY_WEIGHTS.items():
            target_date = cur + timedelta(days=r_days)
            nxt = next((d for d in open_dates if d >= target_date), None)
            if nxt is None:
                dist[count] = dist.get(count, 0.0) + prob * w
            else:
                walk(nxt, count + 1, prob * w)

    walk(anchor, 0, 1.0)
    if not dist:
        return [1.0]
    k_max = max(dist)
    return [dist.get(i, 0.0) for i in range(k_max + 1)]


def _split_mean_to_dist(mean: float) -> list[float]:
    """Split a fractional expected-start count into an integer distribution with
    the same mean: P(ceil)=frac, P(floor)=1-frac. e.g. 1.6 → [0, 0.4, 0.6].

    The non-cadence SP fallback (far-future weeks, or no anchor) uses this so the
    start *count* still varies per sim — coupling the pitching categories — even
    though we drop the rotation-turn placement. Mean is preserved, so the
    matchup's expected production is unchanged vs. the old deterministic fold."""
    if mean <= 0:
        return [1.0]
    lo = int(math.floor(mean))
    frac = mean - lo
    dist = [0.0] * (lo + 2)
    dist[lo] += 1.0 - frac
    dist[lo + 1] += frac
    while len(dist) > 1 and dist[-1] == 0.0:
        dist.pop()
    return dist


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


def _max_slot_assignment(candidates: list[dict], slot_instances: list[int]) -> set[int]:
    """Maximum bipartite matching of candidates to lineup-slot instances.

    Returns the set of indices into `candidates` that won a slot. Augmenting-path
    (Kuhn's): unlike greedy first-fit, it re-routes an already-seated *flexible*
    hitter to an alternate slot so a *constrained* hitter can also play — so a slot
    only one hitter can fill is never wasted (e.g. the lone 3B-eligible bat is put
    at 3B, freeing 2B for a 2B-only hitter, seating both). `candidates` must be
    pre-sorted by descending impact; since augmenting never un-seats anyone, that
    means a capacity-bound day seats the highest-impact subset. `candidates[i]
    ["eligible"]` is the set of slot ids that hitter can fill."""
    match_inst = [-1] * len(slot_instances)  # slot-instance idx → candidate idx

    def augment(ci: int, visited: list[bool]) -> bool:
        for si, slot in enumerate(slot_instances):
            if slot in candidates[ci]["eligible"] and not visited[si]:
                visited[si] = True
                if match_inst[si] == -1 or augment(match_inst[si], visited):
                    match_inst[si] = ci
                    return True
        return False

    seated: set[int] = set()
    for ci in range(len(candidates)):
        if augment(ci, [False] * len(slot_instances)):
            seated.add(ci)
    return seated


def _hitter_days_slotted(roster: list[dict],
                         schedule_by_team: dict[int, list[dict]],
                         lineup_slot_counts: dict[int, int],
                         as_of: date | None = None) -> dict[int, float]:
    """For each hitter, sum of in-progress factors across days they win a
    lineup slot. Honors slot eligibility and league-configured slot counts, and
    seats hitters by per-game impact.

    Two-way players (e.g. Ohtani) are skipped as hitters on days they're
    listed as the probable starter for their team — they can't bat that day.

    Slotting uses optimal bipartite matching per day (`_max_slot_assignment`),
    not greedy first-fit: greedy can spend a flexible bat on an early slot and
    then waste a scarce slot only that bat could fill (e.g. the lone 3B-eligible
    hitter taken at 2B → 3B empty AND a 2B-only hitter benched). The problem is
    tiny (~10 hitters × ~10 slots) and runs once per team in build_budgets — well
    outside the per-sim loop — so the cost is immaterial.
    """
    as_of = as_of or _utc_today()
    units: dict[int, float] = {
        p["player_id"]: 0.0 for p in roster
        if _is_playable(p, as_of) and _is_hitter_candidate(p)
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
    # One node per slot capacity unit (UTIL×2 → two UTIL instances, etc.).
    slot_instances = [s for s, cnt in hitter_slot_counts.items() for _ in range(cnt)]

    hitters = [
        p for p in roster
        if _is_playable(p, as_of) and _is_hitter_candidate(p)
    ]

    # Per-player estimated return date — IL players become available again
    # mid-period and only get slotted from that day onward.
    return_by_pid = {p["player_id"]: _est_return_date(p, as_of) for p in hitters}

    # All dates that appear in the team schedule (sorted for deterministic order).
    dates = sorted({
        g.get("game_date")
        for games in schedule_by_team.values()
        for g in games
        if g.get("game_date")
    })

    for date_str in dates:
        try:
            day = date.fromisoformat(date_str)
        except ValueError:
            day = None
        candidates = []
        for p in hitters:
            ret = return_by_pid.get(p["player_id"])
            if ret is None:
                continue  # indefinitely out (OUT/INJURY_RESERVE/unknown)
            # IL'd players: skip dates before their estimated return. Healthy
            # players (ret <= as_of) get NO date floor — a past day falls out via
            # `_hitter_factor` (Final → 0) below, not a wall-clock comparison. The
            # old `day < ret` floor for healthy players (ret == local-today) is what
            # dropped a whole day's projection at the host's local midnight; keying
            # off game status instead makes this timezone-independent and smooth.
            if ret > as_of and day is not None and day < ret:
                continue
            team_games_today = [
                g for g in schedule_by_team.get(p["pro_team_id"], [])
                if g.get("game_date") == date_str
            ]
            if not team_games_today:
                continue
            # Two-way players starting on the mound today can't bat.
            if _is_probable_starter_on(p, date_str, schedule_by_team):
                continue
            factor = max(_hitter_factor(g) for g in team_games_today)
            if factor <= 0:
                continue
            eligible = {s for s in (p.get("eligible_slots") or [])
                        if s in hitter_slot_counts}
            if not eligible:
                continue
            candidates.append({
                "player_id": p["player_id"],
                "factor": factor,
                "eligible": eligible,
                "impact": _hitter_per_game_impact(p),
            })

        # Optimal assignment: seat the most hitters (highest-impact first), with
        # re-routing so no scarce slot is wasted. See `_max_slot_assignment`.
        candidates.sort(key=lambda c: -c["impact"])
        for ci in _max_slot_assignment(candidates, slot_instances):
            c = candidates[ci]
            units[c["player_id"]] += c["factor"]

    return units


def _team_margin(g: dict) -> int:
    return (g.get("team_runs") or 0) - (g.get("opponent_runs") or 0)


def _is_save_situation(margin: int) -> bool:
    """Simplified: the team leads by 1–3. Ignores tying-run-on-deck and the
    ≥3-IP save rule — the documented MVP heuristic."""
    return 1 <= margin <= 3


def _override_sp_qs(budget: Budget, full_name: str, ros: dict,
                    schedule_by_team: dict[int, list[dict]],
                    live_by_team: dict[int, dict[str, dict]],
                    team_id: int, gs_ros: float, sp_exit_inning: float) -> None:
    """Swap the in-progress start's QS share for the conditional in-game
    projection (`app.ingame`). Other games and other counters are untouched, so
    with no live game this is a no-op."""
    if gs_ros <= 0:
        return
    live = (live_by_team.get(team_id) or {}).get(_norm_name(full_name))
    if not live or not live.get("games_started"):
        return
    ip_games = {g["game_pk"]: g for g in schedule_by_team.get(team_id, [])
                if g.get("game_status") == "In Progress"}
    g = ip_games.get(live["game_pk"])
    if g is None:
        return
    qs_rate = (ros.get(STAT_QS) or 0) / gs_ros
    outs_tot = ros.get(STAT_OUTS) or 0
    exp_outs = outs_tot / gs_ros if gs_ros else 18.0
    er_per_out = (ros.get(STAT_ER) or 0) / outs_tot if outs_tot else 0.13
    state = ingame.StarterState(
        game_status="In Progress", appeared=True, exited=not live["is_last"],
        outs=live["outs"], er=live["er"], exp_outs_per_start=exp_outs,
        er_per_out=er_per_out, pregame_qs_rate=qs_rate,
    )
    ip_share = qs_rate * _sp_factor(g, sp_exit_inning)   # rate-based share to drop
    cur = budget.expected.get(STAT_QS, 0.0)
    budget.expected[STAT_QS] = max(0.0, cur - ip_share) + ingame.project_qs(state)


def _override_rp_svhd(budget: Budget, full_name: str, ros: dict,
                      schedule_by_team: dict[int, list[dict]],
                      live_by_team: dict[int, dict[str, dict]],
                      team_id: int, gp_ros: float, units_p: float,
                      rp_remaining: float) -> None:
    """Swap each in-progress game's SVHD share for the in-game projection: the
    live line if the reliever has appeared, else a game-script-gated rate for a
    closer who hasn't entered yet. No-op when nothing is live."""
    if gp_ros <= 0 or rp_remaining <= 0:
        return
    ip_games = {g["game_pk"]: g for g in schedule_by_team.get(team_id, [])
                if g.get("game_status") == "In Progress"}
    if not ip_games:
        return
    svhd_rate = min((ros.get(STAT_SVHD) or 0) / gp_ros, MAX_SVHD_RATE)
    appearance_per_factor = units_p / rp_remaining   # expected apps per _rp_factor
    live = (live_by_team.get(team_id) or {}).get(_norm_name(full_name))
    for game_pk, g in ip_games.items():
        factor = _rp_factor(g)
        if factor <= 0:
            continue
        base_share = svhd_rate * appearance_per_factor * factor   # rate-based share
        cur = budget.expected.get(STAT_SVHD, 0.0)
        margin = _team_margin(g)
        if live and live["game_pk"] == game_pk:
            state = ingame.RelieverState(
                game_status="In Progress", appeared=True,
                exited=not live["is_last"],
                entered_save_situation=_is_save_situation(margin),
                lead_intact=margin > 0,
                recorded_out=live["outs"] >= 1,
                svhd_rate=svhd_rate,
            )
            budget.expected[STAT_SVHD] = max(0.0, cur - base_share) + ingame.project_svhd(state)
        else:
            gate = ingame.game_script_gate(margin, g.get("current_inning") or 0)
            budget.expected[STAT_SVHD] = max(0.0, cur - base_share) + base_share * gate


def _per_start_rates(ros: dict, denom: float) -> dict[int, float]:
    """Per-start stat rates (`ros_v / denom`) for the pitcher counters — the
    multipliers used to scale a SP's sampled extra starts. Mirrors the `rate`
    computation in `_make_budget`, including the SVHD ceiling."""
    rates: dict[int, float] = {}
    if denom <= 0:
        return rates
    for stat_id in PITCHER_COUNTERS:
        ros_v = ros.get(stat_id)
        if ros_v is None or ros_v <= 0:
            continue
        rate = ros_v / denom
        if stat_id == STAT_SVHD and rate > MAX_SVHD_RATE:
            rate = MAX_SVHD_RATE
        rates[stat_id] = rate
    return rates


def build_budgets(roster: list[dict],
                  schedule_by_team: dict[int, list[dict]],
                  team_total_ros_games: dict[int, int] | None = None,
                  lineup_slot_counts: dict[int, int] | None = None,
                  live_by_team: dict[int, dict[str, dict]] | None = None,
                  last_start_by_pitcher: dict[str, str] | None = None,
                  use_cadence: bool = True,
                  as_of: date | None = None,
                  ) -> list[Budget]:
    """Convert a roster + schedule into per-player production budgets.

    Inclusion rules:
      - IL slot or definitely-out injury status → skipped.
      - All other rostered pitchers (BE included) → considered. SP starts use
        the hybrid estimate (announced probables + a ROS-share estimate over
        games with no probable yet — see the SP branch); RP appearances come
        from the ROS-rate estimator.
      - Hitters → run through the per-day lineup optimizer; their units
        are the sum of days they win a slot.

    GOTCHA: pass `lineup_slot_counts` (the league's slot capacities, from
    scoring_settings.lineup_slots_json — see cli.compute) or the optimizer has
    no slots to fill and **every hitter silently comes back with 0 days / no
    budget**. Easy to miss in ad-hoc analysis scripts; pitchers are unaffected.
    """
    team_total_ros_games = team_total_ros_games or {}
    lineup_slot_counts = lineup_slot_counts or {}
    live_by_team = live_by_team or {}
    last_start_by_pitcher = last_start_by_pitcher or {}
    as_of = as_of or _utc_today()
    hitter_units = _hitter_days_slotted(roster, schedule_by_team, lineup_slot_counts, as_of)
    today = as_of

    out: list[Budget] = []
    for p in roster:
        if not _is_playable(p, as_of):
            continue
        ros = p["ros_stats"]
        pos = p["default_position_id"]
        team_id = p["pro_team_id"]
        # Estimated return for IL'd players — filters games before they
        # can play. None when player is healthy now (no filter).
        ret = _est_return_date(p, today)
        if ret is not None and ret <= today:
            ret = None  # healthy → no game filter needed

        # Estimated (un-probabled) SP starts for this player. Subtracted from a
        # two-way player's hitter days below, since the lineup optimizer only
        # blocks *announced-probable* start days, not estimated ones. 0 for
        # non-SPs and non-pitchers.
        sp_est_units = 0.0
        sp_extra_dist: list[float] | None = None     # cadence: P(k extra starts)
        sp_extra_per_start: dict[int, float] | None = None
        rp_remaining = 0.0   # set in the RP branch; used by the SVHD override

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
                # SP starts split into a FIXED piece (announced probables + any
                # live start; near-certain count) and a STOCHASTIC EXTRA piece
                # (the un-probabled tail of the week). The cadence model projects
                # the extra piece as a distribution over integer starts sampled
                # per-sim; with no usable anchor we fall back to the old flat
                # ROS-share smear so behavior never regresses. Either way the
                # fixed and extra pieces never overlap (probable games are
                # excluded from the extra piece — no double-count).
                if gs_ros > 0:
                    avg_outs_per_start = (ros.get(STAT_OUTS, 0) or 0) / gs_ros
                    sp_exit_inning = max(1.0, avg_outs_per_start / 3.0 + 1.0)
                else:
                    sp_exit_inning = 6.0
                probable_units = _probable_starts_for(
                    p["full_name"], team_id, schedule_by_team, sp_exit_inning, ret,
                )
                # The extra (un-probabled) starts are always the sampled piece;
                # only *how the distribution is built* depends on the horizon:
                #   - current week (use_cadence) → rotation-turn dist
                #     (`_cadence_extra_start_dist`), turn-aware.
                #   - any future week → the flat ROS-share mean split into an
                #     integer dist (`_split_mean_to_dist`). Cadence is current-week
                #     only because a future week's anchor is ~a week stale (the
                #     pitcher starts again this week first, unrecorded), which makes
                #     the walk snap the first turn to day 1 and over-project. The
                #     start *count* still varies per sim either way.
                extra_dist = _cadence_extra_start_dist(
                    p["full_name"], team_id, schedule_by_team,
                    last_start_by_pitcher, ret,
                ) if use_cadence else None
                if extra_dist is None:
                    open_weight = _open_sp_game_weight(
                        team_id, schedule_by_team, sp_exit_inning, ret,
                    )
                    total_ros = team_total_ros_games.get(team_id, 0)
                    if total_ros > 0 and gs_ros > 0 and open_weight > 0:
                        rate = min(gs_ros / total_ros, MAX_SP_RATE)
                        flat_mean = rate * open_weight
                        if flat_mean > 0:
                            extra_dist = _split_mean_to_dist(flat_mean)
                if extra_dist is not None:
                    # Probables are the fixed units; the open tail is sampled.
                    # sp_est_units = E[extra starts], used for the two-way
                    # subtraction and the displayed start count.
                    sp_extra_dist = extra_dist
                    sp_extra_per_start = _per_start_rates(ros, gs_ros)
                    sp_est_units = sum(i * pk for i, pk in enumerate(extra_dist))
                units_p = probable_units
                denom_p = gs_ros
                role_p = "SP"
            else:
                rp_remaining = _rp_remaining_units(team_id, schedule_by_team, ret)
                total_ros = team_total_ros_games.get(team_id, 0)
                if total_ros > 0 and gp_ros > 0:
                    units_p = (gp_ros / total_ros) * rp_remaining
                else:
                    units_p = rp_remaining * RP_APPEARANCE_RATE
                denom_p = gp_ros
                role_p = "RP"

            budget = _make_budget(p, ros, units_p, denom_p, PITCHER_COUNTERS, role_p)
            if role_p == "SP" and sp_extra_dist is not None:
                # Future week (no announced probables): the fixed piece is empty
                # so _make_budget returns None, but the cadence model still
                # projects extra starts — build a budget with an empty fixed
                # expected and the stochastic extra piece only.
                if budget is None and sp_extra_per_start and sp_est_units > 0:
                    budget = Budget(player_id=p["player_id"], name=p["full_name"],
                                    role="SP", units=0.0, expected={})
                if budget is not None:
                    budget.extra_dist = sp_extra_dist
                    budget.extra_per_start = sp_extra_per_start
                    budget.units += sp_est_units   # show total expected starts
            if budget:
                # In-game override for the threshold/context stats — no-op
                # unless this pitcher's team has a game in progress right now.
                # Operates on the fixed `expected` only (a live start is a
                # probable, hence fixed); the extra piece is never live.
                if role_p == "SP":
                    _override_sp_qs(budget, p["full_name"], ros, schedule_by_team,
                                    live_by_team, team_id, gs_ros, sp_exit_inning)
                else:
                    _override_rp_svhd(budget, p["full_name"], ros, schedule_by_team,
                                      live_by_team, team_id, gp_ros, units_p, rp_remaining)
                out.append(budget)

        # ── Hitter budget ──────────────────────────────────────────────
        if _has_hitter_ros(ros):
            units_h = hitter_units.get(p["player_id"], 0.0)
            # Two-way players (Ohtani): the lineup optimizer blocks them as
            # hitters only on *announced-probable* start days, so subtract the
            # *estimated* (un-probabled) start days here to avoid counting them
            # both batting and pitching. For a future week that's all of their
            # SP starts; for the current week it's just the un-announced ones.
            if _has_pitcher_ros(ros):
                units_h = max(0.0, units_h - sp_est_units)
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
        rate = ros_v / denom
        # Bound the per-appearance SVHD rate at a realistic ceiling — see
        # MAX_SVHD_RATE for context on ESPN's projection quirks.
        if stat_id == STAT_SVHD and rate > MAX_SVHD_RATE:
            rate = MAX_SVHD_RATE
        expected[stat_id] = rate * units
    if not expected:
        return None
    return Budget(
        player_id=p["player_id"],
        name=p["full_name"],
        role=role,
        units=units,
        expected=expected,
    )


def _display_expected(b: Budget) -> dict[int, float]:
    """Per-stat expected values for reporting: the fixed `expected` plus the
    mean of the stochastic extra-start piece (E[k] × per-start rate). For
    non-SP budgets (no extra piece) this is just `expected`. Used by the budget
    summary so the shown exp_* reflect *total* expected starts; the sim itself
    samples the two pieces separately."""
    if not b.extra_dist or not b.extra_per_start:
        return b.expected
    e_extra = sum(i * pk for i, pk in enumerate(b.extra_dist))
    if e_extra <= 0:
        return b.expected
    merged = dict(b.expected)
    for stat_id, rate in b.extra_per_start.items():
        merged[stat_id] = merged.get(stat_id, 0.0) + rate * e_extra
    return merged


# ── Score a single simulated matchup ──

def _decide(home_counters: dict,
            away_counters: dict) -> tuple[str, dict[int, str]]:
    """Return (matchup_winner, per_cat) where per_cat maps stat_id to
    'HOME' | 'AWAY' | 'TIE'."""
    per_cat: dict[int, str] = {}
    home_cats = 0
    away_cats = 0
    for stat_id, reversed_ in CATEGORIES:
        h = cat_value(home_counters, stat_id)
        a = cat_value(away_counters, stat_id)
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
    h_tb = cat_value(home_counters, TIEBREAKER_STAT_ID)
    a_tb = cat_value(away_counters, TIEBREAKER_STAT_ID)
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
            if stat_id == STAT_ER:
                # ER is the one counter with measured VMR > 1 (blowup innings).
                # Use a Negative Binomial draw to reflect that overdispersion.
                vmr = ER_VMR_BY_ROLE.get(b.role, 1.0)
                draw = _neg_binom(exp, vmr)
            else:
                draw = _poisson(exp)
            counters[stat_id] = counters.get(stat_id, 0) + draw
        # SP cadence model: draw this sim's integer count of un-probabled extra
        # starts, then scale every pitching counter by that shared k so the
        # categories move together (a two-start week is all-or-nothing, not a
        # smeared mean). The fixed piece above is already in `expected`.
        if b.extra_dist:
            k = _sample_dist(b.extra_dist)
            if k:
                for stat_id, rate in (b.extra_per_start or {}).items():
                    mean = rate * k
                    if stat_id == STAT_ER:
                        draw = _neg_binom(mean, ER_VMR_BY_ROLE.get(b.role, 1.0))
                    else:
                        draw = _poisson(mean)
                    counters[stat_id] = counters.get(stat_id, 0) + draw
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
             team_total_ros_games: dict[int, int] | None = None,
             lineup_slot_counts: dict[int, int] | None = None,
             live_by_team: dict[int, dict[str, dict]] | None = None,
             last_start_by_pitcher: dict[str, str] | None = None,
             use_cadence: bool = True,
             as_of: date | None = None,
             ) -> tuple[float, float, dict]:
    as_of = as_of or _utc_today()   # UTC, never host-local — resolved once per run
    home_budgets = build_budgets(
        inputs.home_roster, schedule_by_team,
        team_total_ros_games=team_total_ros_games,
        lineup_slot_counts=lineup_slot_counts,
        live_by_team=live_by_team,
        last_start_by_pitcher=last_start_by_pitcher,
        use_cadence=use_cadence,
        as_of=as_of,
    )
    away_budgets = build_budgets(
        inputs.away_roster, schedule_by_team,
        team_total_ros_games=team_total_ros_games,
        lineup_slot_counts=lineup_slot_counts,
        live_by_team=live_by_team,
        last_start_by_pitcher=last_start_by_pitcher,
        use_cadence=use_cadence,
        as_of=as_of,
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
            # exp folds the stochastic extra-start piece into the fixed means
            # so the displayed exp_* reflect total expected starts (no-op for
            # non-SP budgets).
            exp = _display_expected(b)
            rec = {
                "player_id": b.player_id,
                "name": b.name,
                "role": b.role,
                "units": round(b.units, 2),
            }
            if b.role == "HIT":
                exp_ab = exp.get(STAT_AB, 0)
                rec.update({
                    "exp_h":   round(exp.get(STAT_H, 0), 1),
                    "exp_hr":  round(exp.get(STAT_HR, 0), 2),
                    "exp_r":   round(exp.get(STAT_R, 0), 1),
                    "exp_sb":  round(exp.get(STAT_SB, 0), 2),
                    # Per-batter OPS only meaningful with a real AB budget;
                    # null otherwise so the UI can hide it.
                    "exp_ops": round(derive_ops(exp), 3) if exp_ab >= 1 else None,
                })
            else:  # SP or RP
                exp_outs = exp.get(STAT_OUTS, 0)
                rec.update({
                    "exp_k":    round(exp.get(STAT_K, 0), 1),
                    "exp_outs": round(exp_outs, 1),
                    "exp_qs":   round(exp.get(STAT_QS, 0), 2),
                    "exp_svhd": round(exp.get(STAT_SVHD, 0), 2),
                })
                # ERA/WHIP need at least 0.5 IP of expected production to be
                # informative — otherwise it's noise from a tiny projection.
                if exp_outs >= 1.5:
                    rec["exp_era"]  = round(derive_era(exp), 2)
                    rec["exp_whip"] = round(derive_whip(exp), 2)
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
            "home_avg": cat_value(avg_h, stat_id),
            "away_avg": cat_value(avg_a, stat_id),
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

    # ESPN-sourced real return dates (norm_name → date), used by
    # _est_return_date to override the fixed-days IL heuristic. Empty until
    # refresh-rosters has populated player_injuries.
    injuries: dict[str, date] = {}
    try:
        for ir in conn.execute("SELECT norm_name, return_date FROM player_injuries"):
            try:
                injuries[ir["norm_name"]] = date.fromisoformat(ir["return_date"])
            except (ValueError, TypeError):
                continue
    except sqlite3.OperationalError:
        pass  # table not created yet (pre-migration DB)

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
            "injury_return_override": injuries.get(_norm_name(r["full_name"])),
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
        SELECT pro_team_id, game_pk, game_date, opponent_pro_team_id, is_home,
               probable_pitcher_mlbam_id, probable_pitcher_name, game_status,
               current_inning, inning_state, team_runs, opponent_runs
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


def load_live_pitchers(conn: sqlite3.Connection) -> dict[int, dict[str, dict]]:
    """Live pitcher lines for in-progress games, keyed by ESPN proTeamId then
    normalized pitcher name (matching how budgets look players up). Empty when
    no games are live — in which case build_budgets behaves exactly as before."""
    rows = conn.execute(
        """
        SELECT game_pk, name, pro_team_id, is_last, games_started, outs, er, k
        FROM live_pitchers
        """
    ).fetchall()
    out: dict[int, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["pro_team_id"], {})[_norm_name(r["name"])] = dict(r)
    return out


def load_last_starts(conn: sqlite3.Connection) -> dict[str, str]:
    """Most recent start date per pitcher (normalized name → 'YYYY-MM-DD') from
    `pitcher_starts`. Anchor for the rotation-cadence SP model. Empty when no
    history has been recorded — build_budgets then falls back to the flat
    ROS-share estimate, so behavior never regresses below today's."""
    rows = conn.execute(
        "SELECT pitcher_name, MAX(game_date) AS last_date "
        "FROM pitcher_starts GROUP BY pitcher_name"
    ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        nm = _norm_name(r["pitcher_name"])
        if not nm or not r["last_date"]:
            continue
        # ISO dates sort lexicographically; keep the latest on name collision.
        if nm not in out or r["last_date"] > out[nm]:
            out[nm] = r["last_date"]
    return out


def load_latest_state(conn: sqlite3.Connection, matchup_id: int,
                      team_id: int) -> dict[int, float]:
    """Latest banked score per stat for the sim's starting counters. Per-stat
    (not a single MAX(fetched_at)) — see `db.latest_category_state`."""
    return {sid: v["score"] for sid, v in
            db.latest_category_state(conn, matchup_id, team_id).items()}
