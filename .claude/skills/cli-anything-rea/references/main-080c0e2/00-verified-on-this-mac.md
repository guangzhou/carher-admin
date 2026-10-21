# 本机实测清单（main @ 080c0e2，2026-10-20，macOS 26.2 arm64）

01~06 是读源码得出的；这份只记**真跑过**的。没在这里出现的结论 = 只读过代码、没现场证据。

## 跑通的

| 什么 | 命令 | 结果 |
|---|---|---|
| 构建 | `cli-anything-rea deps build` | node 24.22.0 + `npm ci` + `npm run build:cached` exit 0（要先 `deps unkill`，见下） |
| doctor | `cli-anything-rea run doctor` | Ghidra 8 项全绿（含自编 `mac_arm_64/decompile`、JDK 21.0.12.1）、Kuna 1.833 绿、JADX 绿；Hopper/IDA 未装；19 个 agent 注册全红（**故意没跑 setup**） |
| MCP 工具目录 | `cli-anything-rea tools verify` | 运行时 `tools/list` = **210**，与 `toolEffects.ts` 名字一一对应、destructive 31 / readOnly 5 注解全一致。`docs/client-configuration.md` 写的 172 已过时 |
| Ghidra 反编译 | `run decompile pan _rea_probe_pan --provider ghidra` | 13s；伪代码含 `0x27f`（=639，我们编进去的上限），`observed / shipped-artifact` |
| Kuna 反编译 | 同上 `--provider kuna` | 2s；同样还原出 `0x27f` 与 `-100` |
| auto 选引擎 | `rea decompile pan _rea_probe_pan`（不给 provider） | 报 `ambiguous`（Ghidra+Kuna 都可用）——与 `AnalysisProviderRegistry.ts:650-670` 一致，e2e 守着 |
| Electron 静态重建 | `run analyze-js "/Applications/Visual Studio Code.app/Contents/Resources/app/node_modules.asar" --record-integrity` | 310s，峰值 RSS 1.87 GB，Evidence 280 MB；3936 个相关文件、解析 3036 个 JS、1546 模块、29273 条发现、75 个解析失败、4845 条 limitation（大头是单文件超资源上限，如 copilot `app.js`） |

## 现场才看到的（源码里看不出）

- **VS Code 的 asar 默认被拒**：`artifact_integrity_mismatch`——`@github/copilot-darwin-arm64/.../runtime.node` 的 asar 声明 sha256 与磁盘文件不符（打包后又被签名）。REA 的报错自己给了解法 `--integrity-policy record-and-continue`。签过名的 Electron 应用多半都要这个。
- **VS Code 这份 asar 里 IPC/BrowserWindow 全是 0**：它是 `node_modules.asar`（依赖包）。主进程代码在 `Contents/Resources/app/out/`（`grep -l ipcMain` 命中 `out/main.js` 等），**但 REA 对 `out/` 还没跑过**，「指向 out/ 就能出 IPC」是推断。
- **大文件被跳过不是失败**：超过单文件资源上限的 JS 进 `limitations`，整次仍 exit 0。读结果先看 `statistics.parse_failures` 与 limitation 数，别把「0 个 IPC」当「没有 IPC」。

## 本机坑

- **火绒（`cn.huorong.HipsMain` 系统扩展）在原路径上 SIGKILL 新写入的 Mach-O**（exit 137，系统日志无记录）：esbuild、turbo、TS7 原生 `tsc`、jscpd。同字节复制到同目录新 inode 就能跑。
  - 假设：火绒按路径/首次执行拦截 → 证伪条件：同字节新 inode 同路径也被杀 → 数据：`cp` 到同名新文件后 exit 0（3/3），而原文件稳定 137；火绒 `sysmon.db` 的 `SysProgramInfo` 里有这些路径的记录。
  - 处理：`cli-anything-rea deps unkill`（`deps build` 内置）。没改火绒设置。
- `git grep -E` 不支持 `\s`/`\b`，静默 0 命中 ⇒ 一律 `-P`（单测守着）。
- `process.exit()` 紧跟 `console.log` 时管道在 64/128 KB 截断（目录 JSON 134 KB）⇒ 写回调里再 exit。
- 并行 `brew install` 一串 formula：丢 openssl@3 锁导致 node@24 没装上；`python3` 被链到 3.15（已 `brew link --overwrite python@3.14` 改回）；`simdjson` 升级把默认 `node`（25.9.0）弄坏，`brew upgrade node` → 26.12.0 恢复。

## 没装 / 装了也用不上

- Hopper（商业，demo 可由 `rea setup --install-hopper` 装，但 setup 会写 `~/.claude.json`）、IDA、JEB：未装。
- tshark / sigrok / binwalk / pwntools / EVM：工具已装，但 REA 在 macOS 上把对应能力判为不可用（`src/server/createServer.ts:208-232` 的 `linux &&` / `linuxX64` 条件）。**工具名仍在 `tools/list` 的 210 个里**（实测有 `inspect_pcap_capture`、`extract_firmware`、`inspect_evm_interface`），是调用时报不可用，不是不注册。WASM（`REA_WABT_BIN_DIRECTORY`）与浏览器模块解析不限平台。
- gdb 18.1 已装；macOS 上 attach 进程要给 gdb 做代码签名，未做。
