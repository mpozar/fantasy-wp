---
name: matchup-summary
description: >-
  Generate a weekly summary write-up for a fantasy-wp matchup, given a matchup_id.
  The script (scripts/matchup_facts.py) emits ONLY neutral, reproducible facts —
  the tie-aware result, the WP arc, the candidate swings, and the raw box scores.
  YOU (the LLM) do all the judgment: pick which swings matter, attribute each to
  the actual play, write the prose, and author the chart annotations. Use when the
  user asks to summarize, recap, or write up a matchup or a team's week in the
  fantasy-wp project (repo: /Users/mpozar/git/fantasy-wp). Trigger on phrases like
  "summarize matchup N", "recap the Teacher matchup", "weekly summary for matchup 58".
---

# Matchup summary

Produce a short, skimmable weekly write-up for one fantasy-wp matchup, and (unless
told otherwise) publish the chart annotations + write-up to the site. Read-only
except the annotations file it commits.

**Philosophy (why this skill is LLM-led, not script-led).** The script is a
deliberately dumb fact emitter — it does the arithmetic an LLM would hallucinate
(tallies, deltas, box lines) and *nothing else*. It does NOT decide who "gained
ground", guess a single driving player, or build trend spans. A previous
script-driven version got all of those wrong (inverted span signs, the same HR
stamped on two swings, span headlines that disagreed with the events inside them).
Those are judgment calls — they're yours. Use the facts as ground truth for
*numbers*; you are the source of the *story*. Never invent a number the facts
don't support.

## Inputs
- `matchup_id` (required). If the user names a team/week instead, resolve it first:
  `SELECT m.id, h.name home, a.name away, m.matchup_period_id FROM matchups m
   JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id
   WHERE m.matchup_period_id=<period> AND (h.name LIKE '%<team>%' OR a.name LIKE '%<team>%')`.

## Steps

1. **Get the facts** (deterministic, neutral):
   ```sh
   cd /Users/mpozar/git/fantasy-wp && .venv/bin/python scripts/matchup_facts.py <matchup_id>
   ```
   It prints, perspective-explicit (away = orange curve, home = blue curve):
   - **RESULT** — the TIE-AWARE category tally (`X - Y (Z tie)`), the actual ESPN
     winner, and — when the categories are level — the **hits tiebreaker**
     comparison that decided it. Use this line verbatim as your headline truth.
   - **FINAL CATEGORIES** — away_avg vs home_avg per cat, the raw sim
     `a/h/t` win counts, and a `<<close` flag.
   - **WP ARC** — daily closes for *both* sides, plus peak/trough (hand-edited
     snapshots are already excluded).
   - **CANDIDATE SWINGS** — every ≥7pp tick, chronological, each with BOTH deltas
     (`Δaway` / `Δhome`), the top category-win% movers, and the per-player budget
     diff (projection movers like a QS locking or a probable announcing). A mover
     line reading "(no projection mover — likely banked counter)" means the swing
     was a real-world counting event (HR/H/SB/save) — attribute it from the box.
   - **BOX SCORES on swing days** — the raw rostered-player lines (HR/H/2B/3B for
     hitters; QS/SVHD/K/IP/ER for pitchers), per side, for every swing day. This
     is your attribution source. ALL contributors are listed — the script can't
     know which swing a given HR caused; that's your call.

