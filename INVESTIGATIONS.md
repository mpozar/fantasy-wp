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
