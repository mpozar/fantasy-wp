"""Thin ESPN fantasy baseball API client for one league."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app import LEAGUE_ID, SEASON_ID

BASE_URL = (
    f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{SEASON_ID}"
    f"/segments/0/leagues/{LEAGUE_ID}"
)

# Prior weight, in pseudo-appearances, for the SVHD-rate blend below.
# Empirical Bayes over the league's rostered relievers, calibrated against how
# wrong the PRIOR is per player rather than against between-reliever spread:
# K = p(1−p) / E[(prior − true)²] ⇒ 7.6, and 6 minimises measured squared error
# directly. 8.0 rounds just above both, deliberately: a reliever's realized rate
# is itself selection-inflated (he is rostered BECAUSE he started earning
# saves), so both estimates overstate how wrong the prior really is, and the
# safe side of that is MORE shrinkage. The error curve is flat K=4..10, so the
# exact value is not load-bearing. Re-measure with
# `scripts/analyze_svhd_rate.py`, which explains why the between-player-spread
# estimator reads ~14 here and should not be used for this one. (The QS script
# made exactly that mistake until 2026-08-10 — see `QS_RATE_PRIOR_STARTS`.)
#
# Replaced a hard 15-appearance cliff (`MIN_ACT_GP_FOR_SVHD_RATE`) on
# 2026-08-10: below 15 GP the rate was 100% ESPN's projection, at 15 GP it
# flipped discontinuously to 100% actuals — a reliever's rate could move 0.3 on
# his 15th outing. Measured against this season's rostered relievers the
# shrinkage cuts squared error 38% over n=1..60 and 46% over n=1..25, the gain
# concentrated below the cliff where it starts using the pitcher's own
# appearances immediately instead of ignoring them.
SVHD_RATE_PRIOR_APPEARANCES = 8.0

# Prior weight, in pseudo-starts, for the QS-rate blend below. Same
# empirical-Bayes shape and calibration as SVHD's constant above: K is measured
# against how wrong the PRIOR is per pitcher, K = p(1−p) / E[(prior − true)²]
# ⇒ 8.6, and a direct squared-error back-test puts the optimum at 7.0 —
# invariant across uniform n, today's per-pitcher start counts, and ROS-start
# weighting. 7.0 takes the back-test: it measures the objective rather than
# approximating it, and selection makes both numbers upper bounds (a rostered
# starter's realized rate is luck-inflated *above* his true talent, while
# ESPN's prior sits above the realized rate, so the true prior error is larger
# than measured and the true K smaller). The error curve is flat K=6..10 —
# within 2.4% of optimum — so the exact value is not load-bearing.
#
# Was 9.0 until 2026-08-10. That value was the *between-pitcher-spread*
# estimator, K = p(1−p)/var_between − 1, run over a stale roster snapshot
# (`analyze_qs_rate.py` fetched with `scoringPeriodId=0`, which returns rosters
# 108 players adrift of the current ones). Both defects were real and they
# nearly cancelled: on the correct sample the spread estimator reads 18.3, and
# switching to the prior-error estimator brings it back to 8.6. Re-measure with
# `scripts/analyze_qs_rate.py`, which prints both estimators and the back-test.
QS_RATE_PRIOR_STARTS = 7.0


def blend_qs_rate(ros_gs: float | None, ros_qs: float | None,
                  act_gs: float | None, act_qs: float | None) -> float | None:
    """Per-start QS rate, shrinking ESPN's ROS projection toward the pitcher's
    SEASON-TO-DATE ACTUAL rate.

        rate = (act_qs + K·prior) / (act_gs + K),   prior = ros_qs / ros_gs

    Why: ESPN's ROS projections are anchored to preseason "true talent" and do
    not track current performance, and for QS specifically they run **~+28%
    above realized rates** (re-measured 2026-08-10 over the league's 89 rostered
    starters, 1714 starts: ESPN-implied .596 vs actual .467) — the level bias
    behind the +40.5% start-of-week QS over-projection in
    `scripts/calibration.py`. ESPN's per-player *ranking* still carries signal,
    so we keep its rate as the prior and let each pitcher's own starts pull it
    toward the truth. (An earlier reading of ".598 vs .438, +37%" came from the
    stale-roster sample described on `QS_RATE_PRIOR_STARTS`; that sample
    overstated the level bias as well as mis-estimating K.)

    `K = QS_RATE_PRIOR_STARTS` is the empirical-Bayes shrinkage weight, so the
    blend is sample-size aware by construction: a 21-start pitcher lands ~75%
    on his actuals, a 2-start callup stays essentially at ESPN's number. The
    SVHD path (`blend_svhd_rate`) uses the same shape and the same calibration;
    it previously had a hard 15-GP cliff, which flipped discontinuously.

    Returns None to mean "leave ESPN's projection alone": no projected starts
    (a pure reliever — his spot-start QS is handled by the sim's promoted-starter
    path), or no usable actuals to blend with. Clamped to [0, 1] since QS is
    capped at one per start."""
    if not ros_gs or ros_gs <= 0:
        return None
    if act_gs is None or act_gs <= 0 or act_qs is None:
        return None
    prior = min(max((ros_qs or 0) / ros_gs, 0.0), 1.0)
    rate = ((act_qs + QS_RATE_PRIOR_STARTS * prior)
            / (act_gs + QS_RATE_PRIOR_STARTS))
    return min(max(rate, 0.0), 1.0)


def apply_qs_rate_blend(ros_stats: dict, act_stats: dict | None) -> None:
    """Rewrite `ros_stats["63"]` (ROS QS) in place to the blended per-start rate
    × projected ROS starts. No-op when `blend_qs_rate` declines.

    Writing back as a *total* (rate × ROS GS) keeps the stored shape identical
    to every other ROS counter, so `sim` needs no change: it recovers the rate
    as `ros_qs / gs_ros` (`_make_budget`'s per-start denominator, and
    `_override_sp_qs`'s `qs_rate`), which is exactly the blended value."""
    a = act_stats or {}
    rate = blend_qs_rate(
        _as_float(ros_stats.get("33")), _as_float(ros_stats.get("63")),
        _as_float(a.get("33")), _as_float(a.get("63")),
    )
    if rate is None:
        return
    ros_gs = _as_float(ros_stats.get("33")) or 0.0
    ros_stats["63"] = rate * ros_gs


def blend_svhd_rate(proj_gp: float | None, proj_svhd: float | None,
                    act_gp: float | None, act_svhd: float | None) -> float | None:
    """Per-appearance SV+HLD rate, shrinking ESPN's FULL-SEASON projected rate
    toward the reliever's season-to-date actual rate.

        rate = (act_svhd + K·prior) / (act_gp + K),   prior = proj_svhd / proj_gp

    Same empirical-Bayes shape as `blend_qs_rate`, with one structural
    difference: QS shrinks toward ESPN's *ROS* rate, but ESPN's ROS encoding of
    stat 83 is genuinely broken (it returns total GP for some players — see
    `fetch_rosters_and_projections`), so it cannot be the prior. The
    **full-season projection** (split=0, src=1) is well-formed and is what the
    old cliff already fell back to, so it is the natural prior. It is also a
    weaker prior than QS's: being preseason, it misses mid-season role changes
    entirely (2026-08-10: 5 of 47 rostered relievers had a .000 projected rate
    against realized rates up to .571), which is why K is measured against the
    prior's per-player error and lands lower than the QS constant.

    `K = SVHD_RATE_PRIOR_APPEARANCES`: a 45-appearance closer lands ~85% on his
    own rate, a 3-appearance callup stays essentially at ESPN's number, and
    nothing happens discontinuously in between.

    **A missing `act_svhd` alongside real appearances means ZERO, not unknown**
    — ESPN omits stat 83 from the actuals block entirely when a pitcher has no
    saves or holds (83 of 133 rostered pitchers with appearances, 2026-08-10;
    an explicit 0 never appears). Reading it as "no data" would park every
    save-less arm on ESPN's prior and manufacture saves for pitchers who have
    demonstrably earned none — measured at +3.9 phantom ROS SVHD for one
    22-appearance middle reliever before this was caught. Note stat 63 (QS) is
    NOT encoded this way: it is always present for a pitcher with starts, which
    is why `blend_qs_rate` can treat a missing value as unknown.

    Degrades one source at a time: with no appearances at all it returns the
    prior alone (the old sub-cliff behavior), with no usable prior it returns
    the pitcher's own rate, and with neither it returns None to mean "leave
    ESPN's ROS value alone". Clamped to [0, 1] since a save and a hold cannot
    both be earned in one appearance; `sim.MAX_SVHD_RATE` (0.80) still caps the
    rate the sim recovers, but it is a backstop against the broken ROS encoding
    and no blended rate this season comes near it (league max ≈ .69)."""
    prior: float | None = None
    if proj_gp and proj_gp > 0:
        prior = min(max((proj_svhd or 0) / proj_gp, 0.0), 1.0)
    if act_gp is None or act_gp <= 0:
        return prior
    act_svhd = act_svhd or 0.0
    if prior is None:
        return min(max(act_svhd / act_gp, 0.0), 1.0)
    rate = ((act_svhd + SVHD_RATE_PRIOR_APPEARANCES * prior)
            / (act_gp + SVHD_RATE_PRIOR_APPEARANCES))
    return min(max(rate, 0.0), 1.0)


def apply_svhd_rate_blend(ros_stats: dict, act_stats: dict | None,
                          proj_stats: dict | None) -> None:
    """Rewrite `ros_stats["83"]` (ROS SVHD) in place to the blended
    per-appearance rate × projected ROS appearances. No-op when
    `blend_svhd_rate` declines or there are no projected ROS appearances — in
    both cases ESPN's (unreliable) ROS value is left as-is, as before.

    Writing back as a *total* (rate × ROS GP) keeps the stored shape identical
    to every other ROS counter, so `sim` needs no change: it recovers the rate
    as `ros_svhd / gp_ros` (`sim._make_budget`, `_override_rp_svhd`), which is
    exactly the blended value."""
    a = act_stats or {}
    f = proj_stats or {}
    rate = blend_svhd_rate(
        _as_float(f.get("32")), _as_float(f.get("83")),
        _as_float(a.get("32")), _as_float(a.get("83")),
    )
    if rate is None:
        return
    ros_gp = _as_float(ros_stats.get("32")) or 0.0
    if ros_gp <= 0:
        return
    ros_stats["83"] = rate * ros_gp


def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ESPNAuthError(RuntimeError):
    """Raised when ESPN responds with a redirect (cookies invalid/expired)."""


def _read_zshenv_var(name: str) -> str:
    """Read an `export NAME=...` value directly from ~/.zshenv.

    Per the user's global memory: never use the env var, read the file.
    """
    path = Path.home() / ".zshenv"
    pat = re.compile(rf'^\s*export\s+{re.escape(name)}=(.*?)\s*$', re.M)
    text = path.read_text()
    m = pat.search(text)
    if not m:
        raise RuntimeError(f"{name} not found in {path}")
    return m.group(1).strip().strip('"').strip("'")


def _cookies() -> dict[str, str]:
    return {
        "SWID": _read_zshenv_var("ESPN_SWID"),
        "espn_s2": _read_zshenv_var("ESPN_S2"),
    }


def _get(views: list[str], extra_params: dict | None = None) -> dict:
    params: list[tuple[str, str]] = [("view", v) for v in views]
    if extra_params:
        params.extend(extra_params.items())
    # follow_redirects=False so we can detect auth failures clearly
    with httpx.Client(cookies=_cookies(), follow_redirects=False, timeout=30.0) as client:
        r = client.get(BASE_URL, params=params)
    if r.status_code in (301, 302, 303, 307, 308):
        raise ESPNAuthError(
            f"ESPN redirected to {r.headers.get('location')} — "
            "ESPN_SWID/ESPN_S2 cookies are likely missing or expired."
        )
    r.raise_for_status()
    return r.json()


# -------- public surface --------

@dataclass(frozen=True)
class Category:
    stat_id: int
    reversed: bool


@dataclass(frozen=True)
class LeagueShape:
    name: str
    size: int
    scoring_type: str
    current_matchup_period: int
    last_regular_season_period: int
    tiebreaker_stat_id: int | None
    categories: list[Category]
    # ESPN slot-id → count (e.g. {0: 1, 1: 1, ..., 13: 5, 15: 3, 16: 6}).
    # Used by the hitter lineup optimizer.
    lineup_slot_counts: dict[int, int]


def fetch_league_shape() -> LeagueShape:
    """League settings + which matchup period is current."""
    d = _get(["mSettings"])
    s = d["settings"]
    ss = s["scoringSettings"]
    sched = s["scheduleSettings"]
    roster_settings = s.get("rosterSettings") or {}
    raw_slots = roster_settings.get("lineupSlotCounts") or {}
    # ESPN returns this as {"0": 1, "1": 1, ...} — coerce keys to int.
    lineup_slots = {int(k): int(v) for k, v in raw_slots.items()}
    cats = [
        Category(stat_id=item["statId"], reversed=item.get("isReverseItem", False))
        for item in ss["scoringItems"]
    ]
    tb = ss.get("matchupTieRuleBy")
    return LeagueShape(
        name=s["name"],
        size=s["size"],
        scoring_type=ss["scoringType"],
        current_matchup_period=d["status"]["currentMatchupPeriod"],
        last_regular_season_period=sched.get("matchupPeriodCount", 0),
        tiebreaker_stat_id=tb if tb else None,
        categories=cats,
        lineup_slot_counts=lineup_slots,
    )


def fetch_teams() -> list[dict]:
    d = _get(["mTeam"])
    out = []
    members_by_id = {m["id"]: m for m in d.get("members", [])}
    for t in d.get("teams", []):
        owner_id = (t.get("owners") or [None])[0]
        owner = members_by_id.get(owner_id, {})
        owner_name = (
            f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
            or owner.get("displayName")
        )
        out.append({
            "id": t["id"],
            "name": t.get("name") or f"Team {t['id']}",
            "abbrev": t.get("abbrev"),
            "owner": owner_name,
        })
    return out


def fetch_rosters_and_projections() -> dict:
    """Pull every fantasy team's roster + each rostered player's ROS projection.

    Returns a dict with:
      - matchup_period_id (int)
      - season_id (int)
      - players: [{id, full_name, pro_team_id, default_position_id,
                   eligible_slots, injury_status}]
      - roster_entries: [{fantasy_team_id, player_id, lineup_slot_id, status}]
      - projections: [{player_id, stat_id, value, split_id, season_id}]
        Only ROS (statSourceId=1, statSplitTypeId=6) is included.
    """
    d = _get(["mRoster"])
    period_id = d["status"]["currentMatchupPeriod"]
    season_id = d.get("seasonId", SEASON_ID)

    players: list[dict] = []
    roster_entries: list[dict] = []
    projections: list[dict] = []
    seen_player_ids: set[int] = set()

    for t in d.get("teams", []):
        team_id = t["id"]
        for entry in t.get("roster", {}).get("entries", []):
            ppe = entry.get("playerPoolEntry") or {}
            p = ppe.get("player") or {}
            pid = p.get("id")
            if pid is None:
                continue

            roster_entries.append({
                "fantasy_team_id": team_id,
                "player_id": pid,
                "lineup_slot_id": entry.get("lineupSlotId"),
                "status": entry.get("status"),
            })

            if pid in seen_player_ids:
                continue
            seen_player_ids.add(pid)

            players.append({
                "id": pid,
                "full_name": p.get("fullName") or "",
                "pro_team_id": p.get("proTeamId"),
                "default_position_id": p.get("defaultPositionId"),
                "eligible_slots": p.get("eligibleSlots") or [],
                "injury_status": p.get("injuryStatus"),
            })

            ros = next(
                (s for s in p.get("stats", [])
                 if s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 6),
                None,
            )
            if ros:
                proj_season = ros.get("seasonId", season_id)
                ros_stats = dict((ros.get("stats") or {}))

                act_ytd = next(
                    (s for s in p.get("stats", [])
                     if s.get("statSourceId") == 0
                     and s.get("statSplitTypeId") == 0
                     and s.get("seasonId") == season_id),
                    None,
                )
                full_proj = next(
                    (s for s in p.get("stats", [])
                     if s.get("statSourceId") == 1
                     and s.get("statSplitTypeId") == 0
                     and s.get("seasonId") == season_id),
                    None,
                )
                act_ytd_stats = (act_ytd.get("stats") or {}) if act_ytd else None

                # SVHD (stat 83): ESPN's ROS encoding is unreliable — for some
                # players it returns total GP — so we rebuild the ROS value from
                # a per-appearance rate instead of trusting it. The rate shrinks
                # ESPN's FULL-SEASON projected rate (well-formed, but a preseason
                # number that never tracks current performance) toward the
                # pitcher's season-to-date actuals. stat_id 83 IS the league's
                # SVHD scoring counter in both actuals and projections — verified
                # against ESPN's web UI; in this league SVHD = SV + HLD with no
                # blown-save penalty (an earlier note claiming 83 "subtracts
                # blown saves" was a mis-read of the broken split=6 ROS value,
                # not the actuals). Prefer 83 over the raw stat_id 56 sum.
                apply_svhd_rate_blend(
                    ros_stats, act_ytd_stats,
                    (full_proj.get("stats") or {}) if full_proj else None)

                # QS (stat 63): ESPN's ROS rate is well-formed here — no encoding
                # bug — but carries the same failure to track current
                # performance, as a pure *level* bias. Same shrinkage, shrinking
                # toward actuals from ESPN's ROS rate. See blend_qs_rate.
                apply_qs_rate_blend(ros_stats, act_ytd_stats)

                for stat_id_str, value in ros_stats.items():
                    if value is None:
                        continue
                    projections.append({
                        "player_id": pid,
                        "stat_id": int(stat_id_str),
                        "value": float(value),
                        "split_id": 6,
                        "season_id": proj_season,
                    })

    return {
        "matchup_period_id": period_id,
        "season_id": season_id,
        "players": players,
        "roster_entries": roster_entries,
        "projections": projections,
    }


def fetch_daily_lineups(scoring_period_id: int | None = None) -> list[dict]:
    """Each fantasy team's lineup-slot assignment for one day.

    Returns [{fantasy_team_id, player_id, full_name, lineup_slot_id}]. With no
    `scoring_period_id` this is the *current* day's (locked) lineup — what we
    snapshot each live tick. Passing a `scoringPeriodId` fetches a historical
    day's lineup for backfill (ESPN serves per-scoring-period roster states).

    This is the source of truth for "did this player count for the team that
    day": a player's box-score line contributes only if their slot here is an
    active (scored) slot, not bench/IL.
    """
    extra = {"scoringPeriodId": scoring_period_id} if scoring_period_id else None
    d = _get(["mRoster"], extra)
    out: list[dict] = []
    for t in d.get("teams", []):
        team_id = t["id"]
        for entry in t.get("roster", {}).get("entries", []):
            pid = ((entry.get("playerPoolEntry") or {}).get("player") or {}).get("id")
            if pid is None:
                continue
            out.append({
                "fantasy_team_id": team_id,
                "player_id": pid,
                "full_name": ((entry.get("playerPoolEntry") or {}).get("player") or {}).get("fullName") or "",
                "lineup_slot_id": entry.get("lineupSlotId"),
            })
    return out


def fetch_all_matchups() -> list[dict]:
    """All matchups across every period in the season, each with cat-by-cat
    scores (zeros for future periods).

    Returns rows of:
      {matchup_id, matchup_period_id, home_team_id, away_team_id, winner,
       scores: [{team_id, stat_id, score, result}, ...]}
    """
    d = _get(["mMatchup", "mMatchupScore"])
    out = []
    for m in d.get("schedule", []):
        period_id = m.get("matchupPeriodId")
        if period_id is None:
            continue
        home = m.get("home") or {}
        away = m.get("away") or {}
        scores: list[dict] = []
        for side in (home, away):
            cs = side.get("cumulativeScore") or {}
            by_stat = cs.get("scoreByStat") or {}
            for stat_id_str, entry in by_stat.items():
                scores.append({
                    "team_id": side.get("teamId"),
                    "stat_id": int(stat_id_str),
                    "score": float(entry.get("score") or 0.0),
                    "result": entry.get("result"),
                })
        out.append({
            "matchup_id": m["id"],
            "matchup_period_id": period_id,
            "home_team_id": home.get("teamId"),
            "away_team_id": away.get("teamId"),
            "winner": m.get("winner"),
            "scores": scores,
        })
    return out
