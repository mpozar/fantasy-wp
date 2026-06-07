"""Tests for live component reconstruction (app/sim.py + app/mlb.py).

Covers the pure pieces that beat ESPN's once-daily REST settle:
  - mlb.parse_boxscore: pitcher (incl. hits/walks) + batter line extraction.
  - sim.reconcile_live_components: daily-lineup attribution, the rate-validation
    guard (accept only when the reconstructed rate matches the live scraped
    rate), H/HR not double-counted, bench/IL & unrostered exclusion, two-way
    handling, and the no-live-data no-op.
  - sim.settle_boundary_date: the unsettled-window cutoff.
"""
import sqlite3
from datetime import datetime, timezone

from app import mlb, sim


# ───────────────────────── parse_boxscore ─────────────────────────

def _mlbam_team():
    return next(iter(mlb.MLBAM_TO_ESPN))


def test_parse_boxscore_pitchers_and_batters():
    team = _mlbam_team()
    espn_id = mlb.MLBAM_TO_ESPN[team]
    payload = {
        "teams": {
            "home": {
                "team": {"id": team},
                "pitchers": [101, 102],
                "batters": [201, 202, 203],
                "players": {
                    "ID101": {"person": {"id": 101, "fullName": "Ace Starter"},
                              "stats": {"pitching": {"gamesStarted": 1,
                                                     "inningsPitched": "6.2",
                                                     "earnedRuns": 2, "strikeOuts": 7,
                                                     "hits": 5, "baseOnBalls": 1}}},
                    "ID102": {"person": {"id": 102, "fullName": "Relief Guy"},
                              "stats": {"pitching": {"gamesStarted": 0,
                                                     "inningsPitched": "1.0",
                                                     "earnedRuns": 0, "strikeOuts": 2,
                                                     "hits": 0, "baseOnBalls": 1}}},
                    "ID201": {"person": {"id": 201, "fullName": "Lead Off"},
                              "stats": {"batting": {"atBats": 4, "hits": 2, "doubles": 1,
                                                    "triples": 0, "homeRuns": 0,
                                                    "baseOnBalls": 1, "hitByPitch": 0,
                                                    "sacFlies": 0}}},
                    "ID202": {"person": {"id": 202, "fullName": "Clean Up"},
                              "stats": {"batting": {"atBats": 3, "hits": 1, "doubles": 0,
                                                    "triples": 0, "homeRuns": 1,
                                                    "baseOnBalls": 0, "hitByPitch": 1,
                                                    "sacFlies": 1}}},
                    # No plate appearances → skipped (defensive sub).
                    "ID203": {"person": {"id": 203, "fullName": "Defensive Sub"},
                              "stats": {"batting": {"atBats": 0, "hits": 0,
                                                    "baseOnBalls": 0, "hitByPitch": 0,
                                                    "sacFlies": 0}}},
                },
            },
        },
    }
    out = mlb.parse_boxscore(payload, game_pk=999)

    assert len(out["pitchers"]) == 2
    ace = out["pitchers"][0]
    assert ace["outs"] == 20 and ace["er"] == 2 and ace["k"] == 7  # 6.2 IP = 20 outs
    assert ace["p_h"] == 5 and ace["p_bb"] == 1
    assert ace["is_last"] is False and ace["order_idx"] == 0
    assert ace["espn_team_id"] == espn_id

    assert len(out["batters"]) == 2  # defensive sub with 0 PA dropped
    names = {b["name"] for b in out["batters"]}
    assert names == {"Lead Off", "Clean Up"}
    cu = next(b for b in out["batters"] if b["name"] == "Clean Up")
    assert cu["hr"] == 1 and cu["hbp"] == 1 and cu["sf"] == 1


def test_parse_boxscore_skips_unknown_team():
    payload = {"teams": {"home": {"team": {"id": -999}, "pitchers": [1],
                                  "players": {"ID1": {"person": {"id": 1}}}}}}
    out = mlb.parse_boxscore(payload, game_pk=1)
    assert out == {"pitchers": [], "batters": []}


# ─────────────────────── reconcile guard ───────────────────────

PITCH_SLOT = 13            # SP — counts
HIT_SLOT = 3               # an infield slot — counts
BENCH = sim.NON_COUNTING_SLOTS  # {16, 17}


