# Codex Skills Manager

这是一个本地优先的 Codex 技能管理页面，用于管理当前 Windows 设备上的 Codex skills。

## 功能

- 通过本地 Codex 自带的 `skill-installer` 脚本安装 GitHub tree URL 中的 skill。
- 将技能安装到独立 skills Git 仓库根目录下的 `skills/`，再按需复制到 `C:\Users\user\.codex\skills` 启用。
- 点击同步时扫描 `C:\Users\user\.codex\skills`，发现不在独立 skills 仓库中的额外技能会自动复制到 `skills/` 并登记为“本机纳管”；普通页面加载和 `GET /api/state` 只读取状态，不执行纳管写入。同步和安装完成后，未填写分类的技能会自动调用本机 `codex exec` 识别分类。
- 在独立 skills 仓库根目录的 `codex-skills-manager.sqlite3` 记录技能来源、分类、标签、依赖、启用状态、启停记录、备注和同步时间。
- 顶部“自动分类”按钮会对当前未分类技能补跑识别；按住 Shift 点击该按钮可以强制重分已有分类。
- 顶部“中文信息”按钮会为英文或中英混合的 skill 生成中文名称和中文触发条件，并保存到 registry 的本地化元数据；左侧可以在“中文/原文”视图间切换，详情页“中文”页签支持查看、生成和手动修正。该功能不修改原始 `SKILL.md`。
- 管理台可以展示技能使用频率。该功能已模块化为独立统计服务，可在“设置”页单独开启或关闭；开启后服务运行期间每天定时扫描一次本机 Codex 会话，结果缓存到 `data/usage-stats.json`。顶部“使用统计”按钮可手动刷新。
- 用户点击“检索上下文”后读取本机 Codex 会话 JSONL，快速查看某个 skill 在会话中的上下文片段，用于判断技能是否有效；切换到上下文页不会自动扫描会话。检索结果优先展示按会话、角色和正文去重后的用户/助手正文，并过滤工具调用、函数调用输出、DOM 快照、浏览器自动化日志和长 JSON 工具输出等低价值片段。
- 顶部“技能审查”入口会打开独立问题审查页，用于扩展多个 skills 常见问题审查项；当前支持按需扫描本机 Codex 会话 JSONL，识别长期未真实触发使用的技能。审查不会把系统注入的技能列表、用户普通提及或上下文关键词命中当作使用；默认只有助手执行过程中的 `SKILL.md` 读取工具调用计为真实使用证据，助手明确使用某技能的声明仅作为辅助证据。
- 详情页“版本”页签会从 Git 提交记录展示当前 skill 的历史版本、提交时间、作者、提交说明和涉及文件的增删统计；如果仓库还没有提交记录，会在首次提交后开始展示。
- 详情页“来源”页签可以按 GitHub 仓库聚合展示从哪些仓库安装了哪些 skills，并按需检查远端 `SKILL.md` 是否有更新；发现差异时可直接查看本地版本与 GitHub 当前版本的 diff。
- 服务运行期间会监测独立 skills 仓库中的 `skills/` 和 `codex-skills-manager.sqlite3`。检测到受管技能变更后，如果 1 小时内没有继续变化，就自动执行一次限定路径的 Git 提交，提交信息会列出涉及的 skill 和文件数量；配置了 GitHub remote 时会尝试 push。
- 管理台默认折叠安装面板，桌面端保留三栏工作台，移动端优先展示技能列表和详情；图标按钮使用一致的线性图标和可访问名称，`SKILL.md` 预览会渲染 Markdown，默认展开完整文档并可收起为摘要。
- 搜索、筛选、切换分类、使用状态筛选或排序后，如果当前详情不在可见结果中，页面会自动选中第一条可见技能；没有可见结果时会清空详情并展示空态。
- 搜索、筛选、分类、使用状态筛选和排序叠加造成空结果时，页面会显示当前条件，并提供清除搜索和重置筛选入口。使用统计开启时，列表可以额外筛选 3 天、7 天或 15 天未使用的技能。

