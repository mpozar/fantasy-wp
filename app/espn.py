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

# Minimum games a reliever needs in the current season for their actual
# SV+HLD rate to be trusted. Below this we fall back to ESPN's full-season
# projection rate so a tiny sample doesn't blow up the projection.
MIN_ACT_GP_FOR_SVHD_RATE = 15


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

                # ESPN's ROS projection encoding for stat_id 83 (SV+HLD) is
                # unreliable — for some players it returns total GP. Their
                # full-season projection (split=0) is also unreliable as a
                # forecasting source: it's a preseason number that doesn't
                # update with current performance, so subtracting actuals
                # from it goes negative when a player outperforms it.
                #
                # Instead, derive a per-appearance SVHD rate from the player's
                # *actual* season-to-date numbers (where stat_id 56 reliably
                # equals SV + HLD) and apply that rate to projected ROS GP.
                # For low-sample players (early in the season or recent
                # call-ups), fall back to the full-season projection's rate.
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
                svhd_rate: float | None = None
                if act_ytd:
                    act_stats = act_ytd.get("stats") or {}
                    act_gp = act_stats.get("32")
                    act_svhd = act_stats.get("56")
                    if act_gp and float(act_gp) >= MIN_ACT_GP_FOR_SVHD_RATE:
                        svhd_rate = float(act_svhd or 0) / float(act_gp)
                if svhd_rate is None and full_proj:
                    fp = full_proj.get("stats") or {}
                    proj_gp = fp.get("32")
                    proj_svhd = fp.get("83")
                    if proj_gp and float(proj_gp) > 0:
                        svhd_rate = float(proj_svhd or 0) / float(proj_gp)
                if svhd_rate is not None:
                    ros_gp = ros_stats.get("32") or 0
                    if float(ros_gp) > 0:
                        ros_stats["83"] = svhd_rate * float(ros_gp)

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
