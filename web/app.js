"use strict";

const $ = (id) => document.getElementById(id);
const all = (selector) => [...document.querySelectorAll(selector)];
const pages = {
  monitor: "实时监控", devices: "设备与控制", orders: "告警工单",
  records: "记录与报告", screen: "管理大屏", rules: "阈值配置",
  business: "业务台账", workbench: "运营工作台", tools: "模型与工具",
};
const metrics = {temperature: "温度", humidity: "湿度", light: "光照"};
const units = {temperature: "°C", humidity: "%", light: "ADC"};
const commandLabels = {
  draft: ["待确认", "warn"], sent: ["等待回执", "warn"], succeeded: ["已执行", "good"],
  rejected: ["联锁拒绝", "bad"], failed: ["板端拒绝或失败", "bad"], unknown: ["执行结果待核实", "warn"],
  expired: ["已过期", "neutral"],
};
const state = {
  session: null, snapshot: null, deviceId: "", page: "monitor", socket: null, reconnect: 0,
  charts: {}, trendKey: "", trendRequest: 0, orderVersion: 0, recordVersion: 0,
  preview: null, alertId: null, toasts: 0, loginBusy: false, lastChat: null,
};
const written = new WeakMap();
const clockShort = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hourCycle: "h23", hour: "2-digit", minute: "2-digit", second: "2-digit",
});
const clockLong = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
});
const dayFormat = new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"});

function escape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}
function insert(id, markup) {
  const node = $(id);
  if (written.get(node) !== markup) {
    node.innerHTML = markup;
    written.set(node, markup);
  }
}
function stamp(value, long = false) {
  return value == null ? "—" : (long ? clockLong : clockShort).format(new Date(value * 1000));
}
function number(value, decimals = 0) {
  return value == null ? "—" : Number(value).toLocaleString("zh-CN", {maximumFractionDigits: decimals});
}
function empty(message) {
  return '<p class="empty">' + escape(message) + "</p>";
}
function badge(label, tone = "neutral") {
  return '<span class="badge" data-tone="' + tone + '">' + escape(label) + "</span>";
}
function device() {
  return state.snapshot?.devices.find((entry) => entry.id === state.deviceId);
}
function titleFor(id) {
  return state.snapshot?.devices.find((entry) => entry.id === id)?.name || id;
}
function canControl() {
  return state.session?.permissions.includes("command.draft") ?? false;
}
function isAdmin() {
  return state.session?.user === "admin";
}
function valueLabel(actuator, value) {
  if (value == null) return "—";
  return actuator === "led" ? value + "%" : (value ? "打开" : "关闭");
}
function connection(label, tone) {
  $("connection-label").textContent = label;
  $("connection-label").dataset.tone = tone;
}
function notify(message, tone = "good") {
  clearTimeout(state.toasts);
  const toast = $("toast");
  toast.textContent = message;
  toast.dataset.tone = tone;
  toast.hidden = false;
  state.toasts = setTimeout(() => { toast.hidden = true; }, 5700);
}

async function request(path, options = {}) {
  const {method = "GET", data, download = false} = options;
  const response = await fetch(path, {
    method, cache: "no-store",
    headers: data === undefined ? {} : {"Content-Type": "application/json"},
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401 && state.session?.user && path !== "/api/login") {
      state.session.user = null;
      showLogin();
    }
    const error = new Error(typeof body.detail === "string" ? body.detail : "请求未成功，请核对输入后重试。");
    error.status = response.status;
    throw error;
  }
  return download ? response : response.json();
}

function withForm(form, operation) {
  const submit = [...form.querySelectorAll("button[type=submit]")];
  if (form.dataset.busy) return Promise.resolve();
  form.dataset.busy = "1";
  for (const button of submit) button.disabled = true;
  return Promise.resolve().then(operation).catch((error) => notify(error.message, "bad")).finally(() => {
    delete form.dataset.busy;
    for (const button of submit) button.disabled = false;
  });
}

function showLogin() {
  disconnectLive();
  resetWorkflows();
  state.snapshot = null;
  state.deviceId = "";
  state.lastChat = null;
  state.trendKey = "";
  state.trendRequest++;
  $("app").hidden = true;
  $("signin").hidden = false;
  $("demo-login").hidden = !state.session?.demo;
  $("login-form").elements.password.value = "";
  $("chat-log").replaceChildren();
  const initial = document.createElement("p");
  initial.className = "muted";
  initial.textContent = "选择终端后，可以查询实时读数或准备一条控制指令。";
  $("chat-log").append(initial);
}

