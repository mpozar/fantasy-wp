"""Just-activated-off-IL handling.

When a manager activates a player off the IL mid-day (after games have started),
the league defers the activation to the next game day, so ESPN leaves the player
in the IL slot for *today* but active from tomorrow. The model must not treat that
as "manager stashing an active player → out for the period" (which zeroed him);
instead he's available for the rest of the matchup, just not today. See
`_is_playable` / `_est_return_date` (IL_SLOT + playable-status branch).
"""
from datetime import date, timedelta

from app import sim

IL = sim.IL_SLOT
TODAY = date(2026, 7, 9)


def _p(slot, status, override=None):
    d = {"lineup_slot_id": slot, "injury_status": status}
    if override is not None:
        d["injury_return_override"] = override
    return d


# ── _est_return_date ──────────────────────────────────────────────────────────

def test_il_slot_active_returns_next_game_day():
    # Just activated but still IL-slotted today → available tomorrow, not today.
    assert sim._est_return_date(_p(IL, "ACTIVE"), TODAY) == TODAY + timedelta(days=1)


def test_il_slot_genuine_il_unchanged():
    # A real IL stint still uses the fixed-days return estimate.
    assert sim._est_return_date(_p(IL, "FIFTEEN_DAY_DL"), TODAY) == TODAY + timedelta(days=10)


def test_injury_override_wins_over_il_activation_heuristic():
    # An explicit ESPN return date beats the tomorrow heuristic.
    ov = date(2026, 7, 12)
    assert sim._est_return_date(_p(IL, "ACTIVE", override=ov), TODAY) == ov


def test_active_in_normal_slot_still_playable_today():
    # Non-IL slot is unaffected — an active player plays today.
    assert sim._est_return_date(_p(3, "ACTIVE"), TODAY) == TODAY


# ── _is_playable ──────────────────────────────────────────────────────────────

def test_il_slot_active_is_now_playable():
    assert sim._is_playable(_p(IL, "ACTIVE"), TODAY) is True


def test_il_slot_genuine_il_playable():
    assert sim._is_playable(_p(IL, "FIFTEEN_DAY_DL"), TODAY) is True


def test_il_slot_out_still_excluded():
    # A genuinely-out status in the IL slot is still excluded outright.
    assert sim._is_playable(_p(IL, "OUT"), TODAY) is False


# ── integration through the hitter optimizer ──────────────────────────────────

def _hitter(slot, status):
    return {"player_id": 1, "full_name": "Test Hitter", "pro_team_id": 100,
            "default_position_id": 3, "injury_status": status,
            "lineup_slot_id": slot, "eligible_slots": [3], "ros_stats": {}}


def _game(d):
    return {"game_date": d, "game_status": "Scheduled", "current_inning": None,
            "inning_state": None, "is_home": 1, "opponent_pro_team_id": 200}


def _ctx(as_of):
    return sim.SimContext(lineup_slot_counts={3: 1}, as_of=as_of,
                          slot_by_norm_name=None, live_batters_by_team={})


def test_il_activated_hitter_gets_future_days_not_today():
    as_of = date(2026, 7, 9)
    sched = {100: [_game("2026-07-09"), _game("2026-07-10"), _game("2026-07-11")]}
    days = sim._hitter_days_slotted([_hitter(IL, "ACTIVE")], sched, _ctx(as_of))
    assert days.get(1) == 2  # 07-10 + 07-11 count; today (07-09) is excluded


def test_il_out_hitter_gets_no_days():
    as_of = date(2026, 7, 9)
    sched = {100: [_game("2026-07-10"), _game("2026-07-11")]}
    days = sim._hitter_days_slotted([_hitter(IL, "OUT")], sched, _ctx(as_of))
    assert days.get(1, 0) == 0


# ── build_budgets: an IL-slotted returning SP is projected (NOT hard-excluded) ──
# Locks the behavior a stale CLAUDE.md note mis-described as "IL slot = hard
# filter, stay excluded." The 2026-07-22 Suárez case: IL-slotted, FIFTEEN_DAY_DL,
# ESPN return date = today → he DOES get a projected start on an open game.

def _il_sp(override):
    return {"player_id": 9, "full_name": "Backfromil Ace", "pro_team_id": 100,
            "default_position_id": 1, "injury_status": "FIFTEEN_DAY_DL",
            "lineup_slot_id": IL, "injury_return_override": override,
            "ros_stats": {sim.STAT_GS: 30, sim.STAT_PITCH_GP: 30,
                          sim.STAT_OUTS: 540, sim.STAT_K: 180}}


def _open_game(d):
    return {"game_pk": 1, "game_date": d, "game_status": "Scheduled",
            "current_inning": None, "inning_state": None,
            "probable_pitcher_name": None, "is_home": 1, "opponent_pro_team_id": 200}


def test_il_slotted_sp_with_return_today_is_projected_a_start():
    sched = {100: [_open_game(TODAY.isoformat())]}
    ctx = sim.SimContext(team_total_ros_games={100: 30}, as_of=TODAY)
    sp = [b for b in sim.build_budgets([_il_sp(TODAY)], sched, ctx) if b.role == "SP"]
    assert sp and sp[0].units > 0        # included & projected, NOT hard-excluded


def test_il_slotted_sp_returning_after_the_window_projects_nothing():
    # Return date past every scheduled game → the per-game return-date gate
    # zeroes him (the inclusion is date-gated, not unconditional).
    sched = {100: [_open_game(TODAY.isoformat())]}
    ctx = sim.SimContext(team_total_ros_games={100: 30}, as_of=TODAY)
    sp = [b for b in sim.build_budgets([_il_sp(TODAY + timedelta(days=30))], sched, ctx)
          if b.role == "SP"]
    assert not sp or sp[0].units == 0
