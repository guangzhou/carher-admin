# REA main@080c0e2：进程监管 / setup·update·uninstall / 安全面 / 其余 provider

源码根：`/Users/Liuguoxian/codes/rea`（下文路径相对此处）。包名 `rea-agents@6.7.0`，skill 版本 43（`src/generatedPackageMetadata.ts:2-8`）。
只读审阅，未 install / build / 运行。「推断」= 没跑过、只从代码推。

## 结论先行（大白话）

1. **子进程所有权不靠 PID**：REA 给每个子进程塞一个随机令牌环境变量 `REA_PROCESS_RUN_ID`，杀之前要同时对上「进程启动时间指纹 + 令牌」，前后各读一次指纹夹住令牌读取，任何一项读不到或对不上就**拒绝杀、保留所有权并报 incomplete**，下次可重试。
2. **`rea setup` 对 Claude Code 只动两处**：`~/.claude.json` 顶层 `mcpServers.rea` 一个键（原文件整份备份成 `~/.claude.json.rea.backup`），以及 `~/.claude/skills/reverse-engineer-anything/` 下 7 个 md。不碰 `~/.claude/settings.json`、`CLAUDE.md`、权限白名单。卸载只删它能证明是自己写的那条 `rea` 和 skill 文件，**备份文件不会被删**。
3. **没有遥测**。网络外发全部是功能性的：install.sh / `rea update` 查 npm/GitHub 最新版、Hopper 安装下载、浏览器 source map 抓取（限已批准 origin）、用户指定的 Frida 远端 / IDA / JEB MCP 端点。`rea update` 只装新版本并校验版本号，**不会自动改 agent 配置**。
4. **frida / adb / 进程捕获没有「总开关」**：工具默认就注册进 MCP 目录，靠 MCP 注解 `destructiveHint=true` 让客户端审批；能不能真跑取决于宿主有没有 frida 模块 / adb 二进制。进程捕获在非 Windows 默认可用，原文明说「不是安全沙箱」。

---

## 1. 进程监管（`src/process/`、`src/windows/`、`native/windows/`）

### 1.1 「这是我起的」的证明链

| 环节 | 机制 | 位置 |
|---|---|---|
| 令牌注入 | 每次 spawn 把 `REA_PROCESS_RUN_ID: options.runId` 合进子进程环境（覆盖宿主同名值） | `src/process/ProviderProcess.ts:251-256` |
| 令牌来源 | 进程捕获用 `randomUUID()` | `src/process/capture/ProcessCaptureLifecycle.ts:1635` |
| 令牌保留名 | 场景环境不允许用户设置 `REA_PROCESS_RUN_ID` | `src/domain/process/processScenario.ts:17`；契约描述 `src/contracts/sessionToolContracts.ts:92` |
| POSIX 进程组 | `detached: true`（独立进程组，pgid=pid），`shell: false` | `src/process/ProviderProcess.ts:286-294`、`:298-313` |
| Linux 启动指纹 | `/proc/<pid>/stat` 的 starttime → `linux-starttime:<n>` | `src/process/ProcessOwnershipObservation.ts:781-800` |
| Linux 令牌读取 | `/proc/<pid>/environ` | `src/process/ProcessOwnershipObservation.ts:986-991` |
| Linux 前置条件 | 必须有 procps 兼容 `ps` 且能列出自身，否则拒绝启动子进程 | `src/process/ProcessOwnershipObservation.ts:965-981` |
| macOS 启动指纹 | Swift 辅助程序读 `KERN_PROC_ALL` 的 `p_starttime`（`sec:usec`） | `bridge/process/ProcessRunTokenReader.swift:57`、`:86-90` |
| macOS 令牌读取 | 同一辅助程序走 `KERN_PROCARGS2` 读环境 | `bridge/process/ProcessRunTokenReader.swift:252` |
| macOS 辅助程序来源 | 运行时用 `/usr/bin/xcrun swiftc` 编译到 `mkdtemp(tmpdir/rea-process-token-)`，TMPDIR 也指进去以便清理 | `src/process/DarwinProcessRunTokenReader.ts:409-410`、`:474-499` |
| 夹逼读取 | 「指纹→令牌→指纹」，两次指纹一致才把令牌归属到这一代进程，防 PID 复用 | `src/process/ProcessOwnershipObservation.ts:584-589`、`:1058-1111` |
| 发信号前复核 | `signalProcessWithStartIdentity`：再读一次指纹，不一致返回 `identity-changed` 不发信号 | `src/process/ProcessOwnershipObservation.ts:1129-1150` |

