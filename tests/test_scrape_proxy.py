"""The live-feed proxy in `espn_scrape` (2026-08-04 Akamai 403).

The scrape itself needs a real browser, so these cover the pure decision logic and
the route scoping — the two things that can regress silently. The end-to-end proof
is a live slate: INV_SCRAPE_STALE stops firing.
"""
from __future__ import annotations

import fnmatch

from app import espn_scrape as es


# ── should_proxy: when do we re-fetch server-side? ──

def test_proxies_the_observed_403():
    assert es.should_proxy(403) is True

def test_proxies_a_request_that_never_completed():
    """None = blocked outright / connection error, not an answer from the server."""
    assert es.should_proxy(None) is True

def test_does_not_proxy_a_healthy_response():
    """The browser attempt runs first, so an unblocked ESPN never takes the proxy
    path — this is what makes the workaround self-heal when the 403 goes away."""
    assert es.should_proxy(200) is False

def test_does_not_proxy_real_answers_or_rate_limits():
    # 404/401 are genuine answers; re-fetching a 429 server-side would just hammer
    # a host that is already rate-limiting us.
    for status in (204, 301, 401, 404, 429, 500, 503):
        assert es.should_proxy(status) is False, status


# ── route scoping: the auth host must never be proxied ──

def _routed(url: str) -> bool:
    """Mirror Playwright's glob matching for the configured route pattern."""
    return fnmatch.fnmatch(url, es.PROXY_ROUTE)

def test_routes_the_blocked_live_feed():
    assert _routed("https://site.api.espn.com/apis/fantasy/v2/games/flb/games"
                   "?lang=en&region=us&useMap=true&dates=20260803-20260809&pbpOnly=true")

def test_never_routes_the_authenticated_host():
    """THE regression that would silently break the page: fulfilling the authed
    endpoint from a cookie-less client strips the ESPN session. `lm-api-reads`
    serves mMatchupScore and must always go through the browser."""
    assert not _routed(
        "https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/2026"
        "/segments/0/leagues/71455?view=mMatchupScore&view=mScoreboard")
    assert "lm-api-reads" not in es.PROXY_ROUTE
    assert es.PROXY_HOST == "site.api.espn.com"

def test_never_routes_the_scoreboard_page_itself():
    assert not _routed("https://fantasy.espn.com/baseball/league/scoreboard?leagueId=71455")
