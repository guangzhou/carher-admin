# REA 入口与适配层（main @ 080c0e2，rea-agents 6.7.0）

> 范围：`scripts/rea.mjs`、`src/main.ts`+`src/main/`、`src/server/`（含 `http/`）、`src/cli.ts`+`src/cli/`、`src/composition/`、`src/contracts/`、`src/config/`、`src/mcpStartupPolicy.ts`、`src/mcpDoctor.ts`。
> 方法：只读源码与 docs（未 `npm install`/build/运行，`node_modules`、`dist/` 均不存在）。所有 `file:line` 都相对 `/Users/Liuguoxian/codes/rea`。标「推断」的是源码没直接写、我从结构推出来的。

## 0. 先说结论

**大白话**：REA 只有一个可执行入口 `scripts/rea.mjs`，它看第一个参数决定走 MCP 服务（stdio 或 HTTP）还是一次性 CLI；两条路在 `BinarySession` 这一层会合，往下（选引擎、开目标、执行、缓存、记证据）完全共用一套代码。MCP 一共 **210 个工具**，全部由 `src/contracts/` 里的契约数组 `TOOL_CONTRACTS` 驱动注册，副作用/destructive 标记也是从契约里的 `TOOL_EFFECTS` 机械推导出来的，不靠工具名猜。HTTP 模式**没有任何鉴权、没有 TLS**，安全面只靠"默认绑 127.0.0.1 + 回环时校验 Host/Origin 头"。

要点（每条展开见后文）：

1. 分发：`rea mcp`/`rea --mcp`（且后面没别的参数）→ stdio MCP；`rea mcp --http ...` → HTTP MCP；`rea mcp doctor` → 体检；其它全部进 incur CLI（`scripts/rea.mjs:15-23`、`:66-136`）。
2. MCP 进程只建**一个** `BinarySession`（`src/main.ts:110`），SDK 每次 new 出来的 server 实例、HTTP 的每个客户端 session 都共享它（`src/main/transport.ts:112-129`）。同一时刻只有一个活动 target（`src/application/binary/BinarySession.ts:78`）。
3. CLI 每条命令自己 open → execute → close，不保留会话（`src/application/DirectAnalysis.ts:99-104`、`src/application/DirectAnalysisCleanup.ts:12-31`）；分叉点是：MCP 走 `session.withAdmittedAnalysis` + `ToolResultDelivery` 编码，CLI 直接 `session.execute` 返回 JSON 给 incur 格式化。
4. 工具总数 210：`TOOL_EFFECTS` 有 210 个 key，单测 `src/contracts/toolEffects.test.ts:104-107` 断言它和 `TOOL_CONTRACTS` 名字集合相等；`tests/boundary/mcp/optionalProviderIsolation.test.ts:132-136` 断言 `tools/list` 长度 == `TOOL_CONTRACTS.length`（这两条测试我没跑，只读了）。
5. MCP annotations 只从 effects 推导：`destructiveHint = mayDiscardData || executesForeignCode || mutatesDevice`（`src/contracts/toolEffects.ts:721-744`）。按这条算，210 个里 **31 个 destructive**、**只有 5 个 readOnly**（大部分工具会往会话里记 Evidence，所以 `mutatesSession=true`，不算只读）。
6. 错误统一走 `Result<T,E>`（`src/domain/result.ts:2-10`）+ 抽象类 `AnalysisError`（27 种 `_tag`，`src/domain/analysisErrorBase.ts:4-32`、`:71`），出口由 `projectAnalysisError` 投成 21 种稳定 `code`（`src/contracts/errorSchemas.ts:38-82`）。MCP 的错误在线上只给 text，`structuredContent` 会被剥掉（`src/server/EvidenceMcpServer.ts:355-370`）。
7. CLI 退出码：0 成功；1 失败（`src/cliLogging.ts:128-131`）；130/143 = SIGINT/SIGTERM，走 `withCommandCancellation` 的命令才有（`src/cli/commandCancellation.ts:15-30`）；141 = 下游关了管道（`scripts/rea.mjs:25`、`:118`）。
8. 环境变量：非测试源码里出现 67 个 `REA_*` 名字，其中 10 个不是给人设的（Hopper 桥 Python 变量、TS 常量、日志前缀、注入给子进程的保留名），**57 个是运维可设的**。核心 schema 在 `src/config/environment.ts:23-79`，MCP 启动时一次性快照并校验（`src/main.ts:102-107`），不合法直接退出 1。
9. HTTP：默认 `127.0.0.1` + 端口 0（随机端口），可用 `--host/--port` 或 `MCP_HOST/MCP_PORT` 改（`src/config/mcpHttpEndpoint.ts:9-12`、`:35-68`）。源码里 `src/server/http/`、`src/main/` **没有任何 Authorization/token 校验**（grep 结果为空）；绑到非回环地址时 Host/Origin 校验也会**关掉**，只打一条 warn 日志（`src/server/http/serveStreamableHttp.ts:99-108`、`:591-595`）。
10. docs 和代码有一处数字对不上：`docs/client-configuration.md:115` 写「On main at 04089d5be, with 172 tools」，那是旧 commit 的测量，现在源码是 210 个（推断：catalog 在那之后扩了）。

---

## 1. 一次 MCP 工具调用的调用链（stdio）

例子：先 `open_binary`，再对某函数 `procedure_pseudo_code`（MCP 里的"反编译"就是这个工具；`batch_decompile` 是增强版批量）。

### 1.1 进程启动（发生一次）

| # | 位置 | 做什么 |
|---|---|---|
| 1 | `scripts/rea.mjs:14` | 写 `process.env.REA_PACKAGE_VERSION` = package.json version |
| 2 | `scripts/rea.mjs:15-16` | `args[0]` 是 `--mcp`/`mcp` 且只有这一个参数 → `isMcpMode` |
| 3 | `scripts/rea.mjs:61-65` | 检查 `dist/mcp/main.js` 存在，不存在就报「compiled runtime is missing」并 exit 1 |
| 4 | `scripts/rea.mjs:66-68` | `import("../dist/mcp/main.js").runEntrypoint()` |
| 5 | `src/main.ts:143-159` `runEntrypoint` | 包一层 try：任何意外异常都**隐藏原因**，只打 `SERVER_START_FAILED`，exit 1（`:153-157`） |
| 6 | `src/main.ts:99-140` `run` | 主装配：|
| 6a | `src/main.ts:102-107` | `snapshotEnvironment(env)` → `parseConfig` → 失败就打印投影后的 message 并返回 1 |
| 6b | `src/main.ts:108-110` | `createLogger("mcp", level)`（pino 写 **stderr fd 2**，`src/logger.ts:31-39`）；`createBinarySession(config, logger, env)` |
| 6c | `src/main.ts:111-117` → `src/main/startup.ts:6-33` | 若设了 `HOPPER_TARGET_PATH`，启动时就 `session.open` 一次；失败则 close 并退出 1 |
| 6d | `src/main.ts:118-129` → `src/main/transport.ts:31-221` | `startMcpTransport`：先加载可选 provider（浏览器/Electron/V8 Inspector/Frida 等，加载失败只 warn，`:46-63`），再调用 `dependencies.serve(factory)`；stdio 下 `serve = serveStdio`（`src/main.ts:39`） |
| 6e | `src/main/transport.ts:113-137` | factory：每次 SDK 要一个 server，就 `createAndroidAnalysisProvider` + `createServer({kind:"session", session}, …)`；**所有实例共享同一个 session** |
| 6f | `src/main.ts:131-138` → `src/main/shutdown.ts:20-93` | 注册关闭：stdio 下 stdin `end`/`close` 或 SIGINT/SIGTERM 都触发（`src/main/processShutdown.ts:34-53`），关闭顺序 transport → android → 可选 provider → `session.close({retainProviderDocuments:true})` |

`createServer`（`src/server/createServer.ts:299-643`）只装配、不起子进程（注释 `:294-298`）。它会 new 一个 `EvidenceMcpServer`（`:267-292`、`:355-360`），把几十个 service 建好，然后按固定顺序注册所有工具：

- `registerDesktopApplicationTool` `:481`
- `registerBinaryAnalysisTools` `:489` → `registerOfficialTools` / `registerEnhancedTools` / `registerNativeTools` / `registerArtifactTools` / `registerManagedTools` / （有 session 时）`registerManagedWorkflowTools`（`:881-935`）
- `registerReverseEngineeringTools`、`registerFridaTools`、`registerCutterTools`、`registerGdbTools`、`registerRizinDebugTools`（`:490-550`）
- `registerCaptureTools`、`registerSignalTools`（`:567-580`）
- `registerConfiguredAnalysisTools`（`:581-589`，内含 Android/JEB/binary diagnostics/analysis view/PE/Go/IL2CPP/Rust/WASM/EVM/SQLite/crash/firmware/web*/JS recovery，`:677-837`）
- `registerAdbTools`、`registerPcapTools`、`registerFlutterTools`、`registerApktoolTools`（`:590-619`）
- `registerObservationTools`（`:620`，浏览器/Electron/JS runtime/application，`:937-985`）
- `registerGuidedPrompts`（`:621`，6 个 prompt）
- **有 session 时**才 `registerSessionTools`（`:622-641`）——`open_binary`/`close_binary`/`binary_session`/evidence bundle/process capture 等全在这里

### 1.2 `open_binary` 一次调用

| # | 位置 | 做什么 |
|---|---|---|
| 1 | SDK `serveStdio` | 从 stdin 读 JSON-RPC `tools/call`（SDK 内部，`node_modules` 不在本地，未读） |
| 2 | `src/server/EvidenceMcpServer.ts:253-288` `ErrorEvidenceTransport` | 包在 SDK transport 外面：记下 `id → tool name`（`:254-259`），处理 `notifications/cancelled`（`:266-278`），然后交给 SDK 的 `onmessage` |
| 3 | SDK `McpServer` | 按名字找 registered tool，用 inputSchema 校验参数（SDK 内部） |
| 4 | `src/server/RequestTrackingMcpServer.ts:22-57` | 所有 `registerTool` 回调都被套一层 `runRequest`；stdio 下 `#tracking=false`，直接调用原回调（`:64`） |
| 5 | `src/server/registerSessionTools.ts:174-221` `registerOpenLifecycleTool` | 回调：`logToolExecution(…, () => session.open(input.path, {signal, formatHint, architecture, entryPoint, providerId, loadSnapshot}))`（`:183-209`） |
| 6 | `src/application/binary/BinarySession.ts:220-237` `open` → `:258-271` `#open` | 进 `#serialize`（`:876-899`）：生命周期操作**串行化**，同时 abort 进行中的 prompt completion |
| 7 | `BinarySession.ts:288-382` `#openWithinTransition` | 解析 target + 选 provider 路由 → `#drainCalls`（等之前的调用结束，`:903-909`）→ 同 target 同 profile 走快路径复用（`:315-322`）→ 否则 retire 旧 client（`:323-335`）→ `route.createClient(target)`（`:344`）→ `client.execute("health")` 启动引擎（`:348`）→ 设 `#active`、选 snapshot（`:361-372`） |
| 8 | `src/application/binary/SessionProviderRouter.ts:130-161` | `createClient` 实际调用 `provider.createClient(target, binding.profile, context)`；provider 由 `AnalysisProviderRegistry([hopper, ghidra, ida, kuna], config.analysisProvider)` 选出（`src/composition/binary.ts:48-70`） |
| 9 | `registerSessionTools.ts:211-219` | 成功 → `server.delivery.toCallToolResult({ok:true, value: binaryTargetSummary(...)}, openContract)`；失败 → 同函数投影错误 |
| 10 | `src/server/toolResult.ts:62-72`、`:159-177` | `kind==="session"` 的契约把结果包成 `{result: …}`；`encodeToolResult` 算预算，返回 `content:[text] + structuredContent`，`returnsTargetContent` 时加 `_meta` 标记 |
| 11 | `EvidenceMcpServer.ts:331-382` `send` | 出口：超预算错误回收（`recoverError`，`:384-524`）、SDK 参数校验错误改写成 `AnalysisInputError`（`:526-551`）、「Tool X not found」改写成提示（`:553-573`）；错误结果删掉 `structuredContent` 只留 text（`:355-370`） |

### 1.3 `procedure_pseudo_code` 一次调用（provider-backed 工具的通用路径）

