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


def monday_of(d: date) -> date:
    """Monday of the Mon→Sun week containing `d`."""
    return d - timedelta(days=d.weekday())


def current_matchup_window(today: date | None = None) -> tuple[date, date]:
    """Monday→Sunday containing `today` (defaults to local today)."""
    monday = monday_of(today or date.today())
    return monday, monday + timedelta(days=6)


def matchup_period_window(period_id: int) -> tuple[date, date]:
    """Mon→Sun for a matchup period, anchored absolutely on the season start.

    Assumes weekly matchup periods (matchupPeriodLength=1 in ESPN settings),
    which is what this league uses. Independent of the current date and of
    ESPN's reported current period — see SEASON_ANCHOR_MONDAY.
    """
    monday = SEASON_ANCHOR_MONDAY + timedelta(days=(period_id - 1) * 7)
    return monday, monday + timedelta(days=6)


def period_for_date(d: date) -> int:
    """Which matchup period a calendar date falls in, by the season anchor.

    Inverse of `matchup_period_window`. Used to attribute live MLB games to
    the correct period by their game date rather than by whatever period
    ESPN currently reports as 'current'.
    """
    return (monday_of(d) - SEASON_ANCHOR_MONDAY).days // 7 + 1


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
                })
    return out
