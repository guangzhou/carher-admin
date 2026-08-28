'use strict';
/*
 * mcp_bridge_offline_cases.js — mcp_bridge.js 离线单测(零网络、注入时钟、fake sink)。
 * 证:①内联三承重件未从 33/33+25/25+17/17 漂移;②完整 Branch-A 缝合链
 * (turn-1 流→inject 发 function_call+completed→turn-2 续流→complete 唤醒 /mcp→收口);
 * ③降级(无活跃桥取号 / sweep 超时取号);④findFunctionCallOutput;⑤默认关。
 * fake sink 解析出 [{event,data}] 序列,逐事件断言形状。
 */
const assert = require('assert');
const path = require('path');
const M = require(path.join(__dirname, '..', 'chatgpt-onboard', 'zerokey-codex', 'zerokey-patch', 'routes', 'mcp_bridge.js'));

let pass = 0, fail = 0;
function t(name, fn) {
  try { fn(); console.log('  ok  ' + name); pass++; }
  catch (e) { console.log('  FAIL ' + name + '  :: ' + (e && e.message || e)); fail++; }
}

// fake sink:捕获 SSE 字节,解析成事件序列。
function mkSink() {
  const chunks = [];
  let ended = false;
  return {
    write: (s) => { chunks.push(s); },
    end: () => { ended = true; },
    _ended: () => ended,
    events: () => chunks.join('').split('\n\n').filter(Boolean).map((blk) => {
      const ev = (blk.match(/^event: (.+)$/m) || [])[1];
      const dm = blk.match(/^data: (.+)$/m);
      return { event: ev, data: dm ? JSON.parse(dm[1]) : null };
    }),
    names: function () { return this.events().map((e) => e.event); },
  };
}

// ── ① 内联件未漂移 ───────────────────────────────────────────────
t('deriveCallId 稳定 + args 顺序无关(canonical)', () => {
  const a = M.deriveCallId('s1', 'exec', { b: 2, a: 1 });
  const b = M.deriveCallId('s1', 'exec', { a: 1, b: 2 });
  assert.strictEqual(a, b, 'key 顺序应无关');
  assert.strictEqual(a.length, 16);
  assert.notStrictEqual(a, M.deriveCallId('s2', 'exec', { a: 1, b: 2 }), 'session 变则键变');
  assert.notStrictEqual(a, M.deriveCallId('s1', 'read', { a: 1, b: 2 }), 'tool 变则键变');
});
t('deriveCallId 不是 JSON-RPC id(连接内计数器不能当键)', () => {
  // 同 (session,tool,args) 恒得同键,与任何 id 无关。
  assert.strictEqual(M.deriveCallId('x', 'y', { z: 1 }), M.deriveCallId('x', 'y', { z: 1 }));
});
t('RendezvousTable: PENDING→complete→RESOLVED resolve result', async () => {
  const rv = new M.RendezvousTable({ now: () => 1000 });
  const d = rv.deliver('c1', 's', 'exec', { a: 1 });
  assert.strictEqual(d.isNew, true);
  rv.markInjected('c1');
  rv.complete('c1', 'OUTPUT');
  const r = await d.promise;
  assert.deepStrictEqual(r, { status: 'result', output: 'OUTPUT' });
  assert.deepStrictEqual(rv.poll('c1'), { status: 'result', output: 'OUTPUT' });
});
t('RendezvousTable: 幂等 deliver(retry-storm 同 callId 不重复注入)', () => {
  const rv = new M.RendezvousTable({ now: () => 1000 });
  const d1 = rv.deliver('c1', 's', 'exec', {});
  const d2 = rv.deliver('c1', 's', 'exec', {});
  assert.strictEqual(d1.isNew, true);
  assert.strictEqual(d2.isNew, false);
  assert.strictEqual(d2.dedup, true);
  assert.strictEqual(d1.promise, d2.promise, '复用同一 promise');
});
t('RendezvousTable: sweep 超时 PENDING→TICKETED resolve ticket', async () => {
  let now = 1000;
  const rv = new M.RendezvousTable({ ticketMs: 30000, now: () => now });
  const d = rv.deliver('c1', 's', 'exec', {});
  now = 1000 + 30000;
  rv.sweep(now);
  const r = await d.promise;
  assert.strictEqual(r.status, 'ticket');
  assert.strictEqual(r.callId, 'c1');
  assert.strictEqual(rv.poll('c1').status, 'ticket');
});
t('RendezvousTable: ticket 后 complete 只 stash 不双 resolve', async () => {
  let now = 1000;
  const rv = new M.RendezvousTable({ ticketMs: 30000, now: () => now });
  const d = rv.deliver('c1', 's', 'exec', {});
  now += 30000; rv.sweep(now);
  await d.promise; // ticket
  const ok = rv.complete('c1', 'LATE');
  assert.strictEqual(ok, true);
  assert.deepStrictEqual(rv.poll('c1'), { status: 'result', output: 'LATE' });
});
t('RendezvousTable: sweep 过 ttl 删除', () => {
  let now = 1000;
  const rv = new M.RendezvousTable({ ticketMs: 10, ttlMs: 100, now: () => now });
  rv.deliver('c1', 's', 'exec', {});
  now += 10; rv.sweep(now);   // → TICKETED
  now += 100; rv.sweep(now);  // 过 ttl → 删
  assert.strictEqual(rv.poll('c1').status, 'unknown');
});
t('dispatch: initialize / tools/list / notifications(202) / unknown(-32601)', async () => {
  const ctx = { tools: [{ name: 'exec', description: 'run', inputSchema: {} }], callTool: async () => ({ text: 'x' }) };
  const init = await M.dispatch({ jsonrpc: '2.0', id: 1, method: 'initialize' }, ctx);
  assert.strictEqual(init.json.result.protocolVersion, M.PROTOCOL_VERSION_DEFAULT);
  assert.deepStrictEqual(init.json.result.capabilities, { tools: {} });
  const list = await M.dispatch({ jsonrpc: '2.0', id: 2, method: 'tools/list' }, ctx);
  assert.strictEqual(list.json.result.tools.length, 1);
  const notif = await M.dispatch({ jsonrpc: '2.0', method: 'notifications/initialized' }, ctx);
  assert.strictEqual(notif.notification, true);
  assert.strictEqual(notif.status, 202);
  const unk = await M.dispatch({ jsonrpc: '2.0', id: 3, method: 'server/discover' }, ctx);
  assert.strictEqual(unk.json.error.code, -32601);
});
t('dispatch: tools/call 包 {content:[{type:text}]} + isError 透传', async () => {
  const ctx = { callTool: async (n, a) => ({ text: 'echo:' + a.text, isError: false }) };
  const r = await M.dispatch({ jsonrpc: '2.0', id: 5, method: 'tools/call', params: { name: 'echo', arguments: { text: 'hi' } } }, ctx);
  assert.deepStrictEqual(r.json.result.content, [{ type: 'text', text: 'echo:hi' }]);
  assert.strictEqual(r.json.result.isError, undefined);
  const ctx2 = { callTool: async () => ({ text: 'boom', isError: true }) };
  const r2 = await M.dispatch({ jsonrpc: '2.0', id: 6, method: 'tools/call', params: { name: 'x', arguments: {} } }, ctx2);
  assert.strictEqual(r2.json.result.isError, true);
});

