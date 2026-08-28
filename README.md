# Codex Skills Manager

这是一个本地优先的 Codex 技能管理页面，用于管理当前 Windows 设备上的 Codex skills。

## 功能

- 通过本地 Codex 自带的 `skill-installer` 脚本安装 GitHub tree URL 或 `SKILL.md` 文件 URL 中的 skill。
- 将技能安装到独立 skills Git 仓库根目录下的 `skills/`，再按需复制到 `C:\Users\user\.codex\skills` 启用。
- 点击同步时扫描 `C:\Users\user\.codex\skills`，发现不在独立 skills 仓库中的额外技能会自动复制到 `skills/` 并登记为“本机纳管”；普通页面加载和 `GET /api/state` 只读取状态，不执行纳管写入。同步和安装完成后，未填写分类的技能会自动调用本机 `codex exec` 识别分类。
- 在独立 skills 仓库根目录的 `codex-skills-manager.sqlite3` 记录技能来源、分类、标签、依赖、启用状态、人工确认状态、启停记录、备注和同步时间。
- 主界面默认展示“待确认”队列，即已启用、非系统且尚未确认或内容已变化的技能；“已确认”表示用户认可当前 `SKILL.md` 内容，不再需要持续分析是否启用。
- 顶部“自动分类”按钮会对当前未分类技能补跑识别；按住 Shift 点击该按钮可以强制重分已有分类。
- 顶部“中文信息”按钮会为英文或中英混合的 skill 生成中文名称和中文触发条件，并保存到 registry 的本地化元数据；左侧可以在“中文/原文”视图间切换，详情页“中文”页签支持查看、生成和手动修正。该功能不修改原始 `SKILL.md`。
- 安装或同步后会为 `SKILL.md` 自动生成完整中文阅读视图；详情页“中文原文”页签可查看或重新生成。译文只保存在管理器本地缓存，绝不会写入 skills 仓库、`.codex/skills` 或 Codex 实际加载的 skill。
- 管理台可以展示技能使用频率。该功能已模块化为独立统计服务，可在“设置”页单独开启或关闭；开启后服务运行期间每天定时扫描一次本机 Codex 与 Pi 会话，结果缓存到 `data/usage-stats.json`。顶部“使用统计”按钮可手动刷新。
- 技能列表会展示检测到加载的次数；详情页“使用记录”页签按需读取最近加载证据，显示 Codex/Pi 来源、会话标题、发生时间、日志位置和调用片段。
- 管理台会自动计算当前已启用 skills 的惰性加载 token：顶部“预注入 token”只统计启动时注入的技能索引，详情页“来源”中同时显示索引 token 和触发后按需加载的完整 `SKILL.md` token。统计优先使用本机 `tiktoken` 的 `o200k_base`/`cl100k_base` 编码；未安装时使用中英文 Unicode 估算。
- 用户点击“检索上下文”后读取本机 Codex 与 Pi 会话 JSONL，快速查看某个 skill 在会话中的上下文片段，用于判断技能是否有效；切换到上下文页不会自动扫描会话。检索结果会标明来源，优先展示按来源、事件和正文去重后的用户/助手正文，并过滤工具调用、函数调用输出、DOM 快照、浏览器自动化日志和长 JSON 工具输出等低价值片段。
- 顶部“技能审查”入口会打开结果评审工作台。工作台增量索引 Codex 与 Pi 会话，分别展示技能加载、任务结果和人工裁决；`SKILL.md` 读取结果与 Pi `/skill:name` 展开形成加载证据，加载事实不解释为任务成功或技能产生价值。
- 详情页“版本”页签会从 Git 提交记录展示当前 skill 的历史版本、提交时间、作者、提交说明和涉及文件的增删统计；如果仓库还没有提交记录，会在首次提交后开始展示。
- 详情页“来源”页签可以按 GitHub 仓库聚合展示从哪些仓库安装了哪些 skills，并按需检查远端 `SKILL.md` 是否有更新；发现差异时可直接查看本地版本与 GitHub 当前版本的 diff。
- 服务运行期间会监测独立 skills 仓库中的 `skills/` 和 `codex-skills-manager.sqlite3`。检测到受管技能变更后，如果 1 小时内没有继续变化，就自动执行一次限定路径的 Git 提交，提交信息会列出涉及的 skill 和文件数量；配置了 GitHub remote 时会尝试 push。
- 管理台默认折叠安装面板，桌面端保留三栏工作台，移动端优先展示技能列表和详情；图标按钮使用一致的线性图标和可访问名称，`SKILL.md` 预览会渲染 Markdown，默认展开完整文档并可收起为摘要。
- 切换工作队列、搜索、筛选、切换分类、使用状态筛选或排序后，如果当前详情不在可见结果中，页面会自动选中第一条可见技能；没有可见结果时会清空详情并展示空态。
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

