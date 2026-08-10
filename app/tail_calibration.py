"""Tail calibration: when the model says a category is nearly decided, is it?

`calibration.py` measures whether a projected *total* is the right size. This
measures something the level analysis structurally cannot see: whether the
*probabilities* derived from those totals are honest at the extremes. The same
level error matters enormously when two teams are close in volume and not at all
when they are far apart, so a category priced at 1% that comes in at 3% is a 3x
probability error the level metric reports as a small bias.

Measurement only — one implementation shared by `scripts/tail_calibration.py`
and any future recurring check (the `INV_SITE_QS_OVERCREDIT` lesson: a second
implementation of the same rule drifts and becomes a false-positive generator).

Three things get measured, because the tail has three distinguishable defects:

  1. **Reliability** — bin every in-week forecast by its stated probability and
     compare against how often that side actually won the category.
  2. **Margin slope** — regress the settled margin on the projected margin
     through the origin. A slope below 1 means the model claims a bigger gap
     than materialises, which makes every probability derived from that gap too
     extreme. This is the *between-matchup* defect.
  3. **Dispersion** — back the sigma the sim used out of (p, projected margin)
     and compare it against the spread of the errors the model actually makes.
     Above 1 means the *within-week* distribution is too narrow.

**Roster churn is separated, not ignored.** The projections condition on the
current roster; a manager streaming in a starter mid-week is a documented
limitation, not a modelling error. A forecast is *churn-exposed* when the side
later gained a budget entrant of the relevant type (hitters for batting cats,
SP/RP for pitching cats) that had never appeared in this matchup's budgets
before. The filter is deliberately generous — it excuses the forecast whether or
not the newcomer produced anything — so the churn-free residual is a LOWER bound
on the model's own error. A single-side forecast is excused by its own side's
churn; a *margin* needs both sides clean.

Ground truth per category comes from a single home-vs-away comparison, never
from the per-team `result` flags: those are stamped independently per side and
can desync (the 2026-06-06 asymmetric-records bug).
"""
from __future__ import annotations

import json
import random
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import NormalDist

from app import stats as appstats
from app.calibration import FIRST_PERIOD, first_pitch
from app.mlb import matchup_period_window

# All ten scored categories — unlike the level metric, the rate cats are usable
# here. Their `*_avg` is an internal scale, but it only has to be monotone in
# the same direction as the displayed rate for a *margin* comparison to hold.
CATS = [1, 5, 18, 20, 23, 41, 47, 48, 63, 83]
BATTING = {1, 5, 18, 20, 23}

# Probability bins. Deliberately fine below 5% — that is the region under test,
# and one flat [0, 0.05) bucket hides an 84x sub-band inside a 2.8x average.
EDGES = [0.0, .001, .005, .01, .02, .05, .10, .20, .30, .40, .50,
         .60, .70, .80, .90, .95, .98, .99, .995, .999, 1.0]

TAIL_HI = 0.05          # the headline band, reported apart from the curve

# Days after the period's Monday at which to take ONE snapshot per matchup for
# the slope and dispersion measurements. One observation per unit per checkpoint
# keeps those two free of the snapshot autocorrelation that inflates the
# reliability curve's apparent sample size.
CHECKPOINTS = (1.0, 3.0, 5.0)
CHECKPOINT_TOL_HOURS = 6.0

# Dispersion is a body measurement: outside this range the probit blows up and
# the discreteness of a small counting category dominates the answer.
BODY_LO, BODY_HI = 0.05, 0.95

_NORM = NormalDist()
ALL, CLEAN = "all", "clean"


def bin_index(p: float) -> int:
    """Index into EDGES. Clamped, so p == 1.0 lands in the top bin."""
    for i in range(len(EDGES) - 1):
        if EDGES[i] <= p < EDGES[i + 1]:
            return i
    return len(EDGES) - 2


def _state(conn: sqlite3.Connection, matchup: sqlite3.Row) -> tuple[dict, dict]:
    from app import db as appdb
    return (appdb.latest_category_state(conn, matchup["id"], matchup["home_team_id"]),
            appdb.latest_category_state(conn, matchup["id"], matchup["away_team_id"]))


