# Investigation log

One row per WP/anomaly investigation — the feedback loop for whether the
evidence-first process (`scripts/wp_diff.py` + the `wp-investigate` skill) is
actually reducing misdiagnoses. Append a row at the end of every
investigation, including clean ones. Review the trend occasionally; a repeat
failure mode here means a layer (skill trigger, tool output, docs) needs
fixing — not another paragraph of discipline prose.

Columns: **corrected?** = did the owner have to push back / correct the
attribution before it was right (`no` / `yes — what was wrong`).

| date | question (short) | first attribution | final attribution | corrected? |
|---|---|---|---|---|
| 2026-07-09 | Norsemen −17pp drop | "Trout benched" (salient, unweighed) | ~half bullpen K collapse (only flipped cat), ~half Trout | yes — attribution, slot semantics (17=IL not bench), impact overclaim (~15pp vs ~0.4pp), recanted a verified fix under date pushback |
| 2026-07-11 | Bus +12pp / Dawgs −12pp @ ~6am (7/11) | "double-count of a completed Friday game", then hand-waved "schedule catch-up" | doubleheader under-count bug — MIL@PIT 7/10 postponement folded into a 7/17 DH the hitter optimizer counted as 1 game (`_hitter_days_slotted` per-date max); fixed → sum | yes — two wrong mechanisms before the owner's postponement→doubleheader clue |
| 2026-07-16 | m85 −33.7pp swing (7/12) | ran wp_diff.py first — no pre-tool claim | sleep-gap catch-up (193-min gap → first post-wake tick spiked then settled) + R leader-flip Bus→Dawgs + whole-slate re-projection; benign | no — tool-first; wp_diff surfaced the gap + leader-flip |
| 2026-07-20 | m96 (wk16 Bears–Norsemen) +5pp to 20.6% this morning | ran wp_diff.py first — no pre-tool claim | overnight week-16 probable announcements (fetched 04:02/07:40 UTC) re-shuffled SP units at the 06:02 UTC medium recompute: Bears gained Cole/Williams/Seymour announced starts (net ~+1.9 units) vs Norsemen's +Singer −share on 4 others (~0 net) → K win share 10→32%, QS leader flipped (4.38→4.97 vs 4.80→4.41); banked flat, no roster moves in-tick; benign | no |
| 2026-07-22 | Dawgs drop Wed 03:35→03:40 Oslo (m91 wk16, corrected from m85/wk15) | first ran wrong week (m85) — user corrected to "this Wednesday"; re-ran m91 | live overshoot: opponent Jo Mamas banked HR 4→6, R 9→11, OPS .94→1.09 in an in-progress game → Dawgs (already underdog ~17%) HR win-share −15pp, OPS/H/R each −10 to −13pp → WP 17%→9.5%; live_recon shows the game being folded in; NOT noise (banked deltas real). Specific HR hitter unrecoverable (live_batters overwritten) | yes — wrong week first (date ambiguity), mechanism right on re-run |
| 2026-07-20 | m85 Dawgs drop Wed 7/15 03:35→03:40 Oslo | ran wp_diff.py first — no pre-tool claim | MC noise: −1.4pp tick inside a ±1.4pp oscillation at p≈0.5 during the All-Star break — banked flat, budgets flat, no flags, no leader flips; also only 1 downsampled point from that window survived to the site chart, so the visible "step" spans wider real time | no |
| 2026-07-20 | why is 13-2 WAR's P(final) 36% vs Norsemen 54% (playoff odds) | evidence-first (per-cat pairwise + season cat records before any claim); first tick-weighted category_state aggregation was garbage, redone via latest_category_state | record ≠ roster: WAR projects elite H (77% vs Po9) but underdog 6/10 cats — K 10% (54 vs 72 proj), SVHD 3% (4 modest RPs = 3.8/wk vs 6 arms = 6.7/wk); season per-cat records agree (K 7-8, ERA 5-10; 13-2 built on batting cats + SVHD); caveat noted: no September streaming/waiver modeling, so pitching-light contenders read worse than their real September selves; benign — worked example added to CLAUDE.md §Playoff odds | no |
