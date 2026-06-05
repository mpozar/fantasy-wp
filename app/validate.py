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

Four layers of check, by scope:
  - **per-matchup** (`_CHECKS`, operate on one `view`): WP range/sum, rate
    components present, all scored cats present, banked totals can't shrink, rate
    sanity bounds, sim accounting, non-empty budgets, projection vs current, unit
    caps, WP swing, rate divergence.
  - **league-level** (`_LEAGUE_CHECKS`, operate on all views): correlated swing —
    many matchups moving in one tick is a systemic-data fingerprint.
  - **pipeline freshness** (`check_pipeline_freshness`): newest snapshot/fetch too
    old ⇒ a cron died silently and the site is serving stale data.
  - **published site** (`check_published_site`): the actual `data.json` the site
    renders — a started week with no scored-cat values is "no stats on the site".

Design note: most past bugs were *emergent* (a fetch change broke what the sim
consumes), so unit tests missed them. These checks assert end-to-end properties
of the actual computed output — the layer where those bugs surface. Corollary
(learned the hard way on 2026-06-04): a check's "should I fire?" gate must not
depend on the data that can go missing (e.g. gate on OUTS, which a partial fetch
still writes, not on the counting cats that vanish), or it goes silent exactly
when it's needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import sim

# League category stat ids, split by kind.
_COUNTING = [1, 5, 20, 23, 48, 63, 83]   # H, HR, R, SB, K, QS, SVHD (cumulative)
_RATES = [18, 47, 41]                     # OPS, ERA, WHIP (ratios)
NAME = {1: "H", 5: "HR", 20: "R", 23: "SB", 48: "K", 63: "QS", 83: "SVHD",
        18: "OPS", 47: "ERA", 41: "WHIP"}

# Rate cats are *derived* from components at publish time (see cli._apply_derived_rates),
# not read from the stored scraped rate — so the cross-source DB check must derive too.
# Each rate is only comparable once its gating component is banked (else publish fell
# back to the scraped value / the derivation degenerates), so gate on it.
_RATE_DERIVERS = {18: sim.derive_ops, 47: sim.derive_era, 41: sim.derive_whip}
_RATE_REQUIRES = {18: sim.STAT_AB, 47: sim.STAT_OUTS, 41: sim.STAT_OUTS}

# Tunable thresholds.
WP_SWING = 0.15           # |home_wp - prev| flagged for review
RATE_DIVERGENCE = 0.40    # projected rate >40% off current → anomaly
MIN_OUTS_FOR_RATE = 60    # ~20 IP banked before a rate divergence is meaningful
PROJ_BELOW_CURRENT_TOL = 0.5  # counting: projected may dip this far below current (MC noise)
MAX_SP_UNITS_PER_WEEK = 2.3    # ceiling on SP starts per 7 days; scaled by actual
                               # period length (some periods — e.g. the All-Star
                               # break — span 14 days, where ~2-3 starts is real).

# A banked counting cat that drops counts as "lost" only past BOTH guards, so a
# routine ESPN ±1 stat correction doesn't cry wolf — the incident halved totals.
BANKED_REGRESS_ABS = 2.0       # must drop by more than this many …
BANKED_REGRESS_FRAC = 0.10     # … AND more than this fraction of the prior value.

# Physically possible bounds for derived rate cats (a blowup → div-by-zero / missing
# components lands far outside these; the 8.37→3.76 bug stayed *in* range, so this is
# a coarse backstop for the gross case, not a replacement for ANOM_RATE_DIVERGENCE).
RATE_BOUNDS = {18: (0.0, 2.0), 47: (0.0, 30.0), 41: (0.0, 6.0)}  # OPS, ERA, WHIP

# A simultaneous swing across many matchups in one compute is a systemic-data
# fingerprint, not coincident roster moves — the signature both 2026-06-04
# incidents shared. Lower per-matchup bar than ANOM_WP_SWING; the signal is the count.
CORRELATED_SWING_EACH = 0.10   # a matchup that moved at least this much "swung"
MIN_CORRELATED = 3             # this many swinging in one tick = systemic

STALE_MINUTES = 20             # snapshot/fetch/site older than this ⇒ pipeline stalled
                               # (~4 missed 5-min fast ticks; medium.sh lock is ≤5 min)

