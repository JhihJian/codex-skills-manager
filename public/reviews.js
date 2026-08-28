const initialRoute = new URLSearchParams(window.location.search);
const state = {
  view: initialRoute.get("view") || "quality", overview: null, selectedCase: initialRoute.get("case"), selectedFeedback: initialRoute.get("feedback"),
  skillFilter: initialRoute.get("skill") || "", feedbackChannel: "", feedbackSeverity: "",
  feedbackResolution: "", feedbackItems: [], feedbackNextCursor: null, busy: false,
  qualitySubject: initialRoute.get("skill") && initialRoute.get("sha") ? {
    skillId: initialRoute.get("skill"), sha: initialRoute.get("sha") || null,
  } : null,
  qualityTab: initialRoute.get("tab") || "overview", qualityObservation: initialRoute.get("observation") || "",
  qualityAttribution: initialRoute.get("attribution") || "", qualityTaskType: initialRoute.get("taskType") || "",
  qualitySource: initialRoute.get("source") || "", qualityFrom: initialRoute.get("from") || "", qualityTo: initialRoute.get("to") || "",
  qualityCompare: [], qualityJudgmentInvocation: null, qualityReturnCase: null,
  qualityItems: [], qualityNextCursor: null,
};
const $ = (id) => document.getElementById(id);

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}

function setStatus(text) {
  $("statusText").textContent = text;
  $("reviewPageStatus").textContent = text;
}

async function api(path, options = {}) {
  const response = await window.skillAuth.fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function badge(text, tone = "") {
  return element("span", `badge ${tone}`.trim(), text);
}

function toneFor(value) {
  if (["pass", "assessable", "loaded", "complete", "active", "resolved-verified"].includes(value)) return "green";
  if (["fail", "error", "blocked", "invalidated", "disputed", "critical", "high", "action-required"].includes(value)) return "red";
  if (["partial", "needs-evidence", "unknown", "pending", "inconclusive", "exception-accepted", "medium", "awaiting-verification", "fix-in-progress"].includes(value)) return "orange";
  return "";
}

function label(value) {
  return {
    loaded: "加载成功", pending: "等待结果", "result-missing": "结果缺失", error: "加载错误",
    unobserved: "尚未观察", "only-loaded": "仅检测到加载", "evidence-insufficient": "证据不足",
    directional: "方向性结论", "judgment-supported": "证据支持达到合同阈值", "threshold-not-met": "证据支持未达到合同阈值", "not-publishable": "不可发布",
    helpful: "有帮助", "not-helpful": "没帮助", "cannot-judge": "无法判断",
    "not-applicable": "不适用", "direct-skill-use": "该技能调用直接相关",
    "task-result-only": "仅与任务结果相关", "cannot-attribute": "无法归因",
    blocked: "已阻止", cancelled: "已取消", pass: "通过", partial: "部分通过", fail: "失败",
    unset: "未形成结论", assessable: "可评审", "needs-evidence": "证据不足",
    "not-assessable": "不可评审", direct: "直接参与", shared: "共享参与", candidate: "候选关系",
    rejected: "已排除", "exception-accepted": "例外接受", disputed: "结论冲突",
    "resolved-by-correction": "已由纠正解决", "manual-decision": "人工裁决",
    "manual-disposition": "人工处置", automatic: "自动评审", exception: "业务例外",
    "user-feedback": "用户反馈", "process-anomaly": "过程异常", "assistant-claim": "助手线索",
    "result-rejection": "结果否定", "observed-defect": "故障反馈",
    "requirement-gap": "需求遗漏", "rework-correction": "返工纠正",
    "process-critique": "过程批评", "external-negative-acceptance": "外部拒绝",
    "mixed-or-unclear": "混合评价", "agent-unavailable": "代理不可用",
    "dispatch-not-executed": "计划未执行", "partial-dispatch": "部分执行",
    "child-result-missing": "子结果缺失", "tool-error": "工具错误",
    "tool-blocked": "工具阻止", "tool-timeout": "工具超时",
    "task-result": "任务结果", "assistant-result": "助手结果",
    "skill-invocation": "技能调用", "tool-call": "工具调用", "tool-result": "工具结果",
    queued: "待处理", claimed: "已领取", triaged: "已确认", "action-required": "需要修复",
    "fix-in-progress": "修复中", "awaiting-verification": "待验证", closed: "已关闭",
    unreviewed: "未评审", "resolved-verified": "已验证解决",
    "resolved-unverified": "未验证关闭", "false-positive": "误报",
    duplicate: "重复", "not-actionable": "不可行动", critical: "严重", high: "高",
    medium: "中", low: "低",
    confirm: "确认反馈", exclude: "标记误报", retarget: "重新指向",
    "mark-duplicate": "标记重复", "start-fix": "开始修复",
    "request-verification": "请求验证", "resolve-verified": "验证解决",
    "resolve-unverified": "未验证关闭", reopen: "重新打开", detected: "检测到",
    superseded: "已替代", orphaned: "证据失效", reactivated: "证据恢复",
    "target-disputed": "目标争议", "previous-episode-result": "上一轮结果",
    "same-message-reference": "消息明确引用", "explicit-reference": "显式引用",
    classified: "已分类", "needs-human": "需要人工",
    complete: "完整", partial_data: "部分数据", unknown: "未知",
  }[value] || value || "未知";
}

function shortSha(value) {
  return value ? String(value).slice(0, 10) : "版本未知";
}

function coverageLabel(value) {
  return { complete: "完整覆盖", partial: "部分覆盖", unknown: "覆盖未知" }[value] || "覆盖未知";
}

function dateText(value) {
  if (!value) return "时间未知";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function sectionHead(title, description = "") {
  const head = element("div", "outcome-section-head");
  const copy = element("div");
  copy.append(element("h1", "", title));
  if (description) copy.append(element("p", "", description));
  head.append(copy);
  return head;
}

function actionButton(text, action, className = "secondary-button") {
  const button = element("button", className, text);
  button.type = "button";
  button.addEventListener("click", async () => {
    try {
      await action();
    } catch (error) {
      setStatus(error.message);
    }
  });
  return button;
}

function renderOverview() {
  const overview = state.overview || {};
  const scan = overview.latest_scan || {};
  const metrics = [
    ["规范事件", overview.event_count || 0],
    ["任务案例", overview.task_case_count || 0],
    ["可评审", overview.assessable_count || 0],
    ["证据不足", overview.needs_evidence_count || 0],
    ["待人工", overview.open_review_count || 0],
    ["覆盖", coverageLabel(scan.coverage_status || "unknown")],
  ];
  $("effectOverview").replaceChildren(...metrics.map(([name, value]) => {
    const metric = element("div", "outcome-overview-item");
    metric.append(element("span", "", name), element("strong", "", value));
    return metric;
  }));
  $("openQueueCount").textContent = overview.open_review_count || 0;
  $("updatedAt").textContent = scan.finished_at ? `索引 ${dateText(scan.finished_at)}` : "尚未建立结果索引";
}

async function loadOverview() {
  state.overview = await api("/api/effect-overview");
  renderOverview();
}

function filterBar({ skill = true, status = false } = {}) {
  const bar = element("div", "outcome-filterbar");
  if (skill) {
    const input = element("input");
    input.type = "search";
    input.placeholder = "筛选技能名称";
    input.value = state.skillFilter;
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { state.skillFilter = input.value.trim(); renderView(); }, 250);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { clearTimeout(timer); state.skillFilter = input.value.trim(); renderView(); }
      if (event.key === "Escape") { clearTimeout(timer); input.value = ""; state.skillFilter = ""; renderView(); }
    });
    bar.append(input);
  }
  if (status) {
    const select = element("select");
    [["", "全部加载状态"], ["loaded", "加载成功"], ["result-missing", "结果缺失"], ["error", "加载错误"]]
      .forEach(([value, text]) => select.append(new Option(text, value)));
    select.addEventListener("change", () => { select.dataset.value = select.value; renderLoads(select.value); });
    bar.append(select);
  }
  return bar;
}

