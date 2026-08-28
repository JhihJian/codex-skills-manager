# 多代理审议真实角色记录：实用整合者

## 任务输入摘要

- 日期：2026-08-28
- 用户目标：以真实多代理审议中的“实用整合者”角色，只读制定一条最小、可审计的路径，使 Codex Skills Manager 能从真实技能调用走到**可发布质量结论**。
- 本轮产物：本记录只描述证据的先后依赖、每项证据的证明能力和禁止捷径；不声称当前任一技能已经满足发布条件。
- 写入边界：仅本文件 `docs/uat-evidence/multi-agent-practical-integrator-2026-08-28.md`。
- 禁止事项：不写入 `data/`，不创建或发布合同，不创建质量判断、反馈、扫描或快照，不修改项目源码、测试或既有文档。
- 既有一手材料：`docs/skill-real-use-evidence-2026-08-28.md` 记录此前隔离任务中的真实技能加载与 UAT；`docs/skill-real-use-validation.md` 明确其不能替代质量系统证据。

## 技能、角色与 Runtime

| 项目 | 记录 |
| --- | --- |
| 已完整读取的技能 | `/home/jhihjian/.codex/skills/multi-agent-deliberation/SKILL.md` |
| 技能 SHA-256 | `6b428139ee11413271ef88cde9f4d53403708344dc14f4e2b403ee469cda4daa` |
| 真实执行方式 | Pi `functions.subagent`，等待独立子代理返回 |
| 子代理配置 | `reviewer` runtime profile，角色提示词指定为“实用整合者” |
| 工作目录 | `/data/dev/codex-skills-manager` |
| 写入权限 | 无。提示词明确禁止任何文件、SQLite、HTTP POST、合同、判断、反馈、扫描和快照写入。 |
| 审议对象 | 从精确技能版本的真实调用，到项目定义的正式质量结论的最小证据链。 |

本记录是该角色的一份真实输出记录，不把单一角色冒充为完整多角色汇总。此前三角色审议的存在和边界见 [技能真实使用验证记录](../skill-real-use-validation.md:66)。本轮角色任务包未预置结论，只提供目标、约束、相关路径、角色职责和要求核验的证据类型，符合技能的最小任务包要求。

## 独立请求、命令与关键输出

### 发给子代理的独立请求

请求要求其以“实用整合者”独立评审以下问题：如何从 `valid + business-use + loaded` 的真实调用，经过精确版本合同、前瞻产物与检查、Task Case 和 direct 归因、current assessment、完整 configured-catalog 扫描，最终形成可发布的正式质量结论。请求明确要求输出最小路径、禁止捷径、文件行号依据、技能设计缺陷、置信度，以及实际只读命令和退出状态。

子代理关键输出：

```text
现有一手 UAT 仅证明 multi-agent-deliberation 的指定 SHA 在隔离审议场景中被真实执行；
它不能产生正式质量结论。

质量结果和试用体验是两条独立通道：
- 合同结果由 complete scope 下 sealed metric_snapshot 支持。
- 体验结论由单独 sealed skill_quality_snapshot 支持。
```

子代理报告执行的只读命令包括 `git status --short`、`rg`、`nl -ba`、`find`、`git diff --check` 与：

```bash
PYTHONDONTWRITEBYTECODE=1 pytest --collect-only -q -p no:cacheprovider \
  test_quality_service.py test_outcome_reviews.py
```

其关键输出为 `60 tests collected`、退出状态 `0`。这只证明相关测试可被收集，不代表执行了测试体或产生质量结论。

### 主执行器的独立只读复核

