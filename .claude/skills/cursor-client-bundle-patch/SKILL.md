---
name: cursor-client-bundle-patch
description: "Cursor 客户端 bundle 补丁 SOP(3.16.x~3.24.x):改用户本机 Cursor.app 的模型菜单(cursor-g 现行名单)、安装器、keepmine/noagenthost 等补丁与诊断。同事 Cursor 菜单缺模型、冒出不该有的模型、BYOK 报不支持时使用。"
---

# Cursor 客户端 bundle 补丁 SOP(3.16.x~3.24.x;安装器跨平台,诊断/日志路径示例为 macOS)

> **现行菜单 = 39 名**(2026-10-13 加 muse-spark / muse-image;37 名那轮 commit `297b176`)。cr-g 整段下架、claude 家族进段 ③;
> std 改过一轮(32→35)后按用户「**只加非出图的**」补了 7 个文本名(claude-opus-4-8/4-7/4-6、
> claude-sonnet-4-6、gemini-3.8-flash、grok-4.3、grok-build-0.1,实打全过)。没进:出图名一概不进;
> `deepseek-v4.1-flash` 出字 4/6(失败率 33%)。菜单有而 std 没有的 7 个(`gpt-5.2` `gpt-5.4`
> `composer-2.5` `deepseek-v4-flash` `grok-4.20-0309-reasoning` `grok-4.5-latest` `grok-4.6-latest`)
> 同事 key 正常、std 点了 403 —— std 是实验 key,**「对齐 std」≠「同事能用」**,改菜单前先逐名实打。
> **给同事的入口 = 单页 https://chat.auto-link.com.cn/oneclick/**(skill `agent-oneclick-page`):重建了本工具的安装器后,必须再跑 `bash scripts/agent-oneclick-docs/publish_198.sh`,否则页面里内嵌的还是旧文件。
> 飞书文档(10-15 起只剩 `OQCPdLd4…` 一篇),正文由 `scripts/agent-oneclick-docs/`(bodies.py + apply.py --all)生成;**改完文档/附件一律跑 `python3 scripts/agent-oneclick-docs-audit.py`**
> (附件回环 sha + 块数 + 陈旧词 0,三个 agent 的文档一起验)。
> ⛔ `grok-4.20-multi-agent` 别进菜单:官方不支持 function calling,带 tools 必兜到 deepseek 作答;std 已摘。

对象:同事/本机 **Cursor.app 本体**的 workbench bundle 补丁与 BYOK 配置,与网关线
(zk-cursor-web-fc-iterate,改 198 CM)完全分层:**这边改的是用户 Mac 上的 Cursor,
那边改的是服务端 lane**。三个脚本全在 `scripts/zk-cursor-web/`:

| 脚本 | 用途 | marker |
|---|---|---|
| `cursor_team_setup.js` + `.sh`/`.cmd` | **首选,也是唯一发出去的**:同事一键装 cursor-g,零依赖跨平台(Win/mac/Linux)。09-15 起**按 AST 定位**(见下节),并带 `--uninstall` 恢复原状(见下节) | 8 补丁全家(见下);`@cx-chain:v3` 默认关+自动摘除 |
| `cursor_team_setup.py` | 同上的 Python 版,**纯正则、故意不上 AST**——不在 zip 交付物里,留作第③腿的跨实现参照 | 8 补丁全家(**无 chain**,legacy 不分发) |
| `cursor_chain_patch.py` | 件B 链式增量独立源(exthost fetch seam),移植进 `.js` 安装器的字节基准 | `@cx-chain:v3` |
| `cursor_queue_pump_patch.py` | 单独打/升级排队泵(旧版 v1→v3.5 全支持原地升级) | `@cx-queue-pump:v4` |
| `cursor_ctxwin_patch.js` | 件C 上下文窗口单位归一**单机版**(`--apply`/`--revert`,不带参数=空跑);已逐字节移植进 `.js` 安装器的 `CTXWIN_*` 家族 | `@cx-ctxwin:v3` |
| `cursor_ctxwin_check.js` | 件C 的尺子:读盘上 4 条 bundle 报 marker/锚点数 | — |
| `cursor_ctx_param_gate.js` | `context` 档位参数门禁(⏸ 还没接进安装器/巡检) | — |
| `cursor_queue_diag_patch.py` | 诊断用:把泵换成"每秒打 tick 日志"版,只观测不改行为 | `@cx-queue-diag:v3.2` |
| `bundle_anchor_probe.js` | 只读数锚点命中(live Cursor 或**显式给 bundle 文件**,新版预检不用装) | — |
| `bundle_patch_regress.sh` | **改 PATCHES 后的四腿离线回归台架**(见「新版本漂移修复流程」) | — |
| `retire_model_offline_cases.sh` | **改模型清单后的九腿/28 断言离线台架**(整表赋值逐字按序 + 选中位 re-point + 写库前快照 + `--uninstall` 还原 + `--upgrade` 不问 Key;带 `RETIRE_JS=` 阳性对照) | — |
| `cursor_key_read.js` | **只读解开本机 BYOK Key**(默认掩码,`--reveal` 才明文);诊断用,**不进交付 zip** | — |
| `cursor_keysan_offline_cases.js` | 台架第 ⑫ 腿:**Key 不可见字符**(真 `new Headers()` 对账 + 旧行为阳性对照,51 项);见「Key 里的不可见字符」 | — |
| `setup_package_freshness.py` | **发出去的 zip 是不是最新的**:逐成员 sha256 vs 工作副本 + 与上一版逐成员 diff + 菜单条数;`--selftest` 自带阳性对照 | — |
| `bundle_audit_daily.sh` + `com.zerokey.cursorbundleaudit.plist` | **每日自动巡检**:官方出新版当天自动跑全四腿并告警(见「每日自动巡检」) | — |
| `regress_queue_pump.js` | 泵离线回归台架(注入真发货 snippet,17 判据+负对照) | — |

**补丁全家(3.20.21 现行 8 处 workbench)**:gate / localagent / dedicated(解锁三件套)+
`@cx-queue-pump:v4`(兜底泵)+ qserial / nosteer-mod / nopromote(排队纵深防御,`nopromote` 是 multi)+
**`@cxteam-norelay`(2026-08-25 连发折叠+僵尸真根因修)**。
2026-09-23 加到 **10 处**:`@cxteam-nocloud` / `@cxteam-nocloudset`(见下「会话被改道去 Cloud Agents」)。

## 🔴 菜单按 Key 动态拉取,不再写死(2026-10-19 起)

`DEFAULT_MODELS` 常量**已删**。下文所有「改 `DEFAULT_MODELS`」「抠 `DEFAULT_MODELS`」「菜单 39 个」都是历史。现状:

- INSTALL / UPGRADE:`resolveMenu` → `fetchKeyModels`(GET `{base}/models`,网关按 key 白名单过滤)→ `orderMenu`
  (去掉名字含 `image|imagine` 的;分段 ① grok/composer ② gpt ③ claude ④ 其他,段内 `grok-4.7` 排首、其余按名字)
  → `mergeConfig` 整表赋值。`pickDefault`:key 里有 `grok-4.7` 就选它,没有就选菜单第一个。
- **拉不到列表(没 Key / 断网 / 空)就不改菜单**,输出写明原因(台架 ⑬)。
- Key 从哪来:INSTALL 粘的(现在在写菜单**之前**问);UPGRADE / 刷模型读 `~/.carher/image.json`(600,画图共用),
  读不到再试解库里的密文(3.24.12 起 keytar 被摘,解不开)。都没有时,UPGRADE 在 TTY 下问一次。
- **「刷模型」= `--refresh-models`**:只改菜单,不碰 bundle、Key、小代理。Cursor 开着时**直接写库没用**(退出时会把内存里的旧菜单写回去),
  所以先返回(让 agent 说完话),再起一个脱离的 worker:等 8 秒 → osascript 正常退出(有弹窗就等,不强杀,
  最多 3 分钟)→ 写库 → `open` 重开。日志 `~/.carher/cursor-refresh.log`。skill 装在
  `~/.cursor/skills/carher-refresh-models/`,命令指向 `~/.carher/cursor-setup/cursor_team_setup.js`(安装时复制过去,
  解压目录被删也能用)。
- 菜单账本 `~/.carher/cursor-menu.json`:前缀表认不出的新名字(网关明天上的)靠它让 `isOurs`/`--uninstall` 认出。
- 台架:`retire_model_offline_cases.sh` 用 `CX_MODELS_JSON` 注入网关返回,不出网;⑬ 没 Key 不动、⑭ 刷模型(删/加/改回默认/幂等/没 Key 拒绝)。
  `setup_impl_parity.py` / `setup_package_freshness.py` 第③腿改成断言「无写死清单 + 有 fetchKeyModels/刷模型」。
- 10-19 本机实测:Cursor 开着时跑 `--refresh-models`,1 秒退出、写库、重开;重开 30 秒后读库,菜单仍是 40 个(含 kimi-k3 / gpt-reserve,
  deepseek-v4-flash 已删),没被写回。`cursor-agent -p "刷模型"` 能命中 skill。

## 给同事的包按系统拆开(2026-10-15)

用户点名「几篇文档的脚本同名、Mac/Windows 混在一起乱」。`package_team_setup.sh` 仍出完整包
`cursor-g-setup.zip`(门禁/台架都对它做),再从同一个 STAGE 切出 **`cursor-mac.zip`**(*-Mac.command + .sh + zk-delta)
与 **`cursor-windows.zip`**(*-Windows.cmd + .cmd,无 zk-delta);引擎 js 逐字节同完整包。
飞书两篇 Cursor 文档挂这两个包(不再挂完整包);freshness 第④腿查两包逐成员 == 完整包且没混另一系统按钮(阳性对照已验)。
Codex / Claude 同理改名 `codex-mac.command` / `codex-windows.bat`、`claude-mac.command` / `claude-windows.bat`。

## 菜单里冒出 Cursor 自带模型(claude-fable-5-1 等,2026-10-14)

显示规则(workbench 原文)`enabled.includes(n) || (!disabled.includes(n) && defaultOn)` ⇒ 服务端目录
`availableDefaultModels2` 里 defaultOn 的自家模型,我们没登记也会显示;同事点了走 Cursor 自家推理
(免费号报 Free plans can only use Auto / does not support custom API keys)。
修:mergeConfig 把「defaultOn 且不在本方案清单」的名字写进 `modelOverrideDisabled`(`default`=Auto 不动,
用户自己关的保留)。js/py 同步,retire 台架 ⑫ 带阳性对照。服务端会继续加新 defaultOn 模型 ⇒ UPGRADE 每次重算。
⚠ 安装器检测到 Cursor 在跑会拒绝,连 `CURSOR_USER_DIR` 指副本也拒 ⇒ 离线验证直接调 py 的 `merge_config` 打副本。

## 升级锁默认开 / 8788 被占的解法(2026-10-14)

- INSTALL/UPGRADE/REPAIR 默认写 `update.mode=none`、`update.enableWindowsBackgroundUpdates=false`、
  `cursor.agentHost.updateMode=none`(`--allow-update` 放开);自动升级会冲掉全部补丁。
