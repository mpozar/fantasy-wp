"""Public ESPN site.api JSON client (no auth, no browser).

Distinct from the other two ESPN paths:
  - app/espn.py        — authenticated fantasy v3 API (league/rosters/projections)
  - app/espn_scrape.py — Playwright DOM scrape (live matchup cat totals)

This hits ESPN's *public* sports API for MLB game context that ESPN exposes
earlier or cleaner than our other sources:
  - **Probable pitchers** — ESPN's feed leads MLB statsapi by a day or two, so
    a start showing on ESPN but not yet posted by MLB can be filled in early.
  - **Injuries with real return dates** — vs our fixed-days IL heuristic.

ESPN's MLB `team.id` in this API equals our fantasy `proTeamId` (verified:
12=SEA, 26=SF, 14=TOR, …), so games map straight to `pro_team_id` — no
abbreviation/name table needed. The API is unofficial/undocumented but stable
and widely used (same risk class as MLB statsapi, far less brittle than scraping).
"""

from __future__ import annotations

import unicodedata
from datetime import date, timedelta

import httpx

SITE_API = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"


def _norm(s: str | None) -> str:
    """Mirror of sim._norm_name so injury/probable names match rostered ones."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def fetch_probables(start: date, end: date) -> dict[tuple[str, int], str]:
    """Probable pitchers from the public scoreboard.

    Returns `{(game_date 'YYYY-MM-DD', pro_team_id): pitcher_name}`. Keyed by the
    queried calendar date (aligns with MLB's official game_date — verified). Best
    effort: a failed day is skipped rather than aborting the range.
    """
    out: dict[tuple[str, int], str] = {}
    with httpx.Client(timeout=30.0) as client:
        d = start
        while d <= end:
            try:
                r = client.get(f"{SITE_API}/scoreboard",
                               params={"dates": d.strftime("%Y%m%d")})
                r.raise_for_status()
                data = r.json()
            except Exception:  # noqa: BLE001 — best effort per day
                d += timedelta(days=1)
                continue
            for ev in data.get("events", []):
                comps = ev.get("competitions") or [{}]
                for c in (comps[0].get("competitors") or []):
                    team = c.get("team") or {}
                    probs = c.get("probables") or []
                    if not team.get("id") or not probs:
                        continue
                    name = (probs[0].get("athlete") or {}).get("displayName")
                    if not name:
                        continue
                    try:
                        pid = int(team["id"])
                    except (TypeError, ValueError):
                        continue
                    out[(d.isoformat(), pid)] = name
            d += timedelta(days=1)
    return out


def fetch_injuries() -> dict[str, tuple[str, date]]:
    """Players on an actual stint, with ESPN's estimated return date.

    Returns `{normalized_name: (full_name, return_date)}`. Excludes `Day-To-Day`
    (those usually still play — benching them on an optimistic return date would
    be wrong); keeps IL / Out / suspension / etc. Entries without a parseable
    return date are skipped.
    """
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"{SITE_API}/injuries")
        r.raise_for_status()
        data = r.json()
    out: dict[str, tuple[str, date]] = {}
    for team in data.get("injuries", []):
        for e in team.get("injuries", []):
            if (e.get("status") or "").strip().lower() == "day-to-day":
                continue
            rd = ((e.get("details") or {}).get("returnDate") or "")[:10]
            name = (e.get("athlete") or {}).get("displayName")
            if not name or not rd:
                continue
            try:
                out[_norm(name)] = (name, date.fromisoformat(rd))
            except ValueError:
                continue
    return out
