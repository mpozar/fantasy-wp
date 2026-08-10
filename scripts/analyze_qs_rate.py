#!/usr/bin/env python
"""Offline tool: re-measure the QS-rate shrinkage constant (QS_RATE_PRIOR_STARTS).

Prints the value to paste into `app/espn.py`, same convention as
`analyze_svhd_rate.py` (K), `analyze_variance.py` (VMR) and `analyze_cadence.py`
(REST_DAY_WEIGHTS).

Method — empirical Bayes, method of moments over the league's rostered STARTERS.
A quality start is one Bernoulli trial per start, so a pitcher's observed rate
p_i = qs_i/gs_i carries binomial noise p_i(1-p_i)/n_i.

K is the prior weight in pseudo-starts: a pitcher with n starts lands n/(n+K) of
the way from ESPN's projected rate to his own realized rate. Minimising the
blended rate's squared error over the weight gives

    K = mean_p·(1-mean_p) / E[(prior − p_true)²]                 <- headline
    E[(prior − p_true)²] ≈ mean((prior − p_obs)²) − E[p(1-p)/n]

i.e. K is calibrated against HOW WRONG THE PRIOR IS, per player. The
between-player-spread estimator this script used until 2026-08-10,

    K_spread = mean_p·(1-mean_p) / var_between − 1,  var_between = var(p_obs) − noise

is only correct when the prior IS the population mean. ESPN's is not: it is a
per-player forecast that can be individually wrong, and being anchored to
preseason talent it is wrong in a way spread cannot see. Both are printed, since
a season with a better prior should see them converge (`analyze_svhd_rate.py`
prints the same pair, and for SVHD they read ~14 vs ~8).

Two outputs beyond K, because K alone doesn't say whether a change is an
improvement:
  * ESPN's aggregate level bias, and what the blend does to it at several K.
    The residual is the PRIOR's bias, not the estimator's — we shrink toward
    ESPN, so a low-sample starter stays near its inflated level.
  * A squared-error back-test of the blend against the PRIOR-ONLY behavior the
    blend replaced (ESPN's ROS QS used as-is), treating each established
    starter's realized rate as truth. This is the number that validates a K
    directly rather than estimating it; re-run it before retuning.

Read-only; hits the ESPN API (needs ESPN_SWID/ESPN_S2 in ~/.zshenv).

    .venv/bin/python scripts/analyze_qs_rate.py
"""
from __future__ import annotations

import argparse
import statistics

from app import espn

SEASON = espn.SEASON_ID
ROS_GS, ROS_QS = "33", "63"

# A 1- or 2-start sample tells us nothing about the prior's error but does bias K
# downward: p_obs is 0 or 1, so the binomial-noise correction p_obs(1-p_obs)/n it
# contributes is exactly 0 while its squared deviation from the prior is maximal —
# E[(prior − true)²] absorbs pure noise and K shrinks. Drop that tail by default,
# as `analyze_svhd_rate.py` does. `--min-starts 1` reproduces the old sample.
DEFAULT_MIN_STARTS = 3


def _stat_block(player: dict, src: int, split: int) -> dict | None:
    return next((s for s in player.get("stats", [])
                 if s.get("statSourceId") == src
                 and s.get("statSplitTypeId") == split
                 and s.get("seasonId") == SEASON), None)


def collect() -> list[dict]:
    """Rostered pitchers with both projected ROS starts and actual starts.

    Fetched exactly as `fetch_rosters_and_projections` does — plain
    `_get(["mRoster"])`, NOT with `{"scoringPeriodId": 0}`, which this script
    passed until 2026-08-10. That parameter does not filter stats; it returns a
    **different, stale roster snapshot**: 108 of its 279 players are not on any
    current roster, and it misses 41 of the 90 currently-rostered usable
    starters (deGrom, Wheeler, Cole, Snell, …). Verified 2026-08-10 — for a
    player present in BOTH responses the split=6 ROS block is identical, so the
    defect was never a missing stat block; the plain fetch matches the league's
    stored `team_rosters` 283/283, `scoringPeriodId=0` only 206/279. Measuring K
    on that snapshot estimated it over a population the constant is not applied
    to.
    """
    d = espn._get(["mRoster"])
    out: list[dict] = []
    seen: set[int] = set()
    for t in d.get("teams", []):
        for e in t.get("roster", {}).get("entries", []):
            p = (e.get("playerPoolEntry") or {}).get("player") or {}
            pid = p.get("id")
            if pid is None or pid in seen:
                continue
            ros = _stat_block(p, 1, 6)
            act = _stat_block(p, 0, 0)
            if not ros:
                continue
            rs, a = (ros.get("stats") or {}), ((act or {}).get("stats") or {})
            rgs = espn._as_float(rs.get(ROS_GS)) or 0.0
            ags = espn._as_float(a.get(ROS_GS))
            aqs = espn._as_float(a.get(ROS_QS))
            if rgs <= 0 or not ags or ags <= 0 or aqs is None:
                continue
            seen.add(pid)
            out.append({
                "name": p.get("fullName") or str(pid),
                "ros_gs": rgs,
                "ros_qs": espn._as_float(rs.get(ROS_QS)) or 0.0,
                "act_gs": ags, "act_qs": aqs,
                "prior": min(max((espn._as_float(rs.get(ROS_QS)) or 0.0) / rgs,
                                 0.0), 1.0),
            })
    return out


def _blend(row: dict, k: float) -> float:
    return (row["act_qs"] + k * row["prior"]) / (row["act_gs"] + k)


