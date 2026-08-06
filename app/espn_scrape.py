"""Headless-browser scraper for ESPN's live matchup scoreboard.

ESPN's REST `mMatchupScore` endpoint lags their web UI by 5-30 minutes
during live games. The UI loads an initial REST snapshot and then receives
real-time updates via a FastCast WebSocket. This module loads the same UI
page with our auth cookies, waits for the WebSocket-driven DOM updates to
settle, then extracts the rendered cat-by-cat cumulative scores.

Output shape mirrors what `espn.fetch_all_matchups()` produces for `scores`:
a list of {team_id, stat_id, score, result} per matchup, so the caller
can drop these in to override the stale REST values.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from app import LEAGUE_ID
from app.stats import BATTING_STAT_IDS, PITCHING_STAT_IDS, is_reversed

# Persistent Chromium profile that's been pre-authenticated by
# scripts/espn_auth_setup.py. The cron-driven scraper launches against this
# profile so ESPN's MyDisney session cookies (httpOnly, can't be set from
# scratch) are already present.
REPO = Path(__file__).resolve().parent.parent
PROFILE_DIR = REPO / ".playwright_profile"

# Column order in ESPN's scoreboard matchup table — the th cells are
# H R HR SB OPS K QS ERA WHIP SVHD, which is exactly the canonical batting+
# pitching display order from app.stats (kept single-sourced so they can't drift).
COLUMN_STAT_IDS = BATTING_STAT_IDS + PITCHING_STAT_IDS


# ── Unblocking the live feed (2026-08-04) ────────────────────────────────
# The scoreboard renders an initial REST snapshot, then applies live in-play
# updates from `site.api.espn.com/apis/fantasy/v2/games/flb/games?…pbpOnly=true`.
# From 2026-08-04 Akamai began returning **403** to that request from this headless
# browser, so the page never applied a live update and the DOM sat frozen at the
# last ~07:00 UTC settle. The scrape kept returning a full ~120 well-formed but
# STALE cells, which is invisible downstream: H/HR/R/SB/K held the previous day's
# totals for a whole slate and the day landed in one tick at the settle (2026-08-06:
# six matchups at once, 35pp absolute). It also dragged the rate cats down with it —
# `sim._judge_group` judges the box-score reconstruction against the *scraped* rate,
# so a stale scrape matching the equally-stale REST baseline rejects every
# reconstruction. `validate.check_scrape_staleness` (INV_SCRAPE_STALE) detects it.
#
# The block is on browser-shaped traffic only: the identical URL returns 200 with
# ~1.6 MB of JSON to plain `httpx`. Nothing local changed when it broke (this file
# was last touched 2026-06-18; Playwright/Chromium installed May 31) — it's an
# upstream CDN policy change. So we re-fetch the blocked request server-side and
# hand the bytes to the page; ESPN still computes the scoreboard, we just stop
# letting one of its requests die.
#
# ONLY the unauthenticated public host is routed. `lm-api-reads.fantasy.espn.com`
# (the authed `mMatchupScore` endpoint) must NEVER be added here: fulfilling it from
# a cookie-less client would strip the session and break the page.
PROXY_HOST = "site.api.espn.com"
PROXY_ROUTE = f"**://{PROXY_HOST}/**"
PROXY_TIMEOUT_S = 25


def should_proxy(status: int | None) -> bool:
    """Whether a routed request needs re-fetching server-side.

    The browser attempt always runs first, so an unblocked ESPN never takes the
    proxy path and this returns False — the workaround **self-heals** the day the
    403 goes away, with no code change. `None` = the browser request didn't
    complete at all (blocked outright / connection error).

    Deliberately narrow: only the observed failure (403) and a dead request. A 4xx
    like 404 is a real answer and must be passed through, and retrying a 429
    server-side would just hammer a host that's already rate-limiting us.
    """
    return status is None or status == 403


def _serve_maybe_proxied(route, client) -> None:
    """Playwright route handler: browser first, server-side re-fetch when blocked.

    Failure of the proxy path falls through to `route.continue_()` — a broken
    workaround must degrade to today's behavior (stale cells, caught by
    INV_SCRAPE_STALE), never to a page that can't load at all.
    """
    status = None
    try:
        resp = route.fetch()
        status = resp.status
        if not should_proxy(status):
            route.fulfill(response=resp)
            return
    except Exception:
        pass          # browser request died outright — try the server-side path
    try:
        r = client.get(route.request.url)
        # Only content-type is carried over: httpx has already decoded the body, so
        # forwarding content-encoding/length would describe it wrongly. ACAO mirrors
        # what this host really sends (`access-control-allow-origin: *`) and is what
        # the page's cross-origin XHR needs to accept the response.
        route.fulfill(
            status=r.status_code,
            body=r.content,
            headers={
                "content-type": r.headers.get("content-type", "application/json"),
                "access-control-allow-origin": "*",
            },
        )
    except Exception:
        route.continue_()


def _parse_score(s: str, stat_id: int) -> float | None:
    """ESPN renders OPS as ".823" (no leading zero), ERA/WHIP with 3 decimals,
    counters as integers. Strip junk and parse."""
    s = (s or "").strip()
    if not s or s == "--":
        return None
    # ".823" → "0.823"
    if s.startswith("."):
        s = "0" + s
    try:
        return float(s)
    except ValueError:
        return None


def _decide_results(home_score: float | None, away_score: float | None,
                    stat_id: int) -> tuple[str | None, str | None]:
    if home_score is None or away_score is None:
        return None, None
    if home_score == away_score:
        return "TIE", "TIE"
    if is_reversed(stat_id):  # ERA, WHIP — lower is better
        home_wins = home_score < away_score
    else:
        home_wins = home_score > away_score
    return ("WIN", "LOSS") if home_wins else ("LOSS", "WIN")


def scrape_live_matchup_scores(matchup_period_id: int,
                               team_abbrev_to_id: dict[str, int],
                               *,
                               headless: bool = True,
                               extra_settle_ms: int = 6000) -> dict[int, list[dict]]:
    """Load the ESPN scoreboard page and read each matchup's cat-by-cat
    values from the rendered DOM.

    Returns:  {team_id: [{stat_id, score, result}, ...]}

    The caller is responsible for matching team_ids back to their matchup
    pairings (which still come from `espn.fetch_all_matchups()`).
    """
    if not PROFILE_DIR.exists():
        raise RuntimeError(
            f"No Playwright profile at {PROFILE_DIR}. Run "
            "`.venv/bin/python scripts/espn_auth_setup.py` first."
        )

    url = (f"https://fantasy.espn.com/baseball/league/scoreboard"
           f"?leagueId={LEAGUE_ID}&matchupPeriodId={matchup_period_id}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
        )
        proxy_client = httpx.Client(timeout=PROXY_TIMEOUT_S)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            # Unblock the live feed — see PROXY_ROUTE. Must be installed before
            # goto, or the page's first request slips past it.
            page.route(PROXY_ROUTE,
                       lambda route: _serve_maybe_proxied(route, proxy_client))
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # Wait for the matchup tables to appear (initial REST snapshot)
            # then let the FastCast WebSocket push deliver any live updates.
            try:
                page.wait_for_function(
                    "document.querySelectorAll('table').length >= 2",
                    timeout=15000,
                )
            except Exception:
                # Probably hit a login wall — the persistent profile expired.
                # Let the caller fall back to the REST data.
                return {}
            page.wait_for_timeout(extra_settle_ms)

            # Walk every matchup table in the DOM and pull the two team rows.
            matchups = page.evaluate(_JS_EXTRACT)
        finally:
            proxy_client.close()
            context.close()

    out: dict[int, list[dict]] = {}
    for m in matchups:
        if len(m.get("teams") or []) != 2:
            continue
        home, away = m["teams"][0], m["teams"][1]
        h_abbrev, a_abbrev = home.get("abbrev"), away.get("abbrev")
        if not h_abbrev or not a_abbrev:
            continue
        h_id = team_abbrev_to_id.get(h_abbrev)
        a_id = team_abbrev_to_id.get(a_abbrev)
        if h_id is None or a_id is None:
            # Unknown abbreviation — skip this matchup; the REST fallback will
            # provide its (lagged) scores.
            continue

        for col_idx, stat_id in enumerate(COLUMN_STAT_IDS):
            h_val = _parse_score(home["stats"][col_idx] if col_idx < len(home["stats"]) else "", stat_id)
            a_val = _parse_score(away["stats"][col_idx] if col_idx < len(away["stats"]) else "", stat_id)
            h_res, a_res = _decide_results(h_val, a_val, stat_id)
            out.setdefault(h_id, []).append({
                "stat_id": stat_id, "score": h_val, "result": h_res,
            })
            out.setdefault(a_id, []).append({
                "stat_id": stat_id, "score": a_val, "result": a_res,
            })
    return out


# Runs in the page context — find scoreboard matchup tables and extract the
# two team rows from each.
_JS_EXTRACT = r"""
() => {
  const out = [];
  for (const table of document.querySelectorAll('table')) {
    // Identify matchup-score tables by their header content.
    const headerText = table.querySelector('thead')?.innerText || '';
    if (!headerText.includes('H') || !headerText.includes('SVHD')) continue;

    const rows = table.querySelectorAll('tbody tr');
    if (rows.length < 2) continue;

    const teams = [];
    for (const row of rows) {
      const cells = [...row.querySelectorAll('td, th')].map(c => c.innerText.trim());
      if (cells.length < 2) continue;
      teams.push({ abbrev: cells[0], stats: cells.slice(1) });
    }
    if (teams.length === 2) out.push({ teams });
  }
  return out;
}
"""
