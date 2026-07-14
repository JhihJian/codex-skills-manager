const state = {
  activeReview: "usage",
  running: false,
  results: new Map(),
};

const $ = (id) => document.getElementById(id);

function setStatus(text) {
  $("statusText").textContent = text;
  $("reviewPageStatus").textContent = text;
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

function badge(text, tone = "") {
  const span = document.createElement("span");
  span.className = `badge ${tone}`.trim();
  span.textContent = text;
  return span;
}

function usageStatusLabel(status) {
  return {
    active: "近期使用",
    stale: "长期未用",
    "never-used": "未确认使用",
    "declared-only": "仅有声明",
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
  if (item.daysSinceLastUsed === null || item.daysSinceLastUsed === undefined) return item.lastUsedAt || "无确认使用证据";
  if (item.daysSinceLastUsed === 0) return "今天";
  return `${item.daysSinceLastUsed} 天前`;
}

function warningNode(warning) {
  const node = document.createElement("div");
  node.className = "review-warning";
  node.textContent = warning.message || String(warning);
  return node;
}

const reviews = {
  usage: {
    title: "长期未真实使用",
    description: "识别长期没有真实触发证据的 skills，避免把会话技能列表或普通关键词命中误判为使用。",
    runningSummary() {
      const staleDays = $("reviewStaleDays").value || "30";
      return `正在扫描本机会话，按 ${staleDays} 天阈值识别未真实使用的技能。`;
    },
    runningPolicy:
      "正在读取 sessions 和 archived_sessions 中的结构化事件，只把 SKILL.md 读取工具调用计为真实使用证据。",
    buildBody() {
      return {
        staleDays: $("reviewStaleDays").value,
        scope: $("reviewScope").value,
        includeSystem: $("reviewIncludeSystem").checked,
      };
    },
    async run() {
      return api("/api/reviews/usage", {
        method: "POST",
        body: JSON.stringify(this.buildBody()),
      });
    },
    renderResult(payload) {
      const stats = payload.stats || {};
      const summaryItems = [
        ["审查", stats.reviewed ?? 0, ""],
        ["问题", stats.issues ?? 0, "red"],
        ["未确认", stats.neverUsed ?? 0, "red"],
        ["仅声明", stats.declaredOnly ?? 0, "orange"],
        ["长期未用", stats.stale ?? 0, "orange"],
        ["近期使用", stats.active ?? 0, "green"],
      ];
      $("reviewSummary").replaceChildren(
        ...summaryItems.map(([label, value, tone]) => {
          const item = document.createElement("div");
          item.className = "review-metric";
          item.append(badge(label, tone), document.createElement("strong"));
          item.querySelector("strong").textContent = value;
          return item;
        }),
      );
      $("reviewPolicy").textContent = `${payload.evidencePolicy || ""} 扫描 ${payload.scan?.scannedFiles || 0} 个会话文件，阈值 ${payload.staleDays} 天。`;

      const interesting = (payload.entries || []).filter((item) => item.status !== "active");
      const entries = interesting.length ? interesting : payload.entries || [];
      const warnings = (payload.warnings || []).map(warningNode);
      if (!entries.length) {
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = stats.reviewed ? "当前范围内没有需要展示的技能。" : "当前审查范围内没有技能。";
        $("reviewResults").replaceChildren(...warnings, empty);
        return;
      }
      $("reviewResults").replaceChildren(...warnings, ...entries.map(renderUsageReviewItem));
    },
  },
};

function renderUsageReviewItem(item) {
  const article = document.createElement("article");
  article.className = `review-item ${item.status}`;
  const head = document.createElement("div");
  head.className = "review-item-head";
  const title = document.createElement("a");
  title.className = "review-skill-link";
  title.href = `/?skill=${encodeURIComponent(item.name)}`;
  title.textContent = item.name;
  title.title = item.title || item.name;
  const status = badge(usageStatusLabel(item.status), usageStatusTone(item.status));
  const meta = document.createElement("span");
  meta.className = "review-item-meta";
  meta.textContent = formatRelativeUsage(item);
  head.append(title, status, meta);

  const details = document.createElement("div");
  details.className = "review-item-details";
  const counts = document.createElement("span");
  counts.textContent = `确认 ${item.confirmedEvidenceCount || 0} · 声明 ${item.announcementEvidenceCount || 0} · ${item.category || "未分类"}`;
  details.appendChild(counts);

  const evidenceList = document.createElement("div");
  evidenceList.className = "review-evidence";
  const evidence = (item.evidence?.length ? item.evidence : item.announcements || []).slice(0, 3);
  if (!evidence.length) {
    const empty = document.createElement("p");
    empty.textContent = "没有发现 SKILL.md 读取证据。";
    evidenceList.appendChild(empty);
  } else {
    for (const evidenceItem of evidence) {
      const row = document.createElement("p");
      const label = document.createElement("span");
      label.textContent = `${evidenceItem.type === "skill-file-read" ? "确认" : "声明"} L${evidenceItem.line || "?"}`;
      row.append(label, document.createTextNode(evidenceItem.snippet || evidenceItem.title || ""));
      evidenceList.appendChild(row);
    }
  }

  article.append(head, details, evidenceList);
  return article;
}

function renderReview() {
  const review = reviews[state.activeReview];
  $("reviewTitle").textContent = review.title;
  $("reviewDescription").textContent = review.description;
  $("reviewProgress").hidden = !state.running;
  $("reviewProgress").setAttribute("aria-busy", state.running ? "true" : "false");
  $("runReviewButton").textContent = state.running ? "审查中" : "开始审查";
  $("runReviewButton").disabled = state.running;
  document.querySelectorAll(".review-type").forEach((button) => {
    button.classList.toggle("active", button.dataset.review === state.activeReview);
  });

  if (state.running) {
    $("reviewSummary").replaceChildren(document.createElement("span"));
    $("reviewSummary").firstElementChild.textContent = review.runningSummary();
    $("reviewPolicy").textContent = review.runningPolicy;
    $("reviewResults").replaceChildren();
    return;
  }

  const payload = state.results.get(state.activeReview);
  if (!payload) {
    $("reviewSummary").replaceChildren(document.createElement("span"));
    $("reviewSummary").firstElementChild.textContent = "尚未运行审查";
    $("reviewPolicy").textContent = "";
    $("reviewResults").replaceChildren();
    return;
  }
  review.renderResult(payload);
}

async function runActiveReview() {
  const review = reviews[state.activeReview];
  state.running = true;
  state.results.delete(state.activeReview);
  renderReview();
  setStatus("正在审查技能问题");
  try {
    const payload = await review.run();
    state.results.set(state.activeReview, payload);
    const warnings = payload.warnings || [];
    if (warnings.some((item) => item.code === "empty-scope")) {
      setStatus("审查完成，当前范围内没有技能");
    } else if (warnings.some((item) => item.code === "no-session-files")) {
      setStatus(`审查完成，但没有扫描到会话证据；发现 ${payload.stats?.issues || 0} 个缺少证据的技能`);
    } else {
      setStatus(`审查完成，发现 ${payload.stats?.issues || 0} 个需要关注的技能`);
    }
  } finally {
    state.running = false;
    renderReview();
  }
}

function bindEvents() {
  $("reviewTypeCount").textContent = Object.keys(reviews).length;
  document.querySelectorAll(".review-type").forEach((button) => {
    button.addEventListener("click", () => {
      if (state.running) return;
      state.activeReview = button.dataset.review || "usage";
      renderReview();
    });
  });
  $("runReviewButton").addEventListener("click", () => runActiveReview().catch((error) => {
    setStatus(error.message);
    state.running = false;
    renderReview();
  }));
}

bindEvents();
renderReview();
