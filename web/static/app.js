/* Quant dashboard front-end. No build step, no external dependencies. */
(() => {
  "use strict";

  const state = {
    snapshot: null,
    symbol: "SPX",
    rangeDays: 252,
    history: [],   // [{role, content}] sent back for assistant continuity
    claude: false,
  };

  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- utils
  const num = (v, digits = 2) =>
    v === null || v === undefined || Number.isNaN(v)
      ? "—"
      : v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

  const pct = (v, digits = 2) =>
    v === null || v === undefined || Number.isNaN(v) ? "—" : `${v >= 0 ? "+" : ""}${num(v, digits)}%`;

  const dirClass = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");

  const escapeHtml = (s) =>
    s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* Minimal markdown: **bold**, `code`, and paragraphs. Escaped first. */
  const renderText = (text) =>
    escapeHtml(text)
      .split(/\n{2,}/)
      .map((block) =>
        `<p>${block
          .replace(/\n/g, "<br>")
          .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
          .replace(/`(.+?)`/g, "<code>$1</code>")}</p>`
      )
      .join("");

  async function api(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch (_) { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // ------------------------------------------------------------- rendering
  function renderQuotes() {
    const wrap = $("quotes");
    const instruments = state.snapshot.instruments || {};
    wrap.innerHTML = Object.entries(instruments)
      .map(([sym, block]) => {
        const q = block.quote;
        const m = block.metrics || {};
        const cls = dirClass(q.change);
        const digits = sym === "SPY" ? 2 : 2;
        return `
          <article class="quote-card">
            <div class="sym">${sym}</div>
            <div class="name">${escapeHtml(q.name)}</div>
            <div class="price">${num(q.price, digits)}</div>
            <div class="delta ${cls}">${num(q.change, digits)} (${pct(q.change_pct)})</div>
            <div class="meta">
              <div>Prev close <b>${num(q.previous_close, digits)}</b></div>
              <div>Day range <b>${num(q.day_low, digits)}–${num(q.day_high, digits)}</b></div>
              <div>YTD <b class="${dirClass(m.returns?.ytd)}">${pct(m.returns?.ytd)}</b></div>
              <div>From 52w high <b class="${dirClass(m.range_52w?.pct_from_high)}">${pct(m.range_52w?.pct_from_high)}</b></div>
            </div>
          </article>`;
      })
      .join("");
  }

  const METRIC_ROWS = [
    { section: "Returns" },
    { label: "1 day", get: (m) => pct(m.returns?.["1d"]), color: (m) => m.returns?.["1d"] },
    { label: "1 week", get: (m) => pct(m.returns?.["1w"]), color: (m) => m.returns?.["1w"] },
    { label: "1 month", get: (m) => pct(m.returns?.["1m"]), color: (m) => m.returns?.["1m"] },
    { label: "3 months", get: (m) => pct(m.returns?.["3m"]), color: (m) => m.returns?.["3m"] },
    { label: "Year to date", get: (m) => pct(m.returns?.ytd), color: (m) => m.returns?.ytd },
    { label: "1 year", get: (m) => pct(m.returns?.["1y"]), color: (m) => m.returns?.["1y"] },
    { section: "Moving averages" },
    { label: "SMA 20", get: (m) => `${num(m.moving_averages?.sma20?.value)} (${pct(m.moving_averages?.sma20?.distance_pct)})` },
    { label: "SMA 50", get: (m) => `${num(m.moving_averages?.sma50?.value)} (${pct(m.moving_averages?.sma50?.distance_pct)})` },
    { label: "SMA 200", get: (m) => `${num(m.moving_averages?.sma200?.value)} (${pct(m.moving_averages?.sma200?.distance_pct)})` },
    { label: "Trend", get: (m) => m.trend ?? "—" },
    { section: "Risk" },
    { label: "Realized vol 20d", get: (m) => `${num(m.volatility?.realized_20d)}%` },
    { label: "Realized vol 60d", get: (m) => `${num(m.volatility?.realized_60d)}%` },
    { label: "Max drawdown 52w", get: (m) => `${num(m.max_drawdown_pct)}%`, color: (m) => m.max_drawdown_pct },
    { label: "RSI (14)", get: (m) => num(m.rsi_14) },
    { section: "52-week range" },
    { label: "High", get: (m) => num(m.range_52w?.high) },
    { label: "Low", get: (m) => num(m.range_52w?.low) },
    { label: "From high", get: (m) => pct(m.range_52w?.pct_from_high), color: (m) => m.range_52w?.pct_from_high },
  ];

  function renderMetrics() {
    const instruments = state.snapshot.instruments || {};
    const cols = ["SPX", "SPY"].filter((s) => instruments[s]);
    const body = $("metrics").querySelector("tbody");
    body.innerHTML = METRIC_ROWS.map((row) => {
      if (row.section) return `<tr class="section"><td colspan="3">${row.section}</td></tr>`;
      const cells = cols
        .map((sym) => {
          const m = instruments[sym].metrics || {};
          const cls = row.color ? dirClass(row.color(m)) : "";
          return `<td class="${cls}">${row.get(m)}</td>`;
        })
        .join("");
      return `<tr><td>${row.label}</td>${cells}</tr>`;
    }).join("");
  }

  // ------------------------------------------------------------------ chart
  const SVG_NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  };

  function visibleBars() {
    const block = state.snapshot?.instruments?.[state.symbol];
    if (!block) return [];
    const bars = block.bars || [];
    return state.rangeDays > 0 ? bars.slice(-state.rangeDays) : bars;
  }

  function drawChart() {
    const svg = $("chart");
    svg.innerHTML = "";
    const bars = visibleBars();
    const caption = $("chart-caption");
    if (bars.length < 2) {
      caption.textContent = "Not enough history for this range.";
      return;
    }

    const W = 900, H = 340, PAD = { top: 14, right: 62, bottom: 26, left: 10 };
    const closes = bars.map((b) => b.close);
    let min = Math.min(...closes), max = Math.max(...closes);
    const pad = (max - min) * 0.08 || max * 0.01;
    min -= pad; max += pad;

    const plotW = W - PAD.left - PAD.right;
    const plotH = H - PAD.top - PAD.bottom;
    const x = (i) => PAD.left + (i / (bars.length - 1)) * plotW;
    const y = (v) => PAD.top + (1 - (v - min) / (max - min)) * plotH;

    // horizontal gridlines + right-hand price axis
    const ticks = 5;
    for (let i = 0; i <= ticks; i++) {
      const value = min + ((max - min) * i) / ticks;
      const yy = y(value);
      svg.appendChild(el("line", { x1: PAD.left, x2: PAD.left + plotW, y1: yy, y2: yy, stroke: "#2a323d", "stroke-width": 1 }));
      const label = el("text", { x: PAD.left + plotW + 8, y: yy + 4, "text-anchor": "start" });
      label.textContent = value.toLocaleString(undefined, { maximumFractionDigits: 0 });
      svg.appendChild(label);
    }

    const rising = closes[closes.length - 1] >= closes[0];
    const stroke = rising ? "#2ea56b" : "#e5534b";

    const line = bars.map((b, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(b.close).toFixed(2)}`).join(" ");
    const gradId = "area-fill";
    const defs = el("defs");
    const grad = el("linearGradient", { id: gradId, x1: "0", y1: "0", x2: "0", y2: "1" });
    grad.appendChild(el("stop", { offset: "0%", "stop-color": stroke, "stop-opacity": "0.28" }));
    grad.appendChild(el("stop", { offset: "100%", "stop-color": stroke, "stop-opacity": "0" }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    svg.appendChild(el("path", {
      d: `${line} L${x(bars.length - 1).toFixed(2)},${PAD.top + plotH} L${PAD.left},${PAD.top + plotH} Z`,
      fill: `url(#${gradId})`, stroke: "none",
    }));
    svg.appendChild(el("path", { d: line, fill: "none", stroke, "stroke-width": 2, "stroke-linejoin": "round" }));

    // x-axis date labels at both ends and the midpoint
    [0, Math.floor(bars.length / 2), bars.length - 1].forEach((i, n) => {
      const t = el("text", {
        x: x(i), y: H - 8,
        "text-anchor": n === 0 ? "start" : n === 2 ? "end" : "middle",
      });
      t.textContent = bars[i].date;
      svg.appendChild(t);
    });

    // hover crosshair
    const cross = el("line", { y1: PAD.top, y2: PAD.top + plotH, stroke: "#8b98a8", "stroke-width": 1, "stroke-dasharray": "3 3", opacity: "0" });
    const dot = el("circle", { r: 3.5, fill: stroke, stroke: "#0d1117", "stroke-width": 1.5, opacity: "0" });
    svg.appendChild(cross);
    svg.appendChild(dot);

    const first = bars[0].close, last = bars[bars.length - 1].close;
    const defaultCaption = `${state.symbol} · ${bars.length} sessions · ${bars[0].date} → ${bars[bars.length - 1].date} · ${pct((last / first - 1) * 100)}`;
    caption.textContent = defaultCaption;

    const overlay = el("rect", { x: PAD.left, y: PAD.top, width: plotW, height: plotH, fill: "transparent" });
    overlay.addEventListener("pointermove", (event) => {
      const rect = svg.getBoundingClientRect();
      const rel = ((event.clientX - rect.left) / rect.width) * W;
      const i = Math.max(0, Math.min(bars.length - 1, Math.round(((rel - PAD.left) / plotW) * (bars.length - 1))));
      const bar = bars[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", "1");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(bar.close)); dot.setAttribute("opacity", "1");
      caption.textContent = `${bar.date}  O ${num(bar.open)}  H ${num(bar.high)}  L ${num(bar.low)}  C ${num(bar.close)}`;
    });
    overlay.addEventListener("pointerleave", () => {
      cross.setAttribute("opacity", "0");
      dot.setAttribute("opacity", "0");
      caption.textContent = defaultCaption;
    });
    svg.appendChild(overlay);
  }

  // ------------------------------------------------------------- data load
  async function loadMarket(refresh = false) {
    const btn = $("refresh");
    btn.disabled = true;
    btn.textContent = "Loading…";
    try {
      state.snapshot = await api(`/api/market?refresh=${refresh}&history=1200`);
      renderQuotes();
      renderMetrics();
      drawChart();

      const badge = $("source-badge");
      badge.hidden = false;
      badge.textContent = state.snapshot.live ? "live" : "demo data";
      badge.className = `badge ${state.snapshot.live ? "live" : "demo"}`;

      const when = new Date(state.snapshot.fetched_at * 1000).toLocaleTimeString();
      const sources = Object.entries(state.snapshot.sources || {}).map(([k, v]) => `${k}→${v}`).join(", ");
      $("status-line").textContent =
        `Updated ${when} · ${sources}` + (state.snapshot.errors?.length ? ` · ${state.snapshot.errors.length} provider issue(s)` : "");
    } catch (err) {
      $("status-line").textContent = `Could not load market data: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Refresh";
    }
  }

  // -------------------------------------------------------------- assistant
  function addMessage(role, text, extraClass = "") {
    const thread = $("thread");
    const div = document.createElement("div");
    div.className = `msg ${role} ${extraClass}`.trim();
    div.innerHTML = `<div class="who">${role === "user" ? "You" : "Analysis"}</div>${renderText(text)}`;
    thread.appendChild(div);
    thread.scrollTop = thread.scrollHeight;
    return div;
  }

  async function askQuestion(question) {
    if (!question.trim()) return;
    addMessage("user", question);
    state.history.push({ role: "user", content: question });
    const pending = addMessage("assistant", "Analysing…", "pending");
    $("ask-btn").disabled = true;

    try {
      const data = await api("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, history: state.history.slice(-12, -1) }),
      });
      pending.classList.remove("pending");
      const footer = data.warning ? `\n\n_${data.warning}_` : "";
      pending.innerHTML =
        `<div class="who">Analysis · ${data.engine}${data.model ? " · " + escapeHtml(data.model) : ""}</div>` +
        renderText(data.answer + footer);
      state.history.push({ role: "assistant", content: data.answer });
    } catch (err) {
      pending.className = "msg assistant error";
      pending.innerHTML = `<div class="who">Error</div>${renderText(err.message)}`;
      state.history.pop();
    } finally {
      $("ask-btn").disabled = false;
    }
  }

  // ---------------------------------------------------------------- context
  async function loadNotes() {
    const { notes } = await api("/api/context");
    const list = $("notes");
    list.innerHTML = notes.length
      ? notes
          .map((n) => `<li><span>${escapeHtml(n.text)}</span><button data-id="${n.id}" title="Delete note">×</button></li>`)
          .join("")
      : `<li class="empty">No context saved yet.</li>`;
    $("note-count").textContent = `${notes.length} note${notes.length === 1 ? "" : "s"} sent with each question`;
  }

  // ------------------------------------------------------------------ wiring
  function wire() {
    $("refresh").addEventListener("click", () => loadMarket(true));

    $("symbol-toggle").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      state.symbol = btn.dataset.symbol;
      [...e.currentTarget.children].forEach((b) => b.classList.toggle("active", b === btn));
      drawChart();
    });

    $("range-toggle").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      state.rangeDays = Number(btn.dataset.days);
      [...e.currentTarget.children].forEach((b) => b.classList.toggle("active", b === btn));
      drawChart();
    });

    $("tabs").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      [...e.currentTarget.children].forEach((b) => {
        const on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", String(on));
      });
      $("tab-ask").hidden = btn.dataset.tab !== "ask";
      $("tab-context").hidden = btn.dataset.tab !== "context";
    });

    $("ask-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const field = $("question");
      const value = field.value;
      field.value = "";
      askQuestion(value);
    });

    $("question").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        $("ask-form").requestSubmit();
      }
    });

    $("chips").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (btn) askQuestion(btn.textContent.trim());
    });

    $("note-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const field = $("note-text");
      const text = field.value.trim();
      if (!text) return;
      field.value = "";
      await api("/api/context", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      loadNotes();
    });

    $("notes").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-id]");
      if (!btn) return;
      await api(`/api/context/${btn.dataset.id}`, { method: "DELETE" });
      loadNotes();
    });
  }

  async function init() {
    wire();
    await loadMarket(false);
    await loadNotes();
    try {
      const health = await api("/api/health");
      state.claude = health.assistant.claude_available;
      $("engine-note").textContent = state.claude
        ? `Claude (${health.assistant.model})`
        : "Local metrics engine · set ANTHROPIC_API_KEY for free-form analysis";
    } catch (_) { /* health is advisory only */ }
  }

  init();
})();
