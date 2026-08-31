# Incident log

Post-mortems for data/model incidents, so a future investigator (fresh context)
isn't baffled by anomalies — especially **hand-edited historical data**. Newest first.

---

## 2026-08-31 — published site frozen 4 days by a wedged Pages deploy (ops, fixed; no data edit)

**TL;DR.** The live site served **`2026-08-27T16:00:37Z`** data for four days while
`docs/data.json` on disk was current every 5 minutes. **No flag ever fired**, because
every check in the battery reads LOCAL state. No data was lost or edited — the DB and
the local artifact were correct throughout; only the last hop was broken.

**How it looked.** Everything upstream was healthy and said so: `publish` wrote a fresh
`data.json` each tick, `fast.sh` logged `pushed update`, `origin/main == main`,
`ANOM_SITE_STALE` quiet (it watches the local file's `generated_at`, which was seconds
old). The owner noticed the site content was stale; nothing in the pipeline had.

**Root cause.** Workflow run **33091447664** (created `2026-08-27T16:06:01Z`) wedged in
status `waiting` on the `github-pages` environment gate, with `wait_timer: 0`,
`reviewers: []` and `current_user_can_approve: false` — no timer to expire and nobody
able to approve it, so it waited indefinitely. It held `pages.yml`'s
`concurrency: group: "pages"`, so every one of the ~1150 runs behind it collapsed to
`cancelled`. The last success was `16:00:57Z`, which published exactly the payload the
site was still serving — the freeze boundary matches to the second.

**Note the cancellations were the SYMPTOM, not the cause.** This is *not* the
2026-07-02 failure recorded in `pages.yml` (where `cancel-in-progress: true` killed
in-flight deploys during a Pages slowdown). That setting was already `false` and is
still correct. Changing it would not have helped.

**Fix.** Cancelled the wedged run (`POST /actions/runs/33091447664/cancel` → 202); the
group released and the next tick deployed in 18s. A second orphan, run
**30157820421** queued since `2026-07-25T12:20:41Z`, **refused to cancel** (HTTP 500
"Failed to cancel workflow run") and was left in place — it does not appear to hold the
lock, since deploys resumed with it still queued.

**Safeguard now in place.** `app pages-guard` (`app/pages.py`), run every tick from
`fast.sh` after the git step, non-fatal: compares the deployed SHA against the newest
`docs/`-touching commit and cancels `waiting`/`queued` runs older than 30 min —
**never `in_progress`**, which is what stops it recreating the July failure. Raises
`ANOM_DEPLOY_STALE` (warn) when it recovers the wedge and `INV_SITE_NOT_DEPLOYED`
(error) when it cannot — the latter exists precisely because of the uncancellable
orphan above. Shipped in `45136c7e`; details in CLAUDE.md, tests in
`tests/test_pages_guard.py`.

**Lessons.**
- **A "site fresh" check that reads the local artifact does not check the site.**
  `ANOM_SITE_STALE` and `INV_SITE_*` all read `docs/data.json` on disk. The whole
  deploy hop had no watcher, which is the same structural blind spot
  `ANOM_CALIBRATION_JUMP` was added for: change/freshness/invariant detectors over
  our own output cannot see a stable failure downstream of it.
- **A wedge is invisible when the symptom is a cancellation.** ~1150 `cancelled` runs
  look like the concurrency group doing its job. The signal was a single run in
  `waiting`, which no dashboard surfaces by default.
- **At 12 pushes/hour, one wedged run freezes the site indefinitely.** The cadence
  (24,904 runs on the repo) is what makes this expensive; loosening the concurrency
  group is an independent lever, not taken.

---

## 2026-06-09 / 06-11 — settle-bound rate swings (ERA/WHIP, OPS) (model bug, fixed; no data edit)

