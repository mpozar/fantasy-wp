"""CLI: app init-db / fetch / compute / publish."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from app import LEAGUE_ID, SEASON_ID, db, espn, model, stats


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@click.group()
def cli() -> None:
    """fantasy-wp commands."""


@cli.command("init-db")
def init_db() -> None:
    """Create SQLite tables (idempotent)."""
    db.init()
    click.echo(f"Initialized {db.DB_PATH}")


@cli.command()
def fetch() -> None:
    """Pull league shape + teams + current matchup period state into SQLite."""
    shape = espn.fetch_league_shape()
    teams = espn.fetch_teams()
    matchups = espn.fetch_matchup_period(shape.current_matchup_period)
    now = _now_iso()

    conn = db.connect()
    try:
        # Persist scoring_settings
        cats_json = json.dumps([
            {"stat_id": c.stat_id, "reversed": c.reversed} for c in shape.categories
        ])
        conn.execute(
            """
            INSERT INTO scoring_settings
                (league_id, season_id, name, size, scoring_type,
                 tiebreaker_stat_id, categories_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(league_id, season_id) DO UPDATE SET
                name=excluded.name,
                size=excluded.size,
                scoring_type=excluded.scoring_type,
                tiebreaker_stat_id=excluded.tiebreaker_stat_id,
                categories_json=excluded.categories_json,
                fetched_at=excluded.fetched_at
            """,
            (LEAGUE_ID, SEASON_ID, shape.name, shape.size, shape.scoring_type,
             shape.tiebreaker_stat_id, cats_json, now),
        )

        # Persist teams
        for t in teams:
            conn.execute(
                """
                INSERT INTO teams (id, name, abbrev, owner, fetched_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    abbrev=excluded.abbrev,
                    owner=excluded.owner,
                    fetched_at=excluded.fetched_at
                """,
                (t["id"], t["name"], t["abbrev"], t["owner"], now),
            )

        # Persist matchups + category state
        for m in matchups:
            conn.execute(
                """
                INSERT INTO matchups
                    (id, matchup_period_id, home_team_id, away_team_id, winner, fetched_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    matchup_period_id=excluded.matchup_period_id,
                    home_team_id=excluded.home_team_id,
                    away_team_id=excluded.away_team_id,
                    winner=excluded.winner,
                    fetched_at=excluded.fetched_at
                """,
                (m["matchup_id"], m["matchup_period_id"],
                 m["home_team_id"], m["away_team_id"], m["winner"], now),
            )
            for s in m["scores"]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO category_state
                        (matchup_id, team_id, stat_id, score, result, fetched_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (m["matchup_id"], s["team_id"], s["stat_id"],
                     s["score"], s["result"], now),
                )
        conn.commit()
    finally:
        conn.close()

    click.echo(
        f"Fetched: league={shape.name!r}, period={shape.current_matchup_period}, "
        f"teams={len(teams)}, matchups={len(matchups)}"
    )


@cli.command()
def compute() -> None:
    """Compute WP for all matchups in the current matchup period."""
    conn = db.connect()
    try:
        ss = conn.execute(
            "SELECT * FROM scoring_settings WHERE league_id=? AND season_id=?",
            (LEAGUE_ID, SEASON_ID),
        ).fetchone()
        if ss is None:
            raise click.ClickException("No scoring_settings. Run `app fetch` first.")

        categories_raw = json.loads(ss["categories_json"])
        categories = [
            model.CatConfig(stat_id=c["stat_id"], reversed=c["reversed"])
            for c in categories_raw
        ]
        tiebreaker = ss["tiebreaker_stat_id"]

        # Latest matchup_period_id we have in matchups table
        row = conn.execute(
            "SELECT MAX(matchup_period_id) AS p FROM matchups"
        ).fetchone()
        period_id = row["p"]
        if period_id is None:
            raise click.ClickException("No matchups in DB. Run `app fetch` first.")

        ms = conn.execute(
            "SELECT * FROM matchups WHERE matchup_period_id=?",
            (period_id,),
        ).fetchall()

        now = _now_iso()
        for m in ms:
            home_scores = _latest_scores(conn, m["id"], m["home_team_id"])
            away_scores = _latest_scores(conn, m["id"], m["away_team_id"])
            home_wp, away_wp, details = model.compute_wp(
                home_scores, away_scores, categories, tiebreaker,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO wp_snapshots
                    (matchup_id, computed_at, home_wp, away_wp,
                     model_version, details_json)
                VALUES (?,?,?,?,?,?)
                """,
                (m["id"], now, home_wp, away_wp, model.MODEL_VERSION,
                 json.dumps(details)),
            )
        conn.commit()
        click.echo(f"Computed WP for {len(ms)} matchups (period {period_id}).")
    finally:
        conn.close()


def _latest_scores(conn, matchup_id: int, team_id: int) -> dict[int, float]:
    rows = conn.execute(
        """
        SELECT stat_id, score
        FROM category_state
        WHERE matchup_id=? AND team_id=?
          AND fetched_at = (
              SELECT MAX(fetched_at) FROM category_state
              WHERE matchup_id=? AND team_id=?
          )
        """,
        (matchup_id, team_id, matchup_id, team_id),
    ).fetchall()
    return {r["stat_id"]: r["score"] for r in rows}


@cli.command()
def publish() -> None:
    """Write docs/data.json from the latest fetch + compute."""
    conn = db.connect()
    try:
        ss = conn.execute(
            "SELECT * FROM scoring_settings WHERE league_id=? AND season_id=?",
            (LEAGUE_ID, SEASON_ID),
        ).fetchone()
        if ss is None:
            raise click.ClickException("No scoring_settings. Run `app fetch` first.")

        categories_raw = json.loads(ss["categories_json"])
        for c in categories_raw:
            c["name"] = stats.name(c["stat_id"])

        period_row = conn.execute(
            "SELECT MAX(matchup_period_id) AS p FROM matchups"
        ).fetchone()
        period_id = period_row["p"]

        teams = {
            r["id"]: dict(r) for r in conn.execute("SELECT * FROM teams").fetchall()
        }

        matchups_out = []
        ms = conn.execute(
            "SELECT * FROM matchups WHERE matchup_period_id=?",
            (period_id,),
        ).fetchall()
        for m in ms:
            home_scores = _latest_scores_named(conn, m["id"], m["home_team_id"])
            away_scores = _latest_scores_named(conn, m["id"], m["away_team_id"])
            wp_row = conn.execute(
                """
                SELECT * FROM wp_snapshots
                WHERE matchup_id=?
                ORDER BY computed_at DESC LIMIT 1
                """,
                (m["id"],),
            ).fetchone()
            matchups_out.append({
                "matchup_id": m["id"],
                "home": {
                    "team_id": m["home_team_id"],
                    "name": teams.get(m["home_team_id"], {}).get("name"),
                    "owner": teams.get(m["home_team_id"], {}).get("owner"),
                    "scores": home_scores,
                    "wp": wp_row["home_wp"] if wp_row else None,
                },
                "away": {
                    "team_id": m["away_team_id"],
                    "name": teams.get(m["away_team_id"], {}).get("name"),
                    "owner": teams.get(m["away_team_id"], {}).get("owner"),
                    "scores": away_scores,
                    "wp": wp_row["away_wp"] if wp_row else None,
                },
                "winner": m["winner"],
                "computed_at": wp_row["computed_at"] if wp_row else None,
                "model_version": wp_row["model_version"] if wp_row else None,
            })

        out = {
            "league": {
                "id": LEAGUE_ID,
                "season": SEASON_ID,
                "name": ss["name"],
                "size": ss["size"],
                "scoring_type": ss["scoring_type"],
                "tiebreaker_stat_id": ss["tiebreaker_stat_id"],
                "tiebreaker_name": stats.name(ss["tiebreaker_stat_id"]) if ss["tiebreaker_stat_id"] else None,
                "categories": categories_raw,
            },
            "matchup_period_id": period_id,
            "generated_at": _now_iso(),
            "matchups": matchups_out,
        }
        out_path = Path(__file__).resolve().parent.parent / "docs" / "data.json"
        out_path.write_text(json.dumps(out, indent=2))
        click.echo(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")
    finally:
        conn.close()


def _latest_scores_named(conn, matchup_id: int, team_id: int) -> list[dict]:
    raw = _latest_scores(conn, matchup_id, team_id)
    return [
        {"stat_id": sid, "name": stats.name(sid), "score": score}
        for sid, score in sorted(raw.items())
    ]
