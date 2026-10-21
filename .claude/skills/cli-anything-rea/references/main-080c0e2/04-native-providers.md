# 04 · 原生分析 provider 层（REA main @ 080c0e2）

> 源码：`/Users/Liuguoxian/codes/rea`，只读核对。行号均指 080c0e2。标「推断」的是没拿到直接代码证据的结论。

## 一页结论

1. **深度分析 provider 只有 4 个**：Hopper、Ghidra、IDA、Kuna，按这个顺序注册进 `AnalysisProviderRegistry`（`src/composition/binary.ts:48-63`）。`auto` 不是优先级：只有恰好 1 个可用候选才选中，≥2 个直接报 `ambiguous`（`src/application/binary/AnalysisProviderRegistry.ts:650-670`）。所以本机同时装了 Hopper 和 Ghidra，就必须显式 `--provider ghidra`。
2. **Ghidra 是"每个会话起一个 headless JVM"**：REA 先用 Ghidra 自己的 `LaunchSupport.jar` 拿 env/vmargs，再直接 exec `java ... ghidra.Ghidra AnalyzeHeadless -import <快照> -readOnly -deleteProject -postScript ReaGhidraBridge.java <session.json>`；脚本在 auto-analysis 跑完后在 Unix socket 上服务 NDJSON 请求（带 32 字节随机 token）。默认启动预算 330 s，堆 `-Xmx` 默认 2G。
3. **Hopper 是"借 GUI 的 Python 线程"**：REA 写一个带 token 的 `bootstrap.py`，用 Hopper 官方 `hopper` 启动器 `-Y bootstrap.py --executable <target>` 注入 `bridge/hopper_bridge.py`，bridge 在 Hopper 内部开 Unix socket。macOS 上 REA 不拥有 Hopper GUI 进程，只靠 `/tmp/rea-hopper-<uid>/` 下的 lease socket 防止两个 REA 会话对同一 target 开重复文档。
4. **IDA / JEB 都是"接你已有的 MCP server"**，REA 不启动引擎本身；**Kuna / objdump / rizin / gdb** 是 BYO 可执行文件，按 env 指定或走 PATH。
5. **mac arm64 让 Ghidra 能用的硬条件**：`GHIDRA_INSTALL_DIR`（绝对路径，或 PATH 上的 `ghidra`/`ghidraRun` 反查）指向 12.1.x；`Ghidra/Features/Decompiler/os/mac_arm_64/decompile` 存在且 Mach-O 头是 arm64；JDK 是 64 位完整 JDK（有 `javac`）、版本落在 `application.java.min/max`（12.1 = ≥21），且 JVM 报的 `os.arch`/`os.name` 必须是 aarch64/Mac（Rosetta 下的 x86_64 JDK 会被拒）。

---

## 1. 统一抽象（接口名、能力位）

| 层 | 名称 | 位置 |
|---|---|---|
| 工厂 + 能力声明 | `AnalysisProvider { identity(); capabilities(); resolveAnalysisProfile?(); createClient() }` | `src/application/AnalysisProvider.ts:289-301` |
| 可被发现的深度候选 | `AnalysisProviderCandidate extends AnalysisProvider`，加 `inspectAvailability()` / `inspectReadinessSnapshot?()` / `inspectTargetSupport()` / `closeOwnedResources?()` | `AnalysisProvider.ts:345-370` |
| 绑定单个 artifact 的会话 | `AnalysisClient extends AnalysisOperationPort`：`execute(operation, params, options)`、`executeIfReady?`、`reuseReadiness?`、`operationHealthSnapshot?`、`close()` | `AnalysisProvider.ts:163-169`, `190-211` |
| 能力描述 | `CapabilityDescriptor = { provider, operation, available, reason, availabilityCode?, cachePolicy?: "snapshot"\|"live", effects, limitations }` | `AnalysisProvider.ts:274-286` |
| **能力位（effects）** | `mutatesArtifact / launchesProcess / mayShowUi / mayAccessNetwork / mayWriteFilesystem / changesPermissions / requiresRoot` 七个布尔 | `AnalysisProvider.ts:252-260` |
| 可用性 | `ProviderAvailability`（available / unavailable + `ProviderRejectionCode`） | `AnalysisProvider.ts:304-316` |
| 惰性启动 | `DeferredAnalysisClient`：`createClient` 只返回壳，第一次 `execute` 才真正创建（共享启动尝试 `SharedStartupAttempt`） | `src/providers/DeferredAnalysisClient.ts:14-63`，`src/providers/AnalysisClientCreation.ts:69-129` |