**TL;DR.** Two matchups dropped sharply on the first tick after a daily ~07:00 UTC
settle, not during the games: **Bear Nation** (m62, 06-09→06-10) ERA/WHIP cratered
(projected ERA 3.76→4.54, WHIP 1.15→1.39); **WAR** (m64, 06-11) OPS fell 0.95→0.81.
In both, the live component reconstruction *had* the data hours earlier but it was
being **rejected by the rate guard**, so the projection ran on the stale REST
baseline until the settle. The `live_recon` telemetry showed `accepted: False` with
the reconstruction actually *closer* to the scrape than the baseline.

**Two compounding root causes.**
1. **Name-match miss → incomplete reconstruction.** `_norm_name` stripped accents but
   not middle initials/suffixes, so "José A. Ferrer" (MLB box) ≠ "Jose Ferrer" (ESPN
   roster). Ferrer's relief line (1 IP, 2 ER) went unmatched, so the reconstructed
   ERA came up short of ESPN's scrape. (Fixed `7769163`: `_norm_name` now drops
   single-letter middle tokens + generational suffixes.)
2. **All-or-nothing guard fell back to a *worse* number.** `_judge_group` committed
   the reconstruction only if it matched the scrape within `LIVE_RATE_TOL`, else kept
   the REST baseline — which was *further* from the scrape than the (imperfect)
   reconstruction. So a single unmatched line sidelined the whole rate group onto a
   ~24h-stale value until the settle. WAR's OPS showed the same shape on the hitting
   side: a lagging AB denominator (66 banked vs 109 real, 32 H) inflated projected
   OPS to 0.95 while the reconstruction (0.886, near the 0.864 scrape) was rejected.
   (Fixed: `_judge_group` now has verdicts **`matched`/`closer`/`baseline`** — commit
   the reconstruction when it matches *or is closer to the scrape than the baseline*.)

**Aggravator.** The cron laptop dark-wake-sleeps, so the overnight tick gap dumped a
whole slate's banked components onto one post-wake tick right at the ~07:00 settle —
which is why the corrections *looked* like one discrete drop. (Telemetry: `live_recon`
carries scraped/reconstructed/baseline + verdict; `pitcher_final_lines` retained
Ferrer's line for the post-hoc attribution.)

---

## 2026-06-10 — in-game SVHD phantom save/hold (model bug, fixed; no data edit)

**TL;DR.** The in-game SVHD model judged "entered a save situation / lead intact"
from the game's **current** run margin every tick, and was routed purely by fantasy
role — three bugs that made an earned hold flicker and gave starters phantom saves.
Surfaced on **Troy Melton** (Scarlet Knights, m63, 2026-06-10): `exp_svhd` projected
**1.0 at 02:30 → 0 at 02:40**, swinging the matchup ~12pp.

**Root cause (three compounding bugs).**
1. `entered_save_situation = _is_save_situation(current_margin)` — a hold earned by
   entering in a 1–3 run save spot and exiting with the lead dropped to 0 the moment
   the team **padded** the lead past 3 (a blowout). Padding a lead never un-earns it.
2. `lead_intact = current_margin > 0` — if a **later** reliever blew the lead, the
   already-exited reliever's hold was wrongly erased (a hold survives the team losing).
3. The override is picked by fantasy role, with no `games_started` check — so an
   RP-classified pitcher making a **spot start** (Melton: `games_started=1`, 5 IP, 4
   ER, 0 SV/HLD) got a phantom save/hold he could never earn. (Surfaced via the new
   `pitcher_final_lines` archive — the line would otherwise have been pruned.)
Root: the model had no memory of each reliever's appearance; it re-derived the
verdict from the current snapshot every tick.

**Fix (commit `6be9cff`).** New `reliever_appearances` table; `refresh-live` persists
each reliever's **entry margin** (first tick seen pitching) and **exit margin** (first
tick seen exited). `_override_rp_svhd` judges the save/hold from those locked
conditions — insurance runs or a later blown lead can't move it — falling back to the
live margin only if the entry tick was missed. Skips entirely when the live line shows
`games_started`. This is the proper fix for the previously-deferred "holds resolve at
Final" limitation. Guarded by `tests/test_ingame_integration.py`.

---

## 2026-06-10 — migration-drift crash (ops, fixed; one missed tick)

