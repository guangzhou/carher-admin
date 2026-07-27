# zerokey → Codex Bridge:实现总结与全员落地计划

> 状态:**本机端到端已跑通(2026-07-25)**,集群化全员部署待做。
> 目标:让 Codex(命令行 + IDE)通过 zerokey 网页额度干活(读飞书文档、跑 lark-cli 等),**不烧 codex 的 5h/7d 配额**。

---

## 1. 已达成(本机验证通过)

Codex(gpt-5.6-terra/sol)→ 本机 bridge → zerokey 网页 pod 池 → 出工具调用 → 本机执行 lark-cli/shell → 读飞书文档 → 总结。全程走网页额度,**零 codex 配额消耗**(pod 打 `backend-api/f/conversation` 网页会话,非 `codex/responses`)。

| 目标 | 状态 | 实测 |
|---|---|---|
| 快 | ✅ | 单轮 3-5s(网页回放固有下限) |
| 出 tool_call | ✅ | 10/10 命中 |
| 落盘可见 | ✅ | litellm Request Logs 能看到(key=cursor-liuguoxian-l08v) |
| 不烧配额 | ✅ | 纯网页会话,pod 无 codex-token |

---

## 2. 架构

```
现状(仅本机可用):
  你的 codex → 本机 bridge(127.0.0.1:8788) → 本机 ssh 隧道(→svc ClusterIP) → 198 zero-N pod

目标(全员可用):
  任何人 codex → cc.auto-link.com.cn/zk/v1 → 198 集群内 bridge 服务 → zero-N pod(同集群直连,无隧道)
```

**核心组件:** `scripts/chatgpt-onboard/zerokey-codex/bridge/zerokey-codex-responses-bridge.py`

---

## 3. 完整根因链(踩过的坑 → 修复)

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 网页模型拒绝调工具 | 提示词含"读/访问/执行"触发拒绝反射 | GUIDE:命令必调工具、闲聊回文本、防瞎试 |
| 2 | zerokey 500 | bridge 走 `Bearer vscode`→老 ToolCompiler | 改走 `/v1/responses` + `Bearer raw` 注入路径 |
| 3 | "没有文件系统" | codex system prompt 声明真实 fs,与网页模型冲突 | 剥离 codex developer/environment_context |
| 4 | IDE 只给编排工具无 shell | `multi_agent` 默认走 WebSocket,bridge 只有 HTTP | provider 配 `supports_websockets=false` 强制 HTTP → **multi_agent 照开不降智**(见 §3.5;早期一刀切 `multi_agent=false` 已废弃) |
| 5 | IDE 死循环 149 条 | bridge 返回 `exec_command` function_call,但 GUI 期待 `exec` custom_tool_call | 按 acct 蓝图返回 `custom_tool_call`(name=exec, input=JS `await tools.exec_command({cmd})`) |
| 6 | 命令跑成 help | 模型多包一层 `bash -lc` | `_strip_shell_wrapper` 剥壳 |
| 7 | "hi"触发 echo 死循环 | 剥离 prompt 后模型失去"何时该调工具"判断 | GUIDE 精准引导 + 重复命令刹车 |
| 8 | 50-90s 极慢 | ①瞎试错命令 ②跑满重试 ③隧道抖动 | AGENTS.md 硬规则 + 首个成功即返回 + svc ClusterIP 隧道 |
| 9 | 隧道频繁 ConnectionReset | ssh -L 钉死 pod IP,pod 重建即失效 | 隧道转发到 **svc ClusterIP**(稳定,pod 重建不变) |
| 10 | 多 pod 抢答浪费 | fanout=5,4 个白跑 | fanout=1 + **pod 健康追踪**(自动跳过坏 pod) |
| 11 | litellm 前端看不到 | 落盘记录缺字段 | 补 `api_key`(真实 hash)+`status=success`+`call_type=aresponses`+`metadata.user_api_key` |

---

## 3.5 关键突破:multi_agent 不用降智(2026-07-25)

**质疑**:"为啥要改 model 名 / 关 multi_agent?先锋本来配的就是 zerokey,关 multi_agent 不是降智吗?"

**根因**:codex 的 multi_agent(IDE/app-server 模式)默认走 **WebSocket** 连 provider,bridge 只有 HTTP → 连不上 → 表现为"没有 shell 工具/降级"。之前一刀切 `multi_agent=false` 是**回避**,确实降智。

**真解**:在 codex model_provider 配 `supports_websockets=false` → codex **强制走 HTTP** 而非 WebSocket → bridge 照常翻译 exec → **multi_agent 开着也能干活**。

```toml
[model_providers.carher_dev]
base_url = "http://127.0.0.1:8788/v1"   # 集群化后 → cc.auto-link.com.cn/zk/v1
env_key = "CARHER_DEV_KEY"
wire_api = "responses"
supports_websockets = false             # ← 关键:强制 HTTP,不关 multi_agent
```

**实测(2026-07-25)**:`multi_agent=true` + `supports_websockets=false` → `echo madump` 真实执行成功,bridge 日志 `REQ items=9 stream=True` → 命令跑通。**不关 multi_agent、不改 model 名、不降智**。

> 偶发"没有 shell 工具"= pod 命中波动(某轮 miss 降级 message),非结构问题,靠 pod 健康追踪 + 首个成功即返回收敛。

---

## 3.6 GUIDE 必须破除"我没有 shell"的自我认知(2026-07-25)

网页模型的默认自我形象是**沙箱聊天机器人**,一遇到 URL/外部资源就拒绝调工具,把命令推回给用户:

