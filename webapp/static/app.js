/* 无构建单页（docs/frontend-plan.md 第 4 节）。三个视图：输入 / 历史 / 详情。
 * POST /api/plan 是阻塞的（响应体即 final_plan），等待期间轮询 /api/job/running 报进度。 */

const ICON = {
  attraction: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-6-5.2-6-10a6 6 0 1 1 12 0c0 4.8-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/></svg>',
  meal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 3v8M5 3v5a2 2 0 0 0 4 0V3M7 11v10M17 3c-1.6 1.4-2 3.4-2 5.6 0 1.6.8 2.4 2 2.4v10"/></svg>',
  hotel: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 18v-6h18v6M3 12V7M21 12v-2a3 3 0 0 0-3-3h-6v5"/></svg>',
  transit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 8h12l1 6v3H5v-3z"/><circle cx="8.5" cy="14" r="1"/><circle cx="15.5" cy="14" r="1"/></svg>',
};
const KIND = { attraction: "景点", meal: "餐饮", transit: "交通", hotel: "住宿" };

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const views = { input: $("#view-input"), history: $("#view-history"), plan: $("#view-plan") };
let lastStages = [];

function show(name) {
  Object.entries(views).forEach(([k, el]) => { el.hidden = k !== name; });
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  window.scrollTo({ top: 0 });
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : {}; }
  catch { throw new Error(`服务端返回不是 JSON（HTTP ${res.status}）：${text.slice(0, 120)}`); }
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

function fail(msg) {
  const el = $("#error");
  el.hidden = false;
  el.textContent = msg;
}

/* ------------------------------------------------------------------ 输入页 */

function setProgress(stages, elapsed) {
  const ol = $("#progress");
  ol.hidden = stages.length === 0;
  ol.innerHTML = stages.map((s) => `<li>${esc(s)}</li>`).join("");
  if (elapsed != null) {
    $("#timer").hidden = false;
    $("#timer").textContent = `已用 ${elapsed.toFixed(1)} s`;
  }
}

$("#plan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = $("#question").value.trim();
  const offline = $("#offline").checked;
  const btn = $("#submit");
  $("#error").hidden = true;
  $("#manifest").hidden = true;
  if (question.length < 4) return fail("题目至少写 4 个字。");

  btn.disabled = true;
  btn.textContent = offline ? "预演中…" : "规划中…";
  lastStages = ["已提交，等待任务开始…"];
  setProgress(lastStages, 0);

  const started = performance.now();
  const tick = setInterval(() => setProgress(lastStages, (performance.now() - started) / 1000), 300);
  const poll = setInterval(async () => {
    try {
      const s = await api("/api/job/running");
      if (s.running && s.stages.length) lastStages = s.stages;
    } catch { /* 进度只是附加信息，拿不到就沿用上一条 */ }
  }, 1000);

  try {
    const res = await api("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, date: $("#date").value || null, offline }),
    });
    if (res.offline) renderManifest(res);
    else { renderPlan(res); show("plan"); }
  } catch (err) {
    fail(err.message);
  } finally {
    clearInterval(tick); clearInterval(poll);
    btn.disabled = false;
    btn.textContent = "开始规划";
    $("#timer").hidden = true;
    lastStages = [];
    $("#progress").hidden = true;
    $("#progress").innerHTML = "";
  }
});

function renderManifest(m) {
  $("#manifest").hidden = false;
  $("#manifest-meta").textContent =
    `外部调用 0 次 ｜ 模型 ${m.llm_calls} 次（只为抽需求）｜ 目的地 ${m.needs.destination_city || "?"} ｜ ` +
    `命中 wiki ${m.wiki_pages.length} 页 ｜ 起始日 ${m.start_date}`;
  $("#manifest-list").innerHTML = m.planned_calls.map((c) =>
    `<li><b>${esc(c.tool)}</b><br><code>${esc(JSON.stringify(c.args))}</code></li>`).join("");
}

/* ------------------------------------------------------------------ 详情页 */

function stayMinutes(it) {
  const toMin = (t) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(t || "").trim());
    return m ? +m[1] * 60 + +m[2] : null;
  };
  const a = toMin(it.start_time), b = toMin(it.end_time);
  return a != null && b != null && b > a ? b - a : null;
}

