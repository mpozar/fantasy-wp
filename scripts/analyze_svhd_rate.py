#!/usr/bin/env python
"""Offline tool: re-measure the SVHD-rate shrinkage constant
(SVHD_RATE_PRIOR_APPEARANCES).

Prints the value to paste into `app/espn.py`, same convention as
`analyze_qs_rate.py` (K), `analyze_variance.py` (VMR) and `analyze_cadence.py`
(REST_DAY_WEIGHTS).

Method — empirical Bayes, method of moments over the league's rostered
RELIEVERS. A save and a hold cannot both be earned in one outing, so each
appearance is a Bernoulli SVHD trial and a reliever's observed rate
p_i = svhd_i/gp_i carries binomial noise p_i(1-p_i)/n_i.

K is the prior weight in pseudo-appearances: a reliever with n appearances
lands n/(n+K) of the way from ESPN's projected rate to his own realized rate.
Minimising the blended rate's squared error over the weight gives

    K = mean_p·(1-mean_p) / E[(prior − p_true)²]                 <- headline
    E[(prior − p_true)²] ≈ mean((prior − p_obs)²) − E[p(1-p)/n]

i.e. K is calibrated against HOW WRONG THE PRIOR IS, per player. Note this is
NOT the between-player-spread estimator

    K = mean_p·(1-mean_p) / var_between − 1,  var_between = var(p_obs) − noise

which `analyze_qs_rate.py` used as its headline until 2026-08-10 (it now prints
this pair the same way). Those agree only when the prior IS the population mean.
ESPN's is not: it is a per-player forecast that can be individually wrong, and
for SVHD it is STRUCTURALLY wrong for the mid-season role-changers it projected
zero saves/holds for (2026-08-10: 5 of 47 rostered relievers, priors of .000
against realized rates up to .571). Spread-based MoM cannot see that and returns
~14; the prior-error version returns ~8, which is what the back-test below
confirms. Both are printed, since a future season with a better prior should see
them converge.

The prior here is ESPN's FULL-SEASON projection rate (split=0, src=1), not its
ROS rate: ESPN's ROS encoding of stat 83 is broken (it returns total GP for
some players), which is why the ROS value is rebuilt from a rate at all.

Starters are excluded — by ACTUAL role (gs/gp > 0.5), since ESPN's ROS GS is
what misclassifies swingmen in the first place. Their true SVHD rate is ~0 with
almost no spread, so pooling them would make the between-player variance
meaningless and collapse K.

Two outputs beyond K, because K alone doesn't say whether a change is an
improvement:
  * ESPN's aggregate level bias, and what the blend does to it at several K.
    The residual is the PRIOR's bias, not the estimator's — we shrink toward
    ESPN, so low-sample relievers stay near its inflated level.
  * A squared-error back-test of the blend against the 15-appearance CLIFF it
    replaced (`MIN_ACT_GP_FOR_SVHD_RATE`, removed 2026-08-10), treating each
    established reliever's realized rate as truth. This is the number that
    justified the change; re-run it before retuning K.

Read-only; hits the ESPN API (needs ESPN_SWID/ESPN_S2 in ~/.zshenv).

    .venv/bin/python scripts/analyze_svhd_rate.py
"""
from __future__ import annotations

import argparse
import statistics

from app import espn

SEASON = espn.SEASON_ID
GP, GS, SVHD = "32", "33", "83"

# The cliff this shrinkage replaced, kept here (not imported — it no longer
# exists in app/) so the back-test can score the old behavior.
OLD_CLIFF_GP = 15

# A 1- or 2-appearance sample tells us nothing about between-reliever spread but
# does bias K downward: p_obs is 0 or 1, so the binomial-noise correction
# p_obs(1-p_obs)/n it contributes is exactly 0 while its squared deviation is
# maximal — var_between absorbs pure noise. Drop that tail by default.
DEFAULT_MIN_APPEARANCES = 3