### 1.2 清理算法（POSIX）

`cleanupOwnedProcessGroup`（`src/process/ProcessOwnership.ts:332-435`）：

1. 读进程表；读不到直接 `cleaned:false`（`:337-342`）。
2. `createOwnedCleanupPlan`（`:662-829`）：根组 + 启动器后代所在组 + 令牌扫描命中的组 + 采样到的组 → 候选组。每个非根组的组长必须是已验证后代或令牌命中，否则整体失败 `descendant process-group leader identity could not be verified`（`:754-775`）。
3. 每组成员逐个过 `processOwnershipFailures`（`:1007-1080`）：
   - 指纹不是 same-generation → `process-identity-unavailable`（`:1040-1047`）
   - 指纹与启动时记录不符 / 组关系读不到 → `process-identity-unavailable`（`:1049-1064`）
   - 令牌读不到 → `environment-unreadable`；令牌不等 → `run-token-mismatch`（`:1066-1074`）
4. 发 `SIGKILL` 之前对每组**再读一次进程表重做第 3 步**（`revalidateOwnedProcessGroup`，`:861-914`），通过才 `signalGroup(pgid, "SIGKILL")`（`:351-377`）。
5. 开了令牌全表扫（`sweepTokenOwnedProcesses`）的，杀完在 1000ms 内每 25ms 复查「还有没有带本令牌的活进程」（`:380-428`；常量 `src/process/ProcessLifecycle.ts:10-11`）。

失败理由优先级：令牌不符 > 指纹不可得 > 环境不可读（`src/process/ProcessOwnership.ts:850-859`），汇总文案见 `:916-975`。

macOS 平台二进制被系统剥离环境、外 uid 进程 `EINVAL` 这类「既不能证明是、也不能排除」的进程，记为 `unverified` 证据而不是失败（`src/process/ProcessOwnership.ts:217-228`）。

### 1.3 超时与取消

- `ProviderStartupDeadline`：一个绝对截止时间贯穿启动各阶段，合并调用方 AbortSignal，区分 `timeout` / `cancelled`，定时器提前触发会补足剩余时间（`src/process/ProviderDeadline.ts:31-158`）。
- 短命令 `runOwnedCommand`：调用方已取消则不启动（`src/process/OwnedCommand.ts:174-182`）；建 deadline（`:183`）；每 10ms 等输出关闭、25ms 轮询中断/超限/进程错误（`:330-344`）；无论成败最后都 `supervisor.stop()`（`:388-391`）。失败类型 `cancelled|timeout|output-limit|process`（`:46`）。
- 没有清理能力的直接子进程：`SIGTERM` → 250ms → `SIGKILL` → 1000ms（`src/process/ProviderProcess.ts:24-25`、`:703-719`）。

### 1.4 清理失败时保留所有权

- `stop()` 结果为 `incomplete` 时清掉缓存的 promise，允许重试（`src/process/ProviderProcess.ts:599-608`）；未完成时 `unref()` 让宿主能退出但不放弃监管（`:627-634`、`:665`）；只有完成才 `dispose()`（`:667-668`）。
- 有 cleanup 能力：先跑 token 校验清理 → 等 1000ms → 再跑一次 → 仍未退出报 `verified process-group cleanup did not stop the launcher`（`:677-701`）。
- `runOwnedCommand` 清理未完成：交给调用方 `retainCleanup(supervisor, result)`；没给就抛 `OwnedCommandFailure` 并携带 `cleanupOwner` 与资源标签 `runId / cwd / pid:N / process-group:N`（`src/process/OwnedCommand.ts:395-417`、`:72-81`）。注释明确这些标签「不授权发信号」（`:72`）。
- 私有运行目录：只要监管者 stop 仍 incomplete 或还有挂起资源，就**不删运行目录**并报 `ProviderCleanupError`（`src/process/ArtifactAnalysisResources.ts:219-261`）。
- `PrivateRuntimeRoot`：POSIX 用 `mkdtemp`（默认 `tmpdir/rea-provider-`，owner-only）；`close()` 失败不缓存，可重试（`src/process/PrivateRuntimeRoot.ts:54-92`）。
- 跨尝试的证据账本 `ProcessOwnershipEvidenceOwner`：注释「identity is required for authority」，未知候选只当证据不当授权（`src/process/ProcessOwnershipEvidence.ts:47-85`）。

