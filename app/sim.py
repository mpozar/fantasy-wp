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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from app import db, ingame, mlb
from app.names import norm_name as _norm_name

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

# Threshold/context stats bounded at ≤1 per event (QS per start, SVHD per
# appearance). Sampled as Binomial, not Poisson, so a single start/appearance
# can never contribute more than one — see `_binomial_from_mean`.
PER_EVENT_CAPPED = frozenset({STAT_QS, STAT_SVHD})

# ── Live component reconstruction (beat the once-daily ESPN REST settle) ──
# ESPN's REST endpoint settles the raw rate *components* (the stats below) only
# ~once a day (~07:00 UTC), so the sim's projected ERA/WHIP/OPS run on banked
# innings/AB that can be ~24h stale even while the scrape keeps the displayed
# rates live. We rebuild these components from the live MLB box-scores we
# already fetch, attributed by the day's fantasy lineup, and trust them only
# when the rate they imply matches ESPN's live *scraped* rate (else we keep the
# stale-but-safe ESPN value). See CLAUDE.md "Live component reconstruction".
SETTLE_LAG_HOURS = 7                    # ESPN absorbs a stat-day ~07:00 UTC next
NON_COUNTING_SLOTS = {16, 17}          # BE (bench), IL — don't score that day
PITCHER_SLOTS = {13, 14, 15}           # SP / RP / P — pitching lines count here
# Pitching-rate components (ERA, WHIP) — all REST-only, so all reconstructed.
PITCH_RATE_COMPONENTS = (STAT_OUTS, STAT_ER, STAT_P_H, STAT_P_BB)
# OPS components we reconstruct: the REST-only ones. H (1) and HR (5) are
# *scored* cats the scrape already owns live, so they stay at the baseline
# value — adding a delta would double-count the scrape.
OPS_RECON_COMPONENTS = (STAT_AB, STAT_2B, STAT_3B, STAT_B_BB, STAT_HBP, STAT_SF)
# Max gap between our reconstructed rate and ESPN's live scraped rate for the
# reconstruction to be trusted (rounding + a fraction of an in-flight out/AB).
LIVE_RATE_TOL = {STAT_ERA: 0.20, STAT_WHIP: 0.04, STAT_OPS: 0.012}

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
    if p.get("lineup_slot_id") == IL_SLOT and inj in PLAYABLE_INJURY_STATUSES:
        # Just activated off the IL but still occupying the IL slot for *today*:
        # when games have already started, the league defers a mid-day activation
        # to the next game day, so ESPN leaves him IL-slotted today and active
        # from tomorrow. Treat him as returning tomorrow — out today, available
        # for the rest of the matchup — rather than out for the whole period.
        return today + timedelta(days=1)
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
      - IL slot: include when the status maps to an IL return estimate (real
        stint), OR when it's a playable status (ACTIVE/etc.) — that's a player
        just activated off the IL who's still IL-slotted for *today only*
        (a mid-day activation the league defers to the next game day); he's
        available for the rest of the matchup, so `_est_return_date` return-dates
        him to tomorrow. Only a genuinely out status (OUT/INJURY_RESERVE) in the
        IL slot is excluded outright.
    """
    inj = (p.get("injury_status") or "").upper()
    if p.get("lineup_slot_id") == IL_SLOT:
        return inj in IL_RETURN_DAYS or inj in PLAYABLE_INJURY_STATUSES
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

# Minimum days between a pitcher's starts — the shortest real rotation turn.
# Used as a physical backstop on total projected starts (fixed + cadence): a
# pitcher can't start more often than this, so we never project, say, both a
# Saturday and a Sunday start.
MIN_REST_DAYS = min(REST_DAY_WEIGHTS)

# A pitcher the season GS/GP ratio classifies as RP, but who is the announced
# probable / currently making a start, is misclassified (a rotation regular or
# spot starter whose ESPN ROS projection lags). When we promote him to the SP
# path his projected GS is tiny, so `ros_outs/gs_ros` is inflated by relief outs:
# cap the per-start length and rebuild cumulative per-start rates from per-out
# rates × this length. QS stays the per-start rate (ros_qs/gs_ros) since it's
# already a per-start event.
TYPICAL_START_OUTS = 17   # ~5.2 IP — fallback start length when GS is unusable (0)
MAX_START_OUTS = 22       # ~7.1 IP — cap on the inferred start length
DEFAULT_QS_RATE = 0.30    # per-start QS prior when there's no usable GS history

# MLB statsapi detailedState values that mean a game is over. Canonical set —
# cli.py and validate.py alias this rather than re-declaring the literal.
FINAL_GAME_STATES = {"Final", "Game Over", "Completed Early"}

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
# `_norm_name` is imported from app.names (shared with espn_public) so the
# write-key and read-key for name matching can never diverge.


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


def _binomial(n: int, p: float) -> int:
    """Number of successes in `n` independent Bernoulli(p) trials."""
    p = max(0.0, min(1.0, p))
    if n <= 0 or p <= 0.0:
        return 0
    return sum(1 for _ in range(n) if random.random() < p)


def _binomial_from_mean(mean: float) -> int:
    """Draw a *per-event-capped* counting stat (QS, SVHD) from its expected
    value so it can never exceed the number of events it came from.

    QS is ≤1 per start and SVHD ≤1 per appearance, so a Poisson draw — unbounded
    and over-dispersed — can return physically impossible totals (e.g. 2 quality
    starts from a single in-progress start, which spuriously let a team "win" a
    locked QS category). Model it instead as Binomial(n, p) with n = ⌈mean⌉
    trials and p = mean/n: the mean is preserved exactly, the draw is capped at
    ⌈mean⌉ ≤ the true start/appearance count (so never impossible), and the
    variance is below Poisson — matching the empirically *under*-dispersed
    behavior of QS/SVHD (see "Variance" in CLAUDE.md)."""
    if mean <= 0:
        return 0
    n = math.ceil(mean)
    return _binomial(n, mean / n)


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


def _sum_counted(lines: list[dict], slot_by_norm_name: dict[str, int],
                 counting_slots: set[int], fields: tuple[str, ...]) -> tuple[dict, int]:
    """Sum box-score `fields` over the lines whose matched fantasy player was in
    a counting lineup slot that day. A line is matched to a rostered player by
    normalized name (no ESPN↔MLBAM id crosswalk); unrostered players and ones on
    the bench/IL contribute nothing. Returns ({field: total}, n_matched)."""
    totals = {f: 0.0 for f in fields}
    matched = 0
    for ln in lines:
        slot = slot_by_norm_name.get(_norm_name(ln.get("name")))
        if slot is None or slot not in counting_slots:
            continue
        matched += 1
        for f in fields:
            totals[f] += ln.get(f) or 0
    return totals, matched


def _count_qs(lines: list[dict], slot_by_norm_name: dict[str, int]) -> tuple[int, int]:
    """Quality starts among **Final** starter lines whose pitcher was slotted in a
    pitching slot that day. QS is computed from the raw line — a started game with
    ≥ QS_OUTS outs and ≤ QS_MAX_ER ER — same definition as the in-progress model.

    Final-only on purpose: while a game is In Progress, `ingame.py` already supplies
    the QS probability in the budget, so crediting it here too would double-count.
    Once Final, that override switches off and the credit otherwise waits for ESPN's
    once-daily settle — the gap this closes. Returns (qs_count, n_started_matched)."""
    qs = matched = 0
    for ln in lines:
        slot = slot_by_norm_name.get(_norm_name(ln.get("name")))
        if slot is None or slot not in PITCHER_SLOTS:
            continue
        if not ln.get("games_started"):
            continue
        if (ln.get("game_status") or "") != "Final":
            continue
        matched += 1
        if (ln.get("outs") or 0) >= ingame.QS_OUTS and (ln.get("er") or 0) <= ingame.QS_MAX_ER:
            qs += 1
    return qs, matched


def _count_svhd(lines: list[dict], slot_by_norm_name: dict[str, int]) -> tuple[int, int]:
    """SVHD (saves + holds) from **Final** reliever lines whose pitcher was slotted
    in a pitching slot. This league scores SVHD = SV + HLD; blown saves are *not*
    scored (ESPN's stat 83 is the standard SV+HLD category — an earlier note that it
    "subtracts blown saves" was a mis-read of the broken ROS projection split, not
    the actuals). Final-only (the in-progress SVHD model in `ingame.py` owns live
    games — crediting here too would double-count) and additive to the banked total
    (the unsettled window prevents double-counting settled games), same safeties as
    `_count_qs`. Returns (svhd, n_decisions_matched)."""
    total = matched = 0
    for ln in lines:
        slot = slot_by_norm_name.get(_norm_name(ln.get("name")))
        if slot is None or slot not in PITCHER_SLOTS:
            continue
        if (ln.get("game_status") or "") != "Final":
            continue
        sv, hld = (ln.get("sv") or 0), (ln.get("hld") or 0)
        if sv or hld:
            matched += 1
            total += sv + hld
    return total, matched


def _judge_group(name: str, state: dict[int, float], recon: dict[int, float],
                 components: tuple[int, ...], derivers: dict[int, callable],
                 scraped: dict[int, float], n_matched: int) -> dict:
    """Decide whether to swap `recon`'s `components` into `state` (a per-team working
    copy that starts on the ESPN **REST baseline**). The governing rule: always move
    the current rate **toward the live scraped rate**, never to a number further from
    it. The live scrape's displayed ERA/WHIP/OPS is the authoritative *current* value;
    the box-score reconstruction's only job is to supply the components (numerator +
    denominator) the sim needs to *project* it. Three verdicts:

      - **`matched`** — every reconstructed rate is within `LIVE_RATE_TOL` of the
        scrape → the reconstruction is provably consistent → commit it. Confident case.
      - **`closer`** — a rate is *out* of tolerance, but the reconstruction is still
        nearer the scrape than the stale REST baseline → commit it anyway. The REST
        baseline lags ~a day; an imperfect reconstruction (a missing/partial box line,
        e.g. an unmatched reliever) is the better estimate of the current rate, so we
        don't fall back to a *worse* number. This is the fix for the settle-bound
        ERA/WHIP & OPS swings — see INCIDENTS.md 2026-06-09/06-11.
      - **`baseline`** — no matched lines, no scraped rate to judge against, or the
        baseline is already at least as close → leave `state` on the baseline.

    Returns a decision record (`verdict`, plus scraped/reconstructed/baseline rates
    for telemetry). Pure aside from mutating `state`."""
    if n_matched <= 0:
        return {"group": name, "accepted": False, "verdict": "no_lines",
                "matched_lines": n_matched, "rates": {}}
    rates: dict[int, dict] = {}
    within_tol = True
    have_scrape = False
    recon_err = base_err = 0.0
    for stat_id, fn in derivers.items():
        scr = scraped.get(stat_id)
        rec_rate, base_rate = fn(recon), fn(state)   # state == baseline (recon not committed yet)
        rates[stat_id] = {"scraped": scr, "reconstructed": round(rec_rate, 4),
                          "baseline": round(base_rate, 4)}
        if scr is None:
            within_tol = False        # can't validate this rate
            continue
        have_scrape = True
        recon_err += abs(rec_rate - scr)
        base_err += abs(base_rate - scr)
        if abs(rec_rate - scr) > LIVE_RATE_TOL[stat_id]:
            within_tol = False
    if not have_scrape:
        verdict = "no_scrape"
    elif within_tol:
        verdict = "matched"
    elif recon_err < base_err:
        verdict = "closer"            # reconstruction nearer the scrape than the stale baseline
    else:
        verdict = "baseline"
    if verdict in ("matched", "closer"):
        for c in components:
            state[c] = recon[c]
    return {"group": name, "accepted": verdict in ("matched", "closer"),
            "verdict": verdict, "matched_lines": n_matched, "rates": rates}


def reconcile_live_components(
    baseline: dict[int, float],
    *,
    pitcher_lines: list[dict],
    batter_lines: list[dict],
    slot_by_norm_name: dict[str, int],
    scraped: dict[int, float],
    settled_floor: dict[int, float] | None = None,
) -> tuple[dict[int, float], list[dict]]:
    """Replace ESPN's once-daily-stale pitching/OPS components in `baseline` with
    live values reconstructed from MLB box-score lines — but only for a rate
    group whose implied rate matches ESPN's live *scraped* rate. Pure.

    `pitcher_lines`/`batter_lines` are the team's unsettled-window lines (games
    not yet in ESPN's banked totals). `slot_by_norm_name` is each rostered
    player's fantasy lineup slot that day. `scraped` holds the live displayed
    rates {STAT_ERA, STAT_WHIP, STAT_OPS} (authoritative for current standings).

    `settled_floor` (optional) is {STAT_QS|STAT_SVHD: floor} — the count already
    banked from games *aged out* of the window (see `load_settled_floor`). It
    guards the QS/SVHD counting credits against double-counting the live scrape
    (see the QS/SVHD block below). Omitted ⇒ those credits fall back to additive
    (isolated/unit callers only; production always supplies it).

    Returns (state, decisions): `state` is a copy of `baseline` with components
    swapped in for accepted groups; `decisions` records each group's verdict.
    """
    state = dict(baseline)
    decisions: list[dict] = []

    # ── pitching group (ERA + WHIP): OUTS/ER/P_H/P_BB, all REST-only ──
    pit, n_pit = _sum_counted(
        pitcher_lines, slot_by_norm_name, PITCHER_SLOTS,
        ("outs", "er", "p_h", "p_bb"),
    )
    recon_pit = dict(baseline)
    recon_pit[STAT_OUTS] = baseline.get(STAT_OUTS, 0) + pit["outs"]
    recon_pit[STAT_ER]   = baseline.get(STAT_ER, 0)   + pit["er"]
    recon_pit[STAT_P_H]  = baseline.get(STAT_P_H, 0)  + pit["p_h"]
    recon_pit[STAT_P_BB] = baseline.get(STAT_P_BB, 0) + pit["p_bb"]
    decisions.append(_judge_group(
        "pitching", state, recon_pit, PITCH_RATE_COMPONENTS,
        {STAT_ERA: derive_era, STAT_WHIP: derive_whip}, scraped, n_pit,
    ))

    # ── OPS group: AB/2B/3B/BB/HBP/SF (REST-only). H & HR stay at baseline —
    #    they're scored cats the scrape already keeps live. ──
    bat, n_bat = _sum_counted(
        batter_lines, slot_by_norm_name, HITTER_SLOT_IDS,
        ("ab", "b2", "b3", "bb", "hbp", "sf"),
    )
    recon_ops = dict(baseline)
    recon_ops[STAT_AB]   = baseline.get(STAT_AB, 0)   + bat["ab"]
    recon_ops[STAT_2B]   = baseline.get(STAT_2B, 0)   + bat["b2"]
    recon_ops[STAT_3B]   = baseline.get(STAT_3B, 0)   + bat["b3"]
    recon_ops[STAT_B_BB] = baseline.get(STAT_B_BB, 0) + bat["bb"]
    recon_ops[STAT_HBP]  = baseline.get(STAT_HBP, 0)  + bat["hbp"]
    recon_ops[STAT_SF]   = baseline.get(STAT_SF, 0)   + bat["sf"]
    decisions.append(_judge_group(
        "ops", state, recon_ops, OPS_RECON_COMPONENTS,
        {STAT_OPS: derive_ops}, scraped, n_bat,
    ))

    # ── QS / SVHD: counting credits the live *scrape* ALSO owns. Unlike the rate
    #    components above (ER/OUTS/AB… are REST-only and genuinely settle-lagged, so
    #    adding box-score values to a baseline that lacks them is safe), QS and SVHD
    #    are scored *display* cats the DOM scrape banks into `baseline` the instant a
    #    game goes Final — long before the 7h settle boundary. So they must NOT be
    #    added on top of `baseline`: the scrape and `_count_qs`/`_count_svhd` both see
    #    the same in-window Final games, and naive addition double-counts for the
    #    whole settle window.
    #      (deGrom incident, 2026-06-07: a legitimate QS the scrape banked (weekly
    #       2→3) was *also* re-added by `_count_qs` while its game sat inside the 7h
    #       window → sim QS 3→4 → WP 100%, reverting to 3 only when the window aged
    #       the game out at the next 07:00 roll → a 100%→0% flip on the tiebreaker.)
    #    Correct rule — split the weekly total into settled (before the window) +
    #    in-window credit, and take whichever source is ahead:
    #        result = max(scraped_weekly, settled_floor + box_count)
    #    `settled_floor` is the QS/SVHD already banked from aged-out games (running
    #    min of the scraped weekly count over the window-day — observation-driven, so
    #    it needs no settle-clock assumption and self-heals a downward correction).
    #    The `max` is fail-safe: never below the authoritative scrape (a lagging
    #    scrape can't drop a real credit — preserves the in-progress→Final gap fill),
    #    never the double-count. No floor (isolated callers) ⇒ default floor =
    #    scraped ⇒ behaves additively. Final-only still avoids overlap with the
    #    in-progress QS/SVHD model. ──
    floors = settled_floor or {}

    qs_added, n_qs = _count_qs(pitcher_lines, slot_by_norm_name)
    qs_scraped = baseline.get(STAT_QS, 0) or 0
    qs_result = qs_scraped
    if qs_added:
        qs_floor = floors.get(STAT_QS, qs_scraped)   # no floor → additive
        qs_result = max(qs_scraped, qs_floor + qs_added)
        state[STAT_QS] = qs_result
    decisions.append({"group": "qs", "accepted": qs_added > 0, "matched_lines": n_qs,
                      "qs_added": qs_added, "scraped": qs_scraped,
                      "floor": floors.get(STAT_QS), "result": qs_result})

    svhd_added, n_svhd = _count_svhd(pitcher_lines, slot_by_norm_name)
    svhd_scraped = baseline.get(STAT_SVHD, 0) or 0
    svhd_result = svhd_scraped
    if svhd_added:
        svhd_floor = floors.get(STAT_SVHD, svhd_scraped)
        svhd_result = max(svhd_scraped, svhd_floor + svhd_added)
        state[STAT_SVHD] = svhd_result
    decisions.append({"group": "svhd", "accepted": svhd_added > 0, "matched_lines": n_svhd,
                      "svhd_added": svhd_added, "scraped": svhd_scraped,
                      "floor": floors.get(STAT_SVHD), "result": svhd_result})

    return state, decisions


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
    # Provenance: which special-case paths shaped this budget (telemetry only —
    # never read by the sim). Emitted into details_json via budget_summary so a
    # WP investigation can see the path without reverse-engineering it. Values:
    #   promoted          gs/gp said RP but announced/live start promoted him to SP
    #   cadence           extra starts from the rotation-turn model
    #   flat-extra        extra starts from the flat ROS-share fallback
    #   start-capped      physical start-count cap clipped the extra dist
    #   qs-ingame         in-progress QS override replaced the rate-based share
    #   svhd-ingame       in-progress SVHD override replaced the rate-based share
    #   relief-svhd       SP's SVHD re-sourced from projected relief appearances
    #                     (starts bank none) — swingman/spot-starter, see _sp_relief_svhd
    #   benched-live-drop benched at first pitch → In-Progress games dropped
    #   live-keepalive    zero-unit budget kept so an exited starter's QS survives
    #   two-way-sub       hitter days reduced by estimated pitching starts
    flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SimContext:
    """Everything `build_budgets` consumes beyond the roster + schedule,
    bundled so it threads `compute → simulate → build_budgets →
    _hitter_days_slotted` as ONE argument instead of eight parallel optionals.

    History: each live-data feature used to add its own `... | None = None`
    parameter down that chain, and every default meant "feature silently off" —
    a caller that forgot one got pre-feature behavior with no error (the
    lineup_slot_counts GOTCHA below; the benched-gating "absent map ⇒ no
    gating" trap). The defaults here still ARE the off-states — that's what
    makes isolated tests cheap — but there is now exactly one construction
    site in `cli.compute` for production, and a test that enables a feature
    names the field it's enabling.

    Adding a new sim input? Add a field here (off-state default + comment),
    populate it in `cli.compute`, and read it via an alias at the top of
    `build_budgets` — no signature changes anywhere.

    Fields (default ⇒ that feature contributes nothing):
      team_total_ros_games   MLB team → remaining games; SP flat-share + RP rates
      lineup_slot_counts     league slot capacities. GOTCHA: without it the
                             hitter optimizer has no slots and every hitter
                             silently budgets 0 days.
      live_by_team           live pitcher box lines (in-game QS/SVHD, exits)
      live_batters_by_team   live batter lines (removed-hitter zeroing)
      last_start_by_pitcher  cadence anchors (norm name → last start date)
      use_cadence            rotation-turn extra-start model (current week only;
                             future weeks use the flat ROS-share split)
      as_of                  injected UTC "today" for IL logic; None ⇒ resolved
                             to _utc_today() once at the build_budgets boundary
      slot_by_norm_name      THIS side's daily lineup slots (benched gating).
                             Side-specific: `simulate` fills it per side from
                             MatchupInputs; None ⇒ no benched gating.
    """
    team_total_ros_games: dict[int, int] = field(default_factory=dict)
    lineup_slot_counts: dict[int, int] = field(default_factory=dict)
    live_by_team: dict[int, dict[str, dict]] = field(default_factory=dict)
    live_batters_by_team: dict[int, dict[str, dict]] = field(default_factory=dict)
    last_start_by_pitcher: dict[str, str] = field(default_factory=dict)
    use_cadence: bool = True
    as_of: date | None = None
    slot_by_norm_name: dict[str, int] | None = None


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


def _rp_remaining_units(team_id: int,
                        schedule_by_team: dict[int, list[dict]],
                        return_date: date | None = None) -> float:
    return sum(_rp_factor(g)
               for g in schedule_by_team.get(team_id, [])
               if _game_after_return(g, return_date))


def _is_announced_or_live_starter(full_name: str, team_id: int,
                                  schedule_by_team: dict[int, list[dict]],
                                  live_by_team: dict[int, dict[str, dict]]) -> bool:
    """True when a pitcher is the **announced probable** for an in-window game, or
    is **currently making a start** (live `games_started`). Such a pitcher is a
    starter for that game regardless of his season GS/GP ratio — used to promote a
    misclassified rotation SP / spot starter to the SP path so his start (and its
    QS/K/innings) is projected, not missed (the spot-starter blind spot: e.g. a
    swingman who's moved into the rotation but whose ESPN ROS projection still has
    GS/GP < 0.5). Caller only consults this when the ratio says RP."""
    nn = _norm_name(full_name)
    if not nn:
        return False
    for g in schedule_by_team.get(team_id, []):
        # Only an *upcoming or in-progress* start promotes him — a Final game he
        # already started is banked, and counting it would route a swingman who's
        # since returned to the bullpen onto the SP path and drop his remaining
        # relief appearances.
        if g.get("game_status") in FINAL_GAME_STATES:
            continue
        if _norm_name(g.get("probable_pitcher_name")) == nn:
            return True
    live = (live_by_team.get(team_id) or {}).get(nn)
    return bool(live and live.get("games_started"))


def _probable_starts_for(player_name: str, team_id: int,
                         schedule_by_team: dict[int, list[dict]],
                         sp_exit_inning: float,
                         return_date: date | None = None,
                         live: dict | None = None) -> float:
    """Sum of SP factors over games where this pitcher is the probable
    starter and the game is on/after their estimated return date. `live` is
    the pitcher's own live box line (`PitcherSituation.live`), if any.

    Once he's **exited** his in-progress start (a later pitcher has appeared —
    `live` line `games_started` with `is_last` falsey), that start is over: its
    remaining counter production (K/OUTS/ER) is zero, so its factor drops out here.
    His *earned* QS for it is supplied separately by `_override_sp_qs`. While he's
    still pitching, the factor decays normally with the game's innings."""
    target = _norm_name(player_name)
    if not target:
        return 0.0
    total = 0.0
    for g in schedule_by_team.get(team_id, []):
        if not _game_after_return(g, return_date):
            continue
        if _norm_name(g.get("probable_pitcher_name")) == target:
            if (live and live.get("games_started") and not live.get("is_last")
                    and live.get("game_pk") == g.get("game_pk")):
                continue   # exited this start → no remaining counters
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
        if g.get("game_status") in FINAL_GAME_STATES:
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
                         ctx: SimContext | None = None,
                         ) -> dict[int, float]:
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
    ctx = ctx or SimContext()
    lineup_slot_counts = ctx.lineup_slot_counts
    slot_by_norm_name = ctx.slot_by_norm_name
    live_batters_by_team = ctx.live_batters_by_team
    as_of = ctx.as_of or _utc_today()
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
            # Drop In-Progress games for a hitter who can't (still) bat in one:
            #   - benched at first pitch (locked out, can't be moved in), or
            #   - already removed from the game (a later batter took his slot —
            #     `still_in` False in the live line).
            # A not-yet-started game on the same day still counts (lineup not locked).
            if (_is_benched_today(p.get("full_name"), slot_by_norm_name)
                    or _is_removed_from_game(p, live_batters_by_team)):
                team_games_today = [g for g in team_games_today
                                    if g.get("game_status") != "In Progress"]
            if not team_games_today:
                continue
            # Two-way players starting on the mound today can't bat.
            if _is_probable_starter_on(p, date_str, schedule_by_team):
                continue
            # SUM across the day's games, not max: on a doubleheader a hitter in
            # the day's lineup bats in BOTH games, so both count (a Final game
            # contributes 0, an in-progress one its remaining fraction, a
            # Scheduled one 1.0). The old max() silently dropped the second game
            # of a doubleheader — fixed 2026-07-11. Slot assignment stays per-day
            # (one lineup slot per day); only the credited production sums.
            factor = sum(_hitter_factor(g) for g in team_games_today)
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


