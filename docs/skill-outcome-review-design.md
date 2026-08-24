# 技能使用结果检查与评审方案

## 文档定位

本文定义 Codex Skills Manager 如何从 Codex 与 Pi 会话中识别一次技能调用所对应的任务、产物、验证和用户反馈，并形成可追溯的结果评审。目标读者是产品负责人、实现人员和评审人员。

本方案覆盖本地会话中的观察性评审。跨设备记录、未持久化会话和真实业务系统中的最终结果，通过外部证据接入或人工裁决进入同一评审模型。

## 现状依据

- [`usage_stats.py`](../usage_stats.py) 当前从 Codex function call、Pi `toolCall` 和 Pi `/skill` block 识别技能加载请求，按来源、会话家族和事件 ID 去重。
- [`session_logs.py`](../session_logs.py) 当前按来源枚举 JSONL，并以文件 mtime 选择扫描窗口；它还没有建立可持续增量游标。
- [`app.py`](../app.py) 与 [`public/app.js`](../public/app.js) 当前提供技能加载次数、最近证据和上下文查看，没有任务结果实体和结果裁决。
- 当前本机缓存扫描 1,533 个会话文件和 971,449 行；Codex/Pi 原始日志合计约 7GB，结果评审需要以增量索引替代每日全量重算。

## 核心结论

技能使用结果评审由三类相互独立的事实组成：

1. **加载事实**：指定技能版本是否被请求加载，以及加载是否成功。
2. **任务结果**：使用该技能的任务是否产生了符合要求的产物和验证结果。
3. **技能价值**：技能是否比未使用该技能时带来更高质量、更低返工或更低成本。

当前系统已经具备第一类事实中的“加载请求识别”。工具结果、任务边界、产物、检查结果和用户裁决属于新增评审范围。后续界面应将现有“真实使用”统一表述为“检测到技能加载”，使加载行为与结果成功保持清晰区分。

任务结果可以通过证据合同和人工评审形成可信结论。技能价值属于因果问题，需要对照实验、匹配样本或长期校准数据支持；普通会话观察只能描述“使用该技能的任务结果”，不能单独证明技能带来了提升。

## 设计目标

- 为每次技能加载建立可追溯的任务与结果证据链。
- 让自动结论能够回到原始日志位置、产物指纹和检查结果。
- 区分可自动确认的事实、模型辅助判断和人工最终裁决。
- 支持不同类型技能声明各自的验收要求。
- 支持 Codex、Pi、并行工具、子代理、fork、clone 和跨会话续作。
- 在 7GB 以上会话日志规模下执行增量索引和重复评审。
- 允许人工纠正误报、归属错误和结果结论，并保留完整审计历史。

## 解释边界

系统输出“证据支持通过”“证据支持失败”“证据不足”等观察性结论。只有满足已发布评审合同或经过人工确认的任务，才能进入已确认状态。

以下信号有固定解释口径：

| 信号 | 可以证明 | 不能单独证明 |
| --- | --- | --- |
| `SKILL.md` 读取调用 | 助手请求读取技能 | 读取成功、指令被遵循 |
| Pi `/skill:name` 展开 | Pi 已将技能正文加入用户消息 | 模型正确应用了技能 |
| 工具结果 `isError=false` | 工具按协议返回非错误结果 | 任务目标完成 |
| 命令 `exit 0` | 进程正常退出 | 检查有效、断言充分 |
| 助手声明“完成” | 助手作出了完成主张 | 产物存在、结果正确 |
| 用户没有继续追问 | 会话中没有观察到后续消息 | 用户认可或业务成功 |
| 测试通过 | 被识别的测试断言通过 | 测试覆盖用户目标 |

## 评审模式与证据可得性

结果评审分为两种模式：

- **历史回溯评审**：只使用已落盘的 Codex/Pi JSONL、Git 对象和外部系统已有记录。系统不会用当前工作区状态推断过去时点的产物。
- **前瞻采集评审**：在任务执行期间由受信采集器记录技能版本、Git HEAD、产物清单、文件指纹、检查结果和会话关系，可形成更完整的新鲜度判断。

历史回溯中缺少时点产物、检查环境或调用结果时，相关维度进入 `unknown`，总体进入 `inconclusive` 或 `not-assessable`。

### 来源与证据可得性矩阵

| 证据 | Codex 历史日志 | Pi 历史日志 | 前瞻采集器 | 外部系统适配器 |
| --- | --- | --- | --- | --- |
| 用户目标与后续反馈 | 通常可回溯 | 通常可回溯 | 可采集 | 按系统能力 |
| 技能加载请求 | 可回溯 | 可回溯 | 可采集 | 不适用 |
| 加载工具最终结果 | 依赖日志版本和工具 | `toolResult` 可配对 | 可采集 | 不适用 |
| 工作目录与会话关系 | 部分可回溯 | header/parent 部分可回溯 | 可采集 | 按系统能力 |
| 调用时 Git HEAD | 偶尔出现在命令输出 | 偶尔出现在命令输出 | 稳定采集 | 可查询时稳定 |
| 会话时点文件指纹 | 通常不可获得 | 通常不可获得 | 稳定采集 | 通过版本 ID 或 ETag |
| 测试断言与跳过数量 | 可从受支持输出解析 | 可从受支持输出解析 | 稳定采集 | 按系统能力 |
| 部署或业务最终状态 | 通常不可获得 | 通常不可获得 | 可记录查询结果 | 适配后可查询 |

“通常可回溯”表示解析器需要按会话格式版本验证字段；字段缺失时保持未知。前瞻采集器由 Pi 扩展、Codex 事件适配器或管理器受控检查器实现，所有采集记录带采集器版本和环境指纹。

