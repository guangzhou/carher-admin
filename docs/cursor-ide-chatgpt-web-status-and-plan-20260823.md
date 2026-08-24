# Cursor IDE 接入 ChatGPT 网页额度（bpi-web 线）：现状 + 目标 + 执行计划（活文档）

> 创建 2026-08-23，**最后更新 2026-08-24**。范围：**仅 bpi-web 这条线**（Cursor IDE 用网页版
> ChatGPT 订阅额度做 agentic 编码）。与 `docs/ws-incremental-transport-handoff-20260823.md`
> （acct 多账户 WS 增量）是**两条不同的线**，别混。
>
> 本文是这条线的**唯一活文档（canonical）**，以后对着它更新。历史快照式表述保留在 §附录。

---

## 0. 一句话现状（2026-08-24）

链路**健康且已扩容**：原 acct101 单线之外，本会话新建并实测跑通**第二条线 acct-82**
（`zero-cursor-bpi-82` + 三档模型 + 授权 key04）。**"走网页订阅额度"已用三段式 + 活体暗号实测
坐实**（非 codex API 面、非 per-token）。**codex 真源码推出的 4 条搬用作业已对着 198 实拉的 live
代码做**静态审计**——判决全部"已实现/架构不适用"，bpi 线无需改代码；其中两条"已实现"（作业1/4）
是数据流层面的定论，**尚差一次 live 计数坐实**（§4.0 + §5）**。目标取向已由用户
行为定调：**成本优先 / 吃网页额度 / 接受工具调用 best-effort，正在扩池**。**多条线组池已落地
并验收**（§4.1：双线加权池 + WA key 级黏性实证 + **fail-over 演练闭环**：死线黑洞 ≈120s → fail-mark
→ 自动甩健康线）；剩余前向项 = 跨线压缩 skill（§4.2，另一 codebase）。

> **命名换装已上线（2026-08-24）**：用户面菜单改 `cursor-g-*`（不暴露 "web"/账号拓扑），
> 6 名真身 slug 池（sol/sol-high/luna/pro/instant/5.5，**xhigh 退役=第三个虚构档**），
> 旧 9 名 `cursor-web-fc*` 冻结当调试工具。命名换装这一件事的唯一活文档 =
> `docs/cursor-g-naming-rollout-20260824.md`（本文只管上游链路，见 §1.3 摘要）。

---

## 1. 进展（按时间，均线上实测）

### 1.1 服务端链路（08-23 实测，acct101 线，仍有效）

| 项 | 值 | 判读 |
|---|---|---|
| bpi pod | `zero-cursor-bpi-*` Running，0 重启，node `aiyjy-litellm-standby` | 健康 |
| 镜像 | `docker.io/library/zerokey-codex:latest` | — |
| env | `PORT=8201` `ZK_USER=acct101` `ZK_DEFAULT_MODEL=gpt-5-5` `CODEX_TOKEN_DIR=`(空) `ZK_INLINE_MAX=125000` | `CODEX_TOKEN_DIR` 空 = 落网页注入（走订阅额度）的关键杠杆 |
| r14–r18 开关 | env 未显式钉 → 全取代码默认（全部启用） | 功能在线 |

**流量（SpendLogs）**：acct101 线收敛到干净的 `openai/gpt-5.6-terra`；`model` 列保留了配置
演进史（早期错指 `cursor-gpt-5.6-terra` → 试 `chat_completions` 失败 → 收敛干净态）。

### 1.2 本会话新增（08-24）

**(a) 坐实走网页订阅额度**（三段式 + 活体暗号）：
- 假设：bpi-web 线消费的是网页订阅额度面（按消息/速率），不是 codex API 面、也不是 per-token。
- 证伪条件：若走 per-token，SpendLogs 该有正常 token 计费；若走 codex API，端点应是
  `/backend-api/codex/responses`。
- 数据：端点是 `/backend-api/f/conversation`（网页面）；SpendLogs spend 近零（符合额度面，不按
  token）；活体暗号在真流量里回显 → **假设成立**。

**(b) 克隆出第二条线 acct-82**（一键脚本 `scripts/zk-cursor-web/clone_web_fc_lane.py`）：
- deploy/svc `zero-cursor-bpi-82`（照 bpi 模板，只换 name/`ZK_USER=acct82`/seed 路径）。
- 三档模型 `cursor-web-fc-82-terra{,-high,-max}`，授权给 key `cursor-liuguoxian04-5rub`（41→44）。
- 账号 franco_dictask@mail.com（Pro）。与 acct-82 的 WS 增量线**共用同一号，各走各的额度**（用户确认）。
- **登录态借用**：不手抓 web session——直接把 `chatgpt-acct-82` WS 线 PVC
  `/chatgpt-auth/auth.json` 里那份会自动续的 codex OAuth `access_token` 灌进 web seed 的
  `parsedFetch.authorization`，web 端点 `/backend-api/f/conversation` 接受它（live 实证）。