def _is_benched_today(full_name: str,
                      slot_by_norm_name: dict[str, int] | None) -> bool:
    """True when the day's locked lineup (today's `daily_lineups`, via
    `load_active_slots`) has this player on the **bench or IL** — not in any active
    slot. League rules lock the lineup at each game's first pitch, so a player benched
    then is locked out of that game and can't score it. Callers therefore drop
    **In-Progress** games from a benched player's projection (every role), zeroing the
    remaining production for a game they can't play; *future* (not-yet-started) games
    stay, since a manager may still activate them (the streaming hedge). No map
    (tests / isolated callers) ⇒ not benched, preserving prior behavior. Keyed on
    `NON_COUNTING_SLOTS` (not 'not a pitcher slot') so it's role-agnostic and
    two-way-safe — a player in *any* active slot is not benched."""
    if not slot_by_norm_name:
        return False
    return slot_by_norm_name.get(_norm_name(full_name)) in NON_COUNTING_SLOTS


def _is_removed_from_game(p: dict,
                          live_batters_by_team: dict[int, dict[str, dict]] | None) -> bool:
    """True when this hitter's live line says he's been **removed** from his
    in-progress game (`still_in` False — a later batter took his lineup slot). He
    can't bat again, so his remaining in-progress production is zero. No live map /
    no line for him ⇒ not removed (he hasn't been pulled, or isn't in a live game)."""
    if not live_batters_by_team:
        return False
    line = live_batters_by_team.get(p.get("pro_team_id"), {}).get(_norm_name(p.get("full_name")))
    return line is not None and not line.get("still_in", 1)