async function signIn(username, password) {
  if (state.loginBusy) return;
  state.loginBusy = true;
  $("login-error").hidden = true;
  $("demo-login").disabled = true;
  try {
    await request("/api/login", {method: "POST", data: {username, password}});
    state.session = await request("/api/session");
    await showApp();
  } catch (error) {
    $("login-error").textContent = error.message;
    $("login-error").hidden = false;
  } finally {
    $("demo-login").disabled = false;
    state.loginBusy = false;
  }
}

async function showApp() {
  $("signin").hidden = true;
  $("app").hidden = false;
  $("account-name").textContent = state.session.roles[state.session.user];
  $("account-id").textContent = state.session.user;
  $("mode-label").textContent = state.session.demo ? "模拟实训" : "真机模式";
  $("model-label").textContent = state.session.model_enabled ? "Agentica · 数据工具" : "本地数据工具";
  for (const node of all("[data-admin]")) node.hidden = !isAdmin();
  for (const node of all("[data-control]")) node.hidden = !canControl();
  $("demo-controls").hidden = !state.session.demo || !canControl();
  insert("agent-select", Object.entries(state.session.agents).map(([id, name]) =>
    '<option value="' + escape(id) + '">' + escape(name) + "</option>").join(""));
  $("agent-select").value = "auto";
  renderPrompts();
  insert("scenario-list", Object.entries(state.session.scenarios).map(([id, name]) =>
    '<button type="button" class="button" data-scenario="' + escape(id) + '">' + escape(name) + "</button>").join(""));
  $("report-date").value = dayFormat.format(new Date());
  $("business-day").value = $("report-date").value;
  $("skill-day").value = $("report-date").value;
  $("record-kind").value = "events";
  $("record-query").value = "";
  $("include-closed").checked = false;
  state.snapshot = await request("/api/state");
  navigate();
  connectLive();
}

function disconnectLive() {
  clearTimeout(state.reconnect);
  const socket = state.socket;
  state.socket = null;
  if (socket) socket.close();
}
function connectLive() {
  disconnectLive();
  connection("正在连接", "neutral");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(protocol + "//" + location.host + "/ws");
  state.socket = socket;
  socket.onopen = () => connection("实时连接", "good");
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "state" && state.socket === socket) {
      state.snapshot = payload.data;
      render();
    }
  };
  socket.onclose = () => {
    if (state.socket !== socket || !state.session?.user) return;
    connection("连接中断 · 正在重试", "warn");
    state.reconnect = setTimeout(async () => {
      try {
        state.snapshot = await request("/api/state");
        render();
      } catch {
        if (!state.session?.user) return;
      }
      if (state.session?.user) connectLive();
    }, 1800);
  };
}

function navigate() {
  const fragment = location.hash.slice(1) || (location.pathname === "/screen" ? "screen" : "monitor");
  const restricted = (["rules", "tools"].includes(fragment) && !isAdmin()) || (fragment === "workbench" && !canControl());
  state.page = Object.hasOwn(pages, fragment) && !restricted ? fragment : "monitor";
  $("page-title").textContent = pages[state.page];
  $("device-picker").hidden = !["monitor", "devices", "workbench", "tools"].includes(state.page);
  for (const page of all("[data-page]")) page.hidden = page.dataset.page !== state.page;
  for (const nav of all("[data-nav]")) {
    if (nav.dataset.nav === state.page) nav.setAttribute("aria-current", "page");
    else nav.removeAttribute("aria-current");
  }
  if (state.page === "records") loadRecords();
  if (state.page === "screen") loadSummary();
  if (state.page === "rules" && state.snapshot) {
    for (const [key, value] of Object.entries(state.snapshot.rules)) $("rules-form").elements[key].value = value;
  }
  if (state.page === "orders" && $("include-closed").checked) loadAllOrders();
  render();
  workflowNavigation();
}

