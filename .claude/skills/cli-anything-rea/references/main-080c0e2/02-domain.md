# REA `src/domain/` 深读（main @ 080c0e2）

> 路径相对 `~/codes/rea/`。这份由子 agent 读码产出、主会话落盘；第 4 节「字节 reader」列只核了位置与行数，未逐个深读内部。

## 0. 结论

`src/domain/` **不是格式解析器目录**，装的是三样：跨 CLI/MCP 的数据契约（zod schema + 类型）、Evidence/Unknown 证据模型与错误代数、各领域的纯语义计算（JS/Electron 静态语义、比较、重建覆盖率、视图投影）。
真正读字节的 reader 在 `src/artifacts/`、`src/go/`、`src/rust/`、`src/il2cpp/`、`src/wasm/`、`src/electron/` 等平级目录；domain 只放它们的输出 schema 和无 I/O 的头部判定。
规模：477 个非 test `.ts`，114,199 行。

## 1. 子目录

| 目录 | 行数 | 内容 |
|---|---|---|
| `javascript/` | 45,737（145 文件，≈40%） | Babel AST 的 JS 静态语义（`javascriptSemantic*` 56 个文件：绑定/别名/成员变更/require 来源/Promise/CJS-ESM 互操作）、Electron 静态分析（IPC、BrowserWindow 选项）、应用图、运行时对账、source→bundle 比较 |
| 根目录平铺 | ≈32.7k（≈200 文件） | Evidence 核心、错误代数、`result.ts`、可执行头判定（`binaryTarget.ts` `elfFixedHeader.ts` `dosMz.ts` `peMachine.ts`）、plist/XML、比较、重建覆盖率/就绪度、canonical JSON/digest |
| `analysisView/` | 12,248 | `inspect_analysis_view` 分页视图：父 Evidence → 摘要，`coverage{status,examined,total,next_offset,exhausted}` + `view_digest` |
| `native/` | 6,803 | NativeAOT PE、ObjC/Swift 元数据、布局、崩溃记录、函数匹配 |
| `apple/` | 4,582 | Mach-O FAT、dylib、NSKeyedArchive、plist、bundle |
| `managed/` | 4,382 | .NET 成员比较、托管↔native 验证 |
| `process/` | 4,217 | 进程捕获、trace 规格 |
| 小目录 | 各 100~840 | adb/electron(fuse)/il2cpp/android/rust/wasm/go/evm/pcap/sqlite/flutter/firmware/jeb：输入/输出 schema + 少量校验 |

## 2. Evidence 模型（`src/domain/evidence.ts`）

一条 Evidence = 一次成功观察的不可变记录，strict schema：

- `evidence_id = ev_<sha256>`：语义内容的 canonical digest，**排除 `subject.name`/`local_path` 显示字段**（同字节换路径 = 同 ID）；`parseEvidence` 重算，被改即拒。
- `subject`：`{name, digest.sha256, format(29 种枚举), architecture, local_path}` 或 null。
- `confidence`：`observed | derived | inferred`（默认 observed）——AGENTS.md「观察/推导/推断」三分的落点。
- `authority`：`shipped-artifact`(默认) / `controlled-replay` / `historical-reference` / `external-service` / `analyst-inference`，与 confidence 正交。
- `raw_result` + `normalized_result` 都保留，raw 参与 ID。
- `limitations: string[]`：未知与边界写这里。
- `relationship_coverage`、`locations`（address / range / artifact-path / file-offset / range）、`evidence_links: ev_*[]`（provenance 链）。
- `createEvidence` 确定性产出；`immutableEvidence` deep-freeze + WeakMap 登记零拷贝借用。

**谁写哪档**：解析器（Go/Rust/IL2CPP/WABT）一律 observed+shipped-artifact；`derived+analyst-inference` 在 changed-behavior / call-path / reconstruction-verification（注册在 `src/server/registerInvestigationTools.ts`）；`inferred` 在 static-runtime-correlation、桌面应用组件候选、Rust `rustc_commit`（从 `/rustc/<40hex>/` 推出）。排序 `inferred=1 < derived=2 < observed=3`（`evidenceBundle.ts` `evidenceQualifies`）。

## 3. Unknown 的两种表示

**① 记录内隐式未知**：`limitations` 字符串（如 Go「缺 build-info ≠ 不是 Go」「main 模块缺失 ⇒ 空依赖 ≠ 没依赖」，`src/go/GoBinaryProvider.ts` `limitationsFor`）；`null`/`"unknown"` 枚举（domain 里 `"unknown"` 593 处、`"unavailable"` 105、`"absent"` 50，语义分开）；`relationshipCoverage.ts`：`complete | partial | unknown`，**provider 没报 = 按未知，不当完整**。

**② ResidualUnknown**（`src/domain/residualUnknown.ts`）：显式登记的待决问题。
- `unknown_id = unk_sha256(domain, question, scope_digest, …)`；`scope_digest` = 创建它的 mutation Evidence 的 subject sha256。
- 修订链：`revision` 递增 + `previous_revision_digest`；更新乐观并发（`expected_revision`）。
- 状态 `open | investigating | blocked | contradicted | resolved`；`depends-on` 全图 DFS 禁环；同一证据不能既支持又反驳。
- `verified` 硬门槛（`validateVerifiedResolution`）：无挂着的反驳证据；至少一条 resolution evidence 也在 supporting 里；那条不能是 mutation 证据本身；其 authority/confidence/environment 满足 unknown 上的 `required_*`。
- 每次变更配一条 mutation Evidence（`src/application/investigation/UnknownEvidence.ts`）：`derived + analyst-inference`，limitation 写「登记行为只证明意图，不证明答案」。
- `reconstructionUnknowns.ts` 沿 `depends-on` 收集未 resolved 的 unknown，挡住重建结论。

