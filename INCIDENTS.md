# Incident log

Post-mortems for data/model incidents, so a future investigator (fresh context)
isn't baffled by anomalies — especially **hand-edited historical data**. Newest first.

---

## 2026-06-04 (evening) — idle-fetch dropped scored cats; WP collapsed toward 50/50

**TL;DR.** When the last live game of the slate went Final (~21:35 UTC), the next
`fetch` took the idle path (`scrape skipped (no games in progress)`). Current-period
`category_state` is split-sourced — the live DOM scrape owns the 10 *scored display
cats*; REST writes only the raw rate components — so the idle fetch wrote **only
components**, at a fresh `fetched_at`. All three readers loaded current state by a
single `MAX(fetched_at)`, so they saw only that components-only tick and **dropped
all 10 scored cats**, making the sim project from ≈zero banked → every WP collapsed
toward 50/50. It self-recovered at 22:00 and was fully fixed at 22:07.

This is a *different mechanism* from the morning incident below (that dropped the
rate **components**; this dropped the scored **display cats** via the read), but the
same family: a partial current-period write that a single-timestamp read mistook for
the complete state.

**Confirmed distinct (verified against the `category_state` write log, not assumed).**
The two have *opposite* signatures for the same matchups (e.g. m57 home):

| window | scored cats (1,5,20,23,48,63,83,18,47,41) | rate components (ER,OUTS,P_H,…) |
|---|---|---|
| 06:00 (pre-bug) | 10/10 ✓ | 10/10 ✓ |
| **morning** 06:16–20:02 | **10/10 ✓** | **0/10 ✗** ← components not written |
| 20:02–21:30 (healthy gap) | 10/10 ✓ | 10/10 ✓ |
| **evening** 21:35–21:55 | **0/10 ✗** ← scored cats dropped by the read | **10/10 ✓** |

