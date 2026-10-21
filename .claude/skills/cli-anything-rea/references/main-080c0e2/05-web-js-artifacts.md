# 05 · Web / JS / Electron / 产物层（REA main @ 080c0e2）

> 只读核对源码 `/Users/Liuguoxian/codes/rea`，所有 `file:line` 均相对该仓库根目录。没跑 build/test，也没执行任何命令（只用 grep/sed 读源码）。
> 标「推断」的是我的推断，其余每条都对着源码核实过。

## 0. 一页结论

- **静态分析 Electron 应用不执行目标代码。** 主进程先做清单（逐文件 sha256），再把每个 JS 文件交给一个**独立的限堆 Node 子进程**，用 `@babel/parser` 解析。IPC、preload、contextBridge、`.node` 原生插件都是从 AST 上的语法模式识别出来的，最后拼成 application graph + semantic graph，作为 Evidence 输出。
- **浏览器观测默认只 attach，不启动浏览器。** 只接受字面量 loopback 的 CDP 端点。被动抓取会开启 Runtime/Debugger/Network 这几个 domain，读 DOM、脚本、网络元数据、截图。只有 `observe_web_execution` 会开 V8 precise coverage，这一步会**改变目标的 JIT/时序**。Playwright 场景模式才会自己启动 Chrome（临时 profile）。
- **Electron 主动观测会往目标进程注入代码。** 它用 `electron -r scripts/electron-active-hook.cjs` 在应用代码之前加载 hook，monkey-patch `ipcMain`、`webContents`、`session`、`shell`、`child_process`、`process.dlopen` 等，记录事件的「形状」，同时**阻断**外部导航、shell、权限、下载、弹窗、协议注册等副作用。被动的 `inspect_electron_page` 不注入。
- **source map 放在独立进程解码**，是为了让不可信的 map 被硬上限框住：192 MiB 堆、20 s 超时、4 MiB 输入、32 MiB 输出。爆了只会让子进程失败，CLI/MCP 主进程活着，并返回明确的 `limit` 错误。
- **不可信输入的防线**有几层：路径规范化（拒绝绝对路径、`..` 和 NUL）；`O_NOFOLLOW` + `realpath` 防止逃逸；ZIP 解压预算按 `max(256 MiB, 500×压缩包大小)` 计；ASAR 头上限 16 MiB；逐成员 sha256 + 长度复核；输出树用 `O_EXCL` 独占创建、权限 0600/0700。

---

## 1. `analyze-javascript-application <dir|asar>` 完整流程

### 1.1 调用链（CLI → 输出）

| 步 | 位置 | 做什么 |
|---|---|---|
| CLI 注册 | `src/cli/electronCommands.ts:271-299` | 参数 `path`（ASAR 或解包目录），选项 `--max-semantic-nodes*`、`--max-semantic-unknowns*` 等 |
| CLI 边界 | `src/cli/javascriptApplicationAnalysis.ts:11-52` | 新建 `ArtifactResourceScope`，进度以 `{"rea_progress":…}` 写 stderr（`:19`），结束时 `resources.close()`，清理失败则包成 `ProviderCleanupError` |
| MCP 入口 | `src/server/registerElectronTools.ts:109` | 同一个 `analyzeJavaScriptApplicationValidated` |
| 输入 schema | `src/domain/javascript/javascriptApplicationAnalysis.ts:28-34` | `input_path`、`format: auto/asar/directory`、`integrity_policy`、`source_paths?`，加上资源控制项 |
| Service | `src/application/javascript/JavaScriptApplicationService.ts:47-108` | 依次：reconstruct → 校验结果/跨图绑定 → 生成并哈希 Evidence；取消时保留部分 Evidence（`:110-140`） |
| 重建主体 | `src/application/javascript/JavaScriptArtifactReconstruction.ts:73-300` | 见下 |

### 1.2 重建主体（`JavaScriptArtifactReconstruction.ts`）

