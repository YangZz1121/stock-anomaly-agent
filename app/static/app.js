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
  maxTurns: 20,
  pendingAsk: null,
  pendingAnswers: {},
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
    state.maxTurns = cfg.conversation_max_turns || 20;
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
  state.pendingAsk = null;
  state.pendingAnswers = {};
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
  const typed = (presetQuery ?? $("queryInput").value).trim();
  if (!typed) return;

  if (state.source) state.source.close();

  let researchQuery = typed;
  if (state.pendingAsk) {
    state.pendingAnswers[state.pendingAsk.field] = typed;
    researchQuery = state.pendingAsk.query || typed;
    if (state.pendingAsk.field === "window") setWindow(typed);
    state.pendingAsk = null;
  }

  state.messages.push({ id: state.nextId++, role: "user", text: typed });
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
  const params = new URLSearchParams({ query: researchQuery });
  if (state.window) params.set("window", state.window);
  const ctx = conversationContext();
  if (ctx.stocks.length) params.set("context_stocks", ctx.stocks.join(","));
  if (ctx.queries.length) params.set("context_queries", ctx.queries.join("\n"));
  if (ctx.window) params.set("context_window", ctx.window);
  if (Object.keys(state.pendingAnswers).length) {
    params.set("context_answers", JSON.stringify(state.pendingAnswers));
  }

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
  source.addEventListener("ask", (e) => {
    const data = JSON.parse(e.data);
    const msg = state.messages.find((m) => m.id === aid);
    if (!msg) return;
    msg.phase = "ask";
    msg.ask = data;
    state.pendingAsk = {
      field: data.field,
      query: researchQuery,
      window: state.window,
    };
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
  const maxTurns = state.maxTurns || 20;
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
    queries: priors.slice(0, -1).slice(-maxTurns),
    window: latestWindow,
    answers: { ...state.pendingAnswers },
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
        <p>输入公司名称或代码。报告按行情事实、驱动因素、基本面含义展开，结论可回溯到证据，不做买卖建议。</p>
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
  if (m.phase === "ask") return renderAsk(m.ask);
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

function renderAsk(ask) {
  const items = (ask && ask.choices) || [];
  const buttons = items.map((item) => {
    const value = typeof item === "string" ? item : (item.value || "");
    const label = typeof item === "string" ? item : (item.label || value);
    return `<button type="button" data-ask="${esc(value)}" data-field="${esc(ask.field || "")}">${esc(label)}</button>`;
  }).join("");
  return `<div class="plain-card">
    <p>${esc((ask && ask.question) || "还需要您补充一点信息。")}</p>
    ${ask && ask.hint ? `<p class="hint">${esc(ask.hint)}</p>` : ""}
    ${buttons ? `<div class="follow" style="margin-top:12px">${buttons}</div>` : ""}
    <p class="hint">也可以直接在输入框里补充。</p>
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

function sectionHead(num, title, desc) {
  return `<header class="sec-head">
    <span class="sec-num">${esc(num)}</span>
    <div>
      <h3>${esc(title)}</h3>
      ${desc ? `<p class="sec-desc">${esc(desc)}</p>` : ""}
    </div>
  </header>`;
}

function moduleBlock(insight, body, extras) {
  const extraItems = uniqueTexts((extras || []).filter((x) => !isVague(x)));
  return `<div class="mod">
    ${insight ? `<p class="mod-insight">${esc(insight)}</p>` : ""}
    ${body ? `<p class="mod-body">${esc(body)}</p>` : ""}
    ${extraItems.length ? `<ul class="mod-extra">${
      extraItems.map((item) => `<li>${item}</li>`).join("")
    }</ul>` : ""}
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
  if (s.window.is_intraday) notes.push("当前为盘中数据，部分指标暂不可用。");
  const kicker = split
    ? `<div class="company-kicker">${esc(s.stock.name)} · ${esc(s.stock.thscode)}</div>`
    : "";
  const priceDrivers = materialPriceDrivers(brief);
  const companyDrivers = materialCompanyDrivers(brief);

  const rest = snapshot
    ? `<p class="disclaimer">以上为行情事实快照。如需完整异动分析——含驱动因素验证与基本面含义——请直接说明。</p>`
    : renderReportSections(brief, w, priceDrivers, companyDrivers);

  const { insight, body } = splitSummary(w.summary, brief);
  return `<article class="brief ${snapshot ? "snapshot" : ""}">
    ${kicker}
    <section class="hero">
      <div class="doc-type">${snapshot ? "行情快照" : "个股异动分析"}</div>
      <div class="hero-top">
        <div>
          <h2>${esc(s.stock.name)}</h2>
          <div class="code">${esc(s.stock.thscode)}</div>
        </div>
        <div class="hero-meta">
          <span>${esc(s.window.label)}</span>
          <span>${esc(dates)}</span>
        </div>
      </div>
      ${moduleBlock(insight, body, notes)}
    </section>

    <section class="block">
      ${sectionHead("01", "行情事实")}
      ${renderPriceModule(w, mid, snapshot)}
    </section>
    ${rest}
  </article>`;
}

function renderReportSections(brief, w, priceDrivers, companyDrivers) {
  const questions = watchableQuestions(brief, companyDrivers);
  const legal = (brief.disclaimers || []).filter((d) =>
    /不构成投资建议|不对未来股价/.test(d)
  ).slice(0, 2);
  return `
    <section class="block">
      ${sectionHead("02", "相对表现")}
      ${renderRelativeModule(w, brief.why_happened)}
    </section>
    <section class="block">
      ${sectionHead("03", "驱动因素")}
      ${renderDriversModule(brief, priceDrivers)}
    </section>
    <section class="block">
      ${sectionHead("04", "基本面含义")}
      ${renderMeansModule(brief, companyDrivers)}
    </section>
    <section class="block">
      ${sectionHead("05", "待观察事项")}
      ${renderWatchModule(questions, brief.open_questions.gaps)}
    </section>
    <div class="follow">
      <button type="button" data-act="evidence">查看证据链</button>
      <button type="button" data-act="trace">查看研究过程</button>
    </div>
    ${legal.length ? `<p class="disclaimer">${esc(legal.join(" "))}</p>` : ""}`;
}

function splitSummary(summary, brief) {
  const parts = String(summary || "").split("。").map((s) => s.trim()).filter(Boolean);
  const insight = parts[0] ? `${parts[0]}。` : "";
  const rest = parts.slice(1).join("。");
  const bodyBits = [];
  if (rest) bodyBits.push(rest.endsWith("。") ? rest : `${rest}。`);
  const overall = brief && brief.what_it_means && brief.what_it_means.overall;
  if (brief && brief.kind !== "snapshot" && overall && overall.can_summarize
      && overall.display && !/暂无法|快照/.test(overall.display)) {
    bodyBits.push(`基本面含义：${overall.display}。`);
  }
  return { insight, body: uniqueSentence(bodyBits.join("")) };
}

function renderPriceModule(w, mid, snapshot) {
  const usable = (w.measures || []).filter((m) => m.value !== null && m.value !== undefined);
  const lead = usable[0];
  const insight = lead
    ? `${w.pattern_label}，${lead.label} ${lead.display}。`
    : `${w.pattern_label}。`;
  const body = cleanText(w.pattern_reason);
  const extras = [];
  usable.slice(1).forEach((m) => {
    if (body.includes(m.label) || insight.includes(m.display)) return;
    if (m.note && !isVague(m.note) && !sameMeaning(m.note, body)) {
      extras.push(`${esc(m.label)} ${measureValue(m)}　${esc(m.note)}`);
    } else {
      extras.push(`${esc(m.label)} ${measureValue(m)}`);
    }
  });
  if (!snapshot && w.industry && w.industry.index_name) {
    extras.push(`所属行业 ${esc(w.industry.index_name)}`);
  }
  return `${moduleBlock(insight, body, extras)}
    <div class="chart-wrap"><svg class="chart" id="chart-${mid}" viewBox="0 0 720 228" preserveAspectRatio="none"></svg></div>
    <div class="chart-legend" id="legend-${mid}"></div>`;
}

function renderRelativeModule(w, why) {
  const c = w.comparison || {};
  if (!hasRelativeData(c)) {
    return moduleBlock("本维度缺少可对照的行业或市场数据。", "", materialGaps(w.gaps));
  }
  const insight = relativeInsight(c);
  const raw = stripLeadingFacts(why && why.priority_reason);
  const body = cleanText(raw) || "三层对照只用于判断应从市场、行业还是公司层面解释本次异动，不做贡献拆分。";
  return `${moduleBlock(insight, body, materialGaps(w.gaps))}
    ${compareHtml(c)}`;
}

function renderDriversModule(brief, drivers) {
  if (!drivers.length) {
    return moduleBlock("未发现与本次异动直接相关、且能指向可验证路径的驱动因素。", "", []);
  }
  const names = drivers.map((d) => d.name).join("、");
  const insight = drivers.length === 1
    ? `${drivers[0].name}是与本次异动相关的主要因素。`
    : `与本次异动相关的因素有 ${drivers.length} 项：${names}。`;
  const cards = drivers.map(renderDriverCard).join("");
  return `${moduleBlock(insight, "", [])}${cards}`;
}

function renderDriverCard(d) {
  const role = d.status === "supported" ? "主要解释" : "部分解释";
  const insight = `${d.name}：${role}。`;
  const body = uniqueSentence([d.summary, usefulRelevance(d.relevance)].filter(Boolean).join(""));
  const extras = [];
  compactRefs(d.supporting_refs, false, body).forEach((item) => extras.push(item.html));
  compactRefs(d.contradicting_refs, true, body).forEach((item) => extras.push(`反向　${item.html}`));
  return `<div class="driver">${moduleBlock(insight, body, extras)}</div>`;
}

function renderMeansModule(brief, drivers) {
  const overall = brief.what_it_means.overall || {};
  if (!drivers.length) {
    return moduleBlock("现有因素尚未确认对公司经营的可验证影响。", "", []);
  }
  const insight = cleanText(overall.display) || "相关因素对公司经营的影响需分项阅读。";
  const body = cleanOverallReason(overall.reason);
  const cards = drivers.map((d) => renderAssessmentCard(d, drivers)).join("");
  return `${moduleBlock(insight, body, [])}${cards}`;
}

function renderAssessmentCard(d, siblings) {
  const a = d.assessment;
  const insight = `${d.name}：${a.display_direction}，${a.display_horizon}。`;
  const body = uniqueSentence([
    chainParagraph(a, d.name),
    cleanText(a.exposure_basis),
  ].filter(Boolean).join(""));
  const extras = [];
  ownFactors(d, siblings || [d], "offsetting_factors").forEach((x) => extras.push(`抵消　${esc(x)}`));
  ownFactors(d, siblings || [d], "amplifying_factors").forEach((x) => extras.push(`放大　${esc(x)}`));
  if (a.display_strength && !/不足/.test(a.display_strength)) {
    extras.push(`证据强度　${esc(a.display_strength)}`);
  }
  compactRefs(a.exposure_refs, false, body).forEach((item) => extras.push(item.html));
  return `<div class="assessment">${moduleBlock(insight, body, extras)}</div>`;
}

function renderWatchModule(questions, gaps) {
  const items = uniqueTexts(questions);
  const extras = materialGaps(gaps);
  if (!items.length && !extras.length) {
    return moduleBlock("没有足以改变当前判断的待观察事项。", "", []);
  }
  const insight = items[0] || extras[0];
  const body = items.length > 1
    ? `其余 ${items.length - 1} 项会改变对方向或期限的判断，列于下方。`
    : "";
  return moduleBlock(insight, body, items.slice(1).map(esc).concat(extras));
}

function compareHtml(c) {
  const rows = [
    [c.market, false], [c.industry, false], [c.stock, false],
    [c.industry_vs_market, true], [c.stock_vs_industry, true],
  ].filter(([m]) => m && m.value !== null && m.value !== undefined);
  if (!rows.length) return "";
  const max = Math.max(...rows.map(([m]) => Math.abs(m.value || 0)), 0.01);
  return `<div class="compare">${rows.map(([m, derived]) => {
    const v = m.value;
    const width = (Math.abs(v) / max) * 50;
    const left = v >= 0 ? 50 : 50 - width;
    const color = v > 0 ? "var(--up)" : "var(--down)";
    return `<div class="compare-row ${derived ? "derived" : ""}">
      <span class="c-label">${esc(m.label)}</span>
      <span class="c-bar"><span class="c-fill" style="left:${left}%;width:${width}%;background:${color}"></span></span>
      <span class="c-val ${signClass(m)}">${esc(m.display)}</span>
    </div>`;
  }).join("")}</div>`;
}

function materialPriceDrivers(brief) {
  return (brief.why_happened.drivers || []).filter(isPriceRelevant);
}

function materialCompanyDrivers(brief) {
  return materialPriceDrivers(brief).filter(pointsToCompany);
}

function isPriceRelevant(d) {
  if (!d || d.status === "insufficient") return false;
  const by = Object.fromEntries((d.checks || []).map((c) => [c.key, c]));
  if (by.timing && by.timing.result === "fail") return false;
  if (by.mechanism && by.mechanism.result === "fail") return false;
  const refs = d.supporting_refs || [];
  if (!refs.length) return false;
  const usable = refs.filter((r) => {
    const ev = state.evidenceById[r.evidence_id] || {};
    return ev.source_tier !== "t4_unverified" && r.support !== "neutral";
  });
  return usable.length > 0;
}

function pointsToCompany(d) {
  const a = d.assessment;
  if (!a || a.display_suppressed) return false;
  if (a.exposure_level === "unconfirmed") return false;
  if (/暂无法可靠判断/.test(a.display_direction || "")) return false;
  return true;
}

function watchableQuestions(brief, drivers) {
  const allow = new Set(drivers.map((d) => d.name));
  const raw = (brief.open_questions && brief.open_questions.questions) || [];
  return raw.map((q) => {
    const m = String(q).match(/^【([^】]+)】(.*)$/);
    const text = m ? m[2] : q;
    if (/市场层面的普遍因素|无法据此判断|无法判定/.test(text || "")) return "";
    if (!m) return cleanText(q);
    if (allow.size && !allow.has(m[1])) {
      return /未被解释/.test(m[2]) ? cleanText(m[2]) : "";
    }
    return cleanText(`${m[1]}：${m[2]}`);
  }).filter((q) => q && !isVague(q));
}

function ownFactors(d, siblings, key) {
  const mine = ((d.assessment && d.assessment[key]) || []).filter((x) => !isVague(x));
  const peers = siblings || [d];
  return mine.filter((x) => {
    if (d.name && x.includes(d.name.slice(0, 10))) return false;
    if (peers.length >= 2 && peers.every((s) => ((s.assessment && s.assessment[key]) || []).includes(x))) {
      return false;
    }
    return true;
  });
}

function compactRefs(refs, counter = false, overlapText = "") {
  const items = [];
  const seen = [];
  for (const r of refs || []) {
    if (counter && !(r.support === "weakens" || r.support === "refutes")) continue;
    if (!counter && (r.support === "neutral" || r.support === "weakens" || r.support === "refutes")) continue;
    const ev = state.evidenceById[r.evidence_id] || {};
    if (ev.source_tier === "t4_unverified") continue;
    const claim = ev.claim || "";
    const key = normText(claim || r.evidence_id);
    if (!key || seen.some((s) => s.includes(key) || key.includes(s))) continue;
    seen.push(key);
    const label = overlapText && sameMeaning(claim, overlapText)
      ? ""
      : ` ${esc(truncate(claim, 72))}`;
    items.push({
      html: `<span class="eid" data-eid="${esc(r.evidence_id)}">${esc(r.evidence_id)}</span>${label}`,
    });
    if (items.length >= 2) break;
  }
  return items;
}

function chainParagraph(a, driverName) {
  const steps = (a.chain || []).map((s) => s.text || "").filter((t) => {
    if (isVague(t)) return false;
    if (/^事件事实/.test(t) && t.includes(driverName)) return false;
    if (/公司暴露：/.test(t) && /确认|无法确认/.test(t)) return false;
    if (/传导链在此中断/.test(t)) return false;
    return true;
  });
  if (!steps.length) return "";
  return uniqueSentence(steps.map((t) => t.replace(/[。；]$/, "")).join("；") + "。");
}

function relativeInsight(c) {
  const vs = c.stock_vs_industry;
  if (vs && vs.value !== null && vs.value !== undefined) {
    const word = vs.value < -0.002 ? "弱于" : vs.value > 0.002 ? "强于" : "贴近";
    return `个股相对行业${word}行业（${vs.display}）。`;
  }
  const bits = [];
  if (c.stock && c.stock.display) bits.push(`个股 ${c.stock.display}`);
  if (c.industry && c.industry.display) bits.push(`行业 ${c.industry.display}`);
  if (c.market && c.market.display) bits.push(`市场 ${c.market.display}`);
  return bits.length ? `${bits.join("，")}。` : "相对表现可核对，但缺少背离幅度。";
}

function hasRelativeData(c) {
  return [c.stock, c.industry, c.market].some((m) => m && m.value !== null && m.value !== undefined);
}

function usefulRelevance(rel) {
  if (!rel) return "";
  const cut = String(rel)
    .replace(/发生在核心证据窗口内[；。]?/g, "")
    .replace(/\d+ 个独立信源确认[；。]?/g, "")
    .replace(/共 \d+ 篇报道[；。]?/g, "")
    .replace(/来源无法确认，仅作检索线索[；。]?/g, "")
    .trim();
  return isVague(cut) ? "" : cut;
}

function cleanOverallReason(text) {
  const t = cleanText(text);
  if (!t) return "";
  if (/请分别查看每个驱动因素/.test(t)) {
    return "不同因素的经营影响方向不一致，故不作合并，分项评估。";
  }
  return t;
}

function materialGaps(gaps) {
  return (gaps || []).filter((g) => g && g.reason && !/检索线索/.test(g.reason))
    .map((g) => esc(`${g.reason}${g.impact ? `，${g.impact}` : ""}`));
}

function measureValue(m) {
  return `<span class="${signClass(m)}">${esc(m.display)}</span>`;
}

function stripLeadingFacts(text) {
  return String(text || "").replace(/^市场[^。]*。/, "").trim();
}

function cleanText(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function isVague(text) {
  const t = String(text || "").trim();
  if (!t || t === "—" || t === "无") return true;
  return /无法判定|暂无法可靠判断|未发现\s*\/\s*未能确认|不代表不存在|仅作检索|来源无法确认/.test(t);
}

function normText(text) {
  return String(text || "").replace(/\s+/g, "").replace(/[。；;,.、：:]/g, "");
}

function sameMeaning(a, b) {
  const x = normText(a), y = normText(b);
  if (!x || !y) return false;
  return x === y || x.includes(y) || y.includes(x);
}

function uniqueTexts(items) {
  const out = [];
  (items || []).forEach((item) => {
    const raw = String(item || "").trim();
    if (!raw) return;
    const n = normText(raw.replace(/<[^>]+>/g, ""));
    if (n.length < 4) {
      out.push(raw);
      return;
    }
    const idx = out.findIndex((x) => {
      const m = normText(String(x).replace(/<[^>]+>/g, ""));
      return m.includes(n) || n.includes(m);
    });
    if (idx === -1) out.push(raw);
    else if (normText(String(out[idx]).replace(/<[^>]+>/g, "")).length < n.length) out[idx] = raw;
  });
  return out;
}

function uniqueSentence(text) {
  const parts = String(text || "").split(/[。]/).map((s) => s.trim()).filter(Boolean);
  return uniqueTexts(parts).map((s) => (/[。！？]$/.test(s) ? s : `${s}。`)).join("");
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
  root.querySelectorAll("[data-ask]").forEach((el) => {
    el.addEventListener("click", () => {
      const field = el.dataset.field || (state.pendingAsk && state.pendingAsk.field);
      if (field) state.pendingAnswers[field] = el.dataset.ask;
      if (field === "window") setWindow(el.dataset.ask);
      run(el.dataset.ask);
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
