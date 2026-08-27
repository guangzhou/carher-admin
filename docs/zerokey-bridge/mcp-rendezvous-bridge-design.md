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
| **0.2** | 临时 echo server + 公网入口 | server 起 + 公网可达 | 🟡 server 起了,nginx `/mcp-echo/` 502 待修 |
| **0.3** | 挂起超时(10/30/60s) | 拿到窗口数字 | ⏳ 待 0.2 |
| **0.4** | 一轮连环调用 | 拿到连环行为 | ⏳ 待 0.2 |
| **0.5** | 结果 moderation 干扰 | 拿到干扰形状 | ⏳ 待 0.2 |
| **0 裁决** | 三前提 GO/NO-GO | 前提①②过才进 Phase 1 | ⏳ |
| **1** | 会合桥最小实现(§3-5,配对用 B) | 端到端一次 MCP 调用打通 | 未开工 |
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