1. **定位输入**：`realpath` + `access(R_OK)`（`:349-353`）。格式由 `lstat` 判断：目录对应 `directory`，`.asar` 结尾的普通文件对应 `asar`，其余都是 `unsupported_target`（`:374-386`）。
2. **清单扫描**：`scanCanonicalArtifactInventoryInScope`（`:96-100`）遍历全部条目并逐个算 sha256（`src/artifacts/inventory/scanReader.ts:189-240`）。每个逻辑路径先过 `normalizeArtifactPath`，再登记进 `ArtifactPathRegistry`，拦截前缀冲突（`scanReader.ts:189-197`）。清单的 `root_format` 必须与第 1 步一致（`:101-105`）。
   - **目录里的 `.asar` 会被展开**：前提是 adapterKey 为绝对路径（即它是真实的文件系统文件），并且完整性没有矛盾（`scanReader.ts:242-258`、`:344-347`）。ASAR 里嵌套的 ASAR，adapterKey 是相对路径，不再展开，所以不存在无限递归（核实于 `isExpandableAsar`）。
3. **读文件**：`readJavaScriptArtifactFiles`（`:124-130` → `src/artifacts/javascript/JavaScriptArtifactFiles.ts:66`）。
   - 文件分类：`package.json`、`.json`、JS/TS、`.html?`、`.map`、`.node`（`JavaScriptArtifactFiles.ts:609-619`）。
   - 每个文件**重新读一遍字节**，与清单记录的 sha256 和长度比对，不一致就报 `integrity`（`:347-357`）。文本用 `TextDecoder(fatal:true)` 解码，非 UTF-8 只计数、不纳入（`:358-375`）。
   - `.node` 原生插件**只校验哈希，不读取文本**（`:339-345`）。
   - 指定了 `source_paths` 时，范围外的文件只校验、不解码（`:327-337`）。
   - 读完后立刻释放 reader（`JavaScriptArtifactReconstruction.ts:131-142`）。
4. **解析与分析（隔离子进程）**：`createJavaScriptSourceAnalysis` → `new JavaScriptAnalysisWorker`（`src/artifacts/javascript/JavaScriptSourceAnalysis.ts:8-12`）。逐文件调用 `worker.analyze`（`src/application/javascript/JavaScriptArtifactAnalysis.ts:176`）。
   - 子进程启动参数：`process.execPath --max-old-space-size=<heapMb> --max-semi-space-size=8 --v8-pool-size=1 --expose-gc --stack_size=2048 <#javascript-analysis-worker>`。同时删掉 `NODE_OPTIONS`，设 `UV_THREADPOOL_SIZE=1`（`src/javascript/analysis/JavaScriptAnalysisWorker.ts:521-536`）。
   - 源码以 `${id}.source` 写入私有目录（`wx`、0600）后交给子进程（`JavaScriptAnalysisWorker.ts:240-244`）。stdin 只传一个 JSON 请求帧，内容是 sha256 + 元数据（`:247-262`）。
   - 子进程入口：`src/composition/javascriptAnalysisWorkerProcess.ts:19-40`，执行 `parseJavaScriptSource` → `analyzeParsedJavaScriptStaticSourceSteps` → semantic 投影。
   - 注释写明「hard worker failure does not terminate this process」（`JavaScriptAnalysisWorker.ts:195`）。
5. **建图**：application graph（`buildImmutableJavaScriptArtifactGraphSteps`，`JavaScriptArtifactReconstruction.ts:232-243`）、semantic graph（`:248-257`）、Electron 边界摘要（`summarizeElectronBoundaries`，`:271-278`）。统计字段包括 relevant_files、nested_asar_containers、text_bytes_read、visited_ast_nodes、parse_failures 等（`:279-293`）。

### 1.3 解析器：是 `@babel/parser`