function resultRow(item, mode) {
  const row = element("div", "outcome-table-row");
  const skill = element("strong", "", item.skill_id || "未知技能");
  const version = element("span", "outcome-mono", shortSha(item.skill_sha256));
  const primary = element("div", "outcome-row-primary");
  primary.append(skill, version);
  row.append(primary);
  if (mode === "loads") {
    row.append(badge(label(item.load_status), toneFor(item.load_status)));
    row.append(element("span", "", label(item.attribution_kind)));
  } else {
    const verdict = item.effective_verdict || item.automated_verdict || item.assessability;
    row.append(badge(label(verdict), toneFor(verdict)));
    row.append(element("span", "outcome-mono", item.contract_version_id ? item.contract_version_id.slice(0, 8) : "无合同"));
  }
  row.append(element("time", "", dateText(item.created_at)));
  if (item.task_case_id) row.append(actionButton("查看", async () => openCase(item.task_case_id), "text-button"));
  else row.append(element("span", "outcome-muted", "尚未形成 Case"));
  row.append(actionButton("质量", async () => openQuality(item.skill_id, item.skill_sha256 || null), "text-button"));
  return row;
}

async function renderLoads(status = "") {
  const content = $("outcomeContent");
  const head = sectionHead("检测到技能加载", "加载记录只证明技能内容被请求并返回，不代表任务成功或技能产生价值。");
  head.append(filterBar({ skill: true, status: true }));
  content.replaceChildren(head, element("div", "loading-state", "正在读取加载记录"));
  const query = new URLSearchParams({ limit: "300" });
  if (state.skillFilter) query.set("skill", state.skillFilter);
  if (status) query.set("status", status);
  const payload = await api(`/api/skill-use-events?${query}`);
  const table = element("div", "outcome-table");
  table.append(element("div", "outcome-table-head", "技能 / 状态 / 参与关系 / 时间"));
  (payload.items || []).forEach((item) => table.append(resultRow(item, "loads")));
  if (!payload.items?.length) table.append(element("div", "empty-state", "当前筛选条件下没有检测到技能加载。"));
  content.replaceChildren(head, table);
}

async function renderOutcomes() {
  const content = $("outcomeContent");
  const head = sectionHead("使用该技能的任务结果", "结论按技能 SHA、合同版本、任务类型和参与关系分别展示。");
  head.append(filterBar());
  content.replaceChildren(head, element("div", "loading-state", "正在读取任务结果"));
  const query = new URLSearchParams({ limit: "300" });
  if (state.skillFilter) query.set("skill", state.skillFilter);
  const payload = await api(`/api/skill-use-events?${query}`);
  const table = element("div", "outcome-table");
  table.append(element("div", "outcome-table-head", "技能 / 当前结论 / 合同 / 时间"));
  (payload.items || []).forEach((item) => table.append(resultRow(item, "outcomes")));
  if (!payload.items?.length) table.append(element("div", "empty-state", "尚无任务结果记录。先扫描会话并评审案例。"));
  content.replaceChildren(head, table);
}

function feedbackTargetText(item) {
  const identifier = item.skill_invocation_id || item.tool_call_id || item.tool_result_id
    || item.target_task_case_id || item.context_task_case_id || "目标待确认";
  return `${label(item.target_kind)} · ${String(identifier).slice(0, 12)}`;
}

function feedbackFilterBar() {
  const bar = element("div", "outcome-filterbar feedback-filterbar");
  const channel = element("select");
  [["", "全部来源"], ["user-feedback", "用户反馈"], ["process-anomaly", "过程异常"]]
    .forEach(([value, text]) => channel.append(new Option(text, value)));
  channel.value = state.feedbackChannel;
  const severity = element("select");
  [["", "全部级别"], ["critical", "严重"], ["high", "高"], ["medium", "中"], ["low", "低"]]
    .forEach(([value, text]) => severity.append(new Option(text, value)));
  severity.value = state.feedbackSeverity;
  const resolution = element("select");
  [["", "全部状态"], ["unreviewed", "未评审"], ["action-required", "需要修复"],
    ["fix-in-progress", "修复中"], ["awaiting-verification", "待验证"],
    ["resolved-verified", "已验证解决"], ["false-positive", "误报"]]
    .forEach(([value, text]) => resolution.append(new Option(text, value)));
  resolution.value = state.feedbackResolution;
  const apply = async () => {
    state.feedbackChannel = channel.value;
    state.feedbackSeverity = severity.value;
    state.feedbackResolution = resolution.value;
    await renderFeedback();
  };
  channel.addEventListener("change", () => apply().catch((error) => setStatus(error.message)));
  severity.addEventListener("change", () => apply().catch((error) => setStatus(error.message)));
  resolution.addEventListener("change", () => apply().catch((error) => setStatus(error.message)));
  bar.append(channel, severity, resolution);
  return bar;
}

async function renderFeedback(reset = true) {
  const content = $("outcomeContent");
  const head = sectionHead("会话负面反馈");
  head.append(feedbackFilterBar());
  content.replaceChildren(head, element("div", "loading-state", "正在读取反馈信号"));
  if (reset) {
    state.feedbackItems = [];
    state.feedbackNextCursor = null;
  }
  const query = new URLSearchParams({ limit: "100" });
  if (state.feedbackChannel) query.set("channel", state.feedbackChannel);
  if (state.feedbackSeverity) query.set("severity", state.feedbackSeverity);
  if (state.feedbackResolution) query.set("resolutionState", state.feedbackResolution);
  if (!reset && state.feedbackNextCursor) query.set("cursor", state.feedbackNextCursor);
  const payload = await api(`/api/feedback-signals?${query}`);
  state.feedbackItems.push(...(payload.items || []));
  state.feedbackNextCursor = payload.next_cursor || null;
  const summary = state.overview?.feedback || {};
  const band = element("section", "feedback-summary-band");
  [["用户反馈", summary.user_feedback || 0], ["过程异常", summary.process_anomalies || 0],
    ["待处理", summary.open || 0], ["待验证", summary.awaiting_verification || 0],
    ["已解决", summary.resolved || 0], ["误报", summary.false_positives || 0]]
    .forEach(([name, value]) => {
      const metric = element("div", "feedback-summary-item");
      metric.append(element("span", "", name), element("strong", "", value));
      band.append(metric);
    });
  const table = element("div", "outcome-table feedback-table");
  table.append(element("div", "outcome-table-head", "评价 / 类别 / 状态 / 目标 / 时间"));
  for (const item of state.feedbackItems) {
    const row = element("div", "outcome-table-row feedback-row");
    const primary = element("div", "feedback-primary");
    primary.append(element("strong", "", item.redacted_excerpt || "正文已清理"));
    primary.append(element("span", "outcome-mono", `${label(item.channel)} · ${(item.confidence * 100).toFixed(0)}%`));
    const classification = element("div", "feedback-badges");
    classification.append(badge(label(item.category)), badge(label(item.severity), toneFor(item.severity)));
    const status = element("div", "feedback-badges");
    status.append(badge(label(item.current_process_state), toneFor(item.current_process_state)));
    status.append(badge(label(item.current_resolution_state), toneFor(item.current_resolution_state)));
    row.append(primary, classification, status, element("span", "", feedbackTargetText(item)),
      element("time", "", dateText(item.observed_at)),
      actionButton("查看", async () => openFeedback(item.id), "text-button"));
    table.append(row);
  }
  if (!state.feedbackItems.length) table.append(element("div", "empty-state", "当前筛选条件下没有反馈信号。"));
  if (state.feedbackNextCursor) {
    table.append(actionButton("加载更多", () => renderFeedback(false), "secondary-button feedback-load-more"));
  }
  content.replaceChildren(head, band, table);
}

function targetTitle(target) {
  const identifier = target.skill_invocation_id || target.tool_call_id || target.tool_result_id
    || target.target_event_id || target.target_task_case_id || "未知";
  return `${label(target.target_kind)} · ${String(identifier).slice(0, 16)}`;
}

