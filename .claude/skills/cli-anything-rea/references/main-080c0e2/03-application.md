# REA 应用层 `src/application/` 深读（main @ 080c0e2）

> 范围：`/Users/Liuguoxian/codes/rea/src/application/`。所有 `file:line` 都相对于 `src/`，除非写了 `docs/` 或 `AGENTS.md`。
> 只读阅读，没有 build，也没有运行测试。标「推断」的条目源码没有直接写明。

## 结论先行（大白话）

1. **一个 target 只绑一个深度引擎**（hopper/ghidra/ida/kuna 四选一）。默认 `auto` 的规则是：所有候选一起探测，能用的只剩一个就选它，能用的超过一个就报 `ambiguous`，让你自己用 `provider_id` / `--provider` / `REA_ANALYSIS_PROVIDER` 指定。显式指定的引擎不能用就直接报错，**不会退回到别的引擎**。
2. **证据（Evidence）和缓存是两套东西**。EvidenceLedger 是会话里按 `evidence_id` 去重、不可变的证据账本；AnalysisSnapshotCache 是「同一个二进制 + 同一个引擎 profile + 同一个操作 + 同一组参数」的结果重放缓存，key 是这几样东西的 sha256，最多 1 万条，满了就不再写入，不做淘汰。
3. **省 token 靠三件事**：`detail: "summary"` 先把完整证据存在服务端，只返回摘要；之后用 `inspect_analysis_view` + `retained-evidence` 引用按 page/item/facet 分页取细节；Unknown、NativeValueTrace 这类列表带 `offset/limit/next_offset`。

---

## 1. 子目录结构与职责

总量：`src/application` 下非 `.test.ts` 文件共 **59,586 行**。其中顶层 122 个文件 30,478 行，子目录合计约 29,100 行。

### 1.1 子目录（文件数 / 行数，不含测试）

| 子目录 | 文件 | 行数 | 职责（依据各文件首个 doc comment） |
|---|---|---|---|
| `javascript/` | 55 | 14,149 | JS/Electron 静态重建：ASAR/目录 → Application Graph + Semantic Graph + Electron 边界；另含运行时观察、对账、版本对比、source 恢复 |
| `binary/` | 15 | 4,330 | 二进制会话核心：Registry 选引擎、Router 路由、BinarySession 生命周期、快照缓存 |
| `native/` | 8 | 1,914 | 原生函数匹配、值流追踪（`NativeValueTrace`）、UI action 追踪、API inspection |
| `environment/` | 6 | 1,745 | 环境 bundle 的导出、回放、serve |
| `reverse/` | 2 | 980 | 通用逆向命令 profile（超时 120s，输出上限 16MiB，并发 8，见 `reverse/ReverseEngineeringService.ts:45-48`） |
| `investigation/` | 6 | 855 | InvestigationRecords、EvidenceLedger、Unknown 变更证据 |
| `frida/` | 3 | 792 | Frida 设备与插桩 |
| `binaryDiagnostics/` | 6 | 522 | 离线 layout、PE resources、recorded crash |
| `analysisView/` | 2 | 478 | `inspect_analysis_view`：对已完成的 Evidence 做摘要、分页、facet 投影 |
| `managed/` | 4 | 438 | .NET managed 图、成员对比、managed/native 校验、反编译重建导入 |
| `wasm/` | 3 | 378 | WASM |
| `il2cpp/` | 3 | 335 | IL2CPP metadata（支持 summary） |
| `process/` | 2 | 324 | 进程场景 CLI 与 Evidence |
| `android/` | 5 | 271 | APK 静态分析（支持 summary） |
| `sqlite/` | 3 | 171 | SQLite |
| `artifacts/` | 4 | 157 | 桌面应用目录、artifact 对比、解包目标目录 |
| `captures/`、`signals/` | 1+1 | 152+152 | 抓包、信号录制 |
| `rust/`、`go/` | 2+2 | 141+139 | Rust、Go 构建信息（Go 支持 summary） |
| `evm/` | 3 | 129 | EVM 接口恢复 |
| `electron/` | 2 | 101 | Electron fuse 读取 |
| `flutter/` | 3 | 94 | Flutter / Dart AOT（支持 summary） |
| `firmware/` | 2 | 78 | binwalk / unblob |
| `jeb/`、`pcap/`、`adb/`、`apktool/` | 各 2 | 61/59/54/54 | 对应外部工具的 port 和 service |
| `harmony/`、`apple/` | 各 1 | 28/27 | 把已认证的包 inventory Evidence 投影成应用 Evidence |

