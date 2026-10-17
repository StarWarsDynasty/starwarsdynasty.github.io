// The Dynasty Awakens: league site. Plain JS, no build step.
// All league logic lives in the "Model" section; pages only render it.

const app = document.getElementById("app");
const foot = document.getElementById("foot");

/* ================= Data loading ================= */

function parseCSV(text) {
  const rows = [];
  let row = [], field = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') q = false;
      else field += ch;
    } else if (ch === '"') q = true;
    else if (ch === ",") { row.push(field); field = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field); field = "";
      if (row.some((v) => v !== "")) rows.push(row);
      row = [];
    } else field += ch;
  }
  row.push(field);
  if (row.some((v) => v !== "")) rows.push(row);
  const [head, ...body] = rows;
  return body.map((r) => Object.fromEntries(head.map((h, i) => [h.trim(), (r[i] ?? "").trim()])));
}

async function load(path, kind, optional = false) {
  try {
    const res = await fetch(path, { cache: "no-cache" });
    if (!res.ok) throw new Error(`${path}: ${res.status}`);
    if (kind === "text") return res.text();
    return kind === "json" ? res.json() : parseCSV(await res.text());
  } catch (e) {
    if (optional) return null;
    throw e;
  }
}

const D = {};

async function loadAll() {
  const [cfg, franchises, seasons, contracts, trades, events, rosters, report, crawl] = await Promise.all([
    load("data/config.json", "json"),
    load("data/franchises.csv", "csv"),
    load("data/seasons.csv", "csv"),
    load("data/contracts.csv", "csv"),
    load("data/cap_trades.csv", "csv"),
    load("data/events.csv", "csv", true),
    load("data/espn/rosters.json", "json", true),
    load("data/espn/report.json", "json", true),
    load("data/crawl.txt", "text", true),
  ]);
  Object.assign(D, { cfg, franchises, seasons, trades, events: events || [], rosters, report, crawl: crawl || "" });
  D.contracts = contracts.map((c) => ({
    ...c,
    draft_year: +c.draft_year,
    price: +c.price || 0,
    years: +c.years || 0,
    dead_from: c.dead_from ? +c.dead_from : null,
  }));
  D.seasons.forEach((s) => { for (const k of ["season", "place", "wins", "losses", "ties"]) s[k] = +s[k]; });
  D.trades.forEach((t) => { t.draft_year = +t.draft_year; t.amount = +t.amount; });
  D.byId = Object.fromEntries(D.franchises.map((f) => [f.id, f]));
}

/* ================= Model ================= */

const CUR = () => D.cfg.current_season;
const STEP = () => D.cfg.extension_step;

const lastYear = (c) => c.draft_year + 1 + c.years;
const annualCost = (c) => c.price + (c.years ? STEP() * (c.years - 1) : 0);
const yearsOn = (c, y) => y - c.draft_year + 1;

// Extensions start after Year B, so the escalator applies from year 3 on.
function costIn(c, y) {
  return yearsOn(c, y) <= 2 ? c.price : annualCost(c);
}

function statusIn(c, y) {
  const n = yearsOn(c, y);
  if (n <= 1) return { key: "a", label: "Year A", rank: 1 };
  if (n === 2) return { key: "b", label: "Year B", rank: 2 };
  if (c.years) return { key: "c", label: "Contract", rank: 3 };
  return { key: "d", label: "Extend or release", rank: 2.5 };
}

const deadPerYear = (c) => Math.floor(annualCost(c) / 2);

/* ---------- Planner scenarios ----------
   A plan is { contractKey: "c" | "eN" }: c = cut/release, eN = extend N years.
   It lives only in the URL: #/team/<fid>/plan/<key>.<move>,<key>.<move> */
const planKey = (c) => `${c.draft_year}-${norm(c.player).replace(/ /g, "-")}`;

function parsePlan(str) {
  const plan = {};
  for (const part of (str || "").split(",")) {
    const m = part.match(/^(.+)\.(c|e\d+)$/);
    if (m) plan[decodeURIComponent(m[1])] = m[2];
  }
  return plan;
}
const planString = (plan) => Object.entries(plan).map(([k, v]) => `${encodeURIComponent(k)}.${v}`).join(",");

function applyPlan(fid, plan) {
  const cur = CUR();
  return D.contracts.map((c) => {
    const mv = c.franchise === fid && c.status === "active" ? plan[planKey(c)] : null;
    if (!mv) return c;
    if (mv === "c") return { ...c, status: "released", dead_from: c.years ? cur : null };
    return { ...c, years: Math.max(1, parseInt(mv.slice(1), 10) || 1) };
  });
}

