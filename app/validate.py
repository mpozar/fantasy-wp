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
    caps, WP swing, WP flapping, WP rail-flip (near-0↔near-100), rate divergence.
  - **league-level** (`_LEAGUE_CHECKS`, operate on all views): correlated swing —
    many matchups moving in one tick is a systemic-data fingerprint.
  - **pipeline freshness** (`check_pipeline_freshness`): newest snapshot/fetch too
    old ⇒ a cron died silently and the site is serving stale data.
  - **published site** (`check_published_site`): the actual `data.json` the site
    renders — a started week with no scored-cat values is "no stats on the site";
    a cross-source DB mismatch; and an independent QS/SVHD over-credit recompute
    (`max(scrape, settled_floor + box)`) that catches the phantom double-count.

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
from datetime import date, datetime, timedelta

from app import db, sim, stats

# League category stat ids, split by kind.
_COUNTING = [1, 5, 20, 23, 48, 63, 83]   # H, HR, R, SB, K, QS, SVHD (cumulative)
_RATES = list(stats.RATE_STATS)           # OPS, ERA, WHIP (ratios) — canonical
NAME = stats.STAT_NAMES                    # canonical stat_id -> name (single source)

# Cats whose *displayed* value publish derives from the live box-score
# reconstruction (cli._fold_live_components → _apply_derived_rates / _count_qs etc.),
# not from raw category_state: ERA/WHIP/OPS (derived from reconstructed components)
# and QS/SVHD (reconstructed counting credits). The cross-source DB check skips
# these — they aren't comparable to raw category_state during live games — and only
# checks the scrape-owned counting cats (H/HR/R/SB/K).
_LIVE_RECON_CATS = {18, 47, 41, 63, 83}   # OPS, ERA, WHIP, QS, SVHD

# Tunable thresholds.
WP_SWING = 0.15           # |home_wp - prev| flagged for review
RATE_DIVERGENCE = 0.40    # projected rate >40% off current → anomaly
MIN_OUTS_FOR_RATE = 60    # ~20 IP banked before a rate divergence is meaningful
# …AND the gap must clear an absolute floor in rate points. The relative test
# alone is mis-specified for ERA/WHIP: its denominator is the *current* rate, so
# a hot small sample (a 1.12 ERA over ~22 IP) blows past 40% on nothing but
# ordinary mean reversion, while the bug this check exists for — the dropped-
# components "8.37 ERA projecting 3.76" — is only a 0.55 relative gap. Measured
# over all 25 ANOM_RATE_DIVERGENCE instances ever recorded (2026-06-25 → 07-24,
# every one triaged benign small-sample regression): max benign gap was 1.59 ERA
# / 0.39 WHIP points, vs 4.61 ERA points for the 3.76 bug. So an absolute floor
# separates them cleanly where the ratio cannot. Raising RATE_DIVERGENCE instead
# would have to exceed 1.42 to quiet the noise and would blind the check to its
# own target case.
RATE_DIVERGENCE_ABS = {47: 2.50, 41: 0.80}   # ERA, WHIP — rate points
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
# A *current* ERA/WHIP off <3 IP (or OPS off a tiny AB sample) is statistical noise
# — e.g. WHIP=inf at 0 IP at every week's Monday rollover. Only range-check the
# current rate once this much is banked; projected (full-week) rates always checked.
MIN_OUTS_FOR_RANGE = 9   # 3 IP
MIN_AB_FOR_RANGE = 10

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

RAIL_FLIP = 0.10               # WP within this of a rail (0 or 1) is "at the rail".
                               # A series touching BOTH rails in one window is a
                               # near-0↔near-100 flip — worst-case UX and a fingerprint
                               # of a flaky/over-credited stat (e.g. a phantom QS).

QS_OVERCREDIT_TOL = 0.5        # published QS/SVHD may exceed the independently-supported
                               # max(scrape, settled_floor + box) by at most this (rounding)
WP_DECIDED = 0.99              # a published WP this lopsided should not have its own
                               # displayed category majority favor the OTHER side


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


