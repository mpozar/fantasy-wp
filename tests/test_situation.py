"""`_resolve_pitcher_situation` — the single per-pitcher state resolution.

Role classification (incl. spot-starter promotion), the benched schedule view,
and the live-line/exited state used to be derived independently in five places;
most live-credit incidents were those derivations disagreeing. These tests pin
the resolver's state machine case by case.
"""

from app import sim
from app.sim import (
    STAT_GS, STAT_PITCH_GP, STAT_OUTS, STAT_ER, STAT_QS, STAT_K, STAT_SVHD,
    SimContext, _resolve_pitcher_situation,
)

TEAM = 100


def _starter(name="Test Starter"):
    return {
        "player_id": 1, "full_name": name, "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 15,
        "ros_stats": {STAT_GS: 20, STAT_PITCH_GP: 20, STAT_OUTS: 360,
                      STAT_ER: 50, STAT_QS: 10, STAT_K: 200},
    }


def _swingman(name="Spot Starter"):
    # RP by season ratio (GS 3 / GP 30) — promotion cases flip him to SP.
    return {
        "player_id": 7, "full_name": name, "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 13,
        "ros_stats": {STAT_GS: 3, STAT_PITCH_GP: 30, STAT_OUTS: 150,
                      STAT_ER: 26, STAT_QS: 1, STAT_K: 40},
    }


def _game(status="In Progress", probable=None, game_pk=999,
          game_date="2026-06-02", inning=6):
    return {
        "game_pk": game_pk, "game_date": game_date, "game_status": status,
        "current_inning": inning, "inning_state": "Top",
        "probable_pitcher_name": probable, "team_runs": 3, "opponent_runs": 1,
        "is_home": 1, "opponent_pro_team_id": 200,
    }


def _live(name, is_last=1, games_started=1, game_pk=999):
    return {TEAM: {sim._norm_name(name): dict(
        game_pk=game_pk, name=name, is_last=is_last,
        games_started=games_started, outs=18, er=1, k=5)}}


def _resolve(p, games, **ctx_kw):
    return _resolve_pitcher_situation(p, {TEAM: games}, SimContext(**ctx_kw))


# ── Role classification ────────────────────────────────────────────────

def test_ratio_sp():
    sit = _resolve(_starter(), [_game(status="Scheduled")])
    assert sit.role == "SP" and sit.ratio_sp and not sit.promoted


def test_ratio_rp_stays_rp_without_promotion_trigger():
    sit = _resolve(_swingman(), [_game(status="Scheduled")])
    assert sit.role == "RP" and not sit.promoted


def test_promoted_by_announced_probable():
    sit = _resolve(_swingman(), [_game(status="Scheduled", probable="Spot Starter")])
    assert sit.role == "SP" and sit.promoted and not sit.ratio_sp


def test_final_game_probable_does_not_promote():
    # A Final game he already started is banked — promoting on it would strip
    # a swingman's remaining relief (the Role-classification caveat).
    sit = _resolve(_swingman(), [_game(status="Final", probable="Spot Starter")])
    assert sit.role == "RP" and not sit.promoted


def test_promoted_by_live_start():
    sit = _resolve(_swingman(), [_game()],
                   live_by_team=_live("Spot Starter"))
    assert sit.role == "SP" and sit.promoted


# ── Live-line state ────────────────────────────────────────────────────

def test_no_live_line():
    sit = _resolve(_starter(), [_game()])
    assert sit.live is None
    assert not sit.has_live_start and not sit.exited
    assert not sit.live_start_in_progress


def test_live_start_still_pitching():
    sit = _resolve(_starter(), [_game()], live_by_team=_live("Test Starter"))
    assert sit.has_live_start and not sit.exited
    assert sit.live_start_in_progress


def test_live_start_exited():
    sit = _resolve(_starter(), [_game()],
                   live_by_team=_live("Test Starter", is_last=0))
    assert sit.has_live_start and sit.exited
    assert sit.live_start_in_progress   # game still live → QS override acts


def test_live_line_final_game_not_in_progress():
    sit = _resolve(_starter(), [_game(status="Final")],
                   live_by_team=_live("Test Starter", is_last=0))
    assert sit.has_live_start
    assert not sit.live_start_in_progress   # Final → reconstruction owns it


def test_reliever_live_line_is_not_a_start():
    sit = _resolve(_swingman(), [_game()],
                   live_by_team=_live("Spot Starter", games_started=0))
    assert sit.live is not None
    assert not sit.has_live_start and not sit.exited
    assert not sit.live_start_in_progress


# ── Benched schedule view ──────────────────────────────────────────────

def test_benched_drops_inprogress_keeps_scheduled():
    slots = {sim._norm_name("Test Starter"): 16}   # BE
    games = [_game(), _game(status="Scheduled", game_pk=1000,
                            game_date="2026-06-04")]
    sit = _resolve(_starter(), games, slot_by_norm_name=slots,
                   live_by_team=_live("Test Starter"))
    assert sit.benched_today
    assert [g["game_pk"] for g in sit.sched[TEAM]] == [1000]
    # His live start is in a game his schedule view no longer contains.
    assert not sit.live_start_in_progress


def test_active_slot_keeps_schedule_identity():
    slots = {sim._norm_name("Test Starter"): 15}   # active pitching slot
    sched = {TEAM: [_game()]}
    sit = _resolve_pitcher_situation(_starter(), sched,
                                     SimContext(slot_by_norm_name=slots))
    assert not sit.benched_today
    assert sit.sched is sched   # no per-player copy unless the gate fires