def _drop_inprogress_for_benched(schedule_by_team: dict[int, list[dict]],
                                 team_id: int, full_name: str,
                                 slot_by_norm_name: dict[str, int] | None
                                 ) -> dict[int, list[dict]]:
    """Schedule view for one player: identical to `schedule_by_team`, except a
    player benched at first pitch (`_is_benched_today`) has his team's **In-Progress**
    games removed — he's locked out of them, so they must contribute nothing to his
    projection. Returns the original dict unchanged when he's not benched (the common
    case), so there's no per-player copy cost unless the gate fires."""
    if not _is_benched_today(full_name, slot_by_norm_name):
        return schedule_by_team
    kept = [g for g in schedule_by_team.get(team_id, [])
            if g.get("game_status") != "In Progress"]
    return {**schedule_by_team, team_id: kept}


def _override_sp_qs(budget: Budget, ros: dict,
                    schedule_by_team: dict[int, list[dict]],
                    live: dict | None,
                    team_id: int, gs_ros: float, sp_exit_inning: float) -> bool:
    """Swap the in-progress start's QS share for the conditional in-game
    projection (`app.ingame`). `live` is the pitcher's own live box line
    (`PitcherSituation.live`). Other games and other counters are untouched, so
    with no live game this is a no-op. (A benched pitcher never reaches here with a
    live start — his In-Progress game is dropped from the schedule view first.)
    Returns True when it materially changed expected[QS] (provenance flag)."""
    if gs_ros <= 0:
        return False
    if not live or not live.get("games_started"):
        return False
    ip_games = {g["game_pk"]: g for g in schedule_by_team.get(team_id, [])
                if g.get("game_status") == "In Progress"}
    g = ip_games.get(live["game_pk"])
    if g is None:
        return False
    qs_rate = (ros.get(STAT_QS) or 0) / gs_ros
    outs_tot = ros.get(STAT_OUTS) or 0
    # Cap the inferred start length: a promoted/spot starter's tiny GS makes
    # outs_tot/gs_ros a relief-inflated nonsense value (e.g. 150 outs / 3 GS = 50).
    # A real start tops out ~MAX_START_OUTS; real SPs are already well under it.
    exp_outs = min(outs_tot / gs_ros, MAX_START_OUTS) if gs_ros else TYPICAL_START_OUTS
    er_per_out = (ros.get(STAT_ER) or 0) / outs_tot if outs_tot else 0.13
    exited = not live["is_last"]
    state = ingame.StarterState(
        game_status="In Progress", appeared=True, exited=exited,
        outs=live["outs"], er=live["er"], exp_outs_per_start=exp_outs,
        er_per_out=er_per_out, pregame_qs_rate=qs_rate,
    )
    # Rate-based share to drop before adding the in-game estimate. Once he's EXITED,
    # `_probable_starts_for` already zeroed this game's factor (his start is over —
    # no remaining counters), so the share isn't in `cur` and there's nothing to drop;
    # subtracting it would wrongly eat into other games' QS. While still in, the base
    # share is present, so drop it as before.
    ip_share = 0.0 if exited else qs_rate * _sp_factor(g, sp_exit_inning)
    cur = budget.expected.get(STAT_QS, 0.0)
    new = max(0.0, cur - ip_share) + ingame.project_qs(state)
    budget.expected[STAT_QS] = new
    return abs(new - cur) > 1e-9


