/* ===================================================================
   RatioGuard 大屏 · 渲染逻辑
   拉取 /api/state，渲染指标卡、信号放行、三档快照、漂移仪表、
   质量指标、信号/漂移/审计列表；时钟 + 5s 自动刷新；断线提示。
   =================================================================== */
(function () {
  "use strict";

  const REFRESH_MS = 5000;
  const $ = (id) => document.getElementById(id);

  // 口径常量：与 config.yaml / core/hard_rules.py 保持一致（均为不可运行期修改的硬规则）。
  // 成本 c 优先取 API 的 snapshots.*.cost_per_unit，缺失时才用此兜底值。
  const COST_FALLBACK = 0.0005;   // config.yaml cost_per_unit
  const MIN_SESSIONS = 20;        // config.yaml min_qualified_sessions
  const EOD_FREEZE = "15:10";     // core/hard_rules.py EOD_FREEZE_HOUR/MINUTE
  // 第 3 条：高胜率必须随附的风险口径，固定放在指标面板与底部免责声明中。
  const RISK_NOTE = "历史样本快照：高胜率不代表未来，套利策略在市场结构切换时会快速失效；"
    + "且本面板统计是配对套利、依赖融券，本金门槛极高，小资金难以参与。";

  // ---------- 工具 ----------
  const f = (v, d = 4) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);
  const pct = (v, d = 2) => (v === null || v === undefined) ? "—" : (Number(v) * 100).toFixed(d) + "%";
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function fmtTs(ts) {
    if (!ts) return "—";
    // 2026-11-11T20:00:33+08:00 -> 11-11 20:00:33
    const m = /^\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/.exec(ts);
    return m ? m[1] + " " + m[2] : ts;
  }

  // ---------- 数据源切换 + 真实行情只读探针 ----------
  // 页面加 ?source=live / ?source=demo 即可即时切换后端数据库，无需重启服务。
  // 后端为旧版本时（无 meta、/api/market 404），两个 chip 自动隐藏，界面保持原样。
  const SOURCE_PARAM = new URLSearchParams(location.search).get("source");
  const STATE_URL = "/api/state" + (SOURCE_PARAM ? "?source=" + encodeURIComponent(SOURCE_PARAM) : "");

  function renderMeta(meta) {
    const chip = $("chip-source");
    if (!chip) return;
    if (!meta || !meta.source) { chip.hidden = true; return; }
    const live = meta.source === "LIVE";
    const c = meta.counts || {};
    chip.hidden = false;
    chip.className = "chip chip-source " + (live ? "live" : "demo");
    chip.textContent = live ? "LIVE 真实库" : "DEMO 演示数据";
    chip.title = [
      `${live ? "真实运行库" : "离线演示库"}：${meta.db_path || "—"}`,
      `行情 ${c.quote_table ?? "—"} 条 / 信号 ${c.signal_table ?? "—"} 条 / 快照 ${c.snapshot_table ?? "—"} 条 / 日志 ${c.run_log ?? "—"} 条`,
      `最后行情：${meta.last_quote_ts || "无"}　库更新：${meta.db_mtime || "—"}`,
      meta.note || "",
      "切换：地址后加 ?source=live 或 ?source=demo（无需重启服务）",
    ].join("\n");
  }

  function renderMarket(m) {
    const chip = $("chip-market");
    if (!chip) return;
    if (!m) { chip.hidden = true; return; }
    chip.hidden = false;
    if (!m.ok) {
      chip.className = "chip chip-market chip-off";
      chip.textContent = "行情探测不可用";
      chip.title = (m.error || "未知错误") + "\n" + (m.note || "");
      return;
    }
    const pa = Number(m.price_a).toFixed(3), pb = Number(m.price_b).toFixed(3);
    chip.className = "chip chip-market " + (m.fresh ? "chip-ok" : "chip-muted");
    chip.textContent = `${m.symbol_a} ${pa} / ${m.symbol_b} ${pb}`;
    chip.title = [
      `真实行情（${m.source || "未知源"} 只读探测 · 与 L1 采集同链路）`,
      `${m.symbol_a} ${m.name_a || ""}：${pa}　报价 ${m.ts_a || "—"}`,
      `${m.symbol_b} ${m.name_b || ""}：${pb}　报价 ${m.ts_b || "—"}`,
      `比价 ${Number(m.ratio).toFixed(4)} / 配置基准 ${Number(m.base_ratio).toFixed(4)}`,
      `报价年龄 ${m.age_sec ?? "—"}s（新鲜度阈值 ${m.max_quote_age_sec}s）→ ${m.fresh ? "新鲜，可进入采集" : "盘后/陈旧，采集时会被 stale_quote 丢弃"}`,
      m.note || "",
    ].join("\n");
  }

  async function refreshMarket() {
    try {
      const res = await fetch("/api/market", { cache: "no-store" });
      if (!res.ok) { renderMarket(null); return; }
      renderMarket(await res.json());
    } catch (e) { renderMarket(null); }
  }
  refreshMarket();
  setInterval(refreshMarket, 30000);   // 行情探针低频拉取，不与 5s 数据刷新同频

  // ---------- 时钟 ----------
  function tickClock() {
    const now = new Date();
    const p = (n) => String(n).padStart(2, "0");
    $("clock-time").textContent = `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
    $("clock-date").textContent = `${now.getFullYear()}/${p(now.getMonth() + 1)}/${p(now.getDate())} 周${"日一二三四五六"[now.getDay()]}`;
  }
  setInterval(tickClock, 1000); tickClock();

  // ---------- 大屏自适应缩放（设计稿 1920×1080，整体等比缩放）----------
  // 以缩放比例更小者为准（contain）：保证整屏完整可见、不裁切、不重叠。
  // 若 scaleX < scaleY：窗口相对设计稿偏窄高 -> 以【宽度】为基准，上下留边；
  // 若 scaleY <= scaleX：窗口偏宽矮 -> 以【高度】为基准，左右留边。
  const DESIGN_W = 1920, DESIGN_H = 1080;
  function fitScreen() {
    const vw = window.innerWidth, vh = window.innerHeight;
    const scaleX = vw / DESIGN_W, scaleY = vh / DESIGN_H;
    const scale = Math.min(scaleX, scaleY);
    const byWidth = scaleX <= scaleY;
    const stage = document.querySelector(".screen");
    if (stage) stage.style.transform = `scale(${scale})`;
    const chip = $("chip-fit");
    if (chip) {
      chip.textContent = `适配：${byWidth ? "宽度基准" : "高度基准"} · ${scale.toFixed(2)}x`;
    }
  }
  window.addEventListener("resize", fitScreen);
  fitScreen();

  // ---------- 指标卡 ----------
  function renderMetrics(q, snaps) {
    const full = snaps.full || {};
    const be = Number(full.break_even ?? 0);
    const ciLow = Number(full.ci_low ?? 0);
    const cost = (full.cost_per_unit === null || full.cost_per_unit === undefined)
      ? COST_FALLBACK : Number(full.cost_per_unit);
    // 放行判定：Wilson 下限是否越过含成本盈亏平衡
    const pass = ciLow > be;
    // 真实库刚接入时 N=0：统计类数值显示「—」而不是 0.00%，避免被误读为"统计结果为 0"
    const hasSample = Number(full.n ?? 0) > 0;
    const show = (v) => hasSample ? pct(v) : "—";
    const barOf = (v) => hasSample ? Number(v ?? 0) * 100 : 0;
    const cards = [
      { label: "有效样本 N", value: full.n ?? 0, sub: hasSample ? `win ${full.win_cnt ?? 0} / loss ${full.loss_cnt ?? 0}` : "暂无有效样本 · 等待交易时段采集", bar: 0 },
      { label: "胜率", value: show(full.win_rate), sub: hasSample ? `覆盖 ${full.effective_sessions ?? 0} 交易日` : "样本不足，不做统计推断", bar: barOf(full.win_rate),
        tip: RISK_NOTE },
      { label: "盈亏平衡胜率", value: pct(be), sub: `已计入双边交易成本 c=${f(cost, 4)}`, bar: be * 100, cls: "",
        tip: `保本线 = (止损偏移 + 双边成本 c) / (止盈偏移 + 止损偏移)。已计入双边交易成本 c=${f(cost, 4)}（含佣金、买卖价差、冲击与融券费折算）。` },
      { label: "Wilson 95% CI 下限", value: show(ciLow), sub: !hasSample ? "样本不足，无法估计" : (pass ? "已越过盈亏平衡 ✓" : "未越过盈亏平衡"), bar: barOf(ciLow), cls: pass ? "" : "warn" },
      { label: "Hold 率", value: show(full.hold_rate), sub: `hold ${full.hold_cnt ?? 0} 笔`, bar: barOf(full.hold_rate), cls: (full.hold_rate_warn ? "warn" : "") },
      { label: "歧义样本占比", value: show(full.ambiguity_rate), sub: `歧义 ${full.ambiguous_cnt ?? 0} 笔`, bar: barOf(full.ambiguity_rate), cls: (full.ambiguity_warn ? "warn" : "") },
    ];
    $("metric-grid").innerHTML = cards.map((c) => `
      <div class="metric-card${c.tip ? " m-tip" : ""}"${c.tip ? ` title="${esc(c.tip)}"` : ""}>
        <div class="m-label">${c.label}</div>
        <div class="m-value ${c.cls || ""}">${c.value}</div>
        <div class="m-sub">${c.sub}</div>
        <div class="m-bar ${c.cls || ""}" style="width:${Math.min(100, c.bar || 0).toFixed(1)}%"></div>
      </div>`).join("");
  }

  // ---------- 信号放行 ----------
  function renderGate(sys, snaps) {
    sys = sys || {};
    snaps = snaps || {};
    const full = snaps.full || {};
    const badge = $("gate-badge");
    const paused = sys.pause_trade_alert;
    let cls = "gate-muted", text = "置信不足";
    if (paused) { cls = "gate-bad"; text = "漂移暂停"; }
    else if (sys.qualified) { cls = "gate-ok"; text = "统计达标"; }
    badge.className = "gate-badge " + cls;
    badge.textContent = text;
    $("gate-reason").textContent = sys.qualified_reason || "—";
    $("gate-source").textContent = (sys.qualified_source === "frozen") ? "15:10 冻结快照" : "实时(live)";
    $("gate-pause").textContent = paused ? "已暂停" : "正常";

    // 顶部 chips：强调放行依据是每日 15:10 唯一一次冻结快照，不是盘中实时计算
    const basisTip = `放行依据为每日 ${EOD_FREEZE} 唯一一次冻结快照，盘中不刷新、不实时重算；所有信号均需人工复核。`;
    const qChip = $("chip-qualified");
    qChip.title = basisTip;
    if (paused) { qChip.className = "chip chip-off"; qChip.textContent = "放行暂停：漂移告警"; }
    else if (sys.qualified) { qChip.className = "chip chip-ok"; qChip.textContent = `放行依据：${EOD_FREEZE}冻结快照`; }
    else { qChip.className = "chip chip-muted"; qChip.textContent = "放行依据：置信不足"; }

    const pChip = $("chip-pause");
    if (paused) { pChip.className = "chip chip-off"; pChip.textContent = "交易推送已暂停"; }
    else { pChip.className = "chip chip-ok"; pChip.textContent = "推送正常"; }

    // 放行判定链：逐项硬规则校验
    const be = Number(full.break_even ?? 0);
    const ciLow = Number(full.ci_low ?? 0);
    // 空库（N=0）时统计类数值不可估计：显示「—」并置 na 灰点。
    // 否则 Number(null)=0 会把 CI 下限显示成 0.00%，还会让 Hold 率 / 歧义率
    // 因 0 < 阈值 被误判成「达标」而亮绿灯。
    const hasSample = Number(full.n ?? 0) > 0;
    const checks = [
      { name: "有效样本 N ≥ 100", pass: Number(full.n ?? 0) >= 100, val: full.n ?? 0 },
      { name: "覆盖交易日 ≥ 20", pass: Number(full.effective_sessions ?? 0) >= 20, val: full.effective_sessions ?? 0 },
      // 名称写的是「要求满足的条件」，右侧只展示实际比较结果：
      // 通过 86.88% > 75.00% / 未通过 60.00% ≤ 75.00%（条件是严格大于，
      // 故未通过统一用 ≤），不再出现恒假的「0.00% > 75.00%」。
      {
        name: "Wilson CI下限 > 盈亏平衡",
        pass: hasSample && ciLow > be, na: !hasSample,
        val: hasSample
          ? `${pct(ciLow)} ${ciLow > be ? ">" : "≤"} ${pct(be)}`
          : `— / 需 > ${pct(be)}`,
        tip: `判定条件：Wilson 95% 区间下限须严格大于含成本盈亏平衡胜率 ${pct(be)}；`
           + `当前 CI 下限 ${hasSample ? pct(ciLow) : "样本不足，无法估计"}。`,
      },
      { name: "策略分布未漂移", pass: !paused, val: paused ? "已暂停" : "正常" },
      {
        name: "Hold 率 < 40%", pass: hasSample && Number(full.hold_rate ?? 0) < 0.40,
        na: !hasSample, val: hasSample ? pct(full.hold_rate) : "—",
      },
      {
        name: "歧义率 < 10%", pass: hasSample && Number(full.ambiguity_rate ?? 0) < 0.10,
        na: !hasSample, val: hasSample ? pct(full.ambiguity_rate) : "—",
      },
    ];
    $("gate-chain").innerHTML = checks.map((c) => `
      <div class="chain-item ${c.na ? "na" : (c.pass ? "ok" : "bad")}"${c.tip ? ` title="${esc(c.tip)}"` : ""}>
        <span class="chain-dot"></span>
        <span class="chain-name">${c.name}</span>
        <span class="chain-val">${c.val}</span>
      </div>`).join("");
  }

  // ---------- 三档快照 ----------
  function renderScopes(snaps) {
    const order = ["full", "roll100", "roll200"];
    const names = { full: "FULL", roll100: "ROLL-100", roll200: "ROLL-200" };
    const tags = { full: "全量", roll100: "近100笔", roll200: "近200笔" };
    if (!snaps) { $("scope-list").innerHTML = `<div class="note">暂无快照数据，需运行日终冻结（每日 15:10）后生成。</div>`; return; }
    $("scope-list").innerHTML = order.map((k) => {
      const s = snaps[k] || {};
      const n = Number(s.n ?? 0);
      const wr = Number(s.win_rate ?? 0);
      const ciW = Number(s.ci_high ?? 0);
      const ciL = Number(s.ci_low ?? 0);
      const be = Number(s.break_even ?? 0);
      // 判定原因中文释义（与 core/stats/snapshot.py::_qualify 的返回值一一对应）
      const REASON_TEXT = {
        qualified: "统计达标",
        insufficient_sessions: `有效交易日不足 ${MIN_SESSIONS}，不满足 qualified 条件（N=${n} 已够，但只覆盖 ${s.effective_sessions ?? "—"} 个交易日）`,
        insufficient_sample: "有效样本 N 不足，不满足 qualified 条件",
        below_hard_floor: "样本数低于硬下限，不满足 qualified 条件",
        drift_paused: "策略分布漂移告警，交易推送已暂停",
        ci_low_below_break_even: "Wilson CI 下限未越过含成本盈亏平衡胜率，不满足 qualified 条件",
      };
      const reason = s.qualified_reason || "未达标";
      const qual = s.qualified
        ? `<span class="scope-qual q-ok">达标</span>`
        : `<span class="scope-qual q-no" title="${esc(REASON_TEXT[reason] || reason)}">${esc(reason)}</span>`;
      // CI 区间条：以 [0, max(be,ciW)] 为刻度
      const scale = Math.max(be, ciW, 0.01) * 1.1;
      const loPos = (ciL / scale) * 100;
      const hiPos = (ciW / scale) * 100;
      const bePos = (be / scale) * 100;
      const wrPos = (wr / 1) * 100;
      return `
      <div class="scope-row ${k === "full" ? "active" : ""}">
        <div class="scope-head">
          <span class="scope-name">${names[k]}</span>
          <span class="scope-tag">${tags[k]}</span>
          <span class="scope-tag">N=${n}</span>
          ${qual}
        </div>
        <div class="scope-metrics">
          <div>胜率<b>${n ? pct(wr) : "—"}</b></div>
          <div>Hold率<b>${n ? pct(s.hold_rate) : "—"}</b></div>
          <div>盈亏平衡<b>${pct(be)}</b></div>
          <div>歧义率<b>${n ? pct(s.ambiguity_rate) : "—"}</b></div>
        </div>
        <div class="bar-mini"><i style="width:${n ? wrPos.toFixed(1) : 0}%"></i></div>
        <div class="bar-mini ci"><i style="width:${n ? hiPos.toFixed(1) : 0}%;margin-left:${n ? loPos.toFixed(1) : 0}%;width:${n ? (hiPos - loPos).toFixed(1) : 0}%"></i></div>
        <div class="q-thresh">${n ? `胜率条 / CI区间[${pct(ciL)}~${pct(ciW)}] · 盈亏平衡线 ${pct(be)}` : "暂无样本 · 需交易时段采集并逐日积累（N≥100 / ≥20 交易日）"}</div>
      </div>`;
    }).join("");
  }

  // ---------- 漂移仪表 ----------
  function renderDrift(d, history) {
    d = d || {};
    const zc = Math.abs(Number(d.z_critical ?? 1.96));
    // 样本不足时后端返回 z_stat=null；Number(null)=0 会误画成 z=0.000，必须显式判空
    const z = (d.z_stat === null || d.z_stat === undefined) ? null : Number(d.z_stat);
    const status = d.status || "insufficient";
    const W = 320, H = 70, mid = W / 2, span = 130;
    const toX = (zv) => mid + (zc ? Math.max(-zc, Math.min(zc, zv)) / zc : 0) * span;
    const red = "#ff5d73", cyan = "#19d3ff", dim = "#46618a";
    let marker = "";
    if (z !== null && !isNaN(z)) {
      const x = toX(z);
      marker = `<line x1="${x}" y1="14" x2="${x}" y2="52" stroke="${Math.abs(z) > zc ? red : cyan}" stroke-width="2.5" filter="url(#g)"/>
                <circle cx="${x}" cy="14" r="3.5" fill="${Math.abs(z) > zc ? red : cyan}"/>`;
    }
    const svg = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      <defs><filter id="g" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
      <!-- 红色拒绝域 -->
      <rect x="${mid + span - 2}" y="18" width="${W - (mid + span - 2)}" height="28" fill="rgba(255,93,115,.18)"/>
      <rect x="2" y="18" width="${(mid - span) - 2}" height="28" fill="rgba(255,93,115,.18)"/>
      <!-- 轴 -->
      <line x1="2" y1="32" x2="${W - 2}" y2="32" stroke="${dim}" stroke-width="1"/>
      <line x1="${mid}" y1="20" x2="${mid}" y2="44" stroke="${cyan}" stroke-width="1.5" opacity=".7"/>
      <text x="${mid}" y="58" fill="${dim}" font-size="9" text-anchor="middle" font-family="monospace">0</text>
      <text x="6" y="58" fill="${red}" font-size="9" font-family="monospace">-${zc.toFixed(2)}</text>
      <text x="${W - 6}" y="58" fill="${red}" font-size="9" text-anchor="end" font-family="monospace">+${zc.toFixed(2)}</text>
      ${marker}
      <text x="${mid}" y="12" fill="#cfe3ff" font-size="11" text-anchor="middle" font-family="monospace">z = ${z === null || isNaN(z) ? "—" : z.toFixed(3)}</text>
    </svg>`;
    $("drift-gauge").innerHTML = svg;

    const alert = d.drift_alert;
    const stText = { ok: "正常", alert: "告警", insufficient: "样本不足", config_change: "参数变更" }[status] || status;
    const stCls = alert ? "t-alert" : (status === "ok" ? "t-ok" : "t-insufficient");
    $("drift-info").innerHTML = `
      <div class="kv"><span>检验状态</span><b class="${stCls}">${stText}</b></div>
      <div class="kv"><span>z 临界值(Bonferroni)</span><b>${f(d.z_critical, 4)}</b></div>
      <div class="kv"><span>效应量 / 门槛</span><b>${f(d.effect_size, 4)} / ${f(d.min_effect_size, 2)}</b></div>
      <div class="kv kv-tip" title="W1 = 旧窗口，W2 = 新窗口，互斥无重叠"><span>W1 胜率 (n1/k1)</span><b>${pct(d.p1)} (${d.n1 ?? "—"}/${d.k1 ?? "—"})</b></div>
      <div class="kv kv-tip" title="W1 = 旧窗口，W2 = 新窗口，互斥无重叠"><span>W2 胜率 (n2/k2)</span><b>${pct(d.p2)} (${d.n2 ?? "—"}/${d.k2 ?? "—"})</b></div>
      <div class="kv"><span>窗口长度</span><b>${d.window_size ?? "—"}</b></div>`;
    renderDriftHistory(history);
  }

  // ---------- 漂移 z 值趋势（0 基线对称 SVG 柱状图）----------
  // 1) 按 .dh-chart 容器实际像素 1:1 绘制（viewBox 与画布同尺寸，不做拉伸），
  //    文字与柱子不再因 preserveAspectRatio="none" 变形；
  // 2) 垂直分层：顶部临界标签带 / 上半绘图区 / 0基线 / 下半绘图区 /
  //    底部临界标签带 / 日期刻度带，各占独立行，标签互不重叠。
  function renderDriftHistory(history) {
    const box = $("drift-history");
    if (!box) return;
    if (!history || !history.length) {
      // 真实库刚接入时尚无漂移检验：给出空态说明，避免中栏留下一大片无解释的空白
      box.innerHTML = `<div class="dh-title">最近漂移检验 z 值趋势（0 基线对称）</div>
        <div class="dh-empty">暂无漂移检验记录 · 每日 ${EOD_FREEZE} 日终例程生成（需 N≥100 且覆盖 ≥20 个交易日）</div>`;
      return;
    }
    const zc = Math.abs(Number(history[0].z_critical ?? 2.24)) || 2.24;
    const rows = history.slice().sort((a, b) => (a.ts || "").localeCompare(b.ts || "")).slice(-12);
    const n = rows.length;
    const cyan = "#19d3ff", red = "#ff5d73", dim = "#46618a";

    // 先落骨架，才能量到图表容器的真实像素尺寸（设计稿固定 1920×1080，
    // 整屏只做 transform scale，clientWidth/Height 恒定，无需监听 resize）
    box.innerHTML = `
      <div class="dh-title">最近 ${n} 次漂移检验 z 值趋势（0 基线对称）
        <span class="dh-legend"><i class="dh-dot"></i>正常<i class="dh-dot dh-alert"></i>超临界(±${zc.toFixed(2)})</span></div>
      <div class="dh-chart" id="dh-chart"></div>`;
    const chart = $("dh-chart");
    const W = Math.max(360, Math.round(chart.clientWidth));
    const H = Math.max(120, Math.round(chart.clientHeight));

    const padTop = 20, padBottom = 34;       // 上/下标签带高度
    const top = padTop;                      // +zc 虚线
    const bottom = H - padBottom;            // -zc 虚线
    const mid = (top + bottom) / 2;          // 0 基线
    const half = Math.max(10, mid - top);    // 单侧柱高上限
    const slot = W / n;
    const bw = Math.min(slot * 0.5, 28);
    const clamp = (z) => Math.max(-zc, Math.min(zc, z));
    const yOf = (z) => mid - (clamp(z) / zc) * half;   // z>0 → 上方(小y)

    let bars = "";
    rows.forEach((r, i) => {
      const z = Number(r.z_stat || 0);
      const alert = Math.abs(z) > zc;
      const yEnd = yOf(z);                             // 柱子自由端
      const y0 = Math.min(yEnd, mid);
      const h = Math.max(1.5, Math.abs(yEnd - mid));
      const cx = slot * i + slot / 2;
      const x = cx - bw / 2;
      const col = alert ? red : cyan;
      const dLabel = (r.ts || "").slice(5, 10);        // MM-DD
      bars += `<rect x="${x.toFixed(1)}" y="${y0.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2" fill="${col}" opacity="0.9">`
            + `<title>z=${z.toFixed(3)}${alert ? " (超临界)" : ""} · ${dLabel}</title></rect>`;
      if (alert) {
        // 超临界数值放在柱子"内部"靠近自由端，深色高对比，不侵入外部标签带
        const ly = z >= 0 ? yEnd + 13 : yEnd - 5;
        bars += `<text x="${cx.toFixed(1)}" y="${ly.toFixed(1)}" fill="#04070f" font-size="10" font-weight="700" text-anchor="middle" font-family="monospace">${z.toFixed(2)}</text>`;
      }
      // 日期刻度独占最后一行
      bars += `<text x="${cx.toFixed(1)}" y="${H - 6}" fill="${dim}" font-size="9" text-anchor="middle" font-family="monospace">${dLabel}</text>`;
    });

    const svg = `<svg class="dh-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">
      <!-- ±临界红虚线：贴在绘图区上下边界 -->
      <line x1="0" y1="${top}" x2="${W}" y2="${top}" stroke="${red}" stroke-width="1" stroke-dasharray="4 3" opacity=".7"/>
      <line x1="0" y1="${bottom}" x2="${W}" y2="${bottom}" stroke="${red}" stroke-width="1" stroke-dasharray="4 3" opacity=".7"/>
      <!-- 临界值标签：各自独占顶部/底部标签带，不与柱或日期同行 -->
      <text x="4" y="${top - 6}" fill="${red}" font-size="9" font-family="monospace">+${zc.toFixed(2)}</text>
      <text x="4" y="${bottom + 12}" fill="${red}" font-size="9" font-family="monospace">-${zc.toFixed(2)}</text>
      <!-- 0 中线 -->
      <line x1="0" y1="${mid}" x2="${W}" y2="${mid}" stroke="${dim}" stroke-width="1"/>
      <text x="${W - 4}" y="${mid - 4}" fill="${dim}" font-size="9" text-anchor="end" font-family="monospace">0</text>
      ${bars}
    </svg>`;
    chart.innerHTML = svg;
  }

  // ---------- 质量指标 ----------
  function renderQuality(q, n) {
    q = q || {};
    const has = Number(n ?? 0) > 0;
    const showQ = (v) => has ? pct(v) : "—";
    const items = [
      { label: "胜率(Wilson)", val: showQ(q.full?.win_rate ?? 0), w: has ? Number(q.full?.win_rate ?? 0) * 100 : 0 },
      { label: "Hold 率", val: showQ(q.hold_rate), w: has ? Number(q.hold_rate ?? 0) * 100 : 0, warn: q.hold_rate_warn },
      { label: "歧义样本占比", val: showQ(q.ambiguity_rate), w: has ? Number(q.ambiguity_rate ?? 0) * 100 : 0, warn: q.ambiguity_warn },
      { label: "有效交易日", val: q.effective_sessions ?? "—", w: 0 },
      { label: "Hold 笔数", val: q.hold_cnt ?? "—", w: 0 },
      { label: "歧义笔数", val: q.ambiguous_cnt ?? "—", w: 0 },
    ];
    $("quality-body").innerHTML = items.map((it) => `
      <div class="q-item">
        <div class="q-label"><span>${it.label}</span><b>${it.val}</b></div>
        ${it.w ? `<div class="q-bar ${it.warn ? "warn" : ""}"><i style="width:${Math.min(100, it.w).toFixed(1)}%"></i></div>` : `<div class="q-thresh">计数型指标</div>`}
      </div>`).join("");
  }

  // ---------- 最近信号 ----------
  function renderSignals(list) {
    list = list || [];
    $("signals-count").textContent = list.length + " 条";
    if (!list.length) {
      $("signals-body").innerHTML = `<tr><td colspan="6" style="color:var(--dim);text-align:center;padding:14px">暂无信号记录 · 交易时段由 L1 常驻服务采集生成</td></tr>`;
      $("signal-foot").innerHTML = "";
      return;
    }
    $("signals-body").innerHTML = list.slice(0, 25).map((s) => {
      const st = s.status === "open" ? `<span class="tag t-open">open</span>` : `<span class="tag t-closed">closed</span>`;
      const res = s.result ? `<span class="tag t-${s.result}">${s.result}</span>` : `<span class="tag t-closed">—</span>`;
      const typ = s.signal_type === "spread_long" ? "多" : "空";
      const amb = s.ambiguity ? `<span class="tag t-amber">歧义</span>` : "—";
      const ratio = s.ratio ? Number(s.ratio).toFixed(4) : "—";
      return `<tr><td>${fmtTs(s.signal_ts)}</td><td class="t-${s.signal_type === "spread_long" ? "long" : "short"}">${typ}</td><td>${st}</td><td>${res}</td><td>${amb}</td><td>${ratio}</td></tr>`;
    }).join("");

    // 汇总条
    const win = list.filter((s) => s.result === "win").length;
    const loss = list.filter((s) => s.result === "loss").length;
    const hold = list.filter((s) => s.result === "hold").length;
    const amb = list.filter((s) => s.ambiguity).length;
    const total = list.length;
    const wr = (win + loss) ? (win / (win + loss)) * 100 : 0;
    $("signal-foot").innerHTML = `
      <div class="sf-row">
        <div class="sf-cell sf-total"><span>信号</span><b>${total}</b></div>
        <div class="sf-cell sf-win"><span>胜</span><b>${win}</b></div>
        <div class="sf-cell sf-loss"><span>负</span><b>${loss}</b></div>
        <div class="sf-cell sf-hold"><span>Hold</span><b>${hold}</b></div>
        <div class="sf-cell sf-amb"><span>歧义</span><b>${amb}</b></div>
      </div>
      <div class="sf-bar"><i style="width:${wr.toFixed(1)}%"></i></div>
      <div class="sf-cap">近 ${total} 条结算胜率 ${wr.toFixed(1)}%（hold 不计入）</div>`;
  }

  // ---------- 漂移记录 ----------
  function renderDriftLog(list) {
    list = list || [];
    $("driftlog-count").textContent = list.length + " 条";
    if (!list.length) { $("driftlog-body").innerHTML = `<tr><td colspan="6" style="color:var(--dim);text-align:center;padding:14px">暂无漂移检验记录 · 每日 15:10 日终例程生成</td></tr>`; return; }
    $("driftlog-body").innerHTML = list.slice(0, 30).map((d) => {
      const stCls = d.drift_alert ? "t-alert" : (d.status === "ok" ? "t-ok" : "t-insufficient");
      const stText = { ok: "正常", alert: "告警", insufficient: "不足", config_change: "变更" }[d.status] || d.status;
      return `<tr><td>${fmtTs(d.ts)}</td><td>${f(d.z_stat, 3)}</td><td>${f(d.effect_size, 3)}</td><td>${pct(d.p1, 1)}</td><td>${pct(d.p2, 1)}</td><td class="${stCls}">${stText}</td></tr>`;
    }).join("");
  }

  // ---------- 运行日志 ----------
  function renderLog(list) {
    list = list || [];
    if (!list.length) { $("log-body").innerHTML = `<div class="log-line"><span class="lt">—</span><span class="lm">暂无运行日志</span></div>`; return; }
    $("log-body").innerHTML = list.slice(0, 30).map((r) => `
      <div class="log-line">
        <span class="lt">${fmtTs(r.ts)}</span>
        <span class="lm">[${esc(r.module)}/${esc(r.action)}] ${esc(r.message)}</span>
        <span class="lv lv-${esc(r.level)}">${esc(r.level)}</span>
      </div>`).join("");
  }

  // ---------- 主刷新 ----------
  async function refresh() {
    try {
      const res = await fetch(STATE_URL, { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      document.body.classList.remove("offline");
      $("chip-live").className = "chip chip-ok"; $("chip-live").innerHTML = '<span class="dot"></span>在线';
      renderMeta(data.meta);

      const dash = data.dashboard;
      if (!dash) {
        $("chip-version").textContent = "v —";
        $("disclaimer-text").textContent = "数据库尚未初始化或暂无可展示数据。";
        renderMetrics({}, {}); renderGate({}); renderScopes(null); renderDrift({}, []); renderQuality({}, 0);
        renderSignals([]); renderDriftLog([]); renderLog([]);
        return;
      }
      $("chip-version").textContent = dash.config_version_tag || "v —";
      // 底部滚动免责：保留后端合规原文，另在前面强化「高胜率≠未来」的风险口径
      let disc = `⚠ ${RISK_NOTE}　｜　${dash.disclaimer || "本系统仅做观测/记录/统计/告警，所有信号需人工复核，不构成投资建议。"}`;
      if (data.meta && data.meta.source === "LIVE" && !Number(data.meta.counts?.signal_table || 0)) {
        disc = "当前为真实运行库且尚无样本：需在交易时段启动 L1 常驻服务采集，统计放行要求 N≥100 且覆盖 ≥20 个交易日　｜　" + disc;
      }
      $("disclaimer-text").textContent = disc;
      const n = dash.snapshots?.full?.n ?? 0;
      renderMetrics(dash.quality, dash.snapshots);
      renderGate(dash.system, dash.snapshots);
      renderScopes(dash.snapshots);
      renderDrift(dash.drift, data.drift);
      renderQuality(dash.quality, n);
      renderSignals(data.signals);
      renderDriftLog(data.drift);
      renderLog(data.runlog);
    } catch (e) {
      document.body.classList.add("offline");
      $("chip-live").className = "chip chip-off"; $("chip-live").innerHTML = '<span class="dot"></span>离线';
      console.error("refresh failed:", e);
    }
  }

  refresh();
  setInterval(refresh, REFRESH_MS);
})();