function feedbackActionPanel(detail) {
  const panel = element("section", "feedback-action-panel");
  const revision = detail.current_action_revision;
  const review = detail.review_task;
  const currentTargets = (detail.targets || []).filter((item) => item.machine_status === "candidate");
  const targetSelect = element("select");
  currentTargets.forEach((target) => targetSelect.append(new Option(targetTitle(target), target.id)));
  const reason = element("input");
  reason.placeholder = "原因代码";
  reason.value = "feedback-reviewed";
  const actorId = window.skillAuth.state.actor?.uuid;
  if (review?.claimed_by_actor_id && review.claimed_by_actor_id !== actorId) {
    panel.append(element("p", "outcome-muted", "该反馈已由其他评审者领取。"));
    return panel;
  }
  if (review && !review.claimed_by_actor_id && review.status === "open") {
    panel.append(actionButton("领取", async () => {
      await api(`/api/feedback-signals/${detail.id}/claim`, {
        method: "POST", body: JSON.stringify({ expectedRevision: revision }),
      });
      await openFeedback(detail.id);
    }, "primary-button"));
    return panel;
  }
  panel.append(actionButton("语义复核", async () => {
    await api(`/api/feedback-signals/${detail.id}/semantic-classify`, {
      method: "POST", body: "{}",
    });
    await openFeedback(detail.id);
  }));
  const submit = async (action, binding = {}) => {
    await api(`/api/feedback-signals/${detail.id}/actions`, {
      method: "POST", body: JSON.stringify({
        expectedRevision: revision, action, reasonCode: reason.value.trim(),
        targetId: targetSelect.value || null, binding,
      }),
    });
    await Promise.all([loadOverview(), openFeedback(detail.id)]);
  };
  const row = element("div", "outcome-action-row");
  row.append(reason);
  if (["queued", "claimed", "candidate", "triaged"].includes(detail.current_process_state)) {
    if (currentTargets.length) row.append(targetSelect, actionButton("确认", () => submit("confirm"), "primary-button"));
    if (currentTargets.length > 1) row.append(actionButton("重新指向", () => submit("retarget")));
    row.append(actionButton("误报", () => submit("exclude"), "secondary-button danger-button"));
    row.append(actionButton("重复", () => submit("mark-duplicate")));
  }
  if (detail.current_resolution_state === "action-required") row.append(actionButton("开始修复", () => submit("start-fix"), "primary-button"));
  if (detail.current_resolution_state === "fix-in-progress") row.append(actionButton("请求验证", () => submit("request-verification"), "primary-button"));
  if (detail.current_resolution_state === "awaiting-verification") {
    const verification = element("input"); verification.placeholder = "验证 Evidence ID";
    row.append(verification, actionButton("验证解决", () => {
      const evidenceId = verification.value.trim();
      if (!evidenceId) throw new Error("请输入验证 Evidence ID");
      return submit("resolve", { evidenceId });
    }, "primary-button"));
    row.append(actionButton("未验证关闭", () => submit("resolve-unverified")));
  }
  if (["resolved-verified", "resolved-unverified", "false-positive", "duplicate", "not-actionable"].includes(detail.current_resolution_state)) {
    row.append(actionButton("重新打开", () => submit("reopen")));
  }
  panel.append(row);
  return panel;
}

async function openFeedback(signalId) {
  if (state.view === "quality" && state.selectedCase) state.qualityReturnCase = state.selectedCase;
  state.selectedFeedback = signalId;
  state.selectedCase = null;
  if (state.view === "quality") qualityRoute();
  const content = $("outcomeContent");
  content.replaceChildren(element("div", "loading-state", "正在读取反馈详情"));
  const detail = await api(`/api/feedback-signals/${encodeURIComponent(signalId)}`);
  const current = (detail.machine_revisions || []).find((item) => item.is_current)
    || detail.machine_revisions?.at(-1) || {};
  const head = sectionHead(label(current.category), current.redacted_excerpt || "正文已清理");
  head.append(actionButton("返回", async () => {
    state.selectedFeedback = null;
    if (state.qualityReturnCase) {
      const returnCase = state.qualityReturnCase;
      state.qualityReturnCase = null;
      state.selectedCase = returnCase;
      qualityRoute({ caseId: returnCase, feedbackId: null, replace: true });
      await openCase(returnCase);
    } else if (state.qualitySubject) {
      state.qualityTab = "feedback";
      qualityRoute({ feedbackId: null, replace: true });
      await renderQualityDetail();
    } else await renderFeedback();
  }, "text-button"));
  const layout = element("div", "feedback-detail-layout");
  const evidence = element("section", "feedback-evidence-pane");
  evidence.append(element("h2", "", "信号与目标"));
  evidence.append(timelineItem(label(current.channel), label(current.severity), dateText(current.observed_at),
    `置信度 ${(Number(current.confidence || 0) * 100).toFixed(0)}% · ${current.detector_version}`));
  for (const target of detail.targets || []) {
    const item = timelineItem("目标", targetTitle(target), label(target.machine_status),
      `${label(target.relation)} · ${(Number(target.confidence || 0) * 100).toFixed(0)}%`);
    if (target.id === detail.current_confirmed_target_id) item.classList.add("confirmed-target");
    const caseId = target.target_task_case_id || target.context_task_case_id;
    if (caseId) item.append(actionButton("查看案例", () => openCase(caseId), "text-button"));
    evidence.append(item);
  }
  const history = element("section", "feedback-history-pane");
  history.append(element("h2", "", "处理记录"));
  for (const action of detail.actions || []) history.append(timelineItem(
    label(action.action), action.reason_code, dateText(action.created_at),
    `${label(action.from_resolution_state)} → ${label(action.to_resolution_state)}`,
  ));
  for (const review of detail.semantic_reviews || []) history.append(timelineItem(
    "语义", label(review.verdict), dateText(review.created_at),
    `${label(review.category)} · ${(Number(review.confidence || 0) * 100).toFixed(0)}% · ${review.model_version}`,
  ));
  history.append(feedbackActionPanel(detail));
  layout.append(evidence, history);
  content.replaceChildren(head, layout);
}

async function renderQueue() {
  const content = $("outcomeContent");
  const head = sectionHead("人工评审队列", "证据不足、适用性未知、确定性失败和冲突案例在此处理。");
  content.replaceChildren(head, element("div", "loading-state", "正在读取队列"));
  const payload = await api("/api/review-tasks?status=open&limit=300");
  const table = element("div", "outcome-table");
  table.append(element("div", "outcome-table-head", "技能 / 队列原因 / 自动结论 / 操作"));
  for (const item of payload.items || []) {
    const row = element("div", "outcome-table-row queue-row");
    row.append(element("strong", "", item.feedback_signal_id ? "会话负面反馈" : (item.skill_id || "技能版本未知")));
    row.append(badge(label(item.queue_reason), "orange"));
    row.append(badge(label(item.automated_verdict), toneFor(item.automated_verdict)));
    row.append(element("span", "", item.task_type || "任务类型未知"));
    row.append(actionButton(item.claimed_by_actor_id ? "继续评审" : "领取并评审", async () => {
      if (item.feedback_signal_id) {
        await openFeedback(item.feedback_signal_id);
        return;
      }
      if (!item.claimed_by_actor_id) {
        await api(`/api/review-tasks/${item.id}/claim`, { method: "POST", body: "{}" });
      }
      await openCase(item.task_case_id);
    }, "primary-button compact-button"));
    table.append(row);
  }
  if (!payload.items?.length) table.append(element("div", "empty-state", "当前没有待人工处理的案例。"));
  content.replaceChildren(head, table);
}

function timelineItem(kind, title, meta, detail = "") {
  const item = element("div", "outcome-timeline-item");
  item.append(badge(kind), element("strong", "", title), element("time", "", meta));
  if (detail) item.append(element("p", "", detail));
  return item;
}

async function submitReview(caseId, invocationId) {
  await api(`/api/task-cases/${encodeURIComponent(caseId)}/review`, {
    method: "POST", body: JSON.stringify({ skillInvocationId: invocationId }),
  });
  setStatus("自动评审已生成新 revision");
  await Promise.all([loadOverview(), openCase(caseId)]);
}

