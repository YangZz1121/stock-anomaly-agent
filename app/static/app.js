/* 个股异动研究 Agent —— 前端
 *
 * 只做三件事：发起 SSE 请求、渲染服务端给的结构化 Brief、提供证据反查。
 * 刻意不在前端做任何数值计算或结论加工——所有口径和判断都必须来自后端，
 * 否则"结论能回到原始字段"这件事就不成立了。
 */

const $ = (id) => document.getElementById(id);

const state = {
  window: null,
  faults: new Set(),
  brief: null,
  evidenceById: {},
  timer: null,
  source: null,
};

const EXAMPLES = [
  { q: "宁德时代最近5个交易日怎么一直跌？", w: "d5" },
  { q: "贵州茅台今天为什么跌了？", w: "today" },
  { q: "伊利股份今天为什么大跌？", w: "today" },
  { q: "宁德时代", w: null },
];

const FAULTS = [
  { key: "daily_bars", label: "个股行情接口失败" },
  { key: "index_daily_bars", label: "指数行情接口失败" },
  { key: "industry_indexes", label: "行业指数清单失败" },
  { key: "search_events", label: "资讯检索接口失败" },
  { key: "anomaly_reasons", label: "异动线索接口失败" },
  { key: "trading_days", label: "交易日历接口失败" },
];

// ---------------------------------------------------------------- 初始化

async function init() {
  renderChips($("exampleChips"), EXAMPLES.map((e) => e.q), (label, i) => {
    $("queryInput").value = EXAMPLES[i].q;
    setWindow(EXAMPLES[i].w);
    run();
  });

  renderChips($("faultChips"), FAULTS.map((f) => f.label), (label, i) => {
    const key = FAULTS[i].key;
    state.faults.has(key) ? state.faults.delete(key) : state.faults.add(key);
    syncFaultChips();
  });

  try {
    const cfg = await (await fetch("/api/config")).json();
    renderProviders(cfg);
    renderWindowChips(cfg.windows);
  } catch (e) {
    showError({ code: "config_failed", message: "无法加载运行配置", hint: String(e) });
  }

  $("runBtn").addEventListener("click", run);
  $("queryInput").addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });

  // 支持 /?q=宁德时代&w=d5 形式的直达链接，便于分享和演示
  const params = new URLSearchParams(location.search);
  const q = params.get("q");
  if (q) {
    $("queryInput").value = q;
    setWindow(params.get("w"));
    run();
  }
  $("evidenceBtn").addEventListener("click", () => openDrawer("evidence"));
  $("traceBtn").addEventListener("click", () => openDrawer("trace"));
  $("drawerClose").addEventListener("click", closeDrawer);
  $("drawerBackdrop").addEventListener("click", closeDrawer);
}