def _override_rp_svhd(budget: Budget, ros: dict,
                      schedule_by_team: dict[int, list[dict]],
                      live: dict | None,
                      team_id: int, gp_ros: float, units_p: float,
                      rp_remaining: float) -> bool:
    """Swap each in-progress game's SVHD share for the in-game projection: the
    live line (`PitcherSituation.live`) if the reliever has appeared, else a
    game-script-gated rate for a closer who hasn't entered yet. No-op when
    nothing is live. (A benched reliever never reaches here — his In-Progress
    games are dropped from the schedule view first.)
    Returns True when it materially changed expected[SVHD] (provenance flag)."""
    if gp_ros <= 0 or rp_remaining <= 0:
        return False
    ip_games = {g["game_pk"]: g for g in schedule_by_team.get(team_id, [])
                if g.get("game_status") == "In Progress"}
    if not ip_games:
        return False
    svhd_rate = min((ros.get(STAT_SVHD) or 0) / gp_ros, MAX_SVHD_RATE)
    appearance_per_factor = units_p / rp_remaining   # expected apps per _rp_factor
    # A pitcher *starting* today can't earn a save or hold — skip the reliever
    # override entirely (it's keyed off the fantasy RP role; an RP-classified spot
    # starter would otherwise get a phantom SV/HLD). The QS path handles his start.
    if live and live.get("games_started"):
        return False
    changed = False
    for game_pk, g in ip_games.items():
        factor = _rp_factor(g)
        if factor <= 0:
            continue
        base_share = svhd_rate * appearance_per_factor * factor   # rate-based share
        cur = budget.expected.get(STAT_SVHD, 0.0)
        margin = _team_margin(g)   # current margin — used for the not-yet-pitched gate
        if live and live["game_pk"] == game_pk:
            exited = not live["is_last"]
            # Judge the save/hold from the conditions WHEN HE PITCHED, not the live
            # score: a hold is earned by entering in a save situation and leaving with
            # the lead — and stays earned if the lead is later padded (blowout) or a
            # *later* reliever blows it. Use the locked entry/exit margins; fall back
            # to the live margin only when they weren't captured (entry tick missed).
            entry_m = live.get("entry_margin")
            exit_m = live.get("exit_margin")
            entered_ss = _is_save_situation(entry_m if entry_m is not None else margin)
            if exited:
                lead_intact = (exit_m if exit_m is not None else margin) > 0
            else:
                lead_intact = margin > 0   # still pitching — hasn't surrendered it yet
            state = ingame.RelieverState(
                game_status="In Progress", appeared=True,
                exited=exited,
                entered_save_situation=entered_ss,
                lead_intact=lead_intact,
                recorded_out=live["outs"] >= 1,
                svhd_rate=svhd_rate,
            )
            new = max(0.0, cur - base_share) + ingame.project_svhd(state)
            budget.expected[STAT_SVHD] = new
        else:
            gate = ingame.game_script_gate(margin, g.get("current_inning") or 0)
            new = max(0.0, cur - base_share) + base_share * gate
            budget.expected[STAT_SVHD] = new
        changed = changed or abs(new - cur) > 1e-9
    return changed


def _cap_svhd_rate(stat_id: int, rate: float) -> float:
    """Bound the per-appearance SVHD rate at a realistic ceiling — see
    MAX_SVHD_RATE for context on ESPN's projection quirks. No-op for other stats."""
    if stat_id == STAT_SVHD and rate > MAX_SVHD_RATE:
        return MAX_SVHD_RATE
    return rate


def _expected_extra_starts(dist: list[float] | None) -> float:
    """E[k] for an extra-start distribution `[P(0), P(1), …]` (= Σ i·P(i))."""
    return sum(i * pk for i, pk in enumerate(dist or []))


def _max_remaining_starts(anchor: date | None, window_end: date | None,
                          min_rest: int = MIN_REST_DAYS) -> int | None:
    """How many more starts a pitcher can physically make from `anchor` (his last
    recorded start) through `window_end`, spaced ≥ `min_rest` days apart. `None`
    when either bound is unknown (caller skips the cap). Counts turns *after* the
    anchor, so it bounds the *remaining* starts — announced + cadence combined."""
    if anchor is None or window_end is None:
        return None
    first = anchor + timedelta(days=min_rest)
    if first > window_end:
        return 0
    return (window_end - first).days // min_rest + 1


def _cap_extra_dist(dist: list[float], max_extra: int) -> list[float]:
    """Fold probability mass above `max_extra` down onto it so the extra-start
    distribution can't exceed what the rotation physically allows. Stays a valid
    distribution (sums to 1). `max_extra=0` ⇒ `[1.0]` (no extra start)."""
    max_extra = max(0, max_extra)
    if len(dist) - 1 <= max_extra:
        return dist
    capped = list(dist[:max_extra + 1])
    capped[max_extra] += sum(dist[max_extra + 1:])
    return capped


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
        rates[stat_id] = _cap_svhd_rate(stat_id, ros_v / denom)
    return rates


