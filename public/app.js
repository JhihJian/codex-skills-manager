const state = {
  data: null,
  selected: null,
  queue: "pending",
  filter: "all",
  usageFilter: "all",
  sort: "default",
  displayMode: "zh",
  category: "全部",
  search: "",
  tab: "meta",
  installOpen: false,
  mobileDetail: false,
  descriptionExpanded: false,
  descriptionOverflow: false,
  descriptionSkill: null,
  previewExpanded: true,
  previewSkill: null,
  previewMarkdown: new Map(),
  chineseViewCache: new Map(),
  chineseViewLoading: new Set(),
  contextSkill: null,
  usageSkill: null,
  usageCache: new Map(),
  usageLoading: false,
  historySkill: null,
  historyCache: new Map(),
  historyLoading: false,
  githubSources: null,
  githubSourcesLoading: false,
  repository: null,
};

const $ = (id) => document.getElementById(id);

function setStatus(text) {
  $("statusText").textContent = text;
}

function setStatusBusy(busy) {
  $("statusSpinner").hidden = !busy;
  $("statusText").classList.toggle("status-busy-text", busy);
  $("statusText").setAttribute("aria-busy", busy ? "true" : "false");
}

function clearGithubSourcesCache() {
  state.githubSources = null;
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

function sourceLabel(source) {
  if (!source) return "未知";
  const type = source.type || "unknown";
  if (type === "github") return "GitHub";
  if (type === "local-path") return "本地路径";
  if (type === "codex-home-adopted") return "本机纳管";
  if (type === "codex-system") return "系统";
  if (type === "project-library") return "项目库";
  return type;
}

function localization(skill) {
  return skill?.localized && typeof skill.localized === "object" ? skill.localized : {};
}

function hasLocalization(skill) {
  const item = localization(skill);
  return Boolean((item.zhName || "").trim() && (item.zhTrigger || "").trim());
}

function displayTitle(skill) {
  const item = localization(skill);
  if (state.displayMode === "zh" && item.zhName) return item.zhName;
  return skill.title || skill.name;
}

function displayDescription(skill) {
  const item = localization(skill);
  if (state.displayMode === "zh" && item.zhTrigger) return item.zhTrigger;
  return skill.description || "无描述";
}

function badge(text, tone = "") {
  const span = document.createElement("span");
  span.className = `badge ${tone}`.trim();
  span.textContent = text;
  return span;
}

function previewText(text, expanded) {
  const value = text || "未找到 SKILL.md 预览。";
  if (expanded) return value;
  const lines = value.split("\n");
  const limit = 8;
  const excerpt = lines.slice(0, limit).join("\n");
  const trimmed = excerpt.length > 900 ? `${excerpt.slice(0, 900).trimEnd()}\n...` : excerpt;
  if (lines.length <= limit && value.length <= 900) return value;
  return `${trimmed}\n\n...`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function slugify(value) {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .replace(/\s+/g, "-")
    .slice(0, 80);
}

function isSafeUrl(value) {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (/^(https?:|mailto:|#|\/|\.\.?\/)/i.test(trimmed)) return true;
  return /^[^:]+$/i.test(trimmed);
}

function inlineMarkdown(value) {
  const placeholders = [];
  let html = escapeHtml(value);
  html = html.replace(/`([^`]+)`/g, (_, code) => {
    const token = `\u0000${placeholders.length}\u0000`;
    placeholders.push(`<code>${code}</code>`);
    return token;
  });
  html = html.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, label, href) => {
    const safeHref = href.replaceAll("&amp;", "&");
    if (!isSafeUrl(safeHref)) return label;
    return `<a href="${escapeHtml(safeHref)}" target="_blank" rel="noreferrer">${label}</a>`;
  });
  html = html
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  return placeholders.reduce((output, replacement, index) => output.replaceAll(`\u0000${index}\u0000`, replacement), html);
}

function parseTable(lines, start) {
  const header = splitTableRow(lines[start]);
  const align = splitTableRow(lines[start + 1]);
  if (!header.length || !align.length || align.some((cell) => !/^:?-{3,}:?$/.test(cell.trim()))) {
    return null;
  }
  const rows = [];
  let index = start + 2;
  while (index < lines.length && /^\s*\|.+\|\s*$/.test(lines[index])) {
    rows.push(splitTableRow(lines[index]));
    index += 1;
  }
  return { header, rows, next: index };
}

function splitTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderMarkdown(markdown) {
  const source = (markdown || "未找到 SKILL.md 预览。").replace(/\r\n?/g, "\n");
  const lines = source.split("\n");
  const html = [];
  let index = 0;

  const renderParagraph = (paragraphLines) => {
    html.push(`<p>${inlineMarkdown(paragraphLines.join(" ").trim())}</p>`);
  };

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^\s*```([\w-]*)\s*$/);
    if (fence) {
      const language = fence[1] ? ` data-language="${escapeHtml(fence[1])}"` : "";
      const code = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      html.push(`<pre${language}><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const text = heading[2].trim();
      const id = slugify(text);
      html.push(`<h${level} id="${escapeHtml(id)}">${inlineMarkdown(text)}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*>/.test(line)) {
      const quoteLines = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      html.push(`<blockquote>${renderMarkdown(quoteLines.join("\n"))}</blockquote>`);
      continue;
    }

    const table = index + 1 < lines.length ? parseTable(lines, index) : null;
    if (table) {
      html.push("<table><thead><tr>");
      for (const cell of table.header) html.push(`<th>${inlineMarkdown(cell)}</th>`);
      html.push("</tr></thead>");
      if (table.rows.length) {
        html.push("<tbody>");
        for (const row of table.rows) {
          html.push("<tr>");
          for (let cellIndex = 0; cellIndex < table.header.length; cellIndex += 1) {
            html.push(`<td>${inlineMarkdown(row[cellIndex] || "")}</td>`);
          }
          html.push("</tr>");
        }
        html.push("</tbody>");
      }
      html.push("</table>");
      index = table.next;
      continue;
    }

    const list = line.match(/^(\s*)([-*+]|\d+\.)\s+(.+)$/);
    if (list) {
      const ordered = /\d+\./.test(list[2]);
      html.push(ordered ? "<ol>" : "<ul>");
      while (index < lines.length) {
        const item = lines[index].match(/^(\s*)([-*+]|\d+\.)\s+(.+)$/);
        if (!item || /\d+\./.test(item[2]) !== ordered) break;
        html.push(`<li>${inlineMarkdown(item[3].trim())}</li>`);
        index += 1;
      }
      html.push(ordered ? "</ol>" : "</ul>");
      continue;
    }

    if (/^\s*---+\s*$/.test(line)) {
      html.push("<hr>");
      index += 1;
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^\s*```/.test(lines[index]) &&
      !/^(#{1,4})\s+/.test(lines[index]) &&
      !/^(\s*)([-*+]|\d+\.)\s+/.test(lines[index]) &&
      !/^\s*>/.test(lines[index])
    ) {
      if (index + 1 < lines.length && parseTable(lines, index)) break;
      paragraph.push(lines[index]);
      index += 1;
    }
    renderParagraph(paragraph);
  }

  return html.join("");
}

function renderPreview(markdown, expanded) {
  const preview = $("skillPreview");
  preview.innerHTML = renderMarkdown(previewText(markdown, expanded));
  preview.classList.toggle("expanded", expanded);
}

async function ensureFullPreview(skill) {
  if (!skill || !state.previewExpanded || state.previewMarkdown.has(skill.name)) return;
  try {
    const payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/markdown`);
    state.previewMarkdown.set(skill.name, payload.markdown || skill.skillMdPreview || "");
    if (selectedSkill()?.name === skill.name) {
      renderPreview(state.previewMarkdown.get(skill.name), true);
    }
  } catch (error) {
    setStatus(error.message);
  }
}

function visibleSkills() {
  if (!state.data) return [];
  const search = state.search.trim().toLowerCase();
  const unusedDays = usageStatsEnabled() ? unusedFilterDays(state.usageFilter) : null;
  const skills = state.data.skills.filter((skill) => {
    const usage = skillUsage(skill);
    const lifecycle = skillLifecycle(skill);
    const confirmation = skillConfirmation(skill);
    if (
      state.queue === "pending" &&
      (!skill.enabled || skill.system || !["unconfirmed", "needs-review"].includes(confirmation.status))
    ) return false;
    if (state.queue === "confirmed" && confirmation.status !== "confirmed") return false;
    if (state.queue === "enabled" && !skill.enabled) return false;
    if (state.filter === "enabled" && !skill.enabled) return false;
    if (state.filter === "managed" && !skill.managed) return false;
    if (state.filter === "system" && !skill.system) return false;
    if (unusedDays !== null && !isUnusedForDays(usage, unusedDays)) return false;
    if (state.category !== "全部" && skill.category !== state.category) return false;
    if (!search) return true;
    const haystack = [
      skill.name,
      skill.title,
      skill.description,
      localization(skill).zhName,
      localization(skill).zhTrigger,
      localization(skill).notes,
      skill.category,
      usageStatusLabel(usage.status),
      usageCountText(usage),
      usage.lastUsedAt,
      lifecycle.lastEnabledAt,
      lifecycle.lastDisabledAt,
      disabledDurationLabel(skill),
      confirmationStatusLabel(confirmation.status),
      confirmation.confirmedAt,
      (skill.tags || []).join(" "),
      (skill.dependencies || []).join(" "),
      JSON.stringify(skill.source || {}),
    ].join(" ").toLowerCase();
    return haystack.includes(search);
  });
  return sortSkills(skills);
}

function selectedSkill() {
  return state.data?.skills.find((skill) => skill.name === state.selected) || null;
}

function filterLabel() {
  return {
    all: "所有范围",
    enabled: "启用",
    managed: "项目库",
    system: "系统",
  }[state.filter] || state.filter;
}

function queueLabel() {
  return {
    pending: "待确认",
    confirmed: "已确认",
    enabled: "已启用",
    all: "全部",
  }[state.queue] || state.queue;
}

function usageFilterLabel() {
  const days = unusedFilterDays(state.usageFilter);
  return days === null ? "全部使用状态" : `${days} 天未使用`;
}

function sortLabel() {
  return {
    default: "默认",
    name: "名称 A-Z",
    category: "分类",
    recent: "最近使用",
    count: "使用次数",
  }[state.sort] || state.sort;
}

function normalizeListControls() {
  if (usageStatsEnabled()) return;
  if (state.usageFilter !== "all") state.usageFilter = "all";
  if (state.sort === "recent" || state.sort === "count") state.sort = "default";
}

function classificationStatusText(classification, fallback = "完成") {
  if (!classification) return fallback;
  const parts = [];
  if (classification.classified?.length) parts.push(`自动分类 ${classification.classified.length} 个`);
  if (classification.skipped?.length) parts.push(`跳过 ${classification.skipped.length} 个`);
  if (classification.errors?.length) parts.push(`失败 ${classification.errors.length} 批`);
  return parts.length ? parts.join("，") : fallback;
}

function localizationStatusText(localizationResult, fallback = "中文信息已检查") {
  if (!localizationResult) return fallback;
  const parts = [];
  if (localizationResult.localized?.length) parts.push(`生成中文 ${localizationResult.localized.length} 个`);
  if (localizationResult.skipped?.length) parts.push(`跳过 ${localizationResult.skipped.length} 个`);
  if (localizationResult.errors?.length) parts.push(`失败 ${localizationResult.errors.length} 批`);
  return parts.length ? parts.join("，") : fallback;
}

function chineseViewStatusText(result, fallback = "中文原文视图已检查") {
  if (!result) return fallback;
  const parts = [];
  if (result.generated?.length) parts.push(`生成中文原文 ${result.generated.length} 个`);
  if (result.skipped?.length) parts.push(`跳过 ${result.skipped.length} 个`);
  if (result.errors?.length) parts.push(`失败 ${result.errors.length} 个`);
  return parts.length ? parts.join("，") : fallback;
}

function usageStatusLabel(status) {
  return {
    active: "近期使用",
    stale: "长期未用",
    "never-used": "暂无使用证据",
    "declared-only": "仅有声明",
    unknown: "未统计",
  }[status] || status;
}

function usageStatusTone(status) {
  return {
    active: "green",
    stale: "orange",
    "never-used": "red",
    "declared-only": "orange",
  }[status] || "";
}

function formatRelativeUsage(item) {
  if (!item || item.status === "unknown") return "尚未统计";
  if (item.daysSinceLastUsed === null || item.daysSinceLastUsed === undefined) return item.lastUsedAt || "无真实使用证据";
  if (item.daysSinceLastUsed === 0) return "今天";
  return `${item.daysSinceLastUsed} 天前`;
}

function skillUsage(skill) {
  return skill?.usage && typeof skill.usage === "object" ? skill.usage : { status: "unknown" };
}

function skillLifecycle(skill) {
  return skill?.lifecycle && typeof skill.lifecycle === "object" ? skill.lifecycle : {};
}

function skillConfirmation(skill) {
  return skill?.confirmation && typeof skill.confirmation === "object"
    ? skill.confirmation
    : { status: skill?.system ? "not-applicable" : "unconfirmed", confirmed: false };
}

function confirmationStatusLabel(status) {
  return {
    confirmed: "已确认",
    unconfirmed: "待确认",
    "needs-review": "需重新确认",
    "not-applicable": "系统管理",
    unavailable: "无法确认",
  }[status] || "待确认";
}

function confirmationStatusTone(status) {
  return {
    confirmed: "green",
    "needs-review": "orange",
    unavailable: "red",
  }[status] || "";
}

function confirmationMetaText(confirmation) {
  if (confirmation.status === "confirmed") {
    return `已确认于 ${formatDateTime(confirmation.confirmedAt)}，当前 SKILL.md 内容未变更。`;
  }
  if (confirmation.status === "needs-review") {
    return `SKILL.md 在 ${formatDateTime(confirmation.confirmedAt)} 确认后发生变化，需要重新确认。`;
  }
  if (confirmation.status === "not-applicable") return "系统技能由 Codex 管理，不进入人工确认队列。";
  if (confirmation.status === "unavailable") return confirmation.error || "技能文件不可用，暂时无法确认。";
  return "尚未确认。确认仅记录评估结果，不会启用或停用技能。";
}

function disabledDurationLabel(skill) {
  if (!skill || skill.enabled) return "";
  const lifecycle = skillLifecycle(skill);
  if (!lifecycle.lastDisabledAt) return "";
  return lifecycle.disabledSeconds === null || lifecycle.disabledSeconds === undefined
    ? "已停用"
    : `停用 ${formatLifecycleDuration(lifecycle.disabledSeconds)}`;
}

function usageStatsEnabled() {
  return state.data?.usageStats?.enabled !== false;
}

function unusedFilterDays(value) {
  const match = String(value || "").match(/^unused-(3|7|15)$/);
  return match ? Number(match[1]) : null;
}

function isUnusedForDays(usage, days) {
  if (!usage || usage.status === "unknown") return false;
  if (usage.status === "never-used" || usage.status === "declared-only") return true;
  const daysSince = Number(usage.daysSinceLastUsed);
  return Number.isFinite(daysSince) && daysSince >= days;
}

function usageTimestamp(usage) {
  const parsed = new Date(usage?.lastUsedAt || 0).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

function compareText(a, b) {
  return String(a || "").localeCompare(String(b || ""), "zh-CN", { sensitivity: "base", numeric: true });
}

function skillNameForSort(skill) {
  return displayTitle(skill) || skill.name || "";
}

function sortSkills(skills) {
  const items = [...skills];
  if (state.sort === "name") {
    return items.sort((a, b) => compareText(skillNameForSort(a), skillNameForSort(b)) || compareText(a.name, b.name));
  }
  if (state.sort === "category") {
    return items.sort(
      (a, b) =>
        compareText(a.category || "未分类", b.category || "未分类") ||
        compareText(skillNameForSort(a), skillNameForSort(b)) ||
        compareText(a.name, b.name),
    );
  }
  if (state.sort === "recent") {
    return items.sort(
      (a, b) =>
        usageTimestamp(skillUsage(b)) - usageTimestamp(skillUsage(a)) ||
        compareText(skillNameForSort(a), skillNameForSort(b)) ||
        compareText(a.name, b.name),
    );
  }
  if (state.sort === "count") {
    return items.sort(
      (a, b) =>
        Number(skillUsage(b).confirmedEvidenceCount || 0) - Number(skillUsage(a).confirmedEvidenceCount || 0) ||
        compareText(skillNameForSort(a), skillNameForSort(b)) ||
        compareText(a.name, b.name),
    );
  }
  return items;
}

function formatDateTime(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function usageCountText(usage) {
  const count = Number(usage?.confirmedEvidenceCount || 0);
  return count ? `${count} 次` : "无";
}

function formatTokenCount(value) {
  const count = Number(value || 0);
  if (count < 1000) return String(count);
  if (count < 1000000) return `${(count / 1000).toFixed(count < 10000 ? 1 : 0)}k`;
  return `${(count / 1000000).toFixed(count < 10000000 ? 1 : 0)}m`;
}

function tokenUsageText(skill) {
  const usage = skill?.tokenUsage || {};
  if (!skill?.enabled) return "未启用";
  if (usage.error) return usage.error;
  return usage.counted ? `${formatTokenCount(usage.tokens)} 预注入 token` : "未统计";
}

function tokenUsageMethodLabel(tokenUsage) {
  const method = tokenUsage?.method || "estimate:unicode";
  if (method.startsWith("tiktoken:")) return `${method.slice(9)} 编码`;
  return "本地 Unicode 估算";
}

function usageStatsUpdatedText() {
  const usageStats = state.data?.usageStats || {};
  if (usageStats.enabled === false) return "使用统计已关闭";
  if (!usageStats.reviewedAt) return "使用统计尚未生成";
  const age = usageStats.ageHours;
  const ageText = typeof age === "number" ? `，约 ${age} 小时前` : "";
  return `使用统计 ${formatDateTime(usageStats.reviewedAt)}${ageText}`;
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (!Number.isFinite(value) || value <= 0) return "0 分钟";
  const minutes = Math.ceil(value / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`;
}

function formatLifecycleDuration(seconds) {
  const value = Number(seconds || 0);
  if (!Number.isFinite(value) || value <= 0) return "不到 1 分钟";
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  if (days < 30) return restHours ? `${days} 天 ${restHours} 小时` : `${days} 天`;
  const months = Math.floor(days / 30);
  const restDays = days % 30;
  if (months < 12) return restDays ? `${months} 个月 ${restDays} 天` : `${months} 个月`;
  const years = Math.floor(days / 365);
  const yearRestDays = days % 365;
  const yearRestMonths = Math.floor(yearRestDays / 30);
  return yearRestMonths ? `${years} 年 ${yearRestMonths} 个月` : `${years} 年`;
}

function versioningStatusText() {
  const versioning = state.data?.versioning || {};
  if (!versioning.enabled) return "版本自动提交已关闭";
  if (versioning.committing) return "技能版本提交中";
  if (!versioning.pending) return "技能版本无待提交变更";
  const remaining = Math.max(0, Number(versioning.delaySeconds || 0) - Number(versioning.ageSeconds || 0));
  const count = Number(versioning.changedFiles || 0);
  return `技能版本待提交 ${count} 个文件，约 ${formatDuration(remaining)} 后自动提交`;
}

function repositoryStatusText(repository = state.repository) {
  if (!repository) return "未读取仓库配置";
  const remote = repository.remote || repository.skillsRepoUrl || "未配置 GitHub remote";
  const branch = repository.branch ? ` · ${repository.branch}` : "";
  const pending = repository.versioning?.pending ? ` · 待提交 ${repository.versioning.changedFiles || 0} 个文件` : "";
  return `${repository.skillsRepoDir || ""}${branch} · ${remote}${pending}`;
}

function repositoryWebUrl(value) {
  const remote = String(value || "").trim();
  if (!remote) return "";
  const sshMatch = remote.match(/^git@github\.com:([^/]+)\/(.+?)(?:\.git)?$/i);
  if (sshMatch) return `https://github.com/${sshMatch[1]}/${sshMatch[2]}`;
  const httpsMatch = remote.match(/^(https:\/\/github\.com\/[^/]+\/[^/]+?)(?:\.git)?$/i);
  if (httpsMatch) return httpsMatch[1];
  return /^https?:\/\//i.test(remote) ? remote : "";
}

function repositoryLinkLabel(value) {
  const url = String(value || "").trim();
  const githubMatch = url.match(/^https:\/\/github\.com\/([^/]+\/[^/]+)$/i);
  if (githubMatch) return githubMatch[1];
  return url.replace(/^https?:\/\//i, "");
}

function historyFileText(file) {
  const added = file.added === null || file.added === undefined ? "-" : `+${file.added}`;
  const deleted = file.deleted === null || file.deleted === undefined ? "-" : `-${file.deleted}`;
  return `${added} ${deleted}`;
}

function historyChangeSummary(files) {
  const fileItems = Array.isArray(files) ? files : [];
  if (!fileItems.length) return "没有文件统计。";
  const skillFiles = fileItems.filter((file) => file.skill && file.skill !== "registry").length;
  const registryFiles = fileItems.some((file) => file.skill === "registry");
  const additions = fileItems.reduce((sum, file) => sum + (Number.isFinite(file.added) ? file.added : 0), 0);
  const deletions = fileItems.reduce((sum, file) => sum + (Number.isFinite(file.deleted) ? file.deleted : 0), 0);
  const parts = [`修改 ${fileItems.length} 个文件`];
  if (skillFiles) parts.push(`技能文件 ${skillFiles} 个`);
  if (registryFiles) parts.push("包含登记信息");
  parts.push(`+${additions} / -${deletions}`);
  return parts.join("，");
}

function changeStatusLabel(status) {
  if (status === "??") return "新增";
  if (status.includes("D")) return "删除";
  if (status.includes("A")) return "新增";
  if (status.includes("M")) return "修改";
  if (status.includes("R")) return "重命名";
  return status || "变更";
}

function remoteStatusLabel(status) {
  if (status === "up-to-date") return "已同步";
  if (status === "updated") return "有更新";
  if (status === "missing-remote") return "远端缺失";
  if (status === "not-github") return "非 GitHub";
  if (status === "unknown") return "待确认";
  if (status === "error") return "检查失败";
  return status || "未知";
}

function remoteStatusTone(status) {
  if (status === "updated") return "orange";
  if (status === "up-to-date") return "green";
  if (["missing-remote", "error"].includes(status)) return "red";
  return "";
}

function renderDiffText(diff) {
  const lines = String(diff || "").split("\n");
  return lines
    .map((line) => {
      const span = document.createElement("span");
      if (line.startsWith("+") && !line.startsWith("+++")) span.className = "diff-add";
      if (line.startsWith("-") && !line.startsWith("---")) span.className = "diff-del";
      if (line.startsWith("@@")) span.className = "diff-hunk";
      span.textContent = line || " ";
      return span;
    });
}

function emptyListMessage() {
  const parts = [];
  if (state.queue !== "all") parts.push(`队列：${queueLabel()}`);
  if (state.search.trim()) parts.push(`搜索：${state.search.trim()}`);
  if (state.filter !== "all") parts.push(`筛选：${filterLabel()}`);
  if (state.usageFilter !== "all" && usageStatsEnabled()) parts.push(`使用：${usageFilterLabel()}`);
  if (state.category !== "全部") parts.push(`分类：${state.category}`);
  if (state.sort !== "default") parts.push(`排序：${sortLabel()}`);
  if (state.queue === "pending" && !state.search.trim() && state.filter === "all" && state.category === "全部" && state.usageFilter === "all") {
    return "没有待确认技能。当前已启用的普通技能都已确认。";
  }
  return parts.length ? `没有匹配的技能（${parts.join("；")}）` : "没有匹配的技能";
}

function syncSelectionWithVisible(skills = visibleSkills()) {
  if (!skills.length) {
    state.selected = null;
    return null;
  }
  if (!skills.some((skill) => skill.name === state.selected)) {
    state.selected = skills[0].name;
  }
  return state.selected;
}

function renderCategories() {
  const counts = new Map();
  for (const skill of state.data.skills) {
    counts.set(skill.category || "未分类", (counts.get(skill.category || "未分类") || 0) + 1);
  }
  const categories = ["全部", ...state.data.categories];
  $("categoryList").replaceChildren(
    ...categories.map((category) => {
      const option = document.createElement("option");
      option.value = category;
      return option;
    }),
  );
  $("categorySelect").replaceChildren(
    ...categories.map((category) => {
      const option = document.createElement("option");
      option.value = category;
      option.textContent = category === "全部" ? `全部分类（${state.data.skills.length}）` : `${category}（${counts.get(category) || 0}）`;
      return option;
    }),
  );
  $("categorySelect").value = state.category;
  $("categoryListView").replaceChildren(
    ...categories.map((category) => {
      const item = document.createElement("div");
      item.className = `category-item ${state.category === category ? "active" : ""}`;
      item.setAttribute("role", "button");
      item.setAttribute("tabindex", "0");
      item.setAttribute("aria-pressed", state.category === category ? "true" : "false");
      const label = document.createElement("span");
      label.textContent = category;
      const count = document.createElement("code");
      count.textContent = category === "全部" ? state.data.skills.length : counts.get(category) || 0;
      item.replaceChildren(label, count);
      const selectCategory = () => {
        state.category = category;
        render();
      };
      item.addEventListener("click", selectCategory);
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectCategory();
        }
      });
      return item;
    }),
  );
}

function renderRows() {
  const skills = visibleSkills();
  syncSelectionWithVisible(skills);
  $("emptyList").hidden = skills.length > 0;
  $("emptyListMessage").textContent = emptyListMessage();
  $("clearSearchButton").hidden = !state.search.trim();
  $("resetFiltersButton").hidden =
    state.queue === "all" && state.filter === "all" && state.category === "全部" && state.usageFilter === "all" && state.sort === "default";
  $("skillTable").replaceChildren(
    ...skills.map((skill) => {
      const usage = skillUsage(skill);
      const confirmation = skillConfirmation(skill);
      const row = document.createElement("button");
      row.type = "button";
      row.className = `row ${state.selected === skill.name ? "active" : ""}`;
      row.setAttribute("aria-pressed", state.selected === skill.name ? "true" : "false");
      const dotTone = skill.status === "missing" ? "missing" : skill.enabled ? "enabled" : "";
      const rowMain = document.createElement("div");
      rowMain.className = "row-main";
      const rowTitle = document.createElement("div");
      rowTitle.className = "row-title";
      const dot = document.createElement("span");
      dot.className = `status-dot ${dotTone}`.trim();
      const title = document.createElement("strong");
      title.title = skill.name;
      title.textContent = displayTitle(skill);
      rowTitle.append(dot, title);
      if (state.displayMode === "zh" && displayTitle(skill) !== skill.name) {
        const originalName = document.createElement("code");
        originalName.className = "row-original-name";
        originalName.textContent = skill.name;
        rowTitle.appendChild(originalName);
      }
      const rowDesc = document.createElement("div");
      rowDesc.className = "row-desc";
      rowDesc.textContent = displayDescription(skill);
      const meta = document.createElement("div");
      meta.className = "row-meta";
      rowMain.append(rowTitle, rowDesc, meta);
      const rowSide = document.createElement("div");
      rowSide.className = `row-side ${confirmationStatusTone(confirmation.status)}`.trim();
      const sideCount = document.createElement("strong");
      sideCount.textContent = confirmationStatusLabel(confirmation.status);
      const sideLabel = document.createElement("span");
      sideLabel.textContent = confirmation.confirmedAt
        ? new Date(confirmation.confirmedAt).toLocaleDateString("zh-CN")
        : formatRelativeUsage(usage);
      rowSide.replaceChildren(sideCount, sideLabel);
      row.append(rowMain, rowSide);
      meta.appendChild(badge(skill.category || "未分类"));
      meta.appendChild(badge(usageStatusLabel(usage.status), usageStatusTone(usage.status)));
      if (usage.confirmedEvidenceCount) meta.appendChild(badge(usageCountText(usage), "blue"));
      if (skill.enabled && state.queue !== "pending") meta.appendChild(badge("启用", "green"));
      if (!skill.enabled && skillLifecycle(skill).lastDisabledAt) meta.appendChild(badge(disabledDurationLabel(skill), "orange"));
      if (skill.system) meta.appendChild(badge("系统", "orange"));
      if (skill.managed) meta.appendChild(badge("纳管", "blue"));
      if (hasLocalization(skill)) meta.appendChild(badge("中文", "green"));
      if (skill.status === "missing") meta.appendChild(badge("缺失", "red"));
      row.addEventListener("click", () => {
        state.selected = skill.name;
        state.mobileDetail = true;
        render();
      });
      const item = document.createElement("li");
      item.className = "skill-list-item";
      item.appendChild(row);
      return item;
    }),
  );
}

function resetContextPanel() {
  state.contextSkill = null;
  $("contextQuery").value = "";
  $("contextSummary").textContent = "未选择技能，无法检索上下文。";
  $("contextResults").replaceChildren();
  $("emptyContexts").hidden = true;
}

function resetHistoryPanel() {
  state.historySkill = null;
  $("historySummary").textContent = "未选择技能，无法读取版本记录。";
  $("historyPending").hidden = true;
  $("historyPending").textContent = "";
  $("pendingChanges").hidden = true;
  $("pendingSummary").textContent = "";
  $("pendingFiles").replaceChildren();
  $("pendingDiff").hidden = true;
  $("pendingDiff").querySelector("code").replaceChildren();
  $("historyList").replaceChildren();
  $("emptyHistory").hidden = true;
}

function renderHistoryPending(pending) {
  const node = $("historyPending");
  if (!pending?.enabled) {
    node.hidden = false;
    node.textContent = pending?.message || "技能版本自动提交已关闭。";
    return;
  }
  if (pending.error) {
    node.hidden = false;
    node.textContent = pending.error;
    return;
  }
  if (!pending.pending) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  const remaining = Math.max(0, Number(pending.delaySeconds || 0) - Number(pending.ageSeconds || 0));
  const skills = (pending.skills || []).map((item) => item.name).filter(Boolean);
  const skillText = skills.length ? `，涉及 ${skills.slice(0, 5).join("、")}${skills.length > 5 ? " 等" : ""}` : "";
  node.hidden = false;
  node.textContent = `检测到 ${pending.changedFiles || 0} 个受管文件待提交${skillText}，静默 ${formatDuration(remaining)} 后自动写入 Git 版本。`;
}

function renderHistoryPayload(skill, payload) {
  const versions = payload?.versions || [];
  $("historyRefreshButton").disabled = false;
  renderHistoryPending(payload?.pending || state.data?.versioning || {});
  renderPendingChanges(skill, payload?.pendingChanges || { files: [] });
  $("historySummary").textContent = payload?.message || `共 ${versions.length} 条 Git 版本记录。`;
  $("emptyHistory").hidden = versions.length > 0;
  $("historyList").replaceChildren(
    ...versions.map((version) => {
      const item = document.createElement("article");
      item.className = "history-item";

      const head = document.createElement("div");
      head.className = "history-item-head";
      const title = document.createElement("strong");
      title.textContent = version.subject || "未命名提交";
      const hash = document.createElement("code");
      hash.textContent = version.shortHash || "";
      head.append(title, hash);

      const meta = document.createElement("div");
      meta.className = "history-meta";
      meta.textContent = `${formatDateTime(version.date) || version.date || "未知时间"} · ${version.author || "未知作者"}`;

      const summary = document.createElement("div");
      summary.className = "history-summary";
      summary.textContent = historyChangeSummary(version.files || []);

      const files = document.createElement("div");
      files.className = "history-files";
      const fileItems = version.files || [];
      if (fileItems.length) {
        files.replaceChildren(
          ...fileItems.map((file) => {
            const row = document.createElement("div");
            row.className = "history-file";
            const path = document.createElement("code");
            path.textContent = file.path || "";
            const stat = document.createElement("span");
            stat.textContent = historyFileText(file);
            row.replaceChildren(path, stat);
            return row;
          }),
        );
      } else {
        files.textContent = "该提交没有可展示的文件统计。";
      }

      item.append(head, meta, summary, files);
      return item;
    }),
  );
  if (selectedSkill()?.name === skill.name) {
    state.historySkill = skill.name;
  }
}

function renderPendingChanges(skill, pendingChanges) {
  const files = pendingChanges?.files || [];
  $("pendingChanges").hidden = files.length === 0;
  $("pendingSummary").textContent = files.length ? `${files.length} 个文件待提交` : "";
  $("pendingDiff").hidden = true;
  $("pendingDiff").querySelector("code").replaceChildren();
  $("pendingFiles").replaceChildren(
    ...files.map((file) => {
      const button = document.createElement("button");
      button.className = "pending-file";
      button.type = "button";
      button.title = file.path;
      const name = document.createElement("code");
      name.textContent = file.path;
      const status = document.createElement("span");
      status.textContent = changeStatusLabel(file.status);
      button.replaceChildren(name, status);
      button.addEventListener("click", () => loadPendingDiff(file.path).catch((error) => setStatus(error.message)));
      return button;
    }),
  );
}

async function loadPendingDiff(path) {
  $("pendingDiff").hidden = false;
  const code = $("pendingDiff").querySelector("code");
  code.textContent = "读取 diff 中...";
  const payload = await api(`/api/diff?path=${encodeURIComponent(path)}`);
  code.replaceChildren(...renderDiffText(payload.diff || ""));
}

function resetChineseViewPanel() {
  $("chineseViewMeta").textContent = "切换到此页签后生成或读取中文视图。";
  $("chineseSkillPreview").replaceChildren();
  $("refreshChineseViewButton").disabled = false;
}

function renderChineseView(payload) {
  const preview = $("chineseSkillPreview");
  const meta = $("chineseViewMeta");
  const skill = selectedSkill();
  if (!skill) {
    meta.textContent = "未选择技能。";
    preview.replaceChildren();
    return;
  }
  if (state.chineseViewLoading.has(skill.name)) {
    meta.textContent = "正在使用本机 Codex 生成只读中文视图。";
    preview.replaceChildren();
    $("refreshChineseViewButton").disabled = true;
    return;
  }
  $("refreshChineseViewButton").disabled = false;
  if (payload?.status === "ready" && payload.markdown) {
    meta.textContent = `仅供管理台查看，未写入技能目录或 Codex。生成于 ${formatDateTime(payload.generatedAt) || payload.generatedAt || "未知时间"}。`;
    preview.innerHTML = renderMarkdown(payload.markdown);
    return;
  }
  if (payload?.error) {
    meta.textContent = payload.error;
    preview.replaceChildren();
    return;
  }
  meta.textContent = payload?.status === "stale" ? "原始 SKILL.md 已变更，正在重新生成中文视图。" : "正在准备中文视图。";
  preview.replaceChildren();
}

async function ensureChineseView(skill) {
  if (!skill || state.tab !== "chineseView" || state.chineseViewLoading.has(skill.name)) return;
  const cached = state.chineseViewCache.get(skill.name);
  if (cached?.status === "ready" && cached.markdown) {
    renderChineseView(cached);
    return;
  }
  state.chineseViewLoading.add(skill.name);
  renderChineseView(cached);
  try {
    let payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/chinese-view`);
    if (payload.status !== "ready") {
      payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/chinese-view`, {
        method: "POST",
        body: "{}",
      });
    }
    state.chineseViewCache.set(skill.name, payload);
  } catch (error) {
    state.chineseViewCache.set(skill.name, { status: "error", error: error.message });
  } finally {
    state.chineseViewLoading.delete(skill.name);
    if (selectedSkill()?.name === skill.name && state.tab === "chineseView") {
      renderChineseView(state.chineseViewCache.get(skill.name));
    }
  }
}

async function regenerateChineseView() {
  const skill = selectedSkill();
  if (!skill || state.chineseViewLoading.has(skill.name)) return;
  state.chineseViewCache.delete(skill.name);
  state.chineseViewLoading.add(skill.name);
  renderChineseView();
  setStatusBusy(true);
  setStatus("正在重新生成当前技能的中文原文视图");
  try {
    const payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/chinese-view`, {
      method: "POST",
      body: JSON.stringify({ force: true }),
    });
    state.chineseViewCache.set(skill.name, payload);
    setStatus("当前技能中文原文视图已生成");
  } catch (error) {
    state.chineseViewCache.set(skill.name, { status: "error", error: error.message });
    setStatus(error.message);
  } finally {
    state.chineseViewLoading.delete(skill.name);
    setStatusBusy(false);
    if (selectedSkill()?.name === skill.name && state.tab === "chineseView") {
      renderChineseView(state.chineseViewCache.get(skill.name));
    }
  }
}

function resetGithubSourcesPanel() {
  $("githubSourcesSummary").textContent = "按仓库展示当前项目库已安装的 skills。";
  $("githubSourcesList").replaceChildren();
  $("remoteDiff").hidden = true;
  $("remoteDiff").querySelector("code").replaceChildren();
  $("githubSourcesRefreshButton").disabled = false;
}

function renderGithubSources(payload) {
  const repositories = payload?.repositories || [];
  const totalSkills = repositories.reduce((sum, repo) => sum + Number(repo.counts?.total || 0), 0);
  const updatedSkills = repositories.reduce((sum, repo) => sum + Number(repo.counts?.updated || 0), 0);
  const checkedAt = payload?.checkedAt ? ` · ${formatDateTime(payload.checkedAt)}` : "";
  $("githubSourcesSummary").textContent = repositories.length
    ? `${repositories.length} 个 GitHub 仓库，${totalSkills} 个已安装 skill，${updatedSkills} 个有更新${checkedAt}`
    : "当前项目库没有登记 GitHub 来源的已安装 skill。";
  $("remoteDiff").hidden = true;
  $("remoteDiff").querySelector("code").replaceChildren();
  $("githubSourcesList").replaceChildren(
    ...repositories.map((repo) => {
      const article = document.createElement("li");
      article.className = "source-group";
      const head = document.createElement("div");
      head.className = "source-group-head";
      const title = document.createElement("div");
      const strong = document.createElement("strong");
      strong.textContent = repo.repo || "未知仓库";
      const meta = document.createElement("span");
      const pushedAt = repo.remote?.pushedAt ? ` · 最近 push ${formatDateTime(repo.remote.pushedAt)}` : "";
      meta.textContent = `ref ${repo.ref || "main"} · ${repo.counts?.total || 0} 个技能 · ${repo.counts?.updated || 0} 个有更新${pushedAt}`;
      title.append(strong, meta);
      const link = document.createElement("a");
      link.href = repo.url || "#";
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "GitHub";
      if (!repo.url) link.removeAttribute("href");
      head.append(title, link);

      const skills = document.createElement("ul");
      skills.className = "source-skill-list";
      skills.replaceChildren(
        ...(repo.skills || []).map((skill) => {
          const item = document.createElement("li");
          item.className = "source-skill-item";
          const button = document.createElement("button");
          button.type = "button";
          button.className = `source-skill ${remoteStatusTone(skill.status)}`.trim();
          button.disabled = !skill.name || ["missing-remote", "not-github", "unknown", "error"].includes(skill.status);
          button.title = skill.error || `${skill.path || ""}/SKILL.md`;
          const main = document.createElement("div");
          const name = document.createElement("strong");
          name.textContent = skill.name || "未知技能";
          const path = document.createElement("code");
          path.textContent = skill.remotePath || (skill.path ? `${skill.path}/SKILL.md` : "路径未知");
          main.append(name, path);
          const side = document.createElement("div");
          const status = document.createElement("span");
          status.textContent = remoteStatusLabel(skill.status);
          const time = document.createElement("small");
          time.textContent = skill.error || (skill.hasUpdate ? "点击查看 diff" : "本地一致");
          side.append(status, time);
          button.replaceChildren(main, side);
          button.addEventListener("click", () => loadRemoteDiff(skill.name).catch((error) => setStatus(error.message)));
          item.append(button);
          return item;
        }),
      );
      article.append(head, skills);
      return article;
    }),
  );
}

function renderGithubSourcesForSelected() {
  if (state.githubSources) {
    renderGithubSources(state.githubSources);
    return;
  }
  resetGithubSourcesPanel();
  if (state.tab === "source" && !state.githubSourcesLoading) {
    loadGithubSources().catch((error) => {
      $("githubSourcesSummary").textContent = error.message;
      $("githubSourcesRefreshButton").disabled = false;
    });
  }
}

function openGithubSources() {
  const skill = selectedSkill() || state.data?.skills?.[0];
  if (!skill) {
    setStatus("当前没有可展示的技能来源");
    return;
  }
  state.selected = skill.name;
  state.tab = "source";
  render();
}

async function loadGithubSources(force = false) {
  if (state.githubSourcesLoading) return;
  if (!force && state.githubSources) {
    renderGithubSources(state.githubSources);
    return;
  }
  state.githubSourcesLoading = true;
  $("githubSourcesRefreshButton").disabled = true;
  $("githubSourcesSummary").textContent = "正在读取已安装的 GitHub 来源并检查远端更新";
  $("githubSourcesList").replaceChildren();
  $("remoteDiff").hidden = true;
  $("remoteDiff").querySelector("code").replaceChildren();
  try {
    const payload = await api("/api/sources/github");
    state.githubSources = payload;
    renderGithubSources(payload);
    setStatus("GitHub 来源检查完成");
  } finally {
    state.githubSourcesLoading = false;
    $("githubSourcesRefreshButton").disabled = false;
  }
}

async function loadRemoteDiff(name) {
  const diff = $("remoteDiff");
  const code = diff.querySelector("code");
  diff.hidden = false;
  code.textContent = "读取远端 diff 中...";
  const payload = await api(`/api/skills/${encodeURIComponent(name)}/remote-diff`);
  const comparison = payload.comparison || {};
  code.replaceChildren(...renderDiffText(payload.diff || "本地和 GitHub 当前 SKILL.md 一致。"));
  setStatus(
    comparison.hasUpdate
      ? `${name} 有 GitHub 更新：${comparison.remoteUpdatedAt || "未知时间"}`
      : `${name} 与 GitHub 当前版本一致`,
  );
}

function resetUsagePanel() {
  state.usageSkill = null;
  $("usageRecordMeta").textContent = "尚未读取";
  $("usageRecordMetrics").replaceChildren();
  $("usageRecordList").replaceChildren();
  $("emptyUsageRecords").hidden = true;
}

function usageMetric(label, value) {
  const group = document.createElement("div");
  group.className = "usage-record-metric";
  const dt = document.createElement("dt");
  const dd = document.createElement("dd");
  dt.textContent = label;
  dd.textContent = value;
  group.append(dt, dd);
  return group;
}

function renderUsagePayload(skill, payload) {
  if (selectedSkill()?.name !== skill.name) return;
  const entry = payload?.entry || {};
  const records = Array.isArray(entry.evidence) ? entry.evidence : [];
  $("usageRecordMeta").textContent = payload.reviewedAt
    ? `统计于 ${formatDateTime(payload.reviewedAt)} · 展示最近 ${records.length} 条`
    : "使用统计尚未生成";
  $("usageRecordMetrics").replaceChildren(
    ...[
      ["真实使用", `${entry.confirmedEvidenceCount || 0} 次`],
      ["涉及会话", `${entry.confirmedSessionCount || 0} 个`],
      ["使用天数", `${entry.confirmedDayCount || 0} 天`],
      ["最近使用", formatRelativeUsage(entry)],
    ].map(([label, value]) => usageMetric(label, value)),
  );
  $("emptyUsageRecords").hidden = records.length > 0;
  $("usageRecordList").replaceChildren(
    ...records.map((record) => {
      const article = document.createElement("article");
      article.className = "usage-record";
      const head = document.createElement("div");
      head.className = "usage-record-row-head";
      const source = document.createElement("strong");
      const sourceName = record.source === "pi" ? "Pi" : "Codex";
      const eventType = record.type === "skill-command-load" ? "/skill 加载" : "SKILL.md 读取";
      source.textContent = `${sourceName} · ${eventType}`;
      const time = document.createElement("span");
      time.textContent = formatDateTime(record.time) || "时间未知";
      head.append(source, time);
      const title = document.createElement("h3");
      title.textContent = record.title || record.sessionId || "未命名会话";
      const location = document.createElement("code");
      location.textContent = `${record.path || ""}${record.line ? `:${record.line}` : ""}`;
      const snippet = document.createElement("p");
      snippet.textContent = record.snippet || "";
      article.append(head, title, location, snippet);
      return article;
    }),
  );
}

async function loadUsageDetails(force = false) {
  const skill = selectedSkill();
  if (!skill || state.usageLoading) return;
  if (!force && state.usageCache.has(skill.name)) {
    renderUsagePayload(skill, state.usageCache.get(skill.name));
    return;
  }
  state.usageLoading = true;
  $("usageRecordMeta").textContent = "正在读取";
  try {
    const payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/usage`);
    state.usageCache.set(skill.name, payload);
    renderUsagePayload(skill, payload);
  } finally {
    state.usageLoading = false;
  }
}