function renderProviders(cfg) {
  $("providerChips").innerHTML = [
    ["行情", cfg.providers.market],
    ["资讯", cfg.providers.evidence],
    ["推理", cfg.providers.llm],
  ].map(([k, v]) => `<span class="provider-chip">${k} <b>${esc(v)}</b></span>`).join("");

  if (cfg.degraded && cfg.notices.length) {
    $("degradedBanner").classList.remove("hidden");
    $("degradedBanner").innerHTML =
      `<div class="wrap"><b>当前运行在降级模式</b><ul>` +
      cfg.notices.map((n) => `<li>${esc(n)}</li>`).join("") +
      `</ul></div>`;
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
  $("windowHint").textContent = value
    ? "已指定研究窗口"
    : "未选择时，将从你的问题中识别";
}

function syncFaultChips() {
  [...$("faultChips").children].forEach((c, i) =>
    c.classList.toggle("active", state.faults.has(FAULTS[i].key))
  );
}

function renderChips(box, labels, onClick) {
  box.innerHTML = "";
  labels.forEach((label, i) => {
    const el = document.createElement("span");
    el.className = "chip";
    el.textContent = label;
    el.addEventListener("click", () => onClick(label, i));
    box.appendChild(el);
  });
}

// ---------------------------------------------------------------- 运行

function run() {
  const query = $("queryInput").value.trim();
  if (!query) return;

  if (state.source) state.source.close();
  $("brief").classList.add("hidden");
  $("errorCard").classList.add("hidden");
  $("progressCard").classList.remove("hidden");
  $("logList").innerHTML = "";
  $("runBtn").disabled = true;

  const params = new URLSearchParams({ query });
  if (state.window) params.set("window", state.window);
  if (state.faults.size) params.set("faults", [...state.faults].join(","));

  const started = performance.now();
  state.timer = setInterval(() => {
    $("elapsed").textContent = ((performance.now() - started) / 1000).toFixed(1) + "s";
  }, 100);

  const source = new EventSource(`/api/research/stream?${params}`);
  state.source = source;

  source.addEventListener("start", (e) => initSteps(JSON.parse(e.data).steps));
  source.addEventListener("step", (e) => updateStep(JSON.parse(e.data)));
  source.addEventListener("log", (e) => appendLog(JSON.parse(e.data).message));
  source.addEventListener("brief", (e) => renderBrief(JSON.parse(e.data)));
  source.addEventListener("error", (e) => {
    if (e.data) showError(JSON.parse(e.data));
  });
  source.addEventListener("done", () => finish(source));
  source.onerror = () => finish(source);
}

function finish(source) {
  source.close();
  clearInterval(state.timer);
  $("runBtn").disabled = false;
}

function initSteps(steps) {
  $("stepList").innerHTML = steps
    .map((s) => `<li id="step-${s.key}"><span class="mark">○</span>
      <span class="label">${esc(s.label)}</span>
      <span class="detail"></span></li>`)
    .join("");
}

function updateStep({ key, state: st, detail }) {
  const el = $(`step-${key}`);
  if (!el) return;
  el.className = st;
  el.querySelector(".mark").textContent =
    st === "done" ? "✓" : st === "running" ? "→" : st === "failed" ? "×" : "○";
  if (detail) el.querySelector(".detail").textContent = detail;
}

function appendLog(message) {
  const el = document.createElement("div");
  el.textContent = message;
  $("logList").appendChild(el);
}

function showError(err) {
  $("errorCard").classList.remove("hidden");
  $("errorTitle").textContent = err.message || "研究未能完成";
  $("errorHint").textContent = err.hint || "";

  const actions = $("errorActions");
  actions.innerHTML = "";
  if (err.code === "need_window") {
    // 只输入了股票名：直接把窗口选项摆出来，点一下就重跑
    [["today", "今日"], ["d3", "最近 3 个交易日"], ["d5", "最近 5 个交易日"]]
      .forEach(([value, label]) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = label;
        chip.addEventListener("click", () => {
          setWindow(value);
          $("errorCard").classList.add("hidden");
          run();
        });
        actions.appendChild(chip);
      });
  }
}

// ---------------------------------------------------------------- 渲染 Brief

function renderBrief(brief) {
  state.brief = brief;
  state.evidenceById = {};
  brief.evidence.forEach((e) => (state.evidenceById[e.id] = e));

  renderSubject(brief);
  renderWhatHappened(brief.what_happened);
  renderWhyHappened(brief.why_happened);
  renderWhatItMeans(brief);
  renderOpenQuestions(brief.open_questions);

  $("disclaimerList").innerHTML = brief.disclaimers
    .map((d) => `<li>${esc(d)}</li>`).join("");
  $("brief").classList.remove("hidden");
}

function renderSubject(brief) {
  const s = brief.subject;
  $("stockName").textContent = s.stock.name;
  $("stockCode").textContent = s.stock.thscode;
  $("userQuestion").textContent = `研究问题：${s.user_question}`;
  $("windowLabel").textContent = s.window.label;
  $("windowDates").textContent =
    s.window.actual_start === s.window.actual_end
      ? s.window.actual_end
      : `${s.window.actual_start} ~ ${s.window.actual_end}`;

  const note = $("remapNote");
  const notes = [];
  if (s.window.remap_note) notes.push(s.window.remap_note);
  if (s.window.is_intraday) notes.push("当前为盘中数据，部分指标不可用。");
  note.classList.toggle("hidden", notes.length === 0);
  note.textContent = notes.join(" ");

  const m = brief.metrics;
  $("metricsStrip").innerHTML = [
    ["可验证洞察时间", (m.time_to_verifiable_insight_ms / 1000).toFixed(1) + "s",
     "从提问到生成可追溯 Brief"],
    ["证据覆盖率", pctStr(m.evidence_coverage), "有可追溯证据支持的核心结论占比"],
    ["无证据推断率", pctStr(m.unsupported_inference_rate), "越低越好"],
    ["证据条目", String(m.evidence_count), `${m.independent_source_count} 个独立信源`],
    ["工具调用", `${m.tool_calls}`, `${m.failed_tool_calls} 次失败`],
  ].map(([k, v, sub]) =>
    `<div class="metric"><span>${esc(k)}</span><b>${esc(v)}</b><span>${esc(sub)}</span></div>`
  ).join("");
}