### 1.5 Windows

- 原生插件 `native/windows/build/rea-windows-x64.node`：加载前校验 manifest 版本 == 包版本、SHA-256、PE 架构（`src/windows/WindowsNativeLoader.ts:126-140`）；无环境变量能换任意模块（`native/windows/README.md:7-10`）。
- 进程：Job Object 设 `KILL_ON_JOB_CLOSE`、读回校验 flags 防 breakaway（`native/windows/src/process.cc:24-41`）；`CREATE_SUSPENDED` 创建，`IsProcessInJob` 确认入 job 后才 `ResumeThread`，否则 `TerminateJobObject`（`:160-212`）。
- 私有目录：受保护 DACL，只允许当前用户 + SYSTEM，按句柄读回校验（`native/windows/src/filesystem.cc:149-156`、`:239-266`）。
- 兜底 `taskkill /pid N /t /f`，注释明说是「bounded P0 cleanup，不是 Job Object 所有权证明」（`src/process/ProcessOwnership.ts:261-304`）。
- Windows 不支持 POSIX 的令牌全表扫（`src/process/ProviderProcess.ts:241-242`），进程捕获在 Windows 默认不可用（`src/server/sessionAvailabilityPolicy.ts:45`）。

---

## 2. `rea setup` 对 Claude Code 写什么

### 2.1 写哪两处

| 目标 | 路径 | 依据 |
|---|---|---|
| MCP 注册 | `${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json` | `src/application/SupportedClients.ts:157-158`、`:487-488` |
| 检测标记 | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` 存在即视为已装 Claude Code | `src/application/SupportedClients.ts:160-161`、`:489`；`src/application/SetupHost.ts:467-487` |
| skill 根 | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/reverse-engineer-anything/` | `src/application/SupportedClients.ts:485-486`；`src/application/SetupSkill.ts:185-196` |
| 共享 skill 根（仅当选了 `shared`） | `~/.agents/skills/reverse-engineer-anything/` | `src/application/SupportedClients.ts:831-849` |

**不写**：`~/.claude/settings.json`、`CLAUDE.md`、permissions/allowedTools —— setup 代码里没有这类目标（`src/application/SetupPlan.ts:291-353` 只产生 `install_hopper` / `configure_client` / `install_skill` 三种动作）。

### 2.2 MCP 注册内容

- 键名固定 `rea`（`src/identity.ts:14`），写在 `.claude.json` **顶层** `mcpServers`（`src/application/ClientConfigurationDocument.ts:85-101` 默认 `mcpServers`；`:258-264` serversPath=`["mcpServers"]`）→ 即 Claude Code 的 user scope。
- `claude_code` 方言走默认分支：`{ "command", "args", "env"? }`，**没有** `type` 字段（`src/application/ClientConfigurationDocument.ts:1652-1726`，default 在 `:1720`）。
- command 取值（`src/application/SetupHost.ts:69-80`、`src/application/ClientRegistrationIdentity.ts:56-66`）：
  - 用 `npx`/`bunx` 跑的 setup → `npx -y rea-agents@6.7.0 mcp`（钉死当前版本，`src/identity.ts:10`）
  - 用全局安装的 `rea` 跑 → `[<process.argv[1] 绝对路径>, "mcp"]`（非 Windows）