function renderUsageForSelected() {
  const skill = selectedSkill();
  if (!skill) {
    resetUsagePanel();
    return;
  }
  if (state.usageSkill !== skill.name) {
    state.usageSkill = skill.name;
    $("usageRecordMeta").textContent = "切换到使用记录页签后读取";
    $("usageRecordMetrics").replaceChildren();
    $("usageRecordList").replaceChildren();
    $("emptyUsageRecords").hidden = true;
  }
  const cached = state.usageCache.get(skill.name);
  if (cached) renderUsagePayload(skill, cached);
  if (state.tab === "usage" && !cached && !state.usageLoading) {
    loadUsageDetails().catch((error) => {
      $("usageRecordMeta").textContent = error.message;
      $("emptyUsageRecords").hidden = false;
    });
  }
}

function renderHistoryForSelected() {
  const skill = selectedSkill();
  if (!skill) {
    resetHistoryPanel();
    return;
  }
  if (state.historySkill !== skill.name) {
    state.historySkill = skill.name;
    $("historySummary").textContent = "切换到版本页签后读取 Git 提交记录。";
    $("historyPending").hidden = true;
    $("pendingChanges").hidden = true;
    $("pendingFiles").replaceChildren();
    $("pendingDiff").hidden = true;
    $("historyList").replaceChildren();
    $("emptyHistory").hidden = true;
  }
  const cached = state.historyCache.get(skill.name);
  if (cached) {
    renderHistoryPayload(skill, cached);
  }
  if (state.tab === "history" && !cached && !state.historyLoading) {
    loadHistory().catch((error) => {
      $("historySummary").textContent = error.message;
      $("emptyHistory").hidden = false;
      $("historyRefreshButton").disabled = false;
    });
  }
}