So the morning was a *fetch-write* regression (current-period REST writes skipped the
rate components — the "over-broad first cut" noted in CLAUDE.md's "Live data
freshness"), and the evening was a *read* bug (split-source write + single-MAX read).
Opposite halves of the split missing, opposite layers at fault — not the same bug, so
the two entries stay separate.

**Root cause fixed in code** (commit `38b4959`): `load_latest_state` (sim.py) and
`_latest_scores` / `_latest_score_rows` (cli.py) now read the latest value **per
(matchup, team, stat)**, mirroring the `last_good` guard loader, so a partial tick
can't hide earlier stats. Added `INV_CURRENT_CATS_MISSING` (validate.py) — an error
flag if any scored cat is absent once a side has pitched (gated on OUTS, which
survives the drop). The old checks stayed quiet because ER/OUTS were still present
and the move only tripped the soft `ANOM_WP_SWING` warn.

### Hand-edited snapshots

**Period 10 (week of 2026-06-01), matchups 55–60**, the **5 snapshots** at
`computed_at` ∈ {21:35:15, 21:40:14, 21:45:15, 21:50:14, 21:55:14} UTC (2026-06-04;
≈ 23:35–23:55 CEST) had `home_wp`/`away_wp` **linearly interpolated** between the
last good snapshot (`21:30:28`) and the first recovered one (`22:00:15`). 30 rows
total. `details_json` was **not** touched — it still holds the original *collapsed*
computed WP (`home_wins / n_sims` ≈ 50/50 for that window), so column ≠ details_json
there, same caveat as the morning entry. Everywhere outside those 5 ticks the columns
are genuine model output.

---

## 2026-06-04 — corrupted ERA/WHIP projections + manually smoothed WP snapshots

**TL;DR.** A fetch fix shipped earlier in the day accidentally dropped the raw
rate *components* (ER, OUTS, P_H, P_BB, AB) from current-period `category_state`.
For a window that day, ERA/WHIP/OPS projections were computed from
*remaining-innings only* (ignoring the week's banked innings), which corrupted
WP — dramatically once games went live. After fixing the root cause, **we
manually overwrote the WP of the affected historical snapshots to smooth the
graphs.** Those rows' `home_wp`/`away_wp` are therefore *not* model output.

### ⚠️ The thing most likely to confuse a future investigation

For **period 10 (week of 2026-06-01) matchups (matchup_id 55–60)**, snapshots with
`computed_at` in **2026-06-04 ~17:05–20:02 UTC** (≈ 19:05–22:02 CEST) had their
`wp_snapshots.home_wp` / `away_wp` **hand-edited** (cosmetic smoothing). They do
**NOT** match the matchup's `details_json` for that row:
- `details_json.home_wins / n_sims` = the **original (corrupted) computed** WP.
- `home_wp` / `away_wp` columns = the **smoothed/overwritten** values.

So: to see what the model actually computed at the time, use `details_json`. To
see what the graph shows, use the `home_wp` column. They diverge only in that
window. (Everywhere else they agree.)

**⚠️ But `details_json` is not "clean" in the morning either.** The *hand-edited*
window is 17:05–20:02, but the underlying **data-corruption window is broader:
~06:16–20:02 UTC**. From ~06:16 until the 17:05 edit boundary, `home_wp` ==
`details_json` (not hand-edited) — yet both are still **corrupted** for any
rate-cat-driven matchup, because the components were already missing. So for the
morning, `details_json` shows what the model computed *on corrupted inputs*, not
what it should have computed. Treat **all** of 06:16–20:02 as unreliable; the
hand-edit is just a cosmetic layer on the visible tail of a longer corrupt span.

How each matchup was smoothed:
- **m55, m56, m57, m58, m60**: `home_wp`/`away_wp` linearly interpolated between
  the last good snapshot ≤17:05 and the first good (post-fix) snapshot ≥20:02.
- **m59 (Big Giraffes vs Bikini Bottom)**: the pre-window anchor was itself
  suppressed (see below), so instead its window values were shifted up by a
  constant **+30.1pp** from the original corrupted values (the measured
  suppression = the 35%→65% jump at the fix). This leaves a small visible **step
  at the 17:00→17:05 boundary** that we chose not to chase (it would be pure
  fabrication — the pre-window value was also corrupted and the true early-day
  curve depends on banked-innings weighting that was never recorded).

`details_json`, budgets, and `category_state` for those rows were left as-is
(still corrupted). Only the WP columns were touched.

### Root cause of the corruption

Timeline (all UTC):
- **~06:16** — the over-broad REST-clobber fix (`a8205a3`) took effect. To stop
  stale REST from overwriting scraped scores, it skipped **all** current-period
  REST writes once a matchup was seeded. But the DOM scrape only provides the 10
  *display* categories (ERA/WHIP/OPS as **rates**); the raw **components**
  (ER, OUTS, P_H, P_BB, AB) come **only from REST**. So they stopped being
  written and fell out of `category_state`.
- **06:16–~17:10** — corruption present and low-*visibility*, but **NOT necessarily
  low-impact** (corrected 2026-06-04, see "Follow-up" below). The original claim
  here was "low-impact (few banked innings pre-game)" — that conflated "today's
  games haven't started" with "nothing is banked." Overnight (West-coast)
  pitching lines that should have been ingested in this window were dropped too,
  so any matchup whose rate cats hinged on those results was already badly
  suppressed all morning — it just didn't *show* until the afternoon swing.
- **~17:10** — today's games went live; with components gone, `derive_era`/
  `derive_whip` projected from *remaining* innings only, ignoring banked innings.
  This produced nonsense (e.g. a team at 8.37 current ERA *projecting* 3.76) and
  big WP swings as live games moved counting cats but rate cats stayed wrong.
- **~20:02** — root cause fixed (`422af6b`): current-period writes split by
  stat_id — scrape owns display cats, **REST fills the raw components** — plus a
  monotonicity guard. ERA/WHIP projections correctly blend current+remaining again.

Why unit tests missed it: the bug was *emergent* (a fetch change broke what the
sim consumes); each function was individually correct. This is what prompted
`app validate` (output-level invariant/anomaly checks — see CLAUDE.md).

### Follow-up investigation (later 2026-06-04) — worked example: m59 (Big Giraffes vs Bikini Bottom)

A later re-investigation of m59 corrected the "low-impact morning" claim above and
nailed the mechanism end-to-end. Recorded here because it's the clearest concrete
trace of how the bug actually bit, and because the corrupted span is wider than
the smoothing window suggests.

**What the graph shows now (home = Big Giraffes):** ~26% through Wed night and all
Thu morning, then a jump to the 60s% that the hand-edit lands at 17:05, settling
~64–65% post-fix. Reliable anchors: **Wed ~26%** (pre-bug, clean) and **post-20:02
~65%** (post-fix, clean). Everything in 06:16–20:02 is unreliable.

**The real story the bug hid.** The Bums' big pitching damage — **Zac Gallen 4 ER,
Nick Martinez 6 ER** — happened in **Wednesday-night US games (early Thu morning
CET)**, not Thursday evening. Those ~11 ER over ~11.7 IP should have cratered the
Bums' projected ERA and lifted Big Giraffes to ~65% **early Thursday morning**. It
didn't, because the earned runs were never ingested:

| Time (UTC)        | computed WP | proj. away ERA | banked ER | banked OUTS |
|-------------------|------------:|---------------:|----------:|------------:|
| Wed 22:00–Thu 17:00 |   ~26–31% |       ~3.3–3.4 |     **0** |       **5** |
| Thu 18:10 (other games live) | 37% |       3.53 |         0 |           5 |
| Thu 20:00 (last corrupted)   | 35% |       3.54 |         0 |           5 |
| **Thu 21:00 (post-fix)**     | **64%** |   **5.12** |    **11** |      **35** |

Banked ER/OUTS were **frozen at 0 / 5 from Wed night straight to the fix**, then
jumped to 11 / 35 in a **single step** at 20:02 — the signature of a stale value
being *corrected*, not innings accumulating live. (An earlier read of that 5→35
jump as live Thursday-evening pitching was wrong.) The sim derives projected ERA
from these counters (`sim.derive_era` = `ER*27/OUTS`), **never** from the scraped
display ERA — so with ER=0/OUTS=5 it projected the Bums off their ~3.4 ROS rate,
kept ERA/WHIP coin-flips, and pinned WP at ~30% the entire morning. The displayed
ERA was meanwhile ~8.5 (the scrape saw it); the sim just doesn't read that value.

**So the ~26%→65% move (verified via `category_wp` diff) is a real, pitching-driven
swing** concentrated in ERA (+~49pp), WHIP (+~43pp) and QS (+~20pp), partly offset
by Giraffes' own HR/R cooling — **not** lifted suppression in a "fake recovery"
sense. What's *not* real is the *timing and shape*: the rise belonged to early
Thursday morning, the bug suppressed it, and the 22:02 fix + the 19:05-CEST
hand-edit seam jointly mis-placed it.

**Mechanism, exactly.** `a8205a3`'s `continue` skipped the whole `for s in
m["scores"]` REST-write loop for seeded current-period matchups. That loop was the
*only* writer of the raw components (REST `scoreByStat`); the DOM scrape reads only
the 10 scored display cats (ERA/WHIP/OPS as finished rates) and cannot see ER/OUTS.
Result: components frozen. Two-layered timing — ordinary REST lag left ER=0/OUTS=5
through ~06:00 (West-coast line not yet aggregated into `mMatchupScore`), then the
06:16 skip **locked that staleness in** so even once REST carried the real line
later that morning, it was never written. `422af6b` (split writes by stat_id +
monotonicity guard) fixed it; the next compute wrote ER=11/OUTS=35 and WP snapped
to 64%.

### Other same-day data artifacts (not from this bug, but visible in the graphs)

- **Norsemen (team 5) K stuck at 20** in period 10: ESPN's scoreboard intermittently
  drops **Ohtani's pitching line** (two-way player) from the team total, knocking
  his 6 K off (true ≈26). The new monotonicity guard prevents *new* such drops
  but can't un-bank a value already entrenched as the latest; it self-heals once
  K climbs past the entrenched value. We deliberately left this alone.
- **That Bus (team 13) H 19→11** earlier: the *original* stale-REST clobber that
  prompted `a8205a3` (REST hours-stale after a slate, overwriting good scraped H).
  Fixed; restored to 19 manually at the time.

### Everything shipped 2026-06-04 (commits)

- `1508900` SP rotation-cadence projection + per-sim start-count sampling
- `a8205a3` fetch: stop stale REST clobbering scraped scores **(introduced the component-drop regression)**
- `4f7ef7b` refresh-live window widened to 4 days
- `14f0e0a` ESPN public API: probable overlay (#3) + real IL return dates (#1)
- `5edd4e9` / `1fbff4f` handle multi-week All-Star matchup (period 15, 14-day window)
- `ea154ee` cadence restricted to current week only (stale-anchor over-projection of next week)
- `422af6b` fetch: restore REST rate components + monotonicity guard **(root-cause fix)**
- `462d3cf` add invariant + anomaly validation (`app validate`), run every fast tick
- `b0b1a1a` validate `--resolve` + triage runbook
- (plus doc commits: `5fef94d`, `2360c9a`, `d49c5e2`, `d28bea5`, `9f1ef92`)

### Lessons / safeguards now in place

- **Monotonicity guard** on current-period writes (`cli._write_category_score`):
  counting stats can't regress (rejects stale/partial reads from either source).
- **`app validate`** invariant + anomaly checks every fast tick → `validation_flags`;
  the `INV_RATE_COMPONENTS_MISSING` check would have caught this instantly. See
  the "Validation / anomaly flags" runbook in CLAUDE.md.
- When changing `fetch`/plumbing, do a **blast-radius check**: "what does this
  change about what's *in* `category_state`, and what downstream consumes it?"
  (The regression came from verifying the fix's *intent* but not its side effects.)
