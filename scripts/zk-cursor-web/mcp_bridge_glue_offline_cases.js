#!/usr/bin/env node
/* mcp_bridge_glue_offline_cases.js — 编排胶水离线单测(分支无关层)。
 *
 * require ./mcp_bridge_glue.js + ./mcp_rendezvous.js(注入确定性时钟)。
 * inject 用桩(记录调用次数+参数),验证:
 *   - deriveCallId 确定性 / args 键序无关 / session·tool·args 任一变则变(retry-storm 键稳定性)
 *   - callTool 新调用:inject 恰调 1 次,complete 后 await 到 result 文本
 *   - callTool ~60s 重发(同参,PENDING):inject **不**二次调用(去重),仍 await 到同结果
 *   - callTool 取号降级:sweep 过 ticketMs → 回 {isError:true, ticketText}
 *   - callTool broken-pipe 重发(RESOLVED):回 stash,inject 不再调用
 *   - inject 抛错(broken injection):callTool 不崩,靠 ticket 降级兜底返回
 *
 * 用法: node scripts/zk-cursor-web/mcp_bridge_glue_offline_cases.js
 */
'use strict';
const path = require('path');
const { deriveCallId, makeCallTool, canonical } = require(path.join(__dirname, 'mcp_bridge_glue.js'));
const { RendezvousTable } = require(path.join(__dirname, 'mcp_rendezvous.js'));

let pass = 0, fail = 0;
const fails = [];
function check(name, cond, why) {
  if (cond) { pass++; console.log('[PASS] ' + name); }
  else { fail++; fails.push({ name: name, why: why }); console.log('[FAIL] ' + name + ' — ' + why); }
}
function mkClock() { return { t: 1000 }; }
function mkRV(clk, opts) { return new RendezvousTable(Object.assign({ ticketMs: 30000, ttlMs: 300000, now: function () { return clk.t; } }, opts || {})); }

// —— deriveCallId 性质 ——
(function callIdProps() {
  const a = deriveCallId('s1', 'shell', { command: 'ls', cwd: '/x' });
  const b = deriveCallId('s1', 'shell', { cwd: '/x', command: 'ls' });   // 键序不同
  check('id-deterministic', a === deriveCallId('s1', 'shell', { command: 'ls', cwd: '/x' }), 'not deterministic');
  check('id-key-order-invariant', a === b, 'a=' + a + ' b=' + b + ' (键序应无关)');
  check('id-session-sensitive', a !== deriveCallId('s2', 'shell', { command: 'ls', cwd: '/x' }), 'session 变哈希未变');
  check('id-tool-sensitive', a !== deriveCallId('s1', 'read', { command: 'ls', cwd: '/x' }), 'tool 变哈希未变');
  check('id-args-sensitive', a !== deriveCallId('s1', 'shell', { command: 'pwd', cwd: '/x' }), 'args 变哈希未变');
  check('id-hex16', /^[0-9a-f]{16}$/.test(a), 'id=' + a);
  // 规范化:数组保序
  check('canonical-array-order', canonical([1, 2]) !== canonical([2, 1]), '数组顺序应有语义');
})();

// —— 新调用:inject 恰 1 次,complete → result ——
(async function newCall() {
  const clk = mkClock(); const rv = mkRV(clk);
  let injects = [];
  const callTool = makeCallTool({ rendezvous: rv, pickSession: function () { return 'sessA'; }, inject: function (s, id, t, a) { injects.push({ s: s, id: id, t: t, a: a }); } });
  const p = callTool('shell', { command: 'ls' }, { id: 0 });
  check('new-inject-once', injects.length === 1 && injects[0].t === 'shell' && injects[0].s === 'sessA', 'injects=' + JSON.stringify(injects));
  const cid = injects[0].id;
  check('new-injected-marked', rv.get(cid).injected === true, 'injected flag not set');
  rv.complete(cid, 'file1\nfile2');
  const r = await p;
  check('new-result-text', r.text === 'file1\nfile2' && !r.isError, 'r=' + JSON.stringify(r));
})();