**(c) 修掉 "hi 无回复" 假绿**：openai/ 模型漏 api_key 占位符 → 真流量 401；master key 测是假绿；
`/model/info` 对 api_key 脱敏误导对比。补 `api_key:"sk-zerokey-web-noop"` 后经**临时真 key
路径**复现验收通过。详见 §3 假绿三连。

### 1.3 命名换装 cursor-g-*（08-24，摘要;全程见 `docs/cursor-g-naming-rollout-20260824.md`）

用户面菜单从内部代号 `cursor-web-fc*` 换成 `cursor-g-*`（见名知意、不暴露 "web" 实现、池名无
数字不暴露账号拓扑）。**名实相符**:菜单背后全是真身 slug（2026-08-24 model_slug 铁证），
不再延续虚构 dot-slug。

- **上线 6 池别名**（无数字,WA 跨线负载+容灾）：`cursor-g-5.6-sol`(`gpt-5-6`)、
  `cursor-g-5.6-sol-high`(`gpt-5-6`+eff=high→web extended)、`cursor-g-5.6-luna`(`gpt-5-6-t-mini`)、
  `cursor-g-5.6-pro`(`gpt-5-6-pro`)、`cursor-g-5.6-instant`(`gpt-5-6-instant`)、
  `cursor-g-5.5`(`gpt-5-5-thinking`)。每名各挂 101+82 两 deployment（id
  `zerokey-cursor-g-{101,82}-<变体>`），已授权 key03/key04。
- **xhigh 退役——第三个虚构档（三段式坐实）**：`xhigh` 经 lane `_CARHER_TE_MAP`→web
  `thinking_effort=max`,对真身 `gpt-5-6` **确定性空 completion**（status=completed /
  output_tokens=0 / reasoning_tokens=0,trivial 与实质 prompt 均空,跨 4 次一致）。旧名 `-max`
  能"出字"仅因走**虚构 slug** `openai/gpt-5.6-terra` 触发上游 fallback、max 被丢弃(实为默认档)。
  已删两 deployment、从 key03/04 摘除。**上线档止于 sol(standard)/sol-high(extended)。**
- **旧 9 名 `cursor-web-fc*` 全部冻结**（单线 6 + 旧池 3),零删零改,当调试/定位工具。
- **hook gate 扩成元组**:`cursor_web_fc_sys_rewrite.py` 的 `_TARGET_PREFIX =
  ("cursor-web-fc-", "cursor-g-")`（str.startswith 原生吃元组）——**唯一改动的共享面**,双 gate +
  fail-open,旧名行为零变化(Step 1 验收②③坐实)。
- **可扩容**:v2 克隆脚本 `scripts/zk-cursor-web/clone_web_fc_lane_v2.py`,新号一键出线(6 直连名
  `cursor-g-<N>-*` + 6 池成员入别名 + 授权 + 自动验收);用户面名字永不变。
- **计费**:新名与旧池同一 responses bridge 路径,走网页订阅额度面,spend≈0 为预期形态,非异常。

---

## 2. 目标（已由用户行为定调，不再是推测）

在 **Cursor IDE** 里把模型指向"**网页版 ChatGPT 订阅额度面**"跑 agentic 编码——**包括真正动手的
工具调用**（改文件、跑命令），计费走网页订阅额度（Plus/Pro 按消息/速率，已付费），**不烧 Cursor
per-token API、不烧 codex API 份额**。

链路：Cursor `/v1/responses` → 198 LiteLLM（hook `cursor_web_fc_sys_rewrite`）→ pod
`zero-cursor-bpi[-N]:8201`（`responses.js`）→ 网页 ChatGPT 有状态会话。

两条硬约束：
- **选网页的唯一理由是"额度面"**——按消息不按 token，省的是额度不是 token。
- **网页面工具调用是 best-effort**（无原生 FC，靠 r8–r18 合成/收割）；100% 可靠只有 codex 原生
  FC（`cursor-fc-*`），但那条烧 codex 份额，**用户未选它当主路径**。