各 provider 的 effects 声明（全部核对过）：

| provider id | mutatesArtifact | launchesProcess | mayShowUi | mayAccessNetwork | cachePolicy | 出处 |
|---|---|---|---|---|---|---|
| `ghidra` | 仅 `annotate_native_function`/`set_address_name(s)` 为 true | true | false | false | 非写操作 `snapshot`；写操作与 `list_documents` 不设 | `src/ghidra/GhidraProviderCapabilities.ts:192-217` |
| `hopper` | 6 个写操作为 true | true | **true** | false | 不设 | `src/hopper/HopperProviderCapabilities.ts:55-85` |
| `ida` | false（全只读） | stdio 或 headless 时 true | attached 模式的部分操作 true | **true** | `live` | `src/ida/IdaProviderCapabilities.ts:69-107` |
| `kuna` | false | true | false | false | `live` | `src/kuna/KunaProviderCapabilities.ts:117-138` |
| `native-macos` | false | 除 `inspect_native_dispatch_metadata` 外 true | UI/调用观测为 true | 同左 | — | `src/native/NativeMacOSProviderMetadata.ts:20-57` |

### 操作覆盖差异（哪些只有某个 provider 有）

- Ghidra 操作表 = inventory 17 个 + function 13 个（`src/ghidra/GhidraInventoryValues.ts:30-48`，`src/ghidra/GhidraFunctionValues.ts:47-61`）；`read_binary_inventory_summary` 对外映射成 `binary_overview`（`GhidraProviderCapabilities.ts:31-37`）。
- **只有 Ghidra 有**：`inspect_native_load_image`、`search_bytes`（Hopper 声明了但 `available:false`，`HopperProviderCapabilities.ts:66-75`）、`annotate_native_function`、`inspect_native_data_type`、`inspect_native_instruction`、`resolve_native_call_targets`、`read_binary_inventory_summary`。
- **只有 Hopper 有**（GUI 状态/书签/注释类）：`current_address`、`current_procedure`、`current_document`、`goto_address`、`next_address`、`prev_address`、`comment`、`inline_comment`、`set_comment`、`set_inline_comment`、`list_bookmarks`、`set_bookmark`、`unset_bookmark`（`HopperProviderCapabilities.ts:16-53`）。
- 两家都可写的：`set_address_name`、`set_addresses_names`。Ghidra 的写只改临时库，close 即丢（`GhidraProviderCapabilities.ts:40-44`）；Windows P0 下 Ghidra 写操作全部 `available:false`（`GhidraProviderCapabilities.ts:229-236`）。
- IDA：14 个只读操作（`src/ida/IdaProviderCapabilities.ts:14-29`）；`resolve_containing_procedure` 只有 headless 模式有（`:33-41`）；headless 或 native 协议下 `procedure_callers` 不可用（`:78-84`）。
- Kuna：13 个只读操作，无原始内存、注释、类型恢复（`src/kuna/KunaProviderCapabilities.ts:19-32`，`docs/kuna-provider.md:50`）。
- JEB 不走 `AnalysisProvider`，而是独立的 `JebAnalysisPort`（`src/application/jeb/JebAnalysisPort.ts:21`），4 个操作映射到上游 MCP 工具名（`src/jeb/JebMcpConnection.ts:24-29`）。
- native-macos：`observe_native_ui`、`capture_native_ui_scenario`、`observe_native_calls`(LLDB)、`inspect_macho`、`inspect_signature`、`inspect_plist`、`list_architectures`、`demangle_swift`、`inspect_native_dispatch_metadata`；非 darwin 全部 `unsupported_host`（`src/contracts/native/nativeToolContracts.ts:78-132`，`src/native/NativeMacOSProviderMetadata.ts:23-35`）。

---

## 2. 各 provider：发现 / 启动 / 操作 / 读写 / 版本