## 4. EvidenceLedger / Bundle

`EvidenceLedger`（**在 application 层**：`src/application/investigation/EvidenceLedger.ts`）：会话级三张 Map（records / unknownRevisions / unknownHeads）。`record` 幂等（同 ID 同内容 → duplicate，冲突 → `EvidenceIntegrityError`）；`import(bundle)` 原子——先整包校验，失败一条不写；`verifyUnknownResolution` 返回 `valid` 与 `truthVerified`（只有 disposition=verified 才 true）。

`EvidenceBundle`（`src/domain/evidenceBundle.ts`）：`{artifacts, providers, environments, scenarios, captures, unknowns, records}`，前五项由 records 派生必须一致；RFC 8785 canonical JSON；`evidenceBundleForTarget(sha256)` 导出时 unknown 整条修订史都属于该 target 才带走，依赖的 unknown 没带走则级联剔除——导出子图闭合。

## 5. Result 与错误代数

`src/domain/result.ts` 只有 `{ok:true,value} | {ok:false,error}` + `ok()`/`err()`，**无 map/flatMap**，调用方一律 `if (!r.ok) return r;`；414 个文件 import。底层纯解析器用 `Result<T,string>`，边界/应用层用 `Result<T, XxxError>`（约 875 处）。domain 仍有约 310 处 `throw`（zod parse、不变式），约定在边界 catch 包成 typed error。

`analysisErrorBase.ts`：`_tag` 取自封闭清单 `ANALYSIS_ERROR_TAGS`（27 个）；必须跨投影保留 `diagnostics`、`capturedOutput`（截断显式标）、`partialObservation`（失败前已完成的观察，≈30 种联合，只留第一份）；清理语义 `cleanupIncomplete` / `bindCleanupDependencies`（WeakRef 反向通知）/ `retainSecondaryFailure`（后续失败不替换主错误）。

`analysisErrorCore.ts` 具体类：UnsupportedTarget、ResourceConstraint（memory|cpu|file-size|transport|capacity；调用方自选上限不可重试）、Input（issue reason 封闭枚举）、Output、CapabilityUnavailable、Cancelled、Timeout、AccessDenied（EACCES/EPERM 不算畸形输入）、ArtifactChanged。

`analysisErrorProjection.ts` `projectAnalysisError` → 对外 `{code(20 种), category(10 种), message, retryable, remediation:{action}, recovery?, details?}`，CLI/MCP 共用。**只要清理没完成，code 一律覆盖成 `cleanup_incomplete`**，真因放 `details.execution_failure`。

## 6. 各格式在哪

| 格式 | domain 里 | 字节 reader |
|---|---|---|
| ELF/PE/Mach-O/DOS | `binaryTarget.ts` `parseExecutableHeader` 按魔数分派、无 I/O；PE 含 COFF/CodeView/RVA→偏移；`apple/machoContainer.ts` FAT「魔数 ≠ 证明」（排除 JVM class 冲突） | `src/native/` |
| ASAR | 无 | `src/artifacts/AsarHeader.ts`（header 上限 16 MiB）、`AsarArtifactReader.ts` |
| Godot PCK | `godotPckFormat.ts`（10 行，只判 `GDPC`） | `src/artifacts/GodotPckArtifactReader.ts` |
| ZIP 族（ipa/apk/msix/hap） | `zipPackageFormat.ts`：签名识别 ≠ 归档有效 | `src/artifacts/ZipArtifactReader.ts` |
| Go buildinfo | `go/goBinary.ts` 输出 schema，保留原始字节 base64 + 源范围 | `src/go/` |
| Rust cargo-auditable | `rust/rustBinary.ts`：`.dep-v0` zlib JSON，格式版本 0/1/8，依赖环检测 | `src/rust/RustAuditableReader.ts` |
| IL2CPP | `il2cpp/il2cppMetadata.ts`：魔数 `0xFAB11BAF`，v24/27/29/31/39 | `src/il2cpp/` |
| WASM | `wasm/*.ts`：WABT 输出 schema，上限 32 MiB | `src/wasm/WabtArtifactProvider.ts`（外部 WABT） |
| Electron fuse | `electron/electronFuses.ts` | `src/electron/ElectronFuseReader.ts` |

共同模式：reader 在边界一次性归一 → domain strict schema + superRefine 二次自洽 → 缺失写 null/unknown 并配 limitation → 上限按格式真实约束写死并写进 limitation。

## 7. 没覆盖

`javascript/`（最大子目录）只摸了结构与入口 `analyzeJavaScriptSemantics`；各字节 reader 内部未深读；依赖方向已由主会话核实：`grep -rln -E "from ['\"](\.\./)+(application|providers|server|cli|main|composition)/" src/domain --include='*.ts'`（排除 test）= **0 个文件**，domain 不反向依赖外层。
