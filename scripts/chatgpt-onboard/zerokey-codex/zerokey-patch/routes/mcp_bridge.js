'use strict';
/*
 * mcp_bridge.js — MCP 会合桥 Branch-A 流缝合运行时(自包含,零外部依赖)。
 *
 * 为什么是一个文件:共享 CM `zk-cursor-bpi-patch` 被 26 个 pod 载,任何 hard require
 * 一个尚未 cp 的模块会让重启的 pod 崩。把会合表/JSON-RPC/胶水三件(各自离线已验)
 * 内联成一份、一个 CM key、一条 cp 行,并自带离线测试,消掉多文件漂移风险。
 * responses.js 侧只留两个极小、全程 ZK_MCP_BRIDGE 门控的缝合钩,所有 SSE 发射 + 跨轮
 * 状态都在本模块,逐字镜像 responses.js 现役事件形状(见 finishWebTools/wtOpen/onText)。
 *
 * 机制(第一性,由 Gate-1 实测数据钉死,非猜):模型原生调 MCP 连接器工具时会**卡在
 * turn 里**等我们 /mcp 的 HTTP 应答;工具真正执行在 Cursor 本机(turn-2),/mcp 只能在
 * turn-2 交回结果后才应答;模型的**工具后续答**又落在**同一条**已挂起的 f/conversation
 * SSE 上——所以网关必须持一个**跨 Cursor 轮存活**的上游 SSE reader,并把输出 sink 从
 * turn-1 socket 重指到 turn-2 socket。这就是 Branch A(§13.3 裁决)。
 *
 * 应答窗口硬顶(Gate-1):tool 结果须 ~45-50s 内回注,≥60s ChatGPT 放弃触发 retry-storm。
 * 故 /mcp 应答 ≤30s;~30s 没等到 Cursor output 立即走取号(ticket)降级。
 */

const crypto = require('crypto');

// ── 内联件 1/3:胶水(deriveCallId / canonical)—— 逐字自 mcp_bridge_glue.js(离线 17/17)──
// JSON-RPC id 是连接内计数器、retry-storm 里复用,不能当会合键;须稳定哈希。
function canonical(v) {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  const keys = Object.keys(v).sort();
  return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
}
function deriveCallId(session, tool, args) {
  const h = crypto.createHash('sha1');
  h.update(String(session == null ? '' : session));
  h.update('\0');
  h.update(String(tool == null ? '' : tool));
  h.update('\0');
  h.update(canonical(args === undefined ? null : args));
  return h.digest('hex').slice(0, 16);
}

// ── 内联件 2/3:会合表状态机 —— 逐字自 mcp_rendezvous.js(离线 33/33)──
// 三态 PENDING→TICKETED→RESOLVED;不内置 setTimeout,GC/取号靠外部 sweep(nowMs) 注入时钟。
const PENDING = 'pending';
const TICKETED = 'ticketed';
const RESOLVED = 'resolved';

