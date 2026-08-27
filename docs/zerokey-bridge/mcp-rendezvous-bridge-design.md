# MCP 会合桥(Rendezvous Bridge)实现方案

> 状态:设计稿(2026-08-27)。**未开工**。Phase 0 通路验证已过一半,两个命门前提待测。
> 关联:`docs/zerokey-bridge/mcp-connector-native-toolcall.md`(7-27 通路发现)、
> `~/.claude/skills/chatgpt-web-mcp-connector/SKILL.md`(注册手法)、计划 `zesty-whistling-lake.md`。

---

## 1. 一句话

把 cursor-g 网页线"教模型写作文 ⟦cmd¦run⟧、我们从散文里抠命令"的脆弱通道,换成 ChatGPT 网页版
**协议级原生工具调用**(MCP connector)。模型发结构化工具调用,不再写作文 → 今天所有"抠作文"
长尾(未闭合块裸漏/方言漂移/自答误判)**从根上不存在**;且模型可在**一轮内连环调用**,多步任务
从"每命令排一次队"塌缩成"一轮跑完"。

## 2. 为什么是"会合桥"而不是直连

拓扑约束(物理事实,非选择):

- ChatGPT 后端(执行工具调用的一方)只能访问**公网 HTTPS**,访问不到用户本机 Mac(NAT 后无公网入口,且 MCP 不支持本地 stdio)。
- 真正执行命令的是用户本机的 Cursor。
- 因此需要一个**公网可达、且能挂住一个进行中调用等待异步结果**的中间件 —— 这就是网关(198 zerokey pod)。

它不是无状态路由(litellm 那种),它要**跨两条独立连接做配对与会合**:
一条是 ChatGPT 后端打进来的 MCP 调用(HTTP 请求,挂起等待),
另一条是 Cursor 与网关之间已有的 `/v1/responses` 流(function_call 下发 + 下一轮 output 回传)。

## 3. 架构图(数据流)

```
                         ┌────────────────────────────────────────┐
                         │  198 zerokey pod (公网 HTTPS 可达)        │
   ChatGPT 网页后端       │                                          │
   (模型发起工具调用)      │   ┌──────────────┐   ┌───────────────┐  │
        │  MCP call       │   │ /mcp endpoint │   │  会合表         │  │
        │───────────────► │──►│ (JSON-RPC)   │──►│ pending{call} │  │
        │  (HTTP 挂起等待) │   └──────────────┘   │  按会话配对     │  │
        │                 │                       └──────┬────────┘  │
        │                 │   ┌──────────────┐          │           │
        │                 │   │ responses.js │◄─────────┘           │
        │                 │   │ (已有网页线)   │  转成 function_call   │
        │                 │   └──────┬───────┘                      │
        └─────────────────┤          │  SSE: function_call           │
          MCP result 回填  │          ▼                               │
          (挂起的请求应答)  │      Cursor(用户本机)  ── 执行 shell ──   │
                         │          │  下一轮请求带 function_call_output│
                         │          ▼                               │
                         │      会合表匹配 call_id → 唤醒挂起的 MCP 请求 │
                         └────────────────────────────────────────┘
```

一次工具调用的完整生命周期:

1. 模型在 ChatGPT 会话里发 `api_tool.call_tool`,path=`/<connector>/<link>/<action>`,args=…
2. ChatGPT 后端把它翻成一条 **MCP `tools/call` 请求**打到网关 `/mcp`,请求**挂起等待响应**。
3. 网关 `/mcp` 收到:生成 `call_id`,查这条调用属于哪个 Cursor 会话(见 §5 配对),把 `{call_id, tool, args, resolve回调}` 塞进**会合表**,**不立即应答**。
4. 网关在对应 Cursor 会话的 `/v1/responses` 流上,以 `function_call` 形式下发这条命令(name=shell,arguments={command}),leadingText 照常处理。
5. Cursor 本机执行,下一轮请求带 `function_call_output`(call_id 对应)。
6. 网关收到 output → 用 call_id 在会合表里找到挂起的 MCP 请求 → **应答那个还挂着的 HTTP 请求**,把执行结果作为 MCP result 回填。
7. ChatGPT 后端拿到 MCP result → 模型在**同一轮**内继续(下一次 call_tool,或收尾)。