def check_wp_rail_flip(view) -> list[Finding]:
    """home_wp touching BOTH rails (near-0 AND near-100) within the window. Distinct
    from ANOM_WP_FLAPPING (which counts reversals): this fires on the *magnitude* —
    a near-certain-win flipping to near-certain-loss (or back) is the worst user
    experience and a classic fingerprint of a flaky or over-credited stat lifting WP
    to ~100% until it reverts (the deGrom phantom-QS shape: 100% all night → 0% at
    the settle). A genuine, decisive resolution can trip it too — hence a warn, not
    an error — but a near-0↔near-100 move always deserves a human eyeball."""
    if not _active(view):
        return []
    h = [x for x in (view.get("wp_history") or []) if x is not None]
    if len(h) < 2:
        return []
    lo, hi = min(h), max(h)
    if hi >= 1 - RAIL_FLIP and lo <= RAIL_FLIP:
        return [Finding("ANOM_WP_RAIL_FLIP", "warn", view["matchup_id"],
                        f"home_wp spanned both rails ({lo * 100:.0f}% ↔ {hi * 100:.0f}%) "
                        f"within {len(h)} ticks {[round(x, 2) for x in h]} — a near-0↔near-100 "
                        f"flip (jarring UX; often a flaky/over-credited stat). Investigate.")]
    return []


def check_rate_divergence(view) -> list[Finding]:
    """A projected rate far from the current one, once a real sample is banked,
    is the '8.37 ERA projecting 3.76' smell.

    Requires the gap to clear BOTH a relative (`RATE_DIVERGENCE`) and an absolute
    (`RATE_DIVERGENCE_ABS`) threshold — see the constants for why the ratio alone
    can't tell mean reversion off a hot small sample from a real components drop."""
    out = []
    for sid in (sim.STAT_ERA, sim.STAT_WHIP):
        avg = view["cat_avg"].get(sid)
        if not avg:
            continue
        for idx, who, st in ((0, "home", view["home_state"]), (1, "away", view["away_state"])):
            cur, proj, outs = st.get(sid), avg[idx], (st.get(sim.STAT_OUTS) or 0)
            if cur and proj and outs >= MIN_OUTS_FOR_RATE and cur > 0:
                gap = abs(proj - cur)
                if gap / cur > RATE_DIVERGENCE and gap > RATE_DIVERGENCE_ABS.get(sid, 0):
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
            if v is None:
                continue
            # Skip the *current* rate when too little is banked — an early-week
            # ERA/WHIP off <3 IP (inf at 0 IP) or OPS off a few ABs is meaningless.
            if sid in (sim.STAT_ERA, sim.STAT_WHIP):
                if (st.get(sim.STAT_OUTS) or 0) < MIN_OUTS_FOR_RANGE:
                    continue
            elif (st.get(sim.STAT_AB) or 0) < MIN_AB_FOR_RANGE:        # OPS
                continue
            if not (lo <= v <= hi):
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
    """A side with no player budgets while the matchup has banked state usually
    means the roster/projection fetch produced nothing — WP degenerates.

    But at end of week a fully-rostered side legitimately has no budgets once all
    its *active* players' games are Final (only IL/bench left → nothing to
    project); that's a decided matchup sitting UNDECIDED until rollover, not a
    failure, and flagging it spams an error for every matchup every Sun→Mon tick.
    So fire only when budgets are empty AND either the roster itself is missing
    (real fetch failure) or the side still has remaining active games to project.
    `{who}_roster_n` / `{who}_active_remaining` are absent in older callers/tests →
    default to firing (old behavior). (Empty before any data, or a finished week,
    skipped as before.)"""
    if view.get("home_budget_n") is None:        # loader didn't populate counts
        return []
    if not _active(view):                        # a finished week has no budgets — fine
        return []
    if not (view["home_state"] or view["away_state"]):
        return []
    out = []
    for who, n in (("home", view["home_budget_n"]), ("away", view["away_budget_n"])):
        if n != 0:
            continue
        # Benign end-of-week: roster IS fetched but 0 active games remain to
        # budget. A missing roster (roster_n == 0) or remaining active games
        # still flags — those are the real fetch/projection failures.
        if view.get(f"{who}_roster_n") and view.get(f"{who}_active_remaining") == 0:
            continue
        out.append(Finding("INV_EMPTY_BUDGETS", "error", view["matchup_id"],
                           f"{who} has no player budgets while the week has data "
                           f"— roster/projection fetch failed?"))
    return out


