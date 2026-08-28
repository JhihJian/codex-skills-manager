# 技能真实使用验证记录

## 状态

- 状态：已完成首轮验证
- 验证时间：2026-08-28
- 目标：让独立 subagent 在隔离的真实任务中实际遵循技能说明，发现技能设计缺陷，并与 Skills Manager 的调用索引和质量口径分开记录。
- 关联设计：[技能质量判断工作台设计](./skill-quality-judgment-design.md)、[技能使用结果检查与评审方案](./skill-outcome-review-design.md)

## 验证边界

验证 fixture 位于被 Git 忽略的 `work/skill-real-use-uat/`。subagent 只允许在各自 fixture 目录写入，不修改项目源码、正式质量数据、合同、体验判断、反馈或快照。

每个结论均区分以下三类事实：

- 技能是否被成功加载和执行。
- 技能说明在真实任务中是否存在可复现的设计缺陷。
- Skills Manager 是否具备把调用、结果和缺陷归因成正式质量结论的证据条件。

加载成功不等同技能有效。真实任务发现的缺陷也不直接形成技能失败率或合同 hard failure。

## 场景与结果

| 技能 | 真实任务 | 执行结果 | 发现的技能设计缺陷 | 证据等级 |
| --- | --- | --- | --- | --- |
| `clone-bare-repo-and-use-worktrees` | 从本地 bare remote 建立 `.bare/`、`main/`、feature worktree，并验证 refspec、fetch/prune 和 worktree 隔离 | 通过 | 管理根 `AGENTS.md` 模板只有 PowerShell 日常操作示例，Bash 用户无法直接执行日常工作流 | 已复现 |
| `local-gradle-wrapper` | 读取 wrapper 版本，检查本机 Gradle，执行最小 Java 项目的 `gradle test` | 部分通过 | 技能中单引号 `sed` 示例使用双反斜杠，正常 `distributionUrl` 不会匹配，版本提取为空 | 已复现 |
| `multi-agent-deliberation` | 对两个当前技能版本的质量阻断原因生成三份独立角色记录 | 角色级证据 | 没有强制的审议证据载体、降级成功语义和前后决策基线，无法可靠判断历史多代理审议是否真实产生改进 | 已复现 |

完整命令、退出状态、fixture 约束、技能版本 SHA 和审议报告哈希见 [真实使用一手证据](./skill-real-use-evidence-2026-08-28.md)。

## 可复现证据

### clone-bare-repo-and-use-worktrees

验证在本地 bare remote 上完成：默认分支 worktree 与 feature worktree 分离，`remote.origin.fetch` 为 `+refs/heads/*:refs/remotes/origin/*`，`git fetch origin --prune` 和 `git worktree list --porcelain` 均成功。

技能初始化步骤为 Bash 和 PowerShell 分开提供，但管理根模板的日常操作块只有 PowerShell。Bash 使用者需要自行转换这些命令，违背模板应可直接指导日常 worktree 操作的目标。

建议：在 `references/root-agents-template.md` 的每个日常操作块中补充 Bash 等价命令，或将命令表按 shell 分列。

### local-gradle-wrapper

最小 Java fixture 的 `gradle/wrapper/gradle-wrapper.properties` 包含：

```text
distributionUrl=https\://services.gradle.org/distributions/gradle-8.12.1-bin.zip
```

技能原始示例：

```bash
sed -n 's/^distributionUrl=.*gradle-\\([0-9][^-]*\\)-\\(bin\\|all\\)\\.zip.*/version=\\1 type=\\2/p' gradle/wrapper/gradle-wrapper.properties
```

执行结果为空。使用单反斜杠表达式后得到：

```text
version=8.12.1 type=bin
```

本机 Gradle `8.12` 与 wrapper `8.12.1` 主版本一致，执行 `gradle test --console=plain` 成功。fixture 没有测试源码，因此 `test` 任务为 `NO-SOURCE`，仅验证了本地 Gradle 路径与 wrapper 元数据解析。

建议：将示例修正为 `sed -n 's/^distributionUrl=.*gradle-\([0-9][^-]*\)-\(bin\|all\)\.zip.*/version=\1 type=\2/p' gradle/wrapper/gradle-wrapper.properties`；补充带 wrapper 脚本和最小测试类的 fixture，覆盖本地 Gradle 与镜像回退两条路径。

### multi-agent-deliberation

主协调器调度严格评审者、最终目标评审者和实用整合者三份独立角色记录。角色记录改变了结论表达和验证范围：将“技能不可发布”收缩为“当前质量口径不可发布”，并拒绝把 shared 调用或部分扫描写成单技能质量结论。

本轮状态为 `role-record-evidence`，不宣称一次完整 `multi-agent-deliberation` 运行成功。最终目标评审者的 runtime 未暴露其自身继续启动 subagent 的工具，已在角色记录中标明。三个角色记录仍可作为“技能说明缺少审议运行证据与降级语义”的一手发现。

缺陷不在于角色未运行，而在于技能没有规定可持久化的最小审议证据。历史调用只留下加载和 shared 归因，无法验证参与者、独立发现、采纳或拒绝、决策前后变化、验证链接和降级状态。

建议：在技能目录新增 `templates/deliberation-record.md`，并规定任务或调用标识、参与者 runtime、角色、独立发现、决策前后、采纳或拒绝及理由、验证证据和降级状态为必填字段；明确本地多角色检查不等同真实多代理成功。

## 与质量系统的关系

本轮真实使用不会自动形成正式技能质量结论，原因如下：

- 当前调用的 Case 缺少已发布合同、current 产物、current 检查和可评审 assessment。
- configured-catalog 全目录扫描仍为 partial，定向补录只证明指定会话被正确解析，不能替代完整范围。
- 多项调用为 shared 参与关系，不能进入单技能 direct 分母或归责。

Skills Manager 仍应保存“调用事实”和“验证发现”两条独立证据链。技能维护者可根据本记录在技能库中建立独立决策记录和修复提交；修复后的技能应再以同一 fixture 回归验证。

## 后续验证门槛

1. 每个已修复技能必须在相同 fixture 上重新成功执行。
2. 修复记录必须链接原始缺陷、修改版本和验证命令。
3. Skills Manager 只有在完整 configured-catalog 覆盖、精确版本、合同和 direct 证据满足时，才能将这类验证发现汇总为正式质量结论。