def outcome_by_stat(conn: sqlite3.Connection, matchup: sqlite3.Row) -> dict[int, str]:
    """Settled per-category winner as "HOME" / "AWAY" / "TIE".

    Derived from ONE comparison of the two sides' banked scores, so the result is
    symmetric by construction. A missing score reads as 0 (counting cats are
    cumulative from zero), keeping both sides comparable.
    """
    hs, aws = _state(conn, matchup)
    out: dict[int, str] = {}
    for sid in CATS:
        hv = (hs.get(sid) or {}).get("score")
        av = (aws.get(sid) or {}).get("score")
        if hv is None and av is None:
            continue                    # category not tracked at all
        hv, av = hv or 0, av or 0
        if hv == av:
            out[sid] = "TIE"
        else:
            home_better = (hv < av) if appstats.is_reversed(sid) else (hv > av)
            out[sid] = "HOME" if home_better else "AWAY"
    return out


def final_scores(conn: sqlite3.Connection, matchup: sqlite3.Row) -> dict[int, tuple]:
    hs, aws = _state(conn, matchup)
    return {sid: (((hs.get(sid) or {}).get("score") or 0),
                  ((aws.get(sid) or {}).get("score") or 0)) for sid in CATS}


@dataclass
class Unit:
    """One (matchup, side, category) — the independent thing being forecast."""
    won: int = 0
    min_p: float = 1.0
    tail_snapshots: int = 0
    last_tail_at: str | None = None
    churn_free: bool = True     # evaluated at the LAST sub-5% snapshot


@dataclass
class Result:
    periods: list[int] = field(default_factory=list)
    n_forecasts: int = 0
    n_churn_free: int = 0
    # (stat|None, ALL|CLEAN, bin_index) -> [count, sum_p, wins]
    bins: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0.0, 0]))
    # (stat|None, ALL|CLEAN, bin_index|"tail") -> {matchup_id: [sum_p, wins]}
    clusters: dict = field(default_factory=lambda: defaultdict(
        lambda: defaultdict(lambda: [0.0, 0])))
    units: dict = field(default_factory=dict)
    checkpoints: list = field(default_factory=list)
    totals: dict = field(default_factory=lambda: defaultdict(lambda: [0.0, 0]))
    gaps: dict = field(default_factory=lambda: defaultdict(lambda: [0.0, 0]))


def _targets(period: int) -> list[datetime]:
    start, _ = matchup_period_window(period)
    base = datetime.fromisoformat(f"{start.isoformat()}T00:00:00+00:00")
    return [base + timedelta(days=d) for d in CHECKPOINTS]


def _scan_matchup(conn: sqlite3.Connection, matchup_id: int, fp: str,
                  deadline: str, targets: list[datetime]) -> tuple:
    """One pass over a matchup's snapshots.

    Returns (forecasts, newest_entry, checkpoints) where `newest_entry` maps
    (side, group) to the latest time a name FIRST appeared in that side's
    budget — all the churn filter needs, as an O(1) comparison per forecast.

    The budget history is scanned from the very first snapshot of the matchup,
    including the pre-week future-week projections, while forecasts are taken
    only from the week itself. That matters: budgets legitimately drop and
    re-add a player as his games get scheduled or he comes off the IL, and
    baselining on the week alone would score those as roster additions. Only a
    name never seen in this matchup's budgets before counts as churn.
    """
    forecasts: list[tuple] = []
    seen: set = set()
    newest: dict[tuple[str, str], str] = {}
    best: dict[int, tuple] = {}

    for s in conn.execute(
        """SELECT computed_at, details_json FROM wp_snapshots
           WHERE matchup_id=? AND details_json IS NOT NULL AND edited=0
             AND computed_at <= ?
           ORDER BY computed_at""", (matchup_id, deadline)):
        d = json.loads(s["details_json"])
        n = d.get("n_sims") or 0
        if not n:
            continue
        at = s["computed_at"]

        for side in ("home", "away"):
            for b in d.get(f"{side}_budgets") or []:
                key = (side, b["name"])
                if key in seen:
                    continue
                seen.add(key)
                role = b.get("role")
                grp = ("hit" if role == "HIT" else
                       "pitch" if role in ("SP", "RP") else None)
                if grp is None:
                    continue        # unknown role: attribute it to neither side
                if newest.get((side, grp), "") < at:
                    newest[(side, grp)] = at

        if at < fp:
            continue                # budget history only; not a live forecast

        cw = d.get("category_wp") or []
        for c in cw:
            for side in ("home", "away"):
                forecasts.append((at, side, c["stat_id"], c[f"{side}_wins"] / n))

        ts = datetime.fromisoformat(at)
        for i, tgt in enumerate(targets):
            dist = abs((ts - tgt).total_seconds())
            if dist <= CHECKPOINT_TOL_HOURS * 3600 and (
                    i not in best or dist < best[i][0]):
                best[i] = (dist, at, cw, n)
    return forecasts, newest, best