_CHECKS = [check_wp_range, check_rate_components, check_current_cats_present,
           check_banked_not_regressed, check_rate_ranges, check_category_sim_counts,
           check_wp_details_consistency, check_empty_budgets, check_proj_vs_current,
           check_units, check_wp_swing, check_wp_flapping, check_wp_rail_flip,
           check_rate_divergence]

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
    try:
        return abs((datetime.fromisoformat(now_iso) - datetime.fromisoformat(stamp_iso))
                   .total_seconds()) / 60.0
    except (ValueError, TypeError):
        return None


def check_pipeline_freshness(conn, now_iso: str | None) -> list[Finding]:
    """The crons can die silently (lock wedged, exception, macOS FDA revoked) and
    the site then serves stale data with no error. Flag when the newest snapshot or
    fetch is too old to be live.

    Scoped to the **current** period — the one `fast.sh` recomputes every 5-min tick
    (source-of-truth = `team_rosters`, written only for the current period; matches
    `cli._current_matchup_period`) — NOT the earliest *undecided* period. They
    coincide mid-week but diverge at the Sun→Mon rollover: once the live week is
    decided but the next hasn't started, earliest-undecided points at an UPCOMING
    week that only `medium` (every 4h) refreshes, so its snapshots are legitimately
    >STALE_MINUTES old and would false-fire "cron stalled" every Monday (2026-07-27:
    week 16 decided, earliest-undecided=17 upcoming → spurious ANOM_STALE_SNAPSHOTS).
    The current period is what stays on the 5-min cadence, so *its* staleness is the
    true stall signal. Scoping to a small matchup_id set also keeps `MAX(...)` on
    idx_category_state_recent instead of full-scanning the 8.8M-row category_state
    table (~10s → ~10ms, the validate hotspot).

    Season fully over (all matchups decided) ⇒ crons legitimately idle ⇒ no flag."""
    if not now_iso:
        return []
    if not conn.execute("SELECT 1 FROM matchups "
                        "WHERE winner='UNDECIDED' OR winner IS NULL LIMIT 1").fetchone():
        return []   # season over — nothing to keep fresh
    try:
        row = conn.execute(
            "SELECT matchup_period_id FROM team_rosters "
            "GROUP BY matchup_period_id ORDER BY MAX(fetched_at) DESC LIMIT 1").fetchone()
        cur_period = row["matchup_period_id"] if row else None
    except Exception:   # no team_rosters (fresh DB / unit tests) → fall back below
        cur_period = None
    if cur_period is None:   # fall back to the earliest period (as cli does)
        row = conn.execute("SELECT MIN(matchup_period_id) AS p FROM matchups").fetchone()
        cur_period = row["p"] if row else None
    if cur_period is None:
        return []
    cur = [r["id"] for r in conn.execute(
        "SELECT id FROM matchups WHERE matchup_period_id=?", (cur_period,))]
    if not cur:
        return []
    ph = ",".join("?" * len(cur))
    # A fully-DECIDED current period is the Sun→Mon rollover: the just-finished week
    # stays `compute`'s current period (team_rosters) until the next medium refresh
    # advances it, but ESPN's `fetch` has already moved on to the newly-current
    # period. So `compute` keeps this period's wp_snapshots fresh every tick (a stall
    # there IS real → keep watching), while its category_state legitimately stops
    # updating (games over, fetch writing elsewhere) → the fetch-staleness flag would
    # false-fire, so skip it for a decided period. (2026-07-27: wk16 decided, fetch on
    # wk17 → spurious ANOM_STALE_FETCH before this guard.)
    decided = conn.execute(
        f"SELECT NOT EXISTS(SELECT 1 FROM matchups WHERE id IN ({ph}) "
        f"AND (winner='UNDECIDED' OR winner IS NULL)) AS d", cur).fetchone()["d"]
    out = []
    for label, code, col, table in (
        ("wp_snapshot", "ANOM_STALE_SNAPSHOTS", "computed_at", "wp_snapshots"),
        ("category_state fetch", "ANOM_STALE_FETCH", "fetched_at", "category_state"),
    ):
        if code == "ANOM_STALE_FETCH" and decided:
            continue  # decided week: category_state legitimately frozen (see above)
        row = conn.execute(
            f"SELECT MAX({col}) m FROM {table} WHERE matchup_id IN ({ph})", cur).fetchone()
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


