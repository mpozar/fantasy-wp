#!/usr/bin/env python3
"""One-time interactive ESPN login for the Playwright profile used by the
live-state scraper.

The web UI requires more than just the SWID/espn_s2 cookies we read from
~/.zshenv — it also needs MyDisney session cookies (ESPN-ONESITE.WEB-PROD.*,
dtcAuth, etc.) which are httpOnly. Easiest way to get them is to log in once
through a real browser; Playwright's persistent context saves the resulting
profile dir, and the cron-driven scraper later launches against that same
profile (headless) with all cookies already there.

Run this when:
  - first setting up the scraper
  - after ESPN expires the session (the scraper will return no data and log
    a "login wall detected" warning; that's the cue to re-run this)

Usage:
  .venv/bin/python scripts/espn_auth_setup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
PROFILE_DIR = REPO / ".playwright_profile"
START_URL = "https://www.espn.com/login"


def main() -> int:
    PROFILE_DIR.mkdir(exist_ok=True)
    print(f"Profile dir: {PROFILE_DIR}")
    print("Opening Chromium — log in to ESPN through the page that appears.")
    print("When you can see the fantasy scoreboard in the browser, come back")
    print("here and press Enter.")
    print()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL)
        try:
            input("[press Enter when logged in and the scoreboard renders] ")
        except KeyboardInterrupt:
            print()
            print("Aborted. Profile may not be fully authenticated.")
            ctx.close()
            return 1
        # Quick sanity check: do we have ESPN auth cookies?
        cookies = ctx.cookies("https://fantasy.espn.com/")
        names = {c["name"] for c in cookies}
        required = {"SWID", "espn_s2"}
        missing = required - names
        if missing:
            print(f"⚠️  Missing cookies after login: {missing}")
            print("   Make sure you completed the MyDisney login flow.")
        else:
            print(f"✓ Got {len(cookies)} cookies including SWID/espn_s2.")
            extras = [n for n in names if n.startswith(("ESPN-ONESITE", "dtcAuth", "espnAuth"))]
            if extras:
                print(f"  Web-UI session cookies present: {extras}")
            else:
                print("⚠️  No MyDisney session cookies (ESPN-ONESITE.*) — the scraper")
                print("   may still hit a login wall. Try logging in again.")
        ctx.close()
    print(f"Saved profile to {PROFILE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