def _expected_sq_error(rows: list[dict], n: float, k: float | None) -> float:
    """Expected squared error of the estimated rate after n starts, averaged over
    the starter pool, treating each one's realized rate as truth.

    `k=None` scores the pre-blend behavior: ESPN's ROS rate used as-is at every
    sample size (equivalently K=∞). Analytic, so it needs no per-start sequence
    (which ESPN does not expose): with E[p̂]=p the bias term is the shrunk
    distance to the prior and the variance term is (1−w)²·p(1−p)/n."""
    def one(r: dict) -> float:
        p = r["act_qs"] / r["act_gs"]
        var = p * (1 - p) / n
        if k is None:
            return (r["prior"] - p) ** 2
        w = k / (n + k)
        return (w * (r["prior"] - p)) ** 2 + (1 - w) ** 2 * var
    return statistics.fmean(one(r) for r in rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-starts", type=int, default=DEFAULT_MIN_STARTS,
                    help="drop pitchers below this many actual starts")
    ap.add_argument("--truth-starts", type=int, default=10,
                    help="back-test only: starts needed to treat a pitcher's "
                         "realized rate as truth")
    args = ap.parse_args()

    rows = [r for r in collect() if r["act_gs"] >= args.min_starts]
    if len(rows) < 10:
        print(f"Only {len(rows)} usable starters — too few to estimate.")
        return

    n = [r["act_gs"] for r in rows]
    p_obs = [r["act_qs"] / r["act_gs"] for r in rows]

    # Start-weighted aggregates: ESPN's level bias.
    act_rate = sum(r["act_qs"] for r in rows) / sum(n)
    w = sum(r["ros_gs"] for r in rows)
    proj_rate = sum(r["prior"] * r["ros_gs"] for r in rows) / w

    mean_p = statistics.fmean(p_obs)
    var_obs = statistics.variance(p_obs)
    noise = statistics.fmean(pi * (1 - pi) / ni for pi, ni in zip(p_obs, n))
    var_between = max(var_obs - noise, 1e-6)
    K_spread = mean_p * (1 - mean_p) / var_between - 1
    prior_err = max(statistics.fmean((r["prior"] - pi) ** 2
                                     for r, pi in zip(rows, p_obs)) - noise, 1e-6)
    K = mean_p * (1 - mean_p) / prior_err

    print(f"QS rate — {len(rows)} rostered starters, {sum(n):.0f} actual starts "
          f"(>={args.min_starts} each)\n")
    print(f"  league actual QS rate      {act_rate:.3f}")
    print(f"  ESPN ROS implied rate      {proj_rate:.3f}  "
          f"({proj_rate / act_rate - 1:+.1%} vs actual)   <- the level bias\n")
    print(f"  mean observed rate         {mean_p:.3f}")
    print(f"  var(observed)              {var_obs:.5f}")
    print(f"  binomial noise             {noise:.5f}")
    print(f"  var(between pitchers)      {var_between:.5f}   -> spread K "
          f"{K_spread:.1f}  (the estimator used until 2026-08-10)")
    print(f"  E[(prior − true)²]         {prior_err:.5f}   <- the prior is "
          f"individually wrong, not just off-level")
    print(f"\n  QS_RATE_PRIOR_STARTS = {K:.1f}   # estimate; confirm against the "
          f"back-test below before pasting\n")

    med = statistics.median(n)
    live = espn.QS_RATE_PRIOR_STARTS          # what app/espn.py currently ships
    ks = sorted({4.0, 6.0, live, round(K, 1), 12.0})
    print(f"  effect on the league aggregate (median {med:.0f} actual starts):")
    for k in ks:
        agg = sum(_blend(r, k) * r["ros_gs"] for r in rows) / w
        mark = ("  <- estimated" if abs(k - round(K, 1)) < 1e-9
                else "  <- in app/espn.py" if abs(k - live) < 1e-9 else "")
        print(f"    K={k:>5}: rate {agg:.3f}  ({agg / act_rate - 1:+.1%} vs actual)"
              f"  weight on actuals {med / (med + k):.0%}{mark}")

    print("\n  Residual bias is the prior's, not the estimator's: we shrink toward")
    print("  ESPN's rate, so low-sample pitchers stay near its inflated level.")
    print("  Recentering ESPN's rates on the league actual would remove more, at")
    print("  the cost of coupling every projection to roster composition.")

    truth = [r for r in rows if r["act_gs"] >= args.truth_starts]
    if len(truth) < 10:
        return
    print(f"\n  back-test vs using ESPN's ROS rate as-is ({len(truth)} starters "
          f"with >={args.truth_starts} starts")
    print("  as truth) — expected squared error of the estimated rate:\n")
    print(f"    {'n':>4}{'prior':>10}" + "".join(f"{'K=' + str(k):>10}" for k in ks))
    for i in (1, 3, 5, 8, 10, 15, 20, 30, 45):
        print(f"    {i:>4}{_expected_sq_error(truth, i, None):10.5f}"
              + "".join(f"{_expected_sq_error(truth, i, k):10.5f}" for k in ks))
    for lo, hi, label in ((1, 34, "a full starter season"), (1, 12, "early season")):
        rng = range(lo, hi + 1)
        prior_only = statistics.fmean(_expected_sq_error(truth, i, None) for i in rng)
        best = min(((k, statistics.fmean(_expected_sq_error(truth, i, k) for i in rng))
                    for k in range(2, 41)), key=lambda t: t[1])
        print(f"\n    mean over n={lo}..{hi} ({label}): prior-only {prior_only:.5f}, "
              f"best K={best[0]} at {best[1]:.5f} ({best[1] / prior_only - 1:+.0%})")


if __name__ == "__main__":
    main()
