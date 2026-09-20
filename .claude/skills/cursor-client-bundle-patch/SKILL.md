# Cursor 客户端 bundle 补丁 SOP(3.16.x~3.20.x;安装器跨平台,诊断/日志路径示例为 macOS)

对象:同事/本机 **Cursor.app 本体**的 workbench bundle 补丁与 BYOK 配置,与网关线
(zk-cursor-web-fc-iterate,改 198 CM)完全分层:**这边改的是用户 Mac 上的 Cursor,
那边改的是服务端 lane**。三个脚本全在 `scripts/zk-cursor-web/`:

| 脚本 | 用途 | marker |
|---|---|---|
| `cursor_team_setup.js` + `.sh`/`.cmd` | **首选,也是唯一发出去的**:同事一键装 cursor-g,零依赖跨平台(Win/mac/Linux)。09-15 起**按 AST 定位**(见下节),并带 `--uninstall` 恢复原状(见下节) | 8 补丁全家(见下);`@cx-chain:v3` 默认关+自动摘除 |
| `cursor_team_setup.py` | 同上的 Python 版,**纯正则、故意不上 AST**——不在 zip 交付物里,留作第③腿的跨实现参照 | 8 补丁全家(**无 chain**,legacy 不分发) |
| `cursor_chain_patch.py` | 件B 链式增量独立源(exthost fetch seam),移植进 `.js` 安装器的字节基准 | `@cx-chain:v3` |
| `cursor_queue_pump_patch.py` | 单独打/升级排队泵(旧版 v1→v3.5 全支持原地升级) | `@cx-queue-pump:v4` |
| `cursor_queue_diag_patch.py` | 诊断用:把泵换成"每秒打 tick 日志"版,只观测不改行为 | `@cx-queue-diag:v3.2` |
| `bundle_anchor_probe.js` | 只读数锚点命中(live Cursor 或**显式给 bundle 文件**,新版预检不用装) | — |
| `bundle_patch_regress.sh` | **改 PATCHES 后的四腿离线回归台架**(见「新版本漂移修复流程」) | — |
| `bundle_audit_daily.sh` + `com.zerokey.cursorbundleaudit.plist` | **每日自动巡检**:官方出新版当天自动跑全四腿并告警(见「每日自动巡检」) | — |
| `regress_queue_pump.js` | 泵离线回归台架(注入真发货 snippet,17 判据+负对照) | — |

**补丁全家(3.20.21 现行 8 处 workbench)**:gate / localagent / dedicated(解锁三件套)+
`@cx-queue-pump:v4`(兜底泵)+ qserial / nosteer-mod / nopromote(排队纵深防御,`nopromote` 是 multi)+
**`@cxteam-norelay`(2026-08-25 连发折叠+僵尸真根因修)**。
另有 exthost 侧 `@cx-chain:v3`(件B 链式增量):**服务端那半 09-01 已下线,安装器 `--chain` 默认关且
`--apply` 会自动摘除**残留补丁,源码仍留着备将来重启用(见末节)。

## 零依赖跨平台安装器(2026-08-25,首选路径)

同事不一定装了 Python,也可能是 Windows。解法:**Cursor 本体就是 Electron,自带完整 Node
运行时**(`ELECTRON_RUN_AS_NODE=1 <Cursor 可执行文件> setup.js`)+ 内置 `node:sqlite`
(备胎 Cursor 自带 `@vscode/sqlite3`)→ 用 Cursor 自己跑自己,不装任何东西。
- 启动器 `cursor_team_setup.sh`(mac/Linux)/`cursor_team_setup.cmd`(Win)只做一件事:
  找到 Cursor 可执行文件,以 node 模式跑 `cursor_team_setup.js`。`CURSOR_BIN=/path` 可覆盖。
- 安装目录/用户目录**全从 `process.execPath` 推导**(darwin `../Resources/app`、win/linux
  `resources/app`;用户目录按平台 Application Support / %APPDATA% / ~/.config),不猜路径。
- 退出检测排除**自身 pid**(脚本本就跑在 Cursor 二进制上,pgrep/tasklist 会看到自己)。
- 与 Python 版**逐字节等价**(合成 bundle 4 锚点补丁输出、GATE_FN/QP_SNIPPET/QP_OLD/baseUrl
  常量均 byte 相同)、**备份格式互通**(同 `~/.cursor-team-setup-backup`,可互相 revert)。
  正因为互通,**`--revert` 的同版本闸两边必须同步**(2026-09-15 已同步:py 也按目录名前缀
  比版本、也先 `isdir` 过滤散装 JSON)。⚠️ 造假 app 测 py 时环境变量**不是** `CURSOR_APP_ROOT`
  而是 **`CURSOR_APP`(指 `.app` 本身)**——喂错的话它静默读 live Cursor,测出来的"红"是假红(实测踩过)。
