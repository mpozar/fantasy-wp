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


def fetch_boxscore(game_pk: int) -> list[dict]:
    """Per-pitcher live lines for one game, for in-game QS/SVHD projection.

    Returns one row per pitcher who has appeared, in appearance order:
      {game_pk, mlbam_id, name, espn_team_id, order_idx, is_last (currently
       pitching for their team), games_started, outs, er, k}

    `order_idx`/`is_last` come from the team's ordered `pitchers` list — a
    starter has exited once they're not the last entry. Skips teams not in the
    MLBAM_TO_ESPN map.
    """
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{BASE_URL}/game/{game_pk}/boxscore")
    r.raise_for_status()
    teams = (r.json().get("teams") or {})

    out: list[dict] = []
    for side in ("home", "away"):
        t = teams.get(side) or {}
        team_mlbam = ((t.get("team") or {}).get("id"))
        if team_mlbam not in MLBAM_TO_ESPN:
            continue
        order = t.get("pitchers") or []           # personIds, appearance order
        players = t.get("players") or {}
        for idx, pid in enumerate(order):
            p = players.get(f"ID{pid}") or {}
            st = (p.get("stats") or {}).get("pitching") or {}
            out.append({
                "game_pk": game_pk,
                "mlbam_id": pid,
                "name": (p.get("person") or {}).get("fullName"),
                "espn_team_id": MLBAM_TO_ESPN[team_mlbam],
                "order_idx": idx,
                "is_last": idx == len(order) - 1,
                "games_started": st.get("gamesStarted") or 0,
                "outs": _ip_to_outs(st.get("inningsPitched")),
                "er": st.get("earnedRuns") or 0,
                "k": st.get("strikeOuts") or 0,
            })
    return out
