# Local Gradle Wrapper 真实 UAT 证据

## 结论

**仅部分通过。** 本机 Gradle 可执行且 `test` 命令退出成功，但 fixture 没有 `src/` 目录，所有编译与测试任务均为 `NO-SOURCE`。因此，本次只能证明本地 Gradle 的构建流程可以运行，**不能证明任何测试代码已经执行或通过**。

## 任务输入摘要

- 日期：2026-08-28
- 目标：对现有最小 Gradle fixture 重跑当前启用的 `local-gradle-wrapper` 技能流程。
- fixture：`work/skill-real-use-uat/gradle/`
- 写入约束：只允许写入本文档以及 fixture 内的 Gradle 构建缓存。
- 禁止变更：项目源码、`data`、`gradle/wrapper/gradle-wrapper.properties`。
- 当前启用技能：`/home/jhihjian/.codex/skills/local-gradle-wrapper/SKILL.md`
- 技能 SHA256：`3685ea547cb300d8a416d4b2e220efad229efd449f1c3a59cfa8d55b5fb2cc37`

## 执行角色

- subagent 角色名：`worker`
- 复核方式：`worker` 在受限目录中执行完整流程；主执行器随后独立重跑四条验证命令并核验 fixture 配置文件哈希。

## 执行记录

所有命令的工作目录均为 `work/skill-real-use-uat/gradle/`。

| 步骤 | 执行命令 | 退出状态 | 关键非敏感输出 |
| --- | --- | ---: | --- |
| 原技能的 wrapper 版本提取 | `sed -n 's/^distributionUrl=.*gradle-\\([0-9][^-]*\\)-\\(bin\\|all\\)\\.zip.*/version=\\1 type=\\2/p' gradle/wrapper/gradle-wrapper.properties` | 0 | 无输出。未提取到 wrapper 版本。 |
| 本机 Gradle 命令检查 | `command -v gradle` | 0 | 已发现本机 `gradle` 命令。按技能约束，不记录本机 Gradle 安装绝对路径。 |
| 本机 Gradle 版本检查 | `gradle --version` | 0 | `Gradle 8.12`；运行时 Java 为 21。 |
| 本地 Gradle 测试 | `gradle --gradle-user-home /data/dev/codex-skills-manager/work/skill-real-use-uat/gradle/.gradle-user-home test` | 0 | `BUILD SUCCESSFUL in 451ms`；`compileJava`、`compileTestJava`、`test` 均为 `NO-SOURCE`。 |

wrapper 配置中的 distribution URL 声明版本为 `8.12.1`，本机 Gradle 为 `8.12`。两者处于相同主版本系列，且本次构建未显示由版本差异引起的失败。

## Fixture 限制与完整性

- fixture 只有 `java` 插件、`rootProject.name` 和 wrapper distribution URL；不存在 `src/` 目录，也没有可编译源码或可执行测试。
- 因 `src/` 缺失，Gradle 报告 `compileJava NO-SOURCE`、`compileTestJava NO-SOURCE` 与 `test NO-SOURCE`。即使命令退出状态为 0，也必须标记为**仅部分通过**。
- 测试使用显式 `--gradle-user-home`，构建缓存写入仅位于 `work/skill-real-use-uat/gradle/.gradle/` 和 `work/skill-real-use-uat/gradle/.gradle-user-home/`。
- 测试前后核验的 fixture 配置文件哈希保持不变：

| 文件 | SHA256 |
| --- | --- |
| `build.gradle` | `90483255e6489e7bd9b4f6f014187a900d3315325f1b8820aabd2965dd1e6d12` |
| `settings.gradle` | `80eb0015a4556d9c9f3f49075729940f6dcd0fd0fc6c0c475c2d5bfd81dbdaf5` |
| `gradle/wrapper/gradle-wrapper.properties` | `4d656a075feb559c85bf9a8eed3f51e380be0527ef9a2a01dc5fe96e98c5cc42` |

## 技能设计缺陷与修正命令

原技能中的 wrapper 版本提取命令在单引号包裹的 sed 正则中使用了双反斜杠。shell 不会在单引号内折叠反斜杠，导致 sed 不匹配 URL；命令仍以状态 0 退出，调用者却拿不到版本值。这使后续的本机 Gradle 兼容性判断缺少输入。

应将技能中的提取和空结果校验替换为以下命令：

```bash
wrapper_version="$(sed -n 's/^distributionUrl=.*gradle-\([0-9][^-]*\)-\(bin\|all\)\.zip.*/version=\1 type=\2/p' gradle/wrapper/gradle-wrapper.properties)"
test -n "$wrapper_version" || { printf '%s\n' '无法从 gradle-wrapper.properties 提取 wrapper 版本' >&2; exit 1; }
printf '%s\n' "$wrapper_version"
```

此外，若将 `gradle test` 用作 UAT 的测试通过证据，UAT 脚本应在标准 Java fixture 中加入以下前置校验，并把校验失败或 Gradle 输出 `NO-SOURCE` 归类为部分通过，而不是完整通过：

```bash
test -d src/test && find src/test -type f \( -name '*.java' -o -name '*.groovy' -o -name '*.kt' \) -print -quit | grep -q . || {
  printf '%s\n' '不存在可执行的标准 JVM 测试源码，不能将 gradle test 判定为完整通过' >&2
  exit 2
}
```

上述命令仅记录建议，未修改技能或 fixture。