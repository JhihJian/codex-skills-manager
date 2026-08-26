# 会话负面反馈发现与处理设计

## 文档状态

- 状态：已实现
- 目标读者：产品负责人、架构维护者、后端与前端开发者、评审规则维护者
- 关联设计：[`skill-outcome-review-design.md`](./skill-outcome-review-design.md)
- 适用系统：Codex Skills Manager 的 Codex/Pi 会话效果索引与结果评审工作台

实现入口：

- `feedback_detector.py`：内容来源、确定性反馈规则与过程异常计划。
- `feedback_service.py`：机器 revision、目标、Action、队列、聚类、派生恢复、清理和快照。
- `feedback_semantic_classifier.py`：严格 schema 的本地语义分类与校准门禁。
- `effect_store.py`：schema v7、派生变更流和不可变反馈指标。
- `outcome_reviews.py`：扫描、跨 Case 目标上下文和 Case 详情投影。
- `app.py`、`public/reviews.*`：认证 API 与负面反馈工作台。
- `scripts/benchmark-feedback.py`：规模化存储和查询性能验收。

## 1. 摘要

本设计在现有会话效果索引之上增加负面反馈发现能力，用于快速回答四个问题：

1. 会话中是否出现了用户对结果、故障、需求遗漏或执行过程的负面评价。
2. 评价指向哪一次任务结果、技能调用、工具调用或工具结果。
3. 问题是否已被返工、验证和解决。
4. 哪些高价值反馈需要立即进入人工评审队列。

系统采用两个正交通道：

- `user-feedback`：用户原创内容中的结果否定、故障报告、需求遗漏、返工纠正和过程批评。
- `process-anomaly`：工具、并行调用和子代理执行过程中出现的结构化异常。

负面反馈首先形成可追溯候选，再经过目标归属和人工复核。用户文本单独不产生任务硬失败，也不直接解释为技能导致失败。工具异常、助手自我批评、引用日志和初始约束使用独立类型保存，保持评价来源和证据能力清晰。

热路径使用确定性规则和结构化字段，目标是在增量会话扫描结束后数秒内产生候选。语义分类用于处理规则无法稳定判断的低置信消息，通过异步校准流程运行，不阻塞主扫描。

## 2. 背景与问题

现有结果评审系统已经保存：

- Codex/Pi 规范事件和 provenance。
- Task Episode、Task Case、技能调用、工具调用和工具结果。
- 结果合同、检查、Outcome Assessment 和人工追加记录。
- session parent、fork 和 subagent 关系。

这些数据能够展示错误结果和人工裁决，但尚未将会话中的评价性语言建模为独立证据。典型遗漏包括：

- 用户说“还是不行”“你漏了权限校验”，消息进入新的 Episode 和 Case，未自动指向上一轮结果。
- 工具返回 `Agent failed: Unknown agent`，外层协议却是 `isError=false`，结果可能只显示为普通返回。
- 用户粘贴的错误日志、技能正文和助手自我批评容易与用户对当前结果的评价混淆。
- 后续修复和用户认可尚未形成与原反馈相连的解决状态。
- 多技能共同参与时，负面任务反馈缺少可辩护的技能归属边界。

负面反馈发现需要同时解决“检测到评价”和“评价对象定位”。目标关系是返工跟踪、技能分析和结果冲突处理的必要输入。

## 3. 目标与范围

### 3.1 目标

1. 增量发现 Codex/Pi 会话中的用户负面反馈和过程异常。
2. 将反馈定位到 Task Case、助手结果、技能调用、工具调用或工具结果。
3. 区分反馈候选、确认反馈、过程异常、任务失败和技能归责。
4. 复用现有 actor、claim、correction、review queue 和 Evidence 定位能力。
5. 记录返工、待验证、已验证解决、误报和重复问题。
6. 提供高精度优先的提醒、列表、筛选和聚类能力。
7. 保持重复扫描幂等、日志重写可恢复、派生记录可失效。
8. 落库前执行敏感信息脱敏，详情访问继续受认证和角色控制。

### 3.2 适用范围

首个实现覆盖：

- 用户原创文本中的直接负面评价。
- 用户对结果缺陷、需求遗漏和过程问题的纠正。
- Codex/Pi 工具失败、阻止、取消、结果缺失和子代理启动失败。
- 同一会话内的上一轮结果定位。
- session parent、fork 和 subagent 结构关系内的目标候选。
- 人工确认、误报、重定向、修复和验证闭环。

以下能力作为扩展方向：

- 跨项目、跨设备的同类问题聚类。
- 外部工单、审批或业务系统的负面验收适配器。
- 面向特定领域的语义分类器和严重度模型。
- 负面反馈与发布、版本回滚、事故记录的关联。

## 4. 核心设计决定

### 4.1 反馈候选与结果结论分离

负面语言提供反证线索，证据能力取决于来源、目标关系和后续验证。系统遵循以下归约规则：

- 用户负面反馈可创建评审任务。
- 高置信且目标明确的反馈可将已有 `pass` 标记为 `disputed`。
- 用户文本单独不产生 `hard_failure`。
- 自动任务失败需要受信确定性检查、已验证外部验收或人工裁决。
- `shared` 技能只记录共同参与，不按单一反馈分摊责任。

### 4.2 用户反馈与过程异常分离

工具错误不属于用户评价，助手自我批评也不属于用户评价。工作台可统一展示三类负面信号，但统计口径保持分离：

| 通道 | 来源 | 主要用途 | 对结果的自动影响 |
| --- | --- | --- | --- |
| `user-feedback` | 用户原创内容 | 结果反证、返工和验收 | 最多形成争议候选 |
| `process-anomaly` | 工具、并行调用、子代理 | 执行完整性和流程质量 | 形成 needs-evidence 或过程队列 |
| `assistant-claim` | 助手自评、道歉、完成声明 | 检索和矛盾线索 | 正向和负向权重均为零 |

### 4.3 目标归属是入队前置条件

负面词命中只产生原始候选。进入高优先级队列需要明确的被评价对象，或至少一个可人工确认的目标候选。时间邻近只提供低置信候选关系，不自动合并 Task Case，也不自动归因到技能。

### 4.4 热路径规则优先

增量扫描主路径执行：

- 内容来源隔离。
- 高精度词法和结构化规则。
- 排除规则。
- 目标候选解析。
- 指纹去重和持久化。

本地语义模型处理低置信候选和混合评价。模型调用独立运行，具备版本、prompt、rubric 和校准语料绑定。

### 4.5 独立反馈实体

反馈信号包含分类、置信度、目标候选和解决状态，生命周期不同于通用 Evidence。系统使用独立表保存反馈，并通过 Evidence ID 与现有评审模型连接，保持查询效率和审计完整性。

## 5. 术语

- **Feedback Signal**：从一个会话事件派生的评价或异常候选。
- **Feedback Span**：触发候选的用户原创文本范围，落库时只保存脱敏摘录和原文哈希。
- **Feedback Target**：被评价的 Task Case、助手结果、技能调用、工具调用或工具结果。
- **Feedback Action**：人工对候选执行的确认、排除、重定向、修复或验证记录。
- **Resolution**：反馈当前的处理与验证状态。
- **Process Anomaly**：工具或代理执行与预期流程不一致的结构化信号。
- **Negative Acceptance**：用户或外部系统明确拒绝当前结果的反馈。
- **Rework**：由已确认反馈触发的修正活动；新增需求不自动视为返工。

