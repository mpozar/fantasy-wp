"""Active-axis game-day windows: overlapping windows must be clamped disjoint.

Regression for the 2026-06-17 chart gap: a Jun-16 game finalized ~a day late
(suspended/resumed), so Jun 16's active_end ran into Jun 17's window. The
Active x-axis assigns each point to the *first* interval containing it, so the
overlap stole Jun 17's early points and left a blank horizontal lead-in.
"""

from app.cli import _clamp_active_intervals


def test_clamps_overlap_to_next_start():
    # Jun 16 window runs past Jun 17's start (the real incident, simplified).
    ivs = [
        {"date": "2026-06-16", "start": "2026-06-16T22:45:00+00:00",
         "end": "2026-06-17T20:05:00+00:00"},
        {"date": "2026-06-17", "start": "2026-06-17T16:45:00+00:00",
         "end": "2026-06-18T04:35:00+00:00"},
    ]
    out = _clamp_active_intervals(ivs)
    # Jun 16 end pulled back to Jun 17 start -> disjoint, no overlap.
    assert out[0]["end"] == "2026-06-17T16:45:00+00:00"
    assert out[1]["end"] == "2026-06-18T04:35:00+00:00"  # untouched
    # the next day's start is never inside the prior window any more
    assert out[0]["end"] <= out[1]["start"]


def test_leaves_disjoint_windows_untouched():
    ivs = [
        {"date": "2026-06-15", "start": "2026-06-15T22:45:00+00:00",
         "end": "2026-06-16T04:50:00+00:00"},
        {"date": "2026-06-16", "start": "2026-06-16T22:45:00+00:00",
         "end": "2026-06-17T05:00:00+00:00"},
    ]
    before = [dict(x) for x in ivs]
    assert _clamp_active_intervals(ivs) == before  # overnight gap preserved


def test_does_not_invert_window():
    # Degenerate: next day's start precedes this day's start (shouldn't happen,
    # since input is ordered by start) -> don't push end below its own start.
    ivs = [
        {"date": "2026-06-16", "start": "2026-06-16T18:00:00+00:00",
         "end": "2026-06-16T23:00:00+00:00"},
        {"date": "2026-06-17", "start": "2026-06-16T12:00:00+00:00",
         "end": "2026-06-17T05:00:00+00:00"},
    ]
    out = _clamp_active_intervals(ivs)
    assert out[0]["end"] == "2026-06-16T23:00:00+00:00"  # unchanged, not inverted


def test_single_and_empty():
    assert _clamp_active_intervals([]) == []
    one = [{"date": "2026-06-16", "start": "2026-06-16T22:45:00+00:00",
            "end": "2026-06-17T05:00:00+00:00"}]
    assert _clamp_active_intervals([dict(one[0])]) == one