# ── Scrape staleness (fetch-time) ────────────────────────────────────────
# Ticks are 5 min (fast.sh), so 9 ticks ≈ 45 min of live baseball with the
# league-wide H+R totals not moving *at all*, across 12 rosters. That isn't a
# quiet night, it's a dead feed. The 2026-08-05 incident sat frozen ~8h; this
# fires ~45 min in.
SCRAPE_STALE_TICKS = 9
# Those ticks must be genuinely consecutive. A wider span means the pipeline
# itself gapped (laptop asleep, network outage) and a settle boundary may sit
# inside the window — a different failure, already covered by
# check_pipeline_freshness, and not evidence of a stale scrape.
SCRAPE_STALE_MAX_SPAN_MIN = 75
# Grace after first pitch: before the slate starts the cats are *legitimately*
# frozen (no baseball has been played), so the window must open late enough that
# a frozen run means something. Without this the check fires every night at the
# moment `in_progress` first goes non-zero.
SCRAPE_STALE_GRACE_MIN = 20
# The scrape-owned cats that move constantly during live play. H and R only:
# HR/SB are low-event enough to stall honestly for 45 min, and QS/SVHD are
# reconstructed elsewhere (_LIVE_RECON_CATS).
_SCRAPE_STALE_CATS = (1, 20)   # H, R


def check_scrape_staleness(conn, in_progress: int, current_period: int,
                           now_iso: str | None) -> list[Finding]:
    """Fetch-time guard: the DOM scrape is returning well-formed but **frozen** cells.

    `ANOM_SCRAPE_EMPTY`'s blind spot. That check fires when the scrape yields
    *nothing*; this one fires when it yields a full 120 cells that never change.
    Both leave the display cats stale, but an empty scrape degrades **loudly**
    (0 cells in the log, one glance at `fetch`'s output) while a frozen one is
    invisible — the values look perfectly well-formed, they're just yesterday's.

    Why this exists (2026-08-05). ESPN's scoreboard page renders an initial REST
    snapshot, then applies live updates from a play-by-play feed. That feed's
    request started returning 403 to our headless browser, so the page never
    applied any live update and the DOM kept showing the REST snapshot — whose
    weekly totals only advance at ESPN's ~07:00 UTC daily settle. The scrape
    faithfully read those frozen numbers all night: H/HR/R/SB/K sat at Monday's
    values through the whole Tuesday slate and the entire day landed in one tick
    (m107 +35.9pp, six categories flipping at once).

    The rate cats went stale *with* them, which is why this is error-grade rather
    than a warn: `sim._judge_group` judges the box-score reconstruction by its
    distance to the **scraped** rate, so a stale scrape that exactly matches the
    equally-stale REST baseline makes every reconstruction look "further away"
    and get rejected (`verdict: baseline`). One frozen source silently takes down
    both input paths, and nothing downstream can tell.

    Gate follows the 2026-06-04 corollary — it keys off the *presence* of ticks
    and live games, never off the cat values that are what goes wrong.
    """
    if not in_progress or not now_iso:
        return []
    row = conn.execute(
        "SELECT MAX(active_start) AS s FROM game_day_activity "
        "WHERE matchup_period_id=? AND active_start IS NOT NULL AND active_end IS NULL",
        (current_period,),
    ).fetchone()
    active_start = row["s"] if row else None
    if not active_start:
        return []          # live games but no open slate recorded yet — too early
    try:
        window_start = (datetime.fromisoformat(active_start)
                        + timedelta(minutes=SCRAPE_STALE_GRACE_MIN)).isoformat()
    except (TypeError, ValueError):
        return []
    if now_iso < window_start:
        return []
    sides = conn.execute(
        "SELECT id, home_team_id, away_team_id FROM matchups WHERE matchup_period_id=?",
        (current_period,)).fetchall()
    if not sides:
        return []
    # One indexed seek per (matchup, team, stat) — the full prefix of
    # idx_category_state_recent. Aggregating across the period instead reads as a
    # whole-index SCAN (2.5s on a 2.6M-row table), far too slow for a 5-min tick.
    stamps: set[str] = set()
    total = 0.0
    for m in sides:
        for team_id in (m["home_team_id"], m["away_team_id"]):
            for stat_id in _SCRAPE_STALE_CATS:
                rows = conn.execute(
                    """
                    SELECT fetched_at, score FROM category_state
                    WHERE matchup_id=? AND team_id=? AND stat_id=?
                          AND fetched_at >= ? AND fetched_at <= ?
                    ORDER BY fetched_at DESC LIMIT ?
                    """,
                    (m["id"], team_id, stat_id, window_start, now_iso,
                     SCRAPE_STALE_TICKS),
                ).fetchall()
                # Not written on every tick ⇒ no clean run to judge; stay quiet.
                if len(rows) < SCRAPE_STALE_TICKS:
                    return []
                if any(r["score"] is None for r in rows):
                    return []
                if len({r["score"] for r in rows}) > 1:
                    return []      # this series moved ⇒ the feed is alive
                stamps.update(r["fetched_at"] for r in rows)
                total += rows[0]["score"]
    try:
        span = max(datetime.fromisoformat(s) for s in stamps) - \
               min(datetime.fromisoformat(s) for s in stamps)
    except (TypeError, ValueError):
        return []
    if span > timedelta(minutes=SCRAPE_STALE_MAX_SPAN_MIN):
        return []          # cadence gapped — that's check_pipeline_freshness's job
    ticks = range(SCRAPE_STALE_TICKS)
    return [Finding(
        "INV_SCRAPE_STALE", "error", None,
        f"{in_progress} game(s) in progress but the scrape's counting cats have not "
        f"moved for {len(ticks)} consecutive ticks ({round(span.total_seconds() / 60)} "
        f"min): league-wide H+R stuck at {total:g}. The scrape is returning "
        f"well-formed but STALE cells (ANOM_SCRAPE_EMPTY can't see this) — display cats "
        f"are frozen at the last settle AND the rate reconstruction is being rejected "
        f"against the stale scrape. Check ESPN's live play-by-play feed request in the "
        f"scoreboard page (2026-08-05: Akamai 403'd it for the headless browser)")]