function render() {
  if (!state.snapshot) return;
  const devices = state.snapshot.devices;
  if (!device()) state.deviceId = devices[0]?.id || "";
  const select = devices.map((entry) => '<option value="' + escape(entry.id) + '">' + escape(entry.name) + "</option>").join("");
  const reportChoice = $("report-device").value;
  insert("device-select", select);
  $("device-select").value = state.deviceId;
  insert("report-device", '<option value="">所有登记设备</option>' + select);
  $("report-device").value = reportChoice;
  const active = state.snapshot.alerts.filter((entry) => !entry.resolved_at).length;
  $("nav-alerts").hidden = active === 0;
  $("nav-alerts").textContent = active;
  if (state.page === "monitor") renderMonitor();
  if (state.page === "devices") renderDevices();
  if (state.page === "orders" && !$("include-closed").checked) renderOrders(state.snapshot.alerts);
  if (state.page === "workbench") refreshDecisions();
}

function metric(label, value, unit, foot, tone = "neutral") {
  return '<div class="metric" data-tone="' + tone + '"><div class="metric-label">' + escape(label) +
    '</div><div class="metric-value">' + escape(value) + (unit ? "<small>" + escape(unit) + "</small>" : "") +
    '</div><div class="metric-foot">' + escape(foot) + "</div></div>";
}
function deviceStatus(entry) {
  if (!entry?.connected) return ["离线", "warn"];
  return entry.fresh ? ["在线", "good"] : ["等待有效采样", "warn"];
}
function renderMonitor() {
  const entry = device();
  const value = entry?.latest || {};
  const [label, tone] = deviceStatus(entry);
  const {rules} = state.snapshot;
  const alarms = state.snapshot.alerts.filter((alert) => alert.device_id === state.deviceId);
  const events = state.snapshot.events.filter((event) => event.device_id === state.deviceId);
  $("chat-handoff").disabled = !state.lastChat || state.lastChat.deviceId !== state.deviceId;
  insert("device-state", '<div><span class="status" data-tone="' + tone + '">' + label + "</span><span>" +
    escape(entry?.id || "未登记终端") + " · " + escape(state.session.domains[entry?.domain] || "—") +
    "</span></div><time>" + (entry?.fresh ? "采样 " : "最近留存 ") + stamp(value.at, true) + "</time>");
  insert("metrics",
    metric("温度", number(value.temperature, 1), "°C", "上限 " + rules.temperature_high + " °C",
      value.temperature != null && value.temperature > rules.temperature_high ? "bad" : "neutral") +
    metric("相对湿度", number(value.humidity, 1), "%", rules.humidity_low + "–" + rules.humidity_high + " % 为正常范围",
      value.humidity != null && (value.humidity < rules.humidity_low || value.humidity > rules.humidity_high) ? "bad" : "neutral") +
    metric("光照", number(value.light), "ADC", "下限 " + rules.light_low + " ADC",
      value.light != null && value.light < rules.light_low ? "bad" : "neutral") +
    metric("人体感应", value.presence == null ? "—" : value.presence ? "有人" : "无人", "",
      value.obstruction == null ? "红外待采集" : value.obstruction ? "红外有遮挡" : "红外通畅", value.obstruction ? "bad" : "neutral"));
  insert("recent-alerts", alarms.length ? '<div class="record-list">' + alarms.slice(0, 4).map((entry) =>
    '<div class="record-line"><div><strong>' + escape(alertText(entry)) + "</strong><p>" +
    escape(entry.resolved_at ? "已恢复，等待处置" : "当前越限") + "</p></div><time>" + stamp(entry.opened_at) + "</time></div>"
  ).join("") + "</div>" : empty("该终端暂无未关闭告警。"));
  insert("recent-events", events.length ? '<div class="record-list">' + events.slice(0, 4).map((event) =>
    '<div class="record-line"><div>' + escape(event.message) + "</div><time>" + stamp(event.at) + "</time></div>").join("") +
    "</div>" : empty("该终端暂无事件。"));
  loadTrend();
}