## 4. 组件清单

| 组件 | 位置 | 职责 | 新建/改造 |
|------|------|------|-----------|
| `/mcp` endpoint | zerokey pod(responses.js 同进程或旁挂) | 收 ChatGPT 后端的 MCP JSON-RPC(initialize/tools/list/tools/call),挂起 call | 新建 |
| 会合表 | zerokey pod 内存 | `Map<call_id, {session, resolve, timer}>`;配对 + 超时 | 新建 |
| function_call 注入 | responses.js 现有 finishWebTools/流式层 | 把会合表里待发的 call 以 function_call 形式下发 Cursor | 改造(复用现有 function_call 下发路径) |
| output 回收 | responses.js 现有 `_convDelta` 解析 | 从 Cursor 下一轮的 function_call_output 提结果 → 唤醒会合表 | 改造(复用现有 `function_call_output` 识别) |
| connector 注册器 | `mcp-connector-cli.js`(已存在,7-27) | 每账号注册 connector + 建 link,指向 `/mcp` | 复用 |
| 门控 + 降级 | responses.js prompt 组装 | `ZK_MCP_BRIDGE` 开时走 MCP,异常/超时降级回 ⟦⟧ 作文通道 | 新建 |

## 5. 关键设计决策(命门在这)

### 5.1 会话配对 —— MCP 调用怎么知道属于哪个 Cursor 会话

**问题**:ChatGPT 后端打来的 MCP 请求是**独立 HTTP 请求**,它不带 Cursor 的会话标识,网关怎么知道该把这条 function_call 下发到哪条正在挂着的 `/v1/responses` 流?

**候选方案**:
- **A. 每会话独立 link(推荐)**:注册 connector 时,link 的 path 里塞一个**会话专属 token**(`/<connector>/<link>/<action>?s=<sessTok>` 或 action 名编码)。网关按 sessTok 反查 Cursor 会话。缺点:link 是账号级注册,做不到"每 Cursor 会话一个 link"——需验证 link path 是否支持动态 query 透传。
- **B. 时间/账号窗口配对**:同一账号(acct82)同一时刻只有一条活跃 Cursor 会话在等工具结果 → 用 `acct + 最近一条挂起的 /v1/responses 流`配对。简单,但并发多会话时会串。**Phase 1 先用这个(canary 单会话足够),Phase 2 再上 A。**
- **C. 上下文回带**:让模型在 args 里带一个我们首轮注入的会话标记。依赖模型配合,不稳,不采用。

**这是整个桥最硬的设计点**,Phase 1 必须先用 B 打通、同时验证 A 的 link path 可行性。

### 5.2 超时与降级(前提①,Phase 0.3 待测)

MCP 请求挂起等 Cursor 执行,ChatGPT 后端给多久?**未知,Phase 0.3 测 10/30/60s。**

- 若窗口够宽(≥60s):普通命令直接"一口气"跑完。
- 若窗口窄(~10s):长命令(build/大扫描)超时 → 降级为**取号模式**:MCP 立即返回"执行中,call_id=X",模型稍后用另一个 action 查结果。多一次往返,但不卡死。
- 若 MCP 通路整体不可用(注册失效/超时频发):`ZK_MCP_BRIDGE` 门控**降级回现有 ⟦cmd¦run⟧ 作文通道**——作文通道全程保留,双跑,模型走哪条网关都接得住。

### 5.3 一轮连环(前提②,Phase 0.4 待测)

