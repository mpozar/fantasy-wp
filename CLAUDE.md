# Claude project notes — fantasy-wp

Context for future Claude sessions working on this repo. The README has the user-facing pitch; this file is the implementer's mental model — design decisions, gotchas, and how to investigate things.

> **This doc is a map, not ground truth — the CODE wins.** Every mechanical
> claim here (what a function does, a slot rule, a threshold, a filter) may have
> drifted since it was written. Before asserting any such claim, open the code
> and cite `file:line`; a statement here is a pointer to *go read*, never the
> basis for an assertion. Distrust absolutes — "hard filter", "always", "never",
> "only", "excluded". This is not aspirational: real misdiagnoses have come from
> trusting a stale line here instead of the code (2026-07-22 "IL slot is a hard
> filter" — wrong). The checkable classes (symbol names, numeric constants, the
> stat-id map) are guarded by `tests/test_docs_consistency.py` /
> `scripts/audit_docs.py`; behavioral prose is on you to verify in code.

## The contract (non-negotiable; everything else in this file is reference)

1. **WP investigation** ("what caused / why did / any flags") → the
   `wp-investigate` skill: run `scripts/wp_diff.py` **before any causal
   claim**; weigh every delta (a leader-flipped category dominates); label
   unconfirmed claims **"hypothesis:"**; log the outcome in `INVESTIGATIONS.md`.
2. **Stat IDs from `app/stats.py`, never memory** (OPS=18, QS=63 — both have
   been mislabeled before, causing real misattributions).
3. **DB is UTC; the owner speaks Europe/Oslo local** ("CET" = CEST in summer).
   Convert explicitly before quoting any time.
4. **Verify mechanics in code, cite `file:line`** — slot **17 = IL, 16 =
   bench**; the hitter optimizer ignores the manager's bench (a BE hitter is
   still slotted). IL slot is **not** a blanket exclude: an IL-slotted player
   with a return estimate is included and gated per-game by his return date
   (`_is_playable`/`_est_return_date`); only genuinely-out statuses are dropped.