## 系统不变量

### 结论不变量

1. 加载请求、加载成功、任务结果和技能价值必须使用不同状态与指标。
2. 助手自述的正向权重为零，只能作为检索线索或矛盾证据。
3. 普通 `exit 0` 只能形成执行证据，不能直接形成结果通过。
4. 受信检查器完成且断言失败时，语义评审不能覆盖该断言失败；检查器崩溃、超时和环境故障不构成任务失败。
5. 日志缺失、扫描截断、格式未知、版本未知和归因歧义必须进入不可评审或证据不足状态。
6. 多技能共同参与时默认记录 shared 参与关系；只有技能专属产物或检查才能记录 direct 参与关系。
7. 所有结论必须绑定技能版本、评审合同版本、解析器版本和证据定位。
8. 人工裁决与自动结论采用追加记录，历史结论不被覆盖。

### 数据不变量

1. 同一个来源、会话家族、事件 ID 和证据类型只能产生一个规范事件。
2. 原始日志保持只读；派生数据可按日志文件身份和解析器版本重建。
3. 前瞻采集或版本化外部对象观察到产物在验证后变化时，原验证结论失效；历史证据缺少时点指纹时，新鲜度保持未知。
4. 评审合同发布后不可修改，只能发布新版本。
5. 聚合指标必须同时展示分母、样本量、数据覆盖率和合同版本。

### 安全不变量

1. 默认评审只解析历史证据，不执行会话中记录的命令。
2. 主动重跑检查必须由用户显式发起，并通过允许列表检查器执行。
3. 原始正文、绝对路径、命令输出和产物内容按最小必要原则保存与展示。
4. 服务监听局域网地址时，结果证据详情和原文接口必须经过访问控制。

## 领域模型

### 任务与调用

- **Task Episode**：从一条用户请求开始，到该轮助手停止、下一条用户请求或会话结束为止的原子任务片段。
- **Task Case**：由一个或多个连续 Task Episode 组成的完整任务，可跨 follow-up、子代理和显式续作会话。
- **Task Fact**：从显式用户命令、工具类型、产物类型和人工标签中提取的规范任务事实，每项都引用证据。
- **Task Classification**：基于 Task Fact 形成的版本化任务标签，保存分类器版本、置信度和人工纠正。
- **Skill Invocation**：一次规范技能版本的加载请求及其加载结果。
- **Skill Version Ref**：调用时技能名称、规范路径、`SKILL.md` SHA-256 和来源。
- **Attribution Link**：技能调用与 Task Episode、Task Case、产物或检查之间的归因关系。

### 证据与评审

- **Evidence Item**：不可变的结构化证据，包含来源、极性、时间、日志定位、内容哈希和解析器版本。
- **Artifact Observation**：文件、Git 提交、结构化输出、部署状态或外部对象的观察记录。
- **Outcome Contract**：某技能版本对适用任务、必需产物、检查器和评审维度的验收约定。
- **Check Run**：一次确定性检查的输入范围、断言、结果和新鲜度。
- **Semantic Review**：独立评审模型基于证据包作出的结构化判断。
- **Outcome Assessment**：系统在某个时点基于合同和证据形成的结果结论。
- **Manual Decision**：人工对归因、证据或结论作出的追加式裁决。

## 任务边界与归因

### Task Episode 构造

会话解析器按事件顺序构造 Task Episode：

1. 用户正文或 Pi `/skill` 展开消息开启一个 episode。
2. 后续 assistant、tool call、tool result、custom tool 和产物事件归入当前 episode。
3. 下一条用户消息关闭当前 episode 并开启新 episode。
4. steering 和 follow-up 保持独立 episode，通过 continuation link 连接。
5. 分支树保留 entry lineage；当前分支和历史分支分别建模，不丢弃已执行事件。

### Task Case 聚合

Task Case 通过以下高置信关系自动聚合：

- Pi `parentSession`、Codex parent thread、fork、clone 和明确的会话替换关系。
- 子代理调用 ID 与子代理结果回传 ID。
- 相同工作项 ID，并且会话显式引用该工作项。
- 用户明确表达“继续上一个任务”并能定位父 episode。

共享提交、产物、部署对象、纯文本相似和时间邻近只生成带方向和关系类型的候选边。实现、审查、修复和回滚可能操作同一对象，候选关系进入人工确认或保持独立。

### 技能参与关系

- `direct`：技能专属动作、产物或检查与结果之间存在明确引用。
- `shared`：多个技能共同参与同一 Task Case，无法拆分各自作用。
- `candidate`：只有时间邻近或语义相关性，尚无结构化关系。
- `rejected`：人工或规则确认该调用属于审查、自测、翻译、迁移等非业务使用。

任务结果聚合使用 `direct` 和 `shared`，并分别展示。`candidate` 不进入结果指标。

### 任务分类与适用性输入

Task Fact 的 `source_kind` 为 `explicit / deterministic-parser / semantic-classifier / manual`，每项都保存 Evidence ID、生产者版本、置信度和状态。Task Classification 只消费状态为 accepted 的 Task Fact。首批谓词限定为用户命令类型、调用工具族、目标文件类型、产物选择器命中和人工 task tag。

语义分类器输出先持久化为 `source_kind=semantic-classifier` 的候选 Task Fact。只有匹配已校准 classification profile、达到合同准入阈值且合同明确允许语义事实时，候选才能自动转为 accepted；其余候选进入人工确认。人工可通过 correction 追加、接受、撤销或替换标签，原 revision 保持可查。

合同适用性解释器读取指定 classification revision。缺少合同要求的 Task Fact 时返回 `unknown`，不从技能名称或最终回答反推任务类型。

## 证据类型与能力