### 1.2 顶层文件按前缀分组（行数）

| 分组 | 文件 | 行数 | 职责 |
|---|---|---|---|
| `Setup*` | 11 | 3,928 | `rea setup`：安装 Hopper/Ghidra 等宿主依赖，注册 MCP client |
| `Client*` + `SupportedClients` | 9 | 4,274 | 各 MCP 客户端配置文件的读、改、发布、回滚（其中 `ClientConfigurationDocument.ts` 1,780 行，是最大的单文件） |
| `LinuxHopper*` / `MacHopper*` / `Hopper*` | 7 | 2,805 | Hopper 下载、验签、安装 |
| `Reconstruction*` | 10 | 1,690 | 重建覆盖度评估和 obligation ledger（见 §5） |
| `Web*` / `Browser*` | 17 | 2,502 | 浏览器观察、场景捕获、网络/脚本导出、模块追踪 |
| `Doctor*` | 5 | 1,386 | 环境诊断 |
| `ReferenceSource*` | 5 | 1,303 | 历史源码导入 |
| `Uninstall*` / `Update*` | 7 | 2,072 | 卸载、自更新 |
| `Enhanced*` / `CallPathTracing` / `CallRelationshipTraversal` | 5 | 1,264 | 增强工具：字面量追踪、调用路径 |
| `DirectAnalysis*` | 4 | 834 | CLI 一次性分析入口 |
| `CapabilityInventory` 等 | 2 | 949 | 能力清单 |
| `BinaryTargetResolver.ts` | 1 | 768 | 解析本地目标的格式、架构、sha256 |
| `AnalysisProvider.ts` | 1 | 370 | Provider / Client / CapabilityDescriptor 接口定义 |
| `EvidenceInputResolver.ts` | 1 | 70 | 解析 `retained-evidence` 引用（见 §6） |
| `InvestigationProviders.ts` | 1 | — | 合成 workflow 的 provider identity，例如 `rea-javascript-application`（`application/InvestigationProviders.ts:88-92`） |

---

## 2. 选 provider：SessionProviderRouter / BinarySession / AnalysisProviderRegistry

**一句话**：Registry 负责「从 4 个深度引擎里挑 0 或 1 个」；Router 把选中的深度引擎和若干辅助 provider 拼成一个 `CompositeProvider`，并要求它们声明的操作互不重叠；BinarySession 持有唯一的 active target，切换 target 时串行执行。

### 2.1 装配

- 生产环境的深度候选固定为 `[hopper, ghidra, ida, kuna]`，默认选择器来自 `config.analysisProvider`（`composition/binary.ts:47-63`）。环境变量 `REA_ANALYSIS_PROVIDER` 的默认值是 `"auto"`（`config/environment.ts:24`）。
- 辅助 provider 用 `LazyAnalysisProvider` 包装：identity 和 capability 可以同步读出，具体实现等第一次执行时才加载（`application/binary/LazyAnalysisProvider.ts:15-19`）。
- managed、artifact、native 会话使用空 Registry `new AnalysisProviderRegistry([])`，只挂辅助 provider（`composition/binary.ts:75,85,99`）。
- `composeBinarySession` = `new BinarySession(SessionProviderRouter.selectable(registry, aux), new InvestigationRecords())`（`application/binary/BinarySessionComposition.ts:11-18`）。

### 2.2 构造期的确定性约束（Registry）

- provider id 必须匹配 `^[a-z][a-z0-9-]*$`，且不能是 `auto`（`contracts/providerSelection.ts:4-10`）。重复 id 会抛异常（`application/binary/AnalysisProviderRegistry.ts:138-139`）。
- 每个 capability 的 provenance 必须和 provider 的 identity 完全一致，同一 provider 不能重复声明同一个 operation（`AnalysisProviderRegistry.ts:141-155`）。
- **「sorted deep candidates」**：候选列表按 provider id 的 Unicode code point 排序后冻结（`AnalysisProviderRegistry.ts:159-163`），类注释写明「Registration order never affects selection or caller-visible output」（`AnalysisProviderRegistry.ts:94-97`）。这里的排序只影响输出顺序，不代表优先级：`defaultSelector()` 的注释是「without interpreting it as priority」（`AnalysisProviderRegistry.ts:172-175`）。