2. **Apply judgment — this is the work:**
   - **Preferred input: owner-flagged windows.** The owner reviews the chart and
     hands you approximate windows (team + day + a clock range, times in **Europe/
     Oslo** local — convert to UTC yourself, handling DST: UTC+2 in summer). Often
     with the WP% endpoints from the hover tooltip, which pin the exact swing. Your
     job per window: (a) pull the matchup's snapshots in that UTC range, (b) **refine
     to the single most dramatic move** — the steepest contiguous swing (report its
     tick + the window's net); (c) attribute it (below). If they don't supply
     windows, fall back to picking the swings yourself.
   - **Refining a window — three things to check first:** is it inside a known
     hand-edited / corrupted window (`INCIDENTS.md`, `edited=1`)? → say so, don't
     annotate. Is the net move ~0 (the eye can misjudge a flat stretch)? → tell the
     owner there's no real swing there. Is it a gradual bleed vs one sharp tick? →
     mark the tick for sharp, use the net for gradual.
   - **Pick the swings that matter** (when self-selecting). A blowout needs 1–2; a
     comeback needs the collapse *and* the recovery. Ignore minor wobbles.
   - **Attribute each swing to the actual play.** Cross-reference the swing's
     driver category + the side whose `avg` rose against that day's box lines.
     - A projection swing (budget mover present) names itself: "Soroka locked a
       QS" (`exp_qs +0.15`), "a probable was announced".
     - A banked swing: find the matching box line. If two players homered that day
       (e.g. Moreno *and* Neto on 06-07 m58), and you can't tell which HR moved
       the WP at that minute, say "a Sox Teacher HR" rather than guessing — do NOT
       stamp the same player on two different swings.
     - HR, H, **R, and SB** all have per-player box lines now — name the
       contributors (e.g. "J-Rod steals ×2", "Wood & Abrams HRs"). Push for names;
       a vague "(overnight)" or "barrage" label is a miss.
   - **Reconcile WP vs scoreboard.** A 100%-WP win can be a one-category nailbiter
     (m58: HR 12–11, OPS .839–.838) or a tiebreaker (m60: 4–4 on hits). Say so —
     "decisive in probability, razor-thin on the board" is usually the real story.

3. **Sanity rules (don't repeat past mistakes — see CLAUDE.md "WP-swing playbook"):**
   - **Check `INCIDENTS.md` for hand-edited / corrupted windows** before trusting a
     swing. Period-10 (m55–60) on **2026-06-04** has corrupted `details_json` across
     ~06:16–20:02 UTC and two smoothed WP windows that day — swings there are
     artifacts, not real events. Don't build the story around them.
   - A projected per-category `avg` moving by ~**+1.0** = one banked counting event
     (QS/SVHD/HR/start). The facts already surface this in the mover line.
   - A swing right at ~**07:00 UTC** is usually the benign daily component settle;
     a reliever's **hold** often only lands at game-Final (a pure-SVHD +1.0 jump).
   - Read current rates from the FINAL CATEGORIES block (already folded), never by
     deriving on raw `category_state`.

4. **Write the chat summary** — tight, a few hundred words:
   ```
   ## <Winner> def. <Loser>, <X>–<Y>[ (Z ties)][ — won on the hits tiebreaker]
   **Result:** <final WP> · <one-line hook>.
   ### The arc
   <1–2 sentences: started where, peaked/troughed when, how it ended>
   ### The swings that decided it
   1. <date/time> — <what happened, the driver category, the NAMED player, WP move>
   **Turning point:** <one sentence — what actually clinched it>
   ```
   Lead with the decisive late swing(s); emphasize what swung it down the stretch.
   Don't enumerate every category — weave only the close/decisive ones into the
   prose (the result line already gives the X–Y, and on the site the scoreboard
   sits right there).

5. **Author the annotations + write-up, then publish.** The site renders, from
   `docs/annotations/<id>.json`: the "✦ Annotate" overlay (events + spans) and a
   "Weekly summary" markdown write-up below the chart.

   Write a single JSON file (`/tmp/ann<id>.json`) with `events`, `spans`, and
   `writeup`. **Sign convention (this is what the old script got wrong — get it
   right):** `wp_delta` is **POSITIVE**, expressed from the perspective of the team
   named in the label.
   - **events** (acute plays — the markers): `{"at": <exact snapshot timestamp from
     the facts>, "label": "<Player> <CAT>" e.g. "Moreno HR", "side": "away"|"home"
     (the team it helped; away=orange, home=blue), "cat": "<CAT>", "wp_delta":
     <+positive, that side's gain>}`. Keep ~3–6, the ones that matter. Mark only
     crisp, well-attributed high-swing *plays* — not gradual day-long slides (those
     belong in the prose; see the no-spans rule below).
   - **spans**: leave this `[]`. The trend bands are NOT used on the chart anymore
     (owner, 2026-06-08) — a day-long drift belongs in the write-up prose, not as a
     faint band fighting the curve. (The writer still accepts the field; just pass
     an empty array.)
   - **writeup** (markdown, in-panel): body-only, compact — a short **arc**
     paragraph, a **What swung it** list (decisive late events, named players), and
     a one-line **turning point**. Do NOT include a full category breakdown — the
     site shows the scoreboard right beside the write-up, so enumerating every
     category is pure redundancy. Weave only the *close/decisive* categories into
     the prose (e.g. "won HR by one, 12–11"). Use `###`/`**bold**`/`- ` only — NO
     H1, NO tables (the renderer is a small subset). The `result` line is added
     automatically.

   Then bundle (the writer validates signs/timestamps + adds the deterministic
   tie-aware result line) and commit:
   ```sh
   .venv/bin/python scripts/matchup_facts.py <matchup_id> --write /tmp/ann<id>.json
   git add docs/annotations/<matchup_id>.json && \
     git commit -m "matchup <id>: summary + annotations" && git push
   ```
   The file is tiny and loaded lazily, so it never bloats data.json. Re-run anytime
   to refresh. Skip publishing only if the user explicitly wants the chat write-up
   alone.

## Notes
- Bump nothing else; the only writes are `docs/annotations/<id>.json` + its commit.
- Never resolve validation flags or touch the DB from this skill.
- If `--write` fails validation, fix your authored JSON (bad sign, out-of-window
  timestamp) and re-run — don't hand-edit the bundled file.

## Editorial rules (evolving — refined from owner review)

This is the living style guide for these write-ups. **Process:** after the owner
reviews a summary and gives feedback, decide whether it's a one-off (just fix that
summary) or a *general* preference. If general, encode it here as a short rule + a
one-line rationale, and commit — so it's automatically in force on every future
summary, including in fresh sessions with no memory of the conversation. Read this
section before writing; treat it as binding. Don't delete a rule without the
owner's nod (they can veto one that overcorrected).

Seeded from the 2026-06-08 redesign + review (the bugs that motivated this skill):
- **Tallies are tie-aware, always.** Report `X–Y` plus the tied cats; never fold a
  tie into a team's column. When cats are level, state the hits tiebreaker and that
  it decided the matchup. (The old script showed m60 as 4–6 when it was a 4–4 hits
  win.)
- **Don't pin a banked counter to a player unless the box is unambiguous.** If two
  rostered players homered that day, say "a <Team> HR", not a guessed name. One
  real-world event = one marker; never stamp the same player on two swings.
- **Span/event signs must agree with the curve and with each other.** `wp_delta`
  positive, from the named team's perspective; a span's label/dir matches the team
  that *gained*, and matches the events sitting inside it (no "X loses ground" when
  X gained; no span headlining ERA when a HR was the real mover).
- **Label a span by what actually moved the WP, not the biggest category-win% delta.**
  A ratio cat can swing in win-% points while a HR moved the WP more.
- **Don't build the story on hand-edited / corrupted snapshots.** Check
  `INCIDENTS.md`; skip swings inside a known bad window (e.g. period-10 2026-06-04).
- **Reconcile WP vs the scoreboard.** Call out when a 100%-WP win was a one-category
  nailbiter or a tiebreaker — that contrast is usually the real story.
- **Scale to the story.** A blowout gets 1 span and no event markers; a comeback
  gets the collapse span + the recovery span + the decisive plays.
- **Name the contributors — including R and SB.** `mlb.parse_boxscore` now carries
  per-batter `r` and `sb` (added 2026-06-08), so the box dump shows them; a
  "SB barrage" or "(overnight)" label with no names is a miss. Pin the steal/HR/hit
  to the player(s). [owner, 2026-06-08]
- **Pad an owner-given window before judging it — the swing often sits on the edge.**
  Query a little wider than the stated range (and use inclusive bounds — a tight
  string upper bound like `...T14:10` lexically excludes `14:10:16`). A flagged
  window that looks flat is usually a swing parked at the boundary, not a non-event.
  *Worked example:* m57's "Tue 3:15–4:10pm" looked like net 0 because the +10pp jump
  landed exactly at the 14:10 UTC edge I'd truncated. [owner, 2026-06-08]
- **A roster add/drop is a valid swing cause** — diff the budget player-set across
  the tick (added/removed names), not just per-player projections. m57's +10pp was
  the Melonheads picking up Arrighetti + Hoffman at the 4-hourly refresh.
- **No full category breakdown in the write-up.** The site shows the scoreboard
  beside it, so listing every category is redundant. Weave only the close/decisive
  cats into the prose (the auto result line carries the X–Y). [owner, 2026-06-08]
- **No spans on the chart.** Author `"spans": []`. Day-level trends go in the prose,
  not as faint bands. Mark discrete high-swing plays as events instead. [owner, 2026-06-08]
- **Attribute a swing by the real PLAY timestamp, not the box-score `game_date`.**
  Fetch the MLB game feed (`/api/v1.1/game/<pk>/feed/live`, play `about.endTime` is
  UTC) and match the play that banked just before the swing's minute. The `game_date`
  grouping in the facts can be ~a day off (a US night game's plays land in the next
  UTC day), so when two rostered players homered on the "same day," timing tells you
  which HR drove *this* tick. And a swing near **~07:00 UTC is usually the daily
  settle** banking overnight production in aggregate — not a single play at that
  minute; don't pin one name to it (prefer to leave it unmarked). [owner, 2026-06-08]
  - *Worked example:* m58's decisive +42pp swing (06-07 20:50) was **Moreno**'s HR
    (play 20:45 UTC), not Neto's — Neto homered at 22:39 UTC when Teacher was already
    ~97%, a ~2pp nudge. The old `max()` script mislabeled it "Neto HR".
- **Only ACTIVE-slot players score — never credit a benched/IL player's box line.**
  A rostered player on the bench (slot 16) or IL (17) that day contributes zero to
  the team total. The box section tags each line: `[BENCH/IL — DID NOT COUNT]`,
  `[slot? — no lineup snapshot]` (pre-2026-06-06 dates, where `daily_lineups` has no
  record), or blank (active). For a `[slot?]` line you MUST reconcile against the
  scored category delta in CANDIDATE SWINGS before crediting it — if the banked avg
  didn't move by the box amount, the player was benched. [owner, 2026-06-08]
  - *Worked example:* m59's June-2 Bums barrage — the box showed 4 HR (Lopez, Olson,
    **Torres**, J-Rod) but the banked HR rose only **+3**: Torres was benched, so the
    counted homers were Lopez, Olson, J-Rod. The +3 scored delta is authoritative.
