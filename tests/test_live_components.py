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

from app import espn_public, mlb, sim


# ───────────────────────── _norm_name matching ─────────────────────────

def test_norm_name_drops_middle_initial():
    # The 2026-06-09 José A. Ferrer case: MLB carries a middle initial the ESPN
    # roster omits — must still match (or his line goes unmatched in reconstruction).
    assert sim._norm_name("José A. Ferrer") == sim._norm_name("Jose Ferrer") == "joseferrer"

def test_injury_normalizer_matches_sim_normalizer():
    # espn_public._norm is the *write* key for player_injuries.norm_name; sim._norm_name
    # is the *read* key (sim.load_team_roster). They MUST produce identical keys or an
    # IL'd player whose injuries-feed name carries a middle initial/suffix silently
    # loses his injury_return_override and falls back to the fixed-days heuristic.
    # (Both now share app.names.norm_name; this guards against them diverging again.)
    for feed_name, roster_name in [
        ("José A. Ferrer", "Jose Ferrer"),    # middle initial in the feed, not the roster
        ("Daniel Lynch IV", "Daniel Lynch"),  # suffix in the feed, not the roster
        ("Lourdes Gurriel Jr.", "Lourdes Gurriel"),
        ("Cristopher Sánchez", "Cristopher Sanchez"),
    ]:
        assert espn_public._norm(feed_name) == sim._norm_name(roster_name), feed_name

def test_norm_name_drops_suffix():
    assert sim._norm_name("Daniel Lynch IV") == sim._norm_name("Daniel Lynch") == "daniellynch"
    assert sim._norm_name("Lourdes Gurriel Jr.") == sim._norm_name("Lourdes Gurriel")

def test_norm_name_strips_diacritics():
    assert sim._norm_name("Cristopher Sánchez") == sim._norm_name("Cristopher Sanchez")

def test_norm_name_keeps_full_middle_name():
    # only single-letter middle tokens are dropped, not real middle names
    assert sim._norm_name("Hyun Jin Ryu") == "hyunjinryu"

def test_norm_name_does_not_collapse_distinct_players():
    # surname-sharing players must stay distinct — first name is preserved, and a
    # multi-letter initials token ("J.D.") is not treated as a droppable middle init.
    assert sim._norm_name("J.D. Martinez") != sim._norm_name("Nick Martinez")
    assert sim._norm_name("J.D. Martinez") == "jdmartinez"

def test_norm_name_plain_and_empty():
    assert sim._norm_name("Gerrit Cole") == "gerritcole"
    assert sim._norm_name(None) == "" and sim._norm_name("") == ""


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


def test_parse_boxscore_still_in_flags_removed_batter():
    team = _mlmam = _mlbam_team()
    def _bat(pid, name, order, h=1):
        return {"person": {"id": pid, "fullName": name},
                "battingOrder": order,
                "stats": {"batting": {"atBats": 3, "hits": h}}}
    payload = {"teams": {"home": {
        "team": {"id": team},
        "batters": [201, 202, 300],
        "players": {
            # Slot 2: starter (200) was replaced by a sub (201) → starter is OUT.
            "ID201": _bat(201, "Starter Two", "200"),
            "ID202": _bat(202, "Sub Two", "201"),
            # Slot 3: unreplaced starter → still in.
            "ID300": _bat(300, "Starter Three", "300"),
        },
    }}}
    out = mlb.parse_boxscore(payload, game_pk=7)
    still = {b["name"]: b["still_in"] for b in out["batters"]}
    assert still == {"Starter Two": False, "Sub Two": True, "Starter Three": True}


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