**TL;DR.** During the `reliever_appearances` rollout, the editable-install code went
live on the 09:35 cron tick *before* the migration was applied → `refresh-live` died
on "no such table: reliever_appearances", left a stale `.app.lock` (dead PID). The
hardened `acquire_lock` would have stolen it; it was cleared manually and 09:40 ran
clean. Net cost: one skipped 5-min tick. No data corrupted.

**Fix (commit `4b3a4c7`).** The CLI group callback now runs `db.init()` before any
subcommand (idempotent), so code and schema can't drift even for a single tick — new
schema just needs to be in `db.SCHEMA`/the migration list, no separate apply step.
See CLAUDE.md "Schema is ensured before every subcommand".

---

## 2026-06-08 — QS double-count → 100%→0% flip at the settle (model bug, fixed; no data edit)

**TL;DR.** The live QS/SVHD reconstruction **added** the box-score count on top of the
banked baseline. That's safe for the REST-only rate components but **wrong for QS/SVHD**
— they're scored display cats the live DOM scrape banks the instant a game goes Final,
well before the 7h settle boundary. So a Final game that was both scrape-banked *and*
still inside the window was counted twice. On **m60 (That Bus vs Jo Mamas, week 10)**,
deGrom's legit QS was scrape-banked (weekly 2→3) **and** re-added by `_count_qs` → sim
QS **3→4 → That Bus 100%**; it reverted to the official 3 only when `now−7h` crossed
midnight at **07:00** and aged deGrom's game out of the window → **100%→0%** (lost the
4-4 tiebreaker on hits). The "settle revert" was the **window boundary**, not an ESPN
correction. The final settled result (Jo Mamas) was correct; the 100% was the artifact.

**Fix (commit `83ab98b`).** `reconcile_live_components` now uses
`state[QS] = max(scraped_weekly, settled_floor + box_count)` for QS and SVHD (not
additive). `settled_floor` (`sim.load_settled_floor`) is the running **MIN** of the
scraped weekly count over the window-day — observation-driven (no settle-clock
assumption; self-heals a downward correction). The `max` is fail-safe: never below the
authoritative scrape (preserves the in-progress→Final gap-fill), never the
double-count. Guarded by `INV_SITE_QS_OVERCREDIT` (independent recompute) +
`ANOM_WP_RAIL_FLIP` (the near-0↔near-100 UX symptom), and `tests/test_live_components.py`.

---

## 2026-06-06 — host-local `date.today()` lurch (model bug, fixed; no data edit)

**TL;DR.** The hitter lineup optimizer (`_hitter_days_slotted`) floored slotted days
with `date.today()` — the *host machine's local date* (CEST). So at **00:00 CEST
(22:00 UTC)** every night, "today" rolled forward and a whole day's hitter projection
was dropped at once — including US evening games that **hadn't been played yet**
(dated "yesterday" in CEST but first pitch ~01:00 CEST). That produced an artificial,
discrete WP step at ~22:00 UTC, intermittently (only when the just-crossed US date
still had unplayed/in-progress games — observed 2026-06-03/04/05, absent 06-01/02).
No data was corrupted or hand-edited; it's a transient projection artifact.

**Why it looked like a jump, not a smooth handoff:** the day-set was gated by a hard
`day < ret` date comparison, bypassing the smooth `_hitter_factor` (which scales an
in-progress game by innings remaining). So a day went from full (1.0) to gone in one
tick instead of decaying as its games played.

**Fix.** Healthy players now take **no date floor** — past games fall out via game
*status* (`_hitter_factor`: Final → 0), which is timezone-free. The only remaining
"now" (the IL-return heuristic) takes an injected **UTC** `as_of` threaded
`simulate → build_budgets → _hitter_days_slotted / _is_playable`, never
`date.today()`. The sim is now a pure function of `(schedule, as_of)` — identical on
any host timezone. Guarded by `tests/test_hitter_days_tz.py`. Investigating a WP step
at ~22:00 UTC on dates **before** this fix? It may be partly this artifact.

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
`_latest_score_rows` (cli.py) now read the latest value **per
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
