"""Hitter day-slotting: timezone-independence + floor-removal regression guards.

The lineup optimizer must behave identically regardless of the host machine's
timezone. It once used `date.today()` (host-local) and dropped a whole day of
still-unplayed US games at the host's local midnight — the 00:00-CEST WP lurch in
INCIDENTS.md. It now keys off game *status* for healthy players and an injected UTC
`as_of` for the IL-return heuristic only.
"""
from datetime import date

from app import sim


def _hitter(pid=1, team=100):
    return {"player_id": pid, "pro_team_id": team, "default_position_id": 3,
            "injury_status": "ACTIVE", "lineup_slot_id": 3,
            "eligible_slots": [3], "ros_stats": {}}


def _game(game_date, status="Scheduled", inning=None):
    return {"game_date": game_date, "game_status": status, "current_inning": inning,
            "inning_state": None, "is_home": 1, "opponent_pro_team_id": 200}


SLOTS = {3: 1}


def test_scheduled_game_not_dropped_when_clock_rolls_past_it():
    """The bug: a game still 'Scheduled' on 06-05 got dropped once the host clock
    rolled to 06-06 (day < local-today). Now status drives it — an unplayed game
    keeps its projection regardless of as_of."""
    roster = [_hitter()]
    sched = {100: [_game("2026-06-05", status="Scheduled")]}
    before = sim._hitter_days_slotted(roster, sched, SLOTS, as_of=date(2026, 6, 5))
    after = sim._hitter_days_slotted(roster, sched, SLOTS, as_of=date(2026, 6, 6))
    assert before == after == {1: 1.0}


def test_final_game_contributes_zero_regardless_of_as_of():
    """A played (Final) game falls out via the factor, not a date floor — so the
    result is identical no matter where 'today' sits."""
    roster = [_hitter()]
    sched = {100: [_game("2026-06-05", status="Final")]}
    for ao in (date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 6)):
        assert sim._hitter_days_slotted(roster, sched, SLOTS, as_of=ao) == {1: 0.0}


def test_in_progress_game_scales_smoothly_not_a_jump():
    """Mid-game → a fractional unit (the smooth handoff), never a clean 0 or 1."""
    roster = [_hitter()]
    sched = {100: [_game("2026-06-05", status="In Progress", inning=5)]}
    u = sim._hitter_days_slotted(roster, sched, SLOTS, as_of=date(2026, 6, 5))
    assert 0.0 < u[1] < 1.0


def test_il_player_still_floored_by_return_date():
    """The IL-return floor is preserved — a player back on 06-07 isn't slotted for
    a 06-05 game (this is the one legitimate use of a date floor)."""
    p = _hitter()
    p["injury_status"] = "TEN_DAY_IL"
    p["injury_return_override"] = date(2026, 6, 7)
    sched = {100: [_game("2026-06-05", status="Scheduled")]}
    u = sim._hitter_days_slotted([p], sched, SLOTS, as_of=date(2026, 6, 5))
    assert u.get(1, 0.0) == 0.0


def test_default_as_of_is_utc_not_local():
    """The default reference is UTC, never the host's local date."""
    from datetime import datetime, timezone
    assert sim._utc_today() == datetime.now(timezone.utc).date()


# ── Benched-at-first-pitch gate (mirrors the pitcher schedule filter) ──────────
# A hitter benched when his game starts is locked out of it, so an In-Progress game
# must contribute nothing — even though the optimizer would otherwise slot him.
# Future (not-yet-started) games still count; he may be activated before they lock.

def _named_hitter():
    h = _hitter()
    h["full_name"] = "Benched Bat"
    return h


def _slots(slot):
    return {sim._norm_name("Benched Bat"): slot}


def test_benched_hitter_dropped_from_inprogress_game():
    roster = [_named_hitter()]
    sched = {100: [_game("2026-06-05", status="In Progress", inning=5)]}
    ao = date(2026, 6, 5)
    # Active hitter slot (3) → fractional unit, as before. Benched (16) → 0.
    assert 0.0 < sim._hitter_days_slotted(roster, sched, SLOTS, ao, _slots(3))[1] < 1.0
    assert sim._hitter_days_slotted(roster, sched, SLOTS, ao, _slots(16))[1] == 0.0


def test_benched_hitter_future_game_still_slotted():
    # Benched in today's locked lineup, but a Scheduled game isn't locked yet → he
    # may be activated, so it still projects (parity with the pitcher streaming hedge).
    roster = [_named_hitter()]
    sched = {100: [_game("2026-06-06", status="Scheduled")]}
    u = sim._hitter_days_slotted(roster, sched, SLOTS, date(2026, 6, 5), _slots(16))
    assert u[1] == 1.0
