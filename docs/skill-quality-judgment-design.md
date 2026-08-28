# 技能质量判断工作台设计

## 文档状态

- 状态：已实现
- 目标读者：试用用户、产品负责人、技能负责人、结果评审维护者和开发者
- 关联设计：[技能结果评审总体设计](./skill-outcome-review-design.md)、[会话负面反馈发现与处理设计](./session-negative-feedback-design.md)
- 范围：桌面端。移动端适配不属于本设计。

## 1. 摘要

当前工作台按加载、任务结果、人工队列、合同、指标和负面反馈组织。它适合运营人员检查索引和处理待办，但试用用户无法在一个连续路径中回答：某个技能的某个版本是否值得继续使用，依据是什么，结论有多可靠。

本设计增加以 `skill_id + skill_sha256` 为中心的“技能质量”路径。质量范围只包含当前注册表中正确启用、非系统、非缺失，且 `.codex/skills` 启用副本可读的技能 ID；历史调用还必须满足 `valid + business-use + loaded`。目录按历史调用的精确 SHA 分版本展示，并标注“当前启用版本”或“历史加载版本”，因此技能升级不会丢失已启用技能的真实历史效果。用户从技能管理页或结果评审目录进入一个精确技能版本，在同一上下文中查看数据可用性、任务结果、可追溯案例、已确认反馈、合同口径和体验判断。系统先说明能否判断，再展示同口径证据，最后允许用户对一次具体使用追加体验判断。

系统不产生单一总分，不用加载次数排序，不将未确认反馈归责给技能，也不把主观试用体验伪装成合同验证结果。

## 2. 当前事实与问题边界

本设计基于 2026-08-26 的真实库走查，不将以下现象解释为技能质量结论：

- 有效技能调用已有 1,336 条，但“已加载”只能证明技能内容被读取。
- 当前 assessment 均为反馈专用的 `needs-evidence / unset` 记录，不是已绑定技能调用的任务结果。
- 当前候选反馈目标没有直接绑定 `skill_invocation_id`；Case 上下文或 shared 参与关系不能证明某个技能导致问题。
- 最新配置扫描覆盖为 `partial`，发现 4,295 个文件，仅索引 698 个，正式结果率、反馈率和横向比较必须失败关闭。
- 合同、前瞻产物和检查证据尚未形成可发布的技能结果链。

因此当前所有技能版本的桌面状态应为“证据不足”或“仅检测到加载”，而不是“好用”“不好用”“0%”或排名。

## 3. 目标与非目标

### 3.1 目标

1. 用户在三次以内的主要点击中看到一个精确技能版本的可判断状态和原因。
2. 用户能从同一页面下钻到支持结论的 Task Case、Evidence、检查、合同和已确认反馈。
3. 用户能对一次明确的技能调用记录“有帮助”“没帮助”“无法判断”或“不适用”，并保持审计和可撤销修订。
4. 系统对版本、任务类型、合同、时间范围、扫描范围和 direct/shared 归因保持严格分离。
5. 用户只能比较同口径的两个技能版本；不可比时系统解释原因，不显示差值或排名。

### 3.2 非目标

- 不用加载频率、会话数或模型主观评分生成技能排行榜。
- 不自动将同 Case、时间相邻或 shared 的负面反馈归责给单一技能。
- 不以 `SkillUseJudgment` 覆盖 `OutcomeAssessment`、Manual Decision 或合同检查结果。
- 不在扫描为 partial、样本不足或版本未知时发布稳定质量标签。
- 不在本设计中扩展移动端、外部工单同步或因果增益实验。

## 4. 核心设计决定

### 4.1 判断对象是一次调用，汇总对象是精确技能版本

一次体验判断绑定一个有效的 `SkillInvocation`，而不是绑定技能名称、Task Case 总体或一条反馈文本。版本级质量统计键固定为：

```text
(skill_id, skill_sha256, contract_version_id, task_type,
 attribution_kind, source_scope_snapshot_id, time_range)
```

`skill_sha256` 未知的调用保留在加载审计的“待补版本”范围，不进入质量目录、个案体验、比较或正式快照。不同 SHA、合同版本、任务类型和 direct/shared 归因永不自动合并。

`SkillUseJudgment` 在提交时冻结 `contract_version_id`；调用没有适用已发布合同时写入受控值 `no-contract`。这类体验记录可供个案回看和探索性试用分析，但不产生合同结果标签或跨合同比较。

### 4.2 任务结果、体验判断和负面反馈是三条正交通道