def _pitcher(name, outs, er, p_h, p_bb):
    return {"name": name, "outs": outs, "er": er, "p_h": p_h, "p_bb": p_bb}


def _starter(name, outs, er, status="Final"):
    return {"name": name, "outs": outs, "er": er, "p_h": 4, "p_bb": 1,
            "games_started": 1, "game_status": status}


def _batter(name, ab, h=0, b2=0, b3=0, hr=0, bb=0, hbp=0, sf=0):
    return {"name": name, "ab": ab, "h": h, "b2": b2, "b3": b3,
            "hr": hr, "bb": bb, "hbp": hbp, "sf": sf}


def test_pitching_accepted_when_rate_matches_scrape():
    # baseline (ESPN settled): 60 outs, 8 ER, 45 H, 20 BB.
    baseline = {sim.STAT_OUTS: 60, sim.STAT_ER: 8,
                sim.STAT_P_H: 45, sim.STAT_P_BB: 20}
    # live unsettled lines: a counted SP + a benched arm (must be ignored).
    lines = [_pitcher("Counted Ace", 30, 4, 20, 8),
             _pitcher("Benched Arm", 9, 5, 10, 3)]
    slots = {sim._norm_name("Counted Ace"): PITCH_SLOT,
             sim._norm_name("Benched Arm"): 16}
    # reconstructed: 90 outs, 12 ER → ERA 3.6; (65+28)*3/90 → WHIP 3.10.
    scraped = {sim.STAT_ERA: 12 * 27 / 90, sim.STAT_WHIP: (65 + 28) * 3 / 90,
               sim.STAT_OPS: None}
    state, decisions = sim.reconcile_live_components(
        baseline, pitcher_lines=lines, batter_lines=[],
        slot_by_norm_name=slots, scraped=scraped)
    pit = next(d for d in decisions if d["group"] == "pitching")
    assert pit["accepted"] is True
    assert pit["matched_lines"] == 1            # benched arm excluded
    assert state[sim.STAT_OUTS] == 90 and state[sim.STAT_ER] == 12
    assert state[sim.STAT_P_H] == 65 and state[sim.STAT_P_BB] == 28


def test_pitching_rejected_falls_back_to_baseline():
    baseline = {sim.STAT_OUTS: 60, sim.STAT_ER: 8,
                sim.STAT_P_H: 45, sim.STAT_P_BB: 20}
    lines = [_pitcher("Counted Ace", 30, 4, 20, 8)]
    slots = {sim._norm_name("Counted Ace"): PITCH_SLOT}
    # scraped ERA wildly different from reconstructed 3.6 → attribution suspect.
    scraped = {sim.STAT_ERA: 6.0, sim.STAT_WHIP: 3.10, sim.STAT_OPS: None}
    state, decisions = sim.reconcile_live_components(
        baseline, pitcher_lines=lines, batter_lines=[],
        slot_by_norm_name=slots, scraped=scraped)
    pit = next(d for d in decisions if d["group"] == "pitching")
    assert pit["accepted"] is False
    assert state[sim.STAT_OUTS] == 60 and state[sim.STAT_ER] == 8  # unchanged


def test_missing_scraped_rate_falls_back():
    baseline = {sim.STAT_OUTS: 60, sim.STAT_ER: 8,
                sim.STAT_P_H: 45, sim.STAT_P_BB: 20}
    lines = [_pitcher("Counted Ace", 30, 4, 20, 8)]
    slots = {sim._norm_name("Counted Ace"): PITCH_SLOT}
    scraped = {sim.STAT_ERA: None, sim.STAT_WHIP: None, sim.STAT_OPS: None}
    state, _ = sim.reconcile_live_components(
        baseline, pitcher_lines=lines, batter_lines=[],
        slot_by_norm_name=slots, scraped=scraped)
    assert state[sim.STAT_OUTS] == 60


def test_no_live_lines_is_noop():
    baseline = {sim.STAT_OUTS: 60, sim.STAT_ER: 8}
    state, decisions = sim.reconcile_live_components(
        baseline, pitcher_lines=[], batter_lines=[],
        slot_by_norm_name={}, scraped={})
    assert state == baseline
    assert all(d["accepted"] is False for d in decisions)