页面顶部安装面板只需要填写一个 GitHub tree URL，或指向 `SKILL.md` 的 GitHub blob URL。系统会从 URL 自动识别仓库、分支和 repo 内路径，不再需要单独填写“路径”或“ref”。例如：

```text
https://github.com/iOfficeAI/OfficeCLI/tree/main/skills

https://github.com/LiamGvchi/gc-minimal-zine-poster/blob/main/SKILL.md
```

URL 可以指向单个 skill、单个 `SKILL.md` 文件或父目录；指向父目录时会批量安装其下所有直接包含 `SKILL.md` 的技能子目录。根目录的 `SKILL.md` 会以仓库名作为默认技能名称。

父目录安装会优先通过 GitHub API 识别技能目录；当公共 API 配额耗尽并返回 403 时，会自动改用 GitHub 源码归档扫描，不需要填写令牌或等待配额恢复。

安装过程会先执行 `codex --version` 确认本地 Codex 可用，再调用：

```text
C:\Users\user\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py
```

安装完成后，页面会登记来源信息。安装只写入独立 skills 仓库的 `skills/` 目录，不会自动启用；启用普通技能前需要二次确认，确认后才会复制到 `C:\Users\user\.codex\skills` 并记录启用时间。新增或启用后的技能需要重启 Codex 会话后才会进入新的技能列表。

## 数据目录

- `<skills-repo>/skills/`：独立 Git 仓库中的技能目录。
- `<skills-repo>/codex-skills-manager.sqlite3`：技能来源、分类、依赖、启用状态、人工确认记录、启停记录、中文信息和备注等管理数据。
- `data/usage-stats.json`：每日生成的技能使用频率统计缓存。
- `data/chinese-skill-views.sqlite3`：完整中文阅读视图缓存，不进入 Git，也不属于 skill 文件。
- `data/audit-log.jsonl`：安装、启用、停用、自动纳管、资料编辑的审计记录。
- `public/`：管理页面前端。
- `app.py`：本地 API 与静态文件服务器。
- `usage_stats.py`：技能使用频率分析、缓存、设置和调度服务。
- `session_logs.py`：Codex 与 Pi 会话 JSONL 枚举、正文抽取和低价值内容过滤等共享逻辑。
- `test_usage_stats.py`、`test_session_logs.py`：双来源统计、证据去重、Pi 工具调用和上下文检索测试。
- `docs/skill-outcome-review-design.md`：技能使用结果检查、证据合同、自动评审和人工裁决的完整设计方案。

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

顶部“来源”入口会直接打开详情页“来源”页签。页面保留当前技能的来源详情，并提供“已安装的 GitHub 来源”二级列表：第一级是 GitHub 仓库，第二级是该仓库安装到当前项目库的 skills。点击“检查远端更新”后，服务只读扫描 registry 中 `source.type = github` 的技能来源，按 `owner/repo + ref` 聚合展示：

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

`GET /api/state` 用于只读状态展示，会扫描独立 skills 仓库和当前 Codex skills 目录生成页面数据，但不会复制目录、写入 SQLite 或追加审计日志。状态数据会返回每个技能的 `lifecycle`，包含最近启用时间、最近停用时间、最近启停动作和未启用技能的停用时长；同时返回派生的 `confirmation` 状态。旧版本留下的 `data/audit-log.jsonl` 启停审计也会用于补齐最近启停时间。需要自动纳管 `.codex/skills` 中额外技能时，点击页面顶部同步按钮或调用 `POST /api/sync`；该操作会把额外技能复制到 `<skills-repo>/skills/` 并记录审计日志，并在同步后尝试为仍是“未分类”的技能自动分类。

