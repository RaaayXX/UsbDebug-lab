/* Power Lab — 前端 */

const MAX_POINTS = 800;
const powerData = { t: [], v: [], i: [], at: [] };
const measureHistory = [];
let chartT0Sec = null;
let serialText = "";
let chart = null;
let socket = null;
let settings = {};
let deviceStatus = {};
let lastAnalysis = null;
let productBriefText = "";
let productBriefFileName = "";

const $ = (id) => document.getElementById(id);

const CHART_THEME = {
  v: "#4a7c9b",
  i: "#5a8f7b",
  grid: "rgba(28, 28, 30, 0.06)",
  tick: "#aeaeb2",
  text: "#636366",
};

/* ── Toast ── */
function toast(level, msg) {
  const root = $("toastRoot");
  const el = document.createElement("div");
  el.className = `toast toast-${level}`;
  el.textContent = msg;
  root.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 300);
  }, 4200);
}

/* ── Gauges ── */
function updateGauges(v, i) {
  const vRing = $("gaugeVRing");
  const iRing = $("gaugeIRing");
  if (v != null) {
    const pct = Math.min(100, (v / 5200) * 100);
    vRing.style.setProperty("--pct", pct);
    $("gaugeVText").textContent = (v / 1000).toFixed(2) + " V";
  }
  if (i != null) {
    const pct = Math.min(100, (i / 2000) * 100);
    iRing.style.setProperty("--pct", pct);
    $("gaugeIText").textContent = String(i);
  }
}

/* ── Chart ── */
function initChart() {
  chart = new Chart($("chartVI").getContext("2d"), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "电压 mV",
          data: [],
          borderColor: CHART_THEME.v,
          backgroundColor: "rgba(74,124,155,0.07)",
          fill: true,
          yAxisID: "yV",
          tension: 0.35,
          pointRadius: 0,
          borderWidth: 2,
        },
        {
          label: "电流 mA",
          data: [],
          borderColor: CHART_THEME.i,
          backgroundColor: "rgba(90,143,123,0.07)",
          fill: true,
          yAxisID: "yI",
          tension: 0.35,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 0 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "bottom",
          labels: { color: CHART_THEME.text, usePointStyle: true, padding: 16 },
        },
      },
      scales: {
        x: {
          ticks: { color: CHART_THEME.tick, maxTicksLimit: 7 },
          grid: { color: CHART_THEME.grid },
          border: { display: false },
        },
        yV: {
          position: "left",
          ticks: { color: CHART_THEME.v },
          grid: { color: CHART_THEME.grid },
          border: { display: false },
        },
        yI: {
          position: "right",
          ticks: { color: CHART_THEME.i },
          grid: { drawOnChartArea: false },
          border: { display: false },
        },
      },
    },
  });
}

function clearPowerChart() {
  powerData.t.length = 0;
  powerData.v.length = 0;
  powerData.i.length = 0;
  powerData.at.length = 0;
  chartT0Sec = null;
  if (!chart) return;
  chart.data.labels = [];
  chart.data.datasets[0].data = [];
  chart.data.datasets[1].data = [];
  chart.update("none");
}

function pushPowerSample(s) {
  if (chartT0Sec === null) chartT0Sec = Date.now() / 1000;
  powerData.t.push(s.t ?? 0);
  powerData.v.push("v" in s ? s.v : null);
  powerData.i.push("i" in s ? s.i : null);
  powerData.at.push(s.at || null);
  if (powerData.t.length > MAX_POINTS) {
    powerData.t.shift();
    powerData.v.shift();
    powerData.i.shift();
    powerData.at.shift();
  }
  chart.data.labels = powerData.t;
  chart.data.datasets[0].data = powerData.v;
  chart.data.datasets[1].data = powerData.i;
  chart.update("none");
  updateGauges("v" in s ? s.v : undefined, "i" in s ? s.i : undefined);
}

function formatSerialLine(line) {
  const s = line || "";
  const bare = s.replace(/\r?\n$/, "");
  if (!bare) return "";
  if (/^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(bare)) {
    return s.endsWith("\n") ? s : `${s}\n`;
  }
  return `[${new Date().toISOString()}] ${bare}\n`;
}

function getSerialText() {
  const el = $("serialLog");
  const shown = el ? el.textContent || "" : "";
  return shown.length > serialText.length ? shown : serialText;
}

function appendSerial(line) {
  const stamped = formatSerialLine(line);
  if (!stamped) return;
  serialText += stamped;
  const el = $("serialLog");
  el.textContent += stamped;
  if ($("chkAutoscroll").checked) el.scrollTop = el.scrollHeight;
}

/* ── Device status bar ── */
function renderDeviceStatus(st) {
  deviceStatus = st || {};
  const serviceOn = socket && socket.connected;
  setStatusItem("service", serviceOn, serviceOn ? "在线" : "离线");
  const hubUi = !!st.hub_available;
  const hubCh = st.hub_channel || parseInt($("selChannel")?.value, 10) || 1;
  setStatusItem(
    "hub",
    hubUi ? st.hub_connected : null,
    hubUi
      ? st.hub_connected
        ? `已连接 · 通道 ${hubCh}`
        : `${st.hub_error || "未连接"} · 通道 ${hubCh}`
      : "未检测到 Hub",
  );
  setStatusItem(
    "serial",
    st.serial_open,
    st.serial_open ? "已连接" : st.esp32_port_configured ? "未打开" : "未配置",
  );
  if (hubUi) {
    const viLabel = viStatusLabel(st);
    setStatusItem("vi", st.vi_running, viLabel);
  }
  renderMeasureButtons(st);
  applyHubAvailabilityUi(st);
  setStatusItem(
    "log",
    st.serial_capturing ? true : null,
    st.serial_capturing ? "场景录制中" : "空闲",
  );
  if (!st.v_sampling && !st.i_sampling && (st.live_v_mv != null || st.live_i_ma != null)) {
    updateGauges(st.live_v_mv, st.live_i_ma);
  }
  applyBatteryUi();
  applyAutoProfileFromStatus(st);
}