## 6. 系统不变量

### 6.1 解释不变量

1. 候选信号、确认反馈、任务失败和技能归责使用不同状态。
2. 用户原创内容与技能展开、系统注入、工具输出、引用内容分别标记来源。
3. 工具错误和助手自评不计入用户负面反馈数量。
4. 单独情绪词或否定词不产生高优先级反馈。
5. 目标关系缺失的候选保持独立，不改变现有 Outcome Assessment。
6. 反馈指向 `shared` Task Case 时，不自动形成单技能负面指标。
7. 后续修复保留原始反馈和返工历史；“已修复”不等于“误报”。
8. 助手声明已修复只能进入待验证状态。
9. 用户文本的证据能力限于反馈与争议候选；受信硬失败和硬失败生成继续由合同证据规则控制。
10. detector、规则、模型或目标解析器版本变化时，机器派生结果可重建，人工动作继续保留。

### 6.2 数据不变量

1. Feedback Signal 的逻辑指纹由反馈事件和稳定来源范围确定；类别、目标和 detector 版本进入机器 revision 指纹。
2. 同一信号允许多个有序目标候选，最多一个目标处于 `confirmed`。
3. 每条信号保存事件 ID、provenance locator、摘录哈希、producer 版本和置信度。
4. 原始日志保持只读。
5. 事件失去全部 provenance 时，对应机器信号和目标标记为 `orphaned`。
6. detector 重跑不覆盖人工确认、误报和解决动作。
7. 正式结果指标默认排除仅有反馈候选、目标歧义和未确认归因的记录。

### 6.3 安全不变量

1. 检测在原始事件解析期间使用完整输入，落库只保存最小脱敏摘录。
2. 摘录脱敏失败时只保存哈希和 locator。
3. 详情正文、目标回答、工具输出和原日志定位经过认证和权限检查。
4. 反馈检测不执行会话中记录的命令。
5. 外部语义模型不接收原始正文；本地模型请求也使用脱敏 Evidence 包。
6. 数据清理覆盖反馈信号、目标、聚类、语义正文和人工备注，同时保留 reason code 与 revision 审计链。

## 7. 反馈分类模型

### 7.1 用户反馈类别

| 类别 | 定义 | 示例 | 默认影响 |
| --- | --- | --- | --- |
| `result-rejection` | 明确否定当前答案或产物 | “不对”“这个方案不能用” | 高优先级结果反证 |
| `observed-defect` | 报告可观察的故障或错误 | “按钮还是没反应”“仍然 500” | 高优先级缺陷候选 |
| `requirement-gap` | 指出遗漏、误解或违反要求 | “权限校验没做” | 结果覆盖争议 |
| `rework-correction` | 要求撤销或修改已有结果 | “撤销这次改动，重新做” | 返工候选 |
| `process-critique` | 批评验证、越权、沟通或执行过程 | “要求并行评审，但首轮没执行” | 过程质量队列 |
| `external-negative-acceptance` | 转述客户、审批或业务系统拒绝 | “验收平台仍判定失败” | 等待外部验证 |
| `mixed-or-unclear` | 同时包含正负评价或目标不清 | “功能可以，但数据还是不对” | 人工分类 |

### 7.2 过程异常类别

| 类别 | 判定依据 | 示例 |
| --- | --- | --- |
| `tool-error` | 结构化 error/failed 状态 | 工具返回 `exitCode=1` |
| `tool-blocked` | blocked/denied/rejected | 权限策略阻止执行 |
| `tool-timeout` | timeout 或超时取消 | checker 超时 |
| `result-missing` | 调用存在且会话结束后无结果 | 并行兄弟之一没有回传 |
| `agent-unavailable` | 请求代理不在 available agents 中 | `Unknown agent: "designer"` |
| `dispatch-not-executed` | 计划任务大于零，实际启动数为零 | 首轮并行代理全为 `turns=0` |
| `partial-dispatch` | 只有部分并行任务启动 | 计划 4 个，启动 2 个 |
| `child-result-missing` | 子会话已启动但无结构化回传 | 有 child session，无结果 ID |

### 7.3 排除类别

排除规则仍保存调试计数，但不进入反馈主列表：

| 类别 | 示例 |
| --- | --- |
| `instructional-negation` | “不要修改文件” |
| `cancel-or-pause` | “先别继续” |
| `scope-change` | “再增加导出功能” |
| `preference-change` | “换成绿色”且未否定现有结果 |
| `quoted-content` | “日志里写着‘执行失败’” |
| `tool-or-check-output` | 工具正文中的错误文本 |
| `assistant-self-assessment` | “我刚才判断错了” |
| `injected-skill-content` | Pi `<skill>` 展开正文 |
| `positive-negation` | “没问题”“现在不报错了” |
| `hypothetical-or-example` | “如果失败，就回滚” |

## 8. 总体架构

```text
Codex/Pi JSONL
  -> Effect Adapter
  -> Canonical Event + Provenance
  -> Content Origin/Span Projection
  -> Feedback Detector
       -> deterministic user-feedback rules
       -> structured process-anomaly rules
       -> suppression rules
  -> Feedback Target Resolver
  -> Feedback Signal / Feedback Target
  -> Evidence projection
  -> Review Task + Feedback Action
  -> Resolution tracking
  -> Workbench / Metrics / Clusters
```

检测器与现有会话扫描共享 generation、checkpoint 和 provenance。反馈派生使用独立游标和预算，使检测规则更新不会触发日志重新解析。

## 9. 内容来源与 Span 投影

### 9.1 来源类型

规范消息需要保留内容块来源：

- `user-authored`：用户实际输入。
- `assistant-authored`：助手正文。
- `system-injected`：system/developer 指令。
- `skill-injected`：Pi `<skill>` 展开或技能正文。
- `tool-output`：工具和检查结果。
- `quoted`：Markdown 引用、代码块、日志块或显式转述。
- `unknown-origin`：协议无法稳定判断来源。

只有 `user-authored` 默认进入用户反馈检测。`process-anomaly` 从结构化工具结果和子代理详情派生，不扫描工具正文情绪。

Pi 的一个 `user_message` 可能同时包含用户输入和一个或多个 `<skill>` 展开块。适配器按原始 content block 保存来源边界，解析规则如下：

1. 每个 text block 独立解析，不先拼接消息全文。
2. 完整的 `<skill ...>` 起始标签、正文和对应 `</skill>` 标记为 `skill-injected`。
3. 同一 block 中多个连续 skill 块分别记录范围，块前、块间和块后的普通文本保持 `user-authored`。
4. `/skill:name` 命令本身属于 `user-authored`，命令展开后的技能正文属于 `skill-injected`。
5. skill 标签不允许嵌套；嵌套、缺少结束标签、属性损坏或范围越界时，从异常起始标签到 block 结尾标记为 `unknown-origin` 并排除检测。
6. 跨 content block 的未闭合标签不继续猜测边界，相关 block 标记为 `unknown-origin`。
7. 每个来源范围保存 block index、原始字符起止位置、原文哈希和 parser 版本。