> "我不能运行 `lark-cli` 命令,因此不能伪造读取结果……你在本机执行后把输出贴给我"

注意它**命令写得完全正确**,只是认为自己没有执行能力。所以不是协议缺陷(同结构请求 `echo` 就能出 exec),是提示词强度不够。

GUIDE 补三条硬约束后修复:
1. 绝不声称自己没有工具/无法执行;
2. 绝不要求用户跑命令再贴回来;
3. URL 不是障碍 —— 飞书 docx 链接直接 `lark-cli api GET /open-apis/docx/v1/documents/<DOC_ID>/raw_content`(`<DOC_ID>` = `/docx/` 后那段)。

**实测**:修复后真调 lark-cli 读到文档并正确总结;文档无权限时如实报 `1770002 not found`(不编造)——手动 lark-cli 直连同样报错,证明是文档本身问题,非链路。

---

## 3.7 回归审查发现的并发缺陷(2026-07-25)

压测(8-10 并发)时抓到两个真缺陷,均已修:

### ① rid 碰撞 → **静默丢账**(严重)

`rid = "zk_%d" % int(time.time()*1000)` 是**裸毫秒时间戳**,而这是 ThreadingHTTPServer。实测 8 并发命中 3 组重复 rid;集群侧 10 并发 → 响应 id 只有 7 个唯一。

危害不止日志混乱:`mk_sql.py` 用 `request_id` 配 `ON CONFLICT DO NOTHING`,**碰撞的行进 litellm 时被静默丢弃 → 用户少记账**。落盘文件里实证到 3 条修复前的重复 rid。

修复:`new_rid()` = 毫秒 + pid + 加锁的单调计数器。验证 8 线程 × 200 = 1600 个 rid 零碰撞;集群侧 10 并发从 7/10 → **10/10 唯一,10 key 全归属,零丢账**。

### ② 客户端取消 → `BrokenPipeError` traceback(噪音)

用户按 Esc 取消是常规操作,但 `_ev()` 写已关闭的 socket 抛未捕获 `BrokenPipeError`,每次取消刷一条完整 traceback 并让 handler 线程中途死掉。

修复:`_ev` 捕获 → 抛 `ClientGone`,三个 SSE 路径分别优雅收尾并记一行 `client gone`。**账不受影响**(`_spendlog` 在流式输出之前调用,已验证取消后仍记账)。

### ③ 顺带:启动端口竞争报 traceback

重启时撞旧进程 TIME_WAIT socket,报裸 `Address already in use` traceback,看着像崩溃。加 `allow_reuse_address` + 友好报错(提示用 lsof 查是否已有 bridge 在跑)。

### 不是 bug 的:本机 10 并发 7 个超时

本机只有 4-5 条 ssh 隧道,10 并发超容量 → `round 0 timed out`。**集群侧配了 19 个 pod,10 并发 10/10 成功(平均 ~5s)**。全员走集群,此瓶颈不存在。三段式验证:降到 3 并发 → 3/3 成功且全记账,假设成立。

> 遗留(已知未修):上游全失败时 `_fail()` 返回 **HTTP 200** + 错误文本,客户端无法从状态码区分成败。

---

## 3.8 代码审查:8 个已修缺陷(2026-07-25)

对 bridge 做了一轮严格审查(逐条实证复现,不接受"可能有问题"),按严重度:

### ① 命令注入 → 远程 RCE(CRITICAL)

`filePath` 未校验就拼进 heredoc,且 sentinel 固定为 `CODEX_PATCH_EOF`:

```
filePath = 'a.txt\n*** End Patch\nCODEX_PATCH_EOF\nrm -rf ~/IMPORTANT'
```

heredoc 提前闭合,`rm -rf ~/IMPORTANT` 变成顶层命令,由 Codex 通过 `zsh -lc` 执行。**关键在于这个参数是远程网页会话选的**,而它的上下文可被它读到的任何内容污染(比如一篇飞书文档)——这是一条打到用户本机的 RCE 路径,只靠 Codex 审批弹窗挡着。

修:`_safe_path()` 拒绝含换行/NUL 的路径 + 随机化 sentinel。

### ② `int('end')` 崩溃 → SSE 永不终止(CRITICAL)

模型给的 `endLine` 可能是 `"end"`,`int()` 抛异常打死 handler 线程,而此时 `response.created` 已发出 → 客户端收到两个事件后**再无终止事件,socket 还开着**,Codex 挂到自己超时。

修:范围解析失败退回 `cat`;并给 `do_POST` 加最外层兜底,任何异常都保证发出终止事件。

### ③ 负载严重倾斜(HIGH)

`mark_health` 把"回文本无 tool_call"也算失败,可按 GUIDE 纯文本是聊天的**正确**回答 → 几轮闲聊就把所有 pod 打到 -3 分;且排序策略让 RR 轮转失效。实测 400 请求 4 pod 分布 **291/109/0/0**,两个 pod 零流量;分数触底的 pod 永久拉黑不再重试。

修:健康分改成**排除过滤器**而非排序键(健康的 pod 之间公平 RR)、miss 只扣 0.34、坏分随闲置时间 decay 恢复。实测 **100/100/100/100 完美均分**;单个坏 pod 被压到 0.5% 而健康 pod 间 133/132。

### ④ 502 突发导致请求丢弃(HIGH)

`max_rounds=2` 固定 2 轮,19 个 pod 的池子里若两次都撞上瞬时 502 就整个请求失败。实测 8 并发 **3 个失败**。

