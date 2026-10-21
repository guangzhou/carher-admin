# REA 在网上实际怎么被用（2026-10-21 调研）

## 结论先说

大家拿 REA 主要干三件事：**问一个具体功能在没源码的程序里怎么实现**（原生 exe/dll、Electron、APK、网页各一条线），
**照着拆出来的行为重写一个**（老游戏重建、桌面软件克隆、硬件/蓝牙协议互通），以及**用它给自己的东西做外部审计**。
社区卡点最多的不是分析本身，而是外围：MCP 客户端吃不下 200 个工具的 schema、大目标 Ghidra 启动/导入超时、Windows 原生权限自检失败、
安装时引擎探测不到，以及大 JS bundle 把内存撑爆。批评集中在「只是把 Ghidra 等现成工具包了一层、比裸工具更费 token」和版权/DRM。

数据口径：GitHub API 拉了最近 300 条 issue+PR、按作者排除后的 811 条非核心作者 issue 标题、18 条 issue 全文与评论、
24 个 Discussions 页面；HN Algolia 7 个帖子；本地 `~/codes/rea/website/`（commit `080c0e2`）读 showcase/guides/evidence。
原始数据在 `~/codes/rea-deps/research-raw/`（30 MB，不进仓库）。

**查不到的**：Reddit（`reddit.com`/`old.reddit.com` 的 curl 都连不上、超时）、X/Twitter、YouTube 搜索、DuckDuckGo/Bing 都没拿到结果；
Geeky Gadgets 被人机验证拦了，MindStudio 返回空页。所以下面说到 Reddit/X/视频的内容，**只有** HN 帖子和 awesome 列表里转述过的那些。