### 2.3 Router 的不相交约束

- 深度候选和辅助 provider 的 id 合在一起必须唯一（`application/binary/SessionProviderRouter.ts:49-53, 213-222`）。
- 两个辅助 provider 声明同一个 operation 会抛异常（`SessionProviderRouter.ts:198-211`）。辅助 provider 和深度 provider 声明同一个 operation 也会抛异常（`SessionProviderRouter.ts:55-59`）。
- 选择结果为 binding=null 且只有一个辅助 provider 时直接使用它，其余情况包装成 `CompositeProvider`（`SessionProviderRouter.ts:138-143`）。CompositeProvider 内部按 operation 建路由表，遇到重复会 throw（`application/binary/CompositeProvider.ts:127-153`）。
- 没有任何 provider 时返回 `emptyClient`：`health` 返回 ok，其它操作返回 `AnalysisCapabilityUnavailableError`（`SessionProviderRouter.ts:162-185`）。

### 2.4 选择算法 `#selectTarget`（`AnalysisProviderRegistry.ts:442-519`）

1. **并行发现**：对每个候选并行执行 `inspectReadinessSnapshot`（没有这个方法时用 `inspectAvailability`）和 `inspectTargetSupport`（`AnalysisProviderRegistry.ts:331-335, 674-694`）。**任意一个候选的探测抛异常，整次选择就失败**，不会跳过出错的候选（`AnalysisProviderRegistry.ts:358-366`）。
2. 请求里的 selector 非法时返回 `invalid_options`（`AnalysisProviderRegistry.ts:464-473`）。selector 的取值顺序是：请求参数 > 构造时传入的默认值（`AnalysisProviderRegistry.ts:474-475`）。
3. **auto 的短路**：如果 `canInspectWithoutDeepProvider(target)` 为真，就不绑定深度引擎（binding=null）（`AnalysisProviderRegistry.ts:480-485`）。生产环境只有 Apple dispatch metadata 目标会传这个谓词（`composition/binary.ts:65`）。
4. selector 来自环境变量、指向一个具体 id，并且所有候选都 `unsupported` 时，静默返回 binding=null（`AnalysisProviderRegistry.ts:486-498`）。
5. **显式指定**（`#selectConcrete`，`AnalysisProviderRegistry.ts:543-587`）：
   - id 未知 → `unknown_provider`（`:550-551`）
   - 不支持该 target → `target_unsupported`（`:559-560`）
   - 不可用 → `provider_unavailable`（`:561-562`）
   - profile 解析失败也直接报错（`:571-575`）
   - **以上任一失败都返回 err，不会改选其他候选**。`selectionSource` 取 `"request"` 或 `"environment"`（`:508`）。
6. **auto**（`#selectAutomatically`，`AnalysisProviderRegistry.ts:589-671`）：
   - 只对 `available && supported` 的候选并行执行 `resolveAnalysisProfile`（`:607-617`）
   - 有被取消的、cleanup 不完整的，或 signal 已中止 → 整体报错（`:630-641`）
   - 能拿到 profile 的候选（`usable`）数量为 0 → binding=null（`:650-655`）
   - 数量 >1 → **`ambiguous`**（`:656-664`），错误文案是「Multiple analysis providers support this target. Choose one with provider_id, --provider, or REA_ANALYSIS_PROVIDER.」（`domain/providerSelectionError.ts:79-80`）
   - 数量 =1 → `selectionSource: "auto-single-candidate"`（`:665-670`）
7. profile 校验（`application/binary/AnalysisProviderEvaluation.ts:29-121`）：
   - `AnalysisUnsupportedTargetError` 会改写成 `target_format_unsupported`（`:63-76`）
   - profile 为 null、identity 不一致或 schema 不合法 → `version_unresolved`，该候选变为不可用（`:87-119, 126-145`）

**所以「deterministic selection, no fallback」的具体含义是**：结果只由 (target, selector, 各候选的 readiness 和 profile) 决定，与注册顺序无关。auto 只接受「恰好一个」可用候选，多于一个就报 ambiguous，不会按顺序挑第一个。显式指定失败时不会换引擎重试。整个选择过程不启动任何引擎 client（`AnalysisProviderRegistry.ts:95`、`SessionProviderRouter.ts:83`）。