修:错误轮数自适应 `min(6, len(UPSTREAMS))` + 重试跳过已失败的 pod。集群复验:同一轮发生 **12 次 502,8/8 全部成功、零最终失败**。

### ⑤ 我自己上一轮的修复对真实客户端无效(HIGH)

§3.7 加的 loop-brake 放行条件要求 `type == "message"`,但 **Codex 真实发送的用户消息没有 `type` 字段**。我的单测用了带 `type` 的构造,自己骗了自己 —— 线上仍会吃掉合法的"再跑一次"。修:接受 `typ in (None, "message")`。

### ⑥ 非 ASCII 命令让 loop brake 失效(HIGH)

历史标签用 `unicode_escape` 解码,把 UTF-8 变成乱码(`测试` → `æµè¯`),而 `_history_commands` 用的是正确的 JSON 解码 —— 两者不一致,模型看到乱码就无法匹配自己的历史,防循环失效,恰好命中 lark-cli 中文这个主力场景。修:统一走 `_json_unquote()`。

### ⑦ keep-alive 连接错位(MEDIUM)

404 路径不排空 body,残留字节被当成下一个请求行解析 → 客户端探测 `/v1/chat/completions`(OpenAI provider 常规能力探测)后,**下一个真实请求被破坏**。另外 `int(Content-Length)` 对 `abc` 抛异常,而超大声明值会把线程卡在 `rfile.read` —— 无鉴权端点上的线程耗尽向量。修:`_drain_body()` + `_content_length()` 校验(上限 32MB)。

### ⑧ salvage 从散文里写文件(MEDIUM)

回退正则匹配任何带 `content`+`name` 的 JSON,于是"这是一份示例清单:`{"name":"demo","content":"hello"}`"这种解释性回答变成 create_file,而文件名是从**用户提问**里推断的 → 一段关于 package.json 的解释可能把真的 package.json 覆盖成 5 字节示例。修:只认显式 `text_document_type` 标记。

### 顺带

- 上游超时从固定 240s 改为**跟随调用方 deadline**(执行器是非 daemon 线程,超长 urlopen 会滞留线程+socket,拖慢 pod 关闭)。
- prompt dump 从"无条件写死路径"改为仅 DEBUG + `O_EXCL` 0600(原来在共享的 188 上会持久化他人 prompt,且可被 symlink 攻击)。
- snake_case 参数(`file_path`)不再静默丢工具调用;无 path 时拒绝而非写入名为 `UNKNOWN` 的文件;grep 尊重 `includePattern` 不再全树递归。

> 审查确认**不是** bug 的:spendlog 并发写(200/200 无损坏)、happy-path SSE 事件序列、`new_rid()` 唯一性。

---

## 3.9 竞品对标 sub2api:采纳了什么(2026-07-25)

读了 sub2api 的实际源码(`backend/internal/service/gateway_scheduling.go`,2499 行,Go+PG+Redis),不是 README 营销话术。**关键结论:它的选号策略和我这轮重写殊途同归,验证了方向正确**;但有一条我们缺,已补。

### 它的选号流水线(已核实源码)

```
listSchedulableAccounts        # 先按"可调度"过滤(平台/配额/RPM/窗口费用)
  → filterByMinPriority        # 优先级
  → filterByMinLoadRate        # 最低负载率
  → filterBySoonestReset       # use-it-or-lose-it:窗口最早重置的先用
  → selectByLRU                # 最久未用 + 同组内随机打散
```

**和我们同构的地方**:健康/配额是**过滤器**(`isAccountSchedulableForQuota` / `ForRPM` / `ForWindowCost` 全是 bool 门禁),真正决定选谁的是 **LRU + 随机**,不是"挑分数最高的"。这正是我 §3.8 ③ 踩坑后改成的形态——先排除坏的,再公平轮转。它连"同分随机打散"(`shuffleWithinSortGroups`、`selectByLRU` 第 5 步 `mathrand.Intn`)都有,和我们 tier 内 RR 一个道理。

### 采纳:429 冷却(我们原本没有)

核实我们 bridge **对 429 完全无特殊处理**——429(限流)和 502(瞬时故障)同等对待,都只扣分继续用,导致被限流的 pod 留在轮转里持续烧重试。sub2api 用 `rate_limited_until` 之类的字段把账号**按时间**挡在调度外。

已补 `cool_down()`:
- **429/503** → 冷却 60s(有 `Retry-After` 就听它,夹在 1-600s)
- **502 等硬失败** → 冷却 10s(短,只为把**并发**请求从刚失败的 pod 引开)

实测(9 pod / 6 坏 / 8 并发):**8/8 成功**,坏 pod 仅被撞 7 次。集群复验:502 从 8 次 → **2 次**,8/8 成功、零最终失败、负载散在 7 个 pod。

### 明确不采纳(不适用)

| sub2api 机制 | 为何不搬 |
|---|---|
| 每账号并发槽位 + 等待队列(`AcquireAccountSlot` / `AccountWaitPlan`) | 需要 Redis。我们 fanout=1 + 冷却已够;Codex 是交互式,排队不如换 pod |
| 会话粘性(`GetSessionAccountID` + `sessionHash`) | 我们每轮都带完整 history 重放,无服务端会话态可粘;绑定反而放大单点故障 |
| 会话数限制(`checkAndRegisterSession`) | 针对 Anthropic OAuth 的多会话限制,网页会话池无此约束 |
| Postgres + Redis + Vue 后台 | 落盘已复用 litellm SpendLogs,不重造计费/后台 |

