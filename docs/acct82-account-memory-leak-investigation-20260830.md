# acct82 账号级 memory 跨用户泄漏 — 机制调查 + rollback-safe 关闭方案(2026-08-30)

> 状态:**只调查、未动手**(用户拍板"先只调查机制不动手"→"先把方案写到文档和记忆索引,暂时不要动手")。
> 未登录 acct82、未改任何 toggle、未碰共享 CM。本文是待执行方案 + 一格待补数据,不是已执行记录。

## 1. 现象
- 用户 A 在某 cursor-g 会话说"记住我的工牌号 X",用户 B 在**另一条 cursor-g 会话**能把 X 问出来。
- 根因媒介:cursor-g 池共享 ChatGPT 账号 **acct82**(同时被 codex 线复用)。

## 2. 已有证据(三段式,数据栏非空)
定因探针见 [[project_chain_srv_gateway_deployed_2026_08_30]]:
- **假设**:跨轮记忆来自会话状态(conv / `previous_response_id`)。
- **证伪条件**:若是会话态,`chatSessionId:null` + 零历史 + 垃圾 `previous_response_id` 应断链答不出。
- **数据**:T2 请求到网关 `chatSessionId:null` + 零历史 + 垃圾 rid → **仍答对**;垃圾 rid 200 无错。
  → 会话态假设被证伪,状态只能活在**上游 ChatGPT 账号级 memory**(saved memories / reference chat
  history),与网关无关(responses.js 零处理该状态)。

## 3. 泄漏根因链
acct82 是共享池号 → 该账号的 "Reference saved memories" / "Reference chat history" 把任一用户写入的
事实持久化到**账号级** → 任何后续会话(不分用户)命中。**是 ChatGPT 账号特性,不是我们注入的**。

## 4. 两条关闭路径与回滚安全性

| 路径 | 手法 | 能否关净 | 回滚 | 对 codex 线连带风险 |
|---|---|---|---|---|
| **A. 账号设置侧** | ChatGPT Settings → Personalization → Memory:关 "Reference saved memories" + "Reference chat history",清空已存 memories | **能**(源头在账号) | 重开 toggle 即恢复;**已清空的 memories 不可逆** → 动手前先导出/记录 | acct82 与 codex 线**共享同账号 token/memory**,关闭是账号级全局 → codex 同时生效,**唯一真连带点** |
| **B. 网关侧拦截** | responses.js 注入指令/剥历史 | **关不净**——账号 memory 由上游生成时自行 reference,请求体看不到、拦不掉;靠 system"忽略记忆"不可靠 | 门控易回滚 | 需碰共享 CM,效果不确定 |

→ **A 是唯一能真正关净的路径**;B 治标不治本。

## 5. 动手前唯一待补的一格数据(❗现在还空)
关闭是账号级全局,所以动手前必须先证伪:**"codex 线不依赖 acct82 账号 memory"**。
- **假设**:codex 线靠 `previous_response_id` 链 + 加密 reasoning 块承载多轮,不读账号 saved-memories。
- **证伪条件**:若 codex 依赖账号 memory,关闭后 codex 多轮任务应"忘记上文"回归。
- **数据**:**空**——尚未跑 codex 关-memory 前后对照。**故现在不能下"关了对 codex 无害"的结论。**

## 6. rollback-safe 执行方案(待批准后才动手)
1. **补第 5 格(只读)**:scoped key 用完即删,在 acct82 跑一轮 codex 风格多轮任务,dump 上下文来源
   (命中 saved-memories vs 纯 reasoning 链)。证实 codex 不读账号 memory。
2. **导出兜底**:先记录/导出 acct82 当前已存 memories(不可逆清空前的还原点)。
3. **执行路径 A**:关 "Reference saved memories" + "Reference chat history",清空 memories。
4. **验收**:重放第 2 节探针(null-session + 零历史 store→query 跨会话)应**不再命中**;codex 多轮任务
   与关闭前结构对照无回归。
5. **回滚**:重开两个 toggle 即恢复(memories 已清不可逆,靠第 2 步导出还原)。

## 硬约束
- 未获批准前不登录 acct82、不改 toggle、不清 memories、不碰共享 CM。
- 触碰 acct82 属 codex 共享面,守既有铁律:禁碰 codex CLI 代码/账号面加号清限;scoped key 用完即删。