`GET /api/health` 是轻量健康检查接口，只返回项目路径、skills 仓库是否存在、SQLite 是否可打开和后台任务状态，不扫描会话记录，也不触发同步、分类、本地化或 Git 提交。启动脚本和 smoke 测试都使用该接口确认服务可用。

## 技能确认状态

人工确认与启用状态相互独立。启用表示技能位于 Codex 的加载目录，确认表示用户已经评估并认可当前 `SKILL.md` 内容。确认或撤销确认都不会复制、删除或修改技能文件，也不会自动启用或停用技能。

主界面顶部按工作队列组织技能：

- “待确认”是默认队列，只包含已启用、非系统、文件可用且尚未确认或需要重新确认的技能。
- “已确认”展示当前内容仍与确认时一致的技能，包括之后被停用但确认结论仍有效的技能。
- “已启用”和“全部”用于跳出确认流程进行常规库存管理；左侧仍可叠加项目库、系统、分类、使用状态和排序条件。
- 确认后当前技能会退出待确认队列，页面自动展示下一项；需要恢复时可在“已确认”队列中打开该技能并撤销确认。

确认记录保存确认时间和当时 `SKILL.md` 的 SHA-256 指纹。文件内容变化后，`GET /api/state` 会把状态派生为 `needs-review`，界面显示“需重新确认”并重新纳入待确认队列；分类、标签、备注、中文信息和中文原文缓存变化不会使确认失效。系统技能由 Codex 管理，不进入人工确认队列。

确认和撤销确认分别以 `confirm-skill`、`unconfirm-skill` 写入 `data/audit-log.jsonl`。若审计写入失败，接口会回滚本次确认状态，避免确认记录与审计结果不一致。

可用接口：

- `POST /api/skills/<skill-name>/confirm`：确认当前 `SKILL.md` 内容，接口幂等。
- `POST /api/skills/<skill-name>/unconfirm`：撤销确认，接口幂等。
- `GET /api/state`：每个技能返回 `confirmation.status`，可取 `unconfirmed`、`confirmed`、`needs-review`、`not-applicable` 或 `unavailable`；`stats.pendingConfirmation`、`stats.confirmed` 和 `stats.needsReview` 返回汇总数量。

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

## 中文原文视图

安装完成和手动同步后，管理器会对缺失或已过期的技能全文调用本机 `codex exec`，生成完整中文 Markdown 视图。详情页“中文原文”页签也会按需检查缓存，并在原文变更或缓存缺失时生成；“重新生成”可强制刷新。译文保留 Markdown 结构、YAML frontmatter 键、代码块、命令、路径、URL、变量名和标识符，只翻译面向读者的自然语言。

中文原文视图是管理器的只读辅助内容，缓存仅位于 `data/chinese-skill-views.sqlite3`，不写入 `<skills-repo>/skills/`、`<skills-repo>/codex-skills-manager.sqlite3` 或 `.codex/skills/`。因此它不会被复制到启用目录、不会被 Codex 扫描为 skill、不会进入 `SKILL.md` 预览原文，也不会影响 token 或使用频率统计。缓存以原文 SHA-256 绑定，原文变化时旧译文会标为过期且不会作为当前内容返回。

相关接口：

- `GET /api/skills/<skill-name>/chinese-view`：只读返回当前译文或缺失、过期状态，不生成内容。
- `POST /api/skills/<skill-name>/chinese-view`：生成当前技能的中文原文视图；请求体可传 `{"force": true}` 强制重生成。

可用环境变量：

- `CODEX_SKILL_AUTO_CHINESE_VIEW=0`：关闭安装和同步后的自动全文生成，详情页生成和接口仍可手动使用。
- `CODEX_SKILL_CHINESE_VIEW_TIMEOUT=360`：单个全文生成超时时间，单位秒。
- `CODEX_SKILL_CHINESE_VIEW_MAX_CHARS=120000`：允许生成完整译文的原文最大字符数。超出限制会明确报错，不生成截断译文。

## Skills Token 占用

`GET /api/state` 会返回 `tokenUsage`，并在每个 skill 上返回 `tokenUsage` 明细。统计范围是当前 `enabled = true` 的技能，包括系统技能。`totalTokens` 是启动时注入的技能索引 token 之和，索引按“名称、描述和文件位置”估算；它不包含完整 `SKILL.md`。`totalLazyTokens` 是这些启用技能在全部被触发时，按需加载完整 `SKILL.md` 的 token 总量，仅用于容量评估。