### 2.5 BinarySession 的生命周期要点

- 单 active target。`open`、`close`、外部变更都经过 `#serialize` 串行，切换前先 drain 已入队的调用（`application/binary/BinarySession.ts:70-77, 876-909`）。
- 同 path、同 sourcePath、同 snapshot 身份时视为同一个 target，此时复用当前 route，不重新选择（`application/binary/BinarySessionOpen.ts:105-109, 136-144`）。如果 profile 也相同且旧 client 仍存活（只认 `idle/busy`），走 fast path（`BinarySession.ts:315-323, 927-940`）。
- 打开失败会尝试恢复上一个 target（`BinarySession.ts:568-591, 942-972`）。
- `providerIdentity(operation)` 按 capability 表把每个操作归属到具体 provider（`BinarySession.ts:134-155`）。
- 外部变更命令（`withAdmittedExternalMutation`，默认操作名 `execute_rizin_command`）遇到 active 的 Ghidra 会话会直接拒绝，要求先显式 close（`BinarySession.ts:384-426`）。

---

## 3. investigation 体系与缓存

### 3.1 InvestigationRecords / EvidenceLedger

- `InvestigationRecords` 是 `EvidenceLedger` 的薄封装，同时实现 4 个窄 port：`EvidenceReader`、`EvidenceWriter`、`EvidenceUnknownWriter`、`UnknownRegistryPort`（`application/investigation/InvestigationRecords.ts:27-34`；port 定义见 `application/investigation/InvestigationRecordPort.ts:16-78`）。它与二进制执行无关（`InvestigationRecords.ts:26`）。
- Ledger 内有三张 Map（`application/investigation/EvidenceLedger.ts:45-47`）：
  - `#records`：key 为 evidence_id
  - `#unknownRevisions`：key 为 `${unknown_id}:${revision}`（`:480-481`）
  - `#unknownHeads`：每个 unknown 的最新版本
- `record()` 是幂等的：相同 id 且内容一致时返回 `"duplicate"`，内容冲突时报错（`EvidenceLedger.ts:49-72`）。比较前会抹掉 subject 的 `name` 和 `local_path`，因此**同一份证据换了本地路径仍视为同一条**（`EvidenceLedger.ts:483-497`）。
- `import()` 是整包原子合并：先 parse 并校验整个 bundle，然后提交（`EvidenceLedger.ts:75-176`）。规范 JSON 不同但语义一致的记录会替换旧记录，并返回 `metadataChanged=true`（`:110-113`），这个标志会让快照缓存失效（`application/binary/BinarySessionRecords.ts:144`）。
- `export()` 返回深拷贝。`forSerialization()` 和 `forAnalysis()` 直接借出不可变对象，避免整图复制（`EvidenceLedger.ts:179-210`）。

### 3.2 Unknown 的归属

- 每次 Unknown 变更（record 或 update）都会生成一条 Evidence。provider 是 `rea-unknown-registry`，`authority: "analyst-inference"`，limitation 是「records analyst intent, not proof of the answer」（`application/investigation/UnknownEvidence.ts:10-47`）。
- Unknown 绑定 mutation Evidence 的 id 和 subject sha256（`EvidenceLedger.ts:229-233`）。subject 取当前 active target（`BinarySessionRecords.ts:327-328, 345-346`）。`recordEvidenceWithUnknown` 不带 subject（`InvestigationRecords.ts:96-103`）。
- 更新使用乐观锁：`expected_revision` 与当前 head 不一致时返回 `revision-conflict`（`EvidenceLedger.ts:299-303`）。
- workflow 自动产生的 Unknown 会在 question 后追加 `(Evidence <id>)`，并把该 id 设为 supporting evidence（`application/investigation/WorkflowUnknown.ts:9-28`）。
- `verifyUnknownResolution` 只校验状态一致性：`valid` 表示状态为 resolved，`truthVerified` 表示 disposition 为 verified。它不证明答案本身正确（`EvidenceLedger.ts:361-377`）。

### 3.3 BinarySessionRecords + AnalysisSnapshotCache

**缓存 key**（`domain/analysisSnapshot.ts:521-529`）：

```ts
`query_${sha256(canonicalJson({ target, binding, operation, parameters }))}`
```

