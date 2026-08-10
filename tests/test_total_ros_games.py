"""The ROS-share denominator must span the same window as ESPN's ROS
projections — the remaining MLB season, not just the remaining fantasy
regular season. Regression for the 2026-08-10 RP appearance inflation:
`compute` passed `last_reg` (week 22) as the upper bound while ESPN's ROS GP
covered through week 25, inflating every RP's appearance share by
games(→25)/games(→22) — ×1.76 by week 19, and >1.0 shares late (Gregory
Soto: ROS GP 26 / 24 truncated games × 6 = 6.5 projected appearances in a
6-game week). K, SVHD and innings scale with the share, so those inflated
with it.
"""
import sqlite3

from app import sim
from app.sim import STAT_PITCH_GP, STAT_OUTS, STAT_ER, STAT_SVHD, STAT_K, build_budgets

TEAM = 100
LAST_REG = 22       # last fantasy regular-season period
SCHED_END = 25      # last period in the stored schedule (MLB season end)


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE team_schedule (
            matchup_period_id INT, game_pk INT, game_date TEXT, pro_team_id INT,
            opponent_pro_team_id INT, is_home INT, probable_pitcher_mlbam_id INT,
            probable_pitcher_name TEXT, game_status TEXT,
            PRIMARY KEY (matchup_period_id, game_pk, pro_team_id))"""
    )
    return conn


def _seed(conn, *, games_per_period=6, first=19, last=SCHED_END, team=TEAM):
    pk = 0
    for period in range(first, last + 1):
        for _ in range(games_per_period):
            pk += 1
            conn.execute(
                "INSERT INTO team_schedule VALUES (?,?,?,?,99,1,NULL,NULL,'Scheduled')",
                (period, pk, f"2026-09-{period:02d}", team),
            )
    conn.commit()


def test_unbounded_counts_through_schedule_end():
    conn = _mem_db()
    _seed(conn)   # periods 19..25, 6 games each = 42
    assert sim.load_total_remaining_games(conn, 19) == {TEAM: 42}


def test_explicit_bound_still_respected():
    conn = _mem_db()
    _seed(conn)
    # periods 19..22 = 24 games — the old (truncating) call shape.
    assert sim.load_total_remaining_games(conn, 19, LAST_REG) == {TEAM: 24}


def test_from_period_excludes_past_periods():
    conn = _mem_db()
    _seed(conn)
    assert sim.load_total_remaining_games(conn, 24) == {TEAM: 12}


def test_postponed_rows_not_counted():
    conn = _mem_db()
    _seed(conn, first=25, last=25)   # 6 scheduled
    conn.execute(
        "INSERT INTO team_schedule VALUES (25, 999, '2026-09-25', ?, 99, 1, NULL, NULL, 'Postponed')",
        (TEAM,),
    )
    conn.commit()
    assert sim.load_total_remaining_games(conn, 19) == {TEAM: 6}


# ── The share itself, through build_budgets ──────────────────────────────


def _reliever(gp_ros=21, k_ros=23):
    return {
        "player_id": 2, "full_name": "Test Reliever", "pro_team_id": TEAM,
        "default_position_id": 1, "injury_status": "ACTIVE", "lineup_slot_id": 15,
        "ros_stats": {STAT_PITCH_GP: gp_ros, STAT_OUTS: gp_ros * 3,
                      STAT_ER: 8, STAT_SVHD: 10, STAT_K: k_ros},
    }


def _week(n_games=6):
    return {TEAM: [
        {"game_pk": i, "game_date": f"2026-08-1{i}", "game_status": "Scheduled",
         "current_inning": None, "inning_state": None,
         "probable_pitcher_name": None, "team_runs": None,
         "opponent_runs": None, "is_home": 1, "opponent_pro_team_id": 200}
        for i in range(n_games)
    ]}


def _rp_budget(budgets):
    return next(b for b in budgets if b.role == "RP")


def test_rp_share_is_gp_over_full_season_games():
    # 21 ROS appearances over 42 full-season remaining games = 0.5/game
    # → 3.0 appearances in a 6-game week (the Vesia shape, corrected).
    budgets = build_budgets([_reliever(gp_ros=21)], _week(6),
                            sim.SimContext(team_total_ros_games={TEAM: 42}))
    b = _rp_budget(budgets)
    assert abs(b.units - 3.0) < 1e-9
    assert "rp-apps-capped" not in b.flags


def test_truncated_denominator_would_inflate_and_now_caps():
    # The bug's inputs: ROS GP 26 vs a 24-game truncated denominator gave a
    # share > 1.0 → 6.5 appearances in a 6-game week. The physical backstop
    # now clamps to one appearance per team game and flags it.
    budgets = build_budgets([_reliever(gp_ros=26)], _week(6),
                            sim.SimContext(team_total_ros_games={TEAM: 24}))
    b = _rp_budget(budgets)
    assert abs(b.units - 6.0) < 1e-9
    assert "rp-apps-capped" in b.flags


def test_expected_k_scales_with_corrected_units():
    # exp K = (ros_k / gp_ros) × units; with the full-season denominator the
    # weekly K lands at per-appearance rate × ~3 appearances, not ~6.
    budgets = build_budgets([_reliever(gp_ros=21, k_ros=23)], _week(6),
                            sim.SimContext(team_total_ros_games={TEAM: 42}))
    b = _rp_budget(budgets)
    assert abs(b.expected[STAT_K] - (23 / 21) * 3.0) < 1e-9
