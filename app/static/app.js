/* 个股异动研究 Agent —— 前端
 *
 * 对话信息流：用户提问在右侧，研究结论作为助手回复展开。
 * 只做三件事：发起 SSE、渲染服务端给的结构化 Brief、提供证据反查。
 * 不在前端做任何数值计算或结论加工。
 */

const $ = (id) => document.getElementById(id);

const state = {
  window: null,
  brief: null,
  evidenceById: {},
  timer: null,
  source: null,
  messages: [],
  nextId: 1,
  windows: [],
};

const EXAMPLES = [
  { q: "宁德时代最近5个交易日怎么一直跌？", w: "d5" },
  { q: "贵州茅台今天为什么跌了？", w: "today" },
  { q: "宁德时代和贵州茅台今天异动对比", w: "today" },
];

async function init() {
  $("sideExamples").innerHTML = EXAMPLES.map((e, i) =>
    `<button class="side-ex" type="button" data-i="${i}">${esc(e.q)}</button>`
  ).join("");
  $("sideExamples").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".side-ex");
    if (!btn) return;
    const ex = EXAMPLES[Number(btn.dataset.i)];
    $("queryInput").value = ex.q;
    setWindow(ex.w);
    run();
  });

  try {
    const cfg = await (await fetch("/api/config")).json();
    state.windows = cfg.windows || [];
    renderProviders(cfg);
    renderWindowChips(cfg.windows);
  } catch (e) {
    appendSystemError("无法加载运行配置", String(e));
  }

  $("runBtn").addEventListener("click", run);
  $("queryInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); run(); }
  });
  $("queryInput").addEventListener("input", autosize);
  $("newChatBtn").addEventListener("click", resetThread);
  $("sidebarToggle").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  $("evidenceBtn").addEventListener("click", () => state.brief && openDrawer("evidence"));
  $("traceBtn").addEventListener("click", () => state.brief && openDrawer("trace"));
  $("drawerClose").addEventListener("click", closeDrawer);
  $("drawerBackdrop").addEventListener("click", closeDrawer);

  const params = new URLSearchParams(location.search);
  const q = params.get("q");
  if (q) {
    $("queryInput").value = q;
    setWindow(params.get("w"));
    run();
  } else {
    paintThread();
  }
}

function renderProviders(cfg) {
  $("sideStatus").innerHTML = [
    ["行情", cfg.providers.market],
    ["资讯", cfg.providers.evidence],
    ["推理", cfg.providers.llm],
  ].map(([k, v]) => `${k} <b>${esc(v)}</b>`).join("<br>");

  if (cfg.degraded && cfg.notices.length) {
    $("degradedBanner").classList.remove("hidden");
    $("degradedBanner").innerHTML =
      `<b>当前跑在降级数据上</b><ul>` +
      cfg.notices.map((n) => `<li>${esc(n)}</li>`).join("") +
      `</ul>`;
  }
}

function renderWindowChips(windows) {
  const box = $("windowChips");
  box.innerHTML = "";
  windows.forEach((w) => {
    const el = document.createElement("span");
    el.className = "chip";
    el.textContent = w.label;
    el.dataset.value = w.value;
    el.addEventListener("click", () => setWindow(state.window === w.value ? null : w.value));
    box.appendChild(el);
  });
}

function setWindow(value) {
  state.window = value;
  [...$("windowChips").children].forEach((c) =>
    c.classList.toggle("active", c.dataset.value === value)
  );
}

function autosize() {
  const el = $("queryInput");
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 140) + "px";
}

function resetThread() {
  if (state.source) state.source.close();
  state.messages = [];
  state.brief = null;
  state.evidenceById = {};
  $("evidenceBtn").disabled = true;
  $("traceBtn").disabled = true;
  state.brief = null;
  $("queryInput").value = "";
  autosize();
  paintThread();
}

function appendSystemError(message, hint) {
  state.messages.push({
    id: state.nextId++,
    role: "assistant",
    phase: "error",
    error: { message, hint },
  });
  paintThread();
}