| # | 位置 | 做什么 |
|---|---|---|
| 1 | `src/server/registerOfficialTools.ts:17-27` | `OFFICIAL_TOOL_CONTRACTS`（38 个 `official(...)` + 3 个 `HOPPER_MEMORY_TOOL_DEFINITIONS`，`src/contracts/officialToolContracts.ts:179`）批量交给 `registerEvidenceTools` |
| 2 | `src/server/registerEvidenceTools.ts:54-62` | 对每个 contract：`server.registerTool(contract.name, toolRegistrationOptions(contract), cb)`；`toolRegistrationOptions` 只投 title/description/inputSchema/outputSchema/annotations（`src/server/toolRegistrationOptions.ts:20-34`） |
| 3 | `registerEvidenceTools.ts:64-69` | 可选 `preflight`（例如 detail 选项不可满足时提前拒） |
| 4 | `registerEvidenceTools.ts:70-73` → `src/server/analysisAdmission.ts:20-29` → `BinarySession.ts:674-698` `withAdmittedAnalysis` | 等当前生命周期过渡（`#waitForTransition`，`:974-1000`）结束再进；把这次调用登记到 `#calls`，让后续 open/close 能 drain 它 |
| 5 | `registerEvidenceTools.ts:77-96` | 进度通知 started → `logToolExecution` → `analysis.execute(contract.name, params, {signal, progress})` |
| 6 | `BinarySession.ts:700-799` `#execute` | `prepareSessionExecution`（`src/application/binary/BinarySessionExecution.ts:55-94`：无 target → `NoBinaryOpenError`；能力不可用 → `AnalysisCapabilityUnavailableError`；命中 snapshot 缓存直接返回）→ 改 artifact 的操作先作废缓存（`:732`）→ `active.client.execute(name, args, options)`（`:733`）→ 成功且可缓存就 `createEvidence` + `recordEvidence` + `recordSnapshot`（`:762-797`） |
| 7 | `registerEvidenceTools.ts:97-151` | 进度 completed/failed → 失败 `delivery.toCallToolResult(execution)`；成功则 `createEvidence(...)`、记录 source evidence、`projectEvidence`、`recordEvidence`，最后 `delivery.toEvidenceToolResult` |
| 8 | `src/server/toolResult.ts:75-94` | 成功结果附 `evidence_id`；已记入会话的再附 `evidence_reference` 和（对 `analyze_function`/native inventory）`continuation` → `inspect_analysis_view` 分页参数（`:30-51`） |
| 9 | `toolResult.ts:178-209` | 结果超过 MCP 预算时**不截断**，改成 `resource_constraint` 错误并告诉调用方用 continuation / 导出 evidence / CLI JSON |
| 10 | 同 1.2 第 11 步 | `ErrorEvidenceTransport.send` → stdout |

另一种 handler 形状：很多 provider 族（ADB、pcap、firmware 等）用 `createContractToolHandler` / `createEvidenceToolHandler`（`src/server/contractToolHandler.ts:60-106`），逻辑相同：`runAdmittedToolOperation`（`src/server/admittedToolOperation.ts:15-30`）→ `logToolExecution` → service.execute → `deliverEvidenceResult`（失败时如果带 partial observation 会先记录再返回，`contractToolHandler.ts:34-50`）。

### 1.4 HTTP 模式在哪里不同

- 入口 `scripts/rea.mjs:20-23`、`:69-71` → `src/main.ts:177-215` `runMcpHttpEntrypoint`：先解析 `--http/--host/--port`（`src/main/mcpHttpArguments.ts:69-104`），再 `parseMcpHttpEndpoint`，在任何异步启动之前先挂信号锁存（`:203-205`）。
- `run()` 同一个函数，只是 `serve` 换成 `serveStreamableHttp`（`src/main.ts:48-91`），并强制 factory 返回 `RequestTrackingMcpServer`（`:68-69`）。
- `serveStreamableHttp`（`src/server/http/serveStreamableHttp.ts:457-601`）：node:http → `NodeHttpRequestOwner`（并发上限默认 8，`:134`、`:505-519`）→ `route`（Host/Origin 校验、只有 `/mcp`，`:472-502`）→ `McpSessionSlot.handle`（`:328-357`）→ `LiveSession.handle`（`:246-284`，套 `instance.runRequest` 做请求追踪）→ SDK `WebStandardStreamableHTTPServerTransport.handleRequest`。之后 tool 回调路径与 stdio 完全相同。
- **只有一个 server 实例**（`createMcpServer` 只调用一次，`:563-570`），一次只服务一个客户端 session；新客户端发 `initialize` 会顶掉旧 session（`:341-351`）。关掉一个 session 不会关 server，也不会关共享的 target（`docs/mcp-contracts.md:556-562`）。
- 关闭只认 SIGINT/SIGTERM，不看 stdin（`src/main.ts:76-89`、`src/main/processShutdown.ts:56-67`），退出码 130/143。

### 1.5 CLI 一次性命令走同一条路的哪里分叉

`rea decompile <path> <procedure>` 为例：

| # | 位置 | 做什么 |
|---|---|---|
| 1 | `scripts/rea.mjs:82-135` | 默认分支：加载 `dist/cli-runtime/cli.js`；`cliConfigurationError` 只预检 `REA_LOG_LEVEL`（`src/cli.ts:61-68`）；`createCli(env, resultOutput).serve(cliArgs, …)` |
| 2 | `src/cli.ts:75-161` `createCli` | `Cli.create("rea", …)`（incur 0.6.1），按顺序调用 42 个 `register*Commands`（`:113-154`）；`createDirectAnalysis(environment)` 绑定 one-shot 依赖（`:83`） |
| 3 | `src/cli/coreBinaryCommands.ts:152-213` | `cli.command("decompile", {args, options, run})`；`run` → `logCliCommand(logger,"decompile", () => runDirectAnalysis(path, "procedure_pseudo_code", {procedure}, opts))` |
| 4 | `src/composition/directAnalysis.ts:17-70` | 懒加载 `DirectAnalysis.js` 和 `composition/binary.js`；配置用 `parseConfig(environment)` |
| 5 | `src/application/DirectAnalysis.ts:105-139` → `:215-402` `runAnalysis` | `readConfiguration()`（`:240`）→ `createBinarySession`（**和 MCP 同一个工厂**，`:243-246`）→ `withSessionCleanup`（`:248`）：`prepareSnapshot` → 有 snapshot 时先 `previewTarget` 并尝试直接从 snapshot 取（`:300-310`）→ `session.open` / `openResolvedTarget`（`:329-333`）→ `executeAnalysisTool`（`:335-342`，内部 `session.execute`，`:463`）→ `session.recordEvidence`（`:344`）→ 带 `--snapshot` 时写回 snapshot（`:371-397`） |
| 6 | `src/application/DirectAnalysisCleanup.ts:12-31` | **无论成败都 `session.close()`**；关闭失败时把清理错误并进输出 |
| 7 | `src/cliLogging.ts:105-149` `logCliCommand` | 推断失败（输出里是错误投影或 `healthy:false`）→ `process.exitCode = 1` |
| 8 | incur → `scripts/rea.mjs:107-119` | 格式化（TOON 默认 / `--json`）写 stdout；`exit` 回调在管道已关时用 141 |

**分叉点**（同一个 `BinarySession` 类，用法不同）：

| 维度 | MCP | CLI |
|---|---|---|
| 会话寿命 | 进程级，一个 session 跨所有调用 | 每条命令新建、用完 close |
| 进入执行 | `withAdmittedAnalysis`（排队等生命周期过渡）→ `#execute` | 直接 `session.execute`（`DirectAnalysis.ts:463`） |
| target 切换 | `open_binary`/`close_binary` 工具显式控制 | 命令参数里的 path，隐式 open |
| 结果出口 | `ToolResultDelivery` → JSON-RPC，受 `REA_MCP_MAX_RESPONSE_BYTES` 预算约束 | JSON 值 → incur 格式化，无 MCP 预算（超大结果的出路就是「用 CLI JSON」，`toolResult.ts:183-186`） |
| 错误形状 | `{error: projection}` 作为 text，`isError:true` | `{error:"Analysis failed", ...projection}`（`DirectAnalysis.ts:659-662`），exit 1 |
| 仅 MCP 有 | `open_binary`/`close_binary`/`binary_session` 等 session 工具（`src/contracts/sessionToolContracts.ts:63` 注释「available only on the long-lived MCP adapter」） | `setup`/`doctor`/`update` 等安装管理命令 |

---

## 2. 工具注册与清单

### 2.1 注册机制

- **单一真源**：`src/contracts/toolContracts.ts:41-80` `TOOL_CONTRACTS` 把 38 个族数组按固定顺序拼起来，注释写着「Complete ordered public inventory used by registration and verification」。按名字取用走 `toolContract(name)`（`:83-98`），找不到就抛异常。
- 每个契约对象都是 `{name, ...toolContractMetadata(name), description, kind, inputSchema, outputSchema, examples}`（例：`src/contracts/officialToolContracts.ts:22-37`、`src/contracts/cutterToolContracts.ts:65-87`）。
- `toolContractMetadata(name)`（`src/contracts/toolEffects.ts:770-783`）从 `TOOL_EFFECTS[name]` 取副作用，**没登记就直接 throw**「Missing effect audit」，所以新工具必须先在 effects 表里登记。它产出 `title`（按下划线转标题）、`effects`、`annotations`、`returnsTargetContent`。
- server 端注册：所有 `register*Tools` 最终调用 `EvidenceMcpServer.registerTool`（`src/server/EvidenceMcpServer.ts:182-219`，compact profile 下改写广告出去的 inputSchema）→ `RequestTrackingMcpServer.registerTool`（`src/server/RequestTrackingMcpServer.ts:22-57`，套 runRequest）→ SDK `McpServer.registerTool`。
- 非测试源码里 `registerTool(` 共 129 处调用（含 `registerEvidenceTools` 之类循环注册一整个数组的情况，所以和工具数不是一一对应）。其中 `src/server/environment/serveEnvironment.ts:95` 属于 `rea environment-serve`（把抓包 manifest 回放成 MCP 环境），不在主 catalog 里。
- `tools/list` 默认带完整 outputSchema；`REA_MCP_OUTPUT_SCHEMA_PROFILE=off` 时只从 `tools/list` 响应里删掉 outputSchema（`EvidenceMcpServer.ts:107-129`、`:341-349`），注册本身不变。

### 2.2 一共多少个：210

数法（全部只读命令，在 `/Users/Liuguoxian/codes/rea` 下执行）：

```bash
# 数 effects 审计表的 key（每个公开工具恰好一条）
awk '/^export const TOOL_EFFECTS/{f=1;next} f&&/^};/{f=0} f' src/contracts/toolEffects.ts \
  | grep -oE '^  [a-z_0-9]+:' | sort -u | wc -l        # → 210
```

为什么能用这张表代替 `TOOL_CONTRACTS`：

- `src/contracts/toolEffects.test.ts:104-107` 断言 `Object.keys(TOOL_EFFECTS).sort()` 等于 `TOOL_CONTRACTS` 的名字 sort；
- `tests/boundary/mcp/optionalProviderIsolation.test.ts:132-136` 断言真实 `tools/list` 的名字集合等于 `TOOL_CONTRACTS`，且长度相等；
- `src/mcpDoctor.ts:214-219` 的 `rea mcp doctor` 运行时也用 `TOOL_CONTRACTS` 对比 `tools/list`。

交叉核对：我把这 210 个名字逐个在 `src/contracts/` 里找 `name: "<tool>"` 或 `official("<tool>", …)` / `session("<tool>", …)` 定义，210 个全部找到唯一的定义位置，分布在 42 个契约文件里。上面两条测试**我没跑**（不允许 install/build），所以「运行时 tools/list = 210」这句是从测试断言 + 源码推出来的。不过注册时只有 `session === undefined` 会少注册 session 族（`createServer.ts:622`），生产 MCP 总是带 session（`transport.ts:120`），所以生产环境广告出去的就是 210 个。

> 提醒：`docs/client-configuration.md:115` 里的「172 tools」是 04089d5be 时的测量，已经过时。

另有 **6 个 MCP prompt**（`src/contracts/promptContracts.ts:94` `PROMPT_CONTRACTS`，例如 `investigate_feature`），它们不是工具。

### 2.3 副作用/destructive 怎么判

- 10 个布尔 effect：`mutatesTarget` / `mutatesSession` / `writesFilesystem` / `launchesProcess` / `accessesNetwork` / `changesUiState` / `mayDiscardData` / `executesForeignCode` / `mutatesDevice` / `idempotent`（`src/contracts/toolEffects.ts:2-22`）。
- 推导（`toolEffects.ts:721-744`）：
  - `readOnlyHint` = 上面前 9 个全 false
  - `destructiveHint` = `mayDiscardData || executesForeignCode || mutatesDevice`
  - `idempotentHint` = `idempotent`
  - `openWorldHint` = `launchesProcess || accessesNetwork`
- 统计（我用脚本按上面的规则对 210 条逐条算）：**destructive 31**、**readOnly 5**（`evaluate_reconstruction_coverage`、`gdb_session_status`、`get_evidence_bundle`、`list_unknowns`、`verify_unknown_resolution`）、`mutatesTarget` 30。
- 测试 `toolEffects.test.ts:133-158` 固定了「执行外部代码或改设备」这 21 个工具的名单。我统计的 31 个 = 这 21 个 + 10 个只因 `mayDiscardData` 而 destructive 的（`annotate_native_function`、`close_binary`、`export_evidence_bundle`、`observe_web_execution`、`set_address_name`、`set_addresses_names`、`set_bookmark`、`set_comment`、`set_inline_comment`、`unset_bookmark`）。
- 「返回目标内容（不可信）」标记：只有 `export_evidence_bundle`、`record_unknown` 两个工具不带；其余 208 个的结果都会带 `_meta` 不可信标记（`toolEffects.ts:745-768`）。

31 个 destructive 工具：