模型拿到第一个工具结果后,是否在**同一 turn 内**继续下一次 call_tool?**未知,Phase 0.4 测。**
- 若是:多步塌缩收益成立(核心卖点)。
- 若每次调用即收尾:退化成"每步一轮",延迟收益打折,但协议稳定性收益仍在(仍值得做)。

### 5.4 与 responses.js 的关系 —— 双跑,不替换

MCP 桥**不删**现有 ⟦⟧ 通道:
- `ZK_MCP_BRIDGE=1` 且该会话 connector 就绪 → 模型看得到原生工具,走 MCP 路。
- 模型偶发仍写 ⟦cmd¦run⟧ 作文(旧习惯)→ 现有 finish 状态机照样接。
- 两条路的 function_call 下发/output 回收**复用同一份 Cursor 侧协议**(Cursor 只认 function_call,不关心网关内部走哪条)。
- 收敛(删 ⟦⟧ 作文通道 + 九补丁)是 **Phase 4 之后**的事,数据证明 MCP 路吃下全部流量才动。

### 5.5 结果 moderation(前提③,Phase 0.5 待测)

工具返回内容(URL/长文本)是否被 ChatGPT 的 moderation 动手脚(url-safe 那条病在不在 MCP 路上)?
**未知,Phase 0.5 测。** 若在,现有 url-safe 回填逻辑需确认能否复用到 MCP result 路径。

## 6. connector 注册(运维面,已验证可脚本化)

- 每账号一次性:`devmode-enable`(账号级持久)→ `register --url https://chat.auto-link.com.cn/mcp --name zk-shell`(生产用正式域名,非 /mcp-echo/)→ `actions` 验 schema → `links/noauth` 建 link。
- 47 号池:`mcp-provision-pool.py`(已存在骨架)批量跑,幂等。
- 幂等/免人工点击已在 7-27 验证。

## 7. 门控与回滚

- 门控:`ZK_MCP_BRIDGE`(默认关)。开在 82 canary。
- 回滚:关门控 + 删 connector 注册(`mcp-connector-cli.js delete <id>`),5 分钟回到现状,作文通道从未离开。
- 字节级:responses.js 改动全部锁在 `ZK_MCP_BRIDGE==='1'` 后,默认关 = 零行为差(与现有所有 ZK_ 开关同纪律)。

## 8. 分阶段(闸门制,当前进度标注)

| Phase | 内容 | 闸 | 状态 |
|-------|------|----|------|
| **0.1** | 通路 probe(注册→抓 schema→删) | 5/5 通过 + plan 够格 | ✅ **已过**(acct82,deepwiki schema 抓到) |
| **0.2** | 临时 echo server + 公网入口 | server 起 + 公网可达 | ✅ **已过**(echo server 起 + nginx `/mcp-echo/` 通;502 是 reload 竞态,自愈) |
| **0.3** | 挂起超时(10/30/60s) | 拿到窗口数字 | ✅ **已过**:窗口 **30-60s**(delay 0/15/30s marker 原样回;60/90s completed=0 空返) |
| **0.4** | 一轮连环调用 | 拿到连环行为 | ✅ **已过**:一轮内连调 chain_step ×3 全 ack(核心塌缩收益成立);**但 server 见 retry-storm(同 call_id 每 ~60s 重发)** |
| **0.5** | 结果 moderation 干扰 | 拿到干扰形状 | ✅ **已过(利好)**:A(无 url)/B(带 url)均 completed,URL **原样穿透**(`url_intact=True`)→ **MCP 路不走 prose 路的 url-moderation** |
| **0 裁决** | 三前提 GO/NO-GO | 前提①②过才进 Phase 1 | ✅ **GO**(见 §12) |
| **1** | 会合桥最小实现(§3-5,配对用 B) | 端到端一次 MCP 调用打通 | 🔶 3 承重件绿(33/33+25/25+17/17);**A/B 已裁决=Branch A(§13.3-裁决)**;剩 Branch-A 流缝合(跨轮 SSE reader+可重指针 socket)未开工,待拍板 |
| **2** | 82 canary(注册 connector + 开门控) | 离线全绿 + 复杂任务探针 + s3h + codex 一票否决 | 未开工 |
| **3** | soak 数据裁决(MCP 轮 vs 作文轮分别统计) | 成功率/时延达标 | 未开工 |
| **4** | 全量(47 号)+ 101 切换 + 收敛删补丁 | 每删一项标注被 MCP 哪个机制替代 | 未开工 |

