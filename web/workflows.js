"use strict";

const workflows = {
  business: null, businessRequest: 0, decisionKey: "", decisionRequest: 0,
  access: null, tools: null, dialog: null,
};
const promptLabels = {
  "现在多少度，超温没？": "查当前温度",
  "今天装配线跑了多少件？": "查装配线产量",
  "今天用了多少度电？": "查今日能耗",
  "打开补光灯": "预览开灯",
  "现在有哪些处置建议？": "查处置建议",
};
const passLabels = {
  issued: ["待核验", "neutral"], verified: ["待确认", "warn"], recorded: ["已登记", "good"],
  released: ["已模拟放行", "good"], rejected: ["联锁拒绝", "bad"], expired: ["已过期", "neutral"],
};

function resetWorkflows() {
  workflows.business = workflows.access = workflows.tools = workflows.dialog = null;
  workflows.businessRequest++;
  workflows.decisionKey = "";
  workflows.decisionRequest++;
  $("issued-code").textContent = "";
  $("issued-code").hidden = true;
  $("verify-pass").reset();
  $("model-test").reset();
  $("workflow-dialog").close();
}

function renderPrompts() {
  const agent = $("agent-select").value;
  const prompts = state.session.prompts;
  const choices = agent === "auto" ? [...prompts.device, prompts.energy[0]].slice(0, 6) : prompts[agent] || [];
  insert("prompts", [...new Set(choices)].filter((text) => canControl() || text !== "打开补光灯").map((text) =>
    '<button class="button prompt" type="button" data-question="' + escape(text) + '">' +
    escape(promptLabels[text] || text) + "</button>").join(""));
}

function workflowNavigation() {
  if (state.page === "business") loadBusiness();
  if (state.page === "workbench") {
    refreshDecisions(true);
    loadHandoffs();
    loadAccess();
  }
  if (state.page === "tools") loadTools();
}

async function loadBusiness() {
  const day = $("business-day").value;
  if (!day) return;
  const requestId = ++workflows.businessRequest;
  try {
    const data = await request("/api/business?day=" + encodeURIComponent(day));
    if (requestId !== workflows.businessRequest || state.page !== "business") return;
    workflows.business = data;
    const total = (kind) => data.counts[kind] ? number(data.totals[kind]) + " 件" : "暂无登记";
    insert("business-totals", "<span>登记产量 <strong>" + total("production") +
      "</strong></span><span>库存盘点 <strong>" + total("inventory") + "</strong></span>");
    insert("business-list", table(["类别", "编号", "名称", "件数", "来源", "更新时间", "操作"], data.records.map((entry, index) =>
      [entry.kind === "production" ? "产量" : "库存", escape(entry.code), escape(entry.label),
        number(entry.quantity), escape(entry.source), stamp(entry.updated_at, true),
        canControl() ? '<button class="button business-edit" type="button" data-edit="' + index + '">编辑</button>' : "—"])));
  } catch (error) { notify(error.message, "bad"); }
}

async function refreshDecisions(force = false) {
  const entry = device();
  if (!entry || state.page !== "workbench") return;
  const key = [entry.id, entry.latest?.id, entry.fresh, entry.outputs.led].join("/");
  if (!force && key === workflows.decisionKey) return;
  workflows.decisionKey = key;
  const version = ++workflows.decisionRequest;
  try {
    const data = await request("/api/decisions/" + encodeURIComponent(entry.id));
    if (version !== workflows.decisionRequest || state.page !== "workbench") return;
    insert("decision-list", data.length ? '<div class="record-list">' + data.map((plan) =>
      '<div class="record-line decision-row"><div><strong>' + escape(plan.title) + "</strong><p>" +
      escape(plan.detail) + '</p></div><button class="button" type="button" data-adopt="' + escape(plan.id) +
      '" data-for-device="' + escape(entry.id) + '">' + (plan.kind === "command" ? "采纳并预览" : "采纳并转人工") +
      "</button></div>").join("") + "</div>"
      : empty("当前终端暂无处置建议。"));
  } catch (error) {
    if (version === workflows.decisionRequest) insert("decision-list", empty(error.message));
  }
}

