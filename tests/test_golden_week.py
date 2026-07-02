"""End-to-end golden-week regression test.

Runs the real `compute` → `publish` → `validate` pipeline against a slimmed
snapshot of a real week (tests/fixtures/golden_week.db.gz, built by
scripts/make_golden_fixture.py) with the app clock frozen at the snapshot's
newest data timestamp. Asserts the pipeline completes and the full validation
battery reports **no error-severity findings**.

Why: nearly every bug in this repo has been emergent — a fetch/plumbing change
that passes unit tests while the end-to-end output goes wrong (dropped rate
components, partial-state reads, phantom credits). `app validate` catches those
in prod; this test runs the same battery pre-commit, against known-good data.

The clock freeze covers every wall-clock read on the compute/publish/validate
path (`cli._now_iso`, `cli._settle_boundary`, `sim._utc_today`). The calendar
math itself (`matchup_period_window`) is absolute and needs no freezing. If a
new `datetime.now()` sneaks onto this path it will typically surface here as a
past-date guard dropping the whole fixture schedule → INV_EMPTY_BUDGETS.
"""
import gzip
import json
import random
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from app import cli, db, sim

FIXTURE = Path(__file__).parent / "fixtures" / "golden_week.db.gz"
SIMS = 2000   # MC SE ~1.1pp at p=0.5 — far inside every validation threshold


@pytest.fixture()
def golden(tmp_path, monkeypatch):
    dbp = tmp_path / "golden.db"
    dbp.write_bytes(gzip.decompress(FIXTURE.read_bytes()))
    with sqlite3.connect(dbp) as c:
        (frozen,) = c.execute(
            "SELECT value FROM golden_meta WHERE key='frozen_now'").fetchone()
    frozen_dt = datetime.fromisoformat(frozen)
    frozen_iso = frozen_dt.isoformat(timespec="seconds")

    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(cli, "_now_iso", lambda: frozen_iso)
    monkeypatch.setattr(cli, "_settle_boundary",
                        lambda: sim.settle_boundary_date(frozen_dt))
    monkeypatch.setattr(sim, "_utc_today", lambda: frozen_dt.date())
    data_json = tmp_path / "data.json"
    monkeypatch.setattr(cli, "DOCS_DATA_JSON", data_json)
    random.seed(20260702)
    return {"db": dbp, "now": frozen_iso, "data_json": data_json}


def _conn(dbp):
    conn = sqlite3.connect(dbp)
    conn.row_factory = sqlite3.Row
    return conn


def test_compute_publish_validate_golden_week(golden):
    runner = CliRunner()

    # ── compute (current week, the full live machinery) ──
    r = runner.invoke(cli.cli, ["compute", "--sims", str(SIMS)])
    assert r.exit_code == 0, r.output

    conn = _conn(golden["db"])
    current = conn.execute(
        "SELECT matchup_period_id p FROM team_rosters "
        "GROUP BY matchup_period_id ORDER BY MAX(fetched_at) DESC LIMIT 1"
    ).fetchone()["p"]
    matchups = [m["id"] for m in conn.execute(
        "SELECT id FROM matchups WHERE matchup_period_id=?", (current,))]
    assert matchups, "fixture has no current-period matchups"

    fresh = conn.execute(
        "SELECT matchup_id, home_wp, away_wp, details_json FROM wp_snapshots "
        "WHERE computed_at=?", (golden["now"],)).fetchall()
    assert {r["matchup_id"] for r in fresh} == set(matchups), \
        "every current-period matchup must get a snapshot at the frozen tick"
    for row in fresh:
        assert 0.0 <= row["home_wp"] <= 1.0 and 0.0 <= row["away_wp"] <= 1.0
        d = json.loads(row["details_json"])
        assert d["n_sims"] == SIMS
        assert d["home_budgets"] and d["away_budgets"], \
            f"m{row['matchup_id']}: a side has no player budgets"

    # ── publish (full site artifact; fresh cache ⇒ every week rebuilt) ──
    r = runner.invoke(cli.cli, ["publish"])
    assert r.exit_code == 0, r.output
    data = json.loads(golden["data_json"].read_text())
    assert data["generated_at"] == golden["now"]
    assert data["current_matchup_period"] == current
    weeks = {w["matchup_period_id"]: w for w in data["weeks"]}
    cur_week = weeks[current]
    assert cur_week["matchups"], "current week published with no matchups"

    # ── validate: the full invariant/anomaly battery over the result ──
    r = runner.invoke(cli.cli, ["validate"])
    assert r.exit_code == 0, r.output
    errors = conn.execute(
        "SELECT code, matchup_id, detail FROM validation_flags "
        "WHERE severity='error' AND resolved=0").fetchall()
    assert not errors, "error-severity validation findings:\n" + "\n".join(
        f"  {e['code']} m{e['matchup_id']}: {e['detail']}" for e in errors)
    conn.close()


def test_harness_catches_dropped_scored_cats(golden):
    """Negative control: the harness must FAIL on the 2026-06-04 incident shape
    (a team's scored cats vanishing from current state while OUTS survives).
    If this ever passes silently, the golden test is asserting nothing."""
    conn = _conn(golden["db"])
    current = conn.execute(
        "SELECT matchup_period_id p FROM team_rosters "
        "GROUP BY matchup_period_id ORDER BY MAX(fetched_at) DESC LIMIT 1"
    ).fetchone()["p"]
    m = conn.execute(
        "SELECT id, home_team_id FROM matchups WHERE matchup_period_id=? LIMIT 1",
        (current,)).fetchone()
    # Drop the H rows (scored cat) but keep OUTS — the check's fire-gate.
    conn.execute(
        "DELETE FROM category_state WHERE matchup_id=? AND team_id=? AND stat_id=1",
        (m["id"], m["home_team_id"]))
    conn.commit()
    conn.close()

    r = CliRunner().invoke(cli.cli, ["validate"])
    assert r.exit_code == 0, r.output
    conn = _conn(golden["db"])
    codes = {e["code"] for e in conn.execute(
        "SELECT code FROM validation_flags WHERE severity='error' AND resolved=0")}
    conn.close()
    assert "INV_CURRENT_CATS_MISSING" in codes, \
        f"corrupted state not flagged (got: {codes or 'nothing'})"
