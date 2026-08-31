"""CLI: app init-db / fetch / compute / publish."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import click

from app import (LEAGUE_ID, SEASON_ID, db, espn, espn_public, mlb, model, names, pages,
                 sim, stats)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settle_boundary() -> str:
    """The unsettled-window cutoff date (games on/after it aren't in ESPN's banked
    totals yet) computed against the current UTC time. See `sim.settle_boundary_date`."""
    return sim.settle_boundary_date(datetime.now(timezone.utc))


# Rate categories (OPS/ERA/WHIP) — derived ratios that legitimately move either
# direction, so they're never monotonicity-guarded.
_RATE_STAT_IDS = frozenset(sim.RATE_DERIVERS)

# Single source of truth for the category_state cell upsert (column list).
_CATEGORY_STATE_UPSERT = (
    "INSERT OR REPLACE INTO category_state "
    "(matchup_id, team_id, stat_id, score, result, fetched_at) VALUES (?,?,?,?,?,?)"
)

# MLB detailedState values that mean a game is over (canonical set lives in sim).
_FINAL_GAME_STATES = sim.FINAL_GAME_STATES

# The published-site artifact. Module-level so the golden-week test can point
# publish + the site validation checks at a temp file instead of the real docs/.
DOCS_DATA_JSON = Path(__file__).resolve().parent.parent / "docs" / "data.json"


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
    conn.execute(_CATEGORY_STATE_UPSERT, (mid, tid, sid, score, result, now))
    return True


def _write_noncurrent_score(conn, prev: dict, mid: int, tid: int, sid: int,
                            score, result, now: str) -> bool:
    """Upsert one past/future-period category_state cell, skipping the write when
    the value is unchanged from the latest stored one.

    Settled past weeks never change and future weeks are all-zero, yet `fetch` runs
    every 5 min — without this guard each tick re-INSERTed an identical snapshot of
    every non-current matchup (~2,600 duplicate rows/tick). That bloated
    category_state to ~21M rows / 3.8 GB and slowed every reader. A rare late ESPN
    correction to an already-settled week still lands, because its (score, result)
    then differs from `prev`. `prev` is keyed (matchup_id, team_id, stat_id) →
    (score, result), latest value per cell. Returns True if a row was written."""
    if prev.get((mid, tid, sid)) == (score, result):
        return False
    conn.execute(_CATEGORY_STATE_UPSERT, (mid, tid, sid, score, result, now))
    return True


# How long after a game goes Final the "closing scrape" keeps running. The scrape
# banks QS/SVHD the instant a game reads Final, but the last games of a night tend
# to finish together — once none are In Progress the scrape stops and that final
# credit waits ~3h for the ~07:00 REST settle. ~4 fast ticks of overlap closes it
# with ESPN's own number. See the `scrape_due` block in `fetch`.
CLOSING_SCRAPE_WINDOW_MIN = 20


def _scrape_due(conn, period_id: int, now_iso: str) -> tuple[bool, int]:
    """Whether a live DOM scrape should run this tick, plus the in-progress count.

    Two cases:
      - any MLB game **In Progress** — the ordinary live-slate case; or
      - a game went **Final** within `CLOSING_SCRAPE_WINDOW_MIN` — the *closing
        scrape* (added 2026-08-09).

    Why the second exists: the scrape banks QS/SVHD the instant a game reads Final,
    so during a slate ESPN's number is current within one tick. But the last games
    of a night tend to finish together — all six of 2026-08-08's went Final at
    04:05:02 — and the moment none are In Progress the scrape stops, so a credit
    posted by that final game sat un-banked until the ~07:00 REST settle ~3h later.
    That window is the *only* thing the QS/SVHD floor/archive reconstruction ever
    existed to bridge, and that reconstruction measured ~8% over-counting against
    settled week 17 (always high, and `max()` made it permanent). One extra scrape
    closes the window with ESPN's own authoritative number instead.

    Deliberately **stateless** — no "last scrape ran at" marker. If the pipeline is
    down longer than the window we fall back to the 07:00 settle, exactly as before;
    strictly no worse than the old behavior, with nothing new to keep in sync.

    Callers use the flag for BOTH the scrape gate and `_scrape_owns_display_cat`, so
    REST never claims a cat the scrape is about to write. `team_schedule` is fresh
    (refresh-live runs first), so `became_final_at` already reflects this tick.
    """
    in_progress = conn.execute(
        "SELECT COUNT(*) FROM team_schedule "
        "WHERE matchup_period_id=? AND game_status='In Progress'",
        (period_id,),
    ).fetchone()[0]
    try:
        cutoff = (datetime.fromisoformat(now_iso)
                  - timedelta(minutes=CLOSING_SCRAPE_WINDOW_MIN)).isoformat()
    except (TypeError, ValueError):
        return bool(in_progress), in_progress
    just_final = conn.execute(
        "SELECT COUNT(*) FROM team_schedule "
        "WHERE matchup_period_id=? AND became_final_at IS NOT NULL "
        "AND became_final_at >= ?",
        (period_id, cutoff),
    ).fetchone()[0]
    return bool(in_progress or just_final), in_progress


def _scrape_owns_display_cat(stat_id: int, scrape_due: bool) -> bool:
    """Whether the live DOM scrape (not REST) owns this current-period display cat
    this tick — i.e. REST must skip writing it.

    - **Rate cats** (OPS/ERA/WHIP) are derived from components at publish time, so
      the scraped rate value is never used — REST always skips them.
    - **Counting display cats** (K/QS/SVHD) are scrape-owned only *while a scrape
      is due this tick* (`scrape_due`: a game In Progress, or one that went Final
      inside `CLOSING_SCRAPE_WINDOW_MIN`). Otherwise the scrape is skipped and REST
      is the authoritative *final* source, so REST reconciles them (through the
      monotonicity guard — it can only ratchet up). This captures a stat applied
      after the last live scrape, e.g. a two-way player's pitching line credited at
      game-final (Ohtani K 23→29, 2026-06-05). League-wide gate: an early-finishing
      matchup waits until the whole slate is idle, then reconciles next tick.

    Takes `scrape_due` rather than raw "games in progress" so ownership tracks the
    scrape exactly. On the closing-scrape ticks the scrape runs with nothing In
    Progress; keying off in-progress there would hand REST ownership of the very
    cats the scrape is about to write, and the two would disagree."""
    if stat_id in _RATE_STAT_IDS:
        return True
    return bool(scrape_due)


def _overlay_espn_probables(games: list[dict], start, end) -> int:
    """Fill `probable_pitcher_name` from ESPN's public feed for games where MLB
    hasn't posted one yet — ESPN's feed leads MLB statsapi by a day or two.
    Fill-only: MLB wins once it posts (we never overwrite an existing probable),
    so the tentative ESPN listing is just an early stand-in. Best-effort: a feed
    error leaves the MLB data untouched. Returns the number of games filled.

    Doubleheader guard: `espn_public.fetch_probables` is keyed by
    `(game_date, pro_team_id)` with ONE probable per key, so on a doubleheader
    date it can't tell the team's two games apart — a blind fill would smear
    that single ESPN name across BOTH games, masking the still-open game (and
    possibly overriding the one MLB will start with someone else). So we skip
    the overlay for any `(date, team)` that has more than one game and leave
    those to MLB: the started game gets MLB's real probable, the open game
    stays open until MLB names it. Single-game days are unaffected.

    Min-rest conflict guard (2026-08-24): **the two feeds can disagree about
    which DAY a starter goes, and fill-only merging cannot see the conflict.**
    Observed live on two teams the same tick — MLB had Misiorowski on 08-27 and
    Sale on 08-27, while ESPN's rotation slotted an extra arm there and pushed
    both to 08-28. Since MLB had not named 08-28, the overlay filled ESPN's guess
    and the same pitcher ended up probable on consecutive days. Nothing
    downstream catches that: `_cap_extra_dist` clips only the *cadence* piece and
    always respects announced starts, so both were credited and the two teams
    projected 2.00 starts where 1 was possible (`INV_SP_STARTS_IMPOSSIBLE`).

    So an overlay fill is skipped when that pitcher is already probable for the
    same team within `sim.MIN_REST_DAYS`. MLB's row always wins because we only
    ever skip the *fill*. Accepted fills join the claim set too, so ESPN cannot
    duplicate a name inside its own un-announced tail either. Leaving the day
    genuinely open is the right outcome — the cadence model can then project a
    real candidate for it.

    Widening `refresh-live`'s window (2026-08-18) took overlay fills from ~27 to
    ~97 per tick, i.e. far more of ESPN's speculative tail, which is where these
    disagreements live — this surfaced within a week of that change.
    """
    try:
        esp = espn_public.fetch_probables(start, end)
    except Exception:  # noqa: BLE001 — best effort; fall back to MLB-only
        return 0
    # Count this team's games per date so doubleheaders (>1) are left to MLB.
    games_per_day_team: dict[tuple, int] = {}
    for g in games:
        key = (g["game_date"], g["espn_team_id"])
        games_per_day_team[key] = games_per_day_team.get(key, 0) + 1
    # Days each pitcher is ALREADY claimed on, per team — seeded from the
    # probables MLB has posted, then extended by each fill we accept.
    claimed: dict[int, list[tuple[date, str]]] = {}
    for g in games:
        nm = names.norm_name(g.get("probable_pitcher_name"))
        if not nm:
            continue
        try:
            gd = date.fromisoformat(g["game_date"])
        except (ValueError, KeyError, TypeError):
            continue
        claimed.setdefault(g["espn_team_id"], []).append((gd, nm))

    def _conflicts(team_id: int, gd: date, nm: str) -> bool:
        return any(other_nm == nm and abs((gd - other_d).days) < sim.MIN_REST_DAYS
                   for other_d, other_nm in claimed.get(team_id, ()))

    n = 0
    # Date order so an accepted fill is visible to later candidates.
    for g in sorted(games, key=lambda x: x.get("game_date") or ""):
        if g.get("probable_pitcher_name"):
            continue
        key = (g["game_date"], g["espn_team_id"])
        if games_per_day_team[key] > 1:
            continue  # doubleheader — ESPN can't disambiguate; leave to MLB
        name = esp.get(key)
        if not name:
            continue
        nm = names.norm_name(name)
        try:
            gd = date.fromisoformat(g["game_date"])
        except (ValueError, KeyError, TypeError):
            continue
        if nm and _conflicts(g["espn_team_id"], gd, nm):
            continue  # feeds disagree on his day — keep MLB's, leave this open
        g["probable_pitcher_name"] = name
        if nm:
            claimed.setdefault(g["espn_team_id"], []).append((gd, nm))
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
    # Ensure the schema matches the running code before any subcommand runs. With
    # the editable install a code edit goes live on the next cron tick immediately,
    # but migrations only ran on demand — so a schema-touching change could crash a
    # tick that referenced a not-yet-created table/column (it did, 2026-06-10: the
    # reliever_appearances rollout). This idempotent init (CREATE IF NOT EXISTS +
    # guarded ALTERs, a few ms) closes that window so code and schema can't drift,
    # even for a single tick.
    db.init()


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
        # Which current-period matchups already have category_state rows (so we know
        # whether to seed them from REST). Probe each matchup_id directly — an index
        # equality seek on idx_category_state_recent — rather than the old
        # `... JOIN matchups WHERE matchup_period_id=?`, which couldn't use that
        # matchup_id-led index and full-scanned all of category_state (~29s once the
        # table grew to ~21M rows: the bulk of every idle `fetch` tick).
        current_matchup_ids = {
            m["matchup_id"] for m in matchups
            if m["matchup_period_id"] == current_period
        }
        seeded_current = {
            mid for mid in current_matchup_ids
            if conn.execute(
                "SELECT 1 FROM category_state WHERE matchup_id=? LIMIT 1", (mid,)
            ).fetchone()
        }
        # Latest stored value per non-current (matchup,team,stat), for the dedup in
        # `_write_noncurrent_score` below. After the one-time prune this scans only
        # the few thousand settled/future rows; see that helper for the rationale.
        prev_noncurrent: dict = {}
        for r in conn.execute(
            "SELECT cs.matchup_id, cs.team_id, cs.stat_id, cs.score, cs.result, "
            "MAX(cs.fetched_at) FROM category_state cs "
            "JOIN matchups m ON m.id = cs.matchup_id "
            "WHERE m.matchup_period_id <> ? "
            "GROUP BY cs.matchup_id, cs.team_id, cs.stat_id",
            (current_period,),
        ).fetchall():
            prev_noncurrent[(r["matchup_id"], r["team_id"], r["stat_id"])] = (
                r["score"], r["result"],
            )
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

        scrape_due, in_progress = _scrape_due(conn, current_period, now)

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
                sid = s["stat_id"]
                if (cur_p and seeded and sid in display_cats
                        and _scrape_owns_display_cat(sid, scrape_due)):
                    continue  # scrape owns these now; REST only fills the rest
                if cur_p:
                    _write_category_score(conn, last_good, m["matchup_id"],
                                          s["team_id"], sid,
                                          s["score"], s["result"], now)
                else:
                    _write_noncurrent_score(conn, prev_noncurrent, m["matchup_id"],
                                            s["team_id"], sid,
                                            s["score"], s["result"], now)
        conn.commit()

        # Override the (lagging) REST scoreByStat for the CURRENT period with
        # values scraped from ESPN's web UI. The UI gets real-time updates via
        # FastCast WebSocket; the REST endpoint we just polled can be 5-30 min
        # stale during live games. Fall back to REST data silently if the
        # scrape errors or returns nothing (auth wall, profile not set up).
        # Only scrape when games are actually in progress (computed above). With
        # nothing live, ESPN's REST scores are current and the ~15-60s headless-
        # browser scrape is pure overhead (and the only place an idle-window
        # runtime spike can come from).
        scraped_count = 0
        scraped = {}
        scrape_skipped_idle = not scrape_due
        if scrape_due:
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

        # Fetch-time health: a live scrape that silently produced nothing rots the
        # display cats while games are live. Flag it here (recorded into
        # validation_flags so it shows in `app validate --list`) — `validate` itself
        # can't see whether a scrape was attempted.
        from app import validate as _v
        health = _v.check_scrape_health(in_progress, scraped_count)
        # ...and its blind spot: a scrape that returns a full set of cells that
        # never change. Needs the DB (a frozen run is only visible across ticks),
        # so it takes `conn` rather than the two counters.
        health += _v.check_scrape_staleness(
            conn, in_progress, shape.current_matchup_period, now)
        if health:
            _v.persist(conn, health, now)
            for f in health:
                click.echo(f"  [{f.severity}] {f.code}: {f.detail}", err=True)
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
        msg += (", scrape skipped (no games live, none final in the last "
                f"{CLOSING_SCRAPE_WINDOW_MIN} min)")
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
                # Write-once ROS archive so this week's projection inputs survive
                # the next fetch's overwrite (see db.SCHEMA: without it the model
                # can never be scored against history). INSERT OR IGNORE gives
                # "first write per period wins" — a later refresh this week must
                # not drag the archive toward mid-week values.
                if pr["split_id"] == sim.ROS_SPLIT_ID:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO ros_projection_archive
                            (matchup_period_id, player_id, stat_id, value,
                             season_id, captured_at)
                        VALUES (?,?,?,?,?,?)
                        """,
                        (period_id, pr["player_id"], pr["stat_id"],
                         pr["value"], pr["season_id"], now),
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
    from app import playoffs as playoffs_mod
    shape = espn.fetch_league_shape()
    current = shape.current_matchup_period
    last = shape.last_regular_season_period
    now = _now_iso()

    total_games = 0
    total_starts = 0
    total_espn_pp = 0
    conn = db.connect()
    try:
        # Through the playoff rounds too (periods last+1..last+3, still inside
        # MLB's regular season) — `app playoffs` builds bracket-week budgets
        # from these slates.
        for period_id in range(current, last + playoffs_mod.NUM_PLAYOFF_PERIODS + 1):
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
        f"Refreshed schedule: periods {current}..{last + playoffs_mod.NUM_PLAYOFF_PERIODS}, "
        f"team_game_rows={total_games}, starts_recorded={total_starts}, "
        f"espn_probables_filled={total_espn_pp}"
    )


