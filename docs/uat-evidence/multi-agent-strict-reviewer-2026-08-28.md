# 多代理审议严格评审者记录

## 任务输入摘要

- 日期：2026-08-28。
- 角色：严格评审者。
- 审查目标：只读审查当前项目质量系统为何不能为 `local-gradle-wrapper` 与 `multi-agent-deliberation` 发布质量结论。
- 审查对象：`local-gradle-wrapper@3685ea547cb300d8a416d4b2e220efad229efd449f1c3a59cfa8d55b5fb2cc37`，以及 `multi-agent-deliberation@6b428139ee11413271ef88cde9f4d53403708344dc14f4e2b403ee469cda4daa`。
- 允许范围：只读取质量服务实现、质量 API 路由、SQLite 表结构和脱敏聚合结果，以及既有 UAT 与质量设计记录。
- 禁止范围：不写入 `data/`、合同、质量判断、反馈、扫描、项目源码或任何其他文件。不读取、不记录、不输出凭据。

## 角色与 Runtime

| 项目 | 记录 |
| --- | --- |
| 独立角色 | 严格评审者 |
| Runtime | Codex / GPT-5 coding-agent runtime |
| 工作目录 | `/data/dev/codex-skills-manager` |
| 审查方式 | 只读代码、只读 SQLite 聚合和既有证据交叉核验 |
| 技能文件 | `/home/jhihjian/.codex/skills/multi-agent-deliberation/SKILL.md` |
| 技能 SHA256 | `6b428139ee11413271ef88cde9f4d53403708344dc14f4e2b403ee469cda4daa` |
| 技能读取状态 | 已完整读取 |
| HTTP 请求 | 未执行。质量 API 要求 reviewer 身份认证；为遵守不读取或输出凭据的限制，改用同一 SQLite 的只读聚合查询和 API 实现审查。 |

## 独立命令与关键输出

以下命令均为只读。输出仅保留质量判断所需的哈希前缀、计数和状态，不含用户目标、备注、原始日志、路径定位信息或凭据。

| 检查 | 独立命令或请求 | 关键输出 |
| --- | --- | --- |
| 技能版本固定 | `sha256sum /home/jhihjian/.codex/skills/multi-agent-deliberation/SKILL.md` | SHA256 为 `6b428139ee11413271ef88cde9f4d53403708344dc14f4e2b403ee469cda4daa`。 |
| 质量 API 权限与只读接口 | 读取 `app.py` 中 `/api/skill-quality*` 路由和 `quality_service.py` | 质量目录、详情、Case 和反馈查询均为 reviewer 只读 API；创建快照为写操作，未调用。 |
| 最新扫描与配置范围 | `sqlite3 ... "SELECT status, coverage_status, discovered_files, indexed_files, pending_files, json_extract(metadata_json,'$.scopeKind') FROM scan_runs ORDER BY finished_at DESC LIMIT 5"` | 最新扫描是 `completed/complete` 的 `ad-hoc` 扫描，不能代表当前 configured catalog。最近 configured-catalog 扫描为 `partial/partial`，发现 `4295` 个文件，仅索引 `698` 个，待处理 `3597` 个。 |
| 目标版本调用与归因 | `sqlite3 ... "SELECT skill_id, substr(skill_sha256,1,16), attribution_kind, COUNT(DISTINCT task_case_id) ... GROUP BY ..."` | `local-gradle-wrapper@3685...` 有 `1` 个 Case，均为 `shared`；`multi-agent-deliberation@6b42...` 有 `3` 个 Case，均为 `shared`。 |
| 当前结果证据 | `sqlite3 ... "SELECT ... COUNT(artifacts), COUNT(check_runs), COUNT(outcome_assessments) ..."` | Gradle 目标版本的 `1` 个关联调用和审议目标版本的 `3` 个关联调用，current 产物、current 检查和 current assessment 均为 `0`。 |
| 合同与可评审性 | 同一调用归因查询，聚合 current assessment 的 `contract_version_id` 与 `assessability` | 两个目标版本的合同关联行和 `assessable` 行均为 `0`。 |
| 正式快照 | `sqlite3 ... "SELECT COUNT(*) FROM skill_quality_snapshots WHERE sealed=1; SELECT COUNT(*) FROM metric_snapshots WHERE sealed=1 AND coverage_status='complete';"` | 已封存 skill-quality snapshot 为 `0`；已封存 complete formal metric snapshot 为 `0`。 |
| 体验判断 | `sqlite3 ... "SELECT ... FROM skill_use_judgments JOIN use_evidence_snapshots ... WHERE is_current=1 AND skill_id IN (...)"` | 无返回行，即两个目标版本当前试用 judgment 均为 `0`。 |
| 派生追平 | `sqlite3 ... "SELECT MAX(id) FROM effect_derivation_changes; SELECT change_cursor, status FROM feedback_derivation_state"` | 最大变更游标 `208127`，派生游标 `200080`，状态 `pending`，尚未追平。 |
| 发布门槛实现 | 读取 `quality_service.py` 的 `_quality_status`、`_formal_results` 和 `seal_snapshot` | partial coverage、缺合同、缺 assessment 证据、缺 formal snapshot 会阻断质量状态；封存还要求最新完整 configured-catalog scope 与追平的派生游标。 |