WP_DETAILS_TOL = 0.005         # home_wp column vs details_json tally; they're the same
                               # sim (column = wins/n_sims) so any real gap is a bug —
                               # hand-edited (edited=1) rows are skipped, not toleranced.

FLAP_WINDOW = 6                # snapshots of WP history to scan for oscillation
FLAP_LEG = 0.08               # a move ≥8pp counts as "significant" (filters MC jitter)
FLAP_MIN_REVERSALS = 2         # this many direction flips among significant moves = flapping
                               # (distinct from a one-way swing or a swing-then-recover)


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


def _active(view) -> bool:
    """Is this matchup still live/upcoming (not a decided past week)? Several checks
    only make sense for active weeks: a *completed* week legitimately has empty
    budgets, and its long-ago finalizing WP snap would re-fire on every `--all`
    audit. ESPN marks decided weeks HOME/AWAY/TIE and live/future ones UNDECIDED."""
    return (view.get("winner") or "UNDECIDED") == "UNDECIDED"


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


def check_wp_flapping(view) -> list[Finding]:
    """home_wp oscillating back-and-forth across recent ticks (up, down, up …) — as
    opposed to one swing, or a swing that recovers — points at a stat that keeps
    being written then dropped then rewritten (a flaky scrape/source regressing a
    counting cat, e.g. K 26→20→26). A single per-tick swing can't reveal this;
    `ANOM_WP_SWING` would just fire repeatedly without naming the pattern."""
    if not _active(view):
        return []
    h = [x for x in (view.get("wp_history") or []) if x is not None]
    if len(h) < 3:
        return []
    signs = [1 if (b - a) > 0 else -1 for a, b in zip(h, h[1:]) if abs(b - a) >= FLAP_LEG]
    reversals = sum(1 for x, y in zip(signs, signs[1:]) if x != y)
    if reversals >= FLAP_MIN_REVERSALS:
        return [Finding("ANOM_WP_FLAPPING", "warn", view["matchup_id"],
                        f"home_wp oscillated ({reversals} reversals ≥{FLAP_LEG * 100:.0f}pp) "
                        f"over the last {len(h)} ticks {[round(x, 2) for x in h]} — a stat may "
                        f"be flapping (flaky scrape/source writing then dropping a cat)")]
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


def check_banked_not_regressed(view) -> list[Finding]:
    """A banked counting total can only go *up* within a week. If a cat dropped
    meaningfully vs the prior fetch, banked scoring totals were lost — a dropped
    scrape line, a stale source the monotonicity guard couldn't un-regress, or a
    partial-write read artifact. Pairs with INV_CURRENT_CATS_MISSING (that catches
    a cat vanishing entirely; this catches it shrinking). Ignores ±1-type ESPN stat
    corrections so it only fires on real loss."""
    out = []
    for who, cur, prev in (("home", view["home_state"], view.get("home_state_prev") or {}),
                           ("away", view["away_state"], view.get("away_state_prev") or {})):
        for sid in _COUNTING:
            c, p = cur.get(sid), prev.get(sid)
            if c is None or p is None:
                continue
            drop = p - c
            if drop > BANKED_REGRESS_ABS and drop > BANKED_REGRESS_FRAC * p:
                out.append(Finding("INV_BANKED_REGRESSED", "error", view["matchup_id"],
                                   f"{who} {NAME[sid]} banked {p:.0f} → {c:.0f} "
                                   f"(lost {drop:.0f}) — banked totals can't decrease"))
    return out


def check_rate_ranges(view) -> list[Finding]:
    """Derived rate cats (ERA/WHIP/OPS), current or projected, outside physically
    possible bounds = a derivation blowup (div-by-zero, missing components, garbage
    state). Coarse backstop for the gross case."""
    out = []
    for sid, (lo, hi) in RATE_BOUNDS.items():
        for who, st in (("home", view["home_state"]), ("away", view["away_state"])):
            v = st.get(sid)
            if v is not None and not (lo <= v <= hi):
                out.append(Finding("INV_RATE_RANGE", "error", view["matchup_id"],
                                   f"{who} current {NAME[sid]}={v:.2f} outside [{lo:g},{hi:g}]"))
        avg = view["cat_avg"].get(sid)
        if avg:
            for idx, who in ((0, "home"), (1, "away")):
                v = avg[idx]
                if v is not None and not (lo <= v <= hi):
                    out.append(Finding("INV_RATE_RANGE", "error", view["matchup_id"],
                                       f"{who} projected {NAME[sid]}={v:.2f} outside [{lo:g},{hi:g}]"))
    return out