function run(presetQuery) {
  const query = (presetQuery ?? $("queryInput").value).trim();
  if (!query) return;

  if (state.source) state.source.close();

  state.messages.push({ id: state.nextId++, role: "user", text: query });
  const aid = state.nextId++;
  state.messages.push({
    id: aid,
    role: "assistant",
    phase: "thinking",
    steps: [],
    elapsed: "0.0s",
    brief: null,
    briefs: [],
    notice: "",
    taskLabel: "",
    error: null,
  });
  $("queryInput").value = "";
  autosize();
  paintThread();

  $("runBtn").disabled = true;
  const params = new URLSearchParams({ query });
  if (state.window) params.set("window", state.window);
  const ctx = conversationContext();
  if (ctx.stocks.length) params.set("context_stocks", ctx.stocks.join(","));
  if (ctx.queries.length) params.set("context_queries", ctx.queries.join("\n"));
  if (ctx.window) params.set("context_window", ctx.window);

  const started = performance.now();
  state.timer = setInterval(() => {
    const msg = state.messages.find((m) => m.id === aid);
    if (msg) msg.elapsed = ((performance.now() - started) / 1000).toFixed(1) + "s";
    const el = document.querySelector(`[data-elapsed="${aid}"]`);
    if (el) el.textContent = msg.elapsed;
  }, 120);

  const source = new EventSource(`/api/research/stream?${params}`);
  state.source = source;

  source.addEventListener("notice", (e) => {
    const msg = state.messages.find((m) => m.id === aid);
    if (!msg) return;
    msg.notice = JSON.parse(e.data).message || "";
    paintAssistant(aid);
  });
  source.addEventListener("start", (e) => {
    const msg = state.messages.find((m) => m.id === aid);
    if (msg) msg.steps = JSON.parse(e.data).steps.map((s) => ({ ...s, state: "pending" }));
    paintAssistant(aid);
  });
  source.addEventListener("task", (e) => {
    const data = JSON.parse(e.data);
    const msg = state.messages.find((m) => m.id === aid);
    if (!msg) return;
    msg.taskLabel = `正在处理：${data.label}（${data.index + 1}/${data.total}）`;
    if (msg.steps) msg.steps.forEach((s) => { s.state = "pending"; s.detail = ""; });
    paintAssistant(aid);
  });
  source.addEventListener("step", (e) => {
    const data = JSON.parse(e.data);
    const msg = state.messages.find((m) => m.id === aid);
    if (!msg) return;
    const step = msg.steps.find((s) => s.key === data.key);
    if (step) { step.state = data.state; step.detail = data.detail; }
    paintAssistant(aid);
  });
  source.addEventListener("brief", (e) => {
    const brief = JSON.parse(e.data);
    const msg = state.messages.find((m) => m.id === aid);
    if (!msg) return;
    msg.phase = "brief";
    msg.briefs = msg.briefs || [];
    msg.briefs.push(brief);
    msg.brief = brief;
    adoptBriefs(msg.briefs);
    paintAssistant(aid);
  });
  source.addEventListener("reply", (e) => {
    const data = JSON.parse(e.data);
    const msg = state.messages.find((m) => m.id === aid);
    if (!msg) return;
    msg.phase = "reply";
    msg.reply = data.message || "";
    msg.replyHint = data.hint || "";
    paintAssistant(aid);
  });
  source.addEventListener("error", (e) => {
    if (!e.data) return;
    const msg = state.messages.find((m) => m.id === aid);
    if (!msg) return;
    msg.phase = "error";
    msg.error = JSON.parse(e.data);
    paintAssistant(aid);
  });
  source.addEventListener("done", () => finish(source));
  source.onerror = () => finish(source);
}

function conversationContext() {
  const priors = [];
  let latestStocks = [];
  let latestWindow = "";
  for (const m of state.messages) {
    if (m.role === "user" && m.text) priors.push(m.text);
    if (m.role !== "assistant") continue;
    const briefs = m.briefs && m.briefs.length ? m.briefs : (m.brief ? [m.brief] : []);
    const names = briefs.map((b) => b && b.subject && b.subject.stock && b.subject.stock.name).filter(Boolean);
    if (names.length) {
      latestStocks = names;
      latestWindow = (briefs[0].subject.window && briefs[0].subject.window.window) || "";
    }
  }
  return {
    stocks: [...new Set(latestStocks)],
    queries: priors.slice(0, -1).slice(-6),
    window: latestWindow,
  };
}

function finish(source) {
  source.close();
  clearInterval(state.timer);
  $("runBtn").disabled = false;
}