// Everything that determines a franchise's cap for season/draft year y.
function teamYear(fid, y, cs = D.contracts) {
  const mine = cs.filter((c) => c.franchise === fid);
  const keepers = mine
    .filter((c) => c.status === "active" && c.draft_year <= y && y <= lastYear(c))
    .map((c) => ({ c, cost: costIn(c, y), st: statusIn(c, y) }))
    .sort((a, b) => b.st.rank - a.st.rank || b.cost - a.cost);
  const pending = mine.filter((c) => c.status === "active" && !c.years && y === c.draft_year + 2 && y > CUR());
  const dead = mine
    .filter((c) => c.status === "released" && c.years && c.dead_from && c.dead_from <= y && y <= lastYear(c))
    .map((c) => ({ c, amt: deadPerYear(c) }));
  const tin = D.trades.filter((t) => t.to === fid && t.draft_year === y);
  const tout = D.trades.filter((t) => t.from === fid && t.draft_year === y);
  const committed = sum(keepers.map((k) => k.cost));
  const deadSum = sum(dead.map((d) => d.amt));
  const net = sum(tin.map((t) => t.amount)) - sum(tout.map((t) => t.amount));
  const budget = D.cfg.salary_cap - committed - deadSum + net;
  return { keepers, pending, dead, tin, tout, committed, deadSum, net, budget };
}

const norm = (s) => s.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
  .replace("d/st", "").replace(/[^a-z0-9 ]/g, "").replace(/\b(jr|sr|ii|iii|iv|v)\b/g, "")
  .replace(/\s+/g, " ").trim();

// Current roster: ESPN (or workbook bootstrap) joined to contracts.
function currentRoster(fid) {
  const y = CUR();
  const active = D.contracts.filter((c) => c.franchise === fid && c.status === "active" && y <= lastYear(c));
  const used = new Set();
  const players = (D.rosters?.teams?.[fid] || []).map((p) => {
    let c = active.find((k) => !used.has(k) && k.espn_id && p.espn_id && String(k.espn_id) === String(p.espn_id));
    if (!c) c = active.find((k) => !used.has(k) && norm(k.player) === norm(p.name));
    if (c) used.add(c);
    return { p, c, cost: c ? costIn(c, y) : 0, st: c ? statusIn(c, y) : { key: "fa", label: "Free agent", rank: 0 } };
  });
  const missing = active.filter((c) => !used.has(c));
  players.sort((a, b) => b.st.rank - a.st.rank || b.cost - a.cost || a.p.name.localeCompare(b.p.name));
  return { players, missing };
}

function franchiseSummary(fid) {
  const ss = D.seasons.filter((s) => s.franchise === fid);
  const w = sum(ss.map((s) => s.wins)), l = sum(ss.map((s) => s.losses)), t = sum(ss.map((s) => s.ties));
  return {
    seasons: ss.length, w, l, t,
    pct: (w + t / 2) / Math.max(1, w + l + t),
    titles: ss.filter((s) => s.place === 1).length,
    podiums: ss.filter((s) => s.place <= 3).length,
    jarjars: ss.filter((s) => s.place === 12).length,
    best: Math.min(...ss.map((s) => s.place)),
  };
}

/* ================= Helpers ================= */

const sum = (a) => a.reduce((x, y) => x + y, 0);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
const money = (n) => (n < 0 ? `−$${-n}` : `$${n}`);
const signed = (n) => (n > 0 ? `+$${n}` : n < 0 ? `−$${-n}` : "$0");
const ord = (n) => n + (["th", "st", "nd", "rd"][(n % 100 - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");
const teamLink = (fid, label) => `<a href="#/team/${fid}">${esc(label ?? D.byId[fid]?.team_name ?? fid)}</a>`;
const mark = (m) => (m ? `<span class="mark" title="${esc(D.cfg.marks[m] || "")}">${esc(m)}</span>` : "");
const chip = (st) => `<span class="status s-${st.key}">${esc(st.label)}</span>`;
const record = (w, l, t) => `${w}–${l}${t ? `–${t}` : ""}`;

function sortable(table) {
  const ths = table.querySelectorAll("th.sortable");
  ths.forEach((th, i) => th.addEventListener("click", () => {
    const col = [...th.parentNode.children].indexOf(th);
    const dir = th.getAttribute("aria-sort") === "descending" ? "ascending" : "descending";
    ths.forEach((x) => x.removeAttribute("aria-sort"));
    th.setAttribute("aria-sort", dir);
    const body = table.tBodies[0];
    const rows = [...body.rows];
    rows.sort((a, b) => {
      const av = a.cells[col].dataset.v ?? a.cells[col].textContent;
      const bv = b.cells[col].dataset.v ?? b.cells[col].textContent;
      const r = isNaN(av) || isNaN(bv) ? av.localeCompare(bv) : av - bv;
      return dir === "ascending" ? r : -r;
    });
    rows.forEach((r) => body.appendChild(r));
  }));
  ths.forEach((th) => th.setAttribute("tabindex", "0"));
  ths.forEach((th) => th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); th.click(); } }));
}

/* ================= Pages ================= */

// Yearly opening text from data/crawl.txt: first line is the title,
// blank lines separate paragraphs.
function openingCrawl() {
  const blocks = D.crawl.replace(/\r/g, "").trim().split(/\n\s*\n/).map((b) => b.trim()).filter(Boolean);
  if (!blocks.length) return "";
  const [title, ...paras] = blocks;
  return `<div class="opening">
    <h2 class="opening-title">${esc(title)}</h2>
    ${paras.map((p) => `<p>${esc(p)}</p>`).join("")}
  </div>`;
}

