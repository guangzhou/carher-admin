---
name: codex-desktop-startup-diagnose
description: 诊断本机 Codex Desktop（进程名 ChatGPT；app 路径按 bundle id `com.openai.codex` 探测，2026-09-10 起是 /Applications/OpenAI Codex.app，别写死）"新建窗口第一条消息转圈几十秒、第二条正常"这一类症状。含渲染层挂载量具（statsig gap vs app routes mounted）、app-server 端到端轮次量具（按 submission.id 配对，不认前台不弹的 turn-complete 通知）、出网三态探测（挂住/快速失败/可达）、干预实验式的重启修复，以及一个把四个已知假判据固化成代码的 GUI 探针。Use when 用户说 codex 客户端/桌面版发消息一直转圈、新会话第一条特别慢、别人正常我不正常，or 提到 startup_timing.py / logs_2.sqlite / statsig-refresh-diagnostics / host-resolver-rules。
---

# Codex Desktop 启动/发送卡顿诊断

> 实证：**2026-09-09 定因并修复**。症状=新建窗口第一条消息转圈几十秒、第二条正常。
> 根因=渲染层挂载路由前同步等一次经 `chatgpt.com` 的 statsig 拉取，本机 chatgpt.com
> **connect 超时**（不是 DNS 失败⇒不会快速报错）⇒ 重试满 ~32.5s。
> 干预实验：`34681ms/33118ms → 4542ms/3180ms`。档案见 memory
> `project_codex_desktop_first_message_33s_statsig_2026_09_09`。

## 0. 三十秒分诊

**先确认你落对了 skill**：本 skill 只管**启动慢 / 发送转圈**。
如果症状是「Codex 说需要环境中配置 `OPENAI_API_KEY`」、「本地 python 脚本跑不起来」、
「pip 装不了任何包」、「编译报 137」，那是另一条链路 ⇒ 转 skill **`codex-local-cli-toolchain`**。

```
scripts/startup_timing.py --days 2      # 渲染层：每个窗口挂载花了多久
scripts/turn_timing.py -n 20            # 服务端：每轮真实耗时
scripts/probe_egress.sh                 # 出网：谁在挂住
```

分诊表：

| startup_timing | turn_timing | 结论 |
|---|---|---|
| SLOW 且 statsig 占 >70% | 正常（个位数秒） | **就是本 skill 的主病**，走 §3 |
| SLOW 但 statsig 占 <70% | 正常 | 挂载慢但不是 statsig，去 §4 排别的 |
| OK | 慢/未完成 | 客户端没问题，慢在上游/路由 —— 转 litellm 那条线 |
| OK | 正常 | 症状不在这两层。**别猜**，先复现再量 |

## 1. 唯一权威判据（渲染层）

日志 `~/Library/Logs/com.openai.codex/YYYY/MM/DD/codex-desktop-<uuid>-<pid>-t<N>-i1-<HHMMSS>-0.log`，
时间戳 **UTC 带 Z**，本地 = +8。三行定生死：

```
[statsig-refresh-diagnostics] React root render requested   rendererWebContentsId=N
[statsig-refresh-diagnostics] ready provider mounted        rendererWebContentsId=N
[startup][renderer] app routes mounted after Xms            rendererWebContentsId=N
```

**routes mounted 之前 UI 发不出消息**，所以 X 就是"新窗口第一条要等多久"的上界。
前两行之差 = statsig gap；gap ≈ X 就说明瓶颈在那次拉取。

坑：
- 这三行**只在本次启动的 t0 日志里**。日志切片后就没有了 ⇒ 想量必须重启一次再跑。
- 每个窗口一个 `rendererWebContentsId`，隐藏的 `avatarOverlay` 也算一个、也付一次代价。
  所以"我只开了一个窗口"却看到两行是正常的。
- `wcid` 在不同启动里会重号，脚本按**文件**分桶，别自己按 wcid 去 join。

## 2. 唯一权威判据（服务端轮次）

`~/.codex/logs_2.sqlite`，表 `logs(id, ts, level, target, feedback_log_body, thread_id, process_uuid, estimated_bytes)`。

- ⚠️ `ts` 是 **unix epoch 秒**，1 秒分辨率 ⇒ 所有耗时读数 ±1s，别拿它量亚秒。
- 提交：`target LIKE '%::handlers'`，body 含 `op: TurnInput` 与
  `UserInput { content: [Text { text: "…" }`，里面的 `Submission { id: "…" }` 就是 submission.id。
- 完成：`target LIKE '%stream_events_utils'`，body 含 `Output item item_type="message"`，
  span 里带 `submission.id="…"`。**按 submission.id 配对，不按时间就近**（并发轮次会串台）。