上述规则采用失败关闭口径。来源边界不完整时可以保留检索 locator，不产生用户反馈候选。

### 9.2 Span 数据

每个候选 Span 包含：

- 规范事件 ID。
- 内容块序号和协议 locator。
- 原文哈希。
- 脱敏摘录。
- 起止偏移或块内 locator。
- 来源类型。
- 截断状态。
- 脱敏状态。

检测在适配器接收原始事件时完成完整文本扫描，用于保持 4,096 字符展示限制之外的尾部反馈可发现。持久化摘录仍受长度预算限制。

## 10. 数据模型

### 10.1 `feedback_signals` 与机器 revision

`feedback_signals` 保存稳定逻辑身份和当前投影。detector 重派生的分类结果保存在 `feedback_signal_revisions`，人工动作始终绑定稳定的 signal ID。

```sql
CREATE TABLE feedback_signals (
  id TEXT PRIMARY KEY,
  logical_fingerprint TEXT NOT NULL UNIQUE,
  feedback_event_id TEXT NOT NULL REFERENCES canonical_events(id),
  feedback_case_id TEXT REFERENCES task_cases(id),
  current_machine_revision_id TEXT,
  current_confirmed_target_id TEXT,
  current_process_state TEXT NOT NULL,
  current_resolution_state TEXT NOT NULL,
  current_action_revision INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(id, current_machine_revision_id),
  FOREIGN KEY(current_machine_revision_id, id)
    REFERENCES feedback_signal_revisions(id, feedback_signal_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(current_confirmed_target_id, id)
    REFERENCES feedback_targets(id, feedback_signal_id)
    DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE feedback_signal_revisions (
  id TEXT PRIMARY KEY,
  feedback_signal_id TEXT NOT NULL REFERENCES feedback_signals(id),
  revision INTEGER NOT NULL,
  revision_fingerprint TEXT NOT NULL UNIQUE,
  channel TEXT NOT NULL,
  category TEXT NOT NULL,
  severity TEXT NOT NULL,
  authority TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  excerpt_hash TEXT NOT NULL,
  redacted_excerpt TEXT,
  locator_json TEXT NOT NULL,
  detector_id TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  suppression_reason TEXT,
  orphaned INTEGER NOT NULL DEFAULT 0,
  is_current INTEGER NOT NULL,
  supersedes_id TEXT,
  observed_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(feedback_signal_id, revision),
  UNIQUE(id, feedback_signal_id),
  FOREIGN KEY(supersedes_id, feedback_signal_id)
    REFERENCES feedback_signal_revisions(id, feedback_signal_id)
);

CREATE UNIQUE INDEX uq_current_feedback_signal_revision
  ON feedback_signal_revisions(feedback_signal_id)
  WHERE is_current = 1;
```

字段口径：

- `feedback_case_id` 表示评价消息自身所属 Case。
- 被评价 Case 保存于 `feedback_targets`。
- `authority` 取值为 `user / external / tool / assistant / unknown`。
- `logical_fingerprint` 不包含 detector 版本，用于承接人工动作。
- `revision_fingerprint` 包含 detector、规则、语义模型和输入 Evidence 版本。
- `current_process_state` 与 `current_resolution_state` 是动作事务维护的权威当前投影。
- `current_machine_revision_id` 通过复合外键保证 revision 属于当前 signal。
- `current_confirmed_target_id` 是唯一人工确认目标的权威投影，空值表示尚未确认。
- 机器重派生只切换 `feedback_signal_revisions.is_current` 和 `current_machine_revision_id`，不改写人工 Action。

更新 current revision 的事务顺序固定为：旧 revision 设为非 current、插入或激活新 revision、更新 signal 指针。`BEFORE UPDATE OF current_machine_revision_id` 触发器校验目标 revision 的 `is_current=1`；部分唯一索引保证每个 signal 至多一个 current revision。事务结束时指针、current 标记和 signal 归属三者一致。

### 10.2 `feedback_targets`

```sql
CREATE TABLE feedback_targets (
  id TEXT PRIMARY KEY,
  feedback_signal_id TEXT NOT NULL REFERENCES feedback_signals(id),
  signal_revision_id TEXT NOT NULL,
  target_fingerprint TEXT NOT NULL UNIQUE,
  rank INTEGER NOT NULL,
  target_kind TEXT NOT NULL,
  context_task_case_id TEXT REFERENCES task_cases(id),
  target_task_case_id TEXT REFERENCES task_cases(id),
  target_event_id TEXT REFERENCES canonical_events(id),
  skill_invocation_id TEXT REFERENCES skill_invocations(id),
  tool_call_id TEXT REFERENCES tool_calls(id),
  tool_result_id TEXT REFERENCES tool_results(id),
  relation TEXT NOT NULL,
  confidence REAL NOT NULL,
  machine_status TEXT NOT NULL,
  resolver_version TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(feedback_signal_id, rank, signal_revision_id),
  UNIQUE(id, feedback_signal_id),
  FOREIGN KEY(signal_revision_id, feedback_signal_id)
    REFERENCES feedback_signal_revisions(id, feedback_signal_id),
  CHECK (
    (target_kind = 'task-result' AND target_task_case_id IS NOT NULL
      AND target_event_id IS NULL AND skill_invocation_id IS NULL
      AND tool_call_id IS NULL AND tool_result_id IS NULL)
    OR
    (target_kind IN ('assistant-result', 'process-plan') AND target_task_case_id IS NULL
      AND target_event_id IS NOT NULL
      AND skill_invocation_id IS NULL AND tool_call_id IS NULL AND tool_result_id IS NULL)
    OR
    (target_kind = 'skill-invocation' AND target_task_case_id IS NULL
      AND skill_invocation_id IS NOT NULL
      AND target_event_id IS NULL AND tool_call_id IS NULL AND tool_result_id IS NULL)
    OR
    (target_kind = 'tool-call' AND target_task_case_id IS NULL AND tool_call_id IS NOT NULL
      AND target_event_id IS NULL AND skill_invocation_id IS NULL AND tool_result_id IS NULL)
    OR
    (target_kind = 'tool-result' AND target_task_case_id IS NULL AND tool_result_id IS NOT NULL
      AND target_event_id IS NULL AND skill_invocation_id IS NULL AND tool_call_id IS NULL)
  )
);

```

`target_kind` 取值：

- `task-result`
- `assistant-result`
- `skill-invocation`
- `tool-call`
- `tool-result`
- `process-plan`

`machine_status` 取值为 `candidate / rejected / superseded / orphaned`。机器状态只描述当前 detector revision 产生的候选，不表达人工确认。

`target_fingerprint` 由稳定 signal ID、机器 revision、目标类型、目标对象和关系组成。人工 `confirm` 或 `retarget` Action 更新 `feedback_signals.current_confirmed_target_id`，单值指针保证每个 signal 最多一个确认目标。

`target_task_case_id` 只表示 `task-result` 目标；其他目标关联的 Case 保存于 `context_task_case_id`。类型 CHECK 因而保证五个目标对象外键中恰好一个非空，同时允许所有目标携带独立的 Case 上下文。