`annotate_native_function` `capture_browser_scenario` `capture_electron_scenario` `capture_native_ui_scenario` `capture_process_scenario` `close_binary` `close_gdb_session` `close_rizin_debug_session` `cutter_command` `execute_rizin_command` `export_evidence_bundle` `gdb_console` `install_adb_package` `instrument_with_frida` `load_frida_script` `observe_native_calls` `observe_web_execution` `push_adb_file` `resume_frida_session` `rizin_debug_command` `set_address_name` `set_addresses_names` `set_bookmark` `set_comment` `set_inline_comment` `start_adb_activity` `start_adb_app` `start_rizin_debug_session` `stop_adb_app` `uninstall_adb_package` `unset_bookmark`

另有 3 个工具 `mutatesTarget=true` 但不是 destructive：`start_frida_session`、`unload_frida_script`、`close_frida_session`。原因是它们没标 `executesForeignCode`/`mayDiscardData`/`mutatesDevice`。反过来，`capture_process_scenario` 是 destructive，但 `mutatesTarget=false`。

会让 MCP 会话失去当前 target 的「外部变更」工具：`cutter_command`、`execute_rizin_command` 都走 `session.withAdmittedExternalMutation`（`createServer.ts:499-511` → `BinarySession.ts:384-442`）。执行时会先作废 snapshot，跑完**关掉当前 target**，要求调用方重新 `open_binary`；如果当前是 Ghidra 会话则直接拒绝（`:420-426`）。

### 2.4 按类别的完整清单（210）

分组按契约所在文件归类（类别名是我取的）。「一句话」是 description 的首句**英文原文**，一句太长的截断到约 170 字符。注解列：**destructive** / readOnly / 其余 effect；只写 `mutatesSession` 表示除了往会话记 Evidence 之外没有其它副作用。契约行号是 `src/contracts/` 下 `name` 或 `official("…")` 所在的行。

| 组 | 数量 |
|---|---|
| A 原生反编译基础 | 41 |
| B 增强组合分析 | 14 |
| C 会话/证据/比较 | 23 |
| D Mach-O / 原生 UI / LLDB | 8 |
| E 二进制诊断 | 3 |
| F 证据分页视图 | 1 |
| G 归档 / App 包 | 6 |
| H .NET 托管 | 7 |
| I 语言/格式专项 | 7 |
| J 抓包/信号/固件 | 8 |
| K Android 静态 | 13 |
| L ADB 设备 | 24 |
| M 浏览器/Web/JS/Electron | 24 |
| N 应用级重建/比较 | 10 |
| O 调试器与任意命令 | 12 |
| P Frida | 9 |
| **合计** | **210** |

#### A 原生反编译基础(kind=official-proxy;Hopper/Ghidra/IDA/Kuna 后端) — 41 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `annotate_native_function` | Atomically update one function name and/or entry comments, read them back, and return a refreshed complete function dossier. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,mayDiscardData | `officialToolContracts.ts:41` |
| `inspect_native_load_image` | Verify the provider's loaded DOS MZ or explicitly selected COM image against its immutable target snapshot: complete original/modified source-byte digests, header/module  | mutatesSession | `officialToolContracts.ts:46` |
| `address_name` | Resolve the primary analyzed name at a code or data address. | mutatesSession | `officialToolContracts.ts:66` |
| `comment` | Read the regular analysis comment at an address, defaulting to the current cursor. | mutatesSession | `officialToolContracts.ts:71` |
| `current_address` | Return Hopper's current cursor address for the selected document. | mutatesSession | `officialToolContracts.ts:76` |
| `current_procedure` | Return the analyzed procedure containing Hopper's current cursor. | mutatesSession | `officialToolContracts.ts:81` |
| `current_document` | Return the document currently selected by REA's Hopper bridge. | mutatesSession | `officialToolContracts.ts:86` |
| `goto_address` | Move Hopper's GUI cursor to a mapped address and return the observed cursor address. | changesUiState | `officialToolContracts.ts:91` |
| `inline_comment` | Read the inline instruction comment at an address, defaulting to the current cursor. | mutatesSession | `officialToolContracts.ts:96` |
| `list_bookmarks` | List every bookmark in the selected Hopper document as address and name pairs; unnamed bookmarks retain a null name. | mutatesSession | `officialToolContracts.ts:101` |
| `list_documents` | List provider program or document identities. | mutatesSession | `officialToolContracts.ts:106` |
| `list_names` | List every analyzed memory and external symbol as address/value pairs. | mutatesSession | `officialToolContracts.ts:111` |
| `list_procedures` | List every analyzed procedure as address/value pairs after provider analysis. | mutatesSession | `officialToolContracts.ts:120` |
| `list_segments` | List segments or memory blocks using exclusive end addresses. | mutatesSession | `officialToolContracts.ts:125` |
| `list_strings` | List every provider-reported string object or candidate, or filter to one address, as address/value pairs with encoding, terminator status, and byte length when available | mutatesSession | `officialToolContracts.ts:130` |
| `next_address` | Return the next analyzed object address after an explicit address or current cursor. | mutatesSession | `officialToolContracts.ts:139` |
| `prev_address` | Return the previous analyzed object start before an explicit address or current cursor. | mutatesSession | `officialToolContracts.ts:144` |
| `procedure_address` | Resolve an unambiguous procedure symbol name or provider-normalized address to its canonical entry address. | mutatesSession | `officialToolContracts.ts:149` |
| `procedure_assembly` | Return assembly for one analyzed procedure identified by symbol or hexadecimal address. | mutatesSession | `officialToolContracts.ts:154` |
| `procedure_callees` | Return the provider's resolved direct callees for one procedure identified by symbol or address. | mutatesSession | `officialToolContracts.ts:159` |
| `procedure_callers` | Return the provider's resolved direct callers for one procedure identified by symbol or address. | mutatesSession | `officialToolContracts.ts:164` |
| `procedure_info` | Return provider metadata for one procedure identified by symbol or address: entrypoint, signature, locals, size, block count, and complete inclusive body ranges when obse | mutatesSession | `officialToolContracts.ts:169` |
| `read_function_instructions` | Return every raw instruction for one analyzed procedure. | mutatesSession | `officialToolContracts.ts:174` |
| `inspect_native_instruction` | Inspect one exact code or data address: decoded bytes, mnemonic, ordered operand tokens, flow, outgoing instruction references and typed incoming references where availab | mutatesSession | `officialToolContracts.ts:56` |
| `inspect_native_data_type` | Inspect one recovered type by exact database category path or defined typed data address. | mutatesSession | `officialToolContracts.ts:51` |
| `resolve_native_call_targets` | Resolve one explicit static call site using typed provider call references. | mutatesSession | `officialToolContracts.ts:61` |
| `read_bytes` | Read analyzed bytes from one provider-normalized virtual address. | mutatesSession | `hopperMemoryContracts.ts:26` |
| `search_bytes` | Find masked byte signatures in initialized analyzed memory. | mutatesSession | `hopperMemoryContracts.ts:11` |
| `address_to_file_offset` | Map one provider-normalized virtual address to its original nonnegative file offset. | mutatesSession | `hopperMemoryContracts.ts:36` |
| `procedure_references` | Return every raw incoming or outgoing reference edge for one procedure. | mutatesSession | `officialToolContracts.ts:182` |
| `procedure_pseudo_code` | Decompile one analyzed procedure by symbol name or provider-normalized address. | mutatesSession | `officialToolContracts.ts:191` |
| `resolve_containing_procedure` | Resolve an arbitrary address, including an interior instruction or exact external entry, to its provider-analyzed containing procedure. | mutatesSession | `officialToolContracts.ts:196` |
| `search_procedures` | Search every analyzed procedure name using literal matching by default or regex when requested. | mutatesSession | `officialToolContracts.ts:201` |
| `search_strings` | Search every provider-reported string object or candidate using literal matching by default or regex when requested. | mutatesSession | `officialToolContracts.ts:206` |
| `set_address_name` | Assign an analyst name to one mapped hexadecimal address and report whether the native edit succeeded and exact readback matched. | **destructive** · mutatesTarget,mayDiscardData | `officialToolContracts.ts:211` |
| `set_addresses_names` | Assign analyst names to multiple mapped addresses in one call and return per-address edit/readback success booleans. | **destructive** · mutatesTarget,mayDiscardData | `officialToolContracts.ts:216` |
| `set_bookmark` | Create or update a bookmark at a mapped hexadecimal address and report readback success. | **destructive** · mutatesTarget,mayDiscardData | `officialToolContracts.ts:221` |
| `set_comment` | Write a regular analysis comment at a hexadecimal address and return whether readback matched. | **destructive** · mutatesTarget,mayDiscardData | `officialToolContracts.ts:226` |
| `set_inline_comment` | Write an inline instruction comment at a hexadecimal address and return whether readback matched. | **destructive** · mutatesTarget,mayDiscardData | `officialToolContracts.ts:231` |
| `unset_bookmark` | Remove the bookmark at a mapped hexadecimal address and return whether it is absent. | **destructive** · mutatesTarget,mayDiscardData | `officialToolContracts.ts:236` |
| `xrefs` | Return analyzed references to a code or data address. | mutatesSession | `officialToolContracts.ts:241` |

#### B 增强组合分析(kind=enhanced) — 14 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_native_dispatch_metadata` | Inspect the bound provider's name inventory for Objective-C class/method symbols and Swift mangled symbols, returning at most max_records decoded names. | mutatesSession | `enhancedToolContracts.ts:28` |
| `trace_native_ui_action` | Trace one unique compiled UI action, object ID, native symbol or exact function address through authored connections, encoded or symbolized Objective-C handlers, and boun | mutatesSession | `enhancedToolContracts.ts:79` |
| `trace_native_values` | Trace a bounded static dependency graph from one explicit native procedure. | mutatesSession | `enhancedToolContracts.ts:84` |
| `get_objc_classes` | Discover and deduplicate Objective-C class labels, optionally filtering by literal substring. | mutatesSession | `enhancedToolContracts.ts:33` |
| `get_objc_protocols` | Discover and deduplicate Objective-C and Swift protocol labels. | mutatesSession | `enhancedToolContracts.ts:38` |
| `batch_decompile` | Decompile each explicit procedure symbol or address concurrently. | mutatesSession | `enhancedToolContracts.ts:43` |
| `get_call_graph` | Resolve one symbol or address to its canonical procedure entry, then traverse the bound provider's caller or callee relationships until the reachable graph is exhausted. | mutatesSession | `enhancedToolContracts.ts:48` |
| `analyze_swift_types` | Categorize analyzed Swift procedure names using their manglings or same-address Swift symbol aliases. | mutatesSession | `enhancedToolContracts.ts:53` |
| `find_xrefs_to_name` | Resolve an exact name against the bound provider's name inventory and return a resolved or unresolved result. | mutatesSession | `enhancedToolContracts.ts:58` |
| `binary_overview` | Return the bound document and exhaustive procedure/string counts, plus every segment and segment_count when the provider supports segment enumeration. | mutatesSession | `enhancedToolContracts.ts:63` |
| `analyze_function` | Build a dossier for one native function identified by symbol or provider-returned address. | mutatesSession | `functionWorkflowToolContracts.ts:37` |
| `inspect_native_api` | Analyze a native API boundary in one function identified by symbol or provider-returned address. | mutatesSession | `functionWorkflowToolContracts.ts:41` |
| `trace_feature` | Trace the complete literal query, preserving spaces, through every matching string and procedure, their xrefs, and truthful containing-procedure resolution. | mutatesSession | `enhancedToolContracts.ts:69` |
| `trace_call_path` | Resolve start and optional goal symbols or addresses to canonical procedure entries, then trace direct callers or callees until the graph is exhausted or the goal is reac | mutatesSession | `enhancedToolContracts.ts:74` |