def check_category_sim_counts(view) -> list[Finding]:
    """Sim accounting: each category's home_wins+away_wins+ties must equal n_sims,
    and so must the matchup tally. A mismatch means the win-counting is broken (and
    every WP derived from it is suspect)."""
    n = view.get("n_sims")
    if not n:
        return []
    out = []
    t = view.get("tally")
    if t and None not in t and abs(sum(t) - n) > 1:
        out.append(Finding("INV_CAT_SIM_COUNT", "error", view["matchup_id"],
                           f"matchup tally {t} sums to {sum(t)} ≠ n_sims {n}"))
    for c in view.get("cat_counts", []):
        s = c["home_wins"] + c["away_wins"] + c["ties"]
        if abs(s - n) > 1:
            out.append(Finding("INV_CAT_SIM_COUNT", "error", view["matchup_id"],
                               f"{NAME.get(c['stat_id'], c['stat_id'])} wins+ties {s} ≠ n_sims {n}"))
    return out


def check_wp_details_consistency(view) -> list[Finding]:
    """The `home_wp`/`away_wp` columns and `details_json`'s win tally are the same
    sim in two forms (column == wins/n_sims), so they must agree. A divergence means
    something wrote one without the other — a publish/compute bug, or an *unlogged*
    hand-edit. Rows hand-smoothed over a logged incident are marked `edited=1` and
    skipped (their divergence is intentional — see INCIDENTS.md)."""
    if view.get("edited"):
        return []
    n, t = view.get("n_sims"), view.get("tally")
    if not n or not t or None in t:
        return []
    out = []
    for who, wp, wins in (("home", view["home_wp"], t[0]), ("away", view["away_wp"], t[1])):
        if wp is None:
            continue
        derived = wins / n
        if abs(wp - derived) > WP_DETAILS_TOL:
            out.append(Finding("INV_WP_DETAILS_MISMATCH", "error", view["matchup_id"],
                               f"{who}_wp column {wp:.3f} ≠ details tally {derived:.3f} "
                               f"({wins}/{n}) — column/details_json disagree "
                               f"(unlogged edit or publish/compute bug)"))
    return out


def check_empty_budgets(view) -> list[Finding]:
    """A side with no player budgets while the matchup has any banked state means
    the roster/projection fetch produced nothing — WP degenerates to a coin flip.
    (Empty before the week has any data is fine — skipped.)"""
    if view.get("home_budget_n") is None:        # loader didn't populate counts
        return []
    if not _active(view):                        # a finished week has no budgets — fine
        return []
    if not (view["home_state"] or view["away_state"]):
        return []
    out = []
    for who, n in (("home", view["home_budget_n"]), ("away", view["away_budget_n"])):
        if n == 0:
            out.append(Finding("INV_EMPTY_BUDGETS", "error", view["matchup_id"],
                               f"{who} has no player budgets while the week has data "
                               f"— roster/projection fetch failed?"))
    return out


_CHECKS = [check_wp_range, check_rate_components, check_current_cats_present,
           check_banked_not_regressed, check_rate_ranges, check_category_sim_counts,
           check_wp_details_consistency, check_empty_budgets, check_proj_vs_current,
           check_units, check_wp_swing, check_wp_flapping, check_rate_divergence]

# League-level checks operate on *all* views at once (cross-matchup correlations).
_LEAGUE_CHECKS = []  # populated below (after the functions are defined)


def check_view(view) -> list[Finding]:
    out = []
    for fn in _CHECKS:
        out.extend(fn(view))
    return out


# ── league-level checks (operate on all views in one tick) ──

