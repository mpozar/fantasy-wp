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