## 启动

推荐使用后台启动脚本：

```powershell
.\scripts\start-server.ps1
```

默认打开：

```text
http://127.0.0.1:8876
```

停止服务：

```powershell
.\scripts\stop-server.ps1
```

也可以直接前台运行：

```powershell
python app.py --host 127.0.0.1 --port 8765
```

打开：

```text
http://127.0.0.1:8765
```

如果 8765 已被其它本地服务占用，可以换端口：

```powershell
python app.py --host 127.0.0.1 --port 8876
```

## 初始化独立 skills 仓库

管理器代码仓库和 skills 仓库是两个独立 Git 项目。skills 仓库根目录约定为：

```text
codex-skills-library/
  skills/
    <skill-name>/
      SKILL.md
  codex-skills-manager.sqlite3
```

默认本地路径是当前项目同级目录：

```text
D:\tmp\codex-skills-library
```

可以通过环境变量指定一个空白 GitHub 仓库地址和本地目录。首次启动时，如果本地目录不存在，服务会 clone 该 GitHub 仓库；如果本地目录已存在但还不是 Git 仓库，服务会执行 `git init` 并设置 `origin`：

```powershell
$env:CODEX_SKILLS_REPO_URL = "https://github.com/<owner>/<empty-skills-repo>.git"
$env:CODEX_SKILLS_REPO_DIR = "D:\tmp\codex-skills-library"
.\scripts\start-server.ps1
```

也可以在页面顶部展开安装面板，在“Skills GitHub 仓库”和“本地仓库路径”中保存配置。页面配置会写入本机 `data/settings.json`，该文件不进入 Git；点击“测试提交”会立即提交并推送当前独立 skills 仓库中的待提交变更。

仓库配置也可以在设置页的“仓库”分组中修改：

```text
http://127.0.0.1:8876/settings.html
```

保存仓库配置前，服务会先校验本地目录。目录不能是磁盘根、用户 Home、`.codex` 目录、当前管理器项目根目录，也不能是已有的非 skills Git 仓库或包含非 skills 仓库文件的目录。验证通过并完成初始化后，才会写入 `data/settings.json`。

历史兼容：如果本项目内已有旧的 `skills-library/` 或 `data/skills-registry.json`，首次初始化外部仓库时会迁移到 `skills/` 和 `codex-skills-manager.sqlite3`。迁移后，本项目内的旧目录不再作为读写位置。

## 安装技能

页面顶部安装面板只需要填写一个 GitHub tree URL。系统会从 URL 自动识别仓库、分支和 repo 内路径，不再需要单独填写“路径”或“ref”。例如：

```text
https://github.com/iOfficeAI/OfficeCLI/tree/main/skills
```

URL 可以指向单个 skill，也可以指向父目录；指向父目录时会批量安装其下所有直接包含 `SKILL.md` 的技能子目录。

父目录安装会优先通过 GitHub API 识别技能目录；当公共 API 配额耗尽并返回 403 时，会自动改用 GitHub 源码归档扫描，不需要填写令牌或等待配额恢复。

安装过程会先执行 `codex --version` 确认本地 Codex 可用，再调用：

```text
C:\Users\user\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py
```

安装完成后，页面会登记来源信息。安装只写入独立 skills 仓库的 `skills/` 目录，不会自动启用；启用普通技能前需要二次确认，确认后才会复制到 `C:\Users\user\.codex\skills` 并记录启用时间。新增或启用后的技能需要重启 Codex 会话后才会进入新的技能列表。

## 数据目录

- `<skills-repo>/skills/`：独立 Git 仓库中的技能目录。
- `<skills-repo>/codex-skills-manager.sqlite3`：技能来源、分类、依赖、启用状态、启停记录、中文信息和备注等管理数据。
- `data/usage-stats.json`：每日生成的技能使用频率统计缓存。
- `data/audit-log.jsonl`：安装、启用、停用、自动纳管、资料编辑的审计记录。
- `public/`：管理页面前端。
- `app.py`：本地 API 与静态文件服务器。
- `usage_stats.py`：技能使用频率分析、缓存、设置和调度服务。
- `session_logs.py`：Codex 会话 JSONL 读取、正文抽取和低价值内容过滤等共享逻辑。