> 未能核实:`codexToolNameMapping`(仓库里搜不到该标识符,可能是旧版或我记错了)。所以 **exec 协议翻译这块没有现成方案可抄**,我们的 `custom_tool_call` 实现是自研的。

---

## 3.10 EWMA 健康度替代整数计分(2026-07-25)

采纳 sub2api 的 `openAIAccountRuntimeStats`(已核实其源码,alpha=0.2)。原来是手搓的 `[-3,3]` 整数分 + `+1/-1/-0.34` 步进 + 单独的时间 decay 函数;现在是一个指数加权移动平均:

```python
rate = ERR_ALPHA * sample + (1 - ERR_ALPHA) * rate   # sample: 1.0 失败, 0.0 成功
```

**为什么更好**(不只是"跟竞品一致"):

| | 整数计分 | EWMA |
|---|---|---|
| 衰减 | 需要单独写 `_tier()` 按闲置时间往 0 爬 | **自带**,恢复的 pod 自己降下来,decay 代码直接删掉 |
| 区分偶发 vs 持续 | 做不到,都是 -1 | 偶发 1/20 失败 → **0.009**;持续失败 → **0.988** |
| 权重调参 | 要手工调 miss 该扣 0.34 还是 0.5 | 只有一个 alpha,语义清晰(0.0 完美 .. 1.0 全挂) |

**miss 不再计入健康**:pod 回纯文本是聊天场景的**正确**回答(见 GUIDE),之前把它算失败才导致几轮闲聊就把全池打到底。现在只有硬错误进 EWMA。

阈值 `ERR_BAD=0.6`("最近多数请求都在失败")故意设高,确保只答文本的 pod 永远不会被排除。

**实测**:20 轮纯聊天后全池 err=0.0(miss 无信号);400 请求 85% 命中 → 104/89/103/104 均分零starve;持续坏的 pod 被排除,连续成功 12 次后 err 降到 0.066 自动回到轮转。集群上线后 19/19 可用、8 并发 8/8。

### 顺带:可观测性(GET /)

之前判断"某个 pod 是否被跳过、为什么"只能 grep 日志——排查负载倾斜和 502 突发时吃了大亏。现在 `GET /` 直接给出每个 pod 的 `error_rate` / `cooling_for_s` / `usable`,加上 `tool_arg_fixes` 计数(哪些参数拼写兼容分支真的被触发过,借鉴 sub2api 的 `ToolCorrectionStats`)。

---

## 3.11 真流式:上游 tools 与流式互斥(2026-07-25)

**现象**:回答"嗖一下全出来",不是逐字打出。

**根因**:`_call_one` 写死 `"stream": False` —— bridge 等整段回答生成完,再切成一个 delta 甩给 Codex。

**但真正的约束在上游**。同一 prompt、同一 pod、重复实测:

| 上游请求 | `output_text.delta` 事件数 |
|---|---|
| `tools=[...]` | **0**(文本只在终止事件的 output 里) |
| `tools=[]` | **10-13**(跨约 3s) |

pod 的 **tool-injection 路径根本不发增量文本**。带 tools 请求 `stream=true` 只会更糟:既没有 delta,回复还更难解析。

**方案**:按轮次类型分流。
- **聊天轮**(客户端没给 tools 也没给 `exec`)→ 上游不带 tools + `stream=true` → **真流式**
- **agent 轮**(有 tools 或 exec)→ 保持带 tools 非流式,协议正确性优先

判据 `wants_tools = bool(req.get("tools")) or _req_uses_exec_tool(req)`。

**实现要点**:
- `_FirstSpeaker` — fanout>1 时多个 pod 同时回答,全转发会把两份不同回复**交错成乱码**。第一个出文本的 pod 独占通道,其余静音。
- `_live_text_sink` — 首个 chunk 时惰性打开 message item,之后逐块发 `output_text.delta`。
- **防重复投递** — 已流式的文本必须从最终 `items` 里剔除,否则同一段回答发两遍;同时 `response.completed` 仍要携带完整文本(有的客户端读终止对象而不累积 delta)。

**实测**:聊天轮本机 13 块 / 集群 12 块,`item.done` 与 `completed` 各 1 个、**文本份数 1(无重复)**;agent 轮仍正确出 `custom_tool_call`。集群 8 并发 8/8。

---

## 3.12 无感接入:透明度审计(2026-07-25)

目标是把先锋流量转到 bridge 而**不让他们改任何配置**。前提是 bridge 对纯聊天必须完全透明。逐项核实后修了三处,也确认了一条改不了的约束。

### ① GUIDE 无条件注入(已修)—— 这才是"一股脑调工具"的真凶

之前 GUIDE(那段"你有 shell,必须调工具")是**每个请求都发**,包括纯聊天。所以问个普通问题它也想去跑命令。

**根因是我的实现顺序反了**:`wants_tools` 在第 1225 行算出,而注入 GUIDE 的 `build_messages` 在 1219 行**更早执行** —— 判断结果算出来时提示词早发出去了。不是做不到动态,是代码顺序错。

修:判断提前,`build_messages(..., with_tools=)` 按需注入。**原生客户端的做法本就是"提示词跟着工具走"**,我们反而做成了永远发。

实测:问"杭州有什么好玩的" → `tools=False`,正常回答景点;说"运行 echo x" → `tools=True`,正确调工具。日志直接可见判定结果。

### ② model 被静默改写(已修)