def collect(conn: sqlite3.Connection, *, first_period: int = FIRST_PERIOD,
            last_period: int | None = None) -> Result:
    """Stream every in-week snapshot of every settled matchup into `Result`.

    Streaming rather than materialising: the observation table is ~1.9M rows
    over periods 10-18 and every consumer here is an accumulation.
    """
    res = Result()
    sql = ("""SELECT DISTINCT matchup_period_id p FROM matchups
              WHERE winner IN ('HOME','AWAY') AND matchup_period_id >= ?""" +
           ("" if last_period is None else " AND matchup_period_id <= ?") +
           " ORDER BY p")
    args = (first_period,) if last_period is None else (first_period, last_period)
    res.periods = [r["p"] for r in conn.execute(sql, args)]

    for period in res.periods:
        fp = first_pitch(conn, period)
        _, end = matchup_period_window(period)
        deadline = f"{end.isoformat()}T23:59:59+00:00"
        targets = _targets(period)

        for m in conn.execute(
            """SELECT id, home_team_id, away_team_id FROM matchups
               WHERE matchup_period_id=? AND winner IN ('HOME','AWAY')
               ORDER BY id""", (period,)):
            final = outcome_by_stat(conn, m)
            if not final:
                continue
            scores = final_scores(conn, m)
            forecasts, newest, best = _scan_matchup(
                conn, m["id"], fp, deadline, targets)

            def clean_side(side: str, stat: int, at: str) -> bool:
                grp = (side, "hit" if stat in BATTING else "pitch")
                return newest.get(grp, "") <= at

            for at, side, sid, p in forecasts:
                if sid not in final:
                    continue
                won = 1 if final[sid] == side.upper() else 0
                cf = clean_side(side, sid, at)
                res.n_forecasts += 1
                res.n_churn_free += int(cf)
                idx = bin_index(p)
                scopes = (ALL, CLEAN) if cf else (ALL,)
                for stat_key in (sid, None):
                    for scope in scopes:
                        b = res.bins[(stat_key, scope, idx)]
                        b[0] += 1
                        b[1] += p
                        b[2] += won
                        c = res.clusters[(stat_key, scope, idx)][m["id"]]
                        c[0] += p
                        c[1] += won
                        if p < TAIL_HI:
                            t = res.clusters[(stat_key, scope, "tail")][m["id"]]
                            t[0] += p
                            t[1] += won
                key = (m["id"], side, sid)
                u = res.units.get(key)
                if u is None:
                    u = res.units[key] = Unit(won=won)
                u.min_p = min(u.min_p, p)
                if p < TAIL_HI:
                    u.tail_snapshots += 1
                    u.last_tail_at = at
                    u.churn_free = cf

            for i, (_, at, cw, n) in best.items():
                for c in cw:
                    sid = c["stat_id"]
                    if sid not in scores:
                        continue
                    sgn = -1 if appstats.is_reversed(sid) else 1
                    ah, aa = scores[sid]
                    ph, pa = c["home_avg"], c["away_avg"]
                    res.checkpoints.append({
                        "offset": CHECKPOINTS[i], "matchup_id": m["id"], "stat": sid,
                        "proj_margin": sgn * (ph - pa),
                        "actual_margin": sgn * (ah - aa),
                        "p_home": c["home_wins"] / n,
                        # A margin needs BOTH sides clean, unlike a one-sided
                        # forecast: either team's late addition moves the gap.
                        "churn_free": (clean_side("home", sid, at)
                                       and clean_side("away", sid, at)),
                    })
                    if i == 0 and sid not in appstats.RATE_STATS:
                        res.totals[sid][0] += ph + pa
                        res.totals[sid][1] += 2
                        res.gaps[sid][0] += abs(ph - pa)
                        res.gaps[sid][1] += 1
    return res