function adoptBriefs(briefs) {
  const list = briefs || [];
  const report = [...list].reverse().find((b) => b.kind !== "snapshot") || list[list.length - 1];
  state.brief = report || null;
  state.evidenceById = {};
  list.forEach((b) => (b.evidence || []).forEach((e) => { state.evidenceById[e.id] = e; }));
  const hasEvidence = list.some((b) => (b.evidence || []).length);
  $("evidenceBtn").disabled = !hasEvidence;
  $("traceBtn").disabled = !report;
}

function paintThread() {
  const box = $("thread");
  if (!state.messages.length) {
    box.innerHTML = `
      <div class="greeting">
        <h1>这只股票，发生了什么？</h1>
        <p>用一句自然语言提问。我会把价格变化说清楚，再把原因和证据摊开，而不是给买卖建议。</p>
      </div>
      <div class="suggest">
        ${EXAMPLES.map((e) =>
          `<button type="button" data-q="${esc(e.q)}" data-w="${e.w || ""}">${esc(e.q)}</button>`
        ).join("")}
      </div>`;
    box.querySelectorAll(".suggest button").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("queryInput").value = btn.dataset.q;
        setWindow(btn.dataset.w || null);
        run();
      });
    });
    return;
  }

  box.innerHTML = state.messages.map((m) =>
    m.role === "user"
      ? `<div class="msg user"><div class="bubble">${esc(m.text)}</div></div>`
      : `<div class="msg assistant" id="msg-${m.id}"><div class="reply">${renderReply(m)}</div></div>`
  ).join("");
  bindReply(box);
  const last = state.messages[state.messages.length - 1];
  revealMessage(last && last.phase === "brief" ? last.id : null);
}

function paintAssistant(id) {
  const msg = state.messages.find((m) => m.id === id);
  const host = $(`msg-${id}`);
  if (!msg || !host) { paintThread(); return; }
  host.querySelector(".reply").innerHTML = renderReply(msg);
  bindReply(host);
  revealMessage(msg.phase === "brief" ? id : null);
}

function revealMessage(id) {
  const thread = $("thread");
  if (id) {
    const host = $(`msg-${id}`);
    if (host) {
      thread.scrollTop = Math.max(0, host.offsetTop - 12);
      return;
    }
  }
  thread.scrollTop = thread.scrollHeight;
}

function renderReply(m) {
  if (m.phase === "thinking") return renderThinking(m);
  if (m.phase === "reply") {
    return `<div class="plain-card">
      <p>${esc(m.reply || "")}</p>
      ${m.replyHint ? `<p class="hint">${esc(m.replyHint)}</p>` : ""}
    </div>`;
  }
  if (m.phase === "error") return renderError(m.error);
  if (m.phase === "brief") {
    const briefs = m.briefs && m.briefs.length ? m.briefs : (m.brief ? [m.brief] : []);
    return briefs.map((brief, i) =>
      renderBriefHtml(brief, `${m.id}-${i}`, briefs.length > 1)
    ).join("");
  }
  return "";
}

function renderThinking(m) {
  if (!(m.steps || []).length && !m.notice) {
    return `<div class="plain-card"><p>正在理解您的问题…</p></div>`;
  }
  const notice = m.notice ? `<div class="think-notice">${esc(m.notice)}</div>` : "";
  const task = m.taskLabel ? `<p class="think-task">${esc(m.taskLabel)}</p>` : "";
  const steps = (m.steps || []).map((s) => {
    const mark = s.state === "done" ? "✓" : s.state === "running" ? "→" : s.state === "failed" ? "×" : "○";
    return `<li class="${s.state || ""}"><span>${mark}</span><span>${esc(s.label)}</span>
      <span class="detail">${esc(s.detail || "")}</span></li>`;
  }).join("");
  return `<div class="think">
    ${notice}
    <div class="think-head"><span><i class="dot"></i>正在研究</span>
      <span data-elapsed="${m.id}">${esc(m.elapsed || "0.0s")}</span></div>
    ${task}
    <ul class="steps">${steps}</ul>
  </div>`;
}