| 类型 | 名称 | 典型证据 | 用途 |
| --- | --- | --- | --- |
| `claim` | 主张 | 助手声明完成、测试通过 | 检索线索、矛盾检测 |
| `execution` | 执行 | 技能加载、工具调用、非错误工具结果 | 证明动作发生 |
| `artifact` | 产物 | 文件指纹、提交、结构化输出、部署对象 | 证明结果对象存在 |
| `deterministic-check` | 确定性验证 | 相关断言、测试、schema 校验、健康检查 | 证明特定性质成立 |
| `semantic-review` | 独立语义评审 | 需求覆盖、内容质量、判断合理性 | 评估规则难以完整表达的维度 |
| `external-acceptance` | 外部验收 | 用户确认、业务系统状态、审批结果 | 最终确认或反证 |

这些类型不存在固定强弱顺序。普通用户确认未必能推翻业务系统断言，语义评审也未必强于确定性检查。合同必须声明每类证据可以证明的性质、冲突优先级和最低组合。

## 结果评审合同

### 合同职责

每个技能版本可以绑定多个历史 Outcome Contract 版本，但任一时点最多只有一个 `active` 版本。合同描述：

- 技能适用的任务类型和排除场景。
- 必需产物及其可接受类型。
- 必需和可选的确定性检查。
- 语义评审维度及判定量表。
- 必须由人工确认的条件。
- 结果失效条件和证据保留策略。

合同存入独立 skills 仓库的 `codex-skills-manager.sqlite3`，与技能元数据共同版本化。运行期证据和裁决存入本地效果索引库。合同通过技能名称和 `SKILL.md` SHA-256 绑定技能版本，状态为 `draft / active / superseded / retired`。数据库对 `(skill_id, skill_sha256)` 建立“最多一个 active”约束；发布新版本在同一事务中将旧 active 标记为 superseded。

合同选择只依据规范技能 ID、精确 `SKILL.md` SHA-256 和评审模式。首次评审使用评审时 active 合同；历史复现使用原 assessment 绑定的 `contract_version_id`；用新合同重评属于显式操作并创建新 assessment revision。统计快照可以包含多个历史 contract version，但结果率必须按 `skill_id + skill_sha256 + contract_version_id + task_type` 分组，不跨合同版本合并分母。合同选定后，适用性规则再读取指定 Task Classification revision，判断任务属于 `applicable`、`not-applicable` 或 `unknown`。

可执行合同必须定义：

- 唯一 requirement ID 和适用性谓词。
- 产物选择器、允许来源、最小/最大基数和观察时间窗。
- `allOf`、`anyOf`、`minCount` 等证据组合语义。
- 检查器 ID、实现版本约束、输出解析器版本和最低受信级别。
- 断言失败、基础设施失败、证据缺失和 `not-applicable` 的映射。
- `pass`、`partial`、`fail` 的归约规则和反证优先级。
- 合同作者、业务责任人和发布审批人。生产合同至少由责任人或审批人复核，不能由单一自动流程自行发布。

### 合同示例

```yaml
skill: local-gradle-wrapper
skillSha256: "..."
contractVersion: 1
applicability:
  anyOf:
    - taskTag: gradle-build
    - taskTag: gradle-test
artifacts:
  - id: build-output
    selector: {kind: file, glob: "**/build/**"}
    minCount: 1
    observedAfter: skill-invocation
requirements:
  - id: gradle-run-completed
    allOf:
      - evidence: tool-result
        operation: gradle
        outcome: completed
  - id: tests-valid
    allOf:
      - checker: gradle-summary
        checkerVersion: ">=1,<2"
        parserVersion: 1
        trustLevel: trusted
        assertions: {failed: 0, skippedPolicy: declared}
verdictRules:
  failWhen: [tests-valid.assertion-failed]
  partialWhen: [gradle-run-completed.pass, tests-valid.inconclusive]
  passWhen: [gradle-run-completed.pass, tests-valid.pass]
semanticReview:
  required: false
humanReview:
  requiredWhen: [custom-wrapper-rewrite, unknown-mirror]
owners:
  contractOwner: build-platform
  approver: skills-reviewer
```

合同编辑采用草稿、样例预览、发布三个状态。发布版本保持不可变，历史评审继续引用原合同。

## 确定性检查

### 调用与结果配对

解析器按 Codex `call_id` 和 Pi `toolCallId` 将调用、增量更新和最终结果配对。检查运行使用三个正交字段：

```text
lifecycle: queued → running → finished
outcome:   assertion-pass | assertion-fail | infrastructure-error
           | timeout | cancelled | blocked | result-missing | parse-error
validity:  valid | stale | environment-mismatch | untrusted
```

并行兄弟调用可以交错完成，每个调用独立归并。最终状态来自结果事件，不从调用顺序推断。只有 `lifecycle=finished`、`outcome=assertion-fail`、`validity=valid` 且检查器达到合同受信级别时，才能形成任务结果失败证据。基础设施错误、超时、取消、环境不匹配和输出解析失败进入 `inconclusive` 或 `needs-human`。

### 检查有效性

确定性检查形成 `deterministic-check` 证据时必须同时满足：

1. 检查器在合同允许列表内，并识别出具体断言或测试数量。
2. 检查目标与本次产物、变更文件或任务范围存在交集。
3. 检查发生在相关产物生成之后。
4. 检查输出没有全部跳过、空断言或仅包装成功。
5. 检查环境、工作目录和代码版本可定位。
6. 前瞻采集模式具有检查前后的产物范围与指纹；历史模式缺少该信息时，新鲜度标为未知。
7. 保存检查器实现版本、输出解析器版本、受信级别和原始结果哈希。