`_call_one` 里上游 model 写死 `UP_MODEL`。先锋用 `gpt-5.6-sol` 时,**上游实际收到 terra,但回复仍回显 sol** —— 隐形替换模型,而且是零改动接入的硬阻塞。

已核实上游尊重该字段(分别请求 sol/luna/terra,各自回显自己),改为透传客户端 model,客户端未给才回落 `UP_MODEL`。

### ③ usage 恒为 0(已修)

上游不报 token,我们就返回硬编码 0 —— 对任何展示用量的客户端/看板来说等于"这次调用免费"。改为复用落盘那套估算值,并标 `"estimated": true` 以免被误当计量真值。

### ④ `instructions` 被静默丢弃(已修)

`build_messages(instructions, ...)` **收了这个参数却从头到尾没用过**。`instructions` 是 Responses API 的系统提示字段,所以任何设了人设/硬规则的调用方,规则被无声无视。

实测(修复前):要求 "Answer ONLY in English. Prefix every sentence with [BOT]." → 回复纯中文、无前缀。

修:转成 `role=system` 消息,**放在 GUIDE 之后**(冲突时用户意图优先),且 tool-less 轮同样转发。已核实上游确实尊重 system role(100% 英文 + `[BOT]` 前缀)。

> 注:修复后首测仍失败一次,复测两次全对 —— 上游**账号间有遵从度差异**,个别 pod 会忽略 system。属上游变异,非代码问题。

### 改不了的约束:上游忽略采样参数

实测同一 prompt:

| `max_output_tokens` | 实际返回 |
|---|---|
| 5 | **2762 字** |
| 500 | 3103 字 |

`temperature` / `top_p` / `max_output_tokens` 上游**接受但完全忽略**,这是网页会话重放的固有限制,转发也没用。

**但这不是 bridge 引入的回归** —— 先锋现在直连 zero-N 同样失效。所以不阻塞无感接入,只需写明。

### 另一条已知不支持:`previous_response_id`

bridge 是**无状态**的,不保存会话。客户端若靠 `previous_response_id` 让服务端记历史,上下文会静默丢失(实测:第1轮说"记住42",第2轮续问答"没看到那个数字")。

**对 Codex 无影响** —— 日志显示它每轮重发完整历史(items 数 2→6→9→10 递增),不依赖服务端状态。仅当接入其他依赖该字段的客户端时才需处理。

### 第三条硬约束:上游只认自己注入的工具(尝试后撤销)

读 sub2api 的 `apicompat/responses_client_tools.go` 学到一个好设计:`AdaptResponsesClientTools` 把客户端工具**可逆降级**成上游能懂的形式,再用 `ResponsesClientToolStreamRestorer` 把响应还原回客户端期望的形状 —— 客户端没给 tools 就完全不介入(`len(tools)==0 → return false`)。

照此发现我们一个缺陷:bridge 的 `tools` 是**写死的 3 个内置工具**,客户端声明的 `tools` 只用于"判断要不要工具"和"回显",从不真正转发。客户端声明 `get_weather` 时,模型压根不知道它存在。

我实现了转发 + 调用穿透(客户端工具的调用直接返回 `function_call`,不再被 `tool_to_cmd` 挤成 shell 命令 —— 实测那会把 `get_internal_ticket_status` 变成 `exec_command{"cmd":"get_internal_ticket_status T-9931"}`,工具名当命令跑、参数丢失)。

**但撤销了**,因为直连上游验证:

| 请求 | 结果 |
|---|---|
| 带任意客户端工具,`tool_choice=auto` | 回文本,不调用 |
| 同上,`tool_choice=required` | **仍回文本** |

上游的 tool-injection **只认它自己预设的那几个工具**(run_in_terminal 等),任意客户端工具无法触发。转发在上游层面无效,留着只是一个"假装能用"的功能,所以整块撤掉(1613 → 1560 行)。

> 含义:bridge 只能提供**自己那套内置工具能力**,不能作为通用 tool-calling 代理。接入自带工具的客户端时,这是硬边界。

### 参考 sub2api 又采纳的两项(2026-07-25)

**① SSE `sequence_number`(已加)**

sub2api 的 `anthropic_to_responses_response.go` 每个事件都递增 `state.SequenceNumber`,真实 OpenAI 也如此。我们**一个都不发**(上游 zero pod 也不发)。Codex 目前不介意,但更严格的客户端有权拒收或乱序处理。

已加,**按请求重置**(不是按连接):一个 handler 实例会顺序服务多个 keep-alive 请求,不重置会让第二个响应从中途序号开始。实测单请求 0-17 连续,同连接第二个请求也从 0 起。

**② 失败必须报成失败(已改)**

`_fail()` 原来返回 `status: "completed"` + 错误文本**当作助手的正常回答**,HTTP 还是 200。客户端无法区分真回答和"所有上游都挂了",错误被洗成了成功响应,还会被存进对话历史。

改为 `HTTP 502` + `status: "failed"` + `error` + `incomplete_details.reason`,`output` 留空。这正是 sub2api 用 `status` / `IncompleteDetails` 表达的区分。

> 我之前把这条列为"不敢改",怕 5xx 触发客户端重试风暴。**实测验证了才改**:拿全坏上游让 Codex 打,bridge 只收到 **1 次**请求,无重试风暴。风险不存在。

### 计费回归:集群侧一直在丢账(2026-07-26 修复)

**① pod `/tmp` 是临时的 → 每次 rollout 丢账**

集群 bridge 把 spendlog 写在 pod 的 `/tmp`,而今天它被 rollout 了十几次 —— **每次重启,上次 flush 之后的账全部消失**。实测发现 pod 内 0 行,而游标是 10。

