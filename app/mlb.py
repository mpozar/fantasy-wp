"""MLB statsapi client — public, unauthenticated.

We only need the schedule endpoint with probable pitchers hydrated.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from app.teams import MLBAM_TO_ESPN

BASE_URL = "https://statsapi.mlb.com/api/v1"


def current_matchup_window(today: date | None = None) -> tuple[date, date]:
    """Monday→Sunday containing `today` (defaults to local today)."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def matchup_period_window(period_id: int, current_period_id: int,
                          today: date | None = None) -> tuple[date, date]:
    """Mon→Sun for an arbitrary matchup period.

    Anchors on the current period's Mon→Sun and adds 7 days per period offset.
    Assumes weekly matchup periods (matchupPeriodLength=1 in ESPN settings),
    which is what this league uses.
    """
    monday_curr, _ = current_matchup_window(today)
    delta = (period_id - current_period_id) * 7
    monday = monday_curr + timedelta(days=delta)
    sunday = monday + timedelta(days=6)
    return monday, sunday


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
                "hydrate": "probablePitcher",
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
                })
    return out