| 通道 | 已有或新增对象 | 回答的问题 | 不回答的问题 |
| --- | --- | --- | --- |
| 合同结果 | `OutcomeAssessment`、检查、产物、人工裁决 | 某次任务是否满足精确合同 | 用户是否觉得技能有帮助 |
| 试用体验 | 新增 `SkillUseJudgment` | 用户认为这次调用是否有帮助 | 任务是否通过合同验证 |
| 负面反馈 | `FeedbackSignal`、Target、Action | 用户或过程是否报告问题，是否确认指向调用 | 未确认问题是否由技能导致 |

三个通道在质量页并列展示。系统只在保留各自口径的前提下提供相关链接，绝不把其中一个通道的结论隐式转写为另一个通道的结论。

### 4.3 先判断数据可用性，再显示质量结论

质量页的主状态为：

| 状态 | 条件 | 页面表达 |
| --- | --- | --- |
| 尚未观察 | 没有有效调用 | 尚无记录，不能判断 |
| 仅检测到加载 | 有调用但无 Case | 技能内容已读取，不代表任务成功 |
| 证据不足 | 有 Case，但缺合同、SHA、任务类型、产物、检查或完整覆盖 | 缺口及数量，不能判断 |
| 方向性结论 | 最新不可变正式快照中，同口径 `metric_eligible` 的 direct 样本 20-49 | 展示计数与 95% CI，不贴稳定标签 |
| 可判断 | 最新不可变正式快照中，同口径 `metric_eligible` 的 direct 样本至少 50，满足合同阈值和护栏 | 证据支持达到或未达到合同阈值 |
| 不可发布 | 发生 coverage partial、争议、失效、合同变化或 scope 不一致 | 解释阻断原因，隐藏比例和排名 |

“好用”在界面中不作为未限定总评。可发布结论使用“证据支持达到合同阈值”“证据支持未达到合同阈值”或“证据不足”。

### 4.4 shared 只表达共同参与，不表达单技能因果

`direct` 和 `shared` 始终分列。shared Case 可用于理解任务上下文和共同参与结果，但不得进入单技能优劣比较、失败归责或 direct 结果率分母。candidate、rejected 和版本未知调用只显示排除原因。

### 4.5 当前试用反馈由用户显式归因

每次体验判断都要求选择关系：

- `direct-skill-use`：用户明确认为该技能调用与问题有关。
- `task-result-only`：问题只关联任务结果，不归因技能。
- `cannot-attribute`：看到了问题，但不能判断来源。

只有 `direct-skill-use` 且调用本身为 `direct` 时可进入该精确版本的体验统计。`task-result-only`、`cannot-attribute` 和 shared 调用保留为个案记录与缺口线索，不进入任何单技能体验分母。体验记录不能自动产生任务 hard failure 或技能失败率。

## 5. 桌面信息架构

### 5.1 入口与导航

保留现有结果评审和全局人工队列，但新增“技能质量”作为用户主入口：

```text
技能管理的技能详情 -> 质量评审 -> 按技能 ID 预筛选的观察版本目录 -> 技能质量 / <技能 ID> / <SHA>
结果评审 -> 技能质量目录 -> 选择技能版本
```

全局人工队列仍是运营入口，不进入试用用户的默认路径。URL 必须保存：

```text
skill, sha, tab, from, to, taskType, source, attribution, case
```

浏览器前进、后退、刷新、面包屑和“返回案例”都恢复这些参数及列表滚动位置。进入反馈、指标、合同或 Case 时只追加目标参数，不清空 `skill + sha`。

技能管理页不将当前工作区文件 SHA 假定为历史会话中的调用版本。入口只预筛选技能 ID，用户从观察版本目录选择实际记录过的 SHA；当前工作区版本没有调用记录时，目录显示“尚未观察”。

### 5.2 技能质量目录

目录采用可扫描表格，不使用大卡片。固定过滤器为搜索、观察状态、时间范围、任务类型、direct/shared 和来源范围。搜索使用 `input` 事件与 250ms 防抖，支持 Enter，清除按钮和 Esc。

| 列 | 内容 |
| --- | --- |
| 技能 | `skill_id` |
| 版本 | 历史成功加载的精确 SHA，并标注是否等于当前启用副本 |
| 判断状态 | 仅检测到加载、证据不足、方向性结论、可判断、不可发布 |
| 合格样本 | 当前统计键下可进入分母的 direct Case 数 |
| 最近观察 | 最近有效调用时间 |
| 操作 | 查看质量 |

目录允许勾选最多两个精确版本进入比较。无 Case 的调用显示“尚未形成 Case”，不提供“查看案例”动作。

### 5.3 单技能质量页