function reviewActions(detail) {
  const panel = element("div", "outcome-action-panel");
  const current = [...(detail.assessments || [])].reverse().find(
    (item) => item.is_current && !String(item.subject_key || "").startsWith("feedback:"),
  );
  const task = (detail.review_tasks || []).find((item) => current && item.assessment_id === current.id);
  const invocation = (detail.invocations || []).find((item) => item.load_status === "loaded" && item.validity === "valid");
  const row = element("div", "outcome-action-row");
  if (invocation) row.append(actionButton("重新评审", () => submitReview(detail.case.id, invocation.id), "secondary-button"));
  if (current && task?.queue_reason === "semantic-review-required") {
    row.append(actionButton("运行语义评审", async () => {
      await api(`/api/task-cases/${detail.case.id}/semantic-review`, {
        method: "POST", body: JSON.stringify({ assessmentId: current.id }),
      });
      setStatus("语义评审已完成");
      await Promise.all([loadOverview(), openCase(detail.case.id)]);
    }, "primary-button"));
  }
  if (!task || !current) {
    panel.append(row, element("p", "outcome-muted", "生成 assessment 后可提交人工裁决。"));
    return panel;
  }
  const verdict = element("select");
  [["pass", "通过"], ["partial", "部分通过"], ["fail", "失败"]].forEach(([value, text]) => {
    const option = new Option(text, value);
    if (current.hard_failure && value === "pass") option.disabled = true;
    verdict.append(option);
  });
  if (current.hard_failure) verdict.value = "fail";
  const reason = element("input"); reason.placeholder = "原因代码"; reason.value = "manual-review";
  const note = element("input"); note.placeholder = "备注（可选）";
  if (current.assessability === "assessable") {
    row.append(verdict, reason, note, actionButton("提交裁决", async () => {
      await api(`/api/review-tasks/${task.id}/decision`, {
        method: "PUT", body: JSON.stringify({
          expectedRevision: task.current_decision_revision, verdict: verdict.value,
          reasonCode: reason.value.trim(), note: note.value.trim(),
        }),
      });
      setStatus("人工裁决已追加");
      await Promise.all([loadOverview(), openCase(detail.case.id)]);
    }, "primary-button"));
  }
  const dispositionRow = element("div", "outcome-action-row");
  const disposition = element("select");
  disposition.append(new Option("不可评审", "not-assessable"), new Option("需要证据", "needs-evidence"));
  dispositionRow.append(disposition, actionButton("提交处置", async () => {
    await api(`/api/review-tasks/${task.id}/disposition`, {
      method: "PUT", body: JSON.stringify({ expectedRevision: task.current_decision_revision, disposition: disposition.value, reasonCode: "manual-disposition" }),
    });
    await openCase(detail.case.id);
  }));
  const correctionRow = element("div", "outcome-action-row");
  const taskType = element("input"); taskType.placeholder = "纠正后的任务类型";
  correctionRow.append(taskType, actionButton("追加纠正", async () => {
    const revision = Math.max(0, ...(detail.corrections || []).map((item) => item.revision));
    await api(`/api/task-cases/${detail.case.id}/corrections`, {
      method: "POST", body: JSON.stringify({ expectedRevision: revision, correctionType: "task-type", reasonCode: "manual-correction", assessmentId: current.id, payload: { task_type: taskType.value.trim() } }),
    });
    await Promise.all([loadOverview(), openCase(detail.case.id)]);
  }));
  const exceptionRow = element("div", "outcome-action-row");
  exceptionRow.append(element("span", "outcome-muted", "业务接受例外会排除正常结果率"), actionButton("接受例外", async () => {
    const revision = Math.max(0, ...(detail.exceptions || []).map((item) => item.revision));
    await api(`/api/task-cases/${detail.case.id}/exception`, {
      method: "POST", body: JSON.stringify({ expectedRevision: revision, assessmentId: current.id, reasonCode: "business-exception", scope: { mode: "single-case" } }),
    });
    await openCase(detail.case.id);
  }, "secondary-button danger-button"));
  panel.append(row, dispositionRow, correctionRow, exceptionRow);
  return panel;
}

async function openCase(caseId) {
  state.selectedCase = caseId;
  state.selectedFeedback = null;
  if (state.view === "quality") qualityRoute();
  const content = $("outcomeContent");
  content.replaceChildren(element("div", "loading-state", "正在读取案例证据"));
  let detail;
  try {
    detail = await api(`/api/task-cases/${encodeURIComponent(caseId)}`);
  } catch (error) {
    const panel = element("section", "quality-error-state");
    panel.append(element("h2", "", "无法读取案例"), element("p", "", error.message),
      actionButton("重试", () => openCase(caseId), "primary-button"));
    if (state.view === "quality") panel.append(actionButton("返回质量页", () => {
      state.selectedCase = null;
      qualityRoute({ caseId: null, replace: true });
      return renderQualityDetail();
    }, "secondary-button"));
    content.replaceChildren(panel);
    return;
  }
  const head = sectionHead(detail.case.task_type || "任务案例", `Case ${detail.case.id.slice(0, 12)} · revision ${detail.case.current_revision}`);
  head.append(actionButton("返回", async () => {
    state.selectedCase = null;
    if (state.view === "quality") qualityRoute({ caseId: null, replace: true });
    await renderView();
  }, "text-button"));
  const layout = element("div", "case-detail-layout");
  const timeline = element("section", "case-timeline");
  timeline.append(element("h2", "", "证据时间线"));
  for (const episode of detail.episodes || []) timeline.append(timelineItem("目标", episode.goal_text || "未恢复用户目标", dateText(episode.created_at), episode.process_state));
  for (const invocation of detail.invocations || []) timeline.append(timelineItem("技能", invocation.skill_id, dateText(invocation.created_at), `${label(invocation.load_status)} · ${shortSha(invocation.skill_sha256)} · ${label(invocation.attribution_kind)}`));
  for (const call of detail.tool_calls || []) timeline.append(timelineItem("工具", call.tool_name, dateText(call.called_at), call.result_status ? `结果：${call.result_status}` : "结果缺失"));
  for (const check of detail.checks || []) timeline.append(timelineItem("检查", check.checker_id, dateText(check.finished_at), `${check.assertion_outcome || check.status} · freshness ${check.freshness}`));
  for (const review of detail.semantic_reviews || []) timeline.append(timelineItem(
    "语义", review.id, dateText(review.created_at),
    `${label(review.verdict)} · assessment ${review.assessment_id?.slice(0, 8) || "未知"} · ${review.model_version}`,
  ));
  for (const feedback of detail.feedback || []) {
    const revision = (feedback.machine_revisions || []).find((item) => item.is_current) || {};
    const item = timelineItem(
      label(revision.channel), label(revision.category), dateText(revision.observed_at),
      `${revision.redacted_excerpt || "正文已清理"} · ${label(feedback.current_resolution_state)}`,
    );
    item.classList.add("feedback-timeline-item");
    item.addEventListener("click", () => openFeedback(feedback.id).catch((error) => setStatus(error.message)));
    timeline.append(item);
  }
  for (const evidence of detail.evidence || []) {
    const locator = evidence.locator?.line ? `generation ${shortSha(evidence.locator.generationId)} · line ${evidence.locator.line}` : JSON.stringify(evidence.locator || {});
    timeline.append(timelineItem("证据", evidence.evidenceId, dateText(evidence.observedAt), `${shortSha(evidence.contentHash)} · ${locator}`));
  }
  const side = element("aside", "case-assessment-side");
  side.append(element("h2", "", "评审结论"));
  for (const assessment of [...(detail.assessments || [])].reverse()) {
    const item = element("div", "assessment-item");
    const verdict = assessment.effective_verdict || assessment.automated_verdict || assessment.assessability;
    item.append(badge(`r${assessment.revision}`), badge(label(verdict), toneFor(verdict)));
    item.append(element("strong", "", assessment.skill_id || "技能未绑定"));
    item.append(element("p", "", `合同 ${assessment.contract_version_id?.slice(0, 8) || "缺失"} · freshness ${assessment.freshness}`));
    if (assessment.effective_source && assessment.effective_source !== "automatic") item.append(element("p", "outcome-muted", `有效来源：${label(assessment.effective_source)} · ${label(assessment.conflict_state)}`));
    if (assessment.hard_failure) item.append(badge("有效硬失败", "red"));
    side.append(item);
  }
  side.append(element("h2", "", "人工操作"));
  for (const decision of detail.decisions || []) side.append(timelineItem(decision.action, label(decision.verdict), dateText(decision.created_at), decision.reason_code));
  side.append(reviewActions(detail));
  layout.append(timeline, side);
  content.replaceChildren(head, layout);
}