class RendezvousTable {
  constructor(opts) {
    opts = opts || {};
    this.ticketMs = opts.ticketMs == null ? 30000 : opts.ticketMs;
    this.ttlMs = opts.ttlMs == null ? 300000 : opts.ttlMs;
    this._now = opts.now || (() => Date.now());
    this.map = new Map();
  }
  _mk(callId, session, tool, args, nowMs) {
    let resolveFn;
    const promise = new Promise((res) => { resolveFn = res; });
    const entry = {
      callId, session, tool, args,
      state: PENDING, createdAt: nowMs, injected: false,
      output: undefined, promise, _resolve: resolveFn,
    };
    this.map.set(callId, entry);
    return entry;
  }
  // /mcp 侧调:同 callId 幂等(ChatGPT 后端 ~60s retry-storm 复发同 call)。
  deliver(callId, session, tool, args) {
    const nowMs = this._now();
    let entry = this.map.get(callId);
    if (!entry) {
      entry = this._mk(callId, session, tool, args, nowMs);
      return { entry, promise: entry.promise, isNew: true, dedup: false };
    }
    // 已存在:RESOLVED 直接回结果;TICKETED/PENDING 复用同 promise,不重新注入。
    return { entry, promise: entry.promise, isNew: false, dedup: true };
  }
  markInjected(callId) {
    const e = this.map.get(callId);
    if (e) e.injected = true;
  }
  // Cursor turn-2 侧调:交回工具输出。PENDING→RESOLVED 并 resolve(唤醒挂起的 /mcp);
  // 已 TICKETED(超时降级过)→ 只 stash,不重复 resolve(ticket promise 已 resolve 过)。
  complete(callId, output) {
    const e = this.map.get(callId);
    if (!e) return false;
    e.output = output;
    if (e.state === PENDING) {
      e.state = RESOLVED;
      e._resolve({ status: 'result', output });
      return true;
    }
    if (e.state === TICKETED) {
      e.state = RESOLVED; // stash for later poll; ticket already delivered
      return true;
    }
    return false;
  }
  poll(callId) {
    const e = this.map.get(callId);
    if (!e) return { status: 'unknown' };
    if (e.state === RESOLVED) return { status: 'result', output: e.output };
    if (e.state === TICKETED) return { status: 'ticket' };
    return { status: 'pending' };
  }
  // 外部时钟:PENDING 过 ticketDeadline→TICKETED(resolve ticket,让 /mcp 降级返回);
  // RESOLVED/TICKETED 过 ttl→删。
  sweep(nowMs) {
    for (const [callId, e] of this.map) {
      if (e.state === PENDING && nowMs - e.createdAt >= this.ticketMs) {
        e.state = TICKETED;
        e._resolve({ status: 'ticket', callId });
      } else if ((e.state === RESOLVED || e.state === TICKETED) && nowMs - e.createdAt >= this.ttlMs) {
        this.map.delete(callId);
      }
    }
  }
}

// ── 内联件 3/3:JSON-RPC 派发器 —— 逐字自 mcp_jsonrpc.js(离线 25/25)──
const PROTOCOL_VERSION_DEFAULT = '2025-06-18';
function envelope(id, resultOrError) {
  const base = { jsonrpc: '2.0', id: id == null ? null : id };
  if (resultOrError && resultOrError.__error) {
    base.error = { code: resultOrError.code, message: resultOrError.message };
    if (resultOrError.data !== undefined) base.error.data = resultOrError.data;
  } else {
    base.result = resultOrError;
  }
  return base;
}
async function dispatch(body, ctx) {
  // notifications(无 id)→ HTTP 202,无 body。
  if (!body || body.id === undefined || body.id === null
      || (typeof body.method === 'string' && body.method.indexOf('notifications/') === 0)) {
    return { notification: true, status: 202 };
  }
  const id = body.id;
  const method = body.method;
  if (method === 'initialize') {
    return {
      status: 200,
      json: envelope(id, {
        protocolVersion: (ctx && ctx.protocolVersion) || PROTOCOL_VERSION_DEFAULT,
        capabilities: { tools: {} },
        serverInfo: (ctx && ctx.serverInfo) || { name: 'zk-mcp-bridge', version: '0.1.0' },
      }),
    };
  }
  if (method === 'tools/list') {
    return { status: 200, json: envelope(id, { tools: (ctx && ctx.tools) || [] }) };
  }
  if (method === 'tools/call') {
    const params = body.params || {};
    const name = params.name;
    const args = params.arguments || {};
    let out;
    try {
      out = await ctx.callTool(name, args);
    } catch (e) {
      return { status: 200, json: envelope(id, { __error: true, code: -32603, message: 'tool error: ' + (e && e.message || e) }) };
    }
    const content = [{ type: 'text', text: (out && out.text != null) ? String(out.text) : '' }];
    const result = { content };
    if (out && out.isError) result.isError = true;
    return { status: 200, json: envelope(id, result) };
  }
  // 未知方法(含 OpenAI 私有 server/discover)→ -32601(Phase0 实证可接受)。
  return { status: 200, json: envelope(id, { __error: true, code: -32601, message: 'method not found: ' + method }) };
}

// ── SSE 发射件:逐字镜像 responses.js 现役事件形状 ──────────────────────────────
// sink = { write(str), end() };offline 用捕获数组的 fake sink 断言逐字节。
function randHex(n) { return crypto.randomBytes(n).toString('hex'); }