固定页头显示技能 ID、精确 SHA、合同版本、时间范围、任务类型、归因口径、数据来源和覆盖状态。页签为：

1. **总览**：主状态、结论摘要、样本漏斗、限制条件、按任务类型的结果表和最近可追溯 Case。
2. **案例**：按结论、证据状态和时间筛选的 Case 表；每行直接打开已有 Case 时间线。
3. **反馈**：仅展示保持当前 `skill + sha` 上下文的反馈，分为直接确认、共同参与上下文、未确认候选和未归因。
4. **指标**：当前预览、正式快照、统计键、分母、排除原因和 CI；partial 时只展示不可发布原因。
5. **合同与口径**：合同版本、适用任务类型、阈值、检查要求、任务类型覆盖与版本未知数量。

总览内容采用连续信息带、漏斗和表格。它首先回答“能不能判断”，不以 KPI 卡片掩盖缺失证据。

### 5.4 个案体验判断

质量页或 Case 时间线中，每条满足前置条件的调用可打开“判断本次使用”轻量面板。面板预填调用、版本、合同版本、Case revision、目标、证据摘要与覆盖状态。用户选择 verdict：

```text
有帮助 | 没帮助 | 无法判断 | 不适用
```

面板先选择 verdict，再明确选择关系 `该技能调用直接相关`、`仅与任务结果相关` 或 `无法归因`；调用为 shared 时默认选择“无法归因”，用户需要显式确认才可改为共同参与线索。选择“没帮助”或“不适用”时需选择受限原因码：`目标不匹配`、`指引不可执行`、`结果不完整`、`验证不足`、`增加返工`、`环境限制`、`无法归因`；备注可选、脱敏并可被治理清理。提交后显示对象化回执、当前 revision、关联证据和可撤销的后续修订。

“没帮助”且关系为 `direct-skill-use` 时，系统创建 `JudgmentFeedbackReferral`，状态为 `pending-review`。reviewer 可将它链接到已有 Feedback Signal、转换为新的试用体验反馈信号，或以原因码关闭转介。转换动作复用 Feedback Action 的目标确认、重复识别和审计规则；体验判断本身不自动进入反馈优先级、聚类或技能问题率。

## 6. 数据模型与投影

### 6.1 新增 `SkillUseJudgment`

新增追加式表及当前投影，核心字段：

```text
id, skill_invocation_id, task_case_id, case_revision, contract_version_id,
evidence_snapshot_id, actor_id, revision, verdict,
reason_code, redacted_note, attribution_relation,
supersedes_id, created_at
```

约束：

- `skill_invocation_id` 必须有效、`business-use` 且可定位 Task Case。
- 体验判断不修改 invocation、attribution、assessment、feedback Action 或合同结果。
- 只能通过追加 revision 撤销或更正；备注正文允许隐私治理清空，摘要哈希保留。
- `shared` 调用允许记录个案判断，但 `aggregation_eligibility=shared-only`。
- 判断提交时冻结 `UseEvidenceSnapshot`：调用、精确技能 SHA、合同版本或 `no-contract`、Case revision、目标、结果、检查、相关反馈状态、scan run、scope、parser/resolver 版本和 Evidence ID。
- 体验汇总以 `actor_id + task_case_id + skill_id + skill_sha256 + contract_version_id + task_type` 去重；同一 actor 在同一 Case 对同一精确版本的多个调用，只采用最新有效 direct judgment。调用级历史仍完整保留。

新增 `SkillUseJudgmentAssignment` 限制试用用户的可见调用集合和有效期；新增 `JudgmentFeedbackReferral` 保存体验判断到反馈流程的显式转介状态、reviewer、目标 Signal 和关闭原因。

referral 转换为信号时，服务端创建不可变 `canonical_event`，`source=trial-judgment`、`event_type=trial_judgment`，其来源 ID 稳定绑定 judgment revision 与 referral。新 revision 使用独立 `channel=trial-experience`、`authority=user`、`detector_id=trial-judgment-referral`；Span locator 仅引用 judgment、assignment 和冻结快照 ID。该 channel 与原始会话 `user-feedback`、`process-anomaly`、`assistant-claim` 分开统计，默认不进入既有反馈正式指标。reviewer 确认的直接目标只作为质量页体验问题线索；是否进入未来专用体验快照由 `SkillUseJudgment` 规则决定。

### 6.2 权限与证据边界

