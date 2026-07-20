"""Playoff odds: Monte Carlo over the remaining regular season + the bracket.

League structure (verified against ESPN mSettings 2026-07-20):
  - H2H_MOST_CATEGORIES: each week's matchup produces one W or L (category
    ties broken by hits — `tiesRule: STAT_POINTS` — so no tied weeks).
  - Seeding by matchup record (`playoffSeedingRule: H2H_RECORD`), 6 playoff
    teams, top 2 byes, 1-week rounds, no reseeding:
        R1:      3v6, 4v5           (period LAST_REG+1)
        Semis:   1 vs W(4v5), 2 vs W(3v6)   (LAST_REG+2)
        Final:   winners            (LAST_REG+3)
  - Seeding ties (ESPN support, H2H Most Categories article): head-to-head
    record among the tied teams → intradivisional record → coin flip, seating
    one team then RESETTING the chain for the rest. Quintonia is a single
    division ("Majors") with a clean double round-robin, so the chain
    collapses to: H2H among tied (always valid — equal games guaranteed) →
    coin flip. The LM can override seeding by hand; the default is modeled.

Two simulation layers:
  1. Season: each remaining matchup is a Bernoulli draw from its latest
     simulated WP (the current week's snapshot IS the live WP — banked cats +
     projected remainder). Records and the head-to-head grid accumulate per
     simulated season; seeding runs the tiebreak chain exactly.
  2. Bracket: the MC sim has no cross-team interaction, so a hypothetical
     playoff matchup is a comparison of two independently sampled team-weeks
     (`sim.sample_team_totals`). Each team gets N sampled 10-cat value
     tuples per playoff period; a round outcome draws one tuple per side and
     applies the same most-cats + hits-tiebreak rule as the weekly sim. A
     dead-heat draw (equal cats AND equal hits) advances the higher seed.

Honest limits (also disclosed in the site's "How this works"): playoff-week
budgets use today's rosters + ROS projections (September call-ups/trades/IL
unknowable), and weeks are treated as independent — odds at the extremes read
slightly overconfident.
"""

from __future__ import annotations

import json
import random
import sqlite3

from app.sim import CATEGORIES, TIEBREAKER_STAT_ID, cat_value

MODEL_VERSION = "playoffs-v1"

NUM_PLAYOFF_PERIODS = 3     # quarters, semis, final — 1-week rounds
PLAYOFF_TEAM_COUNT = 6
BYE_SEEDS = 2

DEFAULT_SEASON_SIMS = 10_000
DEFAULT_TEAM_SAMPLES = 1_000   # sampled team-weeks per team per playoff period

# Index of the hits tiebreaker within the category value tuple.
_TB_IDX = next(i for i, (sid, _) in enumerate(CATEGORIES)
               if sid == TIEBREAKER_STAT_ID)
_REVERSED = [rev for _, rev in CATEGORIES]


# ── DB loaders ──────────────────────────────────────────────────────────

def load_records(conn: sqlite3.Connection,
                 team_ids: list[int]) -> tuple[dict[int, int], dict[int, int],
                                               dict[int, dict[int, int]]]:
    """(wins, losses, h2h[winner][loser] = win count) from decided matchups."""
    wins = {t: 0 for t in team_ids}
    losses = {t: 0 for t in team_ids}
    h2h = {t: {u: 0 for u in team_ids} for t in team_ids}
    for r in conn.execute(
            "SELECT home_team_id, away_team_id, winner FROM matchups "
            "WHERE winner IN ('HOME','AWAY')"):
        w, l = ((r["home_team_id"], r["away_team_id"])
                if r["winner"] == "HOME" else
                (r["away_team_id"], r["home_team_id"]))
        wins[w] += 1
        losses[l] += 1
        h2h[w][l] += 1
    return wins, losses, h2h


def load_remaining(conn: sqlite3.Connection) -> list[dict]:
    """Undecided regular-season matchups with their latest simulated home WP.

    The latest snapshot for the current week is the live WP (banked + projected
    remainder); future weeks carry the medium-tier projection. A matchup with
    no snapshot yet falls back to a coin flip.
    """
    rows = conn.execute(
        """
        SELECT m.id, m.matchup_period_id, m.home_team_id, m.away_team_id,
               (SELECT s.home_wp FROM wp_snapshots s WHERE s.matchup_id = m.id
                ORDER BY s.computed_at DESC LIMIT 1) AS home_wp
        FROM matchups m WHERE m.winner = 'UNDECIDED'
        ORDER BY m.matchup_period_id, m.id
        """).fetchall()
    return [{
        "matchup_id": r["id"],
        "period": r["matchup_period_id"],
        "home": r["home_team_id"],
        "away": r["away_team_id"],
        "home_wp": r["home_wp"] if r["home_wp"] is not None else 0.5,
        "had_snapshot": r["home_wp"] is not None,
    } for r in rows]


def load_odds_history(conn: sqlite3.Connection) -> list[dict]:
    """Chronological per-run odds for the site's odds-over-time chart:
    [{"t": iso, "teams": {"<team_id>": [p_playoffs, p_bye, p_champion]}}].

    Reads the playoff_odds_runs archive; rows are stored WITHOUT their own
    history (cli strips it before insert) so this never recurses/compounds.
    Unparseable rows are skipped rather than killing the run.
    """
    out = []
    for r in conn.execute("SELECT computed_at, payload_json FROM playoff_odds_runs "
                          "ORDER BY computed_at"):
        try:
            d = json.loads(r["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        out.append({
            "t": d.get("generated_at") or r["computed_at"],
            "teams": {str(t["team_id"]): [t["p_playoffs"], t["p_bye"], t["p_champion"]]
                      for t in d.get("teams", [])},
        })
    return out