- REMOTE-SSH 报「远端 8788 被占」的 b 方案改成「退出 Cursor → 双击 TURN-OFF-DELTA」,不再让用户手填网关地址。
- 3.24 删了 keytar ⇒ 新装机自动写 Key 失败时提示「还差一步」:Settings → Models → OpenAI API Key 粘贴 → Verify。

## Windows .cmd 三条硬规矩(2026-10-13,同事实机满屏「不是内部或外部命令」)

cmd 按字节偏移逐行续读 ⇒ `.cmd` 里**非 ASCII、只有 LF、双击启动器里 chcp** 都会让后续行从半截读。
中文与交互全放 JS(`cursor_remote_ssh.js --wizard`),`.cmd` 纯 ASCII+CRLF;打包脚本门禁拦这三样。
wizard 用单个 readline 排队读行(每问新开 interface 会丢缓冲里的后几行)。

## 服务端灰度 cursor_agent_host ⇒ BYOK 报 custom API keys(2026-10-13,@cxteam-noagenthost)

gate 开 ⇒ renderer 日志 `effective agent-host topology is enabled` ⇒ 消息走 Agent Host connect(服务端推理)⇒
任何模型都报「This model does not support custom API keys」,小代理零请求。件D noloop 拦不住(它在 agent host 内部)。
修:workbench 读 gate 那一处(desktop/glass 各 exactly-1)前插 `!1&&` ⇒ 拓扑恒 legacy。js 必带 AST locator。
验收判据=新会话日志 `using legacy implementation` + `[local_agent_retries] Request successful` + 小代理 req_total 涨。
**同日起升级锁默认开**(update.mode / enableWindowsBackgroundUpdates / agentHost.updateMode 三键,`--allow-update` 放开):
自动升级会把这些补丁全部冲掉。

## 会话被改道去 Cloud Agents(2026-09-23,@cxteam-nocloud / nocloudset)

同事升 3.21.18 后每次发消息弹「Upgrade to run Cloud Agents / Cloud Agents are not available
on your current plan」,BYOK 模型一个都用不上。

🔴 **判定在服务端 ≠ 我们修不了。要拆成两问:「谁拒的」和「谁把请求送过去的」。**
那句标题在客户端只出现在埋点翻译函数
(`case"Upgrade to run Cloud Agents":return"cloud_agent_pro_trial_denied"`),正文全 app 0 命中
⇒ 文案确实是服务端 `CUSTOM_MESSAGE`。**但改不改道在客户端**,全 bundle 唯一岔路口:

```js
shouldRunOnBeforeSubmitChat(){const e=this.composerDataService.getComposerData(this.getComposerHandle());
  return e!==void 0&&Oke(e)||!!e?.pendingBackgroundAgent}   // true ⇒ 云端 RPC
```

- `pendingBackgroundAgent` **持久化在会话数据里**,升级/重启都不清 ⇒ 中招一次每轮都弹,自己好不了。
- 置位来源两条:输入框右侧 Send-to-Cloud 手滑点中;服务端把 `push_local_agent_to_cloud` /
  `send_to_cloud_on_followup` / `midturn_move_to_cloud_ads` 这些 **`{client:!0,default:!1}`** 闸门打开。
- 修两处缺一不可:`nocloud-submit` 摘掉岔路口那条腿(老会话立刻回本地线)+
  `nocloud-set` 置位入口恒 `!1`(堵死两条来路)。
  **`Oke(e)` 那条腿保留** —— 那是「这本来就是个云端 agent 会话」(`createdFromBackgroundAgent`),
  用户主动在云端开的不许动。
- 名字位(**参数名 + 局部变量名**)desktop/glass 全不同(`e`/`t`、`Os`/`ji`、`Hs`/`vr`)
  ⇒ 一律 `ID` 捕获 + 反向引用,别写死。
- ⚠️ **有意加补丁 ⇒ 台架② 必红**(pristine 重打 N+2 vs live 已打 N)。正确做法是
  **证明差异恰好只有新插入那两处**:`live 再打这 2 个` 与 `pristine 打全部 N+2` 逐字节相同。
  ⛔ 别拿 `difflib.SequenceMatcher` diff 38MB bundle(300s 超时),用上面那个等价性做法。
  ⛔ 更别把② 的红当"已知问题"挥掉。
- 记忆:`feedback_cursor_cloud_agents_is_a_server_side_plan_gate`。commit `e45ffc4`。
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

## 件C 上下文窗口单位归一 @cx-ctxwin:v3(2026-09-22 上线,**默认开**)

**病**:服务端 protobuf `InferenceExtendedUsageInfo.max_tokens` 回的是**档位名的数字部分**
(500k→`500`、1m→`1`),小 1000 倍。Cursor 自己的阈值 `min(maxTokens-10000, maxTokens*0.9)`
于是变负数,`used >= 负数` 恒真 ⇒ 连发 `hi` 也每轮触发 summarization,界面 8146%~27706%。

- 🔴 **是按时间分的,不是按模型分的**:5/17~6/26 共 42 次全对 0 次错;9/17 是最后一个正确值,
  **9/20 起 19 次全错**。同一个 grok-4.6:9/14、9/17 拿到 256000,9/22 拿到 0。
  ⇒ 服务端 9/17~9/20 之间的回归,**任何模型都会中**,别按模型分诊。
- 🔴 **ctx 档位与 maxTokens 并非一一对应**(gpt-5.6-sol 选 272k 拿到 `500`;grok-4.5-latest
  无 ctx 参数却拿到 `272`)⇒ **不许按 ctx 档位反推窗口**,只能对收到的那个数字做单位归一。
- `maxTokens=0`(`sa-*`/`cr-g-*` 这些只在 198 上存在的名)= 窗口未知 ⇒ **从不压缩**,
  是**另一个病**,补丁**故意放行 0**,别一起治。
- ⚠️ **后端为什么发 k 数字这一项没有数据**(无抓包),不许当成因说出口。

**换算**沿用 Cursor 自己的解析器 `WS_()`(k→1e3 m→1e6):`1→1e6`;`0<n<4096→n×1e3`;
`0` 与 `≥4096` 原样不动。界 4096 的依据:最小档 200k 映射成 `200`,真实窗口最小 200000,
4096 落在两簇之间 ⇒ 自定义模型与真实窗口都零变化。

**补三个点,缺一不可 —— 是穷举出来的,不是挑的**:daemon.cjs 里对 `maxTokens` 做算术/比较的
代码行共 10 处 —— 4 处在 B 体内、5 处走 `tokenDetails.maxTokens`(A 覆盖)、剩下 1 处就是 C。

| 点 | 函数 | 管什么 | 漏了会怎样 |
|---|---|---|---|
| A | `createRedactedConversationTokenDetails()` | tokenDetails **唯一构造入口**:界面百分比、`overageThreshold` 阻塞、持久化 | 显示还是 8146% |
| B | `getBackgroundSummarizationTriggerThreshold(maxTokens, props)` | **所有「该不该压」的判定都过它**(shouldStart/shouldPersist 都调它) | 有一条路径直接用 `currentUsage.maxTokens` **不经过 tokenDetails**,照样每轮压(v2 的漏洞) |
| C | `shouldPersistBackgroundSummarization(used, maxTokens, props)` | persist 档位 | 🔴 它**自己又拿裸值**算了一次 `unusedTokens = maxTokens - usedTokens`;start 一触发 persist 必同时触发,「后台摘要」档位没了 |

- 🔴 **律令:在函数入口归一,那次归一只活在这个函数的局部变量里**。下游函数拿到的还是**裸值**,
  会重新算一遍 —— 「补了唯一构造入口」不等于覆盖了所有消费者(C 就是这么漏掉的)。
  判据只能是**穷举该字段的全部算术/比较点**,数出来,不是读着像就算完。
- 打的是 **4 条 bundle**:`cursor-local-agent-runtime`、`cursor-agent-host`、`cursor-agent-exec`
  三条 `dist/main.js`(minified)+ `cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs`
  (**未压缩,锚点用源码字面量**)。安装器 `CTXWIN_TARGETS`,家族前缀 `CTXWIN_*`,备份扁平名
  `cxctxwin__extensions__…`(与 chain 同源纪律:两条 bundle 都叫 `main.js`,按 basename 存会互相覆盖)。
- **三锚点任一不是 exactly-1 ⇒ 整体放弃,一字节不写**;文件里已有旧 marker(v1/v2)⇒ 拒绝并让先 `--revert`。
- 🔴 **同路径多家族必须 plan 合并,不能各写各的**:件A/件C 都改 `cursor-agent-exec/dist/main.js`,
  安装器主流程按 `plans.findIndex(x=>x.p===abs)` 找到同路径计划,**拿它的 `out` 当 baseText 继续变换**
  再合并 `applied`;各自 push 一条计划 ⇒ 后写的静默冲掉前一个家族,且两边都报成功。

**回归(`bundle_patch_regress.sh` 腿⑤)三条判据**:①三锚点 exactly-1 且每文件 marker×3;
②`node --check` PASS **并且塞一个故意写坏的样本进去确认检查器真会报红**(不然"全绿"不算数);
③**行为腿**:把补后的真实函数抠出来真跑 —— `1→1e6`、`256→256000`、`500→500000`,`0` 与 `≥4096` 不动,
90% 压、89% 不压、1000 token 不压,**4 条 bundle 结果逐行相同**。
另有 42 行压缩矩阵(1/50/89/90/95/120% × 各窗口)0 处不符。

**自证「不是我改坏的」只能靠 pristine 快照真跑**(这轮他直接问了):
原始快照 `~/.cursor-ctxwin-backup/20260922163516/`(marker=0 已验)上实测 `1/256/272/300/500`
阈值 = `-9999/-9744/-9728/-9700/-9500`,1000 token 就触发 ⇒ **原始 Cursor 就有这个病**。
同一轮健康对照:真实窗口 200000~1000000 全部 90% 压/89% 不压 ⇒ **尺子没坏**。
再加两条:21 条坏值时间戳最晚 09-22 15:58 < 本机首次打补丁 16:03;
`cursor_team_setup.js` 全文 grep 不到 `maxTokens`/`context`/`tokenLimit`/`summariz`。
**只有"我没改那里"这句话是不够的,要有原始态上的实测红。**

⚠️ **台架自己坏过**:第一版用松正则,B 抓到了不相干的函数、整列返 `false` —— 一整屏无意义输出。
锚点必须与补丁脚本**逐字一致**。minified bundle 里 B 的依赖函数是压缩名且**重名 8 次**,
按名字抠会抓错 ⇒ 只能按**函数体形状**匹配
(`function X(e,t){return"number"==typeof e&&Number.isSafeInteger(e)&&...}`)。
`shouldPersist` 那列一开始全"不压",是我的 props 漏了 *start* 档阈值(它委托给 shouldStart 用同一份
props ⇒ 阈值 `void 0` ⇒ 恒 false)—— **这个假绿正好也是 C 这个真漏洞的入口**,当时没当成"本来如此"放过。