新增 `trial_user` 角色，权限仅限读取自身被分配调用的脱敏 `UseEvidenceSnapshot`，以及创建、撤销或 supersede 自己的 `SkillUseJudgment`。它不能读取原始日志、完整路径、其他用户的体验判断、review queue、Case 内不在快照中的 Evidence，也不能创建合同、运行检查、确认反馈、修改 assessment 或写入 Manual Decision。

试用快照向 `trial_user` 只显示用户目标、技能版本、结果/检查摘要、Evidence 类型和 freshness、关联反馈数量及处理状态；不包含反馈正文、反馈 actor、原始 locator、完整路径或 reviewer-only Action。`reviewer` 保留合同结果、反馈确认和转介处置职责。它不能修改 trial 用户的历史 judgment，只能追加 referral 处置和必要的审计注释。assignment 过期、调用失效、Case revision 改变或 Evidence 被清理时，试用判断面板变为只读，并保留历史冻结快照与失效原因。

### 6.3 只读投影

| 投影 | 责任 |
| --- | --- |
| `quality_readiness_projection` | 输出分母漏斗、覆盖、排除原因和当前判断状态 |
| `feedback_skill_attribution_projection` | 将反馈分为 direct target、direct Case context、shared Case context、unattributed |
| `quality_group_rollup` | 从正式快照按固定统计键物化 pass/partial/fail、体验判断、样本数与 CI |
| `quality_comparability_projection` | 判断两个版本是否在范围、合同、任务类型、归因和覆盖上可比较 |

新增不可变 `skill_quality_snapshots` 与 item 表，用于冻结体验判断聚合。它保存 scope、cutoff、统计键、纳入 judgment revision、分母、排除原因、版本元组和 CI。只有 complete scope 和追平的 derivation cursor 可以 seal。

### 6.4 API

```text
GET  /api/skill-quality?skillId&sha&from&to&taskType&source&attribution
GET  /api/skill-quality/cases?skillId&sha&...&cursor
GET  /api/skill-quality/feedback?skillId&sha&...&cursor
GET  /api/skill-quality/compare?subject=skill@sha&subject=skill@sha&...
POST /api/skill-use-judgments
GET  /api/skill-use-judgments?skillInvocationId|skillId&sha
GET  /api/skill-use-judgment-assignments/current
POST /api/skill-use-judgment-assignments
POST /api/judgment-feedback-referrals/<id>/decision
POST /api/skill-use-judgments/withdraw
POST /api/skill-quality-snapshots
```

所有质量响应必须回传 `scopeSnapshotId`、scan run、cutoff、coverage、parser/resolver 版本、统计键、分母、排除原因和下钻对象 ID。浏览器不得自行跨版本或跨 scope 聚合。

## 7. 正式口径与发布门槛

### 7.1 合同结果

合同结果率的统计单位保持为 Task Case。某个 `metric_snapshot_case` 进入精确技能版本统计前，服务端用 `case_invocation_anchor` 选择同一 Case 中同一 `skill_id + sha + contract_version + attribution_kind` 的一个有效调用：优先选择与 assessment/attribution 直接绑定的调用，其次选择最早完成的 valid loaded 调用；选择规则及被排除重复调用 ID 冻结进 item。每个 Case 在一个统计键中只能计入一次。

某个 Case 可纳入结果率，至少需要精确 SHA、已发布合同、任务类型确认、适用性为 applicable、current 产物或检查证据、有效 assessment、完整覆盖，以及最新不可变正式 `metric_snapshot` 中的 `metric_eligible=1` item。缺合同、缺检查、陈旧证据、争议、例外和 partial scope 都是排除原因，不是失败。

结果比例仅统计同一统计键中的 direct 调用：

- 少于 20：仅显示计数与区间。
- 20 至 49：显示探索性比例与 Wilson 95% CI，不贴稳定标签。
- 至少 50：当合同阈值、通过率下界和失败率护栏同时满足时，才显示“证据支持达到合同阈值”。

### 7.2 体验判断

体验判断统计单独命名为“已评审 direct 使用中的有帮助反馈占比”。最新不可变 `skill_quality_snapshot` 必须具有 complete scope、冻结 cutoff 和追平的派生游标。分母仅为同一统计键下 `direct-skill-use` 且调用归因为 direct 的 `helpful + not-helpful`；`cannot-judge`、`task-result-only`、`cannot-attribute`、shared、版本未知和 Evidence 失效判断单列为排除或证据不足原因。少于 20 仅显示计数和区间，20 至 49 显示探索性比例和 CI，至少 50 才允许显示稳定体验状态。它不等同合同结果率，不证明技能因果价值。

### 7.3 反馈

反馈确认率为：

```text
confirmed / (confirmed + excluded)
```