def _sp_relief_svhd(ros: dict, gs_ros: float, gp_ros: float,
                    rp_remaining: float, total_ros: float) -> float:
    """SVHD a *starting* pitcher can still bank from RELIEF appearances this
    week — for swingmen / spot-starters whose ROS role is still part-reliever.

    A start banks no save/hold, so a starter's SVHD attaches only to his
    projected *non-start* appearances:

        (relief-appearance share) × (remaining relief-eligible team games)
        × (saves+holds per relief appearance)

    where relief share = (gp_ros − gs_ros) / team_ros_games (mirrors the RP
    branch's `(gp_ros/total_ros) × rp_remaining`, but on non-start appearances
    only), and the per-relief-appearance rate uses `gp_ros − gs_ros` as the
    denominator because his season saves/holds all came in relief. Auto-scales
    to ~0 as a pitcher's ROS GS approaches GP (a true rotation regular has no
    relief appearances left to project). Returns 0 with no projected relief
    role or no schedule room. Does not exclude his specific start day from the
    relief-eligible count — a rate-based expectation, so the ~1-game overlap is
    negligible (same simplification the RP branch already makes)."""
    relief_gp = (gp_ros or 0) - (gs_ros or 0)
    ros_svhd = ros.get(STAT_SVHD) or 0
    if relief_gp <= 0 or ros_svhd <= 0 or total_ros <= 0 or rp_remaining <= 0:
        return 0.0
    relief_units = (relief_gp / total_ros) * rp_remaining
    svhd_per_relief = min(ros_svhd / relief_gp, MAX_SVHD_RATE)
    return svhd_per_relief * relief_units


@dataclass(frozen=True)
class PitcherSituation:
    """One pitcher's current circumstances, resolved once per build_budgets pass.

    "Is he starting / benched / exited / promoted?" used to be re-derived
    independently by five name-matching helpers, and most live-credit incidents
    were those derivations disagreeing or one missing a case (the Melton
    phantom save, the Hunter Brown benched QS, the exited-starter sliver, the
    Phillips invisible start). That state is now decided HERE, once, and every
    downstream branch reads the struct. Per-game arithmetic (factors, the
    cadence walk) stays in the specialist helpers — they receive `sched` and
    `live` from the struct instead of re-matching names.
    """
    role: str                     # 'SP' | 'RP' — final classification
    ratio_sp: bool                # season GS/GP ratio said SP
    promoted: bool                # ratio said RP; announced/live start promoted him
    benched_today: bool           # NON_COUNTING_SLOT in today's locked lineup
    live: dict | None             # his live box line (this team's live game), if any
    has_live_start: bool          # that line is a start (games_started truthy)
    exited: bool                  # started and a later pitcher has appeared
    live_start_in_progress: bool  # the started game is In Progress in `sched`
    sched: dict[int, list[dict]]  # his schedule view (benched ⇒ In-Progress dropped)


def _resolve_pitcher_situation(p: dict, schedule_by_team: dict[int, list[dict]],
                               ctx: SimContext) -> PitcherSituation:
    """Build the PitcherSituation for one rostered pitcher — pure, and the single
    place a pitcher's identity is matched against live lines and lineup slots."""
    team_id = p["pro_team_id"]
    full_name = p["full_name"]
    ros = p["ros_stats"]

    benched_today = _is_benched_today(full_name, ctx.slot_by_norm_name)
    # Schedule view: benched at first pitch → locked out of games already
    # underway, so In-Progress games are dropped and that start contributes
    # nothing anywhere downstream (QS, K/OUTS/ER, SV/HLD alike). Scheduled
    # games stay — the streaming hedge.
    sched = _drop_inprogress_for_benched(
        schedule_by_team, team_id, full_name, ctx.slot_by_norm_name)

    live = (ctx.live_by_team.get(team_id) or {}).get(_norm_name(full_name))
    has_live_start = bool(live and live.get("games_started"))
    exited = bool(has_live_start and not live.get("is_last"))
    # His started game is live *in his schedule view* — i.e. `_override_sp_qs`
    # has a start to act on. False for a benched pitcher (the game was dropped
    # above) and once the game goes Final (the Final-only QS reconstruction
    # takes over — no overlap). Keeps an exited starter's earned-but-unbanked
    # QS creditable while the game runs (the Yamamoto 04:15→07:00 seam).
    live_start_in_progress = has_live_start and any(
        g.get("game_status") == "In Progress"
        and g.get("game_pk") == live.get("game_pk")
        for g in sched.get(team_id, []))

    gs_ros = ros.get(STAT_GS) or 0
    gp_ros = ros.get(STAT_PITCH_GP) or 0
    if gp_ros > 0:
        ratio_sp = (gs_ros / gp_ros) > 0.5
    else:
        ratio_sp = (p["default_position_id"] == 1)
    # Promote a misclassified rotation SP / spot starter: the ratio says RP but
    # he's the announced probable for an upcoming/in-progress game, or is
    # currently starting (see _is_announced_or_live_starter for the Final-game
    # caveat that protects a swingman's remaining relief).
    promoted = (not ratio_sp) and _is_announced_or_live_starter(
        full_name, team_id, sched, ctx.live_by_team)
    return PitcherSituation(
        role="SP" if (ratio_sp or promoted) else "RP",
        ratio_sp=ratio_sp, promoted=promoted, benched_today=benched_today,
        live=live, has_live_start=has_live_start, exited=exited,
        live_start_in_progress=live_start_in_progress, sched=sched)