## 技能版本记录

独立 skills 仓库中的 skill 通过 Git 管理版本。管理器不会维护另一套版本数据库，而是直接读取 skills 仓库提交历史：

- 详情页“版本”页签调用 `GET /api/skills/<skill-name>/history`，展示 `skills/<skill-name>` 和 `codex-skills-manager.sqlite3` 的相关提交。
- 如果本地 skills 仓库有未提交变更，“版本”页签会先展示待提交文件列表；点击文件可以查看当前工作区 diff。
- 底部状态栏会显示当前是否存在受管技能待提交变更，以及距离自动提交大约还有多久。
- 自动提交只会在独立 skills 仓库内暂存并提交 `skills/` 和 `codex-skills-manager.sqlite3`，不会把管理器代码、服务日志或其它文件混进技能版本。
- 每次自动提交后会向 `data/audit-log.jsonl` 写入提交号、涉及技能、文件数量和是否包含 registry 更新。

自动提交使用“静默期”策略：服务检测到受管技能变更后开始计时；如果后续文件继续变化，计时会重置。默认静默 1 小时后提交一次。

可用接口：

- `GET /api/versioning`：读取当前版本监测状态。
- `GET /api/skills/<skill-name>/history`：读取单个技能的 Git 版本记录；支持 `?limit=40`。
- `GET /api/diff?path=<repo-relative-path>`：读取受管 skills 仓库中文件的未提交 diff。

## GitHub 来源与远端更新

详情页“来源”页签保留当前技能的来源详情，并额外提供“GitHub 来源”检查。点击“检查更新”后，服务只读扫描 registry 中 `source.type = github` 的技能来源，按 `owner/repo + ref` 聚合展示：

- GitHub 仓库、ref、来源地址。
- 该仓库安装到本地项目库的 skills 列表和 repo 内路径。
- 每个 skill 的远端 `SKILL.md` 内容 SHA 和状态：已同步、有更新、远端缺失或检查失败；仓库分组会展示 GitHub 最近 push 时间。
- 点击技能行会调用远端 diff 接口，展示本地 `<skills-repo>/skills/<skill>/SKILL.md` 与 GitHub 当前 `SKILL.md` 的 unified diff。

这些检查不会写入 SQLite、不会自动更新本地文件，也不会启用技能。私有仓库或更高 GitHub API 配额可以通过 `GITHUB_TOKEN` 或 `GH_TOKEN` 环境变量提供访问令牌。

相关接口：

- `GET /api/sources/github`：按 GitHub 仓库聚合读取技能来源并检查远端状态。
- `GET /api/skills/<skill-name>/remote-diff`：读取单个 GitHub 来源技能的本地/远端 `SKILL.md` diff。

可用环境变量：

- `CODEX_SKILL_VERSIONING_ENABLED=0`：关闭服务内技能版本自动提交；历史页仍可读取已有 Git 提交。
- `CODEX_SKILL_VERSION_COMMIT_DELAY_SECONDS=3600`：检测到变更后的静默期，默认 1 小时，最小 60 秒。
- `CODEX_SKILL_VERSION_SCAN_INTERVAL_SECONDS=300`：服务后台扫描间隔，默认 5 分钟，最小 30 秒。
- `CODEX_SKILL_VERSION_AUTO_PUSH=0`：关闭自动提交后的 `git push`；默认在配置了 origin 时尝试 push。
- `CODEX_SKILLS_REPO_URL`：独立 skills GitHub 仓库地址，推荐配置为空白仓库。
- `CODEX_SKILLS_REPO_DIR`：独立 skills 仓库本地路径。

## 状态与同步