也可以单独调用 `GET /api/token-usage` 获取同一份统计结果。`scope = enabled-catalog` 表示 `totalTokens` 是预注入索引，`lazyLoadScope = enabled-skill-md` 表示 `totalLazyTokens` 是全部触发后的上限估算。`method` 为 `tiktoken:o200k_base` 或 `tiktoken:cl100k_base` 时是对应 tokenizer 的结果；`estimate:unicode` 表示本地估算值。不同模型使用的 tokenizer 可能不同，因此该数值用于评估技能加载成本，不等同于某一次请求最终的完整 prompt token 数。

## 访问认证

服务首次启动时在 `data/access-token` 生成 256-bit 随机访问密钥，并在 `data/operator.json` 创建稳定的单操作者身份。两个文件的权限均为 `0600`。浏览器使用访问密钥登录后获得仅存于服务内存的 `HttpOnly`、`SameSite=Strict` 会话 cookie；所有写接口同时校验 `X-CSRF-Token`。服务重启会清除已有浏览器会话，长期访问密钥不会写入浏览器存储。

单操作者默认具备 `admin`、`contract-owner` 和 `reviewer` 角色。合同发布必须在合同正文中显式设置 `governance.singleOperatorApproved=true`。业务例外同时要求 reviewer 与 admin 角色，并在指标中单列排除。

认证接口：

- `GET /api/auth/status`：读取当前浏览器会话状态和 CSRF token。
- `POST /api/auth/login`：用 `{"token":"..."}` 换取浏览器会话。
- `POST /api/auth/logout`：注销当前会话，需要有效 CSRF token。

启用 HTTPS 反向代理时设置 `CODEX_SKILL_SECURE_COOKIE=1`。`CODEX_SKILL_OPERATOR_NAME` 可以设置本机操作者显示名称。

## 使用频率统计

主页面在启用后展示最近一次加载统计缓存。统计同时读取 Codex 与 Pi 会话：助手执行的 `SKILL.md` 读取工具调用和 Pi `/skill:name` 命令形成技能加载证据，助手声明“使用某技能”只作为辅助证据。加载次数用于衡量触发频率，涉及会话和使用天数用于控制同一会话内的重复读取。该证据与人工“已确认”状态、任务结果和技能价值分别统计。Pi fork/clone 复制出的同源工具调用会按父会话链和事件 ID 去重，独立会话中的同名事件保持独立；Codex 与 Pi 出现相同会话 ID 时仍按来源区分。

主页面左侧“使用”筛选会复用这份统计缓存，支持查看 3 天、7 天或 15 天未检测到加载的技能；只有助手声明、没有结构化加载证据的技能归入未检测到加载。左侧“排序”支持默认顺序、名称、分类、最近加载和加载次数。

设置页位于：

```text
http://127.0.0.1:8876/settings.html
```

在设置页可以单独配置使用频率分析是否开启、是否每日自动刷新、刷新时间、扫描范围、是否包含系统技能、长期未用阈值和每个来源的最大扫描文件数。页面配置写入 `data/settings.json` 的 `usageStats` 字段。为避免一个来源挤占另一个来源，`maxFiles` 会分别应用于 Codex 和 Pi，然后合并结果。

Pi 会话目录按以下顺序确定：`CODEX_SKILL_PI_SESSIONS_DIR`、`PI_CODING_AGENT_SESSION_DIR`、`~/.pi/agent/settings.json` 中的 `sessionDir`、`~/.pi/agent/sessions`。`PI_CODING_AGENT_DIR` 可以改写 Pi agent 根目录。Pi 命令行临时传入的 `--session-dir` 无法被独立运行的管理器自动发现，需要同时设置 `CODEX_SKILL_PI_SESSIONS_DIR`。旧版只含 Codex 的 version 1 统计缓存会标记为过期，并在启动刷新或手动刷新后升级为双来源 version 2 缓存。