def check_live_lineup_capture(conn, now_iso: str | None) -> list[Finding]:
    """Live component reconstruction needs a `daily_lineups` snapshot to know who
    counted for each team that day. If box-score lines exist for an unsettled day
    but no lineup was captured for it (ESPN auth hiccup / fetch failure in
    refresh-live), every team silently falls back to ESPN's once-daily-stale
    components — the exact staleness reconstruction is meant to fix, failing
    quietly. Cheap two-count guard on the unsettled window."""
    if not now_iso:
        return []
    try:
        boundary = sim.settle_boundary_date(datetime.fromisoformat(now_iso))
    except (ValueError, TypeError):
        return []
    days = conn.execute(
        """
        SELECT DISTINCT ts.game_date AS gd
        FROM team_schedule ts
        WHERE ts.game_date >= ?
          AND (EXISTS (SELECT 1 FROM live_pitchers lp WHERE lp.game_pk = ts.game_pk)
            OR EXISTS (SELECT 1 FROM live_batters  lb WHERE lb.game_pk = ts.game_pk))
        """,
        (boundary,),
    ).fetchall()
    out = []
    for r in days:
        n = conn.execute("SELECT COUNT(*) c FROM daily_lineups WHERE game_date=?",
                         (r["gd"],)).fetchone()["c"]
        if n == 0:
            out.append(Finding(
                "ANOM_LINEUP_SNAPSHOT_MISSING", "warn", None,
                f"live box-score lines exist for {r['gd']} but no daily_lineups "
                f"snapshot — live component reconstruction is falling back to "
                f"stale ESPN components for all teams; check ESPN auth in refresh-live"))
    return out


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