detector 重建只 supersede 旧机器候选，不修改 `current_confirmed_target_id`。新机器 revision 与人工确认目标不一致时，追加 `target-disputed` system Action，将 process state 置为 `triaged` 并进入重新确认队列。人工目标失去 provenance 时保留指针和历史 Action，目标标记 orphaned，signal 进入相同争议流程。

### 10.3 `feedback_actions`

人工动作采用追加记录：

```sql
CREATE TABLE feedback_actions (
  id TEXT PRIMARY KEY,
  feedback_signal_id TEXT NOT NULL REFERENCES feedback_signals(id),
  actor_id TEXT REFERENCES actors(id),
  producer_kind TEXT NOT NULL,
  revision INTEGER NOT NULL,
  action TEXT NOT NULL,
  from_process_state TEXT,
  to_process_state TEXT,
  from_resolution_state TEXT,
  to_resolution_state TEXT,
  reason_code TEXT NOT NULL,
  note TEXT,
  target_id TEXT,
  supersedes_id TEXT,
  binding_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(feedback_signal_id, revision),
  UNIQUE(id, feedback_signal_id),
  FOREIGN KEY(target_id, feedback_signal_id)
    REFERENCES feedback_targets(id, feedback_signal_id),
  FOREIGN KEY(supersedes_id, feedback_signal_id)
    REFERENCES feedback_actions(id, feedback_signal_id),
  CHECK (
    (producer_kind = 'reviewer' AND actor_id IS NOT NULL)
    OR (producer_kind IN ('detector', 'queue', 'system') AND actor_id IS NULL)
  )
);
```

动作包括：

- `confirm`
- `exclude`
- `retarget`
- `mark-duplicate`
- `start-fix`
- `request-verification`
- `resolve-verified`
- `resolve-unverified`
- `reopen`
- `detected`
- `queued`
- `claimed`
- `orphaned`
- `reactivated`
- `superseded`
- `target-disputed`
- `closed`

`feedback_signals.current_process_state`、`current_resolution_state` 和 `current_action_revision` 是当前状态权威投影。所有机器、队列和人工状态变化都追加 Feedback Action。`producer_kind` 区分 detector、queue、reviewer 和 system；人工动作绑定 actor，机器动作绑定 detector/scan/run 信息。每个动作在一个 SQLite 事务中：

1. 校验 expected action revision。
2. `claim` 原子校验任务仍可领取并写入当前 actor；claim 之后的 reviewer 动作校验领取人一致。机器动作校验绑定的 scan、detector 或 orphan 事件。
3. 追加 Feedback Action。
4. 更新 signal 当前投影。
5. 更新或关闭对应 `review_tasks`。

`review_tasks` 只负责领取、队列原因和待办状态；解决状态由 Feedback Action 和 signal 投影负责。`claimed`、`queued`、`closed`、`orphaned` 和 `reactivated` 同时写 Action 与队列，投影可按 Action revision 完整重放，不从队列状态反推解决状态。

### 10.4 派生变更流与 `feedback_derivation_state`

provenance 新增和删除都需要驱动反馈派生。效果索引在相关写事务中追加统一变更流：

```sql
CREATE TABLE effect_derivation_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  change_type TEXT NOT NULL,
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  generation_id TEXT,
  scan_run_id TEXT,
  binding_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_derivation_change_entity
  ON effect_derivation_changes(entity_kind, entity_id, id);
```

`change_type` 首批取值：

- `event-available`
- `provenance-added`
- `provenance-removed`
- `event-orphaned`
- `event-reactivated`
- `target-invalidated`
- `case-invalidated`

generation 删除、provenance 恢复、日志重写和 correction 在原事务内追加对应变更。派生消费者按 change ID 处理新增候选、orphan、重新激活和目标失效。

```sql
CREATE TABLE feedback_derivation_state (
  detector_id TEXT PRIMARY KEY,
  detector_version TEXT NOT NULL,
  change_cursor INTEGER NOT NULL,
  last_scan_run_id TEXT,
  status TEXT NOT NULL,
  stats_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

首次启用 detector 时，对当前非 orphan 事件执行一次受预算 bootstrap，并记录 bootstrap 完成时的 change cursor。之后只消费单调递增的 `effect_derivation_changes.id`。detector 版本变化时重新派生机器记录，人工动作继续绑定原信号 revision；信号指纹相同时保持同一逻辑身份。

### 10.5 与现有评审模型的关系

- 每个高置信候选投影为一个 `evidence_items` 记录。
- 所有进入队列的反馈都创建专用 Outcome Assessment，`subject_key=feedback:<signal_id>`、`automated_verdict=unset`。
- assessment 的 Task Case 优先使用确认目标的 Case 上下文；目标尚未确认时使用 `feedback_case_id`。
- `review_tasks` 绑定该 assessment，`queue_reason=user-negative-feedback` 或具体过程异常原因。
- 反馈评审使用独立 subject，不修改技能合同 assessment。
- 反馈 subject 不进入正式技能结果分母。
- 人工 correction 用于误报、错误分类和错误目标；反馈解决状态由 `feedback_actions` 管理。

### 10.6 索引

```sql
CREATE INDEX idx_feedback_signal_queue
  ON feedback_signals(current_resolution_state, current_process_state, updated_at, id);
CREATE INDEX idx_feedback_signal_process
  ON feedback_signals(current_process_state, updated_at, id);
CREATE INDEX idx_feedback_revision_list
  ON feedback_signal_revisions(channel, observed_at DESC, id DESC)
  WHERE is_current = 1 AND orphaned = 0;
CREATE INDEX idx_feedback_revision_category
  ON feedback_signal_revisions(category, observed_at DESC, id DESC)
  WHERE is_current = 1 AND orphaned = 0;
CREATE INDEX idx_feedback_revision_severity
  ON feedback_signal_revisions(severity, observed_at DESC, id DESC)
  WHERE is_current = 1 AND orphaned = 0;
CREATE INDEX idx_feedback_revision_authority_source
  ON feedback_signal_revisions(authority, source, observed_at DESC, id DESC)
  WHERE is_current = 1 AND orphaned = 0;
CREATE INDEX idx_feedback_revision_source
  ON feedback_signal_revisions(source, observed_at DESC, id DESC)
  WHERE is_current = 1 AND orphaned = 0;
CREATE INDEX idx_feedback_revision_confidence
  ON feedback_signal_revisions(confidence DESC, observed_at DESC, id DESC)
  WHERE is_current = 1 AND orphaned = 0;
CREATE INDEX idx_feedback_revision_event
  ON feedback_signal_revisions(feedback_signal_id, revision);
CREATE INDEX idx_feedback_target_signal
  ON feedback_targets(feedback_signal_id, machine_status, rank);
CREATE INDEX idx_feedback_target_kind
  ON feedback_targets(target_kind, machine_status, feedback_signal_id);
CREATE INDEX idx_feedback_target_case
  ON feedback_targets(target_task_case_id, machine_status);
CREATE INDEX idx_feedback_target_skill
  ON feedback_targets(skill_invocation_id, machine_status);