function alertText(alert) {
  return (metrics[alert.metric] || alert.metric) + " " + number(alert.value, 1) + " " + (units[alert.metric] || "") +
    (alert.direction === "high" ? " 高于 " : " 低于 ") + number(alert.threshold, 1);
}
function chart(id) {
  const node = $(id);
  if (!window.echarts) throw new Error("本地图表资源未加载。");
  if (!state.charts[id]) {
    const instance = window.echarts.init(node);
    state.charts[id] = instance;
    new ResizeObserver(() => {
      if (node.clientWidth && node.clientHeight) instance.resize();
    }).observe(node);
  }
  return state.charts[id];
}
const lineStyle = {color: "#344752"};
const axisLabel = {color: "#acc0ca"};
async function loadTrend() {
  const id = state.deviceId;
  const minutes = $("trend-window").value;
  if (!id) return;
  const key = [id, minutes, device()?.latest?.at, state.snapshot.rules.temperature_high].join("/");
  if (state.trendKey === key) return;
  state.trendKey = key;
  const requestId = ++state.trendRequest;
  try {
    const points = await request("/api/devices/" + encodeURIComponent(id) + "/trend?minutes=" + minutes);
    if (requestId !== state.trendRequest || state.page !== "monitor") {
      if (requestId === state.trendRequest) state.trendKey = "";
      return;
    }
    const hasData = points.some((point) => point.temperature != null || point.humidity != null);
    $("trend-chart").hidden = !hasData;
    $("trend-empty").hidden = hasData;
    $("trend-empty").textContent = "等待设备上报温湿度。";
    if (!hasData) return;
    chart("trend-chart").setOption({
      animation: false, color: ["#78e3c3", "#9dbdff"], backgroundColor: "transparent",
      grid: {left: 48, right: 48, top: 57, bottom: 32},
      legend: {data: ["温度", "湿度"], textStyle: axisLabel, left: 6, top: 4, itemWidth: 17},
      tooltip: {trigger: "axis", valueFormatter: (value) => number(value, 1)},
      xAxis: {type: "time", splitNumber: 4, axisLine: {lineStyle},
        axisLabel: {...axisLabel, formatter: (value) => stamp(value / 1000).slice(0, 5)}},
      yAxis: [
        {type: "value", name: "°C", nameGap: 10, nameTextStyle: axisLabel, scale: true, axisLabel, splitLine: {lineStyle}},
        {type: "value", name: "%", nameGap: 10, nameTextStyle: axisLabel, scale: true, axisLabel, splitLine: {show: false}},
      ],
      series: [
        {name: "温度", type: "line", showSymbol: false, lineStyle: {width: 2.5},
          data: points.map((point) => [point.at * 1000, point.temperature])},
        {name: "湿度", type: "line", yAxisIndex: 1, showSymbol: false, lineStyle: {width: 1.8},
          data: points.map((point) => [point.at * 1000, point.humidity])},
      ],
    });
  } catch (error) {
    if (requestId !== state.trendRequest) return;
    $("trend-chart").hidden = true;
    $("trend-empty").hidden = false;
    $("trend-empty").textContent = error.message;
  }
}

