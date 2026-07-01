const state = {
  settings: null,
  section: "usage",
};

const $ = (id) => document.getElementById(id);

function setStatus(text) {
  $("statusText").textContent = text;
  $("settingsPageStatus").textContent = text;
}

function setStatusBusy(busy) {
  $("statusSpinner").hidden = !busy;
  $("statusText").classList.toggle("status-busy-text", busy);
  $("statusText").setAttribute("aria-busy", busy ? "true" : "false");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function renderUsageSettings() {
  const usage = state.settings?.usageStats || {};
  $("usageEnabled").checked = usage.enabled !== false;
  $("usageDailyEnabled").checked = usage.dailyEnabled !== false;
  $("usageDailyHour").value = usage.dailyHour ?? 3;
  $("usageDailyMinute").value = usage.dailyMinute ?? 0;
  $("usageStaleDays").value = usage.staleDays ?? 30;
  $("usageMaxFiles").value = usage.maxFiles ?? 1000;
  $("usageScope").value = usage.scope || "all";
  $("usageIncludeSystem").checked = usage.includeSystem !== false;
  applyUsageEnabledState();
}

function renderPaths() {
  const paths = state.settings?.paths || {};
  const repository = state.settings?.repository || {};
  const rows = [
    ["设置文件", paths.settings],
    ["统计缓存", paths.usageStats],
    ["Skills 仓库", repository.skillsRepoDir],
    ["Skills 目录", repository.skillsDir],
    ["会话记录", paths.sessions],
    ["归档会话", paths.archivedSessions],
  ];
  $("settingsPaths").replaceChildren(
    ...rows.flatMap(([label, value]) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value || "未配置";
      return [dt, dd];
    }),
  );
}

function applyUsageEnabledState() {
  const enabled = $("usageEnabled").checked;
  for (const id of ["usageDailyEnabled", "usageDailyHour", "usageDailyMinute", "usageStaleDays", "usageMaxFiles", "usageScope", "usageIncludeSystem"]) {
    $(id).disabled = !enabled;
  }
}

function renderSection() {
  const usageActive = state.section === "usage";
  $("settingsTitle").textContent = usageActive ? "使用统计" : "路径";
  $("settingsDescription").textContent = usageActive
    ? "配置 skills 使用频率分析统计是否启用，以及缓存刷新时读取哪些本机会话。"
    : "查看当前设置文件、统计缓存和本机会话目录。";
  $("usageSettingsPanel").hidden = !usageActive;
  $("pathsSettingsPanel").hidden = usageActive;
  $("saveSettingsButton").hidden = !usageActive;
  document.querySelectorAll(".review-type").forEach((button) => {
    button.classList.toggle("active", button.dataset.section === state.section);
  });
}

function render() {
  renderUsageSettings();
  renderPaths();
  renderSection();
}

function collectUsageSettings() {
  return {
    enabled: $("usageEnabled").checked,
    dailyEnabled: $("usageDailyEnabled").checked,
    dailyHour: $("usageDailyHour").value,
    dailyMinute: $("usageDailyMinute").value,
    staleDays: $("usageStaleDays").value,
    maxFiles: $("usageMaxFiles").value,
    scope: $("usageScope").value,
    includeSystem: $("usageIncludeSystem").checked,
  };
}

async function loadSettings() {
  setStatusBusy(true);
  setStatus("正在读取设置");
  try {
    state.settings = await api("/api/settings");
    render();
    setStatus("准备就绪");
  } finally {
    setStatusBusy(false);
  }
}

async function saveSettings() {
  $("saveSettingsButton").disabled = true;
  setStatusBusy(true);
  setStatus("正在保存设置");
  try {
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ usageStats: collectUsageSettings() }),
    });
    render();
    setStatus("设置已保存");
  } finally {
    setStatusBusy(false);
    $("saveSettingsButton").disabled = false;
  }
}

function bindEvents() {
  document.querySelectorAll(".review-type").forEach((button) => {
    button.addEventListener("click", () => {
      state.section = button.dataset.section || "usage";
      renderSection();
    });
  });
  $("usageEnabled").addEventListener("change", applyUsageEnabledState);
  $("saveSettingsButton").addEventListener("click", () => saveSettings().catch((error) => setStatus(error.message)));
}

bindEvents();
loadSettings().catch((error) => {
  setStatus(error.message);
  setStatusBusy(false);
});
