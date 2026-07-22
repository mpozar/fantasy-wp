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