# ── statistics ──────────────────────────────────────────────────────────────

def cluster_ratio(clusters: dict) -> tuple[float, float, float] | None:
    """(observed/predicted, total predicted, total observed) over cluster sums."""
    sp = sum(v[0] for v in clusters.values())
    sw = sum(v[1] for v in clusters.values())
    if sp <= 0:
        return None
    return sw / sp, sp, sw


def cluster_ratio_ci(clusters: dict, *, reps: int, seed: int = 12345,
                     level: float = 0.90) -> tuple[float, float] | None:
    """Bootstrap CI on observed/predicted, resampling MATCHUPS.

    Clustered by matchup, not by forecast: the ~1700 snapshots of one category
    are one story told repeatedly, and treating them as independent would give
    an interval far too narrow to be honest.
    """
    keys = list(clusters)
    if len(keys) < 3:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        sp = sw = 0.0
        for _ in keys:
            v = clusters[keys[rng.randrange(len(keys))]]
            sp += v[0]
            sw += v[1]
        if sp > 0:
            vals.append(sw / sp)
    if len(vals) < reps // 2:
        return None
    vals.sort()
    return (vals[int((1 - level) / 2 * len(vals))],
            vals[int((1 + level) / 2 * len(vals)) - 1])


def slope_through_origin(pairs: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of actual on projected, forced through (0, 0).

    Through the origin because a category margin has a meaningful zero: a
    projected dead heat should settle as a dead heat on average, and a free
    intercept would let a level offset masquerade as good calibration.
    """
    den = sum(p * p for p, _ in pairs)
    if den <= 0:
        return None
    return sum(p * a for p, a in pairs) / den


def slope_ci(pairs: list[tuple[float, float]], *, reps: int, seed: int = 12345,
             level: float = 0.90) -> tuple[float, float] | None:
    if len(pairs) < 10:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        draw = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        s = slope_through_origin(draw)
        if s is not None:
            vals.append(s)
    if len(vals) < reps // 2:
        return None
    vals.sort()
    return (vals[int((1 - level) / 2 * len(vals))],
            vals[int((1 + level) / 2 * len(vals)) - 1])


def robust_z_scale(zs: list[float]) -> float | None:
    """Robust sd of standardized residuals: median(|z|) / 0.6745.

    1.0 means the sim's own spread matches the errors it makes; above 1 means
    the simulated week is too narrow. Robust rather than a plain sd because a
    single confident forecast on a tiny projected margin yields a small sigma
    and an enormous z, which a plain sd would let dominate the answer.
    """
    if len(zs) < 8:
        return None
    m = statistics.median(abs(z) for z in zs)
    return m / 0.6745 if m > 0 else None


def implied_sigma(p: float, proj_margin: float) -> float | None:
    """The sigma the sim implicitly used: p = P(margin > 0) ~= Phi(margin/sigma).

    Returns None on the rails and on a zero margin, where the probit is unusable.
    """
    if not (0.0 < p < 1.0) or proj_margin == 0:
        return None
    z = _NORM.inv_cdf(p)
    if abs(z) < 1e-6:
        return None
    sigma = proj_margin / z
    return sigma if sigma > 0 else None