- 测试钩子(env 门,随脚本发布无副作用):`CX_DUMP_CONSTANTS` 打常量、`CX_APPLY_TO_FILE`/
  `CX_APPLY_OUT` 对任意文件跑真实 PATCHES(js/py **同名同语义**,内部都调 `applyPatchesToText`/
  `apply_patches_to_text`,与 `--apply` 同一份代码:marker 跳过 / 泵原地升级 / multi 全一致。
  09-10 前 js 那个钩子自己另写了一遍「每锚点恰好 1 次」,喂真 bundle 会在 multi 锚点 `nopromote` 上假红)、
  `CURSOR_APP_ROOT`(指的是 **Resources/app**,不是 `.app`)/`CURSOR_USER_DIR` 指假 app/user 目录、
  `CX_SKIP_RUNNING_CHECK` 跳退出检测——假 app 端到端回归全靠这几个,live Cursor 不用退。
- **Windows 路径分支未在 Mac 实测**(按标准安装布局写),诚实标注;逻辑已平台分叉。

## 自动写 Key + 默认模型 + 允许升级(2026-08-25,4 项体验优化)

目标=把同事"要在 Cursor 里手动做的事"降到接近 0。

- **自动写 Key(mac 实测,win/linux best-effort)**:Cursor 把 BYOK Key 存
  `secret://cursorAuth/openAIKey`,值=`{"type":"Buffer","data":[...]}`——Electron safeStorage
  的 OSCrypt 密文(`v10` 前缀 + AES-128-CBC)。主密码在 macOS 钥匙串「Cursor Safe Storage」/
  「Cursor」。**关键**:安装器跑在 Cursor 自己的已签名二进制里(`ELECTRON_RUN_AS_NODE=1`),
  **in-process `require` Cursor 自带 keytar**(`Resources/app/node_modules/keytar`)读钥匙串
  **不弹框**(ACL 认 Cursor 签名);`/usr/bin/security` CLI 会弹(不同二进制)。派生
  `PBKDF2(pw,"saltysalt",mac 1003/linux 1,16,sha1)`,iv=`Buffer.alloc(16,0x20)`,`"v10"`+AES-128-CBC。
  取 Key=`--apply` 末尾 `readline` 提示粘一次(或 `--key` 无人值守);非 TTY/空→回退手填。
  **写前两道自检**(能解开 Cursor *现有* Key=方案与本机一致的强 oracle + 加密回环 `===`)才落盘,
  任一不过→跳过并提示手填(**防假绿 401**)。win=DPAPI 未实测→回退手填。
- **⚠️ `openDb.set` 必须 UPSERT 不能 UPDATE**:`openAIKey` 行在从没填过 key 的新用户机器上
  不存在,`UPDATE ... WHERE key=?` 会 0 行静默成功=假绿(打印"已写入"其实没写)。用
  `INSERT ... ON CONFLICT(key) DO UPDATE`。fake-app E2E(无 key 行)实测复现过。