function renderError(err) {
  const actions = err && err.code === "need_window"
    ? `<div class="follow" style="margin-top:12px">${
        [["today", "今日"], ["d3", "最近 3 个交易日"], ["d5", "最近 5 个交易日"]]
          .map(([v, l]) => `<button type="button" data-win="${v}">${l}</button>`).join("")
      }</div>`
    : "";
  const calm = err && (err.code === "nonsense" || err.code === "need_stock" || err.code === "chitchat");
  if (calm) {
    return `<div class="plain-card">
      <p>${esc(err.message || "")}</p>
      ${err.hint ? `<p class="hint">${esc(err.hint)}</p>` : ""}
    </div>`;
  }
  return `<div class="error-card">
    <h2>${esc(err.message || "研究未能完成")}</h2>
    <p>${esc(err.hint || "")}</p>${actions}
  </div>`;
}

function renderBriefHtml(brief, mid, split) {
  const s = brief.subject;
  const w = brief.what_happened;
  const snapshot = brief.kind === "snapshot";
  const dates = s.window.actual_start === s.window.actual_end
    ? s.window.actual_end
    : `${s.window.actual_start} ~ ${s.window.actual_end}`;
  const notes = [];
  if (s.window.remap_note) notes.push(s.window.remap_note);
  if (s.window.is_intraday) notes.push("当前为盘中数据，部分指标不可用。");
  const met = brief.metrics;
  const kicker = split
    ? `<div class="company-kicker">${esc(s.stock.name)} · ${esc(s.stock.thscode)}</div>`
    : "";

  const heroMetrics = snapshot
    ? ""
    : `<div class="metrics">${
        [
          ["可验证洞察", (met.time_to_verifiable_insight_ms / 1000).toFixed(1) + "s"],
          ["证据覆盖", pctStr(met.evidence_coverage)],
          ["无证据推断", pctStr(met.unsupported_inference_rate)],
          ["证据条目", String(met.evidence_count)],
          ["独立信源", String(met.independent_source_count)],
        ].map(([k, v]) => `<div class="metric"><span>${k}</span><b>${esc(v)}</b></div>`).join("")
      }</div>`;

  const rest = snapshot
    ? `<p class="disclaimer">若需要完整异动分析报告，请直接说明。</p>`
    : `
    <section class="block">
      <h3>和市场、行业比</h3>
      ${compareHtml(w.comparison)}
      <p class="disclaimer">${esc(w.comparison.disclaimer)}</p>
      ${gapsHtml(w.gaps, "这一段缺的数据")}
    </section>

    <section class="block">
      <h3>为什么会这样</h3>
      <div class="priority"><b>${esc(brief.why_happened.priority_label)}</b>
        <p>${esc(brief.why_happened.priority_reason)}</p></div>
      <p class="hint">${esc(brief.why_happened.evidence_window_note)}</p>
      ${brief.why_happened.drivers.length
        ? brief.why_happened.drivers.map(renderDriver).join("")
        : `<p class="hint">这次没有形成候选驱动因素。</p>`}
      ${brief.why_happened.clue_pool.length
        ? `<p class="hint">检索线索（不作为结论）：${brief.why_happened.clue_pool.map(esc).join("、")}</p>`
        : ""}
      ${gapsHtml(brief.why_happened.gaps, "证据检索的缺口")}
    </section>

    <section class="block">
      <h3>对公司意味着什么</h3>
      <div class="overall"><b>${esc(brief.what_it_means.overall.display)}</b>
        <p>${esc(brief.what_it_means.overall.reason)}</p></div>
      ${renderAssessments(brief)}
      <p class="disclaimer">${esc(brief.what_it_means.note)}</p>
    </section>

    <section class="block">
      <h3>现在还不知道的</h3>
      <p class="hint">${esc(brief.open_questions.note)}</p>
      <ul class="open-list">${
        brief.open_questions.questions.length
          ? brief.open_questions.questions.map((q) => `<li>${esc(q)}</li>`).join("")
          : `<li class="hint">这次没有遗留未解决问题。</li>`
      }</ul>
      ${gapsHtml(brief.open_questions.gaps, "全部数据缺口")}
    </section>

    <div class="follow">
      <button type="button" data-act="evidence">查看证据链</button>
      <button type="button" data-act="trace">查看研究过程</button>
    </div>`;

  return `<article class="brief ${snapshot ? "snapshot" : ""}">
    ${kicker}
    <section class="hero">
      <div class="hero-top">
        <div>
          <h2>${esc(s.stock.name)}</h2>
          <div class="code">${esc(s.stock.thscode)}</div>
        </div>
        <div class="hero-meta">${esc(s.window.label)}<br>${esc(dates)}</div>
      </div>
      <p class="hero-lead">${esc(w.summary)}</p>
      ${notes.length ? `<p class="remap">${esc(notes.join(" "))}</p>` : ""}
      ${heroMetrics}
    </section>

    <section class="block">
      <h3>价格怎么走</h3>
      <div class="chart-wrap"><svg class="chart" id="chart-${mid}" viewBox="0 0 720 228" preserveAspectRatio="none"></svg></div>
      <div class="chart-legend" id="legend-${mid}"></div>
      <div class="measure-grid">${w.measures.map(measureCard).join("")}</div>
      <div class="pattern"><b>形态：${esc(w.pattern_label)}</b>
        <p>${esc(w.pattern_reason)}</p>
        ${snapshot ? "" : `<p>所属行业：${esc(w.industry.index_name || "未识别")}（${esc(w.industry.method_label)}）
          ${w.industry.is_weak_evidence ? " · 弱证据" : ""}</p>`}
      </div>
    </section>
    ${rest}
  </article>`;
}