## 9. 验收标准(Phase 2 canary 闸)

1. 离线病例库全绿(现有 6 套 + MCP 桥新增会合状态机单测)。
2. 复杂多步任务探针(如"梳理架构写文档"这类)在 MCP 路下:无裸漏、无假绿、链接完整。
3. s3h 8/8(或 tool.r2 判据按已知错配裁定)。
4. codex 一票否决 PASS(物理隔离仍成立:codex 服 `cursor-fc-5.6-sol` 的是另一 pod)。
5. 时延:MCP 路多步任务 < 作文路同任务(用 digest 分桶对照,scoped key 真流量)。

## 10. 风险台账(如实,不粉饰)

| 风险 | 性质 | 缓解 |
|------|------|------|
| 挂起超时太短 | 前提①,待测 | 降级取号模式 |
| 模型不连环 | 前提②,待测 | 收益打折但协议稳定性仍在 |
| 会话配对串号 | 设计难点 §5.1 | Phase 1 单会话验证,Phase 2 上专属 link |
| developer_mode 被官方收紧 | 固有(整条线骑在对方产品上) | MCP 不加新险;门控可秒回作文通道 |
| 结果 moderation | 前提③,待测 | 复用现有 url-safe 回填 |
| 47 号注册运维 | 一次性 | 已验证幂等脚本 |

## 11. Phase 0 遗留物清理清单(收尾时执行)

- 停 echo server:`pkill -f mcp-echo-server`。
- 删 nginx `/mcp-echo/` location + reload(备份在 `/home/cltx/backups-nginx/chat.*.pre-mcpecho.conf`)。
- 删 pod 内 `/tmp/sess82.json`、`/tmp/mcp-cli.js`(含凭据)。
- 若 Phase 0 期间为测试注册过 connector,`mcp-connector-cli.js delete` 清掉。
  - 本轮实测注册物:connector `asdk_app_6a8ff151eb2c8191806fbd7b4c0b5315`、link `link_6a8ff18fdce481918361bd73021244d0`(name `zk_echo_phase0`,actions echo/long_result/chain_step)。

## 12. Phase 0 裁决(2026-08-27)—— **GO**

三前提逐条落地(全走 82 lane 无 tools 声明的干净直通,scoped key 用完即删):

| 前提 | 闸门 | 实测 | 裁决 |
|------|------|------|------|
| ①挂起窗口 | 够普通命令一口气跑完 | **30-60s**(0/15/30s marker 原样回;60/90s 空返) | ✅ 普通命令直通;长命令(>30s)走取号降级 |
| ②一轮连环 | 同 turn 内连续 call_tool | chain_step ×3 一轮内全 ack,正文逐条复述 | ✅ **核心塌缩收益成立** |
| ③结果 moderation | 工具结果不被换字/拦截 | A/B 均 completed,URL `url_intact=True` 原样穿透 | ✅ **利好**:MCP 路不走 prose url-moderation |

**裁决:GO 进 Phase 1。** 三前提无一 NO-GO,且 ③ 反而消掉一整类 prose 路病(url-moderation 占位)。

### Phase 0 数据强制的 Phase 1 设计修正