技能级反馈率只接纳人工确认、精确 SHA、direct target、完整 scope 下的反馈。Case 上下文和 shared 关系只显示为线索，不进入单技能问题率。

## 8. 比较设计

用户最多选择两个精确版本。服务端先验证以下条件：时间范围、任务类型、来源范围、归因规则、合同/评审语义和覆盖状态一致，且双方只使用 direct 样本。

条件满足时显示：样本漏斗、pass/partial/fail 计数和 CI、已确认直接反馈、排除原因、最后观察和代表 Case。条件不满足时允许并排查看，但明确列出不可比原因，禁止差值、排序和“更好”结论。

## 9. 当前试用状态与首个可判断结论

质量目录只展示当前启用技能 ID 的成功加载版本。未启用、缺失、系统技能、版本未知调用、等待结果、失败加载和维护调用保留在审计与加载检测中，不进入质量分析、比较、试用分配或质量快照。当前启用技能的历史加载版本继续按 SHA 单独展示，并明确标注为历史版本。页面在范围为空时分别说明“没有已启用技能”“启用但没有成功加载”或“当前筛选没有匹配版本”。

当前范围内的技能版本应显示“不可发布：证据不足”。页面必须将原因量化为 partial coverage、无合同、无 current 检查/产物、未确认直接反馈和无可纳入结果样本。

首个可判断结论需要在同一精确技能版本和任务类型下同时具备：完整扫描、发布合同、前瞻产物/检查、可评审 direct Case、明确 SHA 和足够样本。用户体验判断可在此之前开始采集，但只能作为独立试用信号。

## 10. 验收标准

### 10.1 用户路径

1. 用户从技能详情进入精确版本质量页，三次主要点击内看到判断状态、样本数和阻断原因。
2. 用户从总览进入 Case、反馈、指标、合同后，`skill + sha + filter` 上下文始终保留；前进、后退、刷新和返回恢复该状态。
3. 无 Case 的记录不出现可执行查看动作；请求失败时内容区显示错误、重试和返回，不留下永久加载态。
4. 用户可对一次符合条件的调用追加体验判断，并在刷新后看到 revision、冻结证据和审计记录。

### 10.2 口径与治理

1. 加载次数、Case 上下文反馈、shared 结果、版本未知调用和未封存预览不能生成单技能质量标签。
2. partial scope、无合同、样本不足、证据失效和争议都明确阻止正式发布，并列出排除数。
3. 直接确认反馈、合同结果和体验判断在数据模型、API 和页面上可区分、可追溯、不可互相覆盖。
4. 两技能比较在口径不一致时不给出排名或差值。

### 10.3 性能与权限

1. 目录与 Case 列表使用 keyset 分页，不在浏览器聚合全量调用或快照明细。
2. 质量汇总与比较由服务端投影返回；响应按用户角色裁剪正文、路径和 Evidence。
3. 体验判断、撤销与反馈显式关联分别使用 `trial_user` assignment 或 reviewer 转介处置权限，并使用 CSRF、expected revision 和追加审计。

## 11. 未知项与默认决策

| 项目 | 当前决策 | 验证或失效信号 |
| --- | --- | --- |
| 合同阈值归属 | 每个已发布合同冻结阈值；页面不能临时调节 | 首个合同发布时验证阈值字段和护栏 |
| 历史体验补录规模 | 不自动将历史 1,336 次调用入队；只允许用户从可追溯调用显式判断 | 若试用判断样本不足，再定义分层抽样政策 |
| 版本未知调用 | 保留在加载审计，不进入质量页、试用分配或体验判断 | 日志完整读取率改善后重新评估 |
| shared 调用体验 | 可记录、单列展示，不比较不归责 | 若形成经过批准的归因模型，新增独立统计键 |
| 完整覆盖耗时 | partial 时失败关闭，不能以后台积压补录替代正式 scope | 全目录 scan 达到 complete 后重新开启正式快照 |
| 试用用户身份 | 使用显式 assignment 和 `trial_user` 最小权限，不沿用 reviewer | 首个试用用户接入时验证会话/调用分配来源 |
| 体验问题进入反馈 | 通过 reviewer 处置的 referral 转换或关联，不自动写 Feedback Signal | 转介积压出现时定义优先级和 SLA |

## 12. 明确不做

- 不为当前不完整数据补造结果、合同或直接技能反馈归因。
- 不把现有反馈 assessment 批量转为技能 assessment。
- 不用模型或人工一次性给全部历史调用贴“好用/不好用”。
- 不将质量页改造成营销式仪表盘或用单一颜色、总分替代证据、分母和限制条件。