function emitCreated(sink, env) {
  const { respId, created, model } = env;
  sink.write(`event: response.created\ndata: ${JSON.stringify({
    type: 'response.created',
    response: { id: respId, object: 'response', created_at: created, status: 'in_progress', model, output: [] },
  })}\n\n`);
  sink.write(`event: response.in_progress\ndata: ${JSON.stringify({
    type: 'response.in_progress',
    response: { id: respId, object: 'response', created_at: created, status: 'in_progress', model, output: [] },
  })}\n\n`);
}
function emitOpenMessage(sink, msgId) {
  const msgShell = { type: 'message', id: msgId, role: 'assistant', content: [], status: 'in_progress' };
  sink.write(`event: response.output_item.added\ndata: ${JSON.stringify({
    type: 'response.output_item.added', item: msgShell, output_index: 0,
  })}\n\n`);
  sink.write(`event: response.content_part.added\ndata: ${JSON.stringify({
    type: 'response.content_part.added', item_id: msgId,
    output_index: 0, content_index: 0, part: { type: 'output_text', text: '' },
  })}\n\n`);
}
function emitTextDelta(sink, msgId, delta, outputIndex) {
  const idx = outputIndex == null ? 0 : outputIndex;
  sink.write(`event: response.output_text.delta\ndata: ${JSON.stringify({
    type: 'response.output_text.delta', item_id: msgId,
    output_index: idx, content_index: 0, delta,
  })}\n\n`);
}
function emitCloseMessage(sink, msgId, text, outputIndex) {
  const idx = outputIndex == null ? 0 : outputIndex;
  sink.write(`event: response.output_text.done\ndata: ${JSON.stringify({
    type: 'response.output_text.done', item_id: msgId, output_index: idx, content_index: 0, text,
  })}\n\n`);
  sink.write(`event: response.content_part.done\ndata: ${JSON.stringify({
    type: 'response.content_part.done', item_id: msgId, output_index: idx,
    content_index: 0, part: { type: 'output_text', text },
  })}\n\n`);
  sink.write(`event: response.output_item.done\ndata: ${JSON.stringify({
    type: 'response.output_item.done', output_index: idx,
    item: { type: 'message', id: msgId, role: 'assistant', status: 'completed', content: [{ type: 'output_text', text }] },
  })}\n\n`);
}
function emitFunctionCall(sink, fc, outputIndex) {
  const idx = outputIndex == null ? 0 : outputIndex;
  const item = {
    type: 'function_call', id: fc.fcId, call_id: fc.callId,
    name: fc.name, arguments: fc.arguments, status: 'completed',
  };
  sink.write(`event: response.output_item.added\ndata: ${JSON.stringify({
    type: 'response.output_item.added', item, output_index: idx,
  })}\n\n`);
  sink.write(`event: response.function_call_arguments.delta\ndata: ${JSON.stringify({
    type: 'response.function_call_arguments.delta', item_id: item.id, output_index: idx, delta: item.arguments,
  })}\n\n`);
  sink.write(`event: response.function_call_arguments.done\ndata: ${JSON.stringify({
    type: 'response.function_call_arguments.done', item_id: item.id, output_index: idx, arguments: item.arguments,
  })}\n\n`);
  sink.write(`event: response.output_item.done\ndata: ${JSON.stringify({
    type: 'response.output_item.done', item, output_index: idx,
  })}\n\n`);
  return item;
}
function emitCompleted(sink, env, output) {
  const { respId, created, model, usage } = env;
  sink.write(`event: response.completed\ndata: ${JSON.stringify({
    type: 'response.completed',
    response: { id: respId, object: 'response', created_at: created, status: 'completed', model, output, usage: usage || {} },
  })}\n\n`);
  sink.end();
}