**取向定调依据（行为证据）**：本会话用户选择**扩 web 线**（建 acct-82、要三个档位、共用账号各
走各额度），而非转 cursor-fc。→ 成本优先、接受 best-effort、正在扩池。

---

## 2.5 链路隔离边界（改前必读，2026-08-24 实测）

**核心问题："优化 cursor-web 会不会影响别的链路？"** 按解密 api_base（proxy pod 内 /model/info）
实测的路由图——四组 cursor 模型落在**不同 pod + 不同 CM**，本线与其他线物理隔离：

| pod / svc | CM（代码宿主）| 路由到这的模型 | 本线？ |
|---|---|---|---|
| **zero-cursor-bpi:8201** | **zk-cursor-bpi-patch**（responses.js）| `cursor-web-fc-terra{,-high,-max}` | ✅ acct101 |
| **zero-cursor-bpi-82:8201** | **zk-cursor-bpi-patch** | `cursor-web-fc-82-terra{,-high,-max}` | ✅ acct82 |
| zero-cursor-101:8200 | zk-cursor-**protocol**-patch | `cursor-fc-*`(原生FC/codex额度)+`cursor-gpt-*`(共享web) | ❌ 别的线 |
| cursor-agents-shim:8901 | —— | cursor-composer/grok/kimi/opus | ❌ 别的线 |

隔离结论：
1. **改 `responses.js`（CM zk-cursor-bpi-patch）只影响本线**。该 CM **只被 zero-cursor-bpi +
   zero-cursor-bpi-82 挂载**（实测 `grep MOUNTS`），且**只有 6 个 cursor-web-fc-* 模型**路由到这两
   pod。cursor-fc/cursor-gpt（在 zero-cursor-101，另一个 CM）、acct 池、zero-* 池、其他 cursor 模型
   全都碰不到。爆炸半径 = 这两条 web 线。
2. **唯一共享面 = hook `cursor_web_fc_sys_rewrite.py`**（在**共享 CM `litellm-callbacks`**，全 proxy
   加载）。但代码里**双重 gate**：`model.startswith("cursor-web-fc-")` 且有非空 tools 才改，其余模型
   逐字节原样返回，异常 fail-open。**这是将来动手唯一要格外小心的地方**——改它必须保住 gate +
   fail-open，并回归验证非 cursor-web-fc 模型不受影响。
3. **组池会碰共享基础设施**：LiteLLM router / `weighted_affinity`（对所有池生效）。所以"组池"不是
   "只碰本线"的改动 → 需单独设计 + 审批（见 §4.1）。

---

## 3. 验收纪律与已知陷阱（踩过的真坑，必守）

**假绿三连**（本会话真踩，克隆/建模型时逐条排掉）：
1. **openai/ 模型漏 api_key 占位符 → 真流量 401、master key 测假绿**。占位符
   `sk-zerokey-web-noop` 即可（登录态在 seed 里，bpi 不校验 api_key）。这是 "hi 无回复" 根因。
2. **`/model/info` 对 api_key 脱敏**，两条线都显示 `has api_key: False`。配置对比**只能读 DB raw
   `litellm_params`**。
3. **验收必须走临时真 key**（`/key/generate` scoped key → `/v1/responses` 带暗号 → 看
   200+暗号回显+grep pod 日志 → `/key/delete`），master key 绕过 per-key gate 必假绿。

**其他红线**：
- **web seed 的 JWT `exp` 在未来 ≠ 上游还认它**；唯一判据是真去 sentinel 握手。失效了别手抓，
  借 WS 线的活 OAuth token。
- "前门全绿 ≠ Cursor GUI 能过"、"200 ≠ 生效"——ground truth 是 Cursor GUI 里点一次真实工具调用。
- CM `zk-cursor-bpi-patch` 有 12 个 key，改代码只能 `kubectl patch --type merge`，禁 `create
  --from-file`（会抹掉其余 11 个）。

---

## 4. 执行计划

### 4.0 codex 搬用作业单 —— 已逐条**静态审计**，判决入档（2026-08-24）

