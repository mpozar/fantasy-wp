async function load() {
  const r = await fetch("data.json", { cache: "no-store" });
  if (!r.ok) throw new Error(`Failed to load data.json: ${r.status}`);
  return r.json();
}

const fmtPct = (p) => (p == null ? "—" : (p * 100).toFixed(1) + "%");

function fmtStat(statId, val) {
  if (val == null) return "—";
  if (statId === 18) return val.toFixed(4).replace(/^0\./, ".");
  if (statId === 47 || statId === 41) return val.toFixed(3);
  return String(Math.round(val));
}

const recordStr = (rec) => rec ? `${rec.W}-${rec.L}-${rec.T}` : "—";

function cellClass(result) {
  if (result === "WIN") return "win";
  if (result === "LOSS") return "loss";
  if (result === "TIE") return "tie";
  return "";
}

const statCells = (blocks) =>
  blocks
    .map((s) => `<td class="num ${cellClass(s.result)}">${fmtStat(s.stat_id, s.score)}</td>`)
    .join("");

const headerCells = (blocks, tbId) =>
  blocks
    .map((c) => {
      const mark = c.stat_id === tbId ? '<span class="tb" title="Tiebreaker">★</span>' : "";
      return `<th class="cat">${c.name}${mark}</th>`;
    })
    .join("");

// ── WP-over-time SVG line chart ──────────────────────────────────────
function renderChart(history, currentModel) {
  if (!history || history.length === 0) return "";
  const pts = history.filter((h) => h.model_version === currentModel);
  if (pts.length === 0) return "";

  const W = 600, H = 140, padL = 40, padR = 12, padT = 12, padB = 22;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const t0 = new Date(pts[0].computed_at).getTime();
  const tN = new Date(pts[pts.length - 1].computed_at).getTime();
  const span = Math.max(tN - t0, 1);
  const x = (t) => padL + ((new Date(t).getTime() - t0) / span) * innerW;
  const y = (p) => padT + (1 - p) * innerH;

  const polyline = (key, cls) => {
    if (pts.length === 1) {
      // Single point — render a dot
      const p = pts[0];
      return `<circle cx="${x(p.computed_at)}" cy="${y(p[key])}" r="3" class="dot ${cls}"></circle>`;
    }
    const path = pts.map((p) => `${x(p.computed_at)},${y(p[key])}`).join(" ");
    const last = pts[pts.length - 1];
    return `
      <polyline class="${cls}" points="${path}"></polyline>
      <circle cx="${x(last.computed_at)}" cy="${y(last[key])}" r="3" class="dot ${cls}"></circle>`;
  };

  // Y-axis grid + labels
  const yTicks = [0, 0.25, 0.5, 0.75, 1.0];
  const gridY = yTicks
    .map((p) => `<line x1="${padL}" y1="${y(p)}" x2="${W - padR}" y2="${y(p)}" class="grid ${p === 0.5 ? "mid" : ""}"></line>`)
    .join("");
  const labelsY = yTicks
    .map((p) => `<text x="${padL - 6}" y="${y(p) + 3}" class="axis">${(p * 100) | 0}%</text>`)
    .join("");

  // X-axis labels: first + last timestamp
  const fmtT = (iso) => {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { weekday: "short" }) + " " +
           d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  };
  const xLabels = `
    <text x="${padL}" y="${H - 6}" class="axis" text-anchor="start">${fmtT(pts[0].computed_at)}</text>
    <text x="${W - padR}" y="${H - 6}" class="axis" text-anchor="end">${fmtT(pts[pts.length - 1].computed_at)}</text>`;

  // Hover targets — one invisible vertical strip per data point. Each
  // carries its timestamp + WPs as data-attrs so the binder can pop up
  // a tooltip without re-doing time→pixel math.
  const stripHalfW = pts.length > 1
    ? Math.max(6, (innerW / Math.max(pts.length - 1, 1)) / 2)
    : 30;
  const hoverPoints = pts.map((p) => {
    const px = x(p.computed_at);
    return `
      <g class="hover-point"
         data-time="${p.computed_at}"
         data-home="${p.home_wp}"
         data-away="${p.away_wp}"
         data-x="${px.toFixed(2)}">
        <line class="hover-cursor" x1="${px}" y1="${padT}" x2="${px}" y2="${padT + innerH}"></line>
        <circle class="hover-dot home" cx="${px}" cy="${y(p.home_wp)}" r="4"></circle>
        <circle class="hover-dot away" cx="${px}" cy="${y(p.away_wp)}" r="4"></circle>
        <rect class="hover-rect" x="${px - stripHalfW}" y="${padT}" width="${stripHalfW * 2}" height="${innerH}"></rect>
      </g>`;
  }).join("");

  return `
    <div class="wp-chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="wp-chart" preserveAspectRatio="xMidYMid meet">
        ${gridY}
        ${polyline("home_wp", "home")}
        ${polyline("away_wp", "away")}
        ${labelsY}
        ${xLabels}
        <g class="hover-layer">${hoverPoints}</g>
      </svg>
      <div class="chart-tooltip" aria-hidden="true"></div>
    </div>`;
}