async function loadHandoffs() {
  try {
    const rows = await request("/api/handoffs");
    if (state.page !== "workbench") return;
    insert("handoff-list", rows.length ? '<div class="record-list">' + rows.map((entry) =>
      '<div class="record-line decision-row"><div><strong>' + escape(titleFor(entry.device_id)) +
      " · " + escape(entry.reason) + "</strong><p>" + escape([entry.question, entry.resolution].filter(Boolean).join(" · ") || "手动发起") +
      "</p></div>" + (entry.resolved_at ? badge("已完成", "good") : '<button class="button" type="button" data-resolve="' +
        entry.id + '">完成</button>') + "</div>").join("") + "</div>" : empty("暂无待接办的人工工单。"));
  } catch (error) { notify(error.message, "bad"); }
}

function passTable(rows, actionable) {
  return table(["签发时间", "通道", "用途", "状态", "结果", "操作"], rows.map((entry) => {
    const able = actionable && entry.status === "verified" && (isAdmin() || entry.verified_by === state.session.user);
    const expired = ["issued", "verified"].includes(entry.status) && entry.expires_at <= Date.now() / 1000;
    return [stamp(entry.issued_at, true), escape(titleFor(entry.device_id)), escape(entry.purpose),
      badge(...passLabels[expired ? "expired" : entry.status]), escape(entry.detail || "—"),
      able && !expired ? '<button type="button" class="button" data-pass="' + escape(entry.id) +
        '">' + (state.session.demo ? "确认放行" : "确认登记") + "</button>" : "—"];
  }));
}

async function loadAccess() {
  try {
    const data = await request("/api/access");
    if (state.page !== "workbench") return;
    workflows.access = data;
    const selection = $("gate-device").value;
    insert("gate-device", data.devices.map((entry) => '<option value="' + escape(entry.id) +
      '">' + escape(entry.name) + "</option>").join(""));
    if (data.devices.some((entry) => entry.id === selection)) $("gate-device").value = selection;
    $("access-forms").hidden = !data.devices.length;
    $("gate-mode").textContent = data.demo ? "核验后需人工确认。模拟闸机放行三秒，随后自动关闭。" : "人工核验完成后保存通行登记。";
    updateGateState();
    insert("pass-list", passTable(data.passes, true));
  } catch (error) {
    $("gate-state").textContent = error.message;
  }
}

function updateGateState() {
  if (!workflows.access) return;
  const {demo, gates, devices} = workflows.access;
  const open = gates[$("gate-device").value] > Date.now() / 1000;
  $("gate-state").textContent = devices.length === 0 ? "尚未登记安全通道" :
    demo ? (open ? "模拟闸机已开启" : "模拟闸机已关闭") : "人工核验登记";
}

function openWorkDialog(action) {
  const types = {
    handoff: ["发起转人工", "提交工单", titleFor(action.deviceId)],
    resolve: ["完成人工处置", "保存处置结果", "工单 " + action.id],
    review: ["登记工单回访", "保存回访", "告警工单 " + action.id],
    pass: [state.session.demo ? "确认模拟放行" : "确认人工登记", "人工确认",
      action.entry ? titleFor(action.entry.device_id) + " · " + action.entry.purpose + " · 有效至 " +
        stamp(action.entry.expires_at) : ""],
  };
  const [title, actionLabel, context] = types[action.type];
  workflows.dialog = action;
  $("workflow-form").reset();
  $("workflow-title").textContent = title;
  $("workflow-context").textContent = context;
  $("workflow-submit").textContent = actionLabel;
  $("workflow-note-label").hidden = action.type === "pass";
  $("workflow-form").elements.note.required = action.type !== "pass";
  $("workflow-dialog").showModal();
}