async function renderContracts() {
  const content = $("outcomeContent");
  const head = sectionHead("结果评审合同", "合同绑定精确 SKILL.md SHA。发布后不可修改，只能创建新版本。");
  const controls = element("div", "outcome-filterbar");
  const input = element("input"); input.placeholder = "技能名称"; input.value = state.skillFilter;
  const load = actionButton("查询", async () => { state.skillFilter = input.value.trim(); await renderContracts(); }, "secondary-button");
  const createGradle = actionButton("新建 Gradle 合同", async () => createContract(input.value, "gradle"), "secondary-button");
  const createDocument = actionButton("新建文档合同", async () => createContract(input.value, "document"), "secondary-button");
  controls.append(input, load, createGradle, createDocument);
  head.append(controls);
  content.replaceChildren(head);
  if (!state.skillFilter) {
    content.append(element("div", "empty-state", "输入技能名称以读取精确版本合同。"));
    return;
  }
  const payload = await api(`/api/skills/${encodeURIComponent(state.skillFilter)}/outcome-contracts`);
  const list = element("div", "contract-list");
  for (const contract of payload.contracts || []) {
    const item = element("article", "contract-item");
    const title = element("div", "contract-title");
    title.append(element("strong", "", `v${contract.version}`), badge(contract.status, toneFor(contract.status)), element("span", "outcome-mono", shortSha(contract.skill_sha256)));
    if (contract.status === "draft") title.append(actionButton("发布", async () => {
      await api(`/api/outcome-contracts/${contract.id}/publish`, { method: "POST", body: "{}" });
      await renderContracts();
    }, "primary-button compact-button"));
    const pre = element("pre", "contract-json", JSON.stringify(contract.contract, null, 2));
    item.append(title, pre);
    list.append(item);
  }
  if (!payload.contracts?.length) list.append(element("div", "empty-state", "该技能尚无结果评审合同。"));
  content.append(list);
}

async function createContract(rawSkill, template) {
  const skill = rawSkill.trim();
  if (!skill) throw new Error("请输入技能名称");
  state.skillFilter = skill;
  await api(`/api/skills/${encodeURIComponent(skill)}/outcome-contracts`, {
    method: "POST", body: JSON.stringify({ template }),
  });
  setStatus("合同草稿已创建");
  await renderContracts();
}

async function renderMetrics() {
  const content = $("outcomeContent");
  const head = sectionHead("结果指标", "实时预览不进入历史趋势；正式快照冻结案例、结论、覆盖和版本元组。");
  content.replaceChildren(head, element("div", "loading-state", "正在读取指标"));
  const payload = await api("/api/effect-metrics");
  const preview = element("section", "metric-band");
  preview.append(element("div", "metric-big", payload.preview.caseCount), element("div", "", "当前候选案例"), badge("非正式预览", "orange"));
  preview.append(actionButton("创建正式快照", async () => {
    const coverage = state.overview?.latest_scan?.coverage_status || "partial";
    await api("/api/effect-metric-snapshots", {
      method: "POST", body: JSON.stringify({ cutoffAt: new Date().toISOString(), coverageStatus: coverage, dimensions: {}, versions: { parser: "outcome-reviews-v1" } }),
    });
    await renderMetrics();
  }, "primary-button"));
  const latestReport = payload.snapshots?.[0]?.report;
  const report = element("section", "metric-report");
  if (latestReport) {
    report.append(element("h2", "", "最近正式快照口径"));
    report.append(element("p", "outcome-muted", `纳入 ${latestReport.included} · 排除 ${latestReport.excluded} · 覆盖 ${label(latestReport.coverageStatus)}`));
    for (const group of latestReport.groups || []) {
      const row = element("div", "metric-group-row");
      const passRate = group.rates.pass.rate;
      row.append(
        element("strong", "", group.skillId),
        element("span", "outcome-mono", `${shortSha(group.skillSha256)} · ${group.contractVersionId?.slice(0, 8) || "无合同"}`),
        element("span", "", `样本 ${group.denominator}`),
        element("span", "", passRate === null ? "通过率：样本少于 20" : `通过率 ${(passRate * 100).toFixed(1)}%`),
      );
      report.append(row);
    }
    if (!latestReport.groups?.length) report.append(element("p", "outcome-muted", "最近快照没有符合口径的案例。"));
  }
  const list = element("div", "outcome-table");
  list.append(element("div", "outcome-table-head", "快照 / 覆盖 / 截止时间 / 版本"));
  for (const snapshot of payload.snapshots || []) {
    const row = element("div", "outcome-table-row metric-row");
    row.append(element("strong", "outcome-mono", snapshot.id.slice(0, 10)), badge(label(snapshot.coverage_status), toneFor(snapshot.coverage_status)), element("time", "", dateText(snapshot.cutoff_at)), element("span", "", "已封存"));
    list.append(row);
  }
  if (!payload.snapshots?.length) list.append(element("div", "empty-state", "尚未创建正式指标快照。"));
  content.replaceChildren(head, preview, report, list);
}

function qualityRoute({ skillId = state.qualitySubject?.skillId, sha = state.qualitySubject?.sha,
  tab = state.qualityTab, observation = state.qualityObservation, caseId = state.selectedCase,
  feedbackId = state.selectedFeedback, replace = false } = {}) {
  const params = new URLSearchParams(window.location.search);
  params.set("view", "quality");
  if (skillId) params.set("skill", skillId); else params.delete("skill");
  if (sha) params.set("sha", sha); else params.delete("sha");
  if (tab && skillId) params.set("tab", tab); else params.delete("tab");
  if (observation) params.set("observation", observation); else params.delete("observation");
  if (state.qualityAttribution) params.set("attribution", state.qualityAttribution); else params.delete("attribution");
  if (state.qualityTaskType) params.set("taskType", state.qualityTaskType); else params.delete("taskType");
  if (state.qualitySource) params.set("source", state.qualitySource); else params.delete("source");
  if (state.qualityFrom) params.set("from", state.qualityFrom); else params.delete("from");
  if (state.qualityTo) params.set("to", state.qualityTo); else params.delete("to");
  if (caseId) params.set("case", caseId); else params.delete("case");
  if (feedbackId) params.set("feedback", feedbackId); else params.delete("feedback");
  const url = `${window.location.pathname}?${params.toString()}`;
  window.history[replace ? "replaceState" : "pushState"]({}, "", url);
}

function openQuality(skillId, sha) {
  state.view = "quality";
  state.selectedCase = null;
  state.selectedFeedback = null;
  state.qualitySubject = { skillId, sha: sha || null };
  state.qualityTab = "overview";
  qualityRoute();
  return renderQuality();
}

function qualityStatusBadge(status) {
  const tone = status === "judgment-supported" ? "green"
    : status === "threshold-not-met" ? "red"
      : status === "not-publishable" ? "orange" : "";
  return badge(label(status), tone);
}

function qualityFailure(content, message, retry) {
  const panel = element("section", "quality-error-state");
  panel.append(element("h2", "", "无法读取技能质量"), element("p", "", message),
    actionButton("重试", retry, "primary-button"));
  if (state.qualitySubject) panel.append(actionButton("返回目录", () => {
    state.qualitySubject = null;
    qualityRoute({ skillId: null, sha: null, tab: null, replace: true });
    return renderQuality();
  }, "secondary-button"));
  content.replaceChildren(panel);
}

function qualityFilters() {
  const bar = element("div", "outcome-filterbar quality-filterbar");
  const input = element("input");
  input.type = "search";
  input.placeholder = "搜索技能名称";
  input.value = state.skillFilter;
  let timer = null;
  const apply = () => {
    state.skillFilter = input.value.trim();
    qualityRoute({ skillId: state.skillFilter || null, sha: null, tab: null, replace: true });
    renderQuality().catch((error) => setStatus(error.message));
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(apply, 250); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { clearTimeout(timer); apply(); }
    if (event.key === "Escape") { clearTimeout(timer); input.value = ""; apply(); }
  });
  const observation = element("select");
  [["", "全部观察状态"], ["observed", "已观察"], ["evaluable", "可判断"], ["insufficient", "证据不足"]]
    .forEach(([value, text]) => observation.append(new Option(text, value)));
  observation.value = state.qualityObservation;
  observation.addEventListener("change", () => {
    state.qualityObservation = observation.value;
    qualityRoute({ skillId: state.skillFilter || null, sha: null, tab: null, replace: true });
    renderQuality().catch((error) => setStatus(error.message));
  });
  const attribution = element("select");
  [["", "全部参与关系"], ["direct", "直接参与"], ["shared", "共享参与"]]
    .forEach(([value, text]) => attribution.append(new Option(text, value)));
  attribution.value = state.qualityAttribution;
  const taskType = element("input"); taskType.type = "search"; taskType.placeholder = "任务类型"; taskType.value = state.qualityTaskType;
  const source = element("select");
  [["", "全部来源"], ["codex", "Codex"], ["pi", "Pi"]]
    .forEach(([value, text]) => source.append(new Option(text, value)));
  source.value = state.qualitySource;
  const from = element("input"); from.type = "date"; from.value = state.qualityFrom.slice(0, 10);
  const to = element("input"); to.type = "date"; to.value = state.qualityTo.slice(0, 10);
  const applyScope = () => {
    state.qualityAttribution = attribution.value;
    state.qualityTaskType = taskType.value.trim();
    state.qualitySource = source.value;
    state.qualityFrom = from.value ? `${from.value}T00:00:00Z` : "";
    state.qualityTo = to.value ? `${to.value}T23:59:59Z` : "";
    qualityRoute({ skillId: state.skillFilter || null, sha: null, tab: null, replace: true });
    renderQuality().catch((error) => setStatus(error.message));
  };
  attribution.addEventListener("change", applyScope);
  source.addEventListener("change", applyScope);
  taskType.addEventListener("change", applyScope);
  from.addEventListener("change", applyScope);
  to.addEventListener("change", applyScope);
  bar.append(input, observation, attribution, taskType, source, from, to);
  if (state.qualityCompare.length) {
    bar.append(element("span", "outcome-muted", `已选择 ${state.qualityCompare.length}/2 个版本`));
    bar.append(actionButton("清除比较", () => { state.qualityCompare = []; return renderQuality(); }, "text-button"));
  }
  if (state.qualityCompare.length === 2) {
    bar.append(actionButton("比较版本", () => renderQualityComparison(), "primary-button"));
  }
  return bar;
}

