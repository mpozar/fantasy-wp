"""CLI: app init-db / fetch / compute / publish."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from app import LEAGUE_ID, SEASON_ID, db, espn, mlb, model, sim, stats


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
    """Pull league shape + teams + every matchup period's state into SQLite.

    For future periods the cumulative scores are 0 (matchups haven't started);
    they're stored uniformly so downstream queries don't need a special case.
    """
    shape = espn.fetch_league_shape()
    teams = espn.fetch_teams()
    matchups = espn.fetch_all_matchups()
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

        # Persist matchups + category state (regular season only)
        last_reg = shape.last_regular_season_period
        for m in matchups:
            if m["matchup_period_id"] > last_reg:
                continue
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

    periods_seen = sorted({m["matchup_period_id"] for m in matchups})
    click.echo(
        f"Fetched: league={shape.name!r}, current period={shape.current_matchup_period}, "
        f"last regular season period={shape.last_regular_season_period}, "
        f"teams={len(teams)}, matchups={len(matchups)} across "
        f"periods {periods_seen[0]}..{periods_seen[-1]}"
    )


@cli.command("refresh-rosters")
def refresh_rosters() -> None:
    """Pull rosters + per-player ROS projections from ESPN into SQLite.

    Transactional: nothing is changed in the DB unless the whole ESPN fetch
    succeeds. Safe to run every few hours via cron.
    """
    snap = espn.fetch_rosters_and_projections()
    now = _now_iso()
    period_id = snap["matchup_period_id"]

    conn = db.connect()
    try:
        with conn:
            for p in snap["players"]:
                conn.execute(
                    """
                    INSERT INTO players
                        (id, full_name, pro_team_id, default_position_id,
                         eligible_slots_json, injury_status, fetched_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        full_name=excluded.full_name,
                        pro_team_id=excluded.pro_team_id,
                        default_position_id=excluded.default_position_id,
                        eligible_slots_json=excluded.eligible_slots_json,
                        injury_status=excluded.injury_status,
                        fetched_at=excluded.fetched_at
                    """,
                    (p["id"], p["full_name"], p["pro_team_id"],
                     p["default_position_id"], json.dumps(p["eligible_slots"]),
                     p["injury_status"], now),
                )

            # Replace roster for this matchup period in one shot
            conn.execute(
                "DELETE FROM team_rosters WHERE matchup_period_id=?",
                (period_id,),
            )
            for r in snap["roster_entries"]:
                conn.execute(
                    """
                    INSERT INTO team_rosters
                        (matchup_period_id, fantasy_team_id, player_id,
                         lineup_slot_id, status, fetched_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (period_id, r["fantasy_team_id"], r["player_id"],
                     r["lineup_slot_id"], r["status"], now),
                )

            for pr in snap["projections"]:
                conn.execute(
                    """
                    INSERT INTO player_projections
                        (player_id, stat_id, value, split_id, season_id, fetched_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(player_id, stat_id, split_id, season_id) DO UPDATE SET
                        value=excluded.value,
                        fetched_at=excluded.fetched_at
                    """,
                    (pr["player_id"], pr["stat_id"], pr["value"],
                     pr["split_id"], pr["season_id"], now),
                )
    finally:
        conn.close()

    click.echo(
        f"Refreshed rosters: period={period_id}, "
        f"players={len(snap['players'])}, "
        f"roster_entries={len(snap['roster_entries'])}, "
        f"projections={len(snap['projections'])}"
    )


@cli.command("refresh-schedule")
def refresh_schedule() -> None:
    """Pull MLB schedule + probable pitchers for every remaining regular-season
    matchup week.

    Replaces rows per-period transactionally; if the MLB fetch fails the DB
    stays on last-good. Probable pitchers are only populated for the next few
    days of MLB games — future weeks store null probables, and the simulator
    falls back to a ROS-share estimate for SP starts in those weeks.
    """
    shape = espn.fetch_league_shape()
    current = shape.current_matchup_period
    last = shape.last_regular_season_period
    now = _now_iso()

    total_games = 0
    conn = db.connect()
    try:
        for period_id in range(current, last + 1):
            start, end = mlb.matchup_period_window(period_id, current)
            games = mlb.fetch_schedule(start, end)
            with conn:
                conn.execute(
                    "DELETE FROM team_schedule WHERE matchup_period_id=?",
                    (period_id,),
                )
                for g in games:
                    conn.execute(
                        """
                        INSERT INTO team_schedule
                            (matchup_period_id, game_pk, game_date, pro_team_id,
                             opponent_pro_team_id, is_home,
                             probable_pitcher_mlbam_id, probable_pitcher_name,
                             game_status, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(matchup_period_id, game_pk, pro_team_id) DO UPDATE SET
                            game_date=excluded.game_date,
                            opponent_pro_team_id=excluded.opponent_pro_team_id,
                            is_home=excluded.is_home,
                            probable_pitcher_mlbam_id=excluded.probable_pitcher_mlbam_id,
                            probable_pitcher_name=excluded.probable_pitcher_name,
                            game_status=excluded.game_status,
                            fetched_at=excluded.fetched_at
                        """,
                        (period_id, g["game_pk"], g["game_date"], g["espn_team_id"],
                         g["opponent_espn_team_id"], g["is_home"],
                         g["probable_pitcher_mlbam_id"], g["probable_pitcher_name"],
                         g["game_status"], now),
                    )
            total_games += len(games)
    finally:
        conn.close()

    click.echo(
        f"Refreshed schedule: periods {current}..{last}, "
        f"team_game_rows={total_games}"
    )


@cli.command()
@click.option("--model", "model_name",
              type=click.Choice(["mc-v1", "ratio-v0"]),
              default="mc-v1", show_default=True,
              help="Which WP model to use.")
@click.option("--sims", type=int, default=sim.DEFAULT_SIMS, show_default=True,
              help="Monte Carlo sim count (mc-v1 only).")
@click.option("--future", "future_only", is_flag=True,
              help="Compute future regular-season periods instead of the current one. "
                   "SP starts are estimated from ROS projections rather than probable pitchers.")
def compute(model_name: str, sims: int, future_only: bool) -> None:
    """Compute WP for the current matchup period (default) or for every
    future regular-season period (with --future)."""
    conn = db.connect()
    try:
        ss = conn.execute(
            "SELECT * FROM scoring_settings WHERE league_id=? AND season_id=?",
            (LEAGUE_ID, SEASON_ID),
        ).fetchone()
        if ss is None:
            raise click.ClickException("No scoring_settings. Run `app fetch` first.")

        last_reg = _last_regular_season_period(conn)
        current = _current_matchup_period(conn)
        if current is None or last_reg is None:
            raise click.ClickException("Missing period metadata. Run `app fetch` first.")

        if future_only:
            periods = list(range(current + 1, last_reg + 1))
        else:
            periods = [current]

        if not periods:
            click.echo("Nothing to compute (no future periods left in regular season).")
            return

        categories_raw = json.loads(ss["categories_json"])
        categories = [
            model.CatConfig(stat_id=c["stat_id"], reversed=c["reversed"])
            for c in categories_raw
        ]
        tiebreaker = ss["tiebreaker_stat_id"]

        # For future-week SP estimation, the share of a team's remaining season
        # games that fall in any given week. Cheap to compute once.
        team_total_ros_games = (
            sim.load_total_remaining_games(conn, current, last_reg)
            if (future_only and model_name == "mc-v1") else {}
        )

        now = _now_iso()
        total_matchups = 0
        for period_id in periods:
            ms = conn.execute(
                "SELECT * FROM matchups WHERE matchup_period_id=?",
                (period_id,),
            ).fetchall()
            if not ms:
                continue

            schedule_by_team = sim.load_schedule_by_team(conn, period_id) \
                if model_name == "mc-v1" else {}
            if model_name == "mc-v1" and not schedule_by_team:
                raise click.ClickException(
                    f"No team_schedule rows for period {period_id}. "
                    "Run `app refresh-schedule` first."
                )

            for m in ms:
                home_scores = _latest_scores(conn, m["id"], m["home_team_id"])
                away_scores = _latest_scores(conn, m["id"], m["away_team_id"])

                if model_name == "mc-v1":
                    # Rosters are only stored for the current period; future
                    # weeks reuse today's roster (best estimate of who'll be
                    # on each team).
                    roster_period = current if future_only else period_id
                    inputs = sim.MatchupInputs(
                        matchup_id=m["id"],
                        home_state=home_scores,
                        away_state=away_scores,
                        home_roster=sim.load_team_roster(conn, roster_period, m["home_team_id"]),
                        away_roster=sim.load_team_roster(conn, roster_period, m["away_team_id"]),
                    )
                    home_wp, away_wp, details = sim.simulate(
                        inputs, schedule_by_team, n_sims=sims,
                        estimate_sp_starts=future_only,
                        team_total_ros_games=team_total_ros_games,
                    )
                    version = sim.MODEL_VERSION
                else:
                    home_wp, away_wp, details = model.compute_wp(
                        home_scores, away_scores, categories, tiebreaker,
                    )
                    version = model.MODEL_VERSION

                conn.execute(
                    """
                    INSERT OR REPLACE INTO wp_snapshots
                        (matchup_id, computed_at, home_wp, away_wp,
                         model_version, details_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (m["id"], now, home_wp, away_wp, version, json.dumps(details)),
                )
            total_matchups += len(ms)
        conn.commit()
        scope = "future" if future_only else "current"
        click.echo(
            f"Computed WP for {total_matchups} matchups ({scope}: "
            f"periods {periods[0]}..{periods[-1]}) using {model_name}."
        )
    finally:
        conn.close()


def _current_matchup_period(conn) -> int | None:
    """Use the rosters table as the source-of-truth: refresh-rosters writes
    only for the current period. Falls back to the smallest period with
    non-zero scores in category_state."""
    row = conn.execute(
        "SELECT matchup_period_id FROM team_rosters "
        "GROUP BY matchup_period_id ORDER BY MAX(fetched_at) DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["matchup_period_id"]
    row = conn.execute(
        "SELECT MIN(matchup_period_id) AS p FROM matchups"
    ).fetchone()
    return row["p"] if row else None


def _last_regular_season_period(conn) -> int | None:
    """Stored in matchups indirectly — we use the value cached during `fetch`
    via the scoring_settings table. For now, derive from MAX of matchups
    (fetched_at) since fetch only stores regular + playoffs."""
    row = conn.execute(
        "SELECT MAX(matchup_period_id) AS p FROM matchups"
    ).fetchone()
    return row["p"] if row else None


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
    """Write docs/data.json with one entry per remaining regular-season week."""
    from app import mlb  # local import — only publish needs date-window math

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
            c["group"] = stats.group(c["stat_id"])

        cats_by_group = {
            "batting": [{
                "stat_id": sid, "name": stats.name(sid),
                "reversed": stats.is_reversed(sid),
            } for sid in stats.BATTING_STAT_IDS],
            "pitching": [{
                "stat_id": sid, "name": stats.name(sid),
                "reversed": stats.is_reversed(sid),
            } for sid in stats.PITCHING_STAT_IDS],
        }

        current = _current_matchup_period(conn)
        last_reg = _last_regular_season_period(conn)
        if current is None or last_reg is None:
            raise click.ClickException("Missing period metadata. Run `app fetch` first.")

        teams = {
            r["id"]: dict(r) for r in conn.execute("SELECT * FROM teams").fetchall()
        }

        weeks_out = []
        for period_id in range(current, last_reg + 1):
            is_current = period_id == current
            start, end = mlb.matchup_period_window(period_id, current)
            ms = conn.execute(
                "SELECT * FROM matchups WHERE matchup_period_id=? ORDER BY id",
                (period_id,),
            ).fetchall()
            matchups_out = [
                _matchup_block(conn, teams, m, is_current=is_current)
                for m in ms
            ]
            weeks_out.append({
                "matchup_period_id": period_id,
                "label": f"Week {period_id}",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "is_current": is_current,
                "matchups": matchups_out,
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
                "categories_by_group": cats_by_group,
            },
            "current_matchup_period": current,
            "last_regular_season_period": last_reg,
            "generated_at": _now_iso(),
            "weeks": weeks_out,
        }
        out_path = Path(__file__).resolve().parent.parent / "docs" / "data.json"
        out_path.write_text(json.dumps(out, indent=2))
        click.echo(
            f"Wrote {out_path} ({out_path.stat().st_size} bytes) — "
            f"{len(weeks_out)} weeks (periods {current}..{last_reg})"
        )
    finally:
        conn.close()


def _matchup_block(conn, teams: dict, m, *, is_current: bool) -> dict:
    """One matchup with team blocks, current snapshot, and history."""
    home_team_id = m["home_team_id"]
    away_team_id = m["away_team_id"]
    home_state = _latest_score_rows(conn, m["id"], home_team_id)
    away_state = _latest_score_rows(conn, m["id"], away_team_id)
    wp_row = conn.execute(
        """
        SELECT * FROM wp_snapshots
        WHERE matchup_id=?
        ORDER BY computed_at DESC LIMIT 1
        """,
        (m["id"],),
    ).fetchone()
    history_rows = conn.execute(
        """
        SELECT computed_at, home_wp, away_wp, model_version
        FROM wp_snapshots
        WHERE matchup_id=?
        ORDER BY computed_at ASC
        """,
        (m["id"],),
    ).fetchall()
    history = [
        {
            "computed_at": r["computed_at"],
            "home_wp": r["home_wp"],
            "away_wp": r["away_wp"],
            "model_version": r["model_version"],
        }
        for r in history_rows
    ]
    details = None
    if wp_row and wp_row["details_json"]:
        try:
            details = json.loads(wp_row["details_json"])
        except json.JSONDecodeError:
            details = None
    return {
        "matchup_id": m["id"],
        "home": _team_block(teams, home_team_id, home_state,
                            wp_row["home_wp"] if wp_row else None,
                            is_current=is_current),
        "away": _team_block(teams, away_team_id, away_state,
                            wp_row["away_wp"] if wp_row else None,
                            is_current=is_current),
        "winner": m["winner"],
        "computed_at": wp_row["computed_at"] if wp_row else None,
        "model_version": wp_row["model_version"] if wp_row else None,
        "history": history,
        "details": details,
    }


def _latest_score_rows(conn, matchup_id: int, team_id: int) -> dict[int, dict]:
    """Latest score+result keyed by stat_id."""
    rows = conn.execute(
        """
        SELECT stat_id, score, result
        FROM category_state
        WHERE matchup_id=? AND team_id=?
          AND fetched_at = (
              SELECT MAX(fetched_at) FROM category_state
              WHERE matchup_id=? AND team_id=?
          )
        """,
        (matchup_id, team_id, matchup_id, team_id),
    ).fetchall()
    return {r["stat_id"]: {"score": r["score"], "result": r["result"]} for r in rows}


def _team_block(teams: dict, team_id: int, state: dict[int, dict],
                wp: float | None, *, is_current: bool) -> dict:
    t = teams.get(team_id, {})
    record = {"W": 0, "L": 0, "T": 0}
    for s in state.values():
        r = s.get("result")
        if r == "WIN":
            record["W"] += 1
        elif r == "LOSS":
            record["L"] += 1
        elif r == "TIE":
            record["T"] += 1

    def block(stat_ids: list[int]) -> list[dict]:
        out = []
        for sid in stat_ids:
            s = state.get(sid, {})
            # Future weeks haven't started — emit nulls so the UI shows dashes.
            score = s.get("score") if is_current else None
            result = s.get("result") if is_current else None
            out.append({
                "stat_id": sid,
                "name": stats.name(sid),
                "reversed": stats.is_reversed(sid),
                "score": score,
                "result": result,
            })
        return out

    return {
        "team_id": team_id,
        "name": t.get("name"),
        "owner": t.get("owner"),
        "abbrev": t.get("abbrev"),
        "wp": wp,
        "record": record if is_current else None,
        "batting": block(stats.BATTING_STAT_IDS),
        "pitching": block(stats.PITCHING_STAT_IDS),
    }