`GET /api/state` 用于只读状态展示，会扫描独立 skills 仓库和当前 Codex skills 目录生成页面数据，但不会复制目录、写入 SQLite 或追加审计日志。状态数据会返回每个技能的 `lifecycle`，包含最近启用时间、最近停用时间、最近启停动作和未启用技能的停用时长；旧版本留下的 `data/audit-log.jsonl` 启停审计也会用于补齐最近启停时间。需要自动纳管 `.codex/skills` 中额外技能时，点击页面顶部同步按钮或调用 `POST /api/sync`；该操作会把额外技能复制到 `<skills-repo>/skills/` 并记录审计日志，并在同步后尝试为仍是“未分类”的技能自动分类。

`GET /api/health` 是轻量健康检查接口，只返回项目路径、skills 仓库是否存在、SQLite 是否可打开和后台任务状态，不扫描会话记录，也不触发同步、分类、本地化或 Git 提交。启动脚本和 smoke 测试都使用该接口确认服务可用。

## 自动分类

自动分类通过本机 Codex CLI 的非交互模式运行：

```text
codex exec --ephemeral --output-schema <schema> --output-last-message <file>
```

分类器只把 skill 名称、描述、frontmatter、当前分类和截断后的 `SKILL.md` 摘要传给 Codex，不会要求 Codex 读取项目文件或修改文件。返回结果会写入 `category`、`tags`、可明确识别的 `dependencies`、`autoClassifiedAt` 和一条 `[自动分类]` 备注。分类失败不会阻断安装或同步，只会在状态栏和 `data/audit-log.jsonl` 中记录失败批次。

可用接口：

- `POST /api/classify`：对未分类技能自动分类；请求体支持 `{"force": true}` 强制重分。
- `POST /api/skills/<skill-name>/classify`：只重分单个技能。

可用环境变量：

- `CODEX_SKILL_AUTO_CLASSIFY=0`：关闭安装和同步后的自动分类；手动接口仍可通过请求体 `{"enabled": true}` 临时启用。
- `CODEX_SKILL_CLASSIFY_TIMEOUT=240`：单批 Codex 分类超时时间，单位秒。
- `CODEX_SKILL_CLASSIFY_BATCH_SIZE=24`：每批传给 Codex 的 skill 数量。
- `CODEX_SKILL_CLASSIFY_PREVIEW_CHARS=1800`：每个 `SKILL.md` 摘要传入的最大字符数。

## 中文信息视图

中文信息视图用于解决英文 skill 名称和触发条件不便快速扫描的问题。同步或安装后，管理器会尝试调用本机 Codex CLI，为缺少中文信息的 skill 生成中文名称和中文触发条件；即使原触发条件已经含中文，只要 skill 名称仍是英文目录名，也会生成中文可读名称：

- `zhName`：中文可读名称，用于列表和详情标题。
- `zhTrigger`：中文触发条件，用于列表说明和详情描述。
- `sourceLanguage`、`notes`、`generatedAt`：生成依据和时间。

这些字段只写入 `<skills-repo>/codex-skills-manager.sqlite3` 的本地化元数据，不会写回 `<skills-repo>/skills/<skill>/SKILL.md`，也不会写回 `C:\Users\user\.codex\skills\<skill>\SKILL.md`。左侧“中文/原文”分段控件只改变管理台展示；启用到 Codex 的 skill 仍然保持原始文件内容。

可用接口：

- `POST /api/localize`：为缺少中文信息的技能批量生成中文名称和中文触发条件；请求体支持 `{"force": true}` 强制重生成，也支持 `{"onlyEnglish": true}` 只处理原文判断为英文或中英混合的技能。
- `POST /api/skills/<skill-name>/localize`：只重生成单个技能。
- `PUT /api/skills/<skill-name>`：请求体可携带 `localized` 字段，用于保存手动修正后的中文信息。

可用环境变量：