def test_ops_accepted_and_H_HR_not_double_counted():
    # H and HR are scored cats the scrape owns live → baseline already complete.
    baseline = {sim.STAT_AB: 100, sim.STAT_H: 30, sim.STAT_HR: 4,
                sim.STAT_2B: 5, sim.STAT_3B: 1, sim.STAT_B_BB: 12,
                sim.STAT_HBP: 2, sim.STAT_SF: 1}
    # A counted hitter's line includes h/hr — they must NOT be added to baseline.
    lines = [_batter("Counted Bat", ab=10, h=4, b2=2, b3=0, hr=2, bb=3, hbp=1, sf=0),
             _batter("Benched Bat", ab=8, h=4, hr=1)]   # benched → ignored
    slots = {sim._norm_name("Counted Bat"): HIT_SLOT,
             sim._norm_name("Benched Bat"): 16}
    # Build the expected reconstruction and derive the scraped OPS from it so the
    # test pins the *mechanism* (attribution + H/HR handling + accept gate).
    recon = dict(baseline)
    recon[sim.STAT_AB] = 110; recon[sim.STAT_2B] = 7
    recon[sim.STAT_B_BB] = 15; recon[sim.STAT_HBP] = 3
    scraped = {sim.STAT_OPS: sim.derive_ops(recon),
               sim.STAT_ERA: None, sim.STAT_WHIP: None}
    state, decisions = sim.reconcile_live_components(
        baseline, pitcher_lines=[], batter_lines=lines,
        slot_by_norm_name=slots, scraped=scraped)
    ops = next(d for d in decisions if d["group"] == "ops")
    assert ops["accepted"] is True and ops["matched_lines"] == 1
    assert state[sim.STAT_AB] == 110          # delta applied
    assert state[sim.STAT_2B] == 7
    assert state[sim.STAT_H] == 30            # NOT 30+4 — scrape owns H
    assert state[sim.STAT_HR] == 4            # NOT 4+2 — scrape owns HR


def test_unrostered_player_lines_ignored():
    baseline = {sim.STAT_OUTS: 60, sim.STAT_ER: 8,
                sim.STAT_P_H: 45, sim.STAT_P_BB: 20}
    # Line for a player not on this fantasy team's roster (not in slots map).
    lines = [_pitcher("Some Other Team Ace", 30, 0, 5, 1)]
    state, decisions = sim.reconcile_live_components(
        baseline, pitcher_lines=lines, batter_lines=[],
        slot_by_norm_name={}, scraped={sim.STAT_ERA: 3.6, sim.STAT_WHIP: 3.0})
    pit = next(d for d in decisions if d["group"] == "pitching")
    assert pit["matched_lines"] == 0 and pit["accepted"] is False
    assert state[sim.STAT_OUTS] == 60


def test_two_way_pitching_slot_counts_pitching_not_batting():
    # A two-way player slotted as a PITCHER: their pitching line counts, their
    # batting line does not (slot 13 ∉ HITTER_SLOT_IDS).
    baseline = {sim.STAT_OUTS: 0, sim.STAT_ER: 0, sim.STAT_P_H: 0, sim.STAT_P_BB: 0,
                sim.STAT_AB: 50, sim.STAT_H: 15, sim.STAT_HR: 3,
                sim.STAT_2B: 3, sim.STAT_3B: 0, sim.STAT_B_BB: 6,
                sim.STAT_HBP: 1, sim.STAT_SF: 0}
    pitcher_lines = [_pitcher("Two Way", 18, 1, 3, 1)]
    batter_lines = [_batter("Two Way", ab=4, h=2, hr=1, b2=1)]
    slots = {sim._norm_name("Two Way"): PITCH_SLOT}   # pitching slot
    scraped = {sim.STAT_ERA: 1 * 27 / 18, sim.STAT_WHIP: (3 + 1) * 3 / 18,
               sim.STAT_OPS: sim.derive_ops(baseline)}  # OPS unchanged (no hit counted)
    state, decisions = sim.reconcile_live_components(
        baseline, pitcher_lines=pitcher_lines, batter_lines=batter_lines,
        slot_by_norm_name=slots, scraped=scraped)
    pit = next(d for d in decisions if d["group"] == "pitching")
    ops = next(d for d in decisions if d["group"] == "ops")
    assert pit["accepted"] is True and state[sim.STAT_OUTS] == 18
    assert ops["matched_lines"] == 0           # batting line not counted
    assert state[sim.STAT_AB] == 50            # OPS components unchanged