⚠️ `cursor_team_setup.py`(Python 孪生)**没有** ctxwin 家族 ⇒ 两边补丁覆盖面已漂移;
它不进 zip,第③腿跨实现参照对不上 ctxwin 这部分。

- 脚本:`cursor_ctxwin_patch.js`(单机版,`--apply`/`--revert`,空跑只报告)、`cursor_ctxwin_check.js`(尺子)。
  **安装器产物与单机版逐字节相同 4/4**(`setup_impl_parity.py` 之外的额外对账)。
- 记忆:[[project_cursor_ctxwin_maxtokens_1000x_2026_09_22]]。

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

## 模型清单增减 + 老用户升级档(2026-09-20)

改菜单里有哪些模型名 = 改 `DEFAULT_MODELS`,**但删一个名字不等于同事机器上就没了**。

- **`mergeConfig` 的 dedup 原来是纯并集,没有删除路径**:从 `DEFAULT_MODELS` 里删掉一个名字
  只影响**新装**的机器;已装过的同事库里那个名字会**永远留着**,他在菜单里点一下就报错。
  ⇒ 下架名字必须进 **`RETIRED_MODELS`**(两份实现都要,`setup_impl_parity.py` 已把它列为
  第 4 个逐字节门禁常量)。`--models` 显式点名某个退役名时**以他为准**(不替用户做决定)。
- **光从 `userAddedModels` 摘掉不够**:退役名可能正被某个功能位**选中**。菜单里没了、
  `composer` 却还钉着它 ⇒ 他一发消息就报错,而且在菜单里找不到那个名字、不知道怎么换回来。
  ⇒ 按值扫 **`modelConfig` 的全部键**(不只 `composer`/`cmd-k`;本机实测 8 个键,
  同事可能在 `deep-search` / `plan-execution` 里也挑了它),命中就改回 `DEFAULT_MODEL`。
  这一条**连 `--keep-model` 也要做**:那个开关的语义是"别动我钉的量具",
  而钉在一个已经不存在的名字上不是量具,是坏的。
- **加新名字前必须用 Cursor 的真线型逐个探**(`scripts/litellm-198-cursor-newnames-probe.py`:
  克隆真 cursor key 的 `models`/`aliases` 形状 + `POST /v1/chat/completions` 带 `stream`+`tools`
  + 每发唯一 nonce + 假名阴性对照)。**"`/model/info` 读得到"不是能用**。
  09-20 实测:`sa-grok-imagine` 在 chat 线型上 400 `invalid_request_error`,只有
  `/v1/images/generations` 才 200 —— 出图专用名,Cursor 只会打 chat ⇒ 菜单里能选、一点就报错。
  用户点名要它在列 ⇒ 留在菜单 + README/文档标「出图专用」+ 证据写进代码注释。
- **`MODEL_PREFIXES` 必须覆盖每一个菜单名**,否则 `--uninstall` 的 `dropOurs` 摘不掉它
  却告诉他清干净了(09-20 加 `sa-composer-2.5-fast` / `qwen3-coder-next` 时差一步踩到)。
  这不是注释能保证的事 ⇒ `setup_impl_parity.py` 里已落成**能红的门禁腿**
  (菜单名 orphan / 退役名 orphan / 同一个名字既在菜单又在下架名单)。

### 现行菜单:30 名 / 四段(2026-10-12 换代;段序仍是产品要求)

段序不变、段 ③ 换人:**①grok/composer ②gpt ③claude ④其他**,`DEFAULT_MODEL` 仍 `grok-4.7`。

- **cr-g 整段下架**(13 个名),全部进 `RETIRED_MODELS` ⇒ 老机器 `UPGRADE` 自动摘 + 选中位 re-point。
  `MODEL_PREFIXES` 里的 `cr-g-` **留着不许删**:老用户库里还躺着这些名字,靠它才摘得掉。
- **claude 5 名**:`claude-opus-5-5` / `claude-sonnet-5-5` / `claude-haiku-4-5` /
  `claude-opus-5` / `claude-sonnet-5`,落点是 per-key alias → `kiro-claude-*`。
  🔴 **必须全名**。文档原来写的 `opus-5.5` / `sonnet-5.5` / `haiku-4.5` 这种短名,
  在 198 整张 token 表里 **allowlist 和 aliases 两边都是 0 把 key 有**(psql 核过)⇒
  照文档去点必 403/400。**短名不是"简称",是不存在的名字。**
- **对齐 `cursor-liuguoxian-std`**(用户点名):std 的 `models` 有 **32** 个,菜单取 **30** ——
  差的两个是**实打判红**的出图名:`gpt-image-2`(200 但**出字为空**)、
  `grok-imagine-image-2.0`(**500**),与 09-20 `sa-grok-imagine` 同形(chat 线型恒坏)。
  ⛔ **别照着 std 的 models 把它们抄回来。**
  ⚠️ std 的数组顺序是**入库先后**,不是菜单顺序 ⇒ 对齐的是**集合**,不是排列。
- 🔴 **新加前缀宁窄勿宽**:claude 那段我第一版写了一条 `claude-`,**当场被台架 ⑨ 腿判红** ——
  fixture 里同事自己加的第三方名正是 `claude-opus-4.6`,宽前缀让 `isOurs` 认领它,
  `--uninstall` 于是**把他的名字删掉而不是还给他**(dropOurs 在还原之后过滤)。
  改成三条窄前缀 `claude-opus-5` / `claude-sonnet-5` / `claude-haiku-4-5`
  (恰好覆盖 5 个菜单名,且不碰 `claude-opus-4.6` / `claude-4.6-sonnet` 这类别人的名字)。
  `gpt-`/`grok-` 那种宽前缀是历史遗留、后果已登记 —— **新加的别再扩大这个面**。
- ⚠️ **裸名(没有分隔符)要单独一条前缀**:`jev` 就是,漏了 parity 门会红、`--uninstall` 摘不掉。
- ⚠️ **菜单常量的注释里不许出现双引号字符串**:`setup_impl_parity.py` 抠菜单是按引号切的,
  注释里的引号会被读成一个模型名(10-12 当场踩到,菜单被读成 20 个,报"只在 js: ['这是我们装的']")。
- **覆盖面不是 100%**:这 30 名在带 `grok-4.7` 的 530 把当代 key 上 allowlist 518~530、
  alias 506~522 ⇒ 少数缺 alias 的 key 会 400。**那是 key 的事不是名字的事**
  (判据见下面「同一个名字在两把 key 上读数相反」),补 alias 是另一轮生产变更。

### 上一代菜单:30 名 / 四段(2026-09-21,已被上面那版取代)

用户点名的**段序**是产品要求(数组顺序 == 菜单顺序,别按字母重排):
**① `sa-*` → ② `gpt-*` → ③ `cr-*` → ④ 其他**。`DEFAULT_MODEL` 仍是 `sa-grok-4.6-latest`
(段 ① 首位)。`RETIRED_MODELS` 仍是 `cr-g-5.6-pro` / `sa-grok-imagine`。

`MODEL_PREFIXES` 随之从 4 条扩到 **9 条**:
`cr-g-` / `sa-grok-` / `sa-composer-` / `qwen3-coder-` / `gpt-` / `codex-` / `deepseek-` / `kimi-` / `glm-`。
⚠️ **`gpt-` 这条会同时命中 Cursor 自带的 `gpt-*`**(`gpt-5.3-codex`、`gpt-5.4`…)。后果只落在
`--uninstall` 的 `dropOurs`:卸载时会把同事自己加的 `gpt-*` 自定义名一起摘掉。EXACT 语义下装机
本来就会清掉那些名字,方向一致、不新增损失 —— 但**别把 `isOurs` 当"这是我们装的"的证据**用在别处,
它现在是个偏宽的判据。

**这一版起清单里故意含 5 个与 Cursor 自带模型同名的名字**
(`gpt-5.5` / `gpt-5.6-luna` / `gpt-5.6-sol` / `gpt-5.6-terra` / `kimi-k2.7-code`),用户点名照原名装。
同名的实测后果见下节「撞名」。🔴 别"顺手修正"成带前缀的等价名 —— 那会改变菜单里显示的名字。

**09-21 实打**(两把真 key 各跑一轮全 30 名,Cursor 线型 chat+tools+唯一 nonce;
工具 = `scripts/litellm-198-cursor-newnames-probe.py --key <真key> --names <清单>`,
09-21 给它加了 `--key`/`--names` 两个口子,默认行为一字不变、18 条门禁仍全过):

| key | 形状 | 读数 |
|---|---|---|
| `cursor-liuguoxian-std` | 68 models / **aliases n=0** | **27/30 出字**；400 = `kimi-k2.7-code`·`glm-5.3-flash`·`qwen3-coder-next` |
| `cursor-liuguoxian04-5rub` | 172 models / aliases n=27 | 那 3 个里 `qwen3-coder-next` **200 出字** |

🔴 **同一个名字在两把 key 上读数相反 ⇒ 400 是 key 属性,不是名字属性。**
真因:这些名字在网关里**只是 per-key alias 入口、没有同名真实组**。有 alias 的 key
改写到真实组 → 200;没 alias 的 key 虽然 allowlist 放行(`/v1/models` 里看得见)
但路由找不到落点 → **400**。三个 alias 目标直打全部 200 出字
(`claude-kimi-k2.7-code` / `zai-coding-glm-5.3-flash` / `kiro-qwen3-coder-next`)
⇒ **上游是活的**,坏的只是那把 key 缺 alias。

⚠️ **两道闸独立、症状不同,别混**:
**allowlist(`models`)** 决定 403 `key_model_access_denied`;
**per-key `aliases`** 决定 400(名字没有落点)。
⛔ 「`/v1/models` 里有这个名」既不证明能用,也不是 400 的免责
(记忆 `feedback_allowlist_name_without_alias_is_400` 讲的就是这一条)。

⚠️ **别再把 `cursor-liuguoxian-std` 当授权模板** —— 09-21 早些时候我这么写过,**当天证伪**:
它 `models` 更宽(68)但 **`aliases` 是空的**,反而比 04-5rub 少一层。要配一把能用全 30 名的 key,
模板是 **04-5rub 那 27 条 `aliases`**,至少含 `kimi-k2.7-code→claude-kimi-k2.7-code` /
`glm-5.3-flash→openrouter-glm-5.3-flash` / `qwen3-coder-next→kiro-qwen3-coder-next` 三条。
补 alias 是**生产变更,单独一轮、要签字**。

### 档位(10-16 keepmine-fill v2)与画图
- wangzhongwei:名字在、`variants` 空 ⇒ opus 不能换 High/Medium/Low。v2 带 `cursor_tier_catalog.json`(本机 21 个有档位名字的快照)补档位;目录自带档位的不动。v1 机器 UPGRADE 原地升级。台架 `keepfill_offline_cases.js <已打 bundle>`。模板过期(加了新的带档位模型)⇒ 从一台正常机器重新导出这个 json。
- 画图:INSTALL/UPGRADE 第 6 步装 `~/.cursor/skills/carher-image` + `~/.carher/image.json`(Key 只在 INSTALL 粘的那次写入);工具用 Cursor 二进制 `ELECTRON_RUN_AS_NODE=1` 跑。台架 `image_skill_offline_cases.js`。

