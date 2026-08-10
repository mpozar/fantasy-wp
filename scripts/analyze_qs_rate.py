#!/usr/bin/env python
"""Offline tool: re-measure the QS-rate shrinkage constant (QS_RATE_PRIOR_STARTS).

Prints the value to paste into `app/espn.py`, same convention as
`analyze_variance.py` (VMR) and `analyze_cadence.py` (REST_DAY_WEIGHTS).

Method — empirical Bayes, Beta-Binomial method of moments over the league's
rostered starters. Each pitcher's observed QS rate p_i = qs_i/gs_i carries
binomial noise p_i(1-p_i)/n_i on top of real between-pitcher spread:

    var(p_obs) = var_between + E[p(1-p)/n]
    K          = mean_p·(1-mean_p) / var_between − 1

K is the prior weight in pseudo-starts: a pitcher with n actual starts lands
n/(n+K) of the way from ESPN's projected rate to his own realized rate. Larger
between-pitcher spread ⇒ smaller K ⇒ trust the individual sooner.

Also reports ESPN's aggregate level bias (the reason the blend exists) and what
the blend would do to it at several K, so a change is judged on the number that
matters rather than on the estimator alone.

Read-only; hits the ESPN API (needs ESPN_SWID/ESPN_S2 in ~/.zshenv).

    .venv/bin/python scripts/analyze_qs_rate.py
"""
from __future__ import annotations

import argparse
import statistics

from app import espn

SEASON = espn.SEASON_ID
ROS_GS, ROS_QS = "33", "63"


def _stat_block(player: dict, src: int, split: int) -> dict | None:
    return next((s for s in player.get("stats", [])
                 if s.get("statSourceId") == src
                 and s.get("statSplitTypeId") == split
                 and s.get("seasonId") == SEASON), None)


def collect() -> list[dict]:
    """Rostered pitchers with both projected ROS starts and actual starts."""
    d = espn._get(["mRoster"], {"scoringPeriodId": 0})
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
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-starts", type=int, default=1,
                    help="drop pitchers below this many actual starts")
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
    proj_rate = sum((r["ros_qs"] / r["ros_gs"]) * r["ros_gs"] for r in rows) / w

    mean_p = statistics.fmean(p_obs)
    var_obs = statistics.variance(p_obs)
    noise = statistics.fmean(pi * (1 - pi) / ni for pi, ni in zip(p_obs, n))
    var_between = max(var_obs - noise, 1e-6)
    K = mean_p * (1 - mean_p) / var_between - 1

    print(f"QS rate — {len(rows)} rostered starters, {sum(n):.0f} actual starts\n")
    print(f"  league actual QS rate      {act_rate:.3f}")
    print(f"  ESPN ROS implied rate      {proj_rate:.3f}  "
          f"({proj_rate / act_rate - 1:+.1%} vs actual)   <- the level bias\n")
    print(f"  mean observed rate         {mean_p:.3f}")
    print(f"  var(observed)              {var_obs:.5f}")
    print(f"  binomial noise             {noise:.5f}")
    print(f"  var(between pitchers)      {var_between:.5f}\n")
    print(f"  QS_RATE_PRIOR_STARTS = {K:.1f}   # paste into app/espn.py\n")

    med = statistics.median(n)
    print(f"  effect on the league aggregate (median {med:.0f} actual starts):")
    for k in sorted({5.0, 8.0, round(K, 1), 10.0, 12.0, 15.0}):
        blended = [((r["act_qs"] + k * (r["ros_qs"] / r["ros_gs"]))
                    / (r["act_gs"] + k)) for r in rows]
        agg = sum(b * r["ros_gs"] for b, r in zip(blended, rows)) / w
        mark = "  <- estimated" if abs(k - round(K, 1)) < 1e-9 else ""
        print(f"    K={k:>5}: rate {agg:.3f}  ({agg / act_rate - 1:+.1%} vs actual)"
              f"  weight on actuals {med / (med + k):.0%}{mark}")

    print("\n  Residual bias is the prior's, not the estimator's: we shrink toward")
    print("  ESPN's rate, so low-sample pitchers stay near its inflated level.")
    print("  Recentering ESPN's rates on the league actual would remove more, at")
    print("  the cost of coupling every projection to roster composition.")


if __name__ == "__main__":
    main()