// ── 跨轮桥上下文 ────────────────────────────────────────────────────────────
// 一条 Cursor 会话的一次 MCP 缝合。phase: turn1 → suspended → turn2 → done。
// pre-tool 正文流到 turn-1 sink;工具后续答流到 turn-2 sink(sink 可重指针)。
class BridgeContext {
  constructor(opts) {
    this.registry = opts.registry;
    this.session = opts.session;
    this.sink = opts.sink;               // 当前输出 sink(turn-1 → turn-2 重指)
    this.env = { respId: opts.respId, created: opts.created, model: opts.model, usage: opts.usage };
    this.msgId = opts.msgId || ('msg_' + randHex(12));
    this.phase = 'turn1';
    this.msgOpen = false;
    this.injected = false;
    this.continued = false;
    this.callId = null;
    this.pendingCall = null;             // {name,args}
    this.streamed1 = '';                 // turn-1(pre-tool)已流文本
    this.streamed2 = '';                 // turn-2(post-tool)已流文本
    this.pending2 = '';                  // turn-2 sink 就绪前的缓冲
    this.upstreamDone = false;
    this.readerPromise = null;
    this.createdAt = (opts.now || (() => Date.now()))();
    let r; this.injectedPromise = new Promise((res) => { r = res; }); this._injectedResolve = r;
  }

  // 上游可见正文增量(由 responses.js 的 read 回调过滤后喂入)。
  onText(delta) {
    if (!delta) return;
    if (!this.injected) {
      // pre-tool:流到 turn-1 sink
      if (!this.msgOpen) { emitOpenMessage(this.sink, this.msgId); this.msgOpen = true; }
      this.streamed1 += delta;
      emitTextDelta(this.sink, this.msgId, delta, 0);
    } else if (this.phase === 'turn2') {
      // post-tool:流到 turn-2 sink
      this.streamed2 += delta;
      emitTextDelta(this.sink, this.msg2Id, delta, 0);
    } else {
      // suspended 且 turn-2 sink 未就绪:缓冲,beginContinuation 时冲刷
      this.pending2 += delta;
    }
  }

  markUpstreamDone() {
    this.upstreamDone = true;
    // turn-1 阶段流自然结束(模型没调工具)→ 由 responers.js race 后走 finishPlain。
    // turn-2 阶段结束 → 收口续流。
    if (this.phase === 'turn2' && !this._finished2) this.finishContinuation();
  }

  // /mcp 侧命中新调用时缝合:收口 turn-1 message、发 function_call、completed turn-1、
  // 把自己按 callId 登记供 turn-2 查、唤醒 turn-1 handler 的 race。turn-1 sink 随后 end。
  inject(callId, name, args) {
    if (this.injected) return;
    this.injected = true;
    this.phase = 'suspended';
    this.callId = callId;
    this.pendingCall = { name, args: (typeof args === 'string' ? args : JSON.stringify(args || {})) };

    const output = [];
    let idx = 0;
    if (this.msgOpen) {
      emitCloseMessage(this.sink, this.msgId, this.streamed1, 0);
      output.push({ type: 'message', id: this.msgId, role: 'assistant', status: 'completed',
        content: [{ type: 'output_text', text: this.streamed1 }] });
      idx = 1;
    }
    const fcId = 'fc_' + randHex(12);
    const fcItem = emitFunctionCall(this.sink, {
      fcId, callId, name, arguments: this.pendingCall.args,
    }, idx);
    output.push(fcItem);
    emitCompleted(this.sink, this.env, output);

    this.registry.registerCallId(callId, this);
    this.registry.deactivate(this);
    this._injectedResolve();
  }

  // turn-2:把续流指到新 sink,发 response.created/in_progress + 打开新 message item,
  // 冲刷挂起期缓冲的 post-tool 文本(正常情形 Gate-1 证续流在结果回注之后,缓冲为空)。
  beginContinuation(sink, env) {
    this.continued = true;
    this.phase = 'turn2';
    this.sink = sink;
    this.env = { respId: env.respId, created: env.created, model: env.model, usage: env.usage };
    this.msg2Id = 'msg_' + randHex(12);
    emitCreated(this.sink, this.env);
    emitOpenMessage(this.sink, this.msg2Id);
    if (this.pending2) {
      this.streamed2 += this.pending2;
      emitTextDelta(this.sink, this.msg2Id, this.pending2, 0);
      this.pending2 = '';
    }
  }