1. **会合表必须按 call_id 幂等去重**(0.4 硬发现)。server 日志见 ChatGPT 后端对同 `call_id` **每 ~60s 重发** tools/call(与 ① 的 60s 超时同源:窗口内没等到应答就重试)。会合桥若不去重,一条命令会被下发 Cursor 多次 → 重复执行。**Map key = call_id,重发命中在途 entry 直接挂起复用同一 resolve,不新建 function_call。**
2. **应答目标 ≤30s,不吃满 60s**(0.3 硬发现)。挂起在 30s 内没等到 Cursor output → 立即走**取号降级**:MCP result 回 "执行中,call_id=X,稍后用 poll action 查",别让 ChatGPT 后端撞 60s 超时触发 retry-storm。
3. **url-safe 回填 MCP 路免做**(0.5 硬发现)。工具结果里的 URL 原样穿透,§5.5 前提③的担忧不成立;现有 prose 路 url-safe 逻辑不需移植到 MCP result 路径。
4. **broken-pipe 容错**:echo server 实测被 ChatGPT 后端在应答前掐连接(BrokenPipeError)。会合桥 `/mcp` 写回时必须 try/catch,连接已断则把结果暂存(下次同 call_id 重发时直接返),不崩进程。

## 13. Phase 1 实现进度(2026-08-27)

### 13.1 已建并离线验绿的两个承重原语(自含件,验过再嫁接)

两件都是**新写的自含 Node 模块**(不从 responses.js 抠字节),锁在 `ZK_MCP_BRIDGE==='1'` 后才嫁接,默认关=零行为差。

| 件 | 文件 | 职责 | 离线 |
|----|------|------|------|
| 会合表状态机 | `scripts/zk-cursor-web/mcp_rendezvous.js` | PENDING/TICKETED/RESOLVED 三态;§12 四修正全落地;**不内置 setTimeout**,ticket/GC 靠显式 `sweep(nowMs)` 驱动(注入时钟=完全确定性) | **33/33**(`mcp_rendezvous_offline_cases.js`) |
| JSON-RPC 派发器 | `scripts/zk-cursor-web/mcp_jsonrpc.js` | MCP Streamable-HTTP 信封解析/构造;`tools/call` 委托注入的 async `callTool`(会合桥里=会合表 deliver+await);形状逐字镜像 Phase 0 echo 契约 | **25/25**(`mcp_jsonrpc_offline_cases.js`) |

JSON-RPC 派发器额外锚定 Phase 0 日志实录的真实调用序列(`initialize` 重发 → `notifications/initialized` → `server/discover`(OpenAI 私有,落 -32601 = Phase 0 实证可接受)→ `tools/list` → `tools/call` id=0 重发×13 = retry-storm 现场,靠会合表按 `hash(session+tool+args)` 去重,**不能用 JSON-RPC id**,它是连接内计数器每次复用 0/1)。

### 13.2 流缝合承重件的 ground-truth(现场读 82 活字节,非笔记)

Explore 机械测绘 `/tmp/resp_live_82_20260827.js`(md5 `2f94ed79`,2192 行)得**确定事实**:

- **今天没有跨 Cursor 轮held socket**。每个 Cursor `/v1/responses` 轮开一条全新 `chatgptApi.chatCompletion(...)`(L775),把 ChatGPT SSE **一次性 drain 到底**(L1789–1932)后关闭。跨轮续接全靠 `_convCache` 增量:`findConvSession`(L359–371)命中 → `_stripAssistantItems(input.slice(count))`(L493–502)只发新 item → `saveConvSession`(L372–385)落 `{convId,parentId,count,digests}`。
- `finishWebTools(res, ctx)`(L1938)是**唯一**对 Cursor SSE 发 `function_call` 的出口;`ctx.parsed={calls:[{name,arguments}],leadingText}`;`call_id` 在此处 `newCallId(c.name)` 现铸(L1957 区)。
- `function_call_output` 解析点:L96/236/240/489/509/561(取结果文本 / 定位末条输出 / 判本轮是否带工具结果)。
- `ZK_*` 门控惯例:`const proto2 = process.env.ZK_PROTO_V2==='1' && useWebTools`(L526)—— `ZK_MCP_BRIDGE==='1'` 照此在 handler 顶部取值。