async function loadTools() {
  try {
    const tools = await request("/api/admin/tools");
    if (state.page !== "tools") return;
    workflows.tools = tools;
    $("configured-model").textContent = tools.model || "本地数据工具";
    $("model-compare").disabled = !tools.model;
    $("model-compare").checked = false;
    $("model-rates").hidden = true;
    insert("skill-transport", '<option value="http">HTTP 函数</option><option value="local">本机 MCP</option>' +
      (tools.remote_mcp ? '<option value="remote">内网 MCP</option>' : ""));
    insert("skill-name", Object.entries(tools.skills).map(([id, item]) => '<option value="' + escape(id) +
      '">' + escape(item.label) + "</option>").join(""));
    $("skill-date-label").hidden = !tools.skills[$("skill-name").value]?.day;
    for (const [agent, prompts] of Object.entries(tools.prompts)) {
      $("agent-prompts").elements[agent].value = prompts.join("\n");
      $("agent-prompts").elements["guide_" + agent].value = tools.guidance[agent];
    }
    const roles = ["viewer", "operator", "admin"];
    insert("permission-matrix", table(["权限", ...roles.map((role) => tools.roles[role])], tools.permissions.map((row) =>
      [escape(row.label), ...roles.map((role) => row.roles.includes(role) ? "允许" : "—")])));
  } catch (error) { notify(error.message, "bad"); }
}

