"""Build tests/fixtures/golden_week.db.gz — the end-to-end regression fixture.

Snapshots the live data.db into a slimmed SQLite file that
tests/test_golden_week.py runs `compute` + `publish` + `validate` against with
a frozen clock. The point is to catch *emergent* regressions (plumbing changes
that pass unit tests but break the end-to-end output — the class behind most
incidents in INCIDENTS.md) before cron does.

What's slimmed (everything else is copied whole — all small):
  - category_state (670k rows) → the latest row per (matchup, team, stat),
    i.e. exactly the rows every reader uses (db.latest_category_state).
  - wp_snapshots (68k rows)    → the 3 most recent per matchup (enough for the
    WP-swing checks and a non-empty published history).
  - validation_flags / published_week_cache → empty (clean slate).

A `golden_meta` table records `frozen_now` (the newest data timestamp) — the
test freezes the app clock there so the calendar-window and freshness logic
sees the fixture as live forever.

Regenerate (after schema changes, at the start of a season, or — ideally —
during a live slate so the in-game paths carry real data):

    .venv/bin/python scripts/make_golden_fixture.py

Safe to run any time: reads data.db read-only via ATTACH; doesn't take the
app lock (a mid-tick write could at worst skew one row's freshness, which the
frozen clock makes irrelevant).
"""
import gzip
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "data.db"
OUT = REPO / "tests" / "fixtures" / "golden_week.db"

# Tables NOT copied whole (see module docstring).
SLIM = {"category_state", "wp_snapshots", "validation_flags",
        "published_week_cache"}


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.unlink(missing_ok=True)
    conn = sqlite3.connect(OUT)
    conn.execute("ATTACH DATABASE ? AS src", (f"file:{SRC}?mode=ro",))

    # Recreate the full schema (tables + indexes) from the source, so the
    # fixture always matches whatever db.SCHEMA produced there.
    for (sql,) in conn.execute(
            "SELECT sql FROM src.sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"):
        conn.execute(sql)

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM src.sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        if t in SLIM:
            continue
        conn.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")

    conn.execute("""
        INSERT OR IGNORE INTO main.category_state
        SELECT cs.* FROM src.category_state cs
        JOIN (SELECT matchup_id m, team_id t, stat_id s, MAX(fetched_at) f
              FROM src.category_state GROUP BY 1, 2, 3) latest
          ON cs.matchup_id = latest.m AND cs.team_id = latest.t
         AND cs.stat_id = latest.s AND cs.fetched_at = latest.f
    """)
    conn.execute("""
        INSERT INTO main.wp_snapshots
        SELECT s.* FROM src.wp_snapshots s
        WHERE s.computed_at IN (
            SELECT computed_at FROM src.wp_snapshots s2
            WHERE s2.matchup_id = s.matchup_id
            ORDER BY computed_at DESC LIMIT 3)
    """)

    # Frozen clock for the test = the newest data timestamp in the snapshot.
    conn.execute(
        "CREATE TABLE golden_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""
        INSERT INTO golden_meta VALUES ('frozen_now', (
            SELECT MAX(x) FROM (
                SELECT MAX(fetched_at) x FROM main.category_state
                UNION ALL SELECT MAX(computed_at) FROM main.wp_snapshots
                UNION ALL SELECT MAX(fetched_at) FROM main.team_rosters)))
    """)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    raw = OUT.read_bytes()
    gz = Path(str(OUT) + ".gz")
    gz.write_bytes(gzip.compress(raw, 9))
    OUT.unlink()
    print(f"wrote {gz} ({gz.stat().st_size:,} bytes gzipped; "
          f"{len(raw):,} raw)")


if __name__ == "__main__":
    main()