修:deployment 加 hostPath 卷 `/Data/zerokey-bridge-spend`(和 zero-N pod 用 `/Data/zerokey-sessions` 同一模式),`BRIDGE_SPENDLOG` 指向 `/spend/zk_spendlog.jsonl`;flush 改为直接读宿主机路径,不再 `kubectl exec cat` pod 内文件。

**② 空日志时游标不重置 → 重启后前 N 笔被跳过**

`flush_cluster()` 里 `[ -z "$rows" ] && return 0` 在 rewind 检测**之前**,所以 pod 重启后日志为空时直接返回,游标永远停在旧值。下一批新账的前 10 笔会被当成"已 flush"跳过。

修:空日志明确视为"pod 重启",重置游标为 0。

### 两次我差点误判的地方(都靠查数据拦住)

**`spend` 字段全是 0 —— 不是我们的 bug**

`VerificationToken.spend` 对我们的 key 显示 0,我一度判断"账没累加到额度、预算形同虚设"。查全表才发现:**1485 个 key 里 1426 个都是 0** —— litellm 本身靠 SpendLogs 流水统计,不靠这个字段。属系统固有行为。

**1497 / 8203 的巨额消费不是 zerokey 的**

按 key 汇总时看到 8203 远超其 1500 预算,差点当成我们记账虚高。分 provider 拆开后:**zerokey-web 只贡献 0.25 / 0.29**,其余全是 codex 等其他模型的历史消费。我们单笔最大 0.03、平均 0.0016,合理。

### 计费链路现状(全绿)

| 检查项 | 结果 |
|---|---|
| 本机落盘 / 归属率 | 275 行 / **100%** |
| 游标同步 | 275/275 |
| 集群持久落盘 | ✅ 写入 hostPath |
| DB 记录 | 412 条,145 key,时间实时 |
| **重复记账** | **0**(request_id 去重生效) |
| 端到端归属 | 新 key → 落盘 → 入库 → 归属正确 ✅ |
| 真实用户 join | `cursor-guran-v2sb` 等能正确对上账户 ✅ |

> 空 alias 的 293 行是我的测试假 key(不存在于 VerificationToken),非真实用户丢失归属。

### 已验证无问题的项

- **线程泄漏**:4 个请求前后线程数 3→3,无增长(deadline 绑定超时生效)
- **keep-alive 状态污染**:同一连接先流式聊天再 agent 轮,第二个请求正确出 `custom_tool_call`,未被前一个的 `self._live` 污染
- **纯字符串 input**(非 Codex 客户端形状)正常处理

---

## 3.13 多轮工具循环修复(2026-07-27):真正的根因是 21 个 pod 漏了补丁

**现象:** 用户报"lark-cli 很多工具跑不通",实测模型只跑一条命令(如 `lark-cli --help`)
就停下来写"下一步需要...",多步任务全灭。基线实测 **0/6**。

**最大根因(此前从未查到):** 47 个 pod 里 **21 个的启动命令漏了三行 `cp`**:

```sh
cp /patch/web-tools.js /app/routes/web-tools.js
cp /patch/raw.js       /app/routes/raw.js
cp /patch/responses.js /app/routes/responses.js
```

补丁文件本来就在共享 CM `zk-image-patch` 里,只是这 21 个 deploy 从没复制过。
于是它们跑的是镜像里的旧 `responses.js`:

```js
if (tools.length > 0) {
  if (!hasTokens()) return res.status(503)   // ← 直接拒绝工具请求
  return handleCodex(...)
}
```

而打了补丁的 pod 是:

```js
if (tools.length > 0 && hasTokens()) return handleCodex(...)
// 否则 → useWebTools 网页注入兜底
```

**结论:** 那 21 个 pod 不是"坏了/没配额",是漏补丁 → 对带 `tools[]` 的请求一律 503
(`no Codex tokens available for tool_call`)。bridge 把 47 个 pod 当等价,
**约 45% 的挑选注定失败,每次还消耗一次重试** —— 这才是拒答的主因。

**重要:两类 pod 的 `CODEX_TOKEN_DIR` 都是空的**,`hasTokens()` 恒 false,
所以工具流量 **100% 走网页注入,不烧任何 codex 配额**(已逐 pod 核实)。
排查中途我曾误判"在烧 codex 配额" —— 那是只看了未打补丁 pod 的代码就下结论,已纠正。

**修法:** 给 21 个 deploy 的启动命令补上那三行(插在 `exec` 之前,保留 zero-81 原有的
`users.json` 等待)。脚本 `/tmp/patch_one.py`,先单 pod canary 再批量。
修完全池复查 **46/46 CALL,0 NOTOKEN**。

### 同批修复(bridge 侧)