function applyAutoDeviceProfile(profileId) {
  const sel = $("selDeviceProfile");
  if (!sel || sel.value !== "auto") return false;
  const id = (profileId || "").trim();
  if (!id || id === "auto") return false;
  const hasOpt = [...sel.options].some((o) => o.value === id);
  if (!hasOpt) return false;
  sel.value = id;
  return true;
}

function applyAutoProfileFromStatus(st) {
  if ($("selDeviceProfile")?.value !== "auto") return;
  const resolved = st?.device_profile_resolved;
  if (st?.device_profile_auto_state === "detected" && resolved) {
    applyAutoDeviceProfile(resolved);
  }
}

function viStatusLabel(st) {
  if (!st.vi_running) return "空闲";
  if (st.v_sampling && st.i_sampling) return "电压·电流采集中";
  if (st.v_sampling) return "电压采集中";
  if (st.i_sampling) return "电流采集中";
  return "采集中";
}

function renderMeasureButtons(st) {
  const vBtn = $("btnMeasureV");
  const iBtn = $("btnMeasureI");
  if (!vBtn || !iBtn) return;
  vBtn.classList.toggle("active", !!st.v_sampling);
  iBtn.classList.toggle("active", !!st.i_sampling);
  vBtn.setAttribute("aria-pressed", st.v_sampling ? "true" : "false");
  iBtn.setAttribute("aria-pressed", st.i_sampling ? "true" : "false");
  if (chart) {
    chart.data.datasets[0].hidden = st.vi_running ? !st.v_sampling : false;
    chart.data.datasets[1].hidden = st.vi_running ? !st.i_sampling : false;
    chart.update("none");
  }
}

function setStatusItem(key, ok, title) {
  const el = document.querySelector(`.status-item[data-key="${key}"]`);
  if (!el) return;
  el.classList.remove("ok", "bad", "idle");
  if (ok === null) el.classList.add("idle");
  else {
    el.classList.toggle("ok", !!ok);
    el.classList.toggle("bad", !ok);
  }
  el.title = title || "";
}

/* ── Ports & settings ── */
async function loadPorts() {
  const res = await fetch("/api/ports");
  const data = await res.json();
  fillPortSelect($("selHubPort"), data.ports, true);
  fillPortSelect($("selEspPort"), data.ports, false);
  if (data.hub_available !== undefined) {
    applyHubAvailabilityUi({ hub_available: data.hub_available });
  }
  return data;
}

function fillPortSelect(sel, ports, hubOnly) {
  const cur = sel.value;
  sel.innerHTML = hubOnly
    ? '<option value="">— 自动扫描 Hub —</option>'
    : '<option value="">— 请选择 —</option>';
  for (const p of ports) {
    if (hubOnly && !p.is_hub_command) continue;
    if (!hubOnly && p.is_hub_command) continue;
    const opt = document.createElement("option");
    opt.value = p.device;
    const tag = p.is_likely_device || p.is_likely_esp32 ? " ★" : "";
    opt.textContent = `${p.device} — ${p.description || "串口"}${tag}`;
    sel.appendChild(opt);
  }
  if (cur) sel.value = cur;
}

let hubAvailable = false;
let deviceProfiles = [];

async function loadDeviceProfiles() {
  try {
    const res = await fetch("/api/device_profiles");
    const data = await res.json();
    deviceProfiles = data.profiles || [];
    fillDeviceProfileSelect();
  } catch {
    deviceProfiles = [{ id: "auto", label: "自动识别", hint: "" }];
    fillDeviceProfileSelect();
  }
}

function fillDeviceProfileSelect() {
  const sel = $("selDeviceProfile");
  if (!sel) return;
  const cur = sel.value || settings.device_profile || "auto";
  sel.innerHTML = "";
  for (const p of deviceProfiles) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.label;
    if (p.hint) opt.title = p.hint;
    sel.appendChild(opt);
  }
  sel.value = cur;
}

function isHubAvailable(st) {
  if (st && st.hub_available !== undefined) return !!st.hub_available;
  return hubAvailable;
}

function getProductBrief() {
  return productBriefText.trim();
}

function renderProductBriefStatus() {
  const el = $("productBriefStatus");
  const clearBtn = $("btnProductBriefClear");
  if (!el) return;
  if (productBriefFileName) {
    el.textContent = `${productBriefFileName} · ${productBriefText.length} 字`;
    if (clearBtn) clearBtn.hidden = false;
  } else if (productBriefText) {
    el.textContent = `已保存本地 · ${productBriefText.length} 字`;
    if (clearBtn) clearBtn.hidden = false;
  } else {
    el.textContent = "未上传";
    if (clearBtn) clearBtn.hidden = true;
  }
}

async function persistProductBrief() {
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      product_brief: productBriefText,
      product_brief_file: productBriefFileName,
    }),
  });
  const data = await res.json();
  if (data.ok && data.settings) settings = data.settings;
}

async function handleProductBriefFile(file) {
  if (!file) return;
  const name = file.name || "";
  if (!/\.(md|markdown)$/i.test(name)) {
    toast("error", "请选择 .md 或 .markdown 文件");
    return;
  }
  if (file.size > 512 * 1024) {
    toast("error", "文件过大，请小于 512 KB");
    return;
  }
  try {
    productBriefText = await file.text();
  } catch {
    toast("error", "无法读取文件");
    return;
  }
  productBriefFileName = name;
  renderProductBriefStatus();
  await persistProductBrief();
  toast("ok", `已加载 ${name}`);
}

function clearProductBrief() {
  productBriefText = "";
  productBriefFileName = "";
  const inp = $("inpProductBriefFile");
  if (inp) inp.value = "";
  renderProductBriefStatus();
  persistProductBrief();
}

function initProductBriefUpload() {
  const pick = $("btnProductBriefPick");
  const inp = $("inpProductBriefFile");
  const clearBtn = $("btnProductBriefClear");
  if (pick && inp) {
    pick.onclick = () => inp.click();
    inp.onchange = () => {
      const file = inp.files && inp.files[0];
      if (file) handleProductBriefFile(file);
    };
  }
  if (clearBtn) clearBtn.onclick = clearProductBrief;
}