| provider | 发现安装 | 启动方式 | 读写 | 版本要求 |
|---|---|---|---|---|
| **Ghidra** | `GHIDRA_INSTALL_DIR`（必须绝对路径，`src/config/environment.ts:34-38`）；未设时在 PATH 找 `ghidra`/`ghidraRun`，realpath 后要求同目录有 `Ghidra/application.properties` + 可执行 `support/analyzeHeadless`（`src/ghidra/GhidraInstallation.ts:1004-1044`）；显式设置哪怕无效也优先（`:436-441`）。Java：`JAVA_HOME` → PATH 上的 `java` → mac 上 Homebrew `opt/openjdk/libexec/openjdk.jdk/Contents/Home/bin/java`（arm64 先 `/opt/homebrew`）（`:501-532`） | 子进程 JVM + Unix socket（Windows 用认证 loopback TCP）（`src/ghidra/GhidraProviderClient.ts:391-394`） | 只读为主，3 个会话内写操作（不落盘） | 验证版本 `12.1.4`，接受同 major.minor（12.1.x）；JDK 验证 21（`src/ghidra/GhidraReleasePolicy.ts:2-4`, `:36-49`），实际按 `application.java.min/max`（`src/ghidra/GhidraInstallationPolicy.ts:63-71`） |
| **Hopper** | `HOPPER_LAUNCHER_PATH` 优先（`src/config/parseConfig.ts:89`）；mac 依次 `/Applications/Hopper Disassembler.app/Contents/MacOS/hopper`、`~/Applications/...`、其他 `Hopper*.app`（`src/config/parseConfig.ts:25-34`，`src/application/MacHopperPaths.ts:8-33`）；Linux `/opt/hopper/bin/Hopper` 等（`docs/installation.md:663-666`） | GUI 进程 + 注入 Python bridge + Unix socket | 可写（GUI 文档里改名/注释/书签） | 只支持 darwin/linux（`src/hopper/HopperProvider.ts:75-81`）；mac 拒绝 `.hop` 数据库 target（`:118-125`）；代码里未见 Hopper 版本下限（未查到） |
| **IDA** | `REA_IDA_MCP_CONFIG` 指向 JSON（≤64 KiB，接受 `mcpServers["ida-pro-mcp"]` 包装）（`src/ida/IdaConfiguration.ts:57-90`） | 连上游 MCP：stdio（`command/args/env`）或本地 HTTP（只允许 localhost/127.0.0.1/[::1]）（`IdaConfiguration.ts:16-43`；`src/ida/IdaMcpConnection.ts:418-438`） | 只读（`mutatesArtifact:false`） | attached=上游 1.4.0 legacy 工具；headless=有 `idb_open/idb_list/idb_close` 的构建，按 `tools/list` 实测而不是版本号（`docs/ida-provider.md:7-20`）；默认请求超时 300000 ms（`IdaConfiguration.ts:10`） |
| **Kuna** | `REA_KUNA_COMMAND`（必填、绝对路径），只支持 darwin/linux（`src/kuna/KunaConfiguration.ts:111-121`）；另有 `REA_KUNA_CACHE_DIR/_CACHE_MAX_BYTES/_PROJECT_TIMEOUT_MS/_TIMEOUT_MS/_JOBS`（`:123-173`） | 一次性子进程：`kuna --version`（`src/kuna/KunaInstallation.ts:216`）、`kuna functions <in> --json`、`kuna decompile-project`（`src/kuna/KunaProjectExport.ts:247-264`） | 只读 | 验证 1.833，其他版本接受但标 unverified（`src/kuna/KunaRelease.ts:2`） |
| **JEB** | `REA_JEB_MCP_URL`，默认 `http://127.0.0.1:8425/mcp`；只允许 127.0.0.1/[::1]、路径必须 `/mcp`（`src/jeb/JebMcpEndpoint.ts:7-63`）；超时 `REA_JEB_MCP_TIMEOUT_MS`（`src/config/environment.ts:33`） | 连已运行的 JEB 引擎 MCP（streamable HTTP）（`src/jeb/JebProvider.ts:50`） | `open_project` 打开工程，其余只读（推断：未见写回工具） | 无版本门槛，按 `tools/list` 过滤（`JebProvider.ts:155`） |
| **objdump** | `REA_OBJDUMP_COMMAND`，否则 PATH 的 `objdump`（`src/composition/reverseEngineering.ts:18-27`） | 一次性子进程，子进程清空 `DEBUGINFOD_URLS`（`:26`） | 只读 | 未见版本检查 |
| **rizin** | `REA_RIZIN_COMMAND`，否则 `rizin`（`reverseEngineering.ts:28-36`；`src/rizin/RizinDebugSessionManager.ts:255`） | 一次性：`-N -q -e scr.color=0 -c <cmd> <path>`（`src/rizin/RizinCommand.ts:11-19`）；持久调试会话 `-d` + `dbg.exitkills=false`，仅 Linux（`RizinDebugSessionManager.ts:324`, `:373-379`） | 命令不受限，可能有副作用（`docs/reverse-engineering-tools.md:69`） | 未见版本检查 |
| **gdb** | `REA_GDB_COMMAND`，否则 `gdb`（`src/gdb/GdbSessionManager.ts:561`） | `--nx --quiet --interpreter=mi3`，失败回落 mi2（`:695-706`, `:729`）；仅 Linux（`:604-610`） | 不受限 console | 未见版本检查 |
| **Cutter** | 用户自己把 `bridge/cutter/rea_bridge.py` 装进 Cutter 插件目录；描述符目录 `REA_CUTTER_BRIDGE_DIR`，默认 `~/.cache/rea/cutter-bridge`（`bridge/cutter/rea_bridge.py:308-315`；`src/cutter/CutterBridgeClient.ts:293-325`） | 插件在 Cutter 内监听 `127.0.0.1:0`，`rea-auth-v2` HMAC 握手（`rea_bridge.py:473-475`, `:551-561`）；REA 不启动 Cutter | 不受限（`cutter.cmd/cmdj`，`:420`） | 只接受 v2 握手（`docs/reverse-engineering-tools.md:75-77`） |
| **pwntools** | `REA_PWNTOOLS_PYTHON`（绝对路径），仅 Linux x64（`src/native/pwntools/PwntoolsDecoder.ts:237-262`） | 子进程跑 `bridge/pwntools/*.py`（`src/native/pwntools/PwntoolsRecordedCrashProvider.ts:46`） | 只读离线解析 | 钉死 `pwntools@4.15.0;pyelftools@0.33;unicorn@2.1.2`（`bridge/pwntools/recorded_crash_impl.py` 头部 `PROFILE`） |
| **pwndbg** | `REA_PWNDBG_GDB`（默认 `/usr/bin/gdb`）、`REA_PWNDBG_GDBINIT`、`REA_PWNDBG_VENV_PATH`（`src/native/pwndbg/PwndbgCoreContext.ts:75-95`） | gdb 跑固定的 `bridge/pwndbg/core_context.py`，只读 core | 只读 | 未见版本检查 |
| **native-macos** | `/usr/bin/xcrun --find` 找 Xcode 工具（`src/native/CommandRunner.ts:360-370`），lipo/codesign/LLDB 等 | 一次性子进程；`observe_native_calls` 用 LLDB 拉起目标（`bridge/native/rea_lldb_tracer.py` 头注释） | 只读（但调用观测会运行目标） | 仅 darwin |

