"""_overlay_espn_probables fills MLB-blank probables from ESPN's (date, team)-keyed
feed — but must NOT smear one ESPN name across both games of a doubleheader.

Regression for the 2026-07-22 Red Sox DH: ESPN's feed has one probable per
(date, team), so a blind fill put "Jake Bennett" on BOTH games — masking the
still-open game 2 (which Suarez might start) and overriding game 1 (MLB: Rivera).
"""
from app import cli


def _game(pk, date, team, *, probable=None):
    return {"game_pk": pk, "game_date": date, "espn_team_id": team,
            "probable_pitcher_name": probable}


def test_single_game_day_is_filled(monkeypatch):
    monkeypatch.setattr(cli.espn_public, "fetch_probables",
                        lambda s, e: {("2026-07-22", 2): "Ace Pitcher"})
    games = [_game(1, "2026-07-22", 2)]
    n = cli._overlay_espn_probables(games, "2026-07-22", "2026-07-22")
    assert n == 1
    assert games[0]["probable_pitcher_name"] == "Ace Pitcher"


def test_doubleheader_is_left_to_mlb(monkeypatch):
    """Two games, same date+team, both MLB-blank → ESPN overlay skips BOTH."""
    monkeypatch.setattr(cli.espn_public, "fetch_probables",
                        lambda s, e: {("2026-07-22", 2): "Jake Bennett"})
    games = [_game(824735, "2026-07-22", 2), _game(824732, "2026-07-22", 2)]
    n = cli._overlay_espn_probables(games, "2026-07-22", "2026-07-22")
    assert n == 0
    assert all(g["probable_pitcher_name"] is None for g in games)


def test_doubleheader_keeps_mlb_probable_and_leaves_the_open_game_open(monkeypatch):
    """The started DH game keeps MLB's real probable; the open one stays open —
    the overlay never fills either, so no smear."""
    monkeypatch.setattr(cli.espn_public, "fetch_probables",
                        lambda s, e: {("2026-07-22", 2): "Jake Bennett"})
    games = [_game(824735, "2026-07-22", 2, probable="Eduardo Rivera"),  # MLB posted
             _game(824732, "2026-07-22", 2)]                              # still open
    n = cli._overlay_espn_probables(games, "2026-07-22", "2026-07-22")
    assert n == 0
    assert games[0]["probable_pitcher_name"] == "Eduardo Rivera"
    assert games[1]["probable_pitcher_name"] is None


def test_other_teams_single_games_still_filled_alongside_a_dh(monkeypatch):
    """A doubleheader for one team must not suppress overlay fills for other
    teams' single games the same day."""
    monkeypatch.setattr(cli.espn_public, "fetch_probables", lambda s, e: {
        ("2026-07-22", 2): "Jake Bennett",     # Red Sox DH — skipped
        ("2026-07-22", 5): "Solo Starter",     # another team, single game — filled
    })
    games = [_game(824735, "2026-07-22", 2), _game(824732, "2026-07-22", 2),
             _game(900001, "2026-07-22", 5)]
    n = cli._overlay_espn_probables(games, "2026-07-22", "2026-07-22")
    assert n == 1
    assert games[2]["probable_pitcher_name"] == "Solo Starter"
    assert all(g["probable_pitcher_name"] is None for g in games[:2])


# ── min-rest conflict guard (added 2026-08-24) ──────────────────────────────
# The two feeds can disagree about which DAY a starter goes, and fill-only
# merging cannot see the conflict. Live on 2026-08-24: MLB had Misiorowski (and
# Sale) on 08-27; ESPN's rotation slotted another arm there and pushed them to
# 08-28. MLB had not named 08-28, so the overlay filled ESPN's guess and the same
# pitcher was probable on consecutive days — 2.00 projected starts where 1 was
# physically possible (INV_SP_STARTS_IMPOSSIBLE fired on m123 and m125).

def test_overlay_skips_a_pitcher_mlb_already_has_within_min_rest(monkeypatch):
    """THE 2026-08-24 REGRESSION (Brewers, real dates and names)."""
    monkeypatch.setattr(cli.espn_public, "fetch_probables", lambda s, e: {
        ("2026-08-27", 8): "Robert Gasser",        # ESPN's view of 08-27
        ("2026-08-28", 8): "Jacob Misiorowski",    # ...pushed a day late
    })
    games = [
        _game(823581, "2026-08-27", 8, probable="Jacob Misiorowski"),  # MLB posted
        _game(823744, "2026-08-28", 8),                                # MLB blank
    ]
    n = cli._overlay_espn_probables(games, "2026-08-27", "2026-08-28")
    assert n == 0
    assert games[0]["probable_pitcher_name"] == "Jacob Misiorowski"   # MLB untouched
    assert games[1]["probable_pitcher_name"] is None                  # left OPEN


def test_overlay_still_fills_a_different_pitcher_on_the_next_day(monkeypatch):
    """The guard is per-pitcher, not per-day: a *different* arm the day after an
    announced start is normal rotation and must still be filled."""
    monkeypatch.setattr(cli.espn_public, "fetch_probables",
                        lambda s, e: {("2026-08-28", 8): "Logan Henderson"})
    games = [_game(823581, "2026-08-27", 8, probable="Jacob Misiorowski"),
             _game(823744, "2026-08-28", 8)]
    n = cli._overlay_espn_probables(games, "2026-08-27", "2026-08-28")
    assert n == 1
    assert games[1]["probable_pitcher_name"] == "Logan Henderson"


def test_overlay_allows_the_same_pitcher_at_a_legal_gap(monkeypatch):
    """A genuine two-start week must survive — Bear Nation's 08-18 + 08-23 pair
    is exactly MIN_REST_DAYS apart and is legitimate."""
    monkeypatch.setattr(cli.espn_public, "fetch_probables",
                        lambda s, e: {("2026-08-23", 12): "Bryce Miller"})
    games = [_game(1, "2026-08-18", 12, probable="Bryce Miller"),   # MLB posted
             _game(2, "2026-08-23", 12)]                            # 5 days later
    n = cli._overlay_espn_probables(games, "2026-08-18", "2026-08-23")
    assert n == 1
    assert games[1]["probable_pitcher_name"] == "Bryce Miller"


def test_overlay_does_not_duplicate_a_name_inside_its_own_tail(monkeypatch):
    """Both days MLB-blank: the first fill is accepted and then blocks the
    second, so ESPN cannot duplicate a name within its own un-announced tail."""
    monkeypatch.setattr(cli.espn_public, "fetch_probables", lambda s, e: {
        ("2026-08-28", 8): "Jacob Misiorowski",
        ("2026-08-29", 8): "Jacob Misiorowski",
    })
    games = [_game(1, "2026-08-29", 8), _game(2, "2026-08-28", 8)]   # deliberately unordered
    n = cli._overlay_espn_probables(games, "2026-08-28", "2026-08-29")
    assert n == 1
    by_date = {g["game_date"]: g["probable_pitcher_name"] for g in games}
    assert by_date["2026-08-28"] == "Jacob Misiorowski"   # earlier day wins
    assert by_date["2026-08-29"] is None