async function renderQuality(reset = true) {
  await window.skillAuth.fetch("/api/auth/status");
  const roles = window.skillAuth.state.actor?.roles || [];
  if (!roles.includes("reviewer") && !roles.includes("admin")) return renderTrialAssignments();
  if (state.qualitySubject) return renderQualityDetail();
  const content = $("outcomeContent");
  const head = sectionHead("技能质量", "先确认版本、覆盖和证据是否足以判断，再查看任务结果、体验与反馈。");
  head.append(qualityFilters());
  content.replaceChildren(head, element("div", "loading-state", "正在读取技能质量目录"));
  if (reset) {
    state.qualityItems = [];
    state.qualityNextCursor = null;
  }
  const query = new URLSearchParams({ limit: "100" });
  if (state.skillFilter) query.set("skillId", state.skillFilter);
  if (state.qualityObservation) query.set("observation", state.qualityObservation);
  if (state.qualityAttribution) query.set("attribution", state.qualityAttribution);
  if (state.qualityTaskType) query.set("taskType", state.qualityTaskType);
  if (state.qualitySource) query.set("source", state.qualitySource);
  if (state.qualityFrom) query.set("from", state.qualityFrom);
  if (state.qualityTo) query.set("to", state.qualityTo);
  if (!reset && state.qualityNextCursor) query.set("cursor", state.qualityNextCursor);
  let payload;
  try {
    payload = await api(`/api/skill-quality?${query}`);
  } catch (error) {
    qualityFailure(content, error.message, () => renderQuality());
    return;
  }
  state.qualityItems.push(...(payload.items || []));
  state.qualityNextCursor = payload.next_cursor || null;
  const coverage = element("section", "quality-coverage-band");
  coverage.append(
    qualityStatusBadge(payload.coverage?.coverage_status === "complete" ? "complete" : "not-publishable"),
    element("span", "", `发现 ${payload.coverage?.discovered_files || 0} 个日志，已索引 ${payload.coverage?.indexed_files || 0} 个`),
    element("span", "outcome-muted", `待处理 ${payload.coverage?.pending_files || 0} · 失败 ${payload.coverage?.failed_files || 0}`),
  );
  const table = element("div", "outcome-table quality-directory-table");
  table.append(element("div", "outcome-table-head", "比较 / 技能版本 / 判断状态 / 直接 Case / 最近观察 / 操作"));
  for (const item of state.qualityItems) {
    const row = element("div", "outcome-table-row quality-directory-row");
    const choose = element("input"); choose.type = "checkbox";
    const key = `${item.skill_id}@${item.skill_sha256 || ""}`;
    choose.checked = state.qualityCompare.some((subject) => `${subject.skillId}@${subject.sha || ""}` === key);
    choose.disabled = !choose.checked && state.qualityCompare.length >= 2;
    choose.addEventListener("change", () => {
      if (choose.checked) state.qualityCompare.push({ skillId: item.skill_id, sha: item.skill_sha256 || null });
      else state.qualityCompare = state.qualityCompare.filter((subject) => `${subject.skillId}@${subject.sha || ""}` !== key);
      renderQuality().catch((error) => setStatus(error.message));
    });
    const primary = element("div", "outcome-row-primary");
    primary.append(element("strong", "", item.skill_id), element("span", "outcome-mono", shortSha(item.skill_sha256)));
    row.append(choose, primary, qualityStatusBadge(item.quality_status), element("span", "", item.direct_case_count || 0),
      element("time", "", dateText(item.last_observed_at)),
      actionButton("查看质量", () => openQuality(item.skill_id, item.skill_sha256 || null), "text-button"));
    table.append(row);
  }
  if (!state.qualityItems.length) {
    const scope = payload.scope || {};
    const message = !scope.enabled_skill_count
      ? "当前没有可分析的已启用技能。"
      : !scope.eligible_loaded_version_count
        ? `当前已启用 ${scope.enabled_skill_count} 个技能，尚无与当前启用版本匹配的成功加载记录。`
        : "没有符合当前筛选条件的已启用成功加载技能。";
    table.append(element("div", "empty-state", message));
  }
  if (state.qualityNextCursor) table.append(actionButton("加载更多", () => renderQuality(false), "secondary-button feedback-load-more"));
  content.replaceChildren(head, coverage, table);
}

async function renderTrialAssignments() {
  const content = $("outcomeContent");
  const head = sectionHead("已分配试用", "仅显示分配给你的技能调用与脱敏证据摘要。提交判断不会改变合同结果或任务结论。");
  content.replaceChildren(head, element("div", "loading-state", "正在读取试用任务"));
  let payload;
  try {
    payload = await api("/api/skill-use-judgment-assignments/current");
  } catch (error) {
    qualityFailure(content, error.message, () => renderTrialAssignments());
    return;
  }
  const list = element("div", "outcome-table quality-assignment-table");
  list.append(element("div", "outcome-table-head", "技能版本 / 任务类型 / 证据状态 / 反馈状态 / 操作"));
  for (const item of payload.items || []) {
    const row = element("div", "outcome-table-row");
    const primary = element("div", "outcome-row-primary");
    primary.append(element("strong", "", item.skill_id), element("span", "outcome-mono", shortSha(item.skill_sha256)));
    const preview = item.evidence_preview || {};
    row.append(primary, element("span", "", preview.case?.taskType || "任务类型未知"),
      badge(preview.checks?.length ? "存在检查摘要" : "缺少检查摘要", preview.checks?.length ? "green" : "orange"),
      element("span", "", `关联反馈 ${preview.relatedFeedback?.count || 0}`));
    if (item.evidence_stale) row.append(badge(item.evidence_expired ? "分配已过期" : "证据已失效", "orange"));
    else row.append(actionButton("判断本次使用", async () => {
      state.qualityJudgmentInvocation = item.skill_invocation_id;
      await renderTrialAssignments();
    }, "primary-button"));
    list.append(row);
  }
  if (!payload.items?.length) list.append(element("div", "empty-state", "当前没有分配给你的试用调用。"));
  if (state.qualityJudgmentInvocation) list.append(await qualityJudgmentPanel(state.qualityJudgmentInvocation));
  content.replaceChildren(head, list);
}

function qualityTabs(detail) {
  const tabs = element("div", "quality-tabs");
  [["overview", "总览"], ["cases", "案例"], ["feedback", "反馈"], ["metrics", "指标"], ["contracts", "合同与口径"]]
    .forEach(([value, text]) => {
      const button = actionButton(text, () => {
        state.qualityTab = value;
        qualityRoute({ replace: true });
        return renderQualityDetail();
      }, value === state.qualityTab ? "primary-button" : "secondary-button");
      button.dataset.tab = value;
      tabs.append(button);
    });
  return tabs;
}

function funnelTable(funnel) {
  const table = element("div", "quality-funnel");
  [["有效调用", funnel.valid_invocations], ["加载成功", funnel.loaded_invocations], ["关联 Case", funnel.cases],
    ["版本已知 Case", funnel.known_version_cases], ["直接归因 Case", funnel.direct_cases],
    ["存在合同", funnel.contract_cases], ["可评审", funnel.assessable_cases]]
    .forEach(([name, value]) => {
      const item = element("div", "quality-funnel-item");
      item.append(element("span", "", name), element("strong", "", value || 0));
      table.append(item);
    });
  return table;
}