`echo`、`true`、无断言的包装脚本、检查前产物、无关目录测试和结果缺失统一进入 `inconclusive`。

### 产物观察与新鲜度

合同通过 artifact selector 定义产物范围，包括允许根、glob、对象类型和外部对象 ID。产物观察记录观察来源、观察时刻、规范路径或对象 ID、SHA-256/Git blob ID/ETag、大小和采集器版本。

- 已提交文件使用 Git blob ID 还原历史版本。
- 前瞻采集器在技能调用前、产物生成后和确定性检查后记录 manifest。
- 外部对象使用版本号、ETag 或平台 revision 识别状态。
- 当前工作区快照只描述当前状态，不回填为历史时点状态。

同一 artifact selector 在检查后出现不同指纹时，相关 assessment revision 标为 `freshness=stale`。历史日志没有产物 manifest、Git 对象或外部版本时，标为 `freshness=unknown`，合同据此决定进入 `needs-evidence` 或允许基于其他证据评审。

### 主动重跑

主动重跑属于显式操作：

- 默认关闭自动重跑。
- 仅允许注册检查器构造命令，不执行日志中的任意命令文本。
- 重跑前展示工作目录、命令、影响范围和超时。
- 重跑结果记录环境指纹，与历史证据分开显示。
- 检查器在隔离工作区内以清理后的环境变量运行；默认禁止网络、敏感凭据和宿主写权限，并设置进程、时间、内存和输出上限。缺少可用隔离能力的平台关闭主动重跑。

## 独立语义评审

语义评审处理需求覆盖、文档质量、分析合理性和交互结果等难以完全规则化的维度。

评审输入由系统构造证据包：

- 用户目标和合同量表。
- 调用时技能版本。
- 限长、脱敏的产物快照。
- 确定性检查结果。
- 用户后续反馈和合同声明的高优先级反证。

助手最终自述不进入正向证据包。评审产物中的指令按非可信数据处理。输出采用固定 schema，每个判断必须引用 Evidence ID。

语义评审遵循以下裁决关系：

- 受信检查器的有效断言失败时，自动结论进入 `fail`。
- 硬检查通过只确认对应性质；合同要求内容质量时继续语义评审。
- 引用不存在、模型不可用、置信度不足或检测到提示注入时进入 `needs-human`。
- 模型版本、prompt 版本和量表版本随评审结果保存。

上线前使用人工标注黄金集校准，并按合同版本、任务类型和来源分层报告 pass/partial/fail 的精确率与召回率。初始门槛为至少 200 个已裁决案例、每个主要任务类型至少 30 个案例，自动 pass 精确率的 95% 置信区间下界达到 0.95。模型、prompt、量表或合同升级必须重跑黄金集；线上按比例抽样自动结论，监测人工推翻率和分层漂移。未达到门槛的语义结果只进入人工队列。

## 评审维度与状态

### 评审维度

每个维度使用 `pass / partial / fail / not-applicable / unknown`：

- **适用性**：该任务是否应使用此技能。
- **指令遵循**：合同要求的关键步骤是否发生。
- **产物完整性**：必需产物是否存在且可定位。
- **结果正确性**：确定性检查和业务断言是否成立。
- **需求覆盖**：用户要求是否得到完整响应。
- **验证充分性**：验证是否覆盖结果风险。
- **返工与反馈**：后续修正、回滚或用户否定是否构成反证。
- **成本与效率**：耗时、token、工具调用和返工成本是否合理。

系统默认不将维度压缩成单一加权分数，保留各维度状态和证据。

### 正交状态字段

评审记录使用相互独立的字段，防止流程进度、证据能力和结果极性混在一个枚举中：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `process_state` | `discovered / indexed / attributed / evidence-collected / reviewed / blocked / invalidated` | 数据处理进度 |
| `assessability` | `assessable / not-assessable / needs-evidence` | 当前证据是否允许形成结论 |
| `automated_verdict` | `unset / pass / partial / fail / inconclusive` | 合同解释器的结果 |
| `human_verdict` | `unset / pass / partial / fail` | 绑定当前 assessment revision 的人工裁决 |
| `conflict_state` | `none / disputed / resolved-by-correction / exception-accepted` | 自动证据与人工裁决是否冲突 |
| `freshness` | `current / stale / unknown` | 产物和检查是否仍对应同一版本 |

`needs-human` 是 review task 的队列原因，不是结果状态。`not-assessable` 只属于 assessability。重新采集、合同升级、检查器/解析器升级或语义模型升级会创建新的 assessment revision。人工裁决只追加绑定当前 assessment 的 manual decision revision，不创建 assessment revision。

### 当前有效结论投影

1. `process_state=invalidated` 或 `freshness=stale` 时，不发布当前结果，只展示历史 revision。
2. 人工裁决绑定完整评审版本元组：assessment revision、contract version、skill SHA、classification revision、parser/checker/model/prompt/rubric 版本。提交、修订和撤销裁决只追加 manual decision revision，并在同一事务中更新当前 decision 投影。新的 assessment revision 默认不继承旧裁决；carry-forward 必须由操作者显式创建绑定新 assessment 的裁决并记录差异。
3. 人工裁决与受信硬断言冲突时，普通 pass 操作不能产生有效通过。证据错误通过 correction 使原检查失效后重新评审；业务接受例外记录为 `exception-accepted`，单列展示且排除正常通过率和失败率。
4. 未解决冲突的 `conflict_state=disputed`，不进入结果率。
5. 没有当前 revision 人工裁决时，使用当前合同产生的 `automated_verdict`。
6. `assessability` 不是 `assessable` 时，对外结果统一显示“不可评审”或“需要证据”。
7. 合同、检查器、解析器或模型版本变化后创建新 revision；旧 revision 保持可查，评审任务可以重新打开。