| 命令或请求 | 退出状态 | 关键输出与证明能力 |
| --- | ---: | --- |
| `sha256sum /home/jhihjian/.codex/skills/multi-agent-deliberation/SKILL.md` | 0 | 输出本记录顶部的精确 SHA-256，锁定审议所依据的技能版本。 |
| `nl -ba /home/jhihjian/.codex/skills/multi-agent-deliberation/SKILL.md` | 0 | 第 38-46 行要求优先使用真实 subagent 且默认只读；第 57-69 行定义实用整合者与综合要求；第 152-159 行要求记录角色、结论、采纳动作和证据状态。 |
| `nl -ba quality_service.py | sed -n '590,710p;840,1110p'` | 0 | 第 599-612 行要求最新 scan 为 `completed + complete`、当前 `configured-catalog` scope 且派生游标追平；第 852-917 行只从 sealed `metric_snapshot` 读取正式合同结果；第 1065-1094 行定义 `not-publishable`、`evidence-insufficient`、`directional` 和 `judgment-supported`。 |
| `nl -ba outcome_reviews.py | sed -n '2260,2370p'` | 0 | 第 2302-2318 行排除缺失、不可用、未批准或 retired 的合同；第 2319-2358 行对自动语义结论要求校准 profile。 |
| `nl -ba effect_store.py | sed -n '880,940p'` | 0 | 第 907-936 行显示 `skill_quality_snapshot` 及 item 在封存后禁止更新和删除。 |
| `nl -ba docs/skill-quality-judgment-design.md | sed -n '1,90p;228,255p'` | 0 | 第 20-28 行明确 loaded 和 partial scan 不构成质量结论；第 229-239 行列出正式结果与体验判断的独立纳入条件和样本门槛。 |
| `PYTHONDONTWRITEBYTECODE=1 pytest --collect-only -q -p no:cacheprovider test_quality_service.py test_outcome_reviews.py` | 0 | 输出 `60 tests collected in 0.07s`。仅收集，不执行测试，不写入项目测试数据。 |
| `git status --short && git diff --check` | 0 | 仅发现既有 README 和 docs 未提交改动；本轮未回退或修改它们。 |

## 用户最终成功标准

对某个精确技能版本，只有当独立、可定位的调用、合同、产物、检查、归因、评审、全范围覆盖和不可变快照共同成立时，才发布“证据支持达到合同阈值”或“证据支持未达到合同阈值”。在任一条件缺失时，系统必须停留在“仅检测到加载”“证据不足”“方向性结论”或“不可发布”，而不是用正向叙述补足证据。

## 最小可审计路径

以下顺序是合同结果通道的最小路径。每一步都产出下一步不可替代的输入。

1. **固定质量对象和范围。** 先记录 `skill_id + skill_sha256 + task_type + source + attribution_kind` 与当前 configured-catalog scope fingerprint。历史调用必须是 `valid + business-use + loaded`，且 SHA 完整可定位。精确版本是统计对象，不使用当前工作区的 `SKILL.md` 回填历史版本。
2. **在真实任务前发布精确版本的 Outcome Contract。** 合同必须可定位、已批准、未 retired，且定义适用任务、必要产物、受信检查、断言、失效条件和质量阈值。该证据只定义验收口径，不证明任务结果。
3. **前瞻记录一次真实调用及其产物链。** 在调用前、产物生成后、检查完成后采集相同 Task Case revision 下的受信事件和 manifest，保留精确调用 SHA、环境指纹、受控产物与检查证据。它证明证据来源和 freshness，不证明调用因果或质量阈值已经满足。
4. **建立 Task Case、任务类型和 direct 归因。** 将调用和 Case 以结构化记录关联，并确认合同适用性和 `direct` attribution。只有 direct 可进入单技能合同结果分母；shared 只说明共同参与。
5. **形成 current Outcome Assessment。** 使用 current 产物、带断言的受信检查和必要人工裁决形成 assessment。若合同要求语义评审，额外绑定精确 tuple 的合格 calibration profile。超时、环境错误、解析错误、空断言或陈旧证据只能是 `inconclusive` 或证据不足。
6. **运行最新完整的 configured-catalog 扫描。** 只有 `completed + complete`、scope fingerprint 与当前配置一致，且派生游标追平的全目录扫描，才能使第 1-5 步进入正式封存候选。
7. **封存合同结果的 `metric_snapshot` 并读取 `quality_status`。** 快照必须冻结 Case revision、assessment revision、合同、版本、任务类型、归因、有效 verdict 与排除原因。正式结果只统计同一统计键的 `metric_eligible + direct` 样本。少于 20 只能显示计数和区间，20-49 只能是方向性结论，达到合同的最小样本、Wilson 通过率下界与失败率护栏后才可发布质量结论。
8. **需要发布试用体验时，另行封存 `skill_quality_snapshot`。** 此通道需要显式 assignment、冻结的 use-evidence、current `helpful/not-helpful` judgment、direct-skill-use 与 direct 归因，且同样要求完整范围与追平派生。它只能发布体验判断，绝不替代第 7 步的合同结果。

## 禁止捷径

