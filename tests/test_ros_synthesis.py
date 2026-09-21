"""Synthesized ROS blocks for players ESPN's ROS model doesn't cover.

The incident class: ESPN's ROS projections are preseason-anchored, so a
late-season call-up has NO split=6 block and used to get no budget of any
kind — Leo Bernal (2026-09-17) projected zero as a starting C, and Kade
Anderson's un-projected live QS (2026-09-20, m144) hid the QS-tie odds that
decided a semifinal (the model had Norsemen winning QS in 10,000/10,000 sims
through his whole 7 IP / 0 ER start). `espn.synthesize_ros_from_actuals`
builds a ROS-shaped block from season actuals; these tests cover the pure
function, the fetch wiring, and the Anderson scenario end-to-end through
`sim.build_budgets` + the in-game QS override.
"""

import pytest

from app import espn, sim
from app.espn import synthesize_ros_from_actuals
from app.sim import STAT_GS, STAT_OUTS, STAT_QS, STAT_K, build_budgets

TEAM = 100


# ── The pure function ────────────────────────────────────────────────────────

def _anderson_actuals():
    # A September call-up SP: 10 GS, 10 GP, 190 outs, 20 ER, 62 K, 5 QS.
    # No stat 83 — ESPN omits it for save-less arms (absent means zero).
    return {"32": 10, "33": 10, "34": 190, "37": 48, "39": 15,
            "45": 20, "48": 62, "63": 5}


def test_pitcher_rates_are_scale_invariant():
    out = synthesize_ros_from_actuals(_anderson_actuals(), remaining_frac=0.1)
    assert out is not None and out["33"] > 0
    # per-start / per-out rates survive the scaling exactly
    assert out["34"] / out["33"] == pytest.approx(19.0)   # outs per start
    assert out["63"] / out["33"] == pytest.approx(0.5)    # QS rate
    assert out["48"] / out["34"] == pytest.approx(62 / 190)  # K per out


def test_missing_svhd_materialized_as_zero_for_pitchers():
    out = synthesize_ros_from_actuals(_anderson_actuals(), remaining_frac=0.1)
    assert out["83"] == 0.0


def test_reliever_svhd_rate_preserved():
    out = synthesize_ros_from_actuals(
        {"32": 40, "34": 120, "45": 12, "48": 45, "83": 12}, remaining_frac=0.1)
    assert out["83"] / out["32"] == pytest.approx(12 / 40)


def test_hitter_per_game_rates_preserved_and_no_pitching_ids():
    out = synthesize_ros_from_actuals(
        {"81": 20, "0": 70, "1": 21, "5": 3, "20": 10, "23": 2},
        remaining_frac=0.1)
    assert out["1"] / out["81"] == pytest.approx(21 / 20)
    assert "32" not in out and "83" not in out


def test_rate_stats_never_emitted():
    acts = {**_anderson_actuals(), "47": 3.20, "41": 1.10, "18": 0.750}
    out = synthesize_ros_from_actuals(acts, remaining_frac=0.1)
    assert "47" not in out and "41" not in out and "18" not in out


def test_no_usable_actuals_returns_none():
    assert synthesize_ros_from_actuals(None, 0.1) is None
    assert synthesize_ros_from_actuals({}, 0.1) is None
    assert synthesize_ros_from_actuals({"45": 3}, 0.1) is None  # ER but no G


def test_last_day_of_season_still_positive():
    # remaining_frac 0 (season's final day) must not zero the block out — the
    # floor keeps classification and rates alive.
    out = synthesize_ros_from_actuals(_anderson_actuals(), remaining_frac=0.0)
    assert out["33"] > 0
    assert out["34"] / out["33"] == pytest.approx(19.0)


# ── Fetch wiring (monkeypatched _get) ────────────────────────────────────────

def _payload(player_stats_blocks):
    return {
        "status": {"currentMatchupPeriod": 24, "finalScoringPeriod": 187},
        "scoringPeriodId": 170,
        "seasonId": 2026,
        "teams": [{
            "id": 3,
            "roster": {"entries": [
                {"lineupSlotId": 13, "status": "NORMAL",
                 "playerPoolEntry": {"player": {
                     "id": 5198748, "fullName": "Kade Anderson",
                     "proTeamId": 12, "defaultPositionId": 1,
                     "eligibleSlots": [13, 14], "injuryStatus": "ACTIVE",
                     "stats": player_stats_blocks}}},
            ]},
        }],
    }