自动 `pass` 表示该任务结果得到合同证据支持，不解释为技能产生了因果增益。

## 数据架构

### 存储边界

新建本地派生库 `data/skill-effect-index.sqlite3`，启用 WAL 和短事务。原始日志保持在 Codex/Pi 目录中，数据库只保存结构化字段、内容哈希、原文定位和必要的脱敏摘录。

主要数据组：

- 扫描：`scan_runs`、`log_files`、`log_file_generations`、`parser_versions`。
- 会话：`sessions`、`session_edges`、`task_episodes`、`task_cases`、`task_facts`、`task_classifications`。
- 调用：`skill_invocations`、`tool_calls`、`tool_results`。
- 结果：`artifacts`、`evidence_items`、`attribution_links`、`check_runs`。
- 评审：`semantic_reviews`、`outcome_assessments`、`review_tasks`、`manual_decisions`、`corrections`、`exceptions`、`actors`。
- 聚合：按天、技能版本、合同版本和任务类型生成物化指标。

原始事件具有稳定 `event_fingerprint`。会话提供稳定 event ID 时，指纹包含来源、会话家族和 event ID；缺少 event ID 时，使用规范事件类型、协议时间、parent ID、call ID 和规范化 payload 哈希生成内容寻址指纹，不使用路径和行号充当长期身份。人工决定绑定 event fingerprint 和 task-case revision，评审结果使用 revision 追加，写入时校验 `expectedRevision`。

### 增量索引

每次可变日志内容对应一个持久化 `log_file_generation` UUID。首次发现时记录来源、会话 header ID、规范路径和设备/inode（平台支持时）作为身份线索；这些线索不包含会随正常追加变化的尾部哈希、大小、mtime 或 ctime。内容一致性通过独立 checkpoint 验证。

1. 新文件从头解析。
2. 追加文件从上次完整 JSONL 行后的字节游标继续。
3. 半行保留起始偏移，等待下一次补全。
4. 每次续读前验证首条完整 header、旧长度范围内的稀疏分块哈希，以及游标前固定窗口哈希；正常追加只新增 checkpoint，不改变 generation UUID。
5. 文件缩短、原地重写、同尺寸替换或轮转时创建新 generation，并在单事务中重建。文件移动后若 header、平台文件身份和旧范围 checkpoint 连续，则保持 generation UUID，只向 `log_file_locations` 追加新位置。
6. 规范事件按 event fingerprint 唯一存储，`event_provenance` 记录事件与一个或多个 generation、offset、line 的关系。generation 重建只替换 provenance 和派生观察；另一 provenance 仍存在时规范事件保持有效。全部 provenance 消失时事件标记 `orphaned`，人工裁决和审计记录继续保留，等待自动重连或人工处理。
7. 解析器升级标记受影响 generation 重建；合同升级只触发重新评审。

扫描器同时维护目录清单，识别删除和移动；跨文件 Task Case 通过稳定 fingerprint 和 session edge 重建，不依赖派生行自增 ID。扫描预算按字节数和运行时间控制，并报告已发现、已索引、待扫描、失败文件和覆盖时间范围。指标在覆盖不完整时显示“部分数据”。

## 端到端评审流程

以 `local-gradle-wrapper` 参与一次“运行项目测试”任务为例，产品负责人或评审人员按以下顺序处理：

1. **确认任务**：查看用户目标和 Task Case 边界，确认 fork、follow-up 和子代理是否属于同一任务。
2. **确认调用**：核对技能规范路径、调用时 SHA-256、加载工具最终结果和参与关系；审查或翻译技能内容的调用标记为非业务使用。
3. **选择合同**：按技能 ID 和 SHA-256 选择当前唯一 active 合同。精确版本没有合同时，案例进入 `not-assessable`。
4. **检查适用性**：合同解释器将任务判为 `applicable`、`not-applicable` 或 `unknown`。未知适用性进入人工队列。
5. **收集产物**：历史模式优先读取 Git blob、结构化 Gradle 输出和日志内对象；前瞻模式读取采集时的 artifact manifest。
6. **执行合同条款**：`gradle-summary` 解析测试总数、失败数和跳过策略。测试断言失败形成 fail；JDK 缺失、超时或解析器不支持形成 inconclusive。
7. **处理反证**：检查后续用户否定、修复提交、回滚、产物漂移和外部系统失败。反证与自动通过冲突时进入人工队列。
8. **形成自动 revision**：保存各维度状态、合同条款结果、证据引用和当前 `automated_verdict`。
9. **人工裁决**：评审人员查看时间线，提交 pass、partial 或 fail，并说明冲突处理。裁决产生绑定当前 assessment 的 manual decision revision。
10. **进入指标**：只有覆盖完整、归因有效且当前结论不处于 disputed 的案例进入对应分母；页面同时显示样本量和合同版本。

每一步都可以结束为不可评审或需要证据，不以缺证据推断失败。

## 评审界面

### 总览

技能审查页面新增“使用结果”视图，首屏展示：

- 日志索引覆盖率和异常文件。
- 合同覆盖率和可评审率。
- 待人工、证据不足、检查失败和已确认数量。
- 按技能版本和任务类型拆分的趋势。

### 使用该技能的任务结果

单技能页面将“加载频率”和“任务结果”分栏展示：

- 检测到加载的 Task Case 数。
- 可评审数、证据支持通过数、失败数和证据不足数。
- 合同版本、样本量和数据覆盖率。
- 适用性、遵循度、正确性、验证充分性和返工维度分布。

### 单次任务详情

