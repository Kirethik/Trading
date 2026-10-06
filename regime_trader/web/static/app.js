/* Regime-Trader dashboard SPA. Vanilla JS + Plotly (CDN). Same-origin /api/*. */

const PLOTLY_BASE = {
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: "#e7e9f0", size: 11 },
  margin: { l: 50, r: 20, t: 36, b: 50 },
  legend: { orientation: "h", y: -0.25 },
};

const AXIS_BASE = { gridcolor: "#262b3a", zerolinecolor: "#2a2f42", color: "#8b93a8" };

const COLORS = {
  green: "#3ecf8e", red: "#ff5c6c", yellow: "#f5c451", blue: "#4f8cff", muted: "#8b93a8",
};

const DECISION_COLORS = {
  DEPLOY: COLORS.green, APPROVE: COLORS.blue, INCREASE_ALLOCATION: COLORS.green,
  REDUCE_RISK: COLORS.yellow, HOLD: COLORS.muted, REJECT: COLORS.red,
  RETIRE: COLORS.red, ROLLBACK: COLORS.red, RESEARCH: COLORS.blue, REOPTIMIZE: COLORS.yellow,
};

const PLOT_CONFIG = { displayModeBar: false, responsive: true };

const PORTFOLIO_REFRESH_MS = 5000; // matches config monitoring.dashboard_refresh_seconds

const STATE = { date: null, dates: [], strategies: [], registry: {}, jobPoll: null, currentJobLog: null, portfolioPoll: null };

