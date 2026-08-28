# 技能真实使用一手证据

## 记录范围

- 日期：2026-08-28
- 执行位置：被 Git 忽略的 `work/skill-real-use-uat/`
- 写入边界：clone、Gradle、审议三个隔离 fixture；未改项目源码、`data/`、合同、质量判断、反馈或快照。
- 参与方式：三个独立 subagent 分别执行 worktree、Gradle 和多角色审议任务。审议汇总由独立角色记录为受限范围内的只读结论。

| 场景 | 技能 SHA-256 | subagent 任务 | 结果 |
| --- | --- | --- | --- |
| bare worktree | `b943bfe8ce9e2acc66f834eb1a060035af47887ea19f1fb7d78aed6db1a75692` | clone fixture | 通过 |
| local Gradle | `3685ea547cb300d8a416d4b2e220efad229efd449f1c3a59cfa8d55b5fb2cc37` | Gradle fixture | 部分通过 |
| 多角色审议 | `6b428139ee11413271ef88cde9f4d53403708344dc14f4e2b403ee469cda4daa` | deliberation fixture | 角色级证据 |

## bare worktree

Fixture remote：`work/skill-real-use-uat/clone/remote.git`。subagent 按技能建立 `agent/.bare/`、`agent/main/` 和 `agent/feature/uat-isolation/`。

验证命令及结果：

```text
git config --get remote.origin.fetch
+refs/heads/*:refs/remotes/origin/*

git worktree list --porcelain
bare: agent/.bare
main: agent/main @ main
feature: agent/feature/uat-isolation @ feature/uat-isolation

git -C main status --porcelain
<empty>
git -C feature/uat-isolation status --porcelain
<empty>
```

运行 `git fetch origin --prune` 成功。fixture 只有 `main`，未覆盖远程 feature continuation 与 prune 删除过期引用。

## local Gradle

Fixture wrapper 配置：

```text
distributionUrl=https\://services.gradle.org/distributions/gradle-8.12.1-bin.zip
```

原技能示例的命令结果：

```text
sed 原示例输出行数: 0
```

修正表达式：

```bash
sed -n 's/^distributionUrl=.*gradle-\([0-9][^-]*\)-\(bin\|all\)\.zip.*/version=\1 type=\2/p' gradle/wrapper/gradle-wrapper.properties
```

结果：

```text
version=8.12.1 type=bin
Gradle 8.12
GRADLE_USER_HOME=<fixture>/.gradle-user-home gradle test --console=plain
exit=0
compileJava NO-SOURCE
compileTestJava NO-SOURCE
test NO-SOURCE
BUILD SUCCESSFUL
```

该 fixture 未提供 `gradlew` 与测试源码，因而未验证 wrapper 下载、镜像回退或实际断言执行。

## 多角色审议

主协调器通过独立 subagent 调度生成三份角色记录：严格评审者、最终目标评审者、实用整合者。任务是解释 `local-gradle-wrapper` 与 `multi-agent-deliberation` 为什么不能发布质量结论。此组记录的状态为 `role-record-evidence`，不宣称一次完整 `multi-agent-deliberation` 运行成功。最终目标评审者的 runtime 未暴露其自身继续启动 subagent 的工具，该限制已写入其原始记录。每个角色的原始记录独立保存：

```text
严格评审者：docs/uat-evidence/multi-agent-strict-reviewer-2026-08-28.md
632d2a01f0f595cc3f644f996cf589bb3ae0ef7ab6fdd9363a77584f2756e69a

最终目标评审者：docs/uat-evidence/multi-agent-goal-reviewer-2026-08-28.md
0e115921d6d4c3859474eb243de39619fad752a98c239dcab27c66c5229163e5

实用整合者：docs/uat-evidence/multi-agent-practical-integrator-2026-08-28.md
70a6237825cf2b0676cb5709647fbaf7f847c28312de6d9e75f247b97d95d857
```

角色记录改变了结论和验证范围：

1. 将“不可发布”限定为质量系统正式口径，不解释为技能无效。
2. 明确 shared 归因、partial configured-catalog、空合同、空检查/产物和派生未追平都是独立阻断条件。
3. 拒绝为补齐 UAT 证据而创建质量 judgment、合同、scan、snapshot 或反馈数据。

受限只读范围内，实用整合角色无法执行 SQLite 查询。该环境限制未用于任何独立数据结论，最终结论仅使用严格评审者与最终目标评审者的交叉结果及主验证。

## 证据使用规则

本记录证明技能说明在真实任务中的可复现行为与设计缺陷。它不替代 Skills Manager 的合同、direct 归因、完整 configured-catalog 覆盖、检查或正式快照，因此不能单独产生技能通过率、失败率或因果价值结论。