@cli.command("backfill-lineups")
@click.option("--days", type=int, default=14, show_default=True,
              help="How many days back (ending yesterday) to re-pull.")
@click.option("--dry-run", is_flag=True, help="Report differences without writing.")
def backfill_lineups(days: int, dry_run: bool) -> None:
    """Re-pull past days' locked lineups from ESPN and correct `daily_lineups`.

    The forward fix lives in `refresh-live` (`_authoritative_lineups`); this
    repairs days already recorded from a pre-lock snapshot. A wrong *active*
    slot is the expensive direction: slots gate which box-score lines count for a
    team, so one stale row skews the reconstruction. (2026-08-10: Baker's 08-04
    bench slot recorded as 15 → Swamp Dragons SVHD 4 vs ESPN's 3, via the
    QS/SVHD settled floor. That floor was deleted 2026-08-11 — QS/SVHD now come
    straight from ESPN — so the counting-cat blast radius is gone, but slots still
    gate the ERA/WHIP/OPS component reconstruction, where a wrong slot silently
    mis-attributes innings.)

    Only rewrites a day when ESPN returns a non-empty lineup for it, and
    reports every slot that actually changed. Idempotent.
    """
    conn = db.connect()
    try:
        today = datetime.now(timezone.utc).date()
        now = _now_iso()
        changed = scanned = 0
        for i in range(days, 0, -1):
            gd = (today - timedelta(days=i)).isoformat()
            before = {(r[0], r[1]): r[2] for r in conn.execute(
                "SELECT fantasy_team_id, player_id, lineup_slot_id "
                "FROM daily_lineups WHERE game_date=?", (gd,))}
            if not before:
                continue          # never captured — not ours to invent
            scanned += 1
            rows = _authoritative_lineups([gd]).get(gd)
            if not rows:
                click.echo(f"  {gd}: ESPN returned nothing, left as-is")
                continue
            diffs = [(e, before.get((e["fantasy_team_id"], e["player_id"])))
                     for e in rows]
            diffs = [(e, old) for e, old in diffs if old != e["lineup_slot_id"]]
            for e, old in diffs:
                click.echo(f"  {gd}: team {e['fantasy_team_id']} "
                           f"{e['full_name']} slot {old} -> {e['lineup_slot_id']}")
            if not dry_run:
                with conn:
                    _replace_daily_lineups(conn, gd, rows, now)
            changed += len(diffs)
        click.echo(f"{'Would correct' if dry_run else 'Corrected'} {changed} "
                   f"slot(s) over {scanned} day(s).")
    finally:
        conn.close()


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


# Shared so the became_final_at stamping logic is tested without re-fetching games.
# became_final_at = the first tick a game read Final (the credit boundary); COALESCE
# keeps that first stamp across later ticks, and only sets it when the inserted value
# (excluded.became_final_at = now iff the new status is Final) is non-null.
_TEAM_SCHEDULE_UPSERT = """
    INSERT INTO team_schedule
        (matchup_period_id, game_pk, game_date, pro_team_id,
         opponent_pro_team_id, is_home,
         probable_pitcher_mlbam_id, probable_pitcher_name,
         game_status, current_inning, inning_state,
         team_runs, opponent_runs, became_final_at, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(matchup_period_id, game_pk, pro_team_id) DO UPDATE SET
        game_status=excluded.game_status,
        current_inning=excluded.current_inning,
        inning_state=excluded.inning_state,
        probable_pitcher_mlbam_id=excluded.probable_pitcher_mlbam_id,
        probable_pitcher_name=excluded.probable_pitcher_name,
        team_runs=excluded.team_runs,
        opponent_runs=excluded.opponent_runs,
        became_final_at=COALESCE(team_schedule.became_final_at, excluded.became_final_at),
        fetched_at=excluded.fetched_at
"""