// Bind mouseenter/mouseleave on each .hover-point. Show a tooltip near the
// hovered data point with timestamp + both teams' WPs; the SVG hover styles
// reveal the cursor line + dots via :hover.
function bindChartHovers(root) {
  const chartW = 600;  // matches the viewBox W in renderChart
  root.querySelectorAll(".wp-chart-wrap").forEach((wrap) => {
    const svg = wrap.querySelector(".wp-chart");
    const tooltip = wrap.querySelector(".chart-tooltip");
    if (!svg || !tooltip) return;
    wrap.querySelectorAll(".hover-point").forEach((pt) => {
      pt.addEventListener("mouseenter", () => {
        const time = new Date(pt.dataset.time);
        const timeStr =
          time.toLocaleDateString(undefined, { weekday: "short" }) + " " +
          time.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
        const homePct = (parseFloat(pt.dataset.home) * 100).toFixed(1);
        const awayPct = (parseFloat(pt.dataset.away) * 100).toFixed(1);
        tooltip.innerHTML = `
          <div class="tt-time">${timeStr}</div>
          <div class="tt-row tt-home"><span class="tt-swatch home"></span>${homePct}%</div>
          <div class="tt-row tt-away"><span class="tt-swatch away"></span>${awayPct}%</div>`;
        // Position in pixel space — map the SVG viewBox x to the rendered width.
        const svgRect = svg.getBoundingClientRect();
        const xVb = parseFloat(pt.dataset.x);
        const xPx = (xVb / chartW) * svgRect.width;
        tooltip.style.left = `${xPx}px`;
        tooltip.classList.add("visible");
      });
      pt.addEventListener("mouseleave", () => {
        tooltip.classList.remove("visible");
      });
    });
  });
}

// ── Details / top-contributors panel ─────────────────────────────────
function impactScore(b) {
  if (b.role === "HIT") return b.exp_r + b.exp_h * 0.6;
  return b.exp_k + b.exp_qs * 4 + b.exp_outs * 0.2;
}

// OPS-style format: 4 sig figs, drop the leading zero (".683"), matching
// the scoreboard column.
const fmtOps = (v) => v.toFixed(3).replace(/^0\./, ".");

// SP first (typically the highest-impact contributors), then RP, then hitters.
// Impact ranks within each role group.
const ROLE_RANK = { SP: 0, RP: 1, HIT: 2 };