详情以时间线展示用户目标、技能加载、工具调用与结果、产物、检查、最终回答和后续反馈。每条证据可跳转到原日志定位。右侧显示合同要求、维度结论、自动判断依据和人工裁决记录。

### 人工评审队列

队列包含：

- 新合同的校准样本。
- 低置信度和归因歧义任务。
- 确定性检查失败或语义评审冲突任务。
- 无结果证据但具有高风险操作的任务。
- 按比例抽样的自动通过任务。

人工操作分为四类，避免用一个 verdict 承载不同语义：

- **correction**：处理非业务使用、归属错误、重复事件、Task Case 合并/拆分、错误证据和错误 task tag；提交后重建受影响 assessment 并重新打开队列。
- **decision**：在 `assessability=assessable` 时提交 pass、partial 或 fail，绑定当前 assessment revision。
- **disposition**：将案例设为 `not-assessable` 或 `needs-evidence`，填写原因码；前者关闭当前队列，后者保留待补证据状态。
- **exception**：业务接受一个带有效硬失败或偏差的结果，记录批准人、范围和到期条件；案例不进入正常结果率。

每类操作都要求 reason code、actor、expected revision 和可选备注。撤销操作通过新 revision 完成，原始自动结论和人工记录继续保留。

## API 边界

- `POST /api/effect-scans`：启动增量扫描或显式重建。
- `GET /api/effect-scans/<id>`：查询扫描进度、覆盖率和错误。
- `GET /api/effect-overview`：读取覆盖、队列和聚合指标。
- `GET /api/skill-use-events`：按技能、状态、时间和合同版本分页查询。
- `GET /api/skill-use-events/<id>`：读取任务、调用和完整证据包。
- `POST /api/skill-use-events/<id>/corrections`：追加误报或归属纠正。
- `GET /api/review-tasks`：读取人工评审队列。
- `POST /api/review-tasks/<id>/claim`：领取评审任务。
- `PUT /api/review-tasks/<id>/decision`：提交带 revision 的人工裁决。
- `PUT /api/review-tasks/<id>/disposition`：提交不可评审或待补证据处置。
- `POST /api/review-tasks/<id>/exceptions`：记录业务接受例外，排除正常结果率。
- `GET/POST /api/skills/<name>/outcome-contracts`：读取或创建合同草稿。
- `POST /api/outcome-contracts/<id>/publish`：发布不可变合同版本。
- `POST /api/check-runs`：显式运行允许列表检查器。

现有 `/api/usage-stats` 和 `/api/skills/<name>/usage` 保持兼容，后续从增量索引的物化结果读取。

## 指标口径

所有正式指标绑定不可变 `metric_snapshot`。快照保存索引 scan run、截止时间、覆盖状态，以及精确的合同、解析器、检查器审批、classification profile、calibration profile、模型、prompt 和 rubric 版本。`metric_snapshot_cases` 为每个纳入或排除的 Task Case 冻结：Task Case revision、assessment revision、manual decision revision（可为空）、参与关系、当前有效结论、`metric_eligible` 和排除原因。

人工裁决、correction、重评或治理状态变化不会修改已有快照；系统创建新快照反映变化。页面可以提供实时预览，但预览明确标记为非正式且不用于历史趋势。统计单位定义如下：

正式通过率、部分通过率和失败率的最细统计键固定为 `(skill_id, skill_sha256, contract_version_id, task_type, attribution_kind)`。跨合同视图只汇总案例计数和覆盖率；需要展示综合结果率时，必须由用户选择单一合同版本或明确的同口径合同集合。

- **有效加载事件**：人工或规则未标记为 rejected/duplicate，且技能版本可定位的 invocation。
- **有效 Task Case**：统计窗口和完整索引覆盖范围内，至少包含一个有效加载事件，且当前 revision 未失效的 Task Case。
- **可评审 Task Case**：有效 Task Case 中，合同、归因和合同要求的证据能力均满足最低条件的案例。
- **结论明确 Task Case**：当前有效投影为 pass、partial 或 fail，且 `conflict_state=none` 或 `resolved-by-correction` 的案例。
- **指标准入 Task Case**：结论明确，并通过 `metric_eligible` 谓词的案例。

`metric_eligible` 保存布尔值和排除原因，并按结论来源执行：

- 所有结论首先要求 assessment 绑定的 contract version 位于统计快照的已审批合同集合。合同是否准入按快照时点的治理状态判断，active 与 superseded 历史版本均可被显式纳入，retired 版本默认排除。
- 确定性结论要求检查器与解析器版本已审批、有效性为 valid，且对应检查器回归夹具通过。
- 语义结论要求精确匹配 contract/model/prompt/rubric 版本的 calibration profile 达到黄金集门槛。
- 人工结论要求认证 actor 具备 reviewer 权限，绑定当前 assessment revision，且不存在有效硬失败冲突。
- `disputed`、`exception-accepted`、失效、覆盖不完整和治理规则不满足的案例统一排除，并报告原因数量。

`shared` 案例在单技能视图中为每个参与技能各记一个带 shared 标签的案例，在跨技能总览中按 Task Case 去重。系统不使用 shared 案例做技能间优劣比较。每个分母同时报告被合同缺失、覆盖不完整、归因歧义、失效和争议排除的数量。