### 🔴 服务端目录**缺**我们的名字 ⇒ 菜单里没有(2026-10-16 根因,补丁 `keepmine-fill`)

现场:liangjingyu(Win,3.24.12,补丁齐全,key 有全部 opus)菜单里没有任何 claude/gpt,网关 7 天 0 次 claude 请求。
doctor:「目录 = 24 条,装了但目录里没有的名字」20 个 = 全部 gpt 系、claude 系、gemini-3.8-flash。
同办公网本机同版本 64 条全在,直接调 `AvailableModels`(带 `additional_model_names`)本机也是 39/39。

- ⛔ **不是地区/IP**:同网同出口的本机全在(我先归到"中国区"被用户当场否了)。是**服务端按账号**给的目录少了条目,原因不明。
- 菜单只画目录(`computeAvailableDefaultModels` ← `availableDefaultModels2`);`keepmine-claim` 只能把**已有**条目认领成自定义,补不出缺的;重跑 UPGRADE/REPAIR 无效。
- `keepmine-fill`:锚点 `{this.persistAvailableDefaultModels(X),this._reactiveStorageService.setApplicationUserPersistentStorage("modelPickerDisplayConfiguration"` 前插一段,把 userAddedModels 里目录缺的名字按**自定义条目形状**(取自本机活着的 21 字段条目)合成塞回;已有条目不碰。两条 bundle 各命中 1。
- 验法:①打完 js==py ②从产物里切出插入段在假 this 上跑(24 条输入 → 44 条)③本机真装后**手动从 state.vscdb 删掉那 20 条再启动**,看 Cursor 刷完是否 39/39(删前备份 state.vscdb)。
- 判据:doctor 第 1) 节 `keepfill2=1`(v2,带档位)且第 2) 节「装了但目录里没有」仍非空 ⇒ 补丁没生效,要再查;只有 `keepfill=1`(v1)或都没有 ⇒ 让他用新包 UPGRADE。

### 撞名:自定义名 == Cursor 自带名(2026-09-21 根因)

**一个与 Cursor 自带模型同名的自定义名,不会被登记成自定义模型** —— 它**不进
`aiSettings.userAddedModels`**,只在 **`aiSettings.modelOverrideEnabled`** 里留一个
「走我的 key」开关。请求于是以那个**自带名**沿 BYOK 管道发出去。

- **`modelOverrideEnabled` 才是路径开关**:勾上 → 打 BYOK baseUrl(我们的网关);
  取消 → 回 Cursor 自己的服务器 / 他的订阅额度。
- **判"这条是自带的还是自定义的"唯一有效判据 = `availableDefaultModels2` 里的条目形状**:
  自带 = 27~28 个字段,带 `tooltipData`/`contextTokenLimit`/`clientDisplayName`,常有
  `variants`/`legacySlugs`,**没有 `isUserAdded`**;自定义 = 21 个字段、**有 `isUserAdded`**、
  `variants` 为空。本机实测 59 条里 38 条自带。
  🔴 **grep bundle 证明不了任何事** —— 自带清单是服务端推下来的,不在 bundle 里。
- 09-21 本机 403 的完整链条:composer 钉着 `gpt-5.6-sol`(撞名)→ 没进 `userAddedModels`、
  只有 override 开关 → 请求沿 `127.0.0.1:8788` → `cc.auto-link.com.cn/pro` 出去 →
  那把 key 的 allowlist 里没这个名 → 403 `key_model_access_denied`/`param: model` →
  `cursor-agent-exec` 归类 `LocalProviderError`→`NonRetriableError`,第一个 token 之前就中止
  (`pre_network_ms` 127~211ms)。旁证:同一进程生命周期内 22:43 `code=200`(`sa-grok-4.6-latest`)
  → 23:10/23:11 `code=403`(`gpt-5.6-sol`)。
- ⚠️ **`key_model_access_denied` 是授权信号,不是"网关里没这个模型"**。litellm 全库 338 个模型里
  `gpt-5.6-sol` 和 `chatgpt-gpt-5.6-sol` 都在 —— 09-21 我据 403 断言"我们没这个名字",被用户纠正。
  判"有没有"查全库,判"能不能用"查那把 key 的 `models` + `aliases`。
- ⚠️ **读 blob 里这两个数组必须走 `aiSettings.` 这一层**,顶层没有同名字段。09-21 我在顶层读到
  `n=0`,差点报成"清单是空的"。

**`--upgrade` = 老用户升级档**(`UPGRADE-Mac.command` / `UPGRADE-Windows.cmd`):
等价 `--apply` 但**一个字都不问 Key**(隐含 `--apply` —— 双击式升级不该先给他一屏 dry-run
再要他找终端敲参数)。它仍然**读一次库里有没有那条 secret**:"不问 Key"和"他其实从没配过 Key"
是两件事,后者升级完打开 Cursor 会 401 而屏幕上只写了"完成" ⇒ 没有就当场报出来并给下一步。
三个按钮的分工:`INSTALL`=第一次装(问 Key)/ `UPGRADE`=已装过要更新(不问 Key,同步菜单+摘下架名+
重打补丁+更新小代理)/ `REPAIR`=升级后补丁失效,**只重打 bundle**(不改菜单、不更新小代理)。

**台架**:`sh retire_model_offline_cases.sh`(约 60s,只碰临时目录)。九个 case、28 条断言:
退役名从 `userAddedModels`/`modelOverrideEnabled` 摘净 · `composer` 与 `deep-search` 的选中位
改回 `DEFAULT_MODEL` 且兄弟字段(`maxMode`)不丢 · **⑤ 同事自己加的第三方名必须被清掉**
(09-20 第二轮起,期望与上一版相反,见下节)· 新名真进库 · `--upgrade` 不问 Key/不覆盖 Key/幂等 ·
⑦ 两个数组逐字且按序 == `DEFAULT_MODELS` · ⑧ 写库前落了 `-preconfig` 快照 ·
⑨ `--uninstall` 把他装前的名字还回去 · 没 Key 的机器明确报出来。
- **阳性对照**:`RETIRE_JS=<改坏的副本> sh retire_model_offline_cases.sh` —— 把 dedup 的
  `isRetired` 过滤和 re-point 整段摘掉,必须**恰好**那几条腿红、其余全绿。
- ⚠️ **假 app 的 bundle 必须是真 bundle 的副本**。空壳文件 → 锚点命中=0 →
  `applyPatchesToText` 直接 `process.exit(2)`,**根本走不到 `mergeConfig`** ⇒ 12 项全红,
  红的理由还跟被测行为无关(第一版就是这个形状,日志里只有"AST 定位命中=0")。
- ⚠️ **每一次调 Cursor 可执行文件都必须带 `ELECTRON_RUN_AS_NODE=1`**。漏一次不是跑 node,
  而是**弹一个 Cursor 窗口然后永远挂住**(120s 超时、零输出,看起来像脚本死循环)。

### 交付包新鲜度:`setup_package_freshness.py`(2026-09-21)

同事问「脚本是最新的吗?怎么和我之前的大小不一样」时,**mtime 更新不是证据,zip 变大也不是证据**。
唯一能下结论的尺子是 `python3 setup_package_freshness.py`,三条腿:

| 腿 | 判据 | 红了说明 |
|---|---|---|
| ① zip vs 工作副本 | **逐成员 sha256** 相等 | zip 是旧的,重跑 `package_team_setup.sh` |
| ② vs 上次发出去那份 | 逐成员 size/sha diff + 菜单增删名单(**不返红**,是账) | 用来回答"为什么大小变了" |
| ③ zip 内菜单条数 | `--expect`(现 32)、无重名 | 发错版本,或 `--expect` 该更新 |

🔴 **第②腿要手动给 `--against`,不给就整条腿不跑**(而汇总里**不会**写"这腿没跑")。
换附件那一轮一定要跑带 `--against` 的那一发,材料就是刚 `+media-download` 下来的线上那份
—— 它同时是**回滚素材**。只跑裸的一发会 `VERDICT: PASS`,但"为什么大小变了"没有账。

- ⚠️ **zip 成员全带 `cursor-g-setup/` 前缀。**忘了剥它 ⇒ 每个成员都读成"工作副本里没这个文件",
  **一条腿都不红** = 假绿(09-21 第一次比就是这个形状)。
- 13 个双击器 / `README.txt` / `zk-delta/*` 由打包脚本**现生成**,仓库目录里本来没有 ⇒
  脚本里是**显式白名单** `GENERATED_ONLY`,不是"找不到就跳过";少一个双击器要能红。
- `--selftest` 是阳性对照:合成一份改了一个字节的 zip,第①腿必须变红;同时验模型名抠法的界。
  **跑结论前先跑它。**
- 🔴 **这道门禁自己会过期,过期的形状是"每次都红那么几条"**(09-29 修掉两处)。当时三条红:
  `DOCTOR-Mac.command` / `DOCTOR-Windows.cmd` 09-23 就随包发了却漏进 `GENERATED_ONLY`,
  `--expect` 还钉在 30 而菜单早已 32。**常红的门禁等于没门禁** —— 那几行红会被当成老毛病跳过,
  真出问题那天照样跳过。判"是尺子旧了还是真发错版本"有现成的岔:
  看第②腿的 **新增/删除两栏**,两栏都空 = 菜单本身没动,只是 `--expect` 没跟上换代。
  ⚠️ 改完 `GENERATED_ONLY` / `--expect` **必须再跑一次 `--selftest`**:这两处都是
  "让门禁少红"的修改,很容易顺手改成恒绿,阳性对照是唯一能分开"修好了"和"关掉了"的东西。
- 拉"上次发出去那份"只能用 `lark-cli docs +media-download --token <file_token> --output x.zip`;
  ⛔ `lark-cli drive +download` 对文档附件**静默不落盘**。
- 🔴 **换飞书附件的命令形状**(09-23 踩过两次)。`docs +fetch` 用 `--doc`(不是 `--token`,
  `--token` 属于 `+media-download`),且**默认 `--detail simple` 不带 block id** ⇒
  想拿 block id 必须 `--detail with-ids`;`--format` 只有 json/pretty/table/ndjson/csv,**没有 html**。
  块操作**没有** `docs +block-move-after` 这个子命令,全在
  `docs +update --command block_move_after|block_delete|block_insert_after|block_replace`。
  顺序照旧 **add → 回环下载 `cmp` 验字节 → move_after → 删旧**,别先删。
  验收三条:字符数(用同一个 `--detail` 比,否则数不可比)、`<img` 数、**旧 token 残留 0**。
- 🔴 **`block_move_after` 报 `degrade_code=1011 produced no document changes` 不一定是失败**
  (09-29 实测)。`+media-insert` 只能插到**文档末尾**,而这篇的旧附件本来就是最后一块 ⇒
  新块落进来时已经紧贴旧块之后,move 是空操作,于是"成功的位置"和"命令没生效"**同一个返回形状**。
  ⛔ 别在这里重试、也别改成 `block_insert_after` 硬塞。判据是**重 fetch 看兄弟顺序**:
  两个 `<figure>` 是否已相邻且新的在后。顺序对就直接走 `block_delete` 删旧。
  ⚠️ 同理别把 1011 读成"我没权限"——`ok:false` 但 `result` 那栏要自己看。
