# Incident log

Post-mortems for data/model incidents, so a future investigator (fresh context)
isn't baffled by anomalies — especially **hand-edited historical data**. Newest first.

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
- **06:16–~17:10** — corruption present but low-impact (few banked innings pre-game).
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