精读 OpenAI codex 真源码（`~/codes/codex` commit 343074d，锚点地图见记忆
`reference_codex_source_harness_anchors_2026_08_24`）后，把 4 条"可搬用改进"对着 **198 实拉的
live 代码**（hook `cursor_web_fc_sys_rewrite.py` + CM `zk-cursor-bpi-patch` 的 `responses.js`）逐条
三段式**静态审计**（读代码、比数据流，**非**跑运行时日志/计数器）。**结论：4 条全部"已实现 /
架构不适用"，bpi-web 线无需因 codex 学习改任何代码。** 证据强度分两档：作业2/3 是架构层定论
（文本管线无结构化配对、delta==done），可直接信；作业1 的"无累积"**已于 2026-08-24 经 live
闭环坐实**（组池回归时 `[conv] delta send 1 items, 96 chars`——增量轮只发工具结果，注入块
500+ 字符零累积）；作业4 的"已实现"仍是数据流推断（usage 数字未逐轮核对）。
（纪律注：作业单原是从 codex 机制"说得通"推出的假设；静态审计把每条补丁的前提都证伪/证实了，
盲打反而会引入 strip 正则误伤等风险。CLAUDE.md：**代码存在 ≠ 该路径被执行**——所以本表是
静态判决，不是运行时结论。）

| 作业 | 假设 | 数据（file:line，198 实拉） | 判决 |
|---|---|---|---|
| 1 hook 打标记回收 | 无标记注入→conv/delta 累积 | ①hook 有 `_already_done` 哨兵幂等守卫，哨兵=`[EXECUTION ENVIRONMENT]`；②`responses.js:371-372` delta 轮 `flattenInput(...,null)` **压根不发 instructions**，网页会话服务端有状态、注入只在首条；③改写单向不回 Cursor，客户端永远发干净 instructions；④`responses.js:407` `_chatOnlyize` 已用 `/\[EXECUTION ENVIRONMENT\][\s\S]*?/` **识别+剥除**——codex 的"标记+下游回收"已实现 | **证伪 + 已实现**（live 已坐实：delta 轮 96 chars 零累积）|
| 2 发送前 normalize 配对 | 孤儿 call→上游 400 | `responses.js:95-101` `flattenInput` 把 function_call/output 全转文本行发网页 `/f/conversation`；上游收**扁平文本非结构化 items**，无配对校验、无 400。codex `normalize_history` 针对 /responses 原生结构化管线，本线是文本管线 | **架构 N/A** |
| 3 arg 只认 done item | 自拼上游 arg delta | 上游网页无原生 FC，无 delta 可拼；`extractToolCalls` 合成完整 FC，`responses.js:1400-1415` 出站给 Cursor 的 `delta==done`（同一份完整 args，纯 UI）| **已满足 codex 口径** |
| 4 usage 口径 | 需改服务端末轮+本地估 | `responses.js:936-957` r9 已报**客户端真实上下文**（`realIn=measureItems(input)+instructions`，取 `max(estimateTokens(prompt), realIn/4)`），注释已引用 codex 同族教训；web 面无 per-token 权威数（measureItems 恒 null）**无"服务端末轮"可锚** | **核心已实现，精化无数据源**（静态·待 live 计数）|

诚实边界（复述）：codex 消费 `/backend-api/codex/responses`（codex 份额面、结构化 /responses），
本线消费 `/backend-api/f/conversation`（网页订阅面）再把文本转译成 responses 事件发给 Cursor——
**能借的是上下文管理思路，不是端点/事件形状**。上表正是这条边界的体现：思路早已内化，形状不搬。

### 4.1 前向主线：多条线组池 —— **已落地并验收通过（2026-08-24）**

**侦察结果（198 live，三项全查实）：**

1. **"acquireSlot 串行"的说法不准确**——`utils/rate-limiter.js`（pod 镜像内）是**滑动窗限速器**：
   每 pod 进程、每 label（写死 `'ChatGPT'`）**15s 窗口放行 5 个新 turn**，无 release、非互斥锁。
   瓶颈真实形态 = 单 pod 突发 >5 turn/15s 时排队（最长等 15s）。两条线天然 10/15s 聚合。
2. **weighted_affinity 钩子是通用的，组池零代码改动**——挂在 `async_filter_deployments`，对**任何**
   多 deployment 的 model group 自动生效：权重读 `litellm_params.weight`（缺省 1）加权首选 +
   会话黏性 + fail-mark 摘坏节点（Redis 共享、滑动 TTL）。session 指纹来自 `prompt_cache_key` 或
   `x-litellm-session-id` 头——**Cursor 两者都不发** → 自动退回 **key 级亲和**（同一用户 key 钉同一
   条线，直到 fail-mark/TTL）。`previous_response_id`/tag_regex 在场时让路（我们不发，无关）。
3. **现状注册形态**：6 个模型名、每线 3 档、api_base 各指各的 svc、weight 全 None、
   `model_info.id` 已各自唯一（`zerokey-cursor-web-fc-{101,82}-terra*`）。