// ── ② 完整 Branch-A 缝合链 ──────────────────────────────────────
t('端到端:turn-1 流 pre-tool → inject 发 function_call+completed → turn-2 续流收口', async () => {
  let now = 1000;
  const reg = new M.BridgeRegistry({ now: () => now });
  const sink1 = mkSink();
  const ctx = reg.openTurn({ session: 'convA', sink: sink1, respId: 'resp_1', created: 111, model: 'gpt-5-6' });
  assert.strictEqual(reg.pickActiveBridge(), ctx);

  // turn-1 pre-tool 正文(模拟上游 read 回调)
  const cb = reg.makeReadCallbacks(ctx);
  cb.onData({ v: { message: { author: { role: 'assistant' }, content: { content_type: 'text' } } } }); // 设 visible
  cb.onData({ p: '/message/content/parts/0', o: 'append', v: 'let me check. ' });
  let n1 = sink1.names();
  assert.ok(n1.includes('response.output_item.added'), '开 message item');
  assert.ok(n1.includes('response.output_text.delta'), 'pre-tool delta 上屏');

  // /mcp 命中:callTool → inject
  const callTool = reg.makeCallTool();
  const callPromise = callTool('shell', { cmd: 'ls /tmp' });
  await new Promise((r) => setImmediate(r)); // 让 inject 同步跑完
  assert.strictEqual(ctx.injected, true);
  const callId = ctx.callId;
  assert.ok(callId && callId.length === 16);
  // turn-1 sink:收口 message + function_call + completed + end
  n1 = sink1.names();
  assert.ok(n1.includes('response.function_call_arguments.done'), '发 function_call');
  const fcEv = sink1.events().find((e) => e.event === 'response.output_item.done' && e.data.item && e.data.item.type === 'function_call');
  assert.ok(fcEv, 'function_call item.done');
  assert.strictEqual(fcEv.data.item.call_id, callId, 'Responses call_id == 会合键(闭环靠它穿过 Cursor)');
  assert.strictEqual(fcEv.data.item.name, 'shell');
  assert.strictEqual(fcEv.data.item.arguments, JSON.stringify({ cmd: 'ls /tmp' }));
  assert.ok(sink1._ended(), 'turn-1 res 关闭');
  const compEv = sink1.events().find((e) => e.event === 'response.completed');
  assert.strictEqual(compEv.data.response.status, 'completed');
  // 登记 + 从活跃移除
  assert.strictEqual(reg.byCallId(callId), ctx);
  assert.strictEqual(reg.pickActiveBridge(), null, 'inject 后不再活跃');

  // turn-2:Cursor 回传 function_call_output → beginContinuation → complete 唤醒 /mcp
  const sink2 = mkSink();
  ctx.beginContinuation(sink2, { respId: 'resp_2', created: 222, model: 'gpt-5-6' });
  let n2 = sink2.names();
  assert.ok(n2.includes('response.created') && n2.includes('response.in_progress'), 'turn-2 新 envelope');
  reg.rendezvous.complete(callId, 'tmp\nfoo.txt');
  const callResult = await callPromise;
  assert.deepStrictEqual(callResult, { text: 'tmp\nfoo.txt' }, '/mcp 拿到真结果');

  // 上游续流 post-tool 正文
  cb.onData({ v: { message: { author: { role: 'assistant' }, content: { content_type: 'text' } } } });
  cb.onData({ p: '/message/content/parts/0', o: 'append', v: 'here are the files.' });
  cb.onData({ type: 'message_stream_complete' });
  n2 = sink2.names();
  const d2 = sink2.events().filter((e) => e.event === 'response.output_text.delta');
  assert.ok(d2.some((e) => e.data.delta === 'here are the files.'), 'post-tool delta 落 turn-2 sink');
  assert.ok(sink2._ended(), 'turn-2 res 收口');
  const comp2 = sink2.events().find((e) => e.event === 'response.completed');
  assert.strictEqual(comp2.data.response.output[0].content[0].text, 'here are the files.');
  assert.strictEqual(reg.byCallId(callId), null, 'forget 清理');
});