- ⚠️ **`--dry-run` 不校验文件**(09-29 实测):传一个**根本不存在的路径**它照样
  `ok:true` 并把四步 API 打出来 ⇒ 它只预览请求形状,**不能**用来预检"这个文件能不能传"。
  想预检只有真传一次再回环下载 `cmp`。
- ⚠️ 记忆里那条「`--file` 只认 cwd / `/tmp` / `~/files`,仓库路径会被白名单拒」**在 1.0.93
  上已不成立**:09-29 直接传 `/Users/…/scripts/zk-cursor-web/cursor-g-setup.zip` 通过。
  先试真路径,被拒了再 `cp /tmp`,别无条件先搬一次(搬完就多一份明文交付物要清)。
- **换完的验收账要连"没掉东西"一起报**:`<figure>` 数必须**仍是 1**(不是 2,否则旧块没删净),
  `<img`/`<whiteboard>` 数不变 —— 整篇 overwrite 曾把画板搞成孤儿,块操作也要按同一张表验。

🔴 **抠 `DEFAULT_MODELS` 必须有界。**从 `js.index('DEFAULT_MODELS')` 松散切到第一个 `];`
会读出 **36** 条,混着 `'use strict'` / `'path'` / `'crypto'` / `'darwin'` 和 `path.join(...)` 的碎片
⇒ 据此得出的"缺失名单"全是垃圾(09-21 实际发生过,拿它比对飞书文档漏名)。
正确写法 `re.search(r'const DEFAULT_MODELS\s*=\s*\[(.*?)\n\];', js, re.S)` + 去 `//` 注释 +
`assert len(names)==30`,尺子坏了当场停。

**09-21 那次的账**(留作口径示例):61876 → 63469 B,20 个成员里**只有 2 个变**
—— `cursor_team_setup.js` 119510→123035(+3525)、`README.txt` 5555→5732(+177),其余 14 个逐字节相同;
菜单 20→30(新增 10、删 0);`DEFAULT_MODEL`/`RETIRED_MODELS` 未变;把注释行剥掉后真代码只改 4 处。
⇒ 结论是"是最新的,而且装的逻辑没变",而不是"看 mtime 应该是新的"。

**同事拿到的包在哪:两篇同名《Cursor 一键配置》,2026-10-12 起必须同时更新。**

| 文档 | id |
|---|---|
| A | `OQCPdLd4MovEVoxGzdMcD3CJnCf` |
| B | `FapXdudPLoTTRLxWGJQcqITpn9g`(**10-15 用户已删**,只剩 A) |

🔴 **只更新一篇 = 把新旧调个位,而且不报错。** 10-12 就这么踩了一次:我按用户给的链接只换了 A,
B 于是从「现行入口」变成了陈旧那篇(还挂 32 名的包、还写「已适配 3.16~3.20.21」)。
**最阴的一层**:B 的正文写的是「这 **30** 个模型」——数字跟新菜单**正好相同**,名字却是旧的 30
(含 cr-g、含 `sa-` 名)⇒ **按数字对账会得到"一致"的假绿**,必须逐名比。
⇒ 收口判据(两篇各跑一遍,不是只跑入口那篇):附件 token 新的 1 个 / 旧的 0 个、
`<figure>` 仍 1 个、`<img>`/`<whiteboard>` 数不变、**30 个名字逐名命中**、
`sa-` 残留 0、`cr-g-5.*` 真名只允许出现在「已下架」那句提示里、版本号写着 3.24.12
且 `3.20.21`/`3.21.18` 残留 0。
✅ 10-13 起两篇正文已改成**逐字相同**(章节 0~6 一致),上面这些判据已进 `python3 scripts/agent-oneclick-docs-audit.py`:
两篇正文 diff 必须为空(图片 alt 是 AI 生成的不算)、附件回环 sha、陈旧词 0。
⚠️(历史)B 的正文结构曾与 A **不同**(段 ③ 原来是 cr-g、默认名写的是 `sa-grok-4.6-latest`、
"出问题了"是第 5 节),**别把 A 的 block id 套到 B 上**,逐篇重 fetch 取 id。

## 清单语义:并集 → 整表赋值(EXACT,2026-09-20 第二轮)

`mergeConfig` 原来是**纯并集**(只加不减)。后果:同事库里越堆越多 —— 历史代名(`cursor-g-*`/
`cr-g-*-135`)、他自己手工加的第三方名、早年装机留下的全在(**本机实测 `userAddedModels` 52 条 /
`modelOverrideEnabled` 50 条**,升级后先清到 58 再整表换成当时那 20 个;09-21 起清单是 30 个),
菜单拉一屏找不到要用的那个,
而且"文档写了什么"和"他菜单里有什么"永久性地对不上。用户点名:**除文档那份之外全部去掉,
包括他自己配的**。⇒ 两个数组**直接等于** `DEFAULT_MODELS`:

- **数组顺序 = 菜单顺序**,是产品行为不是巧合。改 `DEFAULT_MODELS` 的排列就是改同事看到的菜单。
- **`RETIRED_MODELS` 降级为"报告用"**:EXACT 之下判据是"在不在新清单里",不在就摘、就 re-point。
  它仍登记显式下架名(便于 README/文档/报告点出来),但**不再是唯一的删除依据**。
- **`--zk-delta-only` 必须豁免**:那一趟 `wantModels` 是空的,EXACT 会把人家清单**清空**。
  代码里是 `const exact = !args.zkDeltaOnly && wantModels.length > 0`,少一条腿就是清盘。

**备份必须挪到写库之前**(`backupBlobBeforeWrite()` → `<ver>-<ts>-preconfig/applicationUser.blob.json`,
写不进去就 `exit(4)` 不动手)。并集语义下备份晚一点无所谓(丢了也能重建);整表赋值之后
那份快照是**唯一**还知道他装前有哪些名字的东西。**门禁贴在不可逆那一步正前方**,不是 `main()` 末尾。

🔴 **改写入语义会推翻读这份数据的反向路径**:`--uninstall` 的隐含前提是"库里剩下的就是用户自己的名字"
(并集时代成立:我们只加过)。EXACT 之后**装机那一刻他的名字就没了**,卸载于是"还回 0 个"、
数字对、语义错、全绿。修法=从快照还原,且**取最早那份带非我们名字的快照**——
装过一次之后每次 `--upgrade` 都再落一份 `-preconfig`,里面躺着的已经是**我们那份清单**,
`sort().reverse()[0]` 拿到的就是那份(第一版就是这么写的,腿 ⑨ 报红才发现)。
⚠️ "还回 N 个"里 **N=0 有两种成因同形**(真的没有 / 挑错了快照),所以 ⑨ 的判据是
**点名他那两个名字回来了 + 我们的名字没了**,不是数个数。
记忆:`feedback_changing_merge_to_exact_invalidates_the_uninstall_premise`。

⚠️ **顺序腿的阳性对照不能靠改 `DEFAULT_MODELS` 的顺序**:期望值也是从同一个常量抠的
(`CX_DUMP_CONSTANTS`),改常量两边一起动 = 对照恒绿、**构造上不可能红**。要破坏的是**被测物**
(让安装器写库前 `.sort()` 一下),不是期望。同理 ⑦ 开跑前先断言抠到的数组非空 ——
空数组会让"两个空的相等"恒绿。

## 读同事/本机的 BYOK Key:`cursor_key_read.js`(只读,不进交付包)

`secret://cursorAuth/openAIKey` 存的是 **OSCrypt 密文**(`v10` + AES-128-CBC,iv=16 个 `0x20`,
key=`PBKDF2(钥匙串主密码,"saltysalt",mac 1003/linux 1,16,sha1)`),`sqlite3` 直接 select 出来
只有 `{"type":"Buffer","data":[...]}`,看不出任何东西。

```
ELECTRON_RUN_AS_NODE=1 /Applications/Cursor.app/Contents/MacOS/Cursor cursor_key_read.js            # 掩码
ELECTRON_RUN_AS_NODE=1 /Applications/Cursor.app/Contents/MacOS/Cursor cursor_key_read.js --reveal   # 明文
```

默认只打 `sk-L****…tEYg` + 长度 + sha256 前 16 位 —— 回答"两边是不是同一个 Key"用不着明文。
`CX_DB=` / `CX_RES=` 可指别处(台架/别人的备份)。**不进 `package_team_setup.sh` 的 stage**:
它是诊断工具,交付包里多一个能打印凭据的脚本没有收益。

- 🔴 **`/usr/bin/security find-generic-password` 在 agent shell 里会永远挂住**,不是报错、不是拒绝:
  `Cursor Safe Storage`/`Cursor` 那条 item 的 ACL 只认 Cursor 签名的二进制,别的进程来读就**弹授权框**,
  无人值守时没人点 ⇒ 零输出直到被超时杀掉。**这个形状和"钥匙串里没这条 item"、"脚本死循环"一模一样**,
  我这一轮就先按这条路撞了一次。判据:`security` 几秒内不吐东西就是在等框 —— 不要加 timeout 重试,换 keytar 那条路。
- **四种失败处置完全不同,所以四个不同退出码**:`2`=找不到 app/库(路径问题) · `3`=拿不到主密码
  (**Cursor 从没启动过**,或没用 Cursor 二进制跑 → keytar 的 `.node` ABI 对不上) · `5`=**库里没那一行**
  (他从没配过 Key;升级完会 401,而屏幕上只写"完成") · `6`=有行但解不开(**换过机器/迁移过 Library**
  → 密文方案与本机不符,**别覆盖写新 Key**)。把 `5` 和 `6` 混成一句"读不到"就会做出相反的处置。
- **阳性对照**(每次改这个脚本都重跑一遍,造四个假库):无该行→`5` · 值不是 Buffer 形状→`6` ·
  有 Buffer 无 `v10` 前缀→`6`(不同理由)· `v10`+随机字节→`6` BAD_DECRYPT · `CX_DB` 指不存在→`2` ·
  `CX_RES` 指空目录→`3`。六条各红各自那条、真库绿,才算这把尺子有判别力。
- ⚠️ **Key 是凭据**:明文别进 issue / 聊天记录 / 工单 / 记忆文件。要换就跑 `INSTALL` 重填,
  安装器写前有"能解开现有 Key + 加密回环"两道自检。

## Key 里的不可见字符 ⇒ `ByteString` 报错(2026-09-29)

同事报:

```
Local agent request failed
Cannot convert argument to a ByteString because the character at index 32
has a value of 8203 which is greater than 255.
Model: grok-4.6   Base URL: http://127.0.0.1:8788/v1
```