开启“每日自动刷新”后，服务启动时会创建一个本地后台线程，在每天固定时间刷新 `data/usage-stats.json`。如果缓存不存在或超过 25 小时未更新，服务启动时会自动触发一次后台刷新。该调度只在本地管理服务运行期间生效，不会注册 Windows 计划任务。关闭使用频率分析后，主页面不再展示使用次数，手动刷新接口和后台调度都会跳过扫描。

可用接口：

- `GET /api/settings`：读取设置页使用的配置视图。
- `PUT /api/settings`：保存使用统计配置；请求体支持 `usageStats`，字段包括 `enabled`、`dailyEnabled`、`dailyHour`、`dailyMinute`、`staleDays`、`maxFiles`、`scope`、`includeSystem`。
- `GET /api/usage-stats`：读取当前使用统计缓存。
- `POST /api/usage-stats/refresh`：立即刷新使用统计缓存；请求体可覆盖 `staleDays`、`maxFiles`、`scope`、`includeSystem`。
- `GET /api/skills/<skill-name>/usage`：读取单个技能的加载汇总和最近加载证据。
- `GET /api/token-usage`：计算当前已启用 skills 的 `SKILL.md` token 占用。

可用环境变量：

- `CODEX_SKILL_USAGE_STATS_ENABLED=0`：默认关闭使用频率分析；仍可在设置页重新开启。
- `CODEX_SKILL_USAGE_DAILY_ENABLED=0`：关闭服务内每日自动统计；手动刷新接口仍可使用。
- `CODEX_SKILL_USAGE_DAILY_HOUR=3`：每日统计小时，取值 0-23。
- `CODEX_SKILL_USAGE_DAILY_MINUTE=0`：每日统计分钟，取值 0-59。
- `CODEX_SKILL_USAGE_STATS_SCOPE=all`：主页面统计范围，可选 `enabled`、`managed`、`all`。
- `CODEX_SKILL_USAGE_STATS_INCLUDE_SYSTEM=1`：主页面统计是否包含系统技能。
- `CODEX_SKILL_USAGE_STALE_DAYS=30`：默认长期未用阈值，单位天。
- `CODEX_SKILL_USAGE_MAX_FILES=1000`：一次统计中每个来源最多扫描的会话 JSONL 文件数。
- `CODEX_SKILL_PI_SESSIONS_DIR`：显式指定管理器读取的 Pi 会话目录，优先级最高。
- `PI_CODING_AGENT_DIR`：改写 Pi agent 根目录，默认 `~/.pi/agent`。
- `PI_CODING_AGENT_SESSION_DIR`：改写 Pi 会话目录，优先级低于管理器专用变量。

## 技能结果评审

结果评审工作台位于 `http://127.0.0.1:8876/reviews.html`，包含加载检测、任务结果、人工队列、合同、指标和负面反馈六个视图。单案例详情按时间展示用户目标、技能加载、工具调用与结果、产物、确定性检查、assessment revision、反馈信号和人工操作。

相关设计文档：

- [`docs/skill-outcome-review-design.md`](docs/skill-outcome-review-design.md)：技能结果评审总体设计。
- [`docs/session-negative-feedback-design.md`](docs/session-negative-feedback-design.md)：会话负面反馈、过程异常、目标归属和解决闭环设计。
- [`docs/skill-quality-judgment-design.md`](docs/skill-quality-judgment-design.md)：桌面端按精确技能版本判断质量、试用体验和可比较性的改进设计。
- [`docs/skill-real-use-validation.md`](docs/skill-real-use-validation.md)：subagent 在隔离真实任务中验证技能说明并发现设计缺陷的首轮记录。
- [`docs/skill-real-use-evidence-2026-08-28.md`](docs/skill-real-use-evidence-2026-08-28.md)：首轮验证的命令、退出状态、技能版本哈希与审议证据。

### 增量索引与结果模型

`data/skill-effects.sqlite3` 保存规范事件、文件 generation、字节 checkpoint、provenance、Task Episode、Task Case、技能调用、归因、证据、检查、assessment 和人工追加记录。扫描器支持字节与时间预算；无变化文件从 checkpoint 继续，同尺寸替换、截断后增长、移动和删除会重建 generation 并使失去 provenance 的自动派生记录失效。人工裁决和审计记录继续保留。