def test_fetch_synthesizes_for_missing_ros_block(monkeypatch):
    payload = _payload([
        # actuals only — no split=6 block anywhere
        {"statSourceId": 0, "statSplitTypeId": 0, "seasonId": 2026,
         "stats": _anderson_actuals()},
    ])
    monkeypatch.setattr(espn, "_get", lambda views: payload)
    snap = espn.fetch_rosters_and_projections()
    rows = {r["stat_id"]: r["value"] for r in snap["projections"]
            if r["player_id"] == 5198748}
    assert rows and rows[33] > 0
    assert rows[34] / rows[33] == pytest.approx(19.0)
    assert snap["synthesized_ros"] == ["Kade Anderson"]


def test_fetch_leaves_real_ros_blocks_alone(monkeypatch):
    payload = _payload([
        {"statSourceId": 1, "statSplitTypeId": 6, "seasonId": 2026,
         "stats": {"33": 4, "32": 4, "34": 72, "45": 8, "48": 24, "63": 2}},
        {"statSourceId": 0, "statSplitTypeId": 0, "seasonId": 2026,
         "stats": _anderson_actuals()},
    ])
    monkeypatch.setattr(espn, "_get", lambda views: payload)
    snap = espn.fetch_rosters_and_projections()
    rows = {r["stat_id"]: r["value"] for r in snap["projections"]
            if r["player_id"] == 5198748}
    assert rows[34] == 72          # ESPN's own value, not a synthesized one
    assert snap["synthesized_ros"] == []


def test_fetch_undebuted_player_stays_invisible(monkeypatch):
    payload = _payload([])         # no stats blocks at all
    monkeypatch.setattr(espn, "_get", lambda views: payload)
    snap = espn.fetch_rosters_and_projections()
    assert [r for r in snap["projections"] if r["player_id"] == 5198748] == []
    assert snap["synthesized_ros"] == []


# ── End to end: the Anderson scenario through build_budgets ──────────────────

def _synth_rookie():
    ros = synthesize_ros_from_actuals(_anderson_actuals(), remaining_frac=0.05)
    return {
        "player_id": 5198748, "full_name": "Kade Anderson", "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 13,
        "ros_stats": {int(k): v for k, v in ros.items()},
    }


def _game(status, inning=None, probable="Kade Anderson"):
    return {
        "game_pk": 999, "game_date": "2026-09-20", "game_status": status,
        "current_inning": inning, "inning_state": "Top" if inning else None,
        "probable_pitcher_name": probable, "team_runs": 2, "opponent_runs": 1,
        "is_home": 1, "opponent_pro_team_id": 200,
    }


def _budget(schedule, live):
    bs = build_budgets([_synth_rookie()], schedule, sim.SimContext(
        team_total_ros_games={TEAM: 6}, live_by_team=live))
    return next((b for b in bs if b.name == "Kade Anderson"), None)


def test_synthesized_rookie_projects_his_announced_start():
    b = _budget({TEAM: [_game("Scheduled")]}, {})
    assert b is not None and b.role == "SP"
    assert b.expected.get(STAT_QS, 0) > 0.2
    assert 0 < b.expected.get(STAT_K, 0) < 9


def test_synthesized_rookie_live_qs_projects_midgame():
    # 2026-09-20 as it should have run: 15 outs / 0 ER, still pitching — the
    # in-game model must show a live QS probability, not the flat zero it had.
    live = {TEAM: {sim._norm_name("Kade Anderson"):
                   dict(game_pk=999, name="Kade Anderson", is_last=1,
                        games_started=1, outs=15, er=0, k=6)}}
    b = _budget({TEAM: [_game("In Progress", inning=6)]}, live)
    assert b is not None and b.role == "SP"
    assert b.expected.get(STAT_QS, 0) > 0.5


def test_synthesized_rookie_exited_qs_is_locked_to_one():
    # The owner's second point (2026-09-21): once the starter exits past the
    # threshold the QS is fully determined even mid-game — 21 outs / 0 ER,
    # a later pitcher has appeared (is_last=0) → exactly 1.0 supplied live.
    live = {TEAM: {sim._norm_name("Kade Anderson"):
                   dict(game_pk=999, name="Kade Anderson", is_last=0,
                        games_started=1, outs=21, er=0, k=7)}}
    b = _budget({TEAM: [_game("In Progress", inning=8)]}, live)
    assert b is not None
    assert b.expected.get(STAT_QS, 0) == pytest.approx(1.0)