function table(headers, rows) {
  if (!rows.length) return empty("暂无匹配的记录。");
  return '<div class="table-wrap"><table><thead><tr>' +
    headers.map((head) => '<th scope="col">' + escape(head) + "</th>").join("") + "</tr></thead><tbody>" +
    rows.map((row) => "<tr>" + row.map((cell) => "<td>" + cell + "</td>").join("") + "</tr>").join("") +
    "</tbody></table></div>";
}
function renderDevices() {
  const entry = device();
  insert("device-list", state.snapshot.devices.map((item) => {
    const [label, tone] = deviceStatus(item);
    return '<button type="button" class="device-option" data-device="' + escape(item.id) +
      '" aria-pressed="' + (item.id === state.deviceId) + '"><strong>' + escape(item.name) + '</strong><small>' +
      escape(state.session.domains[item.domain] + " · " + item.id) + '</small><span class="status" data-tone="' +
      tone + '">' + label + "</span></button>";
  }).join(""));
  const [label, tone] = deviceStatus(entry);
  insert("control-status", badge(label, tone));
  const outputs = entry?.capabilities || [];
  insert("output-list", outputs.length ? outputs.map((output) => '<div><span>' + escape(state.session.outputs[output]) +
    "</span><strong>" + escape(valueLabel(output, entry.outputs[output])) + "</strong></div>").join("") :
    empty("该终端未登记执行器。"));
  const old = $("actuator").value;
  insert("actuator", outputs.map((output) => '<option value="' + escape(output) + '">' +
    escape(state.session.outputs[output]) + "</option>").join(""));
  if (outputs.includes(old)) $("actuator").value = old;
  $("control-form").hidden = !canControl() || !outputs.length;
  if ($("actuator").value !== old) setControlRange();
  insert("command-log", formatRecords("commands", state.snapshot.commands.filter((record) => record.device_id === state.deviceId)));
}
function setControlRange() {
  const target = $("target-value");
  target.max = $("actuator").value === "led" ? "100" : "1";
  if (+target.value > +target.max) target.value = target.max;
}
function renderOrders(orders) {
  insert("order-list", table(["触发时间", "设备", "告警", "状态", "处置说明", "回访", "操作"], orders.map((entry) => {
    const label = entry.closed_at ? ["已关闭", "good"] : entry.resolved_at ? ["恢复待处置", "warn"] : ["越限中", "bad"];
    let action = "—";
    if (canControl() && entry.resolved_at && !entry.closed_at) {
      action = '<button type="button" class="button" data-close-alert="' + Number(entry.id) + '">关闭工单</button>';
    } else if (canControl() && entry.closed_at && !entry.review_note) {
      action = '<button type="button" class="button" data-review-alert="' + Number(entry.id) + '">登记回访</button>';
    }
    return [stamp(entry.opened_at, true), escape(titleFor(entry.device_id)), escape(alertText(entry)),
      badge(...label), escape(entry.note || "—"), escape(entry.review_note || "—"), action];
  })));
}
async function loadAllOrders() {
  const version = ++state.orderVersion;
  try {
    const orders = await request("/api/records?kind=alerts");
    if ($("include-closed").checked && version === state.orderVersion) renderOrders(orders);
  } catch (error) {
    notify(error.message, "bad");
  }
}
function formatRecords(kind, rows) {
  if (kind === "commands") return table(["创建时间", "设备", "指令目标", "状态", "回执或原因"], rows.map((entry) =>
    [stamp(entry.created_at, true), escape(titleFor(entry.device_id)), escape(state.session.outputs[entry.actuator] + " " +
      valueLabel(entry.actuator, entry.value)), badge(...(commandLabels[entry.status] || [entry.status])), escape(entry.detail || "—")]));
  if (kind === "alerts") return table(["触发时间", "设备", "告警", "处置说明"], rows.map((entry) =>
    [stamp(entry.opened_at, true), escape(titleFor(entry.device_id)), escape(alertText(entry)),
      escape([entry.note, entry.review_note].filter(Boolean).join(" · ") || "—")]));
  if (kind === "conversations") return table(["时间", "智能体", "问题", "回答", "数据来源"], rows.map((entry) =>
    [stamp(entry.at, true), escape(state.session.agents[entry.agent] || entry.agent), escape(entry.question),
      escape(entry.answer), escape(entry.source)]));
  if (kind === "audit") return table(["时间", "账户", "动作", "细节"], rows.map((entry) =>
    [stamp(entry.at, true), escape(entry.actor), escape(entry.action), escape(entry.detail)]));
  if (kind === "passes") return passTable(rows, false);
  return table(["时间", "设备", "事件"], rows.map((entry) =>
    [stamp(entry.at, true), escape(titleFor(entry.device_id)), escape(entry.message)]));
}
async function loadRecords() {
  const kind = $("record-kind").value;
  const version = ++state.recordVersion;
  try {
    const params = new URLSearchParams({kind, q: $("record-query").value});
    const rows = await request("/api/records?" + params);
    if (version === state.recordVersion) insert("record-results", formatRecords(kind, rows));
  } catch (error) {
    notify(error.message, "bad");
  }
}