CREATE INDEX idx_feedback_target_tool_call
  ON feedback_targets(tool_call_id, machine_status);
CREATE INDEX idx_feedback_target_tool_result
  ON feedback_targets(tool_result_id, machine_status);
CREATE INDEX idx_feedback_action_revision
  ON feedback_actions(feedback_signal_id, revision DESC);
```

技能筛选先由 `feedback_targets.skill_invocation_id` 连接 `skill_invocations`，不扫描 JSON。重复聚类需要的规范 reason code 持久化为独立列或受约束映射表。

## 11. 检测流程

### 11.1 处理顺序

```text
来源确认
  -> 注入内容和引用区域剥离
  -> 正向反转与排除规则
  -> 明确负面规则
  -> 类别与严重度归约
  -> 目标候选解析
  -> 置信度校准
  -> 幂等持久化
  -> 队列和聚类投影
```

排除规则先于负面规则执行。一个 Span 同时命中排除和负面规则时，除非存在更高优先级结构化证据，否则保持排除或低置信候选。

### 11.2 高精度规则

| 规则 | 示例 | 基础置信度 |
| --- | --- | --- |
| 明确结果否定 | “这完全不对” | 0.95 |
| 持续故障 | “还是 500”“仍然不能保存” | 0.93 |
| 明确要求违反 | “你没有按要求并行执行” | 0.92 |
| 明确遗漏 | “漏了 CSRF 校验” | 0.90 |
| 撤销返工 | “撤销这次修改，重新做” | 0.88 |
| 短指代否定 | “不对”“还是不行” | 0.82，需要前序目标 |
| 弱质量评价 | “不太好”“需要优化” | 0.60 |

置信度由以下因素调整：

- 显式对象引用：`+0.05`。
- 紧邻可评价助手结果且无插入目标：`+0.03`。
- 结构化错误码或症状：`+0.03`。
- 仅含情绪词、缺少结果谓词：`-0.20`。
- 消息同时包含新增需求：`-0.10`。
- 引用、示例或假设范围：降至排除。
- 目标歧义或多 Case 候选：最高限制为 `0.79`。

置信度限制在 `[0, 1]`，保存每一项调整理由。

### 11.3 语义分类

语义分类处理：

- 反讽和含蓄否定。
- 同一句中的正负混合评价。
- 新需求与缺陷纠正的区分。
- 过程批评与结果否定的区分。
- 外部验收转述。

模型输出必须引用 Feedback Span ID 和目标候选 ID，不得创造新证据。自动高优先级入队需要按语言和类别校准，至少满足：

- 已人工裁决样本数不低于 200。
- 主要类别样本数不低于 30。
- 高优先级候选精确率 Wilson 95% 下界不低于 0.95。
- 目标归属准确率 Wilson 95% 下界不低于 0.95。

未达到门槛的语义结果只进入低置信候选列表。

## 12. 过程异常检测

### 12.1 工具结果规范化

Pi 工具结果需要保留并脱敏以下结构化字段：

- `details.mode`
- `details.results[].agent`
- `details.results[].agentSource`
- `details.results[].exitCode`
- `details.results[].stderr`
- `details.results[].messages` 数量
- `details.results[].usage.turns`
- `details.results[].usage.input/output`
- available agent 名称集合
- child session ID 和结果回传 ID

字段用于过程判定，不作为用户评价正文。

### 12.2 子代理和并行调用

子代理调用展开为计划项：

```text
Process Plan
  planned_count
  requested_agents[]
  mode
  expected_result_ids[]

Process Execution
  started_count
  completed_count
  failed_before_start_count
  child_session_ids[]
```

判定规则：

```text
agent-unavailable:
  agentSource == unknown
  OR stderr contains "Unknown agent"

dispatch-not-executed:
  planned_count > 0
  AND started_count == 0
  AND every result has exitCode != 0
  AND every result has turns == 0
  AND no child session exists

partial-dispatch:
  0 < started_count < planned_count

child-result-missing:
  child session exists
  AND expected result is absent after episode/session closes
```

“可用代理名称与预期不同，首轮并行调用未执行”会形成：

- 一个 `agent-unavailable` 信号，记录请求名称与可用名称。
- 一个 `dispatch-not-executed` 信号，绑定首轮 Process Plan。
- 后续使用正确代理时，先进入 `awaiting-verification`。只有同一 Process Plan revision 的计划项全部启动、完成、返回预期结果 ID，且没有新的结构化异常时，才由 system Action 进入 `resolved-verified`；首轮返工记录继续保留。

外层 `isError=false` 不覆盖 `details.results[].exitCode`、`agentSource`、`stderr` 和 `turns` 的结构化判定。

## 13. 目标解析

### 13.1 目标优先级

目标解析按以下顺序生成候选：

1. 显式调用 ID、事件 ID、文件、技能名称、工具名称或代理名称。
2. 同句中的技能、工具、命令或结果对象。
3. 当前会话中最近的匹配工具结果。
4. 前一 Episode 的最终 assistant 结果。
5. 前一 Task Case 的任务结果。
6. session parent、fork、subagent 和显式 continuation 关系中的结果。
7. 时间邻近候选。

### 13.2 关系类型

- `explicit-reference`
- `same-message-reference`
- `reply-to-result`
- `previous-episode-result`
- `structured-session-parent`
- `subagent-result-reference`
- `temporal-candidate`
- `manual-retarget`

### 13.3 归属门槛

- `confidence >= 0.90` 且只有一个高置信目标时，可自动确认 `task-result` 或 `tool-result` 目标。
- 技能目标需要显式技能引用、技能专属产物或技能专属检查。
- 多技能 Case 的泛化反馈只指向 `task-result`。
- 多个目标分数接近时保持候选，进入人工目标确认。
- 跨会话目标需要结构化 session edge。
- 时间相邻关系不能单独形成自动确认。

## 14. 严重度与优先级

### 14.1 严重度

| 严重度 | 示例 |
| --- | --- |
| `critical` | 安全越权、数据损失、生产不可用、错误删除 |
| `high` | 核心结果错误、主要流程不可用、明确验收失败 |
| `medium` | 部分遗漏、返工、非核心故障 |
| `low` | 表达、格式、轻微偏好或弱质量评价 |
| `unknown` | 上下文或目标不足 |

严重度由结构化影响词、目标类型和人工动作共同决定。情绪强烈程度不直接等于影响程度。

### 14.2 队列排序

使用可解释的字典序：

1. 解决状态：未处理、处理中、待验证、已解决。
2. 信号权威性：用户直接反馈、外部验收、过程异常、助手线索。
3. 严重度。
4. 同类问题重复次数。
5. SLA 和领取状态。
6. 目标归属置信度。
7. 首次发生时间。

不使用单一不可解释分数替代上述字段。

## 15. 状态与人工闭环

### 15.1 处理状态

```text
candidate
  -> queued
  -> claimed
  -> triaged
  -> action-required
  -> fix-in-progress
  -> awaiting-verification
  -> closed