| 指标 | 定义 |
| --- | --- |
| 加载检测精确率 | 人工确认的有效加载 / 已人工复核加载事件 |
| 合同覆盖率 | 有已发布合同的技能版本 / 发生加载的技能版本 |
| 证据覆盖率 | 有结果证据的有效 Task Case / 有效 Task Case |
| 可评审率 | 可评审 Task Case / 有效 Task Case |
| 评审覆盖率 | 结论明确 Task Case / 可评审 Task Case |
| 指标准入率 | 指标准入 Task Case / 结论明确 Task Case |
| 通过率 | 当前有效结论为 pass 的指标准入 Task Case / 指标准入 Task Case |
| 部分通过率 | 当前有效结论为 partial 的指标准入 Task Case / 指标准入 Task Case |
| 失败率 | 当前有效结论为 fail 的指标准入 Task Case / 指标准入 Task Case |
| 证据不足率 | `assessability=needs-evidence` / 有效 Task Case |
| 人工推翻率 | 被人工更改的自动结论 / 已人工复核自动结论 |
| 返工率 | 出现修正、回滚或用户否定的有效 Task Case / 结论明确 Task Case |

机会召回率需要人工抽样“本应触发但未触发”的漏检正例。没有机会样本审查时显示 `N/A`。结论明确样本少于 20 时只展示计数和置信区间，不展示稳定率值。所有比率同时展示样本数、时间范围、合同版本和覆盖状态。

## 隐私与访问控制

- 派生库优先保存 locator、哈希和限长摘录，不复制完整会话。
- 证据查看按需读取原日志，并执行路径、密钥、token、个人信息和大段源码脱敏。
- 原始路径默认折叠，导出内容使用项目别名和相对定位。
- 局域网访问采用单用户认证：首次启动生成 256-bit 随机访问密钥，文件权限设为仅当前用户可读；登录后换取 `HttpOnly`、`SameSite=Strict` 会话 cookie，写接口同时校验 CSRF token。反向代理认证作为显式替代模式。
- 证据详情、原文、命令输出、合同编辑、主动检查和导出接口统一要求认证与权限检查，不设置局域网免认证旁路。
- 文件读取和检查工作区使用规范路径、允许根和打开后文件身份校验，防止路径逃逸与符号链接替换。
- 派生数据库、WAL/SHM、访问密钥和导出目录使用仅当前用户可读写的文件权限。
- 脱敏器无法确认安全时拒绝展示或发送原文，只返回 locator 和内容哈希。
- 提供按时间、项目和技能清理派生证据的能力；清理范围包含 SQLite/WAL、语义模型缓存、导出文件、备份、人工摘录和可识别审计备注。
- 语义评审数据默认只发送给本机已配置模型，外部模型调用必须显示数据范围并取得授权。

访问控制和落库前脱敏属于证据详情 API、人工评审界面和主动检查器的前置条件。

### 操作者身份与治理

本地单操作者模式在初始化时创建稳定 actor UUID 和 `operatorName`，默认授予 `admin`、`contract-owner` 和 `reviewer` 三个角色。访问密钥登录后的所有合同、领取、裁决、纠正、例外和导出记录该 actor。该模式明确表示“单人操作”，共享访问密钥会破坏人员区分，因此不支持多人共用。

多人治理使用受信反向代理身份模式，将外部认证主体映射到本地 actor，并配置 `admin / contract-owner / reviewer` 角色。系统只信任来自已配置代理地址且带签名校验的身份。合同可以声明 owner 与 approver 分离；单操作者模式无法满足该规则时，发布操作保持 blocked。自审允许的合同必须显式标记 `single-operator-approved`，指标可按治理级别筛选。

授权矩阵固定为：`admin` 管理认证、清理、检查器信任和主动执行权限；`contract-owner` 创建合同草稿并按治理规则发布；`reviewer` 执行领取、correction、decision 和 disposition；exception 同时要求 reviewer 与 admin，单操作者拥有两角色时记录为单人例外。所有写接口在领域内核之外执行 actor 和 revision 校验。

## 测试与验收

### 黄金样本

从真实 Codex/Pi 多版本会话建立脱敏黄金集，至少覆盖：

- 加载成功、加载失败、取消和结果缺失。
- 并行工具完成顺序交错。
- fork、clone、子代理和跨会话续作。
- 多技能共同参与和技能归属错误。
- 助手声明完成但没有产物。
- `exit 0` 空命令、无关测试和全部跳过。
- 检查通过后产物变化。
- 用户确认、用户否定、返工和回滚。
- 日志截断、删除、格式未知和扫描覆盖不完整。
- 语义评审引用伪造和产物提示注入。

### 系统验收

1. 重复增量扫描不产生重复事件或重复裁决。
2. 未变化文件只读取元数据，不重复扫描正文。
3. 每个结论都能回溯到 Evidence ID、日志定位和内容哈希。
4. 仅有助手自述或普通 `exit 0` 的样本产生零个自动 pass。
5. 受信检查器的有效断言失败不能被语义评审改为自动 pass；环境错误和超时产生零个自动 fail。
6. 前瞻模式观察到产物漂移后，相关检查和结论标为 stale；历史模式缺少指纹时保持 unknown。
7. 自动正向结论达到黄金集门槛后才进入正式聚合指标。
8. 覆盖不完整时，页面和 API 都返回明确覆盖状态。
9. 人工修改使用 revision 防止并发覆盖，并保留原结论。
10. generation 原地重写、同尺寸替换、截断后增长、移动和删除夹具均不保留错误派生证据，人工裁决不会被级联删除。
11. 服务崩溃后可从完整 JSONL 行游标恢复，SQLite 事务保持一致。
12. 认证、CSRF、路径逃逸、符号链接替换、脱敏失败和主动检查隔离测试全部通过。
13. 在 `jhihjian-MACO` 基线环境和 8GB 等价日志集上，冷索引不超过 10 分钟，追加 100MB 不超过 60 秒，无变化扫描不超过 15 秒，峰值 RSS 不超过 512MB，任务详情查询 p95 不超过 500ms。
14. 派生库增长、WAL checkpoint、并发页面读取和清理操作具有独立容量与延迟报告。

## 实施计划