function pageHistory() {
  const years = [...new Set(D.seasons.map((s) => s.season))].sort((a, b) => b - a);
  const done = years.length;
  const marksUsed = new Set();

  const who = (s) => {
    if (!s) return "<span class='dim'>none</span>";
    if (s.mark) marksUsed.add(s.mark);
    return `<b>${teamLink(s.franchise, s.team_name)}</b>${mark(s.mark)} <span class="dim">${esc(s.owners)}</span>`;
  };

  let roll = "";
  const cur = CUR();
  if (!years.includes(cur)) {
    const recs = D.rosters?.source === "espn" ? D.rosters.records || {} : {};
    const lead = Object.entries(recs).sort((a, b) => b[1].wins - a[1].wins || b[1].points_for - a[1].points_for)[0];
    roll += `<li class="pending"><div class="yr">${cur}</div><div class="champ">
      <div class="champ-name">Season in progress</div>
      ${lead ? `<div class="owner">Leading the galaxy: ${teamLink(lead[0], lead[1].team_name)} at ${record(lead[1].wins, lead[1].losses, lead[1].ties)}</div>` : ""}
      </div></li>`;
  }
  for (const y of years) {
    const ss = D.seasons.filter((s) => s.season === y);
    const champs = ss.filter((s) => s.place === 1);
    const at = (p) => ss.find((s) => s.place === p);
    const last = ss.reduce((a, b) => (b.place > a.place ? b : a));
    champs.forEach((c) => c.mark && marksUsed.add(c.mark));
    roll += `<li><div class="yr">${y}</div><div class="champ">
      ${champs.map((c) => `<div class="champ-name">${teamLink(c.franchise, c.team_name)}${mark(c.mark)}</div>
        <div class="owner">${esc(c.owners)}</div>`).join("")}
      <div class="places">
        ${champs.length < 2 ? `<span>2nd ${who(at(2))}</span>` : ""}
        <span>3rd ${who(at(3))}</span>
        <span class="jj">Jar Jar Binks ${who(last)}</span>
      </div></div></li>`;
  }

  const rows = D.franchises.map((f) => {
    const r = franchiseSummary(f.id);
    return `<tr>
      <td>${teamLink(f.id)}${f.council ? `<span class="council" title="Jedi High Council">Council</span>` : ""}</td>
      <td>${esc(f.owners)}</td>
      <td class="num" data-v="${r.w}">${r.w}</td><td class="num" data-v="${r.l}">${r.l}</td><td class="num" data-v="${r.t}">${r.t}</td>
      <td class="num" data-v="${r.pct}">${r.pct.toFixed(3).replace(/^0/, "")}</td>
      <td class="num" data-v="${r.titles}">${r.titles || "<span class='dim'>0</span>"}</td>
      <td class="num" data-v="${r.podiums}">${r.podiums}</td>
      <td class="num" data-v="${r.jarjars}">${r.jarjars}</td>
      <td class="num" data-v="${r.best}">${ord(r.best)}</td></tr>`;
  }).join("");

  app.innerHTML = `
    <div class="hero">
      <h1>${esc(D.cfg.league_name)}</h1>
      <p class="crawl">A long time ago in a dynasty league far,<br> far away…</p>
      ${openingCrawl()}
    </div>
    <section aria-labelledby="h-roll">
      <h2 id="h-roll">Supreme Chancellors</h2>
      <ol class="roll" reversed>${roll}</ol>
      <p class="note" style="margin-top:1rem">${[...marksUsed].sort().map((m) => `${esc(m)} ${esc(D.cfg.marks[m] || "")}`).join("<br>")}</p>
    </section>
    <section aria-labelledby="h-rec">
      <h2 id="h-rec">Franchise records</h2>
      <p class="note">Records belong to the franchise, not the owner. Completed seasons only. Select a column to sort.</p>
      <div class="table-wrap"><table id="rec">
        <thead><tr><th class="sortable">Franchise</th><th class="sortable">Owners</th>
        <th class="num sortable">W</th><th class="num sortable">L</th><th class="num sortable">T</th>
        <th class="num sortable" aria-sort="descending">Win %</th><th class="num sortable">Titles</th>
        <th class="num sortable">Top 3</th><th class="num sortable">Jar Jars</th><th class="num sortable">Best</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </section>`;
  const t = document.getElementById("rec");
  sortable(t);
  // initial sort by win %
  const body = t.tBodies[0];
  [...body.rows].sort((a, b) => b.cells[5].dataset.v - a.cells[5].dataset.v).forEach((r) => body.appendChild(r));
}

