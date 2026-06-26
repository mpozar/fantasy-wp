"""One-time (re-runnable) maintenance: collapse duplicate category_state rows.

`fetch` historically re-INSERTed a full snapshot of every non-current matchup every
5-min tick, so settled past weeks (and all-zero future weeks) accumulated millions
of identical rows — category_state grew to ~21M rows / 3.8 GB and the duplicate bulk
slowed every reader (the `seeded_current` full scan, the `prev_noncurrent` load,
publish/compute latest-state reads). `cli._write_noncurrent_score` stops the bleed
going forward; this script removes the historical backlog.

For every NON-current period it keeps only the latest row per (matchup, team, stat)
— which is exactly the value every reader uses — and deletes the rest. The CURRENT
period is left fully intact (it's small, active, and its within-week tick history is
occasionally useful for WP-swing debugging). A late ESPN correction is preserved:
the kept row is the most recent one, so a corrected settled value survives.

Idempotent: re-running after the bleed is fixed deletes nothing.

Run UNDER THE LOCK (no cron tick mid-flight). VACUUM needs ~DB-size free disk and an
exclusive connection; cron ticks skip while we hold .app.lock.

    .venv/bin/python scripts/prune_category_state.py            # current period auto-detected
    .venv/bin/python scripts/prune_category_state.py --period 13
    .venv/bin/python scripts/prune_category_state.py --dry-run  # counts only, no writes
"""

import argparse
import time
from datetime import date

from app import db, mlb


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--period", type=int, default=None,
                    help="current period to PRESERVE intact (default: calendar today)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report row counts only; make no changes")
    ap.add_argument("--no-vacuum", action="store_true",
                    help="skip VACUUM (leave freed pages for SQLite to reuse)")
    args = ap.parse_args()

    current = args.period if args.period is not None else mlb.period_for_date(date.today())

    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM category_state").fetchone()[0]
        noncurrent = conn.execute(
            "SELECT COUNT(*) FROM category_state WHERE matchup_id IN "
            "(SELECT id FROM matchups WHERE matchup_period_id <> ?)", (current,),
        ).fetchone()[0]
        print(f"current period (preserved): {current}")
        print(f"category_state rows: {total:,}  (non-current: {noncurrent:,})")

        if args.dry_run:
            # How many non-current rows are NOT the latest for their cell = deletable.
            deletable = conn.execute(
                """
                SELECT COUNT(*) FROM category_state
                WHERE matchup_id IN (SELECT id FROM matchups WHERE matchup_period_id <> ?)
                  AND fetched_at <> (
                      SELECT MAX(fetched_at) FROM category_state c2
                      WHERE c2.matchup_id = category_state.matchup_id
                        AND c2.team_id    = category_state.team_id
                        AND c2.stat_id    = category_state.stat_id)
                """,
                (current,),
            ).fetchone()[0]
            print(f"would delete: {deletable:,} duplicate non-current rows")
            return

        # Rebuild-and-swap, NOT a correlated DELETE: deleting 21M rows via a
        # per-row `fetched_at <> (SELECT MAX ...)` subquery is ~21M index seeks
        # (>10 min). Instead build the keeper set in two index-friendly scans —
        # all current-period rows + the latest row per non-current cell (one
        # GROUP BY) — into a fresh table, then swap. PK uniqueness per
        # (matchup,team,stat,fetched_at) guarantees the MAX join yields exactly one
        # row per cell.
        t = time.time()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            DROP TABLE IF EXISTS category_state_keep;
            CREATE TABLE category_state_keep (
                matchup_id  INTEGER NOT NULL,
                team_id     INTEGER NOT NULL,
                stat_id     INTEGER NOT NULL,
                score       REAL NOT NULL,
                result      TEXT,
                fetched_at  TEXT NOT NULL,
                PRIMARY KEY (matchup_id, team_id, stat_id, fetched_at)
            );
            """
        )
        # Current period: keep every row (active week, small).
        conn.execute(
            "INSERT INTO category_state_keep "
            "SELECT cs.matchup_id, cs.team_id, cs.stat_id, cs.score, cs.result, cs.fetched_at "
            "FROM category_state cs JOIN matchups m ON m.id = cs.matchup_id "
            "WHERE m.matchup_period_id = ?",
            (current,),
        )
        # Non-current periods: keep only the latest row per (matchup,team,stat).
        conn.execute(
            """
            INSERT INTO category_state_keep
            SELECT cs.matchup_id, cs.team_id, cs.stat_id, cs.score, cs.result, cs.fetched_at
            FROM category_state cs
            JOIN (
                SELECT cs2.matchup_id, cs2.team_id, cs2.stat_id, MAX(cs2.fetched_at) AS mx
                FROM category_state cs2 JOIN matchups m ON m.id = cs2.matchup_id
                WHERE m.matchup_period_id <> ?
                GROUP BY cs2.matchup_id, cs2.team_id, cs2.stat_id
            ) k ON cs.matchup_id = k.matchup_id AND cs.team_id = k.team_id
               AND cs.stat_id = k.stat_id AND cs.fetched_at = k.mx
            """,
            (current,),
        )
        conn.executescript(
            """
            DROP TABLE category_state;
            ALTER TABLE category_state_keep RENAME TO category_state;
            CREATE INDEX IF NOT EXISTS idx_category_state_recent
                ON category_state (matchup_id, team_id, stat_id, fetched_at DESC);
            """
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        remaining = conn.execute("SELECT COUNT(*) FROM category_state").fetchone()[0]
        print(f"rebuilt in {time.time()-t:.1f}s; "
              f"deleted {total - remaining:,} rows; remaining: {remaining:,}")

        if not args.no_vacuum:
            t = time.time()
            print("VACUUM (rewriting the DB file)...")
            conn.execute("VACUUM")
            print(f"VACUUM done in {time.time()-t:.1f}s")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