def test_pitching_keeps_baseline_when_baseline_closer():
    # A reconstruction that lands FURTHER from the live scrape than the (stale)
    # baseline is suspect (likely bad attribution) → keep the baseline.
    baseline = {sim.STAT_OUTS: 60, sim.STAT_ER: 8,        # ERA 3.6, WHIP 3.25
                sim.STAT_P_H: 45, sim.STAT_P_BB: 20}
    lines = [_pitcher("Counted Ace", 30, 8, 20, 8)]      # recon 90 outs/16 ER → ERA 4.8
    slots = {sim._norm_name("Counted Ace"): PITCH_SLOT}
    scraped = {sim.STAT_ERA: 3.5, sim.STAT_WHIP: 3.2, sim.STAT_OPS: None}  # baseline 3.6 closer than recon 4.8
    state, decisions = sim.reconcile_live_components(
        baseline, pitcher_lines=lines, batter_lines=[],
        slot_by_norm_name=slots, scraped=scraped)
    pit = next(d for d in decisions if d["group"] == "pitching")
    assert pit["verdict"] == "baseline" and pit["accepted"] is False
    assert state[sim.STAT_OUTS] == 60 and state[sim.STAT_ER] == 8  # unchanged


def test_pitching_committed_when_closer_than_baseline():
    # The settle-bound fix (Bear Nation / WAR): the reconstruction missed a line so
    # it's outside tolerance, but it's still much nearer the live scrape than the
    # stale REST baseline → commit it rather than fall back to the worse number.
    baseline = {sim.STAT_OUTS: 30, sim.STAT_ER: 3,       # ERA 2.7 (stale, way low)
                sim.STAT_P_H: 8, sim.STAT_P_BB: 2}
    lines = [_pitcher("Counted Ace", 30, 6, 12, 4)]      # recon 60 outs/9 ER → ERA 4.05
    slots = {sim._norm_name("Counted Ace"): PITCH_SLOT}
    # scrape ERA 4.8 (truth, incl. a line the recon missed); recon 4.05 ≪ baseline 2.7 in distance.
    scraped = {sim.STAT_ERA: 4.8, sim.STAT_WHIP: (20 + 6) * 3 / 60, sim.STAT_OPS: None}
    state, decisions = sim.reconcile_live_components(
        baseline, pitcher_lines=lines, batter_lines=[],
        slot_by_norm_name=slots, scraped=scraped)
    pit = next(d for d in decisions if d["group"] == "pitching")
    assert pit["verdict"] == "closer" and pit["accepted"] is True
    assert state[sim.STAT_OUTS] == 60 and state[sim.STAT_ER] == 9  # reconstruction committed


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


# ── the double-count guard: scrape already banked the in-window QS ──

def test_in_progress_start_not_credited():
    # Same line but game still live → ingame model owns it, reconstruction must skip.
    baseline = {sim.STAT_QS: 2}
    lines = [_starter("Ace", outs=21, er=2, status="In Progress")]
    slots = {sim._norm_name("Ace"): PITCH_SLOT}
    state, _ = sim.reconcile_live_components(baseline, **_qs_args(lines, slots))
    assert state.get(sim.STAT_QS) == 2                 # unchanged

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

# ──────────────────── settled floor (box-archive, aged-out games) ────────────────────
# Period 10 window is 2026-06-01..06-07 (mlb.matchup_period_window). The floor counts
# QS/SVHD from in-period games with game_date < since_date, via pitcher_final_lines +
# that day's daily_lineups slots. Robust to *when* a credit was scrape-captured.