历史会话只有在日志中保存了精确且完整的 `SKILL.md` 内容时才绑定技能 SHA。Codex 完整读取结果和 Pi skill block 可提供该内容；带 offset/limit 或截断标记的 Codex 局部读取只记录加载成功，版本保持未知，当前工作区文件不会回填历史版本。加载失败、取消、阻止和结果缺失分别记录。技能审查、翻译、迁移和自测标记为维护调用并从业务归因排除；同一 Task Case 的后续技能默认记录为 shared。时间邻近、共享提交和纯文本相似只形成候选关系，不自动合并 Task Case。

### 会话负面反馈

工作台“负面反馈”视图分别展示用户反馈、过程异常和助手线索。确定性检测覆盖结果否定、持续故障、需求遗漏、返工纠正、过程批评，以及工具错误、阻止、超时、代理不可用、首轮计划未执行和部分并行执行。Pi `<skill>` 正文、引用日志、初始约束、取消指令、正向认可和助手自评使用独立来源口径。

每条反馈保存脱敏 Span、机器 revision、目标候选和追加式 Action。高置信且目标明确的候选进入人工队列；用户 follow-up 即使形成新的 Task Case，也可通过上一助手结果和 session graph 指回被评价 Case。人工可领取、确认、标记误报、重新指向、记录修复、请求验证和关闭。后续用户明确认可或完整 Process Plan 重试可以形成已验证解决。

反馈候选不会直接形成任务 hard failure，也不会直接计入技能失败率。正式反馈指标只纳入已确认或达到校准门槛的目标，冻结 scan、cutoff、机器 revision、Action revision、校准 profile 和 direct/shared 归因。覆盖 partial、目标歧义、证据失效和误报记录逐条排除。

detector 升级时，截断的历史消息按 provenance 字节范围从配置会话根定向重解析，并校验 raw line hash、设备、inode、事件 ID 和 fingerprint。任何身份不一致都会保持 `needs-source-reparse`，同时阻止正式反馈快照。反馈快照还会校验当前配置的 scope fingerprint，并冻结 Span parser、目标解析器、语义模型 tuple 和实际 calibration profile。

数据清理使用可恢复的 `cleanupRunId` 记录 materialize、feedback、outcome 和 prospective 阶段。中途失败可使用同一 ID重试，已完成结果和错误阶段保存在 SQLite；近期 Feedback revision/Action 与 shared 多技能 Case 受保留边界保护。

### 技能质量判断

工作台默认进入“技能质量”目录，只展示当前正确启用、非系统、非缺失技能的历史 `valid + business-use + loaded` 调用。目录按历史调用 SHA 分版本展示，并将当前启用副本与历史加载版本明确区分。未启用、缺失、未加载、等待结果、失败加载、维护调用和版本未知调用保留在加载检测与审计中，不进入质量分析。目录与详情支持观察状态、参与关系、任务类型、来源和时间范围筛选，并在 URL 中保留当前范围。来自技能管理详情的入口先按技能名筛选当前启用技能的观察记录。

质量结论与加载、合同结果、试用体验和负面反馈分别保存：加载只证明内容被读取；合同结果仅使用完整 scope 的已封存 `metric_eligible` Case；试用体验由显式 assignment 的 `trial_user` 追加，并通过独立不可变质量快照统计；未确认、shared、版本未知和 Evidence 失效项始终单列。partial 扫描、合同缺失、样本不足、多个统计键或未封存体验不会生成“好用/不好用”结论。

主要接口：

- `GET /api/skill-quality`、`/detail`、`/cases`、`/feedback`、`/compare`：读取版本质量目录、下钻和同口径比较。
- `POST /api/skill-use-judgment-assignments`、`GET /api/skill-use-judgment-assignments/current`：管理并读取试用 assignment。
- `POST /api/skill-use-judgments`、`POST /api/skill-use-judgments/withdraw`：追加或撤销本人的试用判断。
- `POST /api/judgment-feedback-referrals/<id>/decision`：由 reviewer 将显式负面试用转介链接、转换或关闭。
- `POST /api/skill-quality-snapshots`：在 complete scope 和派生游标追平后封存体验统计。

### 合同与结论

Outcome Contract 存入 skills 仓库的 `codex-skills-manager.sqlite3`，按技能 ID、精确 SHA 和评审模式选择。每个技能版本只有一个 active 合同；发布版本不可修改。工作台提供 Gradle 和文档合同模板，也支持编辑自定义草稿。

