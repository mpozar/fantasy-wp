async function load() {
  const r = await fetch("data.json", { cache: "no-store" });
  if (!r.ok) throw new Error(`Failed to load data.json: ${r.status}`);
  return r.json();
}

// Per-week WP history (docs/history/<period>.json) — split out of data.json so
// the scoreboard paints fast, then fetched eagerly in the background right after
// first render (current week first, not waiting for a Details panel to open) and
// hydrated onto week.matchups[].history in place, so everything downstream
// (renderChart, the point-click category table) is unchanged.
// `cache: "no-cache"` revalidates via ETag: settled weeks' files come back as
// cheap 304s on repeat visits; only the live week's (rewritten every publish)
// re-downloads.
async function fetchWeekHistory(week) {
  try {
    const r = await fetch(`history/${week.matchup_period_id}.json`, { cache: "no-cache" });
    if (!r.ok) return null;
    return (await r.json()).history || {};
  } catch { return null; }
}

async function hydrateHistory(week) {
  const hist = await fetchWeekHistory(week);
  week.matchups.forEach((m) => { m.history = (hist && hist[m.matchup_id]) || []; });
  week.historyLoaded = true;
  // If this week is on screen, re-render so its charts fill in.
  if (active.week === week) rerenderPreservingPanels();
}

function loadAllHistory(data, firstWeek) {
  hydrateHistory(firstWeek).then(() =>
    Promise.all(data.weeks.filter((w) => w !== firstWeek).map(hydrateHistory)));
}

// WP-chart SVG viewBox width — shared by renderChart (drawing) and
// bindChartHovers (pixel-space hover mapping) so they can't drift apart.
const CHART_VIEWBOX_W = 600;