- 不将“技能被读取”“真实 UAT 成功”“命令退出 0”或“测试被收集”写成技能质量结论。
- 不以当前启用副本、最终工作区文件或助手自述回填历史调用的 SHA、产物或检查证据。
- 不用同 Case、时间邻近、shared、candidate 或反馈上下文替代单技能 `direct` 归因。
- 不在调用后以泛化合同追认历史结果，也不以缺失、未批准或 retired 合同进入分母。
- 不把空断言、`NO-SOURCE`、超时、环境错误、解析失败或 stale evidence 解释为 pass 或 hard failure。
- 不以定向、ad-hoc、partial、过期 scope 或派生未追平的 scan 封存正式结果。
- 不将尚未封存的 judgment、`cannot-judge`、反馈转介或试用体验比例混入合同结果率。
- 不将 20 以下样本或 20-49 样本表述为稳定质量标签，不跨合同、任务类型、版本或归因规则合并比例。

## 独立发现

1. **现有真实 UAT 具备调用与角色执行证据，但能力边界明确。** [skill-real-use-evidence-2026-08-28.md](../skill-real-use-evidence-2026-08-28.md:63) 记录了三名独立角色和结论调整；同文件第 90 行明确它不能替代合同、direct 归因、完整扫描、检查或正式快照。因此它应作为“技能真实使用与设计缺陷”的输入，而不是质量分母。
2. **项目实现把正式合同结果与体验判断刻意拆开。** `quality_service.py:852-917` 读取 `metric_snapshot` 的合同结果，`quality_service.py:599-699` 封存 `skill_quality_snapshot` 的体验 judgment。发布材料若同时描述两者，必须分别展示来源和统计口径。
3. **实时数据库状态在本轮未知。** 本轮遵循限制，没有读取或写入 `data/`、SQLite、合同、判断、反馈或扫描状态。设计文档中 2026-08-26 的 partial 状态只能作为历史设计背景，不能被升级为 2026-08-28 的实时事实。路径必须在实际执行时逐步查询、记录并验证当前状态。
4. **子代理改变了本记录的边界。** 初始任务可被误写为“通过一次真实调用得到可发布质量结论”。独立角色指出质量结论有合同结果与试用体验两个不可互换的封存通道，本记录据此增加第 8 步并禁止混用。这是本次审议的可见决策改进。

## 建议

1. 将上述八步实现为发布前的受控编排或检查清单，每一步输出不可变 ID、输入哈希、时间、执行人和拒绝原因；失败时保留为 `evidence-insufficient` 或 `not-publishable`，不自动补录。
2. 在质量页同时展示“合同结果快照 ID”和“体验快照 ID”，并要求任何发布文案选择其中一个明确通道，避免将主观 helpful 混写为合同通过率。
3. 在封存体验快照前补强合同治理校验。当前 `quality_service.py:641-645` 仅排除 `no-contract`，而合同结果快照会进一步校验合同存在、批准状态与 retired 状态（`outcome_reviews.py:2302-2318`）。体验通道也应冻结合同摘要或 digest，并在合同不存在、未批准或 retired 时排除。

## 技能设计缺陷

`multi-agent-deliberation` 已要求记录角色、核心结论、采纳动作、拒绝原因和证据状态（技能第 150-159 行），但没有提供或强制一个可持久化、可由系统索引的审议记录 schema。历史上因而可能只留下“技能已加载”或 shared 调用，无法独立核验：

- 是否真的启动了独立 subagent，而非本地角色模拟。
- 每个角色使用的 runtime、任务包版本、提示词摘要和输入材料。
- 每项独立发现、采纳或拒绝的理由，以及审议前后的决策变化。
- runtime 失败、超时或降级为本地检查时的明确状态。

建议在技能中新增 `templates/deliberation-record.md`，并把以下字段设为必填：任务或调用 ID、技能 SHA、角色、runtime/线程标识、只读或写入边界、提示词与独立输出哈希、实际命令与退出状态、决策基线、采纳/拒绝及理由、验证链接、降级或失败状态。状态至少区分“真实 subagent 完成”“降级本地检查完成”“审议产生可见决策变化”；前两者不得自动推出第三者。

## 审议结论与置信度

**结论：高置信度。** 最小路径和禁止捷径由独立子代理输出、主执行器对实现与设计的只读复核、以及 60 项相关测试的可收集性共同支持。仍未验证实时库状态，也没有产生任何合同、评审、扫描或快照数据，因此本记录不能单独发布某一技能版本的质量结论。