  finishContinuation() {
    if (this._finished2) return;
    this._finished2 = true;
    this.phase = 'done';
    emitCloseMessage(this.sink, this.msg2Id, this.streamed2, 0);
    emitCompleted(this.sink, this.env, [{
      type: 'message', id: this.msg2Id, role: 'assistant', status: 'completed',
      content: [{ type: 'output_text', text: this.streamed2 }],
    }]);
    this.registry.forget(this);
  }

  // 模型没调工具的普通答:turn-1 直接收口交付(由 responses.js 在 race 后调)。
  finishPlain() {
    if (this.injected || this._finishedPlain) return;
    this._finishedPlain = true;
    this.phase = 'done';
    if (!this.msgOpen) { emitOpenMessage(this.sink, this.msgId); this.msgOpen = true; }
    emitCloseMessage(this.sink, this.msgId, this.streamed1, 0);
    emitCompleted(this.sink, this.env, [{
      type: 'message', id: this.msgId, role: 'assistant', status: 'completed',
      content: [{ type: 'output_text', text: this.streamed1 }],
    }]);
    this.registry.deactivate(this);
  }
}

// ── 桥注册表(单例)───────────────────────────────────────────────────────────
class BridgeRegistry {
  constructor(opts) {
    opts = opts || {};
    this.rendezvous = new RendezvousTable(opts.rendezvous || {});
    this._now = opts.now || (() => Date.now());
    this.active = [];               // 已开、尚未 inject/finish 的桥(pickActiveBridge 取最近)
    this.byId = new Map();          // callId → ctx(inject 后、turn-2 查)
    this.ticketText = opts.ticketText || ((callId) => '执行中(call_id=' + callId + '),稍后重试。');
  }
  openTurn(opts) {
    const ctx = new BridgeContext(Object.assign({ registry: this, now: this._now }, opts));
    this.active.push(ctx);
    return ctx;
  }
  // §5.1 方案B:单会话 canary,取最近一条仍挂起等 MCP 的桥。>1 记警。
  pickActiveBridge() {
    if (!this.active.length) return null;
    if (this.active.length > 1) {
      try { console.log('[mcp-bridge] WARN ' + this.active.length + ' active bridges; picking most-recent (方案B single-session assumption)'); } catch (_) {}
    }
    return this.active[this.active.length - 1];
  }
  deactivate(ctx) {
    const i = this.active.indexOf(ctx);
    if (i >= 0) this.active.splice(i, 1);
  }
  registerCallId(callId, ctx) { this.byId.set(callId, ctx); }
  byCallId(callId) { return this.byId.get(callId) || null; }
  forget(ctx) {
    if (ctx.callId) this.byId.delete(ctx.callId);
    this.deactivate(ctx);
  }
  // /mcp tools/call 的 callTool:命中活跃桥→deliver→isNew 时 inject→await 结果/取号。
  makeCallTool() {
    const self = this;
    return async function callTool(name, args) {
      const ctx = self.pickActiveBridge();
      if (!ctx) {
        // 无活跃桥挂起:无法缝合,取号降级(无 callId)。
        return { text: self.ticketText(null), isError: true };
      }
      const session = ctx.session;
      const callId = deriveCallId(session, name, args);
      const d = self.rendezvous.deliver(callId, session, name, args);
      if (d.isNew) {
        try {
          ctx.inject(callId, name, args);
          self.rendezvous.markInjected(callId);
        } catch (e) {
          // 断注入容错:留 PENDING,由 sweep 取号降级。
          try { console.log('[mcp-bridge] inject error: ' + (e && e.message || e)); } catch (_) {}
        }
      }
      const r = await d.promise;
      if (r && r.status === 'result') return { text: String(r.output) };
      return { text: self.ticketText(r && r.callId || callId), isError: true };
    };
  }
  makeReadCallbacks(ctx) {
    // 精简可见性判据(与主 drain L1811-1824 同):assistant 角色 + recipient all/空 + content_type≠code。
    return {
      onData: (d) => {
        if (!d) return;
        const mm = (d.v && d.v.message) || d.message;
        let visible = true;
        if (mm && mm.author) {
          const rc = mm.recipient || (mm.author && mm.author.recipient) || '';
          const ct = mm.content && mm.content.content_type;
          visible = mm.author.role === 'assistant' && (!rc || rc === 'all') && ct !== 'code';
        }
        if (d.p === '/message/content/parts/0' && d.o === 'append') {
          if (visible) ctx.onText(d.v);
          return;
        }
        if (typeof d.v === 'string' && !d.o && !d.p) {
          if (visible) ctx.onText(d.v);
          return;
        }
        if (d.o === 'patch' && Array.isArray(d.v)) {
          for (const op of d.v) {
            if (op.p === '/message/content/parts/0' && op.o === 'append' && visible) ctx.onText(op.v);
          }
        }
        if (d.type === 'message_stream_complete') ctx.markUpstreamDone();
      },
      onDone: () => ctx.markUpstreamDone(),
      onError: () => ctx.markUpstreamDone(),
      isDone: () => ctx.upstreamDone || (ctx.phase === 'done'),
    };
  }
}