- `src/domain/javascript/javascriptSourceParser.ts:1`：`import { parse } from "@babel/parser"`，版本锁定为 `8.0.4`（`package.json:324`）。
- `parseJavaScriptSource`（`:186-221`）使用的选项：`errorRecovery:true`、`attachComment:false`。插件按扩展名选（`:95-113`）：`.js/.jsx/.mjs/.cjs` 只开 `jsx`，TS 系开 `typescript`，并且始终开 `decorators-legacy`。`.cjs` 先按 unambiguous 解析，有错误再回退到 commonjs（`:198-215`）。解析抛异常时返回 `null`，记为 parse failure（`:216-220`）。
- 只解析，不求值。整个流程里没有 `vm`/`eval`（推断：基于本节所读路径；全仓 grep 没做穷尽）。

### 1.4 Electron 语义识别（全部基于 AST 语法模式）

入口是 `inspectElectronStaticNode`（`src/domain/javascript/electronStaticAnalysis.ts:12-41`）。它在共享的 AST 遍历中对每个节点调用（`src/domain/javascript/javascriptStaticAnalysis.ts:143-145`、`:278`）：

| 识别项 | 位置 | 规则 |
|---|---|---|
| IPC | `electronStaticAnalysisIpc.ts:178-242` | `ipcRenderer.send/sendSync/invoke/postMessage/sendToHost/on/once`、`ipcMain.on/once/handle/handleOnce` 等，映射成 side/operation/mode；之后做 renderer↔main 配对（`src/application/javascript/ElectronBoundaryAnalysis.ts:32-84`） |
| BrowserWindow / preload / webPreferences | `electronStaticAnalysisBrowser.ts:40-160` | `new BrowserWindow(opts)`，静态解析 `webPreferences`，必要时延后到 `finishElectronBrowserWindowsSteps` 处理 |
| contextBridge | `javascriptContextBridgeExposure.ts:18` | `exposeInMainWorld` 等 |
| 原生插件 | `electronStaticAnalysisNative.ts:39-50`、`:284` | `import`、`export … from`、`require` 绑定、re-export 中以 `.node` 结尾的说明符 |
| `app.setAppPath` | `electronStaticAnalysis.ts:24-40` | 包括 call/apply/bind 和可能逃逸的 setter |

输出的计数字段见 `electronBoundarySummarySchema`（`javascriptApplicationAnalysis.ts:37-60`）：browser_windows、preload_entrypoints、context_bridge_apis、ipc.*、native_addon_bindings 等。

### 1.5 资源上限（analyze-javascript-application）

| 上限 | 默认值 | 位置 |
|---|---|---|
| 子进程 V8 old-space | 1024 MiB（可调 128~16384） | `javascriptAnalysisResourceControls.ts:7`、`:83-91` |
| 每文件超时（启动+分析+传输） | 300000 ms | `:8`、`:92-100` |
| semantic 节点 总 / 每文件 | 100000 / 20000（上限 1e6） | `:9-16` |
| semantic unknown 总 / 每文件 | 100000 / 20000 | `:11-12` |
| worker 控制帧 | 64 KiB，队列 ≤16 帧 | `JavaScriptAnalysisWorkerProtocol.ts:15`、`JavaScriptAnalysisWorker.ts:122-139` |
| 单条传输记录 | 8 MiB；单文件传输 ≤2 GiB | `JavaScriptAnalysisTransfer.ts:24-28`、`:43` |
| 父进程结果预算 | `(heap_size_limit − heapUsed − 24×8MiB)/2` | `JavaScriptAnalysisWorker.ts:184-192` |
| ASAR 头 | 16 MiB | `src/artifacts/AsarHeader.ts:9`、`:347-350` |
| ASAR 列表（展开路径） | 64 MiB | `src/artifacts/AsarListing.ts:8`、`:84` |
| Bun 宿主 | Windows 用 Job Object 限 heap+512 MiB，其他平台无限 | 描述在 `javascriptAnalysisResourceControls.ts:90` |