**8203 = U+200B 零宽空格,那是 WHATWG `Headers` 在拼 header 时抛的 ⇒ 请求一个字节都没出网。**
所以小代理 / 198 / 模型 / 车道全都无关,`kubectl logs` 和小代理日志里**都不会有这一发** ——
别拿"日志里查不到"当成腿死或尺子坏。判据是报错文案里有 `ByteString`。

定位算术(可复现):飞书 key 表 1374 行 API Key **长度全是 25**,`"Bearer "`(7)+ 25 = 32
⇒ `index 32` 正好是**紧贴 key 末尾**的那一格。想验哪一位就反过来减 7。

🔴 **`trim()` 去不掉零宽字符**:`"  ​sk-abc​  ".trim() === "​sk-abc​"`。
所以"看着没空格、长度也差不多"完全不能排除,肉眼和 `length` 都判不出来 —— 必须**看码位**。

🔴 **`--doctor` 原来量不到这个病**:它只打密文的 `len` + `sha256` 前 16 位,带 ZWSP 的 key
与干净 key **同形**,一路假绿。现在它解一次明文**只报码位、不打任何明文字节**:

```
ELECTRON_RUN_AS_NODE=1 /Applications/Cursor.app/Contents/MacOS/Cursor cursor_team_setup.js --doctor
```

```
    明文 len=26(码位数=26)
    🔴 不可见/越界字符 = 1 ⇒ **就是这个病**…
        字符下标 25  U+200B  ZWSP 零宽空格   ← 报错里的 index 应是 32("Bearer " 占 7)
```

解不开 / 读不到主密码时它报 **「量不到」**,不报"干净" —— 沉默不是绿。

安装器 `writeOpenAIKey` 落盘前调 `sanitizeKey()` 剥掉不可见/越界字符,并**逐个打出剥了什么**。
静默剥等于把"他复制错了"藏起来,下次在 Cursor 设置框里粘还会再中。

- **扫描器判据是 `Headers` 的超集,不是"同进同退"**:`Headers` 只在码位 > 255 时抛,
  **NBSP(U+00A0)≤ 255 ⇒ 它不抛**,脏 key 照样出网、照样错,回来是 **401**。
  这是**两个失败面,不许合并计数**:ByteString = 没出网;401 = 出网了但 key 错。
  所以扫描器抓的是"这把 key 脏了",比 `Headers` 更严。台架 ⑫E 就地量了这条。
- **台架第 ⑫ 腿**(`cursor_keysan_offline_cases.js`,51 项):纯函数 + **真 `new Headers()`**,
  不需要 Cursor / bundle / fixture ⇒ **永远不许 SKIP**。它从**真源码里切出**那三个函数进 vm 跑,
  不复制副本(副本会漂,漂了照样绿)。阳性对照 = **就地重实现旧行为(只 `trim()`)**,
  四个样本必须**仍然抛**;旧行为一旦"也绿了"就说明样本被换成了 `trim` 能处理的普通空白。
  D 段拿 `scanInvisible` 的字符下标 + 7 与 `Headers` 报的 index **逐位对账**。
- **doctor 那一格的阳性对照怎么造**(改动它必重跑):`cp` 一份 `state.vscdb` 到临时目录,
  用真 OSCrypt 方案写一把**故意带 ZWSP** 的假 key,`CURSOR_USER_DIR=<临时目录>` 跑 `--doctor`。
  判据 = 它报 `字符下标 25` 且换算出 **index 32**(与同事的报错逐字相同)。**别对着真库下毒。**
  两条操作细节:① 真库在本机有 **8.9GB**,别整库 `cp`(慢且占盘)—— 用 `sqlite3` 只把
  `ItemTable` 里 `cursorAuth` / `applicationUser` 那十来行抄进新库,真库全程 `mode=ro` 打开。
  ② 收工后 `ls -la` 比真库 mtime,确认仍是动手之前那个时间 —— "我没碰真库"要有据,不能靠记得。
- **还要配一个阴性对照**:同一份 doctor 对**解毒后**的同一个副本再跑一次,必须报
  `✅ 不可见字符 = 0`。只跑脏的那一发,分不清"它真在量"还是"它对谁都报红"。
- 🔴 **台架里的样本 key 只许放合成值,注释不算证据**。09-29 我第一版随手粘了一把
  "看起来像样本"的 25 位 key,注释写着"值是假的";提交前拿它去 `.tmp-lark/*.ndjson`
  grep,**命中 2 行 —— 是飞书表里的真 key**。现在常量是 `sk-FAKE…` 前缀 + 脚本里
  一行 `startsWith("sk-FAKE")` 断言。判据是**拿候选串去飞书快照对撞,命中必须 0**;
  长度仍须 25,否则 D 段那些下标算术前提不成立。**提交前对所有要进这一笔的文件
  (含 zip 内成员)扫一遍这个对撞**,别只扫肉眼可疑的那一行。
- 同事那一侧的处置:Cursor 设置里把 Key **全选删掉重填**,别从飞书表格/聊天/文档里复制
  (那三个地方最容易带零宽字符);或跑一次 `INSTALL` / `--key`,让安装器替他剥。不用重启 Cursor。
- 🔴 **`UPGRADE` 治不了这个病,而它恰好是我们平时让他点的那个按钮**。`--upgrade`
  **按设计不问 Key、不覆盖 Key**(只读一次库确认存在性,见「老用户升级档」)⇒ 新版安装器里
  那个 `sanitizeKey()` **根本不会被调到**。他双击 UPGRADE、看到"完成"、病还在 ——
  **"修复已发布"和"他装上了修复"是两件事**,中间卡的是他点了哪个按钮。
  ⇒ 凡是 Key 内容本身的病,给同事的话术只能是 **INSTALL(会重新问 Key)** 或
  `--key <k>`,或在 Cursor 设置里全选重填。发版说明里要把这句写在按钮表**正上方**,
  不能只写在"出问题了"那一节 —— 他照按钮表操作,不会先读排障节。

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
- **🔴 「名字位」不止函数名 —— 参数名和局部变量名同样会被 minify 摇**(2026-09-23,3.21.18):
  件C 三条 min bundle 锚点全灭。锚点 B 里我写死了 `const n=[]`,3.21.18 摇成 `const r=[]`;
  锚点 C 的第三参数与局部变量**互换**(`(e,t,n)/const r` → `(e,t,r)/const n`)⇒ 命中 0。
  与 3.20.21 的 `qUy`→`$7y` 是**同一个病低一层**:我以为"参数名 e/t/n 是 minify 惯例所以稳定",
  但 terser 分配名字取决于作用域里的变量集合,**上游改一行就能让整组名字换位**。
  正解:`function\s+(ID)\((ID),(ID)\)\{if\(\2<=0\)` —— 名字全捕获,**语义靠反向引用绑定**
  (第 1 参出现在 `<=0` 比较里才是 maxTokens),插入的归一语句用**捕获到的名字**生成。
  插入点也别拼 `function X(e,t){` 这种字面头:改成 `m.replace("{", ...)` 取参数表后第一个 `{`
  (参数表里不可能有 `{`),这样连空格差异也不依赖。
  ⚠️ 判据是 **3.21.18 与 3.20.17 两代都 4/4**:只让新版过,等于把老用户换成新的受害者。
- **升级策略**:默认允许 Cursor 自动升级,升级掀翻补丁后跑 `--repair` 一键重打
  (锚点=语义地标,3.16→3.19.19 全部存活);要锁死不升级才用 `--pin-update`。

### 🔴 离线台架全绿 ≠ 新版能用(2026-10-12 真机打脸)

3.24.12 那一轮我先离线跑完 **12 腿全绿 / 0 SKIP** 就报了"适配完成"。
用户让我升本机 Cursor 实测,**真机当场又挖出两处**离线台架看不见的东西:

| 真机才发现 | 为什么离线看不见 |
|---|---|
| **件D noloop 锚点全灭**(doctor 报 `0 🔴 没摘`) | 腿⑦ 的种子取自 `~/.cursor-ctxwin-backup/*` =**旧版**快照 ⇒ 新版形状从没进过门禁;而件D「只告警跳过、不阻断」⇒ 包照样发 |
| **上游删了 keytar 原生模块** ⇒ 自动写 Key 没了 | 台架从不碰钥匙串;`--doctor` 在 agent shell 里报的是"量不到",不是"坏了" |

⇒ **律令:`VERIFIED_VERSIONS` 加一个大版本之前,必须有一台真机装完并真发一条消息。**
判据(与 bot 升级同口径,见 `feedback_bot_upgrade_done_means_user_got_a_reply`):
① `--doctor` 每个补丁家族都非 0(**含不阻断的件C/件D**);② 小代理 `req_total` 从基线涨;
③ **SpendLogs 里按那把 key 查到该发、且有 `endTime`**(没 `endTime`=没回来)。
⛔ 「台架全绿」「网关 200」「屏幕上有字」都不够 —— ③ 才是落点证据。

**10-12 真机读数(留作口径样例)**:5 发全 `ok`,菜单名 `claude-opus-5-5`/`claude-sonnet-5-5`
→ 落点 `kiro-claude-*`,key=`cursor-liuguoxian-std`,1.4~66.2s;小代理 `req_total` 0→6。

**升级前必须先做的三件事**(做不到就别升):
- **跑一次腿②**(pristine 重打 == live 已打 BYTE-EQUAL)。升上去就**永久失去同版本参照物**,
  那腿以后只能 SKIP 或退化成弱判据。
- **把那一版的 pristine bundle 存成长期资产**(🔴 **只能在升级前做**):
  `~/.cursor-team-setup-backup/<ver>-<ts>/` 里的 `workbench.{desktop,glass}.main.js`
  + 四条 `cxctxwin__*`,一代约 **200MB**。它是「新正则对旧版零漂移」唯一的量具
  (②腿的替代判据)。10-12 本机按用户要求只留最新版,这批材料已经没了。
- **留回滚素材**:Cursor 官方只放最新版 ⇒ **升上去退不回旧版**。
  `mv /Applications/Cursor.app /Applications/Cursor.app.old<ver>` 保一份整 app
  (`~/.cursor-team-setup-backup/` 里那些只能还原 **bundle**,跨版本盖会把 Cursor 弄坏)。
  这份旧 app 还是**阳性对照**的来源:keytar 那条判据就是靠"新版 false / 旧版 true"才证明尺子不是恒 false。
  ⚠️ 删它之前先想清楚:删完"旧版里本来有什么"就再也问不出来了。