def _archive_final_lines(conn, lines, status_by_pk, date_by_pk, now) -> int:
    """Write-once copy of each Final game's pitcher line into `pitcher_final_lines`,
    so it survives the live_pitchers prune (telemetry for QS/SVHD credit audits).
    INSERT OR IGNORE keyed by (game_pk, mlbam_id) → the first Final-tick capture
    wins and later ticks are no-ops. Returns the number of new rows archived."""
    n = 0
    for lp in lines:
        pk = lp["game_pk"]
        if not (status_by_pk.get(pk, set()) & _FINAL_GAME_STATES):
            continue   # only archive Final games — in-progress lines still move
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO pitcher_final_lines
                (game_pk, mlbam_id, name, pro_team_id, game_date, games_started,
                 outs, er, k, p_h, p_bb, sv, hld, final_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (pk, lp["mlbam_id"], lp["name"], lp["espn_team_id"], date_by_pk.get(pk),
             lp["games_started"], lp["outs"], lp["er"], lp["k"],
             lp["p_h"], lp["p_bb"], lp.get("sv") or 0, lp.get("hld") or 0, now),
        )
        n += cur.rowcount
    return n


def _archive_final_batter_lines(conn, lines, status_by_pk, date_by_pk, now) -> int:
    """Hitter analogue of `_archive_final_lines` — write-once copy of each Final
    game's batter line into `batter_final_lines`, so it survives the live_batters
    prune. Without it a hitter's actual games-played and per-game rates were
    unrecoverable after the fact, which is why hitter accuracy could only be
    inferred via the unit-free ratio trick. Same INSERT OR IGNORE keyed by
    (game_pk, mlbam_id): first Final-tick capture wins."""
    n = 0
    for lb in lines:
        pk = lb["game_pk"]
        if not (status_by_pk.get(pk, set()) & _FINAL_GAME_STATES):
            continue   # only archive Final games — in-progress lines still move
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO batter_final_lines
                (game_pk, mlbam_id, name, pro_team_id, game_date,
                 ab, h, b2, b3, hr, bb, hbp, sf, r, sb, final_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (pk, lb["mlbam_id"], lb["name"], lb["espn_team_id"], date_by_pk.get(pk),
             lb["ab"], lb["h"], lb["b2"], lb["b3"], lb["hr"], lb["bb"],
             lb["hbp"], lb["sf"], lb.get("r") or 0, lb.get("sb") or 0, now),
        )
        n += cur.rowcount
    return n


def _track_reliever_appearances(conn, lines, margin_by_game_team, now) -> None:
    """Persist each reliever's entry/exit run-margin across ticks, so the in-game
    SVHD model can judge a save/hold from the conditions WHEN HE PITCHED rather than
    the live score (see db.reliever_appearances). Entry margin is recorded once — the
    first tick we see him pitching (`is_last`); exit margin once — the first tick he's
    no longer pitching. Starters (`games_started`) are skipped: they can't earn SV/HLD.
    A reliever first seen already exited (entry tick missed) gets no row, and the
    override falls back to the live margin (no regression)."""
    for lp in lines:
        if lp.get("games_started"):
            continue
        pk, tid = lp["game_pk"], lp["espn_team_id"]
        margin = margin_by_game_team.get((pk, tid))
        if margin is None:
            continue
        if lp.get("is_last"):
            conn.execute(
                "INSERT OR IGNORE INTO reliever_appearances "
                "(game_pk, mlbam_id, name, pro_team_id, entry_margin, entered_at) "
                "VALUES (?,?,?,?,?,?)",
                (pk, lp["mlbam_id"], lp["name"], tid, margin, now))
        else:  # exited — stamp the exit margin once (first tick no longer pitching)
            conn.execute(
                "UPDATE reliever_appearances SET exit_margin=?, exited_at=? "
                "WHERE game_pk=? AND mlbam_id=? AND exit_margin IS NULL",
                (margin, now, pk, lp["mlbam_id"]))


def _live_recon_block(since_date, hdec, adec):
    """Compact per-snapshot record of the live-component reconciliation, persisted in
    details_json (investigation telemetry). For QS/SVHD each decision carries
    scrape/floor/box(qs_added|svhd_added)/result; rate groups carry their accept
    verdict — so 'why is current QS=N this tick, and was it scrape, floor or box?'
    is a one-row lookup instead of a reverse-engineering exercise. None when no live
    games were reconciled (keeps the key absent rather than writing empty noise)."""
    if not hdec and not adec:
        return None
    return {"since_date": since_date, "home": hdec, "away": adec}


def _authoritative_lineups(game_dates: list[str]) -> dict[str, list[dict]]:
    """ESPN's own locked lineup for each of `game_dates`, keyed by date.

    Asks `mRoster` per day via that day's `scoringPeriodId` instead of reading
    the *current* lineup once and stamping it on every date. Two bugs that fixes:

    1. **Pre-lock capture.** The old rule kept each day's *first* snapshot, on
       the assumption it was taken at/after first pitch. A tick that lands
       before ESPN locks records the manager's *pre-game intent*, and
       `INSERT OR IGNORE` then froze it permanently. Real cost (found
       2026-08-10): Bryan Baker was snapshotted in slot 15 at 22:40:04Z on
       08-04 but ESPN has him on the **bench** that day, so his save was ours
       to credit and ESPN's to withhold — the (since-removed) QS/SVHD settled
       floor counted it and the site published Swamp Dragons SVHD 4 against
       ESPN's 3, turning a lost category into a tie. Two week-17 credits (Wacha 07-31, Abreu
       07-29) were wrong the same way; across weeks 1..18 these three were the
       *only* disagreements between the floor and ESPN's scrape.
    2. **Cross-day smearing.** The current-day lineup was written for *every*
       date in the window, so a day whose own snapshot was missing silently
       inherited a different day's slots.

    Per-day, so one bad response can't poison the others; an empty/failed fetch
    drops that date from the result and leaves its stored rows untouched.
    """
    out: dict[str, list[dict]] = {}
    for gd in game_dates:
        try:
            spid = mlb.scoring_period_for_date(date.fromisoformat(gd))
            rows = espn.fetch_daily_lineups(spid)
        except Exception:  # noqa: BLE001 — auth/REST hiccup → keep what we have
            continue
        if rows:
            out[gd] = rows
    return out


def _replace_daily_lineups(conn, game_date: str, rows: list[dict], now: str) -> None:
    """Overwrite `game_date`'s stored lineup with ESPN's authoritative one.

    A full replace (not an upsert) because a player dropped from a roster
    mid-week would otherwise keep a stale active-slot row and stay creditable.
    Caller guarantees `rows` is non-empty, so this never blanks a day.
    """
    conn.execute("DELETE FROM daily_lineups WHERE game_date=?", (game_date,))
    conn.executemany(
        """
        INSERT INTO daily_lineups
            (game_date, fantasy_team_id, player_id, lineup_slot_id, fetched_at)
        VALUES (?,?,?,?,?)
        """,
        [(game_date, e["fantasy_team_id"], e["player_id"],
          e["lineup_slot_id"], now) for e in rows],
    )


LIVE_FORWARD_MAX_DAYS = 7   # caps the reach on a LONG_MATCHUPS fortnight


def _live_window(today: date) -> tuple[date, date]:
    """(start, end) MLB game dates for `refresh-live`, from a UTC `today`.

    Start is always yesterday (late West-Coast games are dated to the prior
    day). End reaches the current matchup period's last day, so every probable
    that can still affect THIS week's projection is refreshed each tick — but:
      * never shorter than the old `today+2`, so late in a week (when the
        period ends tomorrow) the near-term reach is unchanged, and
      * capped at `LIVE_FORWARD_MAX_DAYS`, so an All-Star `LONG_MATCHUPS`
        fortnight can't quietly turn one tick into a 14-day upsert.
    Pure so the boundaries are testable without a network call.
    """
    period_end = mlb.matchup_period_window(mlb.period_for_date(today))[1]
    end = max(today + timedelta(days=2),
              min(period_end, today + timedelta(days=LIVE_FORWARD_MAX_DAYS)))
    return today - timedelta(days=1), end