合同解释器只接受具有明确证明能力的受信证据。确定性 checker 必须完成生命周期，精确匹配合同声明的审批版本，并通过 trust、checker 和 parser 版本约束，且识别至少一个断言。有效断言失败可形成 hard failure；超时、环境错误、解析失败、陈旧证据和普通命令退出码保持 inconclusive。历史产物缺失默认表示观察不完整，不形成任务失败。

人工操作分为 claim、decision、disposition、correction 和 exception。领取操作防止多名 reviewer 并发裁决；decision 只允许用于 assessable assessment，证据不足和不可评审案例使用 disposition；其余操作绑定 actor、reason code 和 expected revision。普通人工 pass 不能覆盖有效 hard failure；证据错误通过 correction 触发重评，业务例外以 exception 记录并排除正常结果率。当前有效结论综合自动评审、最新人工裁决和未过期例外展示，新 assessment revision 默认不继承旧人工裁决。

### 前瞻采集与语义评审

前瞻采集器记录 `before-invocation`、`after-artifacts` 和 `after-check` 三阶段 manifest，包含允许根内产物的 SHA-256、大小、受控文本摘录、Task Case revision、采集器版本和环境指纹，并投影为当前 revision 的 artifact；旧 revision manifest 不能重放。无漂移状态为 current；manifest 漂移只会把同一 observation group 的产物与检查标记为 stale，同时使当前 assessment 重新进入队列。主动检查必须绑定已存储的 `after-artifacts` manifest，精确匹配 Task Case revision、工作区根哈希、环境和 observation group。文档 checker 的审批配置固定为 manifest 内文件存在且可读，调用者不能提交内容断言参数；Gradle manifest 必须覆盖 runner 会读取的全部工作区文件。检查后由服务端自动采集 `after-check` 并决定 freshness；每次新增检查也会使旧 assessment 失效，必须重评。Gradle 在 bubblewrap 中使用系统安装和受信 init script采集 Test suite 数量，但由于项目构建脚本本身是任意代码，结果保守标记为 inconclusive，只供人工核验，不形成自动 pass 或 hard fail；任意命令、符号链接、路径逃逸和未授权调用被拒绝。

语义评审输入由服务端从当前 Task Case、当前 assessment、精确版本合同、检查和当前 revision 产物构造，浏览器不能提交或删减证据。严格输出 schema 要求模型引用已提供的 Evidence ID；自动 pass 至少引用一个经数据库复核的 current assertion-pass，或内容哈希一致的受控产物文本摘录，用户目标和纯哈希元数据只能作为上下文。助手正向自述不进入正向证据，产物中的提示文本按不受信数据处理，hard failure 在调用模型前和保存 revision 前双重优先；模型运行期间 assessment 失效时拒绝保存。合同声明 `semanticReview.required=true` 时，确定性 pass 只进入语义队列。任何自动语义 pass/partial/fail 都需要精确匹配合同、任务类型、来源、模型、prompt 和 rubric 的 calibration profile。profile 的样本数、混淆计数、语料摘要和 Wilson 95% 下界由服务端从 semantic review 与当前人工裁决语料计算，客户端不能自报；门槛为至少 200 个已裁决案例、主要任务类型至少 30 个案例、自动 pass 精确率下界不低于 0.95，未达到时进入人工队列。

### 指标快照

实时指标预览明确标记为非正式。正式 `metric_snapshot` 的截止时间、最新 scan run、覆盖状态和版本元组由服务端确定，客户端不能声明正式口径；只有服务端配置的 Codex/Pi 全目录扫描可成为正式 scan，调用者自选 `sources` 的 ad-hoc 扫描不能封存指标。快照冻结扫描范围指纹，以及每个案例的 Task Case revision、assessment revision、manual decision revision、技能 SHA、合同、任务类型、参与关系、有效结论和排除原因。覆盖不完整、陈旧或未知 freshness、争议、例外、旧 assessment、缺合同和不满足校准门槛的案例不进入结果率分母。报告按固定统计键分组，提供 pass/partial/fail 计数和 95% 置信区间；明确样本少于 20 时展示计数和区间，稳定率显示为空。

主要接口：