function clearDetailDom() {
  state.descriptionExpanded = false;
  state.descriptionOverflow = false;
  state.descriptionSkill = null;
  state.previewExpanded = true;
  state.previewSkill = null;
  $("detailTitle").textContent = "";
  $("detailDescription").textContent = "";
  $("detailDescription").classList.remove("expanded");
  $("descriptionToggle").hidden = true;
  $("descriptionToggle").setAttribute("aria-expanded", "false");
  $("descriptionToggle").textContent = "显示完整描述";
  $("detailCategory").value = "";
  $("detailTags").value = "";
  $("detailDependencies").value = "";
  $("detailNotes").value = "";
  $("localizedName").value = "";
  $("localizedTrigger").value = "";
  $("localizedNotes").value = "";
  $("localizedMeta").value = "";
  $("skillPreview").replaceChildren();
  $("skillPreview").classList.remove("expanded");
  $("previewToggle").setAttribute("aria-expanded", "false");
  $("previewToggle").textContent = "展开预览";
  resetChineseViewPanel();
  $("confirmationMeta").textContent = "";
  $("confirmButton").hidden = true;
  $("confirmButton").disabled = true;
  $("enableButton").disabled = true;
  $("disableButton").disabled = true;
  $("enableButton").setAttribute("aria-disabled", "true");
  $("disableButton").setAttribute("aria-disabled", "true");
  $("detailBadges").replaceChildren();
  $("sourceList").replaceChildren();
  $("dependencyGraph").replaceChildren();
  resetContextPanel();
  resetUsagePanel();
  resetHistoryPanel();
  resetGithubSourcesPanel();
}

