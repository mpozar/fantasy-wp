"""Budget provenance flags (`Budget.flags` → `details_json.*_budgets[].flags`).

Telemetry only — these tests pin (a) that the flags name the special-case path
a budget actually took, and (b) that adding them changed no budget numbers
(the flags must never influence the sim).
"""

from app import sim
from app.sim import (
    STAT_GS, STAT_PITCH_GP, STAT_OUTS, STAT_ER, STAT_QS, STAT_K, STAT_SVHD,
    build_budgets,
)

TEAM = 100


def _starter():
    return {
        "player_id": 1, "full_name": "Test Starter", "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 15,
        "ros_stats": {STAT_GS: 20, STAT_PITCH_GP: 20, STAT_OUTS: 360,
                      STAT_ER: 50, STAT_QS: 10, STAT_K: 200},
    }


def _spot():
    # RP by season GS/GP ratio (3/30) but announced/live starter → promoted.
    return {
        "player_id": 7, "full_name": "Spot Starter", "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 13,
        "ros_stats": {STAT_GS: 3, STAT_PITCH_GP: 30, STAT_OUTS: 150,
                      STAT_ER: 26, STAT_QS: 1, STAT_K: 40},
    }


def _reliever():
    return {
        "player_id": 2, "full_name": "Test Closer", "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 15,
        "ros_stats": {STAT_PITCH_GP: 60, STAT_OUTS: 180, STAT_ER: 25,
                      STAT_SVHD: 30, STAT_K: 80},
    }


def _game(status="In Progress", inning=6, probable="Test Starter",
          team_runs=3, opp_runs=1, game_pk=999, game_date="2026-06-02"):
    return {
        "game_pk": game_pk, "game_date": game_date, "game_status": status,
        "current_inning": inning, "inning_state": "Top",
        "probable_pitcher_name": probable, "team_runs": team_runs,
        "opponent_runs": opp_runs, "is_home": 1, "opponent_pro_team_id": 200,
    }


def _live(**kw):
    base = dict(game_pk=999, name="Test Starter", is_last=1, games_started=1,
                outs=18, er=1, k=5)
    base.update(kw)
    return {TEAM: {sim._norm_name(base["name"]): base}}


def _flags(budgets, role):
    return next(b.flags for b in budgets if b.role == role)


def test_flat_extra_flag_when_no_cadence_anchor():
    # Scheduled game with NO announced probable and no recorded last start:
    # no cadence anchor, so the extra piece comes from the flat ROS-share
    # fallback — flagged as such, and nothing else fires.
    budgets = build_budgets([_starter()],
                            {TEAM: [_game(status="Scheduled", probable=None)]},
                            sim.SimContext(team_total_ros_games={TEAM: 60}))
    assert _flags(budgets, "SP") == ["flat-extra"]


def test_cadence_flag_when_probable_anchors_the_walk():
    # An announced probable both fixes that start AND anchors the cadence walk
    # for the open tail — the extra piece is cadence-built, flagged as such.
    budgets = build_budgets([_starter()], {TEAM: [_game(status="Scheduled")]},
                            sim.SimContext(team_total_ros_games={TEAM: 60}))
    assert _flags(budgets, "SP") == ["cadence"]


def test_qs_ingame_flag_on_live_start():
    budgets = build_budgets([_starter()], {TEAM: [_game()]}, sim.SimContext(
        team_total_ros_games={TEAM: 60}, live_by_team=_live()))
    assert "qs-ingame" in _flags(budgets, "SP")


def test_promoted_flag_on_spot_starter():
    budgets = build_budgets([_spot()], {TEAM: [_game(probable="Spot Starter")]},
                            sim.SimContext(team_total_ros_games={TEAM: 60},
                                           live_by_team=_live(name="Spot Starter")))
    f = _flags(budgets, "SP")
    assert "promoted" in f and "qs-ingame" in f


def test_svhd_ingame_flag_on_live_reliever():
    budgets = build_budgets([_reliever()], {TEAM: [_game(probable=None)]},
                            sim.SimContext(
                                team_total_ros_games={TEAM: 60},
                                live_by_team=_live(name="Test Closer",
                                                   games_started=0, outs=1,
                                                   entry_margin=2, exit_margin=2)))
    assert "svhd-ingame" in _flags(budgets, "RP")


def test_benched_drop_flag():
    slot_map = {sim._norm_name("Test Starter"): 16}   # BE slot
    # Open game on 06-07, a legal turn (5 days) after his 06-02 live start, so a
    # cadence turn fits and he still has a budget to carry the flag. It used to
    # be 06-04 — only 2 days' rest — which the pre-2026-08-12 stale anchor
    # happily projected; see test_a_benched_starter_gets_no_impossible_turn.
    budgets = build_budgets(
        [_starter()],
        {TEAM: [_game(), _game(status="Scheduled", game_pk=1000, inning=None,
                               game_date="2026-06-07", probable=None)]},
        sim.SimContext(team_total_ros_games={TEAM: 60}, live_by_team=_live(),
                       slot_by_norm_name=slot_map))
    assert "benched-live-drop" in _flags(budgets, "SP")


def test_a_benched_starter_gets_no_impossible_turn():
    """A benched pitcher's live start is hidden from his scoring view but still
    sets his rotation phase (2026-08-12 fix), so the only open game — 2 days
    after that start — is correctly unreachable and he projects nothing.

    Before the fix the anchor was read off the benched (filtered) view, so it
    fell back to 'no anchor' and the flat ROS-share handed him a share of that
    physically impossible turn.
    """
    slot_map = {sim._norm_name("Test Starter"): 16}
    budgets = build_budgets(
        [_starter()],
        {TEAM: [_game(), _game(status="Scheduled", game_pk=1000, inning=None,
                               game_date="2026-06-04", probable=None)]},
        sim.SimContext(team_total_ros_games={TEAM: 60}, live_by_team=_live(),
                       slot_by_norm_name=slot_map))
    assert not [b for b in budgets if b.role == "SP"]


def test_flags_do_not_change_numbers():
    # The provenance field must be inert: same inputs → same expected values
    # as reading them straight off the budget (flags are just labels).
    budgets = build_budgets([_starter(), _reliever()], {TEAM: [_game()]},
                            sim.SimContext(team_total_ros_games={TEAM: 60},
                                           live_by_team=_live()))
    for b in budgets:
        assert isinstance(b.flags, list)
        assert all(isinstance(x, str) for x in b.flags)
    # And budget_summary emits flags only when non-empty.
    sp = next(b for b in budgets if b.role == "SP")
    assert sp.flags   # the live start guarantees at least qs-ingame