- `target` 由 `snapshotTarget()` 生成，**不含路径**：包括 sha256、kind、architecture、format；raw-binary 额外包括 entry_point（`domain/analysisSnapshot.ts:354-369`）。
- `binding` = provider identity + 完整的 analysis profile（`domain/analysisSnapshot.ts:372-378`）。

**哪些操作可以缓存**（`application/binary/AnalysisSnapshotCache.ts:70-85`）：

- 排除 `health`、`current_*`、`goto_address`、`list_documents` 这类有状态操作（`:43-50`）。
- 文档作用域的操作必须显式传 `document`，`xrefs`/`comment` 等必须显式传 `address`（`:52-65`）。
- capability 不能有 `mutatesArtifact`，也不能是 `cachePolicy: "live"`，等等。
- 另外硬编码跳过 4 个操作：`decode_interface_builder`、`inspect_asset_catalog`、`inspect_keyed_archive`、`trace_dylib_resolution`（`BinarySession.ts:719-725, 762-770`）。
- `referencesTruncated` 的结果不写入缓存，防止重放时把截断的数据当成完整结果（`AnalysisSnapshotCache.ts:345-347`）。

**上限**：`SNAPSHOT_CACHE_ENTRY_CEILING = 10_000`，provider 条目和 workflow 条目共用这个额度（`AnalysisSnapshotCache.ts:68, 377-382`）。满了以后新条目直接丢弃，没有淘汰策略（`:138, 267, 354`）。

**命中**：`prepareSessionExecution` 先查缓存，命中就直接返回，不调用 provider（`application/binary/BinarySessionExecution.ts:84-93`、`BinarySession.ts:728-729`）。命中结果的 limitations 会追加「Loaded from a local REA analysis snapshot; this call did not re-run the provider.」（`AnalysisSnapshotCache.ts:313-316`）。Windows 上的 Ghidra 必须先成功执行一次真实操作，之后才允许重放（`AnalysisSnapshotCache.ts:88-90`、`BinarySession.ts:755-760`）。

**写入**：成功且可缓存的执行先 `recordEvidence`，`cachePolicy !== "live"` 时再 `recordSnapshot`（`BinarySession.ts:762-799`）。

**失效**：

| 触发 | 行为 | 位置 |
|---|---|---|
| target 或 profile 变化 | `select()` 发现不匹配就清空条目 | `AnalysisSnapshotCache.ts:115-119` |
| 调用带 `mutatesArtifact` 的操作 | 派发前 `invalidateSnapshot()`：清空并置 `#snapshotInvalidated`，之后 lookup 和 record 都失效，导出和导入快照报错 | `BinarySession.ts:732`；`BinarySessionRecords.ts:153-161, 238, 245, 282-286` |
| 外部变更命令（如 rizin） | 先 invalidate，然后关闭 active 但保留 records | `BinarySession.ts:431, 437` |
| 导入 bundle 时 metadataChanged | invalidate | `BinarySessionRecords.ts:144` |
| 新 target 成功 open | `resetSnapshotInvalidation()`，然后 `selectSnapshot` | `BinarySession.ts:368-372` |

**生命周期**：

- 正常 `close` 会调用 `clearSessionRecords()`，**Evidence 账本和缓存一起清空**（`BinarySession.ts:505`、`BinarySessionRecords.ts:300-305`）。
- open 失败只清缓存，保留 Evidence 和 Unknown（`BinarySession.ts:328-333`）。
- 想跨会话保留，需要用 `closeWithSnapshot` 写盘。快照内容包括 entries、workflow_entries，以及按 target sha256 过滤后的 evidence bundle（`BinarySession.ts:455-481`、`BinarySessionRecords.ts:170-179`）。下次 open 时通过 `snapshot` 或 `loadSnapshot` 导入，导入前会校验 target 和 profile 是否匹配（`BinarySessionOpen.ts:157-176`、`AnalysisSnapshotCache.ts:189-223`）。
- 「post-commit notifications」指 `onAnalysisSnapshotChanged` 监听器：每次成功写入后触发，回调抛出的异常会被吞掉（`BinarySessionRecords.ts:71-74, 307-322`）。推断：MCP 层用它来刷新 snapshot resource。

---

## 4. JavaScript 重建主流程

