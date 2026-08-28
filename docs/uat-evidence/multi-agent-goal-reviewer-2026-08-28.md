# 最终目标评审者独立记录

## 任务输入摘要

- 日期：2026-08-28。
- 角色：最终目标评审者。
- 用户最终成功标准：质量系统中的“不可发布”只能表示当前证据或统计口径不能发布，不能被实现、API 或页面文案写成“技能无效”“技能失败”或等价结论。
- 审议范围：只读检查 `direct/shared`、partial 覆盖、合同、快照，以及用户可见的技能质量页面和 API 输出路径。
- 写入边界：仅本文档。未写入 `data/`、合同、判断、反馈、扫描记录或项目源码。

## 技能版本与运行环境

| 项目 | 记录 |
| --- | --- |
| 已完整读取技能 | `/home/jhihjian/.codex/skills/multi-agent-deliberation/SKILL.md` |
| SHA-256 | `6b428139ee11413271ef88cde9f4d53403708344dc14f4e2b403ee469cda4daa` |
| runtime | Codex，运行于 pi coding-agent harness，工作目录 `/data/dev/codex-skills-manager` |
| 本记录的实际角色 | 最终目标评审者，只读评审，不允许写项目文件 |
| 独立 subagent/worker | 当前 runtime 未暴露可启动独立 subagent、thread 或 worker 的工具；未伪造其他角色或反馈 |
| 外部/API 请求 | 0 次。未启动服务，未调用 HTTP API，避免改变运行中状态或生成扫描/判断数据。 |

本记录是一次真实、独立的角色审议记录，不声称已完成“至少两个独立 subagent”的完整多代理综合。主协调者若需满足该技能的完整成功标准，仍需在具备独立 agent 工具的 runtime 中补充至少一个不同角色，并保留其请求 ID、原始结果和采纳决定。

## 独立命令与关键输出

以下命令均为只读命令，执行目录为仓库根目录，除另列路径外均退出 `0`。

| # | 命令或检查 | 关键输出 |
| --- | --- | --- |
| 1 | `sha256sum /home/jhihjian/.codex/skills/multi-agent-deliberation/SKILL.md` | `6b428139...cda4daa`，与上表完整 SHA 一致。 |
| 2 | 完整读取指定 `SKILL.md` | 技能明确禁止把主 agent 模拟的多个角色伪装成真实 subagent；无工具时应记录降级。 |
| 3 | `rg -n -i '不可发布|技能无效|direct|shared|partial|快照|snapshot|合同|contract' .` | 定位到 `quality_service.py`、`public/reviews.js`、设计文档和对应测试。 |
| 4 | `nl -ba quality_service.py` 读取质量目录、详情、正式结果、快照、状态和聚合资格实现 | `_quality_status()` 仅在 coverage 非 complete 时返回 `not-publishable`；`shared` judgment 返回 `shared-only`，不进入聚合。 |
| 5 | `nl -ba public/reviews.js` 读取状态标签、筛选、详情、指标及比较页面 | UI 将 `not-publishable` 显示为“不可发布”，将 direct/shared 显示为“直接参与/共享参与”；比较请求强制 `attribution=direct`。 |
| 6 | `nl -ba outcome_contracts.py`、`effect_store.py` | 合同绑定精确 SHA、发布后不可变；结果快照与体验质量快照均通过 sealed/trigger 保持不可变。 |
| 7 | `nl -ba test_quality_service.py`、`test_outcome_reviews.py` | 覆盖 partial、ad-hoc、stale scope、合同混合、shared 体验排除和多技能 shared 归因。 |
| 8 | `git diff -- ...`、`git status --short` | 仅看到用户已有的 `README.md` 与其他 docs 改动；本审议未更改它们。 |

## 独立发现

### 已证实的保护