function measureCard(m) {
  const na = m.value === null || m.value === undefined;
  const cls = na ? "na" : signClass(m);
  return `<div class="measure">
    <div class="m-label">${esc(m.label)}</div>
    <div class="m-value ${cls}">${esc(m.display)}</div>
    <div class="m-caliber">${esc(m.caliber || "")}</div>
    ${m.note ? `<div class="m-note">${esc(m.note)}</div>` : ""}
  </div>`;
}

function compareHtml(c) {
  const rows = [
    [c.market, false], [c.industry, false], [c.stock, false],
    [c.industry_vs_market, true], [c.stock_vs_industry, true],
  ].filter(([m]) => m);
  const max = Math.max(...rows.map(([m]) => Math.abs(m.value || 0)), 0.01);
  return `<div class="compare">${rows.map(([m, derived]) => {
    const v = m.value;
    const na = v === null || v === undefined;
    const width = na ? 0 : (Math.abs(v) / max) * 50;
    const left = na ? 50 : v >= 0 ? 50 : 50 - width;
    const color = na ? "var(--faint)" : v > 0 ? "var(--up)" : "var(--down)";
    return `<div class="compare-row ${derived ? "derived" : ""}">
      <span class="c-label">${esc(m.label)}</span>
      <span class="c-bar"><span class="c-fill" style="left:${left}%;width:${width}%;background:${color}"></span></span>
      <span class="c-val ${na ? "" : signClass(m)}">${esc(m.display)}</span>
    </div>`;
  }).join("")}</div>`;
}