def check_correlated_swing(views) -> list[Finding]:
    """Many matchups swinging in a *single* compute is a systemic-data fingerprint
    — banked totals dropped, a fetch wrote a partial set, the read collapsed — not
    a bunch of coincident roster moves (those are per-team and don't cluster on one
    tick). This is the loud, cross-matchup catch the per-matchup ANOM_WP_SWING
    can't make. Both 2026-06-04 incidents had exactly this shape."""
    swung = []
    for v in views:
        if not _active(v):       # a decided week's old finalizing snap isn't a live swing
            continue
        p, h = v.get("prev_home_wp"), v.get("home_wp")
        if p is None or h is None:
            continue
        if abs(h - p) >= CORRELATED_SWING_EACH:
            swung.append((v["matchup_id"], p, h))
    if len(swung) < MIN_CORRELATED:
        return []
    toward_even = sum(1 for _, p, h in swung if abs(h - 0.5) < abs(p - 0.5))
    mids = ",".join(f"m{m}" for m, _, _ in swung)
    return [Finding("ANOM_CORRELATED_SWING", "error", None,
                    f"{len(swung)} matchups swung ≥{CORRELATED_SWING_EACH * 100:.0f}pp in one "
                    f"compute ({toward_even} toward 50/50) — systemic data issue, not "
                    f"coincident roster moves [{mids}]")]


_LEAGUE_CHECKS.append(check_correlated_swing)


# ── pipeline + published-site checks (the output the user actually sees) ──

def _minutes_old(stamp_iso: str | None, now_iso: str | None) -> float | None:
    if not stamp_iso or not now_iso:
        return None
    from datetime import datetime
    try:
        return abs((datetime.fromisoformat(now_iso) - datetime.fromisoformat(stamp_iso))
                   .total_seconds()) / 60.0
    except (ValueError, TypeError):
        return None


def check_pipeline_freshness(conn, now_iso: str | None) -> list[Finding]:
    """The crons can die silently (lock wedged, exception, macOS FDA revoked) and
    the site then serves stale data with no error. Flag when the newest snapshot or
    fetch is too old to be live."""
    if not now_iso:
        return []
    out = []
    for label, code, q in (
        ("wp_snapshot", "ANOM_STALE_SNAPSHOTS", "SELECT MAX(computed_at) m FROM wp_snapshots"),
        ("category_state fetch", "ANOM_STALE_FETCH", "SELECT MAX(fetched_at) m FROM category_state"),
    ):
        row = conn.execute(q).fetchone()
        age = _minutes_old(row["m"] if row else None, now_iso)
        if age is not None and age > STALE_MINUTES:
            out.append(Finding(code, "warn", None,
                               f"latest {label} is {age:.0f} min old (> {STALE_MINUTES}) "
                               f"— compute/fetch cron may be stalled"))
    return out


def check_scrape_health(in_progress: int, scraped_cells: int) -> list[Finding]:
    """Fetch-time guard (called from `fetch`, not `run`). During live games the DOM
    scrape is the only fresh source for the display cats — REST lags 5-30 min. If it
    produced nothing (auth wall, expired Playwright profile, selector drift), `fetch`
    silently falls back to stale REST and the display cats quietly rot *while games
    are live*, exactly when freshness matters most. `run`'s checks can't see this —
    they don't know a scrape was even attempted — so we flag it at the source."""
    if in_progress and scraped_cells == 0:
        return [Finding("ANOM_SCRAPE_EMPTY", "warn", None,
                        f"{in_progress} game(s) in progress but the live scrape returned "
                        f"0 cells — display cats falling back to laggy REST; check ESPN "
                        f"auth / .playwright_profile (re-run scripts/espn_auth_setup.py)")]
    return []


def persist(conn, findings: list[Finding], now: str) -> None:
    """Upsert findings into `validation_flags`, deduped per code+matchup+day (a
    recurrence the same day bumps `occurrences`/`last_seen`). Shared by `validate_cmd`
    and the fetch-time `check_scrape_health` so flags are written one way."""
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