- env 只放：doctor 探测到的 provider 注册变量 + `HOPPER_LAUNCHER_PATH` + 进程环境里存在的这 5 个：`REA_ANALYSIS_PROVIDER`、`REA_GHIDRA_STARTUP_TIMEOUT_MS`、`REA_HOPPER_STARTUP_TIMEOUT_MS`、`REA_JEB_MCP_TIMEOUT_MS`、`REA_MCP_INPUT_SCHEMA_PROFILE`（`src/application/SetupHost.ts:181-189`；`src/application/SetupRegistrationEnvironment.ts:4-21`）。不拷贝其他环境变量（`src/application/SetupClientConfiguration.ts:754-756`）。已有 `rea.env` 里的用户值保留（`src/application/SetupPlan.ts:376-382`）。

示意（`npx` 场景，env 视探测而定）：

```json
{ "mcpServers": { "rea": { "command": "npx", "args": ["-y", "rea-agents@6.7.0", "mcp"] } } }
```

### 2.3 写入方式与备份

- `.claude.json` 按 Claude Code 的规则用严格 `JSON.parse`（只去 BOM，不收 JSONC）（`src/application/ClientConfigurationDocument.ts:164-170`）；坏 JSON 直接 `readback` 失败不写（`src/application/SetupClientConfiguration.ts:182-187`）。
- 原文存在时**只编辑 `mcpServers.rea` 这个成员**，其余文本原样（`src/application/ClientConfigurationDocument.ts:1582-1605`；调用 `src/application/SetupClientConfiguration.ts:286-303`）；文件不存在则写 `{"mcpServers":{"rea":…}}`。
- 符号链接：仅当链接和目标都属当前 uid 才跟随，否则拒绝（`src/application/ClientConfigPath.ts:20-52`）。
- 备份：写前把**整份原文件**以 `COPYFILE_EXCL` 复制为 `~/.claude.json.rea.backup`；该名已被不同内容占用时改用 `~/.claude.json.rea.backup.<sha256>.recovery`；复制后读回比对（`src/application/SetupClientConfiguration.ts:315-324`；`src/application/SetupRecoveryBackup.ts:29-80`、`:103-143`）。
- 原子发布：同目录 `mkdtemp(.rea-config-)` 暂存 → `chmod`（沿用原 mode，否则 0600）→ fsync → `rename` 覆盖（首次创建用 `link` 防竞争）（`src/application/ClientConfigurationPublication.ts:34-95`；mode 0600 `src/application/SetupClientConfiguration.ts:328-333`）。
- 写后读回校验，不符则用原文回滚（`src/application/SetupClientConfiguration.ts:340-389`、`:659-698`）。首次创建的文件无法自动回滚，需手工删（`src/application/ClientConfigurationPublication.ts:122-126`）。
- Claude Code **没有**标 `quitBeforeSetup`（只有 Claude Desktop 标了，`src/application/SupportedClients.ts:492-498`）。推断：Claude Code 运行时会重写 `~/.claude.json`，并发写可能冲掉 `rea` 键；REA 的「读回比对」只覆盖它自己写入的那一刻。

### 2.4 skill 落盘

- 7 个文件：`SKILL.md` + `references/{connection-and-recovery,native-and-artifacts,javascript-applications,android-applications,runtime-observation,evidence-workflows}.md`（`src/application/SetupSkill.ts:22-30`）。
- 内容来自包内 `skills/reverse-engineer-anything/`（`src/application/SetupSkill.ts:78-85`），该目录由 `.agents/skills/reverse-engineer-anything/` 构建生成：加 `tool_count`、把 `rea-agents@latest` 替换成钉死版本（`scripts/generate-skill-metadata.mjs:13-40`）。仓库源 `SKILL.md` 里写的是 `npx -y rea-agents@latest …`（`.agents/skills/reverse-engineer-anything/SKILL.md:34-56`）。
- 目录是符号链接 / 非目录 → 拒绝安装，提示用 `--skill=false`（`src/application/SetupSkill.ts:248-303`）。
- 每个将被改的已有文件先备份 `<file>.rea.backup`（同 2.3 规则），`write-file-atomic` 写入（新文件 mode 0600），读回比对，失败按逆序回滚（`src/application/SetupSkill.ts:536-596`）。
- 默认是否装 skill：显式 `--client X` 时 `installSkill = --skill ?? true`（`src/cli/setupCommands.ts:252-273`）。要只注册不装 skill：`--skill=false`。