---

## 3. Ghidra 逐层拆解

### 3.1 一次 `procedure_pseudo_code`（decompile）的完整调用链

大白话：`open_binary` 只是选定 target，不起 JVM；第一个真正的查询才触发"建私有目录 → 复制 target 快照 → 起 JVM 导入并 auto-analysis → 等 socket → ping 握手"，之后所有请求串行走同一个 socket。

| # | 层 | 动作 | file:line |
|---|---|---|---|
| 1 | Provider | `createClient` 返回 `DeferredAnalysisClient`，真正的创建延后 | `src/ghidra/GhidraProvider.ts:471-500` |
| 2 | Deferred | 首次 `execute` → `AnalysisClientCreation.acquire` → `createGhidraProviderClient` | `src/providers/DeferredAnalysisClient.ts:40-54` |
| 3 | 装配 | 构造 `GhidraClient` + `GhidraHeadlessLauncher`（bridge 路径 `bridge/ghidra/ReaGhidraBridge.java`；非 Windows 用 `unix-socket`） | `src/ghidra/GhidraProviderClient.ts:343-400` |
| 4 | 会话 execute | 校验 operation 属于 Ghidra 表 → 解析入参 → `client.callTool` | `GhidraProviderClient.ts:591-655` |
| 5 | GhidraClient | `callTool` 先 `start()`，再进串行队列 | `src/ghidra/GhidraClient.ts:365-380` |
| 6 | 启动预算 | `#start` 建 `ProviderStartupDeadline(startupTimeoutMs)` | `GhidraClient.ts:762-786` |
| 7 | 私有目录 | `PrivateRuntimeRoot.create(parent=SESSION_ROOT, prefix "rea-ghidra-")`；SESSION_ROOT 会避开带 `.` 的路径元素（Ghidra 拒绝），回落 `/tmp` | `GhidraClient.ts:805-809`，`src/ghidra/GhidraSessionRoot.ts:40-55` |
| 8 | endpoint | `<runtime>/bridge.sock`；路径超过 sockaddr 限制（mac 103 字节）就改到 `/tmp/rea-ghidra-socket-*/s` | `src/ghidra/GhidraTransport.ts:34-62` |
| 9 | target 快照 | 把 target 复制成私有快照并按 sha256 准入 | `GhidraClient.ts:834-862` |
| 10 | token | `randomBytes(32).toString("hex")` + runId | `GhidraClient.ts:864-867` |
| 11 | Launcher | 写 `session.json`（transport/endpoint/token/run_id/target_sha256/provider_version/profile_digest） | `src/ghidra/GhidraLauncher.ts:342-357` |
| 12 | headless 参数 | `<proj> rea-project -import <snapshot> [loader/processor] -readOnly -deleteProject -log -scriptlog -scriptPath <bridge dir> [-preScript ...] -postScript ReaGhidraNoReturnFix.java [-postScript MipsSoftFloat] -postScript <ReaGhidraBridge.java> <session.json>` | `GhidraLauncher.ts:699-866` |
| 13 | LaunchSupport | `java -Duser.home=<私有> -Djava.io.tmpdir=<私有> -cp support/LaunchSupport.jar LaunchSupport <install> -envvars`，再 `-vmargs`；拒绝任何 `-Dos.arch/-Dos.name` 覆盖 | `src/ghidra/GhidraJavaLaunch.ts:95-181` |
| 14 | 最终 JVM argv | `<vmargs> [-Xdock:name=Ghidra-Headless(mac)] -Xmx<堆> -XX:ParallelGCThreads=2 -XX:CICompilerCount=2 -Djava.awt.headless=true ... -cp Ghidra/Framework/Utility/lib/Utility.jar ghidra.Ghidra ghidra.app.util.headless.AnalyzeHeadless <步骤12>` | `GhidraJavaLaunch.ts:182-201` |
| 15 | spawn | POSIX 直接 exec 这个 JVM（不经 shell 脚本，便于进程组归属校验）；Windows 才走 `analyzeHeadless.bat` | `GhidraLauncher.ts:449-469` |
| 16 | 连接 | 每 50 ms 轮询 endpoint 是否出现，进程提前退出则立即失败 | `src/ghidra/GhidraSocketConnection.ts:53-103` |
| 17 | 握手 | `ping` → 校验 run_id / read_only / capabilities / provider.version / profile_digest / target.sha256；`analysis_timed_out` 为真则报 `analysis_timeout` | `src/ghidra/GhidraClientStartup.ts:28-58`，`src/ghidra/GhidraSessionValues.ts:163-175` |
| 18 | 发请求 | 串行队列 → `GhidraWire.request` 写一行 `{"id","token","method","params"}\n` | `src/ghidra/GhidraRequestQueue.ts:45-106`，`src/ghidra/GhidraClientWire.ts:35-98` |
| 19 | Java 入口 | `run()`：读并删除 `session.json`（POSIX）、校验 Ghidra 版本与导入字节 sha256、`end(true)` 结束脚本外层事务、`initializeDecompiler()`、`serve()` | `bridge/ghidra/ReaGhidraBridge.java:185-248` |
| 20 | socket 服务 | `ServerSocketChannel.open(UNIX)` bind 后 chmod `rw-------`，**只 accept 一个客户端** | `ReaGhidraBridge.java:262-273`, `:360-372` |
| 21 | 逐行处理 | `readLine` → `parseRequest`（常量时间比 token）→ `shutdown` 特判 → `handleRequest` | `ReaGhidraBridge.java:374-416`, `:4727` |
| 22 | 分发 | analysis 超时后除 ping 外一律 `analysis_incomplete`；`procedure_pseudo_code` → `procedurePseudocode` | `ReaGhidraBridge.java:418-480` |
| 23 | 反编译器 | 会话级唯一 `DecompInterface`，`setMaxInstructions(Integer.MAX_VALUE)`，开 C/语法树/JumpLoads | `ReaGhidraBridge.java:917-928` |
| 24 | 反编译 | `decompiler.decompileFunction(function, 0, monitor)`（timeout=0 即不限时）；外部函数/空 body 返回 null；取消→`decompile_cancelled`，失败→`decompile_failed` | `ReaGhidraBridge.java:930-940`, `:3351-3378` |
| 25 | 回包 | `{"id","ok":true,"result"}` 或 `{"id","ok":false,"error":{code,message}}`，TS 侧 strict schema 校验 | `src/ghidra/protocol.ts:6-61` |