- `CODEX_SKILL_AUTO_LOCALIZE=0`：关闭安装和同步后的自动中文信息生成；手动接口仍可通过请求体 `{"enabled": true}` 临时启用。
- `CODEX_SKILL_LOCALIZE_TIMEOUT=240`：单批 Codex 中文信息生成超时时间，单位秒。
- `CODEX_SKILL_LOCALIZE_BATCH_SIZE=24`：每批传给 Codex 的 skill 数量。
- `CODEX_SKILL_LOCALIZE_PREVIEW_CHARS=2200`：每个 `SKILL.md` 摘要传入的最大字符数。

## SKILL.md 预览

详情页的 `SKILL.md` 预览支持标题、列表、引用、代码块、表格、链接和常见行内强调。页面初次加载时会先使用状态接口返回的摘要，再通过 `/api/skills/<skill-name>/markdown` 读取当前技能的完整 `SKILL.md`；用户可以点击“收起预览”切回摘要，避免一次性把所有技能全文塞进主状态接口。

## 使用频率统计

主页面在启用后展示最近一次使用统计缓存。统计口径与“技能审查”一致：只有助手执行过程中的 `SKILL.md` 读取工具调用计为确认使用证据，助手声明“使用某技能”只作为辅助证据。确认使用次数用于衡量触发频率，涉及会话和使用天数用于避免同一会话内重复读取造成误判。

主页面左侧“使用”筛选会复用这份统计缓存，支持查看 3 天、7 天或 15 天未使用的技能；没有确认使用证据或仅有助手声明的技能会被视为未使用，统计关闭时该筛选不可用。左侧“排序”支持默认顺序、名称、分类、最近使用和使用次数，其中最近使用、使用次数依赖使用统计数据。

设置页位于：

```text
http://127.0.0.1:8876/settings.html
```

在设置页可以单独配置使用频率分析是否开启、是否每日自动刷新、刷新时间、扫描范围、是否包含系统技能、长期未用阈值和最大扫描文件数。页面配置写入 `data/settings.json` 的 `usageStats` 字段。

开启“每日自动刷新”后，服务启动时会创建一个本地后台线程，在每天固定时间刷新 `data/usage-stats.json`。如果缓存不存在或超过 25 小时未更新，服务启动时会自动触发一次后台刷新。该调度只在本地管理服务运行期间生效，不会注册 Windows 计划任务。关闭使用频率分析后，主页面不再展示使用次数，手动刷新接口和后台调度都会跳过扫描。

可用接口：

- `GET /api/settings`：读取设置页使用的配置视图。
- `PUT /api/settings`：保存使用统计配置；请求体支持 `usageStats`，字段包括 `enabled`、`dailyEnabled`、`dailyHour`、`dailyMinute`、`staleDays`、`maxFiles`、`scope`、`includeSystem`。
- `GET /api/usage-stats`：读取当前使用统计缓存。
- `POST /api/usage-stats/refresh`：立即刷新使用统计缓存；请求体可覆盖 `staleDays`、`maxFiles`、`scope`、`includeSystem`。

可用环境变量：

- `CODEX_SKILL_USAGE_STATS_ENABLED=0`：默认关闭使用频率分析；仍可在设置页重新开启。
- `CODEX_SKILL_USAGE_DAILY_ENABLED=0`：关闭服务内每日自动统计；手动刷新接口仍可使用。
- `CODEX_SKILL_USAGE_DAILY_HOUR=3`：每日统计小时，取值 0-23。
- `CODEX_SKILL_USAGE_DAILY_MINUTE=0`：每日统计分钟，取值 0-59。
- `CODEX_SKILL_USAGE_STATS_SCOPE=all`：主页面统计范围，可选 `enabled`、`managed`、`all`。
- `CODEX_SKILL_USAGE_STATS_INCLUDE_SYSTEM=1`：主页面统计是否包含系统技能。
- `CODEX_SKILL_USAGE_STALE_DAYS=30`：默认长期未用阈值，单位天。
- `CODEX_SKILL_USAGE_MAX_FILES=1000`：一次统计最多扫描的会话 JSONL 文件数。

## 技能审查

技能审查页位于：

```text
http://127.0.0.1:8876/reviews.html
```