### 2.5 什么时候真的写

- 非交互无 `--yes`：只返回 `needs_confirmation`（`src/application/Setup.ts:267-302`）。
- `--dry-run`：返回计划不写（`src/application/Setup.ts:254-265`；`--dry-run` 强制 structured，`src/cli/setupCommands.ts:261-264`）。
- 首次运行 `rea setup --yes` 不带 `--client`：默认客户端只取「已存在且属于 REA 的注册」（`src/application/SetupPlan.ts:110-119`），首次为空 → 推断：什么都不写。
- 交互模式（TTY 且无 `--yes/--dry-run/--json`）会先让你勾选客户端（`src/application/Setup.ts:151-217`；`src/cli/setupCommands.ts:290-302`）。
- **install.sh 装完会自动拉起交互 `rea setup`**，除非 `--no-setup`（`install.sh:92-95`、`:173-178`）。
- setup 开头会跑一次 doctor（探测各 provider，可能启动其版本探针进程）（`src/application/SetupHost.ts:178-181`）。

### 2.6 卸载 `rea uninstall [--purge-data]`

- 遍历**所有**支持客户端的配置路径（不止 Claude Code）（`src/application/Uninstall.ts:260-279`、`:360-369`）。
- 只删「属于 REA」的 `rea` 条目：command 必须分类为 owned（`rea-agents` 包名/钉版本的 npx/bunx、裸 `rea mcp`、或指向当前入口的绝对路径）（`src/application/Uninstall.ts:723-746`；分类规则 `src/application/ClientRegistrationIdentity.ts:152-193`）。不是自己的 → `retained`（`src/application/Uninstall.ts:585-590`）。
- 任一客户端预检失败则**一个都不删**（`src/application/Uninstall.ts:149-167`）；删的过程中某个失败则停在那（`:213-235`）。
- 编辑前同样备份 `<config>.rea.backup`，原子写、读回、失败回滚（`src/application/Uninstall.ts:560-701`）。
- skill：所有 skill 根逐个检查，`SKILL.md` 以 `---\nname: reverse-engineer-anything\n` 开头才算 REA 的（`src/application/SetupSkill.ts:32-34`；`src/application/Uninstall.ts:854-897`）；只删 7 个受管文件，目录空了才删目录，用户自加文件保留（`src/application/Uninstall.ts:899-998`）。
- `--purge-data` 只删 `~/.rea/cache` 与 `~/.rea/state`（`src/application/Uninstall.ts:342`、`:748-779`）。
- **不删任何 `.rea.backup` / `.recovery` 文件**（Uninstall.ts 中无删除备份的代码路径）；Hopper 也不卸（`:168-172`）。
- 注意：`~/.claude.json.rea.backup` 是整份 `.claude.json` 的拷贝，推断其中含该文件原有的账户/项目信息，需自行清理。

### 2.7 要不要在本机跑（建议，非源码结论）

- 最小侵入：先 `npx -y rea-agents@6.7.0 setup --client claude_code --skill=false --dry-run --json` 看计划；或用 `CLAUDE_CONFIG_DIR=<临时目录>` 隔离试跑（路径解析依据 `src/application/SupportedClients.ts:157-161`）。
- 也可以完全手工在 `~/.claude.json` 加 2.2 那段，跳过 setup。
- 若装 skill，会落在 `~/.claude/skills/`（用户全局目录）——按本仓 CLAUDE.md，那是用户常驻配置，需用户自己决定。
- 用 `install.sh` 时加 `--no-setup`，避免自动进交互 setup。

---

## 3. `rea update`

流程（`src/application/Update.ts:175-247`）：