function pageTeams() {
  const next = CUR() + 1;
  const recs = D.rosters?.source === "espn" ? D.rosters.records || {} : {};
  const tiles = D.franchises.map((f) => {
    const now = teamYear(f.id, CUR());
    const nx = teamYear(f.id, next);
    const r = recs[f.id];
    return `<a class="team-tile" href="#/team/${f.id}">
      <div class="tn">${esc(f.team_name)}</div>
      <div class="ow">${esc(f.owners)}</div>
      <div class="kv">
        ${r ? `<div><b>${record(r.wins, r.losses, r.ties)}</b>${CUR()} record</div>` : ""}
        <div><b>${money(now.committed)}</b>${CUR()} salary</div>
        <div><b class="${nx.budget < 0 ? "neg" : ""}">${money(nx.budget)}</b>${next} budget</div>
      </div></a>`;
  }).join("");
  app.innerHTML = `<h1>Teams</h1>
    <p class="lede">The ${next} budget counts players already locked in for next season and any traded cap or dead money. Year B players who still need an extension decision are not counted yet.</p>
    <div class="teams">${tiles}</div>`;
}

function pageTeam(fid, mode, planStr) {
  const f = D.byId[fid];
  if (!f) { app.innerHTML = `<h1>Team not found</h1><p><a href="#/teams">See all teams</a></p>`; return; }
  const planning = mode === "plan";
  const plan = planning ? parsePlan(planStr) : {};
  const cs = planning ? applyPlan(fid, plan) : D.contracts;
  const cur = CUR(), next = cur + 1;
  const sum0 = franchiseSummary(fid);
  const now = teamYear(fid, cur, cs), base = teamYear(fid, cur);
  const nx = teamYear(fid, next, cs), nxBase = teamYear(fid, next);
  const { players, missing } = currentRoster(fid);
  const espn = D.rosters?.source === "espn";
  const rec = espn ? D.rosters.records?.[fid] : null;
  const nMoves = Object.keys(plan).length;
  const was = (a, b) => (planning && a !== b ? `<small class="was">was ${money(b)}</small>` : "");

  // One planner control per roster row, depending on the player's status now.
  function control(c, st) {
    if (!c) return `<span class="dim">Leaves after ${cur}</span>`;
    const k = planKey(c), mv = plan[k] || "";
    const id = `mv-${k}`;
    if (st.key === "c") {
      return `<select id="${id}" data-k="${esc(k)}" aria-label="Move for ${esc(c.player)}">
        <option value="">Keep</option><option value="c" ${mv === "c" ? "selected" : ""}>Cut</option></select>`;
    }
    if (st.key === "b") {
      const yrs = mv.startsWith("e") ? mv.slice(1) : "";
      return `<select id="${id}" data-k="${esc(k)}" aria-label="Move for ${esc(c.player)}">
          <option value="">Decide later</option>
          <option value="e" ${yrs ? "selected" : ""}>Extend</option>
          <option value="c" ${mv === "c" ? "selected" : ""}>Release</option></select>
        ${yrs ? `<input type="number" id="yr-${k}" data-k="${esc(k)}" min="1" step="1" value="${yrs}" aria-label="Contract years for ${esc(c.player)}"> yr` : ""}`;
    }
    return `<select id="${id}" data-k="${esc(k)}" aria-label="Move for ${esc(c.player)}">
      <option value="">Keep</option><option value="c" ${mv === "c" ? "selected" : ""}>Release</option></select>`;
  }

  function effect(c) {
    const mv = c && plan[planKey(c)];
    if (!mv) return "";
    if (mv === "c") {
      if (!c.years) return `<span class="dim">Free</span>`;
      return `<span class="neg">Dead ${money(deadPerYear(c))}/yr, ${cur}–${lastYear(c)}</span>`;
    }
    const x = { ...c, years: parseInt(mv.slice(1), 10) || 1 };
    return `${money(annualCost(x))}/yr, ${next}–${lastYear(x)}`;
  }

  const planned = new Map(cs.filter((c) => c.franchise === fid).map((c) => [planKey(c), c]));
  const rosterRows = players.map(({ p, c, cost, st }) => {
    const mv = c && plan[planKey(c)];
    const pc = c ? planned.get(planKey(c)) || c : null;
    return `<tr class="${mv === "c" ? "cut" : ""}">
    ${espn && !planning ? `<td class="dim">${esc(p.slot || "")}</td>` : ""}
    <td class="pl">${esc(c?.player || p.name)}${p.injury && p.injury !== "ACTIVE" ? ` <span class="neg" title="${esc(p.injury)}">${esc(p.injury[0])}</span>` : ""}</td>
    <td>${esc(p.pos || c?.pos || "")}</td>
    ${espn ? `<td class="dim">${esc(p.nfl || "")}</td>` : ""}
    <td>${chip(st)}</td>
    ${planning ? `<td class="move">${control(c, st)}</td><td>${effect(c)}</td>` : `
    <td class="num">${c ? c.draft_year : ""}</td>
    <td class="num">${c ? money(c.price) : ""}</td>
    <td class="num">${c?.years ? `${c.years} yr` : ""}</td>`}
    <td class="num">${money(mv === "c" ? 0 : cost)}</td>
    <td class="num">${pc ? lastYear(pc) : ""}</td></tr>`;
  }).join("");
  const preCols = (espn && !planning ? 1 : 0) + 3 + (espn ? 1 : 0) + (planning ? 2 : 3);

  const futureYears = [cur, next, next + 1, next + 2, next + 3];
  const deadRows = cs.filter((c) => c.franchise === fid && c.status === "released" && c.years && c.dead_from && lastYear(c) >= cur)
    .map((c) => `<tr><td>${esc(c.player)}${planning && plan[planKey(c)] ? ` <span class="council">planned</span>` : ""}</td><td class="num">${money(annualCost(c))}</td><td class="num neg">${money(deadPerYear(c))}</td><td class="num">${c.dead_from}–${lastYear(c)}</td></tr>`).join("");
  const tradeRows = D.trades.filter((t) => (t.from === fid || t.to === fid) && t.draft_year >= next)
    .sort((a, b) => a.draft_year - b.draft_year)
    .map((t) => {
      const amt = t.to === fid ? t.amount : -t.amount;
      const other = t.to === fid ? t.from : t.to;
      return `<tr><td class="num">${t.draft_year}</td><td>${t.to === fid ? "From" : "To"} ${teamLink(other)}</td><td class="num ${amt < 0 ? "neg" : "pos"}">${signed(amt)}</td></tr>`;
    }).join("");

  const pendingRows = nx.pending.map((c) => `<tr><td>${esc(c.player)}</td><td>${esc(c.pos)}</td><td class="num">${money(c.price)}</td>
    ${[1, 2, 3, 4].map((j) => `<td class="num">${money(c.price + STEP() * (j - 1))}</td>`).join("")}</tr>`).join("");

  const hist = D.seasons.filter((s) => s.franchise === fid).sort((a, b) => b.season - a.season).map((s) =>
    `<tr><td class="num">${s.season}</td><td>${esc(s.team_name)}</td><td>${esc(s.owners)}</td>
     <td class="num">${ord(s.place)}${mark(s.mark)}</td><td class="num">${record(s.wins, s.losses, s.ties)}</td></tr>`).join("");

  const ev = D.events.filter((e) => e.franchise === fid || e.to_franchise === fid).slice(-12).reverse()
    .map((e) => `<tr><td class="num">${esc(e.date)}</td><td>${esc(e.type)}</td><td>${esc(e.player)}</td><td class="dim">${esc(e.detail)}</td></tr>`).join("");

  const projRows = futureYears.map((y) => {
    const t = teamYear(fid, y, cs), b = teamYear(fid, y);
    const d = t.budget - b.budget;
    return `<tr><td class="num">${y === cur ? `${y} season` : y}</td><td class="num">${t.keepers.length}</td><td class="num">${money(t.committed)}</td>
      <td class="num ${t.deadSum ? "neg" : ""}">${money(t.deadSum)}</td><td class="num">${signed(t.net)}</td>
      <td class="num ${t.budget < 0 ? "neg" : ""}"><b>${money(t.budget)}</b></td>
      ${planning ? `<td class="num ${d < 0 ? "neg" : d > 0 ? "pos" : "dim"}">${d ? signed(d) : "none"}</td>` : ""}</tr>`;
  }).join("");

  const scrollY = window.scrollY;
  app.innerHTML = `
    ${planning ? `<div class="plan-bar" role="region" aria-label="Planning mode">
      <div><b>Planning mode.</b> A scenario only. Nothing here changes the league. ${nMoves ? `${nMoves} planned ${nMoves === 1 ? "move" : "moves"}.` : "Pick moves in the roster below."}</div>
      <div class="plan-actions">
        <button type="button" id="copy-link" ${nMoves ? "" : "disabled"}>Copy link to this plan</button>
        <button type="button" id="reset-plan" ${nMoves ? "" : "disabled"}>Reset</button>
        <a href="#/team/${fid}">Exit planning</a>
      </div></div>` : ""}
    <div class="team-head"><div>
      <h1>${esc(f.team_name)}</h1>
      <div class="ow">${esc(f.owners)}${f.council ? `<span class="council">Jedi High Council</span>` : ""}</div>
    </div>
    ${planning ? "" : `<a class="plan-btn" href="#/team/${fid}/plan">Plan moves</a>`}</div>
    <div class="stats">
      ${rec ? `<div class="stat"><b>${record(rec.wins, rec.losses, rec.ties)}</b><span>${cur} record</span></div>` : ""}
      ${planning ? "" : `<div class="stat"><b>${record(sum0.w, sum0.l, sum0.t)}</b><span>All-time record</span></div>
      <div class="stat"><b>${sum0.titles}</b><span>Titles</span></div>`}
      <div class="stat"><b>${money(now.committed + now.deadSum)}</b><span>${cur} salary${now.deadSum ? " + dead money" : ""}</span>${was(now.committed + now.deadSum, base.committed + base.deadSum)}</div>
      <div class="stat"><b class="${nx.budget < 0 ? "neg" : ""}">${money(nx.budget)}</b><span>${next} draft budget</span>${was(nx.budget, nxBase.budget)}</div>
    </div>

    <section aria-labelledby="h-roster">
      <h2 id="h-roster">${cur} roster</h2>
      ${planning ? `<p class="note">Cut a contract and his dead money counts from the ${cur} season through his last contract year. Releasing a rookie is free. Extensions start at the ${next} draft and can be any length.</p>` : ""}
      <div class="legend">${chip({ key: "c", label: "Contract" })}${chip({ key: "b", label: "Year B" })}${chip({ key: "a", label: "Year A" })}${chip({ key: "fa", label: "Free agent ($0)" })}</div>
      <div class="table-wrap"><table class="${planning ? "planning" : ""}">
        <thead><tr>${espn && !planning ? "<th>Slot</th>" : ""}<th>Player</th><th>Pos</th>${espn ? "<th>NFL</th>" : ""}<th>Status</th>
        ${planning ? "<th>Move</th><th>Effect</th>" : `<th class="num">Drafted</th><th class="num">Price</th><th class="num">Contract</th>`}
        <th class="num">${cur} cost</th><th class="num">Expires</th></tr></thead>
        <tbody>${rosterRows || `<tr><td colspan="11" class="dim">No roster yet.</td></tr>`}
        <tr class="total"><td colspan="${preCols}">${players.length} of ${D.cfg.roster_limit} roster spots</td><td class="num">${money(now.committed)}</td><td></td></tr></tbody>
      </table></div>
      ${missing.length ? `<div class="warn">Under contract here but not on the roster: ${missing.map((c) => esc(c.player)).join(", ")}. Check for a trade or drop.</div>` : ""}
      ${planning ? "" : `<p class="note">Rosters ${espn ? `synced from ESPN ${esc(D.rosters.updated?.slice(0, 10))}` : `from the spreadsheet of ${esc(D.rosters?.updated || "")}`}. Cost is the draft price plus $${STEP()} for each contract year after the first.</p>`}
    </section>

    <section aria-labelledby="h-next">
      <h2 id="h-next">${next} draft</h2>
      <div class="table-wrap"><table>
        <tbody>
          <tr><td>Salary cap</td><td class="num">${money(D.cfg.salary_cap)}</td></tr>
          <tr><td>Players kept (${nx.keepers.length})</td><td class="num">${money(-nx.committed)}</td></tr>
          <tr><td>Dead money</td><td class="num ${nx.deadSum ? "neg" : ""}">${money(-nx.deadSum)}</td></tr>
          <tr><td>Traded cap</td><td class="num ${nx.net < 0 ? "neg" : nx.net > 0 ? "pos" : ""}">${signed(nx.net)}</td></tr>
          <tr class="total"><td>Draft budget${nx.pending.length ? ` (${nx.pending.length} Year B ${nx.pending.length === 1 ? "player" : "players"} undecided)` : ""}</td><td class="num ${nx.budget < 0 ? "neg" : ""}">${money(nx.budget)}${was(nx.budget, nxBase.budget)}</td></tr>
        </tbody></table></div>
      ${nx.pending.length && !planning ? `
        <h3 style="margin-top:1.5rem">Extend or release before the ${next} draft</h3>
        <p class="note">These players finish Year B in ${cur}. Annual cost by contract length. Use <a href="#/team/${fid}/plan">Plan moves</a> to try any length.</p>
        <div class="table-wrap"><table>
          <thead><tr><th>Player</th><th>Pos</th><th class="num">Price</th><th class="num">1 yr</th><th class="num">2 yr</th><th class="num">3 yr</th><th class="num">4 yr</th></tr></thead>
          <tbody>${pendingRows}</tbody></table></div>` : ""}
    </section>

    <section aria-labelledby="h-proj"><h2 id="h-proj">Future drafts</h2>
      <p class="note">${planning ? "Budgets with your planned moves. Change shows the difference from the real numbers." : "Budgets from contracts already signed. Extensions and new rookies will lower them."}</p>
      <div class="table-wrap"><table><thead><tr><th class="num">Draft</th><th class="num">Players kept</th><th class="num">Salary</th><th class="num">Dead money</th><th class="num">Traded cap</th><th class="num">${"Budget"}</th>${planning ? `<th class="num">Change</th>` : ""}</tr></thead>
      <tbody>${projRows}</tbody></table></div>
      <p class="note">The ${cur} season row is cap space for this season. The cap is only enforced at drafts.</p>
    </section>

    ${deadRows ? `<section aria-labelledby="h-dead"><h2 id="h-dead">Dead money</h2>
      <div class="table-wrap"><table><thead><tr><th>Player</th><th class="num">Contract cost</th><th class="num">Owed per year</th><th class="num">Seasons</th></tr></thead>
      <tbody>${deadRows}</tbody></table></div></section>` : ""}

    ${tradeRows ? `<section aria-labelledby="h-trade"><h2 id="h-trade">Traded cap</h2>
      <div class="table-wrap"><table><thead><tr><th class="num">Draft</th><th>With</th><th class="num">Amount</th></tr></thead>
      <tbody>${tradeRows}</tbody></table></div></section>` : ""}

    ${ev && !planning ? `<section aria-labelledby="h-ev"><h2 id="h-ev">Recent changes</h2>
      <div class="table-wrap"><table><thead><tr><th class="num">Date</th><th>Change</th><th>Player</th><th>Detail</th></tr></thead><tbody>${ev}</tbody></table></div></section>` : ""}

    ${planning ? "" : `<section aria-labelledby="h-hist"><h2 id="h-hist">Franchise history</h2>
      <div class="table-wrap"><table><thead><tr><th class="num">Season</th><th>Team name</th><th>Owners</th><th class="num">Finish</th><th class="num">Record</th></tr></thead>
      <tbody>${hist}</tbody></table></div></section>`}`;

  if (!planning) return;
  window.scrollTo(0, scrollY);

  const update = (next, focusId) => {
    const str = planString(next);
    history.replaceState(null, "", `#/team/${fid}/plan${str ? `/${str}` : ""}`);
    pageTeam(fid, "plan", str);
    if (focusId) document.getElementById(focusId)?.focus();
  };
  app.querySelectorAll("select[data-k]").forEach((el) => el.addEventListener("change", () => {
    const p = { ...plan }, k = el.dataset.k;
    if (el.value === "") delete p[k];
    else if (el.value === "e") p[k] = "e1";
    else p[k] = el.value;
    update(p, el.value === "e" ? `yr-${k}` : el.id);
  }));
  app.querySelectorAll("input[data-k]").forEach((el) => el.addEventListener("change", () => {
    const n = Math.max(1, parseInt(el.value, 10) || 1);
    update({ ...plan, [el.dataset.k]: `e${n}` }, el.id);
  }));
  document.getElementById("reset-plan")?.addEventListener("click", () => update({}));
  document.getElementById("copy-link")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    try { await navigator.clipboard.writeText(location.href); btn.textContent = "Link copied"; }
    catch { prompt("Copy this link:", location.href); }
  });
}