取消语义：正在执行的请求被取消 → 队列里排队的全部失败 → `#stopAfterActiveCancellation` 强制关闭整个会话（`GhidraRequestQueue.ts:123-135`，`GhidraClient.ts:696-707`）；下次查询会重新导入（`GhidraProviderCapabilities.ts:43`）。正常关闭发 `shutdown`，超时 10 s（`GhidraClient.ts:107`, `:1056`）。

### 3.2 启动超时、堆、CPU

- 启动总预算（导入 + auto-analysis + bridge + ping）默认 **330 000 ms**，`REA_GHIDRA_STARTUP_TIMEOUT_MS` 可调，1..2147483647（`src/config/providerStartupTimeout.ts:4`, `:17-21`；`src/config/environment.ts:49-51`）。运行中单个请求**没有**固定超时（`docs/installation.md:1010-1013`；`ReaGhidraBridge.java:3355` timeout=0）。
- 堆：`GHIDRA_HEADLESS_MAXMEM` → `GHIDRA_MAXMEM` → `2G`，作为 `-Xmx`（`GhidraJavaLaunch.ts:157-158`, `:189`）。这两个变量是从 LaunchSupport `-envvars` 合并后的环境里取的，调用方环境里的值优先（`:144-156`）。
- CPU：只固定 `-XX:ParallelGCThreads=2`、`-XX:CICompilerCount=2`（`GhidraJavaLaunch.ts:190-191`）；不传 Ghidra `-max-cpu`，没有通用 CPU 限制（`docs/installation.md:1156-1162`）。
- 被清空的注入变量：`_JAVA_OPTIONS`、`JAVA_TOOL_OPTIONS`、`JDK_JAVA_OPTIONS`、`GHIDRA_JAVA_OPTIONS`（`src/ghidra/GhidraInstallation.ts:721-744`），launcher 还清 `GHIDRA_HEADLESS_JAVA_OPTIONS`（`GhidraLauncher.ts:974-982`）。
- 其他 Ghidra env：`REA_GHIDRA_LANGUAGE_ID` / `REA_GHIDRA_COMPILER_SPEC_ID`（后者依赖前者）、`REA_GHIDRA_DISABLED_ANALYZERS`、`REA_GHIDRA_SEED_FILE`、`REA_GHIDRA_NATIVEAOT_JAR`（`src/config/environment.ts:44-65`, `:181-190`）。