def check_published_site(data_json_path: str | None, now_iso: str | None,
                         *, conn=None) -> list[Finding]:
    """Validate the *actual published artifact* the site renders. Catches the
    user-visible failure directly: a started week whose matchup blocks have no
    scored-cat values = "no stats showing on the site". Also flags a stale or
    unreadable data.json (publish silently failing). With `conn`, also cross-checks
    that the published scores *agree* with the DB (not just that they exist) — see
    the cross-source note below."""
    if not data_json_path:
        return []
    import json
    import os
    if not os.path.exists(data_json_path):
        return [Finding("INV_SITE_MISSING", "error", None,
                        f"data.json not found at {data_json_path} — publish never ran?")]
    try:
        with open(data_json_path) as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        return [Finding("INV_SITE_UNREADABLE", "error", None, f"data.json unreadable: {e}")]
    out = []
    gen = d.get("generated_at")
    age = _minutes_old(gen, now_iso)
    if age is not None and age > STALE_MINUTES:
        out.append(Finding("ANOM_SITE_STALE", "warn", None,
                           f"data.json generated_at is {age:.0f} min old — publish may be failing"))
    scored = _COUNTING + _RATES
    for w in d.get("weeks", []):
        if w.get("state") not in ("live", "final"):
            continue  # upcoming weeks legitimately show no scores
        pid = w.get("matchup_period_id")
        for m in w.get("matchups", []):
            for side in ("home", "away"):
                blk = m.get(side) or {}
                cats = (blk.get("batting") or []) + (blk.get("pitching") or [])
                present = {c.get("stat_id") for c in cats if c.get("score") is not None}
                missing = [NAME[s] for s in scored if s not in present]
                if missing:
                    out.append(Finding("INV_SITE_MISSING_SCORES", "error", m.get("matchup_id"),
                                       f"period {pid} {side} site block missing scored cat(s) "
                                       f"{','.join(missing)} — stats not showing on the site"))
                # Cross-source: the published scores must match what's in the DB. We
                # compare against category_state *as of generated_at* (what publish
                # actually read), so a fetch landing after publish can't manufacture a
                # false mismatch. Scoped to the live week (fresh, unpruned) to stay cheap.
                if (conn is not None and gen and w.get("state") == "live"
                        and blk.get("team_id") is not None):
                    dbstate = _state_as_of(conn, m.get("matchup_id"), blk["team_id"], gen)
                    for c in cats:
                        sid, pub = c.get("stat_id"), c.get("score")
                        if pub is None:
                            continue
                        # Rate cats: compare against the value derived from the DB
                        # components (what publish emits), not the stored scraped
                        # rate. Counting cats: the banked total. Skip a rate until its
                        # gating component is banked — publish falls back to the
                        # scraped value there, so it isn't comparable to a derivation.
                        deriver = _RATE_DERIVERS.get(sid)
                        if deriver is not None:
                            if dbstate.get(_RATE_REQUIRES[sid], 0) <= 0:
                                continue
                            db_val = deriver(dbstate)
                        elif sid in dbstate:
                            db_val = dbstate[sid]
                        else:
                            continue
                        tol = 0.05 if sid in _RATES else 0.5
                        if abs(pub - db_val) > tol:
                            out.append(Finding("INV_SITE_DB_MISMATCH", "error", m.get("matchup_id"),
                                               f"period {pid} {side} {NAME.get(sid, sid)} "
                                               f"site={pub} vs DB={db_val} (as of {gen[:16]}) "
                                               f"— published artifact disagrees with the DB"))
    return out


# ── DB loading + orchestration ──

def _state_as_of(conn, matchup_id: int, team_id: int, at_iso: str) -> dict[int, float]:
    """Per-stat latest banked value at-or-before `at_iso` — i.e. exactly what a
    publish stamped `generated_at=at_iso` would have read. Lets the cross-source
    check compare data.json to the DB snapshot publish saw, immune to later fetches."""
    rows = conn.execute(
        """
        SELECT stat_id, score FROM category_state cs
        WHERE matchup_id=? AND team_id=? AND fetched_at <= ?
          AND fetched_at = (SELECT MAX(fetched_at) FROM category_state c2
                            WHERE c2.matchup_id=cs.matchup_id AND c2.team_id=cs.team_id
                              AND c2.stat_id=cs.stat_id AND c2.fetched_at <= ?)
        """,
        (matchup_id, team_id, at_iso, at_iso),
    ).fetchall()
    return {r["stat_id"]: r["score"] for r in rows}


