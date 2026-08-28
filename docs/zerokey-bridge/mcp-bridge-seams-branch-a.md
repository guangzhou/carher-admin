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
| responses.js | `responses.js` | `2f94ed79` | `39a08ba4` | CM(共享)+ 仅 82 lane 生效(101 容错 stub+无 env) |
| zerokey-serve-codex.js | `zerokey-serve-codex.js` | `d256ecaf` | `8f28510e` | 同上 |
| mcp_bridge.js | `mcp_bridge.js`(**新增 key**) | —(新) | 见仓库 | 仅 `zero-cursor-bpi-82` Deployment args 加 `cp /patch/mcp_bridge.js /app/routes/mcp_bridge.js` |

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

1. 本地/staging 字节 md5 校验(responses `39a08ba4` / serve `8f28510e`)。
2. 备份现 CM 三键到 `/home/cltx/backups-bpi/`。
3. `kubectl patch cm zk-cursor-bpi-patch --type merge --patch-file <file in /home/cltx/>`:更新 responses.js +
   zerokey-serve-codex.js,新增 mcp_bridge.js。(不重启在跑 pod。)
4. `kubectl patch deploy zero-cursor-bpi-82`:args 加 `cp /patch/mcp_bridge.js /app/routes/mcp_bridge.js`
   (**ZK_MCP_BRIDGE 先不设**)→ rollout restart+status **仅 82** → 验 82 健康 + 行为等同(gate off)。
5. 再 patch 82 env 加 `ZK_MCP_BRIDGE=1` → rollout → 集成测(acct-93 端到端 / acct-82 canary,连接器只在
   活动窗口 link、用完 unlink 复验 404)。
6. 回滚:82 env 去 `ZK_MCP_BRIDGE` / args 去 cp 行 + CM 从 `/home/cltx/backups-bpi/` 还原。101 全程不动。