**设计（纯增量，不动现有 6 个模型名、不改任何共享代码）：**

- 新注册 3 个**共享别名** `cursor-web-fc-pool-terra{,-high,-max}`，每个别名挂 **2 个 deployment**
  （api_base 分指 bpi / bpi-82 两 svc），逐 deployment：唯一 `model_info.id`
  （`zerokey-cursor-web-fc-pool-{101,82}-terra*`）+ `weight:1` + 对应档 `reasoning_effort` +
  **`api_key` 占位符必带**（假绿①）+ `model_info.mode`。模型名保住 `cursor-web-fc-` 前缀
  （hook gate 依赖）。
- WA 自动接管：key 级黏性保 conv-reuse 缓存局部性（同 key 永远同 pod → 增量命中不受损）；
  某线挂了 fail-mark 自动把该 key 甩到另一线。
- **换线安全性（构造保证）**：fail-mark/TTL 导致换 pod 时，新 pod 无 conv 缓存 → `_itemDigest`
  miss → 退化全量新会话（"一切异常退化成第一次输入"），正确性不受损，只损一次增量收益。
  两 pod 各持各的账号 seed，换线即换账号，无跨账号状态泄漏面。
- 授权：把 3 个 pool 模型 append 到目标 key（key03/key04）。旧 6 名保留（可随时点名直连单线）。
- **共享面盘点（审批依据）**：不改 WA 代码、不改 hook、不改 responses.js/CM——唯一写操作 =
  `/model/new` 加 6 行 deployment + `/key/update` 授权。爆炸半径 = 新模型名自身；现有模型名
  路由零变化。
- **验收**：临时 scoped key → 打 `cursor-web-fc-pool-terra` 带暗号 ≥4 次 → 断言 ①200+暗号回显
  ②同 key 全落同一 pod（grep 两 pod 日志，key 级黏性）③DB raw `litellm_params` 双 deployment
  都带 api_key（防假绿②）→ 删临时 key。fail-over 演练（可选）：scale0 一线（非服务时段）
  → 断言请求自动落另一线。

**待办：以上设计需你点头后按验收步骤落地（写操作只有 /model/new + /key/update，可秒回滚
`/model/delete`）。**

**落地记录（2026-08-24，用户批准后执行，全部实测）：**
1. `/model/new` 注册 6/6 OK：`cursor-web-fc-pool-terra{,-high,-max}` × {bpi, bpi-82}，
   `model_info.id=zerokey-cursor-web-fc-pool-{101,82}-terra*`，weight:1，api_key 占位符齐。
2. 验收（临时 scoped key `tmp-poolchk-20260824`，非 master key——假绿③纪律）：4/4 请求
   HTTP 200 + 暗号逐字回显（5.0/6.7/5.5/11.1s）。
3. **黏性实证**：proxy 日志 WA `MISS →weighted-pick pool-101-terra (candidates=2, ttl=3600s)`
   后 3 连 `HIT →pinned 同一 deployment`；pod 侧 bpi-101 见 4 个暗号、bpi-82 见 0——key 级
   亲和逐字命中设计。
4. 授权：key04（44→47）、key03（43→46）各 +3 pool 模型，**读现有列表合并写回**非覆盖。
5. 临时 key 已删。Cursor 侧用法：模型名填 `cursor-web-fc-pool-terra{,-high,-max}` 即入池；
   旧 6 名保留可点名直连单线。
- 踩坑记录：`/key/list` 的 `size` 上限 100（size=200 → 422）；422 被 try/except 包住后会
  伪装成"没找到 key"假阴性——先探参数再翻页（15 页）。

**fail-over 演练记录（2026-08-24，scale0 bpi-82 三个短窗口，全程实测后已恢复）：**
1. **宽限期带病服务**：scale0 后 30s 终止宽限期内，Terminating pod 仍在服务（K8s
   ProxyTerminatingEndpoints 兜底：svc 无 ready endpoint 时流量继续发给垂死 pod）——3 发请求
   全 200，101 零暗号（排除法归属）。
2. **死线黑洞窗口 ≈ 120s**：pod 死透后，钉在死线的 key 发请求 = **前门 0 字节纯黑洞**（curl
   90s/150s 实测，无 HTTP 头无心跳）；时钟是 litellm `stream_timeout: 120`——到点抛
   InternalServerError。