**升级后、删旧版材料之前,必须先给新版造一份 ctxwin pristine 种子**(10-12 实操):
腿⑤/⑦/⑧ 取 `~/.cursor-ctxwin-backup/*` 的 `tail -1`,而那些快照是**旧版**的。
把旧的删掉而不补新的 ⇒ 三腿直接 SKIP(台架白摆着)。造法 = 从升级时自动落的
`~/.cursor-team-setup-backup/<新版>-<ts>/` 里把 `cxctxwin__*` **剥掉前缀**拷过去:
```sh
SRC=~/.cursor-team-setup-backup/<新版>-<ts>; DST=~/.cursor-ctxwin-backup/<ts>-<新版>-pristine
mkdir -p "$DST"; for f in "$SRC"/cxctxwin__*; do cp "$f" "$DST/$(basename "$f" | sed 's/^cxctxwin__//')"; done
```
验收三条:两个 marker 都 0(真 pristine)、目录名排序在最后(`tail -1` 能挑中它)、
**新版标志在/旧版标志不在**(3.24.12 判据:`byok-connect-routed`>0 且
`promptContextUsageSnapshotBlobId`=0)。⛔ 别拿 live 当种子 —— 它装完就带 marker,
pristine 断言会正确判红,**那不是腿坏是喂错了**。
- ⚠️ 覆盖安装前 `kill` 掉 **zk-delta 小代理** —— 它跑在即将被替换的二进制上
  (`pgrep -f "Cursor.app/Contents/MacOS/Cursor"` 会看到它,**那不是 GUI 没退干净**:
  判 GUI 退没退要排除「以 `.js` 为参数」的 node 模式进程)。
- 升级后走 **`--apply`/UPGRADE 而不是 REPAIR**:REPAIR 按设计不碰配置和菜单,
  而换代那一轮菜单/开关都要写(10-12 那台 `useOpenAIKey` 本来是 `false`、菜单还是 24 名)。
- `--doctor` 在**刚装完还没启动 Cursor**时会报 `isUserAdded!==true 的 = 16 个 (keepmine 没兜住)` ——
  **那是启动前的正常态**,启动一次 Cursor 让它整理目录后变 `0 个(全部兜住)`。别当故障查。

### 3.24.12 适配(2026-10-12):离线两处真断 + 真机两处 + 三个门禁自己过期了

官方从 3.21 **直接跳到 3.24**(3.22/3.23 我们没见过机器 ⇒ `VERIFIED_VERSIONS` 只加 `"3.24"`,
不补没数过的版本:软闸漏报比假绿好)。解锁面 13 条非 multi 全 exactly-1、
`nopromote` 2/5 与 `nocloud-set` 2/2 逐字不变。断的是这两处:

- 🔴 **件C 锚 A = 0/4(四个目标全灭)**:上游把 `promptContextUsageSnapshotBlobId:void 0,`
  **这个字段删了**,A 的其余部分逐字不变。
  **与 3.20.21(`qUy`→`$7y`,函数名)/ 3.21.18(参数名与局部变量互换)不是同一个病** ——
  那两次是名字位被摇,ID 捕获能救;这次是**字段消失**,捕获再狠也没用。
  **分辨判据:连不 minify 的 `daemon.cjs` 都命中 0** ⇒ 不是 minify 漂移,是真改了结构。
  修法 = 那个字段整段写成可选组 `(promptContextUsageSnapshotBlobId:void 0,)?`,
  **重建产物时按捕获到的原文回填**(`a[0][4]||""`)—— ⛔ 不许无条件插回去,那等于
  给新版凭空加一个上游已经删掉的键。`src` 那条路同理:两代各一份字面量,
  **两代命中数相加必须恰好 1**(两代同时命中 = 文件里两个同名函数,和 0 命中一样不许动手)。
  同一处改动要**同步 `cursor_ctxwin_patch.js`**(单机版是字节基准)。
- 🔴 **`localagent` 正则命中 0,而 AST 15/15 全中**:那个对象字面量的**最后一个字段第三次换人**
  (`clientSupportsRoutedModelUpdate:!0}` → `localMode:X.localMode})` → `enableSmokeTestDriver===!0})`),
  落点 `if(<ID>.localMode){try{` 三代未变。⇒ 按第 3 步补**第三代前缀**(三代都认,仍要求 exactly-1)。
  ⚠️ **这条正则同时是 `bundle_anchor_probe.js` 的量具** —— 探针读的是 **js 文件的 `PATCHES`**,
  不是 py 的。所以「py 不进交付包、不用管」这个判断是错的:漏改 js 这条,
  **探针会在 AST 已经打上的版本上报 hits=0**,看着像没适配;而且第③腿(js==py byte-equal)必红。
  改完 js+py 两份,③ 在 3.24.12 上恢复 BYTE-EQUAL。
- **老版本零漂移已验**:3.21.18 / 3.20.17 的 workbench 上 `localagent` 仍 exactly-1;
  ②腿(pristine 重打 == live 已打)BYTE-EQUAL 两条全过(本机 live 还在 3.21.18,参照物在)。
- **两处都配了阳性对照**:故意把新前缀 / 锚 A 写坏 → 分别 hits=0、`锚A count=0`,证明尺子会红。

**顺带修掉三个"过期即静默少报"的门禁**(都是自己坏的,不是被测物坏):

| 门禁 | 坏形状 | 后果 |
|---|---|---|
| `package_team_setup.sh` 抠 `RETIRED_MODELS` | `awk '…{print;exit}'` 只读**第一行** | 常量从 1 行长到 6 行后,README「已下架」只印 2/15 个名字,**打包照样成功**(非空就过)。同事在菜单里找不到某名字、也不知道它被下架了 |
| 台架 ⑩ 的越界金丝雀 | 钉的是 `cr-g-5.6` | 那一轮它刚被下架 ⇒ 这条腿**恒红且理由完全指错**(报"--bare-sa 越界",真相是金丝雀自己退役了)。金丝雀必须挑**当前菜单里真有**的名字 |
| `setup_package_freshness.py --expect` | 钉在 32 | 换代后常红 ⇒ 被当老毛病跳过 |

⇒ 新规矩:**抠多行常量的地方要自带条数断言**(抠到的个数 == 常量里 `"…"` 的个数,
不等就中止打包)。⚠️ `printf '%s' "$X" | tr ',' '\n' | wc -l` 会**少 1**(末行无换行符,
而 `wc -l` 数换行符),写 `printf '%s\n'`。这个 off-by-one 就是那条新断言当场抓到的。

**真机那两处(10-12 下午补)**:

- 🔴 **件D noloop 两个目标锚点全灭,两层原因,第二层差点看错**:
  ① 上游把 `__awaiter`+生成器那套转译产物换成**原生 async 箭头**
  (`function h(e){return async t=>{` / `return async (runOptions) => {`);
  ② 放宽 async 那代**仍然 count=0** —— 真正卡住的是锚点要求 gate 名 `agent_host_local_loop`
  与 logger 名 `local-loop-turn-router` **相距 80 字以内**,而 3.24.12 在两者之间插了代码,
  **实测 826 字**。
  **诊断手法**:把正则**逐段加压**数命中(先只量两个字面量 → 再加 function 头 → 再加箭头),
  发现**连第一段就已经是 0**,才看出问题不在 async 那半。⛔ 别一看到 async 就以为修完了。
  **修法**:去掉"两个字面量必须挨得近"这个条件 —— 那是对**排版**的假设,不是对语义的,
  排版每版都会动;logger 名自己就足够钉死语义。
- 🔴 **上游删了 `keytar` 原生模块** ⇒ 自动写 Key 这条路没了,连带 ZWSP 剥除也没了
  (`sanitizeKey()` 只挂在自动写那条路上)。见记忆
  `feedback_cursor_324_removed_keytar_native_module`;提示语必须显式让他**别从飞书表格复制**。

**修这两处时,被测物之外的三样东西也写死了旧形状**(同一个病的不同层,都会制造**假红**):

| 坏在哪 | 形状 | 为什么危险 |
|---|---|---|
| 腿⑦ 位置断言 | 写死 `function\* *( *) *{ */\*marker\*/return{...` | 补丁打对了也报红,红字写"插进去也不会执行"⇒ **理由完全指错方向** |
| 行为腿 `awaiterName()` | 抠不到就 `throw` | 四条腿全报「**原版**跑崩」—— 红在阳性对照上,最容易被读成"补丁把路由改坏了" |
| 行为腿的桩 | 缺 `connectDecision()`、缺 `runOptions.runOptions.localLoopGateEnabled` 这层、logger 名写死 `logger34`(3.24.12 是 `logger109`) | 同上,全是"原版跑崩" |

⇒ **改锚点时,断言与桩必须同代一起放宽**。logger 这种带打包序号的名字**从函数文本里现抠**
(`fnText.match(/\blogger\d*\b/g)`),别一版一版去追。

🔴 **还有一例"上游把病修了",期望值必须分代**:行为腿 case ② 原来断言「BYOK 轮次原版落 `fail`」
—— 3.24.12 新增了 `if (input.hasModelCredentials) return connectDecision("byok-connect-routed")`,
原版自己就 connect 了。**老版本作对照验过**:3.21.18 的 daemon.cjs 里 `byok-connect-routed`
命中 **0** 而 `private-model-not-supported` 命中 2 ⇒ 真的是上游行为变更,不是桩没搭对。
处置 = 期望值按被测文本分代,并且**老实报 `NOTE … 本例对补丁零判别力,不计入通过`**。
⛔ 别为了让它绿就把期望直接改成 `connect` 然后当"补丁有效" —— 那是把阳性对照关掉。

**腿⑦ 的种子挑选顺序**(10-12 重写):① `--new-app` 且那份 app **是 pristine**(marker=0,
比如刚挂载的 dmg)→ ② `~/.cursor-team-setup-backup/<当前版本>-<ts>/` 的 pristine 快照
→ ③ 旧的 `~/.cursor-ctxwin-backup/*`(标注"可能量不到新版")。
⚠️ 拿 **live** app 当种子会被 pristine 断言正确判红(它装完就带 marker)——**那不是腿坏,是喂错了**。

⚠️ **`node --check` 不认 `.out` 扩展名**:会报 `ERR_UNKNOWN_FILE_EXTENSION` 而不是语法错,
我据此一度把"产物语法坏了"报出口,实际是**我的尺子坏了**。验产物先 `cp` 成 `.js`/`.cjs`,
并且**随手配一个坏样本**确认检查器真会红(我第一个坏样本 `function x(){ return ;;; }` 居然合法,
`;;;` 是三个空语句 ⇒ 对照恒绿;改成 `function x( { return 1 }` 才报红)。

### 3.27.12 / 3.27.14 适配(2026-10-26,程文旭 DOCTOR 全红报上来)

- 🔴 **同事的版本 ≠ 下载页版本**:`/download/stable/...` 回 3.27.12,同事是 **3.27.14**,来自
  更新通道 `/updates/api/update/darwin-arm64/cursor/<当前版本>/<commit>/stable`(或 `/download/latest/...`)。
  ⇒ 适配要对**同事 DOCTOR 报的那个版本号**抽 bundle,不是对下载页:用 `bundle_patch_regress.sh --fetch-latest`,
  或自己下好挂载后 `--new-bundles <dir> --new-app <挂载的 Cursor.app>`。
- 四处改动(js+py+`cursor_ctxwin_patch.js` 同步):
  ① localagent:`if(aGo.releaseAfterSend(u.conversationId),mc.localMode){try{` —— 条件成逗号表达式,AST 取最后一项,正则把前缀项并进第 1 组原样保留;
  ② 件C daemon.cjs 去掉了空白 ⇒ 字面量锚点全 0,改「名字逐字、只容忍空白」正则 `CW_*_SRC_RX`;
  ③ 件D daemon.cjs `async runOptions=>{` 单参无括号;
  ④ 我们自己的 bug:`let SOFT_MISS` 在 `CX_APPLY_TO_FILE` 钩子之后才声明 ⇒ 锚点一 miss 报 ReferenceError,盖住真错误(改 var)。