背景数字（10-21 实测）：GitHub 345,499 star、87,047 fork；API 里 `created_at`=2026-04-14，但第一个 Show HN 是 2026-07-14
（[HN 48901763](https://news.ycombinator.com/item?id=48901763)）；npm `rea-agents` latest=6.7.0，10-12～10-18 一周下载 415,576 次，
近 30 天 549,553 次。

---

## 1. 官方用法与案例

### 1.1 rea.tools 上有哪些案例

README 只列了 3 个 showcase（DX-Ball / Notion / TH04，`README.md:229-256`），**官网 showcase 页实际有 5 个**，多出来的是 Aegis（Android）和 CTF；
另外首页还有 2 个入门例子（Windows 计算器、Chrome 小恐龙），guides 有 3 篇，first-investigation 有 1 篇，blog 有 2 篇。
线上 `https://rea.tools/showcase/` 的链接列表和本地 `website/public/showcase/index.html` 一致（aegis/ctf/dx-ball/notion/th04）。

| 案例 | 目标 | 示例提问（页面原文） | REA 工具调用序列 | 怎么验证 |
|---|---|---|---|---|
| [Aegis](https://rea.tools/showcase/aegis/) | Aegis Authenticator v3.4.3 APK，TOTP 六位码怎么算 | "Use REA to inspect aegis-v3.4.3.apk. Find how it generates the six-digit login code, explain why it changes every 30 seconds, and rebuild a small demo where I can change the time." | `inspect_android_package` → `search_android_classes`(query=Totp，3 个命中) → `inspect_android_class` → `inspect_android_method` ×多次 → `trace_android_references`；共 20 次成功调用，展示 15 条 | 把 6 个方法体拼成 JVM 小程序，和独立写的 Python 对 133 个输入一致；含 RFC 6238 全部 6 组向量；事后对照上游源码 |
| [CTF](https://rea.tools/showcase/ctf/) | DownUnderCTF 2023「masked squares」，去符号 x86-64 ELF，15 KB | "Use REA to analyze ms_flag_checker and find the flag. Explain how the checks work, then verify the answer with the original program." | `open_binary` → `search_strings`(regex `flag\|correct\|wrong\|input`) → `xrefs`(0x102004) → `resolve_containing_procedure` → `analyze_function` ×3 → `read_bytes` ×2（26 条掩码+目标值）；8 步 | 掩码和目标值交给 Z3 解出 flag；`capture_process_scenario` 用正确/错 1 字符两个输入跑原程序，分别输出 `Correct!`(exit 0) / `Incorrect!`(exit 255) |
| [DX-Ball](https://rea.tools/showcase/dx-ball/) | 1996 年 Windows 游戏 DXBALL.EXE 的声像（左右声道）计算 | "Use REA to find how DX-Ball calculates sound panning. Explain the calculation and show the code." | `analyze_function`(0x406400) → `analyze_function`(调用者 0x411f40) → `read_bytes`(0x420068,16) → `read_bytes`(0x4210a0,8)；4 步 | 得出 `pan=(x×1.5625−500)×scale`；3,205 个原 x86 差分用例 + 用 VC4.0 编译后 63 字节逐字节一致。注意：Ghidra 伪代码原本漏掉了栈参数和 x87 表达式，是靠汇编+调用者+常量补出来的 |
| [Notion](https://rea.tools/showcase/notion/) | Notion Desktop 7.6.1 的 `app.asar`，复制到系统剪贴板走哪条路 | "Use REA to trace how Notion copies text and HTML to the system clipboard. Show the code at each step." | `analyze_javascript_application`（只喂 `tab_browser_view/preload.js`）→ 人工读 main bundle；另跑两次 `trace_feature` | 证据记录自己写明：按 API 种子追踪只拿到 partial 覆盖、按 channel 种子**没匹配上**，IPC 链是人读源码补的；剪贴板格式是另一份缓存里抽模块+JSDOM 跑出来的，不是真机复现 |
| [TH04](https://rea.tools/showcase/th04/) | 东方幻想乡 PC-98 DOS 版 MAIN.EXE，16 发弹幕环怎么瞄准玩家 | "Use REA to find how TH04 creates a ring of bullets and aims it at the player. Explain the calculation and show the code." | `open_binary`(ghidra) → `inspect_native_load_image` → `analyze_function`(0x2cfc8) → `address_to_file_offset` → `read_bytes`(19 字节)；5 步 | 用已有重建工程的编译回执：Turbo C++ 4.0J 两次冷构建复现原文件 2,139 字节区间。页面明说函数边界来自项目已审过的源码，不是 REA 独立发现的 |

首页两个入门例（证据 `website/evidence/calculator-and-dino.md`）：
- **Windows 计算器百分号**：REA 给出 `CalcViewModel.dll` 里 0x1801245d0 函数的完整档案；但函数是**先用本地指令扫描按常量 100 找到候选、人工选定**的，文档写明「不是自主端到端发现」。
- **Chrome 小恐龙速度**：`inspect-web-page` 拿到 `index.js`（sha256 与上游一致），再用 Playwright 单独驱动 `Runner.update()` 验证 1,000/4,000/7,000/10,000 帧时的速度。

### 1.2 guides 与 first-investigation（都是三条主线的标准动作）

| 页面 | 示例提问 | 命令/工具 |
|---|---|---|
| [guides/native](https://rea.tools/guides/native/) | 同 DX-Ball | CLI：`npx -y rea-agents@latest function /abs/path main ...`；引擎三选一：Hopper（mac/Linux）、Ghidra 12.1.x+JDK21（Windows 实验性）、IDA（Windows） |
| [guides/javascript](https://rea.tools/guides/javascript/) | "Use REA to find what happens when I export notes in this app. Where is the CSV created?" | `analyze-javascript-application <dir或asar> --json` → 读 `summary.ipc`（1 个通道、1 次调用、1 个 handler、0 个歧义） |
| [guides/browser](https://rea.tools/guides/browser/) | "Use REA to inspect this page while I click Export notes. What request does it make, and how is the CSV built?" | `list-browser-targets http://127.0.0.1:9222` → `inspect-web-page <cdp> <TARGET> --observation-ms 10000 --include-accessibility-text --include-json-body-shapes` → `--include-script-sources`；点击是人做的，REA 只被动观测 |
| [first-investigation](https://rea.tools/first-investigation/) | 先让 agent 装 REA（"Install REA ... Show me the setup plan for approval"），再问 Export 写到哪、预测 CSV 后用 Node 跑 `toCsv` 验证 | 同 JS 线 |

FAQ 推荐的提问模板："Use REA to analyze /absolute/path/to/app and explain how it exports data. Show the relevant code and source locations."
即**一个本地目标 + 一个行为问题 + 要代码和位置**。

### 1.3 官方案例的共同点（看到的）

- 每个案例都是**一个很窄的问题**（一个函数/一条数据路径），不是「把整个 app 逆向了」。
- REA 只负责取证据（指令、调用者、常量、源码区间），**解释和重写是 agent/人做的**，验证一律在 REA 外面（差分测试、编译回放、Z3、Playwright、对照上游源码）。
- 好几个案例在 evidence 文件里主动承认 REA 没覆盖到的部分（Notion 的 trace 不完整、计算器是人工选函数、TH04 的边界来自已有工程、DX-Ball 伪代码漏参数）。
- 两个游戏案例（DX-Ball、TH04）和两篇 blog 都出自同一个协作者 N0zoM1z0 的重建工程。
---

## 2. 社区真实用法（GitHub issues / discussions）

### 2.1 先说清楚：最近 100 条 issue 不代表普通用户

最近 100 条 issue（#5448～#5620，全部建于 2026-10-20 一天内）：**76 条出自同一个账号 coygeek**，jayykernel 12 条、morluto（维护者）6 条；
83 条是 `[Bug]`，43 条零评论、只关了 9 条。标题几乎都是「X 报告了完整覆盖但漏了 Y」「拒绝时给了无效的重试建议」这类边界正确性问题
（如 [#5610](https://github.com/morluto/rea/issues/5610)、[#5549](https://github.com/morluto/rea/issues/5549)、[#5456](https://github.com/morluto/rea/issues/5456)）。
按关键词粗分：setup/doctor/CLI 输出 27、Apple Mach-O/ObjC/Swift/plist 20、错误提示不准 17、原生引擎 15、超时/取消/预算 13、JS/Electron 12。
**推断**：这批是系统化审计（很可能是 agent 批量产出），不是用户在用的时候卡住。所以用户画像改用下面这组数据。

### 2.2 去掉核心作者后的 811 条 issue（#501～#5523）

排除 coygeek、morluto、jayykernel、gensokyo-dev、N0zoM1z0、CassieFox23 后，Search API 拿到 811 条。类型：Bug 317、Feature 266、没打标签 158、Design 36、Docs 14。
按标题关键词归类（一条可以落进多类，正则粗分，只看量级）：

| 目标类型 | 条数 | 代表 issue |
|---|---:|---|
| 原生 PE/ELF/通用二进制（Ghidra/Hopper/IDA/GDB…） | 182 | [#1783](https://github.com/morluto/rea/issues/1783) Swift/ObjC Mach-O、[#5168](https://github.com/morluto/rea/issues/5168) Tiny PE、[#2078](https://github.com/morluto/rea/issues/2078) decompile 文件偏移 |
| Electron / JS bundle / source map / WASM | 118 | [#623](https://github.com/morluto/rea/issues/623)、[#1387](https://github.com/morluto/rea/issues/1387)、[#5132](https://github.com/morluto/rea/issues/5132) react-dom 撑爆内存 |
| Android APK/DEX（JADX/apktool/adb） | 49 | [#893](https://github.com/morluto/rea/issues/893) 133 MB 多 dex OOM、[#4537](https://github.com/morluto/rea/issues/4537) APK 签名方案 |
| 浏览器页面 / 网络捕获 | 47 | [#1384](https://github.com/morluto/rea/issues/1384) Chromium 150、[#4425](https://github.com/morluto/rea/issues/4425) worker 请求没记 |
| Apple Mach-O/ObjC/Swift/plist | 46 | [#5315](https://github.com/morluto/rea/issues/5315) 151 MB Mach-O 不返回 |
| .NET/IL2CPP/Unity/NativeAOT | 42 | [#5112](https://github.com/morluto/rea/issues/5112) IL2CPP、[#1801](https://github.com/morluto/rea/issues/1801) 游戏大小的程序集、[#744](https://github.com/morluto/rea/issues/744) |
| 固件/少见架构（MIPS、PowerPC、SPARC、Hexagon、68000、SNES、PS1、XEX…） | 32 | [#1741](https://github.com/morluto/rea/issues/1741) GameCube/Wii、[#4176](https://github.com/morluto/rea/issues/4176) OpenWrt 固件超时 |
| 游戏（显式提到） | 16 | [#4943](https://github.com/morluto/rea/issues/4943) XEX、[#3633](https://github.com/morluto/rea/issues/3633) Godot PCK |
| 其他（SQLite/pcap/zip/Go/Rust/EVM/MQL5/VB3） | ~20 | [#5185](https://github.com/morluto/rea/issues/5185) MQL5 .ex5、[#4627](https://github.com/morluto/rea/issues/4627) VB3 |

问题类别（同样粗分）：新格式/新后端的功能请求 296、原生引擎相关 111、安装/setup/doctor/更新 94、超时/取消/资源上限/OOM 93、Windows 平台 61、MCP 客户端/schema 兼容 51。

需要注意：功能请求大头是「再多支持一种格式/架构/后端」（Kuna [#1957](https://github.com/morluto/rea/issues/1957)、Binary Ninja [#1551](https://github.com/morluto/rea/issues/1551)、rsleigh [#1389](https://github.com/morluto/rea/issues/1389)、llama.cpp [#4867](https://github.com/morluto/rea/issues/4867)），
还有直接要求过保护壳的（[#4662](https://github.com/morluto/rea/issues/4662)「by pass Oreans WinLicense / Themida」，无人回复）。

### 2.3 细读的代表 issue

| # | 用户在干什么 | 卡在哪 | 维护者/社区怎么回 |
|---|---|---|---|
| [#4620](https://github.com/morluto/rea/issues/4620)（开，14 评论） | Gemini CLI 里让 REA 分析 `ReNamer.exe`，找 `GetSysColor/SetBkColor/SetTextColor/CreateSolidBrush` 调用，给出颜色常量的虚拟/文件偏移，**想自己用十六进制编辑器打黑色主题补丁** | 一发请求就 400 INVALID_ARGUMENT，关掉 REA 就正常 | 先判为 [#4025](https://github.com/morluto/rea/issues/4025)（布尔 `const` schema，Gemini 只吃字符串）已在 main 修、npm 没发版；用户在 main 上复测**仍然 400**，讨论转向「207 个工具一次性塞进去太大」，未关 |
| [#1720](https://github.com/morluto/rea/issues/1720)（关） | 25 MB 目标冷导入要约 21 分钟 | `search_strings` 等查询工具在 60 秒整死掉，`MCP error -32001: Request timed out` | 社区查明 60 s 是 MCP SDK 客户端默认超时，不是 REA；维护者先关了，又说「关早了」，之后让 Codex/Kimi 的 setup 按引擎时长 +120 s 自动写工具超时，再修 IDA 的预算漏算 |
| [#1048](https://github.com/morluto/rea/issues/1048)（开，18 评论） | 60 MB x86-64 PE 游戏（219,702 个函数） | 每次打开都要冷导入约 20 分钟（1,203 s / 1,208 s），`--snapshot` 也不省（1,185 s） | 维护者：调大启动超时只是把墙挪远，复用预导入工程仍未解决，保持开启 |
| [#1783](https://github.com/morluto/rea/issues/1783)（关） | 1.85 MB Swift/ObjC arm64 Mach-O | Ghidra 的 `ObjcMessageAnalyzer` 递归卡死，40 分钟超时也过不去 | PR #1800 加了 `REA_GHIDRA_DISABLED_ANALYZERS="Objective-C Message Analyzer"`，维护者确认后关闭 |
| [#1701](https://github.com/morluto/rea/issues/1701)（开，19 评论） | Windows 11 上用 Ghidra | `rea doctor` 报 `native_authority ... Delete owned object by handle failed (Win32 5)`，Ghidra 被硬关掉；四台机器复现，一台只是偶发、自己好了 | 社区多人对照实验，贡献者在 main 构建上 30/30 通过；未关 |
| [#4973](https://github.com/morluto/rea/issues/4973)（开） | Windows + Claude Desktop | `compare_web_screenshots/compare_web_captures` 报 "problem with that connector"，参数被当成字符串传 | 判为 `$ref/$defs` schema 问题，main 修了但 npm 6.6.0 当时 404、latest 还是 6.5.0；后来又发现 `compare_web_captures` 本来就没有 `$ref`、schema 165,694 字节，原因仍未查清 |
| [#4421](https://github.com/morluto/rea/issues/4421)（关，重复） | Claude Code 经 2api 代理接 Qwen3.8-Flash | 34 个 rea 工具 schema 约 430 KB，一次请求 314K prompt token 把上下文撑满 | 贡献者指出非 Anthropic `ANTHROPIC_BASE_URL` 会关掉 Claude Code 的 tool search；归并到 #3246（`REA_MCP_TOOL_FAMILIES` 按族裁剪工具） |
| [#5273](https://github.com/morluto/rea/issues/5273)（开） | Claude Code 里跑长任务 | 超过 120 s 被客户端转后台，完成后被拒：`missing required resultType`（协议 2026-07-28），结果直接丢 | 贡献者认领，补 `resultType`，PR 待开 |
| [#893](https://github.com/morluto/rea/issues/893)（关） | 133 MB、11 个 dex 的生产 APK | `search_android_classes` 报 `Java heap space`，`JAVA_TOOL_OPTIONS=-Xmx8g` 传不进 JADX 子进程 | #908 修：JVM 继承环境变量，另加 `REA_JADX_HEAP_MIB` |
| [#623](https://github.com/morluto/rea/issues/623)（关） | 大 JS 目标（拿 REA 自己的 `dist/` 719 文件 5.7 MB 复现；另一人 2.9 MB Vite/React 产物） | `RangeError: Invalid string length` 被包装成 `{"reason":"io"}` | 维护者「升 4.0.1」，协作者 #646 改成增量哈希；之后同类问题在 [#1387](https://github.com/morluto/rea/issues/1387)、[#4348](https://github.com/morluto/rea/issues/4348)、[#5132](https://github.com/morluto/rea/issues/5132)/[#5138](https://github.com/morluto/rea/issues/5138)（react-dom 18.3.1 撑爆堆）反复出现 |
| [#4881](https://github.com/morluto/rea/issues/4881) / [#4340](https://github.com/morluto/rea/issues/4340) / [#2270](https://github.com/morluto/rea/issues/2270) | 装 REA | mac 上 `prebuild-install` 弃用警告（来自可选依赖 frida，维护者查清无害）；qwen_code setup 因下载 Hopper 失败中断（用户自认网络）；Linux 已装 Hopper 仍被提议重装（新开终端后好了） | 都已关 |
| [#5244](https://github.com/morluto/rea/issues/5244)（开） | 按 skill 文档调工具 | 打包的 skill v42 写了 `inspect_pcap_capture` 等，6.7.0 服务器根本不广播，`doctor` 却报 aligned | 贡献者认领，加 skill↔目录一致性检查 |
| [#5512](https://github.com/morluto/rea/issues/5512)（开） | Claude Code 的某个分支 CLI | 工具名被钉成 `mcp__claude__*`，模型看不到 REA，doctor 仍绿；批准一次就把 `mcp__claude__*` 整个前缀加进白名单 | 未回复 |

**关于「ambiguous provider」**：标题里只搜到 1 条直接相关的 [#5446](https://github.com/morluto/rea/issues/5446)（歧义提示里列出了已判不可用的引擎），
外加设计讨论 [#2417](https://github.com/morluto/rea/issues/2417)（多个深度引擎同时可用时怎么自动选）和已修的 [#4555](https://github.com/morluto/rea/issues/4555)
（同时装了 Hopper+Ghidra 时，连不需要深度引擎的 `inspect-macho` 都报 `capability_unavailable` 要求先选引擎）。**它不是用户高频报的卡点**，只是本机 skill 里记过的坑。

### 2.4 Discussions 里的真实用法（24 个帖子）

- **行为审计**：[#5496](https://github.com/morluto/rea/discussions/5496) 用 REA 6.4.0+Ghidra 审计 2006 年千千静听 4.6.9 有没有偷传隐私：先发现 `tt469.exe` 只有 8 个函数（UPX 壳），转去未加壳的 `TTPlayer.exe`（7,015 函数、1,764 字符串），从字符串里圈出网络相关一簇，再看导入表确认没有 `GetUserName/GetComputerName`；结论是只做三件事：查版本、搜歌词、传歌词。
- **硬件互通**：[#4152](https://github.com/morluto/rea/discussions/4152) 让 LX-D02 蓝牙热敏打印机在 Windows 上当普通打印机用；REA 只做了入口（`doctor`、`list-adb-devices`），关键逻辑在 React Native 的 Hermes 字节码里，用 hermes-dec 拆出来的，作者建议 REA 加 Hermes 支持。
- **自家站点外部审计**：[#1406](https://github.com/morluto/rea/discussions/1406) 对自己的 Next.js+Stripe 门户用 `list-browser-targets`+`inspect-web-page` 看「不登录能看到什么」，15 个脚本、无 source map、无本地存储键；同一台 Win11 上 Ghidra 冷开 PE 约 29 s、JADX 拆 2,876 类 6 s。
- **能力验证**：[#2254](https://github.com/morluto/rea/discussions/2254) 拿 `gcc -O2`+strip 的 bzip2 问「哪个函数打印某条消息」，REA 找到的函数、1 个调用者、24 个被调用者都对上未 strip 的版本。
- **成本测量**：[#2090](https://github.com/morluto/rea/discussions/2090) 预注册实验（rea 6.4.0、Claude Code、Sonnet 5.5、48～329 KB 的小程序，每次问「X 在哪个函数」）：用 REA 中位约 30 万输入 token、$0.19，只用 LLVM 命令行约 11.3 万、$0.08；正确率 68/72 vs 71/72。`list_procedures` 单次返回 51～224 KB；正常安装时 agent 10 次里 0 次主动加载 skill。
- **本地模型**：[#3098](https://github.com/morluto/rea/discussions/3098) Qwen3.8-27B、32K 上下文，全量 206 个工具时 prompt 248,762 token 直接放不下；裁到约 10 个工具后 3/3 答对。
- **模型拒答**：[#3038](https://github.com/morluto/rea/discussions/3038)、[#2095](https://github.com/morluto/rea/discussions/2095)、[#2027](https://github.com/morluto/rea/discussions/2027) 都是 Claude/Codex/Cursor 拒绝；[#5328](https://github.com/morluto/rea/discussions/5328) 分享绕法：用 "analyze" 不说 "reverse engineer"、换旧模型、改 DLL 名，花了约 $14。
- **想要做的东西**（只是提问，没有结果）：微信小游戏 [#3256](https://github.com/morluto/rea/discussions/3256)、MathType 网页版 [#2115](https://github.com/morluto/rea/discussions/2115)、Samsung Tizen TV [#3768](https://github.com/morluto/rea/discussions/3768)、QuickBooks [#5060](https://github.com/morluto/rea/discussions/5060)、Office [#4705](https://github.com/morluto/rea/discussions/4705)、Roblox [#5314](https://github.com/morluto/rea/discussions/5314)。
- **质疑**：[#1928](https://github.com/morluto/rea/discussions/1928)「案例要么是 Electron 这种随便一个 AI 都能读的 JS，要么是轻量 APK、老 exe，实测感觉就是一个 skill」；[#4913](https://github.com/morluto/rea/discussions/4913) 质疑「用 REA 把 FF13 移植到 iPad」这类说法和整体虚假信息。
- **其他**：[#5354](https://github.com/morluto/rea/discussions/5354) 卡巴斯基把用户自己构建的 exe 报成 `VHO:Trojan.Win32.Convagent.gen`（帖子内容是该用户自己的 app，不是 REA 本身被报）；[#5255](https://github.com/morluto/rea/discussions/5255) 有人做了「REA 和 agent 跑在两个断网容器里」的方案来分析不可信文件。

### 2.5 社区产出（awesome 列表）

[awesomerea/awesome-rea](https://github.com/awesomerea/awesome-rea)（[#4470](https://github.com/morluto/rea/discussions/4470)，收录要求 README 明确写了用 REA）列了约 45 个「用 REA 做的项目」，分四类：
- **老游戏重建/移植**（约 23 个）：Pocket PC 纸牌、DOS 游戏、XP 扫雷移植 Linux（本地模型跑 REA MCP）、Ultima VI、Fallout NV 物件持久化、宝可梦究极日月、SA-MP 客户端等。
- **桌面软件克隆**（约 26 个）：QuickBooks 风格记账 [ledger-reborn](https://github.com/lxistired/ledger-reborn)、报税、PowerToys 取色器、Windows 记事本、macOS 预览、Ghostty、Screen Studio、OBS、CapCut、Wispr Flow，还有 [rea-claude-code](https://github.com/victcn/rea-claude-code)/[rea-codex](https://github.com/victcn/rea-codex)（重建 Claude Code 和 Codex CLI）。
- **硬件/协议**（12 个）：Xiaomi/Samsung/Apple TV 遥控、Govee 灯、Sony 耳机、Yamaha 功放串口、AIO 水冷屏、VR 串流 Linux 版。
- **分析/评测/对抗**（18 个）：bzip2、netcat、EVM 合约、视频编码参数，以及反 REA 的 [anti-rea](https://github.com/848gaming/anti-rea)、[reaproof](https://github.com/Junhan2/reaproof)、[die-a-rea](https://github.com/danihelenterprises/die-a-rea)。
- 列表专门注明 storytold 的 Adobe/Office 「Craft」系列作者自称**净室实现、没有逆向**，只是同一周走红（HN 里作者 echelon 本人也这么说：[50029110](https://news.ycombinator.com/item?id=50029110)）。

---

## 3. 其他地方的讨论

### 3.1 Hacker News

- 主帖 [50028275](https://news.ycombinator.com/item?id=50028275)（10-10，812 分、339 条评论）；重复帖 [50058850](https://news.ycombinator.com/item?id=50058850)（10-12，99 分）；404 Media 文章帖 [50167587](https://news.ycombinator.com/item?id=50167587)。
- **「比直接让 Claude 用 Ghidra/radare2 好在哪」**是主帖第二高楼（ethin [50028470](https://news.ycombinator.com/item?id=50028470)、cedws [50028743](https://news.ycombinator.com/item?id=50028743)）：有人说「可能不更好，面向没有 RE 方法的人」（[50028840](https://news.ycombinator.com/item?id=50028840)）；tptacek 认为人人都有自己的脚本，但目前仍需要比 Claude 自由发挥更结构化的东西（[50028484](https://news.ycombinator.com/item?id=50028484)）；一位本地慢模型用户说裸用 Ghidra CLI 迷路，换 REA 后「在推进」（[50029660](https://news.ycombinator.com/item?id=50029660)）。
- **安装方式**：首页「把这句话贴给 agent 让它装」被比作 `curl | bash` 的进化版（[50028433](https://news.ycombinator.com/item?id=50028433)、[50030587](https://news.ycombinator.com/item?id=50030587)）。
- **模型拒答**：Claude Code Opus 5.5 拒过一次、Codex 正常（[50029537](https://news.ycombinator.com/item?id=50029537)）；DRM/授权检查要降到旧模型（[50030822](https://news.ycombinator.com/item?id=50030822)）；反编译本身一般不拒，找漏洞更容易被拒（[50029557](https://news.ycombinator.com/item?id=50029557)）。
- **Android**：有人说 REA 走 jadx-mcp，jadx 预处理一个 APK 要几十分钟，没法批量分析 100 个大 APK（[50029899](https://news.ycombinator.com/item?id=50029899)）。
- **刷 star 质疑**：有人贴 10-05 约 4.2k → 10-10 54.6k star、fork 438 → 10,376，评「像机器人」（[50030534](https://news.ycombinator.com/item?id=50030534)、[50030919](https://news.ycombinator.com/item?id=50030919)、[50028349](https://news.ycombinator.com/item?id=50028349)）。
- **效果正面**：TH04 重建质量被评价「比很多 AI 反编译好、能匹配、命名合理」（[50029703](https://news.ycombinator.com/item?id=50029703)）。很多「我直接让 Claude 改二进制修了 RDP 的 bug」（[50030714](https://news.ycombinator.com/item?id=50030714)）这类故事**没说用了 REA**。
- **法律/行业**：版权、诉讼、SaaS 化、DRM（[50028933](https://news.ycombinator.com/item?id=50028933)、[50030599](https://news.ycombinator.com/item?id=50030599)、[50030872](https://news.ycombinator.com/item?id=50030872)、[50067077](https://news.ycombinator.com/item?id=50067077)）。

### 3.2 媒体、博客、视频

- **404 Media**，Jason Koebler，10-20：[‘Reverse-Engineer Anything’ Is the New Wild West of AI](https://www.404media.co/reverse-engineer-anything-is-the-new-wild-west-of-ai/)。把 REA 放在「AI 克隆软件、改智能电视/IoT」浪潮里，引用 REA 自己的口号（「看到别人 app 里的功能 → 让 agent 用 REA 查 → 给你的产品做一个」）；批评点是克隆软件质量低、bug 没人懂，会冲击小公司和逆向社区。文中电视、吉他效果器的例子**没说用了 REA**。
- **GitTested**（测 6.2.0，[链接](https://gittested.com/reviews/morluto-rea/)）：4 个去符号小程序 + 3 个 Electron 应用，Claude Code+Sonnet 5.5；普通工具 74/75 分、REA 73/75 分，REA 多用约 2.5 倍 token（约 1.5 倍费用），95 次调用里 92 次可用；Electron 全量分析大到一次工具调用装不下。结论「adopt with care」。
- **leanrea**（[链接](https://github.com/aicodeking/leanrea)）：用一个 8 KB 的 skill 代替 REA，直接调 asar/otool/nm/jadx/Ghidra。对比 REA 6.5.0：4 题×3 次，耗时 388 s vs 627 s、费用 $2.17 vs $3.36、分数 74/75 vs 75/75；「回一个 OK」时全量加载工具约 400.7k token vs 34.5k；真实 Electron 应用先被 asar 完整性拦下，加参数后 47.9 s、输出 140 MB JSON（asar 解包+rg 4.8 s、24 KB）。
- **Eigent 博客**（[链接](https://www.eigent.ai/blog/rea-reverse-engineer-anything)）：功能介绍为主；awesome 列表把它的观点概括为「主要是把现成 RE 工具接起来」。正文里我只看到介绍，没看到明确的批评段落。
- **DEV（daviddeng）**（[链接](https://dev.to/daviddeng/rea-explained-for-ai-coding-agents-5g6j)）：定位成「编排层不是反编译器」；看重 evidence_id/limitations、证据缺失就标 incomplete 的 fail-closed 设计；建议钉住版本。
- 没拿到正文的：Geeky Gadgets（被拦，awesome 概括为「IP 和 DRM 担忧」）、MindStudio（空页，awesome 概括为「分别用/不用 REA 重建 macOS 预览的部分功能并对比」）、YouTube [Eric Tech「How To Use REA To Clone Any Software In Minutes」](https://www.youtube.com/watch?v=FnB9tHKBJXQ)（只看到标题）。
- **X/Twitter、Reddit**：没拿到。只有间接证据：[HN 50034003](https://news.ycombinator.com/item?id=50034003) 发帖人说「X 上很多帖子在说 IP 和软件权利快不值钱了」。

---

## 4. 归纳

### 4.1 最常见的 5 种使用模式

| # | 模式 | 典型提问 | 典型工具序列 | 出处 |
|---|---|---|---|---|
| 1 | **原生二进制里定位一个计算/逻辑** | "Use REA to find how X calculates Y. Explain the calculation and show the code." | `open_binary` → `search_strings` → `xrefs` → `resolve_containing_procedure` → `analyze_function`（自己+调用者）→ `read_bytes` 读常量 | CTF、DX-Ball、TH04 showcase；[#2254](https://github.com/morluto/rea/discussions/2254) |
| 2 | **Electron/JS 应用追功能链** | "Use REA to find what happens when I click Z. Show where … and the files and lines." | `analyze_javascript_application`（目录或 asar，签名的应用加 `--record-integrity`）→ 看 `summary.ipc` → `trace_feature` → 人读 handler | Notion showcase、guides/javascript；[#623](https://github.com/morluto/rea/issues/623) |
| 3 | **APK 找一个算法** | "Use REA to inspect app.apk. Find how it generates …, and rebuild a small demo." | `inspect_android_package` → `search_android_classes` → `inspect_android_class` → `inspect_android_method` ×N → `trace_android_references` | Aegis showcase；[#893](https://github.com/morluto/rea/issues/893) |
| 4 | **网页运行时观测 / 自家站点外部审计** | "Use REA to inspect this page while I click …. What request does it make?" | `list_browser_targets` → `inspect_web_page`（观测窗口+JSON 形状）→ 加 `--include-script-sources` | guides/browser；[#1406](https://github.com/morluto/rea/discussions/1406) |
| 5 | **拆出行为后重写/移植/互通**（社区最热的用途） | 「把这个老游戏/工具/设备协议搞清楚，然后用 Rust/浏览器/Linux 重做」 | 上面 1～3 的组合 + REA 外的差分测试/编译回放；常和 hermes-dec、ILSpy、自写脚本混用 | awesome-rea 约 45 个项目；[#4152](https://github.com/morluto/rea/discussions/4152)；[#5496](https://github.com/morluto/rea/discussions/5496)（审计型变体） |

### 4.2 大家公认的好用之处

- **证据可回查**：每条结论带 evidence_id、observed/derived 标签、limitations；DEV 文章、[#2254](https://github.com/morluto/rea/discussions/2254)、[#1406](https://github.com/morluto/rea/discussions/1406)（证据 ID 直接进报告）都把这点当最大价值。
- **给不会 RE 的人/弱模型搭了路**：HN [50028840](https://news.ycombinator.com/item?id=50028840)、[50029660](https://news.ycombinator.com/item?id=50029660)；裁剪工具后 32K 本地模型也能答对（[#3098](https://github.com/morluto/rea/discussions/3098)）。
- **一套装好覆盖多种目标**：Ghidra PE、JADX、浏览器、WSL 上的 Ghidra 都在一台机器上通（[#1406](https://github.com/morluto/rea/discussions/1406)）。
- **反编译代码可读、和源码对得上**（GitTested）。

### 4.3 公认的不好用之处

- **工具目录太大**：207 个工具 / 3.35 MB 定义，挤爆上下文，Gemini/VS Code/Claude Desktop 的 schema 校验不过，本地模型装不下（[#4620](https://github.com/morluto/rea/issues/4620)、[#4421](https://github.com/morluto/rea/issues/4421)、[#3098](https://github.com/morluto/rea/discussions/3098)、leanrea）。
- **小目标上不比裸工具强、还更贵**：GitTested 约 2.5 倍 token；[#2090](https://github.com/morluto/rea/discussions/2090) $0.19 vs $0.08、准确率持平略低；leanrea 贵 35%。
- **大目标慢或直接超时**：Ghidra 冷导入每次 20 分钟、snapshot 不省（[#1048](https://github.com/morluto/rea/issues/1048)）；151 MB Mach-O 不返回（[#5315](https://github.com/morluto/rea/issues/5315)）；大 JS bundle OOM（[#623](https://github.com/morluto/rea/issues/623)、[#5132](https://github.com/morluto/rea/issues/5132)）；Electron 全量结果太大（GitTested、leanrea 140 MB）。
- **发版和文档跟不上**：修复在 main，npm latest 没跟（[#4973](https://github.com/morluto/rea/issues/4973) 时 6.6.0 在 npm 是 404）；skill 写了服务器不广播的工具（[#5244](https://github.com/morluto/rea/issues/5244)）；doctor 绿但实际不可用（[#5512](https://github.com/morluto/rea/issues/5512)）。
- **安全/法律**：「贴给 agent 让它装」像 `curl | bash`；会执行/注入的工具默认注册（本机深读 `06-process-setup-security.md`）；版权、DRM、克隆别人产品（404 Media、HN、[#4913](https://github.com/morluto/rea/discussions/4913)）；有人直接要求破壳（[#4662](https://github.com/morluto/rea/issues/4662)）。
- **模型拒答不是 REA 能管的**，但用户把它算到使用体验里（[#3038](https://github.com/morluto/rea/discussions/3038)、[#2095](https://github.com/morluto/rea/discussions/2095)）。
- **「案例偏简单」**：[#1928](https://github.com/morluto/rea/discussions/1928)；官方 evidence 文件自己也承认几个案例是人工选函数或借用已有工程边界（见 §1.3）。

### 4.4 最常见的 5 个卡点（按用户影响排）

1. **MCP 客户端吃不下工具 schema / 目录太大**：Gemini 400 [#4620](https://github.com/morluto/rea/issues/4620)/[#4025](https://github.com/morluto/rea/issues/4025)/[#5201](https://github.com/morluto/rea/issues/5201)，VS Code [#4736](https://github.com/morluto/rea/issues/4736)，Claude Desktop [#4973](https://github.com/morluto/rea/issues/4973)，token 撑爆 [#4421](https://github.com/morluto/rea/issues/4421)。
2. **长分析超时**：60 s 客户端超时 [#1720](https://github.com/morluto/rea/issues/1720)，120 s 转后台丢结果 [#5273](https://github.com/morluto/rea/issues/5273)，Ghidra 冷导入 [#1048](https://github.com/morluto/rea/issues/1048)，分析器卡死 [#1783](https://github.com/morluto/rea/issues/1783)。
3. **大目标内存/体积**：JS [#623](https://github.com/morluto/rea/issues/623)、[#5132](https://github.com/morluto/rea/issues/5132)，APK [#893](https://github.com/morluto/rea/issues/893)，Mach-O [#5315](https://github.com/morluto/rea/issues/5315)。
4. **Windows 原生权限/路径**：[#1701](https://github.com/morluto/rea/issues/1701)、[#5225](https://github.com/morluto/rea/issues/5225)、[#5237](https://github.com/morluto/rea/issues/5237)、[#5259](https://github.com/morluto/rea/issues/5259)、[#4707](https://github.com/morluto/rea/issues/4707)。
5. **安装/引擎探测**：Hopper 下载/检测 [#4340](https://github.com/morluto/rea/issues/4340)、[#2270](https://github.com/morluto/rea/issues/2270)、[#5245](https://github.com/morluto/rea/issues/5245)；JDK/Ghidra 路径 [#4556](https://github.com/morluto/rea/issues/4556)、[#5264](https://github.com/morluto/rea/issues/5264)、[#832](https://github.com/morluto/rea/issues/832)。

「ambiguous provider」只有 [#5446](https://github.com/morluto/rea/issues/5446)、[#4555](https://github.com/morluto/rea/issues/4555) 两条相关，**不在前五**。

### 4.5 维护者怎么回（看到的模式）

- 多数修复由社区贡献者认领、在 issue 里贴根因+commit，维护者 morluto 用「reconciled with main / qualified main `<sha>`」这种固定格式批量关单（[#1783](https://github.com/morluto/rea/issues/1783)、[#4555](https://github.com/morluto/rea/issues/4555)、[#1720](https://github.com/morluto/rea/issues/1720)）。
- 会公开收回过早的关单（[#1720](https://github.com/morluto/rea/issues/1720)「earlier closure was premature」）。
- 早期常见的回复是「升到某版本」（[#623](https://github.com/morluto/rea/issues/623)「update to rea 4.0.1」）；FAQ 的官方建议也是先 `rea update` 重试。
- 不少社区回复明显是 AI 写的（评论末尾带「Generated with Claude Code」），例如 [#1783](https://github.com/morluto/rea/issues/1783) 里 CassieFox23、[#1435](https://github.com/morluto/rea/discussions/1435)。**推断**：issue 区的大量高质量 bug 报告和回复都是 agent 产出的。