**一句话**：输入一个本地 `.asar` 或已解包的目录，不执行任何代码。流程是 inventory → 读源文件 → 在隔离 worker 里解析 AST → 构建 Application Graph 和 Semantic Graph → 汇总 Electron 边界，最后封装成一条 `rea.javascript-application-analysis` Evidence。

- **入口**：
  - MCP：`server/registerElectronTools.ts:109` 调用 `analyzeJavaScriptApplicationValidated`
  - CLI：`cli/javascriptApplicationAnalysis.ts:24` 调用 `analyzeJavaScriptApplication`
  - 两者都进入 `application/javascript/JavaScriptApplicationService.ts:33-175`
- **输入 schema**（`application/javascript/JavaScriptArtifactReconstructionInput.ts:7-13`）：
  - `input_path`
  - `format`：`auto|asar|directory`
  - `integrity_policy`：`fail|record-and-continue`，默认 `fail`（见 `docs/javascript-artifact-reconstruction.md`）
  - `source_paths`：可选
  - 资源控制参数
- **资源默认值**（`domain/javascript/javascriptAnalysisResourceControls.ts:6-16`）：
  - worker 堆 1024MB，超时 300s
  - semantic 节点总数 100k、单文件 20k；semantic unknown 总数 100k、单文件 20k
  - 调用方最多可调到 1,000,000
- **主流程** `reconstructJavaScriptArtifact`（`application/javascript/JavaScriptArtifactReconstruction.ts:73-341`）：
  1. 对 `input_path` 做 realpath 和可读性检查（`:349-371`）。识别格式：目录为 directory，以 `.asar` 结尾的文件为 asar，其它类型拒绝（`:374-404`）。
  2. `scanCanonicalArtifactInventoryInScope` 生成规范 inventory 快照，并要求快照的 `root_format` 与识别出的格式一致（`:96-105`）。
  3. `readJavaScriptArtifactFiles` 读取相关文件后**立即释放 reader**（`:124-142`）。
  4. 在隔离 worker（`createJavaScriptSourceAnalysis`，带堆和超时限制）中逐文件分析（`:161-196`）。各文件类型的分析方式（`application/javascript/JavaScriptArtifactAnalysisTypes.ts:127-145`）：
     - javascript：AST 静态分析
     - package-json、json、html、source-map：只按 artifact 字节解析
     - native-addon：不分析
  5. 构建 Application Graph（`buildImmutableJavaScriptArtifactGraphSteps`，包含 Webpack/Rspack bundler 投影和 Electron 边界，见 `application/javascript/JavaScriptArtifactGraphBuilder.ts:38-80`、`JavaScriptArtifactGraphBundlers.ts:20`），然后 seal（`JavaScriptArtifactReconstruction.ts:232-243`）。
  6. 构建 Semantic Graph（`:248-257`），并汇总 Electron 边界（`:271-278`）。
- **输出**：`JavaScriptArtifactReconstructionResult`（`JavaScriptArtifactReconstruction.ts:46-70`），包含：
  - 身份字段：`input_path`、`format`、`root_artifact_sha256`、`inventory_manifest_id`、`inventory_graph_sha256`、`integrity_contradictions`、`source_selection`
  - 三个产物：`graph`（JavaScriptApplicationGraph）、`semantic_graph`、`electron_summary`
  - `statistics`：relevant_files、nested_asar_containers、text_bytes_read、parsed、skipped、visited_ast_nodes、findings、modules、parse_failures
  - `limitations`

  这个结果再封装成 Evidence：`predicateType: "rea.javascript-application-analysis"`、`confidence: "derived"`、`authority: "shipped-artifact"`（`application/javascript/JavaScriptApplicationEvidence.ts:76-87`），provider 是 `rea-javascript-application`（`application/InvestigationProviders.ts:88-92`）。
- **中断时保留部分结果**：worker 中断或取消时，已经分析完的文件事实仍会构图并生成 Evidence，挂在错误的 `retainPartialObservation` 上（`JavaScriptArtifactReconstruction.ts:298-313`、`JavaScriptApplicationService.ts:110-135`）。
- **同目录其它服务**：
  - feature 追踪、版本对比、source↔bundle 对比、export shape 对比：`JavaScriptApplicationWorkflowService.ts:38-238`
  - 运行时观察和对账：`JavaScriptRuntimeObservationService.ts:23-57`、`JavaScriptRuntimeReconciliationService.ts:20-32`
  - Electron 主动场景捕获：`ElectronActiveObservationService.ts:107`
  - source 恢复：`JavaScriptRecoveryService.ts:20-31`，对应 `recover_javascript_sources`
  - 最大的文件是 `JavaScriptArtifactPathResolution.ts`（2,067 行），负责模块路径解析