- 验:3.27.12 75 PASS、3.27.14 74 PASS(`--new-bundles <dir> --new-app <挂载的 .app>`),均 0 FAIL 0 SKIP;3.24.12 旧产物逐字节不变。
- 真机:10-26 本机 3.24.12 → 3.27.14(旧 app 留在 `/Applications/Cursor.app.old3.24.12`),**用网页上解出的包**双击同款
  `UPGRADE-Mac.command`:全部家族打上、菜单 40→37(+haiku-5-5);DOCTOR 启动前报 `claude-haiku-5-5 isUserAdded=undefined`
  (=上文「启动前的正常态」),启动一次后 `0 个(全部兜住)`。同事程文旭同一包装完反馈已解决。
  ⚠️ 本机 GUI 发消息那步没测成(小代理 req_total 一直 0,按键没落进聊天框、第 3 条粘进了终端),**别把它当真机发送证据**。
- dmg 下载:`bundle_patch_regress.sh` 已按 Content-Length 续传(10-26 一次 900s 超时只到 310/316MB,hdiutil 报「无法识别映像」)。
- 🔴 **每日巡检之前漏报的两个原因(10-26 修)**:① 只看 stable,同事自动升级拿的是 latest;② launchd PATH 没有 node,
  10-24 起每天 exit 1 但告警里 0 条失败项、状态文件停在 3.20.21。现在 `bundle_audit_daily.sh` 看 latest、补 PATH、
  没跑到腿时把日志尾巴贴进告警;`env -i PATH=/usr/bin:/bin sh bundle_audit_daily.sh` 实测 3.27.14 全绿。

### 新版本漂移修复流程(3.18.25 / 3.19.19 / 3.20.21 / 3.24.12 四次都按这个走)

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
   **①b 新版 app 的件C(ctxwin)四个目标全 OK**(`--new-app "<新版 Cursor.app>"`,`--fetch` 会自动带上):
   🔴 2026-09-23 补的腿。此前①只量 `workbench.*` 两条 bundle 的**解锁**锚点,件C 那四个目标在
   `extensions/` 下,**从来没有任何一腿看过它们**。3.21.18 的件C 锚点 B/C 命中 0,而安装器对件C
   的设计是「警告后跳过、不阻断解锁补丁」⇒ **包照样发出去、台架全绿、同事拿到的包里没有这个修复**。
   这就是「门禁在跑 ≠ 有量具」的标准形状:不阻断的补丁必须自带一腿,否则它静默消失没人知道。
   这腿的负对照要**打在锚点真正吃的那一处**上(同一个标识符全文可能出现 4 次,改错一处
   偏移只会平移 3 字节、锚点照样命中 ⇒ 假绿;按 dry-run 报的 offset 定位再改);
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
   🔴 **2026-10-12 起这条替代判据在本机已经没有材料了**:用户点名「只保留最新版」,
   `~/.cursor-team-setup-backup/` 只剩 `3.24.12-*`、`~/.cursor-ctxwin-backup/` 只剩
   `20261012-324-pristine`,两个旧 app(3.20.17 / 3.21.18)也删了。**官方只放最新版 ⇒ 下不回来。**
   ⇒ 以后改锚点时「新正则对旧版零漂移」**量不到了**,别声称验过。
   现实可行的两条退路:① 从**同事还没升级的机器**上拷一份旧 pristine bundle(最靠谱);
   ② 下一次升级前,**先把那一版的 pristine bundle 存成长期资产**再升
   (`~/.cursor-team-setup-backup/<ver>-*/` 里那两条 `workbench.*.js` + 四条 `cxctxwin__*`,
   一代约 200MB)—— 这件事只能在升级前做,升完就没有了。
   ⚠️ 判据纪律不变:**拿不到材料时报 SKIP 并说明"没有旧版参照物"**,
   ⛔ 不许把"只验了新版"写成"两代都验过"。

   **②腿 SKIP 时的替代判据(2026-09-15 用过;材料见上面那条红字)**:live 已升级、拿不到同版本参照物时,别把 SKIP 当过——
   改成「**只把改动的那几个字符回退**成旧形状生成基准实现(其余代码=当前版本,同一份钩子同一份
   `applyPatchesToText`),对 `~/.cursor-team-setup-backup/` 里**任意几个旧版 pristine** 各出一次产物,
   `cmp` 必须 BYTE-EQUAL」。这证明的是"新正则对旧版零漂移",比原②腿弱(不含 live 实机比对)但可复现,
   报告时必须**明说是替代判据**。⚠️ 别用 `git stash`/`git cat-file HEAD` 造基准:HEAD 可能落后于工作区
   已有的钩子重构(09-15 踩到——HEAD 版还是 09-10 前那个自写"每锚点恰好 1 次"的旧钩子,喂真 bundle
   在 multi 锚点 `nopromote` 上直接假红退出、连产物都不写,`cmp` 于是报"文件不存在"极易被误读成 DIFFER)。
   **②腿 SKIP 的另一种情况(2026-09-20 修掉)**:live 本身 pristine(补丁没装/已 `--revert`/
   刚升级还没 REPAIR)时,台架原来只认 `~/.cursor-team-setup-backup/` 里的种子,于是把
   "live 干净"读成"没种子"⇒ ②③④ **三腿全 SKIP**,而这恰好是最常见的状态(改完代码想验一下、
   手上没装补丁)= 台架白摆着。现在改成:按 **marker 计数**判 pristine(0=干净,>0=已打),
   干净就拿 live 那两条当 ③④ 的种子;② 仍然 SKIP,但理由写明是「**没有"已打"那一边可比**」
   不是通过。⚠️ `grep -c` 零命中时**既打印 0 又 rc=1**,写 `$(grep -c … || echo 0)` 会拿到
   `"0\n0"` 送进 `$(( ))` 直接 syntax error(实测)。
5. `VERIFIED_VERSIONS` 加新大版本(软闸只告警,真安全阀是 exactly-1)→ `python3 setup_impl_parity.py`
   → 改过模型清单的话再跑 `sh retire_model_offline_cases.sh`
   → `sh package_team_setup.sh` 重打 zip
   → **`python3 setup_package_freshness.py`**(逐成员 sha256 == 工作副本;换附件前的最后一道门)
   → 换飞书文档附件(**两篇都要换**:`OQCPdLd4MovEVoxGzdMcD3CJnCf` 与
   `FapXdudPLoTTRLxWGJQcqITpn9g`;只换一篇 = 新旧调位且不报错,判据见上节那张表)。
   **换附件这一步的固定六拍**(09-29 走通一遍,别省中间两拍):
   ① `docs +media-download --token <线上旧 token>` 先把线上那份拉下来 ——
      它既是**回滚素材**又是第②腿的 `--against` 材料;
   ② `setup_package_freshness.py --selftest` 再 `--against /tmp/<刚拉的>`(要有账,不只 PASS);
   ③ `docs +media-insert --type file --file-view card`(只能落文档末尾,记下返回的 `block_id`/`file_token`);
   ④ `docs +media-download --token <新 token>` 回环下来 `cmp` **逐字节**等于本地 zip;
   ⑤ `block_move_after`(1011 空操作要重 fetch 看顺序,别重试)→ `block_delete` 删旧 figure;
   ⑥ 重 fetch 验:旧 token 残留 0、新 token 1、`<figure>` 仍 1 个、`<img>`/`<whiteboard>` 数不变。
   🔴 **换完附件≠正文也对了**。09-29 换完包之后正文还写着「30 个模型」、默认 `sa-grok-4.6`、
   REPAIR「已适配 3.16~3.20.21」,而包里是 32 名 / `grok-4.7` / 已适配 3.21 ——
   同事照正文选模型名会选到已下架的 `sa-grok-*`。`README.txt` 是打包时从常量抠的**自动跟**,
   飞书正文是**手写的不跟** ⇒ 把「正文里的清单/默认名/适配版本号」当成第 7 拍去比一次。
6. 同事侧动作:纯补丁漂移 → **双击 `REPAIR-Mac.command`**;
   这一版还动了模型清单(加名/下架)→ 让他双击 **`UPGRADE-Mac.command`**(不问 Key,
   见「模型清单增减 + 老用户升级档」)。
7. **README.txt 里的模型清单/下架名单不许写死**,从 `DEFAULT_MODELS`/`RETIRED_MODELS` 抠
   (`__MODEL_LIST__` / `__RETIRED_MODELS__` 占位符),抠不到就**中止打包** ——
   "README 这一节是空的"同事不会来问,他只会按旧印象用。占位符残留检查已覆盖这两个新占位符。

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
   cmd-k 归 default / baseUrl=""+useKey=false
   ⚠️ 09-20 第二轮起后两条**换了口径**:EXACT 语义下装机会把他的第三方名删掉,
   所以不再是"没被动过"而是「**从最早那份 -preconfig 快照里还回去**」
   (上一版写的"deep-search 别人的模型没被动 / 两个数组只剩别人的"已作废)。
   判据=点名他那两个名字回来了 + 我们清单里的名字没了,**不是数个数**(N=0 两种成因同形)。
D) 无同版本备份分支                      ✅ 给官方重装指引,旧版 bundle 没被盖上去
E) --revert 版本不匹配                   ✅ rc=1 且不动 bundle(对照:同版本 rc=0 且真还原)
F) 预演不落盘                            ✅ bundle/库 md5 都没变(对照:带 --apply 两者都变)
```

## marker 演进

`@cx-queue-pump:v1`(只清无 uuid,救一半)→ `@cx-queue-diag:v2`(诊断 tick)→
`v3`(僵尸 uuid 判活)→ `v3.3`(去 120-tick 寿命上限)→ `v3.4`(在飞防护)→
`v3.5`(派发收敛:仅 heal 后/空闲派)→ **`v4`(现行:队列被外部消费让路 5 tick+
自家在飞豁免,退居纯兜底)**;根治靠 `@cxteam-norelay`(泵不再是主派发器);
链式增量线独立演进:**`@cx-chain:v3`**(exthost fetch seam,件B,见末节);
上下文窗口归一线独立演进:`@cx-ctxwin:v1`(补的是阈值函数而非构造器,漏 3 个消费者)→
`v2`(补构造器 A,漏了 B 的旁路)→ **`v3`(现行:A+B+C 三点,穷举 10 处算术点得出)**。
marker 停在 `v3` 不变、但**锚点写法 2026-09-23 改过一次**(3.21.18 命中 0 ⇒ 参数名/局部变量名
全改成 ID 捕获 + 反向引用)——语义没变所以不升 marker,3.21.18 与 3.20.17 产物都验过。
记忆:`feedback_cursor_queue_stuck_zombie_uuid_v3_liveness`、
`feedback_cursor_turnended_relay_orphan_fold_norelay_root_fix`;安装器设计与等价性证明:
`docs/cursor-g-naming-rollout-20260824.md` §七。