1. 识别安装方式：Bun 全局 or npm 全局前缀，识别不了就 `unknown-install-method` 不动（`src/application/UpdateRuntime.ts:347-359`；`src/application/Update.ts:199-207`）。
2. **联网**查最新版：`npm view --global [--prefix P] rea-agents dist-tags.latest --json --fetch-retries=0 --fetch-timeout=10000`，或 `bun info rea-agents dist-tags.latest --json`（Bun ≥1.2.17）（`src/application/UpdateRuntime.ts:246-268`、`:360-389`）。
3. 不比当前新 → `current`，结束（`src/application/Update.ts:192-198`）。
4. 安装：`npm install --global --ignore-scripts --prefix P rea-agents@<ver>` 或 `bun add --global --ignore-scripts …`（`src/application/Update.ts:88-115`）。子进程环境去掉 `npm_command`（`src/application/UpdateRuntime.ts:66`）。
5. 校验：只校验「新装的 `scripts/rea.mjs --version` 输出 == 目标版本」（`src/application/UpdateRuntime.ts:401-409`；`src/application/Update.ts:221-230`）。**没有**额外签名/哈希校验，完整性依赖 npm/bun 自身的 lockfile integrity（推断）。
6. 维护计划：用新版执行 `setup --client … --skill-client … --skill=… --dry-run --json`，只针对已有的、属于 REA 的注册和 skill；计划超出既有范围则判 `unavailable`（`src/application/UpdateMaintenance.ts:89-152`）。**不自动应用**，提示用户自己跑（`src/application/Update.ts:240-245`）。

只有用户执行 `rea update` 才会触发；没找到启动时自动检查更新的代码（`dist-tags` 仅出现在 `UpdateRuntime.ts`，调用方只有 `src/cli/setupCommands.ts:173-193`）。

推断：`--ignore-scripts` 意味着更新时 frida 可选依赖的安装脚本不会重跑。

---

## 4. 网络外发 / 遥测

**遥测：无。** `telemetry|analytics|sentry|posthog|mixpanel|amplitude|datadog` 全仓 src 只命中内部诊断命名（`src/hopper/HopperDiagnostics.ts:8`、`src/ghidra/GhidraClientTypes.ts:18`）。日志只写 stderr（`src/logger.ts:31-38`）。

外发清单（`fetch` / `node:http(s)` / `WebSocket` / 外部 URL 全量 grep 后逐条核对）：

| # | 触发 | 目标 | 用途 | 位置 |
|---|---|---|---|---|
| 1 | `install.sh` 未给 `--version` | `api.github.com/repos/morluto/rea/releases/latest` | 解析最新 tag | `install.sh:126-133` |
| 2 | `install.sh` | npm registry | `npm install --global rea-agents@<ver>`（**未**加 `--ignore-scripts`） | `install.sh:141-166` |
| 3 | `rea update` | npm/bun registry | 查 `dist-tags.latest` + 安装 | 见 §3 |
| 4 | setup 中批准安装 Hopper | `www.hopperapp.com`（releases feed、公钥、下载） | 下载 Hopper；mac 用 `codesign --verify` 校验发布者；Linux 用分离 PGP 签名 + 指纹，`pkexec` 调包管理器 | `src/application/HopperReleaseFeed.ts:8-12`；`src/application/HopperDownload.ts:24`；`src/application/MacHopper.ts:75-85`；`src/application/LinuxHopper.ts:45-60`、`:640-647` |
| 5 | 浏览器观察工具 | 页面声明的 source map URL | 仅批准 origin、`credentials:"omit"`、手动跟随重定向、30s/64MiB 上限 | `src/browser/WebSourceMapFetcher.ts:52-57`、`:365-390`、`:829-837` |
| 6 | 浏览器/Electron 工具 | 本地 CDP 调试端点 | HTTP 探测 + WebSocket | `src/browser/CdpEndpoint.ts:1`、`:465`；`src/browser/CdpConnection.ts:83` |
| 7 | IDA provider | `REA_IDA_MCP_CONFIG` 指定的 URL | Streamable HTTP MCP 客户端 | `src/ida/IdaMcpConnection.ts:290`、`:438` |
| 8 | JEB provider | `REA_JEB_MCP_URL`，默认 `http://127.0.0.1:8425/mcp` | MCP 客户端 | `src/application/CapabilityInventory.ts:571`；`src/jeb/JebProvider.ts:127` |
| 9 | Frida 工具带 `remote` | 调用方给的 Frida 远端地址（可带 token/证书） | 远端设备 | `src/contracts/fridaToolContracts.ts:12-25` |
| 10 | ADB 工具 | 本地 adb server / 设备 | 标注 `accessesNetwork` | `src/contracts/toolEffects.ts:357-452` |
| 11 | `rea mcp --http` | 监听，默认 `127.0.0.1`、端口默认随机 | 入站服务 | `src/main/mcpHttpArguments.ts:12-28` |
| 12 | MCP 注册为 `npx -y rea-agents@6.7.0 mcp` 时 | npm registry | 推断：每次客户端拉起 MCP 时 npx 可能联网解析/下载 | `src/application/ClientRegistrationIdentity.ts:60-63` |
| 13 | skill 指导 agent 跑 `npx -y rea-agents@… doctor/setup` | npm registry | skill 文案本身写明 npx 会下载 | `.agents/skills/reverse-engineer-anything/SKILL.md:34-36` |