def resolve(conn, code: str, *, now: str, by: str, note: str | None = None) -> int:
    """Mark open flags resolved *with provenance* (who/when/why). `code='all'`
    resolves everything. Returns the number of rows closed. The note is the whole
    point — it makes a triage conclusion durable next to the flag instead of
    evaporating with the chat that reached it."""
    sql = ("UPDATE validation_flags SET resolved=1, resolved_at=?, resolved_by=?, "
           "resolution_note=? WHERE resolved=0")
    params = [now, by, note]
    if code != "all":
        sql += " AND code=?"
        params.append(code)
    with conn:
        return conn.execute(sql, params).rowcount


def _supported_credit(conn, matchup_id, period_id, team_id, sid, *,
                      scraped, gen, unsettled, since_date) -> float:
    """The maximum QS(63)/SVHD(83) the published artifact is *allowed* to show for one
    side, recomputed independently of publish: `max(scraped_weekly, settled_floor +
    box_count)` — the same rule `cli._fold_live_components` applies, re-derived here
    from raw category_state + box scores. A publish/fold bug (or a regression of the
    double-count fix) then surfaces as `published > supported`. Returns `scraped`
    when no in-window box credit exists (nothing to add), and `None` if the
    supporting data can't be loaded — the caller then skips, so we never flag on an
    inability to recompute, only on a genuine over-credit.

    The floor is consulted **even when there is no in-window box credit** (`box == 0`).
    Publish applies the settled floor in *two* places, not one: the fold's
    `max(scrape, floor + box)`, and — independently — `cli._apply_qs_svhd_floor`,
    which raises the display to `max(scrape, floor)` on every current-week publish so
    a just-Final QS/SVHD the idle scrape hasn't banked yet doesn't render as a stale
    low count (added 2026-07-27, m96 QS 4→5). This recompute originally modelled only
    the fold and short-circuited to `scraped` when `box == 0`, so any credit resting
    purely on the floor read as unsupportable. That produced 1830 false-positive
    occurrences over 2026-07-30..08-07 (m105 Swamp Dragons SVHD site=1 vs "supportable
    0", where the floor genuinely held Bryan Baker's 8/04 save from
    `pitcher_final_lines` and the scrape had never banked it). Always consulting the
    floor keeps the guard intact — a real double-count still exceeds
    `max(scrape, floor + box)` — while matching what publish actually does."""
    try:
        roster = sim.load_team_roster(conn, period_id, team_id)
        slots = sim.load_active_slots(conn, team_id, since_date=since_date,
                                      fallback_roster=roster)
        counter = sim._count_qs if sid == sim.STAT_QS else sim._count_svhd
        box, _ = counter(unsettled["pitchers"], slots)
        floor = sim.load_settled_floor(conn, matchup_id, team_id, (sid,),
                                       since_date=since_date, as_of=gen).get(sid, scraped)
    except Exception:
        return None
    return max(scraped, floor + box)


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
    # For the independent QS/SVHD over-credit guard: derive the unsettled window
    # publish saw (as of generated_at) once, up front.
    unsettled = since_date = None
    if conn is not None and gen:
        try:
            since_date = sim.settle_boundary_date(datetime.fromisoformat(gen))
            unsettled = sim.load_unsettled_lines(conn, since_date=since_date)
        except Exception:   # best-effort guard — never break the rest of validation
            unsettled = since_date = None
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
                # false mismatch. Scoped to the live week (fresh, unpruned) to stay
                # cheap, and to the scrape-owned counting cats (H/HR/R/SB/K) — the
                # rate cats and QS/SVHD are now derived in publish from the *live
                # box-score reconstruction* (so the scoreboard matches the projection;
                # see cli._fold_live_components), not from raw category_state, so
                # they're not comparable to it here. Their freshness is covered by
                # sharing that reconstruction with the WP and by INV_RATE_RANGE.
                if (conn is not None and gen and w.get("state") == "live"
                        and blk.get("team_id") is not None):
                    dbstate = _state_as_of(conn, m.get("matchup_id"), blk["team_id"], gen)
                    for c in cats:
                        sid, pub = c.get("stat_id"), c.get("score")
                        if pub is None or sid in _LIVE_RECON_CATS or sid not in dbstate:
                            continue
                        if abs(pub - dbstate[sid]) > 0.5:
                            out.append(Finding("INV_SITE_DB_MISMATCH", "error", m.get("matchup_id"),
                                               f"period {pid} {side} {NAME.get(sid, sid)} "
                                               f"site={pub} vs DB={dbstate[sid]} (as of {gen[:16]}) "
                                               f"— published artifact disagrees with the DB"))
                    # The live-recon counting cats (QS/SVHD) are skipped above because
                    # they're derived, not raw category_state — but they're exactly the
                    # ones prone to the phantom double-count. Guard them with their own
                    # independent recompute: site must not exceed max(scrape, floor+box).
                    if unsettled is not None:
                        pubmap = {c.get("stat_id"): c.get("score") for c in cats}
                        for sid in (sim.STAT_QS, sim.STAT_SVHD):
                            pub = pubmap.get(sid)
                            if pub is None:
                                continue
                            scraped_sid = dbstate.get(sid, 0) or 0
                            expected = _supported_credit(
                                conn, m.get("matchup_id"), pid, blk["team_id"], sid,
                                scraped=scraped_sid, gen=gen, unsettled=unsettled,
                                since_date=since_date)
                            if expected is None:
                                continue
                            if pub - expected > QS_OVERCREDIT_TOL:
                                out.append(Finding("INV_SITE_QS_OVERCREDIT", "error",
                                    m.get("matchup_id"),
                                    f"period {pid} {side} {NAME.get(sid, sid)} site={pub:g} "
                                    f"> supportable {expected:g} (scrape {scraped_sid:g} + "
                                    f"box-score in-window) — phantom counting credit, the "
                                    f"QS/SVHD double-count vs the live scrape"))
            # Records must mirror: head-to-head category scoring means home wins a
            # category ⟺ away loses it. A non-mirror record is the asymmetric-record
            # bug — per-team stored results desynced under temporal skew (see
            # cli._apply_counting_results, which derives them symmetrically).
            hr, ar = (m.get("home") or {}).get("record"), (m.get("away") or {}).get("record")
            if hr and ar and not (hr["W"] == ar["L"] and hr["L"] == ar["W"] and hr["T"] == ar["T"]):
                out.append(Finding("INV_SITE_RECORD_ASYMMETRIC", "error", m.get("matchup_id"),
                                   f"period {pid} records not mirrored: home "
                                   f"{hr['W']}-{hr['L']}-{hr['T']} vs away "
                                   f"{ar['W']}-{ar['L']}-{ar['T']} — category results desynced"))
            # WP↔category consistency: a near-decided matchup whose displayed
            # category results hand the MAJORITY to the OTHER side is a display bug —
            # the scoreboard contradicts its own WP. (2026-07-27 m96: WP 100% Norsemen
            # yet the cells showed Bear 5-4 via a settle-stale OPS + an un-credited QS.)
            # INV_SITE_DB_MISMATCH can't catch this — it skips the rate cats where the
            # divergence hides. Warn, not error: a decisive WP *can* legitimately lead a
            # current-category deficit projected to flip, but at ≥WP_DECIDED a
            # contradicting majority is almost always a stale-cell display lag to eyeball.
            hb, ab = m.get("home") or {}, m.get("away") or {}
            hwp, awp = hb.get("wp"), ab.get("wp")
            if hwp is not None and awp is not None and max(hwp, awp) >= WP_DECIDED:
                def _wins(blk):
                    return sum(1 for c in (blk.get("batting") or []) + (blk.get("pitching") or [])
                               if c.get("result") == "WIN")
                fav_side, fav_cats, opp_cats = (("home", _wins(hb), _wins(ab)) if hwp >= awp
                                                else ("away", _wins(ab), _wins(hb)))
                if opp_cats > fav_cats:
                    out.append(Finding("INV_SITE_WP_CATEGORY_CONTRADICTION", "warn",
                        m.get("matchup_id"),
                        f"period {pid} WP favors {fav_side} ({max(hwp, awp):.0%}) but displayed "
                        f"categories give the opponent the majority ({fav_cats} vs {opp_cats}) "
                        f"— scoreboard contradicts its own WP (likely a stale rate/QS cell)"))
    return out