function renderDetail() {
  const skill = selectedSkill();
  $("emptyDetail").hidden = Boolean(skill);
  $("emptyDetail").setAttribute("aria-live", "polite");
  $("detailView").hidden = !skill;
  if (!skill) {
    clearDetailDom();
    return;
  }
  const usage = skillUsage(skill);
  const lifecycle = skillLifecycle(skill);
  const confirmation = skillConfirmation(skill);

  if (state.descriptionSkill !== skill.name) {
    state.descriptionExpanded = false;
    state.descriptionSkill = skill.name;
  }
  if (state.previewSkill !== skill.name) {
    state.previewExpanded = true;
    state.previewSkill = skill.name;
  }
  if (state.contextSkill !== skill.name) {
    state.contextSkill = skill.name;
    $("contextSummary").textContent = "点击“检索上下文”后读取本机 Codex 与 Pi 会话记录。";
    $("contextResults").replaceChildren();
    $("emptyContexts").hidden = true;
  }
  renderUsageForSelected();
  renderHistoryForSelected();
  $("detailTitle").textContent = displayTitle(skill);
  $("detailDescription").textContent = displayDescription(skill);
  $("detailDescription").classList.toggle("expanded", state.descriptionExpanded);
  $("descriptionToggle").setAttribute("aria-expanded", state.descriptionExpanded ? "true" : "false");
  $("descriptionToggle").textContent = state.descriptionExpanded ? "收起描述" : "显示完整描述";
  $("confirmationMeta").textContent = confirmationMetaText(confirmation);
  $("detailCategory").value = skill.category || "未分类";
  $("detailTags").value = (skill.tags || []).join(", ");
  $("detailDependencies").value = (skill.dependencies || []).join(", ");
  $("detailNotes").value = skill.notes || "";
  const localized = localization(skill);
  $("localizedName").value = localized.zhName || "";
  $("localizedTrigger").value = localized.zhTrigger || "";
  $("localizedNotes").value = localized.notes || "";
  const sourceLanguage = localized.sourceLanguage ? `原文：${localized.sourceLanguage}` : "原文：待判断";
  const generatedAt = localized.updatedAt || localized.generatedAt || skill.localizedAt || "";
  $("localizedMeta").value = hasLocalization(skill)
    ? `${sourceLanguage}${generatedAt ? ` · ${generatedAt}` : ""}`
    : "尚未生成中文名称和中文触发条件。";
  renderPreview(state.previewMarkdown.get(skill.name) || skill.skillMdPreview, state.previewExpanded);
  $("previewToggle").setAttribute("aria-expanded", state.previewExpanded ? "true" : "false");
  $("previewToggle").textContent = state.previewExpanded ? "收起预览" : "展开预览";
  ensureFullPreview(skill);
  if (state.tab === "chineseView") {
    renderChineseView(state.chineseViewCache.get(skill.name));
    ensureChineseView(skill);
  }
  $("enableButton").disabled = skill.system || skill.enabled || skill.status === "missing";
  $("disableButton").disabled = skill.system || !skill.enabled;
  $("enableButton").setAttribute("aria-disabled", $("enableButton").disabled ? "true" : "false");
  $("disableButton").setAttribute("aria-disabled", $("disableButton").disabled ? "true" : "false");
  const confirmButton = $("confirmButton");
  confirmButton.hidden = skill.system;
  confirmButton.disabled = skill.system || skill.status === "missing" || confirmation.status === "unavailable";
  confirmButton.dataset.action = confirmation.status === "confirmed" ? "unconfirm" : "confirm";
  confirmButton.classList.toggle("secondary-button", confirmation.status === "confirmed");
  confirmButton.querySelector("span").textContent =
    confirmation.status === "confirmed"
      ? "撤销确认"
      : confirmation.status === "needs-review"
        ? "重新确认"
        : "标记已确认";

  const badges = [];
  badges.push(badge(skill.enabled ? "已启用" : "未启用", skill.enabled ? "green" : ""));
  badges.push(badge(confirmationStatusLabel(confirmation.status), confirmationStatusTone(confirmation.status)));
  if (!skill.enabled && lifecycle.lastDisabledAt) badges.push(badge(disabledDurationLabel(skill), "orange"));
  badges.push(badge(sourceLabel(skill.source), skill.system ? "orange" : "blue"));
  badges.push(badge(usageStatusLabel(usage.status), usageStatusTone(usage.status)));
  if (hasLocalization(skill)) badges.push(badge("中文视图", "green"));
  if (skill.chineseView?.status === "ready") badges.push(badge("中文原文", "green"));
  if (skill.dependencies?.length) badges.push(badge(`${skill.dependencies.length} 依赖`));
  $("detailBadges").replaceChildren(...badges);

  const sourceRows = [
    ["确认状态", confirmationStatusLabel(confirmation.status)],
    ["确认时间", formatDateTime(confirmation.confirmedAt) || "无记录"],
    ["使用次数", usageCountText(usage)],
    ["涉及会话", `${usage.confirmedSessionCount || 0} 个`],
    ["使用天数", `${usage.confirmedDayCount || 0} 天`],
    ["最近使用", formatRelativeUsage(usage)],
    ["统计时间", usageStatsUpdatedText()],
    ["预注入 token", tokenUsageText(skill)],
    ["按需加载 token", skill.tokenUsage?.lazyCounted ? `${formatTokenCount(skill.tokenUsage.lazyTokens)} token` : "未统计"],
    ["Token 统计", tokenUsageMethodLabel(state.data.tokenUsage)],
    ["统计文件", skill.tokenUsage?.path || "无"],
    ["名称", skill.name],
    ["来源", sourceLabel(skill.source)],
    ["来源地址", skill.source?.source || skill.source?.path || ""],
    ["项目库", skill.libraryPath || ""],
    ["Codex", skill.codexPath || ""],
    ["最近启用", formatDateTime(lifecycle.lastEnabledAt) || "无记录"],
    ["最近停用", formatDateTime(lifecycle.lastDisabledAt) || "无记录"],
    [
      "停用时长",
      !skill.enabled && lifecycle.lastDisabledAt
        ? formatLifecycleDuration(lifecycle.disabledSeconds)
        : skill.enabled
          ? "当前已启用"
          : "无记录",
    ],
    ["同步时间", skill.lastSyncedAt || ""],
  ];
  $("sourceList").replaceChildren(
    ...sourceRows.flatMap(([key, value]) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = key;
      dd.textContent = value || "无";
      return [dt, dd];
    }),
  );
  renderGithubSourcesForSelected();

  const graph = $("dependencyGraph");
  const deps = skill.dependencies || [];
  graph.replaceChildren(
    badge(skill.name, "blue"),
    ...deps.map((dep) => badge(`→ ${dep}`, state.data.skills.some((item) => item.name === dep) ? "green" : "orange")),
  );

  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `tab${state.tab[0].toUpperCase()}${state.tab.slice(1)}`;
  });
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === state.tab);
    button.setAttribute("aria-pressed", button.dataset.tab === state.tab ? "true" : "false");
  });

  requestAnimationFrame(updateDescriptionToggle);
}