```

机器状态还包括：

- `excluded`
- `orphaned`
- `superseded`

### 15.2 解决状态

- `unreviewed`
- `action-required`
- `fix-in-progress`
- `awaiting-verification`
- `resolved-verified`
- `resolved-unverified`
- `not-actionable`
- `false-positive`
- `duplicate`

### 15.3 验证来源

进入 `resolved-verified` 至少需要一种证据：

- 后续用户明确认可。
- 与反馈目标绑定的受信确定性检查通过。
- 已验证外部验收成功。
- reviewer 人工确认并引用可定位证据。
- 过程异常的后续执行与原 Process Plan revision、全部计划项、child session 和结果 ID 完整匹配。

助手声明“已修复”、会话正常结束或工具普通返回只能进入 `awaiting-verification`。

### 15.4 误报原因

- `quoted-tool-error`
- `assistant-self-critique`
- `scope-extension`
- `preference-change`
- `environment-retry`
- `wrong-target`
- `duplicate-signal`
- `non-user-content`
- `instructional-negation`
- `positive-negation`

误报通过 correction 和 Feedback Action 追加记录。规则抑制范围扩大到其他信号时，需要独立治理和回归语料验证。

人工 Action 的 `reasonCode` 仅接受最多 64 字符的小写字母、数字和短横线机器标识。自由文本、路径和凭据只能进入先脱敏且可治理清理的备注字段。

## 16. API 设计

### 16.1 查询接口

```text
GET /api/feedback-signals
GET /api/feedback-signals/<id>
GET /api/feedback-clusters
GET /api/task-cases/<id>
GET /api/effect-overview
```

`GET /api/feedback-signals` 支持：

- `channel`
- `category`
- `severity`
- `processState`
- `resolutionState`
- `authority`
- `targetKind`
- `skill`
- `source`
- `minConfidence`
- `claimed`
- `cursor`
- `limit`

分页使用 `(observed_at, id)` keyset，不使用大 offset。

### 16.2 写接口

```text
POST /api/feedback-signals/<id>/claim
POST /api/feedback-signals/<id>/actions
POST /api/feedback-signals/<id>/retarget
POST /api/feedback-detector/rebuild
```

写接口要求 reviewer 角色、CSRF 和 expected revision。`rebuild` 需要 admin，按 detector 版本重派生机器记录。

### 16.3 扫描响应

`POST /api/effect-scan` 增加：

```json
{
  "feedback": {
    "scannedEvents": 0,
    "newSignals": 0,
    "queuedSignals": 0,
    "processAnomalies": 0,
    "pending": false,
    "detectorVersion": "feedback-v5"
  }
}
```

反馈派生达到独立预算时返回 `pending=true`，不把日志索引标记为失败。

## 17. 工作台设计

### 17.1 导航与视图

结果评审工作台增加“负面反馈”视图，包含：

- 待处理
- 待验证
- 已解决
- 误报与排除
- 过程异常

### 17.2 列表字段

每行展示：

- 解决状态。
- 通道和类别。
- 严重度。
- 脱敏评价摘要。
- 被评价对象。
- 技能名称和版本；仅在目标明确时显示。
- 任务类型。
- 重复次数。
- 置信度。
- 首次和最近发生时间。
- 领取人和 SLA。

来源使用文字徽标明确标注“用户反馈”“工具状态”“助手线索”，颜色只作为辅助。

### 17.3 详情时间线

详情按真实顺序展示：

```text
原始目标
  -> 被评价的助手结果或工具结果
  -> 用户反馈或过程异常
  -> 目标候选及归属理由
  -> 返工调用与检查
  -> 后续用户反馈
  -> 解决和验证证据
```

详情操作：

- 确认反馈。
- 标记误报。
- 修改类别和严重度。
- 重新指向目标。
- 开始修复。
- 请求验证。
- 确认解决或重新打开。
- 查看 Evidence ID 和日志 locator。

原文和工具输出默认折叠。脱敏失败时只显示 Evidence ID、哈希和 locator。

### 17.4 聚类

聚类用于发现重复问题，首版按确定性键生成：

```text
skill_id
skill_sha256
task_type
category
normalized_reason_code
target_kind
```

目标歧义、`shared` 归因和低置信分类不进入技能级聚类。聚类展示总次数、未解决数、首次和最近发生时间、受影响版本及人工推翻率。

## 18. 指标口径

### 18.1 运营指标

- 新增候选数。
- 高置信入队数。
- 已确认用户负面反馈数。
- 过程异常数。
- 首次响应时间。
- 解决时间。
- 待验证时长。
- 误报率和错误目标率。
- 同类问题复发率。
- 已验证解决率。

### 18.2 质量指标

- 分类别精确率、召回率和 Wilson 95% 区间。
- 高优先级候选精确率。
- 目标归属准确率。
- 人工修改类别比例。
- 人工重新指向比例。
- detector 版本间漂移。

### 18.3 技能指标准入

负面反馈进入技能级分析需要同时满足：

1. 用户反馈已人工确认，或检测器达到正式校准门槛。
2. 目标技能调用和精确 SHA 已确定。
3. Attribution Link 为 `direct`，或以 `shared` 维度独立展示。
4. Task Case、合同和 assessment 均为 current。
5. 反馈未被标记为误报、重复或不可行动。

技能级报表展示确认反馈数、Task Case 分母、归因类型和 detector 版本。候选数量不解释为技能失败率。

### 18.4 不可变正式快照

正式反馈指标复用现有 `metric_snapshots` 作为快照头：

- `dimensions_json.metricKind = session-negative-feedback`。
- 冻结服务端当前 cutoff、最新 configured catalog scan run、覆盖状态和扫描范围指纹。
- `versions_json` 冻结 detector、规则集、Span parser、目标解析器、语义模型、prompt、rubric 和 calibration profile。
- 覆盖不完整、目标歧义、机器 revision 过期、人工 revision 变化和关联 Case 失效时，逐条记录排除原因。

增加反馈快照明细：

```sql
CREATE TABLE feedback_metric_snapshot_items (
  snapshot_id TEXT NOT NULL REFERENCES metric_snapshots(id) ON DELETE RESTRICT,
  feedback_signal_id TEXT NOT NULL REFERENCES feedback_signals(id) ON DELETE RESTRICT,
  signal_machine_revision INTEGER NOT NULL,
  target_id TEXT,
  target_status TEXT,
  target_resolver_version TEXT,
  action_revision INTEGER,
  resolution_state TEXT NOT NULL,
  target_task_case_id TEXT,
  task_case_revision INTEGER,
  calibration_profile_id TEXT,
  metric_eligible INTEGER NOT NULL,
  exclusion_reason TEXT,
  frozen_json TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, feedback_signal_id),
  FOREIGN KEY(feedback_signal_id, signal_machine_revision)
    REFERENCES feedback_signal_revisions(feedback_signal_id, revision),
  FOREIGN KEY(feedback_signal_id, action_revision)
    REFERENCES feedback_actions(feedback_signal_id, revision),
  FOREIGN KEY(target_id, feedback_signal_id)
    REFERENCES feedback_targets(id, feedback_signal_id)
);