该页面用于集中发现常见的 skills 管理问题，左侧是审查项列表，右侧是当前审查项的参数、进度、证据口径和结果。后续新增其它问题审查时，可以继续增加新的审查项，而不用挤在技能管理主页面顶部。

当前已支持的审查项是“长期未真实触发使用”：

- 点击“开始审查”后才会读取 `C:\Users\user\.codex\sessions` 和 `C:\Users\user\.codex\archived_sessions`。
- 审查运行期间会显示不定进度条和当前扫描提示；后端完成一次只读扫描后再切换为结果汇总。
- 默认审查已启用技能，阈值为 30 天；可以在面板中切换为项目库纳管或全部技能，并选择是否包含系统技能。
- 真实使用证据来自结构化会话事件里的工具调用，例如读取 `C:\Users\user\.codex\skills\<skill>\SKILL.md`、`<skills-repo>\skills\<skill>\SKILL.md` 或插件缓存中的 `skills\<skill>\SKILL.md`。
- 仅在用户消息、developer/system 注入内容、技能列表、普通关键词上下文或工具输出中出现 skill 名称，不会计为真实使用。
- 审查结果分为“未确认使用”“仅有声明”“长期未用”和“近期使用”，并展示命中的会话文件、行号和证据片段。

可用接口：

- `POST /api/reviews/usage`：执行一次只读使用审查；请求体支持 `{"staleDays": 30, "scope": "enabled", "includeSystem": true}`。

可用环境变量：

- `CODEX_SKILL_USAGE_STALE_DAYS=30`：默认长期未用阈值，单位天。
- `CODEX_SKILL_USAGE_MAX_FILES=1000`：一次审查最多扫描的会话 JSONL 文件数。

## 注意事项

系统技能位于 `C:\Users\user\.codex\skills\.system`，页面只展示，不允许停用。停用普通技能前需要二次确认；确认后只会删除 `C:\Users\user\.codex\skills\<skill-name>` 下的启用副本，独立 skills 仓库中的 `skills/<skill-name>` 会保留，并在技能详情和审计日志中留下停用时间。未启用技能会在列表和详情里展示最近停用时间及已经停用多久。

## 验收与回滚

启动后先做只读健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8876/api/health
```

也可以运行 smoke 测试脚本。该脚本只读取首页、`/api/health`、`/api/settings` 和 `/api/state`，不会触发同步、分类、本地化、使用统计刷新或 Git 提交：

```powershell
.\scripts\smoke-test.ps1 -HostName 127.0.0.1 -Port 8876
```

启动脚本会在后台进程创建后轮询 `/api/health`。如果服务未能变为健康状态，会输出 `work/server.err.log` 的末尾内容并返回非零。常用日志位置：

- `work/server.log`：服务标准输出和访问日志。
- `work/server.err.log`：Python 启动、端口绑定和运行异常。
- `work/server.pid`：当前后台服务的 PID、端口、项目根目录和启动时间。
- `data/audit-log.jsonl`：安装、启用、停用、同步、仓库配置和自动提交审计。

升级或大改前建议备份：

- `data/settings.json`
- `data/audit-log.jsonl`
- `data/usage-stats.json`
- 独立 skills 仓库目录，例如 `D:\tmp\codex-skills-library`
- 独立 skills 仓库中的 `codex-skills-manager.sqlite3`

回滚管理器代码时，先停止服务，再切回旧版本并重新启动：

```powershell
.\scripts\stop-server.ps1
git status --short
git switch <old-branch-or-tag>
.\scripts\start-server.ps1
.\scripts\smoke-test.ps1
```

回滚 skills 数据时，优先使用独立 skills 仓库的 Git 历史：

```powershell
cd D:\tmp\codex-skills-library
git status --short
git log --oneline -- skills codex-skills-manager.sqlite3
git revert <commit>
```

如果需要从备份恢复 SQLite 或 settings，先停止服务，再替换对应文件，确认路径仍通过仓库配置校验后重新启动并运行 smoke 测试。