function pageCap(yParam) {
  const cur = CUR();
  const years = [cur, cur + 1, cur + 2, cur + 3];
  const y = years.includes(+yParam) ? +yParam : cur + 1;
  const isDraft = y > cur;
  const data = D.franchises.map((f) => ({ f, t: teamYear(f.id, y) }));

  const rows = data.map(({ f, t }) => `<tr>
    <td>${teamLink(f.id)}</td>
    <td class="num" data-v="${t.keepers.length}">${t.keepers.length}</td>
    <td class="num" data-v="${t.committed}">${money(t.committed)}</td>
    <td class="num ${t.deadSum ? "neg" : ""}" data-v="${t.deadSum}">${money(t.deadSum)}</td>
    <td class="num ${t.net < 0 ? "neg" : t.net > 0 ? "pos" : ""}" data-v="${t.net}">${signed(t.net)}</td>
    <td class="num ${t.budget < 0 ? "neg" : ""}" data-v="${t.budget}"><b>${money(t.budget)}</b></td>
    ${isDraft ? `<td class="num" data-v="${t.pending.length}">${t.pending.length || ""}</td>` : ""}</tr>`).join("");

  const keeperBlocks = isDraft ? data.map(({ f, t }) => {
    const adj = t.deadSum - t.net;
    const advice = adj === 0 ? "No adjustment needed."
      : adj > 0 ? `Add <b>$${adj}</b> to one keeper's value.`
      : `Subtract <b>$${-adj}</b> from one keeper's value.`;
    return `<details class="keepers"><summary>${esc(f.team_name)}: ${t.keepers.length} keepers, ESPN budget ${money(t.budget)}</summary>
      <div class="table-wrap" style="margin-top:.5rem"><table><thead><tr><th>Keeper</th><th>Status in ${y}</th><th class="num">ESPN keeper value</th></tr></thead>
      <tbody>${t.keepers.map((k) => `<tr><td>${esc(k.c.player)}</td><td>${chip(k.st)}</td><td class="num">${money(k.cost)}</td></tr>`).join("") || `<tr><td colspan="3" class="dim">No keepers yet.</td></tr>`}</tbody></table></div>
      <p class="adjust">${advice} ${adj !== 0 ? `That folds in ${t.deadSum ? `${money(t.deadSum)} dead money` : ""}${t.deadSum && t.net ? " and " : ""}${t.net ? `${signed(t.net)} traded cap` : ""}.` : ""}</p>
      </details>`;
  }).join("") : "";

  const ledger = [...D.trades].sort((a, b) => a.draft_year - b.draft_year).map((t) =>
    `<tr><td class="num">${t.draft_year}</td><td>${teamLink(t.from)}</td><td>${teamLink(t.to)}</td><td class="num">${money(t.amount)}</td></tr>`).join("");

  app.innerHTML = `
    <h1>Salary cap</h1>
    <p class="lede">The $${D.cfg.salary_cap} cap is enforced only at the auction draft. Traded cap and dead money count toward the draft year they belong to.</p>
    <div class="controls"><span class="dim">Show</span><div class="seg" role="group" aria-label="Year">
      ${years.map((v) => `<button type="button" data-y="${v}" aria-pressed="${v === y}">${v === cur ? `${v} season` : `${v} draft`}</button>`).join("")}
    </div></div>
    <div class="table-wrap"><table id="cap">
      <thead><tr><th class="sortable">Team</th><th class="num sortable">Players</th><th class="num sortable">Salary</th>
      <th class="num sortable">Dead money</th><th class="num sortable">Traded cap</th>
      <th class="num sortable">${isDraft ? "Draft budget" : "Cap space"}</th>${isDraft ? `<th class="num sortable" title="Year B players who need an extension decision">To decide</th>` : ""}</tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${isDraft ? `<p class="note">"To decide" counts players finishing Year B who must be extended or released. Their cost is not in the budget until an extension is entered.</p>` : `<p class="note">Teams can go over the cap during the season.</p>`}

    ${isDraft ? `<section aria-labelledby="h-keep"><h2 id="h-keep">Keeper values for ESPN</h2>
      <p class="lede">Enter each keeper at their contract cost. Then fold dead money and traded cap into one keeper so the ESPN auction budget matches the team's real budget.</p>
      ${keeperBlocks}</section>` : ""}

    <section aria-labelledby="h-led"><h2 id="h-led">Traded cap ledger</h2>
      <div class="table-wrap"><table><thead><tr><th class="num">Draft</th><th>From</th><th>To</th><th class="num">Amount</th></tr></thead>
      <tbody>${ledger || `<tr><td colspan="4" class="dim">No cap trades.</td></tr>`}</tbody></table></div></section>`;

  app.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => { location.hash = `#/cap/${b.dataset.y}`; }));
  sortable(document.getElementById("cap"));
}

