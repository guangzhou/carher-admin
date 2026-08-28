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
| responses.js | `responses.js` | `2f94ed79` | `39a08ba4`(三缝合口)→ **`128e67e6`(+piece#3 桥模式契约)** | CM(共享)+ 仅 82 lane 生效(101 容错 stub+无 env) |
| zerokey-serve-codex.js | `zerokey-serve-codex.js` | `d256ecaf` | `8f28510e` | 同上 |
| mcp_bridge.js | `mcp_bridge.js`(**新增 key**) | —(新) | **活 md5 `e8b70ed9`**(21777B) | 仅 `zero-cursor-bpi-82` Deployment args 加 `cp /patch/mcp_bridge.js /app/routes/mcp_bridge.js` |

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

1. **参数形状不匹配(`cmd` vs `command`)**:serve `bridgeTools` 声明 `required:['cmd']`,模型按此发
   `{cmd:"ls"}`;桥 inject function_call `name=Shell args={"cmd":"ls"}` 给 Cursor,但 Cursor 真 `Shell`
   执行器读 `{command, working_directory, block_until_ms}` → 读 `command` 得 undefined。探针 harness 泛型解析
   所以过了,**真 Cursor 会断**。修:(a) MCP 工具 schema 直接用 `command` 参数对齐 Cursor,或
   (b) inject 时 remap `cmd→command`。
2. **codex-shared 账号面挂持久连接器 = Gate-2 一票否决**:acct-82/acct-93 都是 codex serve 池上游
   (`BRIDGE_UPSTREAMS` 含 zero-82 + zero-93)。用户已豁免 Gate-2 结构否决,但持久 account-level 连接器仍落
   codex 账号面 → 生产唯一安全形态是 **§5.1 方案 A(每会话 link/unlink,活动窗口内挂、用完即摘 + 复验 404)**,
   不留持久连接器。本次实测即遵此:provision→round-trip→立即 delete。
3. **codex 非回归验证受阻(诚实标注,非造绿)**:codex 池当前退化(429/401)+ 禁止加载 + 连接器已删(无可测)
   → 依据 Gate-2c 先证(基模不自发调未请求连接器)+ 瞬时足迹(~分钟级)+ 即时删除;**不宣称已跑绿**。

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

1. 本地/staging 字节 md5 校验(responses 活 `128e67e6`(含 piece #3)/ serve `8f28510e` / mcp_bridge.js 活 `e8b70ed9`)。
2. 备份现 CM 三键到 `/home/cltx/backups-bpi/`。
3. `kubectl patch cm zk-cursor-bpi-patch --type merge --patch-file <file in /home/cltx/>`:更新 responses.js +
   zerokey-serve-codex.js,新增 mcp_bridge.js。(不重启在跑 pod。)
4. `kubectl patch deploy zero-cursor-bpi-82`:args 加 `cp /patch/mcp_bridge.js /app/routes/mcp_bridge.js`
   (**ZK_MCP_BRIDGE 先不设**)→ rollout restart+status **仅 82** → 验 82 健康 + 行为等同(gate off)。
5. 再 patch 82 env 加 `ZK_MCP_BRIDGE=1` → rollout → 集成测(acct-93 端到端 / acct-82 canary,连接器只在
   活动窗口 link、用完 unlink 复验 404)。
6. 回滚:82 env 去 `ZK_MCP_BRIDGE` / args 去 cp 行 + CM 从 `/home/cltx/backups-bpi/` 还原。101 全程不动。