# ── DB loading + orchestration ──

def _state_as_of(conn, matchup_id: int, team_id: int, at_iso: str) -> dict[int, float]:
    """Per-stat latest banked value at-or-before `at_iso` — i.e. exactly what a
    publish stamped `generated_at=at_iso` would have read. Lets the cross-source
    check compare data.json to the DB snapshot publish saw, immune to later fetches."""
    return {sid: v["score"] for sid, v in
            db.latest_category_state(conn, matchup_id, team_id, as_of=at_iso).items()}


def _load_state_prev(conn, matchup_id: int, team_id: int) -> dict[int, float]:
    """The *second*-latest banked value per stat (for the banked-regression check).
    Per-stat, same reasoning as load_latest_state — stats aren't all written every
    tick, so 'previous' is per-stat, not the matchup's prior fetch timestamp."""
    return {sid: v["score"] for sid, v in
            db.latest_category_state(conn, matchup_id, team_id, rank=2).items()}

_FINAL_GAME_STATES = sim.FINAL_GAME_STATES


def _side_remaining(conn, period_id: int, team_id: int, sched: dict,
                    as_of: date | None = None) -> tuple[int, int]:
    """(roster_n, remaining_active_games) for one fantasy side. `roster_n` is the
    fetched roster size (0 ⇒ a real roster-fetch failure); `remaining_active_games`
    counts non-Final games for players in active (non-bench/IL) slots — 0 with a
    fetched roster means the side is done for the week (nothing left to budget),
    which is benign rather than a failure. Used by check_empty_budgets.

    **Must exclude every game `build_budgets` excludes, or the gate it feeds
    mis-fires.** Two filters mirror the sim: `sched` is loaded with `now` by the
    caller (so the past-date guard drops stale non-Final rows — see
    `sim.load_schedule_by_team`), and unplayable players (OUT/INJURY_RESERVE, or
    any status with no return estimate) are skipped via `sim._is_playable` even
    when parked in an *active* slot. Without them this counted games the sim had
    already dropped → `remaining > 0` while budgets were legitimately empty →
    spurious INV_EMPTY_BUDGETS every Sun→Mon (2026-07-26/27: m94/m95/m96, 83
    occurrences, all benign end-of-week drain)."""
    roster = sim.load_team_roster(conn, period_id, team_id)
    rem = 0
    for p in roster:
        if p.get("lineup_slot_id") in sim.NON_COUNTING_SLOTS:   # bench / IL → don't count
            continue
        if not sim._is_playable(p, as_of):                      # OUT/IR → the sim won't budget him
            continue
        rem += sum(1 for g in sched.get(p["pro_team_id"], [])
                   if g.get("game_status") not in _FINAL_GAME_STATES)
    return len(roster), rem