def build_budgets(roster: list[dict],
                  schedule_by_team: dict[int, list[dict]],
                  ctx: SimContext | None = None,
                  ) -> list[Budget]:
    """Convert a roster + schedule into per-player production budgets.

    Everything else the model consumes rides in `ctx` (see SimContext — each
    field documents its off-state default).

    Inclusion rules:
      - IL slot or definitely-out injury status → skipped.
      - All other rostered pitchers (BE included) → considered. SP starts use
        the hybrid estimate (announced probables + a ROS-share estimate over
        games with no probable yet — see the SP branch); RP appearances come
        from the ROS-rate estimator.
      - Hitters → run through the per-day lineup optimizer; their units
        are the sum of days they win a slot.

    GOTCHA: `ctx.lineup_slot_counts` (the league's slot capacities, from
    scoring_settings.lineup_slots_json — see cli.compute) must be set or the
    optimizer has no slots to fill and **every hitter silently comes back with
    0 days / no budget**. Easy to miss in ad-hoc analysis scripts; pitchers
    are unaffected.
    """
    ctx = ctx or SimContext()
    if ctx.as_of is None:
        ctx = replace(ctx, as_of=_utc_today())
    # Local aliases — the body predates SimContext and reads these ~40 times.
    team_total_ros_games = ctx.team_total_ros_games
    lineup_slot_counts = ctx.lineup_slot_counts
    live_by_team = ctx.live_by_team
    live_batters_by_team = ctx.live_batters_by_team
    last_start_by_pitcher = ctx.last_start_by_pitcher
    use_cadence = ctx.use_cadence
    as_of = ctx.as_of
    slot_by_norm_name = ctx.slot_by_norm_name

    # End of the scoring window = latest scheduled game across the period (the
    # schedule is already period-clamped). Used for the physical start-count cap.
    _all_dates = [g["game_date"] for games in schedule_by_team.values()
                  for g in games if g.get("game_date")]
    window_end = date.fromisoformat(max(_all_dates)) if _all_dates else None
    hitter_units = _hitter_days_slotted(roster, schedule_by_team, ctx)

    out: list[Budget] = []
    for p in roster:
        if not _is_playable(p, as_of):
            continue
        ros = p["ros_stats"]
        pos = p["default_position_id"]
        team_id = p["pro_team_id"]
        # Estimated return for IL'd players — filters games before they
        # can play. None when player is healthy now (no filter).
        ret = _est_return_date(p, as_of)
        if ret is not None and ret <= as_of:
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
            # One resolution of role / benched / live-start state per pitcher —
            # every branch below reads the struct (see PitcherSituation: the
            # SP-vs-RP classification incl. spot-starter promotion, the benched
            # schedule view, and the live-line/exited state all live there).
            sit = _resolve_pitcher_situation(p, schedule_by_team, ctx)
            sched = sit.sched
            # Provenance flags for this pitcher's budget (see Budget.flags).
            # Telemetry only — collected along the way, attached at the end.
            pflags: list[str] = []
            if sit.benched_today:
                pflags.append("benched-live-drop")
            gs_ros = ros.get(STAT_GS) or 0
            gp_ros = ros.get(STAT_PITCH_GP) or 0
            promoted_sp = sit.promoted
            is_sp = sit.role == "SP"
            if promoted_sp:
                pflags.append("promoted")

            if is_sp:
                # SP starts split into a FIXED piece (announced probables + any
                # live start; near-certain count) and a STOCHASTIC EXTRA piece
                # (the un-probabled tail of the week). The cadence model projects
                # the extra piece as a distribution over integer starts sampled
                # per-sim; with no usable anchor we fall back to the old flat
                # ROS-share smear so behavior never regresses. Either way the
                # fixed and extra pieces never overlap (probable games are
                # excluded from the extra piece — no double-count).
                ros_outs = ros.get(STAT_OUTS, 0) or 0
                if gs_ros > 0:
                    avg_outs_per_start = ros_outs / gs_ros
                    # A promoted starter's tiny GS makes ros_outs/gs_ros a
                    # relief-inflated nonsense length — cap it to a real start.
                    if promoted_sp:
                        avg_outs_per_start = min(avg_outs_per_start, MAX_START_OUTS)
                    sp_exit_inning = max(1.0, avg_outs_per_start / 3.0 + 1.0)
                else:
                    avg_outs_per_start = TYPICAL_START_OUTS
                    sp_exit_inning = max(1.0, avg_outs_per_start / 3.0 + 1.0)
                # Per-start rate basis for the cumulative counters (K/OUTS/ER/…):
                # a real SP divides ROS totals by GS; a promoted starter's GS is
                # unreliable (relief outs inflate it), so use an effective start
                # count = ros_outs / start-length instead. QS is handled separately
                # below (it's a per-start event, not per-out). Real SPs unchanged.
                if promoted_sp and ros_outs > 0 and avg_outs_per_start > 0:
                    sp_rate_denom = ros_outs / avg_outs_per_start
                else:
                    sp_rate_denom = gs_ros
                probable_units = _probable_starts_for(
                    p["full_name"], team_id, sched, sp_exit_inning, ret,
                    live=sit.live,
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
                    p["full_name"], team_id, sched,
                    last_start_by_pitcher, ret,
                ) if use_cadence else None
                extra_src = "cadence" if extra_dist is not None else None
                if extra_dist is None:
                    open_weight = _open_sp_game_weight(
                        team_id, sched, sp_exit_inning, ret,
                    )
                    total_ros = team_total_ros_games.get(team_id, 0)
                    if total_ros > 0 and gs_ros > 0 and open_weight > 0:
                        rate = min(gs_ros / total_ros, MAX_SP_RATE)
                        flat_mean = rate * open_weight
                        if flat_mean > 0:
                            extra_dist = _split_mean_to_dist(flat_mean)
                            extra_src = "flat-extra"
                if extra_dist is not None:
                    # Physical backstop: a pitcher can't start more often than the
                    # rotation allows, so cap the *extra* (speculative) piece such
                    # that announced starts + extra never exceed the min-rest-spaced
                    # turns physically possible in the remaining window. Only the
                    # extra is clipped — announced probable starts (`probable_units`)
                    # are always respected. Anchored on the last *recorded* start so
                    # the announced start is counted within the budget. Prevents
                    # transient impossible-back-to-back projections (2026-06-26
                    # Rasmussen: a cadence Jun-27 turn colliding with a soon-to-be-
                    # announced Jun-28 start briefly projected 2 starts in 2 days).
                    # Current week only: the anchor (last recorded start) is fresh,
                    # so the physical bound is meaningful. For future weeks the anchor
                    # is ~a week stale (the flat fallback is anchor-independent by
                    # design — see test_use_cadence_flag_gates_the_model), so skip.
                    anchor_iso = (last_start_by_pitcher.get(_norm_name(p["full_name"]))
                                  if use_cadence else None)
                    if anchor_iso:
                        try:
                            phys = _max_remaining_starts(
                                date.fromisoformat(anchor_iso), window_end)
                        except ValueError:
                            phys = None
                        if phys is not None:
                            max_extra = phys - math.ceil(probable_units - 1e-9)
                            pre_cap = extra_dist
                            extra_dist = _cap_extra_dist(extra_dist, max_extra)
                            if extra_dist is not pre_cap:
                                pflags.append("start-capped")
                    # Probables are the fixed units; the open tail is sampled.
                    # sp_est_units = E[extra starts], used for the two-way
                    # subtraction and the displayed start count.
                    if extra_src:
                        pflags.append(extra_src)
                    sp_extra_dist = extra_dist
                    sp_extra_per_start = _per_start_rates(ros, sp_rate_denom)
                    sp_est_units = _expected_extra_starts(extra_dist)
                units_p = probable_units
                denom_p = sp_rate_denom
                role_p = "SP"
            else:
                rp_remaining = _rp_remaining_units(team_id, sched, ret)
                total_ros = team_total_ros_games.get(team_id, 0)
                if total_ros > 0 and gp_ros > 0:
                    units_p = (gp_ros / total_ros) * rp_remaining
                else:
                    units_p = rp_remaining * RP_APPEARANCE_RATE
                if units_p > rp_remaining:
                    # Physical backstop: at most one appearance per team game.
                    # Only reachable when gp_ros exceeds the team's remaining
                    # games — i.e. a truncated/regressed denominator (see
                    # load_total_remaining_games), never healthy inputs.
                    units_p = rp_remaining
                    pflags.append("rp-apps-capped")
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
            # A starter who's pitched past his expected exit has units≈0, so
            # _make_budget drops him — but if his game is still in progress his
            # earned (now locked) QS must stay credited. Keep a minimal budget so
            # _override_sp_qs can supply it; otherwise it vanishes until the daily
            # settle (the Yamamoto 04:15→07:00 case). Hands off to the Final-only
            # QS reconstruction once the game ends (no overlap: this needs the game
            # In Progress, reconstruction needs it Final).
            if budget is None and role_p == "SP" and sit.live_start_in_progress:
                budget = Budget(player_id=p["player_id"], name=p["full_name"],
                                role="SP", units=0.0, expected={})
                pflags.append("live-keepalive")
            if budget:
                if role_p == "SP" and promoted_sp:
                    # The cumulative counters used the per-out denom; QS is a
                    # per-start event, so set it from the per-start QS rate
                    # (ros_qs/gs_ros) × total starts. Keeps the in-game override
                    # below composing — it drops qs_rate × sp_factor for the
                    # in-progress start and adds the live estimate.
                    qs_rate = ((ros.get(STAT_QS) or 0) / gs_ros) if gs_ros > 0 else DEFAULT_QS_RATE
                    budget.expected[STAT_QS] = min(qs_rate, 0.95) * budget.units
                # In-game override for the threshold/context stats — no-op
                # unless this pitcher's team has a game in progress right now.
                # Operates on the fixed `expected` only (a live start is a
                # probable, hence fixed); the extra piece is never live.
                if role_p == "SP":
                    if _override_sp_qs(budget, ros, sched, sit.live,
                                       team_id, gs_ros, sp_exit_inning):
                        pflags.append("qs-ingame")
                else:
                    if _override_rp_svhd(budget, ros, sched, sit.live,
                                         team_id, gp_ros, units_p,
                                         rp_remaining):
                        pflags.append("svhd-ingame")
                # A start banks no save/hold: strip the season-rate SVHD smear
                # from both the fixed start budget and the sampled extra starts,
                # then re-add only the SVHD he'd earn from projected RELIEF
                # appearances this week (swingmen / spot-starters still part-
                # reliever by ROS role; ~0 for a true rotation regular). SVHD
                # follows relief appearances, never the start he's making.
                if role_p == "SP":
                    budget.expected.pop(STAT_SVHD, None)
                    if budget.extra_per_start:
                        budget.extra_per_start.pop(STAT_SVHD, None)
                    relief_svhd = _sp_relief_svhd(
                        ros, gs_ros, gp_ros,
                        _rp_remaining_units(team_id, sched, ret),
                        team_total_ros_games.get(team_id, 0))
                    if relief_svhd > 0:
                        budget.expected[STAT_SVHD] = relief_svhd
                        pflags.append("relief-svhd")
                budget.flags.extend(pflags)
                out.append(budget)

        # ── Hitter budget ──────────────────────────────────────────────
        if _has_hitter_ros(ros):
            units_h = hitter_units.get(p["player_id"], 0.0)
            # Two-way players (Ohtani): the lineup optimizer blocks them as
            # hitters only on *announced-probable* start days, so subtract the
            # *estimated* (un-probabled) start days here to avoid counting them
            # both batting and pitching. For a future week that's all of their
            # SP starts; for the current week it's just the un-announced ones.
            two_way_sub = _has_pitcher_ros(ros) and sp_est_units > 0 and units_h > 0
            if _has_pitcher_ros(ros):
                units_h = max(0.0, units_h - sp_est_units)
            denom_h = ros.get(STAT_HIT_G) or 0
            budget = _make_budget(p, ros, units_h, denom_h, HITTER_COUNTERS, "HIT")
            if budget:
                if two_way_sub:
                    budget.flags.append("two-way-sub")
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
        rate = _cap_svhd_rate(stat_id, ros_v / denom)
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
    e_extra = _expected_extra_starts(b.extra_dist)
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
            elif stat_id in PER_EVENT_CAPPED:
                # QS/SVHD are ≤1 per start/appearance — Binomial, not Poisson,
                # or a single start could "earn" 2+ QS (see _binomial_from_mean).
                draw = _binomial_from_mean(exp)
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
                    elif stat_id in PER_EVENT_CAPPED:
                        # k extra starts, ≤1 QS/SVHD each → Binomial(k, rate).
                        draw = _binomial(k, min(1.0, rate))
                    else:
                        draw = _poisson(mean)
                    counters[stat_id] = counters.get(stat_id, 0) + draw
    return counters