function contributorsList(budgets, side) {
  if (!budgets || budgets.length === 0) {
    return `<div class="contrib-empty">No remaining production projected.</div>`;
  }
  const sorted = [...budgets].sort((a, b) => {
    const r = (ROLE_RANK[a.role] ?? 99) - (ROLE_RANK[b.role] ?? 99);
    if (r !== 0) return r;
    return impactScore(b) - impactScore(a);
  });
  const rows = sorted.map((b) => {
    const isPit = b.role === "SP" || b.role === "RP";
    const cells = isPit
      ? `<span class="m">${b.units.toFixed(1)} ${b.role === "SP" ? "starts" : "app"}</span>
         <span class="m">${b.exp_k.toFixed(1)} K</span>
         <span class="m">${b.exp_outs.toFixed(0)} OUT</span>
         ${b.exp_qs > 0.05 ? `<span class="m">${b.exp_qs.toFixed(2)} QS</span>` : ""}
         ${(b.role === "RP" || b.exp_svhd > 0.05) ? `<span class="m">${b.exp_svhd.toFixed(2)} SVHD</span>` : ""}
         ${b.exp_era != null ? `<span class="m">${b.exp_era.toFixed(2)} ERA</span>` : ""}
         ${b.exp_whip != null ? `<span class="m">${b.exp_whip.toFixed(2)} WHIP</span>` : ""}`
      : `<span class="m">${b.units.toFixed(0)} G</span>
         <span class="m">${b.exp_h.toFixed(1)} H</span>
         <span class="m">${b.exp_hr.toFixed(2)} HR</span>
         <span class="m">${b.exp_r.toFixed(1)} R</span>
         <span class="m">${b.exp_sb.toFixed(2)} SB</span>
         ${b.exp_ops != null ? `<span class="m">${fmtOps(b.exp_ops)} OPS</span>` : ""}`;
    return `<li><span class="cname">${b.name}</span><span class="role role-${b.role}">${b.role}</span>${cells}</li>`;
  }).join("");
  return `<ol class="contrib ${side}">${rows}</ol>`;
}

// Per-category sim win rates. Renders a compact table with a probability →
// color gradient so close vs. settled categories jump out at a glance.
function renderCategoryWP(d, cats, m) {
  if (!d.category_wp || !cats) return "";
  const n = d.n_sims;
  const byStat = Object.create(null);
  for (const c of d.category_wp) byStat[c.stat_id] = c;

  const ordered = [...cats.batting, ...cats.pitching];
  const anyTies = ordered.some((c) => {
    const e = byStat[c.stat_id];
    return e && e.ties / n >= 0.005;
  });

  const cell = (p, avg, statId) =>
    `<td class="num catwp-cell" style="--p:${p.toFixed(3)}">
       <span class="catwp-pct">${(p * 100).toFixed(1)}%</span>
       <span class="catwp-avg">${fmtStat(statId, avg)}</span>
     </td>`;

  const rows = ordered.map((c) => {
    const e = byStat[c.stat_id];
    if (!e) return "";
    const h = e.home_wins / n;
    const a = e.away_wins / n;
    const t = e.ties / n;
    const arrow = c.reversed ? ' <span class="cat-rev" title="lower is better">↓</span>' : "";
    return `
      <tr>
        <td class="catwp-name">${c.name}${arrow}</td>
        ${cell(h, e.home_avg, c.stat_id)}
        ${cell(a, e.away_avg, c.stat_id)}
        ${anyTies ? `<td class="num catwp-tie">${(t * 100).toFixed(1)}%</td>` : ""}
      </tr>`;
  }).join("");

  return `
    <h3>Category win rates</h3>
    <p class="catwp-hint">Out of ${n.toLocaleString()} sims — green = usually wins this category, pink = usually loses, neutral = coin flip.</p>
    <table class="catwp">
      <thead>
        <tr>
          <th></th>
          <th>${m.home.name ?? "Home"}</th>
          <th>${m.away.name ?? "Away"}</th>
          ${anyTies ? "<th>tie</th>" : ""}
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderDetails(m, cats, isCurrent) {
  if (!m.details) return "";
  const d = m.details;
  // WP-over-time only makes sense for the current week (no history exists
  // for future weeks before now).
  const chart = isCurrent && m.history && m.history.length > 1
    ? `<h3>Win probability over time</h3>${renderChart(m.history, m.model_version)}`
    : "";
  return `
    <div class="details-inner">
      ${chart}
      ${renderCategoryWP(d, cats, m)}
      <h3>What's driving the projection</h3>
      <div class="details-grid">
        <div>
          <h4>${m.home.name ?? "Home"} <span class="model-tag">${d.model} · ${d.n_sims.toLocaleString()} sims</span></h4>
          ${contributorsList(d.home_budgets, "home")}
        </div>
        <div>
          <h4>${m.away.name ?? "Away"}</h4>
          ${contributorsList(d.away_budgets, "away")}
        </div>
      </div>
    </div>`;
}

// ── Per-matchup render ────────────────────────────────────────────────
function renderMatchup(m, cats, tbId, idx, isCurrent) {
  const home = m.home, away = m.away;
  const homeFav = (home.wp ?? 0.5) > 0.5;
  const awayFav = (away.wp ?? 0.5) > 0.5;

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
    <section class="matchup ${isCurrent ? "" : "future"}">
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
            <th class="record-h">${isCurrent ? "Cats" : ""}</th>
            <th class="wp-h">WP</th>
            ${headerCells(cats.batting, tbId)}
            ${headerCells(cats.pitching, tbId)}
          </tr>
        </thead>
        <tbody>
          ${teamRow(home, homeFav)}
          ${teamRow(away, awayFav)}
        </tbody>
      </table>
      <button class="expand-toggle" aria-expanded="false" aria-controls="details-${idx}">
        <span class="caret">▸</span> Details
      </button>
      <div class="details" id="details-${idx}" hidden>
        ${renderDetails(m, cats, isCurrent)}
      </div>
    </section>`;
}