---

## 5. reconstruction coverage 与 obligation ledger

**一句话**：两者都是「证据够不够」的判官，本身不做分析。coverage 针对某个 boundary，判断它的 surface、claim、package proof 是否都有可比 authority 的 verifier 证据。obligation ledger 先把 Evidence 拆成一条条「重建义务」，再逐条检查 owner、fixture、verifier 是否齐全。默认结论偏保守：数据不足就是 `unknown`。

### 5.1 Coverage（`evaluate_reconstruction_coverage`）

- application 层只做 parse 并转发给 domain（`application/ReconstructionCoverageService.ts:14-39`）。
- domain 里依次评估 surfaces、claims、coverage risks、package proofs，收集 reasons（`domain/reconstructionCoverage.ts:297-334`）。
- **完成判据** `closureStatus`（`domain/reconstructionCoverage.ts:424-457`）：
  - reasons 为空 → `ready`
  - 含 `verifier-failed`、`active-contradiction`、`authority-routing-detected`、`owner-path-missing`、`owner-undistributed`、`package-proof-failed` 任一 → `failed`
  - 含 stale、incompatible、ambiguous、verifier-unknown、active-unknown 等 → `unknown`
  - 其余情况 → `partial`
- authority 对照表（例如 `controlled-replay` 必须是 observed，inferred 不能升级为 replay）见 `docs/reconstruction-coverage.md`。

### 5.2 Obligation ledger（`build_reconstruction_obligation_ledger`）

- 服务入口（`application/ReconstructionObligationLedgerService.ts:39-70`）的步骤：派生候选 → 评估 → 包成 `rea.reconstruction-obligation-ledger` Evidence。这条 Evidence 是 `confidence: "inferred"`、`authority: "analyst-inference"`（`:72-92`）。
- **候选来源**（`application/ReconstructionObligationCandidates.ts:48-70`）：
  - `analyze_javascript_application`、`reconcile_javascript_runtime`、`project_managed_application_graph` 三类 Evidence 的 application 图节点和 semantic 图节点，按节点 kind 匹配 policy（`:72-172`）
  - `capture_process_scenario` 进程捕获（`:174-`）
  - 人工提交的 `reviewed_obligations`
  - 遇到 graph coverage 不完整或存在 `unknown` 节点，就写入 limitations（`:98-101, 163-166`）
- **单条义务的状态** `obligationStatus`（`application/ReconstructionObligationLedgerEvaluation.ts:452-469`），按优先级判断：
  1. `out-of-scope`
  2. `blocked`
  3. `contradicted`
  4. `unknown`：重复定义、残留 Unknown、authority 不可得
  5. `unowned`：没有 manifest binding
  6. 诊断为空 → **`verified`**；否则 → `implemented`
- 会产生诊断的检查：
  - owner 缺失或多于一个（`:186-197`）
  - 缺 parser type；必需的 case kind 缺 fixture；fixture 的 authority 偏弱（`:253-304`）
  - verifier 缺失、没有列出本义务的 obligation_id、结果 Evidence 不在 bundle 里、authority 偏弱、`status !== "pass"`（`:308-359`）
- 依赖传播：依赖项缺失或依赖项不是 verified 时，本义务变为 `blocked`（`:421-450, 471-479`）。
- **整本 ledger 的完成判据** `reconstructionObligationLedgerStatus`（`domain/reconstructionObligationLedgerIntegrity.ts:58-77`）：
  - 没有任何义务 → `unknown`
  - 未 verified 的 required 义务中有 contradicted 或 verifier-failed → `failed`
  - limitations 非空 → `unknown`
  - 未 verified 的 required 义务中有状态为 unknown 的 → `unknown`
  - **所有 required 义务都 verified → `ready`**；否则 → `open`
  - coverage 字段：没有 limitations 时为 `complete`，否则为 `partial`（同文件 `:79-83`）