- ⚠️ 打开一律 `file:...?mode=ro`。**永不 `immutable=1`** —— 那是"文件永不变"的承诺，
  SQLite 会跳过 `-wal`，实测少读 45 秒的新行，刚发的那轮凭空消失、探针假红。
  也别 `cp` 出来读（实测 **950MB**）。
- ⚠️ 判轮次完成**不许**用 `[desktop-notifications] show turn-complete`：应用在前台时它根本不弹。

`未完成` 的三种无害成因：用户改字重发（旧 submission 被顶掉）、纯工具轮次、本轮还在跑。

## 3. 修法（本次已验证）

### 3.0 「不是已经修好了吗？」——先查参数还在不在，别重新诊断

**这病 09-09 / 09-10 上午 / 09-10 下午 已复发三次，每次都是同一个原因：修复从未持久化。**
`--host-resolver-rules` 是**启动参数**，只作用于用那条命令拉起的那一个进程。用户双击图标、
系统重启、app 自动更新、LaunchServices 重开 —— 任何一次正常启动都不带它，原样退回 33s。

用户一说"怎么又转圈了"，先跑这两条，**30 秒定案**：

```bash
ps -axww -o command= | grep -v grep | grep -o 'host-resolver-rules=[^ ]*'   # 空 = 参数丢了，就是它
scripts/startup_timing.py --days 1                                          # 看最后一行
```

同机同 app 三轮对照：带参数 **2.8 / 4.8 / 4.6 s**，不带 **33.6 / 35.3 / 35.7 s**。
参数在不在就是全部解释，**不要再去查 statsig / MCP / 网络以外的东西**。

真正的了结只有下面「持久」那条，需要用户自己输密码（本机无免密 sudo）。
**只跑 `relaunch_fix.sh` 就是买一次性缓解，明天还会来找你。** 每次都要把持久那行一起递给用户。

### 3.1 原理与命令

让 `chatgpt.com` **快速失败**，而不是挂住。注意这不是"封掉 statsig"——
statsig 自家域名（api.statsig.com / featuregates.org / api.statsigcdn.com / featureassets.org /
events.statsigapi.net）本机**全部可达（403=可达）**，封它们只会制造新病。
瓶颈只在 chatgpt.com 这个入口：失败的请求实测是 `POST https://chatgpt.com/ces/v1/rgstr?k=client-…`。

**临时（免 root，agent 能自己做）** —— Chromium 自带的 app 作用域 DNS 覆盖，只影响该进程：

```bash
scripts/relaunch_fix.sh --yes      # 自带改前/改后对照表
# 等价于：
# osascript -e 'quit app "ChatGPT"'; sleep 7
# open -a "$(scripts/relaunch_fix.sh 打印的 APP)" --args \
#   --host-resolver-rules="MAP chatgpt.com 127.0.0.1,MAP *.chatgpt.com 127.0.0.1"
```

**持久（需要用户自己输密码，agent 不许代劳）** —— **2026-09-10 13:24 已落**：

```
! bash scripts/persist_chatgpt_block.sh        # 会自己 sudo 重入并提示输密码
  bash scripts/persist_chatgpt_block.sh --check # 只读验收，不需要密码
  bash scripts/persist_chatgpt_block.sh --revert # 撤销（日后要用代理开 chatgpt.com 网页时跑）
```

⛔ **不要手搓 `echo ... | sudo tee -a /etc/hosts`** —— 本机实测两次静默失败：

1. `/etc/hosts` 末尾**没有换行符** ⇒ 新行被粘成 `##TEC_END##127.0.0.1 chatgpt.com`，
   当成注释，解析器看不见。此时 `grep chatgpt /etc/hosts` **有输出但完全没生效**。
   ⇒ 判据必须锚定行首：`grep -E '^[[:space:]]*127\.0\.0\.1[[:space:]]+chatgpt\.com'`。
2. 文件里有 `##TEC_BEGIN##`/`##TEC_END##` 托管块（屏蔽 Apple 更新那套），写进去的行会消失。
   保守处置：插在该块**之前**。（"是它冲掉的"未坐实，别说成结论。）

脚本把这两个坑都埋了，并当场跑三条判据：hosts 独立成行 · `dscacheutil` 解析到 127.0.0.1 ·
curl 不再顶满超时。**三条同时绿才算成立**，只看第一条会被上面第 1 个坑骗过去。

原始等价命令（仅供理解，别直接用）：

```
! echo '127.0.0.1 chatgpt.com' | sudo tee -a /etc/hosts >/dev/null && dscacheutil -flushcache
```

副作用前提，落 hosts 前必须逐条确认：
1. 本机 chatgpt.com 本来就完全不通（`probe_egress.sh` 判 HANG）；
2. Codex 用的是 API key auth + `openai_base_url=cc.auto-link.com.cn/pro/v1`，不依赖 chatgpt.com；
3. **日后这台机上要用代理/VPN 访问 chatgpt.com，必须先删掉这行。**