function applyHubAvailabilityUi(st) {
  const hub = isHubAvailable(st);
  hubAvailable = hub;
  document.body.classList.toggle("hub-available", hub);
  document.body.classList.toggle("hub-unavailable", !hub);
  const hubStatus = document.querySelector('.status-item[data-key="hub"]');
  if (hubStatus) hubStatus.style.display = hub ? "" : "none";
  const viStatus = document.querySelector('.status-item[data-key="vi"]');
  if (viStatus) viStatus.style.display = hub ? "" : "none";
  const saveReport = $("btnSaveReport");
  if (saveReport) {
    saveReport.textContent = hub
      ? "保存全部（曲线+测试记录+日志）"
      : "保存报告与日志";
  }
  const summary = $("analysisSummary");
  if (summary && !summary.dataset.hasResult) {
    summary.textContent = hub
      ? "填写现象（可选）并点击「智能分析」后，此处显示诊断摘要；保存将全部导出为诊断报告及原始数据。"
      : "填写现象（可选）并点击「智能分析」后，此处显示诊断摘要；保存将导出诊断报告及串口日志。";
  }
}

async function loadSettings() {
  const res = await fetch("/api/settings");
  settings = await res.json();
  if ($("inpProductName")) $("inpProductName").value = settings.product_name || "";
  productBriefText = settings.product_brief || "";
  productBriefFileName = settings.product_brief_file || "";
  renderProductBriefStatus();
  $("selHubPort").value = settings.hub_command_port || "";
  $("selEspPort").value = settings.esp32_serial_port || "";
  if ($("selDeviceProfile")) {
    $("selDeviceProfile").value = settings.device_profile || "auto";
  }
  $("selChannel").value = String(settings.hub_dut_channel || 1);
  $("selBaud").value = String(settings.esp32_baud || 115200);
  if ($("selProductType").options.length) {
    $("selProductType").value = settings.product_type || "battery";
  }
  if ($("inpScenarioRepeat")) {
    $("inpScenarioRepeat").value = String(settings.scenario_repeat_count ?? 5);
  }
  if ($("inpScenarioInterval")) {
    $("inpScenarioInterval").value = String(settings.scenario_cycle_wait_seconds ?? 4);
  }
  if ($("inpBatteryRepeat")) {
    $("inpBatteryRepeat").value = String(settings.scenario_repeat_count ?? 3);
  }
  if ($("inpBatteryDuration")) {
    $("inpBatteryDuration").value = String(settings.scenario_battery_only_seconds ?? 120);
  }
  renderScenarioSelect(settings.active_scenario || "usb_power_cycle");
  updateProductHint();
  updateScenarioHint();
  updateScenarioParams();
  $("chkAi").checked = settings.ai_enabled !== false;
  $("inpApiBase").value =
    settings.openai_base_url || "https://api.chr1.com/v1";
  if ($("inpApiModel")) {
    $("inpApiModel").value = settings.openai_model || "deepseek-v4-flash";
  }
  if (settings.openai_api_key_set) {
    $("inpApiKey").placeholder = "已保存本机；留空则沿用已保存的 Key";
  } else {
    $("inpApiKey").placeholder = "必填，与 Chatbox 中「自定义 API 密钥」相同";
  }
}

function buildPowerPayload() {
  if (!powerData.t.length) return [];
  return powerData.t.map((t, i) => ({
    t,
    v: powerData.v[i],
    i: powerData.i[i],
    at: powerData.at[i] || undefined,
    snapshot: !!powerData.at[i],
  }));
}

function buildSaveBody(extra = {}) {
  const body = { ...extra };
  if (chartT0Sec != null) body.chart_t0 = chartT0Sec;
  return body;
}

async function saveVI() {
  const samples = buildPowerPayload();
  const body = buildSaveBody({
    power: samples.length ? samples : undefined,
    measures: measureHistory.length ? measureHistory : undefined,
  });
  const res = await fetch("/api/save/vi", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!data.ok) {
    toast("error", data.msg || "保存失败");
    return;
  }
  const parts = [];
  if (data.voltage?.path) parts.push(`电压: ${data.voltage.path}`);
  if (data.current?.path) parts.push(`电流: ${data.current.path}`);
  toast("ok", `已保存 ${data.saved_at}\n${parts.join("\n") || data.directory}`);
}

async function saveSerial() {
  if (!getSerialText().trim()) {
    toast("warn", "暂无串口日志");
    return;
  }
  const res = await fetch("/api/save/serial", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ serial: getSerialText() }),
  });
  const data = await res.json();
  if (data.ok) toast("ok", `已保存 ${data.saved_at}\n${data.path}`);
  else toast("error", data.msg || "保存失败");
}