async function loadSummary() {
  try {
    const summary = await request("/api/summary");
    if (state.page !== "screen") return;
    insert("screen-metrics", metric("在线终端", summary.online, "/" + summary.registered, "收到实时数据的设备") +
      metric("当日采样", number(summary.samples), "条", "全部登记设备") +
      metric("当日告警", summary.alerts, "条", "当天首次触发事件") +
      metric("当日用电估算", summary.kwh.toFixed(6), "kWh", "按采样覆盖区间估算"));
    $("screen-date").textContent = summary.day;
    $("energy-note").textContent = summary.energy_note;
    const sales = summary.business_counts.production ? number(summary.business.production) + " 件" : "暂无登记";
    const inventory = summary.business_counts.inventory ? number(summary.business.inventory) + " 件" : "暂无登记";
    insert("screen-business", "<span>当日产量 <strong>" + sales + "</strong></span><span>库存盘点 <strong>" +
      inventory + "</strong></span><span>核验通行 <strong>" + number(summary.events.pass || 0) + " 次</strong></span>");
    const hourly = new Map(summary.hourly.map((row) => [row.hour, row.kwh]));
    chart("energy-chart").setOption({
      animation: false, color: ["#78e3c3"], tooltip: {trigger: "axis", valueFormatter: (value) =>
        value == null ? "无采样" : value.toFixed(6) + " kWh"},
      grid: {left: 68, right: 24, top: 38, bottom: 32},
      xAxis: {type: "category", data: Array.from({length: 24}, (_, index) => String(index).padStart(2, "0") + ":00"),
        axisLine: {lineStyle}, axisLabel},
      yAxis: {type: "value", name: "kWh", nameTextStyle: axisLabel, axisLabel, splitLine: {lineStyle}},
      series: [{type: "bar", data: Array.from({length: 24}, (_, index) => hourly.get(index) ?? null), barMaxWidth: 38}],
    });
  } catch (error) {
    notify(error.message, "bad");
  }
}

function chatLine(title, text, source, category = "agent", recommendations = false) {
  const log = $("chat-log");
  const element = document.createElement("div");
  element.className = "chat-message " + category;
  element.innerHTML = "<strong>" + escape(title) + "</strong><p>" + escape(text) + "</p>" +
    (source ? '<p class="muted">' + escape(source) + "</p>" : "") +
    (recommendations ? '<p><a href="#workbench">查看并采纳建议</a></p>' : "");
  while (log.childElementCount >= 24) log.firstElementChild.remove();
  log.append(element);
  log.scrollTop = log.scrollHeight;
}
function ask(question) {
  if (!state.deviceId) {
    notify("请先选择终端。", "bad");
    return;
  }
  withForm($("ask-form"), async () => {
    const agent = $("agent-select").value;
    const id = state.deviceId;
    $("question").value = "";
    chatLine("你 · " + titleFor(id), question, "", "user");
    const answer = await request("/api/assistant", {method: "POST", data: {agent, device_id: id, text: question}});
    const engine = {agentica: "Agentica", local: "本地工具", local_fallback: "本地工具 · 模型暂不可用"}[answer.engine];
    chatLine(state.session.agents[answer.agent] + " · " + engine, answer.answer, answer.source, "agent",
      canControl() && id === state.deviceId && answer.recommendations > 0);
    state.lastChat = {deviceId: id, id: answer.conversation_id};
    $("chat-handoff").disabled = state.deviceId !== id;
    if (answer.preview) openPreview(answer.preview);
  });
}
function openPreview(command) {
  state.preview = command;
  const details = [
    ["设备", titleFor(command.device_id) + " · " + command.device_id],
    ["执行器", state.session.outputs[command.actuator]],
    ["目标值", valueLabel(command.actuator, command.value)],
    ["预览编号", command.id.slice(0, 8)],
  ];
  insert("confirm-detail", details.map(([key, value]) => "<dt>" + escape(key) + "</dt><dd>" + escape(value) + "</dd>").join(""));
  $("confirm-dialog").showModal();
}
async function readReceipt(command) {
  for (let step = 0; step < 14 && command.status === "sent"; step++) {
    await new Promise((resolve) => setTimeout(resolve, 450));
    command = await request("/api/commands/" + encodeURIComponent(command.id));
  }
  const [label, tone] = commandLabels[command.status] || [command.status, "neutral"];
  notify(label + (command.detail ? " · " + command.detail : ""), tone === "good" ? "good" : "bad");
}

