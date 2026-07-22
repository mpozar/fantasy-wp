---
name: wp-investigate
description: >-
  Investigate a fantasy-wp win-probability change or anomaly with the mandatory
  evidence-first procedure. Use for ANY question like "what caused the
  drop/increase in <team>'s WP from <time> to <time>?", "why did <team> jump at
  <time>?", "any flags?", "was that swing real?", or any request to explain,
  attribute, or diagnose a WP move, a validation flag, or a suspicious
  projection in the fantasy-wp project (repo: /Users/mpozar/git/fantasy-wp).
  The procedure exists because a run of past investigations attributed moves to
  the first salient cause and got it wrong; this skill makes the decomposition
  run BEFORE any attribution.
---

# WP investigation

The failure mode this skill prevents: naming a cause before decomposing the
evidence. It has happened repeatedly, even with the lesson in context. So the
rule is structural: **no causal statement before step 1's output is in the
conversation.**

## Procedure

1. **Run the decomposition first — before forming any hypothesis:**

   ```sh
   .venv/bin/python scripts/wp_diff.py <matchup_id|team-name> "<start>" "<end>"
   ```

   Naive times are read as **Europe/Oslo** (the owner says "CET" but means
   local; DB is UTC) — check the header echo. If the window is ambiguous in
   the user's question (which day, which team), **ask — don't infer from
   data** (see feedback_ask_dont_guess_references). Rerun on the "biggest
   tick" the output identifies to isolate a single event.

2. **Weigh every candidate in the output, not the loudest one.**
   - A category marked `LEADER FLIPPED` dominates within-lean shifts.
   - Flat banked stats + budget changes ⇒ roster/lineup move (usually the
     *opponent's*). Banked deltas + flat budgets ⇒ live play / backfill.
   - A move that's been wrong for *days* and corrects at a refresh/settle
     boundary ⇒ budget input (phantom start, stale schedule), not a play.
   - If two candidates are comparable, say "~half each" — decompose further
     (revert one side at a time) only if the user needs the split.

3. **Verify mechanics in code before asserting them — cite `file:line`.**
   HARD RULE (not a reminder): **every mechanical claim in your answer carries a
   `file:line` you opened *this session*.** No citation ⇒ you didn't verify it ⇒
   don't assert it (downgrade to "hypothesis:" or go read the function). This is
   the rule that keeps getting broken by trusting a remembered/ documented claim;
   the citation requirement is the forcing function. **CLAUDE.md and this skill
   are a *map*, not ground truth — code wins.** A doc/memory statement is a
   pointer to go read, never the basis for an assertion (the 2026-07-22 "IL slot
   is a hard filter" misdiagnosis came from trusting the doc; it was wrong — IL
   players with a return estimate ARE projected, gated by return date). Slots:
   **17 = IL, 16 = bench** — but IL is *not* a blanket exclude; read
   `_is_playable`/`_est_return_date` rather than recalling a rule. Roles, caps,
   overrides, the stat map (`app/stats.py`): read the function, never memory.

4. **Label everything by evidence class in the answer:**
   - *verified* — a query/output above confirms it (say which).
   - *hypothesis:* — plausible but unconfirmed. Use the word explicitly.
   - Never "the cause was X" unless decomposed and weighed. Prefer "the
     primary driver / a driver / ~half each". A removed player's impact is
     his **marginal** value (the optimizer backfills), not his stat line.

5. **Under pushback, re-derive the specific point** — don't recant a verified
   mechanism because a peripheral detail (a date, a name) was off. If the
   owner questions a conclusion, he's often onto something: re-check the
   *evidence*, not your confidence.

6. **Check reconstruction limits before promising more:** historical
   rosters/schedule are overwritten and `details_json` budgets are display
   summaries — a past tick **cannot be re-simmed**. Best-available estimate,
   labeled as such.

7. **Log the outcome** — append one row to `INVESTIGATIONS.md` (root of the
   repo): date, question, first attribution, final attribution, whether the
   owner had to correct it. This is the feedback loop that tells us whether
   the process is working; skipping it is how lessons got lost before.

## Reference

Mechanics behind the common signatures (settle lag, phantom starts,
finalization lag, overshoot-and-correct, benched-starter first-pitch drop):
CLAUDE.md → "WP-swing investigation playbook" + "Finalization lag".
Known data incidents and hand-edited windows: `INCIDENTS.md`.
Open flags: `.venv/bin/app validate --list` (resolve only with `--note`).
