async function load() {
  const r = await fetch("data.json", { cache: "no-store" });
  if (!r.ok) throw new Error(`Failed to load data.json: ${r.status}`);
  return r.json();
}

const fmtPct = (p) => (p == null ? "—" : (p * 100).toFixed(1) + "%");

function fmtStat(statId, val) {
  if (val == null) return "—";
  if (statId === 18) {
    // OPS: 4 decimals, drop leading 0 (".739" style)
    return val.toFixed(4).replace(/^0\./, ".");
  }
  if (statId === 47 || statId === 41) return val.toFixed(3); // ERA / WHIP
  // Counting stats — round to integer
  return String(Math.round(val));
}

function recordStr(rec) {
  return `${rec.W}-${rec.L}-${rec.T}`;
}

function cellClass(result) {
  if (result === "WIN") return "win";
  if (result === "LOSS") return "loss";
  if (result === "TIE") return "tie";
  return "";
}

function statCells(blocks) {
  return blocks
    .map((s) => `<td class="num ${cellClass(s.result)}">${fmtStat(s.stat_id, s.score)}</td>`)
    .join("");
}

function headerCells(blocks, tiebreakerStatId) {
  return blocks
    .map((c) => {
      const mark = c.stat_id === tiebreakerStatId ? '<span class="tb" title="Tiebreaker">★</span>' : "";
      return `<th class="cat">${c.name}${mark}</th>`;
    })
    .join("");
}

function renderMatchup(m, cats, tiebreakerStatId) {
  const home = m.home, away = m.away;
  const homeFav = (home.wp ?? 0.5) > 0.5;
  const awayFav = (away.wp ?? 0.5) > 0.5;
  const homeWpPct = (home.wp != null) ? (home.wp * 100).toFixed(1) : "—";
  const awayWpPct = (away.wp != null) ? (away.wp * 100).toFixed(1) : "—";

  const teamRow = (side, fav) => `
    <tr class="team-row ${fav ? "favored" : ""}">
      <td class="team-info">
        <div class="team-name">${side.name ?? "Team"}</div>
        <div class="team-owner">${side.owner ?? ""}</div>
      </td>
      <td class="record">${recordStr(side.record)}</td>
      <td class="wp ${fav ? "fav" : ""}">${side.wp != null ? (side.wp * 100).toFixed(1) + "%" : "—"}</td>
      ${statCells(side.batting)}
      ${statCells(side.pitching)}
    </tr>`;

  return `
    <section class="matchup">
      <table>
        <colgroup>
          <col class="c-team"><col class="c-record"><col class="c-wp">
          ${cats.batting.map(() => '<col class="c-bat">').join("")}
          ${cats.pitching.map(() => '<col class="c-pit">').join("")}
        </colgroup>
        <thead>
          <tr class="group-row">
            <th colspan="3" class="spacer"></th>
            <th colspan="${cats.batting.length}" class="group g-batting">BATTING</th>
            <th colspan="${cats.pitching.length}" class="group g-pitching">PITCHING</th>
          </tr>
          <tr class="cat-row">
            <th class="team-h">Team</th>
            <th class="record-h">Cats</th>
            <th class="wp-h">WP</th>
            ${headerCells(cats.batting, tiebreakerStatId)}
            ${headerCells(cats.pitching, tiebreakerStatId)}
          </tr>
        </thead>
        <tbody>
          ${teamRow(home, homeFav)}
          ${teamRow(away, awayFav)}
        </tbody>
      </table>
    </section>`;
}

function render(data) {
  document.getElementById("league-name").textContent = data.league.name;
  document.getElementById("subtitle").textContent =
    `Matchup Period ${data.matchup_period_id} · ${data.league.size}-team H2H · Tiebreaker: ${data.league.tiebreaker_name}`;
  const ts = new Date(data.generated_at);
  document.getElementById("meta").innerHTML =
    `Updated <time datetime="${data.generated_at}">${ts.toLocaleString()}</time>` +
    ` · Model <code>${data.matchups[0]?.model_version ?? "—"}</code>`;

  const root = document.getElementById("matchups");
  const cats = data.league.categories_by_group;
  const tb = data.league.tiebreaker_stat_id;
  root.innerHTML = data.matchups.map((m) => renderMatchup(m, cats, tb)).join("");
}

load().then(render).catch((e) => {
  document.getElementById("matchups").textContent = "Error: " + e.message;
});