---

## 4. Hopper（macOS）注入与 lease

大白话：REA 不"连"Hopper，而是让 Hopper 开文档时顺手执行 REA 的 Python 脚本；脚本在 Hopper 的 Python 线程里开一个只给当前用户的 socket。REA 只拥有那个短命的 `hopper` 启动器进程组，不拥有 GUI 本体，所以关会话不等于关 Hopper。

1. **启动器发现**：见第 2 节表格；可用性只检查启动器是可执行的普通文件（`src/hopper/HopperProvider.ts:82-102`）。
2. **冷启动**：Hopper 没在跑时先 `/usr/bin/open --hide --background -a <App>`，等 5 s（`src/hopper/BridgeLauncher.ts:43`, `:574-595`）；已在跑（`ps -ax` 精确匹配可执行路径）就跳过（`:603-621`）。官方 `hopper` 助手内部会发 AppleScript `activate`，窗口仍可能被拉到前台（`:131-144`）。
3. **注入**：在私有 runtime 目录写 `bootstrap.py`（0600），内容是 `REA_SOCKET/REA_TOKEN/REA_RUN_ID/REA_TARGET_PATH/REA_OWNS_PROCESS_LIFETIME/REA_RECONNECT_TIMEOUT_SECONDS=30` 再 `exec(compile(open(hopper_bridge.py)...))`（`BridgeLauncher.ts:393-417`, `:451-466`；30 s 来自 `src/hopper/HopperSessionValues.ts:6`）。启动参数 `<loaderArgs> --analysis -Y <bootstrap.py> --executable <target>`（database 用 `--database`）（`BridgeLauncher.ts:240-249`）。`-Y` 对应 Hopper 的 Python 脚本选项——bridge 文档串写的是 "Hopper's supported `--python` launcher option"（`bridge/hopper_bridge.py:3-4`），`-Y` 即其短形式属**推断**。非成功路径一律删掉带 token 的 bootstrap（`BridgeLauncher.ts:343-349`）。
4. **mac 原生助手签名**：`codesign --verify --strict -R<Developer ID 要求>`，5 s 超时，结果只影响诊断（`src/hopper/HopperMacPublisher.ts:6-31`，`BridgeLauncher.ts:275-281`）。
5. **bridge 侧**：`AF_UNIX` bind `REA_SOCKET`、chmod 0600、`listen(1)`、accept/重连各 30 s 超时（`bridge/hopper_bridge.py:1177-1204`）；NDJSON，每行严格 `{"id","token","method","params"}`，`hmac.compare_digest` 比 token（`:1057-1088`）；所有 Hopper API 调用必须留在 Hopper 的 Python 线程，换线程会死锁（`:1-6`, `:1207-1211`）。
6. **TS 侧握手**：`<runtime>/bridge.sock` + 32 字节 token（`src/hopper/HopperClient.ts:420-426`），写 `ownership.json`（`:450-464`），连接后发 `health` 校验 run_id（`:469-492`）。启动预算默认 120 000 ms，`REA_HOPPER_STARTUP_TIMEOUT_MS`（`src/config/providerStartupTimeout.ts:7`）。
7. **lease / 单实例规则**：
   - macOS：对"同一 target + kind + loaderArgs"加 lease。key = `sha256(JSON(identity))[:32]`，lease 本体是 `/tmp/rea-hopper-<uid>/<key>.sock` 上的一个 unref 的 Unix server（目录 0700、socket 0600），活着就占着，进程退出留下 stale socket（`src/process/HopperLease.ts:30-62`, `:86-99`, `:148-183`）。target 路径必须先 `realpath`，避免别名绕过（`:43-53`）。
   - 抢不到时读对方 owner（runId+pid）报错 "already open in REA session …"（`BridgeLauncher.ts:373-382`）；ECONNREFUSED 视为 stale，经 `<key>.sock.recovery` 目录串行回收后重试（`HopperLease.ts:102-128`, `:330`）。
   - Linux demo：不是按 target，而是**每用户只能有一个 Hopper 应用**（`acquireLinuxHopperApplicationLease`，`HopperLease.ts:65-69`；`BridgeLauncher.ts:356-364`）。
   - mac 上关闭 REA 会话只释放 lease，Hopper 文档仍开着；REA 不能 attach 已开文档（`docs/installation.md:545-553`）。