超过结果预算时**保留已得到的静态事实**，并在 limitation 里写明「这是父进程结果预算，单加 worker heap 没用」（`JavaScriptAnalysisWorker.ts:365-420`）。

**没找到的上限（需注意）**：
- `analyze-javascript-application` 没有条目数上限。`maximumEntries` 只在 `DesktopApplicationReader` 里使用（`src/artifacts/DesktopApplicationReader.ts:217`）。
- 父进程读单个 JS 文件时，`readAll` 把整个文件 Buffer 化，没有大小上限（`JavaScriptArtifactFiles.ts:581-594`）。推断：一个超大的单 JS 文件会先占用父进程内存，然后才可能在 worker 侧因堆或预算失败。

---

## 2. 浏览器观测

### 2.1 怎么找 / 启动 Chrome

- **被动 CDP（`list-browser-targets`、`inspect-web-page`、`analyze-web-bundle`、`observe-web-session`、`capture-web-screenshot`、`observe-web-execution` 等）**：REA **不找、不启动、不关闭**浏览器，由用户自己用 `--remote-debugging-port` 和独立 `--user-data-dir` 启动（`docs/browser-observation.md:94-108`）。
- 端点必须是字面量 loopback：`isLiteralLoopbackHostname` 只认 `127.0.0.1`、`[::1]`、`::1`（`src/domain/browserObservation.ts:47-58`）。`localhost`、局域网地址、https/wss 都会被拒（`docs/browser-observation.md:170-183`）。HTTP 端点走 `/json/version` 发现（`src/browser/CdpEndpoint.ts:105`），读取时不跟随重定向（`CdpEndpoint.ts:432`）。也接受 `ws://127.0.0.1:PORT/devtools/browser/ID` 形式（`browserObservation.ts:136`）。
- CDP 单条 WebSocket 消息上限 64 MiB（`src/domain/webRuntime.ts:90`、`src/browser/CdpConnection.ts:71-74`）。命令超时 20 s，清理超时 5 s（`webRuntime.ts:93-94`）。
- **Playwright 场景（`capture_browser_scenario`）**：只有这里会启动 Chrome。用的是 `playwright-core`（`1.58.2`，`package.json:353`）的 `chromium.launchPersistentContext`，可执行文件由调用方的 `executable_path` 显式指定，REA 不自动搜索（`src/browser/PlaywrightScenarioBrowser.ts:704-745`）。profile 放在 `mkdtemp(tmpdir()/rea-browser-scenario-)`，权限 0700，用完 `rm -rf`（`:715-717`、`:747-763`）。启动参数 `acceptDownloads:false`，环境变量经 allowlist 过滤（`:722`、`:733`）。也支持 `mode:"connect"` attach 到外部浏览器（`:706-713`）。

### 2.2 抓什么

- `inspect_web_page`：先 `Page.enable` 并确认主框架 origin 授权，然后 `Runtime.enable`、`Debugger.enable`、`Network.enable`（`src/browser/CdpPageCapture.ts:243-258`、`:386-394`）。之后抓 `Page.getResourceTree`（`:131`）、`DOMSnapshot.captureSnapshot`（`:273-274`）、`Accessibility.getFullAXTree`（`:404-408`），以及按需的 `Network.getResponseBody`（`:200-239`）。脚本源码靠 `Debugger.scriptParsed` / `getScriptSource` 获取，属于可选项。
- 默认数据最小化：DOM 只保留节点类型、名字、值长度、属性名，不保留文本和属性值；无障碍文本、console 文本、JSON/WS 形状、脚本源码、storage 键名都需要分别 opt-in（`docs/browser-observation.md:234-246`、`:279-300`）。
- 截图：`Page.captureScreenshot {format:"png", fromSurface:true, captureBeyondViewport:false}`，只截可见视口（`src/browser/CdpScreenshot.ts:77-80`）。
- **执行覆盖率与来源归因（`observe_web_execution`）**：依次开启 Page/Runtime/Debugger/Network/Profiler（`src/browser/execution/CdpExecutionCapture.ts:25-26`），然后 `Profiler.startPreciseCoverage {callCount:true, detailed:true}`（`:30-36`）。窗口结束后做一次 `takePreciseCoverage`（会重置计数器，`:62-65`），接着 `stopPreciseCoverage`（`:67`）。armed 窗口内的请求通过 initiator 栈关联到 scriptId（`docs/web-runtime.md:21`）。
- source map 拉取（`analyze_web_bundle --fetch-source-maps`，默认关闭，`src/domain/webBundleAnalysis.ts:10`）：由 **REA 进程自己用 `fetch` 发 GET**，不经过浏览器。设置了 `credentials:"omit"`、`referrerPolicy:"no-referrer"`、`redirect:"manual"`；只允许 allowed origins 内、无 userinfo 的 http(s) 地址，重定向逐跳复核（`src/browser/WebSourceMapFetcher.ts:365-408`、`:827-837`）。超时 30 s，响应上限 64 MiB（`:56-57`）。

