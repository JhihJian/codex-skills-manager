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
  const response = await window.skillAuth.fetch(path, {
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

function repositoryStatusText() {
  const repository = state.settings?.repository || {};
  const parts = [
    repository.exists ? "目录已存在" : "目录未创建",
    repository.git ? "Git 已初始化" : "Git 未初始化",
  ];
  if (repository.branch) parts.push(`分支 ${repository.branch}`);
  if (repository.remote) parts.push(`remote ${repository.remote}`);
  return parts.join(" · ");
}

function renderRepositorySettings() {
  const repository = state.settings?.repository || {};
  $("repositoryUrl").value = repository.skillsRepoUrl || repository.remote || "";
  $("repositoryDir").value = repository.skillsRepoDir || "";
  $("repositoryStatus").textContent = repositoryStatusText();
}

function renderPaths() {
  const paths = state.settings?.paths || {};
  const repository = state.settings?.repository || {};
  const rows = [
    ["设置文件", paths.settings],
    ["统计缓存", paths.usageStats],
    ["Skills 仓库", repository.skillsRepoDir],
    ["Skills 目录", repository.skillsDir],
    ["Codex 会话", paths.sessions],
    ["Codex 归档会话", paths.archivedSessions],
    ["Pi Agent", paths.piAgent],
    ["Pi 会话", paths.piSessions],
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
  const repositoryActive = state.section === "repository";
  $("settingsTitle").textContent = usageActive ? "使用统计" : repositoryActive ? "仓库" : "路径";
  $("settingsDescription").textContent = usageActive
    ? "配置 skills 使用频率分析统计是否启用，以及缓存刷新时读取哪些本机会话。"
    : repositoryActive
      ? "配置独立 skills 仓库地址和本地目录。"
      : "查看当前设置文件、统计缓存和本机会话目录。";
  $("usageSettingsPanel").hidden = !usageActive;
  $("repositorySettingsPanel").hidden = !repositoryActive;
  $("pathsSettingsPanel").hidden = usageActive || repositoryActive;
  $("saveSettingsButton").hidden = !usageActive;
  document.querySelectorAll(".review-type").forEach((button) => {
    button.classList.toggle("active", button.dataset.section === state.section);
  });
}

function render() {
  renderUsageSettings();
  renderRepositorySettings();
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

async function saveRepositorySettings() {
  $("saveRepositoryButton").disabled = true;
  $("testRepositoryButton").disabled = true;
  setStatusBusy(true);
  setStatus("正在保存仓库配置");
  try {
    const payload = await api("/api/repository", {
      method: "PUT",
      body: JSON.stringify({
        skillsRepoUrl: $("repositoryUrl").value.trim(),
        skillsRepoDir: $("repositoryDir").value.trim(),
      }),
    });
    state.settings = { ...state.settings, repository: payload.repository };
    render();
    setStatus(payload.message || "仓库配置已保存");
  } finally {
    setStatusBusy(false);
    $("saveRepositoryButton").disabled = false;
    $("testRepositoryButton").disabled = false;
  }
}

async function testRepositorySettings() {
  $("testRepositoryButton").disabled = true;
  setStatusBusy(true);
  setStatus("正在测试仓库提交和推送");
  try {
    const payload = await api("/api/repository/test", { method: "POST", body: "{}" });
    state.settings = { ...state.settings, repository: payload.repository };
    render();
    const result = payload.result || {};
    const push = result.push || {};
    const commitText = result.committed ? `提交 ${result.commit || ""}` : result.message || "没有需要提交的变更";
    const pushText = push.pushed ? "，已推送" : push.error ? `，推送失败：${push.error}` : "";
    setStatus(`${commitText}${pushText}`);
  } finally {
    setStatusBusy(false);
    $("testRepositoryButton").disabled = false;
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
  $("saveRepositoryButton").addEventListener("click", () => saveRepositorySettings().catch((error) => setStatus(error.message)));
  $("testRepositoryButton").addEventListener("click", () => testRepositorySettings().catch((error) => setStatus(error.message)));
}

bindEvents();
loadSettings().catch((error) => {
  setStatus(error.message);
  setStatusBusy(false);
});
