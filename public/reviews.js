const state = {
  view: "loads", overview: null, selectedCase: null, selectedFeedback: null,
  skillFilter: "", feedbackChannel: "", feedbackSeverity: "",
  feedbackResolution: "", feedbackItems: [], feedbackNextCursor: null, busy: false,
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
    ["覆盖", label(scan.coverage_status || "unknown")],
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
    input.addEventListener("change", () => { state.skillFilter = input.value.trim(); renderView(); });
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
  row.append(actionButton("查看", async () => openCase(item.task_case_id), "text-button"));
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
  const query = new URLSearchParams({ limit: "500" });
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
  state.selectedFeedback = signalId;
  state.selectedCase = null;
  const content = $("outcomeContent");
  content.replaceChildren(element("div", "loading-state", "正在读取反馈详情"));
  const detail = await api(`/api/feedback-signals/${encodeURIComponent(signalId)}`);
  const current = (detail.machine_revisions || []).find((item) => item.is_current)
    || detail.machine_revisions?.at(-1) || {};
  const head = sectionHead(label(current.category), current.redacted_excerpt || "正文已清理");
  head.append(actionButton("返回", async () => {
    state.selectedFeedback = null;
    await renderFeedback();
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
  const content = $("outcomeContent");
  content.replaceChildren(element("div", "loading-state", "正在读取案例证据"));
  const detail = await api(`/api/task-cases/${encodeURIComponent(caseId)}`);
  const head = sectionHead(detail.case.task_type || "任务案例", `Case ${detail.case.id.slice(0, 12)} · revision ${detail.case.current_revision}`);
  head.append(actionButton("返回", async () => { state.selectedCase = null; await renderView(); }, "text-button"));
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

async function renderView() {
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
  if (state.selectedFeedback) return openFeedback(state.selectedFeedback);
  if (state.selectedCase) return openCase(state.selectedCase);
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
  renderView().catch((error) => setStatus(error.message));
}));
$("effectScanButton").addEventListener("click", () => runScan().catch((error) => setStatus(error.message)));

Promise.all([loadOverview(), renderView()])
  .then(() => setStatus("结果评审数据已加载"))
  .catch((error) => setStatus(error.message));