#### C 会话/证据/比较(多为 kind=session,仅 MCP 长会话) — 23 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `open_binary` | Open a local executable, application bundle, archive, JavaScript, source map, plist, or analysis database after validation. | launchesProcess | `sessionToolContracts.ts:65` |
| `close_binary` | When the investigation is finished, drain earlier provider requests, optionally write a provider-neutral analysis snapshot atomically to the caller-supplied path, then cl | **destructive** · writesFilesystem,mayDiscardData | `sessionToolContracts.ts:70` |
| `binary_session` | Report the complete current target, provider, capability availability, client-feature, analysis, and server-identity status without starting analysis. | launchesProcess | `sessionToolContracts.ts:75` |
| `export_evidence_bundle` | Atomically write the session's deterministic Evidence bundle to the requested local path. | **destructive** · writesFilesystem,mayDiscardData | `sessionToolContracts.ts:80` |
| `get_evidence_bundle` | Return every retained Evidence record and residual unknown as a complete canonical bundle by default. | readOnly | `sessionToolContracts.ts:160` |
| `get_navigation_context` | Return the selected document, current address, and containing/current procedure in one provider-neutral result. | mutatesSession | `sessionToolContracts.ts:165` |
| `inspect_address_context` | Inspect one explicit reproducible address for its analyzed name, containing procedure, regular and inline comments, and matching bookmarks. | mutatesSession | `sessionToolContracts.ts:170` |
| `import_evidence_bundle` | Read the JSON bundle at the supplied local path, validate every Evidence ID and canonical manifest, then atomically merge it. | mutatesSession | `sessionToolContracts.ts:85` |
| `capture_process_scenario` | Run one caller-selected command under a PTY and return process capture Evidence with residual unknowns. | **destructive** · writesFilesystem,launchesProcess,accessesNetwork,executesForeignCode | `sessionToolContracts.ts:90` |
| `compare_process_captures` | Compare two compatible process capture observations across terminal, interaction, lifecycle, process, filesystem, command-shim, HTTP, and WebSocket evidence. | mutatesSession | `sessionToolContracts.ts:95` |
| `compare_artifacts` | Compare complete artifact inventories by logical occurrence path, content identity, metadata, and graph relations. | mutatesSession | `sessionToolContracts.ts:100` |
| `compare_wasm_interfaces` | Compare typed core WASM import/export names, kinds and exact signatures from two authenticated inspect_wasm_artifact Evidence records. | mutatesSession | `wasm/wasmComparisonContracts.ts:55` |
| `compare_functions` | Compare two explicit analyze_function Evidence records or exact retained session references across identity, exact provider text, calls, references, strings, and address- | mutatesSession | `sessionToolContracts.ts:110` |
| `match_native_functions` | Match two retained list_procedures inventories by unique exact reported symbols, then unique identical observed function bytes. | mutatesSession | `sessionToolContracts.ts:105` |
| `compare_bundles` | Compare two canonical Evidence bundles by exact record membership, explicit one-to-one observation pairs, and complete residual-unknown revision histories. | mutatesSession | `sessionToolContracts.ts:115` |
| `find_changed_behavior` | Aggregate validated process and artifact comparison Evidence. | mutatesSession | `sessionToolContracts.ts:120` |
| `build_call_path` | Build every shortest direct-callee path inline from complete analyze_function Evidence records using exact canonical addresses. | mutatesSession | `sessionToolContracts.ts:125` |
| `correlate_static_and_runtime` | Evaluate every explicit caller-declared hypothesis between exact static comparison findings and runtime comparison dimensions. | mutatesSession | `sessionToolContracts.ts:130` |
| `verify_reconstruction` | Verify a finite typed behavioral and structural specification against a canonical Evidence bundle. | mutatesSession | `sessionToolContracts.ts:136` |
| `list_unknowns` | List current residual-unknown heads in deterministic ID order, with optional exact status, severity, and domain filters. | readOnly | `sessionToolContracts.ts:140` |
| `record_unknown` | Create one deterministic residual unknown and immutable mutation evidence. | mutatesSession | `sessionToolContracts.ts:145` |
| `update_unknown` | Append one immutable full-state revision and mutation evidence. | mutatesSession | `sessionToolContracts.ts:150` |
| `verify_unknown_resolution` | Revalidate the current residual-unknown head against live bundled evidence, exact authority/confidence/environment requirements, and revision integrity. | readOnly | `sessionToolContracts.ts:155` |

#### D Mach-O / 原生 UI / LLDB — 8 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `observe_native_ui` | Observe an already-running native app by PID and window ID, bound to the active Mach-O target. | writesFilesystem,launchesProcess | `native/nativeToolContracts.ts:79` |
| `capture_native_ui_scenario` | Run selected-element press, increment/decrement scroll, AXValue text entry and bounded wait steps in one exact native app window. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,accessesNetwork,changesUiState,mayDiscardData,executesForeignCode | `native/nativeToolContracts.ts:84` |
| `observe_native_calls` | Launch the active Mach-O as an owned process under LLDB and record each entry into caller-selected functions or Objective-C methods: thread, module, symbol, load and file | **destructive** · writesFilesystem,launchesProcess,accessesNetwork,changesUiState,executesForeignCode | `native/nativeToolContracts.ts:89` |
| `inspect_macho` | Inspect Mach-O slices, load commands, imports, exports, dependencies, build metadata, segments, sections, permissions, and exact command provenance without launching Hopp | launchesProcess | `native/nativeToolContracts.ts:94` |
| `inspect_signature` | Inspect the active Mach-O executable's code-signing identity, hashes, authorities, requirements, entitlements, hardened-runtime state, and exact command provenance. | launchesProcess | `native/nativeToolContracts.ts:106` |
| `inspect_plist` | Parse Info.plist from the active artifact by default, or pass any local plist path. | launchesProcess | `native/nativeToolContracts.ts:111` |
| `list_architectures` | List thin or universal Mach-O slices with offsets, sizes, alignment, explicit coverage, and native-tool provenance. | launchesProcess | `native/nativeToolContracts.ts:118` |
| `demangle_swift` | Demangle an ordered list of single-line Swift symbols without requiring Hopper. | launchesProcess | `native/nativeToolContracts.ts:123` |

#### E 二进制诊断(布局/崩溃/PE 资源) — 3 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_recorded_crash` | Inspect a supplied Linux ELF64 amd64 or AArch64 little-endian core without launching its target or attaching to any live PID. | writesFilesystem,launchesProcess | `native/binaryDiagnosticsToolContracts.ts:66` |
| `inspect_binary_layout` | Inspect an explicit local binary without launching it as a host process. | writesFilesystem,launchesProcess | `native/binaryDiagnosticsToolContracts.ts:44` |
| `inspect_pe_resources` | Inspect one explicit local PE32/PE32+ image's complete bounded type/name/language resource tree without executing it or opening a disassembler. | mutatesSession | `native/binaryDiagnosticsToolContracts.ts:29` |

#### F 已留存证据分页视图 — 1 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_analysis_view` | Inspect selected views of completed ... Evidence without repeating analysis. | mutatesSession | `analysisViewToolContracts.ts:16` |