async function exportReport(kind) {
  const params = new URLSearchParams({day: $("report-date").value});
  if ($("report-device").value) params.set("device_id", $("report-device").value);
  const response = await request("/api/reports/" + kind + "?" + params, {download: true});
  const object = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = object;
  anchor.download = (kind === "inspection" ? "巡检报告" : "能耗估算报告") + "-" + $("report-date").value + ".docx";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(object), 10_000);
  notify("Word 报告已生成。");
}

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  signIn(event.currentTarget.elements.username.value.trim(), event.currentTarget.elements.password.value);
});
$("demo-login").addEventListener("click", () => signIn("operator", "shuyun-demo"));
$("logout").addEventListener("click", async () => {
  try {
    await request("/api/logout", {method: "POST"});
    state.session.user = null;
    showLogin();
  } catch (error) { notify(error.message, "bad"); }
});
window.addEventListener("hashchange", () => { if (state.session?.user) navigate(); });
$("device-select").addEventListener("change", (event) => {
  state.deviceId = event.target.value;
  render();
});
$("trend-window").addEventListener("change", loadTrend);
$("agent-select").addEventListener("change", () => renderPrompts());
$("prompts").addEventListener("click", (event) => {
  const button = event.target.closest("[data-question]");
  if (button) ask(button.dataset.question);
});
$("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (question) ask(question);
});
$("device-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-device]");
  if (button) {
    state.deviceId = button.dataset.device;
    render();
  }
});
$("actuator").addEventListener("change", setControlRange);
$("control-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withForm(event.currentTarget, async () => openPreview(await request("/api/commands", {method: "POST", data: {
    device_id: state.deviceId, actuator: $("actuator").value, value: Number($("target-value").value),
  }})));
});
$("confirm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = state.preview?.id;
  if (!id) return;
  $("confirm-dialog").close();
  withForm(event.currentTarget, async () => {
    const command = await request("/api/commands/" + encodeURIComponent(id) + "/confirm", {method: "POST"});
    await readReceipt(command);
  });
});
$("scenario-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-scenario]");
  if (!button || !state.deviceId) return;
  button.disabled = true;
  try {
    await request("/api/demo/" + encodeURIComponent(state.deviceId) + "/scenario",
      {method: "POST", data: {scenario: button.dataset.scenario}});
    notify(button.textContent);
  } catch (error) { notify(error.message, "bad"); }
  finally { button.disabled = false; }
});
$("include-closed").addEventListener("change", () => {
  if ($("include-closed").checked) loadAllOrders();
  else render();
});
$("order-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-close-alert]");
  if (button) {
    state.alertId = button.dataset.closeAlert;
    $("close-form").reset();
    $("close-dialog").showModal();
  }
  const review = event.target.closest("[data-review-alert]");
  if (review) openWorkDialog({type: "review", id: review.dataset.reviewAlert});
});
$("close-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const note = event.currentTarget.elements.note.value.trim();
  withForm(event.currentTarget, async () => {
    await request("/api/alerts/" + state.alertId + "/close", {method: "POST", data: {note}});
    $("close-dialog").close();
    notify("告警已关闭，处置说明已记录。");
    if ($("include-closed").checked) await loadAllOrders();
  });
});
for (const button of all("[data-dismiss]")) button.addEventListener("click", () => button.closest("dialog").close());
$("record-form").addEventListener("submit", (event) => {
  event.preventDefault();
  withForm(event.currentTarget, loadRecords);
});
$("record-kind").addEventListener("change", loadRecords);
$("report-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const kind = event.submitter?.name || "inspection";
  withForm(event.currentTarget, () => exportReport(kind));
});
$("rules-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const data = Object.fromEntries([...new FormData(event.currentTarget)].map(([key, value]) => [key, Number(value)]));
  withForm(event.currentTarget, async () => {
    await request("/api/rules", {method: "PUT", data});
    notify("阈值已保存，下一轮采样使用新规则。");
  });
});
$("fullscreen").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
    $("fullscreen").textContent = document.fullscreenElement ? "退出全屏" : "全屏";
  } catch { notify("浏览器未启用全屏，请使用浏览器的全屏功能。", "bad"); }
});

function tickClock() {
  $("clock").textContent = clockShort.format(new Date()) + " 北京时间";
}
tickClock();
setInterval(tickClock, 1000);
setInterval(() => { if (state.page === "screen" && state.session?.user) loadSummary(); }, 5000);

async function start() {
  try {
    state.session = await request("/api/session");
    if (state.session.user) await showApp();
    else showLogin();
  } catch (error) {
    showLogin();
    $("login-error").textContent = error.message;
    $("login-error").hidden = false;
  }
}