// ---------------------------------------------------------------- fetch helpers
async function api(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (res.status === 401) { onUnauthorized(); throw new Error("unauthorized"); }
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401) { onUnauthorized(); throw new Error("unauthorized"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path} -> ${res.status}`);
  return data;
}

function onUnauthorized() {
  if (STATE.jobPoll) { clearInterval(STATE.jobPoll); STATE.jobPoll = null; }
  if (STATE.portfolioPoll) { clearInterval(STATE.portfolioPoll); STATE.portfolioPoll = null; }
  document.getElementById("app").classList.remove("visible");
  document.getElementById("login-screen").style.display = "flex";
}

// ---------------------------------------------------------------- plotly helper
function plot(id, traces, layout) {
  const el = document.getElementById(id);
  if (!el) return;
  const merged = {
    ...PLOTLY_BASE,
    ...layout,
    xaxis: { ...AXIS_BASE, ...(layout && layout.xaxis) },
    yaxis: { ...AXIS_BASE, ...(layout && layout.yaxis) },
  };
  Plotly.react(el, traces, merged, PLOT_CONFIG);
}

function emptyChart(id, message) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = `<p class="muted" style="padding: 12px;">${message}</p>`;
}

// ---------------------------------------------------------------- init / auth
document.addEventListener("DOMContentLoaded", init);

async function init() {
  document.getElementById("login-form").addEventListener("submit", onLogin);
  document.getElementById("logout-btn").addEventListener("click", onLogout);
  document.querySelectorAll("nav.tabs button").forEach((btn) =>
    btn.addEventListener("click", () => showTab(btn.dataset.tab)));
  document.getElementById("date-select").addEventListener("change", onDateChange);
  document.getElementById("strategy-select").addEventListener("change", onStrategyChange);
  document.getElementById("log-refresh").addEventListener("click", loadLogTail);
  document.getElementById("log-channel").addEventListener("change", loadLogTail);

  const session = await fetch("/api/session", { credentials: "same-origin" }).then((r) => r.json());
  document.getElementById("login-title").textContent = session.title || "Regime-Trader";
  document.getElementById("app-title").textContent = session.title || "Regime-Trader";
  document.title = session.title || "Regime-Trader";

  if (!session.password_set) {
    const err = document.getElementById("login-error");
    err.textContent = "DASHBOARD_PASSWORD is not configured on the server (.env).";
  }

  if (session.authed) {
    await showApp();
  } else {
    document.getElementById("login-screen").style.display = "flex";
  }
}

async function onLogin(ev) {
  ev.preventDefault();
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  try {
    await apiPost("/login", { password });
    document.getElementById("login-password").value = "";
    await showApp();
  } catch (e) {
    errEl.textContent = "Incorrect password";
  }
}

async function onLogout() {
  if (STATE.jobPoll) { clearInterval(STATE.jobPoll); STATE.jobPoll = null; }
  if (STATE.portfolioPoll) { clearInterval(STATE.portfolioPoll); STATE.portfolioPoll = null; }
  try { await apiPost("/logout", {}); } catch (e) { /* ignore */ }
  document.getElementById("app").classList.remove("visible");
  document.getElementById("login-screen").style.display = "flex";
  document.getElementById("login-password").value = "";
}

async function showApp() {
  document.getElementById("login-screen").style.display = "none";
  document.getElementById("app").classList.add("visible");
  await loadRuns();
  await Promise.all([loadOverview(), loadRegistry(), loadPortfolio()]);
  await loadCommands();
  await loadLeaderboard();
  await loadStrategiesList();
  await loadDecisions();
  await loadOmnivore();
  await loadJobs();
  await loadLogTail();

  if (!STATE.portfolioPoll) {
    STATE.portfolioPoll = setInterval(() => { loadPortfolio().catch(() => {}); }, PORTFOLIO_REFRESH_MS);
  }
}

// ---------------------------------------------------------------- tabs
function showTab(name) {
  document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "controls") loadJobs();
  if (name === "portfolio") loadPortfolioPage().catch(() => {});
  if (name === "watchlist") loadWatchlistPage().catch(() => {});
}

// ---------------------------------------------------------------- runs / date
async function loadRuns() {
  const r = await api("/api/runs");
  STATE.dates = r.dates || [];
  STATE.date = r.latest;
  const sel = document.getElementById("date-select");
  sel.innerHTML = "";
  STATE.dates.forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    if (d === STATE.date) opt.selected = true;
    sel.appendChild(opt);
  });
}

async function onDateChange() {
  STATE.date = document.getElementById("date-select").value;
  await loadLeaderboard();
  await loadStrategiesList();
}

// ---------------------------------------------------------------- registry
async function loadRegistry() {
  STATE.registry = await api("/api/registry");
}

// ---------------------------------------------------------------- overview
async function loadOverview() {
  const o = await api("/api/overview");

  const breaker = o.breaker || {};
  const badge = document.getElementById("breaker-badge");
  badge.textContent = breaker.locked ? "breaker: LOCKED" : "breaker: clear";
  badge.className = "badge " + (breaker.locked ? "bad" : "ok");

  const summary = o.summary || {};
  const verdicts = o.verdict_counts || {};
  const decisions = o.decision_counts || {};

  const cards = document.getElementById("overview-cards");
  cards.innerHTML = "";
  addCard(cards, "Latest run", o.latest_date || "—");
  addCard(cards, "Tier", summary.tier || "—");
  addCard(cards, "Strategies", o.strategies ?? 0);
  addCard(cards, "Capital allocated", `$${Math.round(o.capital_allocated || 0).toLocaleString()}`);
  addCard(cards, "PASS / REJECT", `${verdicts.PASS || 0} / ${verdicts.REJECTED || 0}`);
  addCard(cards, "DEPLOY", decisions.DEPLOY || 0);

  const statusEntries = Object.entries(o.status_counts || {});
  plot("chart-status", [{
    type: "pie",
    labels: statusEntries.map(([k]) => k),
    values: statusEntries.map(([, v]) => v),
    hole: 0.5,
    textinfo: "label+value",
    marker: { line: { color: "#0b0d13", width: 2 } },
  }], { title: "Registry status" });

  plot("chart-decisions", [{
    type: "bar",
    x: Object.keys(decisions),
    y: Object.values(decisions),
    marker: { color: Object.keys(decisions).map((k) => DECISION_COLORS[k] || COLORS.blue) },
  }], { title: "Decision mix (latest run)", xaxis: { tickangle: -30 } });

  plot("chart-verdicts", [{
    type: "bar",
    x: ["PASS", "REJECTED"],
    y: [verdicts.PASS || 0, verdicts.REJECTED || 0],
    marker: { color: [COLORS.green, COLORS.red] },
  }], { title: "Validation verdicts (latest run)" });
}

function addCard(container, label, value) {
  const div = document.createElement("div");
  div.className = "card";
  div.innerHTML = `<div class="label">${escapeHtml(String(label))}</div><div class="value">${escapeHtml(String(value))}</div>`;
  container.appendChild(div);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function money(v) {
  v = v || 0;
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function pct(v) {
  return `${((v || 0) * 100).toFixed(2)}%`;
}

function pnlClass(v) {
  return (v || 0) >= 0 ? "pnl-pos" : "pnl-neg";
}

function addPnlCard(container, label, value, pctValue) {
  const div = document.createElement("div");
  div.className = "card";
  const suffix = pctValue !== undefined ? ` (${pct(pctValue)})` : "";
  div.innerHTML = `<div class="label">${escapeHtml(label)}</div><div class="value ${pnlClass(value)}">${money(value)}${suffix}</div>`;
  container.appendChild(div);
}

// ---------------------------------------------------------------- live portfolio
async function loadPortfolio() {
  const p = await api("/api/portfolio");
  const t = p.totals || {};

  const cards = document.getElementById("portfolio-cards");
  cards.innerHTML = "";
  addCard(cards, "Total invested", money(t.total_invested));
  addCard(cards, "Current value", money(t.total_equity));
  addCard(cards, "Cash", money(t.total_cash));
  addCard(cards, "In positions", money(t.total_market_value));
  addPnlCard(cards, "Total P&L", t.total_pnl, t.total_pnl_pct);
  addPnlCard(cards, "Unrealized P&L", t.total_unrealized_pnl);
  addPnlCard(cards, "Realized P&L", t.total_realized_pnl);

  const updated = document.getElementById("portfolio-updated");
  if (updated) {
    const ts = p.as_of ? new Date(p.as_of) : null;
    updated.textContent = ts ? `updated ${ts.toLocaleTimeString()}` : "";
  }
  const liveBadge = document.getElementById("live-badge");
  if (liveBadge) {
    const priced = t.symbols_priced ?? 0;
    const total = t.symbols ?? 0;
    liveBadge.textContent = total === 0 ? "live: no positions"
      : p.live ? `live: ${priced}/${total} priced` : "live: prices unavailable";
    liveBadge.className = "badge " + (total === 0 || p.live ? "ok" : "warn");
  }

  renderSortableTable(
    "positions-table",
    ["strategy", "symbol", "qty", "entry", "price", "value", "unrealized_pnl", "unrealized_pnl_pct"],
    p.positions || [],
    (row, col) => {
      if (col === "entry" || col === "price") return `$${(row[col] ?? 0).toFixed(2)}`;
      if (col === "value") return money(row[col]);
      if (col === "unrealized_pnl") return money(row[col]);
      if (col === "unrealized_pnl_pct") return pct(row[col]);
      if (col === "qty") return (row[col] ?? 0).toLocaleString();
      return row[col] ?? "";
    },
    (row) => selectStrategy(row.strategy),
    (row, col) => (col === "unrealized_pnl" || col === "unrealized_pnl_pct") ? pnlClass(row[col]) : null
  );
}

// ---------------------------------------------------------- portfolio (page)
function tsShort(v) {
  return (v || "").replace("T", " ").slice(0, 19);
}

async function loadPortfolioPage() {
  const [p, t] = await Promise.all([api("/api/portfolio"), api("/api/trades?limit=300")]);
  const tot = p.totals || {};

  const cards = document.getElementById("pf-cards");
  cards.innerHTML = "";
  addCard(cards, "Currently invested", money(tot.total_invested));
  addCard(cards, "Available capital (cash)", money(tot.total_cash));
  addCard(cards, "Value of stocks held", money(tot.total_market_value));
  addCard(cards, "Total portfolio value", money(tot.total_equity));
  addPnlCard(cards, "Total profit / loss", tot.total_pnl, tot.total_pnl_pct);
  addCard(cards, "Open positions", tot.open_positions ?? 0);

  const strat = (p.strategies || []).filter((s) => s.equity);
  if (strat.length) {
    plot("chart-pf-alloc", [{
      type: "pie", hole: 0.5,
      labels: strat.map((s) => s.strategy),
      values: strat.map((s) => s.equity),
      textinfo: "label+percent",
      marker: { line: { color: "#0b0d13", width: 2 } },
    }], { title: "Capital by strategy" });
    plot("chart-pf-pnl", [{
      type: "bar",
      x: strat.map((s) => s.strategy),
      y: strat.map((s) => s.total_pnl),
      marker: { color: strat.map((s) => (s.total_pnl >= 0 ? COLORS.green : COLORS.red)) },
    }], { title: "P&L by strategy ($)", xaxis: { tickangle: -30 } });
  } else {
    emptyChart("chart-pf-alloc", "no capital deployed yet");
    emptyChart("chart-pf-pnl", "no capital deployed yet");
  }

  const sm = t.summary || {};
  const note = document.getElementById("pf-trades-note");
  if (note) note.textContent = `— ${sm.fills || 0} fills · ${sm.approved || 0} approved · ${sm.blocked || 0} blocked (recent window)`;

  renderSortableTable(
    "pf-trades-table",
    ["ts", "strategy", "symbol", "side", "requested", "final", "approved", "reason"],
    t.rows || [],
    (row, col) => {
      if (col === "ts") return tsShort(row.ts);
      if (col === "requested" || col === "final") return row[col] == null ? "—" : pct(row[col]);
      if (col === "approved") return row.approved ? "✓ approved" : "✗ blocked";
      if (col === "side") return (row.side || "").toUpperCase();
      return row[col] ?? "";
    },
    null,
    (row, col) => (col === "approved") ? (row.approved ? "pnl-pos" : "pnl-neg") : null
  );
}

// ----------------------------------------------------------- watchlist (page)
async function loadWatchlistPage() {
  const [w, l] = await Promise.all([api("/api/watchlist"), api("/api/learning")]);

  const note = document.getElementById("wl-note");
  if (note) note.textContent = `— ${w.n_symbols || 0} symbols · ${w.n_held || 0} currently held`;

  renderSortableTable(
    "wl-symbols-table",
    ["symbol", "label", "cls", "held", "qty", "held_by"],
    w.symbols || [],
    (row, col) => {
      if (col === "held") return row.held ? "● held" : "watching";
      if (col === "qty") return row.qty ? row.qty.toLocaleString() : "";
      return row[col] ?? "";
    },
    null,
    (row, col) => (col === "held" && row.held) ? "pnl-pos" : null
  );

  renderSortableTable(
    "wl-strategies-table",
    ["key", "status", "market", "source", "allocation", "last_validated"],
    w.strategies || [],
    (row, col) => {
      if (col === "allocation") return `$${Math.round(row.allocation || 0).toLocaleString()}`;
      return row[col] ?? "";
    },
    (row) => selectStrategy(row.key)
  );

  const reg = l.regime;
  const lc = document.getElementById("learn-cards");
  lc.innerHTML = "";
  addCard(lc, "Regime model", reg ? `${reg.states} states` : "—");
  addCard(lc, "Last model fit", reg && reg.ts ? reg.ts.slice(0, 10) : "—");
  addCard(lc, "Risk lessons tracked", (l.risk_reasons || []).length);
  addCard(lc, "Strategies with notes", (l.strategy_insights || []).length);

  const bic = (reg && reg.bic) || {};
  const bicKeys = Object.keys(bic);
  if (bicKeys.length) {
    plot("chart-regime-bic", [{
      type: "bar",
      x: bicKeys.map((k) => `${k} states`),
      y: bicKeys.map((k) => bic[k]),
      marker: { color: bicKeys.map((k) => (Number(k) === reg.states ? COLORS.green : COLORS.blue)) },
    }], { title: "HMM selection — BIC by state count (lower better, green = chosen)" });
  } else {
    emptyChart("chart-regime-bic", "no regime fits logged yet");
  }

  const rr = (l.risk_reasons || []).slice().reverse();
  if (rr.length) {
    plot("chart-risk-reasons", [{
      type: "bar", orientation: "h",
      x: rr.map((r) => r.count),
      y: rr.map((r) => (r.reason.length > 46 ? r.reason.slice(0, 46) + "…" : r.reason)),
      marker: { color: COLORS.yellow },
    }], { title: "What the risk layer is enforcing (reason frequency)", margin: { l: 250, r: 20, t: 36, b: 40 } });
  } else {
    emptyChart("chart-risk-reasons", "no risk decisions logged yet");
  }

  renderSortableTable(
    "learn-insights-table",
    ["ts", "key", "decision", "rationale"],
    l.strategy_insights || [],
    (row, col) => {
      if (col === "ts") return tsShort(row.ts);
      return row[col] ?? "";
    },
    (row) => selectStrategy(row.key)
  );
}

// ---------------------------------------------------------------- leaderboard
async function loadLeaderboard() {
  const lb = await api(`/api/leaderboard?date=${encodeURIComponent(STATE.date || "")}`);
  const rows = lb.rows || [];

  plot("chart-lb-sharpe", [{
    type: "bar",
    x: rows.map((r) => r.strategy),
    y: rows.map((r) => r.sharpe),
    marker: { color: rows.map((r) => (r.verdict === "PASS" ? COLORS.green : COLORS.red)) },
  }], { title: "Sharpe by strategy", xaxis: { tickangle: -45 } });

  plot("chart-lb-scatter", [{
    type: "scatter",
    mode: "markers",
    x: rows.map((r) => r.max_drawdown),
    y: rows.map((r) => r.sharpe),
    text: rows.map((r) => r.strategy),
    marker: {
      size: rows.map((r) => Math.min(40, Math.max(6, Math.sqrt(Math.abs(r.allocation || 0)) / 12))),
      color: rows.map((r) => DECISION_COLORS[r.decision] || COLORS.muted),
    },
  }], {
    title: "Sharpe vs Max Drawdown (bubble = allocation)",
    xaxis: { title: "max drawdown" },
    yaxis: { title: "sharpe" },
  });

  plot("chart-lb-ruin", [{
    type: "bar",
    x: rows.map((r) => r.strategy),
    y: rows.map((r) => r.prob_of_ruin),
    marker: { color: COLORS.yellow },
  }], { title: "Probability of ruin", xaxis: { tickangle: -45 }, yaxis: { tickformat: ".0%" } });

  renderSortableTable(
    "lb-table",
    ["strategy", "status", "decision", "allocation", "verdict", "sharpe", "calmar", "max_drawdown", "prob_of_ruin", "red_flags"],
    rows,
    (row, col) => {
      if (col === "allocation") return `$${Math.round(row[col] || 0).toLocaleString()}`;
      if (col === "sharpe" || col === "calmar") return (row[col] ?? 0).toFixed(2);
      if (col === "max_drawdown" || col === "prob_of_ruin") return `${((row[col] || 0) * 100).toFixed(1)}%`;
      return row[col] ?? "";
    },
    (row) => selectStrategy(row.strategy)
  );
}

// ---------------------------------------------------------------- generic table
function renderSortableTable(tableId, columns, rows, fmt, onRowClick, cellClass) {
  const table = document.getElementById(tableId);
  const thead = table.querySelector("thead");
  const tbody = table.querySelector("tbody");
  let data = rows.slice();

  thead.innerHTML = "";
  const trh = document.createElement("tr");
  columns.forEach((col, idx) => {
    const th = document.createElement("th");
    th.textContent = col.replace(/_/g, " ");
    th.addEventListener("click", () => {
      const dir = th.dataset.dir === "asc" ? "desc" : "asc";
      trh.querySelectorAll("th").forEach((t) => delete t.dataset.dir);
      th.dataset.dir = dir;
      data = data.slice().sort((a, b) => {
        const av = a[col];
        const bv = b[col];
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        if (typeof av === "number" && typeof bv === "number") return dir === "asc" ? av - bv : bv - av;
        return dir === "asc" ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
      });
      renderRows();
    });
    trh.appendChild(th);
  });
  thead.appendChild(trh);

  function renderRows() {
    tbody.innerHTML = "";
    data.forEach((row) => {
      const tr = document.createElement("tr");
      columns.forEach((col) => {
        const td = document.createElement("td");
        td.textContent = fmt ? fmt(row, col) : (row[col] ?? "");
        if (typeof row[col] === "number") td.classList.add("num");
        if (cellClass) {
          const cls = cellClass(row, col);
          if (cls) td.classList.add(cls);
        }
        tr.appendChild(td);
      });
      if (onRowClick) tr.addEventListener("click", () => onRowClick(row));
      tbody.appendChild(tr);
    });
  }
  renderRows();
}

// ---------------------------------------------------------------- strategies
async function loadStrategiesList() {
  const s = await api(`/api/strategies?date=${encodeURIComponent(STATE.date || "")}`);
  STATE.strategies = s.keys || [];
  const sel = document.getElementById("strategy-select");
  const prev = sel.value;
  sel.innerHTML = "";
  STATE.strategies.forEach((k) => {
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent = k;
    sel.appendChild(opt);
  });
  if (STATE.strategies.length) {
    sel.value = STATE.strategies.includes(prev) ? prev : STATE.strategies[0];
    await loadStrategyDetail(sel.value);
  } else {
    document.getElementById("strategy-detail").innerHTML = `<p class="muted">no strategy reports for ${escapeHtml(STATE.date || "this date")}</p>`;
  }
}

function onStrategyChange() {
  loadStrategyDetail(document.getElementById("strategy-select").value);
}

function selectStrategy(key) {
  if (!STATE.strategies.includes(key)) return;
  document.getElementById("strategy-select").value = key;
  showTab("strategies");
  loadStrategyDetail(key);
}

async function loadStrategyDetail(key) {
  let rep;
  try {
    rep = await api(`/api/strategy/${encodeURIComponent(key)}?date=${encodeURIComponent(STATE.date || "")}`);
  } catch (e) {
    document.getElementById("strategy-detail").innerHTML = `<p class="muted">no report for ${escapeHtml(key)}</p>`;
    return;
  }

  const el = document.getElementById("strategy-detail");
  const reg = rep.registry || {};
  const metrics = rep.metrics || {};
  const blocks = rep.blocks || {};

  let html = `<div class="panel">
    <h2>${escapeHtml(rep.key || key)}
      <span class="badge ${rep.verdict === "PASS" ? "ok" : "bad"}">${escapeHtml(rep.verdict || "—")}</span>
      <span class="badge neutral">${escapeHtml(rep.decision || "—")}${rep.escalated ? " (escalated)" : ""}</span>
      <span class="badge neutral">${rep.deep ? "DEEP" : "cheap-daily"}</span>
    </h2>
    <div class="row">
      <div><span class="muted">status</span> ${escapeHtml(reg.status || "—")}</div>
      <div><span class="muted">allocation</span> $${Math.round(rep.allocation || 0).toLocaleString()}</div>
      <div><span class="muted">source</span> ${escapeHtml(reg.source || "—")}</div>
      <div><span class="muted">market</span> ${escapeHtml(reg.market || "—")}</div>
      <div><span class="muted">last validated</span> ${escapeHtml(reg.last_validated || "—")}</div>
    </div>`;

  if (rep.rationale) html += `<p>${escapeHtml(rep.rationale)}</p>`;

  if (rep.red_flags && rep.red_flags.length) {
    html += `<h3>Red flags</h3><ul>${rep.red_flags.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`;
  }
  if (rep.reasons && rep.reasons.length) {
    html += `<h3>Rejection reasons</h3><ul>${rep.reasons.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`;
  }

  html += `<h3>Gates</h3><div class="gates-grid">${
    Object.entries(rep.gates || {}).map(([g, ok]) => `<span class="gate ${ok ? "pass" : "fail"}">${escapeHtml(g)}</span>`).join("")
  }</div>`;

  html += `<h3>Risk metrics</h3><table><tbody>${
    Object.entries(metrics).map(([k, v]) => `<tr><td>${escapeHtml(k.replace(/_/g, " "))}</td><td class="num">${formatMetric(k, v)}</td></tr>`).join("")
  }</tbody></table>
  </div>`;

  html += `<div class="charts-grid">
    <div class="chart-box"><div class="chart" id="chart-mc-return"></div></div>
    <div class="chart-box"><div class="chart" id="chart-mc-drawdown"></div></div>
    <div class="chart-box"><div class="chart" id="chart-regime"></div></div>
    <div class="chart-box"><div class="chart" id="chart-slippage"></div></div>
    <div class="chart-box"><div class="chart" id="chart-oos"></div></div>
    <div class="chart-box"><div class="chart" id="chart-sensitivity"></div></div>
    <div class="chart-box"><div class="chart" id="chart-equity"></div></div>
  </div>`;

  if (blocks.stress) {
    const stress = blocks.stress;
    html += `<div class="panel"><h3>Stress testing</h3>
      <div class="row">
        <span class="badge ${stress.synthetic_passed ? "ok" : "bad"}">synthetic ${stress.synthetic_passed ? "passed" : "failed"}</span>
        <span class="badge ${stress.events_passed ? "ok" : "bad"}">events ${stress.events_passed ? "passed" : "failed"}</span>
      </div>
      <table><thead><tr><th>event</th><th>result</th></tr></thead><tbody>${
        Object.entries(stress.events || {}).map(([name, v]) => `<tr><td>${escapeHtml(name)}</td><td>${
          v.skipped
            ? `<span class="muted">skipped: ${escapeHtml(v.skipped)}</span>`
            : `<span class="badge ${v.survived ? "ok" : "bad"}">${v.survived ? "survived" : "failed"}</span> &nbsp; drawdown ${((v.max_drawdown || 0) * 100).toFixed(1)}% &nbsp; return ${((v.total_return || 0) * 100).toFixed(1)}%`
        }</td></tr>`).join("")
      }</tbody></table>
    </div>`;
  }

  el.innerHTML = html;
  renderStrategyCharts(rep, blocks);
}

function formatMetric(key, value) {
  if (typeof value !== "number") return escapeHtml(String(value));
  const pct = new Set([
    "total_return", "cagr", "max_drawdown", "ann_vol", "win_rate",
    "average_drawdown", "time_underwater", "exposure",
  ]);
  return pct.has(key) ? `${(value * 100).toFixed(2)}%` : value.toFixed(3);
}

function renderStrategyCharts(rep, blocks) {
  const mc = blocks.monte_carlo || {};

  if (mc.return_distribution) {
    const keys = Object.keys(mc.return_distribution);
    plot("chart-mc-return", [{
      type: "bar",
      x: keys,
      y: keys.map((k) => mc.return_distribution[k] * 100),
      marker: { color: COLORS.blue },
    }], {
      title: `Monte Carlo return % (${mc.n_sims || 0} sims, ruin ${((mc.prob_of_ruin || 0) * 100).toFixed(1)}%)`,
      yaxis: { title: "% return" },
    });
  } else {
    emptyChart("chart-mc-return", mc.error || "no Monte Carlo data (run deep validation)");
  }

  if (mc.drawdown_distribution) {
    const keys = Object.keys(mc.drawdown_distribution);
    plot("chart-mc-drawdown", [{
      type: "bar",
      x: keys,
      y: keys.map((k) => mc.drawdown_distribution[k] * 100),
      marker: { color: COLORS.red },
    }], { title: "Monte Carlo drawdown %", yaxis: { title: "% drawdown" } });
  } else {
    emptyChart("chart-mc-drawdown", mc.error || "no Monte Carlo data (run deep validation)");
  }

  if (blocks.regime_breakdown && blocks.regime_breakdown.length) {
    const rb = blocks.regime_breakdown;
    plot("chart-regime", [{
      type: "bar",
      x: rb.map((r) => r.regime),
      y: rb.map((r) => r.sharpe),
      marker: { color: COLORS.blue },
    }], { title: "Sharpe by regime" });
  } else {
    emptyChart("chart-regime", "no per-regime breakdown");
  }

  if (blocks.slippage) {
    const tiers = Object.entries(blocks.slippage).filter(([, v]) => v && typeof v === "object" && "bps" in v);
    if (tiers.length) {
      plot("chart-slippage", [{
        type: "scatter",
        mode: "lines+markers",
        x: tiers.map(([, v]) => v.bps),
        y: tiers.map(([, v]) => v.sharpe),
        text: tiers.map(([label]) => label),
        marker: { color: COLORS.blue },
        line: { color: COLORS.blue },
      }], {
        title: `Slippage sweep (viable @ high: ${blocks.slippage.viable_at_high ? "yes" : "no"})`,
        xaxis: { title: "slippage (bps)" },
        yaxis: { title: "sharpe" },
      });
    } else {
      emptyChart("chart-slippage", "no slippage sweep data");
    }
  } else {
    emptyChart("chart-slippage", "no slippage sweep (run deep validation)");
  }

  const oos = blocks.out_of_sample || {};
  if (oos.train_sharpe !== undefined && oos.test_sharpe !== undefined) {
    plot("chart-oos", [{
      type: "bar",
      x: ["train", "test"],
      y: [oos.train_sharpe, oos.test_sharpe],
      marker: { color: [COLORS.blue, COLORS.green] },
    }], {
      title: `Out-of-sample (degradation ${((oos.degradation || 0) * 100).toFixed(0)}%)`,
      yaxis: { title: "sharpe" },
    });
  } else {
    emptyChart("chart-oos", oos.error || "no out-of-sample data (run deep validation)");
  }

  const sens = blocks.parameter_sensitivity || {};
  if (sens.applicable) {
    const neighbors = sens.neighbors || [];
    plot("chart-sensitivity", [{
      type: "bar",
      name: "neighbor sharpe",
      x: neighbors.map((n) => `${n.param}=${n.value}`),
      y: neighbors.map((n) => n.sharpe),
      marker: { color: COLORS.blue },
    }, {
      type: "scatter",
      mode: "lines",
      name: "center sharpe",
      x: neighbors.map((n) => `${n.param}=${n.value}`),
      y: neighbors.map(() => sens.center_sharpe),
      line: { color: COLORS.yellow, dash: "dash" },
    }], { title: `Parameter sensitivity (degradation ${((sens.degradation || 0) * 100).toFixed(0)}%)` });
  } else {
    emptyChart("chart-sensitivity", sens.reason || "parameter sensitivity not applicable");
  }

  if (rep.equity_history && rep.equity_history.length) {
    const hist = rep.equity_history;
    plot("chart-equity", [{
      type: "scatter",
      mode: "lines",
      x: hist.map((h) => h[0]),
      y: hist.map((h) => h[1]),
      line: { color: COLORS.green },
    }], { title: "Live equity (omnivore)" });
  } else {
    emptyChart("chart-equity", "no live trading history for this strategy");
  }
}

// ---------------------------------------------------------------- decisions
async function loadDecisions() {
  const d = await api("/api/decisions?limit=500");
  const decisions = (d.decisions || []).slice().reverse();

  const counts = {};
  decisions.forEach((x) => { counts[x.decision] = (counts[x.decision] || 0) + 1; });
  plot("chart-decision-mix", [{
    type: "bar",
    x: Object.keys(counts),
    y: Object.values(counts),
    marker: { color: Object.keys(counts).map((k) => DECISION_COLORS[k] || COLORS.blue) },
  }], { title: "Decision counts (full ledger)", xaxis: { tickangle: -30 } });

  renderSortableTable(
    "decisions-table",
    ["ts", "key", "decision", "allocation", "escalated", "rationale"],
    decisions.slice(0, 200),
    (row, col) => {
      if (col === "allocation") return `$${Math.round(row[col] || 0).toLocaleString()}`;
      if (col === "escalated") return row[col] ? "yes" : "";
      if (col === "rationale") return (row[col] || "").slice(0, 140);
      return row[col] ?? "";
    },
    (row) => selectStrategy(row.key)
  );
}

// ---------------------------------------------------------------- omnivore
async function loadOmnivore() {
  const o = await api("/api/omnivore");
  const rows = o.rows || [];
  const equity = o.equity || {};

  const top = rows.slice(0, 10);
  plot("chart-omni-equity", top.map((r) => ({
    type: "scatter",
    mode: "lines",
    name: r.strategy,
    x: (equity[r.strategy] || []).map((h) => h[0]),
    y: (equity[r.strategy] || []).map((h) => h[1]),
  })), { title: "Live equity curves (top 10 by P&L%)" });

  renderSortableTable(
    "omni-table",
    ["strategy", "market", "enabled", "equity", "total_pnl", "pnl_pct", "realized_pnl", "sharpe", "trades", "open_positions", "observations"],
    rows,
    (row, col) => {
      if (col === "equity" || col === "total_pnl" || col === "realized_pnl") return `$${(row[col] || 0).toLocaleString()}`;
      if (col === "pnl_pct") return `${((row[col] || 0) * 100).toFixed(2)}%`;
      if (col === "enabled") return row[col] ? "yes" : "no";
      return row[col] ?? "";
    },
    (row) => selectStrategy(row.strategy)
  );
}

// ---------------------------------------------------------------- controls
async function loadCommands() {
  const c = await api("/api/commands");
  const commands = c.commands || {};
  const grid = document.getElementById("controls-grid");
  grid.innerHTML = "";

  if (commands["govern-daily-notrade"]) {
    addControlCard(grid, "govern-daily-notrade", commands["govern-daily-notrade"], (card) => {
      const wrap = document.createElement("label");
      wrap.className = "check";
      wrap.innerHTML = `<input type="checkbox" id="ctl-gd-deep" /> deep validation (Monte Carlo, sweeps, stress)`;
      card.appendChild(wrap);
      return () => ({ deep: document.getElementById("ctl-gd-deep").checked });
    });
  }

  if (commands["govern-validate"]) {
    addControlCard(grid, "govern-validate", commands["govern-validate"], (card) => {
      const keys = Object.keys(STATE.registry || {});
      const label = document.createElement("label");
      label.textContent = "strategy";
      const select = document.createElement("select");
      select.id = "ctl-gv-strategy";
      keys.forEach((k) => {
        const opt = document.createElement("option");
        opt.value = k;
        opt.textContent = k;
        select.appendChild(opt);
      });
      const check = document.createElement("label");
      check.className = "check";
      check.innerHTML = `<input type="checkbox" id="ctl-gv-deep" /> deep validation`;
      card.appendChild(label);
      card.appendChild(select);
      card.appendChild(check);
      return () => ({
        strategy: document.getElementById("ctl-gv-strategy").value,
        deep: document.getElementById("ctl-gv-deep").checked,
      });
    });
  }

  if (commands["govern-optimize"]) {
    addControlCard(grid, "govern-optimize", commands["govern-optimize"], (card) => {
      const keys = Object.keys(STATE.registry || {});
      const label = document.createElement("label");
      label.textContent = "strategy (optional)";
      const select = document.createElement("select");
      select.id = "ctl-go-strategy";
      const allOpt = document.createElement("option");
      allOpt.value = "";
      allOpt.textContent = "(whole universe)";
      select.appendChild(allOpt);
      keys.forEach((k) => {
        const opt = document.createElement("option");
        opt.value = k;
        opt.textContent = k;
        select.appendChild(opt);
      });
      card.appendChild(label);
      card.appendChild(select);
      return () => {
        const v = document.getElementById("ctl-go-strategy").value;
        return v ? { strategy: v } : {};
      };
    });
  }

  if (commands["refresh-data"]) {
    addControlCard(grid, "refresh-data", commands["refresh-data"], () => () => ({}));
  }

  if (commands["notion-sync"]) {
    addControlCard(grid, "notion-sync", commands["notion-sync"], (card) => {
      const label = document.createElement("label");
      label.textContent = "date (optional, default today)";
      const input = document.createElement("input");
      input.type = "date";
      input.id = "ctl-ns-date";
      card.appendChild(label);
      card.appendChild(input);
      return () => {
        const v = document.getElementById("ctl-ns-date").value;
        return v ? { date: v } : {};
      };
    });
  }
}

function addControlCard(grid, command, label, setup) {
  const card = document.createElement("div");
  card.className = "control-card panel";
  const h3 = document.createElement("h3");
  h3.textContent = label || command;
  card.appendChild(h3);

  const getParams = setup(card) || (() => ({}));

  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = "Run";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      await apiPost("/api/run", { command, params: getParams() });
      showTab("controls");
      await loadJobs();
    } catch (e) {
      alert(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Run";
    }
  });
  card.appendChild(btn);
  grid.appendChild(card);
}

function statusBadgeClass(status) {
  if (status === "running") return "warn";
  if (status === "succeeded") return "ok";
  if (status === "failed") return "bad";
  return "neutral";
}

function renderJobsTable(jobsList) {
  const table = document.getElementById("jobs-table");
  const thead = table.querySelector("thead");
  const tbody = table.querySelector("tbody");
  thead.innerHTML = "<tr><th>label</th><th>status</th><th>started</th><th>ended</th><th>rc</th></tr>";
  tbody.innerHTML = "";
  jobsList.forEach((job) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(job.label || job.command || "")}</td>
      <td><span class="badge ${statusBadgeClass(job.status)}">${escapeHtml(job.status)}</span></td>
      <td>${escapeHtml(job.started || "")}</td>
      <td>${escapeHtml(job.ended || "")}</td>
      <td class="num">${job.returncode ?? ""}</td>`;
    tr.addEventListener("click", () => viewJobLog(job.id));
    tbody.appendChild(tr);
  });
}