def _load_state_prev(conn, matchup_id: int, team_id: int) -> dict[int, float]:
    """The *second*-latest banked value per stat (for the banked-regression check).
    Per-stat, same reasoning as load_latest_state — stats aren't all written every
    tick, so 'previous' is per-stat, not the matchup's prior fetch timestamp."""
    rows = conn.execute(
        """
        SELECT stat_id, score FROM (
            SELECT stat_id, score,
                   ROW_NUMBER() OVER (PARTITION BY stat_id ORDER BY fetched_at DESC) rn
            FROM category_state WHERE matchup_id=? AND team_id=?
        ) WHERE rn = 2
        """,
        (matchup_id, team_id),
    ).fetchall()
    return {r["stat_id"]: r["score"] for r in rows}

def load_view(conn, matchup_id: int) -> dict | None:
    m = conn.execute(
        "SELECT home_team_id, away_team_id, matchup_period_id, winner FROM matchups WHERE id=?",
        (matchup_id,)).fetchone()
    snaps = conn.execute(
        "SELECT home_wp, away_wp, details_json, edited FROM wp_snapshots "
        "WHERE matchup_id=? ORDER BY computed_at DESC LIMIT ?",
        (matchup_id, FLAP_WINDOW)).fetchall()
    if not m or not snaps:
        return None
    import json
    from app import mlb
    ws, we = mlb.matchup_period_window(m["matchup_period_id"])
    d = json.loads(snaps[0]["details_json"] or "{}")
    cat_wp = d.get("category_wp", [])
    have_tally = all(k in d for k in ("home_wins", "away_wins", "ties"))
    return {
        "matchup_id": matchup_id,
        "winner": m["winner"],
        "edited": snaps[0]["edited"],
        "period_days": (we - ws).days + 1,
        "home_wp": snaps[0]["home_wp"],
        "away_wp": snaps[0]["away_wp"],
        "prev_home_wp": snaps[1]["home_wp"] if len(snaps) > 1 else None,
        "wp_history": [s["home_wp"] for s in reversed(snaps)],  # chronological (oldest→newest)
        "cat_avg": {c["stat_id"]: (c.get("home_avg"), c.get("away_avg")) for c in cat_wp},
        "budgets": (d.get("home_budgets", []) + d.get("away_budgets", [])),
        "home_budget_n": len(d.get("home_budgets", [])),
        "away_budget_n": len(d.get("away_budgets", [])),
        "n_sims": d.get("n_sims"),
        "tally": (d.get("home_wins"), d.get("away_wins"), d.get("ties")) if have_tally else None,
        "cat_counts": [{"stat_id": c["stat_id"], "home_wins": c.get("home_wins", 0),
                        "away_wins": c.get("away_wins", 0), "ties": c.get("ties", 0)}
                       for c in cat_wp if "home_wins" in c],
        "home_state": sim.load_latest_state(conn, matchup_id, m["home_team_id"]),
        "away_state": sim.load_latest_state(conn, matchup_id, m["away_team_id"]),
        "home_state_prev": _load_state_prev(conn, matchup_id, m["home_team_id"]),
        "away_state_prev": _load_state_prev(conn, matchup_id, m["away_team_id"]),
    }


def run(conn, period_ids: list[int], *, now: str | None = None,
        data_json_path: str | None = None) -> list[Finding]:
    """Run all checks over the latest snapshot of every matchup in the given
    periods, plus league-level (cross-matchup), pipeline-freshness, and
    published-site checks. Returns findings (does not persist — caller decides).
    `now` (ISO) enables the freshness checks; `data_json_path` enables the
    site check."""
    placeholders = ",".join("?" * len(period_ids))
    mids = [r["id"] for r in conn.execute(
        f"SELECT id FROM matchups WHERE matchup_period_id IN ({placeholders})", period_ids)]
    views = [v for v in (load_view(conn, mid) for mid in mids) if v]

    findings: list[Finding] = []
    for view in views:
        findings.extend(check_view(view))
    for fn in _LEAGUE_CHECKS:
        findings.extend(fn(views))
    findings.extend(check_pipeline_freshness(conn, now))
    findings.extend(check_published_site(data_json_path, now, conn=conn))
    return findings