#### G 归档 / App 包 / Apple 资源 — 6 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_artifact` | Inspect an active archive or application package in one call. | writesFilesystem,launchesProcess | `artifactToolContracts.ts:81` |
| `extract_artifact` | Extract all regular files from the active archive or application package into a fresh temporary directory chosen by REA. | writesFilesystem,launchesProcess | `artifactToolContracts.ts:86` |
| `decode_interface_builder` | Decode compiled storyboard and nib archives inside the active Apple app bundle, including keyed property lists and NIBArchive object tables, into a bounded object, hierar | mutatesSession | `artifactToolContracts.ts:91` |
| `inspect_asset_catalog` | Inspect compiled Assets.car metadata in an active Apple app bundle. | launchesProcess | `artifactToolContracts.ts:111` |
| `inspect_keyed_archive` | Inspect one Foundation NSKeyedArchiver plist in the active app bundle as original object-table nodes, class descriptors, named roots, and UID edges. | mutatesSession | `artifactToolContracts.ts:106` |
| `trace_dylib_resolution` | Trace how dyld would resolve each Mach-O dylib load for the active Mach-O, or for every executable in the active .app bundle (the main app, XPC services, extensions, logi | mutatesSession | `artifactToolContracts.ts:116` |

#### H .NET 托管代码 — 7 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_managed_artifact` | Inspect an explicit PE/CLI path, or the selected managed target, without loading or executing target code. | mutatesSession | `managed/managedToolContracts.ts:62` |
| `inspect_managed_members` | Inspect PE/CLI metadata members, signatures, raw CIL hashes, decoded-instruction-tuple hashes, separately reported exception regions, call edges, field-access anchors, an | mutatesSession | `managed/managedToolContracts.ts:82` |
| `inspect_managed_native_boundaries` | Inspect PE/CLI ModuleRef, ImplMap/PInvoke declarations, CLI native-header indicators, and non-IL method implementation flags without loading or executing target code. | mutatesSession | `managed/managedToolContracts.ts:112` |
| `compare_managed_members` | Compare two authenticated inspect_managed_members Evidence records using unique-only tiers: exact declared type, name, and CIL/signature; exact declared type, name, and s | mutatesSession | `managed/managedWorkflowToolContracts.ts:98` |
| `verify_managed_native_boundaries` | Verify managed P/Invoke/native-boundary declarations against authenticated native export or function Evidence without executing managed code or translating managed metada | mutatesSession | `managed/managedWorkflowToolContracts.ts:116` |
| `import_managed_reconstruction` | Import decompiler-produced managed reconstruction against authenticated inspect_managed_members Evidence. | mutatesSession | `managed/managedWorkflowToolContracts.ts:136` |
| `project_managed_application_graph` | Project authenticated managed artifact, member, and native-boundary Evidence into the provider-neutral application graph without executing managed code, loading assemblie | mutatesSession | `managed/managedWorkflowToolContracts.ts:156` |

#### I 语言/格式专项(Go/Rust/IL2CPP/WASM/EVM/SQLite/桌面应用) — 7 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_desktop_application` | Inspect a local desktop application directory using bounded listings, file headers and selected metadata. | mutatesSession | `desktopApplicationToolContracts.ts:14` |
| `inspect_go_binary` | Inspect embedded Go compiler version, main module, dependency/replacement versions and build settings in an explicit local ELF, PE or thin Mach-O image. | mutatesSession | `go/goToolContracts.ts:13` |
| `inspect_il2cpp_metadata` | Inspect local Unity IL2CPP metadata (24/27/29/31/39), optionally linking a caller-selected 64-bit little-endian Mach-O, PE or ELF binary. | mutatesSession | `il2cpp/il2cppToolContracts.ts:14` |
| `inspect_rust_binary` | Inspect the cargo-auditable crate dependency list embedded in an explicit local ELF, PE or thin Mach-O image. | mutatesSession | `rust/rustToolContracts.ts:12` |
| `inspect_sqlite_database` | Inspect an explicit local SQLite database snapshot and its existing sibling WAL without modifying source files or opening a native analysis provider. | writesFilesystem,launchesProcess | `sqlite/sqliteToolContracts.ts:12` |
| `inspect_wasm_artifact` | Validate an explicit local WASM artifact with caller-supplied WABT 1.0.42. | writesFilesystem,launchesProcess | `wasm/wasmToolContracts.ts:21` |
| `inspect_evm_interface` | Inspect explicit local flat EVM bytecode without target execution or chain/RPC access. | writesFilesystem,launchesProcess | `evm/evmToolContracts.ts:22` |

#### J 抓包/信号/固件 — 8 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_packet_fields` | Discover protocols or field names/types registered in the installed TShark 4.x. | writesFilesystem,launchesProcess | `captures/captureToolContracts.ts:14` |
| `inspect_packet_capture` | Inspect caller-selected fields in an existing local packet capture with TShark 4.x. | writesFilesystem,launchesProcess | `captures/captureToolContracts.ts:30` |
| `inspect_signal_decoders` | Discover installed sigrok protocol decoders or inspect one decoder's channel/option help on Linux. | writesFilesystem,launchesProcess | `signals/signalToolContracts.ts:14` |
| `decode_signal_recording` | Decode an existing local logic/signal recording on Linux with sigrok-cli 0.7.2 and util-linux prlimit. | writesFilesystem,launchesProcess | `signals/signalToolContracts.ts:30` |
| `inspect_firmware_regions` | Inspect signature candidates in a local firmware file with Binwalk 3.1.x (verified with 3.1.0). | writesFilesystem,launchesProcess | `firmware/firmwareToolContracts.ts:12` |
| `extract_firmware` | Extract caller-selected firmware bytes with Unblob 26.6.x (verified with 26.6.4) into an absent output directory. | writesFilesystem,launchesProcess | `firmware/firmwareToolContracts.ts:29` |
| `inspect_pcap_client` | Inspect the caller-selected tshark and capinfos binaries: selected commands, selection sources, and versions. | writesFilesystem,launchesProcess | `pcap/pcapToolContracts.ts:17` |
| `inspect_pcap_capture` | Request detail summary for capture identity/counts, then page retained facts with inspect_analysis_view kind pcap. | writesFilesystem,launchesProcess | `pcap/pcapToolContracts.ts:27` |

#### K Android 静态(JADX/apktool/JEB/Flutter) — 13 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_android_package` | Inspect a local APK\'s package, SDK and permission declarations, decoded manifest and class/resource counts. | writesFilesystem,launchesProcess | `android/androidToolContracts.ts:15` |
| `identify_flutter_build` | Identify a Flutter app's build facts by pure parsing: the Dart VM snapshot hash from every lib/<abi>/libapp.so AOT section (vm and isolate, cross-checked), per-ABI librar | writesFilesystem | `flutter/flutterToolContracts.ts:15` |
| `inspect_dart_aot` | Inspect one ABI's Dart AOT snapshot inside an APK: the four snapshot symbols located through the ELF dynamic symbol table with their offsets, sizes, and section headers ( | writesFilesystem | `flutter/flutterToolContracts.ts:30` |
| `inspect_apktool_client` | Inspect the caller-selected apktool launcher: resolved command, selection source, and version. | launchesProcess | `apktool/apktoolToolContracts.ts:12` |
| `decode_android_resources` | Decode one local APK's resources with `apktool d --no-src` in a provider-owned workspace that is removed after projection: the readable AndroidManifest.xml, apktool.yml v | writesFilesystem,launchesProcess | `apktool/apktoolToolContracts.ts:22` |
| `inspect_jeb_client` | Inspect the running JEB client serving MCP: engine version, GUI or headless mode, and startup time. | accessesNetwork | `jeb/jebToolContracts.ts:12` |
| `open_jeb_project` | Open or create a JEB project from an artifact file or existing .jdb2 database on the engine host. | accessesNetwork | `jeb/jebToolContracts.ts:22` |
| `list_jeb_units` | List project units by stable unit path and type, with optional wildcard filter, parent restriction, and pagination. | accessesNetwork | `jeb/jebToolContracts.ts:37` |
| `decompile_jeb_item` | Decompile one type or method to engine pseudo-code using its decompiler (Dalvik, Java, or native). | accessesNetwork | `jeb/jebToolContracts.ts:56` |
| `search_android_classes` | Search complete top-level decompiler class-name rows in an explicit local APK by case-sensitive substring. | writesFilesystem,launchesProcess | `android/androidToolContracts.ts:36` |
| `inspect_android_class` | Inspect one exact Android class's parsed methods, fields and inner classes, including synthetic members. | writesFilesystem,launchesProcess | `android/androidToolContracts.ts:51` |
| `inspect_android_method` | Decompile one Android method with smali fallback. | writesFilesystem,launchesProcess | `android/androidToolContracts.ts:66` |
| `trace_android_references` | Trace incoming static using nodes to an exact Android class or selected method overload. | writesFilesystem,launchesProcess | `android/androidToolContracts.ts:85` |

#### L ADB 设备 — 24 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_adb_client` | Inspect the caller-selected adb client: resolved binary, version, and install path. | launchesProcess | `adb/adbToolContracts.ts:12` |
| `list_adb_devices` | List attached devices exactly as `adb devices -l` reports them: serial, state, transport, model metadata, and an inferred emulator/physical kind with its evidence basis. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:22` |
| `inspect_adb_device` | Inspect one device's build identity through a fixed `getprop` whitelist: Android and SDK versions, security patch, fingerprint, product identity, CPU ABIs, and debuggable | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:32` |
| `list_adb_packages` | List installed packages on one device with each base APK's device path, optionally scoped to third-party or system packages. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:44` |
| `pull_adb_package` | Pull one installed package's complete APK set (base plus every split, as `pm path` reports) from the device into a caller-selected output directory, creating a package-na | writesFilesystem,launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:59` |
| `read_adb_logcat` | Read a bounded dump of the device log (`logcat -d -t`) for one buffer, optionally restricted to a process id. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:78` |
| `inspect_adb_package` | Inspect one installed package through `dumpsys package`: version name/code, install and update times, installer, user id, flags, and the requested-permission count. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:93` |
| `pull_adb_file` | Pull one absolute device path (a database, shared-preferences XML, or any readable file) into a caller-selected output directory, digesting it with SHA-256. | writesFilesystem,launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:108` |
| `push_adb_file` | Push one local file to an absolute device path. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,accessesNetwork,mutatesDevice | `adb/adbToolContracts.ts:127` |
| `capture_adb_screen` | Capture one frame of the device screen as a PNG (`screencap -p` through exec-out, no device-side file) into a caller-selected output directory, digesting it and reporting | writesFilesystem,launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:146` |
| `list_adb_processes` | List running processes exactly as `ps -A` reports them: user, pid, ppid, rss, state, and name. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:164` |
| `list_adb_directory` | List the immediate children of one absolute device directory, including hidden entries: names, kinds, permissions, owner, group, sizes, dates, and symlink targets. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:174` |
| `list_adb_features` | List the device's declared hardware and software features (`pm list features`) for device fingerprinting. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:192` |
| `list_adb_services` | List the device's binder services with their interfaces (`service list`). | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:202` |
| `inspect_adb_display` | Report the display's physical and any overridden size (`wm size`) and density (`wm density`) as the device declares them. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:212` |
| `inspect_adb_window` | Report the window manager's focus declarations with display context, original lines and explicit summary status. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:224` |
| `read_adb_setting` | Read one settings value (`settings get`) from the system, secure, or global namespace. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:236` |
| `collect_adb_bugreport` | Collect one device-generated bugreport archive (`adb bugreport`) into a caller-selected output directory, digesting the zip. | writesFilesystem,launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:255` |
| `resolve_adb_packages` | Find installed packages by case-insensitive substring of the package name (e.g. | launchesProcess,accessesNetwork | `adb/adbToolContracts.ts:273` |
| `install_adb_package` | Install one local APK on the device (`adb install [-r]`), recording the APK's host digest and byte size. | **destructive** · mutatesTarget,launchesProcess,accessesNetwork,mutatesDevice | `adb/adbToolContracts.ts:288` |
| `uninstall_adb_package` | Request package removal for user 0 (`adb shell pm uninstall --user 0`) without data retention. | **destructive** · mutatesTarget,launchesProcess,accessesNetwork,mutatesDevice | `adb/adbToolContracts.ts:311` |
| `start_adb_app` | Start one installed app's resolved launcher activity with `am start -W -n`. | **destructive** · mutatesTarget,launchesProcess,accessesNetwork,mutatesDevice | `adb/adbToolContracts.ts:326` |
| `start_adb_activity` | Start an intent with `am start -W`: one action, optional data URI, optional explicit component, and up to 16 typed extras (string, boolean, int, long, float). | **destructive** · mutatesTarget,launchesProcess,accessesNetwork,mutatesDevice | `adb/adbToolContracts.ts:341` |
| `stop_adb_app` | Force-stop every process of one package (`am force-stop`), ending a dynamic-analysis run. | **destructive** · mutatesTarget,launchesProcess,accessesNetwork,mutatesDevice | `adb/adbToolContracts.ts:368` |

#### M 浏览器 / Web / JS / Electron — 24 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `inspect_web_network_capture` | Explicit record_ordinals also decode retained protobuf/gRPC bodies whose declared content type is recognized. | writesFilesystem,launchesProcess | `webNetworkCaptureToolContracts.ts:16` |
| `observe_web_execution` | Instrument one selected CDP page for a finite, externally driven precise coverage window. | **destructive** · mutatesTarget,accessesNetwork,mayDiscardData | `webRuntimeToolContracts.ts:16` |
| `inspect_web_event_listeners` | Inspect listeners registered directly on the first main-document CSS match in one selected CDP page. | accessesNetwork | `webRuntimeToolContracts.ts:35` |
| `trace_web_source_location` | Trace one generated position in an exported website script through an explicitly paired local source map. | writesFilesystem,launchesProcess | `webScriptToolContracts.ts:20` |
| `trace_web_module_imports` | Trace one exported website script's outgoing native ES imports/re-exports, using exact URL and optional selected import-map context. | writesFilesystem,launchesProcess,accessesNetwork | `webScriptToolContracts.ts:44` |
| `list_browser_targets` | List page targets exposed by the selected loopback Chrome DevTools Protocol endpoint. | accessesNetwork | `browserToolContracts.ts:139` |
| `inspect_web_page` | Passively inspect one selected page target through CDP without evaluating JavaScript, navigating, clicking, closing, or mutating the page. | accessesNetwork | `browserToolContracts.ts:156` |
| `analyze_web_bundle` | Capture JavaScript source from one selected CDP page and statically derive a chunk graph, route and endpoint candidates, vendor fingerprints, page-declared WebMCP metadat | accessesNetwork | `browserToolContracts.ts:174` |
| `observe_web_session` | Arm a CDP observation window of the requested duration while the user operates the page. | accessesNetwork | `browserToolContracts.ts:197` |
| `discover_webmcp_tools` | Passively inventory every page-registered WebMCP tool and its complete structural input-schema summary using the experimental CDP WebMCP domain. | accessesNetwork | `browserToolContracts.ts:216` |
| `compare_web_captures` | Compare passive web captures by providing before and after, each with the complete inspect_web_page normalized_result in inspection and an optional complete discover_webm | mutatesSession | `browserToolContracts.ts:235` |
| `capture_web_screenshot` | Capture the current visible viewport of one configured page as an inline, content-addressed PNG artifact. | accessesNetwork | `browserToolContracts.ts:263` |
| `compare_web_screenshots` | Compare two self-verifying PNG screenshot artifacts with local pixel metrics. | mutatesSession | `browserToolContracts.ts:281` |
| `capture_browser_scenario` | Run a controlled browser scenario when passive observation cannot exercise the application. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,accessesNetwork,changesUiState,mayDiscardData,executesForeignCode | `browserScenarioToolContracts.ts:26` |
| `list_electron_targets` | List every Electron file:// page target from a selected loopback CDP endpoint. | accessesNetwork | `javascript/electronToolContracts.ts:57` |
| `inspect_electron_page` | Passively inspect one Electron file page by supplying its loopback CDP endpoint and target ID. | accessesNetwork | `javascript/electronToolContracts.ts:74` |
| `capture_electron_scenario` | Use this for a provider-owned Electron run when passive CDP or Inspector observation cannot exercise application behavior. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,accessesNetwork,changesUiState,mayDiscardData,executesForeignCode | `javascript/electronToolContracts.ts:141` |
| `list_javascript_runtime_targets` | List every eligible Node/Electron V8 Inspector target from an explicit literal-loopback endpoint. | accessesNetwork | `javascript/javascriptRuntimeObservationToolContracts.ts:16` |
| `observe_javascript_runtime` | Attach passively to one Node/Electron V8 Inspector target by supplying its loopback Inspector endpoint and target ID. | accessesNetwork | `javascript/javascriptRuntimeObservationToolContracts.ts:33` |
| `analyze_javascript_application` | Reconstruct one local ASAR or extracted JavaScript application as an inline application graph without executing it. | writesFilesystem,launchesProcess | `javascript/electronToolContracts.ts:92` |
| `inspect_electron_fuses` | Inspect packaged Electron fuse bytes in one local macOS app bundle, Electron Framework binary, Windows PE executable, or Linux ELF executable. | mutatesSession | `javascript/electronToolContracts.ts:153` |
| `export_web_scripts` | Export JavaScript bytes already retained in one local inspect_web_page or capture_browser_scenario JSON capture (normalized result or complete Evidence). | writesFilesystem | `webScriptToolContracts.ts:66` |
| `reconcile_javascript_runtime` | Reconcile verified static JavaScript application graphs with existing passive web/Electron CDP, passive V8 Inspector, or provider-owned active Electron Evidence. | mutatesSession | `javascript/electronToolContracts.ts:126` |
| `recover_javascript_sources` | Recover readable modules from local UTF-8 JavaScript scripts/bundles into an absent directory, using caller-supplied Wakaru ^1.13.0 (verified with 1.14.0) on Linux x64. | writesFilesystem,launchesProcess | `javascript/javascriptRecoveryToolContracts.ts:12` |

#### N 应用级重建/跨版本比较/移动应用图 — 10 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `trace_application_feature` | Trace a typed literal seed through every reachable part of an authenticated JavaScript Application Graph supplied as inline Evidence or exact same-session retained refere | mutatesSession | `applicationToolContracts.ts:72` |
| `trace_javascript_semantics` | Trace static JavaScript data-flow, direct call/return, and closure relations from inline application Evidence or an exact same-session retained reference. | mutatesSession | `applicationToolContracts.ts:96` |
| `compare_application_versions` | Compare two authenticated JavaScript Application Graph versions supplied as inline Evidence or exact same-session retained references. | mutatesSession | `applicationToolContracts.ts:117` |
| `compare_source_to_bundle` | Compare a cryptographically committed HistoricalSourceGraph with authenticated JavaScript Application Graph Evidence supplied inline or by exact same-session retained ref | mutatesSession | `applicationToolContracts.ts:144` |
| `compare_javascript_export_shapes` | Compare static return shapes for one exact module/export selector on each authenticated JavaScript Application Graph supplied as inline Evidence or exact same-session ret | mutatesSession | `applicationToolContracts.ts:160` |
| `build_reconstruction_obligation_ledger` | Generate the complete deterministic ReconstructionObligationLedger from an authenticated Evidence bundle, reviewed obligations, and an explicit reconstruction manifest. | mutatesSession | `applicationToolContracts.ts:188` |
| `evaluate_reconstruction_coverage` | Evaluate inline evidence-backed reconstruction coverage against one named boundary. | readOnly | `applicationToolContracts.ts:218` |
| `project_android_application_graph` | Project authenticated APK inventory_artifact Evidence into an execution-free Android application inventory. | mutatesSession | `applicationToolContracts.ts:269` |
| `project_apple_application_graph` | Project authenticated IPA, macOS .app directory, ZIP, or DMG inventory Evidence into an execution-free Apple application inventory. | mutatesSession | `applicationToolContracts.ts:285` |
| `project_harmony_application_graph` | Project authenticated HarmonyOS HAP, HSP, or App Pack inventory Evidence into an execution-free application inventory. | mutatesSession | `applicationToolContracts.ts:305` |

#### O 调试器与任意命令(Cutter/Rizin/GDB/objdump) — 12 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `list_cutter_sessions` | List live REA bridge sessions hosted by upstream Cutter instances, including observed document identity and bridge-directory security status. | accessesNetwork | `cutterToolContracts.ts:67` |
| `cutter_command` | Run one arbitrary Rizin command in a selected live Cutter session. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,accessesNetwork,changesUiState,executesForeignCode | `cutterToolContracts.ts:77` |
| `inspect_with_objdump` | Run caller-selected GNU objdump inspection against a local artifact, or request operation=architectures without a path to list BFD targets advertised by the installed bin | launchesProcess | `reverseEngineeringToolContracts.ts:28` |
| `start_gdb_session` | Start a BYO GDB process in MI3 mode when available, suppress startup files, and disable auto-loading. | launchesProcess | `gdbToolContracts.ts:27` |
| `gdb_console` | Run one unrestricted GDB CLI command through MI interpreter-exec console and return raw MI, console, target, and log records inline with Evidence. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,accessesNetwork,executesForeignCode | `gdbToolContracts.ts:46` |
| `gdb_session_status` | Return the observed lifecycle state and bounded recent MI records and process diagnostics for one persistent REA GDB session; truncation and invalid-UTF-8 fields report d | readOnly | `gdbToolContracts.ts:67` |
| `start_rizin_debug_session` | Start a persistent Rizin debugger session using `-N` to suppress user settings and scripts while retaining plugins, `-0` for NUL framing, and `-d` with an optional caller | **destructive** · mutatesTarget,launchesProcess,executesForeignCode | `rizinDebugToolContracts.ts:22` |
| `rizin_debug_command` | Execute one unrestricted command in a persistent Rizin debugger session. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,accessesNetwork,changesUiState,mayDiscardData,executesForeignCode | `rizinDebugToolContracts.ts:44` |
| `rizin_debug_session_status` | Return observed lifecycle state and bounded recent NUL-framed output and process diagnostics for one Rizin debugger session; truncation fields report discarded data, and  | mutatesSession | `rizinDebugToolContracts.ts:71` |
| `close_rizin_debug_session` | Close the REA-owned Rizin debugger only after verifying no inferior remains or detaching one with a verified caller-selected TTY output sink. | **destructive** · mutatesTarget,launchesProcess,executesForeignCode | `rizinDebugToolContracts.ts:94` |
| `close_gdb_session` | Close the owned GDB process after verifying the inferior state. | **destructive** · mutatesTarget,launchesProcess,executesForeignCode | `gdbToolContracts.ts:96` |
| `execute_rizin_command` | Execute one caller-selected Rizin command against a local artifact using the Rizin CLI profile that suppresses user settings and scripts while retaining plugins. | **destructive** · mutatesTarget,writesFilesystem,launchesProcess,accessesNetwork,mayDiscardData,executesForeignCode | `reverseEngineeringToolContracts.ts:47` |

#### P Frida 动态插桩 — 9 个

| 工具 | 一句话(description 首句原文) | 注解/副作用 | 契约 |
|---|---|---|---|
| `list_frida_devices` | List Frida devices currently available to this host, optionally connecting to the caller-selected remote Frida endpoint. | launchesProcess,accessesNetwork | `fridaToolContracts.ts:69` |
| `list_frida_processes` | List processes observed on a selected Frida device or caller-supplied remote endpoint. | launchesProcess,accessesNetwork | `fridaToolContracts.ts:88` |
| `start_frida_session` | Attach Frida to one selected process or spawn one selected target. | mutatesTarget,launchesProcess,accessesNetwork | `fridaToolContracts.ts:120` |
| `load_frida_script` | Load caller-supplied JavaScript into a Frida session. | **destructive** · mutatesTarget,launchesProcess,accessesNetwork,executesForeignCode | `fridaToolContracts.ts:154` |
| `resume_frida_session` | Resume the process created by a paused Frida spawn session. | **destructive** · mutatesTarget,launchesProcess,accessesNetwork,executesForeignCode | `fridaToolContracts.ts:189` |
| `unload_frida_script` | Unload one caller-selected Frida script from a live session. | mutatesTarget,launchesProcess,accessesNetwork | `fridaToolContracts.ts:204` |
| `frida_session_status` | Return observed Frida session state and queued script messages. | mutatesSession | `fridaToolContracts.ts:225` |
| `close_frida_session` | Unload session scripts and attempt to detach REA's Frida session. | mutatesTarget,launchesProcess,accessesNetwork | `fridaToolContracts.ts:254` |
| `instrument_with_frida` | Run one caller-authored Frida script against an explicitly selected process and detach when the observation window ends. | **destructive** · mutatesTarget,launchesProcess,accessesNetwork,executesForeignCode | `fridaToolContracts.ts:269` |

---

## 3. 会话模型

**大白话**：一个 MCP 进程只有一个"当前打开的目标"。`open_binary` 打开或切换，`close_binary` 关闭（可顺手存 snapshot），`binary_session` 只看状态、不启动分析。所有分析调用都对着这个目标执行；切换目标时会先等前面的调用跑完。

### 3.1 结构

- `BinarySession`（`src/application/binary/BinarySession.ts:78-1013`）注释：「Owns the single active target shared by CLI and MCP adapters. Target transitions are serialized …」（`:71-76`）。状态字段：`#active`（当前绑定）、`#pendingCleanup`（关不掉的旧 client，下次重试）、`#transition` 串行链、`#calls`（在途调用 → generation）（`:81-99`）。
- 绑定 = `{target, profile, client, route, runId, nativeSourceAdmitted}`（`:60-69`）。target 是**打开前**就采集好的不可变制品身份（`activeTarget()` 返回 `structuredClone`，`:615-619`）。
- 一个 target 只绑**一个深分析 provider**：`SessionProviderRouter` 由 `AnalysisProviderRegistry`（候选：Hopper、Ghidra、IDA、Kuna，`src/composition/binary.ts:48-64`）选出 binding，再和一组 auxiliary provider（artifact/native/managed 等，`auxiliaryAnalysisProviderDeclarations`）合成 `CompositeProvider`；不同 provider 声明的 operation 必须互不重叠，否则构造时直接 throw（`SessionProviderRouter.ts:44-60`）。`docs/architecture.mermaid` 把这层描述为「one immutable deep binding per target」「deterministic selection, no fallback」。
- 选择器 `REA_ANALYSIS_PROVIDER` 取值是 `"auto"` 或某个 provider id（`src/contracts/providerSelection.ts:4-16`），默认 `auto`（`src/config/environment.ts:24`）；`open_binary` 的 `provider_id` 可以按次覆盖（`registerSessionTools.ts:194-196`）。契约原文：「the binding remains stable until close or an explicit switch, with no failure fallback」（`src/contracts/sessionToolContracts.ts:66`）。

### 3.2 生命周期

| 操作 | 位置 | 语义 |
|---|---|---|
| open | `BinarySession.ts:220-382` | 串行化 → drain 之前的调用 → 重试旧的 pending cleanup → **同 target 同 profile 且旧 client 活着就复用**（保留数据库里的编辑，`:117-133`、`:315-322`）→ 否则 retire 旧 client → 新建 client 并 `health` 启动 → 失败就尝试**恢复上一个 target**（`#restore`，`:943-972`） |
| execute | `BinarySession.ts:630-639`、`:700-799` | 可以并发；如果有挂起的 target 过渡，新调用要等它结束 |
| withAdmittedAnalysis | `:674-698` | MCP 工具用的入口，整次调用（含记录 evidence）都算 admitted，open/close 会等它 |
| 外部变更 | `:384-442` | `cutter_command`/`execute_rizin_command`：串行化 → 作废 snapshot → 执行 → 关闭当前 target（拒绝 Ghidra） |
| close | `:444-452`、`:483-532` | drain → 关 client → **清空会话记录**（Evidence/Unknown）→ 关写文件资源和 provider 自有资源 |
| closeWithSnapshot | `:456-481` | drain → export snapshot → 原子写到 `snapshot_path` → close；**写失败就保持打开**（契约原文 `sessionToolContracts.ts:71`） |
| status | `:594-613` | `binary_session` 工具：报 target/route/运行期不可用/run id/请求活动/清理是否挂起，不启动分析 |
| 进程退出 | `src/main/shutdown.ts:36-41` | `session.close({retainProviderDocuments: true})` |

MCP 层面：
- `TARGET_FREE_INSTRUCTIONS`（`src/server/createServer.ts:148-149`）告诉客户端「An opened target persists across turns. When finished, export needed Evidence and call close_binary」。
- HTTP 模式下多个客户端 session **共享**同一个 target 和 provider；session 被顶替时会 cancel 在途调用，最多等 10 秒 drain（`serveStreamableHttp.ts:133`、`:421-438`）。超时返回 503，旧调用的资源仍归它们自己（`docs/mcp-contracts.md:562-566`）。

### 3.3 snapshot（分析快照）

- 定义在 `src/domain/analysisSnapshot.ts`，缓存在 `src/application/binary/AnalysisSnapshotCache.ts`（单个上限 `SNAPSHOT_CACHE_ENTRY_CEILING = 10_000` 条，`:68`）。
- 什么能进缓存：provider 声明的 operation 结果，前提是有具体 profile、操作不是 `STATEFUL_OPERATIONS`（`health`/`current_*`/`goto_address`/`list_documents`，`AnalysisSnapshotCache.ts:43-50`）、`cachePolicy !== "live"`（`BinarySession.ts:791-797`）。
- 命中：`#execute` 前置的 `prepareSessionExecution` 查 snapshot，命中就不调 provider（`BinarySessionExecution.ts:84-93`、`BinarySession.ts:731`）。Ghidra 要先等一次真实调用成功、证明 native source 已被接纳之后才允许回放（`nativeSourceAdmitted`，`BinarySession.ts:756-760`）。
- 作废：任何 `capability.effects.mutatesArtifact` 的操作调用前就作废（`:732`）；外部变更也作废（`:415`）。作废后 `exportAnalysisSnapshot` 会报 `EvidenceIntegrityError`（`BinarySessionRecords.ts:151-161`）。
- 导出内容：primitive entries + workflow entries + 该 target sha256 的 Evidence bundle（`BinarySessionRecords.ts:151-180`、`BinarySession.ts:471-477`）。
- 导入：`open_binary.snapshot_path`（MCP，读文件被放在 open 的串行段里，`registerSessionTools.ts:197-207`）或 CLI `--snapshot`（读写同一文件，`DirectAnalysis.ts:251-310`、`:371-397`）。必须和 binary 身份、具体 provider、规范化 profile 完全一致（契约原文 `sessionToolContracts.ts:66`）。

---

## 4. 错误模型

**大白话**：预期内的失败一律用 `Result` 返回，不抛异常。错误对象是带 `_tag` 的 `AnalysisError` 子类，出口统一投成一个稳定的 JSON 形状 `{code, category, message, retryable, remediation, recovery?, details?}`。MCP 和 CLI 用的是**同一份**投影。

### 4.1 Result 与 tagged error

- `Result<T,E> = {ok:true, value} | {ok:false, error}`，`ok()`/`err()` 构造（`src/domain/result.ts:2-10`）。AGENTS.md 要求「Parse unknown boundary values and model expected failures with the tagged error algebra and `Result`」（`AGENTS.md:31`）。
- `AnalysisError`（`src/domain/analysisErrorBase.ts:71-287`）是抽象基类，`abstract readonly _tag`（`:102`），携带 `diagnostics`、`capturedOutput`（stdout/stderr/截断/退出码）、`cleanup` / `cleanupIncomplete`、`partialObservation`、`secondaryFailures`、`cleanupPrimary`（失败链上的主因）。
- 27 种 tag（`analysisErrorBase.ts:4-32`）：`AnalysisProtocolError`、`AnalysisInputError`、`AnalysisAccessDeniedError`、`AnalysisArtifactChangedError`、`AnalysisOutputError`、`AnalysisCapabilityUnavailableError`、`AnalysisUnsupportedTargetError`、`AnalysisCancelledError`、`AnalysisTimeoutError`、`AnalysisResourceConstraintError`、`ProviderSelectionError`、`ProviderAdapterError`、`BrowserObservationError`、`ArtifactOperationError`、`ProcessCaptureError`、`EvidenceIntegrityError`、`EvidenceFileError`、`UnknownRegistryError`、6 个 `Hopper*Error`、`ConfigurationError`、`NoBinaryOpenError`、`BinaryTargetError`。

### 4.2 投影：`projectAnalysisError` 与 errorSchemas

- `projectAnalysisError`（`src/domain/analysisErrorProjection.ts:52-121`）：顺着 cleanup 链找诊断信息 → `cleanupIncomplete` 时 code 强制为 `cleanup_incomplete` → 合并 details（含 `captured_output`、`partial_observation`、`cleanup_report`）→ 算 `retryable` 和 `remediation`。
- tag 到 code 的映射：静态表 `STATIC_ERROR_CODES`（`:241-264`）+ 7 种特化类型各自的映射（`:139-171`，例如 `BinaryTargetError` 有 systemCode 时映射为 `access_denied`，否则为 `target_unavailable`）。
- `retryable`：默认集合 `artifact_changed`、`invalid_request`、`provider_timeout`、`cancelled`、`revision_conflict`、`provider_unavailable`（`:593-600`），另加 capability/resource 的个别情况（`:111-117`）。
- **对外 schema**：`src/contracts/errorSchemas.ts:38-82` `analysisErrorProjectionSchema`，按 `code` 区分的 discriminated union，共 21 种：`invalid_request` `access_denied` `artifact_changed` `unreadable_output` `capability_unavailable` `unsupported_target` `provider_unavailable` `provider_timeout` `resource_constraint` `cancelled` `artifact_integrity_mismatch`（唯一一个 details 是强类型的，`:49-72`）`artifact_operation_failed` `evidence_integrity_mismatch` `truncated` `process_capture_failed` `cleanup_incomplete` `revision_conflict` `configuration_invalid` `target_unavailable` `execution_failure`。
- 10 种 `category`：`invalid_input` `unsupported_provider` `unsupported_target` `integrity_mismatch` `truncated` `cancelled` `timeout` `resource_constraint` `unavailable` `execution_failure`（`errorSchemas.ts:7-18`）。
- 生成的 JSON Schema 是 `analysisErrorJsonSchema`（`:112-114`），仓库里还有 `docs/error-contract.schema.json`（推断是用它生成的）。
- CLI 包装：`analysisCliErrorEnvelopeSchema`（`:85-109`）= `{error:"<label>", input_path?, input_reason?, ...projection}`；`DirectAnalysis.ts:659-662` 用的是 `{error:"Analysis failed", ...projectAnalysisError(e)}`。

### 4.3 MCP 侧的错误出口

- 正常错误：`ToolResultDelivery.toErrorToolResult` → `{content:[text], structuredContent:{error: projection}, isError:true}`（`src/server/toolResult.ts:102-157`）。之后 transport 在发送前**删掉 `structuredContent`**，因为错误不符合 outputSchema（`EvidenceMcpServer.ts:355-370`）。
- SDK 自己产生的参数校验错误（「Input validation error: …」）会被改写成 `AnalysisInputError` 的投影（`EvidenceMcpServer.ts:526-551`）。
- 错误本身超过响应预算时：把原始错误存成一条 Evidence（operation `mcp_tool_error`），返回 `resource_constraint`，并在 `evidence_reference` 里提示用 `export_evidence_bundle` 导出（`EvidenceMcpServer.ts:455-523`）。
- 未知工具：把 SDK 的 `-32602 Tool X not found` 改写成「call tools/list…」（`:553-573`）。
- 启动期意外异常：只打 `SERVER_START_FAILED`，原因被刻意隐藏（`src/main.ts:150-158`）。

### 4.4 CLI 退出码

| 码 | 来源 | 何时 |
|---|---|---|
| 0 | 默认 | 命令完成（结果里可以有部分证据/警告） |
| 1 | `src/cliLogging.ts:128`、`:131`、`:141` | `logCliCommand` 判定失败（输出是错误投影或 `healthy:false`，`:73-80`）或抛了异常；`scripts/rea.mjs:96-102` 配置/输出参数错误；`:131` 格式化失败后的恢复；`src/mcpDoctor.ts:322-324` doctor 不健康 |
| 130 / 143 | `src/cli/commandCancellation.ts:15-30` | 走 `withCommandCancellation` 的命令收到 SIGINT/SIGTERM，等操作和清理结束后设置。`rea mcp --http` 也是 130/143（`src/main/processShutdown.ts:4-7`） |
| 141 | `scripts/rea.mjs:25`、`:118`、`:131`、`:144-153` | stdout EPIPE（`rea … \| head`），不报错 |

`docs/cli.md:449-529` 的说明与此一致，另外列了几个例外：`inspect-artifact` 被取消时 exit 1；`setup` 被取消 exit 0，`needs_confirmation`/`needs_human` exit 1；`doctor` 只有必需检查失败才 exit 1。这几条我**没逐个对源码核实**。

---

## 5. 配置：`REA_*` 环境变量

**大白话**：核心的十几个变量（选哪个引擎、超时、MCP 输出档位、日志级别）在启动时一次性用 zod 校验，任何一个不合法就整个起不来。其余几十个是"各工具自带的外部程序路径"，只在那个工具被调用（或探测可用性）时才读；读不到不影响别的工具。REA 不会替你装这些外部工具。

### 5.1 统计方法

```bash
cd /Users/Liuguoxian/codes/rea
grep -rhoE "\bREA_[A-Z0-9_]+\b" src --include='*.ts' --exclude='*.test.ts' | sort -u | wc -l   # → 67
```

这 67 个名字里有 **10 个不是给人设的配置**，已排除：

| 名字 | 实际是什么 | 位置 |
|---|---|---|
| `REA_SOCKET` `REA_TOKEN` `REA_RUN_ID` `REA_TARGET_PATH` `REA_OWNS_PROCESS_LIFETIME` `REA_RECONNECT_TIMEOUT_SECONDS` | 生成给 Hopper 内 Python 桥的**脚本变量**，不是 env | `src/hopper/BridgeLauncher.ts:457-462` |
| `REA_WORKFLOW_PROVIDER` | TS 常量（composed workflow 的 provider 身份） | `src/application/InvestigationProviders.ts:8` |
| `REA_MANAGED_LINUX_HOPPER_LAUNCHER` | TS 常量 `/opt/hopper/bin/Hopper` | `src/application/LinuxHopper.ts:439` |
| `REA_X11_DIAGNOSTIC` | stderr 诊断行前缀 | `src/hopper/LinuxPrivateDisplayDiagnostic.ts:9` |
| `REA_PROCESS_RUN_ID` | REA **注入给**被抓包进程的保留 env 名，调用方不能占用 | `src/domain/process/processScenario.ts:17` |

还有两个虽然算进了下面的清单或统计，但通常不用手设：`REA_PACKAGE_VERSION`（`scripts/rea.mjs:14` 写入，src 里不读，不在 67 个里）、`REA_BUILD_COMMIT`（只在 `serverIdentity.ts:68` 读，没设就是 `null`，算在 57 个里）。`bridge/` 和 `scripts/` 里另外还有约 150 个 `REA_*`，全是测试、验证或 fixture 用的（`REA_VERIFY_*`、`REA_NATIVEAOT_*`、`REA_AGENT_EVAL_*` 等），不在本文范围内。

非 `REA_` 前缀但同样重要的：`MCP_HOST`、`MCP_PORT`（`src/config/mcpHttpEndpoint.ts:42-59`），`GHIDRA_INSTALL_DIR`、`JAVA_HOME`、`HOPPER_LAUNCHER_PATH`、`HOPPER_TARGET_PATH`、`HOPPER_TARGET_KIND`、`HOPPER_LOADER_ARGS_JSON`（`src/config/environment.ts:34-74`），`ENVIRONMENT_NAME`（environment-serve，`src/config/environmentName.ts`）。

### 5.2 读取时机

| 时机 | 内容 | 位置 |
|---|---|---|
| 分发器最早 | 写 `REA_PACKAGE_VERSION`；CLI 先预检 `REA_LOG_LEVEL` | `scripts/rea.mjs:14`、`:96`；`src/cli.ts:61-68` |
| MCP 启动（一次） | `snapshotEnvironment(process.env)` 冻结一份快照（`src/process/snapshotEnvironment.ts:18-33`）→ `parseConfig` 跑核心 schema（`src/config/parseConfig.ts:43-103`），失败直接 exit 1 | `src/main.ts:102-107` |
| MCP `createServer`（每个 server 实例） | `REA_MCP_MAX_RESPONSE_BYTES` / `REA_MCP_OUTPUT_SCHEMA_PROFILE` / `REA_MCP_INPUT_SCHEMA_PROFILE`，不合法就 **throw** | `src/server/createServer.ts:645-675` |
| MCP 可用性策略 | 用 `REA_TSHARK_COMMAND`、`REA_SIGROK_COMMAND`、`REA_WABT_BIN_DIRECTORY`、`REA_BROWSER_EXECUTABLE`、`REA_PWNTOOLS_PYTHON`、`REA_BINWALK_COMMAND`、`REA_UNBLOB_COMMAND`、`REA_WAKARU_COMMAND` 是否为**绝对路径**（加平台条件）决定 `binary_session` 里报告的可选功能是否开启 | `src/server/createServer.ts:194-265` |
| 工具首次调用/可用性探测 | 各 provider 自己从快照读自己的路径/超时（如 `src/adb/AdbConfiguration.ts:28-31`、`src/kuna/KunaConfiguration.ts:111-178`） | 见下表 |
| CLI 每条命令 | `createCli` 时快照（`src/cli.ts:82`），分析命令执行时才 `parseConfig`（`DirectAnalysis.ts:240`） | |

`REA_JEB_MCP_TIMEOUT_MS` 被特意从 `parseConfig` 里剥掉，它不合法时只让 JEB 不可用，不阻止启动（`parseConfig.ts:46-50`、`environment.ts:81-100`）。

### 5.3 清单（57 个运维可设变量，按模块分组）

「默认」一栏写「无」表示未设置时该功能不可用，或者退回 `PATH` 查找（以「作用」栏为准）。

**核心 / MCP 适配（启动期校验，`src/config/environment.ts:23-79`）**

| 变量 | 作用 | 默认 | 位置 |
|---|---|---|---|
| `REA_ANALYSIS_PROVIDER` | 深分析引擎选择：`auto` 或 provider id（hopper/ghidra/ida/kuna…） | `auto` | `environment.ts:24` |
| `REA_LOG_LEVEL` | pino 级别 trace…silent（日志写 stderr） | `info`（CLI 未设时是 `silent`，`src/cli.ts:85-92`） | `environment.ts:75-77` |
| `REA_MCP_MAX_RESPONSE_BYTES` | 单次 MCP 响应预算，≥ 10 MiB | SDK `STDIO_DEFAULT_MAX_BUFFER_SIZE`（`toolResult.ts:24-27`）；最小值 10 MiB（`mcpResponseBudget.ts:9`） | `environment.ts:25` |
| `REA_MCP_INPUT_SCHEMA_PROFILE` | `full` / `compact`（compact 把单个 inputSchema 压到 13 KiB 内，兼容 Moonshot 等） | `full` | `mcpInputSchemaProfile.ts:11-13` |
| `REA_MCP_OUTPUT_SCHEMA_PROFILE` | `full` / `off`（off 时 tools/list 不带 outputSchema） | `full` | `mcpOutputSchemaProfile.ts:7` |
| `REA_REFERENCE_SECRET_PATTERNS_JSON` | 参考源码读取时的敏感模式数组（JSON） | `[]` | `environment.ts:78` |

**Ghidra / Hopper / IDA / .NET（启动期校验）**

| 变量 | 作用 | 默认 | 位置 |
|---|---|---|---|
| `REA_GHIDRA_STARTUP_TIMEOUT_MS` | Ghidra 导入+分析+桥就绪的总期限 | `330000` | `providerStartupTimeout.ts:4` |
| `REA_GHIDRA_LANGUAGE_ID` | 强制 processor/language | 无 | `environment.ts:58` |
| `REA_GHIDRA_COMPILER_SPEC_ID` | 强制 cspec，**必须**同时设 LANGUAGE_ID | 无 | `environment.ts:59`、`:179-194` |
| `REA_GHIDRA_DISABLED_ANALYZERS` | 禁用的 analyzer 列表 | 无 | `environment.ts:60` |
| `REA_GHIDRA_SEED_FILE` | 绝对路径 seed 文件 | 无 | `environment.ts:61-65` |
| `REA_GHIDRA_NATIVEAOT_JAR` | NativeAOT 扩展 jar（绝对路径） | 无 | `environment.ts:44-48` |
| `REA_HOPPER_STARTUP_TIMEOUT_MS` | Hopper 启动+分析+桥就绪期限 | `120000` | `providerStartupTimeout.ts:7` |
| `REA_IDA_MCP_CONFIG` | 外部 ida-pro-mcp 注册 JSON（绝对路径，≤64 KiB） | 无 | `environment.ts:28-32`、`src/ida/IdaConfiguration.ts:64` |
| `REA_ILSPY_CMD_PATH` | ilspycmd 绝对路径 | 无 | `environment.ts:66-70` |
| `REA_SWIFTC_COMPILE_TIMEOUT_MS` | Darwin 进程归属 helper 的 swiftc 编译期限 | `120000` | `providerStartupTimeout.ts:11` |
| `REA_DARWIN_SWIFTC_PATH` / `REA_DARWIN_MODULE_CACHE_PATH` | 上述 helper 用的 swiftc 和 module cache | 无 | `src/process/ProcessOwnershipObservation.ts:718-719` |

**Kuna（`src/kuna/KunaConfiguration.ts`，调用时校验）**

| 变量 | 作用 | 默认 | 位置 |
|---|---|---|---|
| `REA_KUNA_COMMAND` | kuna 可执行文件（必须绝对路径） | 无 | `:111-118` |
| `REA_KUNA_CACHE_DIR` | 项目缓存根 | macOS `~/Library/Caches/rea/kuna`，其它 `$XDG_CACHE_HOME`(或 `~/.cache`)`/rea/kuna` | `:91-104`、`:123` |
| `REA_KUNA_CACHE_MAX_BYTES` | 缓存上限（≥1 MiB） | 4 GiB | `:32`、`:131` |
| `REA_KUNA_TIMEOUT_MS` | 单函数 decompile 期限 | `120000` | `:21` |
| `REA_KUNA_PROJECT_TIMEOUT_MS` | 整项目导出期限 | 30 分钟 | `:19` |
| `REA_KUNA_JOBS` | 并行度 1..32 | `1` | `:29`、`:12` |

**Android / JADX / apktool / JEB / ADB / Flutter**

| 变量 | 作用 | 默认 | 位置 |
|---|---|---|---|
| `REA_JADX_MCP_JAR` | JADX 桥 jar | 无 | `src/android/JadxConfiguration.ts:546` |
| `REA_JADX_HEAP_MIB` | JVM `-Xmx` | 未设就不传 | `JadxConfiguration.ts:663` |
| `REA_JADX_ACTIVE_PROCESSOR_COUNT` | JVM `-XX:ActiveProcessorCount` | 未设就不传 | `:664` |
| `REA_JADX_OPERATION_TIMEOUT_MS` | 单操作期限 | `120000` | `src/android/JadxRelease.ts:27` |
| `REA_APKTOOL_COMMAND` | apktool 启动器 | `PATH` 查找 | `src/apktool/ApktoolConfiguration.ts:35` |
| `REA_JEB_MCP_URL` | 外部 JEB MCP 地址 | `http://127.0.0.1:8425/mcp`（契约文本，`src/contracts/jeb/jebToolContracts.ts:15`） | `src/composition/jeb.ts:37` |
| `REA_JEB_MCP_TIMEOUT_MS` | JEB 请求期限（单独校验） | 无 | `environment.ts:81-100` |
| `REA_ADB_PATH` | adb 绝对路径 | `PATH` 上的 `adb` | `src/adb/AdbConfiguration.ts:19-40` |

**抓包 / 信号 / 固件 / 资源限制器**

| 变量 | 作用 | 默认 | 位置 |
|---|---|---|---|
| `REA_TSHARK_COMMAND` / `REA_CAPINFOS_COMMAND` | Wireshark 4.x 工具 | pcap 工具走 `PATH`；`inspect_packet_*` 在 Linux 上要求绝对路径才启用 | `src/pcap/PcapConfiguration.ts:10-11`、`createServer.ts:209-210` |
| `REA_TSHARK_PRLIMIT_COMMAND` | tshark 的 prlimit | `/usr/bin/prlimit` | `src/captures/TsharkCaptureProvider.ts:154` |
| `REA_SIGROK_COMMAND` | sigrok-cli 0.7.2（绝对路径） | 无 | `src/signals/SigrokRecordingProvider.ts:137-143` |
| `REA_SIGROK_PRLIMIT_COMMAND` | sigrok 的 prlimit | `/usr/bin/prlimit` | `:152-154` |
| `REA_BINWALK_COMMAND` / `REA_UNBLOB_COMMAND` | 固件扫描/提取 | 无（Linux 下绝对路径才启用） | `src/firmware/FirmwareCommand.ts:51` |
| `REA_FIRMWARE_PRLIMIT_COMMAND` | 固件的 prlimit | `/usr/bin/prlimit` | `FirmwareCommand.ts:85` |
| `REA_MITMDUMP_COMMAND` | mitmdump 12.2.3（解码原生抓包，Linux） | 无（HAR 不需要） | `src/browser/history/MitmproxyCaptureAdapter.ts:10-21` |

**WASM / EVM / JS / 浏览器**

| 变量 | 作用 | 默认 | 位置 |
|---|---|---|---|
| `REA_WABT_BIN_DIRECTORY` | WABT 1.0.42 工具目录（绝对路径） | 无 | `src/wasm/WabtArtifactProvider.ts:107-111` |
| `REA_EVM_PRLIMIT_COMMAND` | EVMole worker 的 prlimit | `/usr/bin/prlimit` | `src/evm/EvmoleInterfaceProvider.ts:73`、`:117` |
| `REA_WAKARU_COMMAND` | Wakaru（JS 源码恢复，Linux x64） | 无 | `src/javascript/recovery/WakaruCommand.ts:29` |
| `REA_JAVASCRIPT_PRLIMIT_COMMAND` | Wakaru 的 prlimit | `/usr/bin/prlimit` | `WakaruCommand.ts:86` |
| `REA_BROWSER_EXECUTABLE` | Chromium 可执行（原生 ES 模块解析，必须绝对路径） | 无 | `src/browser/modules/NativeModuleResolver.ts:111` |
| `REA_BROWSER_NO_SANDBOX` | `"true"` 时加 `--no-sandbox` | 关 | `NativeModuleResolver.ts:193` |

**调试器 / 命令行工具 / pwntools**

| 变量 | 作用 | 默认 | 位置 |
|---|---|---|---|
| `REA_GDB_COMMAND` | GDB | `gdb` | `src/gdb/GdbSessionManager.ts:561` |
| `REA_RIZIN_COMMAND` | rizin（静态命令 + 调试会话） | `rizin` | `src/composition/reverseEngineering.ts:30`、`src/rizin/RizinDebugSessionManager.ts:255` |
| `REA_OBJDUMP_COMMAND` | GNU objdump | `objdump` | `src/composition/reverseEngineering.ts:20` |
| `REA_CUTTER_BRIDGE_DIR` | Cutter 桥描述符目录（支持 `~`） | Win `%LOCALAPPDATA%\REA\CutterBridge`，其它 `~/.cache/rea/cutter-bridge` | `src/cutter/CutterBridgeClient.ts:293`、`:993-1010` |
| `REA_PWNTOOLS_PYTHON` | 带 pwntools 4.15.0 的 Python（绝对路径；开启 crash/layout 工具，Linux x64） | 无 | `src/native/pwntools/PwntoolsDecoder.ts:237` |
| `REA_PWNTOOLS_MAX_INPUT_BYTES` | pwntools 输入上限 | 32 MiB | `src/native/pwntools/PwntoolsRelease.ts:13`、`PwntoolsInputLimit.ts:33` |
| `REA_PWNDBG_GDB` / `REA_PWNDBG_GDBINIT` / `REA_PWNDBG_VENV_PATH` | core 文件 pwndbg 上下文 | `/usr/bin/gdb` / 空 / 空 | `src/native/pwndbg/PwndbgCoreContext.ts:75-77` |

**身份**

| 变量 | 作用 | 默认 | 位置 |
|---|---|---|---|
| `REA_BUILD_COMMIT` | 写进 `binary_session` 的 `package.build_commit` | `null` | `src/serverIdentity.ts:68` |

核对：各表第一列共有 **57** 个不同的变量名（有几行一行写了两三个变量），加上 5.1 的 10 个排除项正好 67。`REA_PACKAGE_VERSION` 只出现在 `scripts/`，不在这 67 个里，由分发器写入，不要手设。

---

## 6. HTTP 模式：监听地址与鉴权（安全面）

**大白话**：`rea mcp --http` 默认只监听本机 `127.0.0.1`，端口随机，**没有 token、没有 TLS、没有用户隔离**。谁能连上端口，谁就能调全部 210 个工具，包括 `gdb_console`、`cutter_command`、`capture_process_scenario` 这种能跑任意命令的工具。唯一的防护是：绑在回环地址时检查 Host/Origin 头，防 DNS rebinding 和浏览器跨站。一旦改成绑 `0.0.0.0` 或内网 IP，连这个检查也会关掉。

### 6.1 监听地址

| 项 | 值 | 位置 |
|---|---|---|
| 默认 host | `127.0.0.1` | `src/config/mcpHttpEndpoint.ts:9-10` |
| 默认 port | `0`（系统分配，绑定后把 URL 以一条 JSON 日志写到 **stderr**，无视日志级别） | `mcpHttpEndpoint.ts:11-12`；`serveStreamableHttp.ts:596-599` |
| 优先级 | `--host/--port` 参数 > `MCP_HOST/MCP_PORT` 环境变量 > 默认值 | `mcpHttpEndpoint.ts:34-68`；`src/main.ts:194-197` |
| 路径 | 只有 `/mcp`，其它路径返回 404 JSON-RPC 错误 | `serveStreamableHttp.ts:38`、`:487-501` |
| 拒绝 | scoped IPv6（`fe80::1%eth0`）在绑定前就拒绝 | `:462-465` |
| 参数 | 只接受 `--http`、`--host`、`--port`、`-h/--help`；`--host/--port` 不带 `--http` 会报错 | `src/main/mcpHttpArguments.ts:12-35`、`:90` |

### 6.2 鉴权：没有

- 在 `src/server/http/`、`src/main/`、`src/config/mcpHttpEndpoint.ts` 里 grep `authoriz|bearer|token|apiKey`（不含测试文件），**结果为空**。
- 官方文档原文：「This endpoint uses plain HTTP without TLS or client authentication. Use it only within a trusted local or isolated environment; an explicit non-loopback bind makes the same tools and active analysis state reachable at that address. It does not provide separate users or independent target sessions.」（`docs/mcp-contracts.md:550-554`）
- `EvidenceMcpServer.ts:264-265` 注释里的「Keep transport authentication outside retained data」只是说不把传输层凭据写进 Evidence，并不是说存在鉴权。

### 6.3 现有的防护

| 防护 | 行为 | 位置 |
|---|---|---|
| 回环判定 | 按**实际绑定到的 IP** 判断（`127.0.0.0/8`、`::1`、IPv4-mapped 回环），不看配置里写的名字 | `serveStreamableHttp.ts:84-108`、`:545` |
| Host 头白名单 | 回环绑定时：SDK 的 localhost 列表 + 配置的 host 名 + 实际地址；不在白名单 → 403 | `:99-108`、`:478-484` |
| Origin 头 | 同一白名单；**没有 Origin 头直接放行**（CLI 客户端就是这种情况） | `:478-484`、`docs/mcp-contracts.md:604-606` |
| 非回环绑定 | 两项校验**都关闭**，只记一条 warn「Host header validation relaxed for a non-loopback bind」 | `:591-595` |
| 并发 | HTTP 输入/路由同时最多 8 个，多出来的返回 503 | `:134`、`:505-519` |
| 请求体 | SDK 限 4 MiB（`docs/mcp-contracts.md:617-618`，这条没在本地 SDK 源码里核实，`node_modules` 不存在） | |
| 单客户端 | 一次只服务一个 session；新客户端的 `initialize` 会**直接顶掉**旧 session（不是拒绝） | `:341-351` |
| 请求目标 | absolute-form、含 `#`、带 `..` 等会被 URL 解析改写的路径一律 400，不支持的方法 405 | `serveNodeRequest` `:618-692`；`docs/mcp-contracts.md:606` |

### 6.4 对我们使用的含义（推断）

- 本机自用：保持默认 `127.0.0.1` 即可。但本机上任何进程（包括其它用户的进程）只要能连 TCP 端口，就能调用全部工具。端口是随机的，只能算"隐藏"，不能算防护。
- 想给集群或远端用：**不要**直接 `--host 0.0.0.0`。需要在前面放一层带鉴权的反向代理（或者走 ssh 隧道到回环地址）。原因是 HTTP 模式下 `capture_process_scenario`、`gdb_console`、`execute_rizin_command`、`cutter_command`、Frida、ADB 安装/卸载等 destructive 工具全部开放，没有任何二次确认。
- 多人共用一个 HTTP 实例时，后连上的人会顶掉前一个人的 session；target 和 Evidence 是共享的。不适合多租户使用。

---

## 7. `mcpStartupPolicy` 与 `mcp doctor`

- `src/mcpStartupPolicy.ts:10-19` `MCP_STARTUP_POLICY`：Codex 启动超时 30 s、Kimi 30 000 ms、工具操作余量 120 000 ms、initialize 预算 15 s、tools/list 5 s、首次 catalog 20 s、doctor 期限 30 s、doctor 保留的 stderr 64 KiB。
- 推荐的客户端工具超时 = `max(选中引擎的启动期限, JEB 超时, provider 请求超时)` + 120 s（`:31-61`）。例如 `auto` 时取 `max(Ghidra 330 s, Hopper 120 s)` + 120 s = 450 s（推断，按默认值算）。消费方：`src/application/SetupClientConfiguration.ts`、`src/application/ClientRegistrationStatus.ts`（grep 到引用，未细读）。
- `rea mcp doctor`（`scripts/rea.mjs:17`、`:72-81` → `src/mcpDoctor.ts:287-325`）：用当前 node 再 spawn 一个 `rea mcp` 作为 stdio 子进程（`:306-311`），**去掉** `HOPPER_TARGET_PATH/KIND/LOADER_ARGS_JSON`，保证测的是无 target 启动（`:476-491`）。然后检查 4 项：initialize 返回的名字和版本；tools/prompts 清单与 `TOOL_CONTRACTS`/`PROMPT_CONTRACTS` 完全一致；`binary_session` 返回的 package 版本和 catalog digest；`binary_session` 调用本身不报错（`:195-285`）。不健康时 exit 1。

---

## 8. 未核实 / 留给后续

- 没跑任何测试，也没 build。「运行时 tools/list = 210」依赖 `toolEffects.test.ts:104-107` 和 `optionalProviderIsolation.test.ts:132-136` 这两个断言是绿的，这一点没验证。
- `@modelcontextprotocol/server` 2.3.1 和 `incur` 0.6.1 的内部实现（stdio 帧解析、zod 校验、incur 输出格式化）不在本地，没读。
- `docs/cli.md` 里列的各命令退出码例外（setup/doctor/inspect-artifact）没逐个对源码核实。
- CLI 一共多少条命令没数：`src/cliCommandNames.ts:2-162` 的 `CLI_COMMANDS` 有 158 个名字，但 incur 还会加内置命令（`mcp add`、`--update` 等），两者不是同一个口径。