5. **The editable install is live**: an `app/` edit runs on the next 5-min
   cron tick. Never leave unwanted working-tree edits sitting. **A multi-step
   edit is the real hazard** — change a signature in one edit and its call site
   in the next, and a tick landing between them dies on a half-applied tree.
   This has now killed a tick **three times** — two NameErrors (on a constant
   and on a local that the half-applied edit hadn't defined yet) and, on
   2026-08-12T10:20Z, "_cadence_extra_start_dist() takes from 3 to 4
   positional arguments but 5 were given" from editing a signature and its
   call site as two separate steps.
   So for any edit that isn't atomic, **hold the lock for the whole edit**, not
   just the test run:
   ```sh
   echo $$ > .app.lock; trap 'rm -f .app.lock' EXIT   # ticks skip; release when consistent
   ```
   Cost of getting it wrong is one lost tick (no snapshot, no publish) and it
   self-heals on the next one — no corruption — but it is pure noise in the log
   and it can mislead the next investigation.
6. **Never leave anything staged**: `fast.sh`'s bare `git commit` sweeps the
   whole index every 5 min. Stage+commit atomically, only after
   `pgrep -fl 'scripts/(fast|medium|daily).sh'` comes back clear.
7. **Never delete wp_snapshots**; hand-edits are marked `edited=1` and logged
   in `INCIDENTS.md`.
8. **Secrets stay in `~/.zshenv`** — read into a variable, never inline.
9. **Bump `?v=N`** in `index.html` on any `docs/` UI change; verify front-end
   in a real browser (there is no node toolchain).
10. **A correlated multi-matchup swing is systemic** even if only warns fired
    — absence of error flags is NOT reassurance (2026-06-04).

## What this is

Win-probability dashboard for one ESPN H2H fantasy baseball league (Quintonia, leagueId=71455, season=2026). Python cron jobs pull data from ESPN + MLB statsapi, store in SQLite, run a Monte Carlo simulation, and publish a static site to GitHub Pages.

Pipeline (read top-to-bottom):
```
ESPN/MLB APIs  ──(refresh-rosters, refresh-schedule, refresh-live, fetch)──▶
   SQLite (data.db)  ──(compute, compute --future)──▶  wp_snapshots  ──(publish)──▶
      docs/data.json  ──▶  docs/index.html  ──(git push)──▶  GitHub Pages
```

## Layout

```
app/
  cli.py        # Click commands: init-db, fetch, refresh-rosters, refresh-schedule,
                # backfill-starts, backfill-lineups, refresh-live,
                # compute [--future], publish
  espn.py       # ESPN fantasy API client (authed v3). fetch_league_shape, fetch_teams,
                # fetch_all_matchups, fetch_rosters_and_projections
  espn_public.py# ESPN public site.api client (no auth). fetch_probables (overlay
                # MLB's), fetch_injuries (real IL return dates). team.id == proTeamId
  espn_scrape.py# Playwright DOM scrape of live matchup cat totals (REST lags)
  mlb.py        # MLB statsapi client + calendar-absolute period windows.
                # matchup_period_window / period_for_date (SEASON_ANCHOR_MONDAY),
                # monday_of, fetch_schedule, parse_boxscore/fetch_boxscore
  sim.py        # Monte Carlo simulator (model mc-v1). build_budgets, simulate,
                # sample_team_totals (per-team week draws for playoff pairings)
  playoffs.py   # Playoff odds (playoffs-v1): season sim over remaining-matchup WPs,
                # ESPN seeding tiebreak chain (record → H2H among tied → coin flip),
                # 6-team bracket from sampled team-weeks. `app playoffs` writes
                # docs/playoffs.json (medium tier); tests/test_playoffs.py
  model.py      # Legacy ratio-v0 model — kept as fallback via `app compute --model ratio-v0`
  db.py         # SQLite schema + migrations
  stats.py      # Stat-id → human-name + display group/order
  calibration.py# Start-of-week projected-vs-settled-actual measurement (counting
                # cats only). ONE implementation, two consumers: the human report
                # (scripts/calibration.py) and the recurring alarm
                # (validate.check_calibration) — deliberately shared so they can't
                # diverge the way INV_SITE_QS_OVERCREDIT did from publish's rule
  teams.py      # ESPN proTeamId ↔ MLBAM team_id map (hardcoded; both APIs are stable)
docs/
  index.html, app.js, style.css   # Static site
  data.json                       # Generated by publish; scoreboard payload (fast first paint)
  history/<period>.json           # Per-week WP history, split out of data.json 2026-07-16
                                  # (was ~98% of a 6 MB payload); app.js hydrates them in
                                  # the background after first render. Rewritten only when
                                  # the week's publish cache-block rebuilds.
  playoffs.json                   # Playoff odds (written by `app playoffs`, medium tier)
scripts/
  fast.sh, medium.sh, daily.sh    # Cron tiers (see "Cron architecture")
  _common.sh                      # Shared: paths, lockfile, read_zshenv_var helper
  analyze_variance.py             # Offline tool: re-measure per-stat VMRs from MLB game logs
  analyze_cadence.py              # Offline tool: re-measure REST_DAY_WEIGHTS (SP rotation gaps) from MLB game logs
  backfill_game_activity.py       # One-time: estimate game_day_activity for past days
data.db                           # SQLite, gitignored
.app.lock                         # Shared lockfile, gitignored
```

## Sim model (mc-v1)

For each matchup, 10,000 sims of how the rest of the week plays out. Each sim:
1. Starts from the current ESPN live matchup state (cumulative cat-by-cat counters).
2. For each rostered player on each fantasy side, draws their remaining production from a Poisson (NB for ER — see "Variance"). Each player has a `Budget` of expected counter values for the week.
3. Compares home vs away on each category and counts wins/losses/ties.
4. Tiebreaker on `hits` if categories are tied (per league setting).

WP = fraction of sims a side wins.

**`SimContext` (added 2026-07-02).** Everything `build_budgets` consumes beyond
the roster + schedule — live pitcher/batter lines, cadence anchors, slot counts,
`use_cadence`, `as_of`, the side's daily-lineup slot map — rides one frozen
dataclass built in exactly one place (`cli.compute`; per-side slot maps come via
`MatchupInputs`). It replaced eight `| None = None` parameters threaded through
four layers, where every default meant "feature silently off" and a forgotten
kwarg silently reverted behavior. Adding a new sim input = add a field with an
off-state default + populate it in `cli.compute`; no signature changes. Field
docs live on the class in `sim.py`.

### Player budgets

Each `Budget` has:
- `role` (SP/RP/HIT)
- `units` — expected weekly contributions: starts for SP, appearances for RP, days-in-lineup for HIT
- `expected[stat_id]` — projected counter values for the week, computed as `(ros_v / denom) * units`
- `extra_dist` / `extra_per_start` (SP only, else `None`) — the stochastic extra-start piece: `extra_dist` is `[P(0 extra starts), P(1), …]` and `extra_per_start[stat_id]` is the per-start rate. `_simulate_team` samples `k ~ extra_dist` per sim and adds `Poisson(extra_per_start·k)` to each pitching counter (NB for ER). See "SP start estimation". For these budgets `expected` holds only the *fixed* piece; `_display_expected` merges in `E[k]·rate` for reporting.

Rate stats (OPS, ERA, WHIP) are *never* sampled directly — they're derived from the underlying counter sums (AB, H, BB, HBP, SF, HR, 2B, 3B for OPS; ER, OUTS for ERA; P_H, P_BB, OUTS for WHIP). See `derive_ops`, `derive_era`, `derive_whip` in `sim.py`. The category decision and the "average per category" in publish use `cat_value` which routes to these derivations.

### Role classification (not by ESPN's `default_position_id`)

**Resolved once per pitcher in `sim._resolve_pitcher_situation` →
`PitcherSituation` (added 2026-07-02).** Role (incl. the spot-starter
promotion below), the benched schedule view, the live box line, and the
exited/live-start state used to be re-derived independently by five
name-matching helpers — most live-credit incidents (Melton, Hunter Brown, the
exited-starter sliver, Phillips) were those derivations disagreeing. They now
come from one struct that `build_budgets` and the in-game overrides branch on;
tests: `tests/test_situation.py`.

ESPN's `default_position_id` is wrong for some players. We classify pitchers by their projected usage:
- `gs/gp > 0.5` → SP path (uses GS as denominator)
- else → RP path (uses GP as denominator)
- **OR: an *announced probable* (for an upcoming/in-progress game) or a *live
  `games_started`* line → SP path regardless of the ratio** (`_is_announced_or_live_starter`).
  This promotes a misclassified rotation SP / spot starter whose ESPN ROS projection
  still has `gs/gp < 0.5` (2026-06-28 Tyler Phillips: GS 3 / GP 30, but starting every
  5–6 days). Without it his start — and its QS — were invisible to the live projection
  (he was modeled as relief). Only a *non-Final* game promotes him (a Final game he
  already started is banked; counting it would strip a swingman's remaining relief).

This catches RP-eligible swingmen (e.g. Wrobleski, pos=11 but usage ≈ SP), and also catches the two-way case below.

**Promoted-starter rate basis.** A promoted pitcher's projected GS is tiny, so
`ros_v / gs_ros` blows up (the 70 K / 1 GS problem: `ros_outs/gs_ros` can be 50).
So for a promoted starter the *cumulative* counters (K/OUTS/ER/…) are scaled by
**per-out rate × a capped start length** (`MAX_START_OUTS`=22, or `TYPICAL_START_OUTS`=17
when GS=0), via an effective `denom = ros_outs / start_outs`. **QS is set separately**
to the per-start rate `ros_qs/gs_ros × units` (or `DEFAULT_QS_RATE` when GS=0) — QS is a
per-start event, not per-out — placed so the in-game `_override_sp_qs` (which drops
`qs_rate × sp_factor` for the in-progress start and adds the live estimate) still composes
without double-counting. Real SPs (ratio-classified) are unchanged. Tests: `test_ingame_integration.py`.

**SVHD follows relief appearances, not the start (fixed 2026-07-03).** `_make_budget`
fills *every* pitcher counter from season rates, and QS gets a dedicated SP override
but SVHD's override is RP-only — so a promoted swingman's season saves/holds used to
smear onto his one start (Tyler Phillips: `ros_svhd/sp_rate_denom × units` = `2.73/6.8`
≈ **0.40 SVHD on a game he's starting**, which can't bank a save/hold). Now the SP branch
**strips SVHD from both the fixed start and the sampled extra starts**, then re-adds only
the SVHD he'd earn from *projected relief appearances* this week via `_sp_relief_svhd`:
`min(ros_svhd/(gp−gs), MAX_SVHD_RATE) × ((gp−gs)/team_ros_games × rp_remaining)` — his
non-start appearance share × remaining relief-eligible team games × saves/holds per relief
appearance. **Auto-scales to 0** as ROS `GS → GP` (a true rotation regular has no relief
appearances left to project); Phillips dropped 0.40 → ~0.14, flagged `relief-svhd`. No-op
for real SPs (≈0 season SVHD). Does not exclude his own start day from the relief-eligible
count — a rate-based expectation, so the ~1-game overlap is negligible (same simplification
the RP branch makes). Tests: `test_ingame_integration.py` (`test_promoted_starter_svhd_*`,
`test_starter_with_no_relief_share_projects_no_svhd`, `_sp_relief_svhd` scaling).

### SP start estimation (rotation cadence + per-sim start-count sampling)

A rostered SP's starts for the week split into two pieces that **never overlap**
(a game with any probable announced is excluded from the cadence piece):

1. **Fixed piece** — announced probable starts (`_probable_starts_for`, weighted
   by `_sp_factor` so an in-progress start gets partial credit) **+ any live
   start**. Near-certain *count*; lands in `Budget.expected` as means and is
   drawn per-stat exactly as every other budget.
2. **Stochastic extra piece** — the un-probabled tail of the week, modeled by the
   **rotation-cadence model** (`_cadence_extra_start_dist`) as a *distribution*
   over integer extra starts `[P(0), P(1), …]`, stored on the budget as
   `extra_dist` + per-start rates `extra_per_start`. `_simulate_team` draws an
   integer `k` from `extra_dist` **once per sim** and scales every pitching
   counter by that shared `k`, so the categories move together (a two-start week
   is all-or-nothing — the bimodal variance the old smeared mean dropped).

**How the cadence dist is built** (`_cadence_extra_start_dist`): anchor on the
later of the pitcher's last recorded start (`pitcher_starts` table, matched by
`_norm_name`) and his latest *announced* probable this period (an arm slated for
Tuesday projects his next turn from Tuesday — and announced games, being the
fixed piece, also retain their probable on Final rows so they anchor the phase
even without `pitcher_starts`). From the anchor, project forward turns by
enumerating rest-day scenarios (`REST_DAY_WEIGHTS`, modal 5), snapping each
projected date to this team's next **open** game (no probable yet), capped at
`MAX_EXTRA_STARTS`. Aggregated over all scenarios into the discrete dist.

**The anchor is resolved ONCE, in `_rotation_anchor`, off the UNFILTERED
schedule — and this is load-bearing (fixed 2026-08-12).** It lives on
`PitcherSituation.cadence_anchor`; `_cadence_extra_start_dist` now *receives* a
resolved anchor rather than deriving one, so there is exactly one definition.
Why unfiltered: a benched pitcher's In-Progress start is dropped from his
*scoring* view by `_drop_inprogress_for_benched` (he's locked out, so it must
score nothing) — but the start still happened and is exactly what sets his
phase. Deriving the anchor from that filtered view left a benched starter
anchored on his **previous** turn for the whole length of his outing, so the
walk projected a phantom extra start — the very turn he was making — which then
vanished at Final. Measured 2026-08-12: **~1 benched starter/day** league-wide
(16 cases in 15 days), each worth ~5-6 phantom K and ~0.45 QS for ~3h; the
Hunter Brown case (m113) moved the matchup **5.8pp**. Note the physical
start-count cap (`_max_remaining_starts`) deliberately keeps its **own**,
*unraised* anchor — the last recorded start — because its window must begin
before the announced start for the `- probable_units` subtraction to be right.
**Two anchors, two jobs; don't unify them.** Tests:
`test_cadence.py::test_rotation_anchor_*`,
`test_budget_flags.py::test_a_benched_starter_gets_no_impossible_turn`.

`E[k] = sum(i·P(i))` is reused for the displayed start count (`Budget.units` =
fixed + E[k]) and the two-way hitter-day subtraction; `_display_expected` folds
`E[k]·rate` back into the per-stat means for the budget summary in `data.json`.

**Scope — turn-awareness only for the CURRENT week.** `compute` passes
`use_cadence=(period_id == current)`. The extra-start count is *always* sampled
(so it varies per sim); only *how the distribution is built* depends on the horizon:
- **current week** → the turn-aware cadence dist.
- **any future week** → the flat ROS-share mean (`rate × open_weight`) split into
  an integer dist by its fractional part (`_split_mean_to_dist`, e.g.
  1.6 → `[0, 0.4, 0.6]`, mean preserved). We drop only the rotation-turn
  *placement*, not the count variance.

Why current-week-only (this was briefly `<= current + 1`): the cadence anchor is
the pitcher's last *recorded* start. For any future week that anchor is already
~a week stale — he'll start again *this* week first, and those turns aren't
recorded yet — so the walk (which only sees the future week's games) snaps his
first turn to day 1 of that week and invents a second, badly over-projecting
(observed Week-11 mean ~1.5 with ~40% of SPs at 1.8–1.9, vs a realistic ~1.3).
Flat tier-B avoids that (Week-11 mean ~1.2). A *proper* way to keep next-week
turn-awareness would be to advance the anchor through the gap (account for the
pitcher's announced/expected current-week starts) — deferred; flat is correct
on average. Far-out weeks were always flat anyway (anchor washes out, and it
keeps the slow `compute --future` path lean).

**Deferred: proper next-week turn-awareness (and why it's harder than it looks).**
Cadence *could* identify next week's genuine two-start pitchers — "current place
in the rotation" is knowable (last start + this week's announced/expected starts).
It doesn't today because of two implementation gaps, plus one irreducible limit:
  - *Gap 1 — stale anchor.* `_cadence_extra_start_dist` anchors on the last
    *recorded* (Final) start in `pitcher_starts`. For next week that's ~a week
    stale; this week's upcoming starts aren't recorded yet, and the announced
    ones aren't seen (see Gap 2).
  - *Gap 2 — per-period isolation.* `compute` runs each period with only *that
    period's* schedule, so the walk can't "consume" the intervening (this-week)
    turns. From a stale anchor it snaps the first projected turn onto day 1 of
    the future week and fits a phantom second → the ~1.9 over-projection.
  - *Fix shape:* anchor on the latest *known* start before the target week
    (recorded starts **plus** this week's announced probables / projected turns),
    and walk a **continuous timeline** (this week + next week), counting only
    starts that land in the target week. Then a real two-turns-in-the-window
    pitcher projects 2, and a one-turn pitcher projects 1.
  - *Irreducible limit:* even done right, next week is genuinely fuzzier than the
    current week — rest-day variance compounds over the 1–2 intervening turns
    (±2–3 days of phase uncertainty, enough to flip whether a 2nd start lands
    inside Mon–Sun), rotations churn over a 10–14 day horizon (skips, rainouts,
    IL, spot 6th starters), and there are no announced probables that far out. So
    the *correct* output is a soft dist like `[0.1, 0.55, 0.35]`, never the bogus
    `[0, 0.11, 0.88]`. Bottom line: a proper version beats flat for next week, but
    only modestly and with wide error bars — which is why flat was the safe
    interim, and why a *broken* cadence (inventing 2nd starts) is worse than flat.

The flag rides `SimContext.use_cadence`, set per period in `cli.compute`.

**Degenerate cases** collapse cleanly: future weeks (no probables) → empty fixed
piece, pure cadence (a full week ≈ `[0, 0.27, 0.73]` → ~1.7 starts); late in a
week (all probables posted) → no open games → `extra_dist = [1.0]`, probables
only. **No-anchor guard:** if there's no recorded start *and* no announced
probable, `_cadence_extra_start_dist` returns `None` and the SP branch uses the
same flat-ROS-share-split fallback as far-future weeks (`_open_sp_game_weight` ×
`min(ros_gs/total_ros, MAX_SP_RATE)`, then `_split_mean_to_dist`) — so the count
still varies; we only lose the turn placement, never regressing below the
pre-cadence model.

**Physical start-count cap (`_max_remaining_starts` / `_cap_extra_dist`,
current-week only).** A backstop against projecting more starts than the rotation
physically allows: a pitcher can't start more often than `MIN_REST_DAYS` (5) apart.
After the fixed + extra pieces are built, the **extra (cadence) piece is clipped**
so that `announced_starts + extra ≤` the min-rest-spaced turns possible from his
last recorded start through the period's last game. **Only the extra is clipped —
announced probable starts are always respected** (a real two-start week like
Jun 23 + Jun 28 survives; physical max over that window is 2). This catches the
transient where the fixed and cadence pieces are computed against in-flux game
statuses around the daily rollover and momentarily sum to the impossible — e.g.
2026-06-26 Rasmussen: a cadence Jun-27 turn colliding with a soon-to-be-announced
Jun-28 start briefly projected 2 starts in 2 days. Gated on `use_cadence` (current
week): for future weeks the anchor is ~a week stale, so the cap would be unreliable
and the flat fallback stays anchor-independent by design. Tests: `test_start_cap.py`.

**Anchor data:** `pitcher_starts` is populated forward by `refresh-schedule`
(every Final game's probable IS its starter) and seeded once by
`app backfill-starts [--days 21]` (the regular fetch window only spans the
current period forward, so first-start-of-week anchors need a lookback). The
rest-day distribution is measured by `scripts/analyze_cadence.py` (pulls MLB
game logs, prints `REST_DAY_WEIGHTS = {...}` to paste into `sim.py`, like
`analyze_variance.py`).

Historical note: before this, `compute` used a *deterministic* hybrid — announced
probables + a flat ROS-share float — collapsed straight into per-stat means. That
got the count's center roughly right but carried **zero start-count variance** and
**decorrelated** the pitching categories, and a flat per-game rate couldn't
distinguish a one- from a two-start week. The cadence model fixes the point
estimate (turn order → two-start weeks) and the sampling fixes the variance.

### Two-way players (Ohtani)

A player with both `gs > 0` (pitcher projections) AND `hit_g > 0` (hitter projections) gets **two budgets**: one pitcher (handled by the pitcher path; gs/gp ratio decides SP vs RP), one hitter (handled by the hitter path). They appear twice in the contributors UI with separate role pills.

Day-conflict resolution (so a two-way isn't counted batting *and* pitching the same day):
- The hitter optimizer skips the player on days they're the **announced** probable starter (`_is_probable_starter_on`).
- That only covers announced days, so we also subtract the **estimated** (un-probabled) start days afterward: `units_h = max(0, units_h - sp_est_units)` where `sp_est_units` is the open-game estimate from the SP hybrid. For a future week that's all their starts; for the current week it's just the un-announced tail.

### Lineup optimization (hitters)

`_hitter_days_slotted` runs a per-day matcher:
1. For each day in the matchup week
2. List rostered hitters whose MLB team plays that day, who aren't IL'd, who aren't pitching that day (two-way), and who have eligible slots
3. Sort by `_hitter_per_game_impact` (R + 0.6·H + 0.3·SB + 0.5·HR per game)
4. **Optimal bipartite matching** (`_max_slot_assignment`, Kuhn's augmenting paths) assigns hitters to slot instances — impact-sorted, so a capacity-bound day seats the highest-impact subset
5. Each hitter who wins a slot gets the **sum of `_hitter_factor` across that day's games** toward their `units` — so a **doubleheader counts as both games** (Final game → 0, in-progress → its remaining fraction, Scheduled → 1.0). This was `max()` (i.e. one game/day) until **2026-07-11**, which silently under-projected doubleheaders: when a postponed game folds into a same-day doubleheader, `max` made a hitter's remaining slate *shrink* by a game instead of stay flat — e.g. MIL@PIT's 7/10 postponement → 7/11 doubleheader dropped Yelich/Turang/Gonzales 6→5 games and abruptly moved a matchup ~12pp. Relievers already sum per-game (`_rp_remaining_units`) and starters are per-game via probables, so the bug was hitter-only. Slot assignment stays per-day (one lineup slot/day); only the credited production sums. Tests: `test_hitter_days_tz.py`.

Step 4 was greedy first-fit until 2026-06-05. Greedy could spend a *flexible*
bat on an early slot and then waste a *scarce* slot only that bat could fill —
e.g. the lone 3B-eligible hitter taken at 2B leaves 3B empty AND benches a
2B-only hitter, dropping a whole hitter (3 games) from the projection and biasing
the matchup WP (observed ~8pp on one matchup). Kuhn's re-routes the flexible bat
(→3B) so both play. It's pure-Python, no new deps; the problem is tiny (~10×10)
and runs once per team in `build_budgets` (outside the per-sim loop), so the cost
is immaterial. Regression: `tests/test_lineup_matching.py`.

**Timezone-independence (no `date.today()`).** Which days a hitter is still slotted
for is driven by game **status** (`_hitter_factor`: Final → 0, in-progress →
innings-left/9, scheduled → 1.0), *not* a wall-clock date floor. Healthy players get
no date floor at all — a played day falls out via its Final status, smoothly. The
only "now" the sim needs (the IL-return heuristic) is an injected **UTC** `as_of`
(`SimContext.as_of`, resolved once at the simulate/build_budgets boundary); the sim
never calls `date.today()`, so it behaves identically on any host timezone. (Until
2026-06-06 it *did* use host-local `date.today()`, which dropped a whole day's
projection at 00:00 CEST — the midnight WP lurch in `INCIDENTS.md`.) Regression:
`tests/test_hitter_days_tz.py`.

### IL handling

`_est_return_date(p, today)` — in priority order:
- **`injury_return_override`** (ESPN's real estimated return date) → used as-is, when present. This is the accurate path (see below).
- `TEN_DAY_IL/DL` → today + 7  *(fallback heuristic)*
- `FIFTEEN_DAY_IL/DL` → today + 10
- `SIXTY_DAY_IL/DL` → today + 30
- `OUT`, `INJURY_RESERVE`, unknown → None (indefinite, excluded entirely)

**Just-activated-off-IL: still IL-slotted today, available tomorrow (fixed 2026-07-09).**
A player in the **IL slot (17) with a *playable* status (`ACTIVE`/etc.)** is a player
just activated off the IL, not a stash. When a manager activates mid-day after games
have started, the league defers it to the next game day, so ESPN leaves him in the IL
slot for *today* and active from tomorrow — and the model only ever sees one
period-level `lineup_slot_id` (today's = IL) and does **not** ingest future-day
lineups. The old rule treated `IL-slot + ACTIVE` as "manager intends out for the
period" and **zeroed him** (Mike Trout, activated 12:10 PT, showed 0 games). Now
`_is_playable` returns True for that case and `_est_return_date` return-dates him to
**tomorrow** — so today's (already-started) game is filtered out but the **rest of the
matchup is projected**. Genuine IL statuses use their return estimate; `OUT`/`INJURY_RESERVE`
in the IL slot are still excluded. Tests: `test_il_activation.py`. NOTE: the WP impact
can still be tiny — the hitter optimizer had already backfilled his slots with
replacements (so he's a marginal upgrade, not from-zero), and if the matchup is being
decided on the *pitching* side a returning bat won't move it (Trout's return moved the
Norsemen ~0.4pp: they were losing K/QS/ERA/WHIP, which a hitter can't touch).
- `ACTIVE`, `NORMAL`, `DAY_TO_DAY`, `QUESTIONABLE`, `PROBABLE`, null → today (playable now)

**Real return dates from ESPN.** `refresh-rosters` pulls ESPN's public injuries feed (`espn_public.fetch_injuries`, excludes Day-To-Day) into the `player_injuries` table; `load_team_roster` attaches each rostered player's `return_date` as `injury_return_override`. That overrides the fixed-days heuristic above with an actual estimated activation date — and also catches IL moves/activations the fantasy `injury_status` hasn't reflected yet (e.g. status `ACTIVE` but ESPN has them out until tomorrow → benched until then). The heuristic remains the fallback when ESPN has no entry. Far-future return dates (e.g. a 60-day IL returning in September) naturally exclude the player from the current week.

The fallback estimate is conservative (counts from today, not from IL placement date which ESPN doesn't expose). Games before the return date are filtered out of `_open_sp_game_weight`, `_probable_starts_for`, `_rp_remaining_units`, and the hitter optimizer.

**IL slot (17) is NOT a hard filter** (corrected 2026-07-22 — the old note here
wrongly claimed it excluded IL-slotted players outright). `_is_playable`
(`sim.py`) includes an IL-slotted player when his status maps to an IL return
estimate (`IL_RETURN_DAYS` — the `*_DAY_IL/DL` statuses) **or** is a playable
status (`ACTIVE`/etc.); only genuinely-out statuses (`OUT`/`INJURY_RESERVE`, or
any status with no return estimate) in the IL slot are excluded. Inclusion is
then **gated per-game by the estimated return date** (`_est_return_date`, which
prefers ESPN's `injury_return_override`): games before it are filtered out, games
on/after it count. So a still-IL-slotted pitcher with a return date of *today*
(e.g. Ranger Suárez 2026-07-22, `FIFTEEN_DAY_DL`, ESPN return 7/22) **does get
projected** for today's+ games — the model bets he'll be activated, since a
player literally cannot score from the IL slot until his manager moves him to an
active slot. **This is deliberate (owner decision 2026-07-22): keep projecting
likely-activated returners, same philosophy as projecting BE-slot pitchers**,
accepting that an IL-stashed player who never gets activated will over-credit his
fantasy team. (It's also what let the doubleheader fix surface Suárez's start and
move WAR ~−7pp — see INVESTIGATIONS.md.) BE slot (16) is included for pitchers
(managers cycle SPs/RPs through bench day-to-day); hitters in BE go through the
optimizer.

### In-progress game scaling

When a game's status is `In Progress`, its *remaining* production scales by role-specific factor (live cumulative state already includes the partial production):

- **Hitters**: linear by innings remaining. `(9 − elapsed) / 9`.
- **SPs**: scale to expected exit inning, derived from the SP's ros_outs/ros_gs. Past their exit, factor = 0.
- **RPs**: bullpen work happens in the back of the game. Factor stays at 1.0 until inning 6, then ramps down.

Game state comes from MLB statsapi via `refresh-live` (calls `mlb.fetch_schedule` with `linescore` hydrated).

**Exited starter → zero remaining counters (fixed 2026-06-28).** The SP factor above
decays with the *game's* innings, not whether the pitcher has actually been pulled, so
an already-departed starter kept a small phantom remaining K/OUTS/ER (the "Sale 0.1-start
sliver": exp_K 0.4 / exp_OUTS 1.1 after a 6 IP / 1 ER / 10 K start that was over). Now,
when his live line shows he's **exited** (`games_started` with `is_last` falsey — a later
pitcher has appeared), `_probable_starts_for` drops that game's factor (it takes
`live_by_team` and matches on `game_pk`), zeroing his remaining counters. His **earned
QS** is unaffected — `_override_sp_qs` still supplies it, and it now skips its `ip_share`
subtraction when exited (the base no longer carries that game's QS share, so there's
nothing to drop). A starter *still pitching* keeps the projected remainder as before.

**Removed hitter → zero remaining counters (fixed 2026-06-29).** The batter analogue:
a hitter pulled mid-game (pinch-hit/defensive sub) can't bat again, but the hitter
factor `(9−elapsed)/9` is purely game-clock based, so he kept a phantom remainder. We
now capture a per-batter **`still_in`** from the boxscore: the active occupant of a
lineup slot is the player with the **highest `battingOrder`** in that slot (`slot =
order // 100`; starter `300`, subs `301/302/…`), so a batter below his slot's max has
been replaced (`mlb.parse_boxscore`; stored on `live_batters.still_in`). The hitter
optimizer (`_hitter_days_slotted`) drops In-Progress games for a hitter whose live line
is `still_in=False` (`_is_removed_from_game`), same as it does for a benched one —
zeroing his remaining H/R/HR/SB. `load_live_batters_inprogress` builds the
team→name→line map (In-Progress only); rides `SimContext.live_batters_by_team`.
Future games still count (he can play tomorrow). No live line ⇒
not removed. Tests: `test_live_components.py` (parse_boxscore still_in), `test_hitter_
days_tz.py` (removed → 0 / still-in → fractional / future game still slotted).

### In-progress QS & SVHD (`app/ingame.py`, wired into `build_budgets`)

The linear scaling above is wrong for **QS** and **SVHD** because they're not
accumulating counters — they're threshold/context outcomes:
- **QS** = ≥18 outs AND ≤3 ER over the *whole* start. Time-scaling ignores the ER
  already allowed (the main driver) and the threshold. A starter at 5 ER has QS
  prob 0; one cruising through 5 should project *up*, not down.
- **SVHD** = a save or hold, which only happens in a save situation (close-and-late
  lead). Time-scaling ignores the score entirely.

`app/ingame.py` implements a state machine for these, keyed on the pitcher's own
status (not game innings elapsed). Decisions made:

- **We compute QS/SVHD ourselves from raw running tallies** (`outs`, `earnedRuns`,
  score, inning). The credited `qualityStarts`/`saves`/`holds`/`blownSaves` fields
  are **only populated at Final**, so they're a Final-time cross-check, never a
  live input.
- **Exit detection** comes from the boxscore `pitchers` order: a pitcher has exited
  once a later pitcher from their team appears (robust to between-innings, unlike
  "current pitcher"). The starter is `pitchers[0]` (`gamesStarted=1`).
- **Banked only at Final.** An earned QS/SVHD lands in the live cumulative totals
  (which the sim starts from) only when the game finalizes and is scraped. So:
  exited-and-earned **while the game is still live → we supply the 1**; once
  **Final → 0** (it's in the totals; adding it would double-count). This replaces
  the current bug where the linear factor hands an already-departed pitcher
  spurious leftover credit.
- **QS while still in**: `P(reach 18 outs) × P(stay ≤3 ER)`, both from a per-out
  **continuation hazard** (`_continuation_prob`) conditioned on the line so far —
  high while cruising, rising hazard with ER and once past the usual workload
  (pitch-limit proxy). ER exposure = expected remaining outs from that same hazard,
  so a met-threshold pitcher still in projects ~0.96 (not 1.0).
- **SVHD while still in**: not a save situation → 0; in one with the lead intact →
  fixed conversion prob; blew the lead → 0. *Not-yet-entered* (the common case for
  a closer mid-game) → season SVHD rate gated by live score/inning
  (`game_script_gate`). Determining "entered a save situation / blew it" needs the
  score *at entry* — the simple version infers it from the current margin; the
  accurate version would track entry score across `refresh-live` ticks.

State tables live in the `project_qs`/`project_svhd` docstrings. Tuning knobs:
`P_CONT_*`, `DEFAULT_SVHD_CONVERSION`, `game_script_gate`.

**How it's wired:** `refresh-live` fetches a per-live-game **boxscore**
(`mlb.fetch_boxscore`) for every In Progress game and stores per-pitcher lines in
the `live_pitchers` table (outs/ER/K + appearance order → exit detection); the
team score is stored on `team_schedule` (`team_runs`/`opponent_runs`). `compute`
loads these via `sim.load_live_pitchers` and passes them to `simulate` →
`build_budgets`, where `_override_sp_qs` / `_override_rp_svhd` replace **only the
in-progress game's** QS/SVHD share with the `ingame.py` projection (matched to
rostered players by `_norm_name`); other games and all other counters stay on the
linear scale. With no live games `live_pitchers` is empty and the overrides are
no-ops — behavior is identical to before. Tested in `tests/test_ingame.py`
(model) and `tests/test_ingame_integration.py` (the build_budgets wiring on mock
in-progress lines); `scripts/ingame_scenarios.py` prints the model over scenarios.

**Save/hold judged from entry/exit margins (fixed 2026-06-10).** The SVHD model
used to read `_is_save_situation` / `lead_intact` from the **current** live margin
every tick, so an exited reliever's earned hold flickered — dropping to 0 the moment
the lead grew past a save (a blowout) or a *later* reliever blew it, and only landing
at Final via `_count_svhd`. Now `refresh-live` persists each reliever's **entry
margin** (first tick seen pitching) and **exit margin** (first tick seen exited) in
`reliever_appearances`, and `_override_rp_svhd` judges the save/hold from those —
locked once he exits (insurance runs or a later blown lead can't move it). Falls back
to the live margin only when the entry tick was missed (no regression). A reliever
who *entered* outside a save situation, or exited trailing, still gets 0. Saves by a
closer pitching the 9th are handled live as before. Still worth a one-time confirm
that ESPN's live totals exclude in-progress QS/SVHD (the "banked at Final"
assumption); if they don't, exited-while-live would double-count.

**RP-classified spot starters skip the SVHD path (fixed 2026-06-10).** The in-game
override is selected by fantasy role, so an RP-classified pitcher making a *spot
start* (`games_started=1` in the live line) used to get a phantom save/hold (the
2026-06-10 Troy Melton case: projected ~1.0 SVHD mid-game for a pitcher who was
actually *starting*, then 0 when the game blew open — wrong at every step).
`_override_rp_svhd` now returns early when the live line shows `games_started`, since
a starter can't earn SV/HLD. (Mirror of the Roki Sasaki QS case — an RP genuinely
starting; there we *keep* the QS, here we *drop* the impossible save/hold.)

**Benched players contribute nothing to a game already underway (fixed 2026-06-28).**
A player **benched at first pitch** is locked out of that game — league rules forbid
moving a player into the lineup once his game has started — so he can't score it, even
though `build_budgets` includes BE-slot pitchers (the streaming hedge: a manager may
activate them *before a future start*). The fix is at the **schedule level, all roles,
all counters** (not just QS/SVHD): for a player `_is_benched_today` (slot in
`NON_COUNTING_SLOTS` per today's `daily_lineups`, via `load_active_slots` — the **same
source** the banked `_count_qs`/`_count_svhd` gate on), `build_budgets` runs his
projection against a schedule with **In-Progress games dropped** (`_drop_inprogress_for_
benched`), and `_hitter_days_slotted` likewise drops In-Progress games from a benched
hitter's slotting. So a benched pitcher's whole started game — QS *and* K/OUTS/ER (and a
reliever's SV/HLD) — and a benched hitter's H/R/HR/SB all zero out at the source. The
in-game QS/SVHD overrides need no gate of their own: they only act on In-Progress games,
which the benched player no longer sees. **Future (Scheduled) games stay** — not yet
locked, so the streaming hedge is intact. Keyed on `NON_COUNTING_SLOTS` (bench/IL), not
"not a pitcher slot", so it's role-agnostic and two-way-safe (a player in *any* active
slot isn't benched). The per-side daily-lineup slot map rides
`MatchupInputs.{home,away}_slot_by_norm_name` → `SimContext.slot_by_norm_name`;
absent map (tests / isolated callers) ⇒ no gating, prior behavior. The bug: 2026-06-28 Hunter Brown, benched all week, threw a 6 IP / 2 ER
QS → projected **+1.0 QS for the Bus** that ESPN won't score (the projection over-credited
vs the banked `_count_qs`, which already excluded him). Tests: `test_ingame_integration.py`
(benched SP exited / still-pitching / future-start-still-projected / benched RP),
`test_hitter_days_tz.py` (benched hitter dropped from In-Progress / future game still
slotted).

Validated live on 2026-06-03 across 13 rostered relievers in all four scripts —
save-spot (+1..3) → ~0.85, big-lead (>3) → 0, tied → 0, trailing → 0 — and the
in-game QS path on two live starters (a cruising 0-ER start projected ~0.82 and
fell as runs scored). One latent quirk remains in `game_script_gate(margin, inning)`,
the gate on the *not-yet-entered* SVHD path (a reliever who has *entered* with no
lead already short-circuits to 0 before the gate, so this only touches the
not-yet-pitched case). **Late** deficits/blowouts are damped — `margin ≤ −4 → 0.1`,
`|margin| ≥ 6 → 0.3` — but `inning < 6` returns **1.0 unconditionally**, so a
rostered reliever in an *early* blowout-loss game he hasn't entered (down 8–0 in the
4th) still gets his full per-game season-rate SVHD share. Bounded (~0.1–0.3 SVHD/game,
innings 1–5 only) and self-healing: the margin gate engages from the 6th and the
entry/exit-margin logic takes over once he pitches. The early branch is *deliberately*
1.0 ("too early to tell" — early scores are noisy and comebacks happen), so the fix
isn't another hard threshold but a softer early-deficit discount (or a continuous
gate); worth measuring real save-situation probability vs. (margin, inning) before
tuning, since the docstring rightly calls this the fuzziest piece.

### Variance / overdispersion

We Poisson-sample most counters, but ER is the one stat with measurable overdispersion in real MLB data. Per-(stat, role) VMRs are empirically measured by `scripts/analyze_variance.py` from ~14k hitter games and ~5.4k pitcher appearances in this season's MLB statsapi game logs. Result table is in the script and the key bits are baked into `sim.py`:

- `ER SP`: VMR ≈ 1.60 (blowup innings)
- `ER RP`: VMR ≈ 1.83
- Everything else: VMR ≈ 1.0 (Poisson is fine — including K, which "feels" volatile but actually isn't)
- AB, QS, SVHD, OUTS for SP: actually *underdispersed* (workhorses are stable)

Sampling by stat (`_simulate_team` in `sim.py`):
- **ER** → Negative Binomial (`_neg_binom`, the one over-dispersed counter).
- **QS, SVHD** (`PER_EVENT_CAPPED`) → **Binomial** (`_binomial_from_mean`): they're bounded at ≤1 per start / per appearance, so Poisson — unbounded and over-dispersed — could return impossible totals (e.g. 2 QS from one start, which spuriously "won" a locked QS category, 2026-06-07). Binomial(⌈mean⌉, mean/⌈mean⌉) preserves the mean, can never exceed the event count, and is under-dispersed (matching the note above). The cadence extra-start piece uses `Binomial(k, rate)` for the same reason.
- **Everything else** (K, H, R, OUTS, AB, …) → Poisson.

Note this caps QS/SVHD *per start*; a pitcher projected for a phantom *extra start* (cadence over-projection, e.g. on the last day of a week) can still carry an extra QS — that's a separate cadence-model limitation, not a sampling one.

If the user complains that a category WP "feels too lopsided," it's almost always projection asymmetry (one team really does project better), not variance — `_decide` in `sim.py` is doing the right math.

### Caps (defensive)

- `MAX_SP_RATE = 0.21` — caps per-team-game SP start rate (5-man rotation ceiling). ESPN's ROS GS projection for aces sometimes implies > 25%/game, which no real rotation produces.
- `MAX_SVHD_RATE = 0.80` — caps per-appearance SV+HLD rate. Realistic elite RPs top out near 0.75-0.80.
- `RP_APPEARANCE_RATE = 0.40` — fallback only, used when ROS GP or team-total games unavailable. Normal path is per-player derived.
- **RP appearances ≤ team games** (`rp-apps-capped` flag) — physical backstop on
  the RP branch; only reachable when `gp_ros` exceeds the denominator, i.e. a
  denominator regression, never healthy inputs.

**ROS-share denominator spans the MLB season, not the fantasy season (fixed
2026-08-10).** Every "share of team games" rate built from ESPN's ROS split —
the RP appearance share `(gp_ros/total_ros) × rp_remaining`, the future-week SP
flat share, `_sp_relief_svhd` — divides an MLB-season-remaining numerator by
`team_total_ros_games`. `compute` used to bound that at `last_reg` (week 22)
while ESPN's ROS GP ran through week 25, inflating every RP's appearances (and
K/SVHD/innings with them) by games(→25)/games(→22): ~×1.25 early season, ×1.76
by week 19, ×4+ by week 22 — Gregory Soto projected 6.5 appearances in a 6-game
week. `sim.load_total_remaining_games` now defaults to unbounded (through the
stored schedule's end, skipping Postponed/Suspended/Cancelled rows since makeups
get their own row) and both `cli` call sites pass no bound. Tests:
`tests/test_total_ros_games.py`.

## Playoff odds (playoffs-v1, added 2026-07-20)

`app/playoffs.py` + the `app playoffs` command. Simulates the rest of the
regular season and the playoff bracket; writes `docs/playoffs.json` (table +
odds-over-time history) and archives each run in `playoff_odds_runs`.

**League structure** (verified against ESPN mSettings + their H2H-Most-Categories
support doc, 2026-07-20): standings = weekly **matchup** W-L record
(`H2H_MOST_CATEGORIES`; hits tiebreak ⇒ no tied weeks). 6 playoff teams, top 2
byes, 1-week rounds, **no reseed**: R1 3v6 + 4v5; semis 1 vs W(4v5), 2 vs W(3v6);
final. Playoff periods = `last_reg+1..last_reg+3` (23–25 in 2026) —
`refresh-schedule` fetches their MLB slates too. Seeding ties: **H2H record among
the tied teams → coin flip**, seating one team then *resetting the chain* for the
rest (ESPN's documented behavior). Two league-specific collapses, revisit if the
league changes shape: single division ⇒ the intradivisional step can't
discriminate (skipped); double round-robin ⇒ the "equal games among tied teams"
validity condition for H2H always holds.

**Two layers:**
1. *Season*: 10k sims; every `UNDECIDED` matchup is a Bernoulli draw from its
   **latest snapshot WP** (current week ⇒ the live WP). Wins + the full H2H grid
   accumulate per sim; seeding runs the exact chain above.
2. *Bracket*: the MC sim has **no cross-team interaction**, so a hypothetical
   pairing = compare two independently sampled team-weeks. Per team per playoff
   period: `sim.sample_team_totals(build_budgets(today's roster, that week's
   schedule, use_cadence=False), 1000)` reduced to 10-cat value tuples
   (`playoffs.totals_to_values`). A round draws one tuple per side →
   `decide_values` (most cats → hits tiebreak → **dead heat advances the higher
   seed**). ROS shares spread over `current..last_reg+3`.

**Live-finale refresh (added 2026-08-08).** The 4-hourly cadence is right for most
of the week — odds are driven by the *remaining* matchups' WPs, which barely move on a
Tuesday. The **last day of a matchup period** is different: six matchups resolve within
a few hours, each flipping a win from probable to banked, so seeds and bye odds can
swing genuinely between two 4-hourly runs and the odds-over-time chart would render the
whole finale as one step. So `fast.sh` also offers a refresh **every tick**, and
`app playoffs --if-live-finale` self-throttles via `cli._finale_skip_reason`:
- **gate 1** — an *In Progress* game whose `game_date` is the period's last day. Keying
  off the game's own date (not the wall clock) is what makes this correct across the UTC
  rollover: Sunday's West-Coast games are still live at 02:00 UTC Monday, and that is
  precisely the window we want. A "is today the last day" test would switch off at
  midnight UTC, mid-finale.
- **gate 2** — `PLAYOFF_LIVE_INTERVAL_MIN` (30) since the last **archived** run, read
  from `playoff_odds_runs` so the throttle survives restarts and can't drift from what
  was actually published.
Costs ~0.4s (CLI startup) on every other tick of the week; runs *before* the git step so
the refreshed `docs/playoffs.json` ships in the same commit; non-fatal like medium.sh's.
Tests: `tests/test_playoffs.py` (`test_finale_refresh_*`, incl. the UTC-rollover case).

**Cron/publish wiring:** medium.sh runs `app playoffs` after `compute --future`
(**non-fatal** — odds are derived; a failure must not kill the roster refresh).
The run is archived **without** history (insert first), then
`playoffs.load_odds_history` rebuilds the full series from the archive and embeds
it in playoffs.json — archive blobs stay per-run sized, no compounding.
fast.sh commits `docs/playoffs.json` alongside data.json/history. Front-end:
`renderPlayoffs`/`renderPoChart` in app.js — table with seed-probability columns,
odds-over-time chart with a Playoffs/Bye/Champion **metric** toggle and a
**Range** toggle (Full / Past 7 days / Past 24 hours, `PO_RANGES` +
`poHistoryInRange`, added 2026-08-09). Both are the same `scope-btn` segmented
control the WP chart uses. The range window is measured back from the series'
**last point, not `now`** — the archive only gains a point per `app playoffs` run
(4-hourly, or every 30 min during a period's live finale), so a wall-clock window
would render empty whenever the pipeline has been quiet; it also falls back to the
full series if the window would leave <2 points (a 1-point line draws nothing and
reads as a bug). Range, metric and pinning compose independently. Top 6 teams by payload
order get the 6-hue palette (`PO_COLORS`), rest muted gray with hover + table-chip
identity. Tests: `tests/test_playoffs.py` (tiebreak chain incl. the 3-way reset,
dead-heat rule, probability-conservation invariants, history loader).

**Gotchas & how to investigate "why are X's odds low/high":**
- **Record ≠ roster.** Seeding runs on record; the bracket runs on *projected
  rosters*. A team can be seed-1 favorite and a bracket underdog. Worked example
  (2026-07-20): WAR 13-2 (best cat record .629) but P(champ) ~11% vs the 12-3
  Norsemen's ~34% — WAR projects elite H (77% vs Po9) but underdog in 6/10 cats
  (K 10%: 54 vs 72 projected; SVHD 3%: 3.8 vs 6.7 — four modest RPs vs six
  high-leverage arms). Season per-cat records agreed (K 7-8, ERA 5-10): their
  13-2 was built on batting cats + SVHD. Method: per-cat pairwise win rates from
  `sample_team_totals` + per-cat season records from `db.latest_category_state`
  over decided matchups — **never aggregate raw `category_state` rows; the
  tick-weighted sum is garbage** (same per-stat-read rule as everywhere else).
- **The log's "Title favorite" is the champion-odds leader**, chosen via
  `max(p_champion)` — NOT payload row 1, which sorts by p_playoffs first and
  flips on single-sim wobble near 100% (a 0.9999 vs 1.0000 artifact, seen
  2026-07-20).
- **MC noise:** ±~0.5pp on mid-range odds at 10k sims; don't read tick-to-tick
  history-chart jitter as signal.
- **Known blind spots** (disclosed in the UI footer): today's rosters + ROS
  projections for September (no trades/call-ups/streaming — a pitching-light
  contender WILL look worse than its September self); weeks independent ⇒
  extremes read overconfident; LM can override seeding by hand.

## Matchup periods & the Monday rollover

Matchup periods are weekly (Mon→Sun) **except** the All-Star break, which ESPN
keeps as one 2-week `matchupPeriodId`. Their date windows are **calendar-absolute**,
anchored on `SEASON_ANCHOR_MONDAY` in `mlb.py`. `matchup_period_window(period)`
and `period_for_date(date)` are exact inverses and depend on nothing but the
calendar — not on "today", not on ESPN.

### Multi-week matchups (the All-Star break)

`LONG_MATCHUPS = {period_id: num_weeks}` in `mlb.py` lists periods that span more
than one week (2026: `{15: 2}` = July 6–19). The window calc sums one week per
earlier period **plus** the extra weeks any earlier long matchup adds, so every
period after the break is pushed one week later and the anchor stays exact for the
whole back half of the season. `period_for_date` walks the same per-period spans
to invert it, so both weeks of the break attribute to matchup 15 (not leaking into
16). `tests/test_calendar.py` locks period 9 = May 25–31, period 15 = July 6–19,
period 16 = July 20–26, and full round-trip consistency.

**Why a hand-maintained constant, not read from ESPN:** ESPN's
`scheduleSettings.matchupPeriods` map is identity here (matchupPeriodLength=1) and
carries no length/date info. The only field that reveals a matchup's true daily
span is each side's `pointsByScoringPeriod` (daily scoring-period IDs, 1…187) —
but ESPN populates it **only up to the latest played scoring period**. The break is
in the future, so ESPN won't expose its 2-week span until we reach it, yet
`compute --future` projects it *now*. So it's set once per season alongside
`SEASON_ANCHOR_MONDAY`. To validate after the fact, the daily-SPID dates of any
*played* period give ground truth (SPID 62 = Mon 2026-05-25, then linear).

**Why this matters (the bug it fixes):** the sim is entirely schedule-driven —
`load_schedule_by_team` filters team_schedule by `matchup_period_id` only (no date
cap) and the hitter optimizer iterates the actual game dates — so a correct 14-day
window flows through to budgets/units/cadence automatically. Before the fix, a flat
7-day stride gave matchup 15 only its first week of games (WP computed on half the
schedule, collapsing toward 100/0 in week 2 while ESPN's live cat totals kept
climbing), and shifted **every** post-break matchup one calendar week early (each
simulated against the wrong week's games), with the true final week never
simulated at all.

**Do not trust ESPN's `status.currentMatchupPeriod` for date math.** It lags the
calendar by several hours around the Monday rollover (it flips near US midnight =
early-to-mid Monday *morning* in Oslo). The old code anchored period windows
relative to *today* + ESPN's current period; when those two disagreed on Monday
morning, every period shifted forward a week — the just-finished matchup absorbed
the new week's games and got re-simulated instead of resolving to 100/0. The
absolute anchor removes that entire class of bug.

Consequences:
- `refresh-schedule` writes each period's games via `matchup_period_window`.
- `refresh-live` attributes each game to `period_for_date(game_date)` — by the
  game's own date, never the current period — so a new week's games can't leak
  into the prior matchup.
- The UI's "current week" is **data-driven**, not from ESPN's number — see
  "Front-end behavior".
- `SEASON_ANCHOR_MONDAY` is a dated constant: **update it once per season**
  (verify against a known period→week mapping, e.g. period 9 = May 25–31 in 2026).
- `LONG_MATCHUPS` (the All-Star break span): **also update once per season** — see
  "Multi-week matchups" above.

## ESPN API quirks (the gotchas)

### `stat_id 83` = SVHD in actuals AND projections (UI-displayed value)

But its meaning differs across sources:
- **Full-season projection** (split=0, src=1): stat 83 = the league's SVHD scoring value. **Trustworthy** — matches ESPN's web UI.
- **ROS projection** (split=6, src=1): stat 83 is **broken** for some players (sometimes equals GP). DO NOT use directly.
- **Actuals** (split=0, src=0): stat 83 = the league's SVHD scoring value. **Trustworthy.**
  But **absent means zero** — ESPN omits the key entirely rather than sending 0
  (83 of 133 rostered pitchers with appearances, 2026-08-10; an explicit `0`
  never appears). Read a missing 83 as "no saves/holds", never "unknown", or a
  save-less arm inherits whatever the fallback is. Stat **63 (QS) is NOT encoded
  this way** — it is always present for a pitcher with starts.
- `stat_id 56` in actuals = raw `SV + HLD` sum. Prefer **stat 83** in actuals (it's
  the league's scored SVHD counter and what `espn.fetch_rosters_and_projections`
  uses). *(An earlier version of this note claimed 83 "subtracts blown saves" — that
  was an unverified guess, probably from the broken split=6 ROS values; this league
  scores SVHD = SV + HLD, no blown-save penalty. The live SVHD reconstruction uses
  SV + HLD accordingly.)*

Our SVHD derivation (in `espn.fetch_rosters_and_projections`): at fetch time the
broken split=6 stat 83 is overwritten with `rate × ros_gp`, where `rate` is an
**empirical-Bayes shrinkage** (`espn.blend_svhd_rate` / `apply_svhd_rate_blend`)
of ESPN's full-season projected rate toward the pitcher's season-to-date actual:

    rate = (act_svhd + K·prior) / (act_gp + K),   prior = proj_s83 / proj_gp
    SVHD_RATE_PRIOR_APPEARANCES = 8.0

Same shape as the QS blend below, with two structural differences worth keeping
straight:
- **The prior is the FULL-SEASON projection, not the ROS one.** ESPN's ROS
  encoding of 83 is broken (above), so it cannot be the prior the way
  `ros_qs / ros_gs` is for QS. The full-season projection is well-formed, and it
  is what the old cliff already fell back to.
- **`K` is calibrated against the prior's per-player error**, not against
  between-player spread: `K = p(1−p) / E[(prior − true)²]` ⇒ 7.6, vs ~14 from
  the between-player-spread estimator (which `analyze_qs_rate.py` used as its
  headline until 2026-08-10 — see the QS section). The two agree only when
  the prior is the population mean; ESPN's is a *preseason* forecast that misses
  mid-season role changes outright (2026-08-10: 5 of 47 rostered relievers had a
  **.000** projected rate against realized rates up to .571). Re-measure with
  `scripts/analyze_svhd_rate.py`; the error curve is flat from K=4 to K=10.

**This replaced a hard 15-appearance cliff** (`MIN_ACT_GP_FOR_SVHD_RATE`, removed
2026-08-10): below 15 GP the rate was 100% ESPN's projection, at 15 GP it flipped
to 100% actuals, so one outing could move a reliever's rate 0.3. The shrinkage
beats it by **38% squared error over n=1..60 and 46% over n=1..25**, the gain
concentrated below the old threshold. Effect on the live model was small — league
projected SVHD **−2.2%** for the current week, max WP move ~3pp — because by
mid-August almost every rostered reliever was already past the cliff; the win is
early-season and at the boundary. It is a genuine blend, not a haircut: Bryan
Abreu went *up* (.311 realized, .620 prior ⇒ .358) while Bryan Baker came down
(.750 realized, .294 prior ⇒ .685). Tests: `tests/test_svhd_rate_blend.py`.

`sim.MAX_SVHD_RATE` (0.80) still caps the rate the sim recovers, but it is a
backstop against the *broken* ROS encoding — no blended rate this season comes
near it (league max ≈ .69).

### `stat_id 63` = QS — ROS rate blended toward actuals (added 2026-08-10)

Unlike SVHD, QS has no *encoding* bug — ESPN's ROS QS is a well-formed number
that is simply **biased high in level**, because ROS projections are anchored to
preseason talent and don't track current performance. Measured over the league's
89 rostered starters (1714 actual starts): ESPN-implied **.596 vs actual .467,
i.e. +27.5%** — the level bias behind the +40.5% start-of-week QS
over-projection. (First reported as ".598 vs .438, +36.7%" over 65 starters;
that came from the stale-roster sample described under "`K` is measured" below,
which overstated the level bias as well as mis-estimating K.)

So `espn.fetch_rosters_and_projections` rewrites ROS QS at fetch time via
`espn.blend_qs_rate` / `apply_qs_rate_blend`, an **empirical-Bayes shrinkage
toward each pitcher's season-to-date actual rate**:

    rate = (act_qs + K·prior) / (act_gs + K),   prior = ros_qs / ros_gs
    QS_RATE_PRIOR_STARTS = 7.0

`K` is the prior weight in *pseudo-starts*, so the blend is sample-size aware by
construction: ~21 actual starts (the league median) ⇒ ~75% weight on actuals, a
2-start callup stays essentially at ESPN's number. The SVHD path above uses the
same shape and the same calibration (it previously had a hard 15-GP cliff that
flipped discontinuously).

**`K` is measured, not chosen — and it was re-measured on 2026-08-10 after two
defects surfaced in `scripts/analyze_qs_rate.py`, giving 9.0 → 7.0.** The
estimator is the same as SVHD's: calibrate against how wrong the *prior* is per
pitcher, `K = p(1−p) / E[(prior − true)²]` ⇒ **8.6**, then validate directly with
a squared-error back-test, which puts the optimum at **7.0** — invariant across
uniform *n*, today's per-pitcher start counts, and ROS-start weighting. 7.0
takes the back-test (it measures the objective instead of approximating it), and
selection makes both numbers *upper* bounds: a rostered starter's realized rate
is luck-inflated above his true talent while ESPN's prior sits above the realized
rate, so the true prior error is larger than measured and the true K smaller.
The error curve is **flat K=6..10** (within 2.4% of optimum), so the exact value
is not load-bearing — but stay inside that band, which is what
`test_prior_weight_constant_sits_in_the_measured_flat_basin` guards.

The two defects, both real, and they **nearly cancelled** — which is why the
constant barely moved and why neither should be re-introduced:
- **A stale roster sample.** The script fetched with `{"scoringPeriodId": 0}`.
  That parameter does *not* filter stats; it returns a **different roster
  snapshot** — 108 of its 279 players are not on any current roster, and it
  misses 41 of the 90 currently-rostered usable starters (deGrom, Wheeler, Cole,
  Snell, …). For a player present in *both* responses the split=6 ROS block is
  identical, so this was never "ESPN omits the ROS block" (the original
  diagnosis, since corrected): the plain fetch matches the league's stored
  `team_rosters` 283/283, `scoringPeriodId=0` only 206/279. Fixing it pushes K
  **up** (spread estimator 9.0 → 15.5).
- **The wrong estimator.** Between-player-spread MoM
  (`K = p(1−p)/var_between − 1`) is only correct when the prior *is* the
  population mean; ESPN's is a per-player forecast that can be individually
  wrong. Switching to the prior-error form pushes K back **down** (18.3 → 8.6 on
  the correct sample). Both scripts now print the pair, so the disagreement is
  visible rather than assumed away.

Also fixed: `--min-starts` now defaults to 3 (was 1), matching
`analyze_svhd_rate.py`. A 1-start pitcher contributes a maximal squared
deviation with a *zero* binomial-noise correction, so he biases K downward with
no information — 7.2 at min 1 vs 8.6 at min 3.

**Written back as a TOTAL** (`rate × ros_gs`), not a rate, so the stored shape
matches every other ROS counter and `sim` needs no change — it recovers the rate
as `ros_qs / gs_ros` (`_make_budget`'s per-start denominator and
`_override_sp_qs`'s `qs_rate`). No-op for pure relievers (no ROS GS — their
spot-start QS goes through the promoted-starter path) and when actuals are
missing. Tests: `tests/test_qs_rate_blend.py`.

**Effect (2026-08-10):** introducing the blend dropped league projected QS
**−14%** in every remaining week at K=9 (wk19 54.4→46.9, wk22 63.4→54.2), and it
is a genuine blend, not a haircut — Logan Webb went *up* (.636→.658, actual .667)
while Peralta fell .600→.294 (actual .174). WP moved ≤2.7pp, for the usual reason
(a symmetric bias mostly cancels head-to-head). The **K=9→7 re-measure later the
same day** is a second-order tweak on top of that: A/B'd end-to-end on a sandbox
copy (single ESPN payload, both arms, paired RNG) it moved wk19 projected QS
46.9→46.3 (**−1.3%**, so ≈−15% against no blend at all) and **max WP 0.2pp**
across all six matchups — as expected, since K=9 was already only +1.2% off the
back-tested optimum. **Expected residual QS bias ≈ +20%, not ~+12%** — the
+40.5% total was rate bias × *start-count* bias, and only the rate half is
fixed. The remainder is projected starts, the same suspected root as K's flat
+18%.

### `split_id` semantics

ESPN's `statSplitTypeId`:
- `0` = full season
- `1, 2, 3` = last 7/15/30 days
- `5` = per scoring period (with `scoringPeriodId` for the specific day)
- `6` = rest-of-season

`statSourceId`:
- `0` = actuals
- `1` = projection

`ROS_SPLIT_ID = 6` is hardcoded in `sim.py`.

### Other projection encoding caveats (uninvestigated)

ESPN's ROS projections often disagree with current season-to-date rates — e.g. Skenes' QS rate (actual 45% vs projection 86%). These aren't encoding bugs — they're ESPN's model being anchored to preseason "true talent" rather than current performance. We trust the projection for these. Only SVHD had a real encoding error.

**Measured 2026-08-10 (`scripts/calibration.py`) — the QS half of that claim
holds, the HR half does NOT.** Over periods 10-18, start-of-week **QS is
over-projected +40.5%** (90% CI [+32.7, +48.2]), consistent with the inflated
ROS QS rate above. **Fixed 2026-08-10 for QS** — see the QS-rate blend below;
we no longer "trust the projection" for this one stat. But the old note here also claimed "most hitters' HR rates
(projection ~2× actual)" — as it flows through to *scored totals* that is
wrong and has been removed: the unit-free ratio test puts the HR **rate**
slightly *under*-projected (HR/H −5.1% vs actual), and HR's small +2.5% total
bias is the shared hitter-units over-projection partly cancelling it. Don't
re-add an HR-rate correction on the strength of the old sentence.

### Probable pitchers — name-matching

`_probable_starts_for` matches by normalized name (`_norm_name`: strip diacritics → lowercase → alphanumeric only → drop generational suffixes and single-letter middle-initial tokens; see line 552). It lives in `app/names.py` (`norm_name`) and is imported by both `sim.py` and `espn_public.py` so the write-key and read-key for name matching can never diverge. Diacritic stripping matters — MLB's probable feed accents names ("Cristopher Sánchez") while ESPN's rosters often don't ("Sanchez"); without normalization they miss and the SP loses credit for a confirmed start (and with the hybrid estimate, the announced game is *also* excluded from the open-game weight, so the start vanishes entirely). Remaining mismatch risk is genuinely different spellings (nicknames, punctuation) — rare. The reverse, two names colliding after normalization, is also possible but rare.

When a probable pitcher gets announced for an upcoming game (typically by MLB ~24h before), that game flips from the estimated open-game share to a confirmed start. With the hybrid SP estimate the swing is now modest (the start was already partly credited via the ROS estimate) rather than the old 0→1 jump — that confirmed-start credit replaces the estimate it had displaced. This is normal behavior.

**Source lag — ESPN leads MLB statsapi, so we overlay it.** Primary probable source is MLB statsapi (`mlb.fetch_schedule`), but ESPN's feed surfaces expected probables a day or two earlier. So `fetch_schedule`'s results get a **fill-only overlay** from ESPN's public API (`espn_public.fetch_probables` → `_overlay_espn_probables` in cli.py): for any game where MLB has *no* probable yet, we fill in ESPN's. MLB always wins once it posts (we never overwrite an existing probable), so ESPN is just an early stand-in for the un-announced tail; the cadence model only kicks in for games neither source has named. Wired into `refresh-schedule` (current period only — ESPN has nothing useful for future weeks) and `refresh-live` (its window, every tick — since 2026-08-18 that reaches the
end of the current period, so the back half of the week's probables refresh every
5 min instead of only at the 04:02Z daily run). **ESPN's probable horizon is ~5
days and hard** — measured 2026-08-18, games 5 days out had 29/30 probables and 6
days out had **0/30** — so widening buys *latency, not reach*: a probable crossing
the horizon mid-day now lands within a tick, but nothing surfaces one ESPN has not
posted. That distinction is what made the 2026-08-18 Bear Nation +4.1pp look like
a run-timing problem when it was really the horizon advancing overnight. Observed 2026-06-03: ESPN had Roupp (Sat) and Gausman (Sun) while MLB's feed still showed those games open; the overlay now fills them so both project a confirmed 1.0 start instead of a ~0.88/0.34 cadence estimate. **Caveat:** ESPN's early probables are tentative and can change — but since we only fill where MLB is blank and MLB overrides on post, the blast radius is just the few-day tail.

**Min-rest conflict guard (added 2026-08-24).** The blast radius above was understated: **the two feeds can disagree about which DAY a starter goes, and fill-only merging cannot see the conflict.** Caught live on two teams in one tick — MLB had Misiorowski and Sale on 08-27, while ESPN's rotation slotted an extra arm there and pushed both to 08-28. MLB had not named 08-28, so the overlay filled ESPN's guess and the same pitcher was probable on **consecutive days**. Nothing downstream catches that: `_cap_extra_dist` clips only the *cadence* piece and **always respects announced starts**, so both were credited and those teams projected **2.00 starts where 1 was possible**. `_overlay_espn_probables` now skips a fill when that pitcher is already probable for the same team within `sim.MIN_REST_DAYS`; MLB's row always wins (only the *fill* is skipped), accepted fills join the claim set so ESPN cannot duplicate inside its own tail, and leaving the day open is correct — the cadence model then projects a real candidate. A legitimate two-start week (exactly `MIN_REST_DAYS` apart, e.g. 08-18 + 08-23) is untouched. **This surfaced within a week of widening `refresh-live`'s window** (2026-08-18), which took overlay fills from ~27 to ~97 per tick — far more of ESPN's speculative tail, which is where the disagreements live. Detected by `INV_SP_STARTS_IMPOSSIBLE`, on its first day in production. Tests: `test_overlay_probables.py::test_overlay_*min_rest*` and neighbours.

**Doubleheader guard (fixed 2026-07-22).** `fetch_probables` is keyed by
`(game_date, pro_team_id)` — one probable per team per day — so on a
doubleheader date it can't distinguish the team's two games. A blind fill
**smeared** ESPN's single name across BOTH games, which (a) masked the
still-open game as non-open — so the cadence/open-game logic couldn't project
anyone for it — and (b) could stamp a guessed pitcher on the game MLB starts
with someone else. So `_overlay_espn_probables` now **skips any `(date, team)`
that has more than one game** and leaves doubleheaders to MLB: the started game
gets MLB's real per-game probable, the open game stays open until MLB names it.
Single-game days are unaffected. (2026-07-22 Red Sox DH: ESPN "Jake Bennett"
smeared onto both games; game 2 — a likely Suárez spot start — was invisible as
open until this fix.) Note the `mlbam_id` on a probable is NOT a
real-vs-guessed signal: MLB-sourced probables carry it, ESPN-overlay ones are
name-only (`mlbam_id` NULL) — ~44% of stored probables. Tests:
`test_overlay_probables.py`.

## Cron architecture

Three tiers, all hold a shared `.app.lock` (in `_common.sh`):

| Tier | Cadence | What | Lock behavior |
|---|---|---|---|
| `fast.sh` | every 5 min | `refresh-live` + `fetch` + `compute` (current week) + `playoffs --if-live-finale` (self-throttling no-op except on a period's last day) + `publish` + git push + `pages-guard` (deploy watchdog, non-fatal) | Skip if lock held |
| `medium.sh` | every 4h | `refresh-rosters` + `compute --future` + `playoffs` (non-fatal) | Wait for lock |
| `daily.sh` | once/day | `refresh-schedule` (all remaining weeks **+ 3 playoff periods**) + `publish --rebuild` (no push) + `validate --calibration` (non-fatal) | Wait for lock |

The fast tier is the only one that **pushes**. `daily.sh` also runs `publish --rebuild` (forces a full per-week block-cache rebuild + picks up rare late corrections to already-settled weeks), but doesn't push — the next fast tick does. `medium.sh` just updates the DB.

### Schema is ensured before every subcommand (editable-install drift guard)

The CLI group callback (`cli()` in `app/cli.py`) calls `db.init()` before *any*
subcommand runs — idempotent `CREATE TABLE IF NOT EXISTS` + guarded `ALTER`s, a few
ms. This exists because of the **editable install** (`.venv/bin/app` runs the
working-tree source, so an `app/` edit goes live on the *next* cron tick
immediately) while migrations historically ran only on demand: a schema-touching
change could land code that referenced a table/column the DB didn't have yet, and
crash the tick. That bit the 2026-06-10 `reliever_appearances` rollout (one
`refresh-live` tick died on "no such table" before the migration was applied). With
the group-level `db.init()`, code and schema can't drift even for a single tick — so
**new tables/columns just need to be in `db.SCHEMA` / the migration list; no separate
"apply the migration" step is required** before the edit goes live. (Don't remove the
callback init; `tests/test_telemetry.py::test_cli_group_ensures_schema_before_subcommand`
guards it.)

### Why some commands are slow

- `compute --future` recomputes 78 matchups (13 weeks × 6) × 10k sims each. Takes ~3-5 min.
- `refresh-rosters` pulls 12 teams × ~25 players × ROS projections. ~10s.
- `refresh-schedule` (all weeks) hits MLB statsapi 14 times. ~5-10s.
- `refresh-live` hits MLB statsapi once for the schedule (window = yesterday
  through the current period's end, `cli._live_window`; widened from a flat
  today+2 on 2026-08-18) plus one boxscore per *unsettled* game (in-progress +
  recently-Final today, ~10-30 during a slate) and one ESPN `mRoster` call **per
  game-day that actually has such a game** (1-2, each addressed by its own
  `scoringPeriodId`) for the daily lineups. That last count is driven by
  `fetch_pks` — days holding only *Scheduled* games are excluded — **not** by the
  width of the window. That is why widening cost no extra ESPN or boxscore
  calls, just a wider date range on the one statsapi request. ~1s idle,
  ~2-4s during a live slate — still well under the 5-min budget. All best-effort
  (failures fall back silently; live component reconstruction degrades to ESPN's
  stale-but-safe components).

### git push from cron

Cron can't reach the macOS keychain where `gh auth login` stores the GitHub token. `fast.sh` reads `GH_TOKEN` from `~/.zshenv` (added once with `echo "export GH_TOKEN=$(gh auth token)" >> ~/.zshenv`) and uses it as a one-shot git credential helper for the push. Same `~/.zshenv` pattern used for `ESPN_SWID`, `ESPN_S2`, `GITLAB_TOKEN`.

If the token rotates (`gh auth refresh`), update the line in `~/.zshenv`.

### Manual cron-equivalent commands

```sh
# Pick up roster moves + recompute future weeks
.venv/bin/app refresh-rosters && .venv/bin/app compute --future
# Then publish to push immediately (otherwise next fast.sh tick will)
.venv/bin/app publish && cd /Users/mpozar/git/fantasy-wp && git add docs/data.json && git commit -m "manual" && git push
```

Acquire the lock first when running anything that touches the DB:
```sh
echo $$ > .app.lock
trap 'rm -f .app.lock' EXIT
```

## Live data freshness — DOM scraping with Playwright

> ### The whole picture (read this first)
>
> We want a near-real-time win probability for live H2H matchups, but **no single
> ESPN source gives fresh + complete + projectable data**, so the live-data layer is
> a stack of compensations — each closes one gap, and each has a past incident that
> motivated it. This is the map; the subsections below are the detail.
>
> **Three sources, three different lags:**
>
> | Source | Gives | Freshness |
> |---|---|---|
> | **DOM scrape** (`espn_scrape.py`) | the 10 **scored display cats** per team (incl. the *displayed* ERA/WHIP/OPS values) | ~live, but **only while games are in progress** |
> | **ESPN REST** (`mMatchupScore`) | raw **rate components** (ER, OUTS, P_H, P_BB; AB, 2B, BB…) the scoreboard never shows | **settles ~once a day (~07:00 UTC)** |
> | **MLB box scores** (`statsapi`) | per-pitcher/batter lines = the components, live | live, but matched to rosters **by name** |
>
> **Why rate cats are the hard case.** Counting cats (H/HR/R/SB/K) are scored values
> the scrape reads and owns live. But **ERA/WHIP/OPS are *projected* from components**
> (numerator + denominator) — and the scrape only sees the *displayed rate*, which
> can't be projected forward (5.40 ERA over 5 IP vs 50 IP project differently). The
> components are REST-only and settle-lagged. **Nearly every rate-cat surprise traces
> back to this split.**
>
> **The compensation stack** (layer → what it fixes → where):
> 1. **Split-source `category_state` + per-stat reads** — scrape owns the scored
>    cats, REST writes only components, readers load latest *per (matchup,team,stat)*
>    (never a global `MAX(fetched_at)`). *Fixes the stale-REST clobber (~20pp flip) &
>    the idle-fetch scored-cat drop (2026-06-04). `db.latest_category_state`.*
> 2. **Monotonicity write guard** — banked counting stats can't decrease; a lower
>    read is a stale/partial source, rejected. *`cli._write_category_score`.*
> 3. **Live component reconstruction** — rebuild rate components from MLB box scores
>    during the slate, attributed to the day's lineup. *`sim.reconcile_live_components`.*
>    - **Name match (`_norm_name`)** strips accents, **middle initials, suffixes** so
>      MLB ⇄ ESPN spellings match. *Fixes unmatched relievers — "José A. Ferrer" ⇄
>      "Jose Ferrer" (2026-06-09).* **Disambiguated by `pro_team_id` since 2026-08-24**
>      (`_sum_counted`'s `team_by_norm_name`): two different MLB players can
>      normalize to ONE key, and the box-score pool holds *every* player, so both
>      of their lines matched the single rostered player and both were summed.
>      Two active Max Muncys did exactly that — the unrostered one injected
>      **+4 AB and +0 H** (the OPS group adds AB/2B/3B/BB/HBP/SF but NOT H/HR,
>      which the scrape owns), i.e. pure denominator inflation, dropping derived
>      OPS 0.6491→0.6387 and flipping m120 to a wrong winner for 4h40m. Fails
>      open: a line is dropped only when both team ids are known and disagree.
>    - **Rate guard (`_judge_group`)** — verdicts **`matched` / `closer` / `baseline`**:
>      commit the reconstruction when it matches the scrape, *or* is at least closer
>      to it than the stale baseline; keep the baseline only when it's genuinely
>      closer/unverifiable. *Fixes the settle-bound ERA/WHIP & OPS swings (2026-06-09/11).*
> 4. **In-progress QS/SVHD model (`ingame.py`)** — threshold/context stats; SVHD judged
>    from **entry/exit margins**, spot-starters skip the SVHD path. *Fixes flickering
>    holds / phantom saves (2026-06-10).*
> 5. **~~Settle-window QS/SVHD credit~~ — REMOVED 2026-08-11.** QS/SVHD now pass
>    straight through from ESPN (the scrape). The `max(scraped, settled_floor + box)`
>    reconstruction existed to bridge one gap: the scrape only ran while games were
>    In Progress, so a credit from the LAST game of a night waited for the ~07:00
>    settle. The **closing scrape** (`cli._scrape_due`, 2026-08-09) closed that at
>    source — measured 2026-08-10, four credits from games finalizing with nothing
>    else live all banked within ~8s. Deleting it removed four consumers of one
>    value with four different gates, which had produced 1830 false
>    `INV_SITE_QS_OVERCREDIT` flags, the m105 +16.4pp/−8.6pp swing pair, and a live
>    sim-vs-display disagreement. `scripts/late_credit_probe.py` re-measures the
>    premise for any date; `INV_SCRAPE_STALE` is the detector if the scrape dies.
>
> **Debugging a wrong-looking projection — the telemetry names the layer:**
> `details_json.live_recon` (per tick: scraped vs reconstructed vs baseline rate + the
> `matched/closer/baseline` verdict), `pitcher_final_lines` (the durable box line
> behind a credit), `team_schedule.became_final_at` (the credit boundary). And note
> the cron runs on a laptop that **dark-wake-sleeps**: a ~1h tick gap dumps a slate's
> worth of change onto one post-wake tick (often at the ~07:00 settle), so a drop can
> *look* like one big step when it's really accumulated. Incidents: `INCIDENTS.md`.
>
> **Full-day-offline variant (the big synchronized ~09:00 CET / 07:00 UTC lurch).**
> When the laptop is asleep/offline through the *entire* slate — not just a 1h nap —
> the scrape **never runs during any in-progress game**, so *zero* live banking happens
> all day; every scored cat stays frozen at its pre-sleep value. When the machine wakes,
> the games are already Final, so the scrape (In-Progress-only) still can't read them —
> the whole day banks in one shot at the next **~07:00 UTC REST settle**. Signature: the
> same tick moves **many matchups at once, same direction** (a normal settle nudges one
> or two). Confirm it's this and not real play: `SELECT DISTINCT computed_at FROM
> wp_snapshots WHERE computed_at BETWEEN <evening> AND <morning> ORDER BY computed_at`
> and look for a multi-hour **gap** (e.g. 2026-07-02: no computes 20:00 UTC → 05:20 UTC,
> then a 116-value banked batch at 07:00 hit 4/6 matchups −8 to −21pp). Also
> `SELECT ... COUNT(changed category_state rows) per tick` — a normal tick changes 0
> overnight; the settle tick changes ~100+. So "what caused the jump at 9 AM?" on such a
> day resolves to **the offline-all-day backlog settling**, not any single play. Same
> laptop-sleep root cause as the stale-schedule (`daily.sh` skips) and
> `ANOM_STALE_SNAPSHOTS` flag. Real fix = run the pipeline always-on, or add a second
> overnight REST settle (~05:00-06:00 UTC, after West-coast games end) so results bank
> near real time instead of piling onto 07:00.

ESPN's REST `mMatchupScore` endpoint lags ~5-30 minutes behind their web UI. The UI loads an initial REST snapshot then receives real-time updates via a **FastCast WebSocket** (`fastcast.semfs.engsvc.go.com`). The REST endpoint we use is updated by ESPN's backend on a slower aggregation cycle, so we can't get true real-time data from it.

We work around this by **scraping the rendered DOM** with headless Chromium via Playwright. `app/espn_scrape.py` opens `fantasy.espn.com/baseball/league/scoreboard`, waits for tables + WebSocket settle, then reads cat-by-cat values straight from the matchup tables. `cli.fetch` overrides the REST `cumulativeScore.scoreByStat` values with the scraped ones for the current period. Falls back to REST data silently if the scrape errors.

**The scrape gate: live games OR a just-Final game (`cli._scrape_due`).** `fetch` scrapes when any game is In Progress **or** one went Final within `CLOSING_SCRAPE_WINDOW_MIN` (20) — the **closing scrape**, added 2026-08-09. The second case matters because the scrape banks QS/SVHD *the instant a game reads Final*, so during a slate ESPN's number is current within one tick — but the last games of a night finish together (all six of 2026-08-08's went Final at 04:05:02), and the moment none are In Progress the scrape stops, leaving that final credit un-banked until the ~07:00 REST settle ~3h later. **That window was the only thing the QS/SVHD floor/archive reconstruction ever existed to bridge**, and measured against fully-settled week 17 that reconstruction over-counted in 2 of 24 cases (~8%, always high, and `max()` made it permanent — it cost the m105 +16.4pp/−8.6pp swing pair). One extra scrape closes it with ESPN's own number. **Root cause of those over-counts found 2026-08-10, and it was not the floor's arithmetic** — it was a stale pre-lock `daily_lineups` slot feeding it (see the Attribution bullet under "Live component reconstruction"). With lineups pulled per scoring period, the floor agrees with ESPN's settled scrape on **all 432** settled (matchup, team, stat) pairs of weeks 1-18, where it previously disagreed on exactly 3 — so the floor is a sound backstop again, not a ~8% liability. Deliberately stateless (no "last scrape ran at" marker): if the pipeline is down longer than the window we fall back to the 07:00 settle exactly as before. **`_scrape_owns_display_cat` takes the same `scrape_due` flag, not raw in-progress** — on a closing-scrape tick, keying off in-progress would hand REST ownership of the very cats the scrape is about to write.

**Current-period `category_state` is split by stat_id between two sources, with a monotonicity guard.** The headless browser is pure overhead when no scrape is due, and REST does **not** reliably catch up when idle — observed hours-stale after a slate finalized (a team's H stuck at 11 while the UI/scrape showed 19). So:
- **The scrape owns the league's display categories** (`display_cats` = the scored cats, incl. ERA/WHIP/OPS as rates). For a *seeded* current-period matchup, REST never writes these — that's what stops the stale-REST clobber that once flipped a WP ~20pp.
- **REST fills only the raw rate *components*** the scrape can't see but the sim needs to derive projected ERA/WHIP/OPS: ER, OUTS, P_H, P_BB, AB, 2B, 3B, BB, HBP, SF. (An earlier over-broad version skipped *all* current-period REST writes, which dropped these components and made rate projections ignore the week's banked innings — a team at 8.37 ERA projecting 3.76. The split restores the blend.)
- **First fetch of a matchup** (no rows yet) seeds everything from REST so it isn't empty before the first scrape.
- **Monotonicity guard** (`_write_category_score`): counting stats are cumulative within a period, so any read *below* the last-good (per matchup/team/stat) is a stale/partial source (laggy REST, a mid-render scrape, a dropped two-way line — e.g. Ohtani's pitching line vanishing dropped a team's K 26→20) and is rejected; rates are written as-is. Note the guard only prevents *new* regressions — it can't un-regress a bad value already entrenched as the latest (recover happens when the stat next climbs past it).

Past/future periods are REST-only (no scrape, no guard — all-zero or settled).

**The split-source write implies a per-stat read — don't read by a single
`MAX(fetched_at)`.** Because the scrape and REST write *different* stat_ids, and
the scrape only runs when games are live, **a given fetch tick writes only a
*subset* of a matchup's stats.** In particular the first *idle* fetch after a
slate (`scrape skipped (no games in progress)`) writes **only the REST components**
at a fresh `fetched_at` — the scored display cats keep their older (last-scrape)
timestamp. So any reader that loads "the latest state" as *all rows at the single
latest `fetched_at`* will see only that idle tick's components and **drop all 10
scored cats**, making the sim project from ≈zero banked → every WP collapses toward
50/50, and `publish` emits empty current-week scores (blank site). This bit us on
2026-06-04 evening (see `INCIDENTS.md`); the morning corruption that day was the
same family (a partial current-period state mistaken for complete).

The readers therefore load the latest value **per `(matchup, team, stat)`**,
not by a global `MAX(fetched_at)` — `sim.load_latest_state` and `_latest_score_rows`
in `cli.py` (both delegating to `db.latest_category_state`). This mirrors the `last_good` loader the
monotonicity guard already uses (cli.py, with the same rationale in its comment).
If you add another current-state reader, use the same per-stat pattern. The
`INV_CURRENT_CATS_MISSING` validation check (below) guards against a regression:
once a side has pitched, every scored cat must be present in current_state.

### Live component reconstruction (beating the once-daily REST settle)

**The problem.** The scrape owns the displayed scored cats and updates them live,
but the sim *projects* end-of-week ERA/WHIP/OPS/QS from the raw rate **components**
(`OUTS`, `ER`, `P_H`, `P_BB`; `AB`, `2B`, `3B`, `B_BB`, `HBP`, `SF`) — and those come
only from ESPN's REST endpoint, which **settles them ~once a day (~07:00 UTC ≈
midnight Pacific, ESPN's stat finalization)**, not live. So during a slate the
displayed rates move (scrape) while the projection baseline runs on banked
innings/AB that can be ~24h stale; when REST finally settles, the WP takes a
discrete step. (This is the mechanism behind "why did the WP jump at ~07:00 UTC"
— a benign daily catch-up, not a bug. See the WP-swing note below.)

**The fix.** We rebuild the components live from the MLB box-scores we already
fetch, attribute them by the day's fantasy lineup, and trust them only when the
rate they imply matches ESPN's live scraped rate. Pieces:

- **Source.** `mlb.fetch_boxscore` (now `mlb.parse_boxscore` + a network wrapper)
  returns `{"pitchers": [...], "batters": [...]}` — pitcher lines carry `p_h`/`p_bb`
  (for WHIP); batter lines carry the full OPS component set. We already fetched
  every live game's boxscore for the in-game QS/SVHD model; this just stops
  discarding the batting half and adds two pitching fields.
- **Retention (`refresh-live`).** Keep box-score lines (`live_pitchers`,
  `live_batters`) for every **unsettled** game — `game_date >= sim.settle_boundary_date(now)`
  (= `now − 7h`'s date; games on/after it aren't in ESPN's banked totals yet) —
  re-fetching each tick so a just-Final game's true final line stays current.
  Older games are pruned (ESPN has them; the sim reads them from category_state).
- **Attribution (`daily_lineups`).** Each tick we pull **each game-day's own**
  locked lineup from ESPN, addressed by that day's `scoringPeriodId`
  (`cli._authoritative_lineups` → `espn.fetch_daily_lineups(spid)`,
  `mlb.scoring_period_for_date`), and **replace** the stored rows for that date
  (`cli._replace_daily_lineups`). ESPN's per-scoring-period state is the source
  of truth; our own in-day observation is not.
  - *This was "fetch the current lineup once, stamp it on every date in the
    window, first snapshot per day wins" until 2026-08-10.* That assumed the
    first tick of a day lands at/after lineup lock. When it doesn't, the
    snapshot captures the manager's **pre-game intent** and `INSERT OR IGNORE`
    freezes it forever — and a wrong *active* slot is the expensive direction,
    because slots gate which box-score lines count for a team. (Until 2026-08-11
    the sharpest case was the QS/SVHD settled floor, where publish's
    `max(scrape, floor)` could never
    pull the phantom back down. Bryan Baker was stored slot 15 for 08-04 while
    ESPN had him **benched**, so the site published Swamp Dragons SVHD **4
    against ESPN's 3** — a lost category displayed as a tie, for six days.
    Re-pulling 14 days corrected **43 slots across 8 of 12 teams**, almost all
    in pairs (one bat out, one in) — late swaps are routine, so this was never
    a one-off. Repair past days with `app backfill-lineups [--days N]
    [--dry-run]`. Tests: `tests/test_daily_lineups.py`.
  - *Cross-day smearing* went with it: writing the current lineup under every
    date in the window meant a day missing its own snapshot silently inherited
    another day's slots.
  A box-score line counts
  for a team only if the matched player (by `_norm_name`; no ESPN↔MLBAM id
  crosswalk) was in an active slot that day: pitching lines need a **pitcher slot
  {13,14,15}**, batting lines a **hitter slot ({0–12,19})**; bench (16) / IL (17)
  never count. This is what correctly handles two-way players (Ohtani counts on
  the side he's slotted) and lineup changes.
- **The guard (`_judge_group`, pure).** For each rate group (pitching = ERA+WHIP
  over OUTS/ER/P_H/P_BB; OPS over AB/2B/3B/B_BB/HBP/SF — `H`/`HR` stay at baseline
  since the scrape owns those scored cats live, so adding a delta would
  double-count), reconstructed = ESPN baseline + summed unsettled delta. The
  governing rule: **always move the current rate toward the live scraped rate, never
  to a number further from it.** Three verdicts:
  - **`matched`** — every derived rate within `LIVE_RATE_TOL` of the scrape → commit
    (the confident case).
  - **`closer`** — out of tolerance, but the reconstruction is *nearer the scrape*
    than the stale REST baseline (summed abs error) → commit it anyway. An imperfect
    reconstruction (a missing/partial box line) still beats a ~24h-stale baseline.
    **This is the fix for the settle-bound swings** (2026-06-09 Bear Nation ERA/WHIP,
    2026-06-11 WAR OPS): there the baseline was *further* from the scrape than the
    reconstruction, so the old "reject → baseline" rule held the projection on a
    badly-off number (ERA 3.76 vs ~4.5; OPS 0.95 vs ~0.86) until the 07:00 settle.
  - **`baseline`** — no matched lines, no scraped rate to judge against, or the
    baseline is already at least as close → keep the baseline.

  The scraped rate is authoritative for current standings, so this stays fail-safe
  (a wrong attribution that lands *further* from the scrape than baseline is still
  rejected) while no longer discarding a good-enough reconstruction over a stale one.
  The decision (verdict + scraped/reconstructed/baseline rates) is recorded in
  `details_json.live_recon` for debugging.
- **QS / SVHD — no longer reconstructed (2026-08-11).** They pass through from
  `category_state`, i.e. straight from ESPN via the scrape. Previously
  `state[QS] = max(scraped_weekly, settled_floor + box_count)` (`max`, never
  additive — the 2026-06-07 deGrom double-count that flipped a matchup 100%→0%
  came from adding a Final game the scrape had already banked). All of it —
  `load_settled_floor`, `_count_qs`/`_count_svhd`, `cli._apply_qs_svhd_floor`,
  `validate._supported_credit` and `INV_SITE_QS_OVERCREDIT` — is deleted. Do not
  reintroduce a QS/SVHD adjustment on either the sim or the display side without
  first re-running `scripts/late_credit_probe.py`: the whole justification is that
  ESPN now credits within ~8s of Final even with nothing live, so any adjustment
  is either a no-op or an over-credit. **Confirmed on the decisive case 2026-08-11**
  (the earlier 08-10 measurement had no closing-batch credit to test): the night's
  LAST game finalized 05:05:02Z with nothing else live and its two QS both banked at
  05:05:13Z, **+11s**, against 12 mid-slate controls at 9-14s. Guarded by
  `tests/test_publish_display_lag.py::test_publish_does_not_adjust_qs_svhd`.

- **Wiring.** `compute` (current week only, mc-v1) loads the unsettled lines once
  and calls `sim.apply_live_components` per team before `simulate`; the echo reports
  `live_component_groups_accepted`. No-op for `--future`, non-mc models, or when
  nothing is live — behaves exactly as before. `app/validate.py`
  `check_live_lineup_capture` warns (`ANOM_LINEUP_SNAPSHOT_MISSING`) if box-score
  lines exist for a day but no lineup was captured (ESPN-auth failure → silent
  fallback for everyone).

**Validated** (2026-06-06): reconstructing each team's prior-day pitching `OUTS`
from real box-scores + the real ESPN lineup reproduced ESPN's own banked `OUTS`
delta **exactly for 10/12 teams**; the 2 misses were lineup *day*-drift (a proxy
current-day lineup vs the actual prior-day one) — which the per-day `daily_lineups`
snapshot fixes and the guard catches by falling back. Tests: `tests/test_live_components.py`.

Tuning knobs at the top of `sim.py`: `SETTLE_LAG_HOURS` (7), `PITCHER_SLOTS`,
`NON_COUNTING_SLOTS`, `LIVE_RATE_TOL`, `PITCH_RATE_COMPONENTS`, `OPS_RECON_COMPONENTS`.

### The live-feed proxy (2026-08-04 Akamai 403) — why `page.route` is there

The scoreboard renders an **initial REST snapshot**, then applies live in-play updates
from `site.api.espn.com/apis/fantasy/v2/games/flb/games?…pbpOnly=true`. From
**2026-08-04** Akamai began **403ing that request from our headless browser**, so the
page never applied a live update and the DOM sat frozen at the last ~07:00 UTC settle.

This is the nastiest shape of scrape failure, because it is **invisible**: the scrape
kept returning a full ~120 *well-formed but stale* cells. `ANOM_SCRAPE_EMPTY` can't see
it (nothing is empty). H/HR/R/SB/K held the previous day's totals for an entire slate and
the whole day landed in one tick at the settle — 2026-08-06: **all six matchups moved at
once, 35pp absolute**. It also dragged the **rate cats** down with it: `sim._judge_group`
scores the box-score reconstruction by its distance to the *scraped* rate, so a stale
scrape that exactly matches the equally-stale REST baseline makes every reconstruction
look "further away" and get rejected (`verdict: baseline` on every group, all night).
One frozen source silently takes down both input paths.

**Root cause is upstream, not ours** — nothing local changed in the break window
(`espn_scrape.py` untouched since 2026-06-18, Playwright/Chromium installed May 31,
macOS since Jun 25); identical code worked at 04:00:10Z on 8/04 and failed by 22:40Z the
same day. The response is Akamai's edge "Access Denied" (`errors.edgesuite.net`), and the
rule is **host-wide** on `site.api.espn.com`, not league- or feed-specific.

**The fix** (`_serve_maybe_proxied` + `PROXY_ROUTE` in `espn_scrape.py`): the block is on
*browser-shaped* traffic only — the identical URL returns **200 with ~1.6 MB to plain
`httpx`** (as does our `espn_public` client, which is why probables/injuries never broke).
So a `page.route` handler re-fetches the blocked request server-side and hands the bytes
to the page. ESPN still computes the scoreboard; we just stop letting one of its requests
die. Two properties worth preserving if you touch this:

- **Browser first, proxy only on 403/dead** (`should_proxy`) — so an unblocked ESPN never
  takes the proxy path and the workaround **self-heals** with no code change.
- **Only `site.api.espn.com` is routed.** It is the *unauthenticated* public host.
  **Never add `lm-api-reads.fantasy.espn.com`** (the authed `mMatchupScore` endpoint):
  fulfilling it from a cookie-less client strips the ESPN session and breaks the page.
  Guarded by `tests/test_scrape_proxy.py::test_never_routes_the_authenticated_host`.

Proxy failure falls through to `route.continue_()` — degrade to today's behavior, never
to a page that can't load. `INV_SCRAPE_STALE` remains the detector for the whole class:
if Akamai changes the rule again, that flag is what tells you.

### Auth (the tricky part)

ESPN's web UI requires more than the `SWID`/`espn_s2` cookies we use for REST — it also needs MyDisney session cookies (`ESPN-ONESITE.WEB-PROD.token`, `dtcAuth`, `espnAuth`) that are httpOnly and only get set through the full MyDisney login flow.

Solution: a **Playwright persistent profile** at `.playwright_profile/` (gitignored). One-time setup by running `scripts/espn_auth_setup.py` — opens visible Chromium, user logs in once, profile dir saves all cookies. The cron-driven scraper then launches `launch_persistent_context(user_data_dir=...)` headlessly against that same profile.

When ESPN expires the session (weeks/months later), the scraper returns empty data and `cli.fetch` falls back to REST. The fix is to re-run `espn_auth_setup.py`.

### Performance

- Adds ~10-25s to each `fast.sh` tick (browser launch + page load + 6s settle wait)
- ~200 MB RAM per scrape, released after each run
- Total `fast.sh` runtime ~30-40s (up from ~15s) — still well under the 5-min cadence budget

## Front-end behavior (docs/app.js)

- **`publish` emits every regular-season week** (period 1 → last_reg), so past
  matchups stay selectable in the dropdown. Each week carries a data-driven
  `state` in `data.json`: `final` / `live` / `upcoming`, computed by `_week_state`
  from game statuses — with a decided-winner fallback for old weeks whose
  `team_schedule` rows have been pruned (only current+future weeks keep them).
- **Default week = the latest week with `state != "upcoming"`** (the latest one
  that has *started*). No wall clock: on Monday morning it stays on last week
  until the new week's first game goes live, then flips on its own. `state` also
  gates whether team blocks show real scores vs projection dashes (the `started`
  flag through `_matchup_block`/`_team_block`).
- **WP-over-time graph x-axis** — one global segmented control with four modes
  (`renderChart`, `CHART_SCOPES`):
  - **Full** (default): linear real time over all history; faint "matchup start"
    divider where the week began (when there's pre-matchup history to its left).
  - **Matchup**: clips to the week's Monday (`week.start`), dropping the flat
    pre-matchup `--future` projection points.
  - **Active**: collapses the dead time *between game-days* — concatenates each
    day's observed game window proportionally, with a labeled divider per day.
    Days with no plotted points (e.g. a week whose early history was trimmed)
    are skipped so the axis doesn't allocate blank space.
  - **Today**: clips to the start of the current day's games (the most recent
    `active_intervals` entry), then renders linearly — a live zoom on today,
    starting at today's first pitch (not 24h back). Falls back to the full range
    if today has no points yet.
  Full/Matchup are pure front-end. Active needs the **observed game-day windows**
  (`week.active_intervals`), which come from the `game_day_activity` table:
  `refresh-live` stamps `active_start` the first tick it sees a game In Progress
  and `active_end` once all that day's games are Final; `publish` emits them
  (an in-progress day stays open-ended at "now"). `scripts/backfill_game_activity.py`
  did a one-time estimate fill for weeks 9–10's already-elapsed days (earliest
  first pitch → latest first pitch + ~3h15m) since live tracking only records
  forward; the COALESCE upsert means empirical values always win over the estimate.
  - **Windows are clamped disjoint at publish (`_clamp_active_intervals`).** A game
    that finalizes ~a day late (a **suspended game** resuming the next day) leaves
    one day's `active_end` overlapping the next day's `active_start`. The Active axis
    assigns each point to the *first* interval containing it, so an overlap steals the
    next day's early points and renders that segment's lead-in as a **blank horizontal
    gap** (observed 2026-06-16/17, Week 12). `_active_intervals` pulls each day's `end`
    back to ≤ the next day's `start` so segments stay disjoint. Display-only; the
    per-week publish cache means a one-time `publish --rebuild` is needed to apply an
    emission-logic change like this to already-cached weeks. Test: `test_active_intervals.py`.
  - **Active deliberately omits everything after the last game window** — so the
    **post-game settle climb isn't shown in Active**. A matchup that clinches at the
    overnight reconcile (WP reaching 100% *after* the last game went Final — see
    "Finalization lag") happens in dead time outside any window, so the Active line
    **ends mid-climb** (e.g. "ends at 3:45 AM at 94%, never shows 100%"). This is by
    design, not a bug; **Full/Matchup** plot linear real time and show the final climb.
    Active's last visible point is the last *in-window* point that survived thinning
    (since 2026-08-03 the final 24h are un-thinned, so that is the last raw 5-min
    tick before the window closed).
- **Chart annotations (the "✦ Annotate" toggle).** Off by default; overlays major
  events + trend spans on the WP chart in *any* scope (placed via the same
  `xt(timestamp)→x` mapping, so they land correctly even in Active's collapsed
  axis). Two SVG layers: faint span bands + event guide-lines BEHIND the curves
  (no pointer events), and interactive markers ON TOP of the hover layer
  (`.annot-top`, painted last) so they're reliably hoverable/tappable — single-
  player event triangles along the top edge, trend-span handle bars along the
  bottom edge. Detail shows in the chart tooltip on hover **and** click/tap
  (`bindChartHovers` binds `.annot-hit`); no caption. Data is **per-matchup**
  `docs/annotations/<matchup_id>.json` — lazily
  `fetch`ed only when the toggle is on (so **data.json is never touched / no
  payload bloat**), `null`-cached when absent. These files are **generated
  on-demand**, not by `publish`: the `/matchup-summary` skill writes + commits
  them. The events (named plays) and spans (day-level trends) are **authored by
  the LLM** running the skill and bundled via
  `scripts/matchup_facts.py <id> --write <authored.json>`; the writer validates
  the sign convention (positive `wp_delta` from the named team's perspective;
  `side` away/home, `dir` up=away-gained/down=home-gained) and adds a
  deterministic tie-aware `result` line. Empty file / 404 → no overlay (ask for a
  summary to generate it).
- **Weekly write-up in Details.** The same `docs/annotations/<id>.json` may carry a
  `writeup` (markdown) + `result` line; when present, Details renders a "Weekly
  summary" section below the chart (a tiny markdown subset renderer, `mdToHtml` —
  headings/bold/lists/paragraphs, no tables/raw HTML). Fetched lazily the first
  time a matchup's panel is expanded (`fetchSummary`), independent of the Annotate
  toggle. Authored + bundled by `/matchup-summary` (`scripts/matchup_facts.py
  <id> --write <authored.json>`, where the json carries `events`+`spans`+`writeup`);
  absent → the section just doesn't render.

## Investigating "why did this WP change?"

> **Check `INCIDENTS.md` first.** Known data incidents (and any **hand-edited
> historical snapshots**) are logged there. Period-10 matchups (id 55–60) on
> **2026-06-04** have *two* hand-edited windows that day — those `home_wp`/`away_wp`
> columns are smoothed and don't match `details_json`; don't chase them as live bugs:
> - **~17:05–20:02 UTC** (morning corruption) — dropped rate *components*; `details_json`
>   is *also* corrupted across the broader **~06:16–20:02** span (model output on
>   missing components). See the m59 worked example in `INCIDENTS.md`.
> - **21:35–21:55 UTC** (evening) — the idle-fetch dropped the *scored cats* (WP
>   collapsed to 50/50); 5 ticks linearly interpolated between the 21:30 and 22:00
>   anchors. Root cause fixed in code (`38b4959`) — see the read-side note under
>   "Live data freshness" and `INV_CURRENT_CATS_MISSING`.
>
> All 210 hand-edited rows are now marked **`wp_snapshots.edited=1`** (machine-readable,
> so you don't have to eyeball date ranges):
> `SELECT computed_at, matchup_id FROM wp_snapshots WHERE edited=1`. The
> `INV_WP_DETAILS_MISMATCH` check skips them; any *un*marked row whose `home_wp`
> column disagrees with its `details_json` tally is a real bug.

> **Step 0 — always, before any hypothesis:**
> ```sh
> .venv/bin/python scripts/wp_diff.py <matchup_id|team-name> "<start>" "<end>"
> ```
> (naive times = Europe/Oslo). One command prints the full decomposition the
> method below assembles by hand: the tick series with sleep-gap warnings,
> per-cat win-share deltas with **leader flips marked**, per-player budget
> diffs with provenance flags, banked-state deltas, games-gone-Final,
> `live_recon`, overlapping validation flags, and which points survived the
> site's downsample. The `wp-investigate` skill holds the procedure and
> output rules; **log every investigation's outcome in `INVESTIGATIONS.md`**.
>
> **Signature table — symptom → likely mechanism → confirm** (details in the
> numbered playbook below):
>
> | signature | likely mechanism | confirm via |
> |---|---|---|
> | banked deltas move, budgets flat, during a slate | live play banking (overshoot-and-correct if it reverts) | opponent's `category_state` deltas (meta-lesson) |
> | banked flat, a budget player added/removed | roster/lineup move — usually the *opponent's* | budget diff names (#14) |
> | discrete drop exactly at a game's first pitch | benched starter's projected start dropping | `benched-live-drop` flag + bench slot (#15) |
> | step at ~07:00 UTC, rate cats only | daily REST component settle — benign | Δ concentrated in ERA/WHIP/OPS/QS (#4) |
> | many matchups move same tick, same direction | systemic: offline-all-day settle lurch OR data bug | `computed_at` gap; `ANOM_CORRELATED_SWING` |
> | wrong for DAYS, corrects at a refresh boundary | phantom schedule game / stale budget input | past-dated non-Final `team_schedule` row (#7) |
> | decided matchup stuck at ~9x% overnight | finalization lag (status-lag sliver) | fractional remaining avg in `category_wp` |
> | one-tick blip that snaps back | transient bad read — don't over-attribute | (#6) |
> | touches BOTH near-0 and near-100 | over-credited stat (historically the QS/SVHD double-count family, removed 2026-08-11) | `live_recon` result vs scrape; `INV_SITE_DB_MISMATCH` |

Common case: user notices a sudden WP shift and asks why. Method:

1. **Pull the snapshot history** for the matchup:
   ```sql
   SELECT computed_at, home_wp FROM wp_snapshots
   WHERE matchup_id=? ORDER BY computed_at
   ```
2. Identify the transition timestamp(s) — look for jumps > 1pp between consecutive 5-min ticks (anything within ±1pp is Monte Carlo noise; MC SE ≈ √(p(1-p)/10000) × 100 ≈ 0.4pp at p≈0.5).
3. **Diff the budgets** between the snapshot before and after the transition:
   - Roster changes: compare `details_json.home_budgets[].player_id` sets — added/removed players
   - Probable pitcher changes: same roster, but an SP whose `units` stepped up toward 1 (from its estimated open-game share) — or back down. MLB just announced/un-announced their start.
   - IL/injury changes: a player who disappeared from the budget — check their injury_status
   - SVHD/projection refresh: usually happens around medium.sh runs (every 4h) — same players, different `expected[83]` values
4. **Diff the live category state** for backfilled stat corrections:
   ```sql
   SELECT team_id, stat_id, score, result FROM category_state
   WHERE matchup_id=? AND fetched_at LIKE '<before>%' OR fetched_at LIKE '<after>%'
   ```
   ESPN sometimes retroactively credits stats hours after games — H goes from 27→32, result might flip from LOSS to WIN.
5. **Cron events to know**:
   - medium.sh runs ~1-2 min every 4h at minute :02 of `*/4` hours local time (offset since 2026-07-02 so fast.sh's :00 tick no longer skips; before that it fired at :00 and cost a fast tick). Big jumps often line up with the first fast.sh tick after medium.sh finishes.
   - 5-min boundary fast.sh fires every `*/5` minute. New live data lands every tick.
   - GitHub Pages rebuild lag — pushes appear ~30-90s later on the live site.
6. **Investigation telemetry** (added 2026-06-10 — closes the gaps that made the deGrom/Melton digs slow):
   - **`details_json.live_recon`** (per snapshot, live week) — the live-component reconciliation that fed *that* tick: per team, QS/SVHD `{scrape, floor, box, result}` and the rate-group verdicts + `since_date`. Answers "why is current QS=N this tick, and was it scrape, floor, or box?" without reverse-engineering from `category_wp`. Since 2026-08-11 the QS/SVHD entries are gone — those cats are no longer reconstructed — so `live_recon` carries the rate groups only.
   - **`pitcher_final_lines`** table — write-once archive of every Final starter/reliever line (`outs/er/k/p_h/p_bb/sv/hld`, `games_started`, `final_at`), durable past the `live_pitchers` prune. The line that earned/missed a QS/SVHD credit, answerable offline (this is how the Melton spot-start surfaced).
   - **`team_schedule.became_final_at`** — the first tick a game read Final (the credit boundary), instead of inferring it from `category_state` steps.
   - **`reliever_appearances`** — each reliever's entry/exit run-margin (drives the in-game save/hold judging; see "In-progress QS & SVHD").
   - **`batter_final_lines`** (added 2026-08-10) — the hitter analogue of `pitcher_final_lines`: write-once per `(game_pk, mlbam_id)` from `_archive_final_batter_lines` in `refresh-live`, Final games only. `live_batters` is pruned once a game ages out of the unsettled window, so before this there was **no record of what a hitter actually did** — which is why hitter accuracy could only be reached via the unit-free ratio trick (HR/H etc., which cancels games-played), and why the ~+8% lineup-days over-projection is an *inference* rather than a direct reading. Carries the full OPS component set (`ab/h/b2/b3/bb/hbp/sf`) plus the scored counting cats (`hr/r/sb`), so per-game rates **and** games-played become directly checkable from period 19 forward. Tests: `tests/test_batter_archive.py`.
   - **`ros_projection_archive`** (added 2026-08-10) — the split=6 ROS block per matchup period, **first write per period wins** (refresh-rosters, `INSERT OR IGNORE`; same rule as `daily_lineups`). Exists because `player_projections` has **no period key** and every fetch overwrites it, so a past week's projection *inputs* were unrecoverable — which is why the model as it runs today can never be scored against history, only "the model as it ran" (the standing caveat on `scripts/calibration.py`). ~6.5k rows/week. **Starts at period 19; weeks 1-18 are gone for good.** A later refresh in the same week must not overwrite the first capture, or the archive silently becomes a record of mid-week values — verified live by a second `refresh-rosters` leaving row count, `captured_at` and value-sum byte-identical. Tests: `tests/test_ros_archive.py`.
   - **`details_json.{home,away}_budgets[].flags`** (added 2026-07-02) — per-budget provenance: which special-case path shaped the projection (`promoted`, `cadence` vs `flat-extra`, `start-capped`, `rp-apps-capped`, `qs-ingame`/`svhd-ingame`, `benched-live-drop`, `live-keepalive`, `two-way-sub`). Answers "was this pitcher promoted / capped / overridden this tick?" in one lookup instead of a forensic dig. Omitted when no special case fired. Defined on `sim.Budget.flags`; tests in `tests/test_budget_flags.py`.

The repo history has a handful of investigation commits (e.g. `cd4b187` Lineup-aware projections, `aab6951` ROS SVHD from full-season proj minus actuals, `10c60fe` Empirical-rate SVHD) — those commit messages contain real numbers for the player examples used during the investigation. Useful reference.

### WP-swing investigation playbook — themes & gotchas (read before diagnosing)

Distilled from many live WP-swing investigations. These are the traps that waste
time and the signatures that explain ~every swing fast:

1. **Diff `details_json.category_wp`, not just `home_wp`.** Pull the before/after
   snapshots and compare each category's win% and projected avg; the category with
   the big win% delta IS the cause. The fastest tell: **a projected per-category
   avg that jumps by exactly +1.0 = a discrete counting event was banked.**
   - **QS +1.0** → a starter completed a quality start (6 IP, ≤3 ER), credited at
     Final (or in-progress once he passes 18 outs). e.g. deGrom's 6 IP / 0 ER.
   - **SVHD +1.0 as a rostered reliever's situation resolves** → a save/hold
     credited. Since the 2026-06-10 entry/exit-margin fix this is credited *live*
     (once he exits with the lead), not deferred to Final, so the old all-game-0
     →+1-at-Final flicker is largely gone (cause #8). e.g. Bazardo, Ferrer.

2. **NEVER read "current" rates off raw `category_state` / `load_latest_state`.**
   `derive_ops`/`derive_era`/`derive_whip` on the raw banked state mixes
   **scrape-live H/HR** with **REST-stale AB/ER/OUTS** components → a bogus current
   rate that contradicts the scoreboard (cost hours: "Teacher leads OPS .958" when
   the board said .824, and "behind in ERA/WHIP" when he led). For the *current*
   standing use the **scraped** value (stat 18/47/41) or the **folded** state —
   run `sim.apply_live_components(...)` (what `compute` does) or
   `cli._fold_live_components(...)` (what `publish` does) *before* deriving. The
   folded-derived rate matches the board; the raw-derived one is meaningless.

3. **Current standing ≠ projection.** A category's win% is about the *projected
   end-of-week* totals. "Currently behind but projected to win" is legitimate
   (e.g. a ratio cat where the opponent's ERA/WHIP will regress up as they throw
   more innings). Don't treat a current deficit as a contradiction of a high win%.

4. **The model can be AHEAD of the ESPN scoreboard.** Live reconstruction credits
   QS/SVHD/components from finalized box scores *before* ESPN's once-daily
   ~07:00 UTC settle, so the model's numbers can legitimately differ from (lead)
   the board. Conversely a swing right at ~07:00 UTC is usually the benign daily
   component settle (cause #7), not a bug.

5. **Reachable-bounds sanity check for "impossible" category odds.** QS ≤1 per
   start, SVHD ≤1 per appearance. For a near-locked category, compute each team's
   banked + max-possible-remaining; if the sim shows probability for an outcome
   outside those bounds, suspect a sampling/projection bug — that's how the
   Poisson-lets-one-start-score-2-QS bug (→ Binomial, `c710cbf`) was caught.

6. **One-tick blip that recovers = transient bad read, not an event.** `live_pitchers`
   is overwritten each fetch, so a single-tick QS/SVHD dip/spike that snaps back
   next tick is usually a momentary stale/partial box-score read; you can't replay
   the exact line. Don't over-attribute it.

7. **Phantom schedule games inflate starts/appearances/lineup-days.** If a probable
   projects >1 start, or an RP/hitter's "games remaining" looks too high, check for
   postponed / out-of-window games still filed under the period (the
   `matchup_period_id` PK keeps a postponed game in-period with a far-future makeup
   date). `load_schedule_by_team` excludes three classes: out-of-window dates,
   `Postponed/Suspended/Cancelled` status, and (when passed `now=`) **past-dated
   games that aren't `Final`/`In Progress`**.
   - **The lag-window trap (2026-06-18, m70 Imanaga — fixed).** A postponement is
     NOT reflected in `team_schedule` immediately: the makeup date + `Postponed`
     status only land at the next daily `refresh-schedule`. Until then the row keeps
     its **original in-window date + a stale `Scheduled` status**, so the first two
     filters don't fire and the sim credits a phantom start. A postponed Cubs game
     (Imanaga probable) kept a full start (~5.7 K, ~0.5 QS) on the *Bus's* books all
     of week 12 → inflated Bus K projection to 49.7 vs a real 44 → suppressed the
     **Knights to ~6% the entire week**, only correcting Monday when refresh-schedule
     moved the date to August. (The Knights had actually won K 45-44 and the 5-5 hits
     tiebreaker the whole time.) Fix: the past-date guard — a game whose date is
     well past yet isn't Final/live *didn't happen*, so drop it without waiting
     for the status feed. `compute` passes `now`; the guard is opt-in (no `now` ⇒
     old behavior) so other callers/tests are unchanged. Tests: `test_schedule_filter.py`.
   - **The guard uses a 1-day buffer (`game_date < today_utc − 1`), NOT `< today`
     — learned the hard way (2026-06-24 Gage Jump regression).** `game_date` is the
     *US calendar* date, a day behind UTC for late/West-Coast games (date D plays
     into D+1 early UTC). A bare `< today` dropped a legit not-yet-started start the
     instant UTC passed midnight: Jump dated Jun 24, still pre-game at 00:01 UTC Jun
     25 → dropped → Sox Teacher suppressed ~13%→~3% for ~4h until his game went Final
     and the 9 K banked (recovering to ~14%). The buffer keeps a game through all of
     the next UTC day while still catching genuinely-stale postponed rows (≥1 day past
     before they matter; Imanaga's lingered ~4 days). **Lesson: test the boundary** —
     the original tests used a 2-days-stale game and passed under both the buggy and
     fixed cutoff; the bug lived exactly at the today−1 edge.
   - **Diagnostic tell:** a phantom start suppresses/inflates a WP **persistently
     (the whole week, not one tick)** and corrects exactly at a `refresh-schedule`
     boundary, not at a game event. So a WP that's been "off" for days — not a single
     swing — points at a *budget input* (a phantom start, a stale projection), not a
     live play: diff the budgets (`details_json.{home,away}_budgets` — look for an
     SP with `units≈1.0`/`exp_k>0` who has no corresponding Final line in
     `pitcher_final_lines`), then check `team_schedule` for a past-dated non-Final
     row on that pitcher's team.

8. **Owner-known benign behaviors (don't flag as bugs):** RP-classified pitchers
   can carry a small QS (they spot-start); the ~07:00 UTC daily settle step; sub-1pp
   tick-to-tick jitter is Monte Carlo noise. (Holds used to "resolve at Final" here —
   fixed 2026-06-10, now credited live from entry/exit margins.)
9. **Attributing a *past* QS/SVHD:** `live_pitchers` is pruned once a game ages
   out of the unsettled window, so the exact box line that earned (or missed) a
   credit is gone after the fact. The write-once **`pitcher_final_lines`** archive
   (`cli._archive_final_lines`, captured the first tick a game reads Final — outs/
   er/k/p_h/p_bb/sv/hld + `games_started` + `final_at`) is the durable source —
   query it to answer "was it 17 or 18 outs / who got the hold / was he the
   starter" offline (it's how the Melton spot-start surfaced). For hitters there's
   no archive yet; fall back to fetching the MLB box score by date (as
   `/matchup-summary` does).
10. **Use the canonical `stat_id` map — never hardcode it from memory.** The scored
    cats are `1=H, 5=HR, 18=OPS, 20=R, 23=SB, 34=OUTS, 41=WHIP, 47=ERA, 48=K,
    63=QS, 83=SVHD` (authoritative source: the dict at the top of `app/stats.py`;
    `import app.stats` and read it, don't retype). The footguns that have actually
    bitten: **OPS is 18, not 41** (41 is WHIP), and **QS is 63, not 53**. When
    diffing `details_json.category_wp`, label rows from this map — a wrong label
    caused a real misattribution (an *H* swing reported as an *R* swing). Sanity
    check: if a "category" you've labeled R shows a projected avg ~52, it's H, not R
    (weekly R tops out ~40); if a rate cat's `home_avg` is >1.0 it can't be OPS as
    displayed (see #11). **Same rule for player→MLB-team identity: the 2026 league's
    stored data is the source of truth, NOT real-world roster memory.** (2026-07-22:
    assumed Ranger Suárez was a Phillie from memory — he's a Red Sox in this league,
    proTeamId 2→MLBAM 111 — and wrongly dismissed his *correct* stored Red Sox
    schedule as "incoherent." Resolve team via `players.pro_team_id` →
    `teams.ESPN_TO_MLBAM`, never memory. There's no ESPN↔MLBAM player-id crosswalk,
    so player→team matching is name-only anyway.)
11. **`category_wp[].home_avg`/`away_avg` are trustworthy for counting cats — and
    for rate cats too (CORRECTED 2026-08-10).** This entry used to claim the rate
    cats' stored `avg` is "an internal/derived scale (OPS showed ~1.0–1.6, not
    ~.800)" that "won't match the site", and that claim is what kept OPS/ERA/WHIP
    out of every accuracy measurement. **It does not hold for the current model.**
    Verified two ways: at each decided week's **final** snapshot — where the
    projection must equal the settled result — `avg / category_state.score` is
    **1.0000 (sd 0.001, n=24 per cat)**; and pre-play values sit in plausible
    display ranges (OPS .750–.805, ERA 3.36–3.88, WHIP 1.14–1.29). If you ever see
    OPS ~1.0–1.6 again, that's a real regression worth chasing, not the expected
    encoding. The **win%** (`home_wins/n_sims`) is always reliable.
12. **Reconcile against the *downsampled* site history before explaining "what the
    user saw."** `publish` keeps each week's final `RECENT_FULL_HOURS` (24) of
    history at raw 5-min resolution and snaps everything older to an
    `OLDER_GRID_MINUTES` (15) grid, so a brief mid-week raw-DB blip (a 5–10 min dip
    across 1–2 snapshots) can still be invisible on the chart. Don't anchor a
    narrative on a transient `wp_snapshots` value the user could never have seen —
    load the matchup's published `history` from `docs/history/<period>.json` and
    check whether the point survived downsampling (`wp_diff.py` prints this as
    "published site"). (This burned a "Knights were at 48%" explanation: 48% was a
    real 20-min raw dip that the site never plotted; at the time the user
    referenced, the chart showed 64%. Under the *old* rule — a flat 200 points per
    week, ≈55-min steps — even a 78pp end-of-week cliff could vanish: 2026-08-03
    m98, which is why the 24h tail is now un-thinned.)
13. **A banked lead's category win% is about *projected end-of-week* totals, so
    "leading ⇒ favored" only holds for high-event cats.** A big counting lead is a
    lock (a 9-run R lead sat ~99% all of the final day — correctly). But a slim lead
    in a **low-event cat (SB, HR, QS, SVHD)** can sit *below* 50% if the opponent
    *projects* to add more of that event — e.g. Knights led SB 6–5 entering the last
    day yet were ~44% because the model projected the opponent (rostered better
    base-stealers) to out-steal them; from a 6–6 live tie it was Opp 52% / tie 31% /
    lead-holder 17%. So these slim-lead, low-event categories are the most
    projection-sensitive and look far more volatile than a tidy 1–1 final suggests —
    expect hard intraday whipsaws driven purely by *which side banks the next event
    first*, not by a real change in who's "ahead." Don't call this a bug.
14. **A projection swing with *flat banked stats and an unchanged schedule* is a
    roster/lineup change — diff the budgets to find who.** When `category_wp` avgs
    move but banked `category_state` is flat AND `team_schedule` wasn't refreshed,
    an active player was added / dropped / benched, changing projected *remaining*
    production and flipping contested cats. Diff `details_json.{home,away}_budgets`
    player-by-player across the two ticks: a name whose `units→0`/vanishes was pulled
    from the active lineup; a new name was added. It's usually the **opponent** side
    that changed (the side you're asking about didn't move — its own avgs are flat).
    Examples (2026-07-02/03): Desert Dawgs dropped **Matt Chapman** → their projected
    H fell 38.4→34.9, flipping H+R to the Giraffes (**+12pp**); WAR dropped **Bryce
    Elder** (a projected SP start) → their K/QS fell, handing the Knights (**+11pp**).
    Contrast #7's *phantom* start (persists for days); a roster move is a clean
    one-tick step that then holds.
15. **A benched starter's projected start counts until *first pitch*, then drops —
    a discrete opponent-favoring swing at game time, NOT gradual "end-of-day
    convergence."** `benched-live-drop` only zeroes a benched player's game once it's
    **In Progress** (a manager can still activate him up to game time), so a starter
    sitting on the bench (`team_rosters.lineup_slot_id` = a bench slot) keeps his full
    projected line — K/QS/OUTS/ER + his ERA/WHIP contribution — on his team's books
    all through Pre-Game, then loses it the instant his real game starts. Signature:
    the *losing* side's projected K/QS fall and ERA/WHIP worsen **exactly at that
    game's first pitch**, and the vanished pitcher carried the `benched-live-drop`
    flag. Diagnose by diffing budgets across the first-pitch tick + checking the
    pitcher's bench slot and his `team_schedule` status flipping to In Progress.
    Example (2026-07-05): the Bus benched **Kyle Bradish**; at first pitch his line
    (5.9 K, 0.6 QS, 3.6 ERA / 1.24 WHIP) dropped, the Bus's projected K fell 7.5→1.6,
    and the **Shih Tzus firmed ~95%→97.6%**. (Side effect: a benched SP slightly
    *over*-credits his own team pre-game, correcting at first pitch — by design.)
16. **A "locked" rate cat (ERA/WHIP at 85–95%) is still the starter's to swing.**
    When judging which players can move a matchup — or attributing a rate-cat swing —
    don't dismiss a starter because the category looks settled: the win% already bakes
    in his *expected* line, so the residual tail IS the variance of his actual start.
    On the final day especially, one start is a large share of the remaining innings,
    so a gem vs. a blow-up is precisely what decides an 85/15 ERA/WHIP category.
    (2026-07-05: Hancock was ~70% of Jo Mamas' remaining innings — the whole lever for
    their 11% ERA / 6% WHIP longshot, despite the point estimate looking lopsided.)

> **Meta-lesson from a multi-swing investigation (parallel weekend matchups):** the
> recurring live-week swing pattern is **overshoot-and-correct** — a side's WP drops
> (or spikes) hard as the *opponent's* offense banks counters live during a
> Fri/Sat-night slate, then reverts as those games go Final and the inflated
> remaining-projection mean-reverts. A within-night dip-then-recover with no roster/
> probable change is almost always this, concentrated in the offensive cats
> (H/HR/R/OPS). Confirm by diffing the banked `category_state` deltas for the
> *opponent* across the window (timestamped), not just the WP curve. And: timestamps
> are UTC; the owner reasons in local time — in summer that's **CEST = UTC+2** (he
> says "CET" but means CEST in June), so convert before quoting "8:15 PM".

> **Finalization lag — why a decided matchup doesn't read 100%/0% until *after* the
> last game (and how to explain it).** WP is an *end-of-week projection* = banked +
> **expected remaining production**, and "remaining" is driven by game **status** in
> the model's inputs. A matchup can be mathematically over (every category locked by
> the actual results) while the model still carries a sliver of remaining production
> for one side, leaving a category live in the sim — so the WP sits at, say, 97% (or
> 6%) until the inputs reconcile, then snaps to 100% (or 0%). It's the model being
> *correct given stale inputs*, not a bug in the sim. Three observed roots, easiest
> to spot via the per-category `home_avg`/`away_avg` carrying a fractional remainder
> above the banked total:
> - **Status-lag remaining counters.** A finished game still read as not-`Final` in
>   `team_schedule` keeps a fraction of a hitter's expected H/HR/etc. alive. (Norsemen
>   2026-06-21: a ~0.16 expected remaining HR gave the opponent a ~13% chance to *tie*
>   HR 12-12, capping WP at ~98% until the overnight reconcile zeroed it → 100%.)
> - **Phantom start from a postponed game** (playbook #7 above) — the worst case:
>   it's not a sliver, it's a whole projected start (~5.7 K), and it can suppress a WP
>   for *days*, not minutes (m70 Imanaga). Distinguish from the benign sliver: phantom
>   starts persist and correct at a `refresh-schedule` boundary; status-lag slivers are
>   small and correct at the ~07:00 UTC settle / next live tick.
> - **Over-long active window from a suspended game** — the display analogue, see the
>   `_clamp_active_intervals` note under "Front-end behavior".
>
> The corrective swing therefore lands at a **schedule/settle boundary, not a play** —
> don't attribute it to a single event (it's the daily reconcile, same caveat as the
> ~07:00 settle). The standing fix for the whole family is the same: **zero a team's
> remaining projected production the moment its game reads Final / its date passes**,
> rather than waiting for the daily refresh. The past-date schedule guard (playbook #7)
> does this for the postponed-game case; the status-lag-sliver case is still open
> (it only ever costs a late, cosmetic climb to 100%, never the result).

> **Investigation discipline (2026-07-09, after a run of misdiagnoses).** The
> operative rules live in the **`wp-investigate` skill** (procedure + output
> contract) and `scripts/wp_diff.py` does the decomposition mechanically —
> follow them. The load-bearing facts, kept here for reference:
> - **Salient ≠ causal**: weigh every simultaneous delta; a category that
>   flips leaders dominates within-lean shifts (the Norsemen −17pp drop was
>   ~half a bullpen-K flip, not "Trout benched"). A removed player's impact is
>   his **marginal** value — the optimizer backfills ("Trout recovers ~15pp"
>   was really ~0.4pp).
> - **Slot facts** (verify in code, cite the line): `IL_SLOT=17`, bench=16;
>   `_hitter_days_slotted` ignores the manager's bench (a BE hitter is still
>   slotted). IL slot is **not** a blanket exclude (`_is_playable`, `sim.py`):
>   an IL-slotted player with a return estimate (`*_DAY_IL/DL`) or playable
>   status is included and gated per-game by `_est_return_date`; only
>   `OUT`/`INJURY_RESERVE` (no return estimate) are dropped. So a still-IL-slotted
>   pitcher with a near return date DOES project (owner-confirmed 2026-07-22 —
>   see the IL-handling section). `IL-slot + ACTIVE` = just-activated, projected
>   from the next game day.
> - **Retention**: historical rosters/schedule are overwritten and
>   `details_json` budgets are display summaries — **a past tick cannot be
>   re-simmed**; give estimates labeled as estimates.
> - **Under pushback, re-derive the specific point** — a peripheral correction
>   (a date) doesn't refute a verified mechanism; don't panic-recant.
> - **Log the outcome in `INVESTIGATIONS.md`** — the feedback loop that shows
>   whether this process is actually holding.

## Operations

### Running manually

```sh
cd /Users/mpozar/git/fantasy-wp
# Wait for any active cron jobs to finish
while pgrep -f 'scripts/(fast|medium|daily).sh|app (fetch|compute|publish|refresh)' >/dev/null; do sleep 3; done
# Then run with the lock
echo $$ > .app.lock
trap 'rm -f .app.lock' EXIT
.venv/bin/app fetch && .venv/bin/app compute && .venv/bin/app publish
```

### Measuring projection accuracy (start-of-week calibration)

```sh
.venv/bin/python scripts/calibration.py
```
Read-only. Compares each settled week's **pre-play** category projection (the
last snapshot before the period's first pitch) against the settled actual, for
the seven counting cats, with 90% CIs bootstrapped **clustered by week**.
Reports per-category bias, a per-week table (so a regime shift stays visible),
a trend slope (a span/denominator bug *grows* over the season; a rate bias is
flat), a **unit-free ratio test** that cancels lineup-days to separate a
units bias from a per-category rate bias, and a **rate-category section** with its
own aggregation (a ratio can't be summed).

**Rate cats — measured for the first time 2026-08-10; they had been excluded on a
false premise (see playbook #11). Level bias is small; DISCRIMINATION is the
problem:**

| cat | proj | actual | level | sd(proj)/sd(actual) | corr |
|---|---|---|---|---|---|
| OPS | .782 | .767 | +0.015 | 0.13 | 0.14 |
| ERA | 3.607 | 3.850 | −0.243 | 0.11 | 0.04 |
| WHIP | 1.199 | 1.215 | −0.016 | 0.18 | 0.06 |

No bias fix is warranted here (unlike QS/SVHD) — but the projections barely vary
(11-18% of the outcome's spread) and are **near-uncorrelated with the result**.
Interpret carefully: a conditional mean *should* be less variable than the
outcome, and weekly team ERA over ~50 IP is genuinely noisy, so even a perfect
forecast with this spread would only reach corr ≈ sd(proj)/sd(actual) ≈ 0.11 —
the observed 0.04-0.14 is within sampling noise of that (SE ≈ 0.10 at n=108).
**Hypothesis, unconfirmed:** the spread itself is too small — real rosters differ
in pitching quality by more than 0.12 ERA points — which would mean the model
homogenises teams. Telling "honestly weak forecast" from "under-dispersed
forecast" needs the per-category win% calibration, not this table.

**Findings as of 2026-08-10 (periods 10-18, 108 team-weeks per cat) — every
counting category is over-projected:** H +8.3%, R +7.1%, HR +2.5%, SB +19.6%,
K +18.4%, QS +40.5%, SVHD +55.1%. The hitter side decomposes *exactly* into a
shared **~+8% lineup-days (units) over-projection** × a per-cat rate error
(R/H −1.0%, HR/H −5.1%, SB/H +10.5%). Open hypotheses for the units bias:
`_hitter_days_slotted` uses an *optimal* matching and ignores the manager's
bench, so it assumes daily lineup optimization real managers don't do; plus the
deliberate over-credits (BE-slot pitchers, IL-slotted players with return
dates). **Why WP barely notices:** the bias is one-directional on *both* sides,
so it largely cancels in a head-to-head category comparison — which is why
fixing the ×1.76 RP-appearance inflation moved week-19 WPs ≤2pp. It bites via
roster asymmetry and via the rate cats (innings move ERA/WHIP denominators
non-linearly). Details + caveats in `INVESTIGATIONS.md` (2026-08-10).

**Do NOT read those numbers as today's bias.** They score the model *as it ran*,
so the two biggest 2026-08-10 fixes are still baked in — and re-running the tool
cannot remove them, because `player_projections` has no period key and the
weeks cannot be re-simmed. For **SVHD in particular the headline +55.1% is
mostly the since-fixed RP denominator**: dividing each week's projection by that
week's measured inflation factor `G(w..season end)/G(w..last_reg)` (1.218 in
week 10 rising to 1.585 in week 18) leaves an estimated **+15.2% residual, and
the growing trend disappears** (per-week residuals 32/18/6/4/7/36*/11/11/8%,
* = the 2-week All-Star period). That estimate is a *lower* bound on what
remains — it divides the whole projection by the factor, so where the
`rp-apps-capped` backstop bound, the true post-fix number is higher. The
QS trend was already flat (−0.025/wk), consistent with the current week using
the cadence model rather than the flat ROS share, so its +40.5% was genuinely
rate-driven. For the *pitching* cats the companion decomposition is
`scripts/analyze_starts.py` below — read those biases against **credited**
starts, not projected ones.

### Measuring tail calibration (are extreme category probabilities honest?)

```sh
.venv/bin/python scripts/tail_calibration.py
```
Read-only, ~35s. The companion to `calibration.py`: that one asks whether a
projected *total* is the right size, this one asks whether the *probabilities*
built from those totals survive contact with the tail. Measurement lives in
`app/tail_calibration.py` (shared, per the `INV_SITE_QS_OVERCREDIT` lesson);
`scripts/` is the report. Tests: `tests/test_tail_calibration.py`.

Why it exists: a level bias cancels head-to-head **only when both teams carry
similar volume**, so the same bias reads as a benign +40% on the level metric
while flipping the *sign* of a projected margin. Nothing else in the battery
sees that.

Four outputs, each isolating a different defect:
- **Reliability curve** — is a stated 1% actually 1%?
- **Margin slope** — settled margin regressed on projected margin through the
  origin. Below 1 = the model claims a bigger gap than materialises
  (over-differentiates teams; the *between-matchup* defect).
- **Dispersion** — errors expressed in units of the sim's own sigma. `typical`
  (robust) is the width of the middle; `|z|>2` is the tail, and a correct model
  shows 4.6%. `typical ≈ 1` with `|z|>2` far above 4.6% means a heavy tail, not
  a wide middle — read the two together, never `typical` alone.
- **Comeback units** — every category won from under 5%, flagged for churn.

**Roster churn is separated, not assumed away.** Projections condition on the
current roster, so a mid-week streaming add is a documented limitation, not a
model error. A forecast is churn-exposed when that side later gained a budget
entrant of the relevant type **never seen in this matchup's budgets before** —
so an IL activation or a player dropping out and returning does NOT count
(guarded by tests). The filter still excuses a forecast whether or not the
newcomer produced, so churn-free columns are a **lower bound** on model error.

**Findings as of 2026-08-10 (periods 10-18, 1.87M in-week forecasts, 1080
units).** Calibration is fine everywhere above 5% (5-10% → 1.01×, 10-20% →
1.06×) and **2.8× overconfident below it**: p<5% forecasts win 3.49% of the
time against a stated 1.26%, ratio **2.77, 90% CI [1.55, 4.17]** clustered by
matchup. Churn explains only part of it — 11 of the 38 comebacks, and the band
only moves to **2.57 [1.18, 4.19]**. Churn-free residual by cat: **SVHD 8.3×,
QS 5.1×, ERA 4.9× (2 episodes), SB 2.5× (1 episode)**; **K collapses 1.08 →
0.38 — K's entire tail problem was churn.** Margin slopes: **K 0.63 [0.55,0.71],
QS 0.57 [0.36,0.76], SVHD 0.58 [0.47,0.71]** (all exclude 1.0) vs 0.81-0.99 for
every hitting cat and WHIP. The `gap / team total` column is the mechanism:
H 10%, R 9%, K 19%, QS 20%, SB 35%, **SVHD 42%** — hitting volume is near-uniform
so the bias cancels, pitching volume is not so it scales the gap.

**Read the numbers with two caveats.** They score the model *as it ran*: QS and
SVHD predate the 2026-08-10 rate blends and cannot be re-simmed, so those two
are pessimistic while **K is current** (nothing was applied to it, and its bias
is start-count driven, not rate driven — see the start decomposition below).
And the reliability curve is snapshot-weighted, so one long-lived tail state
contributes hundreds of rows; the unit table is the de-duplicated restatement
and the CIs are matchup-clustered. Full write-up in `INVESTIGATIONS.md`
(2026-08-10, two rows).

### Is the published WP calibrated? (`scripts/wp_calibration.py`)

```sh
.venv/bin/python scripts/wp_calibration.py
```
`calibration.py` measures the model's *inputs*; this measures its *output* — when
the site says 70%, does that side win 70%? Two levels: **matchup** (n=54, home
side only — `away_wp ≈ 1 − home_wp`, so scoring both double-counts and shrinks
every CI by √2) and **category** (n=540, clustered by matchup, ~10× the power and
it localises the problem). Ties get half credit on both sides of the comparison.
Reports skill vs always-50% (a Brier score alone is meaningless), AUC, sharpness,
a reliability table, and the calibration slope — **slope < 1 = overconfident
(too extreme), slope > 1 = too timid**.

**Findings 2026-08-10 (periods 10-18, the model AS IT RAN):**

- **Matchup level is fine.** Brier .2127 vs .2500 baseline ⇒ skill **+0.149** [CI
  +0.015, +0.286], AUC **0.693** [0.567, 0.812], calibration slope **+1.00**. The
  headline percentage genuinely beats a coin flip and isn't systematically
  mis-scaled. (Intercept −0.075 and mean forecast .483 vs a .407 home win rate is
  **not** a model bias — home/away is arbitrary in fantasy, and 0.407 is ~1.4 SE
  from 0.5 at n=54. Season-wide it's 50 HOME / 58 AWAY.)
- **Category level is OVERCONFIDENT.** Slope **+0.673**, counting cats pooled
  **+0.67 [CI +0.517, +0.818] — the CI excludes 1.0.** The reliability table shows
  it plainly: the 80-100% bin predicted **.910 and observed .739**; the 0-20% bin
  predicted .092 and observed .210. Per-category win probabilities are too extreme.
- **SVHD scores worst (skill −0.301) while being the MOST confident category
  (sharpness 0.332, slope +0.27)** — of 16 forecasts at a mean stated 96%, it went
  7 W / 3 T / 6 L. QS (−0.035) and HR (−0.013) are also net-negative, and H
  (+0.303), K (+0.286) and SB (+0.258) carry all the real skill. **But do NOT read
  the SVHD/QS numbers as properties of today's model — see the era split below;
  most of that deficit is bugs that were fixed during the window.**
- **ERA SPLIT (`--split-at`, default period 14) — the interpretive control that
  matters.** Several correctness fixes landed *inside* the measurement window, and
  the ones that produced confidently-wrong output cluster early: Binomial-not-
  Poisson (06-07, the "2 QS from one start spuriously wins a locked category"
  bug), QS `max`-not-add (06-08), SVHD entry/exit margins + spot-starter skip
  (06-10), relief-SVHD smear (07-03). Splitting at period 14:

  | cat | skill pre | skill post | slope pre | slope post |
  |---|---|---|---|---|
  | SVHD | −0.527 | **−0.125** | +0.03 | **+0.41** |
  | QS | −0.162 | **−0.015** | +0.00 | **+0.37** |
  | HR | −0.109 | −0.052 | +0.04 | +0.32 |

  A slope near **zero** means the forecast carried *no information whatsoever* —
  that's what the early SVHD/QS eras look like, and it's the signature of a
  correctness bug rather than a modelling weakness. Both roughly halve their
  deficit afterwards. So **the headline −0.301 substantially measures dead bugs.**
- **The general overconfidence, however, is NOT an era artifact:** pooled slope
  **+0.63 [0.41, 0.84] pre vs +0.65 [0.37, 0.91] post** — unchanged, and still
  excluding 1.0. That finding survives, so the under-dispersed-Binomial hypothesis
  stays live for the residual. (n≈24 per cell in the split: directional only.)
- **Even the "post" era predates today's fixes.** The RP-appearance denominator
  was live for *all* of weeks 10-18 and grew, and because it inflated relievers in
  proportion to how many a team rostered it was **asymmetric across rosters** —
  a textbook mechanism for confidently favouring the wrong side in SVHD. Together
  with the QS/SVHD rate blends, expect the true current-model numbers to be better
  than even the post-era figures.
- **Rate cats are honestly weak, not fixably timid.** Sharpness 0.041-0.067 (they
  sit at ~50% always) and skill ~+0.013 — near-zero information — but slopes
  ~1.0, so they're not mis-scaled. Pooled slope +0.90 with CI [−0.32, +2.01] is
  far too wide to conclude, so the under-dispersion hypothesis from the rate-cat
  table is **neither confirmed nor refuted**; it just doesn't cost much either way,
  since a category stuck at 50% barely moves the matchup WP.

**Hypothesis for the overconfidence, code-grounded but unconfirmed:** the two
worst categories are exactly the two sampled with a **deliberately
under-dispersed** distribution — QS and SVHD are `PER_EVENT_CAPPED` → Binomial
(see "Variance / overdispersion"), chosen to stop Poisson returning 2 QS from one
start. Under-dispersed sampling ⇒ team totals too tightly concentrated ⇒ the
winner looks more determined than it is. Teammate independence (a known
limitation) pushes the same way for low-event cats. **Testable prediction:** the
2026-08-10 RP-denominator + QS-rate + SVHD-rate fixes should move SVHD/QS skill
upward; re-run this after ~6 post-fix weeks settle before touching the sampling.

### Decomposing the SP start over-projection

```sh
.venv/bin/python scripts/analyze_starts.py
```
Splits projected SP starts against (a) starts those pitchers actually **made**
and (b) starts actually **credited** (active pitching slot that day — the
accounting ESPN's banked totals use, so the one `calibration.py` biases should be
read against).

**Findings 2026-08-10 (periods 11-18, 96 team-weeks): projected starts run
+23.7% above credited, and the gap is an almost exact 50/50 split of two things,
NEITHER of which is a general bug:**

1. **Slot attribution, ~10.6% of real starts (74 of 696) — DELIBERATE**, owner
   call 2026-08-10. A bench/IL-slotted pitcher is projected at **full weight**:
   the model assumes every manager activates a benched starter when he should, so
   a team's WP is never penalised for a neglectful owner. Measured activation
   rates, if this is ever revisited: prior slot active → 97.8% (453/463), bench →
   74.9% (125/167), IL → 8.3% (1/12). An activation-probability discount was
   built and **deliberately reverted** — don't re-add it without a new decision.
2. **The All-Star fortnight.** Rotation error is **+3.9% (90% CI [+1.7, +6.3])
   in normal weeks** — about as good as rotations are forecastable (rainouts,
   mid-week IL moves, skipped turns) — but **+43.9% in period 15**. Root cause is
   visible in the schedule: a `LONG_MATCHUPS` period has 14 calendar days but only
   **11 game dates, 0.68 games/team/day vs ~0.90** normally. The break is ~3
   gameless days that both rotation models (`_cadence_extra_start_dist`'s
   rest-day walk and the flat `MAX_SP_RATE × open_weight` share) walk straight
   through, so a fortnight projects ~2.3 turns per starter against 1.57 actual
   (deGrom 2.98→1, Misiorowski 3.00→1). **Known limitation, not yet fixed** —
   it costs nothing for the rest of 2026 (only period 15 is long) and can't be
   validated again until July 2027.

**Why this closes the pitching-bias accounting.** Read against *credited* starts
(+23.7%), the category biases decompose cleanly into units × rate:
K's +18.4% total ⇒ per-start K rate ≈ **−4%** (ESPN's K rate is fine), and QS's
+40.5% ⇒ per-start QS rate ≈ **+13.6%**, which is exactly what the QS blend
removed (−14%). So after that fix the residual pitching over-projection is
essentially all item 1 — i.e. as calibrated as the modelling choices allow.

### What-if playoff odds ("what does this one game swing?")

```sh
.venv/bin/python scripts/whatif_playoffs.py 115                       # both outcomes
.venv/bin/python scripts/whatif_playoffs.py "Desert Dawgs"            # their next undecided matchup
.venv/bin/python scripts/whatif_playoffs.py 115 --winner "Desert Dawgs"
```
Pins one undecided matchup to a result and re-prices the season. `app playoffs`
says where things stand; this says what a single game is *worth* — which is a
different question, because a result's value to a team is mostly about which
**rival** it eliminates. Worked example (2026-08-14, m115 Dragons @ Dawgs): a
Dawgs win is worth only **+11.0pp** to them but **−34.5pp** to the Dragons, and
the biggest gainer is neither side — **Big Giraffes +12.6pp**, the team actually
contesting that spot. A Dawgs loss eliminates them outright (2.4% → 0.0%).

**READ-ONLY by design** — never writes `docs/playoffs.json`, never inserts into
`playoff_odds_runs`. That second one is the load-bearing part:
`playoffs.load_odds_history` rebuilds the published odds-over-time chart from
that table, so one stray hypothetical row would put a fictional kink in it.

Two seeding properties, both needed for the deltas to mean anything:
**paired arms** (each arm re-runs `simulate_odds` with a fresh `Random(seed)`;
`simulate_odds` consumes one `rng.random()` per remaining matchup regardless of
its WP, so pinning one does not desynchronise the stream and the ~±0.5pp of MC
noise cancels instead of landing in the delta), and a **seeded bracket sample**
(`sim.sample_team_totals` draws from the module-global RNG with no seed hook, so
unseeded the *baseline* drifts ~0.5pp between invocations — observed before this
was fixed). Deltas within a run are precise; absolute levels are still ~±0.5pp.

### Repairing past daily lineups

```sh
.venv/bin/app backfill-lineups --days 14 --dry-run   # report, then drop --dry-run
```
Re-pulls each past day's locked lineup from ESPN (per `scoringPeriodId`) and
corrects `daily_lineups`. `refresh-live` keeps current days right on its own;
this is for days recorded *before* that fix, or after any ESPN-auth outage
(`ANOM_LINEUP_SNAPSHOT_MISSING`). Only rewrites a day ESPN answers for, prints
every slot it changes, idempotent. A stale **active** slot is the one that costs
real money — it manufactures a QS/SVHD credit ESPN never gave.

### Re-measuring the SVHD-rate shrinkage constant

```sh
.venv/bin/python scripts/analyze_svhd_rate.py
```
Prints `SVHD_RATE_PRIOR_APPEARANCES` to paste into `app/espn.py`, both estimators
(prior-error and spread-based) with the reason they disagree, ESPN's current
level bias, and a squared-error back-test of the blend against the 15-appearance
cliff it replaced. Read-only; hits the ESPN API. See the `stat_id 83` section
under ESPN API quirks.

### Re-measuring the QS-rate shrinkage constant

```sh
.venv/bin/python scripts/analyze_qs_rate.py
```
Prints `QS_RATE_PRIOR_STARTS` to paste into `app/espn.py` — both estimators
(prior-error and between-player-spread) with the reason they disagree, ESPN's
current aggregate QS level bias and what the blend does to it at several K, and a
squared-error back-test against using ESPN's ROS rate as-is, which is what
actually justifies a chosen K. Read-only; hits the ESPN API. See the `stat_id 63`
section under ESPN API quirks — including the two defects this script had until
2026-08-10, neither of which is safe to re-introduce.

### Re-measuring variance

```sh
.venv/bin/python scripts/analyze_variance.py
```
Pulls fresh MLB game logs, prints a `VMR = {...}` dict. Paste into `sim.py`. Re-run yearly or when the model seems off.

### Re-measuring rotation cadence

```sh
.venv/bin/python scripts/analyze_cadence.py
```
Pulls fresh MLB pitching game logs, prints a `REST_DAY_WEIGHTS = {...}` dict (the
distribution of calendar-day gaps between a SP's consecutive starts). Paste into
`sim.py`. Drives the SP cadence model's turn projection — re-run yearly or if the
rest-day mix drifts (e.g. league-wide six-man rotations). Self-contained; needs
no local DB. Seed anchors once with `app backfill-starts` (then `refresh-schedule`
keeps `pitcher_starts` current going forward).

### Tests

```sh
.venv/bin/pip install -e '.[dev]'   # once: installs pytest
.venv/bin/python -m pytest -q
```
Covers `app/ingame.py` (in-progress QS/SVHD), `app/sim.py` cadence + extra-start
sampling (`test_cadence.py`), the category_state monotonicity guard
(`test_category_guard.py`), and the validation checks (`test_validate.py`).

**Golden-week end-to-end test** (`tests/test_golden_week.py`): runs the real
`compute` → `publish` → `validate` pipeline against a slimmed snapshot of a real
week (`tests/fixtures/golden_week.db.gz`) with the app clock frozen at the
snapshot's newest timestamp, asserting zero error-severity validation findings —
plus a negative control that re-creates the 2026-06-04 dropped-scored-cats
corruption and asserts the battery flags it. This is the pre-commit guard for
the *emergent* bug class (plumbing changes that pass unit tests but break the
end-to-end output). Regenerate the fixture with
`.venv/bin/python scripts/make_golden_fixture.py` after schema changes, at
season start, or ideally during a live slate (richer in-game paths). The test
monkeypatches `db.DB_PATH`, `cli._now_iso`, `cli._settle_boundary`,
`sim._utc_today`, and `cli.DOCS_DATA_JSON` — a new wall-clock read on the
compute/publish/validate path typically surfaces here as the past-date guard
dropping the fixture's schedule (INV_EMPTY_BUDGETS).
`scripts/ingame_scenarios.py` prints projections for hand-built in-progress states;
`scripts/ingame_spotcheck.py` does the same for *live* rostered pitchers from the
current DB state (read-only, safe to run mid-slate) — a quick reality check on the
in-game QS/SVHD model during real games.

### Verifying front-end (`docs/`) changes

There is **no `node`/JS toolchain installed** — you can't `node --check` or lint
`app.js`. Verify front-end edits **in a real browser** against a static server on
`docs/` (the page just fetches `data.json` + `annotations/*.json` relatively):
- Serve `docs/` (e.g. `python3 -m http.server --directory docs`, or the Preview
  MCP `fantasy-wp` launch config). 
- Then **check the console for errors** (a syntax slip shows up only at load) and
  drive the UI: switch weeks/scopes, expand a matchup's **Details**, toggle
  **✦ Annotate**, hover/click chart points + annotation markers. Inspect the DOM
  (e.g. confirm `.annot-top` is the last SVG child, tooltips populate) rather than
  trusting a screenshot — screenshots here are flaky for scroll/layout.
- **Always bump the `?v=N` cache-bust** for `style.css` / `app.js` in `index.html`
  on any UI change (see Style notes) or the live site serves stale assets.

### Validation / anomaly flags (`app validate`) — what it is

`app/validate.py` runs cheap **invariant + anomaly checks** (no sims). Why it
exists: nearly every bug in this repo has been *emergent* — a fetch/plumbing change
breaks what the sim consumes, so per-function unit tests still pass while the
end-to-end output goes wrong (the dropped rate-components → an 8.37 ERA projecting
3.76 failed *no* test; a human just eyeballed it). These checks assert
output-level properties at exactly that layer, and `fast.sh` runs `app validate`
every 5-min tick (cheap, non-fatal), upserting findings into `validation_flags`
(deduped per `code + matchup_id + flag_date`, with an `occurrences` count).

**The battery's one structural blind spot (closed 2026-08-10).** Every check below
is a *change* detector, a *freshness* detector, or an internal *consistency
invariant*. None of them asked "was the projection **right**", so a large-but-stable
bias in the model's inputs fired nothing at all — the RP-appearance inflation ran a
whole season (growing to ×1.76) and the QS rate ran +28% with **zero flags**.
`ANOM_CALIBRATION_JUMP` (daily tier) is the accuracy dimension: projected vs settled
actual, per category. When adding a check, ask which of these four kinds it is — if
it only reads the model's own output, it cannot see this class.

**Four scopes of check** (the 2026-06-04 incidents — see `INCIDENTS.md` — drove the
last three):
- **per-matchup** — properties of one matchup's latest snapshot + current_state.
- **league-level** (`_LEAGUE_CHECKS`, over *all* matchups in one tick) — a
  *correlated* swing across many matchups is a systemic-data fingerprint, which no
  per-matchup check can see. Scoped to active (`UNDECIDED`) weeks.
- **pipeline freshness** — newest snapshot/fetch too old ⇒ a cron died silently and
  the site is serving stale data. Needs `now`.
- **published site** — reads the actual `docs/data.json` the site renders; a
  started week with no scored-cat values = "no stats showing on the site". Needs the
  data.json path. Both wired from `validate_cmd`.

Plus two **fetch-time** checks (called from `fetch`, not `run`, and persisted via the
shared `validate.persist`) — they live at fetch because only `fetch` knows a scrape was
attempted; `run` can't infer it after the fact:
- `check_scrape_health` — a live-games scrape that silently returned **nothing** →
  `ANOM_SCRAPE_EMPTY`.
- `check_scrape_staleness` — a live-games scrape returning a **full set of cells that
  never change** → `INV_SCRAPE_STALE`. Takes `conn` (a frozen run is only visible
  across ticks). This is the failure the first check can't see, and the more dangerous
  of the two: an empty scrape degrades loudly, a frozen one is invisible and poisons
  the rate reconstruction too. See its row in the flag reference.

Two severities: **error** = an invariant that must never hold (almost certainly a
bug); **warn** = an anomaly that's unusual but may be legit (eyeball it).

**Design rule (learned the hard way):** a check's "should I fire?" gate must not
depend on the data that can go missing — gate on something orthogonal (e.g. OUTS, a
component a partial fetch still writes), or the check goes silent exactly when
needed. And treat a flagged anomaly as *investigate-first*: a correlated swing +
"no stats on site" is systemic corruption, never "probably benign."

### Triage runbook (safe to do from a cold start)

1. **See what's open:** `app validate --list` → lists unresolved flags
   (`[severity] CODE mNN ×occurrences  detail  (since …)`).
2. **Investigate each flag** using the table below — figure out *bug vs legit*.
   The general method is the WP-change investigation under "Investigating WP
   changes": pull the matchup's snapshots, diff budgets / category_state /
   probables across the relevant tick. For data shape, recall `category_state`
   for the **current period** is split-sourced (scrape owns the display cats;
   REST fills raw components ER/OUTS/AB/…) — see "Live data freshness".
3. **Resolve the outcome — always with a `--note`** (the conclusion is the durable
   artifact; a bare resolve loses it and the next investigator re-derives or, for
   "who/when", *can't* recover it):
   - *Real bug* → fix it, add a regression test in `tests/test_validate.py`, then
     the next compute clears the flag (or `--resolve` it).
   - *Legit one-off* (e.g. a genuine 18pp WP swing from a real roster move) →
     `app validate --resolve CODE --note "why it's benign"` to dismiss the open
     instances. Records `resolved_at`/`resolved_by`(=$USER)/`resolution_note`.
   - *Legit recurring* (the check is too strict, like the All-Star period below)
     → **tune the check** in `app/validate.py` (raise a threshold / add an
     exception) and update its test; don't just keep resolving it daily.
   - `app validate --resolve all --note "…"` clears everything (use sparingly).
4. **Audit closed flags:** `app validate --resolved` → recently-resolved flags with
   their provenance (who/when/why), so a cold chat reads the prior triage instead of
   redoing it. (Pre-2026-06-05 resolves predate provenance → shown as "unknown".)
5. **On-demand sweep:** `app validate --all` re-runs every period (not just the
   current one) — good for a full audit or after a model change.

### Flag reference (code → meaning → how to read it)

| Code | Sev | Asserts | Likely a bug when… |
|---|---|---|---|
| `INV_RATE_COMPONENTS_MISSING` | error | current_state has ER+OUTS once a week's underway | almost always — the sim can't blend current innings into ERA/WHIP (the 3.76 bug). Check `fetch`'s current-period write split. |
| `INV_CURRENT_CATS_MISSING` | error | once a side has pitched (OUTS banked), all 10 *scored* cats present in current_state | almost always — a partial current-period write read as complete (the 2026-06-04 evening idle-fetch drop → WP collapses to 50/50, blank site). Check the per-`(matchup,team,stat)` read in `load_latest_state` / `_latest_score*`. Gated on OUTS (survives the drop) not the counting cats (which don't). |
| `INV_PROJ_LT_CURRENT` | error | projected end counting total ≥ current banked | always — the sim isn't seeding current_state. |
| `INV_WP_RANGE` / `INV_WP_SUM` | error | WP ∈ [0,1]; home+away ≤ 1 | always — arithmetic/derive bug. |
| `INV_SP_UNITS_CAP` | error | SP starts ≤ ~2.3 per 7 days (scaled by period length) | bug *if* a normal 7-day week; **legit for long periods** (All-Star = 14 days → ~2.3 real). The cap is already period-aware. |
| `INV_NEG_UNITS` | error | no negative budget units | always. |
| `INV_BANKED_REGRESSED` | error | a banked counting cat can only go up within a week | banked totals were lost (a dropped scrape line, a stale source the write-guard couldn't un-regress, a partial-write read artifact). Ignores ±1-type ESPN corrections; fires on real loss (the incident halved totals). |
| `INV_RATE_RANGE` | error | derived ERA/WHIP/OPS (current or projected) within physical bounds | always — a derivation blowup (div-by-zero, missing components). Coarse backstop; the 3.76 bug stayed *in* range, so this won't catch that — `ANOM_RATE_DIVERGENCE` does. |
| `INV_CAT_SIM_COUNT` | error | each category's & the matchup's home+away+ties == n_sims | always — the sim's win-counting is broken; every WP off it is suspect. |
| `INV_EMPTY_BUDGETS` | error | an active matchup's side has ≥1 player budget | roster/projection fetch produced nothing → WP degenerates. Scoped to active (`UNDECIDED`) weeks. Skips two benign cases: a finished week (winner set), and **end-of-week** — a side with a *fetched* roster (`team_rosters` rows) but **0 remaining active games** (all active players Final, only IL/bench left → nothing to budget; decided-but-UNDECIDED Sun→Mon). Still fires on a real failure: no roster fetched, or active games remain with no budgets. Gating via `load_view`'s `{side}_roster_n` / `{side}_active_remaining`. **`_side_remaining` must exclude every game `build_budgets` excludes** or the gate mis-fires. Three filters: schedule loaded **with `now`** (past-date guard on, matching `cli.compute`), unplayable players skipped via `sim._is_playable` even in active slots (both 2026-07-29, after 83 spurious errors on the 2026-07-26/27 Sun→Mon drain), and — added 2026-08-11 — **games already underway (`current_inning is not None`) don't count**. That third one is the one people miss: the check recurred twice more after the first fix, and the 2026-08-09/10 case (m104 home, m106 away, 37 occurrences) was **not an end-of-week drain at all** — it fired 23:36→02:36 mid-slate and stopped at the last tick before game 823268 went Final. Each side had exactly ONE active player left in that live game (both relievers), while every other active player's game was already Final; the sim gives an underway game a *factor* and `_rp_factor` ramps to ~0 deep in the bullpen window, so those players were legitimately dropped and the side projected nothing. An underway game is exactly where the sim decides per-player whether anything remains (removed hitter, exited starter, reliever past the window) and zero is a valid answer, so the gate only counts games that **haven't started**. Still caught: `roster_n == 0`, and empty budgets with a not-yet-started game. Now missed: a genuine projection failure occurring when *only* in-progress games remain. Tests: `test_side_remaining_*`. |
| `INV_SETTLED_RATE_DRIFT` | error | once a side has **nothing left to play** (`{side}_unfinished == 0`), its projected ERA/WHIP/OPS equals its banked (scraped) value | **always — the model is deciding a category on a reconstruction instead of ESPN's number.** Detector for the 2026-08-24 m120 incident: derived OPS 0.6387 vs scraped 0.6491, and because that 0.0104 error sits inside `LIVE_RATE_TOL` (0.012) `_judge_group` called it `matched` and committed it. With the Bus on 0.6448 the 0.0043 margin was **smaller than the error**, so OPS — the deciding category — flipped and the site published a 100%-confident WRONG winner for **4h40m**, until the 07:00Z settle. **No other check could see it**: every one of them is a change, freshness or internal-consistency test, and this state was stable, fresh and internally consistent — just wrong. This is the only check comparing the model against the authoritative scrape. Gated on `unfinished`, **not** `active_remaining` — the latter excludes in-progress games (see `_side_remaining`) so it reaches 0 while a game is still being played, when a gap is legitimate. That instance's root cause was a `_norm_name` collision, fixed separately; this is the backstop for the whole class, since ERA's tolerance (0.20) and WHIP's (0.04) are coarser still |
| `INV_SP_STARTS_IMPOSSIBLE` | error | an SP's projected `units` fit between his last recorded start and the period end at `MIN_REST_DAYS` (5) spacing | **almost always — this is the gap `INV_SP_UNITS_CAP` cannot see.** That one is a blunt ~2.3-starts/7-day ceiling; the 2026-08-13 case projected exactly **2.0** for a pitcher with room for **1** and sailed under it. Root cause is two announced probables closer than min-rest (a tentative ESPN-overlay entry alongside the real MLB one): the sim's `_cap_extra_dist` folds only the *cadence* piece and **always respects announced starts** by design (2026-06-26 Rasmussen fix), so nothing clips the sum. Reuses `sim._max_remaining_starts` rather than recomputing the bound — a second implementation is what made `INV_SITE_QS_OVERCREDIT` drift and cost 1830 false flags; scope is "was the cap bypassed", not "is the bound right". **Current period only** (matches `use_cadence`, the only place the sim applies the cap); skips a pitcher with no recorded start. A stale anchor loosens the bound, so it under-detects rather than false-fires. **To triage: check `team_schedule` IMMEDIATELY** — it is current-state, so once the duplicate probable is overwritten the evidence is gone (that is why the 08-13 instance could only be reproduced synthetically). Grew more load-bearing on 2026-08-18 when `refresh-live`'s wider window took `espn_probables_filled` from ~27 to ~97 per tick. |
| `INV_WP_DETAILS_MISMATCH` | error | `home_wp`/`away_wp` column == `details_json` tally (`wins/n_sims`) | always — they're the same sim; a gap means a publish/compute bug or an **unlogged** hand-edit. Hand-smoothed rows are marked `wp_snapshots.edited=1` and skipped (their divergence is intentional — see INCIDENTS.md). |
| `ANOM_CORRELATED_SWING` | error | <`MIN_CORRELATED` (3) active matchups swing ≥10pp in one compute | **almost always systemic** (banked-loss, partial fetch, read collapse) — both 2026-06-04 incidents. Detail reports how many moved toward 50/50 (the collapse signature). Rare false trigger only if a busy live slate genuinely moves many at once — check whether the moves are gradual/real vs a single wholesale jump. |
| `ANOM_STALE_SNAPSHOTS` / `ANOM_STALE_FETCH` | warn | newest wp_snapshot / category_state fetch < `STALE_MINUTES` (20) old | a cron stalled (wedged lock, exception, macOS FDA revoked) — site serves stale data. Legit briefly while `medium.sh` holds the lock (≤5 min). |
| `INV_SITE_MISSING_SCORES` | error | each started (live/final) week's matchup blocks in `data.json` carry all scored cats | the published artifact is missing stats — "no data on the site". Pairs with `INV_CURRENT_CATS_MISSING` (DB cause) but checks the actual output. |
| `INV_SITE_MISSING` / `INV_SITE_UNREADABLE` | error | `data.json` exists and parses | publish never ran / wrote garbage. |
| `INV_SITE_DB_MISMATCH` | error | each live-week published score == the DB's banked value | publish transform bug / corrupted artifact — the site shows a *different* number than the DB. Compared against `category_state` **as of `data.json`'s `generated_at`** (what publish read), so a later fetch can't cause a false mismatch. Live week only. Skips only the derived rate cats (ERA/WHIP/OPS), guarded by `INV_RATE_RANGE`. **Since 2026-08-11 it also covers QS/SVHD**, which are no longer reconstructed — published must equal ESPN's `category_state` exactly, a stricter test than the deleted `INV_SITE_QS_OVERCREDIT` recompute. |
| `INV_SITE_QS_OVERCREDIT` | error | published QS/SVHD ≤ independently-recomputed `max(scrape, settled_floor + box)` | the site shows a **phantom counting credit** — the QS/SVHD double-count vs the live scrape (the 2026-06-07 deGrom case: site QS 4 where only 3 is supportable). Recomputed from raw `category_state` + box scores (a second implementation, so it catches a regression of the `max`-rule fix, not just publish drift). Live week only; best-effort (skips if the recompute can't load). **The recompute must consult the settled floor even when `box == 0`** — publish applies the floor in *two* places (the fold's `max(scrape, floor+box)`, and independently `cli._apply_qs_svhd_floor`'s `max(scrape, floor)` on every current-week publish). `_supported_credit` originally modelled only the fold and short-circuited to `scraped` when there was no in-window box credit, so a credit resting purely on the floor read as unsupportable: **1830 flag-occurrences over 2026-07-30..08-07**, judged false positives at the time and silenced on 2026-08-07 by teaching `_supported_credit` to consult the floor (regression tests `test_site_qs_overcredit_quiet_when_credit_rests_on_the_settled_floor` + the still-fires-above-the-floor counterpart). **⚠ That triage was wrong, established 2026-08-10.** The reasoning was "the floor genuinely held Bryan Baker's 8/04 save"; ESPN's own `mRoster` for that scoring period has Baker **benched**, ESPN never credited the save, and the site was showing a real phantom (m105 SVHD 4 vs ESPN's 3). The check was firing correctly and we taught it to stop. Two lessons, both expensive: (a) **1830 was never 1830 distinct problems** — flags carry an `occurrences` count and m105 alone was 185+233+117 ticks of the *same* credit, so a big number meant "long-lived", not "widespread"; count **distinct (code, matchup, stat)** before concluding a check is noisy. (b) **Never resolve a QS/SVHD flag by reasoning about our own archive** — ask ESPN (`espn.fetch_all_matchups()` carries the settled per-period `scoreByStat`); it is the authority the site is judged against. The underlying phantom is fixed at source (per-scoring-period lineups), so the floor path is sound and the 2026-08-07 accommodation now costs nothing — but it is an accommodation, not a validation. Lesson that still holds: this check is a *second implementation* of publish's rule — when publish gains a new path to a displayed value, this recompute has to gain it too. |
| `INV_SITE_RECORD_ASYMMETRIC` | error | a matchup's two W-L-T records mirror (home W=away L, etc.) | always a bug — head-to-head category scoring is zero-sum. Was caused by summing per-team stored `result` flags that desync under temporal skew (fixed: `cli._apply_counting_results` derives results from a single home-vs-away score comparison). |
| `ANOM_SITE_STALE` | warn | `data.json` `generated_at` < `STALE_MINUTES` old | publish step failing while compute still runs. **Reads the LOCAL artifact** — it cannot see the published site, which is what `ANOM_DEPLOY_STALE` is for. |
| `ANOM_DEPLOY_STALE` | warn | the **published** site is not more than `pages.DEPLOY_STALE_MIN` (30) behind the newest local `docs/` commit | the deploy hop wedged and the guard **recovered it** (cancelled the blocking run); the next tick's push should land. Raised from `app pages-guard` in fast.sh, not from `validate.run` — only that step knows the deploy state (same precedent as `check_scrape_health` living in `fetch`). **This is the check that did not exist on 2026-08-31, when the site served 4-day-old data with zero flags**: every other check in this table reads local state, so `publish` + push succeeding while GitHub's Pages deploy is stuck was invisible. Staleness is judged on the deployed **SHA vs the newest docs-touching commit**, never on elapsed time alone — the cron laptop dark-wake-sleeps, and an idle stretch with nothing to deploy must not read as a wedge; comparing against the newest *docs* commit (not HEAD) also respects the workflow's `paths: ["docs/**"]` filter, so a code-only commit never looks permanently undeployed. |
| `INV_SITE_NOT_DEPLOYED` | error | when the published site is stale, the guard could fix it | **the site is frozen and the guard cannot recover it** — either nothing wedged was found to cancel (cause outside what it models: workflow disabled, Pages misconfigured, `GH_TOKEN` rotated) or a cancel was refused. The refusal is real, not hypothetical: the 2026-07-25 orphan run returned HTTP 500 "Failed to cancel workflow run" and had to be left in place. This is deliberately an error while the recovered case is a warn, and they are two codes rather than one escalating severity because `validate.persist` dedups on `(code, matchup_id, flag_date)` and does **not** update severity on conflict — one code would be pinned to whichever severity landed first that day. |
| `ANOM_SCRAPE_EMPTY` | warn | games in progress ⇒ the live scrape returns >0 cells | the DOM scrape silently failed (auth wall, expired `.playwright_profile`, selector drift) and `fetch` fell back to laggy REST — display cats rot *while games are live*. Raised at **fetch** time (not `run`), since only `fetch` knows a scrape was attempted. Fix: re-run `scripts/espn_auth_setup.py`. ×N occurrences/day = persistent vs a one-off hiccup. Blind to a scrape that returns a *full* set of frozen cells — that's `INV_SCRAPE_STALE`. |
| `INV_SCRAPE_STALE` | error | games in progress ⇒ the scrape-owned counting cats (H+R) move at least once per `SCRAPE_STALE_TICKS` (9 ≈ 45 min) | **the scrape is returning well-formed but STALE cells** — `ANOM_SCRAPE_EMPTY`'s blind spot, and error-grade because it degrades *silently* (an empty scrape shows 0 cells in the log; a frozen one looks perfectly healthy). 2026-08-05: ESPN's scoreboard renders an initial REST snapshot then applies live updates from a play-by-play feed (`site.api.espn.com/apis/fantasy/v2/games/flb/games?…pbpOnly=true`); Akamai began **403ing that request from the headless browser**, so the page never applied a live update and the DOM kept showing REST — whose weekly totals only advance at the ~07:00 UTC settle. H/HR/R/SB/K sat at the previous day's values through the whole slate and the day landed in one tick (m107 +35.9pp, six leader flips). **The rate cats go stale with them:** `sim._judge_group` judges the box-score reconstruction by distance to the *scraped* rate, so a stale scrape that exactly matches the equally-stale REST baseline makes every reconstruction look "further away" → `verdict: baseline` on every group. One frozen source takes down both input paths. Gated on an **open slate** (`game_day_activity.active_start`, + `SCRAPE_STALE_GRACE_MIN` 20 min, else it fires at every first pitch) and on a **tight cadence** (`SCRAPE_STALE_MAX_SPAN_MIN` 75 — a frozen run spread over a pipeline outage is `check_pipeline_freshness`'s job). Reads one indexed seek per (matchup, team, stat); do NOT rewrite it as a period-wide aggregate — that plans as a whole-index SCAN (2.5s on 2.6M rows, every 5-min tick). **The stall threshold SCALES with live games** (`scrape_stale_threshold_min`, 2026-08-10): `BASE_MIN`(45) × `REF_GAMES`(8) / games, clamped to [45, 240] — 15 games→45 min, 4→90, 2→180, 1→240. A flat 45-min window gated only on "any game in progress" **false-fired twice on 2026-08-09's 2-game tail** (23:20, 01:30–02:35) while the scrape was verifiably healthy: with 2 games, real H+R gaps were 95 min (1056→1057) and 55 min (1059→1060) — impossible on a full slate, routine on a tail. Two subtleties that cost a debugging round each: the samples being constant is **not enough**, the frozen span must itself reach the threshold (else a window that simply doesn't reach back far enough fires on a short flat run); and the fetch bound is widened by one `SCRAPE_STALE_TICK_MIN` because 5-min ticks inside `[now-45min, now]` span only ~40 min, so a strict `span >= 45` could never be met. Verify a change by **replaying both real nights**: silent on 08-03 (healthy) and on the 08-09 2-game tail, fires 08-04 23:55Z (broken, 16 games). **Cause addressed 2026-08-06** by the live-feed proxy (see "The live-feed proxy" under Live data freshness) — keep this check regardless: it is the regression detector for the whole class, and it is what will tell you if Akamai changes the rule again. |
| `ANOM_WP_SWING` | warn | |home_wp − prior| < 15pp/tick | usually **legit** — a roster move, probable announcement, ESPN stat backfill, games going live, or a benign ~07:00-UTC daily component settle (see "Live component reconstruction"). Bug only if no such cause (diff the budgets/state across the tick). **But** if *many* matchups swing at once, see `ANOM_CORRELATED_SWING` — that's systemic, not legit. |
| `ANOM_LINEUP_SNAPSHOT_MISSING` | warn | a day with live box-score lines has a `daily_lineups` snapshot | the ESPN lineup fetch in `refresh-live` failed (auth hiccup / expired cookies) → live component reconstruction can't attribute and silently falls back to stale ESPN components for every team. Check ESPN auth; verify `espn.fetch_daily_lineups` works. Quiet whenever nothing is live. |
| `ANOM_WP_FLAPPING` | warn | home_wp doesn't oscillate (≥`FLAP_MIN_REVERSALS` direction flips ≥8pp over the last `FLAP_WINDOW` ticks) | a stat keeps being written then dropped then rewritten (flaky scrape/source regressing a counting cat). Distinct from a one-way swing or a swing-then-recover (those don't reverse repeatedly). Active weeks only. |
| `ANOM_WP_RAIL_FLIP` | warn | home_wp doesn't touch BOTH rails (≤`RAIL_FLIP` and ≥1−`RAIL_FLIP`, i.e. near-0 AND near-100) within the window | a near-certain-win flipping to near-certain-loss is the worst UX and a fingerprint of a flaky/over-credited stat (the deGrom phantom-QS shape). Fires on *magnitude* (vs `ANOM_WP_FLAPPING`'s reversal count). A genuine decisive resolution can trip it too — hence warn. Active weeks only. |
| `ANOM_CALIBRATION_JUMP` | warn | the most recently settled week's **projected-vs-actual** bias hasn't jumped away from the recent norm, per counting category | **This is the only check that compares a projection to a realized outcome.** Every other row here is a *change* detector, a *freshness* detector, or an internal *invariant* — so a large-but-STABLE input bias is invisible to the whole battery by construction, and two real bugs proved it: the RP-appearance inflation ran all season growing to ×1.76, and the QS rate ran +28%, with **zero flags ever firing**. Daily tier only (`app validate --calibration` from `daily.sh`): retrospective, aggregate, reads `details_json` across every settled week, and its answer only moves when a week settles — running it per-tick would inflate `occurrences` into meaninglessness. Fires on a **jump, never on the level**: the level legitimately bakes in deliberate modelling choices (bench/IL pitchers projected at full weight ⇒ ~10.6% of starts, owner call 2026-08-10), so a level threshold would either fire forever (the `INV_SITE_QS_OVERCREDIT` failure mode) or be too loose to catch anything. The jump is scaled by each category's **own** week-to-week volatility (robust MAD), because the noise floor differs wildly — H moved in a ~5pp band while HR ranged −17%..+41%. `CALIBRATION_MIN_ABS` (0.15) is load-bearing, not belt-and-braces: MAD collapses at n≤7 when a category clusters (K's prior weeks 11,12,11 ⇒ MAD 1pp ⇒ 3σ≈4pp). **Both constants were set by REPLAYING periods 10-18** — zero false positives — so re-verify against history before retuning. `LONG_MATCHUPS` periods are excluded (a fortnight isn't comparable to a week, and it carries the known +44% rotation artifact; it fired twice on exactly that before the exclusion). The detail carries the window **trend** as a mechanism hint: growing ⇒ suspect a span/denominator, flat ⇒ suspect a rate. **Known limitation:** catches *step* changes, not slow drift — the RP bug crept ~+5pp/week, which no jump test separates from noise at this n. Slow drift is the human read of `scripts/calibration.py`. Measurement shared via `app/calibration.py` so this and the report can't diverge. Tests: `tests/test_calibration_check.py`. **EXPECT A LEGITIMATE FIRE WHEN WEEK 19 SETTLES** — the baseline is periods 10-18, all of which ran the *pre-fix* model, while wk19+ carry the 2026-08-10 RP-denominator, QS-rate and SVHD-rate fixes. QS bias should drop ~14pp and SVHD further. That flag is **the fixes working**, not a regression: resolve it with a note and let the baseline roll forward. |
| `ANOM_RATE_DIVERGENCE` | warn | projected ERA/WHIP within **both** 40% (`RATE_DIVERGENCE`) **and** an absolute floor (`RATE_DIVERGENCE_ABS`: 2.50 ERA / 0.80 WHIP points) of current, once ≥20 IP banked | bug if components dropped (pairs with `INV_RATE_COMPONENTS_MISSING`). **The absolute floor was added 2026-07-29 after all 25 instances ever recorded (2026-06-25 → 07-24) were triaged benign** — the relative test alone divides by the *current* rate, so a hot small sample (1.12 ERA over 24 IP → 2.71) trips 40% on plain mean reversion. Max benign gap was 1.59 ERA / 0.39 WHIP points vs 4.61 for the 8.37→3.76 bug, so the floor separates them where the ratio can't (raising `RATE_DIVERGENCE` would need >1.42 and would blind the check to its own target case). |

Thresholds live as constants at the top of `app/validate.py`
(`WP_SWING`, `RATE_DIVERGENCE`, `MAX_SP_UNITS_PER_WEEK`, `BANKED_REGRESS_ABS/FRAC`,
`RATE_BOUNDS`, `CORRELATED_SWING_EACH`, `MIN_CORRELATED`, `STALE_MINUTES`, …).

### The Pages deploy guard (`app pages-guard`, added 2026-08-31)

```sh
.venv/bin/app pages-guard --dry-run   # report what it would cancel
```
Watches the pipeline's **last hop** — the GitHub Actions workflow
(`.github/workflows/pages.yml`) that deploys `docs/` to Pages. Runs every tick
from fast.sh, after the git step, non-fatal. Logic + thresholds in
`app/pages.py`; tests `tests/test_pages_guard.py`.

**Why it exists.** On 2026-08-31 the site served `2026-08-27T16:00:37Z` data for
**4 days with no flag of any kind**. Run 33091447664 (created 08-27 16:06:01Z)
wedged in status `waiting` on the `github-pages` environment gate with
`wait_timer: 0`, `reviewers: []` and `current_user_can_approve: false` — no timer
to expire, nobody able to approve — and it held the workflow's
`concurrency: group: "pages"`, so all ~1150 runs behind it collapsed to
`cancelled`. Cancelling that one run cleared the group and the next tick deployed
in 18s. Note the freeze required **both** the wedge and the concurrency group;
loosening the group is a second, independent lever if this recurs.

**The one rule that keeps it safe: it never cancels an `in_progress` run** —
only `waiting`/`queued` ones older than `RUN_STUCK_MIN` (30). Cancelling
in-flight deploys is precisely the 2026-07-02 incident recorded in `pages.yml`
(GitHub's backend slowed deploys to 4-10+ min; `cancel-in-progress: true` killed
every one and the site froze). A slow-but-healthy deploy is `in_progress`; a run
stranded behind a wedge is `waiting`. **Do not widen the eligible statuses**, and
keep both thresholds well above 10 min, or the guard recreates the failure it
exists to fix (`test_thresholds_stay_clear_of_a_merely_slow_backend`).

Recovery needs no manual re-trigger: `generated_at` changes on every publish, so
every tick produces a commit and a push, and the next push fires the deploy.

### Adding a new check

When a new failure mode surfaces: add a pure `check_*(view) -> list[Finding]`
in `app/validate.py` (operate on the `view` dict so it's unit-testable), append
it to `_CHECKS`, add a row to the reference table above, and add a test to
`tests/test_validate.py` (ideally encoding the actual incident as a regression
guard — that's how `INV_RATE_COMPONENTS_MISSING` and the guard tests were born).

### wp_snapshot history retention

**Policy: never delete snapshots.** The DB keeps every snapshot so every week —
including completed past weeks — keeps its full WP-over-time graph. (One-time
exception 2026-06-04: a corrupted window of period-10 snapshots had its `home_wp`/
`away_wp` *overwritten* — not deleted — to smooth the graphs; `details_json` still
holds the original computed values. See `INCIDENTS.md`. Overwriting computed WP
is otherwise not something to do lightly.) Payload size
is handled at *publish* time instead: `_downsample_history` thins each matchup's
history per model version when writing the per-week history files.

**Two tiers, both anchored to the series, not to the clock (rewritten 2026-08-03):**
- the last `RECENT_FULL_HOURS` (24) of **that week's own history** stay at raw
  5-min resolution — the "Today"/"Active" zoom, and where end-of-week resolution
  cliffs live;
- everything older snaps to a round `OLDER_GRID_MINUTES` (15) wall-clock grid
  (first snapshot per bucket), with `MAX_HISTORY_POINTS` (2400) as a backstop that
  shouldn't bind (a 7-day week lands ≈1200 points);
- `category_wp` (≈920 bytes, ~10× a point's own size) gets the **same two-tier
  treatment on its own shorter clock** (added 2026-08-08): every carrier in the
  last `CAT_RECENT_HOURS` (6) survives, and only older ones thin to
  `MAX_CAT_HISTORY_POINTS` (200) evenly spaced. `app.js` still matches a clicked
  point to the **nearest** carrier (not an exact timestamp) and labels the panel
  "Category win rates as of HH:MM" — but inside the tail every 5-min tick is now
  its own carrier, so that label matches what you clicked.
  *Why the tail exists:* carriers used to be a flat 200 over the whole week, i.e.
  a **median 75 min apart** and ~25 min even in the live tail — so five
  consecutive ticks all snapped to one carrier and showed an identical category
  table while the WP line moved (reported 2026-08-08: 23:15/23:20/23:25 CEST all
  resolved to the 23:25 carrier). *Why 6h and not `RECENT_FULL_HOURS` (24):*
  measured cost is ~920 B/carrier — 6h = +404 KB on a week's history file
  (1760→2164 KB), 24h would have been +1552 KB, and the extra 18h is mostly dead
  time between slates where consecutive tables are near-identical anyway. Keep
  this window short; it is the expensive tier.

Why not the old rule (a flat 200 evenly-spaced points): the step then depends on
how long the series happens to be — a 7-day week landed on a ~55-min grid, a value
nobody chose — and even thinning *drops* extremes rather than aggregating them, so
short-lived truth disappears. It cost the week-17 m98 settle cliff: an 80%→2% drop
in one tick published as a gentle 55-min slope from 71.8% to 1.5%, with the 80.2%
peak gone entirely. Anchoring the fine window to the series' **last point** (not
`now`) matters because `daily.sh` runs `publish --rebuild` over every week: a
wall-clock window would slide off a finished week and silently re-thin the detail
it had while live. Tests: `tests/test_publish_history.py`.

Mixed-model history is *not* a reason to delete: the front-end chart filters to
the matchup's current `model_version` (`renderChart`), so old-model points are
never plotted — they just sit harmlessly in the DB.

Historical note: commit `fad4684` did a one-off `DELETE` keeping only the latest
snapshot per matchup on a model-change day. That destroyed the graphs for weeks
7–8 (they predate the surviving history). Don't do that again — if old-model
points ever need clearing, scope the delete to `model_version = '<old>'` and only
for non-final weeks, never a blanket "keep latest only."

### crontab

```
*/5  *   * * *  /Users/mpozar/git/fantasy-wp/scripts/fast.sh
2    */4 * * *  /Users/mpozar/git/fantasy-wp/scripts/medium.sh
2    6   * * *  /Users/mpozar/git/fantasy-wp/scripts/daily.sh
```

medium/daily fire at **minute :02** deliberately: fast's :00 tick finishes in
~45s, so they no longer collide with (and skip) it — on the old :00 schedule
every medium/daily run cost one fast tick (a 10-min site-freshness gap). Both
finish before the :05 fast tick. Their network step runs under `with_retries`
(_common.sh: 60/120/240s backoff, ~7 min worst case, still far under
`MAX_LOCK_AGE`) to ride out the recurring ~06:00-07:00 UTC Wi-Fi drops; an
outage that outlasts the retries aborts the run and the next scheduled one
catches up.

macOS gotcha: `/usr/sbin/cron` needs **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access → `+` → Cmd-Shift-G → `/usr/sbin/cron`). Otherwise it can't read `~/.zshenv` and silently fails.

## Style notes

- Single-database SQLite (`data.db`), migrations live in `db.init()` via `ALTER TABLE ADD COLUMN` with `try/except OperationalError` (idempotent).
- Secrets in `~/.zshenv` (never inline in commands). Read via `read_zshenv_var` in shell or `_read_zshenv_var` in Python.
- Cache-bust assets on every UI change: bump `?v=N` in `index.html` for `style.css` and `app.js`.
- Don't recompute history snapshots on model changes — let new snapshots accumulate forward. **Never delete snapshots** (see "wp_snapshot history retention"); the chart filters by `model_version` and `publish` downsamples the payload, so stale points are harmless.
- Front-end is intentionally tiny: no framework, vanilla JS, ~770 lines.

## Known limitations (documented in "How this works")

- Roster moves you make later this week aren't reflected until the next medium.sh
- Lineup changes — same
- SP start *count* now carries variance (cadence `extra_dist` sampled per sim), but per-start blowups still lean on Poisson for OUTS/K (only ER gets NB); within-start tail risk is undersold
- Cadence over-count: two rostered SPs from the *same* MLB team can each project the same open game as their turn (per-player independence, same as the old flat model). Rare on a real roster; not corrected
- **Multi-week (All-Star) periods over-project starts ~+44%** — both rotation
  models walk straight through the break's ~3 gameless days (`LONG_MATCHUPS`
  period 15: 14 days but 11 game dates, 0.68 games/team/day vs ~0.90). Normal
  weeks are +3.9%. Measured 2026-08-10 via `scripts/analyze_starts.py`; not
  fixed (affects one period a season, next occurrence July 2027)
- **Bench/IL-slotted pitchers are projected at full weight by design** (owner
  call 2026-08-10) — costs ~10.6% of projected starts in credit that ESPN will
  never score, deliberately, so WP never penalises a team for a neglectful
  manager. See "Decomposing the SP start over-projection"
- Cadence anchor uses name matching (no ESPN↔MLBAM player-id crosswalk) — same rare-miss risk as probable matching
- Teammate correlation: each player's stats are independent in the sim, but real-world teammates share weather/pitcher/etc. Slightly over-spreads the outcome distribution.
- Reliever leverage: an RP appearance in a save spot counts the same as one in a blowout
- Off-days, platoon splits, weather, lineup card details — assumed away
- Probable-pitcher matching is by name (rare misses on spelling variations)
- **Spot starts / misclassified rotation SPs — fixed 2026-06-28** (see "Role
  classification"). A reliever/swingman the season `GS/GP` ratio calls RP but who is
  the **announced probable or currently starting** is now promoted to the SP path, so
  his start's QS/K/innings are projected (and his live QS credited) instead of missed.
  Cumulative counters use per-out × a capped start length (avoids the `season_total/GS`
  blowup), QS uses the per-start rate. (Earlier the projection modeled him as relief —
  2026-06-28 Tyler Phillips's 7-IP QS was invisible; the SVHD half was fixed 2026-06-10
  by skipping the save/hold override on a `games_started` line.) *Residual:* the per-out
  rates and the capped/typical start length are approximations — a genuine one-off
  opener still gets ~one sane start's worth rather than a precisely-calibrated line.

## When the user asks about a WP swing

Use the `wp-investigate` skill: run `scripts/wp_diff.py` first (step 0), then
read this file's "Investigating WP changes" section. The most common causes (in rough order of frequency):
1. Probable pitcher announcement (an SP's units step toward 1 from its estimate)
2. Roster move (player added/removed)
3. ESPN stat backfill (live category state updates retroactively)
4. medium.sh refresh (projections updated)
5. Monte Carlo noise (small jiggles up to ~1pp at p≈0.5)
6. Live game in progress (cum state updates as plays happen)
7. **Daily component settle (~07:00 UTC)** — ESPN finalizes the raw rate
   components once a day, so projected ERA/WHIP/OPS/QS can step then. Live
   component reconstruction (see that section) now smooths most of this by
   rebuilding components from box-scores during the slate; a residual step can
   remain if reconstruction was falling back (e.g. `ANOM_LINEUP_SNAPSHOT_MISSING`).
8. **A reliever's save/hold resolving** — projected SVHD steps by ~1.0 when a
   rostered reliever's outcome firms up. Since the 2026-06-10 fix this is judged
   **live** from his entry/exit margins (`reliever_appearances`), so it's credited
   when he exits with the lead — not deferred to Final, and it no longer flickers
   off when the lead is later padded/blown. A residual one-tick effect can still
   occur if the entry tick was missed (fallback to the live margin) or via the
   `_count_svhd` Final reconciliation. Tell-tale: the jump is entirely in SVHD,
   projected SVHD ±1.0, around a reliever entering/exiting a save situation.

Always **compare the budgets before vs after** the transition. The "what changed" is usually obvious from the diff. For a swing isolated to ERA/WHIP/OPS/QS with the counting cats unchanged, suspect the component settle (cause 7).
