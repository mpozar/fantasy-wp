async function load() {
  const r = await fetch("data.json", { cache: "no-store" });
  if (!r.ok) throw new Error(`Failed to load data.json: ${r.status}`);
  return r.json();
}

function fmtPct(p) {
  if (p == null) return "—";
  return (p * 100).toFixed(1) + "%";
}

function fmtScore(v) {
  if (v == null) return "—";
  // Show ratios with 3 decimals, everything else as the underlying number.
  if (!Number.isInteger(v) && Math.abs(v) < 10) return v.toFixed(3);
  return String(v);
}

function categoryRows(home, away, cats, tiebreakerStatId) {
  const byStat = (scores) => Object.fromEntries(scores.map((s) => [s.stat_id, s]));
  const h = byStat(home.scores);
  const a = byStat(away.scores);
  return cats.map((c) => {
    const hs = h[c.stat_id]?.score ?? 0;
    const as_ = a[c.stat_id]?.score ?? 0;
    let cls = ["", ""];
    if (hs !== as_) {
      const homeBetter = c.reversed ? hs < as_ : hs > as_;
      cls = homeBetter ? ["win", "loss"] : ["loss", "win"];
    } else {
      cls = ["tie", "tie"];
    }
    const label = c.name + (c.stat_id === tiebreakerStatId ? " *" : "");
    return `
      <tr>
        <td>${label}</td>
        <td class="${cls[0]}">${fmtScore(hs)}</td>
        <td class="${cls[1]}">${fmtScore(as_)}</td>
      </tr>`;
  }).join("");
}

function render(data) {
  document.getElementById("league-name").textContent =
    `${data.league.name} — Matchup Period ${data.matchup_period_id}`;
  document.getElementById("meta").textContent =
    `Generated ${data.generated_at} · Model ${data.matchups[0]?.model_version ?? "—"} · ` +
    `Tiebreaker: ${data.league.tiebreaker_name ?? "—"}`;

  const root = document.getElementById("matchups");
  root.innerHTML = data.matchups.map((m) => {
    const homeWp = m.home.wp ?? 0.5;
    return `
      <section class="matchup">
        <div class="matchup-header">
          <div>
            <div class="team-name">${m.home.name ?? "Home"}</div>
            <div class="team-owner">${m.home.owner ?? ""}</div>
          </div>
          <div class="vs">vs</div>
          <div style="text-align:right">
            <div class="team-name">${m.away.name ?? "Away"}</div>
            <div class="team-owner">${m.away.owner ?? ""}</div>
          </div>
        </div>
        <div class="wp-bar" style="--home-wp:${(homeWp * 100).toFixed(1)}%"><div></div></div>
        <div class="wp-row">
          <span>${fmtPct(m.home.wp)}</span>
          <span>${fmtPct(m.away.wp)}</span>
        </div>
        <table class="cats">
          <thead>
            <tr>
              <th>Cat</th>
              <th>${m.home.name ?? "Home"}</th>
              <th>${m.away.name ?? "Away"}</th>
            </tr>
          </thead>
          <tbody>
            ${categoryRows(m.home, m.away, data.league.categories, data.league.tiebreaker_stat_id)}
          </tbody>
        </table>
      </section>`;
  }).join("");
}

load().then(render).catch((e) => {
  document.getElementById("matchups").textContent = "Error: " + e.message;
});