def load_view(conn, matchup_id: int, now: str | None = None) -> dict | None:
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
    # Per-side roster size + remaining *active* games — lets check_empty_budgets
    # tell a real fetch failure (no roster) from the benign end-of-week case
    # (roster present but all active players' games Final → nothing to budget).
    # `now` matters: it turns on the same past-date guard `cli.compute` passes, so
    # the "remaining games" count here matches what build_budgets actually saw.
    sched = sim.load_schedule_by_team(conn, m["matchup_period_id"], now=now)
    as_of = None
    if now:
        try:
            as_of = datetime.fromisoformat(now).date()
        except (ValueError, TypeError):
            as_of = None
    h_roster_n, h_rem = _side_remaining(conn, m["matchup_period_id"], m["home_team_id"], sched, as_of)
    a_roster_n, a_rem = _side_remaining(conn, m["matchup_period_id"], m["away_team_id"], sched, as_of)
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
        "home_roster_n": h_roster_n,
        "away_roster_n": a_roster_n,
        "home_active_remaining": h_rem,
        "away_active_remaining": a_rem,
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
    views = [v for v in (load_view(conn, mid, now=now) for mid in mids) if v]

    findings: list[Finding] = []
    for view in views:
        findings.extend(check_view(view))
    for fn in _LEAGUE_CHECKS:
        findings.extend(fn(views))
    findings.extend(check_pipeline_freshness(conn, now))
    findings.extend(check_live_lineup_capture(conn, now))
    findings.extend(check_published_site(data_json_path, now, conn=conn))
    return findings
