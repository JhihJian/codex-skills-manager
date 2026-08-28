# Clone Bare Worktree 真实 UAT 重跑证据

## 任务输入摘要

- 日期：2026-08-28
- 目标：使用现有本地 remote `work/skill-real-use-uat/clone/remote.git`，按当前启用的 `clone-bare-repo-and-use-worktrees` 技能重建裸仓库和 worktree 管理根，并验证默认分支、fetch refspec、`fetch --prune`、`main` 与 feature 分离及清洁状态。
- 写入边界：仅 `docs/uat-evidence/clone-bare-worktree-2026-08-28.md` 与 `work/skill-real-use-uat/clone/revalidation/`。项目源码、`data/` 和 `work/skill-real-use-uat/clone/remote.git` 均只读。
- subagent：角色名 `worker`。其执行日志位于 `work/skill-real-use-uat/clone/revalidation/manager/worker-execution.log`。

## 技能版本与输入 fixture

| 项目 | 证据 |
| --- | --- |
| 已读取技能 | `/home/jhihjian/.codex/skills/clone-bare-repo-and-use-worktrees/SKILL.md` |
| SHA-256 | `b943bfe8ce9e2acc66f834eb1a060035af47887ea19f1fb7d78aed6db1a75692` |
| 按需读取参考 | `references/setup-commands.md`、`references/root-agents-template.md` |
| fixture remote | `work/skill-real-use-uat/clone/remote.git` |
| 重跑管理根 | `work/skill-real-use-uat/clone/revalidation/manager` |
| fixture 初始及最终 HEAD | `a749718d44ee96ddfae2971b3a06ce25fd2d9c70` (`refs/heads/main`) |

## 执行记录

除特别标注外，以下命令均由 `worker` 在管理根建立期间执行，退出状态为命令进程的实际退出码。命令中使用的绝对路径均指向上述受限的 `revalidation/` 目录或只读 fixture。

| # | 执行命令 | 退出状态 | 关键非敏感输出 |
| --- | --- | --- | --- |
| 1 | `git ls-remote --symref <remote.git> HEAD` | `0` | `ref: refs/heads/main HEAD`；HEAD 为 `a749718...` |
| 2 | `mkdir -p <revalidation>/manager` | `0` | 无输出 |
| 3 | `git clone --bare --single-branch --branch main <remote.git> <manager>/.bare` | `0` | `Cloning into bare repository ... done.` |
| 4 | `printf 'gitdir: ./.bare\n' > <manager>/.git` | `0` | 写入管理根 Git 指针 |
| 5 | `git -C <manager> config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'` | `0` | 无输出 |
| 6 | `git -C <manager> fetch origin --prune` | `0` | `* [new branch] main -> origin/main` |
| 7 | `git -C <manager> symbolic-ref --short HEAD` | `0` | `main` |
| 8 | `git -C <manager> branch --set-upstream-to=origin/main main` | `0` | `branch 'main' set up to track 'origin/main'.` |
| 9 | `git -C <manager> worktree add <manager>/main main` | `0` | `Preparing worktree`；`HEAD is now at a749718 initial` |
| 10 | `mkdir -p <manager>/feature <manager>/fix <manager>/review` | `0` | 无输出 |
| 11 | `git -C <manager> fetch origin --prune` | `0` | 无输出 |
| 12 | `git -C <manager> worktree add -b feature/uat-revalidation <manager>/feature/uat-revalidation main` | `0` | 新分支 `feature/uat-revalidation`；HEAD 为 `a749718...` |
| 13 | `git --git-dir=<manager>/.bare for-each-ref --format='%(refname):%(objectname)' refs/heads refs/remotes/origin`，第一次以未加 shell 引号的 format 参数调用 | `2` | Bash 报 `syntax error near unexpected token '('`；Git 未执行，仓库状态未改变 |
| 14 | 同第 13 条，但将 format 参数正确作为单个参数传递 | `0` | 本地 `refs/heads/main`、`refs/heads/feature/uat-revalidation`；远程跟踪 `refs/remotes/origin/main` |
| 15 | `git -C <manager> worktree list --porcelain` | `0` | bare、`main`、`feature/uat-revalidation` 三个 worktree，后两者各自绑定不同 branch |
| 16 | `git -C <manager>/main status --porcelain=v1 --branch` | `0` | 仅 `## main...origin/main` |
| 17 | `git -C <manager>/feature/uat-revalidation status --porcelain=v1 --branch` | `0` | 仅 `## feature/uat-revalidation` |
| 18 | `git -C <manager>/main diff --exit-code` | `0` | 无输出 |
| 19 | `git -C <manager>/feature/uat-revalidation diff --exit-code` | `0` | 无输出 |
| 20 | `perl -ne 'BEGIN { $count = 0 } $inside = 1 if /^## Rules$/; $inside = 0 if /^## Repository Instructions$/; $count++ if $inside && /^```bash$/; END { print "bash_fenced_blocks_in_daily_rules=$count\n"; exit($count == 0 ? 0 : 1); }' <root-agents-template.md>` | `0` | `bash_fenced_blocks_in_daily_rules=0` |
| 21 | `file -bi <manager>/worker-execution.log` | `0` | `text/plain; charset=utf-8` |
| 22 | `git -C <manager> rev-parse --git-dir --is-bare-repository` | `0` | `<manager>/.bare`；`true` |