- **默认模型(#3)**:活字段=`aiSettings.modelConfig.composer`(还有 `cmd-k`),
  形如 `{modelName, selectedModels:[{modelId,parameters:[]}]}`。设为 `cursor-g-5.6-sol`;
  **保守——仅当当前非任一 cursor-g 时才设**,不覆盖用户已选。
- **允许升级 + REPAIR(#4)**:默认**不再**写 `update.mode:none`(`--pin-update` 才锁)。
  新增 `--repair`=只重打 bundle 补丁(升级后失效一键修,不碰配置/Key)。分发包加
  `REPAIR-Mac.command`/`REPAIR-Windows.cmd`。**软版本闸**:不在 `VERIFIED_VERSIONS` 里只告警不拒,
  真正安全阀仍是"锚点 exactly-1"(结构变了认不出→拒绝,绝不改坏)。
- **revert 扩展**:备份+还原新增 `openAIKey.secret.json`(现有 Key 原值),回滚含 Key secret。
- **Python 版 Key 写入=JS-only**:Python 进程非 Cursor 签名,读钥匙串会弹框;Py 版
  `--apply` 仍提示"在 Cursor 里粘 Key",但同步了默认模型/软版本闸/允许升级/`--repair`。

## 排队卡死 bug(原厂,2026-08-25 v3 端到端闭环)

**真形态(诊断 tick 实测,推翻两轮猜想)**:一轮 `Stream completed successfully` 之后,
composer status 仍=`generating` 且 `chatGenerationUUID` **残留**(hasUUID=true/bubbles=0
持续 2min+),后续消息进 addToQueue 队列后永不派发=静默丢。该负载下**几乎每轮完成后都卡**
(收尾复位被跳过,伴随 "Failed to resolve hook model legacy slug" 报错)。重启不救——
启动会自动重放队列并原样复现。

**为什么难判**:真生成时 hasUUID 也 true、bubbles 也 0——光看形态分不清真假。
**铁判据(抄 bundle 自己的口径)**:`aiService.streamingAbortControllers.has(uuid)`——
每个真生成登记控制器,流一结束(完成/中止两条路径)必被 `.delete()`。

**v3 修法**(泵 tick 内,泵类可达链 `this.composerChatService._aiService.streamingAbortControllers`):
- uuid 在但表里没它 = 僵尸标记 → 连续 **3 tick** 确认后
  `updateComposerData({status:"completed", chatGenerationUUID:void 0, generatingBubbleIds:[]})`
- 表里有它 = 真在生成 → 绝不碰(实测 70s 长生成零误杀)
- 摸不到表(未来版本改名)= 退化 v1 只清无 uuid 形态(fail-safe 不更坏)

**验收判据**:结构化日志 `[cx-queue-pump] healed stuck generating status` 带 `hadUUID` 字段;
端到端=积压队列逐条自动派发(完成→~3s heal→下一条发出→完成…)。日志位置:
`~/Library/Application Support/Cursor/logs/<session>/window*/exthost/anysphere.cursor-always-local/*Structured*.log`

## 连发折叠+僵尸真根因(2026-08-25 六轮实测闭环,@cxteam-norelay)

v3 之后仍复发「两问挤一轮只答后一条」(甲乙丙丁戊己/壬癸子丑寅卯 事故),泵连背三层黑锅
(v3.3 疑起跑误杀→v3.4 疑穿闸→v4 让路了仍折叠),最终锁死在**官方 turnEnded 处理器**:

- 有排队消息时官方走「轮内接力」:弹队首+appendQueuedHumanMessage 写气泡+
  `submitConversationAction` 续进**当前 agent 循环**,然后 `break`——**跳过 status 复位**
  (官方设计:接力=同轮继续)。
- 但 BYOK 本地线 agent 循环在 `Request successful` 即退出,接力消息没人接:
  ①status 永卡 generating(**僵尸真身**;"Failed to resolve hook model legacy slug" 只
  log 返 "unknown" 不崩,旧归因作废);②弹队消息成孤儿气泡,被下一请求
  `prepending user messages` 原生机制捎走 → 一请求两问只答后一条(**折叠真身**)。
- **修**:能力闸门 `k_d(agentBackend==="cursor-agent"&&!isLocalMode&&!isAgentHostEnabled&&
  !isNewRequestIdGateEnabled())` 在本线误判 true → 调用点(语义锚
  `isNewRequestIdGateEnabled:()=>this.isQueuedPromptNewRequestIdEnabled()`,desktop/glass
  各 exactly-1)插 `!1&&` → turnEnded 走复位分支 → 官方队列机制逐条各自成轮。
- **验收**:修后 12 问 12 答严格交替、全程零 heal 零 prepend、status=completed;
  折叠类问题四个硬观测=①每周期 requestId 数 ②convLen 增量(+2 用户气泡=折叠)
  ③`prepending user messages` 出现=有孤儿 ④队列掉但无派发日志=接力在弹队。
  记忆:`feedback_cursor_turnended_relay_orphan_fold_norelay_root_fix`。

## 件B 链式增量 @cx-chain:v3(2026-08-30 上线,**2026-09-01 服务端下线后默认关**)

⚠️ 现状:配套服务端 chain-srv+shim 09-01 已下线,安装器
`--chain` **默认关**,`--apply` 会把老包留在客户端的残留补丁**自动摘掉**(留着会少发上下文=模型忘事)。
下面是当时的实现记录,将来重启这条线照它走(记忆 `project_cx_chain_shim_decommissioned_2026_09_01`);
3.19.19 上锚点 `customHeaders:d}=e,m=` 已消失(非阻断:`planChainBundle` 认不出只 warn+skip)。

配套服务端件A(网关 chain-srv 重建),让 Cursor 长会话发 delta+`previous_response_id` 而非全量重放,
根治 LiteLLM pre-call 1.05M 上限(400 ContextWindowExceededError)与上游 413。独立源
`cursor_chain_patch.py` 是**字节基准**,已逐字节移植进 `cursor_team_setup.js`(08-30~09-01 曾随 zip 全员发)。

- **打的是另一批 bundle**:不是 workbench,是 **exthost** 两条 `extensions/{cursor-agent-exec,
  cursor-local-agent-runtime}/dist/main.js`(ai-sdk 管道 fetch seam 在这)。安装器 `CHAIN_TARGETS`。
- **三处变换**(`planChainBundle`/`wrapBuilderCall`):①锚 `customHeaders:d}=e,m=` 后**括号配平**
  包住工厂底层 fetch → `(globalThis.__cxWrap||(f=>f))(builder(...))`;②SDK 客户端 `fetch:t.fetch` 包 wrap;
  ③**force-responses**:`?"responses":"chat_completions"`→`?"responses":"responses"`(不强制则 terra
  模型走 chat_completions,shim 无请求可拦)。shim 前置 prepend。
- **shim 必须 base64 内嵌**(`CHAIN_SHIM_B64`,运行时 `Buffer.from(...,'base64')` 解码):shim 含正则
  反斜杠 `\s \/ \n`,直接写 JS 字面量会被吃转义 → base64 是唯一逐字节等价搬运法。这是与 QP_SNIPPET
  "三处逐字节一致"同源的纪律,只是介质换成 b64。
- **碰撞坑**:两条 bundle 都叫 `main.js` → 备份/回滚用扁平名 `cxchain__extensions__…__main.js`
  (`chainBakName`),不能按 basename 存(会互相覆盖)。workbench/blob/settings 备份格式**未动**,
  与 py 版 revert 互通保持。
- **非致命降级**:`planChainBundle` 锚点没了(未来版本改结构)只 `warn+skip`,**不阻断**核心解锁补丁
  ——链式是增益,失配时 shim 自身也全量回退(`CX_CHAIN=0` 关 + 前缀 digest 失配/400/404 回退全量,不劣化)。
- **逐字节自测判据**(`node --check` + 等价钩子):安装器 `CX_CHAIN_APPLY_TO_FILE` 钩子在
  `.pre-cxchain.bak` 上跑变换 → 与当前真实 main.js(=py 产物)`cmp -s` **BYTE-EQUAL** 两条全过
  (md5 `4e416c21`/`24acdab1`)。改任一处必重跑此钩子对齐 py 基准。
- **验收只认网关** `[chain-srv] hit stored=N delta=M -> full=K`——**答案对不算证据**(acct82 账号级
  memory 会假阳性)。客户端 trace `passthrough-full` bodyLen>1.05M 是双 wrap 记账假象,别据此判 400。
- 记忆:[[project_cx_chain_client_shim_verified_2026_08_30]]、[[project_chain_srv_gateway_deployed_2026_08_30]]。

## 诊断方法论(内存态读不到时,可复用)

composer 运行时状态只活在 renderer 内存,workspace state.vscdb 里没有——**磁盘取证无效**。
正解三步:①先上**只加日志、不改行为**的 tick 补丁(diag 脚本,逐字节替换现行泵);
②复现:不需要手工发消息——**重启 Cursor 即触发启动重放**(恢复队列+重放提交),卡死自己复现;
③读 tick(status/hasUUID/bubbles/queueLen 每秒一行)定形态,再写修法。禁止跳过①③直接猜。

## AST 定位层(2026-09-15 上线,`cursor_team_setup.js` 现在的**首选**定位方式)

**动机**:每次 Cursor 发版都要手改正则锚点,起因不是"锚点写得不好",而是**正则在给 minify
名字定位**。实测的稳定律:**源码里写死的方法名/属性名跨版本逐字节存活**(`getQueueMessageBehavior`、
`promoteQueueItemToSteer` 这类 15 个 token 在 3.16.29→3.20.21 计数近乎不变),**churn 全在 minify
出来的局部标识符上**(`qUy`→`$7y`)。所以解法是**根本不提名字**:按 AST 结构定位,minify 名只从
AST 子节点的 `start/end` 里 `src.slice()` 出来搬运,不写进任何模式。

- **acorn 零新依赖**:Cursor 自带 `resources/app/node_modules/acorn`(8.17.0),mac 和 win32
  **两边都有**(`innoextract -l` 验过)。同事跑的是 `ELECTRON_RUN_AS_NODE=1 <Cursor 可执行文件>`,
  默认堆 4096MB,glass 解析实测 **733MB / 6.3s** → 不用调 `--max-old-space-size`。
  ⚠️ **`acorn-walk` 没随 Cursor 发**,所以 `astWalk` 是自写的类型无关栈式遍历。
- **acorn 必须取自"正在跑的运行时",不是"被打的目标"**(`runtimeRoot()` 排在 `RES` 前面):
  真实安装里两者同一个目录,但台架的假 app 里不是,顺序写反 → 找不到 acorn → 静默退回正则。
- **单次解析 + 逆序落刀**:每个补丁各自重解析要 8×~7s ≈ 67s/bundle;改成一次解析、把 8 处
  改动收进 `astEdits`、按 `start` **降序**依次替换 → **9.1s**,且完全不用做偏移量算术。
- **`LOCATORS` 里三个判别器是踩出来的,别删**:
  - `localagent`:目标的 `consequent` 必须是 `BlockStatement` 且首句是 `TryStatement`
    ——否则会多命中一处日志 helper(它的 consequent 是**裸 `TryStatement`**)。
  - `nopromote`:`n.value.async !== true` 直接 return ——非 async 那处是 `promoteQueueItemToSteer(t){return Promise.resolve(!1)}` 的 unsupported 存根。
  - `nosteer-mod`:`case"send"` 必须是**空 case 且紧跟 `case"queue"`** ——`case"queue"` 全文出现 7~8 次,
    只靠它定位会命中错的那处(第一版就是这么错的,靠一个 `behavior` 子串检查侥幸过关)。
- **安全阀原样保留**:AST 分支同样是"非 multi 命中 ≠1 → `process.exit(2)`,一个补丁都不打"。
- **禁止静默退回正则**:退回的提示是无条件 `console.log`(不是 `say`),且台架/巡检一律带
  `CX_REQUIRE_AST=1` → AST 没跑起来直接 `exit 3`。**这条是 09-15 踩出来的**:第一次 AST 跑只花
  0.98s,因为系统 node 下 `RES` 由 `process.execPath` 推导 → 指向 node 自己的目录 → 没有 acorn →
  静默退回正则,四腿全绿但**AST 一行没跑**=假绿。
- **`CX_FORCE_REGEX=1`** 强制走正则,用来取基线做**逐字节对照**(下面第 4 步的硬门)。
- **`cursor_team_setup.py` 仍是纯正则,故意不动**:`cursor-g-setup.zip` 里 `unzip -l` 确认
  **只发 js**,py 不在交付物里 → 让它留在正则上,正好当第③腿的**跨实现独立参照**。
- **诚实的边界**:AST 让"每版重新审"从**改代码**降成**跑一遍台架**,但**消灭不了它**——官方真把
  那段逻辑重构掉时,结构判别器同样会失配(这时安全阀报 hits=0 拒绝动手,行为与正则时代一致)。

## 每日自动巡检(`bundle_audit_daily.sh` + LaunchAgent,2026-09-15)

坏形状:同事升级 Cursor → 补丁全掉 → 要等他反馈"模型不见了"才知道。巡检把发现时间提前到**官方放版本当天**。

- 每天 **10:40**(躲整点)问一次官方 stable 版本号(~1KB)。**版本没变就静默退出,不下 dmg**。
- 变了才跑 `bundle_patch_regress.sh --fetch` 全四腿,**绿红都告警**——绿也要出声,否则"没告警"
  分不清是"没新版"还是"巡检自己挂了"。拿不到版本号也告警并 `exit 1`。
- **只有全绿才写状态文件** `~/.cursor-bundle-audit/last_audited_version`;红的话不写 → 明天重跑同一版,
  不会因为"审过了"而静音。
- 装:`cp com.zerokey.cursorbundleaudit.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) <plist>`;
  卸:`launchctl bootout gui/$(id -u)/com.zerokey.cursorbundleaudit && rm <plist>`。日志
  `/tmp/cursor_bundle_audit.{log,err}` + `~/.cursor-bundle-audit/logs/`。
- 飞书推送只从 `FEISHU_WEBHOOK` 环境变量读,**文件里不含任何硬编码 webhook**;判"送到了"看 body
  `code==0`,HTTP 200 不算。
- **写这个脚本踩的三个 sh 坑**(改的时候别"简化"回去):
  1. BSD sed 的 BRE **不认 `\(A\|B\)` 交替** → 告警正文里的失败项会**整段静默消失**。
  2. `grep -c` 没命中时**打印 0 且退出码 1** → `$(grep -c … || echo 0)` 吐出两行 `0\n0`,告警里出现 `FAIL=0\n0`。用 `wc -l` 或加 `head -1`。
  3. UTF-8 locale 下 bash 会把紧跟变量的**中文标点吞进变量名** → `$SKIPN。` 触发 `set -u` 的
     `unbound variable` 把整条告警打掉。变量紧跟中文字符必须写 `${SKIPN}`。

## 硬规矩

- **动 bundle 前 Cursor 必须完全退出**;卡死时 osascript quit 和 SIGTERM 都可能不响应
  (优雅退出被挂死的流卡住)→ 只能 `kill -9`,聊天记录持续落盘不丢。
- **锚点纪律**:稳定语义地标+通用捕获 minify 尾巴,全文件命中 ≠1 → 拒绝动手;
  落盘前整 bundle `node --check`。装过 CursorX 的机器 3 解锁锚点命中 0 → 自动拒(防重复打)。
- **⚠️ 捕获 minify 名字一律用 `[A-Za-z0-9_$]+`(js `ID`/py `_ID` 常量),永远不要用 `\w+`**:
  `\w` = `[A-Za-z0-9_]` **不含 `$`**,而 JS 合法标识符首字符含 `$`,esbuild/terser 名字用尽
  就大量吐 `$xx`(3.20.21 glass 里 1163 个不同的 `$` 名、5186 处调用点,不是偶发)。
  **2026-09-15 就是这么栽的**:3.20.21 glass 的 gate 包装函数名 `qUy`→**`$7y`**,`\w+` 认不出
  → hits=0 → 整体拒绝 → 一个补丁都没打;同版本 desktop 抽到 `l_f`(下划线在 `\w` 里)所以躲过,
  于是**报错只出现在第二条 bundle 上**(同事日志里 desktop 的"将打 [8 个]"成功行紧跟 gate 命中=0)。
  同理参数位别钉死 `[a-z]`(gate 旧写法)或必填 `\w+`(nopromote 旧写法,用 `ID1` 允许空参)。
  **诊断口径:先分清是哪条 bundle 报的**——`planBundle` 按 desktop→glass 顺序打印,
  拒绝行之前若已有一条成功的"将打 [...]",漂的就是 glass 那条,别去查 desktop。
- **升级策略**:默认允许 Cursor 自动升级,升级掀翻补丁后跑 `--repair` 一键重打
  (锚点=语义地标,3.16→3.19.19 全部存活);要锁死不升级才用 `--pin-update`。

### 新版本漂移修复流程(3.18.25 / 3.19.19 / 3.20.21 三次都按这个走)

同事报「锚点命中=0 → 拒绝动手」= 那处代码被官方重构了,**不是模型/LiteLLM 问题**。
不需要同事的机器,自己**离线拿到那一版 bundle** 就能修完并回归:

1. **取新版 bundle**(不用装):
   `curl -s https://api2.cursor.sh/updates/api/download/stable/darwin-arm64/cursor` → `downloadUrl`+`version`;
   下 dmg → `hdiutil attach -nobrowse -readonly` → 拷 `Cursor.app/Contents/Resources/app/out/vs/workbench/workbench.{desktop,glass}.main.js`。
   ⚠️ dmg 没下完就 `hdiutil attach` 会报"无可装载的文件系统"(`file` 认成 zlib data),先比 `Content-Length` 再挂。
   **1b. 同事在 Windows 时必须另抽一次 win32 bundle**——win32 是独立构建,不能拿 mac 产物替它作证。
   Cursor **不发 Windows zip**(`win32-x64-archive`/`-zip`/`win32-x64` 全重定向到安装器 exe),
   而那个 exe 是 **Inno Setup 6.4.0.1**(不是 NSIS):`7zz` 只解析出 PE 节 + 一个 211MB 的 `[0]` 条目,
   带 glob 抽出 **0 个文件**且照样打印 `Everything is Ok`——**别把这个当"包里没有"**。
   homebrew 的 `innoextract 1.9` 只认到 6.3(`Unexpected setup data version: 6.4.0.1` → 头解析失败),
   **从源码编 HEAD**:`git clone .../innoextract && brew install cmake && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j8`。
   抽:`innoextract -e -d win -I workbench.desktop.main.js -I workbench.glass.main.js <exe>`——
   `-I` **只匹配文件名**,给全路径时 `-l` 列得出来但 `-e` 抽 0 个(静默,极易误判)。
   产物落在 `win/code$GetDestDir/resources/app/out/vs/workbench/`(路径含 `$`,shell 里记得转义)。
   2026-09-15 实测 3.20.21:**win32 与 mac 的 minify 名字逐字相同**(desktop `l_f`、glass `$7y`),
   但这是一次观测,不是可以省掉这步的理由。
2. **数命中**:`node bundle_anchor_probe.js ./cursor_team_setup.js <新版两条 bundle>`(探针 09-10 起收显式文件参数,
   不用 live Cursor)。只修 hits=0 的那个,别顺手动别的。
   **2b. 先看是不是根本不用改**(09-15 起):js 走 AST 定位,minify 名字漂移**不再需要动代码**。
   探针数的是正则命中(py 那条路),js 那条路要单独验:
   `env ELECTRON_RUN_AS_NODE=1 CX_REQUIRE_AST=1 CX_APPLY_TO_FILE=<bundle> CX_APPLY_OUT=/tmp/a.js <Cursor 可执行文件> cursor_team_setup.js`
   ——`exit 3` = AST 没跑起来(找不到 acorn/被迫退回正则),`exit 2` = 结构真的变了(这才要改 `LOCATORS`)。
3. **看新形状再写正则**(py 侧,和 `LOCATORS` 的结构判别器同理):把老锚点附近的语义地标(如 `getQueueMessageBehavior`)grep 出来读官方新代码,
   **确认新代码的语义落点**(3.19 把 switch 搬进纯函数 `Meo`,且修饰键分支新增 `stop-and-send` 出口,
   照抄旧改法会漏)。正则 `(?:旧|新)` 两代都认,命中仍必须**恰好 1 次**。
4. **js/py 同步 + 四腿回归 = 一条命令**(全部离线,live Cursor 不用退):
   `sh bundle_patch_regress.sh --new-bundles <新版 bundle 目录>`(或 `--fetch` 让它自己下 dmg 抽 bundle,
   第 1 步就省了)。四腿分别是:
   ①新版两条 bundle 每个非 multi 锚点 exactly-1(`nopromote` 是 multi,≥1);
   ②**旧版 pristine bundle**(`~/.cursor-team-setup-backup/<live 版本>-<ts>/`)打完的产物与**当前 live 已打 bundle**
   `cmp` **BYTE-EQUAL**(= 旧代零漂移,这条是防"修新版把老用户改坏"的真门;⚠️**必须在自己的 Cursor 升级前跑**,
   live 一旦升到新版就没有同版本参照物了,台架会 SKIP);
   ③**js(AST) 产物与 py(正则) 产物 byte-equal** —— 09-15 起这腿升级成**跨实现对照**:js 走 AST、py 走正则,
   两条独立路径产出必须逐字节相同。台架**强制用 Cursor 的 Electron 当 node**(`CXNODE` + `CX_REQUIRE_AST=1`),
   拿不到 Cursor 可执行文件就**判红而不是判绿**——否则系统 node 下退回正则,这腿退化成"正则 vs 正则",
   AST 一行没跑却全绿。两边 rc 都非 0 时按"两实现一致拒绝"处理(该版本形状确实不匹配,如 3.16.29 的
   `nosteer-mod`:那版 `behavior` 是 `"stop-and-send"`,压根没有 steer 出口要修);
   ⚠️ **台架里不许把 `env ELECTRON_RUN_AS_NODE=1 "$CXNODE"` 包成 shell 函数**:POSIX sh 里
   `VAR=x somefunc` 的前置赋值会**留在当前 shell**(不像外部命令只作用于那一次调用)→ 第③腿的
   `CX_APPLY_TO_FILE`/`CX_APPLY_OUT` 泄漏到第④腿,让 `--repair` 走进测试钩子,日志里只有 `applied:`
   没有 `修复完成`,**第④腿全红且原因完全指错方向**(09-15 实测踩过);
   ④假 app 端到端 `--repair`:两条 bundle 都出 `patched:` + marker 落盘 + `node --check` 过 + 再跑一次「无需修复」=幂等。
   **SKIP 不是 PASS**,台架末尾会单独报有几腿跳过。改完正则先跑一次**负对照**(故意把锚点写错,四腿必须全红)。
   **4b. 改 AST 定位层时的硬门**:`CX_FORCE_REGEX=1` 取正则基线,和 AST 产物 `cmp` **逐字节相同**,
   4 个版本 × 2 条 bundle 全过才算。09-15 实测:3.17.19/3.18.25/3.20.21 六条全 BYTE-EQUAL;
   3.16.29 两条 `AST_rc=2 == RX_rc=2`(同上,两实现一致拒绝,不是回归——跑 `git show HEAD:` 那版复现过同样的 hits=0)。
   **②腿 SKIP 时的替代判据(2026-09-15 用过)**:live 已升级、拿不到同版本参照物时,别把 SKIP 当过——
   改成「**只把改动的那几个字符回退**成旧形状生成基准实现(其余代码=当前版本,同一份钩子同一份
   `applyPatchesToText`),对 `~/.cursor-team-setup-backup/` 里**任意几个旧版 pristine** 各出一次产物,
   `cmp` 必须 BYTE-EQUAL」。这证明的是"新正则对旧版零漂移",比原②腿弱(不含 live 实机比对)但可复现,
   报告时必须**明说是替代判据**。⚠️ 别用 `git stash`/`git cat-file HEAD` 造基准:HEAD 可能落后于工作区
   已有的钩子重构(09-15 踩到——HEAD 版还是 09-10 前那个自写"每锚点恰好 1 次"的旧钩子,喂真 bundle
   在 multi 锚点 `nopromote` 上直接假红退出、连产物都不写,`cmp` 于是报"文件不存在"极易被误读成 DIFFER)。
5. `VERIFIED_VERSIONS` 加新大版本(软闸只告警,真安全阀是 exactly-1)→ `python3 setup_impl_parity.py`
   → `sh package_team_setup.sh` 重打 zip → 换飞书文档 `OQCPdLd4MovEVoxGzdMcD3CJnCf` 附件。
6. 同事侧动作:**只需双击 `REPAIR-Mac.command`**(新包)。

- **snippet 单一来源**:三处 QP_SNIPPET(`cursor_team_setup.py`、`cursor_team_setup.js`、
  `cursor_queue_pump_patch.py`)必须逐字节一致(改一处必同步另两处并跑等价断言 `CX_DUMP_CONSTANTS`);
  升级泵版本时把旧版原文加进 OLD_SNIPPETS/QP_OLD 以支持原地升级。**同理 chain shim 单一来源**:
  `cursor_chain_patch.py` 的 SHIM 是基准,`cursor_team_setup.js` 里是它的 base64(`CHAIN_SHIM_B64`),
  改 shim 必重生 b64 并跑 `CX_CHAIN_APPLY_TO_FILE` 对齐 py 产物 byte-equal(见末节)。
- **revert 选备份**:优先选「含 bundle 的最新备份」,跳过 `-cfgonly`(幂等重跑只存配置的备份)
  ——否则二次 `--apply` 后 `--revert` 会漏还原 bundle(两版已同步修)。
- 回滚:各脚本 `--revert`(备份链 `~/.cursor-team-setup-backup` / `~/.cursor-queue-pump-backup`
  / `~/.cursor-queue-diag-backup`)。**`--revert` 现在只认同版本备份**,见下节。
- key 自动写仅 JS 版(in-process keytar+OSCrypt,写前双自检防假绿 401);
  Python 版进程非 Cursor 签名会弹框 → 仍提示在 Cursor 设置里粘 key。

## 恢复原状:`--uninstall`(2026-09-15,双击器已进交付包)

**两条路,不是一条**:

| | `--revert` | `--uninstall` |
|---|---|---|
| 做什么 | 把最近一份**同版本**备份整包盖回去(bundle+blob+settings+Key) | bundle 从同版本备份还原 + BYOK 配置清掉 + 摘 Key + 卸小代理 + 去 `update.mode` 锁 |
| 版本不匹配时 | **拒绝并 `exit 1`**,指向 `--uninstall` | bundle 这半让他用官方安装包覆盖安装,**配置/Key/小代理照样清干净** |
| 适合谁 | 刚装完想撤回、备份就是当前版本 | 任何时候要"恢复原状",尤其是中间升过 Cursor 的机器 |

三个坑,都是看真数据定的:

- **跨版本盖 bundle 会把 Cursor 弄坏**。旧 `--revert` 没有版本比较,这台机器上就是
  live 3.20.17 / 最新含 bundle 备份 3.18.25 的组合,直接盖等于把 3.18 的 workbench 塞进 3.20 的 app。
  现在 js/py 两版都按目录名前缀 `^(\d+\.\d+\.\d+)-` 取版本,不同就拒。
- **备份根目录里有散装 JSON**(zk-delta 指纹之类,7 个),名字排在版本号后面,
  `readdirSync().sort()` 的最后一项会挑到**文件**当备份目录。两版都改成先 `isDirectory()` 过滤。
- **不能靠 marker 反向摘补丁**:`dedicated`/`localagent` 两个补丁把原文改掉了(不是包裹),
  没有可逆的原文。所以"恢复原状"只有"拿原厂字节盖回来"一条路 —— 同版本备份,或官方安装包。

配置清理的三条判据(台架实测定的,不是推的):

- **不能 `delete mc[feat]`**:那个对象里还有 `maxMode` 兄弟设置,整键删会连带抹掉。
  改成写回 Cursor 自己对"没选过"的表示法(`modelName`/`selectedModels` 都是 `default`)。
- **要扫全部功能位**:安装只设 `composer`/`cmd-k`,但同事可能自己在 `deep-search` 等里挑了 cr-g
  (实测本机 `modelConfig` 有 8 个键)。按**值**判而不是按键名判。
- **不能一律重置成 `default`**:`--apply` 会把 `composer` 从他自己的模型换成 `DEFAULT_MODEL`,
  卸载时库里已经没有原值了,写 `default` 等于**静默弄丢他的选择**。
  装前的值在同一份备份的 `applicationUser.blob.json` 里 —— 优先从那儿取,取不到才退 `default`。
  (这条是台架报红报出来的:装前 `composer=claude-opus-4.6`,卸完变 `default`。)

交付包里是 `UNINSTALL-Mac.command` / `UNINSTALL-Windows.cmd`:**先跑一遍不带 `--apply` 的预演**
列出要动什么,同事输入 `yes` 才真执行(bundle 覆盖不可逆,不做静默执行)。
`package_team_setup.sh` 的占位符替换现在**扫整个 stage** 不只 README —— 启动器里也写了
`__MODEL_PREFIX__`,只替 README 的话同事双击看到的是字面量。

判据(全部实测过,每条都配阳性对照):

```
A) bundle 与 pristine 逐字节相同        ✅ desktop + glass
B) marker 全清 = 0                      ✅ (对照:patched 产物同尺子报 9,尺子有判别力)
C) 库回到装前形状                        ✅ 9/9
   composer 还给 claude-opus-4.6(连 parameters)/ maxMode=True 没丢 /
   cmd-k 归 default / deep-search 别人的第三方模型没被动 /
   userAddedModels 与 modelOverrideEnabled 只剩别人的 / baseUrl=""+useKey=false
D) 无同版本备份分支                      ✅ 给官方重装指引,旧版 bundle 没被盖上去
E) --revert 版本不匹配                   ✅ rc=1 且不动 bundle(对照:同版本 rc=0 且真还原)
F) 预演不落盘                            ✅ bundle/库 md5 都没变(对照:带 --apply 两者都变)
```

## marker 演进

`@cx-queue-pump:v1`(只清无 uuid,救一半)→ `@cx-queue-diag:v2`(诊断 tick)→
`v3`(僵尸 uuid 判活)→ `v3.3`(去 120-tick 寿命上限)→ `v3.4`(在飞防护)→
`v3.5`(派发收敛:仅 heal 后/空闲派)→ **`v4`(现行:队列被外部消费让路 5 tick+
自家在飞豁免,退居纯兜底)**;根治靠 `@cxteam-norelay`(泵不再是主派发器);
链式增量线独立演进:**`@cx-chain:v3`**(exthost fetch seam,件B,见末节)。
记忆:`feedback_cursor_queue_stuck_zombie_uuid_v3_liveness`、
`feedback_cursor_turnended_relay_orphan_fold_norelay_root_fix`;安装器设计与等价性证明:
`docs/cursor-g-naming-rollout-20260824.md` §七。