def _floor_db(*, lineups=(), final_lines=(), period_id=10, team=13):
    """lineups: (game_date, player_id, full_name, slot). final_lines: (game_date,
    name, gs, outs, er, sv, hld, final_at)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE matchups (id INT, matchup_period_id INT)")
    conn.execute("INSERT INTO matchups VALUES (60, ?)", (period_id,))
    conn.execute("CREATE TABLE players (id INT, full_name TEXT)")
    conn.execute("CREATE TABLE daily_lineups (game_date TEXT, fantasy_team_id INT, "
                 "player_id INT, lineup_slot_id INT, fetched_at TEXT)")
    conn.execute("CREATE TABLE pitcher_final_lines (game_date TEXT, name TEXT, "
                 "games_started INT, outs INT, er INT, sv INT, hld INT, final_at TEXT)")
    seen = set()
    for gd, pid, name, slot in lineups:
        if pid not in seen:
            conn.execute("INSERT INTO players VALUES (?,?)", (pid, name)); seen.add(pid)
        conn.execute("INSERT INTO daily_lineups VALUES (?,?,?,?,?)",
                     (gd, team, pid, slot, gd + "T12:00:00+00:00"))
    for gd, name, gs, outs, er, sv, hld, fin in final_lines:
        conn.execute("INSERT INTO pitcher_final_lines VALUES (?,?,?,?,?,?,?,?)",
                     (gd, name, gs, outs, er, sv, hld, fin))
    conn.commit()
    return conn


# ───────── live box-score persistence: duplicate personId tolerance ─────────
# Regression for the 2026-06-28 refresh-live crash: MLB statsapi repeated a
# personId within one game's `batters` array (game 824256 listed Matt Vierling
# & Ben Malgeri twice), so live_b/live_p can carry a duplicate (game_pk,
# mlbam_id) in a single tick. parse_boxscore does NOT dedup (by design — the
# repeated line is identical), so the DB write must be an idempotent upsert, not
# a plain INSERT that trips the (game_pk, mlbam_id) primary key. The SQL below
# mirrors cli.refresh_live's writes.

from app import db as _db

_LIVE_PITCHER_UPSERT = """
    INSERT INTO live_pitchers
        (game_pk, mlbam_id, name, pro_team_id, order_idx, is_last,
         games_started, outs, er, k, p_h, p_bb, sv, hld, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(game_pk, mlbam_id) DO UPDATE SET
        name=excluded.name, pro_team_id=excluded.pro_team_id,
        order_idx=excluded.order_idx, is_last=excluded.is_last,
        games_started=excluded.games_started, outs=excluded.outs,
        er=excluded.er, k=excluded.k, p_h=excluded.p_h,
        p_bb=excluded.p_bb, sv=excluded.sv, hld=excluded.hld,
        fetched_at=excluded.fetched_at
"""
_LIVE_BATTER_UPSERT = """
    INSERT INTO live_batters
        (game_pk, mlbam_id, name, pro_team_id, ab, h, b2, b3, hr, bb, hbp, sf, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(game_pk, mlbam_id) DO UPDATE SET
        name=excluded.name, pro_team_id=excluded.pro_team_id,
        ab=excluded.ab, h=excluded.h, b2=excluded.b2, b3=excluded.b3,
        hr=excluded.hr, bb=excluded.bb, hbp=excluded.hbp,
        sf=excluded.sf, fetched_at=excluded.fetched_at
"""


def _schema_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_db.SCHEMA)  # real DDL incl. PRIMARY KEY (game_pk, mlbam_id)
    for col, type_ in (("p_h", "INTEGER"), ("p_bb", "INTEGER"),
                       ("sv", "INTEGER"), ("hld", "INTEGER")):
        try:
            conn.execute(f"ALTER TABLE live_pitchers ADD COLUMN {col} {type_}")
        except sqlite3.OperationalError:
            pass
    return conn


def test_live_batter_upsert_tolerates_duplicate_personid():
    conn = _schema_db()
    # Same (game_pk=824256, mlbam_id=663837) line twice in one tick.
    row = (824256, 663837, "Matt Vierling", 100, 3, 1, 0, 0, 0, 0, 0, 0, "t0")
    conn.execute(_LIVE_BATTER_UPSERT, row)
    conn.execute(_LIVE_BATTER_UPSERT, row)  # would raise IntegrityError on plain INSERT
    got = conn.execute("SELECT mlbam_id, ab, h FROM live_batters").fetchall()
    assert len(got) == 1
    assert (got[0]["mlbam_id"], got[0]["ab"], got[0]["h"]) == (663837, 3, 1)


def test_live_pitcher_upsert_last_write_wins():
    conn = _schema_db()
    conn.execute(_LIVE_PITCHER_UPSERT,
                 (824256, 999, "Spot Starter", 100, 0, 1, 1, 15, 2, 6, 4, 1, 0, 0, "t0"))
    # A later tick re-fetches the same game with an advanced line → overwrites.
    conn.execute(_LIVE_PITCHER_UPSERT,
                 (824256, 999, "Spot Starter", 100, 0, 1, 1, 18, 3, 7, 5, 1, 0, 0, "t1"))
    got = conn.execute("SELECT outs, er, k, fetched_at FROM live_pitchers").fetchall()
    assert len(got) == 1
    assert (got[0]["outs"], got[0]["er"], got[0]["k"], got[0]["fetched_at"]) == (18, 3, 7, "t1")