| 问题 | 修法 | 证据 |
|---|---|---|
| GUIDE 说"拿到结果就给最终答案" | 改为显式要求继续;history 末尾是 tool 结果时追加 **continuation nudge**(重述原始目标+禁止叙述计划),放最后一条消息 | 这句是多轮断掉的直接原因 |
| 上游工具名 `enqueue_job` | 改 `shell_enqueue_job`:名字含 shell → harvest 路由,描述保留 job-queue 框架(两者可解耦) | 三轮独立实测 28 样本:25% / 56% / 61% / **90%** |
| 拒答检测漏 7/14 且误报 | 要求否定指向**模型自身能力**(denial+self / handoff / narration),窗口 200→600 | 语料 17/17,留出 12/12,误报 0/20 |
| LOOP BRAKE 误杀成功结果 | 只对**失败过**的命令硬刹车;重复成功命令改为关掉工具重问一次,让它用已有输出作答 | 曾把已取到的飞书文档换成"I stopped after repeating..." |
| 503 风暴烧光重试预算 | 错误轮/拒答轮**拆两个预算** | 实测 1 秒内烧掉 8 轮,拒答一次没重试 |
| fanout=1 串行找可用 pod | 工具轮 fanout=3,聊天轮仍 1 | 单轮 11-20s,串行 3-6 轮就超时 |
| fanout 下好答案被后到拒答覆盖 | 保留本轮最优文本 | 单元测试覆盖 |
| `{"tool_calls":[]}` 以文本泄漏 | 打捞成真调用;空壳当无答案重试 | 实测出现过 |

### 最终验证

- 报的原任务(读飞书 docx):**10/10**,每次 3-5 条命令
- 跨任务:本地磁盘 4/4、git 分支 4/4、纯聊天 4/4(0 命令)、多步统计 4/4 → 合计 **26/26**
- 真实 Codex CLI(非测试脚本)读文档成功
- 计费:196 行/小时,`spend 6.8080/910` 正常累计
- codex 配额:全池 `CODEX_TOKEN_DIR` 空,**零消耗**

### 复现测试的工具(在 /tmp/zkloop)

- `agentloop.py` — 扮演 Codex 另一半:发请求 → 收 `custom_tool_call` → **在本机真执行** →
  回灌 history → 循环。之前所有测试都是单轮,这是第一次真正闭环。
  注意:`exec` 工具要放在 **`input[]` 里 type=`additional_tools` 的项**,
  放顶层字段 `_req_uses_exec_tool()` 认不出 → tools=False → 不注入 GUIDE → 秒拒答。
- `loop_trials.py` — N 个循环并发跑,分类 ANSWERED/BRAKED/REFUSED/NOCMD
- `corpus.py` + `detector_v2.py` — 拒答检测离线语料(真实样本),秒级迭代,不用打网络
- `toolname_ab.py` / `cwd_ab.py` — 工具名、环境框架 A/B
- `survey.py` — 全池 tool_call 能力普查(**并发压到 4-5**,15 路会把健康 pod 打成"不可用")

---

## 4. bridge 关键机制(实现要点)

- **exec 协议翻译**:codex 的 `exec` custom 工具(JS code_mode)↔ zerokey 网页注入。网页模型出 `run_in_terminal{command}` → bridge 包成 `custom_tool_call(exec, input=JS)` 返回。蓝图来自 acct 原生录制。
- **GUIDE 引导**(build_messages 注入):"有真实 shell,要跑命令必须调工具、绝不脑补输出;纯闲聊才回文本;做完就收尾"。
- **pod 健康追踪**:每 pod 滚动打分,命中+1/miss-1,`next_healthy_upstream` 优先健康 pod。
- **首个成功即返回**:并发但第一个出 tool_call/文本立即返回,不等整轮(24s→4s)。
- **重复命令刹车**:同命令历史已执行又要执行 → 停止调工具、返回文本(防网页模型不收尾的死循环)。
- **落盘**:每请求写 `/tmp/zk_spendlog.jsonl`,flush 器每 60s 批量灌 198 litellm SpendLogs。

---

## 5. 竞品调研:sub2api(避免闭门造车)

`Wei-Shaw/sub2api`(34k⭐)= 订阅版 AI 转 API 网关,架构印证并优化了本方案。

| 维度 | sub2api 做法 | 本方案对齐 |
|---|---|---|
| 服务化 | 单 Go 镜像 + postgres + redis,compose 一键 | bridge 打镜像上 198 ✅ 一致 |
| 多用户鉴权 | 平台发 key,请求带 key 鉴权 | **复用 litellm cursor-* key**,不另造 |
| 账号池 | 智能选择+粘性会话+并发控制+健康检查 | 已有 pod 健康追踪 ✅ |
| 计费落盘 | Token 级,postgres | 写 litellm SpendLogs ✅ |
| **codex app-server** | 专门检测 UA/版本/指纹,开关 `codex_cli_only_allow_app_server` 决定放行 | **借鉴**:识别 app-server 流量单独处理,而非一刀切关 multi_agent |
| 反代 | Nginx/Caddy,**须保留下划线 header**(`session_id` 粘性) | 暴露公网时注意 |

**关键收获:**
1. 鉴权别自己造 → 用户已有 litellm cursor key,bridge 从请求头拿。
2. codex app-server(multi_agent)→ sub2api 是"检测+可配置放行",一期先关求稳,二期做原生适配。
3. 粘性会话保留 `session_id` header(反代默认丢下划线 header)。
4. redis 做并发控制/会话状态(一期可不用,多用户高并发需要)。

---

## 6. 全员落地计划

### 一期(先让先锋能用,求稳)

