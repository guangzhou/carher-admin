# Cursor 客户端 bundle 补丁 SOP(3.16.x;安装器跨平台,诊断/日志路径示例为 macOS)

对象:同事/本机 **Cursor.app 本体**的 workbench bundle 补丁与 BYOK 配置,与网关线
(zk-cursor-web-fc-iterate,改 198 CM)完全分层:**这边改的是用户 Mac 上的 Cursor,
那边改的是服务端 lane**。三个脚本全在 `scripts/zk-cursor-web/`:

| 脚本 | 用途 | marker |
|---|---|---|
| `cursor_team_setup.js` + `.sh`/`.cmd` | **首选**:同事一键装 cursor-g,零依赖跨平台(Win/mac/Linux) | 8 补丁全家(见下) |
| `cursor_team_setup.py` | 同上,mac 本地/CI 用的 Python 版(与 JS 同源、备份互通) | 同上 |
| `cursor_queue_pump_patch.py` | 单独打/升级排队泵(旧版 v1→v3.5 全支持原地升级) | `@cx-queue-pump:v4` |
| `cursor_queue_diag_patch.py` | 诊断用:把泵换成"每秒打 tick 日志"版,只观测不改行为 | `@cx-queue-diag:v3.2` |
| `regress_queue_pump.js` | 泵离线回归台架(注入真发货 snippet,17 判据+负对照) | — |

**补丁全家(3.17.19 现行 8 处)**:gate / localagent / dedicated(解锁三件套)+
`@cx-queue-pump:v4`(兜底泵)+ qserial / nosteermod / nopromote(排队纵深防御)+
**`@cxteam-norelay`(2026-08-25 连发折叠+僵尸真根因修,见下节)**。

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
- 测试钩子(env 门,随脚本发布无副作用):`CX_DUMP_CONSTANTS` 打常量、`CX_APPLY_TO_FILE`/
  `CX_APPLY_OUT` 对任意文件跑真实 PATCHES、`CURSOR_APP_ROOT`/`CURSOR_USER_DIR` 指假 app/user 目录、
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
  `REPAIR-Mac.command`/`REPAIR-Windows.cmd`。**软版本闸**:非 3.16.x 只告警不拒,
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

## 诊断方法论(内存态读不到时,可复用)

composer 运行时状态只活在 renderer 内存,workspace state.vscdb 里没有——**磁盘取证无效**。
正解三步:①先上**只加日志、不改行为**的 tick 补丁(diag 脚本,逐字节替换现行泵);
②复现:不需要手工发消息——**重启 Cursor 即触发启动重放**(恢复队列+重放提交),卡死自己复现;
③读 tick(status/hasUUID/bubbles/queueLen 每秒一行)定形态,再写修法。禁止跳过①③直接猜。

## 硬规矩

- **动 bundle 前 Cursor 必须完全退出**;卡死时 osascript quit 和 SIGTERM 都可能不响应
  (优雅退出被挂死的流卡住)→ 只能 `kill -9`,聊天记录持续落盘不丢。
- **锚点纪律**:稳定语义地标+通用捕获 minify 尾巴,全文件命中 ≠1 → 拒绝动手;
  落盘前整 bundle `node --check`。装过 CursorX 的机器 3 解锁锚点命中 0 → 自动拒(防重复打)。
- **升级策略**:默认允许 Cursor 自动升级,升级掀翻补丁后跑 `--repair` 一键重打
  (锚点=语义地标,3.16→3.17.19 全部存活);要锁死不升级才用 `--pin-update`。
- **snippet 单一来源**:三处 QP_SNIPPET(`cursor_team_setup.py`、`cursor_team_setup.js`、
  `cursor_queue_pump_patch.py`)必须逐字节一致(改一处必同步另两处并跑等价断言 `CX_DUMP_CONSTANTS`);
  升级泵版本时把旧版原文加进 OLD_SNIPPETS/QP_OLD 以支持原地升级。
- **revert 选备份**:优先选「含 bundle 的最新备份」,跳过 `-cfgonly`(幂等重跑只存配置的备份)
  ——否则二次 `--apply` 后 `--revert` 会漏还原 bundle(两版已同步修)。
- 回滚:各脚本 `--revert`(备份链 `~/.cursor-team-setup-backup` / `~/.cursor-queue-pump-backup`
  / `~/.cursor-queue-diag-backup`)。
- key 自动写仅 JS 版(in-process keytar+OSCrypt,写前双自检防假绿 401);
  Python 版进程非 Cursor 签名会弹框 → 仍提示在 Cursor 设置里粘 key。

## marker 演进

`@cx-queue-pump:v1`(只清无 uuid,救一半)→ `@cx-queue-diag:v2`(诊断 tick)→
`v3`(僵尸 uuid 判活)→ `v3.3`(去 120-tick 寿命上限)→ `v3.4`(在飞防护)→
`v3.5`(派发收敛:仅 heal 后/空闲派)→ **`v4`(现行:队列被外部消费让路 5 tick+
自家在飞豁免,退居纯兜底)**;根治靠 `@cxteam-norelay`(泵不再是主派发器)。
记忆:`feedback_cursor_queue_stuck_zombie_uuid_v3_liveness`、
`feedback_cursor_turnended_relay_orphan_fold_norelay_root_fix`;安装器设计与等价性证明:
`docs/cursor-g-naming-rollout-20260824.md` §七。