没有 statsig 的 env kill switch（`app.asar` 搜遍只有 `STATSIG_STABLE_ID` 之类）。
`[analytics] enabled=false` 是 app-server 的旋钮，与渲染层 statsig **无关**，别拿它当解。

回归判据：`startup_timing.py` 的**最后一行** < 15000ms（退出码 0）；
再跑一次 `gui_send_probe.py` 拿 PASS。历史里的慢行是修复前的档案，不算失败。

## 4. 已证伪，别再捡回来

每条都有数据，重新提出前先自己拿到反例：

| 假设 | 证伪数据 |
|---|---|
| `no rollout found` 是成因 | 14:35:12 NO_ROLLOUT 之后 14:35:13 CONV_CREATED 照样成功 |
| MCP server 启动慢挡住了 | 卡住那条 thread 的 4 个 MCP 全在 ≤0.2s ready；几百秒的 `starting→ready` 是**懒启动闲置**，不是启动耗时 |
| 渲染层主线程卡死 | `sample` 2420/2497 主线程采样在 `mach_msg2_trap` = 消息循环**空闲** |
| Secure Input 吃掉了按键 | `ioreg` 里没有 `kCGSSessionSecureInputPID` |
| analytics flush 超时 | 日志里零次 |
| 渲染层 JS 异常 | sentry 队列空 |
| 重启 app-server 能修 | 重启后原样复现 |
| 297 个 skill 被截断导致慢 | 截断确实发生（`skills.max_context_tokens=5440`），但实测不进延迟路径 |

## 5. GUI 自动化纪律（血泪）

2026-09-09 我在同一轮里**四次**因为判据选错说出假结论。`scripts/gui_send_probe.py`
把这些固化成代码，用它而不是手搓 osascript：

1. 中文输入法会吞空格、把 `:` 变全角、吃掉回车 ⇒ 正文一律 `pbcopy` + Cmd+V，**绝不** `keystroke` 打正文。
2. `click at {x,y}` 在 Electron 上**空转**；`click menu item` 可靠；`keystroke` 可靠。
3. 「新建窗口」**没有快捷键**，`Cmd+N` 是「新聊天」、不改窗口数 ⇒ 别拿 `count of windows`
   判 Cmd+N 生效。下手前先读 `AXMenuItemCmdChar`/`AXMenuItemCmdModifiers`。
4. 空白新会话里再按 Cmd+N 是**空操作** ⇒ 别拿"有没有新 thread"判它。
5. 终判只认**截屏**（`screencapture -x -o -R`，区域必须来自真实窗口 bounds，
   写死坐标会拍到纯白）。Electron 不暴露 composer 的 `AXTextArea`。
6. 每个"X 没生效"的判断，先给判据本身立**阳性对照**。判据不会动，就没有"没生效"这个结论。

## 6. 顺带确认、但与本病无关的浪费（未处置，需用户裁决）

- 每个新会话额外一次 `feature=thread_title` 内部轮次：**20,005 input tokens**、
  `cachedInputTokens=0`、走 `gpt-5.6-luna`。`turn_timing.py --show-title` 能看见。
- 297 个 skill 被压进 `skills.max_context_tokens=5440`，全部截断到 ~325 字符（描述全废）。
- `remote installed plugin bundle sync failed` ×322、`failed to warm featured plugin ids cache` ×18
  —— 同源于 chatgpt.com 不通。
- `logs_2.sqlite` 已 **950MB**，无自动收缩。

## 脚本

| 脚本 | 作用 | 退出码 |
|---|---|---|
| `scripts/startup_timing.py` | 渲染层挂载量具（主量具） | 0=最后一次挂载 <15s |
| `scripts/turn_timing.py` | 服务端端到端轮次量具 | 0=窗口内无慢轮次 |
| `scripts/probe_egress.sh` | 出网三态：HANG / FAIL-FAST / REACHABLE | 0=无挂住域名 |
| `scripts/relaunch_fix.sh` | 干预实验式重启（`--revert` 可复现故障） | 0=改后达标 |
| `scripts/gui_send_probe.py` | 真发一条消息、按服务端日志判成败 | 0=PASS |
| `scripts/persist_chatgpt_block.sh` | **了结**：把 hosts 那行持久写好（两个静默坑已埋）+ 三判据验收；`--check` 只读 / `--revert` 撤销 | 0=三判据全绿 |

`startup_timing.py` / `turn_timing.py` / `probe_egress.sh` / `gui_send_probe.py`
均在 2026-09-09 本机实跑通过。`relaunch_fix.sh` 只验了参数守卫，
其重启命令体是当天证实修复的那条原命令（未再整跑，避免打断用户在用的会话）。
`persist_chatgpt_block.sh` 2026-09-10 本机实跑：`--check` 在未落地时正确翻红（三条判据全红、退出码 1），
落地后三条全绿 —— **红绿两态都见过**。