本次独立复核另执行了以下命令，均退出 `0`：

```bash
git ls-remote --symref work/skill-real-use-uat/clone/remote.git HEAD
git -C work/skill-real-use-uat/clone/revalidation/manager config --get-all remote.origin.fetch
git -C work/skill-real-use-uat/clone/revalidation/manager fetch origin --prune
git -C work/skill-real-use-uat/clone/revalidation/manager fsck --no-dangling
git -C work/skill-real-use-uat/clone/revalidation/manager worktree list --porcelain
git -C work/skill-real-use-uat/clone/revalidation/manager/main status --porcelain
git -C work/skill-real-use-uat/clone/revalidation/manager/feature/uat-revalidation status --porcelain
git --git-dir=work/skill-real-use-uat/clone/remote.git show-ref --head
```

复核关键输出：fetch refspec 精确为 `+refs/heads/*:refs/remotes/origin/*`；remote 的 `HEAD` 与 `refs/heads/main` 均为 `a749718d44ee96ddfae2971b3a06ce25fd2d9c70`；两个 `status --porcelain` 均为空；`fsck --no-dangling` 无输出且成功。

## 验证结论

1. 默认分支正确。`git ls-remote --symref` 和裸仓库 `HEAD` 都指向 `main`。
2. fetch 隔离正确。初始化时使用 `--single-branch --branch main`，随后 refspec 被精确改为 `+refs/heads/*:refs/remotes/origin/*`，`fetch origin --prune` 两次初始化执行和一次独立复核均成功。远程分支位于 `refs/remotes/origin/main`，没有被 fetch 直接写入本地 branch ref。
3. worktree 隔离正确。`main/` checkout `main` 并跟踪 `origin/main`；`feature/uat-revalidation/` checkout 独立的 `feature/uat-revalidation`。两者路径、branch ref 和工作目录均独立。
4. 清洁状态正确。`main` 和 feature worktree 的 porcelain 状态为空，且各自 `git diff --exit-code` 成功。
5. fixture 未被修改。重跑前后 remote 的 `HEAD` 与 `refs/heads/main` 都是 `a749718d44ee96ddfae2971b3a06ce25fd2d9c70`。流程未执行 push、remote config 写入或 remote ref update。

## Fixture 限制

- fixture remote 只有 `main`，没有可继续的远程 feature 分支。因此本次验证了新建本地 feature worktree，但不能覆盖“从 `origin/feature/<task>` 建立带 upstream worktree”的路径。
- 为保持 remote fixture 只读，未人为创建或删除远程分支。因此 `fetch --prune` 的命令、成功退出状态和 refspec 隔离已验证，但未覆盖删除过期远程跟踪引用的行为。
- fixture 只有初始提交，故主分支和 feature 分支都指向相同 commit；隔离依据是不同 checkout 路径和不同 branch ref，而不是提交内容差异。
- 第 13 条失败是 worker 日志包装命令的 shell 引号问题，不是技能流程或 Git 的失败。该命令在 Bash 解析阶段终止，已在第 14 条纠正并成功重跑。

## 技能设计缺陷与复现依据

当前技能要求在创建管理根时读取并使用 `references/root-agents-template.md`，但该模板的 `## Rules` 日常操作部分只提供 PowerShell fenced code block。第 20 条命令在当前已哈希的技能版本上输出 `bash_fenced_blocks_in_daily_rules=0`，退出状态 `0` 正是对“缺少 Bash 块”的断言成功；同一段共有六个 `powershell` fenced block。

这使 Bash 使用者不能直接从管理根说明执行“创建 feature/fix worktree、继续远程分支、创建 detached review、检查与删除 worktree”等日常操作，必须自行翻译路径和命令。该缺陷不影响本次 Bash 初始化流程，因为 `setup-commands.md` 提供了 Bash 版本，但会降低后续管理根指南的可执行性。复现依据是第 20 条精确命令、当前技能 SHA-256，以及生成的管理根 [AGENTS.md](../../work/skill-real-use-uat/clone/revalidation/manager/AGENTS.md)。

## 可复核命令

```bash
root=work/skill-real-use-uat/clone/revalidation/manager
git ls-remote --symref work/skill-real-use-uat/clone/remote.git HEAD
git -C "$root" config --get-all remote.origin.fetch
git -C "$root" fetch origin --prune
git -C "$root" worktree list --porcelain
git -C "$root" branch -a
git -C "$root/main" status --porcelain
git -C "$root/feature/uat-revalidation" status --porcelain
git --git-dir="$root/.bare" for-each-ref --format='%(refname):%(objectname)' refs/heads refs/remotes/origin
git --git-dir=work/skill-real-use-uat/clone/remote.git show-ref --head
```