### 13.3 流缝合的**唯一承重未知**(数据栏空 —— 按 CLAUDE.md 纪律不臆断)

- **假设**:ChatGPT-web 经 `chatCompletion` 路驱动、且模型调用 MCP connector 工具时,它对网关的 SSE 会**在同一连接上挂起等 `/mcp` 结果**(分支 A),生成完再续。
- **证伪条件**:若为分支 B —— ChatGPT **结束本轮 SSE**(把 tool-call 记进会话),靠后续 `chatCompletion` 续接 —— 则网关会看到本轮 SSE 正常 drain 到 `message_stream_complete`,且下一轮上行 delta 里带该 tool 的调用记录。
- **数据**:**无**。Phase 0 只证了 ChatGPT **自家 web UI** 打 connector 时 `/mcp` HTTP 请求被挂起 ~60s;那不是网关 `chatCompletion` 路的观测。两分支的缝合设计完全不同:
  - **分支 A(挂起-续流)**:需新增一个**跨 Cursor 轮存活**的 ChatGPT SSE reader,其"输出目标 Cursor socket"是可重指针 —— turn-1 socket 用 `finishWebTools` 发完 function_call 关闭后,把 reader 续流重定向到 turn-2 socket。这是比会合表更重的改造,现有 per-turn drain 模式不直接支持。
  - **分支 B(轮末-续轮)**:直接复用现有 `_convCache` 续接机械 —— `rendezvous.complete` → 触发一次新的 `chatCompletion`(delta=function_call 记录+结果)→ drain → 灌进 turn-2 Cursor socket。几乎全是已有字节。
- **合法下一步(不跳过数据)**:分支判定需一次**针对性探针**——在网关挂最小 `/mcp`(仅接 `tools/list`+`tools/call`,`callTool` 先只记录不真会合),让账号注册的 connector 在 `chatCompletion` 驱动的一轮里被模型选中,**观测 SSE 帧**:本轮是 drain 到底(→B)还是在 tool-call 处停住不出 `message_stream_complete`(→A)。此探针**离线做不到**(要 ChatGPT 后端真的经网关路调 connector),属 Phase 1 canary 实验,须走完整闸门 + 显式 canary go 才点。

### 13.3-裁决(2026-08-27,canary 探针实测)—— ✅ **Branch A(挂起-续流)**

探针形态遵 §13.5(最小、可回滚、账号面零副作用):
- **最小 `/mcp`**:`scripts/zk-cursor-web/mcp_probe_server.js`,单工具 `zk_probe`,`callTool` **秒回**固定
  marker `ZK7391`(不真会合),每请求落 `/tmp/mcp-probe.log` 带时间戳。挂 198 host nginx
  `location ^~ /mcp-probe` → 公网 `https://chat.auto-link.com.cn/mcp-probe`。
- **connector**:在 **acct93**(授权"装",web-quota resale 活字号,devmode 已开)`provision`
  → connector `asdk_app_6a903a86…` + noauth link `link_6a903a8f…`。
- **驱动一轮**:用 pod 自带 `ChatGPTAPI`(=真实 serve 路,内部 `_refreshSentinel` PoW +
  `_prepareConversation`,解决 raw replay 的 403 "unusual activity")发**一轮** f/conversation,
  唯一改动 `body.metadata.developer_mode_connector_ids=[connector]` 让模型本轮看得到 `zk_probe`。
  **真实 serve 路该字段恒为 `[]`**(api.js L57),故真实用户看不到本工具 = 账号面隔离。观测唯一。

**双侧对时数据(三段式"数据"栏,不再空):**