- `ledger_id` 的格式是 `rol_<digest(evidence_ids, obligation_ids, closure_digest)>`，可以复现（`ReconstructionObligationLedgerEvaluation.ts:99-113`）。
- 空请求返回 `unknown`，并带 limitation「No reconstruction obligations were derived; closure cannot be claimed.」（`ReconstructionObligationLedgerEvaluation.ts:162`；文档 `docs/reconstruction-obligation-ledgers.md` 的「Complete CLI request」节）。

---

## 6. 让 agent 少花 token 的机制

依据：`AGENTS.md:43-47` 把「Agent tokens and context」列为第一优化目标，原文是「Use task selection, summaries, paging and retained Evidence where supported」。另外，`docs/tool-design.md:66-69` 规定 limit 必须有真实约束作为依据，「An assumed agent budget is insufficient justification for a limit」。

| 机制 | 字段与上限 | 位置 |
|---|---|---|
| **summary + retained Evidence** | `detail: "summary"`：完整 Evidence 先 `recordEvidence` 进会话账本，只返回 `inspect_analysis_view {kind:"summary"}` 的投影；服务端不保留 Evidence 时报错，并提示改用 `complete` | `application/analysisView/AnalysisViewService.ts:197-248` |
| 默认值为 summary 的工具 | native function matching（`contracts/native/functionMatchingContracts.ts:29`）、`inspect_desktop_application`（`contracts/desktopApplicationToolContracts.ts:20`）；Dart AOT、Android、Go、IL2CPP 也支持 summary 分支 | `application/flutter/DartAotInspection.ts:32-39`、`application/android/AndroidPackageInspection.ts:32`、`application/go/GoBinaryService.ts:67`、`application/il2cpp/Il2cppMetadataService.ts:163` |
| `get_evidence_bundle` | `detail: complete\|summary`，默认 `complete`；`filters`（evidence_id、operation、target_sha256、analysis_profile_digest、procedure_address）只能和 summary 一起使用 | `contracts/sessionToolSchemas.ts:12-15`、`application/investigation/InspectEvidenceBundle.ts:11-35`、`domain/evidenceRecordSummary.ts:8-14, 118-143` |
| **按引用传 Evidence** | 用 `{kind:"retained-evidence", evidence_id}` 代替内联整条 Evidence | `domain/evidenceReference.ts:5-16`、`application/EvidenceInputResolver.ts:10-35` |
| analysis view 的来源 | `retained-evidence` / `inline` / `evidence-file`；`evidence-file` 的 schema 说明是「large captures are not charged as inline input」；认证前预估堆占用（按 2 份拷贝计） | `domain/analysisView/analysisView.ts:296-311`、`application/analysisView/evidenceFileSource.ts:28-73` |
| analysis view 分页 | `kind:"page"`，collection 为 sections/symbols/modules，参数 `offset`、`limit`（正整数，**schema 无上限**）；coverage 返回 `status(complete-within-view\|page\|empty)`、`examined`、`total`、`next_offset`、`exhausted` | `domain/analysisView/analysisView.ts:219-224, 319-325` |
| Unknown 分页 | `list_unknowns`：`offset` 默认 0，`limit` 可选，**省略时返回全部**；返回 `items`、`total`、`offset`、`next_offset`，按 unknown_id 稳定排序 | `contracts/sessionToolSchemas.ts:55-75`、`application/investigation/EvidenceLedger.ts:329-358` |
| NativeValueTrace 分页 | `max_depth` 3（≤16）、`max_functions` 16（≤64）、`max_call_sites` 256（≤4096）、`max_nodes` 5000（≤20k）、`max_edges` 10000（≤40k）、`offset` 0、`limit` 1000（≤5000）；返回 `next_offset` 和 `truncated`；单节点 payload 上限 8MiB，整体输出上限 32MiB，超出报错；**翻页会重新执行分析** | `domain/native/nativeValueTrace.ts:7-13`、`application/native/NativeValueTrace.ts:21, 418-449` |
| 快照重放 | 同一查询命中缓存时不再调用引擎（§3.3），最多 1 万条 | `application/binary/AnalysisSnapshotCache.ts:68` |

推断：省 token 的主路径是「重工具用 summary 返回摘要，并拿到 evidence_id → 用 `inspect_analysis_view` 按需翻页 → 跨工具传 `retained-evidence` 引用」。`list_unknowns` 和 analysis view 的 page `limit` 在 schema 里没有服务端上限，调用方必须自己传。