1. **“不可发布”不是技能无效。** [quality_service.py](../../quality_service.py:1065) 的 `not-publishable` 只由 coverage 非 `complete` 触发，阻断原因包含 `coverage-partial`；该分支不产生 `pass`、`partial` 或 `fail`。设计也明确将 partial scope 归为排除原因而非失败。[skill-quality-judgment-design.md](../skill-quality-judgment-design.md:84)
2. **direct 与 shared 的单技能归因边界在数据和快照层成立。** 体验判断在 shared 时标记为 `shared-only`，不具备 `aggregate-eligible` 资格。[quality_service.py](../../quality_service.py:1177) sealed 体验快照只纳入 direct 且 `direct-skill-use` 的合格判断。[quality_service.py](../../quality_service.py:641) 合同结果读取同样固定为 direct。[quality_service.py](../../quality_service.py:872)
3. **partial 覆盖不能被快照发布绕过。** seal 要求最近扫描已完成、完整、为当前 configured-catalog scope 且派生已追平。[quality_service.py](../../quality_service.py:599) 测试覆盖 partial、ad-hoc 和 stale scope 都返回 `not-publishable`，并拒绝 seal。[test_quality_service.py](../../test_quality_service.py:212)
4. **合同和快照没有把缺失条件写成失败。** 缺合同、评审不可用、陈旧证据、争议和 coverage 不完整均以 `exclusion_reason` 排除；合同结果按 SHA、合同版本、任务类型分组。[effect_store.py](../../effect_store.py:2550) [quality_service.py](../../quality_service.py:895)
5. **比较页面不能把不同口径写成优劣。** 服务端要求 complete shared scope、同一正式快照、同一合同和 direct 归因；否则只返回不可比原因。[quality_service.py](../../quality_service.py:349) 页面也仅显示“当前仅可并排查看”。[reviews.js](../../public/reviews.js:1171)

### 仍会影响用户理解的缺口

1. **页面没有明确说出“不可发布不是技能无效”。** 质量目录覆盖带和详情主状态只显示“不可发布”及内部原因码，如 `coverage-partial`。[reviews.js](../../public/reviews.js:863) [reviews.js](../../public/reviews.js:1009) 虽然实现语义正确，用户仍可能把醒目的橙色主状态理解为对技能的负面判决。
2. **shared 筛选失去“仅共同参与上下文”的语义。** 用户可选 `shared`。[reviews.js](../../public/reviews.js:796) 但正式结果函数对任何非 direct 归因直接返回空结果。[quality_service.py](../../quality_service.py:872) 随后状态计算将 `formal-snapshot-missing` 归入“证据不足”。[quality_service.py](../../quality_service.py:1065) 用户看到的是零个“合同结果”和证据不足，而不是“shared 只能作为上下文，不能生成单技能发布结论”。
3. **阻断原因未本地化为可行动的用户文案。** `label()` 未映射 `coverage-partial`、`formal-snapshot-missing`、`multiple-statistical-keys` 等质量阻断码，直接回退输出原始代码。[reviews.js](../../public/reviews.js:50) 这降低了“数据/口径限制”与“技能结论”的可区分性。

## 建议

1. 将用户可见主状态调整为“当前证据不可发布”，并在状态旁固定显示“这不表示技能无效或任务失败”。响应可增加稳定的 `publicationStatus` 和 `publicationExplanation` 字段，避免浏览器根据内部 reason code 推断语义。
2. 当 `attribution=shared` 时，返回并显示显式的 context-only 状态和原因，例如“共享参与仅用于查看任务上下文，不能形成单技能合同结果、体验比例或版本比较”；不要复用 `formal-snapshot-missing`。
3. 为所有 `blocking_reasons` 建立 API 返回的用户文案与下一步动作，页面只展示这些受控文案。`coverage-partial`、合同缺失、快照缺失、统计键混合和 shared-only 应分别解释。
4. 增加端到端 API/UI 测试，断言 partial、shared、合同缺失和快照缺失的文本从不包含“技能无效”“技能失败”，并断言 direct 与 shared 请求的发布资格不同。

## 技能设计缺陷

1. 当前技能要求完整多代理审议至少两个独立 subagent，但没有规定“单一角色记录”在上游综合中的状态字段。遇到本次这类只指定一个角色的任务时，容易被错误记为已完成多代理审议。应定义 `role-record-only`、`multi-agent-complete`、`runtime-unavailable` 三种不可混淆的结果状态。
2. 技能要求记录角色、核心结论、采纳/拒绝及证据状态，却未提供可审计的运行证据模板。应要求每个角色记录 runtime/tool 名称、独立请求或 prompt 摘要、请求 ID 或不可用原因、实际命令及退出状态，防止将本地推理包装成外部代理结果。
3. “最终目标评审者”的职责没有要求显式检查用户可见 API/页面措辞。对本任务而言，代码层门禁正确仍不足以避免用户把“不可发布”理解为“技能无效”；角色卡应把用户可见语义、空状态和错误状态列为必查项。

## 审议结论

现有质量系统在归因、合同、快照与 partial 覆盖的核心数据语义上，没有把“不可发布”实现成技能无效或任务失败。发布门禁是保守的，且 direct/shared、版本、合同和 scope 均有隔离。

最终风险位于表达层：shared 筛选退化为“证据不足”，以及阻断码缺少人类可读解释。采纳上述文案与状态建模建议后，用户将能清楚区分“当前不能发布质量结论”和“技能本身无效”。