3. **fail-mark 自愈实证**：120s 抛错瞬间 WA `fail-marked pool-82 (InternalServerError,
   transient) 180s` + `re-picking`；**下一发请求 MISS → 加权重选 101 → 200 + 暗号回显 + 101
   pod 实见流量**。挂起的那一发本身收不回（客户端靠自己的超时/取消放弃）。
4. **结论**：单线死亡的最坏用户代价 = 钉在该线的 key **一发请求挂 ~2 分钟**，之后自动甩到健康
   线。可接受，无需改代码。可选精化旋钮（未做）：给 pool deployment 加 connect 级短超时可把
   黑洞从 120s 缩到 ~10s，但要先确认 openai/ provider 的超时参数不影响正常 thinking 长流。
5. 演练后已复原：82 线 1/1、演练 key 已删、无残留。

### 4.2 跨线：压缩线 token 触发 + 留一条总结当账本（不落 bpi）

**不适用 bpi**（bpi 靠服务端会话态 conv-reuse + ZK_DIET/HISTDIET 结构瘦身，无模型总结压缩）。
**目标文件是另一个 codebase**：压缩线 skill `~/.claude/skills/codex-compaction-v2-nonnative/SKILL.md`。
动作：压缩触发改按 token 阈值（上下文窗口 90%）不按轮数；保留法照 codex——只留最近 user 消息
（≤20K token，newest→oldest）+ 一条总结（模型自写的该轮末条 assistant），推理/工具历史丢掉，
**总结即账本**。**要动这条得先单独审计该 skill 的现状**（与 bpi 无关，别混着改）。验收：压缩前后
闭环 harness 不失忆；token 触发点可复现。

### 4.3 codex 传输韧性锚点 + bpi 差距判决（2026-08-24 精读，补第5条从未审过的空档）

搬用清单第 5 条（"上游 client timeout=None + idle-timeout 兜底，不只靠心跳"）此前从没对着
responses.js 审过。这次三路 Explore 精读 codex 传输/重试/压缩层，逐条落判决（锚点全表见记忆
`reference_codex_transport_resilience_anchors_2026_08_24`）。

**codex 怎么做（真源码）**：SSE 面**完全不靠心跳**，靠 `timeout(idle_timeout, stream.next())` 每读一
chunk 套空闲超时（默认 `DEFAULT_STREAM_IDLE_TIMEOUT_MS=300000`=5min），超时→可重试 `Stream` 错→
**整轮从历史重建重发**（max 5，backoff `200ms*2^n±10%`）；耗尽后 WS→HTTPS session 级兜底。
关键纪律：codex 敢在流出错后重试，是因为它 **delta 从不落历史、只完整 item 落**、整轮从历史重建；
**已把字节转发给客户端的透明代理不能盲目重试（会吐重复）——只能在"还没吐第一个字节"的窗口重试**。

**bpi 现状 vs 差距（三段式）**：
- **假设**：上游卡死会把 turn 挂死（responses.js 无 idle-abort，只有下游 ZK_HB 5s 心跳，心跳只保
  Cursor 连接不死、检测不到上游已死）。
- **证伪条件**：若成立，pod 日志该有 `[stall] upstream gap` 长空档 / `BODY_TIMEOUT` / 终端挂死。
- **数据**：bpi 主线 ~17 轮 `[stall]` 0 次、0 upstream_error；bpi-82（8h）3 次 3–6s 短空档
  （6417/3174/3369ms，两次@0chars）**全部自恢复**、0 终端挂死。responses.js `_fetch`=裸 `fetch()`
  无 AbortController；唯一兜底=undici 默认 `bodyTimeout≈300s`（与 codex 300s 同量级）。
- **判决**：**无数据支持"因卡死而挂"**（代码路径存在≠在咬人，CLAUDE.md 红线）。当前无触发事件
  →**不盲打生产**。唯一隔离安全候选=**"首字节前(`started===false`)窗口 idle-abort + 单次干净重试"**
  （零重复风险——尚未吐字节；仅碰 bpi 两 pod）。要上须：`ZK_IDLE_ABORT` env-gate **默认关** +
  阈值 > 最坏 thinking 延迟（实测首包间 thinking 可 20s+，取 120s，低于 undici 300s）+ SOP 六步 +
  临时真 key 前门回归。**改动需触及 api.js（`_fetch` 挂 AbortController）+ responses.js（首字节前计时器），
  两文件都在 CM `zk-cursor-bpi-patch`，隔离仍只碰 bpi 两 pod。等你点头再落。**

### 4.4 第二轮深读（2026-08-24）：中断处理 / 缓存前缀 / 多工具生命周期 —— 判决"再无必改项"