// "Mon 3:05 PM" — used for the WP chart x-axis labels and hover tooltip,
// where the span is a single matchup week so the weekday is unambiguous.
const fmtWeekdayTime = (iso) => {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { weekday: "short" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
};

// "Jul 20, 3:05 PM" — the playoff odds chart spans weeks, so its axis and
// tooltip need the date, not a weekday.
const fmtDateTime = (iso) => {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " +
         d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
};

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
// WP-over-time chart. `week` carries the matchup start (week.start) and the
// observed game-day windows (week.active_intervals). `scope` picks the x-axis:
//   "full"    — linear real time over all history; marks where the matchup began.
//   "matchup" — clipped to the week's Monday (drops the pre-matchup projection).
//   "active"  — dead time between game-days collapsed to nothing; one segment
//               per game-day, with a labeled divider between days.
//   "today"   — clipped to the start of the current day's games (the most recent
//               active interval), then linear — a live zoom on today.
function renderChart(history, currentModel, week, scope, ann) {
  if (!history || history.length === 0) return "";
  let pts = history.filter((h) => h.model_version === currentModel);
  if (pts.length === 0) return "";

  const W = CHART_VIEWBOX_W, H = 140, padL = 40, padR = 12, padT = 12, padB = 22;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const tms = (p) => new Date(p.computed_at).getTime();
  const y = (p) => padT + (1 - p) * innerH;

  const cutoff = week && week.start ? new Date(week.start + "T00:00:00").getTime() : null;
  const intervals = (week && week.active_intervals) || [];

  // Build the time→x mapping (`xt`), the visible point set, and divider lines.
  let xt = null;
  let dividers = [];

  if (scope === "active" && intervals.length) {
    // Concatenate each game-day's [start,end] window proportionally to its real
    // duration, dropping the dead gaps between them. Skip days with no plotted
    // points (e.g. early days of a week whose history was trimmed) so the axis
    // doesn't allocate blank space to them.
    const raw = intervals
      .map((iv) => ({ s: new Date(iv.start).getTime(), e: new Date(iv.end).getTime(), date: iv.date }))
      .sort((a, b) => a.s - b.s)
      .filter((seg) => pts.some((p) => tms(p) >= seg.s && tms(p) <= seg.e));
    let cum = 0;
    const segs = raw.map((seg) => {
      const dur = Math.max(seg.e - seg.s, 1);
      const withOffset = { ...seg, dur, offset: cum };
      cum += dur;
      return withOffset;
    });
    const total = Math.max(cum, 1);
    const keep = pts.filter((p) => segs.some((s) => tms(p) >= s.s && tms(p) <= s.e));
    if (keep.length >= 2) {
      pts = keep;
      xt = (t) => {
        const seg = segs.find((s) => t >= s.s && t <= s.e);
        let pos;
        if (seg) {
          pos = seg.offset + (t - seg.s);
        } else if (t <= segs[0].s) {
          pos = 0;                                   // before the first window → left edge
        } else {
          // In a dead gap between game-days (or past the last window): snap to the
          // end of the latest window that began before t — i.e. the divider at the
          // next day — NOT the last day of the week (which flung pre-game events to
          // the far right).
          const prev = segs.filter((s) => s.s <= t).pop() || segs[segs.length - 1];
          pos = prev.offset + prev.dur;
        }
        return padL + (pos / total) * innerW;
      };
      // A divider at the start of each day after the first.
      dividers = segs.slice(1).map((s) => ({ x: padL + (s.offset / total) * innerW, label: fmtDay(s.date) }));
    }
  } else if (scope === "matchup" && cutoff != null) {
    const clipped = pts.filter((p) => tms(p) >= cutoff);
    if (clipped.length >= 2) pts = clipped;
  } else if (scope === "today" && intervals.length) {
    // Clip to the start of the current day's games — the most recent active
    // interval's start — then render linearly: a live zoom on today, starting at
    // today's first pitch (not 24h back). Keep the clip as long as it leaves a
    // point to draw; only fall back to the full range if today has none yet.
    const dayStart = Math.max(...intervals.map((iv) => new Date(iv.start).getTime()));
    const clipped = pts.filter((p) => tms(p) >= dayStart);
    if (clipped.length >= 1) pts = clipped;
  }

  // Linear fallback (full, matchup, or active with no usable intervals).
  if (!xt) {
    const t0 = tms(pts[0]);
    const tN = tms(pts[pts.length - 1]);
    const span = Math.max(tN - t0, 1);
    xt = (t) => padL + ((t - t0) / span) * innerW;
    if (scope === "full" && cutoff != null && cutoff > t0 && cutoff < tN) {
      dividers = [{ x: xt(cutoff), label: "matchup start" }];
    }
  }

  const x = (p) => xt(tms(p));

  const polyline = (key, cls) => {
    if (pts.length === 1) {
      const p = pts[0];
      return `<circle cx="${x(p)}" cy="${y(p[key])}" r="3" class="dot ${cls}"></circle>`;
    }
    const path = pts.map((p) => `${x(p)},${y(p[key])}`).join(" ");
    const last = pts[pts.length - 1];
    return `
      <polyline class="${cls}" points="${path}"></polyline>
      <circle cx="${x(last)}" cy="${y(last[key])}" r="3" class="dot ${cls}"></circle>`;
  };

  // Y-axis grid + labels
  const yTicks = [0, 0.25, 0.5, 0.75, 1.0];
  const gridY = yTicks
    .map((p) => `<line x1="${padL}" y1="${y(p)}" x2="${W - padR}" y2="${y(p)}" class="grid ${p === 0.5 ? "mid" : ""}"></line>`)
    .join("");
  const labelsY = yTicks
    .map((p) => `<text x="${padL - 6}" y="${y(p) + 3}" class="axis">${(p * 100) | 0}%</text>`)
    .join("");

  const dividerSvg = dividers.map((d) =>
    `<line class="matchup-start" x1="${d.x}" y1="${padT}" x2="${d.x}" y2="${padT + innerH}"></line>` +
    (d.label ? `<text x="${d.x}" y="${padT - 3}" class="axis matchup-start-label" text-anchor="middle">${d.label}</text>` : "")
  ).join("");

  // X-axis labels: first + last timestamp
  const xLabels = `
    <text x="${padL}" y="${H - 6}" class="axis start" text-anchor="start">${fmtWeekdayTime(pts[0].computed_at)}</text>
    <text x="${W - padR}" y="${H - 6}" class="axis" text-anchor="end">${fmtWeekdayTime(pts[pts.length - 1].computed_at)}</text>`;

  // Hover targets — one invisible vertical strip per data point.
  const stripHalfW = pts.length > 1
    ? Math.max(6, (innerW / Math.max(pts.length - 1, 1)) / 2)
    : 30;
  const hoverPoints = pts.map((p) => {
    const px = x(p);
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

  // Annotations in two layers: faint span bands + event guide-lines BEHIND the
  // curves (visual only, no pointer events), and interactive markers ON TOP of
  // the hover layer so they're reliably hoverable/tappable (the full-height WP
  // hover strips used to cover the triangles). Detail shows via the chart tooltip
  // (bindChartHovers binds .annot-hit) — events along the top edge, trend-span
  // handles along the bottom edge, so neither competes with the WP hover.
  let annotBg = "", annotTop = "";
  if (ann && (ann.events || ann.spans)) {
    const t0 = tms(pts[0]), tN = tms(pts[pts.length - 1]);
    const vis = (iso) => { const t = new Date(iso).getTime(); return t >= t0 && t <= tN; };
    const spans = (ann.spans || []).filter((s) => vis(s.start) || vis(s.end));
    const events = (ann.events || []).filter((e) => vis(e.at));
    const pp = (d) => `${d > 0 ? "+" : ""}${Math.round(d * 100)}pp`;
    const spanX = (s) => {
      const x0 = Math.max(padL, xt(new Date(s.start).getTime()));
      const x1 = Math.min(W - padR, xt(new Date(s.end).getTime()));
      return { x0, w: Math.max(x1 - x0, 2) };
    };
    annotBg =
      spans.map((s) => { const { x0, w } = spanX(s);
        return `<rect class="annot-span ${s.dir}" x="${x0.toFixed(1)}" y="${padT}" width="${w.toFixed(1)}" height="${innerH}"></rect>`;
      }).join("") +
      events.map((e) => { const ex = xt(new Date(e.at).getTime());
        return `<line class="annot-vline ${e.side}" x1="${ex.toFixed(1)}" y1="${padT}" x2="${ex.toFixed(1)}" y2="${padT + innerH}"></line>`;
      }).join("");
    annotTop =
      spans.map((s) => { const { x0, w } = spanX(s);
        return `<g class="annot-hit" data-label="${escHtml(s.label + " (" + pp(s.wp_delta) + ")")}" data-x="${(x0 + w / 2).toFixed(1)}">` +
          `<rect class="annot-hit-area" x="${x0.toFixed(1)}" y="${(padT + innerH - 9).toFixed(1)}" width="${w.toFixed(1)}" height="11"></rect>` +
          `<rect class="annot-span-bar ${s.dir}" x="${x0.toFixed(1)}" y="${(padT + innerH - 4).toFixed(1)}" width="${w.toFixed(1)}" height="3"></rect></g>`;
      }).join("") +
      events.map((e) => { const ex = xt(new Date(e.at).getTime());
        return `<g class="annot-hit annot-event ${e.side}" data-label="${escHtml(e.label + " (" + pp(e.wp_delta) + ")")}" data-x="${ex.toFixed(1)}">` +
          `<rect class="annot-hit-area" x="${(ex - 7).toFixed(1)}" y="${(padT - 5).toFixed(1)}" width="14" height="16"></rect>` +
          `<polygon class="annot-tri" points="${(ex - 4).toFixed(1)},${(padT - 3).toFixed(1)} ${(ex + 4).toFixed(1)},${(padT - 3).toFixed(1)} ${ex.toFixed(1)},${(padT + 4).toFixed(1)}"></polygon></g>`;
      }).join("");
  }

  return `
    <div class="wp-chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="wp-chart" preserveAspectRatio="xMidYMid meet">
        ${gridY}
        ${dividerSvg}
        <g class="annot-bg">${annotBg}</g>
        ${polyline("home_wp", "home")}
        ${polyline("away_wp", "away")}
        ${labelsY}
        ${xLabels}
        <g class="hover-layer">${hoverPoints}</g>
        <g class="annot-top">${annotTop}</g>
      </svg>
      <div class="chart-tooltip" aria-hidden="true"></div>
    </div>`;
}

// Bind mouseenter/mouseleave on each .hover-point. Show a tooltip near the
// hovered data point with timestamp + both teams' WPs; the SVG hover styles
// reveal the cursor line + dots via :hover.
function bindChartHovers(root) {
  const chartW = CHART_VIEWBOX_W;  // matches the viewBox W in renderChart
  root.querySelectorAll(".wp-chart-wrap").forEach((wrap) => {
    const svg = wrap.querySelector(".wp-chart");
    const tooltip = wrap.querySelector(".chart-tooltip");
    if (!svg || !tooltip) return;
    // Annotation markers: show the label in the chart tooltip on hover AND tap
    // (so the detail is reachable on mobile too). These sit on top of the WP
    // hover strips, so they win the pointer in their small zones.
    wrap.querySelectorAll(".annot-hit").forEach((el) => {
      const show = () => {
        tooltip.innerHTML = `<div class="tt-annot">${el.dataset.label}</div>`;
        const svgRect = svg.getBoundingClientRect();
        tooltip.style.left = `${(parseFloat(el.dataset.x) / chartW) * svgRect.width}px`;
        tooltip.classList.add("visible");
      };
      el.addEventListener("mouseenter", show);
      el.addEventListener("mouseleave", () => tooltip.classList.remove("visible"));
      el.addEventListener("click", (e) => { e.stopPropagation(); show(); });
    });
    wrap.querySelectorAll(".hover-point").forEach((pt) => {
      pt.addEventListener("mouseenter", () => {
        const timeStr = fmtWeekdayTime(pt.dataset.time);
        const homePct = (parseFloat(pt.dataset.home) * 100).toFixed(1);
        const awayPct = (parseFloat(pt.dataset.away) * 100).toFixed(1);
        tooltip.innerHTML = `
          <div class="tt-time">${timeStr}</div>
          <div class="tt-row"><span class="tt-swatch home"></span>${homePct}%</div>
          <div class="tt-row"><span class="tt-swatch away"></span>${awayPct}%</div>`;
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
      // Click a point → show the category win rates as they were at that time.
      // Only live-week points carry per-snapshot category_wp (see _matchup_block);
      // for other weeks this is a no-op.
      pt.addEventListener("click", () => {
        const detailsEl = pt.closest(".details");
        if (!detailsEl) return;
        const idx = Number(detailsEl.id.replace("details-", ""));
        const m = active.week && active.week.matchups[idx];
        if (!m) return;
        const point = (m.history || []).find(
          (h) => h.computed_at === pt.dataset.time && h.category_wp);
        if (!point) return;  // no category history at this point (e.g. past week)
        const cats = active.data.league.categories_by_group;
        const panel = document.getElementById("catwp-" + idx);
        if (!panel) return;
        wrap.querySelectorAll(".hover-point.selected")
          .forEach((el) => el.classList.remove("selected"));
        pt.classList.add("selected");
        panel.innerHTML = categoryPanel(
          { category_wp: point.category_wp, n_sims: point.n_sims }, cats, m, point.computed_at);
        panel.querySelector(".catwp-live").addEventListener("click", () => {
          pt.classList.remove("selected");
          panel.innerHTML = categoryPanel(m.details, cats, m, null);
        });
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

// Friendly snapshot timestamp for the "as of" banner, e.g. "Thu, Jun 5, 10:05 AM".
function fmtSnapTime(iso) {
  const t = new Date(iso);
  return t.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) +
         ", " + t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

// Category-win-rates panel content. `asOfIso` set → prepend an "as of <time>"
// banner with a button to return to the week's latest table (used when a past
// chart point is clicked). "Latest" reads correctly for both the live week
// (current values) and a finished week (final values).
function categoryPanel(source, cats, m, asOfIso) {
  const banner = asOfIso
    ? `<div class="catwp-asof">Category win rates as of <strong>${fmtSnapTime(asOfIso)}</strong>` +
      `<button type="button" class="catwp-live">↩ Return to latest</button></div>`
    : "";
  return banner + renderCategoryWP(source, cats, m);
}

function renderDetails(m, cats, week, scope, idx) {
  if (!m.details) return "";
  const d = m.details;
  // WP-over-time renders whenever snapshot history exists — for the live week
  // and for past weeks (upcoming weeks have no history yet).
  const chart = m.history && m.history.length > 1
    ? `<h3>Win probability over time</h3>${renderChart(m.history, m.model_version, week, scope, chartAnnotate ? summaryCache[m.matchup_id] : null)}`
    : (!week.historyLoaded
        ? `<h3>Win probability over time</h3><div class="chart-loading">Loading chart…</div>`
        : "");
  // Weekly write-up (if a summary has been generated). Filled from cache on
  // re-render; the expand handler injects it on first open after the lazy fetch.
  const sum = summaryCache[m.matchup_id];
  const writeup = sum && sum.writeup
    ? `<h3>Weekly summary</h3><div class="matchup-writeup" id="writeup-${idx}">` +
      (sum.result ? `<p class="wu-result"><strong>${escHtml(sum.result)}</strong></p>` : "") +
      `${mdToHtml(sum.writeup)}</div>`
    : `<div class="matchup-writeup" id="writeup-${idx}" hidden></div>`;
  return `
    <div class="details-inner">
      ${chart}
      ${writeup}
      <div class="catwp-panel" id="catwp-${idx}">${categoryPanel(d, cats, m, null)}</div>
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
function renderMatchup(m, cats, tbId, idx, started, week, scope) {
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
    <section class="matchup ${started ? "" : "future"}">
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
            <th class="record-h">${started ? "Cats" : ""}</th>
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
      <button class="expand-toggle" aria-expanded="false" aria-controls="details-${idx}" data-mid="${m.matchup_id}">
        <span class="caret">▸</span> Details
      </button>
      <div class="details" id="details-${idx}" hidden>
        ${renderDetails(m, cats, week, scope, idx)}
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

// Short day label for chart dividers, e.g. "Jun 2". Input is an MLB official
// date string ("YYYY-MM-DD").
function fmtDay(dateStr) {
  return new Date(dateStr + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// WP-chart x-axis scope, chosen by the segmented control:
//   "full"    — entire history
//   "matchup" — clipped to the week's start
//   "active"  — dead time between game-days collapsed, day dividers (default)
//   "today"   — clipped to the start of the current day's games
// Falls back to the full linear range for weeks with no active intervals
// (e.g. upcoming weeks). Global; `active` holds the currently displayed week
// so the control can re-render in place.
let chartScope = "active";
const active = { data: null, week: null };

// Per-matchup summary file (docs/annotations/<id>.json): chart `events`/`spans`
// for the overlay AND an optional markdown `writeup` shown in Details. Loaded
// lazily — separate tiny files, never in data.json. `null` cached = fetched, none
// exist (generate via /matchup-summary). The chart overlay also needs the toggle
// on (chartAnnotate); the write-up shows whenever a panel is expanded.
let chartAnnotate = false;
const summaryCache = {};   // matchup_id -> {events, spans, writeup, result} | null

const escHtml = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function fetchSummary(mid) {
  if (mid in summaryCache) return summaryCache[mid];
  try {
    const r = await fetch(`annotations/${mid}.json`, { cache: "no-store" });
    summaryCache[mid] = r.ok ? await r.json() : null;
  } catch { summaryCache[mid] = null; }
  return summaryCache[mid];
}

async function ensureSummaries(week) {
  await Promise.all(week.matchups.map((m) => fetchSummary(m.matchup_id)));
}

// Minimal, safe Markdown → HTML for the persisted write-up (we author it, but
// escape anyway). Supports headings (#..), **bold**, "- " bullet lists, and
// blank-line paragraphs — no tables/raw HTML (the chart already shows the arc).
function mdToHtml(md) {
  const inline = (s) => escHtml(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const lines = String(md).replace(/\r/g, "").split("\n");
  let html = "", inList = false, para = [];
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^#{1,6}\s/.test(line)) {
      flushPara(); closeList();
      const lvl = Math.min(line.match(/^#+/)[0].length + 3, 5);   // # -> h4, ## / ### -> h5
      html += `<h${lvl}>${inline(line.replace(/^#+\s/, ""))}</h${lvl}>`;
    } else if (/^[-*]\s/.test(line)) {
      flushPara(); if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(line.replace(/^[-*]\s/, ""))}</li>`;
    } else if (line === "") {
      flushPara(); closeList();
    } else { para.push(line); }
  }
  flushPara(); closeList();
  return html;
}

// Re-render the current week, preserving which detail panels are expanded.
function rerenderPreservingPanels() {
  const openIds = [...document.querySelectorAll('.expand-toggle[aria-expanded="true"]')]
    .map((b) => b.getAttribute("aria-controls"));
  if (active.week) renderWeek(active.data, active.week);
  openIds.forEach((id) => {
    const panel = document.getElementById(id);
    const tog = document.querySelector(`.expand-toggle[aria-controls="${id}"]`);
    if (panel && tog) { panel.hidden = false; tog.setAttribute("aria-expanded", "true"); }
  });
}
const CHART_SCOPES = [
  { id: "full", label: "Full" },
  { id: "matchup", label: "Matchup" },
  { id: "active", label: "Active" },
  { id: "today", label: "Today" },
];

// A week is "started" once any of its games has begun (state set server-side
// from game statuses). Started weeks show real scores; upcoming weeks are
// pure projections.
function isStarted(week) {
  return week.state !== "upcoming";
}

// Default to the latest week that has started — keeps last week's results
// visible until the new week's first game goes live, then flips on its own.
// No wall clock involved.
function pickDefaultWeek(data) {
  const started = data.weeks.filter(isStarted);
  return started.length ? started[started.length - 1] : data.weeks[0];
}

function renderWeek(data, week) {
  active.data = data;
  active.week = week;
  const cats = data.league.categories_by_group;
  const tb = data.league.tiebreaker_stat_id;
  const started = isStarted(week);

  const root = document.getElementById("matchups");
  root.innerHTML = week.matchups
    .map((m, i) => renderMatchup(m, cats, tb, i, started, week, chartScope))
    .join("");

  // Hook up expand toggles (re-bound on every week switch since DOM is fresh).
  root.querySelectorAll(".expand-toggle").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("aria-controls");
      const panel = document.getElementById(id);
      const open = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", open ? "false" : "true");
      panel.hidden = open;
      if (open) return;   // just collapsed — nothing to load
      // First open: lazily fetch this matchup's summary and show its write-up.
      const mid = Number(btn.dataset.mid);
      const idx = id.replace("details-", "");
      const sum = await fetchSummary(mid);
      const wEl = document.getElementById(`writeup-${idx}`);
      if (wEl && sum && sum.writeup && !wEl.innerHTML) {
        wEl.innerHTML =
          (sum.result ? `<p class="wu-result"><strong>${escHtml(sum.result)}</strong></p>` : "") +
          mdToHtml(sum.writeup);
        // ensure the "Weekly summary" heading exists (renderDetails omits it when
        // the summary wasn't cached at render time)
        if (!wEl.previousElementSibling || wEl.previousElementSibling.textContent !== "Weekly summary") {
          wEl.insertAdjacentHTML("beforebegin", "<h3>Weekly summary</h3>");
        }
        wEl.hidden = false;
      }
    });
  });
  bindChartHovers(root);
}

// Game-day spotlight — an authored, auto-expiring card pinned above the matchups.
// Content lives in spotlight.json (LLM-authored, like the chart annotations); this
// renders it only when `active` and its `date` equals "today" in the file's
// timezone, so the card removes itself once the day passes — no cleanup needed, and
// fast.sh never touches it (it only writes data.json). Live WP badges are pulled
// from the already-loaded `data` so the numbers stay current even though the prose
// is fixed as-of-first-pitch.
async function renderSpotlight(data) {
  const el = document.getElementById("spotlight");
  if (!el) return;
  let s;
  try {
    const r = await fetch("spotlight.json", { cache: "no-store" });
    if (!r.ok) { el.hidden = true; return; }
    s = await r.json();
  } catch { el.hidden = true; return; }
  const today = new Date().toLocaleDateString("en-CA", s.tz ? { timeZone: s.tz } : {});
  if (!s.active || s.date !== today) { el.hidden = true; el.innerHTML = ""; return; }

  const byId = {};
  data.weeks.forEach((w) => (w.matchups || []).forEach((m) => { byId[m.matchup_id] = m; }));
  const liveWp = (mid, manager) => {
    const m = byId[mid];
    if (!m) return "";
    const mine = m.home.name === manager ? m.home : m.away.name === manager ? m.away : null;
    if (!mine) return "";
    const opp = mine === m.home ? m.away : m.home;
    const pct = (x) => `${Math.round(x.wp * 100)}%`;
    return `<span class="spot-wp" title="current win probability">${pct(mine)}` +
           `<span class="spot-wp-opp"> · vs ${escHtml(opp.name)} ${pct(opp)}</span></span>`;
  };
  const teamBadge = (t) => `<span class="spot-team ${t === "SEA" ? "sea" : "tor"}">${escHtml(t)}</span>`;

  const watchRows = (s.watch || []).map((p) => `
    <tr>
      <td class="spot-player">${teamBadge(p.team)} ${escHtml(p.player)}</td>
      <td class="spot-mgr">${escHtml(p.manager)} ${liveWp(p.matchup_id, p.manager)}</td>
      <td class="spot-watch">${escHtml(p.watch)}</td>
    </tr>`).join("");
  const minorRows = (s.minor || []).map((m) =>
    `<li><strong>${escHtml(m.label)}:</strong> ${escHtml(m.body)}</li>`).join("");
  const noteBlocks = (s.notes || []).map((n) =>
    `<div class="spot-note"><h4>${escHtml(n.title)}</h4><p>${escHtml(n.body)}</p></div>`).join("");

  el.innerHTML =
    `<div class="spot-head"><h2>${escHtml(s.headline)}</h2>` +
    `<button class="spot-close" aria-label="Hide spotlight" title="Hide">✕</button></div>` +
    (s.subhead ? `<p class="spot-sub">${escHtml(s.subhead)}</p>` : "") +
    `<h3 class="spot-h">🔴 Watch these</h3>` +
    `<div class="spot-table-wrap"><table class="spot-table">` +
    `<thead><tr><th>Player</th><th>Manager · WP</th><th>What to watch</th></tr></thead>` +
    `<tbody>${watchRows}</tbody></table></div>` +
    (noteBlocks ? `<div class="spot-notes">${noteBlocks}</div>` : "") +
    (minorRows ? `<details class="spot-minor"><summary>⚪ Barely matters / out tonight</summary><ul>${minorRows}</ul></details>` : "");
  el.hidden = false;
  const close = el.querySelector(".spot-close");
  if (close) close.addEventListener("click", () => { el.hidden = true; });
}

// ── Playoff odds section ──────────────────────────────────────────────
// Rendered from playoffs.json (written by `app playoffs` on the medium cron
// tier: 10k season sims over the remaining schedule's WPs + a sampled-team-week
// bracket sim). Fetched in the background after first paint like the history
// files; a missing file (first deploy, playoffs over) leaves the section hidden.
// Categorical palette (validated reference set): the top 6 teams by playoff
// odds get distinct hues; the rest render muted gray — 12 distinct hues would
// be unreadable. The tail's identity is reachable via the line hover tooltip,
// the matching chip in the table, and by PINNING: clicking a line, its end
// label, or the team's table row toggles the team into `poPinned`, which
// renders it full-color (gray-tail teams borrow a hue from PO_PIN_COLORS)
// and dims everything unpinned. Light values; the site is light-only.
const PO_COLORS = ["#2a78d6", "#008300", "#e87ba4", "#eda100", "#1baf7a", "#eb6834"];
const PO_GRAY = "#b9bfc9";
// Reserve hues for pinned gray-tail teams — distinct from PO_COLORS so a
// pinned tail team never collides with a top-6 line.
const PO_PIN_COLORS = ["#8a5cd6", "#c2417a", "#0aa2c0", "#946846", "#5b6ee1", "#8e9c1a"];
let poPinned = new Set(); // String(team_id) — dataset attrs are strings
const PO_METRICS = [
  { idx: 0, label: "Playoffs" },
  { idx: 1, label: "Bye" },
  { idx: 2, label: "Champion" },
];
let poMetric = 0;

const poPct = (x) =>
  x >= 0.9995 ? "100%" :
  x < 0.0005 ? "—" :
  (x * 100).toFixed(x >= 0.095 ? 0 : 1) + "%";

// Odds-over-time line chart: one series per team from payload.history
// (one point per `app playoffs` run), y = the toggled metric. Same SVG
// idiom as renderChart; gray tail drawn first so colored lines paint on top.
function renderPoChart(p, colorOf) {
  const hist = (p.history || []).filter((h) => h.teams);
  if (!hist.length) return "";
  const W = CHART_VIEWBOX_W, H = 300, padL = 40, padR = 46, padT = 12, padB = 22;
  const innerH = H - padT - padB;
  const t0 = new Date(hist[0].t).getTime();
  const tN = new Date(hist[hist.length - 1].t).getTime();
  const span = Math.max(tN - t0, 1);
  const x = (t) => padL + ((t - t0) / span) * (W - padL - padR);
  const y = (v) => padT + (1 - v) * innerH;

  const yTicks = [0, 0.25, 0.5, 0.75, 1.0];
  const grid = yTicks.map((v) =>
    `<line x1="${padL}" y1="${y(v)}" x2="${W - padR}" y2="${y(v)}" class="grid ${v === 0.5 ? "mid" : ""}"></line>` +
    `<text x="${padL - 6}" y="${y(v) + 3}" class="axis">${(v * 100) | 0}%</text>`).join("");
  const xLabels = hist.length > 1 ? `
    <text x="${padL}" y="${H - 6}" class="axis start" text-anchor="start">${fmtDateTime(hist[0].t)}</text>
    <text x="${W - padR}" y="${H - 6}" class="axis" text-anchor="end">${fmtDateTime(hist[hist.length - 1].t)}</text>` : "";

  // Colored series last (paint on top of the gray tail), pinned last of all,
  // then direct labels.
  const teams = [...p.teams].reverse()
    .sort((a, b) => (poPinned.has(String(a.team_id)) ? 1 : 0) - (poPinned.has(String(b.team_id)) ? 1 : 0));
  let lines = "", hovers = "";
  const labels = [];
  for (const t of teams) {
    const color = colorOf(t.team_id);
    const pinned = poPinned.has(String(t.team_id));
    const cls = `po-line${pinned ? " pinned" : ""}`;
    const pts = hist
      .map((h) => ({ h, v: (h.teams[t.team_id] || [])[poMetric] }))
      .filter((e) => e.v != null);
    if (!pts.length) continue;
    const coords = pts.map((e) => `${x(new Date(e.h.t).getTime()).toFixed(1)},${y(e.v).toFixed(1)}`);
    lines += pts.length === 1
      ? `<circle cx="${coords[0].split(",")[0]}" cy="${coords[0].split(",")[1]}" r="3" fill="${color}" class="${cls}" data-team="${t.team_id}"></circle>`
      : `<polyline points="${coords.join(" ")}" fill="none" stroke="${color}" stroke-width="${pinned ? 2.5 : 2}" stroke-linejoin="round" stroke-linecap="round" class="${cls}" data-team="${t.team_id}"></polyline>`;
    hovers += `<polyline points="${coords.join(" ")}" fill="none" stroke="transparent" stroke-width="14" class="po-hover" data-abbrev="${escHtml(t.abbrev ?? t.name)}" data-team="${t.team_id}"></polyline>`;
    if (color !== PO_GRAY) {
      labels.push({ y: y(pts[pts.length - 1].v), color, text: t.abbrev ?? "", team: t.team_id, pinned });
    }
  }
  // End-of-line labels for the colored series, nudged apart (≥11px): a forward
  // pass pushes overlaps down, then a backward pass pushes everything that ran
  // past the bottom edge back up (keeping the spacing) — a plain bottom clamp
  // would stack rail-hugging labels on top of each other.
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) {
    labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 11);
  }
  let maxLabelY = padT + innerH;
  for (let i = labels.length - 1; i >= 0; i--) {
    labels[i].y = Math.min(labels[i].y, maxLabelY);
    maxLabelY = labels[i].y - 11;
  }
  const labelSvg = labels.map((l) =>
    `<text x="${W - padR + 4}" y="${l.y + 3}" class="po-label${l.pinned ? " pinned" : ""}" data-team="${l.team}" fill="${l.color}">${l.text}</text>`).join("");

  return `
    <div class="po-chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" class="po-chart${poPinned.size ? " has-pins" : ""}" preserveAspectRatio="xMidYMid meet">
        ${grid}${xLabels}${lines}${labelSvg}
        <g class="po-hover-layer">${hovers}</g>
      </svg>
      <div class="chart-tooltip" aria-hidden="true"></div>
    </div>`;
}

// Hover: nearest run by x on the hovered team's line → "ABB 34% · Mon 3:05 PM".
// Click (works as tap on touch, where hover doesn't exist): onToggle pins/unpins
// the team via renderPlayoffs' togglePin.
function bindPoChartHovers(el, p, onToggle) {
  const wrap = el.querySelector(".po-chart-wrap");
  if (!wrap) return;
  const svg = wrap.querySelector("svg");
  const tooltip = wrap.querySelector(".chart-tooltip");
  const hist = (p.history || []).filter((h) => h.teams);
  const t0 = new Date(hist[0].t).getTime();
  const tN = new Date(hist[hist.length - 1].t).getTime();
  const rowOf = (tid) => el.querySelector(`.po-table tbody tr[data-team="${tid}"]`);
  wrap.querySelectorAll(".po-label").forEach((lbl) => {
    lbl.addEventListener("click", () => onToggle && onToggle(lbl.dataset.team));
  });
  wrap.querySelectorAll(".po-hover").forEach((line) => {
    line.addEventListener("click", () => onToggle && onToggle(line.dataset.team));
    line.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0,
        ((e.clientX - rect.left) / rect.width * CHART_VIEWBOX_W - 40) / (CHART_VIEWBOX_W - 40 - 46)));
      const target = t0 + frac * Math.max(tN - t0, 1);
      let best = hist[0], bd = Infinity;
      for (const h of hist) {
        const d = Math.abs(new Date(h.t).getTime() - target);
        if (d < bd) { bd = d; best = h; }
      }
      const v = (best.teams[line.dataset.team] || [])[poMetric];
      if (v == null) return;
      tooltip.innerHTML = `<div class="tt-time">${fmtDateTime(best.t)}</div>` +
        `<div class="tt-row">${escHtml(line.dataset.abbrev)} ${poPct(v)}</div>`;
      tooltip.style.left = `${e.clientX - rect.left}px`;
      tooltip.classList.add("visible");
    });
    line.addEventListener("mouseenter", () => {
      svg.classList.add("dimming");
      const vis = svg.querySelector(`.po-line[data-team="${line.dataset.team}"]`);
      if (vis) vis.classList.add("hot");
      const row = rowOf(line.dataset.team);
      if (row) row.classList.add("hot");
    });
    line.addEventListener("mouseleave", () => {
      tooltip.classList.remove("visible");
      svg.classList.remove("dimming");
      svg.querySelectorAll(".po-line.hot").forEach((l) => l.classList.remove("hot"));
      el.querySelectorAll(".po-table tr.hot").forEach((r) => r.classList.remove("hot"));
    });
  });
}

async function renderPlayoffs() {
  const el = document.getElementById("playoffs");
  if (!el) return;
  let p;
  try {
    const r = await fetch("playoffs.json", { cache: "no-cache" });
    if (!r.ok) return;
    p = await r.json();
  } catch { return; }

  // Stable per-team color: top 6 of the current payload sort get the palette,
  // the gray tail gets a reserved pin hue that shows only while pinned.
  const baseColor = {}, pinColor = {};
  p.teams.forEach((t, i) => {
    baseColor[t.team_id] = i < PO_COLORS.length ? PO_COLORS[i] : PO_GRAY;
    if (i >= PO_COLORS.length) {
      pinColor[t.team_id] = PO_PIN_COLORS[(i - PO_COLORS.length) % PO_PIN_COLORS.length];
    }
  });
  const colorOf = (tid) =>
    (poPinned.has(String(tid)) && pinColor[tid]) || baseColor[tid] || PO_GRAY;

  const cell = (x, cls = "") =>
    `<td class="num po-cell ${cls}" style="--p:${x.toFixed(3)}">${poPct(x)}</td>`;
  const seedCells = (t) =>
    t.seed_dist.slice(0, p.playoff_team_count)
      .map((s) => cell(s, "po-seed")).join("");
  const rows = p.teams.map((t) => `
    <tr data-team="${t.team_id}"${poPinned.has(String(t.team_id)) ? ' class="po-pinned"' : ""}>
      <td class="po-team">
        <div class="team-name"><span class="po-chip" style="background:${colorOf(t.team_id)}"></span>${escHtml(t.name ?? "")}</div>
        <div class="team-owner">${escHtml(t.owner ?? "")}</div>
      </td>
      <td class="num po-rec">${t.w}–${t.l}</td>
      ${cell(t.p_playoffs)}${cell(t.p_bye)}${cell(t.p_final)}${cell(t.p_champion)}
      ${seedCells(t)}
    </tr>`).join("");

  const metricControl =
    `<span class="chart-scope" role="group" aria-label="Odds metric">` +
    `<span class="chart-scope-label">Odds</span>` +
    PO_METRICS.map((m) =>
      `<button class="scope-btn po-metric-btn${m.idx === poMetric ? " active" : ""}" data-pom="${m.idx}">${m.label}</button>`
    ).join("") + `</span>`;

  el.innerHTML = `
    <h2>Playoff odds</h2>
    <p class="po-hint">${p.n_sims.toLocaleString()} simulated seasons — every remaining
      matchup drawn from its current win probability (live for this week), then the
      ${p.playoff_team_count}-team bracket (top ${p.bye_seeds} seeds bye, 1-week rounds,
      seeding ties: head-to-head, then coin flip) played out with simulated
      September team-weeks.</p>
    <div class="po-table-wrap"><table class="po-table">
      <thead><tr>
        <th></th><th>Record</th><th>Playoffs</th><th>Bye</th><th>Final</th><th>Champ</th>
        ${Array.from({ length: p.playoff_team_count }, (_, i) => `<th class="po-seed-h">#${i + 1}</th>`).join("")}
      </tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${(p.history || []).length ? `
    <div class="po-chart-head"><h3>Odds over time</h3>${metricControl}
      <span class="po-pin-hint">tap a line or table row to pin a team</span></div>
    <div id="po-chart-box">${renderPoChart(p, colorOf)}</div>` : ""}
    <p class="po-foot">Updated <time datetime="${p.generated_at}">${fmtSnapTime(p.generated_at)}</time>.
      Bracket weeks use today's rosters and rest-of-season projections — September
      call-ups, trades, and injuries aren't knowable, so odds at the extremes read
      a touch overconfident. #1–#${p.playoff_team_count} columns are seed probabilities.</p>`;
  el.hidden = false;

  // Re-render the chart (pins/metric changed) and sync the table's pinned
  // state + chip colors in place — the table itself is never rebuilt, so its
  // row listeners survive.
  function refreshPoChart() {
    const box = document.getElementById("po-chart-box");
    if (box) { box.innerHTML = renderPoChart(p, colorOf); bindPoChartHovers(el, p, togglePin); }
    el.querySelectorAll(".po-table tbody tr").forEach((row) => {
      const tid = row.dataset.team;
      row.classList.toggle("po-pinned", poPinned.has(tid));
      const chip = row.querySelector(".po-chip");
      if (chip) chip.style.background = colorOf(tid);
    });
  }
  function togglePin(tid) {
    tid = String(tid);
    if (poPinned.has(tid)) poPinned.delete(tid); else poPinned.add(tid);
    refreshPoChart();
  }
  bindPoChartHovers(el, p, togglePin);

  // Table rows double as the chart legend: hover highlights the team's line,
  // click/tap pins it (same toggle as clicking the line itself).
  function setLineHot(tid, on) {
    const svg = el.querySelector(".po-chart");
    if (!svg) return;
    svg.classList.toggle("dimming", on);
    const line = svg.querySelector(`.po-line[data-team="${tid}"]`);
    if (line) line.classList.toggle("hot", on);
  }
  el.querySelectorAll(".po-table tbody tr").forEach((row) => {
    row.addEventListener("click", () => togglePin(row.dataset.team));
    row.addEventListener("mouseenter", () => setLineHot(row.dataset.team, true));
    row.addEventListener("mouseleave", () => setLineHot(row.dataset.team, false));
  });

  el.querySelectorAll(".po-metric-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = Number(btn.dataset.pom);
      if (next === poMetric) return;
      poMetric = next;
      el.querySelectorAll(".po-metric-btn").forEach((b) =>
        b.classList.toggle("active", Number(b.dataset.pom) === next));
      refreshPoChart();
    });
  });
}

function render(data) {
  document.getElementById("league-name").textContent = data.league.name;
  const ts = new Date(data.generated_at);
  const defaultWeek = pickDefaultWeek(data);
  // Model version of whatever week we land on; fall back to the first week that
  // has any computed snapshot (weeks before the season's first sim show none).
  const firstModel =
    defaultWeek.matchups.find((m) => m.model_version)?.model_version ??
    data.weeks.flatMap((w) => w.matchups).find((m) => m.model_version)?.model_version ??
    "—";
  const stateTag = (w) =>
    w.state === "live" ? " · live" : w.state === "upcoming" ? " · projection" : "";
  const select = `
    <label class="week-picker">
      Week
      <select id="week-select">
        ${data.weeks.map((w) => `
          <option value="${w.matchup_period_id}" ${w.matchup_period_id === defaultWeek.matchup_period_id ? "selected" : ""}>
            ${w.label} · ${fmtDateRange(w.start, w.end)}${stateTag(w)}
          </option>`).join("")}
      </select>
    </label>`;
  const scopeControl =
    `<span class="chart-scope" role="group" aria-label="Graph time axis">` +
    `<span class="chart-scope-label">Graph</span>` +
    CHART_SCOPES.map((s) =>
      `<button class="scope-btn${s.id === chartScope ? " active" : ""}" data-scope="${s.id}">${s.label}</button>`
    ).join("") +
    `</span>`;

  const annotControl =
    `<button id="annot-toggle" class="annot-btn${chartAnnotate ? " active" : ""}" ` +
    `aria-pressed="${chartAnnotate}" title="Overlay major events & trends (lazily loaded; ` +
    `generate via /matchup-summary)">✦ Annotate</button>`;

  // Primary controls (what you're looking at) in the toolbar; passive metadata
  // on a smaller line below.
  document.getElementById("toolbar").innerHTML = select + scopeControl + annotControl;
  document.getElementById("meta").innerHTML =
    `Updated <time datetime="${data.generated_at}">${ts.toLocaleString()}</time>` +
    ` · Model <code>${firstModel}</code>` +
    ` · <button id="about-toggle" class="about-toggle" aria-expanded="false" aria-controls="about-panel">` +
      `<span class="caret">▸</span> How this works</button>`;

  renderSpotlight(data);
  renderWeek(data, defaultWeek);

  document.getElementById("week-select").addEventListener("change", async (e) => {
    const periodId = parseInt(e.target.value, 10);
    const w = data.weeks.find((w) => w.matchup_period_id === periodId);
    if (!w) return;
    if (chartAnnotate) await ensureSummaries(w);   // load this week's annotations first
    renderWeek(data, w);
  });

  // Annotate toggle: lazily load this week's annotation files, then overlay them
  // on the chart (whatever scope is selected). Off → instant, no extra fetches.
  document.getElementById("annot-toggle").addEventListener("click", async (e) => {
    chartAnnotate = !chartAnnotate;
    const btn = e.currentTarget;
    btn.classList.toggle("active", chartAnnotate);
    btn.setAttribute("aria-pressed", String(chartAnnotate));
    if (chartAnnotate && active.week) await ensureSummaries(active.week);
    rerenderPreservingPanels();
  });

  // Segmented graph-scope control: switch x-axis mode and re-render the
  // current week in place, preserving which detail panels are expanded.
  document.querySelectorAll(".scope-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = btn.dataset.scope;
      if (next === chartScope) return;
      chartScope = next;
      // rerenderPreservingPanels re-renders only #matchups (the charts), not the
      // #toolbar where these buttons live, so move the selected state here.
      document.querySelectorAll(".scope-btn").forEach((b) =>
        b.classList.toggle("active", b.dataset.scope === next));
      rerenderPreservingPanels();
    });
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

load().then((data) => {
  render(data);
  // Charts' history + playoff odds load in the background AFTER the
  // scoreboard is on screen.
  loadAllHistory(data, active.week);
  renderPlayoffs();
}).catch((e) => {
  document.getElementById("matchups").textContent = "Error: " + e.message;
});
