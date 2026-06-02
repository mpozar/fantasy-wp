"""One-time backfill of `game_day_activity` for weeks 9–10.

Empirical activity tracking (in `refresh-live`) only records windows going
forward, so game-days that completed before the feature shipped have no rows.
For each *completed* game-day in the target weeks this fetches the day's
scheduled first pitches from MLB statsapi and estimates the active window as
`[earliest first pitch, latest first pitch + GAME_LEN]`.

Uses a COALESCE upsert, so any value already recorded by live tracking is
preserved (empirical always wins over the estimate). Idempotent — safe to
re-run.

    .venv/bin/python scripts/backfill_game_activity.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import db, mlb

TARGET_PERIODS = [9, 10]
GAME_LEN = timedelta(hours=3, minutes=15)  # rough length of a game


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _estimate_window(d: date) -> tuple[str, str] | None:
    """[earliest first pitch, latest first pitch + GAME_LEN] for an official
    date, from MLB scheduled game times. None if the day has no games."""
    r = httpx.get(
        f"{mlb.BASE_URL}/schedule",
        params={"sportId": "1", "startDate": d.isoformat(), "endDate": d.isoformat()},
        timeout=30,
    )
    r.raise_for_status()
    starts = [
        datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
        for entry in r.json().get("dates", [])
        for g in entry.get("games", [])
        if g.get("gameDate")
    ]
    if not starts:
        return None
    return _iso(min(starts)), _iso(max(starts) + GAME_LEN)


def main() -> None:
    today = date.today()
    now = _iso(datetime.now(timezone.utc))
    conn = db.connect()
    filled = 0
    try:
        with conn:
            for period in TARGET_PERIODS:
                start, end = mlb.matchup_period_window(period)
                d = start
                while d <= end:
                    # Only completed days — ongoing/future days are tracked live.
                    if d < today:
                        win = _estimate_window(d)
                        if win:
                            a_start, a_end = win
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
                                (period, d.isoformat(), a_start, a_end, now),
                            )
                            filled += 1
                            print(f"  period {period} {d}: {a_start} .. {a_end}")
                    d += timedelta(days=1)
    finally:
        conn.close()
    print(f"Backfilled {filled} game-days (estimated; live tracking overrides going forward).")


if __name__ == "__main__":
    main()