function renderWhatHappened(w) {
  $("whatSummary").textContent = w.summary;

  $("measureGrid").innerHTML = w.measures.map((m) => {
    const na = m.value === null || m.value === undefined;
    const cls = na ? "na" : signClass(m);
    return `<div class="measure">
      <div class="m-label">${esc(m.label)}</div>
      <div class="m-value ${cls}">${esc(m.display)}</div>
      <div class="m-caliber">${esc(m.caliber || "")}</div>
      ${m.note ? `<div class="m-note">${esc(m.note)}</div>` : ""}
    </div>`;
  }).join("");

  $("patternBox").innerHTML = `<b>价格形态：${esc(w.pattern_label)}</b>
    <p>${esc(w.pattern_reason)}</p>
    <p>所属行业：${esc(w.industry.index_name || "未识别")}
       （${esc(w.industry.method_label)}）${w.industry.is_weak_evidence ? " · 弱证据" : ""}</p>`;

  renderCompare(w.comparison);
  $("compareDisclaimer").textContent = w.comparison.disclaimer;
  renderChart(w.series);
  renderGaps($("whatGaps"), w.gaps, "第一阶段的数据缺口");
}

function renderCompare(c) {
  const rows = [
    [c.market, false], [c.industry, false], [c.stock, false],
    [c.industry_vs_market, true], [c.stock_vs_industry, true],
  ].filter(([m]) => m);

  const values = rows.map(([m]) => Math.abs(m.value || 0));
  const max = Math.max(...values, 0.01);

  $("compareBox").innerHTML = rows.map(([m, derived]) => {
    const v = m.value;
    const na = v === null || v === undefined;
    const width = na ? 0 : (Math.abs(v) / max) * 50;
    const left = na ? 50 : v >= 0 ? 50 : 50 - width;
    const color = na ? "var(--faint)" : v > 0 ? "var(--up)" : "var(--down)";
    return `<div class="compare-row ${derived ? "derived" : ""}">
      <span class="c-label">${esc(m.label)}</span>
      <span class="c-bar"><span class="c-fill"
        style="left:${left}%;width:${width}%;background:${color}"></span></span>
      <span class="c-val ${na ? "" : signClass(m)}">${esc(m.display)}</span>
    </div>`;
  }).join("");
}

function renderChart(series) {
  const svg = $("chart");
  const pts = series.filter((p) => p.close !== null && p.close !== undefined);
  if (pts.length === 0) {
    svg.innerHTML = `<text x="360" y="110" fill="#6b7483" font-size="13"
      text-anchor="middle">没有可用的价格数据</text>`;
    $("chartLegend").innerHTML = "";
    return;
  }

  const W = 720, H = 220, PAD = 30;
  const highs = pts.map((p) => p.high ?? p.close);
  const lows = pts.map((p) => p.low ?? p.close);
  const hi = Math.max(...highs), lo = Math.min(...lows);
  const span = hi - lo || hi * 0.02 || 1;
  const y = (v) => PAD + (1 - (v - lo) / span) * (H - PAD * 2);
  const step = (W - PAD * 2) / Math.max(pts.length, 1);
  const x = (i) => PAD + step * (i + 0.5);

  // 单日窗口画不出折线，改用 K 线柱更诚实
  const body = pts.map((p, i) => {
    const up = (p.close ?? 0) >= (p.prev_close ?? p.open ?? p.close);
    const color = up ? "var(--up)" : "var(--down)";
    const cx = x(i);
    const bw = Math.min(step * 0.45, 22);
    const o = y(p.open ?? p.close), c = y(p.close);
    const top = Math.min(o, c), h = Math.max(Math.abs(c - o), 1.5);
    return `<line x1="${cx}" y1="${y(p.high ?? p.close)}" x2="${cx}"
              y2="${y(p.low ?? p.close)}" stroke="${color}" stroke-width="1"/>
            <rect x="${cx - bw / 2}" y="${top}" width="${bw}" height="${h}"
              fill="${color}" opacity="0.85"/>`;
  }).join("");

  const line = pts.length > 1
    ? `<polyline fill="none" stroke="#5b8def" stroke-width="1.5" opacity="0.7"
        points="${pts.map((p, i) => `${x(i)},${y(p.close)}`).join(" ")}"/>`
    : "";

  const labels = pts.map((p, i) =>
    `<text x="${x(i)}" y="${H - 8}" fill="#6b7483" font-size="10"
      text-anchor="middle">${p.date.slice(5)}</text>`).join("");

  const axis = [hi, (hi + lo) / 2, lo].map((v) =>
    `<line x1="${PAD}" y1="${y(v)}" x2="${W - PAD}" y2="${y(v)}"
      stroke="#2a2f3a" stroke-width="1"/>
     <text x="4" y="${y(v) + 3}" fill="#6b7483" font-size="10">${v.toFixed(2)}</text>`
  ).join("");

  svg.innerHTML = axis + line + body + labels;
  $("chartLegend").innerHTML =
    `<span style="color:var(--up)">上涨</span>
     <span style="color:var(--down)">下跌</span>
     <span style="color:#5b8def">收盘价连线</span>`;
}