function detailRows(it) {
  const rows = [];
  if (it.open_time) rows.push(["开放时间", esc(it.open_time)]);
  rows.push(["参考费用", it.cost == null ? '<span style="color:var(--warn)">待核实</span>'
    : `${Number(it.cost).toFixed(0)} 元`]);
  if (it.rating) rows.push(["高德评分", esc(it.rating)]);
  rows.push(["来源", `<b>${esc(it.source || "none")}</b>${it.location
    ? ` ｜ 坐标 ${Number(it.location.lng).toFixed(5)}, ${Number(it.location.lat).toFixed(5)}`
    : " ｜ 无坐标"}`]);
  if (it.path && it.path.length) rows.push(["实测折线", `${it.path.length} 个点`]);
  return rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
}

function photoBlock(it) {
  if (!it.photo) return "";
  return `<img src="${esc(it.photo)}" alt="" loading="lazy"
    onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'no-photo',textContent:'照片暂时加载不出来'}))">`;
}

function stopCard(it) {
  const stay = stayMinutes(it);
  const warn = !it.verified
    ? `<p class="stop-warning">待核实 · ${it.cost == null ? "金额没取到" : "依据为本次现查"}（来源 ${esc(it.source || "none")}）</p>`
    : "";
  const thumb = it.photo
    ? `<img class="thumb" src="${esc(it.photo)}" alt="" loading="lazy"
         onerror="this.remove()">` : "";
  return `<div class="stop">
    <div class="stop-when"><strong>${esc(it.start_time || "待定")}</strong>
      ${it.end_time ? `<span>${esc(it.end_time)}</span>` : ""}</div>
    <div class="stop-node ${esc(it.type)}" aria-hidden="true">${ICON[it.type] || ICON.attraction}</div>
    <div class="stop-card ${it.type === "meal" ? "is-meal" : ""}" data-toggle data-mk="${it.__i}"
         role="button" tabindex="0" aria-expanded="false"
         title="展开看照片、开放时间、评分与坐标；有坐标时顺带在地图上定位">
      <div class="stop-meta">
        <span>${KIND[it.type] || esc(it.type)}</span>
        <span class="tag ${it.verified ? "" : "unverified"}">${it.verified ? "已核实" : "待核实"}</span>
        ${stay ? `<span class="stay">建议停留 ${(stay / 60).toFixed(1)} 小时</span>` : ""}
      </div>
      <h4 class="stop-name">${esc(it.name)}</h4>
      <div class="stop-row">${thumb}
        ${it.tip ? `<p class="stop-summary">${esc(it.tip)}</p>` : ""}
      </div>
      ${warn}
      <div class="detail" hidden>
        ${it.photo && it.type !== "transit" ? `<figure>${photoBlock(it)}</figure>`
          : (it.type === "transit" ? "" : `<figure><div class="no-photo">暂无景点照片</div></figure>`)}
        <dl>${detailRows(it)}</dl>
        ${it.transport ? `<p class="tips">${esc(it.transport)}</p>` : ""}
      </div>
    </div>
  </div>`;
}

function transferPill(it) {
  const measured = Boolean(it.path && it.path.length);
  const text = it.transport || it.name || "";
  // transport 里常常已经写着"票5元"，再拼一次 cost 就成了"5元 · 5 元"。
  const dupCost = it.cost != null && /(\d+(?:\.\d+)?)\s*元/.test(text)
    && RegExp(`${Number(it.cost)}\\s*元`).test(text);
  const cost = it.cost == null || dupCost ? "" : ` · <b>${Number(it.cost).toFixed(0)}</b> 元`;
  return `<div class="stop"><div class="stop-when"><strong>${esc(it.start_time || "待定")}</strong></div>
    <div class="stop-node transit" aria-hidden="true">${ICON.transit}</div>
    <div class="transfer" data-mk="${it.__i}" title="点击在地图上画出这段实测路线">
      <span class="pill ${measured ? "measured" : ""}">${esc(text)}${cost}</span>
      <span class="pill">${measured ? `实测 ${it.path.length} 点折线` : "路线待实测"}</span>
      ${it.verified ? "" : '<span class="pill" style="color:var(--warn)">班次/票价待核实</span>'}
    </div>
  </div>`;
}