### 2.3 对目标的副作用（核实项）

| 动作 | 副作用 | 依据 |
|---|---|---|
| 任一 page attach（列 target 除外） | Chromium 可能清掉其他客户端设置的 `navigator.platform` 仿真 | `docs/browser-observation.md:63-73` |
| 被动 inspect | 开启 Runtime/Debugger/Network domain（Debugger 开启本身会让 V8 记录脚本）；不导航、不 eval | `CdpPageCapture.ts:391-393`；`docs/browser-observation.md:75-79`、`:264-266` |
| `observe_web_execution` | precise coverage 会**重置计数器并阻止优化执行**，改变时序和 JIT | `docs/web-runtime.md:13` |
| source map 拉取 | 对目标 origin 多发一次 GET（不带 cookie） | `WebSourceMapFetcher.ts:380-388` |
| Playwright 场景 | 主动驱动页面；注入 `Page.addScriptToEvaluateOnNewDocument`，内容是空的 storage 探针脚本，以及「阻止 service worker 注册」的策略脚本；用 `Debugger.setBreakpointByUrl` / `evaluateOnCallFrame` 读 storage；设置 locale/时区/视口仿真 | `PlaywrightScenarioStorage.ts:289-315`、`PlaywrightScenarioServiceWorkers.ts:153-170`、`:251-256`、`PlaywrightScenarioBrowser.ts:453-500` |
| V8 Inspector（Node/Electron） | 只开 `Runtime.enable` / `Debugger.enable`；**从不发 `runIfWaitingForDebugger`** | `src/inspector/V8InspectorProvider.ts:367-368`；`docs/javascript-runtime-observation.md:33-36` |

---

## 3. Electron 运行时观测会不会注入？注入了什么

分两种：

**(a) 被动 `inspect_electron_page`**：attach 到已有的 CDP 端点，只看 `file://` 页面，「without evaluating JavaScript or invoking Electron APIs」（`docs/electron-observation.md:3`、`:88`）。**不注入。**

**(b) 主动 `capture-electron-scenario`**：**注入。**

- 启动方式：`playwright._electron.launch({ executablePath, cwd, env: safeElectronEnvironment(...), args: ["-r", hookPath, <app>, ...args] })`（`src/browser/PlaywrightElectronActiveProvider.ts:156-167`）。`hookPath` = `scripts/electron-active-hook.cjs`（`:52-54`）。也就是用 Node 的 `-r` 在应用代码之前 require hook。
- 子进程环境只带 HOME/LANG/PATH/TMP/USER/DISPLAY 等白名单变量，外加 `REA_PROCESS_RUN_ID`，因此 `NODE_OPTIONS` 被排除（`PlaywrightElectronActiveProvider.ts:360-398`；`docs/electron-observation.md:218-219`）。
- 启动前先读 fuse：如果 `EnableNodeCliInspectArguments` 明确被禁，直接快速失败，不启动（`src/browser/PlaywrightElectronActiveFuses.ts:30-50`）。启动加初始快照共 60 s 截止（`PlaywrightElectronActiveProvider.ts:48`）。