t('普通答:模型没调工具 → finishPlain 交付 turn-1 全文', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink, respId: 'r', created: 1, model: 'm' });
  const cb = reg.makeReadCallbacks(ctx);
  cb.onData({ v: { message: { author: { role: 'assistant' }, content: { content_type: 'text' } } } });
  cb.onData({ p: '/message/content/parts/0', o: 'append', v: 'Paris.' });
  cb.onData({ type: 'message_stream_complete' });
  // race 后 responses.js 调 finishPlain
  ctx.finishPlain();
  const comp = sink.events().find((e) => e.event === 'response.completed');
  assert.strictEqual(comp.data.response.output[0].content[0].text, 'Paris.');
  assert.strictEqual(ctx.injected, false);
  assert.ok(sink._ended());
});

t('injectedPromise 在 inject 时 resolve(turn-1 race 依据)', async () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const ctx = reg.openTurn({ session: 'c', sink: mkSink(), respId: 'r', created: 1, model: 'm' });
  let resolved = false;
  ctx.injectedPromise.then(() => { resolved = true; });
  reg.makeCallTool()('shell', { cmd: 'x' });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(resolved, true);
});

t('toolName 映射:openTurn 带 toolName → function_call.name 用它,callId 仍按 MCP 名', async () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  // MCP 工具名 'shell';Cursor 侧执行器名 'run_terminal_cmd'
  const ctx = reg.openTurn({ session: 'convX', sink, respId: 'r', created: 1, model: 'm', toolName: 'run_terminal_cmd' });
  reg.makeCallTool()('shell', { cmd: 'ls' });
  await new Promise((r) => setImmediate(r));
  const fcEv = sink.events().find((e) => e.event === 'response.output_item.done' && e.data.item && e.data.item.type === 'function_call');
  assert.ok(fcEv, 'function_call 已发');
  assert.strictEqual(fcEv.data.item.name, 'run_terminal_cmd', 'name 用 Cursor 执行器名');
  // callId 仍是 deriveCallId(session, MCP名, args),与 toolName 正交
  assert.strictEqual(ctx.callId, M.deriveCallId('convX', 'shell', { cmd: 'ls' }), 'callId 按 MCP 名');
});