补充：

- `--http` 模式**没有认证**（`src/server/http/serveStreamableHttp.ts` 内无 auth/authorization 逻辑，只有 Host 头白名单；非回环绑定时连 Host 校验都放宽并打 warn，`:588-595`）。绑到非 127.0.0.1 等于把整套工具（含执行类）无鉴权暴露出去。
- 明确**不自动下载**的：jadx-headless-mcp JAR（`src/android/JadxRelease.ts:16`）、Windows 原生插件（`native/windows/README.md:47-48`）、Wireshark/apktool/binwalk 等（各自 remediation 文案）。
- `isomorphic-git` 只用 `resolveRef` 读本地仓库元数据（`src/application/ReferenceSourceVcsAdapter.ts:83-84`）。
- 推断：install.sh 不加 `--ignore-scripts`，且 `package.json:400-405` 对 `frida@17.23.2` 设了 `allowScripts: true`，frida 的安装脚本会跑（frida npm 包通常会下载预编译 binding，本仓未核实）。

---

## 5. 会执行/注入目标的能力：默认开关

结论：**没有 REA 自己的开关**。工具无条件注册（`src/server/createServer.ts:512-520` frida、`:590-596` adb、`:567-573` capture），是否可用看宿主依赖；风险通过 MCP 注解传给客户端审批。

- 注解推导：`executesForeignCode || mutatesDevice || mayDiscardData` → `destructiveHint=true`（`src/contracts/toolEffects.ts:720-742`）。
- 被标 `executesForeignCode` 的：`load_frida_script`、`resume_frida_session`、`instrument_with_frida`、`capture_process_scenario`、`capture_native_ui_scenario`、`observe_native_calls`、`capture_browser_scenario`、`capture_electron_scenario`、`cutter_command`、`gdb_console`、rizin 系列（`src/contracts/toolEffects.ts:57-118`、`:213-244`、`:570-639`、`:696-703`）。
- 被标 `mutatesDevice` 的：`push_adb_file`、`install_adb_package`、`uninstall_adb_package`、`start_adb_app`、`start_adb_activity`、`stop_adb_app`（`src/contracts/toolEffects.ts:357-452`）。
- 契约里没找到逐次确认参数（grep `confirm|acknowledge|allow_` 于 adb/frida/session 契约无命中）。

| 能力 | 什么时候可用 | 位置 |
|---|---|---|
| Frida | `frida@17.23.2` 是 optionalDependency，首次用时 `import("frida")`；能加载就可用 | `package.json:400-405`；`src/frida/FridaInstrumentationManager.ts:131-140` |
| Frida spawn | 目标保持暂停直到 `resume_frida_session`；关闭会话只 detach，不杀目标 | `src/contracts/fridaToolContracts.ts:120-125`、`:254-258` |
| ADB | `REA_ADB_PATH`（须绝对路径）或 PATH 上的 `adb` | `src/adb/AdbConfiguration.ts:19`、`:31-48` |
| 进程捕获 `capture_process_scenario` | 非 Windows 默认开；在 PTY 下跑调用方指定命令、继承宿主环境，「not a security sandbox」 | `src/server/sessionAvailabilityPolicy.ts:45`；`src/application/CapabilityInventory.ts:576-580`；`src/contracts/sessionToolContracts.ts:92` |
| 抓包/信号/固件解包/WABT/Wakaru | Linux（部分要求 x64）+ 对应**绝对路径**环境变量才开 | `src/server/createServer.ts:208-232` |
| EVM | Linux x64 默认开（WASM 跑在受 prlimit 约束的 worker） | `src/server/createServer.ts:217`；`src/evm/EvmoleInterfaceProvider.ts:73`、`:117-122` |

