"""Invariant + anomaly checks over computed WP snapshots — "common-sense
validation".

Cheap: reads stored snapshots + category_state (no simulation), so it can run on
every fast tick. Two flavors of finding:

  - **error** (invariant): something that must NEVER hold — almost certainly a
    bug (e.g. a team's projected end total is *below* what it's already banked,
    or the rate components vanished from current_state).
  - **warn** (anomaly): unusual but possibly legit — flagged for a human to
    eyeball (e.g. a >15pp WP swing, or a projected rate wildly off the current
    one). Investigate open flags in Claude Code: `app validate --list`.

Findings are upserted into `validation_flags` (deduped per code+matchup+day).

Design note: most past bugs were *emergent* (a fetch change broke what the sim
consumes), so unit tests missed them. These checks assert end-to-end properties
of the actual computed output — the layer where those bugs surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import sim

# League category stat ids, split by kind.
_COUNTING = [1, 5, 20, 23, 48, 63, 83]   # H, HR, R, SB, K, QS, SVHD (cumulative)
_RATES = [18, 47, 41]                     # OPS, ERA, WHIP (ratios)
NAME = {1: "H", 5: "HR", 20: "R", 23: "SB", 48: "K", 63: "QS", 83: "SVHD",
        18: "OPS", 47: "ERA", 41: "WHIP"}

# Tunable thresholds.
WP_SWING = 0.15           # |home_wp - prev| flagged for review
RATE_DIVERGENCE = 0.40    # projected rate >40% off current → anomaly
MIN_OUTS_FOR_RATE = 60    # ~20 IP banked before a rate divergence is meaningful
PROJ_BELOW_CURRENT_TOL = 0.5  # counting: projected may dip this far below current (MC noise)
MAX_SP_UNITS_PER_WEEK = 2.3    # ceiling on SP starts per 7 days; scaled by actual
                               # period length (some periods — e.g. the All-Star
                               # break — span 14 days, where ~2-3 starts is real).


@dataclass
class Finding:
    code: str
    severity: str          # 'error' | 'warn'
    matchup_id: int | None
    detail: str


def _started(view) -> bool:
    """Has this matchup accrued any counting stats yet (week underway)?"""
    for st in (view["home_state"], view["away_state"]):
        if any((st.get(c) or 0) > 0 for c in _COUNTING):
            return True
    return False


# ── pure checks (operate on a loaded view dict; unit-testable) ──

def check_wp_range(view) -> list[Finding]:
    out = []
    h, a = view["home_wp"], view["away_wp"]
    for who, v in (("home", h), ("away", a)):
        if v is None or not (0.0 <= v <= 1.0):
            out.append(Finding("INV_WP_RANGE", "error", view["matchup_id"],
                               f"{who}_wp out of [0,1]: {v}"))
    if h is not None and a is not None and (h + a) > 1.01:
        out.append(Finding("INV_WP_SUM", "error", view["matchup_id"],
                           f"home_wp+away_wp={h + a:.3f} > 1"))
    return out


def check_rate_components(view) -> list[Finding]:
    """Once a matchup is underway, current_state must carry the raw rate
    components (ER, OUTS) — without them the sim derives ERA/WHIP from the
    remaining innings only, ignoring the week's banked innings. This is the exact
    bug that flew under the unit tests."""
    if not _started(view):
        return []
    out = []
    for who, st in (("home", view["home_state"]), ("away", view["away_state"])):
        missing = [n for sid, n in ((sim.STAT_ER, "ER"), (sim.STAT_OUTS, "OUTS"))
                   if sid not in st]
        if missing:
            out.append(Finding("INV_RATE_COMPONENTS_MISSING", "error", view["matchup_id"],
                               f"{who} current_state missing {','.join(missing)} "
                               f"— ERA/WHIP projections will ignore banked innings"))
    return out


def check_current_cats_present(view) -> list[Finding]:
    """Once a side has pitched (OUTS banked), current_state must carry every
    *scored* category. The current-period fetch is split-sourced — the live DOM
    scrape owns the 10 display cats, REST writes only the raw rate components —
    and a read keyed on a single MAX(fetched_at) once dropped all 10 scored cats
    on the first *idle* fetch after a slate (only components got a fresh
    timestamp), collapsing every WP toward 50/50. INV_RATE_COMPONENTS_MISSING
    stayed quiet there (ER/OUTS were still present), so nothing flagged.

    Gate on OUTS — a component REST writes every tick, so it survives the very
    drop we're detecting; gating on the counting cats would inherit the same
    blind spot that let this through."""
    out = []
    for who, st in (("home", view["home_state"]), ("away", view["away_state"])):
        if (st.get(sim.STAT_OUTS) or 0) <= 0:
            continue  # this side hasn't pitched yet — nothing banked to expect
        missing = [NAME[sid] for sid in (_COUNTING + _RATES) if sid not in st]
        if missing:
            out.append(Finding("INV_CURRENT_CATS_MISSING", "error", view["matchup_id"],
                               f"{who} current_state missing scored cat(s) "
                               f"{','.join(missing)} — fetch wrote a partial row-set "
                               f"(idle-fetch drop?); WP collapses toward 50/50"))
    return out


def check_proj_vs_current(view) -> list[Finding]:
    """Projected end-of-week counting totals can't be below what's already
    banked (you don't lose K/H/R). A projection under current means the sim isn't
    seeding the current state."""
    out = []
    for sid in _COUNTING:
        avg = view["cat_avg"].get(sid)
        if not avg:
            continue
        for idx, who, st in ((0, "home", view["home_state"]), (1, "away", view["away_state"])):
            proj, cur = avg[idx], (st.get(sid) or 0)
            if proj is not None and proj < cur - PROJ_BELOW_CURRENT_TOL:
                out.append(Finding("INV_PROJ_LT_CURRENT", "error", view["matchup_id"],
                                   f"{who} {NAME[sid]} projected {proj:.1f} < current {cur:.0f}"))
    return out


def check_units(view) -> list[Finding]:
    out = []
    days = view.get("period_days", 7)
    max_sp = MAX_SP_UNITS_PER_WEEK * days / 7.0
    for b in view["budgets"]:
        u = b.get("units")
        if u is None:
            continue
        if u < 0:
            out.append(Finding("INV_NEG_UNITS", "error", view["matchup_id"],
                               f"{b.get('name')} units {u}"))
        if b.get("role") == "SP" and u > max_sp:
            out.append(Finding("INV_SP_UNITS_CAP", "error", view["matchup_id"],
                               f"{b.get('name')} SP units {u:.2f} > {max_sp:.2f} "
                               f"({days}-day period)"))
    return out


def check_wp_swing(view) -> list[Finding]:
    p = view.get("prev_home_wp")
    h = view["home_wp"]
    if p is None or h is None:
        return []
    if abs(h - p) >= WP_SWING:
        return [Finding("ANOM_WP_SWING", "warn", view["matchup_id"],
                        f"home_wp {p * 100:.1f}% → {h * 100:.1f}% "
                        f"(Δ{(h - p) * 100:+.1f}pp) since prior compute")]
    return []


def check_rate_divergence(view) -> list[Finding]:
    """A projected rate far from the current one, once a real sample is banked,
    is the '8.37 ERA projecting 3.76' smell."""
    out = []
    for sid in (sim.STAT_ERA, sim.STAT_WHIP):
        avg = view["cat_avg"].get(sid)
        if not avg:
            continue
        for idx, who, st in ((0, "home", view["home_state"]), (1, "away", view["away_state"])):
            cur, proj, outs = st.get(sid), avg[idx], (st.get(sim.STAT_OUTS) or 0)
            if cur and proj and outs >= MIN_OUTS_FOR_RATE and cur > 0:
                if abs(proj - cur) / cur > RATE_DIVERGENCE:
                    out.append(Finding("ANOM_RATE_DIVERGENCE", "warn", view["matchup_id"],
                                       f"{who} {NAME[sid]} projected {proj:.2f} vs current "
                                       f"{cur:.2f} ({outs / 3:.0f} IP banked)"))
    return out


_CHECKS = [check_wp_range, check_rate_components, check_current_cats_present,
           check_proj_vs_current, check_units, check_wp_swing, check_rate_divergence]


def check_view(view) -> list[Finding]:
    out = []
    for fn in _CHECKS:
        out.extend(fn(view))
    return out


# ── DB loading + orchestration ──

def load_view(conn, matchup_id: int) -> dict | None:
    m = conn.execute(
        "SELECT home_team_id, away_team_id, matchup_period_id FROM matchups WHERE id=?",
        (matchup_id,)).fetchone()
    snaps = conn.execute(
        "SELECT home_wp, away_wp, details_json FROM wp_snapshots "
        "WHERE matchup_id=? ORDER BY computed_at DESC LIMIT 2", (matchup_id,)).fetchall()
    if not m or not snaps:
        return None
    import json
    from app import mlb
    ws, we = mlb.matchup_period_window(m["matchup_period_id"])
    d = json.loads(snaps[0]["details_json"] or "{}")
    return {
        "matchup_id": matchup_id,
        "period_days": (we - ws).days + 1,
        "home_wp": snaps[0]["home_wp"],
        "away_wp": snaps[0]["away_wp"],
        "prev_home_wp": snaps[1]["home_wp"] if len(snaps) > 1 else None,
        "cat_avg": {c["stat_id"]: (c.get("home_avg"), c.get("away_avg"))
                    for c in d.get("category_wp", [])},
        "budgets": (d.get("home_budgets", []) + d.get("away_budgets", [])),
        "home_state": sim.load_latest_state(conn, matchup_id, m["home_team_id"]),
        "away_state": sim.load_latest_state(conn, matchup_id, m["away_team_id"]),
    }


def run(conn, period_ids: list[int]) -> list[Finding]:
    """Run all checks over the latest snapshot of every matchup in the given
    periods. Returns findings (does not persist — caller decides)."""
    findings: list[Finding] = []
    placeholders = ",".join("?" * len(period_ids))
    mids = [r["id"] for r in conn.execute(
        f"SELECT id FROM matchups WHERE matchup_period_id IN ({placeholders})", period_ids)]
    for mid in mids:
        view = load_view(conn, mid)
        if view:
            findings.extend(check_view(view))
    return findings