**hook 的具体内容**（`scripts/electron-active-hook.cjs` + `electron-active-hook-boundaries.cjs`）：

| 类别 | patch 的对象 | 行为 | 位置 |
|---|---|---|---|
| 记录 | `app` 事件（open-url/second-instance 等） | 记事件 | `electron-active-hook.cjs:1246-1259` |
| 记录 | `ipcMain.on/once/handle/removeListener/off/removeAllListeners` | 包装回调，记 channel 和参数**形状** | `:608-1000` |
| 记录 | `ipcRenderer.send/sendSync/invoke/postMessage` | 同上（renderer→main） | `boundaries.cjs:13-53` |
| 记录 | `contextBridge.exposeInMainWorld/exposeInIsolatedWorld` | 记录暴露 | `boundaries.cjs:55-` |
| 记录 | `browser-window-created`：读 preload 路径（`getLastWebPreferences` / `_getPreloadScript`） | 不从应用代码推断 | `electron-active-hook.cjs:1195-1232` |
| 记录 | `webContents.send/postMessage` | main→renderer | `:1079-1135` |
| 记录 | `utilityProcess.fork`、`child_process.spawn/fork/exec/execFile` | 记录后**照常执行** | `boundaries.cjs:275-332`、`:366-388` |
| 记录 | `process.dlopen` | 对 ≤16 MiB 的 `.node` 算 sha256 后**照常加载** | `boundaries.cjs:235-273` |
| **阻断** | `webContents.loadURL` 目标不是 file/data/about 或 loopback http(s) | reject `External navigation is blocked` | `electron-active-hook.cjs:1030-1046`、`:169-181` |
| **阻断** | `shell.openExternal/openPath/showItemInFolder/writeShortcutLink` | 返回拒绝，保持原返回契约 | `boundaries.cjs:337-364` |
| **阻断** | `session` 权限请求/检查 → false；证书校验 → -2；`will-download` → cancel | | `boundaries.cjs:86-157` |
| **阻断** | `setWindowOpenHandler` → 始终 `{action:"deny"}` | | `boundaries.cjs:159-196` |
| **阻断** | `app.relaunch/setAsDefaultProtocolClient/setLoginItemSettings` | | `boundaries.cjs:214-233` |
| 暴露 | `globalThis.__reaElectronActiveSnapshot()` | REA 通过 `application.evaluate` 取快照 | `electron-active-hook.cjs:1311-1329`；`PlaywrightElectronActiveActions.ts:328-331` |

- 留存预算 16 MiB，超出部分只计数为 dropped（`electron-active-hook.cjs:63`、`:1311-1328`）。**参数值不留存**，只留形状（`docs/electron-observation.md:99-100`、`:119-121`）。
- REA 另外还会通过 Playwright `application.evaluate` 在**主进程执行**动作：强制崩溃 renderer（`forcefullyCrashRenderer`）、`app.emit("open-url"/"second-instance")`、读窗口列表和版本（`PlaywrightElectronActiveActions.ts:192-197`、`:253-262`、`:284-331`）。
- 文档明确：这**不是沙箱**，被启动的进程保留正常的文件系统和网络权限（`docs/electron-observation.md:109-111`）。child_process 与 dlopen 只记录不拦截（见上表）。

---

## 4. source map 为什么放独立的限堆 Node 进程

结论：source map 的解码量（VLQ 段数、索引 section 嵌套）由不可信输入决定，且使用的是未修改的上游库 `@jridgewell/trace-mapping 0.3.31`（`docs/web-source-location.md:62-69`；`src/javascript/sourceMaps/DecodedSourceMap.ts:1`）。放进独立子进程后，可以用 V8 硬堆限制和 deadline 兜底：爆了只杀子进程，返回明确的 `limit`/`timeout`，主进程不受影响，也不返回半截结果。