三路 Explore 精读此前**从未审过**的三个 harness 区域，同时 grep 198 live responses.js/api.js 对照。
**新坐实一个事实：responses.js/api.js 对客户端断连零处理**（无 `req.on('close')`/abort/
AbortController/destroy，grep 逐模式计数全 0，对照模式命中正常）。逐区判决：

**(a) 中断/取消（Cursor 点"停止"）——零处理，但构造上安全：**
- codex 做法：CancellationToken 真掐在途读；已完成 item 留史、未 done 的 delta 丢；孤儿 call
  **下一轮组 prompt 时**惰性补合成 output（正文 `"aborted"`，`normalize.rs:21`）；塞
  `<turn_aborted>` 标记告知模型；**WS 增量基线只在 Completed 提交**（`client.rs:2106`）→被打断的
  流永不提交→下轮自动全量。"打断后回退全量"是构造保证。
- bpi 行为（数据流推演 + 0 重启佐证）：无 handler → **上游消费到完整结束**，`res.write` 进死 socket
  静默丢弃；`finish()` 照常跑 → conv session 保存的 conversation_id/parent_id 与**服务端真实状态
  一致**（安全的那一侧）。下轮 Cursor 发来的历史与缓存指纹不符（它只留了半截）→ `_itemDigest`
  前缀校验 miss → **退化全量**。与 codex "基线不提交→全量" **同构、不同机制**（我们靠指纹兜）。
- 真实代价（有界）：slot 被占到上游讲完（下条消息在 acquireSlot 排队）+ 白烧一条上游消息（网页面
  消息发出即计入，掐了也不退）。**判决：无数据显示这在咬人（0 崩溃、0 用户投诉路径），不改。**
  如将来要蹲证据，候选是加一行 `req.on('close')` 计数日志（纯观测，非行为变更）。
- ⭐可白嫖的思想：codex 对被打断轮塞 `<turn_aborted>` 标记教模型"别当无事发生"——我们若将来发现
  截断轮模型自我混乱，可在下轮 delta 里加同型标记（目前无此现象，不做）。
- 诚实边界：**"上游消费到完整结束/finish 照常跑"是数据流推演**（无 handler ⇒ 无路径中断它），
  未拿"断连时刻的 pod 日志"直接实证；佐证是 0 重启 + 0 异常日志。
**(b) 缓存前缀稳定性——对 bpi metered 收益不适用，纪律已同构：** codex 全套（`prompt_cache_key`=
  session id 恒定、tools 存原始字节防 key 重排、环境上下文做成 world-state diff item 不进
  instructions、合成 ID 确定性 UUIDv5 注释明写"变了毁缓存"）都服务于"前缀逐字节稳定"。bpi 走
  网页订阅面不按 token 计费，**无 prompt-cache 折扣可赚**；"动态信息不进逐轮重发前缀"的纪律
  我们 cache key（`sha1(input[0])+sha1(instructions)` 稳定锚点）已在守。**不改。**
**(c) 多工具/turn 生命周期——聚合行为趋同，无可搬项：** codex 输出按发射序非完成序、配对靠
  call_id 非位置邻接（我们 flatten 成文本，结构配对本就 N/A，作业2 同理）；工具输出截断默认
  10KB **middle-out 带标记**（`truncate.rs:131`）——与我们 HISTDIET `_HIST_KEEP` 头+尾200字符+
  截断标记**同族**；turn loop 无最大迭代护栏（靠压缩兜，且 loop 在 Cursor 侧不在我们侧，N/A）；
  rollout 持久化=语义重放非字节拷贝——启发"conv 缓存持久化跨 pod 重启"，但内存缓存清空→天然
  miss→全量是**安全默认**，pod 重启罕见，持久化反引入陈旧态风险。**不改。**

**第二轮总判决：0 必改项。** bpi 线在中断恢复、配对安全、截断策略三个维度与 codex 的做法
**殊途同归**（它靠结构化不变式，我们靠"一切异常退化成第一次输入"）。两轮深读合计审完 codex
harness 的传输/重试/压缩/配对/中断/缓存/工具生命周期七个面，搬用清单全部关账。

---

## 5. 事实 vs 推测

**事实（有数据实证）：**
- §1.1 acct101 线现状、§1.2 全部（web 额度三段式坐实、acct-82 克隆四步经真 key 路径验收、
  api_key 假绿修复）——本会话 198/standby 实测。