---

## 5. macOS arm64 本机让 Ghidra 能用

### 5.1 doctor 的检查项（Ghidra）

检查名枚举：`configuration / platform / architecture / installation / version / headless / native_decompiler / java`（`src/ghidra/GhidraInstallation.ts:187-195`），生成顺序见 `installationChecks`（`:576-629`）。doctor 投影时：configuration 失败只报这一项；installation 通过才继续报 version/headless/native_decompiler；java 总是报（`src/ghidra/GhidraDoctor.ts:94-120`）。Windows 额外加 `native_authority`（`GhidraDoctor.ts:46-64`）。

| 检查 | 通过条件 | file:line |
|---|---|---|
| configuration | `GHIDRA_INSTALL_DIR`（或 PATH 反查）存在，且它与 `JAVA_HOME` 都是绝对路径 | `GhidraInstallation.ts:635-660` |
| platform | `darwin` 在 `NATIVE_PLATFORMS` 中 | `:56-58`, `:584-591` |
| architecture | `darwin/arm64 → mac_arm_64` | `:57`, `:592-598` |
| installation | `Ghidra/application.properties` 可读 | `:442-445`, `:599-608` |
| version | `application.version` 属 12.1.x（12.1.4 = verified，其余 compatible） | `:903-921`，`src/ghidra/GhidraReleasePolicy.ts:36-49` |
| headless | `support/analyzeHeadless` 可执行 | `GhidraInstallation.ts:446-453`, `:610-619` |
| native_decompiler | `Ghidra/Features/Decompiler/os/mac_arm_64/decompile`（或 `build/os/mac_arm_64/decompile`）可执行；读文件头（≤4096 字节）是 Mach-O arm64；JVM 报的 `os.arch` 映射到同一目录、`os.name` 匹配 darwin | `:454-480`, `:99-158`, `:768-864` |
| java | 版本在 `application.java.min/max` 内、64 位、是 JDK（`<home>/bin/javac` 存在） | `:925-971`；JDK 判定 `src/ghidra/GhidraJavaProbe.ts:365-373` |

Java 探针：`java -XshowSettings:properties -version`（附带 UTF-8 与 `-XX:-UsePerfData`），5 s 超时（`src/ghidra/GhidraJavaProbe.ts:154-172`）。

### 5.2 本机 checklist