async function renderQualityDetail() {
  const content = $("outcomeContent");
  const subject = state.qualitySubject;
  content.replaceChildren(element("div", "loading-state", "正在读取技能版本质量"));
  const query = new URLSearchParams({ skillId: subject.skillId });
  if (subject.sha) query.set("sha", subject.sha);
  if (state.qualityTaskType) query.set("taskType", state.qualityTaskType);
  if (state.qualityAttribution) query.set("attribution", state.qualityAttribution);
  if (state.qualitySource) query.set("source", state.qualitySource);
  if (state.qualityFrom) query.set("from", state.qualityFrom);
  if (state.qualityTo) query.set("to", state.qualityTo);
  let detail;
  try {
    detail = await api(`/api/skill-quality/detail?${query}`);
  } catch (error) {
    qualityFailure(content, error.message, () => renderQualityDetail());
    return;
  }
  let trialUsers = [];
  if ((window.skillAuth.state.actor?.roles || []).includes("admin")) {
    try { trialUsers = (await api("/api/trial-users")).items || []; }
    catch (error) { qualityFailure(content, error.message, () => renderQualityDetail()); return; }
  }
  const head = sectionHead(detail.subject.skill_id, `版本 ${shortSha(detail.subject.skill_sha256)} · 精确版本质量口径`);
  head.append(actionButton("返回目录", () => {
    state.qualitySubject = null; state.qualityTab = "overview"; state.qualityJudgmentInvocation = null;
    qualityRoute({ skillId: state.skillFilter || null, sha: null, tab: null });
    return renderQuality();
  }, "text-button"));
  const context = element("section", "quality-context-band");
  const scopeText = [
    state.qualityTaskType && `任务类型 ${state.qualityTaskType}`,
    state.qualityAttribution && `参与关系 ${label(state.qualityAttribution)}`,
    state.qualitySource && `来源 ${state.qualitySource}`,
    (state.qualityFrom || state.qualityTo) && `观察时间 ${state.qualityFrom || "起始"} 至 ${state.qualityTo || "当前"}`,
  ].filter(Boolean).join(" · ");
  const contextItems = [qualityStatusBadge(detail.quality_status),
    element("span", "outcome-mono", detail.subject.skill_sha256 || "版本未知"),
    element("span", "", `覆盖 ${coverageLabel(detail.coverage.coverage_status)}`),
    element("span", "", `索引 ${detail.coverage.indexed_files || 0}/${detail.coverage.discovered_files || 0}`)];
  if (scopeText) contextItems.push(element("span", "outcome-muted", scopeText));
  context.append(...contextItems);
  const reasons = element("section", "quality-reasons");
  reasons.append(element("h2", "", "判断依据与限制"));
  if (detail.blocking_reasons?.length) detail.blocking_reasons.forEach((reason) => reasons.append(badge(reason, "orange")));
  else reasons.append(element("p", "outcome-muted", "当前统计键满足发布前置条件。"));
  const panel = element("section", "quality-detail-panel");
  if (state.qualityTab === "overview") {
    panel.append(element("h2", "", "样本漏斗"), funnelTable(detail.funnel));
    const results = element("div", "quality-result-strip");
    results.append(element("strong", "", "合同结果"), element("span", "", `合格 Case ${detail.formal_results.eligible_cases || 0}`),
      element("span", "", `通过 ${detail.formal_results.pass || 0}`), element("span", "", `部分 ${detail.formal_results.partial || 0}`),
      element("span", "", `失败 ${detail.formal_results.fail || 0}`));
    panel.append(results);
    if (detail.formal_results.groups?.length) {
      const groups = element("div", "quality-formal-groups");
      detail.formal_results.groups.forEach((group) => {
        const text = [
          `合同 ${group.contract_version_id?.slice(0, 10) || "缺失"}`,
          group.task_type || "任务类型未知", `样本 ${group.eligible_cases}`,
          `通过 ${group.pass} / 部分 ${group.partial} / 失败 ${group.fail}`,
          group.pass_lower_bound === undefined ? "阈值未配置" : `通过下界 ${(group.pass_lower_bound * 100).toFixed(1)}% · 失败上界 ${(group.fail_upper_bound * 100).toFixed(1)}%`,
        ].join(" · ");
        groups.append(element("p", "outcome-muted", text));
      });
      panel.append(groups);
    }
    const experience = element("div", "quality-result-strip");
    experience.append(element("strong", "", "试用体验"));
    if (detail.experience.official) {
      experience.append(element("span", "", `有帮助 ${detail.experience.helpful || 0}`),
        element("span", "", `没帮助 ${detail.experience.not_helpful || 0}`),
        element("span", "outcome-muted", `质量快照 ${detail.experience.snapshot_id.slice(0, 10)}`));
    } else experience.append(element("span", "outcome-muted", `未封存体验判断 ${detail.experience.pending_judgments || 0} 条，不参与质量比例。`));
    panel.append(experience);
    panel.append(element("h2", "", "最近可追溯案例"), qualityCaseTable(detail.latest_cases || [], trialUsers));
  } else if (state.qualityTab === "cases") {
    let cases;
    try { cases = await api(`/api/skill-quality/cases?${query}`); }
    catch (error) { qualityFailure(content, error.message, () => renderQualityDetail()); return; }
    panel.append(element("h2", "", "关联案例"), qualityCaseTable(cases.items || [], trialUsers));
    if (state.qualityJudgmentInvocation) panel.append(await qualityJudgmentPanel(state.qualityJudgmentInvocation));
  } else if (state.qualityTab === "feedback") {
    let feedback;
    try { feedback = await api(`/api/skill-quality/feedback?${query}`); }
    catch (error) { qualityFailure(content, error.message, () => renderQualityDetail()); return; }
    const table = element("div", "outcome-table");
    table.append(element("div", "outcome-table-head", "反馈 / 关系 / 状态 / 时间 / 操作"));
    for (const item of feedback.items || []) {
      const row = element("div", "outcome-table-row");
      row.append(element("strong", "", item.redacted_excerpt || "反馈正文已治理清理"), badge(item.relation_kind),
        badge(label(item.current_resolution_state), toneFor(item.current_resolution_state)), element("time", "", dateText(item.observed_at)),
        actionButton("查看反馈", () => openFeedback(item.id), "text-button"));
      table.append(row);
    }
    if (!feedback.items?.length) table.append(element("div", "empty-state", "该版本没有直接或 Case 上下文反馈。"));
    panel.append(table);
  } else if (state.qualityTab === "metrics") {
    panel.append(element("h2", "", "正式口径"));
    panel.append(element("p", "outcome-muted", detail.formal_results.snapshot_id
      ? `使用正式快照 ${detail.formal_results.snapshot_id.slice(0, 10)}，合格 Case ${detail.formal_results.eligible_cases || 0}。`
      : "当前没有可用于该版本的完整覆盖正式快照。"));
    panel.append(element("p", "outcome-muted", "体验比例仅在完整 scope 的专用质量快照中发布；当前预览不生成质量标签。"));
  } else {
    panel.append(element("h2", "", "合同与统计键"));
    panel.append(element("p", "outcome-muted", `合同版本：${detail.contracts.contract_version_ids?.join("、") || "未绑定"}`));
    panel.append(element("p", "outcome-muted", "统计键固定为技能 ID、SHA、合同、任务类型、归因、scope 和时间范围。"));
  }
  content.replaceChildren(head, context, qualityTabs(detail), reasons, panel);
}