- §3 假绿三连、JWT exp ≠ 上游认——本会话真踩并修复。
- §2 目标取向由用户本会话行为定调（建 acct-82、选三档、共用号各走额度）。
- 网页面工具调用 best-effort；100% 可靠只有 codex 原生 FC。
- §4.0 codex 4 条作业的审计判决——本会话对 198 实拉的 hook + responses.js 逐条静态审计，
  file:line 证据在表内（delta 轮传 null instructions、flattenInput 转文本、delta==done、r9 usage）。
- §4 codex 源码锚点——三个 Explore agent 精读真源码 commit 343074d。
- §4.3 codex 传输韧性锚点（idle-timeout 300s / backoff / WS→HTTPS / retry 重复输出纪律）+ bpi
  stall 数据（主线 0 次、bpi-82 3 次短空档全恢复、0 终端挂死）——本会话精读 + pod 日志实测。
- §4.4 responses.js/api.js **客户端断连零处理**——198 live 代码逐模式 grep 计数全 0（含对照模式
  验证 grep 本身有效）；codex 中断/缓存/生命周期锚点——三路 Explore 精读。
- §4.1 侦察三项——acquireSlot=滑动窗 5/15s 限速器（非互斥锁，pod 镜像内 rate-limiter.js 实读）；
  WA 钩子通用、多 deployment 自动生效、Cursor 无 session 头→key 级亲和（wa.py 871 行实读）；
  6 模型注册形态（proxy 内部 /model/info 实查）。
- §4.1 组池落地全链路——6/6 注册、4/4 200+暗号回显、WA MISS→3×HIT 同 deployment、
  pod 侧 4:0 归属、key03/44→47 key04/43→46——2026-08-24 实测。
- §4.1 fail-over 演练——宽限期带病服务 / 死线黑洞 ≈120s（stream_timeout 时钟）/ fail-mark
  180s + 重选 101 闭环 / 最坏代价=钉线 key 一发挂 ~2min——2026-08-24 scale0 实测三窗口。
- pool 工具闭环 + hook 经 pool fire + conv 增量 96 chars（作业1 live 计数坐实）——2026-08-24
  loop 闭环实测（82 线）；`-max` 档 200/4.5s/回显（`-high` 同机制未单测）。

**推测（缺确认，已标明）：**
- §4.1 可选精化：pool deployment connect 级短超时缩黑洞（120s→~10s）——参数对 thinking 长流的
  副作用未验，未做。
- §4.0 作业 4 的 usage 数字未在闭环里逐轮核对（作业 1 的"无累积"已由 delta 96 chars live 坐实）。
- §4.3 "上游卡死会挂死 turn"——**假设未被数据支持**（0 终端挂死），idle-abort 硬化候选属预防性、
  当前无触发事件，未落地；要上须 `ZK_IDLE_ABORT` 默认关 + 阈值 120s + SOP 六步。
- §4.4(a) "断连后上游消费到完整结束、finish 照常跑"——**数据流推演**（无 handler ⇒ 无路径中断），
  佐证 0 重启/0 异常日志，未拿断连时刻 pod 日志直接实证；要实证可加 `req.on('close')` 观测计数。

---

## 附录 / 相关资产

- SOP skill：`.claude/skills/zk-cursor-web-fc-iterate/SKILL.md`（迭代六步 / 12-key CM patch 纪律 /
  开关总表 / 高频坑 / 克隆到新账号 / codex 搬用作业单）。
- 克隆脚本：`scripts/zk-cursor-web/clone_web_fc_lane.py`（一键克隆一条 web-fc 线到新账号）。
- 补丁/harness 脚本：`scripts/zk-cursor-web/`；harness `/Data/backups/zk-cursor-web-harness-20260822.tgz`。
- CM 备份链：`/Data/backups/zk-cursor-bpi-cm-*-pre-r{9..18}.json`。
- 记忆索引：`topic_zk_cursor_bpi_web_channel_index`（当前态 + 多账号克隆）·
  `reference_codex_source_harness_anchors_2026_08_24`（codex 源码地图 + 搬用清单）·
  `feedback_openai_model_needs_placeholder_api_key`（假绿三连）·
  `feedback_ws_oauth_token_drives_web_seed`（两套登录态）·
  `feedback_web_seed_jwt_exp_not_upstream_honored`·
  `feedback_cursor_gpt_web_toolcall_collapses_under_full_payload`（r1–r18 考古）·
  `feedback_cursor_hits_responses_endpoint_not_chat_shim_dormant`（cursor-fc 原生 FC 路线）。