@cli.command("refresh-live")
def refresh_live() -> None:
    """Upsert recent + near-future MLB games' status + inning state into
    team_schedule.

    Window = `_live_window`: yesterday … the end of the current matchup period
    (never less than today+2). Yesterday covers in-progress games from the
    previous MLB calendar day (any timezone east of US Pacific rolls over local
    "today" while West Coast night games are still being played and dated to the
    prior day).

    The forward reach refreshes the rest of the week's *probable pitchers* every
    fast tick. It used to stop at today+2, which left days 3-6 of a period
    updatable only by the daily `refresh-schedule` — so a probable that MLB or
    the ESPN overlay posted mid-day sat unseen for up to 24h. That is not
    hypothetical: on 2026-08-17 the back half of period 20 had no probables at
    all (ESPN's horizon is ~5 days, so 08-23 was still beyond it), and the two
    second starts that appeared the moment the horizon advanced were worth
    ~4pp of WP to Bear Nation in one tick. Widening lets those land within 5
    minutes instead of at the next 04:02Z daily run.

    Cost is ~nil: still ONE MLB statsapi call (a wider date range), and boxscore
    fetches are gated on `{"In Progress"} | _FINAL_GAME_STATES`, which future
    games never match — so no extra per-game requests. It also avoids
    `refresh-schedule`'s DELETE+INSERT, which is why the freshness is bought
    here rather than by running the daily job twice (that path resets
    `became_final_at`, the closing-scrape credit boundary).

    No DELETE, just upserts on the existing rows.
    """
    today = datetime.now(timezone.utc).date()   # UTC, not host-local (tz-independent)
    yesterday, end = _live_window(today)
    games = mlb.fetch_schedule(yesterday, end)
    espn_pp = _overlay_espn_probables(games, yesterday, end)
    now = _now_iso()

    # Per game-date: distinct game statuses, for the activity tracker below.
    statuses_by_date: dict[str, set[str]] = {}

    # Fetch live boxscores *before* opening the write transaction (network I/O
    # shouldn't hold the DB lock). A boxscore failure just means no live
    # components / in-game QS-SVHD for that game — fall back silently.
    #
    # We keep box-score lines for every "unsettled" game (game_date on/after the
    # ESPN settle boundary): in-progress AND recently-Final ones whose stats
    # ESPN's once-daily REST hasn't absorbed yet. Re-fetching them each tick
    # keeps a just-Final game's true final line current (cheap: only the last
    # ~1-2 days of games, and only ones with data). Older games are pruned —
    # they're already in ESPN's banked totals, so the sim reads them from there.
    settle_boundary = _settle_boundary()
    window_pks = {g["game_pk"] for g in games}
    status_by_pk: dict[int, set] = {}
    date_by_pk: dict[int, str] = {}
    for g in games:
        status_by_pk.setdefault(g["game_pk"], set()).add(g["game_status"])
        date_by_pk[g["game_pk"]] = g["game_date"]
    in_progress_pks = sorted(pk for pk, st in status_by_pk.items()
                             if "In Progress" in st)
    unsettled_pks = {pk for pk in window_pks
                     if date_by_pk[pk] >= settle_boundary}
    _has_data = {"In Progress"} | _FINAL_GAME_STATES
    fetch_pks = sorted(pk for pk in unsettled_pks
                       if status_by_pk[pk] & _has_data)
    live_p: list[dict] = []
    live_b: list[dict] = []
    for pk in fetch_pks:
        try:
            bs = mlb.fetch_boxscore(pk)
            live_p.extend(bs["pitchers"])
            live_b.extend(bs["batters"])
        except Exception:  # noqa: BLE001 — best-effort; REST hiccup → skip game
            pass

    # Snapshot each game-day's locked fantasy lineups (who counts for whom).
    # Fetched PER DAY via ESPN's own `scoringPeriodId`, and REPLACED each tick.
    lineup_dates = sorted({date_by_pk[pk] for pk in fetch_pks})
    lineups_by_date = _authoritative_lineups(lineup_dates)

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
                final_at = now if g["game_status"] in _FINAL_GAME_STATES else None
                conn.execute(
                    _TEAM_SCHEDULE_UPSERT,
                    (period_id, g["game_pk"], g["game_date"], g["espn_team_id"],
                     g["opponent_espn_team_id"], g["is_home"],
                     g["probable_pitcher_mlbam_id"], g["probable_pitcher_name"],
                     g["game_status"], g.get("current_inning"), g.get("inning_state"),
                     g.get("team_runs"), g.get("opponent_runs"), final_at, now),
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

            # Live box-score lines (pitchers + batters). Prune games ESPN has now
            # settled or that fell out of the window; refresh each fetched game's
            # rows. Both tables are keyed by game_pk so a per-game DELETE + insert
            # keeps the latest snapshot (player sets only grow within a game).
            existing = {r[0] for r in conn.execute(
                "SELECT game_pk FROM live_pitchers "
                "UNION SELECT game_pk FROM live_batters")}
            for pk in (existing - unsettled_pks) | set(fetch_pks):
                conn.execute("DELETE FROM live_pitchers WHERE game_pk=?", (pk,))
                conn.execute("DELETE FROM live_batters WHERE game_pk=?", (pk,))
            # Upsert (not plain INSERT): MLB statsapi occasionally repeats a
            # personId within one game's `batters`/`pitchers` array (re-entry /
            # batting-order shuffle — e.g. 2026-06-28 game 824256 listed Matt
            # Vierling & Ben Malgeri twice), so the line can arrive twice in one
            # tick. The repeated line is identical, so last-write-wins is correct
            # (summing would double-count); ON CONFLICT also makes the whole
            # write idempotent regardless of the duplicate's source.
            for lp in live_p:
                conn.execute(
                    """
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
                    """,
                    (lp["game_pk"], lp["mlbam_id"], lp["name"], lp["espn_team_id"],
                     lp["order_idx"], 1 if lp["is_last"] else 0, lp["games_started"],
                     lp["outs"], lp["er"], lp["k"], lp["p_h"], lp["p_bb"],
                     lp.get("sv") or 0, lp.get("hld") or 0, now),
                )
            for lb in live_b:
                conn.execute(
                    """
                    INSERT INTO live_batters
                        (game_pk, mlbam_id, name, pro_team_id,
                         ab, h, b2, b3, hr, bb, hbp, sf, still_in, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(game_pk, mlbam_id) DO UPDATE SET
                        name=excluded.name, pro_team_id=excluded.pro_team_id,
                        ab=excluded.ab, h=excluded.h, b2=excluded.b2, b3=excluded.b3,
                        hr=excluded.hr, bb=excluded.bb, hbp=excluded.hbp,
                        sf=excluded.sf, still_in=excluded.still_in,
                        fetched_at=excluded.fetched_at
                    """,
                    (lb["game_pk"], lb["mlbam_id"], lb["name"], lb["espn_team_id"],
                     lb["ab"], lb["h"], lb["b2"], lb["b3"], lb["hr"],
                     lb["bb"], lb["hbp"], lb["sf"],
                     1 if lb.get("still_in", True) else 0, now),
                )
            # Durable archive of Final pitcher + batter lines (survives the
            # prune above). The batter half closes the hitter-actuals gap: a
            # hitter's games-played and per-game rates were unrecoverable once
            # live_batters was pruned.
            _archive_final_lines(conn, live_p, status_by_pk, date_by_pk, now)
            _archive_final_batter_lines(conn, live_b, status_by_pk, date_by_pk, now)

            # Track reliever entry/exit margins for the in-game SVHD model, then
            # prune appearances for games that have aged out of the window.
            margin_by_game_team = {
                (g["game_pk"], g["espn_team_id"]):
                    (g.get("team_runs") or 0) - (g.get("opponent_runs") or 0)
                for g in games if g.get("team_runs") is not None}
            _track_reliever_appearances(conn, live_p, margin_by_game_team, now)
            appr_pks = {r[0] for r in conn.execute(
                "SELECT DISTINCT game_pk FROM reliever_appearances")}
            for pk in appr_pks - unsettled_pks:
                conn.execute("DELETE FROM reliever_appearances WHERE game_pk=?", (pk,))

            # Daily lineup snapshots: ESPN's per-scoringPeriod state replaces
            # whatever we recorded before (see `_authoritative_lineups`).
            for gd, rows in lineups_by_date.items():
                _replace_daily_lineups(conn, gd, rows, now)
    finally:
        conn.close()

    click.echo(
        f"Refreshed live game state: {yesterday.isoformat()}..{end.isoformat()}, "
        f"team_game_rows={len(games)}, in_progress={len(in_progress_pks)}, "
        f"boxscore_games={len(fetch_pks)}, live_pitcher_rows={len(live_p)}, "
        f"live_batter_rows={len(live_b)}, "
        f"lineup_days={len(lineups_by_date)}/{len(lineup_dates)}, "
        f"espn_probables_filled={espn_pp}"
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
        # No upper period bound: ESPN's ROS projections span the remaining
        # MLB season, so the share denominator must too. Bounding at last_reg
        # truncated it and inflated RP appearances (and K/SVHD with them)
        # hyperbolically as the fantasy regular season wound down — ×1.76 by
        # week 19 of 2026 (see sim.load_total_remaining_games).
        team_total_ros_games = (
            sim.load_total_remaining_games(conn, current)
            if model_name == "mc-v1" else {}
        )

        # Live pitcher lines for in-progress games → in-game QS/SVHD. Empty
        # unless games are live now, in which case the sim behaves as before.
        live_by_team = (
            sim.load_live_pitchers(conn) if model_name == "mc-v1" else {}
        )
        # Live batter lines for in-progress games → lets the hitter optimizer zero a
        # removed hitter's remaining production (still_in=False). Empty unless live.
        live_batters_by_team = (
            sim.load_live_batters_inprogress(conn) if model_name == "mc-v1" else {}
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

        # Live component reconstruction (current week only). The unsettled-window
        # MLB box-score lines we fold into each team's banked pitching/OPS
        # components, beating ESPN's once-daily REST settle. Empty → no-op for
        # --future, non-mc models, or when nothing is live right now.
        live_components = (not future_only) and model_name == "mc-v1"
        settle_boundary = (
            _settle_boundary()
            if live_components else None
        )
        unsettled_lines = (
            sim.load_unsettled_lines(conn, since_date=settle_boundary)
            if live_components else {"pitchers": [], "batters": []}
        )
        live_accepts = 0

        # Everything the sim consumes beyond roster + schedule, built ONCE —
        # the single production construction site for sim.SimContext (per-side
        # lineup slots are per-matchup and ride on MatchupInputs instead).
        base_ctx = sim.SimContext(
            team_total_ros_games=team_total_ros_games,
            lineup_slot_counts=lineup_slot_counts,
            live_by_team=live_by_team,
            live_batters_by_team=live_batters_by_team,
            last_start_by_pitcher=last_start_by_pitcher,
        )

        total_matchups = 0
        for period_id in periods:
            ms = conn.execute(
                "SELECT * FROM matchups WHERE matchup_period_id=?",
                (period_id,),
            ).fetchall()
            if not ms:
                continue

            schedule_by_team = sim.load_schedule_by_team(conn, period_id, now=now) \
                if model_name == "mc-v1" else {}
            if model_name == "mc-v1" and not schedule_by_team:
                raise click.ClickException(
                    f"No team_schedule rows for period {period_id}. "
                    "Run `app refresh-schedule` first."
                )

            for m in ms:
                home_scores = sim.load_latest_state(conn, m["id"], m["home_team_id"])
                away_scores = sim.load_latest_state(conn, m["id"], m["away_team_id"])
                hdec, adec = [], []   # live-recon decisions (telemetry), set below if live

                if model_name == "mc-v1":
                    # Rosters are only stored for the current period; future
                    # weeks reuse today's roster (best estimate of who'll be
                    # on each team).
                    roster_period = current if future_only else period_id
                    home_roster = sim.load_team_roster(conn, roster_period, m["home_team_id"])
                    away_roster = sim.load_team_roster(conn, roster_period, m["away_team_id"])
                    home_slots = away_slots = None
                    if live_components:
                        home_scores, hdec = sim.apply_live_components(
                            conn, m["home_team_id"], home_scores, home_roster,
                            unsettled_lines, since_date=settle_boundary)
                        away_scores, adec = sim.apply_live_components(
                            conn, m["away_team_id"], away_scores, away_roster,
                            unsettled_lines, since_date=settle_boundary)
                        live_accepts += sum(1 for d in (hdec + adec) if d["accepted"])
                        # Daily lineup slots → gate the in-game QS/SVHD override:
                        # a pitcher benched at first pitch is locked out of today's
                        # game (can't be moved in), so his in-progress start mustn't
                        # be credited. Same source the banked _count_qs/_svhd use.
                        home_slots = sim.load_active_slots(
                            conn, m["home_team_id"], since_date=settle_boundary,
                            fallback_roster=home_roster)
                        away_slots = sim.load_active_slots(
                            conn, m["away_team_id"], since_date=settle_boundary,
                            fallback_roster=away_roster)
                    inputs = sim.MatchupInputs(
                        matchup_id=m["id"],
                        home_state=home_scores,
                        away_state=away_scores,
                        home_roster=home_roster,
                        away_roster=away_roster,
                        home_slot_by_norm_name=home_slots,
                        away_slot_by_norm_name=away_slots,
                    )
                    # Rotation-cadence start projection ONLY for the current
                    # week. For any future week the anchor (last recorded
                    # start) is already a week-plus stale — the pitcher will
                    # start again *this* week first, and those turns aren't
                    # recorded yet — so the cadence walk snaps the first turn
                    # to day 1 of the future week and invents a 2nd, badly
                    # over-projecting (~1.9 vs a realistic ~1.3). Future weeks
                    # use the flat ROS-share split (tier B) instead.
                    ctx = dataclasses.replace(
                        base_ctx, use_cadence=(period_id == current))
                    home_wp, away_wp, details = sim.simulate(
                        inputs, schedule_by_team, ctx, n_sims=sims)
                    version = sim.MODEL_VERSION
                else:
                    home_wp, away_wp, details = model.compute_wp(
                        home_scores, away_scores, categories, tiebreaker,
                    )
                    version = model.MODEL_VERSION

                recon = _live_recon_block(settle_boundary, hdec, adec)
                if recon is not None and isinstance(details, dict):
                    details["live_recon"] = recon
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
        live_note = (
            f", live_component_groups_accepted={live_accepts}"
            if live_components else ""
        )
        click.echo(
            f"Computed WP for {total_matchups} matchups ({scope}: "
            f"periods {periods[0]}..{periods[-1]}) using {model_name}{live_note}."
        )
    finally:
        conn.close()


@cli.command("pages-guard")
@click.option("--dry-run", is_flag=True,
              help="Report what would be cancelled, without cancelling anything.")
def pages_guard(dry_run: bool) -> None:
    """Detect a wedged GitHub Pages deploy and cancel what is blocking it.

    The published site is the one hop nothing else watches: `publish` and the
    push can both succeed while the Pages workflow is stuck, so
    `docs/data.json` stays fresh on disk and the live site freezes. That ran for
    4 days undetected in 2026-08-31 with no flag. Mechanism, thresholds and the
    never-cancel-`in_progress` safety rule are documented in `app/pages.py`.

    Best-effort by contract (a network hiccup must not cost a 5-min tick), so
    findings ride `validate.persist` and show up in `app validate --list`.
    """
    conn = db.connect()
    try:
        res = pages.check_and_recover(conn, _now_iso(), dry_run=dry_run)
    finally:
        conn.close()
    click.echo(f"Pages guard: {res.note}")


@cli.command("validate")
@click.option("--all", "all_periods", is_flag=True,
              help="Check every regular-season period (default: current only).")
@click.option("--future", "future_periods", is_flag=True,
              help="Check current + future periods.")
@click.option("--list", "list_only", is_flag=True,
              help="List open (unresolved) flags and exit.")
@click.option("--resolve", "resolve_code", default=None, metavar="CODE",
              help="Mark open flags with this CODE (or 'all') resolved, and exit.")
@click.option("--note", "resolve_note", default=None, metavar="TEXT",
              help="Triage note recorded with --resolve (why it's benign / what it was).")
@click.option("--by", "resolve_by", default=None, metavar="NAME",
              help="Who resolved it (default: $USER). Recorded with --resolve.")
@click.option("--resolved", "list_resolved", is_flag=True,
              help="List recently *resolved* flags with their provenance, and exit.")
@click.option("--calibration", "calibration", is_flag=True,
              help="Also run the retrospective projected-vs-actual calibration "
                   "check (daily tier — reads every settled week, so it's off the "
                   "5-min path).")
def validate_cmd(all_periods: bool, future_periods: bool, list_only: bool,
                 resolve_code: str | None, resolve_note: str | None,
                 resolve_by: str | None, list_resolved: bool,
                 calibration: bool) -> None:
    """Run invariant + anomaly checks over the latest WP snapshots and record
    findings in `validation_flags`. Cheap (no sims) — safe to run every fast tick.
    Review open flags with `--list`, dismiss triaged-legit ones with
    `--resolve CODE --note "why"`, audit closed ones with `--resolved`."""
    from app import validate as _v

    conn = db.connect()
    try:
        if resolve_code:
            import getpass
            who = resolve_by or getpass.getuser()
            if not resolve_note:
                click.echo("  (tip: pass --note \"why this is benign\" so the reasoning "
                           "survives — a bare resolve loses the triage.)", err=True)
            n = _v.resolve(conn, resolve_code, now=_now_iso(), by=who, note=resolve_note)
            click.echo(f"Resolved {n} flag(s)"
                       + ("" if resolve_code == "all" else f" with code {resolve_code}")
                       + f" (by {who}" + (f": {resolve_note}" if resolve_note else "") + ").")
            return
        if list_resolved:
            rows = conn.execute(
                "SELECT code, matchup_id, severity, resolved_at, resolved_by, resolution_note "
                "FROM validation_flags WHERE resolved=1 "
                "ORDER BY resolved_at DESC NULLS LAST, last_seen DESC LIMIT 30").fetchall()
            if not rows:
                click.echo("No resolved flags.")
                return
            click.echo(f"{len(rows)} resolved flag(s) (most recent first):")
            for r in rows:
                mid = "" if r["matchup_id"] in (None, -1) else f" m{r['matchup_id']}"
                when = (r["resolved_at"] or "?")[:16]
                who = r["resolved_by"] or "unknown"
                note = r["resolution_note"] or "(no note)"
                click.echo(f"  [{r['severity']:<5}] {r['code']}{mid}  "
                           f"resolved {when} ({who}): {note}")
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

        now = _now_iso()
        data_json_path = str(DOCS_DATA_JSON)
        findings = _v.run(conn, periods, now=now, data_json_path=data_json_path,
                          calibration=calibration)
        _v.persist(conn, findings, now)
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


def _week_stamp(conn, period_id: int, ms, state: str) -> str:
    """Cheap change-signal for a week's published block — rebuild only when it moves.
    Keys off `wp_snapshots.computed_at` (frozen for settled weeks, 4-hourly for
    future, per-tick for current), the `edited` flag (so a hand-smoothing edit forces
    a rebuild), winners, and state. Deliberately NOT category_state.fetched_at: fetch
    re-writes settled weeks' state every tick with identical values, which would
    defeat the cache (see db.published_week_cache)."""
    mids = [m["id"] for m in ms]
    winners = "|".join((m["winner"] or "?") for m in ms)
    if not mids:
        return f"{state}|none|{winners}"
    ph = ",".join("?" * len(mids))
    r = conn.execute(
        f"SELECT MAX(computed_at) c, MAX(edited) e FROM wp_snapshots WHERE matchup_id IN ({ph})",
        mids).fetchone()
    return f"{state}|{r['c']}|{r['e']}|{winners}"


@cli.command()
@click.option("--rebuild", is_flag=True,
              help="Ignore the per-week block cache and rebuild every week (run "
                   "daily to pick up rare late stat corrections to settled weeks).")
def publish(rebuild: bool) -> None:
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
        # Per-week block cache: only the current week changes per fast tick (settled
        # weeks are frozen; future weeks change only on the 4-hourly medium run), so
        # reuse the cached block for any week whose change-stamp is unchanged instead
        # of re-deriving it (a latest_category_state read per team). --rebuild bypasses.
        cache = {} if rebuild else {
            r["period_id"]: (r["stamp"], r["block_json"])
            for r in conn.execute("SELECT period_id, stamp, block_json FROM published_week_cache")
        }
        fresh_blocks: dict[int, tuple[str, str]] = {}   # period_id -> (stamp, block_json), to upsert
        # Per-point category history (lets the chart be clicked back to a past
        # category table) is attached for the live week AND the most recently
        # completed week — so "last week" stays scrubbable — but no further back:
        # each such week adds ~1 MB to data.json (see _matchup_block).
        states = {pid: _week_state(conn, pid) for pid in range(first, last_reg + 1)}
        live_period = next((pid for pid, s in states.items() if s == "live"), None)
        final_periods = [pid for pid, s in states.items() if s == "final"]
        prev_period = max(final_periods) if final_periods else None
        cat_periods = {p for p in (live_period, prev_period) if p is not None}
        weeks_out = []
        for period_id in range(first, last_reg + 1):
            state = states[period_id]
            ms = conn.execute(
                "SELECT * FROM matchups WHERE matchup_period_id=? ORDER BY id",
                (period_id,),
            ).fetchall()
            with_cat = period_id in cat_periods
            # Fold the category-history decision into the cache stamp, so a week's
            # block is rebuilt (and its ~1 MB of category history dropped) the tick
            # it ages out of the live/previous window.
            stamp = _week_stamp(conn, period_id, ms, state) + (":cat" if with_cat else "")
            hit = cache.get(period_id)
            if hit is not None and hit[0] == stamp:
                weeks_out.append(json.loads(hit[1]))
                continue
            started = state != "upcoming"
            start, end = mlb.matchup_period_window(period_id)
            week = {
                "matchup_period_id": period_id,
                "label": f"Week {period_id}",
                "start": start.isoformat(),
                "end": end.isoformat(),
                # "state" drives the UI's default week selection.
                "state": state,
                # Observed game-day windows for the chart's "Active" x-axis.
                "active_intervals": _active_intervals(conn, period_id, now),
                "matchups": [
                    _matchup_block(conn, teams, m, started=started,
                                   live=(state == "live"),
                                   is_current=(period_id == current),
                                   cat_history=with_cat)
                    for m in ms
                ],
            }
            weeks_out.append(week)
            fresh_blocks[period_id] = (stamp, json.dumps(week, separators=(",", ":")))

        out_path = DOCS_DATA_JSON
        # Split each matchup's WP `history` out of data.json into per-week side
        # files (docs/history/<period>.json). The history series was ~98% of the
        # 6 MB payload, all of it behind collapsed Details panels — moving it out
        # lets the scoreboard render off a small data.json while the site fetches
        # the charts' data in the background (docs/app.js hydrates on arrival).
        # Written BEFORE data.json so the page never sees a data.json that points
        # at not-yet-written history. Rewritten only when the week's block was
        # rebuilt (cache-miss ⇒ its history changed) or the file is missing.
        history_dir = out_path.parent / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        slim_weeks = []
        for week in weeks_out:
            pid = week["matchup_period_id"]
            hist_path = history_dir / f"{pid}.json"
            if pid in fresh_blocks or not hist_path.exists():
                hist_path.write_text(json.dumps({
                    "matchup_period_id": pid,
                    "generated_at": now,
                    "history": {str(m["matchup_id"]): m.get("history") or []
                                for m in week["matchups"]},
                }, separators=(",", ":")))
            slim_weeks.append({
                **week,
                "matchups": [{k: v for k, v in m.items() if k != "history"}
                             for m in week["matchups"]],
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
            "weeks": slim_weeks,
        }
        # Compact (no indent/whitespace) — data.json is machine-generated and
        # fetched on every page load; pretty-printing ~doubled the payload.
        out_path.write_text(json.dumps(out, separators=(",", ":")))
        # Persist the per-week cache only after the write succeeds (a failed write
        # must not leave the cache claiming a week is current when data.json isn't).
        if fresh_blocks:
            conn.executemany(
                "INSERT OR REPLACE INTO published_week_cache (period_id, stamp, block_json) "
                "VALUES (?,?,?)",
                [(pid, stamp, bj) for pid, (stamp, bj) in fresh_blocks.items()])
            conn.commit()
        click.echo(
            f"Wrote {out_path} ({out_path.stat().st_size} bytes) — "
            f"{len(weeks_out)} weeks (periods {first}..{last_reg}); "
            f"{len(fresh_blocks)} week(s) rebuilt, {len(weeks_out) - len(fresh_blocks)} cached"
        )
    finally:
        conn.close()

# Live-finale playoff-odds refresh (see `_finale_skip_reason`). 30 min is a
# deliberate compromise: odds move on *decided matchups*, so the interesting
# fluctuations come as each of the six games resolves categories over the final
# afternoon — fast enough to catch a seed flipping, slow enough that a ~10s run
# lands on only ~2 of the 12 fast ticks per hour.
PLAYOFF_LIVE_INTERVAL_MIN = 30


def _finale_skip_reason(conn, period_id: int, now_iso: str) -> str | None:
    """Why a live-finale playoff-odds refresh should be SKIPPED, or None if due.

    Playoff odds normally ride medium.sh's 4-hourly cadence, which is right for
    most of the week — they're driven by the *remaining* matchups' WPs, and those
    barely move on a Tuesday. The last day of a matchup period is different: six
    matchups resolve within a few hours, each flipping a win from "probable" to
    banked, so seeds and bye odds can genuinely swing between two 4-hourly runs
    and the odds-over-time chart would show a single step across the whole finale.

    Two gates, both cheap:

    - **An in-progress game dated the period's LAST day.** Keying off the game's
      own `game_date` (not the wall clock) is what makes this correct across the
      UTC rollover: Sunday's West-Coast games are still in progress at 02:00 UTC
      Monday, and that is exactly the window we care about. A wall-clock "is today
      the last day" test would switch off at midnight UTC, mid-finale.
    - **Throttle since the last archived run**, read from `playoff_odds_runs`
      (the same table the odds-over-time history is built from). Using the archive
      rather than a marker file means the throttle survives restarts and can't
      drift from what was actually published.
    """
    try:
        period_end = mlb.matchup_period_window(period_id)[1].isoformat()
    except Exception:
        return "period window unavailable"
    live = conn.execute(
        "SELECT COUNT(*) FROM team_schedule WHERE matchup_period_id=? "
        "AND game_date=? AND game_status='In Progress'",
        (period_id, period_end),
    ).fetchone()[0]
    if not live:
        return f"no in-progress games on the period's last day ({period_end})"
    row = conn.execute("SELECT MAX(computed_at) c FROM playoff_odds_runs").fetchone()
    last = row["c"] if row else None
    if last:
        try:
            age_min = (datetime.fromisoformat(now_iso)
                       - datetime.fromisoformat(last)).total_seconds() / 60
        except (TypeError, ValueError):
            return None          # unparseable stamp — don't let it block a refresh
        if age_min < PLAYOFF_LIVE_INTERVAL_MIN:
            return (f"last run {age_min:.0f} min ago "
                    f"(< {PLAYOFF_LIVE_INTERVAL_MIN} min)")
    return None


@cli.command("playoffs")
@click.option("--sims", type=int, default=None,
              help="Season-simulation count (default: playoffs.DEFAULT_SEASON_SIMS).")
@click.option("--samples", type=int, default=None,
              help="Sampled team-weeks per team per playoff round "
                   "(default: playoffs.DEFAULT_TEAM_SAMPLES).")
@click.option("--if-live-finale", is_flag=True,
              help="No-op unless a game is in progress on the current period's "
                   f"LAST day and the previous run was >{PLAYOFF_LIVE_INTERVAL_MIN} "
                   "min ago. Lets the 5-min fast tier self-throttle to a "
                   "half-hourly refresh through the finale.")
def playoffs_cmd(sims: int | None, samples: int | None,
                 if_live_finale: bool) -> None:
    """Simulate the rest of the regular season + the playoff bracket and
    write docs/playoffs.json (per-team odds of playoffs / bye / final /
    championship + full seed distribution)."""
    from app import playoffs

    n_sims = sims or playoffs.DEFAULT_SEASON_SIMS
    n_samples = samples or playoffs.DEFAULT_TEAM_SAMPLES
    conn = db.connect()
    try:
        ss = conn.execute(
            "SELECT * FROM scoring_settings WHERE league_id=? AND season_id=?",
            (LEAGUE_ID, SEASON_ID),
        ).fetchone()
        current = _current_matchup_period(conn)
        last_reg = _last_regular_season_period(conn)
        if ss is None or current is None or last_reg is None:
            raise click.ClickException("Missing league metadata. Run `app fetch` first.")

        if if_live_finale:
            skip = _finale_skip_reason(conn, current, _now_iso())
            if skip:
                click.echo(f"Playoff odds: skipped — {skip}")
                return

        teams = {r["id"]: dict(r)
                 for r in conn.execute("SELECT * FROM teams").fetchall()}
        team_ids = sorted(teams)
        wins, losses, h2h = playoffs.load_records(conn, team_ids)
        remaining = playoffs.load_remaining(conn)
        missing_wp = sum(1 for m in remaining if not m["had_snapshot"])
        if missing_wp:
            click.echo(f"  ({missing_wp} remaining matchup(s) had no WP snapshot; "
                       "using 0.5)", err=True)

        # Playoff-round budgets: today's rosters, flat SP model (the cadence
        # anchor is weeks stale by September — same rule as compute --future),
        # ROS spread over everything left INCLUDING the playoff weeks.
        now = _now_iso()
        lineup_slot_counts: dict[int, int] = {}
        if ss["lineup_slots_json"]:
            try:
                lineup_slot_counts = {int(k): int(v) for k, v in
                                      json.loads(ss["lineup_slots_json"]).items()}
            except (json.JSONDecodeError, ValueError, TypeError):
                lineup_slot_counts = {}
        last_playoff = last_reg + playoffs.NUM_PLAYOFF_PERIODS
        ctx = sim.SimContext(
            # Unbounded = through the stored schedule's end. Equivalent to the
            # old `current..last_playoff` bound in 2026 (playoffs end with the
            # MLB season) but matches ESPN's ROS span by construction.
            team_total_ros_games=sim.load_total_remaining_games(conn, current),
            lineup_slot_counts=lineup_slot_counts,
            use_cadence=False,
        )
        rosters = {t: sim.load_team_roster(conn, current, t) for t in team_ids}
        value_samples: dict[int, list[list[tuple[float, ...]]]] = {
            t: [] for t in team_ids}
        for period_id in range(last_reg + 1, last_playoff + 1):
            schedule_by_team = sim.load_schedule_by_team(conn, period_id, now=now)
            if not schedule_by_team:
                raise click.ClickException(
                    f"No team_schedule rows for playoff period {period_id}. "
                    "Run `app refresh-schedule` first.")
            for t in team_ids:
                budgets = sim.build_budgets(rosters[t], schedule_by_team, ctx)
                value_samples[t].append([
                    playoffs.totals_to_values(c)
                    for c in sim.sample_team_totals(budgets, n_samples)])

        odds = playoffs.simulate_odds(team_ids, wins, h2h, remaining,
                                      value_samples, n_sims=n_sims)

        blocks = []
        for t in team_ids:
            o = odds[t]
            blocks.append({
                "team_id": t,
                "name": teams[t].get("name"),
                "abbrev": teams[t].get("abbrev"),
                "owner": teams[t].get("owner"),
                "w": wins[t], "l": losses[t],
                **{k: round(v, 4) for k, v in o.items() if k != "seed_dist"},
                "seed_dist": [round(p, 4) for p in o["seed_dist"]],
            })
        blocks.sort(key=lambda b: (-b["p_playoffs"], -b["p_champion"], -b["w"]))
        payload = {
            "generated_at": now,
            "model_version": playoffs.MODEL_VERSION,
            "n_sims": n_sims,
            "team_samples": n_samples,
            "playoff_team_count": playoffs.PLAYOFF_TEAM_COUNT,
            "bye_seeds": playoffs.BYE_SEEDS,
            "playoff_periods": list(range(last_reg + 1, last_playoff + 1)),
            "remaining_matchups": len(remaining),
            "teams": blocks,
        }
        # Archive this run WITHOUT history (blobs stay per-run sized), then
        # assemble the odds-over-time series from the archive — including the
        # row just inserted — and publish it with the payload for the chart.
        conn.execute("INSERT OR REPLACE INTO playoff_odds_runs "
                     "(computed_at, payload_json) VALUES (?,?)",
                     (now, json.dumps(payload, separators=(",", ":"))))
        conn.commit()
        payload["history"] = playoffs.load_odds_history(conn)
        (DOCS_DATA_JSON.parent / "playoffs.json").write_text(
            json.dumps(payload, separators=(",", ":")))
        # blocks[0] is the playoff-odds sort leader, which near 100% flips on
        # single-sim wobble — report the champion-odds leader instead.
        fav = max(blocks, key=lambda b: b["p_champion"])
        click.echo(
            f"Playoff odds: {n_sims} season sims over {len(remaining)} remaining "
            f"matchups + bracket (periods {last_reg + 1}..{last_playoff}, "
            f"{n_samples} team-week samples/round). "
            f"Title favorite: {fav['name']} P(champ)={fav['p_champion']:.1%}")
    finally:
        conn.close()


# WP-over-time points published per matchup, per model version (the DB keeps
# every snapshot — this only thins what the static site downloads).
#
# Two tiers, both anchored to the series itself so a rebuild is deterministic:
# the last RECENT_FULL_HOURS of a week's own history stay at raw 5-min
# resolution, and everything older is snapped to a round OLDER_GRID_MINUTES
# wall-clock cadence. Do NOT go back to "N evenly-spaced points": the step then
# depends on how long the series happens to be (a 7-day week landed on a ~55-min
# grid), and thinning *drops* extremes rather than aggregating them — the
# 2026-08-02 m98 −78pp settle cliff fell entirely between two kept points, so
# the chart drew a gentle 55-min slope where the real series had a wall.
MAX_HISTORY_POINTS = 2400   # backstop only; the grid below is the real bound
                            # (a 7-day week ≈ 1200 pts). Not expected to bind.
RECENT_FULL_HOURS = 24      # tail of each week's history kept at raw 5-min
                            # resolution — the "Today"/"Active" zoom, and where
                            # end-of-week resolution cliffs live.
OLDER_GRID_MINUTES = 15     # round cadence for the rest of the week
MAX_CAT_HISTORY_POINTS = 200  # cap on category_wp carriers OUTSIDE the recent
                              # window below. A point's category_wp is ~920
                              # bytes — ~10x the point itself — so the older
                              # span stays sampled.
CAT_RECENT_HOURS = 6        # tail kept at FULL 5-min category resolution.
                            # Until 2026-08-08 carriers were a flat 200 evenly
                            # spaced over the whole week, which put them a
                            # *median 75 min apart* (≈25 min even in the live
                            # tail) — and since app.js snaps a click to the
                            # NEAREST carrier, five consecutive ticks all showed
                            # one identical category table while the WP line
                            # moved (reported 2026-08-08: 23:15/23:20/23:25 CEST
                            # all resolved to the 23:25 carrier). Full resolution
                            # over the evening slate fixes clicking where people
                            # actually click. Deliberately shorter than
                            # RECENT_FULL_HOURS (24): category_wp is expensive,
                            # and the dead hours between slates would spend it
                            # storing near-identical tables over and over. 0
                            # disables the tail (pure even sampling, pre-2026-08-08
                            # behavior).


def _downsample_history(history: list[dict],
                        max_points: int = MAX_HISTORY_POINTS,
                        recent_since: str | None = None,
                        recent_hours: int = RECENT_FULL_HOURS,
                        grid_minutes: int = OLDER_GRID_MINUTES,
                        max_cat_points: int = MAX_CAT_HISTORY_POINTS,
                        cat_recent_hours: int = CAT_RECENT_HOURS) -> list[dict]:
    """Thin a matchup's snapshot history for the published payload.

    Grouped by model_version (the chart only ever plots one model's series).
    Within a group: points in the last `recent_hours` **of that group's own
    history** are kept in full; older ones are snapped to a `grid_minutes`
    wall-clock grid (first snapshot in each bucket), then hard-capped at
    `max_points` evenly spaced. First and last points always survive.

    Anchoring the recent window to the series' last point rather than "now" is
    what makes it stable: `publish --rebuild` runs daily over every week, so a
    wall-clock window would slide off a finished week and silently re-thin the
    detail it had while live (that is how week 17's settle cliff was lost). This
    way a week keeps its final game-day at 5-min resolution permanently, and for
    the live week "series end" is the current tick anyway.

    `recent_since` (UTC ISO) overrides the derived cutoff; `recent_hours=0`
    disables the full-resolution tail entirely.

    `category_wp` gets the same two-tier treatment as the line, on its own
    (shorter) clock: every carrier in the last `cat_recent_hours` survives, and
    only older ones are thinned to `max_cat_points` evenly spaced. Anchored to
    the series' last point for the same rebuild-stability reason as above.
    """
    by_ver: dict[str, list[dict]] = {}
    for h in history:
        by_ver.setdefault(h["model_version"], []).append(h)

    def even(rows: list[dict], n: int) -> list[dict]:
        """`n` evenly-spaced rows, endpoints included."""
        if len(rows) <= n or n < 2:
            return rows
        step = (len(rows) - 1) / (n - 1)
        return [rows[i] for i in sorted({round(i * step) for i in range(n)})]

    def cutoff_for(rows: list[dict]) -> str | None:
        if recent_since is not None:
            return recent_since
        if not recent_hours:
            return None
        last = datetime.fromisoformat(rows[-1]["computed_at"])
        return (last - timedelta(hours=recent_hours)).isoformat(timespec="seconds")

    def thin(rows: list[dict]) -> list[dict]:
        cutoff = cutoff_for(rows)
        if cutoff is None:
            older, recent = rows, []
        else:  # ISO-8601 UTC strings sort lexicographically
            older = [r for r in rows if r["computed_at"] < cutoff]
            recent = [r for r in rows if r["computed_at"] >= cutoff]
        if grid_minutes:
            bucketed, seen = [], set()
            for r in older:
                t = datetime.fromisoformat(r["computed_at"])
                b = (t.replace(minute=0, second=0, microsecond=0)
                     + timedelta(minutes=(t.minute // grid_minutes) * grid_minutes))
                if b not in seen:
                    seen.add(b)
                    bucketed.append(r)
            older = bucketed
        return even(older, max_points) + recent   # recent kept at full resolution

    out = [h for rows in by_ver.values() for h in thin(rows)]
    out.sort(key=lambda h: h["computed_at"])
    # Category carriers: full resolution over the recent tail, sampled before it.
    with_cat = [h for h in out if "category_wp" in h]
    if with_cat:
        if cat_recent_hours:
            last = datetime.fromisoformat(with_cat[-1]["computed_at"])
            cat_cutoff = (last - timedelta(hours=cat_recent_hours)
                          ).isoformat(timespec="seconds")
            older_cat = [h for h in with_cat if h["computed_at"] < cat_cutoff]
            recent_cat = [h for h in with_cat if h["computed_at"] >= cat_cutoff]
        else:
            older_cat, recent_cat = with_cat, []
        keep = {id(h) for h in even(older_cat, max_cat_points)}
        keep |= {id(h) for h in recent_cat}
        for h in with_cat:
            if id(h) not in keep:
                h.pop("category_wp", None)
                h.pop("n_sims", None)
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
    ivs = [
        {
            "date": r["game_date"],
            "start": r["active_start"],
            "end": r["active_end"] or now,
        }
        for r in rows
    ]
    return _clamp_active_intervals(ivs)


def _clamp_active_intervals(ivs: list[dict]) -> list[dict]:
    """Make consecutive game-day windows disjoint by clamping each day's `end`
    to no later than the next day's `start`.

    A game that finalizes very late (e.g. a suspended game that resumes the next
    day) can leave `active_end` ~a day long, so the window overlaps the next
    day's window. The "Active" x-axis assigns each point to the *first* interval
    containing it (app.js), so an overlap steals the next day's early points and
    renders the next segment's lead-in as a blank horizontal gap. Clamping keeps
    the segments ordered and non-overlapping (input is ordered by start). `end`
    is never pushed below its own `start` (app.js floors duration at 1 anyway).
    """
    for i in range(len(ivs) - 1):
        nxt_start = ivs[i + 1]["start"]
        if ivs[i]["end"] > nxt_start >= ivs[i]["start"]:
            ivs[i]["end"] = nxt_start
    return ivs


# Display decimals for the derived rate cats (deriver routing lives in
# sim.RATE_DERIVERS — single source).
_RATE_DECIMALS = {sim.STAT_OPS: 4, sim.STAT_ERA: 3, sim.STAT_WHIP: 3}
_RATE_NO_DATA = 999.0  # derive_era/whip sentinel for no innings pitched


def _apply_derived_rates(home_state: dict[int, dict],
                         away_state: dict[int, dict], *,
                         derived: bool = True) -> None:
    """Set OPS/ERA/WHIP score+result in both team states. Mutates in place.
    Mirrors `sim._decide`'s comparison so the published number and result stay
    consistent with the WP's decision.

    `derived=True` (a live fold happened this publish — components are fresh):
    derive each rate from the component counters. This beats a lagging scrape
    mid-slate (the scraped rate freezes while its components keep moving via REST).

    `derived=False` (NO live components were folded — idle/finished week): the raw
    component counters are settle-stale and **time-mismatched** with the frozen
    scrape (e.g. a scored-cat H frozen at the last scrape over an AB that REST kept
    settling → a bogus OPS that can flip the category — 2026-07-27 m96). Re-deriving
    then is *worse* than the scrape, so trust the **atomically-scraped displayed
    rate** already in state (authoritative for current standing; playbook: never
    raw-derive a rate off unfolded banked components). Falls back to deriving only
    if a side has no scraped rate at all."""
    def components(state: dict[int, dict]) -> dict[int, float]:
        return {sid: v["score"] for sid, v in state.items()
                if v.get("score") is not None}

    home_c, away_c = components(home_state), components(away_state)
    for sid, derive in sim.RATE_DERIVERS.items():
        ndigits = _RATE_DECIMALS[sid]
        if derived:
            hv, av = derive(home_c), derive(away_c)
        else:
            hv = (home_state.get(sid) or {}).get("score")
            av = (away_state.get(sid) or {}).get("score")
            if hv is None or av is None:   # no scraped rate → fall back to deriving
                hv, av = derive(home_c), derive(away_c)
        reversed_ = stats.is_reversed(sid)
        if hv == av:
            h_res, a_res = "TIE", "TIE"
        else:
            home_better = (hv < av) if reversed_ else (hv > av)
            h_res, a_res = ("WIN", "LOSS") if home_better else ("LOSS", "WIN")
        for state, val, res in ((home_state, hv, h_res), (away_state, av, a_res)):
            # Keep the derived result (it mirrors the sim's _decide via the
            # no-data sentinel), but never surface 999 as a score — fall back to
            # the scraped display value when the team has no innings yet.
            score = (round(val, ndigits) if val < _RATE_NO_DATA
                     else (state.get(sid) or {}).get("score"))
            state[sid] = {"score": score, "result": res}


def _apply_counting_results(home_state: dict[int, dict],
                            away_state: dict[int, dict]) -> None:
    """Recompute WIN/LOSS/TIE for the *counting* categories by comparing the two
    teams' banked scores, so the two sides' results are ALWAYS mirror images.

    The per-team `result` stored by the fetch/scrape is stamped independently per
    (team, stat) and read per-stat-latest, so a category lead that flips between
    the two teams' last writes — e.g. mid overnight stat-reconciliation — leaves
    the stored results non-complementary, and `_team_block` then sums them into
    asymmetric W-L-T records (Dawgs 9-1-0 vs Bear 2-7-1 instead of mirror images).
    Deriving from a single comparison makes it symmetric by construction. Rates are
    already handled this way in `_apply_derived_rates`; this covers the rest.
    A missing counting score reads as 0 (cumulative-from-zero) so both sides are
    always comparable and the record always mirrors."""
    rate_ids = set(sim.RATE_DERIVERS)
    counting = [s for s in (stats.BATTING_STAT_IDS + stats.PITCHING_STAT_IDS)
                if s not in rate_ids]
    for sid in counting:
        h, a = home_state.get(sid), away_state.get(sid)
        if h is None and a is None:
            continue  # category not tracked at all
        hv = (h or {}).get("score") or 0
        av = (a or {}).get("score") or 0
        if hv == av:
            h_res, a_res = "TIE", "TIE"
        else:
            home_better = (hv < av) if stats.is_reversed(sid) else (hv > av)
            h_res, a_res = ("WIN", "LOSS") if home_better else ("LOSS", "WIN")
        home_state[sid] = {"score": (h or {}).get("score"), "result": h_res}
        away_state[sid] = {"score": (a or {}).get("score"), "result": a_res}


def _slim_category_wp(details_json: str) -> tuple[list | None, int | None]:
    """Pull a compact category_wp + n_sims from a snapshot's details_json, for
    attaching per-point category history (live week only). Same shape
    `renderCategoryWP` expects; avgs rounded to keep the payload small. Returns
    (None, None) if the blob is missing/unparseable."""
    try:
        d = json.loads(details_json)
    except (TypeError, json.JSONDecodeError):
        return None, None
    cwp = [
        {"stat_id": c["stat_id"], "home_wins": c["home_wins"],
         "away_wins": c["away_wins"], "ties": c["ties"],
         "home_avg": round(c["home_avg"], 3), "away_avg": round(c["away_avg"], 3)}
        for c in d.get("category_wp", [])
    ]
    return (cwp or None), d.get("n_sims")


def _fold_live_components(conn, home_state, away_state,
                          home_team_id, away_team_id, period_id,
                          matchup_id=None) -> None:
    """Fold live box-score components into the published team states so the
    scoreboard's *derived* ERA/WHIP/OPS (and QS/SVHD) reflect today's games — the
    same reconstruction the WP projection uses (`sim.apply_live_components`).

    Without this, `_apply_derived_rates` derives the displayed rates from the
    REST-lagged `category_state` components, so during live games the scoreboard
    drifts stale while the projection (which folds in the live box scores) moves —
    e.g. a team shown at ERA 2.38 while its projection and the live scrape both read
    4.5 after a rough inning. No-op when nothing is live.

    Returns True iff live components were actually folded (unsettled lines present).
    The caller uses this to decide whether the displayed rate cats may be re-derived
    from the (now-fresh) component counters: when nothing was folded, the raw REST
    components are settle-stale and time-mismatched with the frozen scrape, so
    deriving from them produces a bogus rate — the caller must trust the scraped
    rate instead (see `_apply_derived_rates`)."""
    settle = _settle_boundary()
    unsettled = sim.load_unsettled_lines(conn, since_date=settle)
    if not unsettled["pitchers"] and not unsettled["batters"]:
        return False
    for state, tid in ((home_state, home_team_id), (away_state, away_team_id)):
        baseline = {sid: c.get("score") for sid, c in state.items()
                    if c.get("score") is not None}
        roster = sim.load_team_roster(conn, period_id, tid)
        recon, _ = sim.apply_live_components(conn, tid, baseline, roster, unsettled,
                                             since_date=settle)
        for sid, val in recon.items():
            if val is not None:
                state.setdefault(sid, {"score": None, "result": None})["score"] = val
    return True


def _matchup_block(conn, teams: dict, m, *, started: bool, live: bool = False,
                   is_current: bool = False, cat_history: bool = False) -> dict:
    """One matchup with team blocks, current snapshot, and history.

    `started` = the week has begun (state != "upcoming"); when False the team
    blocks emit null scores/records so the UI shows dashes for a pure projection.
    `live` = the current (in-progress) week; only then do we keep the last
    ~game-day at full resolution for the chart's "Today" zoom.
    `cat_history` = attach each history point's category_wp so the chart can be
    clicked back to a past category table. Enabled for the live week AND the most
    recently completed week (so last week stays scrubbable) — but no further back:
    per-point category data costs ~1 MB/week in data.json (older weeks rely on
    their latest table in `details`, which covers a settled week).
    """
    home_team_id = m["home_team_id"]
    away_team_id = m["away_team_id"]
    # Upcoming weeks emit null scores anyway — skip the category_state read for them.
    home_state = _latest_score_rows(conn, m["id"], home_team_id) if started else {}
    away_state = _latest_score_rows(conn, m["id"], away_team_id) if started else {}
    if started:
        folded = False
        if live:   # make the displayed rates match the projection's live view
            folded = _fold_live_components(conn, home_state, away_state,
                                           home_team_id, away_team_id,
                                           m["matchup_period_id"], matchup_id=m["id"])
        # Rate cats: derive from components only when a live fold made them fresh,
        # OR for any non-current week (unchanged historical behavior). For the
        # current week with no fold (idle/finished slate) trust the scraped rate —
        # the raw components are time-mismatched with the frozen scrape and derive
        # to a bogus value that can flip a category (2026-07-27 m96 OPS).
        _apply_derived_rates(home_state, away_state,
                             derived=(folded or not is_current))
        # QS/SVHD need no display adjustment: they come straight from
        # category_state (ESPN via the scrape). The settled-floor raise that lived
        # here until 2026-08-11 existed because an idle scrape could leave a
        # just-Final credit un-banked for hours; the closing scrape (_scrape_due)
        # now keeps ESPN current within a tick, so the display and the WP read the
        # same number from the same source.
        _apply_counting_results(home_state, away_state)
    wp_row = conn.execute(
        """
        SELECT * FROM wp_snapshots
        WHERE matchup_id=?
        ORDER BY computed_at DESC LIMIT 1
        """,
        (m["id"],),
    ).fetchone()
    cols = "computed_at, home_wp, away_wp, model_version" + (", details_json" if cat_history else "")
    history_rows = conn.execute(
        f"SELECT {cols} FROM wp_snapshots WHERE matchup_id=? ORDER BY computed_at ASC",
        (m["id"],),
    ).fetchall()
    history = []
    for r in history_rows:
        pt = {
            "computed_at": r["computed_at"],
            "home_wp": r["home_wp"],
            "away_wp": r["away_wp"],
            "model_version": r["model_version"],
        }
        if cat_history and r["details_json"]:
            cwp, n = _slim_category_wp(r["details_json"])
            if cwp is not None:
                pt["category_wp"] = cwp
                pt["n_sims"] = n
        history.append(pt)
    # Every week keeps the last RECENT_FULL_HOURS of its OWN history at raw
    # 5-min resolution (the window is derived from the series' last point, not
    # from `now` — see _downsample_history); older points go on a round grid.
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
    """Latest score+result per stat for the published team block."""
    return db.latest_category_state(conn, matchup_id, team_id)


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