function pageRules() {
  const s = D.cfg.extension_step;
  app.innerHTML = `<div class="rules">
    <h1>League rules</h1>
    <p class="lede">How contracts and the salary cap work. The site calculates everything on this page automatically.</p>

    <h2>Format</h2>
    <ul><li>12 teams, superflex, half-point PPR.</li>
      <li>$${D.cfg.salary_cap} salary cap, enforced only at the auction draft. Teams can go over it during the season through trades.</li>
      <li>${D.cfg.roster_limit} roster spots, never exceeded. No keeper limit.</li></ul>

    <h2>Rookie contracts</h2>
    <p>Your winning auction bid is the player's price. He plays Year A (the draft season) and Year B at that price.</p>

    <h2>Extensions</h2>
    <p>After Year B, extend him or release him. An extension can be any length. Each year past the first adds $${s} to the annual cost, and the cost stays flat for the whole contract.</p>
    <div class="formula">cost = price + ${s} × (years − 1)</div>
    <p>The contract's last season is the draft year + 1 + contract years. Then the player goes back into the draft pool.</p>

    <h2>Free agents</h2>
    <p>Pickups cost $0 and go back into the draft pool at season's end.</p>

    <h2>Trades</h2>
    <p>Contracts move with the player unchanged. Cap money can be traded for a specific future draft. It only counts at that draft.</p>

    <h2>Releases and dead money</h2>
    <p>Releasing a player on his rookie deal is free. Cutting a player under an extension costs half his annual cost, rounded down, every season from the season he is cut through the end of the original contract. It matters at each of those drafts.</p>
    <div class="formula">dead money = ⌊cost ÷ 2⌋ per season</div>
    <p>Every team page has a <b>Plan moves</b> button for trying extensions and cuts before you make them.</p>
  </div>`;
}

/* ================= Router ================= */

function route() {
  const [, page = "", arg, mode, planStr] = location.hash.replace(/^#/, "").split("/");
  const nav = page === "team" ? "teams" : page || "history";
  document.querySelectorAll("[data-nav]").forEach((a) => (a.dataset.nav === nav ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  if (page === "teams") pageTeams();
  else if (page === "team") pageTeam(arg, mode, planStr);
  else if (page === "cap") pageCap(arg);
  else if (page === "rules") pageRules();
  else pageHistory();
  const title = { teams: "Teams", team: D.byId[arg] && `${D.byId[arg].team_name}${mode === "plan" ? " plan" : ""}`, cap: "Salary cap", rules: "Rules" }[page];
  document.title = title ? `${title} | The Dynasty Awakens` : "The Dynasty Awakens";
  window.scrollTo(0, 0);
}

loadAll().then(() => {
  foot.innerHTML = `Records kept by the Jedi Chronicler, ${esc(D.cfg.chronicler)}.`;
  window.addEventListener("hashchange", route);
  route();
}).catch((e) => {
  app.innerHTML = `<h1>The archives could not be loaded</h1><p class="lede">${esc(e.message)}. Check that the data files exist and are valid CSV/JSON.</p>`;
});