function renderPlan(p) {
  // 降级载荷里没有 days/destination/日期这些字段，标题与摘要要能退到 question，别打出 undefined。
  const dash = (v) => (v == null || v === "" ? "—" : v);
  PLAN = p;
  p.items = [];
  (p.days || []).forEach((d) => d.timeline.forEach((it) => {
    it.__i = p.items.length;
    p.items.push(it);
  }));
  $("#plan-title").textContent = p.destination
    ? `${p.destination} ${p.days_count || ""} 日`.trim()
    : String(p.question || "方案").slice(0, 40);
  $("#plan-meta").textContent =
    `${dash(p.start_date)} → ${dash(p.end_date)} ｜ 出发地 ${p.origin || "未给"} ｜ ` +
    `预算 ${p.budget_yuan ? p.budget_yuan + " 元" : "未给"}`;
  const s = p.sources || {};
  $("#plan-chips").innerHTML = [
    `<span class="chip">外部调用 ${esc(s.external_calls)} 次</span>`,
    `<span class="chip">模型 ${esc(s.llm_calls)} 轮</span>`,
    `<span class="chip">耗时 ${esc(s.seconds)} s</span>`,
    `<span class="chip">wiki ${esc((s.wiki_pages || []).length)} 页</span>`,
    `<span class="chip">${(s.failures || []).length ? `失败 ${s.failures.length} 次` : "外部调用全成功"}</span>`,
  ].join("");

  const wx = {};
  (p.weather_forecast || []).forEach((w) => { wx[w.date] = w; });

  $("#days").innerHTML = (p.days || []).map((d) => {
    const stops = d.timeline.filter((i) => i.type !== "transit");
    const w = wx[d.date];
    return `<section class="day">
      <div class="day-head">
        <span class="dnum">第 ${esc(d.day)} 天</span>
        <span class="dsub">${esc(d.date)} · ${stops.length} 个安排</span>
        <span class="dtheme">${esc(d.theme || "")}</span>
        ${w ? `<span class="day-wx">${esc(w.day)}/${esc(w.night)} ${esc(w.temp_range)} ｜ 风 ${esc(w.wind)}</span>` : ""}
      </div>
      <div class="timeline">${d.timeline.map((it) =>
        it.type === "transit" ? transferPill(it) : stopCard(it)).join("")}</div>
    </section>`;
  }).join("");

  document.querySelectorAll("#days .transfer[data-mk]").forEach((row) => {
    row.addEventListener("click", () => focusItem(Number(row.dataset.mk)));
  });

  document.querySelectorAll("#days [data-toggle]").forEach((card) => {
    const toggle = () => {
      const det = card.querySelector(".detail");
      if (!det) return;
      det.hidden = !det.hidden;
      card.setAttribute("aria-expanded", String(!det.hidden));
    };
    const onActivate = () => {
      toggle();
      // 点卡片顺带在地图上定位这一站（有 location 或实测 path 才有意义）
      focusItem(Number(card.dataset.mk));
    };
    card.addEventListener("click", onActivate);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onActivate(); }
    });
  });

  const deg = $("#degraded");
  deg.hidden = !p.degraded;
  if (p.degraded) {
    $("#degraded-reason").textContent = p.reason || "模型没给出可解析的 JSON";
    $("#degraded-text").textContent = p.markdown || "";
  }

  const fails = s.failures || [];
  $("#sources").innerHTML = `
    <h2>来源与用量</h2>
    <p class="meta">${esc(p.question || "")}</p>
    <h3>知识库命中页（可追溯，带 raw 快照）</h3>
    <ul>${(s.wiki_pages || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>无</li>"}</ul>
    <h3>失败的外部调用（不静默吞）</h3>
    <ul class="${fails.length ? "fail" : ""}">${fails.length
      ? fails.map((f) => `<li>${esc(f.tool)} · ${esc(f.error_code)} · ${esc(f.error_msg)}<br><code>${esc(f.args)}</code></li>`).join("")
      : "<li>无</li>"}</ul>
    <h3>时效</h3>
    <p class="meta">${esc(p.weather_note || "")}。标「待核实」的条目是本次现查或没取到数据，只对 ${esc(p.start_date)} 前后有效。</p>`;

  const hasGeo = (p.days || []).some((d) => d.timeline.some((i) => i.location
    || (i.path && i.path.length)));
  const box = $("#map-block");
  box.hidden = !hasGeo;
  if (hasGeo) {
    // 小屏默认收起：手机上时间线才是主角，地图想看再展开。
    const collapse = window.innerWidth <= 560;
    box.classList.toggle("collapsed", collapse);
    $("#map-toggle").textContent = collapse ? "展开" : "收起";
    $("#map-toggle").setAttribute("aria-expanded", String(!collapse));
    drawMap(p);                       // 地图已就绪就直接画
    if (!MAP.tried) ensureMap();      // 首次进入详情才拉 loader.js，别一开页就打外网
  }
}

/* ------------------------------------------------------------------ 地图块（M4） */

const MAP = { AMap: null, map: null, info: null, overlays: [], fitTargets: [], markerByIdx: {}, lineByIdx: {}, tried: false, ok: false };
const FIT_MAX_KM = 20;   // 离质心超过这个距离的站点不参与初始视野
let PLAN = null;   // 卡片与 marker 靠条目下标对上

function injectScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.async = true;
    el.onload = resolve;
    el.onerror = () => reject(new Error(`脚本加载不出来：${src}`));
    document.head.appendChild(el);
  });
}

function withTimeout(promise, ms, label) {
  let timer;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label}：${ms / 1000} 秒内没响应`)), ms);
    }),
  ]);
}

function mapNote(msg) { $("#map-note").textContent = msg; }

async function ensureMap() {
  if (MAP.tried) return MAP.ok;
  MAP.tried = true;                       // 只试一次：key 被白名单拒时别反复打网络
  mapNote("地图加载中…");
  try {
    const cfg = await api("/api/config");
    if (!cfg.amap_js_key) {
      mapNote("未配置 AMAP_JS_KEY（.env 的 AMAP_JS_KEY / AMAP_JS_SECURITY_CODE），地图块留空。");
      return false;
    }
    // 安全密钥必须在 loader 之前挂到 window 上，否则 2.0 会报 INVALID_USER_SCODE。
    window._AMapSecurityConfig = { securityJsCode: cfg.amap_js_security_code || "" };
    // key 不对时 AMapLoader.load 既不 resolve 也不 reject（实测停在"加载中"不动），必须自己超时。
    await withTimeout(injectScript("https://webapi.amap.com/loader.js"), 8000, "loader.js");
    const AMap = await withTimeout(
      window.AMapLoader.load({ key: cfg.amap_js_key, version: "2.0", plugins: ["AMap.Scale"] }),
      12000, "高德 SDK");
    MAP.AMap = AMap;
    MAP.map = new AMap.Map("map-canvas", { viewMode: "2D", zoom: 11 });
    MAP.map.addControl(new AMap.Scale());
    MAP.info = new AMap.InfoWindow({ offset: new AMap.Pixel(0, -30) });
    MAP.ok = true;
    mapNote("marker 出自条目 location，折线出自本次实测 path");
    if (PLAN) drawMap(PLAN);
    return true;
  } catch (err) {
    mapNote(`地图没起来：${err.message}（不影响下面的时间线）`);
    return false;
  }
}

function infoHtml(it) {
  return `<div class="iw"><b>${esc(it.name)}</b>
    <span>${esc(it.start_time || "")}${it.end_time ? `–${esc(it.end_time)}` : ""} ·
      ${KIND[it.type] || esc(it.type)}</span>
    <span>${it.cost == null ? "费用待核实" : `${Number(it.cost).toFixed(0)} 元`} ·
      ${it.verified ? "已核实" : "待核实"}（${esc(it.source || "none")}）</span>
    ${it.transport ? `<span>${esc(it.transport)}</span>` : ""}</div>`;
}

function focusItem(idx) {
  if (!MAP.ok || !PLAN) return;
  const m = MAP.markerByIdx[idx];
  const line = MAP.lineByIdx[idx];
  if (m) {
    const pos = m.getPosition();
    MAP.map.panTo(pos);
    MAP.info.setContent(infoHtml(PLAN.items[idx]));
    MAP.info.open(MAP.map, pos);
  } else if (line) {
    MAP.map.setFitView([line], false, [50, 50, 50, 50]);
  }
}

function overlayPoint(o) {
  const p = o.getPosition ? o.getPosition() : (o.getPath ? o.getPath()[0] : null);
  if (!p) return null;
  // AMap 的 LngLat 只保证 getLng/getLat，直接取 .lng 会拿到 undefined（算出 NaN 就整批走兜底）。
  const lng = Number(typeof p.getLng === "function" ? p.getLng() : p.lng);
  const lat = Number(typeof p.getLat === "function" ? p.getLat() : p.lat);
  return Number.isFinite(lng) && Number.isFinite(lat) ? [lng, lat] : null;
}

// 初始视野排除离群点：一份三日方案里 40 km 外的机场会把 zoom 拉到 9.85，
// 实测市中心与熊猫基地各自 4~5 个点叠在 11 px 内，只剩最上面那个看得见。
function pickFitTargets(overlays) {
  const pts = overlays.map(overlayPoint).filter(Boolean);
  if (pts.length < 3) return { kept: overlays.slice(), dropped: 0 };
  const med = (i) => pts.map((p) => p[i]).slice().sort((a, b) => a - b)[Math.floor(pts.length / 2)];
  const [mlng, mlat] = [med(0), med(1)];
  const kept = overlays.filter((o) => {
    const p = overlayPoint(o);
    if (!p) return true;
    const dx = (p[0] - mlng) * 111 * Math.cos((mlat * Math.PI) / 180);
    return Math.hypot(dx, (p[1] - mlat) * 111) <= FIT_MAX_KM;
  });
  return kept.length ? { kept, dropped: overlays.length - kept.length } : { kept: overlays.slice(), dropped: 0 };
}

function drawMap(p) {
  if (!MAP.ok) return;
  const AMap = MAP.AMap;
  MAP.map.remove(MAP.overlays);
  MAP.overlays = []; MAP.fitTargets = []; MAP.markerByIdx = {}; MAP.lineByIdx = {};
  let n = 0;
  (p.days || []).forEach((d) => d.timeline.forEach((it) => {
    const idx = it.__i;
    if (it.location) {
      const pos = [it.location.lng, it.location.lat];
      n += 1;
      const marker = new AMap.Marker({
        position: pos,
        content: `<div class="mk ${esc(it.type)}">${n}</div>`,
        anchor: "center",
        zIndex: 30,
      });
      marker.on("click", () => {
        MAP.info.setContent(infoHtml(it));
        MAP.info.open(MAP.map, pos);
      });
      MAP.map.add(marker);
      MAP.overlays.push(marker);
      MAP.markerByIdx[idx] = marker;
    }
    if (it.path && it.path.length > 1) {
      const line = new AMap.Polyline({
        path: it.path.map((c) => c.slice()),
        strokeColor: "#2864A8",
        strokeWeight: 5,
        strokeOpacity: .85,
        lineJoin: "round",
        showDir: true,
        bubble: true,
      });
      MAP.map.add(line);
      MAP.overlays.push(line);
      MAP.lineByIdx[idx] = line;
    }
  }));
  const fit = pickFitTargets(MAP.overlays);
  MAP.fitTargets = fit.kept;
  if (MAP.fitTargets.length) MAP.map.setFitView(MAP.fitTargets, false, [46, 46, 46, 46]);
  mapNote(`${n} 个 marker ｜ ${Object.keys(MAP.lineByIdx).length} 条实测折线`
    + (fit.dropped ? `｜ 视野按 ${FIT_MAX_KM} km 内的站点算，${fit.dropped} 个远点没框进来` : ""));
}

$("#map-toggle").addEventListener("click", () => {
  const box = $("#map-block");
  const collapsed = box.classList.toggle("collapsed");
  const btn = $("#map-toggle");
  btn.textContent = collapsed ? "展开" : "收起";
  btn.setAttribute("aria-expanded", String(!collapsed));
  // 收起时容器是 display:none，零尺寸下 setFitView 算不出视野；
  // 所以必须在展开之后先 resize 再重算一次，否则地图会停在错的缩放上。
  if (!collapsed && MAP.map) {
    setTimeout(() => {
      MAP.map.resize();
      if (MAP.fitTargets.length) MAP.map.setFitView(MAP.fitTargets, false, [46, 46, 46, 46]);
    }, 30);
  }
});

/* ------------------------------------------------------------------ 历史页 */

async function loadHistory() {
  show("history");
  $("#history-meta").textContent = "加载中…";
  try {
    const { plans, total } = await api("/api/plans");
    $("#history-meta").textContent = `共 ${total ?? plans.length} 份，列出最近 ${plans.length} 份`;
    $("#history-list").innerHTML = plans.length ? plans.map((r) => `
      <li>
        <a href="#" data-id="${esc(r.plan_id)}">${esc(r.destination || r.plan_id)}${r.days_count ? ` · ${r.days_count} 日` : ""}</a>
        <p class="meta">${esc(r.start_date || "")} → ${esc(r.end_date || "")} ｜
          外部 ${esc(r.external_calls)} 次 ｜ ${esc(r.seconds)} s ｜ ${esc(r.created)}
          ${r.degraded ? " ｜ 结构化降级" : ""}${r.error ? ` ｜ ${esc(r.error)}` : ""}</p>
      </li>`).join("") : "<li>还没有落盘的方案，先去「新规划」跑一题</li>";
    $("#history-list").querySelectorAll("a[data-id]").forEach((a) => {
      a.addEventListener("click", async (e) => {
        e.preventDefault();
        try { renderPlan(await api(`/api/plans/${encodeURIComponent(a.dataset.id)}`)); show("plan"); }
        catch (err) { fail(err.message); show("input"); }
      });
    });
  } catch (err) {
    $("#history-meta").textContent = `读不动：${err.message}`;
  }
}

document.querySelectorAll(".tab").forEach((b) => {
  b.addEventListener("click", () => (b.dataset.view === "history" ? loadHistory() : show("input")));
});
$(".back").addEventListener("click", () => show("input"));

show("input");
