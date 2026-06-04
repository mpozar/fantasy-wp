"""CLI: app init-db / fetch / compute / publish."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from app import LEAGUE_ID, SEASON_ID, db, espn, espn_public, mlb, model, sim, stats


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Rate categories (OPS/ERA/WHIP) — derived ratios that legitimately move either
# direction, so they're never monotonicity-guarded.
_RATE_STAT_IDS = frozenset({sim.STAT_OPS, sim.STAT_ERA, sim.STAT_WHIP})


def _write_category_score(conn, last_good: dict, mid: int, tid: int, sid: int,
                          score, result, now: str) -> bool:
    """Upsert one current-period category_state cell with a monotonicity guard.

    Counting stats (everything except the rate cats) are cumulative within a
    scoring period, so a value *below* the last-good stored one is a stale or
    partial read (laggy REST, a mid-render scrape, a two-way line dropped, …) —
    reject it and keep the higher last-good. Rates are written as-is. Returns
    True if a row was written."""
    if sid not in _RATE_STAT_IDS and score is not None:
        lg = last_good.get((mid, tid, sid))
        if lg is not None and score < lg:
            return False
    conn.execute(
        "INSERT OR REPLACE INTO category_state "
        "(matchup_id, team_id, stat_id, score, result, fetched_at) VALUES (?,?,?,?,?,?)",
        (mid, tid, sid, score, result, now),
    )
    return True


def _overlay_espn_probables(games: list[dict], start, end) -> int:
    """Fill `probable_pitcher_name` from ESPN's public feed for games where MLB
    hasn't posted one yet — ESPN's feed leads MLB statsapi by a day or two.
    Fill-only: MLB wins once it posts (we never overwrite an existing probable),
    so the tentative ESPN listing is just an early stand-in. Best-effort: a feed
    error leaves the MLB data untouched. Returns the number of games filled."""
    try:
        esp = espn_public.fetch_probables(start, end)
    except Exception:  # noqa: BLE001 — best effort; fall back to MLB-only
        return 0
    n = 0
    for g in games:
        if g.get("probable_pitcher_name"):
            continue
        name = esp.get((g["game_date"], g["espn_team_id"]))
        if name:
            g["probable_pitcher_name"] = name
            n += 1
    return n


def _record_starts(conn, games: list[dict], now: str) -> int:
    """Upsert Final games' starters into `pitcher_starts` — the probable
    pitcher IS the actual starter once a game is complete. Anchor data for the
    rotation-cadence SP model. Idempotent via the (mlbam_id, game_pk) PK, so
    re-recording the same Final game is a no-op. Returns rows written."""
    n = 0
    for g in games:
        if g.get("game_status") not in _FINAL_GAME_STATES:
            continue
        pid = g.get("probable_pitcher_mlbam_id")
        if not pid:
            continue
        conn.execute(
            """
            INSERT INTO pitcher_starts
                (mlbam_id, pitcher_name, game_pk, game_date, pro_team_id, fetched_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(mlbam_id, game_pk) DO UPDATE SET
                pitcher_name=excluded.pitcher_name,
                game_date=excluded.game_date,
                pro_team_id=excluded.pro_team_id,
                fetched_at=excluded.fetched_at
            """,
            (pid, g.get("probable_pitcher_name"), g["game_pk"], g["game_date"],
             g["espn_team_id"], now),
        )
        n += 1
    return n


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
        slots_json = json.dumps(shape.lineup_slot_counts)
        conn.execute(
            """
            INSERT INTO scoring_settings
                (league_id, season_id, name, size, scoring_type,
                 tiebreaker_stat_id, categories_json, lineup_slots_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(league_id, season_id) DO UPDATE SET
                name=excluded.name,
                size=excluded.size,
                scoring_type=excluded.scoring_type,
                tiebreaker_stat_id=excluded.tiebreaker_stat_id,
                categories_json=excluded.categories_json,
                lineup_slots_json=excluded.lineup_slots_json,
                fetched_at=excluded.fetched_at
            """,
            (LEAGUE_ID, SEASON_ID, shape.name, shape.size, shape.scoring_type,
             shape.tiebreaker_stat_id, cats_json, slots_json, now),
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
        current_period = shape.current_matchup_period

        # CURRENT-period category_state comes from two sources, by stat_id:
        #   - The DOM scrape owns the league's *display* categories (the scored
        #     cats incl. ERA/WHIP/OPS as rates). REST lags badly and doesn't
        #     reliably catch up when idle (seen hours-stale after a slate), so we
        #     must NOT let REST overwrite these — the original clobber bug.
        #   - REST writes only the raw rate *components* (ER, OUTS, P_H, P_BB,
        #     AB, 2B, …) that the scrape can't see but the sim needs to derive
        #     projected ERA/WHIP/OPS. (Skipping these entirely — the over-broad
        #     first cut — made rate projections ignore the current week's innings.)
        # On the FIRST fetch of a matchup (no rows yet) we seed everything from
        # REST so it isn't empty before the first scrape. A monotonicity guard
        # (`_write_category_score`) rejects any counting-stat read below last-good
        # so a stale/partial source can't regress it. Past/future periods: REST
        # only, no scrape, no guard.
        display_cats = {c.stat_id for c in shape.categories}
        seeded_current = {
            r["matchup_id"] for r in conn.execute(
                "SELECT DISTINCT cs.matchup_id FROM category_state cs "
                "JOIN matchups m ON m.id = cs.matchup_id "
                "WHERE m.matchup_period_id = ?", (current_period,),
            ).fetchall()
        }
        # Last-good current-period scores, for the monotonicity guard — latest
        # value *per (matchup, team, stat)* (a stat not written every tick, like
        # the just-restored ER/OUTS, must compare against its own last value, not
        # the matchup's overall latest fetch).
        last_good: dict = {}
        for r in conn.execute(
            "SELECT cs.matchup_id, cs.team_id, cs.stat_id, cs.score "
            "FROM category_state cs JOIN matchups m ON m.id = cs.matchup_id "
            "WHERE m.matchup_period_id = ? AND cs.fetched_at = "
            "(SELECT MAX(fetched_at) FROM category_state c2 WHERE c2.matchup_id = cs.matchup_id "
            " AND c2.team_id = cs.team_id AND c2.stat_id = cs.stat_id)",
            (current_period,),
        ).fetchall():
            last_good[(r["matchup_id"], r["team_id"], r["stat_id"])] = r["score"]

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
            cur_p = m["matchup_period_id"] == current_period
            seeded = m["matchup_id"] in seeded_current
            for s in m["scores"]:
                if cur_p and seeded and s["stat_id"] in display_cats:
                    continue  # scrape owns display cats; REST only fills components
                if cur_p:
                    _write_category_score(conn, last_good, m["matchup_id"],
                                          s["team_id"], s["stat_id"],
                                          s["score"], s["result"], now)
                else:
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

        # Override the (lagging) REST scoreByStat for the CURRENT period with
        # values scraped from ESPN's web UI. The UI gets real-time updates via
        # FastCast WebSocket; the REST endpoint we just polled can be 5-30 min
        # stale during live games. Fall back to REST data silently if the
        # scrape errors or returns nothing (auth wall, profile not set up).
        # Only scrape when games are actually in progress. With nothing live,
        # ESPN's REST scores are current and the ~15-60s headless-browser scrape
        # is pure overhead (and the only place an idle-window runtime spike can
        # come from). team_schedule status is fresh — refresh-live ran first.
        in_progress = conn.execute(
            "SELECT COUNT(*) FROM team_schedule "
            "WHERE matchup_period_id=? AND game_status='In Progress'",
            (shape.current_matchup_period,),
        ).fetchone()[0]
        scraped_count = 0
        scraped = {}
        scrape_skipped_idle = not in_progress
        if in_progress:
            try:
                from app import espn_scrape
                abbrev_to_id = {t["abbrev"]: t["id"] for t in teams}
                scraped = espn_scrape.scrape_live_matchup_scores(
                    shape.current_matchup_period, abbrev_to_id,
                )
            except Exception as e:
                scraped = {}
                click.echo(f"  (live scrape skipped: {e})", err=True)

        if scraped:
            # Re-find the current-period matchups and overlay
            current_matchups = [m for m in matchups
                                if m["matchup_period_id"] == shape.current_matchup_period]
            for m in current_matchups:
                for team_id in (m["home_team_id"], m["away_team_id"]):
                    rows = scraped.get(team_id)
                    if not rows:
                        continue
                    for s in rows:
                        if s["score"] is None:
                            continue
                        # Same monotonicity guard: a scrape that drops a two-way
                        # player's line or reads mid-render can regress a counting
                        # cat (e.g. K 26→20) — reject it, keep last-good.
                        if _write_category_score(conn, last_good, m["matchup_id"],
                                                 team_id, s["stat_id"],
                                                 s["score"], s["result"], now):
                            scraped_count += 1
            conn.commit()
    finally:
        conn.close()

    periods_seen = sorted({m["matchup_period_id"] for m in matchups})
    msg = (f"Fetched: league={shape.name!r}, current period={shape.current_matchup_period}, "
           f"last regular season period={shape.last_regular_season_period}, "
           f"teams={len(teams)}, matchups={len(matchups)} across "
           f"periods {periods_seen[0]}..{periods_seen[-1]}")
    if scraped_count:
        msg += f", live-scraped {scraped_count} category cells"
    elif scrape_skipped_idle:
        msg += ", scrape skipped (no games in progress)"
    click.echo(msg)


@cli.command("refresh-rosters")
def refresh_rosters() -> None:
    """Pull rosters + per-player ROS projections from ESPN into SQLite.

    Transactional: nothing is changed in the DB unless the whole ESPN fetch
    succeeds. Safe to run every few hours via cron.
    """
    snap = espn.fetch_rosters_and_projections()
    # ESPN public injuries feed → real IL return dates (best-effort; on failure
    # we leave the last-good table rather than wiping it).
    try:
        injuries = espn_public.fetch_injuries()
    except Exception:  # noqa: BLE001
        injuries = {}
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

            # Replace injury return dates wholesale (skip on a failed fetch so
            # we keep the last-good set rather than wiping it).
            if injuries:
                conn.execute("DELETE FROM player_injuries")
                for nm, (full, rd) in injuries.items():
                    conn.execute(
                        "INSERT INTO player_injuries "
                        "(norm_name, full_name, return_date, fetched_at) "
                        "VALUES (?,?,?,?)",
                        (nm, full, rd.isoformat(), now),
                    )
    finally:
        conn.close()

    click.echo(
        f"Refreshed rosters: period={period_id}, "
        f"players={len(snap['players'])}, "
        f"roster_entries={len(snap['roster_entries'])}, "
        f"projections={len(snap['projections'])}, "
        f"injuries={len(injuries)}"
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
    total_starts = 0
    total_espn_pp = 0
    conn = db.connect()
    try:
        for period_id in range(current, last + 1):
            start, end = mlb.matchup_period_window(period_id)
            games = mlb.fetch_schedule(start, end)
            # ESPN only lists probables ~5 days out, so only the current week is
            # worth overlaying here (future weeks would be ~90 wasted calls).
            # refresh-live's 4-day window keeps the near-term fresh every tick.
            if period_id == current:
                total_espn_pp += _overlay_espn_probables(games, start, end)
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
                             game_status, current_inning, inning_state, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(matchup_period_id, game_pk, pro_team_id) DO UPDATE SET
                            game_date=excluded.game_date,
                            opponent_pro_team_id=excluded.opponent_pro_team_id,
                            is_home=excluded.is_home,
                            probable_pitcher_mlbam_id=excluded.probable_pitcher_mlbam_id,
                            probable_pitcher_name=excluded.probable_pitcher_name,
                            game_status=excluded.game_status,
                            current_inning=excluded.current_inning,
                            inning_state=excluded.inning_state,
                            fetched_at=excluded.fetched_at
                        """,
                        (period_id, g["game_pk"], g["game_date"], g["espn_team_id"],
                         g["opponent_espn_team_id"], g["is_home"],
                         g["probable_pitcher_mlbam_id"], g["probable_pitcher_name"],
                         g["game_status"], g.get("current_inning"), g.get("inning_state"),
                         now),
                    )
                # Record any Final games' starters as anchor history.
                total_starts += _record_starts(conn, games, now)
            total_games += len(games)
    finally:
        conn.close()

    click.echo(
        f"Refreshed schedule: periods {current}..{last}, "
        f"team_game_rows={total_games}, starts_recorded={total_starts}, "
        f"espn_probables_filled={total_espn_pp}"
    )


@cli.command("backfill-starts")
@click.option("--days", type=int, default=21, show_default=True,
              help="Days before the current period start to scan for Final-game starters.")
def backfill_starts(days: int) -> None:
    """Seed `pitcher_starts` with recent Final games — anchor history for the
    rotation-cadence SP model.

    The regular `refresh-schedule` window only spans the current period forward,
    so on first run there's no history for a pitcher's first start of the week.
    This scans the `days` before the current period start and records the
    starters. Also feeds `scripts/analyze_cadence.py`. Idempotent.
    """
    from datetime import date, timedelta

    conn = db.connect()
    try:
        current = _current_matchup_period(conn)
        if current is None:
            raise click.ClickException("Missing period metadata. Run `app fetch` first.")
        period_start, _ = mlb.matchup_period_window(current)
        end = period_start - timedelta(days=1)
        start = period_start - timedelta(days=days)
        games = mlb.fetch_schedule(start, end)
        now = _now_iso()
        with conn:
            n = _record_starts(conn, games, now)
    finally:
        conn.close()

    click.echo(
        f"Backfilled starts: {start.isoformat()}..{end.isoformat()}, "
        f"games={len(games)}, starts_recorded={n}"
    )


@cli.command("refresh-live")
def refresh_live() -> None:
    """Upsert recent + near-future MLB games' status + inning state into
    team_schedule.

    Window = yesterday … today+2 (4 days). Yesterday covers in-progress games
    from the previous MLB calendar day (any timezone east of US Pacific rolls
    over local "today" while West Coast night games are still being played and
    dated to the prior day). The +2-day forward reach refreshes the back half of
    the current week's *probable pitchers* every fast tick — otherwise those
    games only update at the daily `refresh-schedule`, so a newly-posted probable
    (MLB posts ~24-48h out) could lag up to a day. Only one MLB statsapi call
    either way; in-progress boxscore fetches are unaffected (no live games 2 days
    out), so the runtime cost is negligible.

    No DELETE, just upserts on the existing rows.
    """
    from datetime import date, timedelta

    today = date.today()
    yesterday = today - timedelta(days=1)
    end = today + timedelta(days=2)
    games = mlb.fetch_schedule(yesterday, end)
    espn_pp = _overlay_espn_probables(games, yesterday, end)
    now = _now_iso()

    # Per game-date: distinct game statuses, for the activity tracker below.
    statuses_by_date: dict[str, set[str]] = {}

    # Fetch live boxscores for in-progress games *before* opening the write
    # transaction (network I/O shouldn't hold the DB lock). A boxscore failure
    # just means no in-game QS/SVHD detail for that game — fall back silently.
    window_pks = {g["game_pk"] for g in games}
    in_progress_pks = sorted({g["game_pk"] for g in games
                              if g["game_status"] == "In Progress"})
    live_rows: list[dict] = []
    for pk in in_progress_pks:
        try:
            live_rows.extend(mlb.fetch_boxscore(pk))
        except Exception:  # noqa: BLE001 — best-effort; REST hiccup → skip game
            pass

    conn = db.connect()
    try:
        with conn:
            for g in games:
                # Attribute each game to the period its date falls in — not to
                # ESPN's reported current period, which lags the calendar around
                # the Monday rollover and would otherwise file a new week's games
                # under the period that just ended.
                period_id = mlb.period_for_date(date.fromisoformat(g["game_date"]))
                statuses_by_date.setdefault(g["game_date"], set()).add(g["game_status"])
                conn.execute(
                    """
                    INSERT INTO team_schedule
                        (matchup_period_id, game_pk, game_date, pro_team_id,
                         opponent_pro_team_id, is_home,
                         probable_pitcher_mlbam_id, probable_pitcher_name,
                         game_status, current_inning, inning_state,
                         team_runs, opponent_runs, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(matchup_period_id, game_pk, pro_team_id) DO UPDATE SET
                        game_status=excluded.game_status,
                        current_inning=excluded.current_inning,
                        inning_state=excluded.inning_state,
                        probable_pitcher_mlbam_id=excluded.probable_pitcher_mlbam_id,
                        probable_pitcher_name=excluded.probable_pitcher_name,
                        team_runs=excluded.team_runs,
                        opponent_runs=excluded.opponent_runs,
                        fetched_at=excluded.fetched_at
                    """,
                    (period_id, g["game_pk"], g["game_date"], g["espn_team_id"],
                     g["opponent_espn_team_id"], g["is_home"],
                     g["probable_pitcher_mlbam_id"], g["probable_pitcher_name"],
                     g["game_status"], g.get("current_inning"), g.get("inning_state"),
                     g.get("team_runs"), g.get("opponent_runs"), now),
                )

            # Observed game-day activity windows. active_start is stamped the
            # first tick any game is In Progress; active_end the first tick all
            # of that day's games are Final. COALESCE keeps the earliest stamp
            # of each (first observation wins), so repeated ticks don't move it.
            for game_date, statuses in statuses_by_date.items():
                period_id = mlb.period_for_date(date.fromisoformat(game_date))
                any_live = "In Progress" in statuses
                all_final = bool(statuses) and statuses <= _FINAL_GAME_STATES
                conn.execute(
                    """
                    INSERT INTO game_day_activity
                        (matchup_period_id, game_date, active_start, active_end, updated_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(matchup_period_id, game_date) DO UPDATE SET
                        active_start = COALESCE(game_day_activity.active_start, excluded.active_start),
                        active_end   = COALESCE(game_day_activity.active_end, excluded.active_end),
                        updated_at   = excluded.updated_at
                    """,
                    (period_id, game_date,
                     now if any_live else None,
                     now if all_final else None,
                     now),
                )

            # Live pitcher lines: drop rows for games in this window that are no
            # longer in progress (finished → QS/SVHD banked into totals), then
            # replace the rows for each in-progress game with a fresh snapshot.
            stale = [pk for pk in window_pks if pk not in in_progress_pks]
            for pk in stale:
                conn.execute("DELETE FROM live_pitchers WHERE game_pk=?", (pk,))
            for pk in in_progress_pks:
                conn.execute("DELETE FROM live_pitchers WHERE game_pk=?", (pk,))
            for lp in live_rows:
                conn.execute(
                    """
                    INSERT INTO live_pitchers
                        (game_pk, mlbam_id, name, pro_team_id, order_idx, is_last,
                         games_started, outs, er, k, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (lp["game_pk"], lp["mlbam_id"], lp["name"], lp["espn_team_id"],
                     lp["order_idx"], 1 if lp["is_last"] else 0, lp["games_started"],
                     lp["outs"], lp["er"], lp["k"], now),
                )
    finally:
        conn.close()

    click.echo(
        f"Refreshed live game state: {yesterday.isoformat()}..{end.isoformat()}, "
        f"team_game_rows={len(games)}, in_progress={len(in_progress_pks)}, "
        f"live_pitcher_rows={len(live_rows)}, espn_probables_filled={espn_pp}"
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

        # Used by both future-week SP estimation and per-player RP rates.
        # Cheap (one query) so always load when we're running mc-v1.
        team_total_ros_games = (
            sim.load_total_remaining_games(conn, current, last_reg)
            if model_name == "mc-v1" else {}
        )

        # Live pitcher lines for in-progress games → in-game QS/SVHD. Empty
        # unless games are live now, in which case the sim behaves as before.
        live_by_team = (
            sim.load_live_pitchers(conn) if model_name == "mc-v1" else {}
        )

        # Anchor history for the rotation-cadence SP model (last start per
        # pitcher). Empty until pitcher_starts is populated, in which case the
        # SP estimate falls back to the flat ROS-share.
        last_start_by_pitcher = (
            sim.load_last_starts(conn) if model_name == "mc-v1" else {}
        )

        # League lineup-slot configuration for the hitter optimizer.
        lineup_slot_counts: dict[int, int] = {}
        if ss["lineup_slots_json"]:
            try:
                raw = json.loads(ss["lineup_slots_json"])
                lineup_slot_counts = {int(k): int(v) for k, v in raw.items()}
            except (json.JSONDecodeError, ValueError, TypeError):
                lineup_slot_counts = {}

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
                        team_total_ros_games=team_total_ros_games,
                        lineup_slot_counts=lineup_slot_counts,
                        live_by_team=live_by_team,
                        last_start_by_pitcher=last_start_by_pitcher,
                        # Rotation-cadence start projection ONLY for the current
                        # week. For any future week the anchor (last recorded
                        # start) is already a week-plus stale — the pitcher will
                        # start again *this* week first, and those turns aren't
                        # recorded yet — so the cadence walk snaps the first turn
                        # to day 1 of the future week and invents a 2nd, badly
                        # over-projecting (~1.9 vs a realistic ~1.3). Future weeks
                        # use the flat ROS-share split (tier B) instead.
                        use_cadence=(period_id == current),
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


@cli.command("validate")
@click.option("--all", "all_periods", is_flag=True,
              help="Check every regular-season period (default: current only).")
@click.option("--future", "future_periods", is_flag=True,
              help="Check current + future periods.")
@click.option("--list", "list_only", is_flag=True,
              help="List open (unresolved) flags and exit.")
@click.option("--resolve", "resolve_code", default=None, metavar="CODE",
              help="Mark open flags with this CODE (or 'all') resolved, and exit.")
def validate_cmd(all_periods: bool, future_periods: bool, list_only: bool,
                 resolve_code: str | None) -> None:
    """Run invariant + anomaly checks over the latest WP snapshots and record
    findings in `validation_flags`. Cheap (no sims) — safe to run every fast tick.
    Review open flags with `--list`, dismiss triaged-legit ones with
    `--resolve CODE`, and investigate the rest in Claude Code."""
    from app import validate as _v

    conn = db.connect()
    try:
        if resolve_code:
            with conn:
                if resolve_code == "all":
                    n = conn.execute(
                        "UPDATE validation_flags SET resolved=1 WHERE resolved=0").rowcount
                else:
                    n = conn.execute(
                        "UPDATE validation_flags SET resolved=1 WHERE resolved=0 AND code=?",
                        (resolve_code,)).rowcount
            click.echo(f"Resolved {n} flag(s)"
                       + ("" if resolve_code == "all" else f" with code {resolve_code}") + ".")
            return
        if list_only:
            rows = conn.execute(
                "SELECT code, matchup_id, severity, detail, occurrences, first_seen "
                "FROM validation_flags WHERE resolved=0 "
                "ORDER BY severity, last_seen DESC").fetchall()
            if not rows:
                click.echo("No open validation flags.")
                return
            click.echo(f"{len(rows)} open flag(s):")
            for r in rows:
                mid = "" if r["matchup_id"] in (None, -1) else f" m{r['matchup_id']}"
                click.echo(f"  [{r['severity']:<5}] {r['code']}{mid}  ×{r['occurrences']}  "
                           f"{r['detail']}  (since {r['first_seen'][:16]})")
            return

        cur = _current_matchup_period(conn)
        last = _last_regular_season_period(conn)
        if cur is None or last is None:
            raise click.ClickException("Missing period metadata. Run `app fetch` first.")
        if all_periods:
            periods = list(range(1, last + 1))
        elif future_periods:
            periods = list(range(cur, last + 1))
        else:
            periods = [cur]

        findings = _v.run(conn, periods)
        now = _now_iso()
        today = now[:10]
        with conn:
            for f in findings:
                mid = f.matchup_id if f.matchup_id is not None else -1
                conn.execute(
                    """
                    INSERT INTO validation_flags
                        (code, matchup_id, flag_date, severity, detail,
                         first_seen, last_seen, occurrences, resolved)
                    VALUES (?,?,?,?,?,?,?,1,0)
                    ON CONFLICT(code, matchup_id, flag_date) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        occurrences=validation_flags.occurrences+1,
                        detail=excluded.detail
                    """,
                    (f.code, mid, today, f.severity, f.detail, now, now),
                )
        errs = sum(1 for f in findings if f.severity == "error")
        click.echo(f"Validation periods {periods[0]}..{periods[-1]}: "
                   f"{errs} error(s), {len(findings) - errs} warning(s).")
        for f in findings:
            mid = "" if f.matchup_id is None else f" m{f.matchup_id}"
            click.echo(f"  [{f.severity:<5}] {f.code}{mid}: {f.detail}")
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

        first_row = conn.execute(
            "SELECT MIN(matchup_period_id) AS p FROM matchups"
        ).fetchone()
        first = first_row["p"] if first_row and first_row["p"] else 1

        teams = {
            r["id"]: dict(r) for r in conn.execute("SELECT * FROM teams").fetchall()
        }

        now = _now_iso()

        # Emit every regular-season week — past weeks stay selectable in the
        # dropdown so prior sims/graphs remain viewable. Whether a week is
        # "started" (has real scores) is data-driven: derived from its games'
        # statuses, not from the date or ESPN's current-period number.
        weeks_out = []
        for period_id in range(first, last_reg + 1):
            state = _week_state(conn, period_id)
            started = state != "upcoming"
            start, end = mlb.matchup_period_window(period_id)
            ms = conn.execute(
                "SELECT * FROM matchups WHERE matchup_period_id=? ORDER BY id",
                (period_id,),
            ).fetchall()
            matchups_out = [
                _matchup_block(conn, teams, m, started=started)
                for m in ms
            ]
            weeks_out.append({
                "matchup_period_id": period_id,
                "label": f"Week {period_id}",
                "start": start.isoformat(),
                "end": end.isoformat(),
                # "state" drives the UI's default week selection.
                "state": state,
                # Observed game-day windows for the chart's "Active" x-axis.
                "active_intervals": _active_intervals(conn, period_id, now),
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
            "generated_at": now,
            "weeks": weeks_out,
        }
        out_path = Path(__file__).resolve().parent.parent / "docs" / "data.json"
        out_path.write_text(json.dumps(out, indent=2))
        click.echo(
            f"Wrote {out_path} ({out_path.stat().st_size} bytes) — "
            f"{len(weeks_out)} weeks (periods {first}..{last_reg})"
        )
    finally:
        conn.close()


# MLB detailedState values that mean a game is over.
_FINAL_GAME_STATES = {"Final", "Game Over", "Completed Early"}

# Max WP-over-time points embedded per matchup, per model version, in
# data.json. The DB keeps every snapshot (so no history is ever lost); we
# only thin what the static site downloads. ~200 points is far more than the
# ~640px-wide chart can resolve, so downsampled graphs look identical to full.
MAX_HISTORY_POINTS = 200


def _downsample_history(history: list[dict],
                        max_points: int = MAX_HISTORY_POINTS) -> list[dict]:
    """Thin a matchup's snapshot history for the published payload.

    Grouped by model_version (the chart only ever plots one model's series),
    each group is reduced to evenly-spaced points that always include its
    first and last. Nothing is deleted from the DB — this only shrinks
    data.json so past weeks can keep their graphs without unbounded growth.
    """
    by_ver: dict[str, list[dict]] = {}
    for h in history:
        by_ver.setdefault(h["model_version"], []).append(h)

    def evenly(rows: list[dict]) -> list[dict]:
        n = len(rows)
        if n <= max_points:
            return rows
        step = (n - 1) / (max_points - 1)
        idx = sorted({round(i * step) for i in range(max_points)})
        return [rows[i] for i in idx]

    out = [h for rows in by_ver.values() for h in evenly(rows)]
    out.sort(key=lambda h: h["computed_at"])
    return out


def _week_state(conn, period_id: int) -> str:
    """Data-driven status of a matchup week, from its games' statuses:

      - "upcoming": no game has started (all Scheduled/Pre-Game) — show as projection
      - "live":     at least one game started, but not all are final
      - "final":    every game is over

    Used by the UI to pick the default week (the latest non-"upcoming" one)
    without consulting any wall clock.
    """
    # ESPN-finalized weeks: every matchup has a decided winner. This also
    # covers older weeks whose team_schedule rows have since been deleted
    # (refresh-schedule only retains current+future weeks).
    winners = [
        r["winner"] for r in conn.execute(
            "SELECT winner FROM matchups WHERE matchup_period_id=?", (period_id,)
        ).fetchall()
    ]
    if winners and all(w and w != "UNDECIDED" for w in winners):
        return "final"

    # Otherwise derive from game statuses — handles the just-finished week
    # (games final but ESPN hasn't set the winner yet), live, and upcoming.
    rows = conn.execute(
        "SELECT DISTINCT game_status FROM team_schedule WHERE matchup_period_id=?",
        (period_id,),
    ).fetchall()
    statuses = [r["game_status"] for r in rows]
    if not statuses:
        return "upcoming"
    started = any(
        s == "In Progress" or s in _FINAL_GAME_STATES for s in statuses
    )
    if not started:
        return "upcoming"
    return "final" if all(s in _FINAL_GAME_STATES for s in statuses) else "live"


def _active_intervals(conn, period_id: int, now: str) -> list[dict]:
    """Observed game-day windows for a period, for the chart's "Active" x-axis.

    One entry per game-day that has started, ordered in time. A day still in
    progress (no active_end yet) is left open-ended at `now` so the live day's
    interval extends to the latest snapshot.
    """
    rows = conn.execute(
        """
        SELECT game_date, active_start, active_end
        FROM game_day_activity
        WHERE matchup_period_id=? AND active_start IS NOT NULL
        ORDER BY active_start
        """,
        (period_id,),
    ).fetchall()
    return [
        {
            "date": r["game_date"],
            "start": r["active_start"],
            "end": r["active_end"] or now,
        }
        for r in rows
    ]


def _matchup_block(conn, teams: dict, m, *, started: bool) -> dict:
    """One matchup with team blocks, current snapshot, and history.

    `started` = the week has begun (state != "upcoming"); when False the team
    blocks emit null scores/records so the UI shows dashes for a pure projection.
    """
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
    history = _downsample_history(history)
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
                            started=started),
        "away": _team_block(teams, away_team_id, away_state,
                            wp_row["away_wp"] if wp_row else None,
                            started=started),
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
                wp: float | None, *, started: bool) -> dict:
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
            # Upcoming weeks haven't started — emit nulls so the UI shows dashes.
            score = s.get("score") if started else None
            result = s.get("result") if started else None
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
        "record": record if started else None,
        "batting": block(stats.BATTING_STAT_IDS),
        "pitching": block(stats.PITCHING_STAT_IDS),
    }