- 类注释：「Integrate the pinned upstream codec through an independently bounded owned process」（`src/javascript/sourceMaps/SourceMapDecoder.ts:32`）。
- 启动：`process.execPath --max-old-space-size=192 --max-semi-space-size=8 --v8-pool-size=1 --single-threaded --stack_size=2048 SourceMapCodecProcess.js <requestPath>`。删掉 `NODE_OPTIONS`，设 `UV_THREADPOOL_SIZE=1`，cwd 为私有临时目录（`src/javascript/sourceMaps/SourceMapCommand.ts:168-207`）。
- 请求以文件形式传入（`wx`、0600，`SourceMapCommand.ts:173-179`）。子进程用 `readStableArtifact(path, 32MiB)` 读取（`SourceMapCodecProcess.ts:20-23`）。
- 预算（`src/domain/webSourceLocation.ts:113-123`）：map 4 MiB、输出 32 MiB、解码行/段/记录各 262144、section 嵌套 64 层、20 s、堆 192 MiB。父进程发送前先检查 4 MiB 和 32 MiB（`SourceMapDecoder.ts:91-112`），子进程内再查一次（`SourceMapCodecProcess.ts:27-40`）。
- 父进程**复核子进程报告的实际堆上限**：大于 256 MiB，或 Bun 报告的内存上限与启动器不一致，都判为输出错误（`SourceMapDecoder.ts:182-201`）。
- Bun 宿主忽略 V8 flag，Windows 改用 Job Object 限 704 MiB，其他平台没有限制，并在 limitations 中写明（`docs/web-source-location.md:76-84`）。
- 同类设计还有：JS 源码分析 worker（§1.2-4）、web bundle worker（1024 MiB，`src/browser/WebBundleAnalysisWorkerProtocol.ts:8-15`、`WebBundleAnalysisWorker.ts:506-516`）、HAR/SAZ/qlog 解码子进程（32 MiB 输入、96 MiB 回复、30 s，`src/browser/history/HistoricalCaptureDecoder.ts:169`、`:288`）。推断：项目的统一原则是「解析不可信大输入一律放进 owned 子进程 + 硬预算」。

---

## 5. 安全面：读不可信 JS/ASAR/ZIP

### 5.1 路径穿越

- **逻辑路径规范化** `normalizeArtifactPath`（`src/artifacts/ArtifactPaths.ts:24-41`）：含 `\0`、以 `/` 开头或带盘符的路径一律拒绝；空段、`.`、`..` 拒绝；`posix.normalize` 后与原值不等也拒绝；backslash 当普通字符处理。
- **前缀冲突** `ArtifactPathRegistry`（`ArtifactPaths.ts:44-77`）：拦截「`a` 是文件、又出现 `a/b`」这类冲突，以及同名不同类型。清单扫描（`scanReader.ts:195`）和输出树（`SafeOutputTree.ts:176-177`）都会经过它。
- **目录读取**：`DirectoryArtifactReader` 不跟随符号链接（`src/artifacts/DirectoryArtifactReader.ts:63`），symlink 只记为 symlink，limitation 写「target was not followed」（`:441-460`）。每进入一个目录都复核：`realpath` 后相对根路径不得以 `..` 开头、不得是绝对路径，根的 dev/ino 不能变（`:585-606`）。打开文件用 `O_NOFOLLOW|O_NONBLOCK`（`:248`）。
- **ASAR**：符号链接条目不跟随（`src/artifacts/AsarArtifactReader.ts:93-114`）。`unpacked` 条目从 `<archive>.unpacked` 读取，要求根目录不是 symlink，`realpath` 后 `relative` 不能越界，文件不能是 symlink，打开后 dev/ino 必须与 lstat 一致（`:437-485`）。packed 条目要求 offset+size 落在容器内（`:200-219`）。
- **ZIP**：拒绝 symlink、目录、加密条目（`src/artifacts/ZipArtifactReader.ts:244-255`）。zip.js 的 `filenameValidation:"tolerant"` 是故意的，统一交给 REA 自己的路径校验（`:185-187`）。开启 `checkSignature` 和 `checkOverlappingEntry`（`:180-181`）。源文件用 `symlinks:"reject"` 打开（`:81`）。
- **解包写出** `SafeOutputTree`：输出根必须原本不存在，`mkdir` 0700（`src/artifacts/SafeOutputTree.ts:118-126`）。每个文件 `O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW` 0600（`:183-189`）。每级父目录 `mkdir` 前后复核 identity（`:466-495`）。文件头注释坦承：Node 没有可移植的 dirfd 相对遍历，syscall 边界上仍有 pathname 竞态窗口（`:55-60`）。