# ───────────────────── settle boundary ─────────────────────

def test_settle_boundary_after_morning_settle():
    # 08:00 UTC: the 07:00 settle has absorbed the prior day → boundary = today.
    now = datetime(2026, 6, 6, 8, 0, tzinfo=timezone.utc)
    assert sim.settle_boundary_date(now) == "2026-06-06"


def test_settle_boundary_before_morning_settle():
    # 05:00 UTC (pre-settle): yesterday's slate not yet absorbed → boundary = yesterday.
    now = datetime(2026, 6, 6, 5, 0, tzinfo=timezone.utc)
    assert sim.settle_boundary_date(now) == "2026-06-05"


# ───────────────── integration: SQL loaders + apply ─────────────────

def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE team_schedule (matchup_period_id INT, game_pk INT, game_date TEXT,
            pro_team_id INT, game_status TEXT,
            PRIMARY KEY (matchup_period_id, game_pk, pro_team_id));
        CREATE TABLE live_pitchers (game_pk INT, mlbam_id INT, name TEXT, pro_team_id INT,
            order_idx INT, is_last INT, games_started INT, outs INT, er INT, k INT,
            p_h INT, p_bb INT, sv INT, hld INT, fetched_at TEXT,
            PRIMARY KEY (game_pk, mlbam_id));
        CREATE TABLE live_batters (game_pk INT, mlbam_id INT, name TEXT, pro_team_id INT,
            ab INT, h INT, b2 INT, b3 INT, hr INT, bb INT, hbp INT, sf INT,
            fetched_at TEXT, PRIMARY KEY (game_pk, mlbam_id));
        CREATE TABLE daily_lineups (game_date TEXT, fantasy_team_id INT, player_id INT,
            lineup_slot_id INT, fetched_at TEXT,
            PRIMARY KEY (game_date, fantasy_team_id, player_id));
        CREATE TABLE players (id INT PRIMARY KEY, full_name TEXT);
        """
    )
    return conn


def test_loaders_scope_to_unsettled_window_and_attribute_by_lineup():
    conn = _mem_db()
    # Two games: one unsettled (today), one already settled (older) → must be excluded.
    conn.execute("INSERT INTO team_schedule VALUES (10, 5001, '2026-06-06', 100, 'Final')")
    conn.execute("INSERT INTO team_schedule VALUES (10, 4001, '2026-06-04', 100, 'Final')")
    # Pitcher lines in both games for the same rostered pitcher.
    conn.execute("INSERT INTO live_pitchers VALUES "
                 "(5001, 1, 'Counted Ace', 100, 0, 1, 1, 30, 4, 7, 20, 8, 0, 0, 't')")
    conn.execute("INSERT INTO live_pitchers VALUES "
                 "(4001, 1, 'Counted Ace', 100, 0, 1, 1, 21, 9, 5, 15, 5, 0, 0, 't')")
    conn.execute("INSERT INTO players VALUES (1, 'Counted Ace')")
    conn.execute("INSERT INTO daily_lineups VALUES ('2026-06-06', 7, 1, 13, 't')")
    conn.commit()

    lines = sim.load_unsettled_lines(conn, since_date="2026-06-06")
    assert len(lines["pitchers"]) == 1            # settled game excluded
    assert lines["pitchers"][0]["outs"] == 30

    slots = sim.load_active_slots(conn, 7, since_date="2026-06-06", fallback_roster=[])
    assert slots[sim._norm_name("Counted Ace")] == 13

    baseline = {sim.STAT_OUTS: 60, sim.STAT_ER: 8, sim.STAT_P_H: 45, sim.STAT_P_BB: 20,
                sim.STAT_ERA: 12 * 27 / 90, sim.STAT_WHIP: (65 + 28) * 3 / 90}
    roster = [{"full_name": "Counted Ace", "lineup_slot_id": 13}]
    state, decisions = sim.apply_live_components(
        conn, 7, baseline, roster, lines, since_date="2026-06-06")
    pit = next(d for d in decisions if d["group"] == "pitching")
    assert pit["accepted"] is True
    assert state[sim.STAT_OUTS] == 90             # 60 + 30 (only the unsettled game)


def test_apply_falls_back_to_roster_slot_without_daily_snapshot():
    conn = _mem_db()
    conn.execute("INSERT INTO team_schedule VALUES (10, 5001, '2026-06-06', 100, 'Final')")
    conn.execute("INSERT INTO live_pitchers VALUES "
                 "(5001, 1, 'Counted Ace', 100, 0, 1, 1, 30, 4, 7, 20, 8, 0, 0, 't')")
    conn.commit()
    lines = sim.load_unsettled_lines(conn, since_date="2026-06-06")
    # No daily_lineups row → falls back to the roster's current slot.
    roster = [{"full_name": "Counted Ace", "lineup_slot_id": 13}]
    baseline = {sim.STAT_OUTS: 60, sim.STAT_ER: 8, sim.STAT_P_H: 45, sim.STAT_P_BB: 20,
                sim.STAT_ERA: 12 * 27 / 90, sim.STAT_WHIP: (65 + 28) * 3 / 90}
    state, decisions = sim.apply_live_components(
        conn, 7, baseline, roster, lines, since_date="2026-06-06")
    assert next(d for d in decisions if d["group"] == "pitching")["accepted"] is True
    assert state[sim.STAT_OUTS] == 90


# ───────────────── validate: lineup-capture health ─────────────────

def test_lineup_capture_check_flags_missing_snapshot():
    from app import validate as v
    conn = _mem_db()
    # A live game on the unsettled day, with box-score lines but NO daily_lineups.
    conn.execute("INSERT INTO team_schedule VALUES (10, 5001, '2026-06-06', 100, 'Final')")
    conn.execute("INSERT INTO live_pitchers VALUES "
                 "(5001, 1, 'A', 100, 0, 1, 1, 18, 1, 5, 3, 1, 0, 0, 't')")
    conn.commit()
    findings = v.check_live_lineup_capture(conn, "2026-06-06T20:00:00+00:00")
    assert {f.code for f in findings} == {"ANOM_LINEUP_SNAPSHOT_MISSING"}


def test_lineup_capture_check_quiet_when_snapshot_present():
    from app import validate as v
    conn = _mem_db()
    conn.execute("INSERT INTO team_schedule VALUES (10, 5001, '2026-06-06', 100, 'Final')")
    conn.execute("INSERT INTO live_pitchers VALUES "
                 "(5001, 1, 'A', 100, 0, 1, 1, 18, 1, 5, 3, 1, 0, 0, 't')")
    conn.execute("INSERT INTO daily_lineups VALUES ('2026-06-06', 7, 1, 13, 't')")
    conn.commit()
    assert v.check_live_lineup_capture(conn, "2026-06-06T20:00:00+00:00") == []


# ───────────────────── QS reconstruction (counting credit) ─────────────────────

def _qs_args(lines, slots):
    """reconcile() with rate validation disabled (scraped=None) so we isolate QS."""
    return dict(pitcher_lines=lines, batter_lines=[], slot_by_norm_name=slots,
                scraped={sim.STAT_ERA: None, sim.STAT_WHIP: None, sim.STAT_OPS: None})


def test_qs_credited_from_final_start():
    baseline = {sim.STAT_QS: 2}
    lines = [_starter("Ace", outs=21, er=2)]          # 7 IP, 2 ER → QS
    slots = {sim._norm_name("Ace"): PITCH_SLOT}
    state, decisions = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    qs = next(d for d in decisions if d["group"] == "qs")
    assert qs["accepted"] and qs["qs_added"] == 1
    assert state[sim.STAT_QS] == 3                     # additive to banked

def test_in_progress_start_not_credited():
    # Same line but game still live → ingame model owns it, reconstruction must skip.
    baseline = {sim.STAT_QS: 2}
    lines = [_starter("Ace", outs=21, er=2, status="In Progress")]
    slots = {sim._norm_name("Ace"): PITCH_SLOT}
    state, _ = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    assert state.get(sim.STAT_QS) == 2                 # unchanged

def test_non_qualifying_final_start_not_credited():
    baseline = {sim.STAT_QS: 2}
    lines = [_starter("Shelled", outs=15, er=5),       # <6 IP and >3 ER
             _starter("Decent", outs=18, er=4)]        # 6 IP but 4 ER
    slots = {sim._norm_name("Shelled"): PITCH_SLOT, sim._norm_name("Decent"): PITCH_SLOT}
    state, decisions = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    qs = next(d for d in decisions if d["group"] == "qs")
    assert qs["matched_lines"] == 2 and qs["qs_added"] == 0
    assert state.get(sim.STAT_QS) == 2

def test_benched_or_unrostered_starter_not_credited():
    baseline = {sim.STAT_QS: 2}
    lines = [_starter("Benched Ace", outs=21, er=1)]
    slots = {sim._norm_name("Benched Ace"): 16}        # bench slot
    state, _ = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    assert state.get(sim.STAT_QS) == 2

def test_reliever_line_not_counted_as_qs():
    baseline = {sim.STAT_QS: 1}
    lines = [{"name": "Long Man", "outs": 21, "er": 0, "p_h": 3, "p_bb": 0,
              "games_started": 0, "game_status": "Final"}]   # 7 IP but not a start
    slots = {sim._norm_name("Long Man"): PITCH_SLOT}
    state, _ = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    assert state.get(sim.STAT_QS) == 1


# ──────────────────── SVHD reconstruction (SV + HLD) ────────────────────

def _reliever(name, sv=0, hld=0, status="Final"):
    return {"name": name, "outs": 3, "er": 0, "p_h": 1, "p_bb": 0, "games_started": 0,
            "game_status": status, "sv": sv, "hld": hld}


def test_svhd_credited_from_final_reliever():
    baseline = {sim.STAT_SVHD: 5}
    lines = [_reliever("Closer", sv=1)]
    slots = {sim._norm_name("Closer"): PITCH_SLOT}
    state, decisions = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    svhd = next(d for d in decisions if d["group"] == "svhd")
    assert svhd["accepted"] and svhd["svhd_added"] == 1
    assert state[sim.STAT_SVHD] == 6                  # additive to banked

def test_svhd_hold_credited():
    baseline = {sim.STAT_SVHD: 5}
    lines = [_reliever("Setup Man", hld=1)]
    slots = {sim._norm_name("Setup Man"): PITCH_SLOT}
    state, _ = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    assert state[sim.STAT_SVHD] == 6

def test_svhd_ignores_blown_save():
    # Blown saves are NOT scored in this league — a stray `bs` key must not change SVHD.
    baseline = {sim.STAT_SVHD: 5}
    lines = [{**_reliever("Blew It"), "bs": 1}]   # bs present but irrelevant
    slots = {sim._norm_name("Blew It"): PITCH_SLOT}
    state, decisions = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    svhd = next(d for d in decisions if d["group"] == "svhd")
    assert svhd["svhd_added"] == 0
    assert state.get(sim.STAT_SVHD) == 5

def test_svhd_in_progress_not_credited():
    baseline = {sim.STAT_SVHD: 5}
    lines = [_reliever("Closer", sv=1, status="In Progress")]
    slots = {sim._norm_name("Closer"): PITCH_SLOT}
    state, _ = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    assert state.get(sim.STAT_SVHD) == 5             # ingame model owns it

def test_svhd_unrostered_or_benched_not_credited():
    baseline = {sim.STAT_SVHD: 5}
    lines = [_reliever("Benched Closer", sv=1)]
    slots = {sim._norm_name("Benched Closer"): 16}   # bench slot
    state, _ = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    assert state.get(sim.STAT_SVHD) == 5

def test_svhd_additive_over_multiple_relievers():
    baseline = {sim.STAT_SVHD: 5}
    lines = [_reliever("A", sv=1), _reliever("B", hld=1)]
    slots = {sim._norm_name(n): PITCH_SLOT for n in ("A", "B")}
    state, _ = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    assert state[sim.STAT_SVHD] == 5 + 2            # SV + HLD
