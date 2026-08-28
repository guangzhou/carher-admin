# MCP 会合桥 Branch-A —— responses.js / serve 缝合口(部署字节 = 活 CM 字节)

> 仓库里的 `routes/responses.js`(953 行,md5 `2eddb8d1`)与 `zerokey-serve-codex.js`(88 行,
> md5 `8da7d6a2`)是**陈旧快照**——生产走 CM `zk-cursor-bpi-patch`(ns `litellm-product` on
> 10.68.13.198)直接 patch,git 不是这两个文件的 source of truth(见记忆
> `feedback_manifest_prod_drift_apply_overwrites`)。故 Branch-A 的缝合口 author 在**活 CM 字节**上,
> 本文档逐字记录三处缝合 + 前后 md5,便于复现/审计/回滚。自包含的可离线证模块
> `routes/mcp_bridge.js` + 离线单测 `scripts/zk-cursor-web/mcp_bridge_offline_cases.js`(17/17)在仓库。

全部行为锁在 `ZK_MCP_BRIDGE==='1'`,默认关 = 零行为差。

## 字节链

| 文件 | CM key | 基线 md5(活) | 缝合后 md5 | 部署目标 |
|------|--------|--------------|-----------|---------|
| responses.js | `responses.js` | `2f94ed79` | `39a08ba4`(三缝合口)→ `128e67e6`(+piece#3 桥模式契约)→ **`5432d09253c23252dae58fb90e5485c4`(+缺口#1 seam 传 toolParam/toolParamArray)** | CM(共享)+ 仅 82 lane 生效(101 容错 stub+无 env) |
| zerokey-serve-codex.js | `zerokey-serve-codex.js` | `d256ecaf` | `8f28510e` | 同上 |
| mcp_bridge.js | `mcp_bridge.js`(**新增 key**) | —(新) | `e8b70ed9` → **`41b19c394e63962c8939c26206d7edfa`(+缺口#1 remapArgsForCursor)** | 仅 `zero-cursor-bpi-82` Deployment args 加 `cp /patch/mcp_bridge.js /app/routes/mcp_bridge.js` |

> **piece #3(桥模式契约)**:三缝合口只搭了运输管道,但 turn-1 挂起口 fire 时,若模型仍收到旧
> `V2_CONTRACT` 的 `⟦cmd¦run⟧` 方言,它会写 `⟦cmd⟧` 散文 → 无 tool call → reader 到底 → `finishPlain`
> 把 `⟦cmd¦run=ls⟧` **裸漏**给用户(实测回归)。piece #3 在 prompt 级联首位加 `BRIDGE_CONTRACT`
> 分支(见缝合口 4/4),桥开时用"调 linked connector 的 shell 工具"替代 `⟦cmd⟧` 教学 → 模型改走
> 原生工具调用。修后探针 `LEAK=False`、正文纯净。

## 缝合口 1/3 —— responses.js 容错 require(接在 bpi-codex require 块之后)

```js
} catch (e) {
  console.warn('[bpi] bpi-codex.js not mounted, BPI compilation disabled:', e.message)
}
// 容错 require:MCP 会合桥(Branch-A)。默认 stub=isEnabled()->false,零行为差。
let mcpBridge = {
  isEnabled: () => false, getRegistry: () => null, findFunctionCallOutput: () => null,
  buildMcpRoute: () => ((req, res) => res.status(404).end()),
}
try { mcpBridge = require('./mcp_bridge') } catch (e) {
  try { console.warn('[mcp-bridge] mcp_bridge.js not mounted (gated off):', e.message) } catch (_) {}
}
const { normalizeToolDefs, ... } = require('./web-tools')
```

## 缝合口 2/3 —— responses.js turn-2 续流(接在 `const mdl = ...` 之后,`if (stream){` 头块之前)

命中 `byCallId(fco.call_id)` 的挂起桥 → 抢在 acquireSlot/握手/建上游会话之前短路:自发头 +
`beginContinuation`(重指 sink 到 turn-2 socket + 发新 response.created/in_progress)→ `rendezvous.complete`
唤醒挂起的 `/mcp`(把 output 交回 Cursor 侧那次 `tools/call`)→ 等 turn-1 那条 held reader 续流收口。
`byCallId` miss(pod 重启/多副本/桥已 forget)→ 落常规流程(降级,记日志),**不 return**。

```js
if (mcpBridge.isEnabled() && stream) {
  const _fco = mcpBridge.findFunctionCallOutput(input)
  if (_fco && _fco.call_id) {
    const _reg = mcpBridge.getRegistry()
    const _ctx = _reg && _reg.byCallId(_fco.call_id)
    if (_ctx && !_ctx.continued) {
      res.setHeader('Content-Type', 'text/event-stream'); res.setHeader('Cache-Control', 'no-cache')
      res.setHeader('Connection', 'keep-alive'); res.setHeader('Access-Control-Allow-Origin', '*')
      if (res.flushHeaders) res.flushHeaders()
      const _sink2 = { write: (s) => res.write(s), end: () => res.end() }
      _ctx.beginContinuation(_sink2, { respId, created, model: mdl })
      _reg.rendezvous.complete(_fco.call_id, _fco.output)
      console.log(`[mcp-bridge] turn-2 continuation call_id=${_fco.call_id}`)
      try { await _ctx.readerPromise } catch (_) {}
      return
    } else {
      console.log(`[mcp-bridge] turn-2 byCallId miss call_id=${_fco.call_id} -> normal flow (degrade)`)
    }
  }
}
```

## 缝合口 3/3 —— responses.js turn-1 挂起(接在 upstream try/catch 之后,`let full = ''` 之前)

头 + response.created/in_progress 已在上方发过 → openTurn 不重发。持一条跨 Cursor 轮存活的上游
reader,race reader vs injectedPromise:reader 先到底(普通答/没调工具)→ finishPlain;inject 先到
(/mcp 命中)→ return 把 held reader 交给 turn-2。只接管 `useWebTools && !chatOnly && stream`。

```js
if (mcpBridge.isEnabled() && useWebTools && !chatOnly && stream) {
  const _reg = mcpBridge.getRegistry()
  const _bsink = { write: (s) => res.write(s), end: () => res.end() }
  const _bsess = (convSess && convSess.convId) || _convKeyOf(input, instructions)
  const _bctx = _reg.openTurn({
    session: _bsess, sink: _bsink, respId, created, model: mdl, msgId,
    toolName: (shellTool && shellTool.name) || undefined,
  })
  const _brp = readSSE(upstream, _reg.makeReadCallbacks(_bctx))
  _brp.catch(() => {})               // held reader 悬到 turn-2;弃单 reject 不成 unhandledRejection 崩 pod
  _bctx.readerPromise = _brp
  await Promise.race([_brp, _bctx.injectedPromise])
  if (_bctx.injected) { console.log('[mcp-bridge] turn-1 injected call_id=' + _bctx.callId); return }
  try { await _brp } catch (_) {}
  _bctx.finishPlain()
  return
}
```

## 缝合口 4/4 —— responses.js 桥模式契约(BRIDGE_CONTRACT,piece #3)

`BRIDGE_CONTRACT` 常量(L337)+ prompt 级联分支(L592)。桥开且 `useWebTools && !chatOnly` 时,
用"调 linked connector 的 `shell` 工具"替代 `V2_CONTRACT` 的 `⟦cmd¦run⟧` 方言教学 → 模型改走原生
工具调用,turn-1 挂起口才有 inject 可等;否则模型写 `⟦cmd⟧` 散文 → 无 tool call → `finishPlain` 裸漏。

```js
// L337
const BRIDGE_CONTRACT = `\n\nYou can run shell commands on the user's machine by calling the ` +
  `connector tool named "shell" (argument: the command string). Call it as a real tool — do NOT ` +
  `write ⟦cmd¦run=…⟧ prose, do NOT emit tool_calls text, and do NOT simulate the result with your ` +
  `own python/analysis tools (their output is FAKE to this user). The only real execution path is ` +
  `the connector shell tool; wait for its result, then continue.`

// L592
if (mcpBridge.isEnabled() && useWebTools && !chatOnly) {
  prompt = basePrompt + BRIDGE_CONTRACT
  console.log('[mcp-bridge] prompt: native-connector shell mode (⟦cmd⟧ contract suppressed)')
}
```

## Round-trip 实测 —— 端到端已证(canary 82,WEB 路由)

`/tmp/mcp_roundtrip_probe.py`(MODEL=`cursor-web-fc-82-terra`,1h scoped key 用完即删):

- **turn-1**:`completed=1 failed=0 text_len=0`,`fc={"call_id":"9a30cec0b3ba0f50","name":"Shell","arguments":"{\"cmd\":\"ls\"}"}` — 模型调了 linked connector,inject 把 function_call 打到 turn-1 SSE。
- **turn-2**:回 `function_call_output(call_id, FAKE_LS)`,`FAKE_LS` 含独创串 `ZKPROBE_ALPHA.txt` → `completed=1 failed=0 text_len=45`,续流正文 = `"ZKPROBE_ALPHA.txt\nZKPROBE_BRAVO.log\nREADME.md"` → **PASS**(模型复述了只有我发明的假输出 = tool output 确实回流且续流建立其上)。
- **pod seam 日志**:`/mcp mounted (ZK_MCP_BRIDGE=1)`、`prompt: native-connector shell mode`(×3)、`turn-1 injected call_id=9a30cec0b3ba0f50`、`turn-2 continuation call_id=9a30cec0b3ba0f50` — **同一 call_id 跨两口**,证 `deriveCallId`(sha1(session\0tool\0canonical(args)).slice(0,16))跨轮稳定(JSON-RPC id 是连接内计数器,做不了 key)。
- **验后清理**:连接器 `delete`→200,`actions <id>`→404,pod 内临时 session bundle + CLI 擦除 → acct-82 账号面回基线。

## 已知缺口(投产前必补)

1. **参数形状不匹配(`cmd` vs `command`)—— ✅ 已修 + 生产实测(2026-08-28)**:serve `bridgeTools` 声明
   `required:['cmd']`(实测连接器 `actions` 亦回 `required:[cmd]`),模型按此发 `{cmd:"ls"}`;修前桥 inject
   function_call `name=Shell args={"cmd":"ls"}` 给 Cursor,但 Cursor 真 `Shell` 执行器 `required=['command']`
   → 读 `command` 得 undefined,真 Cursor 会断(探针泛型解析所以旧证过了)。
   **修**:取 (b) inject 时 remap —— `mcp_bridge.js` 新增纯函数 `remapArgsForCursor(rawArgs, toolParam, toolParamArray)`
   (helper + `BridgeContext` 构造存 `toolParam/toolParamArray` + `inject` 发 Cursor 前 remap + 导出);
   `responses.js` turn-1 seam 从 `shellTool.key`/`.isArray` 透传 `toolParam/toolParamArray`(Cursor Shell → `command`/非数组)。
   **自适应**:有 `toolParam`(真 Cursor 路)才 remap,缺省(离线/自控端)原样透传 = 零行为差;`deriveCallId` 用
   ChatGPT 侧**原始** args,remap 只改发给 Cursor 的那一份,不动会合键。
   **离线**:`mcp_bridge_offline_cases.js` 19/19(+`remapArgsForCursor` 纯函数 7 断言 + inject remap 端到端)。
   **生产实测**(canary-82,provision→round-trip→即删):turn-1 fc
   `{"call_id":"9a30cec0b3ba0f50","name":"Shell","arguments":"{\"command\":\"ls\"}"}` —— 发给 Cursor 的
   `arguments` 已是 **`{"command":"ls"}`**(修前 `{"cmd":"ls"}`);`call_id` 与修前**逐字一致** = 证 remap 不动会合键;
   ROUND-TRIP PASS(续流复述 `ZKPROBE_ALPHA/BRAVO`)。**缺口#1 闭合。**
2. **codex-shared 账号面挂持久连接器 = Gate-2 一票否决 —— ✅ 离线核心已产品化(2026-08-28,commit `516ffb9`)**:
   acct-82/acct-93 都是 codex serve 池上游(`BRIDGE_UPSTREAMS` 含 zero-82 + zero-93)。用户已豁免 Gate-2 结构否决,
   但持久 account-level 连接器仍落 codex 账号面 → 生产唯一安全形态是 **§5.1 方案 A(每会话 link/unlink,活动窗口内挂、
   用完即摘 + 复验 404)**,不留持久连接器。本次实测即遵此:provision→round-trip→立即 delete。
   **产品化**:`routes/mcp_connector_mgr.js`(293 行)`ConnectorManager` 引用计数五态机(IDLE/PROVISIONING/LINKED/
   GRACE/TEARDOWN):`acquire`(首会话 provision 全链 devmode→register→actions→link,并发单飞,grace 中取回则取消拆)/
   `release`(refCount→0 进 grace)/`sweep`(过 idleGraceMs 拆:del + verify actions 落 GONE_PLANES=404 复验)。
   硬约束落地:无 list-by-account → register 一成功即 `store.save`(崩在 link 前也留可回收孤儿),`recoverOrphans()`
   启动删旧;409 EXISTS 幂等复用;rollback(actions 空/无 enabled/link 失败)→del 回滚。副作用全注入(adapter+store+
   注入时钟),核心零网络零磁盘。**离线** `scripts/zk-cursor-web/mcp_connector_mgr_offline_cases.js` **19/19**;
   sibling `mcp_bridge` 19/19 无回归。`makeLiveAdapter` 逐字镜像 `mcp-connector-cli.js` 请求形状(live-enable 前不接线)。
   **网关接线 ✅ 已做 + 活体生命周期 PROVEN(2026-08-28)**:6 处接线 diff(A–F)把管理器挂进 responses.js turn-1
   seam(provision/release + 启动 recoverOrphans + 10s sweep 定时器),活字节 responses `5432d092`→`8aa6bf88`,
   CM 新增 `mcp_connector_mgr.js` key。分段部署:Stage 1 字节安全(无 `ZK_MCP_PUBLIC_URL` → 管理器 null =
   零行为差)→ Stage 2 激活(canary-82 env `ZK_MCP_PUBLIC_URL=…/zkmcp-<32hex>` + grace 30s)。活体实测
   (`connmgr_live_test2.js`,acct82 真会话):acquire→provision(devmode→register→actions→link,tools=[shell])→
   release→grace→sweep→teardown(del + **verify404=true**)→ 账号面回基线无孤儿 = **LIFECYCLE PASS**。门控
   `mcpBridge.isEnabled() && ZK_MCP_PUBLIC_URL`,101 无 cp mcp_connector_mgr.js → require 失败 → stub → 全 gated off。
   接线字节链/6 diff/回滚见 `mcp-connmgr-gateway-wiring.md`。
3. **codex 非回归(诚实标注,非造绿)**:acct82 是 codex serve 池上游;连接器**仅活跃桥会话窗口 + 30s grace 内 link**,
   窗口外 account 面**零连接器**(稳态 = codex 见基线)。窗口内足迹由 Gate-2c 实证框定(基模对未请求连接器自发调用率=0)。
   结构安全阀 = auto-teardown(exposure 有界)+ delete-verify(每会话删净)+ Gate-2c。**不主动生成 codex serve 流量去
   "证"非回归**(那本身扰动 owner 划死的 codex account 面);full codex-harness 逐字非回归是唯一未独立复跑项,如实标注。

## 缝合口(serve)—— zerokey-serve-codex.js

容错 require(接在 `buildImagesRoute` require 之后):

```js
let mcpBridge = { isEnabled: () => false, buildMcpRoute: () => ((req, res) => res.status(404).end()) }
try { mcpBridge = require('./routes/mcp_bridge') } catch (e) {
  console.error('[mcp-bridge] require failed (gated off):', e && e.message)
}
```

gated `/mcp` mount(IIFE 内,接在 `app.use('/v1/images', ...)` 之后):

```js
if (mcpBridge.isEnabled()) {
  const bridgeTools = [{
    name: 'shell',
    description: 'Run a shell command on the user\'s machine and return its stdout/stderr.',
    inputSchema: { type: 'object', properties: { cmd: { type: 'string', description: '...' } }, required: ['cmd'] },
  }]
  app.use('/mcp', mcpBridge.buildMcpRoute({ tools: bridgeTools, serverInfo: { name: 'zk-mcp-bridge', version: '0.1.0' } }))
  console.log('[mcp-bridge] /mcp mounted (ZK_MCP_BRIDGE=1)')
}
```

## 部署纪律(仅 82 lane)

1. 本地/staging 字节 md5 校验(responses 活 `5432d092`(含 piece #3 + 缺口#1 seam)/ serve `8f28510e` / mcp_bridge.js 活 `41b19c39`(含缺口#1 remap))。
2. 备份现 CM 三键到 `/home/cltx/backups-bpi/`。
3. `kubectl patch cm zk-cursor-bpi-patch --type merge --patch-file <file in /home/cltx/>`:更新 responses.js +
   zerokey-serve-codex.js,新增 mcp_bridge.js。(不重启在跑 pod。)
4. `kubectl patch deploy zero-cursor-bpi-82`:args 加 `cp /patch/mcp_bridge.js /app/routes/mcp_bridge.js`
   (**ZK_MCP_BRIDGE 先不设**)→ rollout restart+status **仅 82** → 验 82 健康 + 行为等同(gate off)。
5. 再 patch 82 env 加 `ZK_MCP_BRIDGE=1` → rollout → 集成测(acct-93 端到端 / acct-82 canary,连接器只在
   活动窗口 link、用完 unlink 复验 404)。
6. 回滚:82 env 去 `ZK_MCP_BRIDGE` / args 去 cp 行 + CM 从 `/home/cltx/backups-bpi/` 还原。101 全程不动。

---

## 【2026-08-28】bridge-v2 重设计 + canary-82 Stage 1/2 验收

### 病根(为什么要 v2)
phase-0 的 `BRIDGE_CONTRACT` 教模型"自带工具输出对本用户是 FAKE"。真 Cursor turn-2 发原生
`call_web_*_Shell`、其 call_id 与网关 turn-1 注入的 deriveCallId **必然不同** → `byCallId` MISS →
旧契约让模型弃掉真工具输出 → 编造无 URL 的飞书文档(memory `feedback_mcp_bridge_breaks_real_cursor_native_toolcalls`,
上次 ON 即回归、已回滚 82)。合成 probe 因为忠实回显网关 call_id 恒 HIT = 假绿陷阱。

### v2 机制(全部离线可测)
- **V2B_CONTRACT 双通道**替换互斥的 BRIDGE_CONTRACT:`PREFERRED — call the connected tool "shell"`(同轮会合)
  / `FALLBACK — ⟦cmd¦run=<bash>⟧`(下一轮);关键一句 **`Results returned through EITHER channel are REAL`**——
  byCallId miss 降级到常规 proto2 flow 时,模型照常消化真输出,不再判 FAKE。
- **`_stream` 冻结**:turn-1/turn-2 一见 `⟦`(U+27E6)停流,只下发净前缀(frozenN/sentN/streamedN)。
- **verdict2 三态收口**(finishContinuation):`kind:'call'`→兜底续链发 function_call+forget;
  `kind:'text'`→剥净 prose;`null`→原样交付(back-compat)。
- **finishWithCall**:⟦cmd¦run⟧ 兜底轮发 message+function_call+completed,**不 registerCallId**
  → turn-2 走常规 tool-feed(byCallId 有意 miss → 降级消化)。
- **ticketText steer 安全红线**:仅 `callId==null`(无活跃桥)才 steer;真超时(callId 非空=inject 已发)
  保"稍后重试",绝不 steer(杜绝双执行)。

### 离线覆盖
- 新增 `scripts/zk-cursor-web/mcp_bridge_v2_offline_cases.js` **13/13**(冻结/verdict2 三态/finishPlainText
  reconcile/finishWithCall 不注册/ticketText steer 红线,注入双时钟 rendezvous.now+now)。
- 新增 `scripts/zk-cursor-web/mcp_bridge_v2_responses_cases.js` **43/43**(V2B 三常量/9 双通道不变量/
  proto2 5 装配选择器/seam3 三态收口/turn-2 续流/steer 条件/acquire 门/rebuild 选择器)。
- 全 16 套件回归 BATTERY_FAIL=0。

### 部署(deploy-source = CM live bytes;repo copy STALE)
- responses.js → md5 `0b665ea1`;`routes/mcp_bridge.js`(tracked repo 源)→ md5 `c7463c8c`。
- 备份 `/home/cltx/backups-bpi/*.20260828-224031.pre-bridgev2.*`(responses `ff57424c` / mcp_bridge `41b19c39`)。
- **Stage 1**(字节,gate OFF):CM merge patch → rollout 82 → 活字节 `0b665ea1`,ZK_MCP_BRIDGE unset,
  boot 干净,101 未动 = 行为惰性(全部桥码在 `isEnabled()=false` 后)。
- **Stage 2**(翻闸):`set env ZK_MCP_BRIDGE=1 ZK_MCP_PUBLIC_URL=…zkmcp-95aeab5c… ZK_MCP_ACCOUNT=acct82
  ZK_MCP_GRACE_MS=30000`(单次 rollout)→ boot:`[conn-mgr] wired`/`[mcp-bridge] /mcp mounted`/
  `recoverOrphans verified404=true`。

### Stage 2 验收(gate ON,scoped 3h key 用完即删)
| 闸 | 结果 |
|----|------|
| gate-on 首轮(真 Cursor 载荷) | ✅ 3/3 非空 |
| FALLBACK ⟦cmd¦run⟧ → Shell function_call | ✅ 3/3(`turn-1 fallback ⟦cmd⟧ -> function_call Shell, normal-flow turn-2, no rendezvous`) |
| **事故回归:降级 → 逐字 URL、零编造** | ✅ 喂 `REGRESSIONPROBE_A1B2C3D4` → turn-2 `[turn-verdict-v2] complete-prose` 回逐字链接、`HAS_VERBATIM_URL=True` |
| 连接器 teardown verified404(每会话 link/unlink) | ✅ `teardown … verified404=true`,窗口外零连接器 |
| s3h 8/8(101 对照 + 82 桥) | ✅ PASS |
| codex 物理隔离(101 pod 早于 rollout + 活 md5 ≠ 本线) | ✅ 101 startTime 2026-08-27、md5 `2f94ed79` |

### 唯一未闭合(如实标注)
**rendezvous PREFERRED 路(模型调连接器 shell → 同轮续流)在合成验收里从未 fire**——模型每次走 ⟦cmd¦run⟧
FALLBACK。此路只能打真 Cursor 客户端证(合成 probe = 假绿,memory 判据)。但 FALLBACK+降级路 = 真实生产路
(真 Cursor 发自己的原生工具、不调 web 连接器),正是上次回归点、现已实证降级安全。gate 留 ON 在 canary-82
soak,真客户端 rendezvous 活体证 = soak 观察项。回滚:`set env ZK_MCP_BRIDGE-` + rollout;或 CM 从
pre-bridgev2 bak 还原。

## 【2026-08-28 深夜】empty-retry 空轮梯子 + rendezvous PREFERRED 模型侧首 fire

**病根(用户实锤 15:12:10→15:12:28)**:gate-ON 后 web 任务轮全走 seam3,而 seam3 的 turn-1
真空分支此前直达 `finishPlainText('本轮上游未产出内容')`——**没接老重发网**(非桥 violation 路
有同会话重发预算 1)。单次尝试 18s 即报错,窗口内 5 次空轮全此形态。

**修(commit dda731d,CM `b9fc7d16`,bak `pre-emptyretry` 旧 `0b665ea1`)**:`ZK_EMPTY_RETRY=N`
(默认 0=关,82 上 =2)fresh-conv 全量重掷梯子——同会话重发对空轮实测无效(两例死轮重发同样
200+零正文),换新会话才是换信号。首轮逐字节复用已装配 prompt;增量轮全量 flatten 重建+剥
execenv+桥感知契约(同 conv 失效重建路公式)。救回按 `_bridgeVerdict` 三态收口 +
`saveConvSession` 收养新会话(r15);violation 路同会话重发后仍空同样重掷(共享 `emptyRerolls`
计数跨 finish() 重入有界)。守卫:`res.writableEnded` 弃单即停;心跳 `: empty-retry` 2s 帧。

**验收**:离线 empty_retry 29/29 + 控制组未改字节 20 FAIL;全 16 套件 BATTERY_FAIL=0;
首轮探针 6/6 非空;降级链 turn-2 逐字消化零编造;s3h 8/8;101 未动(startTime 08-27 不变)。
天然空轮未在验收窗口出现→梯子 live-fire 归 soak(`[empty-retry]` 行可 grep 统计)。

**重大观测**:`[mcp-bridge] turn-1 injected call_id=e8b100d3…/c832f2e3…`(15:32:33/15:33:15)
——V2B 契约下**模型自发选择调 MCP 连接器**,rendezvous PREFERRED 路模型侧首次真实 fire
(~2/11 轮,随机)。此前合成验收它从未 fire(模型总走 ⟦cmd⟧ 兜底)。完整会合(注入→真
Cursor 执行→turn-2 byCallId HIT→同轮续流)仍待真客户端,但"模型会不会选这条路"已有肯定答案。

## 【2026-08-29 凌晨】conn-watchdog:injected 轮引用泄漏(PREFERRED 首 fire 即暴露)

**实锤**:PREFERRED 首 fire ×2 的那个 pod,conn-mgr 全窗口 1 provisioned / 0 release /
0 teardown,连接器存活 40min 不死。机制:seam3 injected 提前 return 不 release(设计上等
turn-2 HIT 路配平),turn-2 永不来(弃单/探针不喂)→ refCount 永卡 ≥1 → 违"用完即拆",
orphan 只有 pod 重启 recoverOrphans 才清。真 Cursor 流量 turn-2 正常会来,泄漏只在弃单轮
——但弃单轮真实存在,不能赌。

**修(commit 0231b83,CM `bcd3d4b4`,bak `pre-connwatchdog` 旧 `b9fc7d16`)**:turn-1
injected 在 ctx 挂幂等 `connRelease` + 看门狗 setTimeout(`ZK_MCP_REL_WATCHDOG_MS` 默认
10min/下限 60s/unref);turn-2 HIT 走同一幂等出口,先到先放不双放;非 injected 路原样。
看门狗放掉后连接器 grace→拆,后续轮自动重 provision,晚到的 turn-2 byCallId miss → 降级网
兜住(不悬挂)。

**验收**:离线 conn_watchdog 15/15 + 控制组 8 FAIL;全套 BATTERY_FAIL=0;boot recoverOrphans
删掉泄漏连接器 verified404=true;探针后 release→grace 恢复配平。s3h 两跑 flake 判定:82
tool.r2 首跑 hollow 二跑 508c OK,**对照组 101(未改字节)二跑同病 133c** = 上游惜字波动
非本字节。同窗 empty-retry 梯子 live-fire 双向实锤:rescued attempt 2 → function_call
(原本是用户可见报错);一例三连空仍诚实报错(当前窗口 acct82 空率 ≈71%)→ `ZK_EMPTY_RETRY=3`。