# ── Sampled team-weeks → category value tuples ─────────────────────────

def totals_to_values(counters: dict[int, float]) -> tuple[float, ...]:
    """One sampled team-week reduced to its 10 category values (rates derived),
    in CATEGORIES order — all a pairing comparison needs."""
    return tuple(cat_value(counters, sid) for sid, _ in CATEGORIES)


def decide_values(hi: tuple[float, ...], lo: tuple[float, ...]) -> bool:
    """True if `hi` (the higher seed's draw) wins: most categories, hits
    tiebreak, higher seed on a dead heat — mirrors sim._decide plus the
    playoff no-tie rule."""
    hi_cats = lo_cats = 0
    for i, rev in enumerate(_REVERSED):
        a, b = hi[i], lo[i]
        if a == b:
            continue
        if (a < b) if rev else (a > b):
            hi_cats += 1
        else:
            lo_cats += 1
    if hi_cats != lo_cats:
        return hi_cats > lo_cats
    if hi[_TB_IDX] != lo[_TB_IDX]:
        return hi[_TB_IDX] > lo[_TB_IDX]
    return True     # dead heat → higher seed advances


# ── Seeding ─────────────────────────────────────────────────────────────

def seed_order(wins: dict[int, int], h2h: dict[int, dict[int, int]],
               rng: random.Random) -> list[int]:
    """Full seeding order (best first): record, then per tied group the ESPN
    chain — H2H record among the *remaining* tied teams, coin flip — seating
    one team and resetting, exactly as ESPN documents it. (Intradivisional
    record is skipped: single-division league, it equals the already-tied
    overall record. H2H is always valid: double round-robin ⇒ every tied
    group has balanced games.)"""
    order: list[int] = []
    for w in sorted(set(wins.values()), reverse=True):
        group = [t for t, tw in wins.items() if tw == w]
        while group:
            if len(group) == 1:
                order.append(group.pop())
                break
            gwins = {t: sum(h2h[t][u] for u in group if u != t) for t in group}
            best = max(gwins.values())
            top = [t for t in group if gwins[t] == best]
            pick = top[0] if len(top) == 1 else rng.choice(top)
            order.append(pick)
            group.remove(pick)
    return order


# ── Season + bracket simulation ────────────────────────────────────────

def simulate_odds(team_ids: list[int],
                  wins: dict[int, int],
                  h2h: dict[int, dict[int, int]],
                  remaining: list[dict],
                  samples: dict[int, list[list[tuple[float, ...]]]],
                  n_sims: int = DEFAULT_SEASON_SIMS,
                  rng: random.Random | None = None) -> dict[int, dict]:
    """Run n_sims full seasons; return per-team odds.

    `samples[team_id]` = one list of category value tuples per playoff round
    (NUM_PLAYOFF_PERIODS lists). Each simulated round draws one fresh tuple
    per side — independent weeks, same as the underlying model.
    """
    rng = rng or random.Random()
    tally = {t: {"playoffs": 0, "bye": 0, "final": 0, "champ": 0,
                 "win_sum": 0, "seeds": [0] * len(team_ids)}
             for t in team_ids}

    def play(a: int, b: int, rnd: int, seed_of: dict[int, int]) -> int:
        """One playoff game; a/b in any order, returns winner team_id."""
        hi, lo = (a, b) if seed_of[a] < seed_of[b] else (b, a)
        vh = rng.choice(samples[hi][rnd])
        vl = rng.choice(samples[lo][rnd])
        return hi if decide_values(vh, vl) else lo

    for _ in range(n_sims):
        w = dict(wins)
        g = {t: dict(h2h[t]) for t in team_ids}
        for m in remaining:
            if rng.random() < m["home_wp"]:
                mw, ml = m["home"], m["away"]
            else:
                mw, ml = m["away"], m["home"]
            w[mw] += 1
            g[mw][ml] += 1

        order = seed_order(w, g, rng)
        seed_of = {t: i + 1 for i, t in enumerate(order)}
        for t in team_ids:
            tally[t]["win_sum"] += w[t]
            tally[t]["seeds"][seed_of[t] - 1] += 1
        six = order[:PLAYOFF_TEAM_COUNT]
        for t in six:
            tally[t]["playoffs"] += 1
        for t in six[:BYE_SEEDS]:
            tally[t]["bye"] += 1

        w45 = play(six[3], six[4], 0, seed_of)
        w36 = play(six[2], six[5], 0, seed_of)
        f1 = play(six[0], w45, 1, seed_of)
        f2 = play(six[1], w36, 1, seed_of)
        tally[f1]["final"] += 1
        tally[f2]["final"] += 1
        tally[play(f1, f2, 2, seed_of)]["champ"] += 1

    out = {}
    for t in team_ids:
        c = tally[t]
        out[t] = {
            "p_playoffs": c["playoffs"] / n_sims,
            "p_bye": c["bye"] / n_sims,
            "p_final": c["final"] / n_sims,
            "p_champion": c["champ"] / n_sims,
            "exp_wins": c["win_sum"] / n_sims,
            "seed_dist": [n / n_sims for n in c["seeds"]],
        }
    return out