实施按依赖关系组织为工作包。每个横向包有局部验收，`V1` 提供产品负责人可实际裁决单个案例的纵向闭环：

| ID | 工作包 | 前置 | 主要边界 | 输出与验收 |
| --- | --- | --- | --- | --- |
| A | 访问与数据治理 | 无 | `auth.py`、`app.py`、登录页、脱敏器 | 单用户 actor、CSRF、文件权限和清理协议；对应验收 12 |
| B | 规范事件与增量索引 | 无 | `effect_store.py`、Codex/Pi adapter、SQLite migration | generation、游标、fingerprint、兼容查询；对应验收 1、2、10、11、13、14 |
| C | 加载结果与版本绑定 | B | invocation parser、skill resolver、现有 usage 兼容层 | 调用/结果状态机、规范路径和 SHA-256；失败加载不计成功 |
| D | Task Episode 与分类 | A、B、C | episode builder、Task Fact、classification revision | 单会话 episode、适用性输入、带 actor 的人工标签纠正；黄金样本可复现 |
| E | 合同与确定性解释器 | A、B、D | contract store/interpreter、Gradle 与文档 checker | 受治理的 active 合同选择、条款求值、正交状态；对应验收 4、5、6 |
| V1 | 首个评审闭环 | A-E | review API、单案例详情、队列、correction/decision/disposition/exception | 产品负责人完成一例端到端裁决；对应验收 3、8、9、12 |
| F | 前瞻采集与隔离重跑 | A、B、C、E、V1 | collector protocol、artifact manifest、sandbox runner | 指纹新鲜度和受控重跑；对应验收 6、12 |
| G | 复杂任务关系 | B-D、V1 | session graph、subagent/fork adapter | 跨会话 Task Case 和 shared 参与关系，不因共享对象错误合并 |
| H | 语义评审与聚合 | E、V1，按需使用 G | semantic reviewer、calibration profile、metric snapshot | 分层校准、metric_eligible 和分母一致；对应验收 7 |

`V1` 限定为本机 Codex/Pi、单会话 Task Episode、已提交文件或结构化工具输出、Gradle 与文档类两个代表性合同、人工最终裁决。该闭环不启用自动语义通过和主动重跑，但使用与完整目标设计相同的数据模型、状态字段和证据协议。

每个工作包都必须维持“加载事实、任务结果、技能价值”三类语义分离，并提供向后兼容查询。访问控制工作包必须在新的证据详情界面和写接口之前完成。

## 未知项与待决策

| 优先级 | 类型 | 事项 | 当前默认 | 验证或决策动作 | 截止点 |
| --- | --- | --- | --- | --- | --- |
| P1 | 未知项 | 各技能的成功定义和合同责任人 | 无合同的任务进入 `not-assessable` | 选择代表性技能，由业务负责人发布首批合同 | 合同模块开发前 |
| P1 | 未知项 | 历史任务可恢复哪些时点产物 | 只接受 Git 对象、日志内结构化对象和外部版本 | 对代表性技能统计可评审率 | 合同模块开发前 |
| P1 | 待决策 | 主动重跑检查的平台隔离能力 | 默认只读，不自动执行 | 验证无网络、无凭据、只读宿主和资源限制 | 主动检查开发前 |
| P1 | 未知项 | Codex fork、子代理和跨会话关系字段的版本差异 | 关系不明时保持独立 | 建立真实多版本夹具并验证字段 | 任务图开发前 |
| P1 | 未知项 | 无稳定 event ID 的内容指纹跨解析器稳定性 | 人工裁决保留并允许 orphan/relink | 用日志重排和解析器升级夹具验证 | 增量索引开发前 |
| P1 | 待决策 | 检查器受信级别的审批责任 | 未审批检查器只生成执行证据 | 确定代码审查、版本签名和撤销流程 | 首批检查器发布前 |
| P1 | 未知项 | 前瞻采集器在 Codex/Pi 的字段完整度 | 缺字段维度保持 unknown | 构建最小采集原型并比较矩阵 | 前瞻采集开发前 |
| P2 | 未知项 | 语义评审模型、成本和数据边界 | 本机模型优先 | 对代表性样本比较准确率、耗时和数据发送范围 | 语义评审开发前 |
| P2 | 待决策 | 原始摘录及派生副本保留期 | 只保存限长摘录和哈希 | 确定默认天数、备份和端到端删除规则 | 数据迁移前 |
| P2 | 未知项 | 性能基线在 Windows 与 Linux 的差异 | 使用文档中的初始门槛 | 在两类设备运行 8GB 基准并校准 | 增量索引验收前 |
| P2 | 未知项 | 跨设备会话与业务系统验收接入 | 通过外部 Evidence Adapter 扩展 | 选择首个外部系统验证接口 | 外部验收接入前 |

上述默认值只允许完成不会产出结果结论的只读探索索引。合同解释器、指标、证据详情和主动检查分别受对应截止点约束。

## 已采纳决策

- 使用结果评审与现有使用频率统计保持独立语义。
- 结果证据采用增量 SQLite 索引，不继续扩展覆盖式 JSON 缓存。
- 历史回溯与前瞻采集采用不同证据能力，当前工作区状态不回填历史结论。
- 受信检查器的有效断言失败优先于语义评审；基础设施失败保持不确定。
- 无合同、无版本或归因歧义统一进入不可评审，不解释为失败。
- 多技能任务默认记录 shared 参与关系，不进行缺少证据的单技能归功。
- 人工裁决、合同和自动评审全部版本化并保留审计链。
- 结果详情上线前完成单用户认证、CSRF 和落库前脱敏。
- 使用该技能的任务结果聚合显示分母、样本量、覆盖率和合同版本。