// Friendly Mon-Sun date range, e.g. "May 25 – May 31".
function fmtDateRange(startIso, endIso) {
  const opts = { month: "short", day: "numeric" };
  const s = new Date(startIso + "T00:00:00").toLocaleDateString(undefined, opts);
  const e = new Date(endIso + "T00:00:00").toLocaleDateString(undefined, opts);
  return `${s} – ${e}`;
}

function renderWeek(data, week) {
  const cats = data.league.categories_by_group;
  const tb = data.league.tiebreaker_stat_id;
  const isCurrent = week.is_current;

  document.getElementById("subtitle").innerHTML =
    `${week.label} · ${fmtDateRange(week.start, week.end)}` +
    (isCurrent ? "" : ' · <span class="future-pill">Projection</span>') +
    ` · ${data.league.size}-team H2H · Tiebreaker: ${data.league.tiebreaker_name}`;

  const root = document.getElementById("matchups");
  root.innerHTML = week.matchups
    .map((m, i) => renderMatchup(m, cats, tb, i, isCurrent))
    .join("");

  // Hook up expand toggles (re-bound on every week switch since DOM is fresh).
  root.querySelectorAll(".expand-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("aria-controls");
      const panel = document.getElementById(id);
      const open = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", open ? "false" : "true");
      panel.hidden = open;
    });
  });
  bindChartHovers(root);
}

function render(data) {
  document.getElementById("league-name").textContent = data.league.name;
  const ts = new Date(data.generated_at);
  const firstModel = data.weeks[0]?.matchups[0]?.model_version ?? "—";
  const select = `
    <label class="week-picker">
      Week
      <select id="week-select">
        ${data.weeks.map((w) => `
          <option value="${w.matchup_period_id}" ${w.is_current ? "selected" : ""}>
            ${w.label}${w.is_current ? " (current)" : ""} · ${fmtDateRange(w.start, w.end)}
          </option>`).join("")}
      </select>
    </label>`;
  document.getElementById("meta").innerHTML =
    `Updated <time datetime="${data.generated_at}">${ts.toLocaleString()}</time>` +
    ` · Model <code>${firstModel}</code>` +
    ` · <button id="about-toggle" class="about-toggle" aria-expanded="false" aria-controls="about-panel">` +
      `<span class="caret">▸</span> How this works</button>` +
    ` · ${select}`;

  const current = data.weeks.find((w) => w.is_current) ?? data.weeks[0];
  renderWeek(data, current);

  document.getElementById("week-select").addEventListener("change", (e) => {
    const periodId = parseInt(e.target.value, 10);
    const w = data.weeks.find((w) => w.matchup_period_id === periodId);
    if (w) renderWeek(data, w);
  });
}

// About / "How this works" toggle — delegated so it works regardless of
// whether the button is in the static HTML or injected by render().
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#about-toggle");
  if (!btn) return;
  const panel = document.getElementById("about-panel");
  const open = btn.getAttribute("aria-expanded") === "true";
  btn.setAttribute("aria-expanded", open ? "false" : "true");
  panel.hidden = open;
  if (!open) panel.scrollIntoView({behavior: "smooth", block: "nearest"});
});

load().then(render).catch((e) => {
  document.getElementById("matchups").textContent = "Error: " + e.message;
});