$("business-day").addEventListener("change", loadBusiness);
$("business-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-edit]");
  if (!button || workflows.business?.day !== $("business-day").value) return;
  const entry = workflows.business?.records[Number(button.dataset.edit)];
  if (!entry) return;
  const form = $("business-form");
  for (const key of ["kind", "code", "label", "quantity", "source"]) form.elements[key].value = entry[key];
  form.elements.quantity.focus();
});
$("business-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  data.quantity = Number(data.quantity);
  data.day = $("business-day").value;
  if (!data.day) return notify("请先选择统计日期。", "bad");
  withForm(form, async () => {
    await request("/api/business", {method: "PUT", data});
    notify("台账已保存。");
    await loadBusiness();
  });
});
$("refresh-decisions").addEventListener("click", () => refreshDecisions(true));
$("decision-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-adopt]");
  if (!button || button.dataset.forDevice !== state.deviceId) return;
  const path = "/api/decisions/" + encodeURIComponent(button.dataset.forDevice) + "/" + encodeURIComponent(button.dataset.adopt) + "/adopt";
  button.disabled = true;
  try {
    const result = await request(path, {method: "POST"});
    if (result.preview?.status === "draft") openPreview(result.preview);
    else if (result.handoff) {
      notify("建议已提交人工接办。");
      await loadHandoffs();
    } else notify("该建议已有指令处理记录。");
  } catch (error) { notify(error.message, "bad"); }
  finally { button.disabled = false; }
});
$("chat-handoff").addEventListener("click", () => {
  if (state.lastChat && state.lastChat.deviceId === state.deviceId) {
    openWorkDialog({type: "handoff", deviceId: state.deviceId, conversationId: state.lastChat.id});
  }
});
$("create-handoff").addEventListener("click", () => {
  if (state.deviceId) openWorkDialog({type: "handoff", deviceId: state.deviceId});
});
$("handoff-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-resolve]");
  if (button) openWorkDialog({type: "resolve", id: button.dataset.resolve});
});
$("gate-device").addEventListener("change", updateGateState);
$("issue-pass").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  withForm(form, async () => {
    const result = await request("/api/access/issue", {method: "POST", data: {
      device_id: form.elements.device_id.value, purpose: form.elements.purpose.value.trim(), minutes: Number(form.elements.minutes.value),
    }});
    $("issued-code").textContent = result.code + " · 有效至 " + stamp(result.expires_at);
    $("issued-code").hidden = false;
    await loadAccess();
  });
});
$("verify-pass").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  withForm(form, async () => {
    const data = await request("/api/access/verify", {method: "POST", data: {code: form.elements.code.value.trim()}});
    form.reset();
    $("issued-code").hidden = true;
    $("issued-code").textContent = "";
    notify("通行码核验通过，等待人工确认。");
    await loadAccess();
    openWorkDialog({type: "pass", id: data.id, entry: data});
  });
});
$("pass-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-pass]");
  if (!button) return;
  const entry = workflows.access?.passes.find((row) => row.id === button.dataset.pass);
  if (entry) openWorkDialog({type: "pass", id: entry.id, entry});
});
$("workflow-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const action = workflows.dialog;
  if (!action) return;
  const note = form.elements.note.value.trim();
  withForm(form, async () => {
    switch (action.type) {
      case "handoff":
        await request("/api/handoffs", {method: "POST", data: {
          device_id: action.deviceId, reason: note, conversation_id: action.conversationId || null,
        }});
        notify("已进入人工接办。");
        if (state.page === "workbench") await loadHandoffs();
        break;
      case "resolve":
        await request("/api/handoffs/" + action.id + "/resolve", {method: "POST", data: {note}});
        notify("人工处置结果已登记。");
        await loadHandoffs();
        break;
      case "review":
        await request("/api/alerts/" + action.id + "/review", {method: "POST", data: {note}});
        notify("工单回访已登记。");
        await loadAllOrders();
        break;
      case "pass": {
        const result = await request("/api/access/" + action.id + "/confirm", {method: "POST"});
        notify(result.detail, result.status === "rejected" ? "bad" : "good");
        await loadAccess();
        break;
      }
    }
    $("workflow-dialog").close();
  });
});
$("model-compare").addEventListener("change", () => { $("model-rates").hidden = !$("model-compare").checked; });
$("model-test").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  withForm(form, async () => {
    const payload = await request("/api/admin/model-test", {method: "POST", data: {
      device_id: state.deviceId, agent: "auto", text: form.elements.text.value.trim(), compare: form.elements.compare.checked,
      input_rate: Number(form.elements.input_rate.value), output_rate: Number(form.elements.output_rate.value),
    }});
    insert("model-result", table(["方式", "用时", "输入 / 输出 token", "费用估算 元", "结果"], payload.results.map((row) => [
      escape(row.model + (row.engine === "local_fallback" ? " · 降级" : "")), number(row.elapsed_ms) + " ms",
      number(row.usage.input) + " / " + number(row.usage.output),
      row.estimated_cost == null ? "—" : Number(row.estimated_cost).toFixed(8), escape(row.answer),
    ])));
  });
});
$("skill-name").addEventListener("change", () => {
  $("skill-date-label").hidden = !workflows.tools?.skills[$("skill-name").value]?.day;
});
$("skill-test").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const selected = workflows.tools?.skills[form.elements.name.value];
  if (!selected) return;
  withForm(form, async () => {
    const transport = form.elements.transport.value;
    const path = transport === "http" ? "/api/skills/run" : "/api/skills/mcp/" + encodeURIComponent(transport);
    const result = await request(path, {method: "POST", data: {
      name: form.elements.name.value, device_id: selected.device ? state.deviceId : null,
      day: selected.day ? form.elements.day.value || null : null,
    }});
    $("skill-result").hidden = false;
    $("skill-result").textContent = JSON.stringify(result, null, 2);
  });
});
$("agent-prompts").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const roles = ["device", "safety", "energy"];
  const data = Object.fromEntries(roles.map((key) =>
    [key, form.elements[key].value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)]));
  data.guidance = Object.fromEntries(roles.map((role) => [role, form.elements["guide_" + role].value.trim()]));
  withForm(form, async () => {
    state.session.prompts = await request("/api/admin/prompts", {method: "PUT", data});
    renderPrompts();
    notify("常用问法已保存。");
  });
});

setInterval(() => { if (state.session?.user && state.page === "workbench") updateGateState(); }, 450);
start();