// —— ~60s 重发(同参,PENDING):inject 不二次调用,仍解析到同结果 ——
(async function resendPending() {
  const clk = mkClock(); const rv = mkRV(clk);
  let injects = 0;
  const callTool = makeCallTool({ rendezvous: rv, pickSession: function () { return 'sessB'; }, inject: function () { injects++; } });
  const p1 = callTool('shell', { command: 'build' }, { id: 0 });
  const p2 = callTool('shell', { command: 'build' }, { id: 0 });   // ChatGPT ~60s 重发(JSON-RPC id 仍 0)
  check('resend-inject-once', injects === 1, 'inject called ' + injects + ' times (重发应去重不二次注入)');
  check('resend-size-one', rv.size() === 1, 'size=' + rv.size());
  const cid = deriveCallId('sessB', 'shell', { command: 'build' });
  rv.complete(cid, 'built ok');
  const r1 = await p1; const r2 = await p2;
  check('resend-both-resolve', r1.text === 'built ok' && r2.text === 'built ok', 'r1=' + JSON.stringify(r1) + ' r2=' + JSON.stringify(r2));
})();

// —— 取号降级:sweep 过 ticketMs → isError + ticketText ——
(async function ticketDegrade() {
  const clk = mkClock(); const rv = mkRV(clk, { ticketMs: 30000 });
  const callTool = makeCallTool({ rendezvous: rv, pickSession: function () { return 'sessC'; }, inject: function () {} });
  const p = callTool('shell', { command: 'huge-scan' }, { id: 0 });
  clk.t += 30000; rv.sweep(clk.t);
  const r = await p;
  const cid = deriveCallId('sessC', 'shell', { command: 'huge-scan' });
  check('ticket-isError', r.isError === true && r.text.indexOf(cid) >= 0 && /poll/.test(r.text), 'r=' + JSON.stringify(r));
})();

// —— broken-pipe 重发(RESOLVED):回 stash,inject 不再调用 ——
(async function brokenPipeResend() {
  const clk = mkClock(); const rv = mkRV(clk);
  let injects = 0;
  const callTool = makeCallTool({ rendezvous: rv, pickSession: function () { return 'sessD'; }, inject: function () { injects++; } });
  const p1 = callTool('shell', { command: 'echo hi' }, { id: 0 });
  const cid = deriveCallId('sessD', 'shell', { command: 'echo hi' });
  rv.complete(cid, 'hi');
  await p1;                                   // 首答(假设写回 broken-pipe,结果已 stash)
  const p2 = callTool('shell', { command: 'echo hi' }, { id: 0 });   // ChatGPT 重发
  check('bp-inject-once', injects === 1, 'inject called ' + injects + ' (RESOLVED 重发不应再注入)');
  const r2 = await p2;
  check('bp-stash-returned', r2.text === 'hi' && !r2.isError, 'r2=' + JSON.stringify(r2));
})();

// —— inject 抛错(broken injection):callTool 不崩,ticket 兜底 ——
(async function injectThrows() {
  const clk = mkClock(); const rv = mkRV(clk, { ticketMs: 30000 });
  const callTool = makeCallTool({ rendezvous: rv, pickSession: function () { return 'sessE'; }, inject: function () { throw new Error('cursor socket gone'); } });
  const p = callTool('shell', { command: 'x' }, { id: 0 });   // 不应抛
  clk.t += 30000; rv.sweep(clk.t);
  const r = await p;
  check('inject-throw-tolerated', r.isError === true && /poll/.test(r.text), 'r=' + JSON.stringify(r));
})();

setTimeout(function () {
  console.log('\n== ' + pass + '/' + (pass + fail) + ' PASS ==');
  if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1); }
  console.log('VERDICT: GO (callId 稳定/键序无关/敏感 + 新调用注入一次 + 重发去重不二次注入 + 取号降级 + broken-pipe 回 stash + inject 抛错容错,分支无关编排全绿)');
}, 100);
