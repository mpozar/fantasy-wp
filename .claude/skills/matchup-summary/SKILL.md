---
name: matchup-summary
description: >-
  Generate a weekly summary write-up for a fantasy-wp matchup, given a matchup_id.
  Produces: result + category score, the win-probability arc over the week, final
  category standings (flagging the close ones), and the key WP swings with their
  causes AND the decisive players. Use when the user asks to summarize, recap, or
  write up a matchup or a team's week in the fantasy-wp project (repo:
  /Users/mpozar/git/fantasy-wp). Trigger on phrases like "summarize matchup N",
  "recap the Teacher matchup", "weekly summary for matchup 58".
---

# Matchup summary

Produce a short, skimmable weekly write-up for one fantasy-wp matchup. Read-only.

## Inputs
- `matchup_id` (required). If the user names a team/week instead, resolve it first:
  `SELECT m.id, h.name home, a.name away, m.matchup_period_id FROM matchups m
   JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id
   WHERE m.matchup_period_id=<period> AND (h.name LIKE '%<team>%' OR a.name LIKE '%<team>%')`.

## Steps

1. **Run the data layer** (does the deterministic heavy lifting):
   ```sh
   cd /Users/mpozar/git/fantasy-wp && .venv/bin/python scripts/matchup_summary.py <matchup_id>
   ```
   It prints: teams, result + category score, final per-category standings (with
   `<<close` flags), the WP arc (daily closes, peak, trough), and the top swings —
   each with its driving category and, for *projection* swings, the named player
   (`by: <name> (exp_x ±d)`). Swings marked `by: (banked — check box score)` are
   banked-counter events (usually a HR/hit) you must attribute in step 2.

2. **Attribute the banked swings** (this is the "name the decisive contributors"
   part). For each swing the script left as `(banked — check box score)`, especially
   the late/decisive ones:
   - The driver category + which side's `avg` rose tells you what happened and to
     whom (e.g. driver=HR, `Sox Teacher 10.4->11.4` ⇒ a Teacher hitter homered).
   - Resolve the player. Cheapest first:
     - **Recent swing (today/yesterday):** check `live_batters` / `pitcher_final_lines`
       / `live_pitchers` for the rostered players whose stat matches.
     - **Older swing:** fetch the MLB box score for that fantasy side's rostered
       players' games on the swing's date and find who logged the event:
       ```python
       from app import db, sim, mlb
       conn=db.connect()
       # side = the team whose avg rose (home_team_id or away_team_id)
       roster={sim._norm_name(p['full_name']): p for p in sim.load_team_roster(conn, <period>, <side_team_id>)}
       # for each game_pk the side's players' pro_teams played that date:
       for line in mlb.fetch_boxscore(<game_pk>)['batters']:
           if sim._norm_name(line['name']) in roster and line['hr']>0: print(line['name'], line['hr'])
       ```
       (game_pks for a date: `SELECT DISTINCT game_pk FROM team_schedule WHERE matchup_period_id=<period> AND game_date='<date>'`.)
   - If it can't be pinned cleanly, say "a <Team> home run" rather than guessing.

3. **Sanity rules (don't repeat past mistakes — see CLAUDE.md "WP-swing playbook"):**
   - Read current rates from the **scraped/folded** value, never `derive_*` on raw
     `category_state` (it mixes scrape-live H/HR with REST-stale components).
   - A projected per-category `avg` moving by ~**+1.0** = one banked counting event
     (QS/SVHD/HR/start). The script already surfaces this.
   - The script reports from the **away** team's perspective (WP, category win%).
   - Swings near ~07:00 UTC are usually the benign daily component settle (cause #7);
     a reliever's hold often only lands at game-Final (cause #8).

4. **Write the summary** in this format (keep it tight — a few hundred words):

   ```
   # Weekly Matchup Summary — Period <n> (<start>–<end>)
   ## <Winner> def. <Loser>, <X>–<Y>

   **Result:** <final WP> · took <X> of 10 categories. <one-line hook>.

   ### The arc
   <1–2 sentences: started where, peaked/troughed when, how it ended>
   <compact daily WP table>

   ### Final category breakdown
   - **<Winner> (X):** <cats, with the close ones called out: e.g. HR 12–11, OPS .839–.838>
   - **<Loser> (Y):** <cats>

   ### The swings that decided it
   1. <date/time> — <what happened, the driver category, the named player, WP move>
   2. <the decisive late one> — <…>

   **Turning point:** <one sentence — what actually clinched it>
   ```

   Emphasize **late-week** events (the user cares most about what swung it down
   the stretch). Lead with the decisive swing(s); don't list every minor wobble.

5. **Publish to the site (chart annotations + the write-up).** The site shows two
   things from the per-matchup file `docs/annotations/<id>.json`: the "✦ Annotate"
   overlay (events/spans) and a **"Weekly summary" write-up** rendered in Details
   below the chart. Generate + push it:
   - Write the **in-panel write-up** to a temp markdown file. Keep it body-only and
     compact (the chart shows the arc; a `result` line is added automatically):
     a short **arc** paragraph, a **Final categories** bullet list (call out the
     close ones), a **What swung it** list (the decisive late events with named
     players), and a one-line **turning point**. Use `###` sub-headings, `**bold**`,
     `- ` bullets — **no big H1 title, no markdown tables** (the renderer is a small
     subset: headings/bold/lists/paragraphs).
   - Then bundle + commit:
     ```sh
     .venv/bin/python scripts/matchup_summary.py <matchup_id> --annotate --writeup /tmp/wu<id>.md
     git add docs/annotations/<matchup_id>.json && \
       git commit -m "matchup <id>: summary + annotations" && git push
     ```
   The file is tiny and loaded lazily (only when a panel is expanded / annotations
   toggled on), so it never bloats data.json. `--annotate` reuses the swing/
   attribution logic (events named, incl. box-score hitters; day-level spans) and
   adds a deterministic `result` line. Re-run anytime to refresh. Skip only if the
   user explicitly wants the chat write-up alone.

## Notes
- Scale the swing list to the story: a blowout needs 1–2 swings; a comeback needs
  the collapse + the recovery. The script shows up to 8 — pick the ones that matter.
- The DB/flags are read-only here; the only writes are the annotations file + its
  git commit (step 5). Never resolve flags or touch the DB from this skill.