### 5.2 Zip bomb

- 解压预算 = `max(256 MiB, min(MAX_SAFE_INTEGER, 500 × 压缩包字节数))`（`src/domain/artifactExpansionBudget.ts:12-23`）。
- 枚举时累加每个成员**声明的** uncompressedSize，超过预算就报 `limit`（`ZipArtifactReader.ts:203-218`）。真实输出由 zip.js 按声明大小强制约束（注释 `:203`），并做 CRC 校验（`ERR_INVALID_CRC32` 等，`:16-17`）。
- 元数据读取可以设单次上限，在分配 Buffer 之前就拦截（`ZipArtifactReader.ts:138-145`）。
- SAZ 更严：展开总量 ≤32 MiB（`src/browser/history/SazCapture.ts:21-57`），并拒绝重名成员、加密、链接（`:30-46`）。
- 写出时逐块检查「不超过清单记录的大小」，结束后检查长度和 sha256 都相等（`SafeOutputTree.ts:216-237`）。

### 5.3 超大文件 / 头部炸弹

- ASAR 头 16 MiB（`AsarHeader.ts:9`、`:347-350`），展开列表 64 MiB（`AsarListing.ts:8`、`:84`）。
- ASAR 先**复制一份快照**到 `tmpdir()/rea-asar-snapshot-*/container.asar`（`wx`、0600），边复制边算 sha256，后续所有读取都基于快照，防止 TOCTOU（`AsarArtifactReader.ts:320-350`）。推断：大 ASAR 会额外占用同等大小的临时磁盘空间。
- 通用的 `readStableFileBytes`：先 stat 比较上限（`src/filesystem/readStableFileBytes.ts:39-41`），读后复核没被改动（`:51`）。`openRegularFile` 用 `O_NOFOLLOW|O_NONBLOCK|O_NOCTTY`，Windows 没有 O_NOFOLLOW 时改用 lstat 复核（`src/filesystem/RegularFile.ts:73-120`）。
- Electron fuse 读取：可执行文件 ≤1 GiB，按 256 KiB 分块搜索 sentinel（`src/electron/ElectronFuseReader.ts:30-35`）。
- **入口统一的「读两次、比哈希」**：清单期算的 sha256 和长度，在读文本时（`JavaScriptArtifactFiles.ts:351-357`）、解包写出时（`SafeOutputTree.ts:226-237`）都会再核一次，中途被替换就报 `integrity`。

### 5.4 已知缺口（基于源码，均已核实）

- `analyze-javascript-application` 没有条目数上限和单文件大小上限（§1.5）；内存最终由 worker 堆和父进程结果预算兜底。
- 主动 Electron 场景**不是沙箱**：child_process 和 dlopen 只记录不阻止（`boundaries.cjs:235-273`、`:366-388`；`docs/electron-observation.md:109-111`）。
- `SafeOutputTree` 自述存在 pathname 竞态窗口（`SafeOutputTree.ts:55-60`）。
