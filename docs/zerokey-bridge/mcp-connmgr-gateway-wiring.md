# MCP 连接器生命周期管理器 —— 网关接线(缺口#2 live-enable)

> 缺口#2 的离线核心 `ConnectorManager`(commit `516ffb9`,19/19)接进 responses.js turn-1 缝合口,
> 让桥会话按 §5.1 方案A **逐会话 provision→link→用完 teardown(verify 404)**,不留持久连接器。
> 接线字节 author 在**活 CM 字节**上(manifest-drift 纪律:仓库 responses.js 是陈旧快照,生产走 CM
> `zk-cursor-bpi-patch` on 10.68.13.198,见记忆 `feedback_manifest_prod_drift_apply_overwrites`)。
> 本文档逐字记录 6 处接线 diff + 分段部署 + 活体生命周期实测,便于复现/审计/回滚。

全部行为锁在 `mcpBridge.isEnabled() && process.env.ZK_MCP_PUBLIC_URL`,两者缺一 = 管理器 null = 零行为差。

## 字节链

| 文件 | CM key | 基线 md5 | 接线后 md5 | 部署目标 |
|------|--------|----------|-----------|---------|
| responses.js | `responses.js` | `5432d092`(缺口#1) | **`8aa6bf88`(+6 接线 diff A–F)** | CM(共享)+ 仅 82 lane 激活(101 无 mcp_connector_mgr.js cp + 无 env) |
| mcp_connector_mgr.js | `mcp_connector_mgr.js`(**新增 key**) | —(commit `516ffb9`) | `77fe031a` | 仅 `zero-cursor-bpi-82` args 加 `cp /patch/mcp_connector_mgr.js /app/routes/` |
| mcp_bridge.js | `mcp_bridge.js` | `41b19c39` | `41b19c39`(不动) | 同缺口#1 |

## 6 处接线 diff(responses.js,活字节 `5432d092`→`8aa6bf88`)

- **Diff A(管理器构造,接在 mcp_bridge 容错 require catch 之后)**:门 `mcpBridge.isEnabled() && ZK_MCP_PUBLIC_URL`
  才构造;`_seedHeaders = require('/seed/users.json').chatgpt[_acct].parsedFetch.headers`(in-pod ephemeral,
  never to host disk);`store: fileStore('/app/convcache/mcp_conn_'+_acct+'.json')`(pod-local 原子写,穿越重启);
  `Promise.resolve().then(()=>mcpConnMgr.recoverOrphans())`(启动删旧孤儿);`setInterval(sweep, 10000).unref()`
  (10s 巡检拆过期 grace)。缺任一门 → `mcpConnMgr=null`。
- **Diff B(BRIDGE_CONTRACT 分支加 `_bridgeReady`/`_connAccount` 门)**:仅非续流轮 `acquire`(provision/复用/grace-cancel)。
- **Diff C(turn-1 挂起缝合口)**:门收紧为 `if (_bridgeReady)`(管理器活且连接器已 link 才接管挂起)。
- **Diff D(finishPlain)** / **Diff E(turn-2 续流)** / **Diff F(error catch)**:三条收口路径均 `release(_connAccount)`
  → refCount-- 归零进 grace → 由 sweep 定时器拆。

## 分段部署(字节安全 → 激活)

1. **Stage 1(字节安全)**:CM merge patch 写入 wired responses.js + 新 mcp_connector_mgr.js key;canary-82 args
   加 `cp /patch/mcp_connector_mgr.js /app/routes/`;**不设 `ZK_MCP_PUBLIC_URL`** → 管理器 null,行为等同缺口#1,
   零差。备份 `/home/cltx/backups-bpi/responses.js.20260828-133354.pre-connmgr.bak`(`5432d092`)。
2. **Stage 2(激活)**:canary-82 env 加 `ZK_MCP_BRIDGE=1 ZK_USER=acct82 ZK_MCP_ACCOUNT=acct82
   ZK_MCP_PUBLIC_URL=https://cc.auto-link.com.cn/zkmcp-<32hex> ZK_MCP_GRACE_MS=30000` → rollout **仅 82** →
   boot log `[conn-mgr] wired account=acct82 publicUrl=…`。

## 活体生命周期实测 —— §5.1 方案A PROVEN(canary-82,acct82 真会话,2026-08-28)

`/tmp/connmgr_live_test2.js`(pod 内 node,memStore = 不碰 pod fileStore,插桩 poll snapshot 到 teardown 完成):

```
[conn-mgr] provisioned account=acct82 connector=asdk_app_6a911fa3… link=link_6a911fab…
ACQUIRE ok id=asdk_app_6a911fa3…                       # provision 全链 devmode→register→actions→link,tools=[shell]
[conn-mgr] release→grace account=acct82 graceMs=1
[conn-mgr] teardown account=acct82 connector=asdk_app_6a911fa3… verified404=true
teardown COMPLETED after ~7000ms (snapshot entry gone) # del 后 ChatGPT actions 反映 404 需 ~7s 传播
t+0ms actions plane=ROUTE_OK_BAD_ID gone=true tools=0
FINAL cleanup verify plane=ROUTE_OK_BAD_ID gone=true   # 账号面回基线,无孤儿
```

**LIFECYCLE PASS**:acquire(provision)→ LINKED(tools=[shell])→ release→grace → sweep→teardown(del + verify404=true)。
- **首测 gone404=false 非 bug**:首测单 sweep + 固定 4s 等待,撞上 ~7s 异步 teardown 未完;插桩 poll 版证 teardown
  真跑完 `verified404=true`。管理器正确,固定等待太短而已(三段式:证伪条件=teardown 未跑 → snapshot 条目不删;
  数据=条目 7s 后删 + verify404=true → teardown 确实跑完,首测是过早退出)。
- **账号干净**:pod `/app/convcache/mcp_conn_*` 不存在(实测用 memStore,pod fileStore 未碰,无孤儿);两测连接器
  ID 均 delete-verify `ROUTE_OK_BAD_ID`。

## codex 非回归(诚实标注,不造绿)

acct82 是 codex serve 池上游(`BRIDGE_UPSTREAMS`)。连接器**仅在活跃桥会话窗口 + 30s grace 内 link**,窗口外
account 面**零连接器**(稳态 = codex 见基线)。窗口内足迹由 **Gate-2c 实证**框定(基座对未请求连接器**自发调用率=0**,
残余仅上下文足迹)。**不主动生成 codex serve 流量去"证"非回归**——那本身扰动 owner 划死的 codex account 面;
结构安全阀 = **auto-teardown(exposure 有界)+ delete-verify(每会话删净)+ Gate-2c(自发调用=0)**。full
codex-harness 逐字非回归是唯一未独立复跑项,如实标注,非宣称 PASS。

## 回滚

1. canary-82 env 去 `ZK_MCP_PUBLIC_URL` → 管理器 null(回 Stage 1 字节安全,行为等同缺口#1)。
2. 彻底回滚:env 去 `ZK_MCP_BRIDGE` + args 去两条 cp 行 + CM 从 `pre-connmgr.bak` 还原 responses.js + 删
   mcp_connector_mgr.js key。101 全程未动(无 cp mcp_connector_mgr.js → require 失败 → stub → isEnabled=false)。
