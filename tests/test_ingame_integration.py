"""Integration tests: in-game QS/SVHD flowing through `sim.build_budgets`.

Builds mock rosters + an in-progress `schedule_by_team` + `live_by_team` and
checks the resulting budget's expected[QS]/expected[SVHD] reflect the in-game
projection (not the rate-based estimate). This is the "mock data for in-progress
games" harness — exercises the real wiring without a live fetch.
"""

from app import sim
from app.sim import (
    STAT_GS, STAT_PITCH_GP, STAT_OUTS, STAT_ER, STAT_QS, STAT_K, STAT_SVHD,
    build_budgets,
)

TEAM = 100


def _starter():
    # 20 GS, 360 outs (18/start), 50 ER, 10 QS (0.50 rate), 200 K.
    return {
        "player_id": 1, "full_name": "Test Starter", "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 15,
        "ros_stats": {STAT_GS: 20, STAT_PITCH_GP: 20, STAT_OUTS: 360,
                      STAT_ER: 50, STAT_QS: 10, STAT_K: 200},
    }


def _reliever():
    return {
        "player_id": 2, "full_name": "Test Closer", "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 15,
        "ros_stats": {STAT_PITCH_GP: 60, STAT_OUTS: 180, STAT_ER: 25,
                      STAT_SVHD: 30, STAT_K: 80},
    }


def _game(status="In Progress", inning=6, state="Top", probable="Test Starter",
          team_runs=3, opp_runs=1):
    return {
        "game_pk": 999, "game_date": "2026-06-02", "game_status": status,
        "current_inning": inning, "inning_state": state,
        "probable_pitcher_name": probable, "team_runs": team_runs,
        "opponent_runs": opp_runs, "is_home": 1, "opponent_pro_team_id": 200,
    }


def _live(**kw):
    base = dict(game_pk=999, name="Test Starter", is_last=1, games_started=1,
                outs=18, er=1, k=5)
    base.update(kw)
    return {TEAM: {sim._norm_name(base["name"]): base}}


def _qs(roster, schedule, live):
    budgets = build_budgets(roster, schedule, team_total_ros_games={TEAM: 60},
                            live_by_team=live)
    return next(b.expected.get(STAT_QS, 0.0) for b in budgets if b.role == "SP")


def _svhd(roster, schedule, live):
    budgets = build_budgets(roster, schedule, team_total_ros_games={TEAM: 60},
                            live_by_team=live)
    return next(b.expected.get(STAT_SVHD, 0.0) for b in budgets if b.role == "RP")


# ── QS through build_budgets ──────────────────────────────────────────────────

def test_qs_no_live_data_uses_rate_estimate():
    # No live line → rate-based: qs_rate(0.5) × a fractional in-progress unit → small.
    qs = _qs([_starter()], {TEAM: [_game()]}, {})
    assert 0.0 < qs < 0.3


def test_qs_still_in_threshold_met_overrides_high():
    # Live: 6 IP / 1 ER, still pitching → QS projection ~0.95, far above the
    # rate-based value the same game would otherwise contribute.
    qs = _qs([_starter()], {TEAM: [_game()]}, _live(outs=18, er=1, is_last=1))
    assert qs > 0.8


def test_qs_exited_qualified_is_one():
    qs = _qs([_starter()], {TEAM: [_game()]}, _live(outs=18, er=1, is_last=0))
    assert qs == 1.0


def test_qs_exited_shelled_is_zero():
    qs = _qs([_starter()], {TEAM: [_game()]}, _live(outs=12, er=5, is_last=0))
    assert qs == 0.0


# ── SVHD through build_budgets ────────────────────────────────────────────────

def test_svhd_currently_in_save_spot_uses_conversion():
    live = {TEAM: {sim._norm_name("Test Closer"): dict(
        game_pk=999, name="Test Closer", is_last=1, games_started=0,
        outs=1, er=0, k=1)}}
    svhd = _svhd([_reliever()], {TEAM: [_game(inning=9, probable=None)]}, live)
    assert svhd == 0.85       # DEFAULT_SVHD_CONVERSION (margin 2 = save spot)


def test_svhd_exited_with_save_is_one():
    live = {TEAM: {sim._norm_name("Test Closer"): dict(
        game_pk=999, name="Test Closer", is_last=0, games_started=0,
        outs=3, er=0, k=2)}}
    svhd = _svhd([_reliever()], {TEAM: [_game(inning=9, probable=None)]}, live)
    assert svhd == 1.0


def test_svhd_not_entered_blowout_gated_down():
    # No live line (hasn't pitched), team up 8 in the 8th → game-script gate
    # cuts the SVHD share well below the un-gated rate estimate.
    base = _svhd([_reliever()], {TEAM: [_game(inning=8, probable=None,
                                              team_runs=10, opp_runs=2)]}, {})
    ungated = _svhd([_reliever()], {TEAM: [_game(inning=8, probable=None,
                                                 team_runs=3, opp_runs=2)]}, {})
    assert base < ungated


# ── save/hold judged from ENTRY/EXIT margins, not the live score (Bugs 1–3) ──

def _rp_live(**kw):
    base = dict(game_pk=999, name="Test Closer", is_last=0, games_started=0,
                outs=3, er=0, k=2)
    base.update(kw)
    return {TEAM: {sim._norm_name(base["name"]): base}}