| 侧 | 事件 | 相对/绝对时刻 |
|----|------|------|
| f/conversation SSE(zero-93,**单条 HTTP,55 帧**) | `api_tool.call_tool` 发出(模型调 zk_probe) | @10.311s |
| probe server(zero-140,`/tmp/mcp-probe.log`) | OpenAI 后端 POST `/mcp-probe`:`initialize`+`tools/call zk_probe args{}` | 13:30:10.494Z(**turn 进行中**) |
| **同一** f/conversation SSE | tool 结果回注同流(`author.role:"tool" name:"api_tool.call_tool"`,content_type code,`ZKPROBE_RESULT…marker=ZK7391`) | @10.989s |
| **同一** SSE | 模型正文续接(`data:{"v":"-alive marker=ZK7391"}`) | @12.554s |
| **同一** SSE | `message_stream_complete` / 收尾(末帧 17.140s) | @16.511s |

SSE VERDICT(探针自打印):`{"frames":55,"sawToolCall":true,"toolCallAt":"10.311s","markerAfterToolCall":true,"sawComplete":true,"completeAt":"16.511s"}`

**裁决 = Branch A**。call→result→continue→complete 全生命周期在**一条** f/conversation 连接上:
模型对网关的 SSE 在同连接挂起等 `/mcp` 结果,拿到后回注 tool result、在同流续接模型正文、drain 到
complete。**证伪条件(本轮 SSE drain 到底后 tool 调用只记进会话、靠后续请求续接 = B)未出现** —— marker
在 tool-call **之后**、于**同一流内**出现,而非下一轮 delta。

**对设计的硬含义**:§13.3 分支 A 成立 = 流缝合走**较重的分支** —— 网关须持一个**跨 Cursor 轮存活**的
ChatGPT SSE reader,其"输出目标 Cursor socket"可重指针(turn-1 socket 用 `finishWebTools` 发完
function_call 关闭后,把 held reader 续流重定向到 turn-2 socket)。现有 per-turn drain(§13.2)**不直接支持**,
须新增。

⚠️ **诚实旗:长挂起时长未测(数据栏空)**。本探针 `/mcp` **秒回**,实测证的是"流能挂起-再续"(挂 <1s)。
真实桥 `/mcp` 要挂 **30-60s** 等 Cursor 干完 + 下一轮 output。那个 30-60s 数字来自 **ChatGPT 自家网页
界面**调工具的观测(Phase0),**不是网关这条 SSE 路的观测** —— 把"流能扛 30-60s 长挂起"当已知,是拿两条
不同路子的数据拼出来的**假设**,非本路证据。按 CLAUDE.md 纪律:这不是"实现要求",是**动手前必须先补的一次
测量**(见 §13.6 Gate-1)。