def _stat_block(player: dict, src: int, split: int) -> dict | None:
    return next((s for s in player.get("stats", [])
                 if s.get("statSourceId") == src
                 and s.get("statSplitTypeId") == split
                 and s.get("seasonId") == SEASON), None)


def collect() -> list[dict]:
    """Rostered relievers with actual appearances and a full-season projection.

    Fetched exactly as `fetch_rosters_and_projections` does — plain
    `_get(["mRoster"])`, NOT with `scoringPeriodId=0`, which `analyze_qs_rate.py`
    passed until 2026-08-10 and which returns a **stale roster snapshot**: 108 of
    its 279 players are not on any current roster, so only 110 of them carry ROS
    GP against 133 under a plain fetch (the difference is roster composition —
    for a player in both responses the split=6 block is identical). Requiring the
    ROS block on that snapshot silently measures a biased subset of the
    population the constant is applied to. The ROS block is optional here
    anyway: it is only needed to weight the aggregate, not to estimate K.
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
            act = _stat_block(p, 0, 0)
            proj = _stat_block(p, 1, 0)
            if not act or not proj:
                continue
            rs = (_stat_block(p, 1, 6) or {}).get("stats") or {}
            a = act.get("stats") or {}
            f = proj.get("stats") or {}
            agp = espn._as_float(a.get(GP))
            pgp = espn._as_float(f.get(GP))
            if not agp or agp <= 0 or not pgp or pgp <= 0:
                continue
            if (espn._as_float(a.get(GS)) or 0.0) / agp > 0.5:
                continue                                   # actual-role starter
            seen.add(pid)
            out.append({
                "name": p.get("fullName") or str(pid),
                "act_gp": agp, "act_svhd": espn._as_float(a.get(SVHD)) or 0.0,
                "prior": min((espn._as_float(f.get(SVHD)) or 0.0) / pgp, 1.0),
                "ros_gp": espn._as_float(rs.get(GP)) or 0.0,
            })
    return out


def _blend(row: dict, k: float) -> float:
    return (row["act_svhd"] + k * row["prior"]) / (row["act_gp"] + k)


def _expected_sq_error(rows: list[dict], n: float, k: float | None) -> float:
    """Expected squared error of the estimated rate after n appearances,
    averaged over the reliever pool, treating each one's realized rate as truth.

    `k=None` scores the old cliff: the prior below OLD_CLIFF_GP appearances,
    pure actuals at or above it. Analytic, so it needs no per-appearance
    sequence (which ESPN does not expose): with E[p̂]=p the bias term is the
    shrunk distance to the prior and the variance term is (1−w)²·p(1−p)/n."""
    def one(r: dict) -> float:
        p = r["act_svhd"] / r["act_gp"]
        var = p * (1 - p) / n
        if k is None:
            return (r["prior"] - p) ** 2 if n < OLD_CLIFF_GP else var
        w = k / (n + k)
        return (w * (r["prior"] - p)) ** 2 + (1 - w) ** 2 * var
    return statistics.fmean(one(r) for r in rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-appearances", type=int, default=DEFAULT_MIN_APPEARANCES,
                    help="drop relievers below this many actual appearances")
    ap.add_argument("--truth-appearances", type=int, default=10,
                    help="back-test only: appearances needed to treat a "
                         "reliever's realized rate as truth")
    args = ap.parse_args()

    rows = [r for r in collect() if r["act_gp"] >= args.min_appearances]
    if len(rows) < 10:
        print(f"Only {len(rows)} usable relievers — too few to estimate.")
        return

    n = [r["act_gp"] for r in rows]
    p_obs = [r["act_svhd"] / r["act_gp"] for r in rows]

    # Appearance-weighted aggregates: ESPN's level bias.
    act_rate = sum(r["act_svhd"] for r in rows) / sum(n)
    w = sum(r["ros_gp"] for r in rows)
    prior_rate = sum(r["prior"] * r["ros_gp"] for r in rows) / w

    mean_p = statistics.fmean(p_obs)
    var_obs = statistics.variance(p_obs)
    noise = statistics.fmean(pi * (1 - pi) / ni for pi, ni in zip(p_obs, n))
    var_between = max(var_obs - noise, 1e-6)
    K_spread = mean_p * (1 - mean_p) / var_between - 1
    prior_err = max(statistics.fmean((r["prior"] - pi) ** 2
                                     for r, pi in zip(rows, p_obs)) - noise, 1e-6)
    K = mean_p * (1 - mean_p) / prior_err
    zero_prior = [r["name"] for r in rows if r["prior"] <= 0]

    print(f"SVHD rate — {len(rows)} rostered relievers, "
          f"{sum(n):.0f} actual appearances\n")
    print(f"  league actual SVHD rate    {act_rate:.3f}")
    print(f"  ESPN full-season prior     {prior_rate:.3f}  "
          f"({prior_rate / act_rate - 1:+.1%} vs actual)   <- the level bias\n")
    print(f"  mean observed rate         {mean_p:.3f}")
    print(f"  binomial noise             {noise:.5f}")
    print(f"  var(between relievers)     {var_between:.5f}   -> spread K "
          f"{K_spread:.1f}  (the between-player-spread estimator)")
    print(f"  E[(prior − true)²]         {prior_err:.5f}   <- the prior is "
          f"individually wrong, not just off-level")
    if zero_prior:
        print(f"    incl. {len(zero_prior)} reliever(s) ESPN projected ZERO "
              f"SVHD for: {', '.join(zero_prior)}")
    print(f"\n  SVHD_RATE_PRIOR_APPEARANCES = {K:.1f}   # paste into app/espn.py\n")

    med = statistics.median(n)
    ks = sorted({6.0, round(K, 1), 10.0, 15.0, 20.0})
    print(f"  effect on the league aggregate (median {med:.0f} actual appearances):")
    for k in ks:
        agg = sum(_blend(r, k) * r["ros_gp"] for r in rows) / w
        mark = "  <- estimated" if abs(k - round(K, 1)) < 1e-9 else ""
        print(f"    K={k:>5}: rate {agg:.3f}  ({agg / act_rate - 1:+.1%} vs actual)"
              f"  weight on actuals {med / (med + k):.0%}{mark}")

    print("\n  Residual bias is the prior's, not the estimator's: we shrink toward")
    print("  ESPN's rate, so a low-sample reliever stays near it. Recentering")
    print("  ESPN's rates on the league actual would remove the level half of")
    print("  that, at the cost of coupling every projection to roster")
    print("  composition — but note the level bias is the SMALL half here: the")
    print("  prior's per-player error dwarfs it (compare the two variances).")

    truth = [r for r in rows if r["act_gp"] >= args.truth_appearances]
    if len(truth) < 10:
        return
    print(f"\n  back-test vs the old {OLD_CLIFF_GP}-appearance cliff "
          f"({len(truth)} relievers with >={args.truth_appearances} appearances")
    print("  as truth) — expected squared error of the estimated rate:\n")
    print(f"    {'n':>4}{'cliff':>10}" + "".join(f"{'K=' + str(k):>10}" for k in ks))
    for i in (1, 3, 5, 10, 14, 15, 20, 30, 45, 60):
        print(f"    {i:>4}{_expected_sq_error(truth, i, None):10.5f}"
              + "".join(f"{_expected_sq_error(truth, i, k):10.5f}" for k in ks))
    for lo, hi, label in ((1, 60, "all season"), (1, 25, "early season")):
        rng = range(lo, hi + 1)
        cliff = statistics.fmean(_expected_sq_error(truth, i, None) for i in rng)
        best = min(((k, statistics.fmean(_expected_sq_error(truth, i, k) for i in rng))
                    for k in range(2, 41)), key=lambda t: t[1])
        print(f"\n    mean over n={lo}..{hi} ({label}): cliff {cliff:.5f}, "
              f"best K={best[0]} at {best[1]:.5f} ({best[1] / cliff - 1:+.0%})")


if __name__ == "__main__":
    main()