- [ ] **bridge 镜像化**:写 Dockerfile,打 `her/zerokey-codex-bridge`(在 47.84.112.136 nerdctl 构建,禁本地 Mac)
- [ ] **198 Deployment + Service**:集群内 bridge **直连 zero-N svc DNS**(`zero-N.litellm-product.svc.cluster.local:8200`),**去掉 ssh 隧道**
- [x] **落盘归属**(2026-07-25 完成):bridge `caller_key_hash()` 从请求头 `Authorization` 取 Bearer → `sha256(明文key)` 写进 spendlog 行的 `api_key`;`mk_sql.py` 按行归属(无归属才回落 `BRIDGE_DEFAULT_KEYHASH`)。**算法已验证**:`sha256(明文key)` == `LiteLLM_VerificationToken.token`,拿 `cursor-liuguoxian-l08v` 对上 `70524ea8...`。集群实测:发 `Bearer sk-CLUSTER-FINAL-TEST` → 落盘 `api_key=3823b5dc...` 正确。
  - 仍待:**校验**(目前只归属不鉴权,任何 Bearer 都放行)。公网暴露前必须加 key 有效性校验,否则是开放代理。
- [ ] **公网入口**:复用 cc.auto-link.com.cn 网关暴露 `/zk/v1`,**注意保留 session_id header**
- [ ] **用户 config 下发**:每人 `~/.codex/config.toml` 把 provider 指向 bridge + `supports_websockets=false`(**不关 multi_agent、不改 model 名**)+ 共享 `AGENTS.md`(lark-cli 用法)。做一键脚本群发
- [ ] **灰度**:2-3 先锋试 → 全员

### 二期(优化)

- [ ] redis 并发控制 + 粘性会话
- [ ] pod 池扩容/自动补活 token

---

## 7. 本机运维现状(过渡期)

本机跑着 3 个守护(nohup,未开机自启):
- `~/.zerokey-bridge/_tunnel.sh` — ssh 隧道(→svc ClusterIP)
- `~/.zerokey-bridge/_bridge.sh` — bridge(fanout=1)
- `~/.zerokey-bridge/spendlog_flush.sh` — 落盘 flush(用 `mk_sql.py` 生成 SQL)

一键启动:`bash ~/.zerokey-bridge/start-all.sh`

**待办**:launchd 开机自启(集群化后本机守护即可废弃)。

### 用户 config 关键项(cursor-liuguoxian 现状)
```toml
# 方式:走 carher_dev provider(不改 model 名,先锋原 gpt-5.6-terra 照用)
[model_providers.carher_dev]
base_url = "http://127.0.0.1:8788/v1"   # 集群化后改 cc.auto-link.com.cn/zk/v1
env_key = "CARHER_DEV_KEY"
wire_api = "responses"
supports_websockets = false             # ← 关键:强制 HTTP → multi_agent 照开不降智
```
> 不再需要 `multi_agent=false`——那是早期回避方案(降智),已废弃。见 §3.5。
+ `~/.codex/AGENTS.md`:飞书 docx 链接直接用 `lark-cli api GET .../raw_content`,禁 curl/requests。

---

## 8. 关键坐标

### ⚠️ 部署必看:ConfigMap 的 key 必须是 `bridge.py`

Pod 跑的是 **`python3 /code/bridge.py`**,所以 ConfigMap `zerokey-bridge-code`
里**只有 `bridge.py` 这个 key 会生效**。

2026-07-27 踩过的坑:用
`create cm --from-file=zerokey-codex-responses-bridge.py=... --dry-run | apply`
部署,结果 CM 里多了一个同内容、不同名的 key。`bridge.py` 还是旧的,
**连续 6 次部署 + rollout 全部是空操作**,pod 里 `grep` 得到的新代码只是那个没人读的 key。
更坑的是期间指标确实变好了(1/6 → 3/6),很容易误判成"改对了"——实际是噪声。

正确姿势(key 名写死 `bridge.py`):

```bash
kubectl -n litellm-product create cm zerokey-bridge-code \
  --from-file=bridge.py=/tmp/bridge.py --dry-run=client -o yaml | kubectl apply -f -
kubectl -n litellm-product rollout restart deploy/zerokey-codex-bridge
```

部署后**必须验证进程读的就是新文件**,不要只 grep 文件:

```bash
kubectl -n litellm-product exec $POD -- sh -c 'tr "\0" " " < /proc/1/cmdline'   # 应为 python3 /code/bridge.py
kubectl -n litellm-product exec $POD -- grep -c <本次新增的标识> /code/bridge.py
```

另外 CM 挂载有传播延迟:`rollout status` 完成后进程可能仍以旧文件启动。
若新标识没生效,**再 restart 一次**(已验证第二次必成)。

- bridge 脚本:`scripts/chatgpt-onboard/zerokey-codex/bridge/zerokey-codex-responses-bridge.py`
  (本地文件名 ≠ CM key 名,部署时务必映射成 `bridge.py`)
- 落盘 SQL 生成:`~/.zerokey-bridge/mk_sql.py`
- litellm SpendLogs:provider=`zerokey-web`,归属 key=`cursor-liuguoxian-l08v`(hash `70524ea8...`)
- zero pod:198 litellm-product ns,`zero-N`(svc ClusterIP 稳定),打网页后端 `backend-api/f/conversation`
- 198 直连:`sshpass -p "$(python3 -c 'import sys;sys.path.insert(0,"scripts/lib");import carher_secrets as c;print(c.require("SUDO_PW"))')" ssh cltx@10.68.13.198`,kubectl 需 `sudo -S`
  - 密码**不写进仓库**。放在本地 gitignore 的 `.carher-secrets.json`,用
    `scripts/lib/carher-secrets-init.sh` 生成;脚本侧统一走
    `scripts/lib/carher_secrets.py`(Python)或 `scripts/lib/carher-secrets.sh`(shell)。
    环境变量 `SUDO_PW` 优先,便于 CI 和临时覆盖。
- sub2api 参考:`github.com/Wei-Shaw/sub2api`(Go+postgres+redis 单镜像)