def sample_team_totals(budgets: list[Budget], n: int) -> list[dict[int, float]]:
    """Draw n independent samples of one team's weekly counter totals from a
    fresh 0-0 state. The sim has no cross-team interaction (each side's
    production is sampled independently, then compared), so any hypothetical
    matchup between two teams is just `_decide` over a pair of these draws —
    the factorization app/playoffs.py uses to price all 66 possible playoff
    pairings from 12 per-team sample sets instead of 66 pairwise sims."""
    return [_simulate_team({}, budgets) for _ in range(n)]


# ── Top-level entrypoint ──

@dataclass
class MatchupInputs:
    matchup_id: int
    home_state: dict[int, float]
    away_state: dict[int, float]
    home_roster: list[dict]
    away_roster: list[dict]
    # Per-side daily lineup slots (benched gating) — per-matchup like the
    # rosters, so they live here rather than on the shared SimContext;
    # `simulate` copies each into the side's ctx.slot_by_norm_name.
    home_slot_by_norm_name: dict[str, int] | None = None
    away_slot_by_norm_name: dict[str, int] | None = None


def simulate(inputs: MatchupInputs,
             schedule_by_team: dict[int, list[dict]],
             ctx: SimContext | None = None,
             n_sims: int = DEFAULT_SIMS,
             ) -> tuple[float, float, dict]:
    ctx = ctx or SimContext()
    if ctx.as_of is None:
        # UTC, never host-local — resolved once per run so both sides (and the
        # hitter optimizer below them) see the same "today".
        ctx = replace(ctx, as_of=_utc_today())
    home_budgets = build_budgets(
        inputs.home_roster, schedule_by_team,
        replace(ctx, slot_by_norm_name=inputs.home_slot_by_norm_name))
    away_budgets = build_budgets(
        inputs.away_roster, schedule_by_team,
        replace(ctx, slot_by_norm_name=inputs.away_slot_by_norm_name))

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
            # Provenance (see Budget.flags) — which special-case paths shaped
            # this budget. Omitted when empty to keep the payload lean.
            if b.flags:
                rec["flags"] = b.flags
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
    # NB: team_rosters.status is stored by refresh-rosters but intentionally not
    # selected here — IL/bench logic keys off players.injury_status and
    # lineup_slot_id, never the ESPN roster-entry status, so it's left unread.
    rows = conn.execute(
        """
        SELECT tr.player_id, tr.lineup_slot_id,
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
                               to_period_id: int | None = None) -> dict[int, int]:
    """Total scheduled games per pro team from `from_period_id` through
    `to_period_id` — or, when `to_period_id` is None (the normal case),
    through the END of the stored schedule (the MLB regular season).

    This is the denominator for every "share of team games" rate derived
    from ESPN's ROS projections (the RP appearance share, the future-week
    SP flat share, the swingman relief-SVHD share). ESPN's ROS split spans
    the remaining *MLB* season, so the game count it's divided by must span
    the same window. Bounding it at the last fantasy regular-season week
    truncated the denominator and inflated every RP's weekly appearances —
    and with them K/SVHD/innings — by games(→season end)/games(→last_reg):
    ~×1.25 early season, ×1.76 by week 19, ×4+ by week 22 (2026-08-10
    Gregory Soto: ROS GP 26 / 24 truncated games → 6.5 projected
    appearances in a 6-game week).

    Postponed/suspended/cancelled rows are excluded: a makeup gets its own
    Scheduled row (possibly in a later period), so counting the dead row
    too would double-count that game."""
    where = "matchup_period_id >= ?"
    params: list = [from_period_id]
    if to_period_id is not None:
        where = "matchup_period_id BETWEEN ? AND ?"
        params.append(to_period_id)
    skip = ",".join("?" * len(_SKIP_SCHEDULE_STATES))
    params.extend(_SKIP_SCHEDULE_STATES)
    rows = conn.execute(
        f"""
        SELECT pro_team_id, COUNT(*) AS n
        FROM team_schedule
        WHERE {where}
          AND (game_status IS NULL OR game_status NOT IN ({skip}))
        GROUP BY pro_team_id
        """,
        params,
    ).fetchall()
    return {r["pro_team_id"]: r["n"] for r in rows}


# Game statuses that mean the game is NOT being played as part of this week —
# a makeup gets its own Scheduled row, so these must not count as a remaining
# start/appearance.
_SKIP_SCHEDULE_STATES = {"Postponed", "Suspended", "Cancelled", "Canceled"}


def load_schedule_by_team(conn: sqlite3.Connection,
                          matchup_period_id: int,
                          now: str | None = None) -> dict[int, list[dict]]:
    """Team → its games in this matchup period, for the sim's start/appearance
    projections.

    Three exclusions keep phantom games out of the projection:
      - **Out-of-window dates.** `matchup_period_id` is part of the
        `team_schedule` PK, so when a game is postponed its row keeps the
        original period id while its `game_date` moves months out (the makeup
        date). Without clamping to the period's Mon→Sun window, that row would
        be counted as a remaining game — making a probable project 2.0 starts
        and inflating teammates' RP appearances (Ranger Suarez / Chapman,
        2026-06-06).
      - **Postponed/suspended/cancelled status**, even if still dated in-window.
      - **Stale past-dated games that never went Final or live** (only when `now`
        is given). A postponement isn't reflected immediately — the makeup date and
        `Postponed` status can lag a day-plus behind in `team_schedule` (the row
        keeps its original in-window date + a stale `Scheduled` status until the
        next `refresh-schedule`). In that window the two checks above don't fire,
        so the sim counts a game that already should have happened and credits a
        phantom start. (2026-06-18 Imanaga: a postponed Cubs game suppressed a
        matchup's WP all week, only correcting when the Monday refresh-schedule
        finally moved the date to August.)

        **The cutoff uses a 1-day buffer (`game_date < today_utc − 1`), NOT
        `< today`.** `game_date` is the game's *US calendar* date, which is a day
        behind UTC for late/West-Coast games (a game dated D plays D-evening US =
        into D+1 early UTC). A bare `< today` drops a legit not-yet-started game
        the instant UTC ticks past midnight — the 2026-06-24 Gage Jump regression:
        a real start dated Jun 24, still pre-game at 00:01 UTC Jun 25, got dropped
        and suppressed Sox Teacher ~3% for ~4h until the game finally went Final.
        The buffer keeps a game through all of the next UTC day (covering any late
        game) while still catching genuinely-stale postponed rows, which sit ≥1 day
        past before they matter (Imanaga's lingered ~4 days).
    """
    start, end = mlb.matchup_period_window(matchup_period_id)
    rows = conn.execute(
        """
        SELECT pro_team_id, game_pk, game_date, opponent_pro_team_id, is_home,
               probable_pitcher_mlbam_id, probable_pitcher_name, game_status,
               current_inning, inning_state, team_runs, opponent_runs
        FROM team_schedule
        WHERE matchup_period_id = ? AND game_date BETWEEN ? AND ?
        ORDER BY game_date
        """,
        (matchup_period_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    # 1-day buffer for the US-date ⇄ UTC offset (see docstring).
    cutoff = (date.fromisoformat(now[:10]) - timedelta(days=1)).isoformat() if now else None
    out: dict[int, list[dict]] = {}
    for r in rows:
        if r["game_status"] in _SKIP_SCHEDULE_STATES:
            continue
        if (cutoff and r["game_date"] < cutoff
                and r["game_status"] not in FINAL_GAME_STATES
                and r["game_status"] != "In Progress"):
            continue
        out.setdefault(r["pro_team_id"], []).append(dict(r))
    return out


def load_live_pitchers(conn: sqlite3.Connection) -> dict[int, dict[str, dict]]:
    """Pitcher lines for games **in progress right now**, keyed by ESPN proTeamId
    then normalized pitcher name (matching how budgets look players up). For the
    in-game QS/SVHD override only, which must not fire on a Final game (its credit
    is already banked). `live_pitchers` now also retains recently-Final games for
    component reconstruction, so filter to In Progress here explicitly. Empty when
    no games are live — in which case build_budgets behaves exactly as before."""
    rows = conn.execute(
        """
        SELECT lp.game_pk, lp.name, lp.pro_team_id, lp.is_last, lp.games_started,
               lp.outs, lp.er, lp.k,
               ra.entry_margin, ra.exit_margin
        FROM live_pitchers lp
        LEFT JOIN reliever_appearances ra
               ON ra.game_pk = lp.game_pk AND ra.mlbam_id = lp.mlbam_id
        WHERE EXISTS (SELECT 1 FROM team_schedule ts
                      WHERE ts.game_pk = lp.game_pk AND ts.game_status = 'In Progress')
        """
    ).fetchall()
    out: dict[int, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["pro_team_id"], {})[_norm_name(r["name"])] = dict(r)
    return out


def load_live_batters_inprogress(conn: sqlite3.Connection) -> dict[int, dict[str, dict]]:
    """Batter lines for games **in progress right now**, keyed by ESPN proTeamId then
    normalized name. Carries `still_in` (False once a later batter took his lineup
    slot) so the hitter optimizer can zero a **removed** hitter's remaining production
    for an in-progress game — he won't bat again. In Progress only (a Final game's
    factor is already 0). Empty when nothing is live → optimizer behaves as before."""
    rows = conn.execute(
        """
        SELECT lb.pro_team_id, lb.name, lb.still_in
        FROM live_batters lb
        WHERE EXISTS (SELECT 1 FROM team_schedule ts
                      WHERE ts.game_pk = lb.game_pk AND ts.game_status = 'In Progress')
        """
    ).fetchall()
    out: dict[int, dict[str, dict]] = {}
    for r in rows:
        out.setdefault(r["pro_team_id"], {})[_norm_name(r["name"])] = dict(r)
    return out


def settle_boundary_date(now_utc) -> str:
    """The earliest game_date ESPN has NOT yet settled into its banked totals,
    as 'YYYY-MM-DD'. ESPN absorbs a stat-day ~SETTLE_LAG_HOURS after midnight
    UTC (~07:00), so shifting now back by that lag and taking the date gives the
    boundary: games on-or-after it are still 'live' (not in ESPN's REST totals)
    and are the ones we reconstruct."""
    return (now_utc - timedelta(hours=SETTLE_LAG_HOURS)).date().isoformat()


def load_unsettled_lines(conn: sqlite3.Connection, *, since_date: str) -> dict[str, list[dict]]:
    """All live box-score lines for games not yet in ESPN's banked totals
    (game_date >= since_date) → {"pitchers": [...], "batters": [...]}. Flat (not
    grouped by team): reconcile matches each line to a fantasy roster by name, so
    a line only contributes where its player is rostered + slotted. Empty when
    nothing is live, in which case reconciliation is a no-op."""
    pit = [dict(r) for r in conn.execute(
        """
        SELECT lp.name, lp.outs, lp.er, lp.p_h, lp.p_bb, lp.games_started,
               lp.sv, lp.hld,
               (SELECT ts.game_status FROM team_schedule ts
                WHERE ts.game_pk = lp.game_pk LIMIT 1) AS game_status
        FROM live_pitchers lp
        WHERE EXISTS (SELECT 1 FROM team_schedule ts
                      WHERE ts.game_pk = lp.game_pk AND ts.game_date >= ?)
        """, (since_date,))]
    bat = [dict(r) for r in conn.execute(
        """
        SELECT lb.name, lb.ab, lb.h, lb.b2, lb.b3, lb.hr, lb.bb, lb.hbp, lb.sf
        FROM live_batters lb
        WHERE EXISTS (SELECT 1 FROM team_schedule ts
                      WHERE ts.game_pk = lb.game_pk AND ts.game_date >= ?)
        """, (since_date,))]
    return {"pitchers": pit, "batters": bat}


def load_active_slots(conn: sqlite3.Connection, fantasy_team_id: int, *,
                      since_date: str, fallback_roster: list[dict]) -> dict[str, int]:
    """{norm_name: lineup_slot_id} for one fantasy team's players. Source of
    truth is `daily_lineups` (the latest snapshot on-or-after `since_date` — the
    locked lineup that actually scored that day); for any player without a daily
    snapshot yet we fall back to their current roster slot."""
    out: dict[str, int] = {}
    for p in fallback_roster:
        nm = _norm_name(p.get("full_name"))
        if nm:
            out[nm] = p.get("lineup_slot_id")
    rows = conn.execute(
        """
        SELECT dl.lineup_slot_id, dl.game_date, p.full_name
        FROM daily_lineups dl JOIN players p ON p.id = dl.player_id
        WHERE dl.fantasy_team_id = ? AND dl.game_date >= ?
        ORDER BY dl.game_date
        """,
        (fantasy_team_id, since_date),
    ).fetchall()
    for r in rows:  # ascending → the latest day's slot wins
        nm = _norm_name(r["full_name"])
        if nm:
            out[nm] = r["lineup_slot_id"]
    return out


def load_settled_floor(conn: sqlite3.Connection, matchup_id: int, team_id: int,
                       stat_ids, *, since_date: str,
                       as_of: str | None = None) -> dict[int, float]:
    """Per-cat 'settled floor' for the QS/SVHD double-count guard: the QS/SVHD
    already banked from games **aged out** of the reconstruction window (in-period
    games with `game_date < since_date`), counted directly from the write-once
    `pitcher_final_lines` archive + that date's locked `daily_lineups` slots — the
    same definition `_count_qs`/`_count_svhd` apply to in-window games.

    Why not the scrape: this used to be the running MIN of the scraped weekly count
    over the window-day, on the assumption that aged-out games' credits were all
    banked *before* the window-day began. That breaks when a prior-day (West-Coast /
    post-midnight) game's QS/SVHD scrape-banks **late** — *inside* the window-day:
    the day-min is then taken before that credit lands and drops it from the floor,
    which masks an in-window box credit. (2026-06-26: Ohtani's Jun-24 QS banked
    02:30 Jun-25 → floor=1 vs the true 2 → Early's Jun-25 box QS was hidden until the
    07:00 settle.) Counting aged-out games straight from the archive + that day's
    lineup is immune to *when* the scrape happened to capture them.

    The deGrom double-count guard is preserved: aged-out games are `game_date <
    since_date`, exactly disjoint from the in-window box count (`game_date >=
    since_date`), so `floor + box` never counts the same game twice.

    `as_of` (publish reproducibility) bounds the archive to lines finalized by then.
    Trade-off vs the old MIN: a *downward* correction to a settled QS/SVHD no longer
    self-heals (the archive is write-once) — but QS/SVHD are deterministic thresholds
    rarely revised after Final (unlike the H/error revisions the min guarded against).
    Returns {stat_id: floor} for whichever of QS/SVHD were requested."""
    want = {sid for sid in stat_ids if sid in (STAT_QS, STAT_SVHD)}
    if not want:
        return {}
    prow = conn.execute("SELECT matchup_period_id FROM matchups WHERE id=?",
                        (matchup_id,)).fetchone()
    if prow is None:
        return {}
    try:
        period_start = mlb.matchup_period_window(prow["matchup_period_id"])[0].isoformat()
    except Exception:
        return {}
    # That date's pitching-slot players, keyed by (game_date, normalized name), so
    # a pitcher is credited only on a day he was actually slotted to pitch.
    try:
        slot_rows = conn.execute(
            "SELECT dl.game_date, p.full_name, dl.lineup_slot_id "
            "FROM daily_lineups dl JOIN players p ON p.id = dl.player_id "
            "WHERE dl.fantasy_team_id=? AND dl.game_date>=? AND dl.game_date<?",
            (team_id, period_start, since_date)).fetchall()
        slot_by_day_name = {(r["game_date"], _norm_name(r["full_name"])): r["lineup_slot_id"]
                            for r in slot_rows}
        sql = ("SELECT game_date, name, games_started, outs, er, sv, hld "
               "FROM pitcher_final_lines WHERE game_date>=? AND game_date<?")
        params: list = [period_start, since_date]
        if as_of:
            sql += " AND final_at <= ?"
            params.append(as_of)
        lines = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return {}
    qs = svhd = 0
    for ln in lines:
        if slot_by_day_name.get((ln["game_date"], _norm_name(ln["name"]))) not in PITCHER_SLOTS:
            continue
        if (ln["games_started"] and (ln["outs"] or 0) >= ingame.QS_OUTS
                and (ln["er"] or 0) <= ingame.QS_MAX_ER):
            qs += 1
        svhd += (ln["sv"] or 0) + (ln["hld"] or 0)
    out: dict[int, float] = {}
    if STAT_QS in want:
        out[STAT_QS] = qs
    if STAT_SVHD in want:
        out[STAT_SVHD] = svhd
    return out


def apply_live_components(conn: sqlite3.Connection, fantasy_team_id: int,
                         baseline: dict[int, float], roster: list[dict],
                         unsettled_lines: dict[str, list[dict]], *,
                         since_date: str,
                         matchup_id: int | None = None) -> tuple[dict[int, float], list[dict]]:
    """Thin DB wrapper around `reconcile_live_components`: loads this team's
    daily lineup slots, takes the live scraped rates straight from `baseline`
    (the scrape writes them), looks up the QS/SVHD settled floors (when
    `matchup_id` is given), and returns (adjusted_state, decisions). No-op
    (returns baseline unchanged) when there are no live lines."""
    if not unsettled_lines["pitchers"] and not unsettled_lines["batters"]:
        return baseline, []
    slot_by_norm_name = load_active_slots(
        conn, fantasy_team_id, since_date=since_date, fallback_roster=roster)
    scraped = {STAT_ERA: baseline.get(STAT_ERA),
               STAT_WHIP: baseline.get(STAT_WHIP),
               STAT_OPS: baseline.get(STAT_OPS)}
    settled_floor = None
    if matchup_id is not None:
        settled_floor = load_settled_floor(
            conn, matchup_id, fantasy_team_id, (STAT_QS, STAT_SVHD),
            since_date=since_date)
    return reconcile_live_components(
        baseline,
        pitcher_lines=unsettled_lines["pitchers"],
        batter_lines=unsettled_lines["batters"],
        slot_by_norm_name=slot_by_norm_name,
        scraped=scraped,
        settled_floor=settled_floor,
    )


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
