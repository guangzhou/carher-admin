---
name: cli-anything-rea
description: >-
  REA（morluto/rea，npm `rea-agents`，「用 agent 逆向任何东西」的 MCP+CLI，210 个工具）在本机怎么装、怎么跑、源码怎么读。
  源码 `~/codes/rea`、依赖 `~/codes/rea-deps`（Ghidra 12.1.4 + 自编 arm64 反编译器、Kuna 1.833、JDK 21、jadx-mcp 0.7.1 等，路径全在 `rea.env`）；
  入口 `cli-anything-rea`（真身 `tools/cli-anything-rea/`）的 `src` / `tools` / `deps` / `run` 四组命令。
  `references/main-080c0e2/` 是 7 份逐行深读（入口/MCP、domain、application、原生引擎、Web/JS、进程/setup/安全）+ 本机实测清单。
  Use when 用户说「rea / reverse engineer anything / 逆向某个 app / 反编译 / 拆 Electron / 拆 asar / 看某功能怎么实现的 /
  rea 源码 / rea 有哪些工具 / 哪些工具危险 / rea setup 会改什么 / rea 装好了吗 / ghidra 跑不起来 / 构建被 Killed 137」。
---

# cli-anything-rea：本机的 REA

**一句话**：REA 让 agent 不看源码就能拆一个 App——原生二进制交给 Ghidra/Kuna 反编译，JS/Electron 用 Babel 静态重建模块图和 IPC，
结果都是带出处（`file:line`、sha256）的 Evidence。它本身已有完整 CLI，这个 harness 不重包它的分析功能，只补 REA 回答不了的四件事：
源码在某 commit 怎么写的、210 个工具里哪些危险（源码表 vs 运行时一致性）、本机依赖到底能不能执行、真样本跑不跑得通。

```bash
uv tool install --editable tools/cli-anything-rea      # 已装则跳过
cli-anything-rea deps status                            # 16 个依赖逐个执行版本命令
cli-anything-rea run doctor                             # rea doctor，引擎 vs agent 注册分开看
```

## 命令

| 想知道 | 命令 |
|---|---|
| 克隆在哪个 commit、深读文档对不对得上 | `src head` |
| 分几层、每层看哪份文档 | `src layers` |
| 某函数/类在哪 | `src find createBinarySession [--path src/ghidra]` → `src show src/composition/binary.ts:21` |
| 有哪些 `REA_*` 配置 | `src env-vars`（70 个名字，含非配置常量） |
| 210 个工具 / 只看危险的 | `tools list [--destructive] [--grep adb]` |
| 运行时广告的和源码表一致吗 | `tools verify`（真起 `rea mcp` 翻 `tools/list`） |
| 依赖都能跑吗 / rea.env 设了啥 | `deps status` / `deps env` |
| 构建被 Killed: 9 / exit 137 | `deps unkill` 或直接 `deps build` |
| 反编译一个函数 | `run decompile BIN _func --provider ghidra\|kuna`（Mach-O 的 C 符号带前导 `_`） |
| 拆一个 Electron 应用 | `run analyze-js <dir 或 .asar> [--record-integrity]` |

完整 Evidence 存 `~/codes/rea-deps/runs/`，命令只回摘要 + 路径。直接用 REA：`source ~/codes/rea-deps/rea.env && node ~/codes/rea/scripts/rea.mjs <cmd>`。

## 用之前必须知道的

1. **`auto` 不会替你挑引擎**：恰好 1 个可用才绑定，≥2 个报 `ambiguous`，显式指定不可用也不回退（`AnalysisProviderRegistry.ts:650-670`）。本机 Ghidra+Kuna 都可用 ⇒ **原生命令一律带 `--provider`**。Kuna 快（2s vs 13s），Ghidra 能力多（`search_bytes`、数据类型、调用目标解析只有它有）。
2. **签了名的 Electron 应用默认被拒**：`artifact_integrity_mismatch`（打包后又签名了 unpacked `.node`）。加 `--record-integrity`（= REA 的 `--integrity-policy record-and-continue`），差异记成 untrusted 继续。
3. **「0 个 IPC」≠ 没有 IPC**：先看 `statistics.parse_failures` 和 limitation 数——超单文件资源上限的 JS 直接跳过进 limitation，整次照样 exit 0。还要确认指对了目录：VS Code 的 `node_modules.asar` 只是依赖包。
4. **结论分三档**：Evidence 的 `confidence` = observed / derived / inferred，`authority` 另一轴（shipped-artifact / analyst-inference …）。向用户转述时别把 inferred 说成 observed。
5. **会执行或注入目标的工具默认就注册**：frida、adb、进程捕获、`capture_electron_scenario`（`-r` 注入 hook，**child_process 和 dlopen 照常执行，不是沙箱**）、`gdb_console`、`execute_rizin_command`（跑完关掉当前 target）。REA 自己没开关，只靠 MCP 的 `destructiveHint` 让客户端审批（31 个）。只对信得过的目标用。
6. **`rea mcp --http` 无鉴权**：默认绑 127.0.0.1；绑到别的地址后 Host/Origin 校验也关掉。远程用要带鉴权的代理或 ssh 隧道。新客户端 `initialize` 会顶掉旧会话。

## 已接进 Claude Code（10-21）