- `POST /api/effect-scan`：按预算增量扫描 Codex 与 Pi 会话。
- `GET /api/feedback-signals`、`GET /api/feedback-signals/<id>`：筛选和读取反馈信号、目标及 Action 历史。
- `POST /api/feedback-signals/<id>/claim`、`POST /api/feedback-signals/<id>/actions`：领取并处理反馈。
- `POST /api/feedback-signals/<id>/semantic-classify`：显式运行本地语义复核；校准未达标时保持人工处理。
- `GET /api/feedback-clusters`：读取已确认反馈的重复问题聚类。
- `GET/POST /api/feedback-calibration-profiles`：读取或从人工语料生成校准 profile。
- `GET/POST /api/feedback-metric-snapshots`：读取或封存不可变反馈指标。
- `GET /api/effect-overview`、`GET /api/skill-use-events`：读取覆盖、加载和评审汇总。
- `GET /api/task-cases/<id>`、`POST /api/task-cases/<id>/review`：读取带 Evidence ID、内容哈希和 generation/行号 locator 的案例证据，并生成 assessment revision。
- `POST /api/task-cases/<id>/semantic-review`：使用本机 Codex 执行结构化语义评审并追加 assessment revision。
- `GET /api/review-tasks`、`POST /api/review-tasks/<id>/claim`、`PUT /api/review-tasks/<id>/decision`、`PUT /api/review-tasks/<id>/disposition`：领取并处理人工队列。
- `POST /api/task-cases/<id>/corrections`、`POST /api/task-cases/<id>/exception`：追加纠正和业务例外。
- `GET/POST /api/skills/<name>/outcome-contracts`、`PUT /api/outcome-contracts/<id>`、`POST /api/outcome-contracts/<id>/publish`：管理版本化合同。
- `POST /api/prospective-events`、`POST /api/artifact-manifests`、`POST /api/artifact-manifests/compare`：记录前瞻事件和产物 manifest。
- `POST /api/outcome-checks/run`：显式授权并绑定 `after-artifacts` manifest 后执行受控 checker，服务端自动完成检查后 manifest 和 freshness 比较。
- `POST /api/calibration-profiles`：注册不可变语义校准 profile。
- `GET /api/effect-metrics`、`POST /api/effect-metric-snapshots`：读取预览并创建正式快照。
- `POST /api/effect-data/cleanup`：按时间、项目或技能清理前瞻事件、产物、检查、语义正文、工具输出和可识别路径；人工 reason code 与 revision 审计链保留，备注清空，并在 `secure_delete` 模式下生成哈希审计摘要、截断 WAL 和受控压缩 SQLite 主文件。

`CODEX_SKILL_CHECK_ROOTS` 使用系统路径分隔符配置允许采集和主动检查的项目根目录。未配置时默认允许管理器父目录。主动检查依赖 Linux `bubblewrap`；运行环境不满足时返回 environment mismatch，不形成自动失败。

## 注意事项

系统技能位于 `C:\Users\user\.codex\skills\.system`，页面只展示，不允许停用。停用普通技能前需要二次确认；确认后只会删除 `C:\Users\user\.codex\skills\<skill-name>` 下的启用副本，独立 skills 仓库中的 `skills/<skill-name>` 会保留，并在技能详情和审计日志中留下停用时间。未启用技能会在列表和详情里展示最近停用时间及已经停用多久。

## 验收与回滚

启动后使用本机访问密钥做只读健康检查：

```powershell
$Token = (Get-Content -Raw -Encoding UTF8 .\data\access-token).Trim()
Invoke-RestMethod -Headers @{ Authorization = "Bearer $Token" } http://127.0.0.1:8876/api/health
```

也可以运行 smoke 测试脚本。脚本从 `data/access-token` 读取 Bearer 密钥，只读取首页、`/api/health`、`/api/settings` 和 `/api/state`，不会触发同步、分类、本地化、加载统计刷新或 Git 提交：

```powershell
.\scripts\smoke-test.ps1 -HostName 127.0.0.1 -Port 8876
```

启动脚本会在后台进程创建后等待访问密钥文件并使用 Bearer 轮询 `/api/health`。如果服务未能变为健康状态，会输出 `work/server.err.log` 的末尾内容并返回非零。常用日志位置：

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
