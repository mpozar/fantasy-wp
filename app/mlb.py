"""MLB statsapi client — public, unauthenticated.

We only need the schedule endpoint with probable pitchers hydrated.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from app.teams import MLBAM_TO_ESPN

BASE_URL = "https://statsapi.mlb.com/api/v1"

# Monday of matchup period 1 for the current season. Period windows are
# computed *absolutely* off this anchor — never relative to "today" or to
# ESPN's `currentMatchupPeriod`, both of which drift around the Monday
# rollover (ESPN's period number lags the calendar by several hours, and a
# server clock past midnight Monday has already advanced to the new week).
# Anchoring to a fixed Monday keeps each period pinned to its true Mon→Sun
# week regardless of when the pipeline runs. Verified: period 9 = May 25–31,
# so period 1 = March 30 (= May 25 − 8×7 days). Both are Mondays.
# Update once per season.
SEASON_ANCHOR_MONDAY = date(2026, 3, 30)

# Matchup periods that span MORE than one Mon→Sun week. ESPN keeps the
# All-Star break as a single `matchupPeriodId` covering two calendar weeks
# (every team is idle for several days, so a one-week matchup would be mostly
# empty) — its scoreByStat accumulates across both weeks. Maps that
# matchupPeriodId to its length in weeks; every period AFTER a long one is
# pushed later by the extra weeks, so the anchor stays exact for the whole
# back half of the season.
#
# Why this is a hand-maintained constant rather than read from ESPN:
# ESPN's `scheduleSettings.matchupPeriods` map is identity here
# (matchupPeriodLength=1), so it carries no length/date info, and the only
# field that *would* reveal the span — each side's `pointsByScoringPeriod`
# (daily scoring-period IDs) — is populated only up to the latest *played*
# scoring period. The break is in the future, so ESPN won't expose its
# 2-week span until we reach it, yet `compute --future` projects it now.
# Hence: configure once per season, same cadence as SEASON_ANCHOR_MONDAY.
# Verify against ESPN once the break is known (2026: period 15 = July 6–19).
LONG_MATCHUPS: dict[int, int] = {15: 2}


def monday_of(d: date) -> date:
    """Monday of the Mon→Sun week containing `d`."""
    return d - timedelta(days=d.weekday())


def _period_length_weeks(period_id: int) -> int:
    """How many Mon→Sun weeks `period_id` spans (1 unless it's a long matchup)."""
    return LONG_MATCHUPS.get(period_id, 1)


def _weeks_before(period_id: int) -> int:
    """Whole weeks between SEASON_ANCHOR_MONDAY and `period_id`'s Monday.

    One week per earlier period, plus the extra weeks any earlier *long*
    matchup contributes (a 2-week matchup adds 1 extra week to everything
    after it).
    """
    extra = sum(n - 1 for p, n in LONG_MATCHUPS.items() if p < period_id)
    return (period_id - 1) + extra


def matchup_period_window(period_id: int) -> tuple[date, date]:
    """[start_monday, end_sunday] for a matchup period, anchored absolutely.

    Periods are weekly (matchupPeriodLength=1) except the few in LONG_MATCHUPS
    (the All-Star break), which span multiple weeks. Independent of the current
    date and of ESPN's reported current period — see SEASON_ANCHOR_MONDAY.
    """
    monday = SEASON_ANCHOR_MONDAY + timedelta(days=_weeks_before(period_id) * 7)
    length = _period_length_weeks(period_id)
    return monday, monday + timedelta(days=length * 7 - 1)


def period_for_date(d: date) -> int:
    """Which matchup period a calendar date falls in, by the season anchor.

    Exact inverse of `matchup_period_window`, including the multi-week
    LONG_MATCHUPS. Used to attribute live MLB games to the correct period by
    the game's own date rather than by whatever period ESPN reports as
    'current'. Dates before the anchor clamp to period 1.
    """
    week_idx = (monday_of(d) - SEASON_ANCHOR_MONDAY).days // 7
    if week_idx < 0:
        return 1
    # Walk periods, consuming each one's week-span, until week_idx lands inside.
    consumed = 0
    period_id = 1
    while True:
        length = _period_length_weeks(period_id)
        if week_idx < consumed + length:
            return period_id
        consumed += length
        period_id += 1


def fetch_schedule(start: date, end: date) -> list[dict]:
    """Return a flat list of (game, team) rows for the date range.

    Each row is a single team's perspective on a single game:
      {
        game_pk, game_date, mlbam_team_id, espn_team_id,
        opponent_mlbam_team_id, opponent_espn_team_id,
        is_home, probable_pitcher_mlbam_id, probable_pitcher_name,
        game_status,
      }

    Skips games whose teams aren't in the MLBAM_TO_ESPN map (e.g. exhibition
    games against minor-league affiliates, if they ever appear).
    """
    with httpx.Client(timeout=30.0) as client:
        r = client.get(
            f"{BASE_URL}/schedule",
            params={
                "sportId": "1",
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                # linescore gives currentInning + inningState for in-progress
                # games, used by the sim to scale remaining production.
                "hydrate": "probablePitcher,linescore",
            },
        )
    r.raise_for_status()
    d = r.json()

    out: list[dict] = []
    for d_entry in d.get("dates", []):
        for g in d_entry.get("games", []):
            game_pk = g.get("gamePk")
            game_date = (g.get("officialDate") or g.get("gameDate") or "")[:10]
            status = (g.get("status") or {}).get("detailedState")
            linescore = g.get("linescore") or {}
            current_inning = linescore.get("currentInning")
            inning_state = linescore.get("inningState")  # "Top"/"Middle"/"Bottom"/"End"
            ls_teams = linescore.get("teams") or {}
            home_runs = (ls_teams.get("home") or {}).get("runs")
            away_runs = (ls_teams.get("away") or {}).get("runs")
            teams = g.get("teams") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            home_id = (home.get("team") or {}).get("id")
            away_id = (away.get("team") or {}).get("id")
            if home_id not in MLBAM_TO_ESPN or away_id not in MLBAM_TO_ESPN:
                continue

            for side, opp, is_home in ((home, away, 1), (away, home, 0)):
                pp = side.get("probablePitcher") or {}
                team_mlbam = (side.get("team") or {}).get("id")
                opp_mlbam = (opp.get("team") or {}).get("id")
                team_runs = home_runs if is_home else away_runs
                opp_runs = away_runs if is_home else home_runs
                out.append({
                    "game_pk": game_pk,
                    "game_date": game_date,
                    "mlbam_team_id": team_mlbam,
                    "espn_team_id": MLBAM_TO_ESPN[team_mlbam],
                    "opponent_mlbam_team_id": opp_mlbam,
                    "opponent_espn_team_id": MLBAM_TO_ESPN[opp_mlbam],
                    "is_home": is_home,
                    "probable_pitcher_mlbam_id": pp.get("id"),
                    "probable_pitcher_name": pp.get("fullName"),
                    "game_status": status,
                    "current_inning": current_inning,
                    "inning_state": inning_state,
                    "team_runs": team_runs,
                    "opponent_runs": opp_runs,
                })
    return out


def _ip_to_outs(ip: str | float | None) -> int:
    """MLB inningsPitched ('5.2' = 5⅔) → integer outs."""
    if ip is None:
        return 0
    whole, _, frac = str(ip).partition(".")
    try:
        return int(whole or 0) * 3 + int(frac or 0)
    except ValueError:
        return 0


def parse_boxscore(payload: dict, game_pk: int) -> dict:
    """Pure parse of an MLB statsapi boxscore JSON into per-pitcher and
    per-batter lines (split out so it's unit-testable without the network).

    Returns {"pitchers": [...], "batters": [...]}:
      pitchers — one row per pitcher who appeared, in appearance order:
        {game_pk, mlbam_id, name, espn_team_id, order_idx, is_last, games_started,
         outs, er, k, p_h (hits allowed), p_bb (walks allowed)}
      batters  — one row per batter who appeared:
        {game_pk, mlbam_id, name, espn_team_id, ab, h, b2, b3, hr, bb, hbp, sf}

    `order_idx`/`is_last` come from the team's ordered `pitchers` list — a
    starter has exited once they're not the last entry. Skips teams not in the
    MLBAM_TO_ESPN map.
    """
    teams = (payload.get("teams") or {})
    pitchers: list[dict] = []
    batters: list[dict] = []
    for side in ("home", "away"):
        t = teams.get(side) or {}
        team_mlbam = ((t.get("team") or {}).get("id"))
        if team_mlbam not in MLBAM_TO_ESPN:
            continue
        espn_team_id = MLBAM_TO_ESPN[team_mlbam]
        players = t.get("players") or {}

        order = t.get("pitchers") or []           # personIds, appearance order
        for idx, pid in enumerate(order):
            p = players.get(f"ID{pid}") or {}
            st = (p.get("stats") or {}).get("pitching") or {}
            pitchers.append({
                "game_pk": game_pk,
                "mlbam_id": pid,
                "name": (p.get("person") or {}).get("fullName"),
                "espn_team_id": espn_team_id,
                "order_idx": idx,
                "is_last": idx == len(order) - 1,
                "games_started": st.get("gamesStarted") or 0,
                "outs": _ip_to_outs(st.get("inningsPitched")),
                "er": st.get("earnedRuns") or 0,
                "k": st.get("strikeOuts") or 0,
                "p_h": st.get("hits") or 0,
                "p_bb": st.get("baseOnBalls") or 0,
                # SVHD components: ESPN's scored SVHD (stat 83) is SV + HLD − BS
                # (blown saves subtracted — see the stat-83 note in CLAUDE.md), so
                # we keep blown saves too, not just raw SV+HLD.
                "sv": st.get("saves") or 0,
                "hld": st.get("holds") or 0,
                "bs": st.get("blownSaves") or 0,
            })

        # Batters: everyone with a batting line who actually batted (AB+BB+HBP+SF
        # > 0 catches pinch-hitters/walks; a defensive sub with no PA contributes
        # nothing and is skipped so it can't be mis-attributed).
        for pid in (t.get("batters") or players.keys()):
            pid_int = pid if isinstance(pid, int) else None
            p = players.get(f"ID{pid}" if pid_int is not None else pid) or {}
            person_id = (p.get("person") or {}).get("id")
            if person_id is None:
                continue
            st = (p.get("stats") or {}).get("batting") or {}
            ab = st.get("atBats") or 0
            bb = st.get("baseOnBalls") or 0
            hbp = st.get("hitByPitch") or 0
            sf = st.get("sacFlies") or 0
            if ab + bb + hbp + sf == 0:
                continue
            batters.append({
                "game_pk": game_pk,
                "mlbam_id": person_id,
                "name": (p.get("person") or {}).get("fullName"),
                "espn_team_id": espn_team_id,
                "ab": ab,
                "h": st.get("hits") or 0,
                "b2": st.get("doubles") or 0,
                "b3": st.get("triples") or 0,
                "hr": st.get("homeRuns") or 0,
                "bb": bb,
                "hbp": hbp,
                "sf": sf,
            })
    return {"pitchers": pitchers, "batters": batters}


def fetch_boxscore(game_pk: int) -> dict:
    """Fetch one game's boxscore → {"pitchers": [...], "batters": [...]}.
    Thin network wrapper around `parse_boxscore`."""
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{BASE_URL}/game/{game_pk}/boxscore")
    r.raise_for_status()
    return parse_boxscore(r.json(), game_pk)