## 独立发现

1. **覆盖范围不具备发布条件。** 当前 configured-catalog 扫描仍为 partial。最新的 complete 扫描属于 ad-hoc scope，不能替代当前配置目录的完整覆盖。质量服务会把这种 scope 状态投影为 `coverage-partial`，并拒绝以其封存正式质量快照。
2. **两个目标版本都没有 direct 归因样本。** `local-gradle-wrapper@3685...` 的唯一 Case 为 shared；`multi-agent-deliberation@6b42...` 的三个 Case 均为 shared。按质量系统规则，shared 只表示共同参与，不能进入单技能 direct 结果分母、体验分母、问题率或横向比较。
3. **合同结果链为空。** 目标调用关联的 Case 没有 current 产物、检查或 assessment，也没有合同版本和 `assessable` assessment。已加载调用只能证明技能文件被读取，不能证明任务满足合同，不能转写为通过或失败。
4. **没有正式可用的结果或体验快照。** complete formal metric snapshot 和 sealed skill-quality snapshot 均为零。即使创建体验 judgment，当前 shared 归因也会令其成为 `shared-only`，不能作为单技能聚合样本。
5. **反馈派生未追平。** `change_cursor` 落后于最大变更游标且状态为 pending。质量快照封存逻辑明确要求派生 settled，因此这一条件独立阻断体验统计的发布。
6. **样本量远未达到最低展示门槛。** 正式结果可纳入样本为零，体验可纳入 judgment 为零。系统在少于 20 个同口径 direct 样本时也不会形成结论，更不可能达到稳定质量标签所需的 50 个样本与合同阈值。

结论：这是“当前质量口径不可发布”，不是对两个技能有效性本身的否定。既有 UAT 可证明真实任务流程的局部行为与缺陷，但在缺少完整 scope、direct 归因、合同结果链、正式快照和追平派生的情况下，不能被升级为版本级质量结论。

## 建议

1. 完成一次当前 configured-catalog 的全量扫描，处理全部 pending 文件，并确认 scope fingerprint 与当前配置一致。
2. 为两个精确技能版本建立已发布合同和前瞻 Case 采集，记录 current 产物、可执行检查、assessment 与任务类型。
3. 仅对可独立归因的使用建立 direct 调用链。shared 调用保留为上下文，不得补写或推断为 direct。
4. 在完整 scope 下追平反馈派生游标，再封存 formal metric snapshot 与 skill-quality snapshot。
5. 对满足前置条件的 direct 调用分配试用 judgment，保留 `helpful/not-helpful`、显式 direct-skill-use 关系、冻结证据和排除原因；积累到相同统计键的最小样本量后再发布结论。
6. 将多代理审议记录纳入与质量结果分离的 UAT 证据链。它可以证明审议过程影响了决策或验证范围，但不能替代合同结果或体验判断。

## 实际缺少的技能审议证据字段

当前质量数据模型记录技能调用、Case、归因、结果、体验判断和反馈，但没有可关联到一次 `multi-agent-deliberation` 使用的持久化审议记录。技能说明要求的以下字段在当前质量系统中没有可验证载体，因而不能用历史 shared 调用证明“真实多代理审议已完成且产生价值”。

| 缺少字段 | 用途 |
| --- | --- |
| `deliberation_id`、关联 `task_case_id`、关联 invocation ID | 唯一标识一次审议及其判断对象。 |
| 用户最终成功标准、任务输入摘要、范围与禁止事项 | 判断角色是否收到足够且未泄露预期答案的任务包。 |
| 每个参与者的角色名、agent/run/thread ID、runtime/model、启动与结束时间、完成或超时状态 | 验证至少两个独立真实代理运行，而非主代理模拟角色。 |
| 每个角色的完整提示词或内容哈希、材料版本与访问边界 | 验证角色差异和输入可复现性。 |
| 每个角色的独立输出、发现、置信度、依据和输出哈希 | 验证反馈确实独立产生，并允许追溯最强反对意见。 |
| 审议前决策或验证基线 | 判断审议前原方案、风险判断和检查范围。 |
| 综合决策、采纳动作、拒绝或暂缓项及逐项理由 | 满足技能的综合规则，并证明不是按票数平均。 |
| 审议后变化与验证证据链接 | 证明审议实际改变了方案、风险、验证或范围，而非低价值调用。 |
| 降级状态、失败原因和未完成角色 | 区分真实多代理完成、工具失败后的本地检查和未完成任务，防止虚构反馈。 |
| 关闭或清理状态 | 证明已完成的 subagent 被适当关闭，并保留必要审计事件。 |

这些字段应以追加式、可引用的审议记录保存，并与正式质量统计严格分离。只有在它们存在时，才能对“该次多代理审议真实运行并对决策产生可审计影响”给出过程结论；它们本身仍不构成 `local-gradle-wrapper` 或 `multi-agent-deliberation` 的发布级质量结论。
