"""Tests for the matchup-period calendar (app/mlb.py).

Locks in the absolute-anchor period windows AND the multi-week All-Star
matchup handling (LONG_MATCHUPS), so a future schedule change can't silently
reintroduce the 1-week-per-matchup assumption.

Run: .venv/bin/python -m pytest tests/ -q
"""

from datetime import date, timedelta

from app import mlb
from app.mlb import matchup_period_window, period_for_date


def test_anchor_checkpoint_period_9():
    # Known mapping verified against ESPN: period 9 = May 25–31, 2026.
    assert matchup_period_window(9) == (date(2026, 5, 25), date(2026, 5, 31))


def test_period_1_starts_on_anchor():
    assert matchup_period_window(1) == (date(2026, 3, 30), date(2026, 4, 5))


def test_periods_before_break_are_weekly():
    # Periods 2..14 are plain Mon→Sun weeks, each 7 days, contiguous.
    for p in range(1, 15):
        start, end = matchup_period_window(p)
        assert end - start == timedelta(days=6), p
        assert start.weekday() == 0, p  # Monday
        # Next period starts the day after this one ends.
        nxt_start, _ = matchup_period_window(p + 1)
        if p < 14:
            assert nxt_start == end + timedelta(days=1), p


def test_allstar_matchup_15_spans_two_weeks():
    # ESPN keeps the All-Star break as one matchupPeriodId: July 6–19, 2026.
    assert matchup_period_window(15) == (date(2026, 7, 6), date(2026, 7, 19))


def test_period_16_shifted_after_break():
    # Everything after the 2-week matchup is pushed one week later.
    assert matchup_period_window(16) == (date(2026, 7, 20), date(2026, 7, 26))
    assert matchup_period_window(17) == (date(2026, 7, 27), date(2026, 8, 2))


def test_period_for_date_is_inverse():
    # Walk every day of periods 1..20 and confirm round-trip consistency.
    for p in range(1, 21):
        start, end = matchup_period_window(p)
        d = start
        while d <= end:
            assert period_for_date(d) == p, (p, d.isoformat())
            d += timedelta(days=1)


def test_both_weeks_of_break_attribute_to_15():
    # The crux of the bug: week-2 games of the break must land in matchup 15,
    # not leak forward into 16.
    assert period_for_date(date(2026, 7, 6)) == 15   # week 1 Monday
    assert period_for_date(date(2026, 7, 13)) == 15  # week 2 Monday
    assert period_for_date(date(2026, 7, 19)) == 15  # week 2 Sunday
    assert period_for_date(date(2026, 7, 20)) == 16  # first day after break


def test_no_gaps_or_overlaps_across_break():
    # Periods 14, 15, 16 tile the calendar with no missing or double-counted day.
    _, end14 = matchup_period_window(14)
    start15, end15 = matchup_period_window(15)
    start16, _ = matchup_period_window(16)
    assert start15 == end14 + timedelta(days=1)
    assert start16 == end15 + timedelta(days=1)


def test_dates_before_anchor_clamp_to_period_1():
    assert period_for_date(date(2026, 3, 25)) == 1  # ESPN period 1 opening days


# ── refresh-live window (cli._live_window) ──────────────────────────────────
# Widened 2026-08-18 from a flat today+2 to "through the current period's end",
# so days 3-6 of a week get their probables refreshed every 5-min tick instead
# of only at the 04:02Z daily refresh-schedule.

def test_live_window_reaches_the_end_of_the_current_period():
    from datetime import date
    from app import cli
    # Period 20 = 2026-08-17 (Mon) .. 2026-08-23 (Sun).
    start, end = cli._live_window(date(2026, 8, 18))
    assert start == date(2026, 8, 17)            # always yesterday
    assert end == date(2026, 8, 23)              # period end, not today+2


def test_live_window_never_shrinks_below_the_old_two_day_reach():
    """Late in a week the period ends sooner than today+2 — the near-term reach
    must not regress, or a just-posted probable for tomorrow's game would fall
    outside the window entirely."""
    from datetime import date
    from app import cli
    for d in (date(2026, 8, 22), date(2026, 8, 23)):   # Sat, Sun of period 20
        _, end = cli._live_window(d)
        assert end == d + timedelta(days=2)


def test_live_window_caps_a_long_matchup_fortnight():
    """`LONG_MATCHUPS` period 15 spans 2026-07-06..07-19; without the cap the
    window would be a 14-day upsert on every 5-minute tick."""
    from datetime import date
    from app import cli
    _, end = cli._live_window(date(2026, 7, 8))
    assert end == date(2026, 7, 8) + timedelta(days=cli.LIVE_FORWARD_MAX_DAYS)
    assert end < mlb.matchup_period_window(15)[1]