function renderDriver(d) {
  return `<div class="driver">
    <div class="driver-head">
      <div class="d-title">
        <h4>${esc(d.name)}</h4>
        <p class="d-sum">${esc(d.summary)}</p>
        <p class="d-sum">相关程度：${esc(d.relevance)}</p>
      </div>
      <span class="tag cat">${esc(d.category_label)}</span>
      <span class="tag ${d.status}">${esc(d.status_label)}</span>
    </div>
    <div class="driver-body">
      <div class="checks">${d.checks.map(renderCheck).join("")}</div>
      ${refBlock("支持证据", d.supporting_refs)}
      ${refBlock("反向证据", d.contradicting_refs, "本次检索未找到反向证据，不代表不存在。")}
      ${d.unresolved.length ? `<h3>该因素仍未解决的问题</h3>
        <ul class="unresolved">${d.unresolved.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>` : ""}
    </div>
  </div>`;
}

function renderCheck(c) {
  return `<div class="check">
    <div class="c-head">
      <span class="c-name">${esc(c.label)}</span>
      <span class="c-res ${c.result}">${esc(c.result_label)}</span>
    </div>
    <p>${esc(c.reasoning)}</p>
  </div>`;
}

function refBlock(title, refs, empty = "无") {
  if (!refs || !refs.length) {
    return `<h3>${esc(title)}</h3><p class="hint">${esc(empty)}</p>`;
  }
  return `<h3>${esc(title)}</h3><ul class="ref-list">${refs.map((r) => {
    const ev = state.evidenceById[r.evidence_id] || {};
    return `<li>
      <div class="r-head">
        <span class="eid" data-eid="${esc(r.evidence_id)}">${esc(r.evidence_id)}</span>
        <span class="tier">${esc(tierLabel(ev.source_tier))} · ${esc(ev.source_name || "")}</span>
        <span class="support ${r.support}">对本论点：${esc(supportLabel(r.support))}</span>
        ${ev.published_at ? `<span class="tier">${esc(ev.published_at)}</span>` : ""}
      </div>
      <p class="r-claim">${esc(truncate(ev.claim || "", 160))}</p>
      ${r.rationale ? `<p class="r-why">${esc(r.rationale)}</p>` : ""}
    </li>`;
  }).join("")}</ul>`;
}

function renderAssessments(brief) {
  const assessed = brief.why_happened.drivers.filter((d) => d.assessment);
  if (!assessed.length) {
    return `<p class="hint">没有驱动因素达到进入基本面分析的证据门槛。</p>`;
  }
  return assessed.map((d) => {
    const a = d.assessment;
    return `<div class="assessment">
      <div class="a-head">
        <h4>${esc(d.name)}</h4>
        <span class="headline ${a.display_suppressed ? "suppressed" : ""}">${esc(a.display_headline)}</span>
      </div>
      ${a.display_suppressed ? `<div class="suppression">${esc(a.suppression_reason || "")}</div>` : ""}
      <div class="exposure ${a.exposure_level === "unconfirmed" ? "unconfirmed" : ""}">
        <span class="e-key">公司暴露</span>${esc(a.exposure_label)}　${esc(a.exposure_basis)}
      </div>
      <h3>基本面传导链</h3>
      <ul class="chain">${a.chain.map((s) =>
        `<li class="${s.is_conditional ? "conditional" : ""}">${esc(s.text)}
          ${s.is_conditional ? `<span class="cond-tag">条件性推断</span>` : ""}</li>`
      ).join("")}</ul>
      <div class="factor-cols">
        ${factorCol("抵消因素", a.offsetting_factors)}
        ${factorCol("放大因素", a.amplifying_factors)}
        ${factorCol("关键未知", a.key_unknowns)}
      </div>
      <div class="reasons">
        <div><span class="r-key">为什么是这个方向</span>${esc(a.direction_reason || "—")}</div>
        <div><span class="r-key">为什么是这个期限</span>${esc(a.horizon_reason || "—")}</div>
        <div><span class="r-key">证据强度如何得出</span>${esc(a.strength_reason || "—")}</div>
      </div>
      ${refBlock("公司暴露证据", a.exposure_refs)}
    </div>`;
  }).join("");
}

function factorCol(title, items) {
  const empty = !items || !items.length;
  return `<div class="factor-col ${empty ? "empty" : ""}">
    <h5>${esc(title)}</h5>
    <ul>${empty ? "<li>未发现 / 未能确认</li>" : items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
  </div>`;
}

function gapsHtml(gaps, title) {
  if (!gaps || !gaps.length) return "";
  return `<div class="gaps"><h3>${esc(title)}</h3>` +
    gaps.map((g) => `<div class="gap"><b>${esc(g.field)}</b>：${esc(g.reason)}
      <p>影响：${esc(g.impact)}</p></div>`).join("") + `</div>`;
}

function bindReply(root) {
  root.querySelectorAll(".eid").forEach((el) => {
    el.addEventListener("click", () => openDrawer("evidence", el.dataset.eid));
  });
  root.querySelectorAll("[data-act]").forEach((el) => {
    el.addEventListener("click", () => openDrawer(el.dataset.act));
  });
  root.querySelectorAll("[data-win]").forEach((el) => {
    el.addEventListener("click", () => {
      setWindow(el.dataset.win);
      const lastUser = [...state.messages].reverse().find((m) => m.role === "user");
      if (lastUser) run(lastUser.text);
    });
  });
  state.messages.filter((m) => m.phase === "brief").forEach((m) => {
    const briefs = m.briefs && m.briefs.length ? m.briefs : (m.brief ? [m.brief] : []);
    briefs.forEach((brief, i) => {
      const id = `${m.id}-${i}`;
      if (root.querySelector(`#chart-${id}`)) {
        drawChart(`chart-${id}`, `legend-${id}`, brief.what_happened.series);
      }
    });
  });
}

function drawChart(svgId, legendId, series) {
  const svg = $(svgId);
  const legend = $(legendId);
  if (!svg) return;
  const pts = (series || []).filter((p) => p.close !== null && p.close !== undefined);
  if (!pts.length) {
    svg.innerHTML = `<text x="360" y="114" fill="#9a9aa6" font-size="13" text-anchor="middle">没有可用的价格数据</text>`;
    if (legend) legend.innerHTML = "";
    return;
  }

  const W = 720, H = 228, L = 48, R = 16, T = 16, B = 36;
  const highs = pts.map((p) => p.high ?? p.close);
  const lows = pts.map((p) => p.low ?? p.close);
  const vols = pts.map((p) => p.volume || 0);
  const hi = Math.max(...highs), lo = Math.min(...lows);
  const span = hi - lo || hi * 0.02 || 1;
  const maxVol = Math.max(...vols, 1);
  const plotH = H - T - B;
  const priceH = plotH * 0.72;
  const volH = plotH * 0.22;
  const y = (v) => T + (1 - (v - lo) / span) * priceH;
  const vy = (v) => T + priceH + 10 + (1 - v / maxVol) * volH;
  const step = (W - L - R) / Math.max(pts.length, 1);
  const x = (i) => L + step * (i + 0.5);

  const grid = [hi, (hi + lo) / 2, lo].map((v) =>
    `<line x1="${L}" y1="${y(v)}" x2="${W - R}" y2="${y(v)}" stroke="#ececf1" />
     <text x="${L - 6}" y="${y(v) + 3}" fill="#9a9aa6" font-size="10" text-anchor="end">${v.toFixed(2)}</text>`
  ).join("");

  const volume = pts.map((p, i) => {
    const up = (p.close ?? 0) >= (p.prev_close ?? p.open ?? p.close);
    const color = up ? "#e5534b" : "#16a34a";
    const bw = Math.min(step * 0.55, 18);
    const h = Math.max(T + priceH + 10 + volH - vy(p.volume || 0), 1);
    return `<rect x="${x(i) - bw / 2}" y="${vy(p.volume || 0)}" width="${bw}" height="${h}" fill="${color}" opacity="0.28"/>`;
  }).join("");

  const candles = pts.map((p, i) => {
    const up = (p.close ?? 0) >= (p.prev_close ?? p.open ?? p.close);
    const color = up ? "#e5534b" : "#16a34a";
    const cx = x(i);
    const bw = Math.min(step * 0.45, 16);
    const o = y(p.open ?? p.close), c = y(p.close);
    const top = Math.min(o, c), h = Math.max(Math.abs(c - o), 1.6);
    return `<line x1="${cx}" y1="${y(p.high ?? p.close)}" x2="${cx}" y2="${y(p.low ?? p.close)}" stroke="${color}" stroke-width="1.4"/>
            <rect x="${cx - bw / 2}" y="${top}" width="${bw}" height="${h}" fill="${color}" rx="1"/>`;
  }).join("");

  const line = pts.length > 1
    ? `<polyline fill="none" stroke="#10a37f" stroke-width="1.6" opacity="0.85"
        points="${pts.map((p, i) => `${x(i)},${y(p.close)}`).join(" ")}"/>`
    : "";

  const labels = pts.map((p, i) =>
    `<text x="${x(i)}" y="${H - 10}" fill="#9a9aa6" font-size="10" text-anchor="middle">${p.date.slice(5)}</text>`
  ).join("");

  svg.innerHTML = grid + volume + line + candles + labels;
  if (legend) {
    legend.innerHTML =
      `<span><i style="background:#e5534b"></i>上涨</span>
       <span><i style="background:#16a34a"></i>下跌</span>
       <span><i style="background:#10a37f"></i>收盘连线</span>
       <span><i style="background:#d4d4d8"></i>成交量</span>`;
  }
}

function openDrawer(kind, highlightId) {
  if (!state.brief) return;
  const body = $("drawerBody");
  if (kind === "evidence") {
    $("drawerTitle").textContent = `证据链（${state.brief.evidence.length} 条）`;
    body.innerHTML = state.brief.evidence.map((e) => `
      <div class="ev ${e.id === highlightId ? "highlight" : ""}" id="ev-${esc(e.id)}">
        <div class="ev-head">
          <span class="eid">${esc(e.id)}</span>
          <span class="tier">${esc(tierLabel(e.source_tier))}</span>
          <span class="tier">${esc(e.source_name)}</span>
          ${e.published_at ? `<span class="tier">${esc(e.published_at)}</span>` : ""}
        </div>
        <p class="ev-claim">${esc(e.claim)}</p>
        <p class="ev-meta">类型 ${esc(e.kind)} · 数据源 ${esc(e.provider)}
          · 事件聚类 ${esc(e.cluster_id || "-")} · 独立信源标识 ${esc(e.origin_key || "-")}
          ${e.caliber ? ` · 口径 ${esc(e.caliber)}` : ""}</p>
        ${e.source_url ? `<p class="ev-meta"><a href="${esc(e.source_url)}" target="_blank" rel="noopener">查看原文</a></p>` : ""}
        ${e.raw_ref ? `<pre>${esc(JSON.stringify(e.raw_ref, null, 2))}</pre>` : ""}
      </div>`).join("");
  } else {
    const t = state.brief.trace;
    $("drawerTitle").textContent = "研究过程";
    body.innerHTML =
      `<h3>数据源</h3>
       <p class="hint">行情 ${esc(t.providers.market)} · 资讯 ${esc(t.providers.evidence)}
          · 推理 ${esc(t.providers.llm)}</p>
       <h3>工具调用（${t.tool_calls.length} 次）</h3>` +
      t.tool_calls.map((c) => `<div class="trace-call">
        <div class="t-head">
          <span>#${c.seq}</span>
          <span class="t-tool">${esc(c.tool)}</span>
          <span class="t-status ${c.status}">${esc(c.status)}</span>
          <span class="tier">${esc(c.provider)}${c.latency_ms != null ? ` · ${c.latency_ms}ms` : ""}</span>
        </div>
        <div class="t-params">${esc(JSON.stringify(c.params))}</div>
        ${c.note ? `<div class="t-params">${esc(c.note)}</div>` : ""}
      </div>`).join("") +
      (t.llm_calls.length
        ? `<h3>模型调用（${t.llm_calls.length} 次）</h3>` +
          t.llm_calls.map((c) => `<div class="trace-call">
            <div class="t-head">
              <span class="t-tool">${esc(c.purpose)}</span>
              <span class="t-status ${c.ok ? "ok" : "failed"}">${c.ok ? "ok" : "failed"}</span>
              <span class="tier">${esc(c.model)} · ${c.latency_ms}ms</span>
            </div>
            ${c.note ? `<div class="t-params">${esc(c.note)}</div>` : ""}
          </div>`).join("")
        : `<h3>模型调用</h3><p class="hint">本次运行未调用大模型。</p>`);
  }
  $("drawer").classList.remove("hidden");
  $("drawerBackdrop").classList.remove("hidden");
  if (highlightId) {
    const target = $(`ev-${highlightId}`);
    if (target) target.scrollIntoView({ block: "center" });
  }
}

function closeDrawer() {
  $("drawer").classList.add("hidden");
  $("drawerBackdrop").classList.add("hidden");
}

const TIER_LABELS = {
  t1_authoritative: "一级·原始权威",
  t2_professional: "二级·专业财经",
  t3_general: "三级·一般可信",
  t4_unverified: "四级·来源无法确认",
  market_data: "结构化行情数据",
};
const SUPPORT_LABELS = {
  supports: "支持", weakly_supports: "弱支持", neutral: "中性",
  weakens: "削弱", refutes: "证伪",
};
const DIRECTIONAL_UNITS = new Set(["pct", "pct_points"]);

const tierLabel = (t) => TIER_LABELS[t] || t || "";
const supportLabel = (s) => SUPPORT_LABELS[s] || s || "";
const pctStr = (v) => (v * 100).toFixed(0) + "%";
const truncate = (s, n) => (s.length > n ? s.slice(0, n) + "…" : s);

function signClass(m) {
  if (!DIRECTIONAL_UNITS.has(m.unit)) return "";
  return m.value > 0 ? "pos" : m.value < 0 ? "neg" : "";
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init();