// ── ②b arg 形状 remap(坑#1:MCP {cmd} → Cursor {command}) ──────────
t('remapArgsForCursor:有 toolParam → cmd 映射到 command,可选字段透传,callId 与其正交', () => {
  // 纯函数:MCP {cmd} → Cursor {command}
  assert.deepStrictEqual(M.remapArgsForCursor({ cmd: 'ls' }, 'command', false), { command: 'ls' });
  // 已是 command 形状 → 原样(优先 toolParam 键)
  assert.deepStrictEqual(M.remapArgsForCursor({ command: 'ls -la' }, 'command', false), { command: 'ls -la' });
  // 可选字段透传
  assert.deepStrictEqual(
    M.remapArgsForCursor({ cmd: 'ls', working_directory: '/tmp', block_until_ms: 5000 }, 'command', false),
    { command: 'ls', working_directory: '/tmp', block_until_ms: 5000 });
  // string args → parse 后映射
  assert.deepStrictEqual(M.remapArgsForCursor('{"cmd":"pwd"}', 'command', false), { command: 'pwd' });
  // array 型 command → 包成数组
  assert.deepStrictEqual(M.remapArgsForCursor({ cmd: 'ls' }, 'command', true), { command: ['ls'] });
  // 无 toolParam(离线/自控端)→ 原样透传(零行为差)
  assert.deepStrictEqual(M.remapArgsForCursor({ cmd: 'ls' }, null, false), { cmd: 'ls' });
  // 找不到命令字符串 → 透传不吞
  assert.deepStrictEqual(M.remapArgsForCursor({ x: 1 }, 'command', false), { x: 1 });
});

t('inject remap:openTurn 带 toolParam → 发给 Cursor 的 function_call.arguments 用 command', async () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  const ctx = reg.openTurn({ session: 'convR', sink, respId: 'r', created: 1, model: 'm',
    toolName: 'Shell', toolParam: 'command', toolParamArray: false });
  reg.makeCallTool()('shell', { cmd: 'ls -la' });
  await new Promise((r) => setImmediate(r));
  const fcEv = sink.events().find((e) => e.event === 'response.output_item.done' && e.data.item && e.data.item.type === 'function_call');
  assert.ok(fcEv, 'function_call 已发');
  assert.strictEqual(fcEv.data.item.name, 'Shell', 'name 用 Cursor 执行器名');
  assert.strictEqual(fcEv.data.item.arguments, JSON.stringify({ command: 'ls -la' }), 'args 已 remap 成 {command}');
  // callId 仍按 MCP 侧原始 args(deriveCallId 与 remap 正交)
  assert.strictEqual(ctx.callId, M.deriveCallId('convR', 'shell', { cmd: 'ls -la' }), 'callId 按 MCP 原始 args');
});

// ── ③ 降级 ────────────────────────────────────────────────────
t('降级:无活跃桥 → callTool 取号 isError', async () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const r = await reg.makeCallTool()('shell', { cmd: 'x' });
  assert.strictEqual(r.isError, true);
  assert.ok(/执行中|call_id/.test(r.text));
});
t('降级:Cursor 迟迟不回 → sweep 取号,/mcp 不吃满', async () => {
  let now = 1000;
  const reg = new M.BridgeRegistry({ rendezvous: { ticketMs: 30000 }, now: () => now });
  const ctx = reg.openTurn({ session: 'c', sink: mkSink(), respId: 'r', created: 1, model: 'm' });
  const p = reg.makeCallTool()('shell', { cmd: 'slow' });
  await new Promise((r) => setImmediate(r));
  now += 30000;
  reg.rendezvous.sweep(now);
  const r = await p;
  assert.strictEqual(r.isError, true, '取号降级');
  assert.ok(/call_id=/.test(r.text));
});

// ── ④ findFunctionCallOutput ──────────────────────────────────
t('findFunctionCallOutput:取最后一个,展平 output', () => {
  assert.strictEqual(M.findFunctionCallOutput([]), null);
  const a = M.findFunctionCallOutput([{ type: 'function_call_output', call_id: 'c1', output: 'RESULT' }]);
  assert.deepStrictEqual(a, { call_id: 'c1', output: 'RESULT' });
  const b = M.findFunctionCallOutput([
    { type: 'function_call_output', call_id: 'old', output: 'x' },
    { type: 'message', role: 'user', content: 'q' },
    { type: 'function_call_output', call_id: 'new', output: { text: 'Y' } },
  ]);
  assert.deepStrictEqual(b, { call_id: 'new', output: 'Y' });
  const c = M.findFunctionCallOutput([{ type: 'custom_tool_call_output', call_id: 'c', output: [{ text: 'A' }, { text: 'B' }] }]);
  assert.deepStrictEqual(c, { call_id: 'c', output: 'AB' });
});

// ── ⑤ 默认关 ──────────────────────────────────────────────────
t('默认关:未设 ZK_MCP_BRIDGE → isEnabled=false', () => {
  const saved = process.env.ZK_MCP_BRIDGE;
  delete process.env.ZK_MCP_BRIDGE;
  assert.strictEqual(M.isEnabled(), false);
  process.env.ZK_MCP_BRIDGE = '1';
  assert.strictEqual(M.isEnabled(), true);
  if (saved === undefined) delete process.env.ZK_MCP_BRIDGE; else process.env.ZK_MCP_BRIDGE = saved;
});

setTimeout(() => {
  console.log('\nmcp_bridge offline: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
}, 200);