CREATE TABLE feedback_metric_snapshot_attributions (
  snapshot_id TEXT NOT NULL,
  feedback_signal_id TEXT NOT NULL,
  skill_invocation_id TEXT NOT NULL,
  skill_id TEXT NOT NULL,
  skill_sha256 TEXT,
  attribution_kind TEXT NOT NULL,
  metric_eligible INTEGER NOT NULL,
  exclusion_reason TEXT,
  frozen_json TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, feedback_signal_id, skill_invocation_id),
  FOREIGN KEY(snapshot_id, feedback_signal_id)
    REFERENCES feedback_metric_snapshot_items(snapshot_id, feedback_signal_id)
);
```

signal 明细冻结反馈与目标状态；attribution 明细按技能调用分别冻结 direct/shared 参与关系。一个 shared 反馈可对应多个技能 attribution 行，报表按 shared 维度展示，不选择单一技能代表整个 Case。

反馈快照和现有结果快照使用同一不可变触发器模式：seal 后禁止新增、更新和删除头及两类明细。快照读取冻结 revision，不重新读取 signal、target 或 action 的当前状态。实时反馈预览明确标记为非正式。

## 19. 增量、幂等与失效

### 19.1 增量游标

- 使用 `effect_derivation_changes.id` 作为派生游标。
- 每批最多处理 5,000 条新增事件或运行 2 秒。
- 无新增 provenance 时只读取派生状态元数据。
- detector 处理预算独立于 JSONL 索引预算。

### 19.2 幂等

逻辑信号指纹：

```text
feedback event fingerprint
+ channel
+ origin block/span locator
```

机器 revision 指纹：

```text
logical signal id
+ detector/rule/model/prompt/rubric version tuple
+ category/severity/confidence
+ excerpt hash
+ ordered target candidate fingerprints
```

目标指纹：

```text
feedback signal id
+ signal machine revision
+ target kind
+ target object id
+ relation
```

重复扫描不增加信号和队列。协议重复消息通过现有事件 alias 和反馈指纹联合去重。

### 19.3 日志重写和删除

- 信号事件失去全部 provenance 时标记 `orphaned`。
- 目标对象失效时目标标记 `orphaned`，信号重新进入目标确认队列。
- generation 重建后，同一事件指纹恢复时重新激活机器记录。
- 人工动作和 reason code 保留，binding 标记原对象失效。

### 19.4 detector 升级

- detector 版本、规则集版本和目标解析器版本分别保存。
- 规则升级在同一稳定 signal 下追加 `feedback_signal_revisions`。
- 新旧 revision 切换、目标 supersede 和 `current_machine_revision_id` 更新位于同一事务。
- 旧机器 revision 的 `is_current` 设为 0，记录 supersedes 关系。
- 人工动作继续绑定稳定 signal ID；新 revision 改变类别、严重度或 confirmed 目标时，signal 进入复核。
- revision 重建不复制 Feedback Action，也不重置 action revision。

## 20. 性能预算

| 场景 | 预算 |
| --- | --- |
| 无变化反馈扫描 | 100 ms 以内 |
| 5,000 条新增用户消息 | 2 秒以内 |
| 追加 100 MB JSONL | 检测增加不超过 3 秒或总耗时 5% |
| Feedback 列表 100 条 p95 | 200 ms 以内 |
| Case 反馈详情 p95 | 500 ms 以内 |
| 额外 RSS | 32 MB 以内 |
| 单条信号平均存储含索引 | 4 KB 以内 |

语义分类任务不计入扫描热路径预算，使用独立队列和并发上限。

### 20.1 性能验收基线

性能测试数据固定为：

- 1,000,000 条 canonical events。
- 100,000 条 Feedback Signal，每条 2 个机器 revision。
- 300,000 条 Feedback Target，包含 task、skill、tool call 和 tool result。
- 200,000 条 Feedback Action。
- channel、category、severity、source、confidence 和解决状态按黄金语料分布生成，至少 20% 为高基数技能或工具目标。

查询验收覆盖列表 API 的主要筛选组合：

1. 单独按 resolution/process state。
2. 单独按 channel、category、severity、authority/source 和 minConfidence。
3. 按 targetKind、skill、tool result。
4. 上述条件与 `(observed_at, id)` keyset 组合。
5. 待处理默认排序和重复聚类列表。

每个场景在记录 CPU、内存、SQLite 版本、数据库大小和磁盘类型的固定环境中执行：

- 冷启动：关闭连接并新建服务进程后首次查询。
- 热缓存：同一查询预热 3 次后连续执行 30 次。
- 并发：10 个只读客户端，每个执行 100 次混合查询，同时运行一个 5,000 事件反馈派生批次。

p95 从完整样本计算，冷启动结果单列。`EXPLAIN QUERY PLAN` 不得对 Feedback Signal、Revision、Target 或 Action 主表执行无约束全表扫描。存储和 RSS 预算在同一数据集上测量，报告数据库增量大小、每条信号平均大小和派生前后进程峰值 RSS。

## 21. 安全与数据治理

### 21.1 最小化保存

- 原文摘要保存上限 512 字符。
- 摘录经过私钥、Bearer、常见 token、Base64 高熵串、URL 密码和路径脱敏。
- 原文哈希在脱敏前计算，用于幂等和审计。
- 原文详情按 locator 从受控日志读取，不复制到反馈表。
- semantic request 使用脱敏摘要和结构化上下文。

### 21.2 权限

| 操作 | 角色 |
| --- | --- |
| 查看聚合数量 | 已认证用户 |
| 查看脱敏反馈详情 | reviewer |
| 领取、确认、排除和重定向 | reviewer |
| 修改 detector 配置、重建派生 | admin |
| 修改正式校准 profile | admin + reviewer 治理流程 |
| 数据清理 | admin |

### 21.3 清理

按时间、项目或技能清理时覆盖：

- Feedback Signal 摘录和 locator。
- Feedback Target evidence JSON。
- 语义分类正文。
- Feedback Action note。
- 聚类成员关系。
- 派生游标中的敏感统计。
- 本地语义请求/响应缓存。
- 系统生成的反馈导出文件和临时下载文件。
- 系统管理的数据库备份、快照副本和恢复暂存文件。

reason code、actor、revision、分类版本和哈希审计摘要继续保留。共享 Task Case 和共享 Episode 使用现有保留策略，防止按单技能清理破坏其他技能证据。

系统管理的备份记录数据保留截止时间和加密密钥版本。清理发生后，对仍在保留期的加密备份登记待删除对象；达到清理 SLA 时删除备份或执行密钥销毁，并将结果写入 cleanup audit。无法证明已删除的外部副本使清理任务保持 `pending-external-cleanup`。

## 22. 测试设计

### 22.1 黄金语料

建立至少 200 条中英文标注样本，按来源和类别分层：

| 输入 | 期望 |
| --- | --- |
| “不要修改文件” | `instructional-negation` |
| “你不应该修改这些文件” | `process-critique` |
| “错误日志如下：测试失败” | `quoted-content` |
| “你改完后测试还是失败” | `observed-defect` |
| “把标题改短一点” | `scope-change` 或低置信纠正 |
| “标题还是太长，没按要求” | `requirement-gap` |
| “没问题，现在可以了” | 正向解决证据 |
| “还是不行” | 反馈候选，目标为上一结果 |
| “如果失败就回滚” | `hypothetical-or-example` |
| 助手：“我刚才判断错了” | `assistant-self-assessment` |

### 22.2 过程异常样例

1. `isError=false`，但 `details.results[].exitCode=1`。
2. `Unknown agent`，包含可用代理列表。
3. 并行计划 4 项，全部 `turns=0`、无 child session。
4. 并行计划 4 项，只启动 2 项。
5. 子会话存在但结果回传缺失。
6. 工具超时后成功重试。
7. 首轮代理名错误，第二轮使用可用代理成功。

### 22.3 目标关系

- 显式技能名和工具名。
- “刚才那个结果”指向前一 assistant 事件。
- 下一条用户消息创建新 Case 后仍能指回上一 Case。
- fork、parent、subagent 和 clone。
- 多技能 shared Case。
- 多个候选目标分数接近。
- 无结构化关系的跨会话时间邻近。

### 22.4 幂等与恢复

- 重复扫描不新增信号。
- 半行追加不提前检测。
- 同尺寸重写替换信号。
- 文件移动保持 provenance。
- generation 删除使信号 orphaned。
- detector 版本升级重派生机器 revision。
- 人工动作在重派生后保持。

### 22.5 安全

- 私钥、Bearer、标准 Base64、Base64URL、路径、邮箱和客户名称脱敏。
- 脱敏失败时 API 和 DOM 不显示原文。
- 匿名访问返回 401。
- reviewer 和 admin 角色边界。
- 清理后 SQLite、WAL、语义缓存、导出文件、系统管理备份、API、浏览器和模型请求均无敏感摘录。

### 22.6 UI

- 桌面和移动列表滚动到底。
- 状态、来源和类别不只依赖颜色。
- 目标回答与反馈事件可在时间线互相定位。
- 长文本、长技能名和 Evidence ID 不溢出。
- 误报、重定向和解决动作具备 expected revision。

## 23. 验收标准

### 23.1 功能验收

1. 新增用户负面反馈在增量扫描后进入候选列表。
2. 高置信反馈显示被评价对象和归属理由。
3. 用户 follow-up 位于新 Case 时，反馈仍可指向上一 Case。
4. 工具错误与用户反馈分别统计和展示。
5. `Unknown agent + turns=0 + 无 child session` 产生 `agent-unavailable` 和 `dispatch-not-executed`。
6. 初始约束、引用日志、技能正文和助手自评不进入用户反馈主队列。
7. 人工可确认、排除、重定向、标记重复和记录解决状态。
8. 后续用户认可或受信检查可形成 `resolved-verified`。
9. 已解决反馈保留返工历史。
10. 反馈候选不直接改变正式任务结果和技能失败率。

### 23.2 质量验收

1. 高优先级候选精确率 Wilson 95% 下界不低于 0.95。
2. 自动目标归属准确率 Wilson 95% 下界不低于 0.95。
3. 分类别报告精确率、召回率、误报率和错误目标率。
4. 真实 Codex/Pi fixture 覆盖协议重复、注入内容和结构化工具详情。
5. 无变化扫描符合元数据快路径和性能预算。
6. 5,000 条新增用户消息检测在 2 秒内完成。
7. 追加 100 MB JSONL 的检测开销不超过 3 秒或扫描总耗时 5%。
8. Feedback 列表 100 条 p95 不超过 200 ms，Case 详情 p95 不超过 500 ms。
9. 检测额外 RSS 不超过 32 MB，单条信号平均存储含索引不超过 4 KB。

### 23.3 治理验收

1. 每条信号可回溯事件、provenance、detector 版本和目标解析依据。
2. 每个人工动作绑定 actor、reason code 和 revision。
3. 正式聚合冻结 detector、目标解析器和校准语料版本。
4. 数据清理覆盖反馈正文和模型输入，保留最小审计链。

## 24. 实施工作包

| 工作包 | 内容 | 依赖 | 交付证据 |
| --- | --- | --- | --- |
| N1 | 内容来源、Span 投影和协议去重 | 现有适配器 | Codex/Pi fixture、注入内容排除测试 |
| N2 | Feedback Signal、机器 revision、Target、Action、Derivation State schema | N1 | 迁移、复合外键、部分唯一索引、幂等、权限测试 |
| N3 | 用户反馈规则、排除规则和置信度归约 | N1-N2 | 中英文黄金语料与分类报告 |
| N4 | 过程异常和并行/子代理计划建模 | N1-N2 | Unknown agent、零启动、部分启动 fixture |
| N5 | 目标解析、跨 Case 和 session graph 候选 | N2-N4 | 目标准确率、fork/subagent/shared 测试 |
| N6 | 队列、人工动作和解决验证闭环 | N2-N5 | claim、revision、误报、重定向、解决测试 |
| N7 | API、工作台、Case 时间线和聚类 | N2-N6 | HTTP 纵向测试、桌面/移动截图 |
| N8 | 语义分类、校准、不可变反馈快照和清理 | N3-N7 | 校准语料、scan/cutoff/revision 冻结、缓存/导出/备份清理测试 |

工作包完成顺序由依赖关系约束。每个工作包都需要可运行的纵向测试，机器候选、人工动作和正式指标保持独立验收。

当前实现已覆盖 N1-N8。自动高优先级语义准入继续受黄金语料门槛控制；本机样本未达到门槛时，语义输出保存为 `needs-human`。

## 25. 设计取舍

### 25.1 通用情感分数

单一情感分数省略了评价对象、来源权威性和解决状态。本设计使用类别、证据、目标和状态组合表达反馈，支撑返工和结果冲突处理。

### 25.2 直接修改任务结果

用户反馈可能来自环境问题、需求变化、误解或多技能共同结果。反馈通过独立 Evidence 和争议队列连接 Outcome Assessment，任务失败继续遵循合同、检查和人工裁决规则。

### 25.3 同步调用语义模型

同步模型调用会增加扫描延迟，并使检测可用性依赖模型环境。确定性规则承担快速发现，语义分类通过异步队列处理混合和低置信候选。

### 25.4 因 follow-up 自动合并 Task Case

用户纠正经常评价上一结果，也可能开启新需求。系统建立反馈目标关系，保持 Case 独立；明确 continuation 和人工确认可在现有任务图中补充关系。

### 25.5 将所有工具错误视为负面评价

工具错误描述执行状态，用户评价描述验收态度。两者使用独立通道，在详情时间线和聚类层关联。

## 26. 开放决策

实施前需要确认以下产品口径：

1. 工作台默认首页是否展示“用户反馈”或“全部负面信号”。建议默认用户反馈，过程异常使用独立筛选。
2. 高置信用户反馈是否自动将当前 pass 标为 `disputed`。建议首个版本只入队，积累校准语料后再启用自动争议。
3. 已确认反馈的 SLA 是否按严重度配置。建议 critical 4 小时、high 1 个工作日、medium 3 个工作日。
4. 弱质量评价是否进入人工队列。建议只进入可筛选候选列表。
5. 外部系统验收是否纳入首个实现。建议先保留 adapter 接口，首个版本聚焦 Codex/Pi 会话。

默认实现采用上述建议值；变更需要记录产品决策和对应验收口径。