1. 解压 Ghidra 12.1.x，`export GHIDRA_INSTALL_DIR=/绝对路径`（或把 `ghidraRun` 放进 PATH，REA 会 realpath 反查；Homebrew Cellar 路径会稳定成 `opt/<formula>/libexec`，`GhidraInstallation.ts:1050-1080`）。
2. arm64 原生 JDK 21+：Homebrew `openjdk` 是 keg-only，设 `JAVA_HOME=/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home`；不设时 REA 也会自己找这个路径（`GhidraInstallation.ts:517-531`）。**不能用 Rosetta 下的 x86_64 JDK**（`:791-833`）。
3. 补 `mac_arm_64` 反编译器：官方 Makefile 在 Darwin 钉 `-arch x86_64`，用 `make ghidra_opt ARCH_TYPE="-arch arm64" OSDIR=mac_arm_64` 编，`install -m 755 ghidra_opt .../os/mac_arm_64/decompile`；JDK 不参与编译；工具链拒 `-mmacosx-version-min=10.6` 时加 `ADDITIONAL_FLAGS="-w"`（`docs/installation.md:1043-1068`）。REA 不编译、不改 Gatekeeper（`docs/installation.md:836-840`；`GhidraProviderClient.ts:331-334`）。
4. `rea doctor --json` 看到 `native_decompiler` 报 "reports mach-o arm64, which matches darwin/arm64"（文案来源 `GhidraInstallation.ts:146-150`）。
5. 同时装着 Hopper 时，用 `--provider ghidra` / `REA_ANALYSIS_PROVIDER=ghidra`，否则 auto 会 `ambiguous`（见一页结论第 1 条）。
6. TMPDIR 带 `.` 开头的路径元素会被自动绕开（`GhidraSessionRoot.ts:40-55`），无需手动处理。

---

## 6. `bridge/` 目录速查

| 路径 | 宿主 | 作用 |
|---|---|---|
| `bridge/ghidra/ReaGhidraBridge.java` | Ghidra HeadlessScript（postScript） | 主 bridge，Unix socket/loopback TCP 服务 |
| `bridge/ghidra/ReaGhidraPrepareCom.java` / `PrepareRaw` / `ApplySeeds` / `AnalysisOptions` | preScript | DOS COM、raw binary 入口准备、种子、禁用 analyzer（`src/ghidra/GhidraBridgeSources.ts:2-12`；参数顺序 `GhidraLauncher.ts:812-846`） |
| `bridge/ghidra/ReaGhidraNoReturnFix.java` / `MipsSoftFloat` | postScript | analysis 后修正，非 Windows 必跑 NoReturnFix（`GhidraLauncher.ts:847-859`） |
| `bridge/ghidra/ReaGhidraExtensions.java` / `BundleActivator` | Ghidra | 分析扩展（如 NativeAOT），Windows P0 禁用（`ReaGhidraBridge.java:235-237`） |
| `bridge/nativeaot/*.java` | Ghidra 扩展 | NativeAOT 元数据恢复（`NativeAotExtension.java:22-23`），jar 由 `REA_GHIDRA_NATIVEAOT_JAR` 指定 |
| `bridge/hopper_bridge.py` | Hopper 内嵌 Python | Hopper bridge |
| `bridge/cutter/rea_bridge.py` | Cutter 插件 | loopback + HMAC v2 |
| `bridge/android/ReaJadxBridge.java` | jadx-headless-mcp 0.7.1 JAR 边界 | Android 元数据（`ReaJadxBridge.java:27`） |
| `bridge/native/rea_lldb_tracer.py` | `lldb --batch` 内嵌 Python | `observe_native_calls` |
| `bridge/native/ReaNativeUI.swift` / `NativeUIChildren.swift` | swift | 原生 UI 观测 |
| `bridge/process/ProcessRunTokenReader*.swift` | swift | Darwin 进程 run-token 归属校验 |
| `bridge/pwntools/*.py` | caller 的 Python | 离线 core/layout 解析 |
| `bridge/pwndbg/core_context.py` | gdb + pwndbg | 固定只读 core 上下文 |
| `bridge/mitmproxy/capture.py` | mitmdump | 离线 tnetstring 解码，不起代理（头注释） |

## 7. 没查 / 不确定

- Hopper 最低版本要求：代码里没找到版本门槛（只校验启动器可执行、mac 签名仅作诊断）。
- `ProviderRejectionCode` 全集、`AnalysisProviderRegistry` 显式选择分支的细节没有展开读。
- Ghidra `analysisTimeoutOccurred()` 的定义在 `ReaGhidraBridge.java` 中只看到调用（`:424`, `:483`），没在本文件 grep 到定义，可能来自 `HeadlessScript` 父类（**推断**）。