async function saveReport() {
  const samples = buildPowerPayload();
  if (
    !lastAnalysis &&
    !getSerialText().trim() &&
    !samples.length &&
    !getUserObservation()
  ) {
    toast("warn", "请先执行「智能分析」，或填写现象并确保有日志/曲线数据");
    return;
  }
  toast("info", "正在生成诊断报告…");
  const body = buildSaveBody({
    analysis: lastAnalysis || undefined,
    power: samples.length ? samples : undefined,
    serial: getSerialText(),
    measures: measureHistory.length ? measureHistory : undefined,
    ai_enabled: $("chkAi").checked,
    ...analysisPayloadExtra(),
  });
  const key = $("inpApiKey").value.trim();
  if (key) body.openai_api_key = key;
  body.openai_base_url = $("inpApiBase").value.trim();
  body.openai_model = ($("inpApiModel")?.value || "").trim() || "deepseek-v4-flash";
  const res = await fetch("/api/save/report", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (data.ok) {
    if (!lastAnalysis && data.analysis) {
      lastAnalysis = data.analysis;
      renderAnalysis(data.analysis);
    }
    toast("ok", `报告已保存 ${data.saved_at}\n${formatSaveResult(data)}`);
  } else toast("error", data.msg || "保存失败");
}

function getSelectedScenarioId() {
  return $("selScenario")?.value || settings.active_scenario || "usb_power_cycle";
}

function getScenarioParams() {
  const scenarioId = getSelectedScenarioId();
  if (scenarioId === "battery_only_serial") {
    const repeat = parseInt($("inpBatteryRepeat")?.value, 10);
    const duration = parseInt($("inpBatteryDuration")?.value, 10);
    return {
      active_scenario: scenarioId,
      scenario_repeat_count: Number.isFinite(repeat) && repeat >= 1 ? repeat : 3,
      scenario_battery_only_seconds:
        Number.isFinite(duration) && duration >= 10 ? duration : 120,
    };
  }
  const repeat = parseInt($("inpScenarioRepeat")?.value, 10);
  const interval = parseInt($("inpScenarioInterval")?.value, 10);
  return {
    active_scenario: scenarioId,
    scenario_repeat_count: Number.isFinite(repeat) && repeat >= 1 ? repeat : 5,
    scenario_cycle_wait_seconds: Number.isFinite(interval) && interval >= 0 ? interval : 4,
  };
}

function updateScenarioParams() {
  const scenarioId = getSelectedScenarioId();
  const usbParams = $("scenarioParamsUsb");
  const batteryParams = $("scenarioParamsBattery");
  if (usbParams) usbParams.hidden = scenarioId !== "usb_power_cycle";
  if (batteryParams) batteryParams.hidden = scenarioId !== "battery_only_serial";
}

function collectSettings() {
  return {
    product_name: ($("inpProductName")?.value || "").trim(),
    product_brief: getProductBrief(),
    product_brief_file: productBriefFileName,
    hub_command_port: $("selHubPort").value,
    hub_dut_channel: parseInt($("selChannel").value, 10),
    esp32_serial_port: $("selEspPort").value,
    esp32_baud: parseInt($("selBaud").value, 10),
    device_profile: $("selDeviceProfile")?.value || "auto",
    product_type: $("selProductType")?.value || "battery",
    ...getScenarioParams(),
    ai_enabled: $("chkAi").checked,
    openai_base_url: $("inpApiBase").value.trim(),
    openai_model: ($("inpApiModel")?.value || "").trim() || "deepseek-v4-flash",
    connect_hub: true,
  };
}

function hubShortcutPayload(extra = {}) {
  return { ...collectSettings(), ...extra };
}

function analysisPayloadExtra() {
  return {
    user_observation: getUserObservation(),
    product_name: ($("inpProductName")?.value || "").trim(),
    product_brief: getProductBrief(),
    product_brief_file: productBriefFileName,
    hub_available: hubAvailable,
    hub_connected: !!deviceStatus.hub_connected,
    device_profile: $("selDeviceProfile")?.value || settings.device_profile || "auto",
  };
}

async function applySettings() {
  const body = collectSettings();
  const key = $("inpApiKey").value.trim();
  if (key) body.openai_api_key = key;
  toast("info", "正在应用配置…");
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (data.ok) {
    settings = data.settings;
    if ($("selDeviceProfile")?.value === "auto" && $("selEspPort")?.value) {
      const portRes = await fetch("/api/ports");
      const portData = await portRes.json();
      applyPortProfileHint(portData.ports, $("selEspPort").value);
    }
    toast("ok", data.message || "配置已应用");
  } else {
    toast("error", "保存失败");
  }
}

function renderProductTypes() {
  const sel = $("selProductType");
  if (!sel) return;
  const types = window.POWER_LAB_PRODUCT_TYPES || [];
  sel.innerHTML = "";
  for (const t of types) {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = t.label;
    sel.appendChild(opt);
  }
  sel.onchange = () => {
    updateProductHint();
    renderScenarioSelect();
    updateScenarioHint();
    updateScenarioParams();
    applySettings();
  };
}

function renderScenarioSelect(preferredId) {
  const sel = $("selScenario");
  if (!sel) return;
  const productId = $("selProductType")?.value || settings.product_type || "battery";
  const scenarios = (window.POWER_LAB_SCENARIOS || []).filter(
    (sc) =>
      sc.enabled !== false &&
      (!sc.products || sc.products.length === 0 || sc.products.includes(productId)),
  );
  const prev = preferredId || sel.value || settings.active_scenario || "usb_power_cycle";
  sel.innerHTML = "";
  for (const sc of scenarios) {
    const opt = document.createElement("option");
    opt.value = sc.id;
    opt.textContent = sc.title;
    sel.appendChild(opt);
  }
  const ids = scenarios.map((s) => s.id);
  sel.value = ids.includes(prev) ? prev : ids[0] || "usb_power_cycle";
  sel.onchange = () => {
    updateScenarioHint();
    updateScenarioParams();
    applySettings();
  };
  updateScenarioParams();
}

function updateProductHint() {
  const id = $("selProductType")?.value;
  const t = (window.POWER_LAB_PRODUCT_TYPES || []).find((x) => x.id === id);
  const el = $("productHint");
  if (el) el.textContent = t ? t.hint : "";
}

function updateScenarioHint() {
  const id = getSelectedScenarioId();
  const sc = (window.POWER_LAB_SCENARIOS || []).find((s) => s.id === id);
  const el = $("scenarioHint");
  if (el) el.textContent = sc ? sc.hint || "" : "";
}

function getScenarioById(id) {
  return (window.POWER_LAB_SCENARIOS || []).find((s) => s.id === id);
}

/* ── Analysis UI ── */
const OBS_STORAGE_KEY = "powerlab_user_observation";

function getUserObservation() {
  const el = $("inpUserObservation");
  return el ? el.value.trim() : "";
}

function loadUserObservation() {
  const el = $("inpUserObservation");
  if (!el) return;
  try {
    const saved = localStorage.getItem(OBS_STORAGE_KEY);
    if (saved) el.value = saved;
  } catch (_) {}
}

function saveUserObservation() {
  try {
    localStorage.setItem(OBS_STORAGE_KEY, getUserObservation());
  } catch (_) {}
}

function formatSaveResult(data) {
  const parts = [];
  if (data.report_path) parts.push(`报告: ${data.report_path}`);
  else if (data.directory) parts.push(data.directory);
  if (data.power?.path) parts.push(`曲线: ${data.power.path} (${data.power.rows || 0} 行)`);
  if (data.measure?.path) parts.push(`采样: ${data.measure.path} (${data.measure.rows || 0} 行)`);
  if (data.serial?.path) parts.push(`日志: ${data.serial.path}`);
  if (!data.power?.path && hubAvailable) {
    parts.push("提示: 未包含电压/电流曲线，请先点击「测电压/测电流」或运行场景后再保存");
  }
  return parts.join("\n");
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function formatAnalysisText(text) {
  const raw = (text == null ? "" : String(text)).trim();
  if (!raw || raw === "—") return '<span class="analysis-muted">—</span>';
  const lines = raw.split(/\n+/).map((l) => l.trim()).filter(Boolean);
  if (lines.length >= 2 && lines.every((l) => /^\d+[\.\)、]\s*/.test(l))) {
    const items = lines.map((l) => l.replace(/^\d+[\.\)、]\s*/, ""));
    return `<ol class="analysis-list">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ol>`;
  }
  if (lines.length >= 2 && lines.every((l) => /^[-*•]\s+/.test(l))) {
    const items = lines.map((l) => l.replace(/^[-*•]\s+/, ""));
    return `<ul class="analysis-list">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;
  }
  return lines.map((l) => `<p class="analysis-para">${esc(l)}</p>`).join("");
}

function renderFindingCard(f, extraClass = "") {
  const div = document.createElement("div");
  div.className = `finding finding-${f.severity || "info"} ${extraClass}`.trim();
  const sev = f.severity_label || f.severity || "提示";
  const logOrigin = f.log_origin || "";
  const logLoc = f.log_location || "";
  const logExcerpt = f.log_excerpt || f.evidence || "";
  const logSourceLine =
    logOrigin && logLoc
      ? `${logOrigin} · ${logLoc}`
      : logOrigin || logLoc || "";
  div.innerHTML = `
    <div class="finding-title"><span class="finding-sev">${esc(sev)}</span> ${esc(f.message || f.title || "")}</div>
    <dl class="finding-detail">
      <dt>发现来源</dt><dd class="finding-body">${formatAnalysisText(f.source || "—")}</dd>
      ${logSourceLine ? `<dt>日志来源</dt><dd class="finding-body">${esc(logSourceLine)}</dd>` : ""}
      <dt>可能原因</dt><dd class="finding-body">${formatAnalysisText(f.likely_cause || "—")}</dd>
      <dt>建议排查</dt><dd class="finding-body finding-actions">${formatAnalysisText(f.recommendation || "—")}</dd>
      ${logExcerpt ? `<dt>匹配日志</dt><dd class="finding-evidence"><pre class="finding-log-line">${esc(logExcerpt)}</pre></dd>` : ""}
    </dl>`;
  return div;
}

function renderAnalysis(data) {
  lastAnalysis = data;
  const summaryEl = $("analysisSummary");
  summaryEl.textContent = data.summary || "";
  summaryEl.classList.toggle("has-result", !!data.summary);
  summaryEl.dataset.hasResult = data.summary ? "1" : "";
  const aiHint = $("analysisAiHint");
  if (aiHint) {
    const skip = data.ai_skip_reason || "";
    if (skip) {
      aiHint.hidden = false;
      aiHint.textContent = skip;
    } else {
      aiHint.hidden = true;
      aiHint.textContent = "";
    }
  }
  const list = $("findingsList");
  list.innerHTML = "";

  const ai = data.ai_structured;
  if (ai) {
    list.appendChild(
      renderFindingCard(
        {
          severity: "info",
          severity_label: "AI 分析",
          message: ai.title,
          source: ai.source,
          likely_cause: ai.likely_cause,
          recommendation: ai.recommendation,
          evidence: ai.evidence,
        },
        "finding-ai-primary",
      ),
    );
  }

  const ruleFindings = (data.findings || []).filter((f) => f.category !== "ai");
  if (!ai) {
    const notable = ruleFindings.filter(
      (f) => f.severity === "critical" || f.severity === "warning",
    );
    const shown = notable.length ? notable : ruleFindings.slice(0, 3);
    if (!shown.length) {
      list.innerHTML = '<p class="hint-empty">未发现规则匹配项</p>';
    } else {
      for (const f of shown) list.appendChild(renderFindingCard(f));
    }
  } else if (ruleFindings.length) {
    const ref = document.createElement("details");
    ref.className = "findings-ref";
    ref.innerHTML = `<summary>规则检出参考（${ruleFindings.length} 项）</summary>`;
    const inner = document.createElement("div");
    inner.className = "findings findings-nested";
    for (const f of ruleFindings) inner.appendChild(renderFindingCard(f));
    ref.appendChild(inner);
    list.appendChild(ref);
  }

  const badge = $("analysisBadge");
  badge.className = "status-chip";
  const allForBadge = data.findings || [];
  const c = allForBadge.filter((x) => x.severity === "critical").length;
  const w = allForBadge.filter((x) => x.severity === "warning").length;
  if (c) {
    badge.textContent = `${c} 严重`;
    badge.classList.add("chip-crit");
  } else if (w) {
    badge.textContent = `${w} 警告`;
    badge.classList.add("chip-warn");
  } else if (ai) {
    badge.textContent = "AI 已分析";
    badge.classList.add("chip-ok");
  } else {
    badge.textContent = "正常";
    badge.classList.add("chip-ok");
  }

  const aiBlock = $("aiDetails");
  if (data.ai_text && ai) {
    aiBlock.hidden = false;
    const el = $("aiText");
    if (typeof marked !== "undefined") {
      marked.setOptions({ gfm: true, breaks: true });
      el.innerHTML = marked.parse(data.ai_text);
    } else {
      el.textContent = data.ai_text;
    }
  } else if (data.ai_text && !ai) {
    aiBlock.hidden = false;
    const el = $("aiText");
    if (typeof marked !== "undefined") {
      marked.setOptions({ gfm: true, breaks: true });
      el.innerHTML = marked.parse(data.ai_text);
    } else {
      el.textContent = data.ai_text;
    }
  } else {
    aiBlock.hidden = true;
  }
}

let analysisProgressTimer = null;

function setAnalysisLoading(loading, opts = {}) {
  const btn = $("btnAnalyze");
  const spinner = $("btnAnalyzeSpinner");
  const label = $("btnAnalyzeLabel");
  const panel = $("analysisProgress");
  const bar = $("analysisBarFill");
  const step = $("analysisStepText");
  const barWrap = panel?.querySelector(".analysis-bar");
  const badge = $("analysisBadge");

  if (analysisProgressTimer) {
    clearInterval(analysisProgressTimer);
    analysisProgressTimer = null;
  }

  if (!btn) return;

  if (!loading) {
    btn.disabled = false;
    btn.classList.remove("btn-analyzing");
    if (spinner) spinner.hidden = true;
    if (label) label.textContent = "智能分析";
    if (panel) panel.hidden = true;
    if (bar) {
      bar.style.width = "0";
      bar.classList.remove("is-indeterminate");
    }
    if (barWrap) barWrap.setAttribute("aria-valuenow", "0");
    return;
  }

  const aiOn = opts.aiEnabled !== false && ($("chkAi")?.checked ?? true);
  const stages = aiOn
    ? [
        { pct: 12, text: "正在读取日志与曲线…" },
        { pct: 32, text: "规则检测中…" },
        { pct: 55, text: "AI 分析中，请稍候…" },
        { pct: 78, text: "正在整理诊断结论…" },
        { pct: 90, text: "等待 AI 响应（可能需 30–60 秒）…" },
      ]
    : [
        { pct: 15, text: "正在读取日志与曲线…" },
        { pct: 45, text: "规则检测中…" },
        { pct: 75, text: "正在生成诊断摘要…" },
        { pct: 90, text: "即将完成…" },
      ];

  btn.disabled = true;
  btn.classList.add("btn-analyzing");
  if (spinner) spinner.hidden = false;
  if (label) label.textContent = "分析中…";
  if (panel) panel.hidden = false;
  if (badge) {
    badge.textContent = "分析中";
    badge.className = "status-chip chip-warn";
  }

  let stageIdx = 0;
  const applyStage = (idx) => {
    const s = stages[Math.min(idx, stages.length - 1)];
    if (bar) {
      bar.style.width = `${s.pct}%`;
      bar.classList.add("is-indeterminate");
    }
    if (barWrap) barWrap.setAttribute("aria-valuenow", String(s.pct));
    if (step) step.textContent = s.text;
  };

  applyStage(0);
  analysisProgressTimer = setInterval(() => {
    stageIdx += 1;
    applyStage(stageIdx);
  }, aiOn ? 4500 : 2500);
}

async function runAnalyze() {
  const obs = getUserObservation();
  if (!getSerialText().trim() && !powerData.t.length && !obs) {
    toast("warn", "请先录制日志/曲线，或填写观察到的现象");
    return;
  }
  saveUserObservation();
  const aiOn = $("chkAi").checked;
  setAnalysisLoading(true, { aiEnabled: aiOn });
  try {
    const body = {
      serial: getSerialText(),
      power: buildPowerPayload(),
      ai_enabled: aiOn,
      ...analysisPayloadExtra(),
    };
    const key = $("inpApiKey").value.trim();
    if (key) body.openai_api_key = key;
    body.openai_base_url = $("inpApiBase").value.trim();
    body.openai_model = ($("inpApiModel")?.value || "").trim() || "deepseek-v4-flash";
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    const bar = $("analysisBarFill");
    const barWrap = $("analysisProgress")?.querySelector(".analysis-bar");
    const step = $("analysisStepText");
    if (bar) {
      bar.style.width = "100%";
      bar.classList.remove("is-indeterminate");
    }
    if (barWrap) barWrap.setAttribute("aria-valuenow", "100");
    if (step) step.textContent = "分析完成";
    renderAnalysis(data);
    lastAnalysis = data;
    if (data.ai_used && data.ai_text) {
      toast("ok", "分析完成（含 AI 说明）");
    } else if (data.ai_skip_reason) {
      toast("warn", `规则分析完成；${data.ai_skip_reason}`);
    } else {
      toast("ok", "规则分析完成");
    }
    await new Promise((r) => setTimeout(r, 400));
  } catch (err) {
    toast("error", "分析失败，请检查网络或服务状态");
  } finally {
    setAnalysisLoading(false);
  }
}

/* ── Scenario progress ── */
function showScenarioProgress(show) {
  $("scenarioProgress").hidden = !show;
}

function setScenarioProgress(pct, text) {
  $("scenarioBarFill").style.width = `${pct}%`;
  $("scenarioStepText").textContent = text;
}

/* ── Socket ── */
function connectSocket() {
  socket = io({ transports: ["polling", "websocket"] });

  socket.on("connect", () => {
    renderDeviceStatus({ ...deviceStatus });
    toast("ok", "调试服务已连接");
  });
  socket.on("disconnect", () => {
    renderDeviceStatus({});
    toast("error", "与调试服务断开");
  });

  socket.on("device_status", renderDeviceStatus);
  socket.on("toast", (d) => toast(d.level || "info", d.msg || ""));
  socket.on("power_sample", pushPowerSample);
  socket.on("hub_measure_result", (row) => {
    measureHistory.push(row);
    if (measureHistory.length > 500) measureHistory.shift();
  });
  socket.on("measure_state", (d) => {
    if (d.reset_chart) clearPowerChart();
    renderDeviceStatus({
      ...deviceStatus,
      v_sampling: d.v_sampling,
      i_sampling: d.i_sampling,
      vi_running: d.v_sampling || d.i_sampling,
    });
  });
  socket.on("serial_line", (d) => appendSerial(d.line || ""));
  socket.on("reboot_hint", () => {});

  socket.on("capture_state", (d) => {
    if (d.serial_capturing !== undefined || d.vi_running !== undefined) {
      renderDeviceStatus({
        ...deviceStatus,
        serial_capturing: d.serial_capturing ?? deviceStatus.serial_capturing,
        vi_running: d.vi_running ?? deviceStatus.vi_running,
      });
    }
    if (d.msg && !d.ok) toast("error", d.msg);
  });

  socket.on("scenario_progress", (d) => {
    showScenarioProgress(true);
    setScenarioProgress(d.pct || 0, `${d.step}: ${d.detail || ""}`);
    if (d.step === "analyze") {
      setAnalysisLoading(true, { aiEnabled: $("chkAi")?.checked });
    }
  });

  socket.on("analysis_result", (data) => {
    setAnalysisLoading(false);
    lastAnalysis = data;
    renderAnalysis(data);
    showScenarioProgress(false);
  });
}

/* ── Bind ── */
let guideLoaded = false;

async function loadGuideMarkdown() {
  const el = $("guideContent");
  try {
    const res = await fetch("/api/guide");
    const data = await res.json();
    if (!data.ok || !data.markdown) {
      el.innerHTML = "<p>无法加载使用指南。</p>";
      return;
    }
    if (typeof marked !== "undefined") {
      marked.setOptions({ gfm: true, breaks: true });
      el.innerHTML = marked.parse(data.markdown);
    } else {
      el.textContent = data.markdown;
    }
    guideLoaded = true;
  } catch (e) {
    el.innerHTML = `<p>加载失败：${esc(e.message)}</p>`;
  }
}

function openGuide() {
  const overlay = $("guideOverlay");
  overlay.hidden = false;
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("guide-open");
  if (!guideLoaded) loadGuideMarkdown();
}

function closeGuide() {
  const overlay = $("guideOverlay");
  overlay.hidden = true;
  overlay.setAttribute("aria-hidden", "true");
  document.body.classList.remove("guide-open");
}

/* ── 场景 & 说明弹层 ── */
function renderHubHelpHtml() {
  const cmds = window.POWER_LAB_HUB_COMMANDS || [];
  const groups = ["通道电源", "USB 数据", "采样"];
  let html = '<div class="help-cmd-list">';
  for (const g of groups) {
    const items = cmds.filter((c) => c.group === g);
    if (!items.length) continue;
    html += `<div class="help-group"><span class="help-group-tag">${esc(g)}</span>`;
    for (const c of items) {
      html += `<div class="help-cmd-row"><span class="help-cmd-name">${esc(c.cmd)}</span><span class="help-cmd-desc">${esc(c.desc)}</span></div>`;
    }
    html += "</div>";
  }
  html += "</div>";
  return html;
}

function renderScenarioHelpHtml(help) {
  if (!help) return "<p>暂无说明</p>";
  let html = "";
  if (help.goal) {
    html += `<p class="help-lead">${esc(help.goal)}</p>`;
  }
  if (help.byProduct?.length) {
    html += '<div class="help-block"><span class="help-block-label">插拔方式</span><div class="help-kv-list">';
    for (const row of help.byProduct) {
      html += `<div class="help-kv"><span>${esc(row.type)}</span><span>${esc(row.action)}</span></div>`;
    }
    html += "</div></div>";
  }
  if (help.flow?.length) {
    html += '<div class="help-block"><span class="help-block-label">流程</span><ol class="help-steps">';
    for (const step of help.flow) {
      html += `<li>${esc(step)}</li>`;
    }
    html += "</ol></div>";
  }
  if (help.note) {
    html += `<p class="help-note">${esc(help.note)}</p>`;
  }
  return html;
}

function openHelpPopover(title, content) {
  $("helpPopoverTitle").textContent = title;
  const body = $("helpPopoverBody");
  body.className = "help-popover-body";
  body.classList.remove("prose");
  if (typeof content === "string" && content.startsWith("<")) {
    body.innerHTML = content;
  } else if (content && typeof content === "object" && content.goal) {
    body.innerHTML = renderScenarioHelpHtml(content);
  } else if (typeof content === "string" && typeof marked !== "undefined") {
    body.classList.add("prose");
    body.innerHTML = marked.parse(content);
  } else {
    body.textContent = String(content || "");
  }
  const pop = $("helpPopover");
  pop.hidden = false;
  pop.setAttribute("aria-hidden", "false");
}

function closeHelpPopover() {
  const pop = $("helpPopover");
  pop.hidden = true;
  pop.setAttribute("aria-hidden", "true");
}

function renderScenarioHelpButton() {
  const btn = $("btnScenarioHelp");
  if (!btn) return;
  btn.onclick = () => {
    const sc = getScenarioById(getSelectedScenarioId());
    if (sc) openHelpPopover(sc.title, sc.help || sc.detail);
  };
}

async function runSelectedScenario() {
  const id = getSelectedScenarioId();
  const sc = getScenarioById(id);
  const title = sc ? sc.title : id;
  const params = getScenarioParams();
  const repeats = params.scenario_repeat_count;
  let confirmMsg = sc?.confirm;
  if (!confirmMsg) {
    if (id === "battery_only_serial") {
      const batteryS = params.scenario_battery_only_seconds ?? 120;
      confirmMsg =
        `即将运行场景「${title}」。\n\n` +
        `将切断 Hub VBUS ${repeats} 轮（每轮仅电池运行 ${batteryS}s），` +
        `USB 数据保持连通以便持续看串口，并录制 V/I 曲线。期间请勿烧录。是否继续？`;
    } else {
      const interval = params.scenario_cycle_wait_seconds ?? 4;
      confirmMsg =
        `即将运行场景「${title}」。\n\n` +
        `将重复 USB 插拔 ${repeats} 次（间隔 ${interval}s），` +
        `并联合录制串口日志与 V/I 曲线。期间请勿烧录。是否继续？`;
    }
  }
  if (!confirm(confirmMsg)) return;
  await applySettings();
  showScenarioProgress(true);
  setScenarioProgress(0, "准备…");
  const payload = {
    name: id,
    refresh_serial: true,
    repeat_count: repeats,
    active_scenario: id,
  };
  if (id === "battery_only_serial") {
    payload.battery_only_seconds = params.scenario_battery_only_seconds ?? 120;
  } else {
    payload.cycle_wait_seconds = params.scenario_cycle_wait_seconds ?? 4;
  }
  socket.emit("run_scenario", hubShortcutPayload(payload));
}

function bindUi() {
  initProductBriefUpload();
  $("btnGuide").onclick = openGuide;
  $("btnGuideClose").onclick = closeGuide;
  $("guideOverlay").onclick = (e) => {
    if (e.target === $("guideOverlay")) closeGuide();
  };
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("helpPopover").hidden) closeHelpPopover();
    else if (!$("guideOverlay").hidden) closeGuide();
  });

  $("btnHubHelp").onclick = () => {
    openHelpPopover("快捷指令", renderHubHelpHtml());
  };
  $("btnHelpPopoverClose").onclick = closeHelpPopover;
  $("btnHelpPopoverOk").onclick = closeHelpPopover;
  $("helpPopover").onclick = (e) => {
    if (e.target === $("helpPopover")) closeHelpPopover();
  };

  $("btnRefreshPorts").onclick = async () => {
    const portData = await loadPorts();
    const s = portData.suggest || {};
    if (!$("selHubPort").value && s.hub_command_port) $("selHubPort").value = s.hub_command_port;
    if (!$("selEspPort").value && s.esp32_serial_port) $("selEspPort").value = s.esp32_serial_port;
    if ($("selDeviceProfile")?.value === "auto" && $("selEspPort")?.value) {
      applyPortProfileHint(portData.ports, $("selEspPort").value);
    }
    toast("ok", "端口列表已刷新");
  };

  $("btnApply").onclick = applySettings;

  const btnRunScenario = $("btnRunScenario");
  if (btnRunScenario) btnRunScenario.onclick = () => runSelectedScenario();
  renderScenarioHelpButton();

  $("btnHubDisconnect").onclick = async () => {
    await fetch("/api/hub/disconnect", { method: "POST" });
    toast("info", "Hub 已断开");
  };

  $("btnPowerOn").onclick = () =>
    socket.emit("hub_power", hubShortcutPayload({ on: true, refresh_serial: false }));
  $("btnPowerOff").onclick = () =>
    socket.emit("hub_power", hubShortcutPayload({ on: false, refresh_serial: false }));
  $("btnDataOn").onclick = () =>
    socket.emit("hub_dataline", hubShortcutPayload({ on: true, refresh_serial: false }));
  $("btnDataOff").onclick = () =>
    socket.emit("hub_dataline", hubShortcutPayload({ on: false, refresh_serial: false }));
  $("btnVbusOnlyOff").onclick = () =>
    socket.emit("hub_vbus_only_off", hubShortcutPayload({ refresh_serial: false }));
  $("btnHardReboot").onclick = () =>
    socket.emit("hub_reboot", hubShortcutPayload({ refresh_serial: true }));
  $("btnHubTest").onclick = () =>
    socket.emit("run_scenario", hubShortcutPayload({ name: "hub_self_test", refresh_serial: true }));
  $("btnMeasureV").onclick = () =>
    socket.emit("hub_measure", hubShortcutPayload({ kind: "voltage", refresh_serial: false }));
  $("btnMeasureI").onclick = () =>
    socket.emit("hub_measure", hubShortcutPayload({ kind: "current", refresh_serial: false }));

  $("btnAnalyze").onclick = runAnalyze;
  const obsEl = $("inpUserObservation");
  if (obsEl) {
    let obsTimer;
    obsEl.addEventListener("input", () => {
      clearTimeout(obsTimer);
      obsTimer = setTimeout(saveUserObservation, 400);
    });
  }
  $("btnSaveVI").onclick = saveVI;
  $("btnSaveSerial").onclick = saveSerial;
  $("btnSaveReport").onclick = saveReport;
  $("btnClearLog").onclick = () => {
    serialText = "";
    $("serialLog").textContent = "";
    toast("info", "日志已清空");
  };
}

function applyPortProfileHint(ports, deviceName) {
  if ($("selDeviceProfile")?.value !== "auto") return;
  const port = (ports || []).find((p) => p.device === deviceName);
  if (port?.device_profile_hint) {
    applyAutoDeviceProfile(port.device_profile_hint);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  initChart();
  loadUserObservation();
  renderProductTypes();
  bindUi();
  connectSocket();
  await loadDeviceProfiles();
  await loadSettings();
  const portData = await loadPorts();
  const suggest = portData.suggest || {};
  if (suggest.hub_command_port && !$("selHubPort").value) {
    $("selHubPort").value = suggest.hub_command_port;
  }
  if (suggest.esp32_serial_port && !$("selEspPort").value) {
    $("selEspPort").value = suggest.esp32_serial_port;
  }
  if ($("selDeviceProfile")?.value === "auto") {
    if (suggest.device_profile_hint) {
      applyAutoDeviceProfile(suggest.device_profile_hint);
    } else if ($("selEspPort")?.value) {
      applyPortProfileHint(portData.ports, $("selEspPort").value);
    }
  }
  $("selEspPort")?.addEventListener("change", async () => {
    const res = await fetch("/api/ports");
    const data = await res.json();
    applyPortProfileHint(data.ports, $("selEspPort").value);
  });
  $("selDeviceProfile")?.addEventListener("change", async () => {
    if ($("selDeviceProfile").value === "auto" && $("selEspPort")?.value) {
      const res = await fetch("/api/ports");
      const data = await res.json();
      applyPortProfileHint(data.ports, $("selEspPort").value);
    }
  });
  updateGauges(null, null);
});