def test_svhd_hold_locked_despite_later_blowout():
    # Entered in a save situation (margin 2), exited still leading; the team THEN
    # pads it to a blowout (live margin 8). The earned hold must stay 1.0 — padding a
    # lead never un-earns it (Bug 1: was judged from the live margin → dropped to 0).
    live = _rp_live(is_last=0, entry_margin=2, exit_margin=4)
    svhd = _svhd([_reliever()], {TEAM: [_game(inning=9, probable=None,
                                              team_runs=10, opp_runs=2)]}, live)
    assert svhd == 1.0


def test_svhd_hold_locked_when_later_reliever_blows_lead():
    # Exited with the lead (exit_margin 2); a LATER reliever then loses it (live
    # margin −1). A hold is credited even if the team later loses → must stay 1.0
    # (Bug 2: lead_intact was read from the live margin → wrongly erased).
    live = _rp_live(is_last=0, entry_margin=2, exit_margin=2)
    svhd = _svhd([_reliever()], {TEAM: [_game(inning=9, probable=None,
                                              team_runs=4, opp_runs=5)]}, live)
    assert svhd == 1.0


def test_svhd_not_credited_if_entered_outside_save_situation():
    # Entered with a 6-run lead (not a save situation) → no hold, judged from the
    # ENTRY margin even though he exited leading.
    live = _rp_live(is_last=0, entry_margin=6, exit_margin=6)
    svhd = _svhd([_reliever()], {TEAM: [_game(inning=9, probable=None,
                                              team_runs=8, opp_runs=2)]}, live)
    assert svhd == 0.0


def test_svhd_not_credited_if_exited_trailing():
    # Entered in a save spot but surrendered the lead before exiting (exit_margin −1).
    live = _rp_live(is_last=0, entry_margin=2, exit_margin=-1)
    svhd = _svhd([_reliever()], {TEAM: [_game(inning=9, probable=None,
                                              team_runs=3, opp_runs=4)]}, live)
    assert svhd == 0.0


def test_svhd_spot_starter_skips_save_hold_override():
    # The Melton case: an RP-classified pitcher making a SPOT START (games_started=1)
    # must NOT get a phantom SV/HLD. Same exited-with-lead line: as a reliever it's a
    # 1.0 hold; flip games_started and the override is skipped (no in-game credit).
    g = {TEAM: [_game(inning=9, probable=None, team_runs=5, opp_runs=3)]}
    as_reliever = _svhd([_reliever()], g,
                        _rp_live(is_last=0, games_started=0, entry_margin=2, exit_margin=2))
    as_starter = _svhd([_reliever()], g,
                       _rp_live(is_last=0, games_started=1, outs=15, entry_margin=2, exit_margin=2))
    assert as_reliever == 1.0
    assert as_starter < as_reliever     # guard suppressed the phantom save/hold


def test_svhd_falls_back_to_live_margin_without_appearance_record():
    # No entry/exit captured (entry tick missed) → fall back to the live margin so
    # behavior never regresses below today's. Live margin 2 = save spot, exited → 1.0.
    live = _rp_live(is_last=0)   # no entry_margin/exit_margin keys
    svhd = _svhd([_reliever()], {TEAM: [_game(inning=9, probable=None,
                                              team_runs=3, opp_runs=1)]}, live)
    assert svhd == 1.0


# ── seam: a deep in-progress starter (units≈0) must keep his earned QS ──

def test_deep_inprogress_starter_keeps_qs_credit():
    """A starter who pitched past his exit inning has units≈0, so _make_budget
    would drop his SP budget — but his game is still in progress and he's earned a
    QS (21 outs, 1 ER). The credit must survive via a kept minimal budget + the
    in-game override, not vanish into the in-progress→settle seam (the Yamamoto
    04:15→07:00 case)."""
    roster = [_starter()]
    schedule = {TEAM: [_game(status="In Progress", inning=8)]}   # deep game
    live = _live(is_last=0, outs=21, er=1)                       # exited, 7 IP, 1 ER → QS
    budgets = build_budgets(roster, schedule, team_total_ros_games={TEAM: 60},
                            live_by_team=live)
    sp = [b for b in budgets if b.role == "SP"]
    assert sp, "deep in-progress starter's budget must be kept (else QS is lost)"
    assert sp[0].expected.get(STAT_QS, 0.0) > 0.9   # earned QS credited live

def test_no_minimal_budget_without_live_start():
    """The minimal-budget rescue is scoped to a live in-progress start — a deep
    starter whose game is already Final isn't resurrected here (the Final-only QS
    reconstruction owns that), so no spurious SP budget."""
    roster = [_starter()]
    schedule = {TEAM: [_game(status="Final", inning=9)]}
    live = {TEAM: {sim._norm_name("Test Starter"):
                   dict(game_pk=999, name="Test Starter", is_last=0,
                        games_started=1, outs=21, er=1, k=5)}}
    budgets = build_budgets(roster, schedule, team_total_ros_games={TEAM: 60},
                            live_by_team=live)
    # Final game → load path would exclude it live; here live_by_team is passed but
    # the game is Final, so _has_live_inprogress_start is False → no minimal budget.
    assert not [b for b in budgets if b.role == "SP"]