`rea setup --client claude_code --yes` 已跑：`~/.claude.json` 加了 `mcpServers.rea`（命令 = 本地 `~/codes/rea/scripts/rea.mjs mcp`，
env 带 GHIDRA_INSTALL_DIR / JAVA_HOME / REA_KUNA_COMMAND / REA_JADX_MCP_JAR），装了 `~/.claude/skills/reverse-engineer-anything/`。
另在 `~/.claude/settings.json` 的 env 加了 `MCP_TOOL_TIMEOUT=450000`（REA 给其他客户端推的默认值：Ghidra 启动 330s + 120s 余量；
Claude Code 默认太短，大目标会被切断）。备份：`~/.claude.json.rea.backup` 与 `~/codes/rea-deps/backup-20261021-213603/`。
撤销：`rea uninstall --client claude_code`，再删 settings 里那个 env。**需要重启 Claude Code 才生效。**

注意：注册的命令用 `#!/usr/bin/env node` 起，PATH 上默认 node 是 26.x（REA 也支持）；若哪天默认 node 掉到 <22.19 会起不来。

## `rea setup` 会改什么

对 Claude Code 只两处：`~/.claude.json` 顶层 `mcpServers.rea`（`npx -y rea-agents@6.7.0 mcp` 或本地入口绝对路径 + provider 探测出的 env），
和 `~/.claude/skills/reverse-engineer-anything/` 下 7 个 md。写前整份备份到 `~/.claude.json.rea.backup`（**uninstall 不删这份备份**）；
不碰 settings.json / CLAUDE.md / 权限。非交互又没 `--yes` 只出计划。想先看效果：`CLAUDE_CONFIG_DIR=<临时目录> rea setup --client claude_code`。
细节与 file:line 见 `references/main-080c0e2/06-process-setup-security.md` §2。

**零遥测**；联网只在：`rea update`（`npm view`，新版装时 `--ignore-scripts`，只校验 `--version`）、setup 下 Hopper、浏览器取 source map（限已批准 origin）、你自己配的 IDA/JEB/Frida 端点。

## 安装与本机坑（10-20 实测）

依赖全在 `~/codes/rea-deps/`，来源与版本见 `deps status` 的 source 列。重装顺序：brew 装工具 → Ghidra 官方 zip →
`make ghidra_opt ARCH_TYPE="-arch arm64" OSDIR=mac_arm_64` 编反编译器放 `os/mac_arm_64/decompile` → Kuna/jadx-mcp 官方 release（jadx 按 `docs/android-analysis.md` 的 sha256 校验）→ `deps build`。

- 🔴 **火绒在原路径 SIGKILL 新写入的 Mach-O（exit 137，系统日志无痕）**：esbuild、turbo、TS7 原生 tsc、jscpd。同字节复制成新 inode 就能跑 ⇒ `deps unkill`。别去改火绒设置，也别归因成 Gatekeeper（SIP 已关、签名有效、新编 clang 产物不被杀）。
- 🔴 **REA 要 node 22.19+/24.11+/26+，用 brew `node@24`**（`rea.env` 已前置到 PATH）；默认 `node` 会随 `brew upgrade` 漂移。
- 🔴 **并行 `brew install` 一长串**会丢锁（node@24 没装上）、把 `python3` 链到新小版本、升 `simdjson` 弄坏默认 node ⇒ 分批装，装完查 `python3 --version` 和 `node -v`。
- **只在 Linux 可用的**：pcap / 信号 / 固件 / pwntools / EVM。macOS 上这些工具**照样列在 210 个里**（10-20 实测 `tools/list` 有 `inspect_pcap_capture`、`extract_firmware` 等），只是会话可用性策略判为不可用（`createServer.ts:208-232` 的 `linux &&` 条件），调用才报错。WASM 和浏览器模块解析不限平台，给了绝对路径就开。

## 深读文档（`references/main-080c0e2/`，每条带 file:line）

| 文件 | 讲什么 |
|---|---|
| `00-verified-on-this-mac.md` | **只记真跑过的**：构建、doctor、210 工具对账、两引擎反编译、VS Code asar 310s 重建、火绒三段式 |
| `01-entry-mcp-cli.md` | 分发器、一次 MCP 调用逐层链、210 工具分 16 类清单、错误投影 21 种 code、57 个可设 `REA_*`、HTTP 无鉴权 |
| `02-domain.md` | Evidence 字段与 ID 算法、ResidualUnknown 修订链与 verified 门槛、Result/27 种错误 tag、各格式 reader 在哪 |
| `03-application.md` | 选引擎规则、缓存 key/上限 10000/失效、EvidenceLedger、JS 重建主流程、obligation ledger 判完成、省 token 机制 |
| `04-native-providers.md` | Ghidra 起 JVM→socket 协议→`decompileFunction`、Hopper `-Y` 注入与 lease、7 个能力位、各引擎独有操作、doctor 8 项 |
| `05-web-js-artifacts.md` | Babel 限堆 worker、Electron 识别模式、CDP 只连 localhost、覆盖率副作用、Electron 注入 hook 清单、防穿越/防 zip bomb |
| `06-process-setup-security.md` | 进程所有权（随机 token + 启动时间指纹）、setup/uninstall/update 逐文件、13 条网络外发、执行类能力默认开启 |

文档是 `080c0e2` 时读的；`src head` 发现 HEAD 变了就先 `src show` 复核行号再引用。

**网上实际怎么用**（官方 5 个 showcase 的提问+工具序列、800+ issue 归类、HN/评测的批评、常见卡点）⇒ `references/usage-in-the-wild-2026-10.md`。

**实践案例（照着做一遍就会）**：Cursor 内置浏览器怎么在网页里执行 JS——选目录 → `run analyze-js` → 从 IPC/contextBridge 挑线索 →
按行列号读原文核对 → 顺通道名 grep 另一端 ⇒ `references/case-cursor-browser-isolated-eval.md`。教训：语义图会撞 10 万节点上限、
大文件被跳过、动态通道不解析、跨文件调用方要人补。