function renderWhyHappened(w) {
  $("priorityBox").innerHTML =
    `<b>研究优先级：${esc(w.priority_label)}</b><p>${esc(w.priority_reason)}</p>`;
  $("evidenceWindowNote").textContent = w.evidence_window_note;

  $("driverList").innerHTML = w.drivers.length
    ? w.drivers.map(renderDriver).join("")
    : `<p class="hint">本次研究没有形成任何候选驱动因素。</p>`;
  bindEvidenceLinks($("driverList"));

  $("cluePool").innerHTML = w.clue_pool.length
    ? `<h3>检索线索</h3><p class="hint">以下线索来自第三方异动解读等低可信来源，
        仅用于指引检索方向，不作为结论依据：${w.clue_pool.map(esc).join("、")}</p>`
    : "";

  renderGaps($("whyGaps"), w.gaps, "证据检索的缺口");
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

// empty 的措辞要区分「检索过但没找到」和「确实不存在」——只有前者是真的。
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

function renderWhatItMeans(brief) {
  const o = brief.what_it_means.overall;
  $("overallBox").innerHTML = `<b>总体：${esc(o.display)}</b><p>${esc(o.reason)}</p>`;
  $("meansNote").textContent = brief.what_it_means.note;

  const assessed = brief.why_happened.drivers.filter((d) => d.assessment);
  $("assessmentList").innerHTML = assessed.length
    ? assessed.map(renderAssessment).join("")
    : `<p class="hint">没有驱动因素达到进入基本面分析的证据门槛
        （只有「支持」和「部分支持」的因素才会进入第三阶段）。</p>`;
  bindEvidenceLinks($("assessmentList"));
}

function renderAssessment(d) {
  const a = d.assessment;
  return `<div class="assessment">
    <div class="a-head">
      <h4>${esc(d.name)}</h4>
      <span class="headline ${a.display_suppressed ? "suppressed" : ""}">
        ${esc(a.display_headline)}</span>
    </div>

    ${a.display_suppressed
      ? `<div class="suppression">${esc(a.suppression_reason || "")}</div>` : ""}

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
}

function factorCol(title, items) {
  const empty = !items || !items.length;
  return `<div class="factor-col ${empty ? "empty" : ""}">
    <h5>${esc(title)}</h5>
    <ul>${empty ? "<li>未发现 / 未能确认</li>"
                : items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
  </div>`;
}

function renderOpenQuestions(o) {
  $("openNote").textContent = o.note;
  $("openList").innerHTML = o.questions.length
    ? o.questions.map((q) => `<li>${esc(q)}</li>`).join("")
    : `<li class="hint">本次研究没有遗留未解决的问题。</li>`;
  renderGaps($("openGaps"), o.gaps, "本次研究中出现的全部数据缺口");
}

function renderGaps(container, gaps, title) {
  if (!gaps || !gaps.length) { container.innerHTML = ""; return; }
  container.innerHTML = `<div class="gaps"><h3>${esc(title)}</h3>` +
    gaps.map((g) => `<div class="gap">
      <b>${esc(g.field)}</b>：${esc(g.reason)}
      <p>影响：${esc(g.impact)}</p>
    </div>`).join("") + `</div>`;
}

// ---------------------------------------------------------------- 抽屉

function bindEvidenceLinks(root) {
  root.querySelectorAll(".eid").forEach((el) => {
    el.addEventListener("click", () => openDrawer("evidence", el.dataset.eid));
  });
}

function openDrawer(kind, highlightId) {
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
        ${e.source_url ? `<p class="ev-meta"><a href="${esc(e.source_url)}"
            target="_blank" rel="noopener">查看原文</a></p>` : ""}
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
          <span class="tier">${esc(c.provider)}${c.latency_ms !== null && c.latency_ms !== undefined ? ` · ${c.latency_ms}ms` : ""}</span>
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

// ---------------------------------------------------------------- 工具

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

const tierLabel = (t) => TIER_LABELS[t] || t || "";
const supportLabel = (s) => SUPPORT_LABELS[s] || s || "";
const pctStr = (v) => (v * 100).toFixed(0) + "%";
const truncate = (s, n) => (s.length > n ? s.slice(0, n) + "…" : s);

// 红涨绿跌只对带方向的单位成立。振幅、占比、倍数都是无方向的幅度，
// 给「路径效率 100%」上红色会被读成涨了 100%，所以一律保持中性。
const DIRECTIONAL_UNITS = new Set(["pct", "pct_points"]);

function signClass(m) {
  if (!DIRECTIONAL_UNITS.has(m.unit)) return "";
  return m.value > 0 ? "pos" : m.value < 0 ? "neg" : "";
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init();
