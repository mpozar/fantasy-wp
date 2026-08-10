"""ANOM_CALIBRATION_JUMP — the recurring projected-vs-actual alarm.

This check exists to close a structural blind spot: every other check in
`validate.py` is a change detector, a freshness detector, or an internal
invariant, so a large-but-STABLE bias in the model's inputs fires nothing. The
2026-08-10 RP-appearance inflation (grew to ×1.76 over a season) and the QS rate
bias (+28%) both ran with zero flags.

The tests that matter here are the two failure modes a threshold check has:
firing on the known-benign baseline (which buries real flags — the
INV_SITE_QS_OVERCREDIT lesson) and staying silent on a real regression.
"""
import sqlite3

import pytest

from app import calibration as calib
from app import validate as v


def _series(weeks: dict[int, float], stat: int = 48) -> dict[int, dict[int, float]]:
    return {stat: dict(weeks)}


def _jumped(series, monkeypatch) -> list:
    """Run check_calibration against a synthetic per-week bias series."""
    monkeypatch.setattr(calib, "collect", lambda conn, **kw: ["sentinel"])
    monkeypatch.setattr(calib, "weekly_bias", lambda obs: series)
    return v.check_calibration(object())


# ── it must not fire on a stable baseline, however BIASED that baseline is ──

def test_quiet_on_a_large_but_stable_bias(monkeypatch):
    # SVHD really did sit near +50% all season, most of it a deliberate
    # modelling choice. A level-based check would fire forever and bury
    # everything else; this one must say nothing.
    s = _series({10: .52, 11: .48, 12: .50, 13: .47, 14: .51, 16: .49, 17: .53, 18: .50})
    assert _jumped(s, monkeypatch) == []


def test_quiet_on_ordinary_week_to_week_wobble(monkeypatch):
    s = _series({10: .19, 11: .11, 12: .12, 13: .11, 14: .21, 16: .24, 17: .18, 18: .13})
    assert _jumped(s, monkeypatch) == []


def test_quiet_on_a_noisy_low_event_category(monkeypatch):
    # HR's real series ranged -17%..+41%; a tolerance tuned for a stable
    # category would false-fire here every few weeks.
    s = _series({10: -.04, 11: -.17, 12: -.13, 13: .41, 14: -.07, 16: .05, 17: .29,
                 18: .04}, stat=5)
    assert _jumped(s, monkeypatch) == []


# ── ...and it must fire when the latest week genuinely departs ──

def test_fires_when_the_latest_week_jumps(monkeypatch):
    s = _series({10: .10, 11: .11, 12: .09, 13: .10, 14: .12, 16: .11, 17: .10, 18: .95})
    out = _jumped(s, monkeypatch)
    assert len(out) == 1
    assert out[0].code == "ANOM_CALIBRATION_JUMP"
    assert out[0].severity == "warn"
    assert out[0].matchup_id is None          # league-level, not per-matchup
    assert "K" in out[0].detail


def test_fires_downward_too(monkeypatch):
    # An over-correction (a fix overshooting) is as much a regression.
    s = _series({10: .40, 11: .42, 12: .38, 13: .41, 14: .39, 16: .40, 17: .41, 18: -.40})
    assert len(_jumped(s, monkeypatch)) == 1


def test_detail_carries_the_trend_as_a_mechanism_hint(monkeypatch):
    # Growing ⇒ suspect a span/denominator bug; flat ⇒ suspect a rate. This is
    # the signal that identified the RP denominator, so it must reach the triager.
    s = _series({10: .10, 11: .11, 12: .09, 13: .10, 14: .12, 16: .11, 17: .10, 18: .95})
    detail = _jumped(s, monkeypatch)[0].detail
    assert "trend" in detail and "/wk" in detail
    assert "baseline" in detail


def test_reports_every_offending_category_in_one_finding(monkeypatch):
    # Flags dedupe on code+matchup+date, so per-category findings would collide
    # on one row and lose all but the last detail.
    stable = {10: .10, 11: .11, 12: .09, 13: .10, 14: .12, 16: .11, 17: .10}
    s = {48: {**stable, 18: .95}, 63: {**stable, 18: .99}}
    out = _jumped(s, monkeypatch)
    assert len(out) == 1
    assert "K" in out[0].detail and "QS" in out[0].detail


# ── guards on the sample it needs ──

def test_silent_without_enough_weeks_for_a_baseline(monkeypatch):
    s = _series({10: .10, 11: .11, 18: .95})
    assert _jumped(s, monkeypatch) == []


def test_silent_on_empty_series(monkeypatch):
    assert _jumped({}, monkeypatch) == []


def test_survives_a_missing_table(monkeypatch):
    def boom(conn, **kw):
        raise sqlite3.OperationalError("no such table: game_day_activity")
    monkeypatch.setattr(calib, "collect", boom)
    assert v.check_calibration(object()) == []


# ── wiring: it must stay OFF the 5-minute path ──

def test_not_in_the_per_tick_check_list():
    assert v.check_calibration not in v._CHECKS
    assert v.check_calibration not in v._LEAGUE_CHECKS


def test_run_only_calls_it_when_opted_in(monkeypatch):
    called = []
    monkeypatch.setattr(v, "check_calibration", lambda conn, now=None: called.append(1) or [])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE matchups (id INT, matchup_period_id INT)")
    for fn in ("check_pipeline_freshness", "check_live_lineup_capture"):
        monkeypatch.setattr(v, fn, lambda *a, **k: [])
    monkeypatch.setattr(v, "check_published_site", lambda *a, **k: [])
    v.run(conn, [19], now=None)
    assert called == []                        # default: off
    v.run(conn, [19], now=None, calibration=True)
    assert called == [1]                       # daily.sh opts in


# ── the long-period exclusion ──

def test_long_periods_are_excluded_from_the_recurring_series():
    # A fortnight isn't comparable to a week (2x games, different variance, and a
    # known +44% rotation artifact). Left in for the human report, out of here.
    from app.mlb import LONG_MATCHUPS
    assert LONG_MATCHUPS, "expected at least one long period this season"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE matchups (id INTEGER, matchup_period_id INTEGER, "
                 "home_team_id INTEGER, away_team_id INTEGER, winner TEXT)")
    long_p = sorted(LONG_MATCHUPS)[0]
    conn.execute("INSERT INTO matchups VALUES (1,?,1,2,'HOME')", (long_p,))
    conn.commit()
    # No wp_snapshots/game_day_activity needed: skip_long must bail before touching them.
    assert calib.collect(conn, periods=[long_p], skip_long=True) == []


def test_min_abs_floor_stays_above_the_replayed_false_positive():
    # Week 16's K departure was +12.5pp with a collapsed MAD scale; the floor is
    # what keeps that quiet. Guards a careless retune.
    assert v.CALIBRATION_MIN_ABS > 0.125