function qualityCaseTable(items, trialUsers = []) {
  const table = element("div", "outcome-table quality-case-table");
  table.append(element("div", "outcome-table-head", "Case / 参与关系 / 结论 / 证据状态 / 时间 / 操作"));
  for (const item of items) {
    const row = element("div", "outcome-table-row");
    const verdict = item.effective_verdict || item.automated_verdict || item.assessability || "needs-evidence";
    row.append(element("span", "outcome-mono", String(item.task_case_id).slice(0, 12)), badge(label(item.attribution_kind)),
      badge(label(verdict), toneFor(verdict)), badge(label(item.freshness || "unknown"), toneFor(item.freshness)),
      element("time", "", dateText(item.created_at)),
      actionButton("查看案例", () => openCase(item.task_case_id), "text-button"));
    const roles = window.skillAuth.state.actor?.roles || [];
    if (roles.includes("admin")) {
      const assignee = element("select");
      (trialUsers.length ? trialUsers : [{ id: window.skillAuth.state.actor?.uuid, displayName: "当前用户" }])
        .forEach((user) => assignee.append(new Option(user.displayName, user.id)));
      if (window.skillAuth.state.actor?.uuid) assignee.value = window.skillAuth.state.actor.uuid;
      row.append(assignee);
      row.append(actionButton("判断本次使用", async () => {
        await api("/api/skill-use-judgment-assignments", { method: "POST", body: JSON.stringify({
          skillInvocationId: item.skill_invocation_id, actorId: assignee.value,
        }) });
        state.qualityTab = "cases";
        state.qualityJudgmentInvocation = assignee.value === window.skillAuth.state.actor?.uuid ? item.skill_invocation_id : null;
        await renderQualityDetail();
      }, "secondary-button compact-button"));
    }
    table.append(row);
  }
  if (!items.length) table.append(element("div", "empty-state", "当前筛选下没有可追溯 Case。"));
  return table;
}

async function qualityJudgmentPanel(invocationId) {
  const panel = element("section", "quality-judgment-panel");
  panel.append(element("h2", "", "判断本次使用"));
  const history = await api(`/api/skill-use-judgments?skillInvocationId=${encodeURIComponent(invocationId)}`);
  const current = history.items?.[0];
  const verdict = element("select");
  [["helpful", "有帮助"], ["not-helpful", "没帮助"], ["cannot-judge", "无法判断"], ["not-applicable", "不适用"]]
    .forEach(([value, text]) => verdict.append(new Option(text, value)));
  const relation = element("select");
  [["direct-skill-use", "该技能调用直接相关"], ["task-result-only", "仅与任务结果相关"], ["cannot-attribute", "无法归因"]]
    .forEach(([value, text]) => relation.append(new Option(text, value)));
  const reason = element("select");
  [["", "原因代码"], ["goal-mismatch", "目标不匹配"], ["unexecutable-guidance", "指引不可执行"],
    ["incomplete-result", "结果不完整"], ["insufficient-verification", "验证不足"],
    ["increased-rework", "增加返工"], ["environment-limitation", "环境限制"], ["cannot-attribute", "无法归因"]]
    .forEach(([value, text]) => reason.append(new Option(text, value)));
  const note = element("textarea"); note.placeholder = "补充说明（可选，将脱敏保存）";
  const controls = element("div", "outcome-action-row");
  controls.append(verdict, relation, reason, note, actionButton("提交判断", async () => {
    const requiresReason = ["not-helpful", "not-applicable"].includes(verdict.value);
    await api("/api/skill-use-judgments", { method: "POST", body: JSON.stringify({
      skillInvocationId: invocationId, expectedRevision: current?.revision || 0,
      verdict: verdict.value, attributionRelation: relation.value,
      reasonCode: requiresReason ? reason.value : null, note: note.value.trim() || null,
    }) });
    state.qualityJudgmentInvocation = null;
    await renderAfterTrialJudgment();
  }, "primary-button"));
  if (current) {
    controls.append(element("span", "outcome-muted", `当前 r${current.revision}：${label(current.verdict)}`));
    controls.append(actionButton("撤销当前判断", async () => {
      await api("/api/skill-use-judgments/withdraw", { method: "POST", body: JSON.stringify({
        skillInvocationId: invocationId, expectedRevision: current.revision,
      }) });
      state.qualityJudgmentInvocation = null;
      await renderAfterTrialJudgment();
    }, "secondary-button"));
  }
  panel.append(controls);
  return panel;
}

async function renderAfterTrialJudgment() {
  const roles = window.skillAuth.state.actor?.roles || [];
  if (roles.includes("reviewer") || roles.includes("admin")) await renderQualityDetail();
  else await renderTrialAssignments();
}

async function renderQualityComparison() {
  const subjects = state.qualityCompare.map((subject) => `subject=${encodeURIComponent(`${subject.skillId}@${subject.sha || ""}`)}`).join("&");
  const scope = new URLSearchParams({ attribution: "direct" });
  if (state.qualityTaskType) scope.set("taskType", state.qualityTaskType);
  if (state.qualitySource) scope.set("source", state.qualitySource);
  if (state.qualityFrom) scope.set("from", state.qualityFrom);
  if (state.qualityTo) scope.set("to", state.qualityTo);
  const payload = await api(`/api/skill-quality/compare?${subjects}&${scope}`);
  const content = $("outcomeContent");
  const head = sectionHead("技能版本比较", "仅在完整、同 scope、direct 归因和同一正式快照下给出可比较结论。");
  head.append(actionButton("返回目录", () => renderQuality(), "text-button"));
  const result = element("section", "quality-compare-panel");
  if (!payload.comparable) {
    result.append(element("h2", "", "当前仅可并排查看"));
    payload.reasons.forEach((reason) => result.append(badge(reason, "orange")));
  } else result.append(element("h2", "", "口径一致，可比较"));
  const table = element("div", "outcome-table");
  table.append(element("div", "outcome-table-head", "技能版本 / 判断状态 / 合格 Case / 通过 / 部分 / 失败"));
  for (const detail of payload.subjects || []) {
    const row = element("div", "outcome-table-row");
    row.append(element("strong", "", detail.subject.skill_id), element("span", "outcome-mono", shortSha(detail.subject.skill_sha256)),
      qualityStatusBadge(detail.quality_status), element("span", "", detail.formal_results.eligible_cases || 0),
      element("span", "", detail.formal_results.pass || 0), element("span", "", detail.formal_results.partial || 0),
      element("span", "", detail.formal_results.fail || 0));
    table.append(row);
  }
  result.append(table);
  content.replaceChildren(head, result);
}

async function renderView() {
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
  $("effectOverview").hidden = state.view === "quality";
  if (state.selectedFeedback) return openFeedback(state.selectedFeedback);
  if (state.selectedCase) return openCase(state.selectedCase);
  if (state.view === "quality") return renderQuality();
  if (state.view === "loads") return renderLoads();
  if (state.view === "outcomes") return renderOutcomes();
  if (state.view === "feedback") return renderFeedback();
  if (state.view === "queue") return renderQueue();
  if (state.view === "contracts") return renderContracts();
  return renderMetrics();
}

async function runScan() {
  if (state.busy) return;
  state.busy = true;
  $("effectScanButton").disabled = true;
  setStatus("正在增量扫描会话");
  try {
    const scan = await api("/api/effect-scan", { method: "POST", body: JSON.stringify({ budgetBytes: 268435456, budgetSeconds: 20 }) });
    setStatus(`扫描完成：索引 ${scan.indexed_files} 个文件，新增反馈 ${scan.feedback?.newSignals || 0} 条`);
    await loadOverview();
    await renderView();
  } finally {
    state.busy = false;
    $("effectScanButton").disabled = false;
  }
}

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => {
  state.view = button.dataset.view;
  state.selectedCase = null;
  state.selectedFeedback = null;
  if (state.view !== "quality") state.qualityJudgmentInvocation = null;
  if (state.view === "quality") qualityRoute({ replace: false });
  else {
    const route = new URLSearchParams();
    route.set("view", state.view);
    window.history.pushState({}, "", `${window.location.pathname}?${route}`);
  }
  renderView().catch((error) => setStatus(error.message));
}));

window.addEventListener("popstate", () => {
  const route = new URLSearchParams(window.location.search);
  state.view = route.get("view") || "quality";
  state.qualitySubject = route.get("skill") && route.get("sha")
    ? { skillId: route.get("skill"), sha: route.get("sha") } : null;
  state.skillFilter = route.get("skill") || "";
  state.qualityTab = route.get("tab") || "overview";
  state.qualityObservation = route.get("observation") || "";
  state.qualityAttribution = route.get("attribution") || "";
  state.qualityTaskType = route.get("taskType") || "";
  state.qualitySource = route.get("source") || "";
  state.qualityFrom = route.get("from") || "";
  state.qualityTo = route.get("to") || "";
  state.selectedCase = route.get("case");
  state.selectedFeedback = route.get("feedback");
  renderView().catch((error) => setStatus(error.message));
});
$("effectScanButton").addEventListener("click", () => runScan().catch((error) => setStatus(error.message)));

Promise.all([loadOverview(), renderView()])
  .then(() => setStatus("结果评审数据已加载"))
  .catch((error) => setStatus(error.message));