**探针遗留物清理(已执行,回到 pre-probe)**:connector 已 `delete`(200,复验 404 "Connector not
found");zero-93 `/tmp` 凭据/CLI/probe/SSE-log 擦净;probe server(zero-140)kill、文件删净;nginx
`/mcp-probe` 从 backup `chat.auto-link.com.cn.conf.pre-mcpprobe.20260827-210819.bak` 还原(`-t` OK +
reload,外部复验 502 = block 已移除)。**残留**:noauth link `link_6a903a8f…` 无 list-by-account 无法
单删,connector 删后该 link 指向死 connector = 模型调不动 = 已知无害残渣(与 Phase0 `asdk_app_6a8ff151…`
同性质)。

### 13.4 账号面风险旗(**探针期已消化,但上线期重新打开** —— 见 §13.6 Gate-2)

探针**安全**靠的是:真实 serve 路 `developer_mode_connector_ids` 恒为 `[]`(api.js L57),真实用户看不到
connector,探针只在注入字段的那一轮可见 → 账号面零副作用,已验(connector 删复验 404)。

**但这份安全不能平移到上线。** 生产的桥要工作,**恰恰必须往真实 serve 路持久塞 connector**,否则模型看
不到工具、桥即死。也就是:让探针安全的那道隔离,正是上线时必须打破的东西。一旦打破,真实服务每轮都带
connector,acct 与 codex CLI 线共享账号面(custom instructions/memory/token) → **"给真实 serve 路持久
挂 connector 是否对 codex CLI 可见/有副作用"仍无数据**。探针期的"安全"≠ 上线期的安全,**不得用前者当后者
的证据**。动手前须单独验(§13.6 Gate-2,codex 一票否决)。

### 13.6 动手前的两道数据闸(Branch-A 实现的前置,数据栏现为空)

Review(2026-08-27)钉出:§13.3 裁决对(A 站得住),但"能开工"这个结论还差两格数据。补齐前不写任何
Branch-A 流缝合代码。两闸均设计成**最小、可回滚、用完即拆**的探针,不动线上代码。

- **Gate-1 长挂起保活(可现在跑,不碰账号面加号)**:把 `mcp_probe_server.js` 的 `callTool` 从秒回改成
  **可配置延迟**(`ZK_MCP_PROBE_DELAY_MS`,取 30000/45000/60000),复用 §13.3 同一 canary 形态发一轮,
  观测:held 期间 f/conversation SSE **reader 是否断 / 心跳帧形态 / broken-pipe 时刻**。
  - **证伪条件**:若流在 30-60s 挂起中被上游掐(READ_ERROR / done 提前 / 无 keepalive 帧)→ Branch-A
    "同连接保活"在真实时长下**不成立**,须改设计(如网关侧 keepalive 注入 / 或退回研究 Branch-B 式续接)。
  - **通过判据**:delay 30/45/60s 三档,tool 结果回注仍在**同一条** SSE、drain 到 complete、reader 未提前
    断 → 长挂起保活成立,Gate-1 绿。
- **Gate-2 codex 零影响(账号面,须显式授权 + 一票否决)**:验"给真实 serve 路**持久**挂 connector"对
  codex CLI 的影响。**注意与探针期不同**:探针只在注入字段那轮可见,Gate-2 要测的是**常驻可见**的形态。
  - 做法:在授权账号挂 connector 后,**不注入** `developer_mode_connector_ids`,跑 codex CLI 回归
    (compare 三跑,工具轮 item 结构 `["function_call","reasoning"]` + called_tool=true 与基线逐字对齐)。
  - **一票否决**:任一结构漂移 / codex 侧看到 connector / 账号面异常 → 立即 delete connector、Branch-A
    路**账号面配对方案作废**,回退研究"不碰 serve 路 connector"的替代(如独立账号面 / 方案 A 专属 link token)。
  - 未获显式授权前 **Gate-2 不跑**;Gate-1 与 Gate-2 均绿才谈动工。

### 13.5 本轮结论

会合桥两个可离线证的承重件已 GO(33/33 + 25/25)。**流缝合的 A/B 承重未知已由 canary 探针裁决 =
Branch A(挂起-续流,见 §13.3-裁决,2026-08-27)**,双侧对时数据钉死,证伪条件未出现。探针本身已按最小/
可回滚/账号面零副作用形态执行完并全量拆除,回到 pre-probe。

**但 review(2026-08-27)钉出"能开工"结论还差两格数据(§13.6)**:①长挂起(30-60s)保活未在网关这条 SSE
路实测过——秒回探针只证了 <1s 的挂起-再续,30-60s 数字借自 ChatGPT 网页界面,非本路证据(Gate-1);
②让探针安全的账号面隔离(serve 路 connector 字段恒空)正是上线时必须打破的东西,持久挂 connector 对
codex 的影响仍无数据(Gate-2)。**Gate-1、Gate-2 均绿前不写 Branch-A 流缝合代码。** 实现全程锁在
`ZK_MCP_BRIDGE==='1'` 默认关后,codex 一票否决。`装` 授权只覆盖判定探针,Gate-1 可现在跑(不碰账号面),
Gate-2 与动工均须显式拍板。