function renderStats() {
  const stats = state.data.stats;
  const tokenUsage = state.data.tokenUsage || {};
  const usageStats = state.data.usageStats || {};
  $("totalCount").textContent = stats.total;
  $("enabledCount").textContent = stats.enabled;
  $("pendingCount").textContent = stats.pendingConfirmation || 0;
  $("confirmedCount").textContent = stats.confirmed || 0;
  $("visibleCount").textContent = `${visibleSkills().length} 项`;
  document.querySelectorAll("#queueSegments button").forEach((button) => {
    const active = button.dataset.queue === state.queue;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("#filterSegments button").forEach((button) => {
    const active = button.dataset.filter === state.filter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  $("enabledTokenCount").textContent = formatTokenCount(tokenUsage.totalTokens);
  $("tokenMetric").title = `当前 ${tokenUsage.enabledSkillCount || 0} 个已启用 skills 的索引，共 ${tokenUsage.totalTokens || 0} 预注入 token；全部按需加载约 ${tokenUsage.totalLazyTokens || 0} token；${tokenUsageMethodLabel(tokenUsage)}`;
  $("usageFilter").value = state.usageFilter;
  $("usageFilter").disabled = !usageStatsEnabled();
  $("usageFilter").title = usageStatsEnabled() ? "按最近真实使用证据筛选技能" : "使用统计已关闭，可在设置页开启";
  $("sortSelect").value = state.sort;
  $("sortSelect").querySelectorAll("option").forEach((option) => {
    option.disabled = !usageStatsEnabled() && ["recent", "count"].includes(option.value);
  });
  const repositoryUrl = repositoryWebUrl(state.data.paths.remote || state.repository?.remote || state.repository?.skillsRepoUrl);
  const libraryPath = $("libraryPath");
  if (repositoryUrl) {
    libraryPath.textContent = repositoryLinkLabel(repositoryUrl);
    libraryPath.href = repositoryUrl;
    libraryPath.title = repositoryUrl;
  } else {
    libraryPath.textContent = state.data.paths.library;
    libraryPath.removeAttribute("href");
    libraryPath.title = state.data.paths.library;
  }
  $("codexPath").textContent = state.data.paths.codexSkills;
  const usageText = usageStats.enabled !== false && usageStats.refreshing ? "使用统计刷新中" : usageStatsUpdatedText();
  $("usageRefreshButton").disabled = usageStats.enabled === false;
  $("usageRefreshButton").title = usageStats.enabled === false ? "使用统计已关闭，可在设置页开启" : "刷新技能使用频率统计";
  const versionText = versioningStatusText();
  $("updatedAt").textContent = state.data.updatedAt ? `同步 ${state.data.updatedAt} · ${usageText} · ${versionText}` : `${usageText} · ${versionText}`;
  const codex = state.data.codex;
  $("codexStatus").textContent = codex.available ? codex.version : "codex 不可用";
}

function render() {
  if (!state.data) return;
  normalizeListControls();
  syncSelectionWithVisible();
  document.querySelector(".shell").classList.toggle("mobile-detail-open", state.mobileDetail && Boolean(selectedSkill()));
  renderStats();
  renderCategories();
  renderRows();
  renderDetail();
  renderInstallPanel();
}

function renderInstallPanel() {
  $("installPanel").hidden = !state.installOpen;
  $("installToggleButton").setAttribute("aria-expanded", state.installOpen ? "true" : "false");
  $("installToggleButton").querySelector("use").setAttribute("href", state.installOpen ? "#icon-minus" : "#icon-plus");
  renderRepositoryPanel();
}

function setMobileMenuOpen(open) {
  document.querySelector(".toolbar").classList.toggle("mobile-menu-open", open);
  $("mobileMoreButton").setAttribute("aria-expanded", open ? "true" : "false");
  if (open) $("toolbarOverflow").querySelector("button, a")?.focus();
}

function renderRepositoryPanel() {
  if (!state.repository) return;
  $("repositoryUrl").value = state.repository.skillsRepoUrl || state.repository.remote || "";
  $("repositoryDir").value = state.repository.skillsRepoDir || "";
  $("repositoryStatus").textContent = repositoryStatusText();
}

function updateDescriptionToggle() {
  const description = $("detailDescription");
  const toggle = $("descriptionToggle");
  const hasOverflow = description.scrollHeight > description.clientHeight + 2;
  state.descriptionOverflow = hasOverflow || state.descriptionExpanded;
  toggle.hidden = !state.descriptionOverflow;
}

function toggleDescription() {
  state.descriptionExpanded = !state.descriptionExpanded;
  $("detailDescription").classList.toggle("expanded", state.descriptionExpanded);
  $("descriptionToggle").setAttribute("aria-expanded", state.descriptionExpanded ? "true" : "false");
  $("descriptionToggle").textContent = state.descriptionExpanded ? "收起描述" : "显示完整描述";
}

function showInstallSourceError(message) {
  const input = $("installSource");
  const error = $("installSourceError");
  input.classList.toggle("field-invalid", Boolean(message));
  input.setAttribute("aria-invalid", message ? "true" : "false");
  error.textContent = message || "";
  error.hidden = !message;
  if (message) input.focus();
}

async function togglePreview() {
  state.previewExpanded = !state.previewExpanded;
  const skill = selectedSkill();
  renderPreview(state.previewMarkdown.get(skill?.name) || skill?.skillMdPreview, state.previewExpanded);
  $("previewToggle").setAttribute("aria-expanded", state.previewExpanded ? "true" : "false");
  $("previewToggle").textContent = state.previewExpanded ? "收起预览" : "展开预览";
  await ensureFullPreview(skill);
}

async function refresh() {
  setStatus("同步中");
  const payload = await api("/api/state");
  state.data = payload;
  try {
    state.repository = await api("/api/repository");
  } catch {
    state.repository = null;
  }
  syncSelectionWithVisible();
  render();
  setStatus("准备就绪");
}

async function sync() {
  setStatus("正在扫描 .codex/skills");
  const payload = await api("/api/sync", { method: "POST", body: "{}" });
  state.data = payload.state;
  state.historyCache.clear();
  state.chineseViewCache.clear();
  clearGithubSourcesCache();
  syncSelectionWithVisible();
  render();
  setStatus(
    `同步完成，${classificationStatusText(payload.classification, "没有新增分类")}，${localizationStatusText(payload.localization, "没有新增中文信息")}，${chineseViewStatusText(payload.chineseView, "没有新增中文原文")}`,
  );
}

async function classifySkills(force = false) {
  $("classifyButton").disabled = true;
  setStatus(force ? "正在重新分类" : "正在自动分类未分类技能");
  try {
    const payload = await api("/api/classify", {
      method: "POST",
      body: JSON.stringify({ force }),
    });
    state.data = payload.state;
    state.historyCache.clear();
    clearGithubSourcesCache();
    syncSelectionWithVisible();
    render();
    setStatus(classificationStatusText(payload, payload.message || "自动分类完成"));
  } finally {
    $("classifyButton").disabled = false;
  }
}

async function localizeSkills(force = false) {
  $("localizeButton").disabled = true;
  setStatusBusy(true);
  setStatus(force ? "正在重新生成中文信息" : "正在生成中文名称和触发条件");
  try {
    const payload = await api("/api/localize", {
      method: "POST",
      body: JSON.stringify({ force }),
    });
    state.data = payload.state;
    state.historyCache.clear();
    clearGithubSourcesCache();
    syncSelectionWithVisible();
    render();
    setStatus(localizationStatusText(payload, payload.message || "中文信息生成完成"));
  } finally {
    setStatusBusy(false);
    $("localizeButton").disabled = false;
  }
}

async function refreshUsageStats() {
  if (state.data?.usageStats?.enabled === false) {
    setStatus("使用统计已关闭，可在设置页开启");
    return;
  }
  $("usageRefreshButton").disabled = true;
  setStatusBusy(true);
  setStatus("正在刷新技能使用频率");
  try {
    const payload = await api("/api/usage-stats/refresh", {
      method: "POST",
      body: "{}",
    });
    state.data = await api("/api/state");
    state.usageCache.clear();
    state.historyCache.clear();
    clearGithubSourcesCache();
    syncSelectionWithVisible();
    render();
    const stats = payload.stats || {};
    setStatus(`使用统计已刷新：有真实使用证据 ${(stats.active || 0) + (stats.stale || 0)} 个，需关注 ${stats.issues || 0} 个`);
  } finally {
    setStatusBusy(false);
    $("usageRefreshButton").disabled = state.data?.usageStats?.enabled === false;
  }
}

async function localizeSelectedSkill(force = true) {
  const skill = selectedSkill();
  if (!skill) return;
  $("localizeCurrentButton").disabled = true;
  setStatusBusy(true);
  setStatus("正在生成当前技能中文信息");
  try {
    const payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/localize`, {
      method: "POST",
      body: JSON.stringify({ force }),
    });
    state.data = payload.state;
    state.historyCache.delete(skill.name);
    clearGithubSourcesCache();
    syncSelectionWithVisible();
    render();
    setStatus(localizationStatusText(payload, payload.message || "当前技能中文信息已生成"));
  } finally {
    setStatusBusy(false);
    $("localizeCurrentButton").disabled = false;
  }
}

async function install() {
  const source = $("installSource").value.trim();
  const category = $("installCategory").value.trim();
  if (!source) {
    setStatus("需要填写 GitHub 地址");
    showInstallSourceError("请填写 GitHub tree 地址或 SKILL.md 文件地址，例如 https://github.com/LiamGvchi/gc-minimal-zine-poster/blob/main/SKILL.md。");
    return;
  }
  showInstallSourceError("");
  $("installButton").disabled = true;
  setStatus("安装中");
  try {
    const body = { source, category };
    const payload = await api("/api/install", { method: "POST", body: JSON.stringify(body) });
    state.data = payload.state;
    state.historyCache.clear();
    clearGithubSourcesCache();
    state.chineseViewCache.clear();
    state.queue = "all";
    state.selected = payload.installed[0] || state.selected;
    syncSelectionWithVisible();
    render();
    const classification = classificationStatusText(payload.classification, "没有新增分类");
    const localization = localizationStatusText(payload.localization, "没有新增中文信息");
    const chineseView = chineseViewStatusText(payload.chineseView, "没有新增中文原文");
    setStatus(`已安装 ${payload.installed.join(", ")}，${classification}，${localization}，${chineseView}`);
  } catch (error) {
    setStatus(error.message);
  } finally {
    $("installButton").disabled = false;
  }
}

async function saveRepositoryConfig() {
  $("repositorySaveButton").disabled = true;
  $("repositoryTestButton").disabled = true;
  setStatus("正在保存 skills 仓库配置");
  try {
    const payload = await api("/api/repository", {
      method: "PUT",
      body: JSON.stringify({
        skillsRepoUrl: $("repositoryUrl").value.trim(),
        skillsRepoDir: $("repositoryDir").value.trim(),
      }),
    });
    state.repository = payload.repository;
    state.data = payload.state;
    state.historyCache.clear();
    clearGithubSourcesCache();
    render();
    setStatus(payload.message || "skills 仓库配置已保存");
  } finally {
    $("repositorySaveButton").disabled = false;
    $("repositoryTestButton").disabled = false;
  }
}

async function testRepositoryConfig() {
  $("repositoryTestButton").disabled = true;
  setStatus("正在测试 skills 仓库提交和推送");
  try {
    const payload = await api("/api/repository/test", { method: "POST", body: "{}" });
    state.repository = payload.repository;
    state.data = await api("/api/state");
    state.historyCache.clear();
    clearGithubSourcesCache();
    render();
    const result = payload.result || {};
    const push = result.push || {};
    const commitText = result.committed ? `提交 ${result.commit || ""}` : result.message || "没有需要提交的变更";
    const pushText = push.pushed ? "，已推送" : push.error ? `，推送失败：${push.error}` : "";
    setStatus(`${commitText}${pushText}`);
  } finally {
    $("repositoryTestButton").disabled = false;
  }
}

async function saveSkill() {
  const skill = selectedSkill();
  if (!skill) return;
  const localized = localization(skill);
  const body = {
    category: $("detailCategory").value.trim() || "未分类",
    tags: $("detailTags").value,
    dependencies: $("detailDependencies").value,
    notes: $("detailNotes").value,
    localized: {
      zhName: $("localizedName").value.trim(),
      zhTrigger: $("localizedTrigger").value.trim(),
      notes: $("localizedNotes").value.trim(),
      sourceLanguage: localized.sourceLanguage || "",
    },
  };
  setStatus("保存中");
  const payload = await api(`/api/skills/${encodeURIComponent(skill.name)}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
  state.data = payload.state;
  state.historyCache.delete(skill.name);
  clearGithubSourcesCache();
  syncSelectionWithVisible();
  render();
  setStatus("已保存");
}

function confirmSkillToggle(action, skill) {
  if (action === "enable") {
    return window.confirm(
      `确认启用技能“${skill.name}”？\n\n这会把项目库中的技能复制到 .codex/skills。新的 Codex 会话会加载该技能，当前已打开的会话通常需要重启后才会看到变化。`,
    );
  }
  return window.confirm(
    `确认停用技能“${skill.name}”？\n\n这只会删除 .codex/skills 下的启用副本，项目库中的技能仍会保留，并记录本次停用时间。新的 Codex 会话将不再加载该启用副本。`,
  );
}

async function toggleSkill(action) {
  const skill = selectedSkill();
  if (!skill) return;
  if (skill.system) return;
  if (!confirmSkillToggle(action, skill)) {
    setStatus("已取消");
    return;
  }
  setStatus(action === "enable" ? "启用中" : "停用中");
  const payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/${action}`, {
    method: "POST",
    body: "{}",
  });
  state.data = payload.state;
  state.historyCache.delete(skill.name);
  clearGithubSourcesCache();
  syncSelectionWithVisible();
  render();
  setStatus(payload.message || "完成");
}

async function toggleConfirmation() {
  const skill = selectedSkill();
  if (!skill || skill.system) return;
  const action = $("confirmButton").dataset.action || "confirm";
  const name = skill.name;
  $("confirmButton").disabled = true;
  setStatus(action === "confirm" ? "正在记录确认结果" : "正在撤销确认");
  const payload = await api(`/api/skills/${encodeURIComponent(name)}/${action}`, {
    method: "POST",
    body: "{}",
  });
  state.data = payload.state;
  state.historyCache.delete(name);
  syncSelectionWithVisible();
  render();
  const remaining = state.data.stats?.pendingConfirmation || 0;
  setStatus(
    action === "confirm"
      ? `${name} 已确认，已从待确认队列移除；剩余 ${remaining} 个待确认`
      : `${name} 已撤销确认，已重新进入待确认队列`,
  );
}

async function loadContexts() {
  const skill = selectedSkill();
  if (!skill) return;
  $("contextButton").disabled = true;
  $("contextSummary").textContent = "检索中";
  $("contextResults").replaceChildren();
  try {
    const params = new URLSearchParams();
    const q = $("contextQuery").value.trim();
    if (q) params.set("q", q);
    const payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/contexts?${params}`);
    $("contextSummary").textContent = `命中 ${payload.matchedSessionCount} 个会话，展示 ${payload.results.length} 条记录。${payload.summary || "仅展示用户/助手正文。"}`;
    $("emptyContexts").hidden = payload.results.length > 0;
    $("contextResults").replaceChildren(
      ...payload.results.map((item) => {
        const node = document.createElement("article");
        node.className = "context-item";
        const title = document.createElement("h3");
        title.title = item.title;
        title.textContent = `${item.source === "pi" ? "Pi" : "Codex"} · ${item.title}`;
        const path = document.createElement("code");
        path.textContent = `${item.updatedAt} · ${item.path}`;
        node.append(title, path);
        for (const snippet of item.snippets) {
          const div = document.createElement("div");
          div.className = "snippet";
          const marker = document.createElement("span");
          marker.textContent = `${snippet.roleLabel || snippet.role} L${snippet.line}`;
          div.append(marker, document.createTextNode(snippet.text));
          node.appendChild(div);
        }
        return node;
      }),
    );
  } catch (error) {
    $("contextSummary").textContent = error.message;
    $("emptyContexts").hidden = false;
  } finally {
    $("contextButton").disabled = false;
  }
}

async function loadHistory(force = false) {
  const skill = selectedSkill();
  if (!skill || state.historyLoading) return;
  if (!force && state.historyCache.has(skill.name)) {
    renderHistoryPayload(skill, state.historyCache.get(skill.name));
    return;
  }
  state.historyLoading = true;
  $("historyRefreshButton").disabled = true;
  $("historySummary").textContent = "正在读取 Git 版本记录";
  $("emptyHistory").hidden = true;
  try {
    const payload = await api(`/api/skills/${encodeURIComponent(skill.name)}/history`);
    state.historyCache.set(skill.name, payload);
    if (state.data?.versioning && payload.pending) {
      state.data.versioning = payload.pending;
    }
    renderHistoryPayload(skill, payload);
    renderStats();
  } finally {
    state.historyLoading = false;
    $("historyRefreshButton").disabled = false;
  }
}

async function showAudit() {
  const payload = await api("/api/audit");
  const lines = payload.events.map((event) => `${event.time} ${event.action} ${event.skill || (event.skills || []).join(", ") || ""}`);
  setStatus(lines.slice(0, 4).join(" | ") || "暂无操作记录");
}

function bindEvents() {
  $("mobileMoreButton").addEventListener("click", () => {
    const open = !document.querySelector(".toolbar").classList.contains("mobile-menu-open");
    setMobileMenuOpen(open);
  });
  $("toolbarOverflow").addEventListener("click", (event) => {
    if (event.target.closest("button, a")) setMobileMenuOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.querySelector(".toolbar").classList.contains("mobile-menu-open")) {
      setMobileMenuOpen(false);
      $("mobileMoreButton").focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (document.querySelector(".toolbar").classList.contains("mobile-menu-open") && !event.target.closest(".toolbar")) {
      setMobileMenuOpen(false);
    }
  });
  $("mobileBackButton").addEventListener("click", () => {
    state.mobileDetail = false;
    render();
    requestAnimationFrame(() => document.querySelector(".list-pane")?.scrollIntoView({ block: "start" }));
  });
  $("installToggleButton").addEventListener("click", () => {
    state.installOpen = !state.installOpen;
    renderInstallPanel();
  });
  $("syncButton").addEventListener("click", () => sync().catch((error) => setStatus(error.message)));
  $("classifyButton").addEventListener("click", (event) => classifySkills(event.shiftKey).catch((error) => setStatus(error.message)));
  $("localizeButton").addEventListener("click", (event) => localizeSkills(event.shiftKey).catch((error) => setStatus(error.message)));
  $("usageRefreshButton").addEventListener("click", () => refreshUsageStats().catch((error) => setStatus(error.message)));
  $("localizeCurrentButton").addEventListener("click", (event) =>
    localizeSelectedSkill(!event.shiftKey).catch((error) => setStatus(error.message)),
  );
  $("saveLocalizedButton").addEventListener("click", () => saveSkill().catch((error) => setStatus(error.message)));
  $("installButton").addEventListener("click", install);
  $("repositorySaveButton").addEventListener("click", () => saveRepositoryConfig().catch((error) => setStatus(error.message)));
  $("repositoryTestButton").addEventListener("click", () => testRepositoryConfig().catch((error) => setStatus(error.message)));
  $("installSource").addEventListener("input", () => showInstallSourceError(""));
  $("saveButton").addEventListener("click", () => saveSkill().catch((error) => setStatus(error.message)));
  $("confirmButton").addEventListener("click", () => toggleConfirmation().catch((error) => {
    $("confirmButton").disabled = false;
    setStatus(error.message);
  }));
  $("enableButton").addEventListener("click", () => toggleSkill("enable").catch((error) => setStatus(error.message)));
  $("disableButton").addEventListener("click", () => toggleSkill("disable").catch((error) => setStatus(error.message)));
  $("descriptionToggle").addEventListener("click", toggleDescription);
  $("previewToggle").addEventListener("click", togglePreview);
  $("refreshChineseViewButton").addEventListener("click", regenerateChineseView);
  $("contextButton").addEventListener("click", loadContexts);
  $("historyRefreshButton").addEventListener("click", () => loadHistory(true).catch((error) => setStatus(error.message)));
  $("githubSourcesButton").addEventListener("click", openGithubSources);
  $("githubSourcesRefreshButton").addEventListener("click", () => loadGithubSources(true).catch((error) => setStatus(error.message)));
  $("auditButton").addEventListener("click", () => showAudit().catch((error) => setStatus(error.message)));

  $("searchInput").addEventListener("input", (event) => {
    state.search = event.target.value;
    render();
  });
  $("usageFilter").addEventListener("change", (event) => {
    state.usageFilter = event.target.value;
    render();
  });
  $("sortSelect").addEventListener("change", (event) => {
    state.sort = event.target.value;
    render();
  });
  $("categorySelect").addEventListener("change", (event) => {
    state.category = event.target.value;
    render();
  });
  document.querySelectorAll("#queueSegments button").forEach((button) => {
    button.addEventListener("click", () => {
      state.queue = button.dataset.queue || "pending";
      state.mobileDetail = false;
      if (state.queue !== "all" && state.filter === "system") state.filter = "all";
      render();
    });
  });
  document.querySelectorAll("#filterSegments button").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      state.mobileDetail = false;
      if (state.filter === "system") state.queue = "all";
      document.querySelectorAll("#filterSegments button").forEach((item) => item.classList.toggle("active", item === button));
      render();
    });
  });
  document.querySelectorAll("#displayModeSegments button").forEach((button) => {
    button.addEventListener("click", () => {
      state.displayMode = button.dataset.mode || "zh";
      document.querySelectorAll("#displayModeSegments button").forEach((item) => item.classList.toggle("active", item === button));
      render();
    });
  });
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.addEventListener("click", () => {
      state.tab = button.dataset.tab;
      renderDetail();
    });
  });
  $("clearSearchButton").addEventListener("click", () => {
    state.search = "";
    $("searchInput").value = "";
    render();
  });
  $("resetFiltersButton").addEventListener("click", () => {
    state.mobileDetail = false;
    state.queue = "all";
    state.filter = "all";
    state.category = "全部";
    state.usageFilter = "all";
    state.sort = "default";
    $("usageFilter").value = state.usageFilter;
    $("sortSelect").value = state.sort;
    $("categorySelect").value = state.category;
    document.querySelectorAll("#filterSegments button").forEach((item) => item.classList.toggle("active", item.dataset.filter === "all"));
    render();
  });
}

bindEvents();
refresh().catch((error) => setStatus(error.message));