// ── 单例 + /mcp 路由 + sweep ─────────────────────────────────────────────────
let _singleton = null;
let _sweepTimer = null;
function getRegistry() {
  if (!_singleton) _singleton = new BridgeRegistry({});
  return _singleton;
}
function isEnabled() { return process.env.ZK_MCP_BRIDGE === '1'; }

// turn-2 缝合钩:从 input 里找 function_call_output {call_id, output}(Cursor 回传的工具结果)。
function findFunctionCallOutput(input) {
  if (!Array.isArray(input)) return null;
  // 取最后一个 function_call_output(本轮新交回的)。
  for (let i = input.length - 1; i >= 0; i--) {
    const it = input[i];
    if (it && (it.type === 'function_call_output' || it.type === 'custom_tool_call_output')) {
      let out = it.output;
      if (out && typeof out === 'object') {
        if (Array.isArray(out)) out = out.map((p) => (p && (p.text || p.output)) || '').join('');
        else out = out.text != null ? out.text : JSON.stringify(out);
      }
      return { call_id: it.call_id, output: out == null ? '' : String(out) };
    }
  }
  return null;
}

// deps: { tools:[{name,description,inputSchema}], serverInfo, protocolVersion }
function buildMcpRoute(deps) {
  deps = deps || {};
  const reg = getRegistry();
  if (!_sweepTimer) {
    _sweepTimer = setInterval(() => { try { reg.rendezvous.sweep(Date.now()); } catch (_) {} }, 1000);
    if (_sweepTimer.unref) _sweepTimer.unref();
  }
  const ctxBase = {
    tools: deps.tools || [],
    serverInfo: deps.serverInfo || { name: 'zk-mcp-bridge', version: '0.1.0' },
    protocolVersion: deps.protocolVersion || PROTOCOL_VERSION_DEFAULT,
    callTool: reg.makeCallTool(),
  };
  // express-style (req,res)。req.body 若已被 body-parser 解析则用之,否则自行缓冲。
  return async function mcpRoute(req, res) {
    let body = req.body;
    if (body === undefined || body === null || (typeof body === 'object' && Object.keys(body).length === 0 && req.readable)) {
      body = await new Promise((resolve) => {
        let buf = '';
        req.on('data', (c) => { buf += c; });
        req.on('end', () => { try { resolve(JSON.parse(buf || '{}')); } catch (_) { resolve({}); } });
        req.on('error', () => resolve({}));
      });
    }
    let r;
    try {
      r = await dispatch(body, ctxBase);
    } catch (e) {
      res.status(500).json(envelope(body && body.id, { __error: true, code: -32603, message: 'internal: ' + (e && e.message || e) }));
      return;
    }
    if (r.notification) { res.status(r.status || 202).end(); return; }
    res.status(r.status || 200);
    if (res.setHeader) res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(r.json));
  };
}

module.exports = {
  // 门控 + 装配
  isEnabled, buildMcpRoute, getRegistry, findFunctionCallOutput,
  // 供 responses.js 缝合钩 + 离线测试
  BridgeRegistry, BridgeContext, RendezvousTable, deriveCallId, canonical, dispatch, envelope,
  emitCreated, emitOpenMessage, emitTextDelta, emitCloseMessage, emitFunctionCall, emitCompleted,
  PENDING, TICKETED, RESOLVED, PROTOCOL_VERSION_DEFAULT,
};