---

## 6. 范围 B：其余 provider 一行一个

| provider | 外部依赖 / 版本 | 入口 |
|---|---|---|
| android (JADX) | 调用方提供 `jadx-headless-mcp 0.7.1` JAR（`REA_JADX_MCP_JAR`，带 sha256 常量）+ 含 `jdk.compiler` 的完整 JDK；不下载 | `src/android/JadxProvider.ts`；`src/android/JadxRelease.ts:2-21` |
| adb | `adb`（`REA_ADB_PATH` 或 PATH），只探 `adb version` 不碰设备 | `src/adb/AdbProvider.ts:126-130`；`src/adb/AdbConfiguration.ts:87` |
| apktool | `REA_APKTOOL_COMMAND` 或 PATH，最低 2.7.0 | `src/apktool/ApktoolProvider.ts:73-77`；`src/apktool/ApktoolConfiguration.ts:35`、`:424-430` |
| dotnet (managed) | 无外部工具，纯 TS 解析 PE/ECMA-335 元数据 | `src/dotnet/ManagedStaticProvider.ts` |
| flutter | 无外部工具，进程内解析 ELF/Dart 快照 | `src/flutter/FlutterBuildProvider.ts:61-65` |
| il2cpp | 无外部工具，读 `global-metadata.dat`；可写 Ghidra 种子文件 | `src/il2cpp/Il2cppMetadataProvider.ts:54-58`；`src/il2cpp/Il2cppGhidraSeedWriter.ts:32` |
| go | 无外部工具，读 ELF/Mach-O/PE 的 Go build info | `src/go/GoBinaryProvider.ts:40-44` |
| rust | 无外部工具，读 cargo-auditable 依赖段 | `src/rust/RustBinaryProvider.ts:40-44` |
| wasm | WABT 1.0.42（`wasm-validate`/`wasm-objdump`/`wasm2wat`），`REA_WABT_BIN_DIRECTORY` 绝对路径 | `src/wasm/WabtArtifactProvider.ts:107-119`；`src/wasm/WabtRelease.ts:2-6` |
| evm | npm 包 `evmole@0.9.3`（WASM），Linux 下 `/usr/bin/prlimit`（`REA_EVM_PRLIMIT_COMMAND`） | `src/evm/EvmoleInterfaceProvider.ts`；`src/evm/EvmoleRelease.ts:2-6` |
| firmware | binwalk 3.1.0 / unblob 26.6.4，仅 Linux，`REA_BINWALK_COMMAND` / `REA_UNBLOB_COMMAND` 绝对路径 | `src/firmware/FirmwareProvider.ts`；`src/firmware/FirmwareRelease.ts:2-13` |
| pcap（离线分析） | Wireshark 4.x 的 `tshark` + `capinfos`（PATH 或 `REA_TSHARK_COMMAND`/`REA_CAPINFOS_COMMAND`） | `src/pcap/PcapProvider.ts:52-56`；`src/pcap/PcapConfiguration.ts:10-13` |
| captures（tshark 抓包 provider） | Linux + util-linux `prlimit` + 绝对 `REA_TSHARK_COMMAND`（tshark 4） | `src/captures/TsharkCaptureProvider.ts:61-64`、`:141-145` |
| signals | `sigrok-cli 0.7.2` + libsigrokdecode，Linux + prlimit，绝对 `REA_SIGROK_COMMAND` | `src/signals/SigrokRecordingProvider.ts:60-63`、`:137-142` |
| sqlite | Node 内置 `node:sqlite`，对快照只读打开、禁扩展 | `src/sqlite/SqliteDatabaseProvider.ts`；`src/sqlite/SqliteDatabaseInspection.ts:1`、`:500-510` |
| frida | `frida@17.23.2` optionalDependency（安装脚本被允许） | `src/frida/FridaInstrumentationManager.ts:131-136`；`package.json:400-405` |