async function loadJobs() {
  const j = await api("/api/jobs");
  const jobsList = j.jobs || [];
  renderJobsTable(jobsList);

  const running = jobsList.some((x) => x.status === "running");
  if (running && !STATE.jobPoll) {
    STATE.jobPoll = setInterval(pollJobs, 4000);
  } else if (!running && STATE.jobPoll) {
    clearInterval(STATE.jobPoll);
    STATE.jobPoll = null;
  }
}

async function pollJobs() {
  const j = await api("/api/jobs");
  const jobsList = j.jobs || [];
  renderJobsTable(jobsList);
  if (STATE.currentJobLog) await viewJobLog(STATE.currentJobLog);
  if (!jobsList.some((x) => x.status === "running") && STATE.jobPoll) {
    clearInterval(STATE.jobPoll);
    STATE.jobPoll = null;
  }
}

async function viewJobLog(jobId) {
  STATE.currentJobLog = jobId;
  const log = await api(`/api/jobs/${encodeURIComponent(jobId)}/log?n=400`);
  document.getElementById("job-log-label").textContent = `${jobId} (${log.status || "?"})`;
  document.getElementById("job-log").textContent = (log.lines || []).join("\n") || (log.error || "(no output yet)");
}

// ---------------------------------------------------------------- logs
async function loadLogTail() {
  const name = document.getElementById("log-channel").value;
  const n = document.getElementById("log-lines").value || 200;
  const l = await api(`/api/logs/tail?name=${encodeURIComponent(name)}&n=${encodeURIComponent(n)}`);
  document.getElementById("log-view").textContent = (l.lines || []).join("\n") || (l.error || "(empty)");
}
