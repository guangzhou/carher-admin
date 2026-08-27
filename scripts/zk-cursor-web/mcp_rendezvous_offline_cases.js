#!/usr/bin/env node
/* mcp_rendezvous_offline_cases.js — 会合表状态机离线单测(Phase 1 承重原语)。
 *
 * 直接 require ./mcp_rendezvous.js(本模块是新写的自含件,非从 responses.js 抠;验绿后再嫁接)。
 * 注入确定性时钟:clock.t 手推,now=()=>clock.t,sweep(clock.t) 显式打点,无任何 wall-clock sleep。
 *
 * 覆盖(逐条对齐设计文档 §12 四修正 + 基础生命周期):
 *   基础 A  正常一次:deliver(new)→isNew=true→complete→挂起 promise 得 result,原样透传
 *   基础 B  poll:resolved 回结果 / pending 未落 / unknown 不认识
 *   修正1 C  幂等去重:同 call_id 重发命中 PENDING → isNew=false 且复用同一 promise,不二次注入
 *   修正1 D  两个 await 者(原发+重发)在 complete 时一起 resolve 到同一结果
 *   修正2 E  取号降级:PENDING 过 ticketMs 未回 → sweep 把挂起 promise settle 成 ticket
 *   修正2 F  取号后 output 迟到 → 落 stash,poll 能取到(不重复 settle 已 ticket 的 promise)
 *   修正2 G  取号后重发同 call_id → 回 ticket(不新建注入)
 *   修正4 H  broken-pipe:complete 已 resolve+stash,重发命中 RESOLVED → 立刻回 stash 结果(不二次注入)
 *   透传 I  URL/长文本 output 原样穿透(修正3:MCP 路不做 url-safe 改写)
 *   孤儿 J  complete 一个不存在的 call_id → 返 false 记 orphan,不崩
 *   GC   K  resolved/ticketed 过 ttl 被 sweep 回收;PENDING 不被 ttl 误收(只受 ticket 迁移)
 *   会话 L  entry 记录 session(配对方案 B 用),不同 session 各自独立
 *   计数 M  stats 计数自洽(delivered/deduped/resolved/ticketed/orphanOutputs/gc)
 *
 * 用法: node scripts/zk-cursor-web/mcp_rendezvous_offline_cases.js
 */
'use strict';
const path = require('path');
const { RendezvousTable, PENDING, TICKETED, RESOLVED } = require(path.join(__dirname, 'mcp_rendezvous.js'));

let pass = 0, fail = 0;
const fails = [];
function check(name, cond, why) {
  if (cond) { pass++; console.log('[PASS] ' + name); }
  else { fail++; fails.push({ name: name, why: why }); console.log('[FAIL] ' + name + ' — ' + why); }
}

// 确定性时钟:所有表共享一个可推进的 t
function mkClock() { return { t: 1000 }; }
function mkTable(clock, opts) {
  opts = Object.assign({ ticketMs: 30000, ttlMs: 300000, now: function () { return clock.t; } }, opts || {});
  return new RendezvousTable(opts);
}

// —— 基础 A:正常一次调用 ——
(async function baseNormal() {
  const clk = mkClock(); const tb = mkTable(clk);
  const d = tb.deliver('c1', 'sess-A', 'shell', { command: 'ls' });
  check('A-isNew', d.isNew === true && d.dedup === null, 'first deliver should be new; got isNew=' + d.isNew);
  check('A-pending-state', tb.get('c1').state === PENDING, 'state=' + tb.get('c1').state);
  const matched = tb.complete('c1', 'file1\nfile2\n(exit 0)');
  check('A-complete-matched', matched === true, 'complete not matched');
  const r = await d.promise;
  check('A-result', r.status === 'result' && r.output === 'file1\nfile2\n(exit 0)' && r.callId === 'c1', 'r=' + JSON.stringify(r));
  check('A-resolved-state', tb.get('c1').state === RESOLVED, 'state=' + tb.get('c1').state);
})();

// —— 基础 B:poll 三态 ——
(async function pollStates() {
  const clk = mkClock(); const tb = mkTable(clk);
  check('B-unknown', tb.poll('nope').status === 'unknown', 'poll unknown');
  tb.deliver('c2', 'sess-B', 'shell', { command: 'sleep' });
  check('B-pending', tb.poll('c2').status === 'pending', 'poll pending');
  tb.complete('c2', 'done');
  const p = tb.poll('c2');
  check('B-result', p.status === 'result' && p.output === 'done', 'poll result p=' + JSON.stringify(p));
})();

// —— 修正1 C:幂等去重,重发命中 PENDING ——
(async function dedupPending() {
  const clk = mkClock(); const tb = mkTable(clk);
  const d1 = tb.deliver('c3', 'sess-C', 'shell', { command: 'build' });
  const d2 = tb.deliver('c3', 'sess-C', 'shell', { command: 'build' });   // ~60s 重发
  check('C-resend-not-new', d2.isNew === false && d2.dedup === 'pending', 'resend isNew=' + d2.isNew + ' dedup=' + d2.dedup);
  check('C-same-promise', d1.promise === d2.promise, 'resend must reuse same promise (no 2nd injection)');
  check('C-size-one', tb.size() === 1, 'size=' + tb.size() + ' (dup created 2nd entry)');
  check('C-deduped-count', tb.stats.deduped === 1 && tb.stats.delivered === 1, 'stats=' + JSON.stringify(tb.stats));
})();

// —— 修正1 D:原发+重发两个 await 者一起 resolve ——
(async function dedupBothResolve() {
  const clk = mkClock(); const tb = mkTable(clk);
  const d1 = tb.deliver('c4', 'sess-D', 'shell', { command: 'pwd' });
  const d2 = tb.deliver('c4', 'sess-D', 'shell', { command: 'pwd' });
  tb.complete('c4', '/home/cltx');
  const r1 = await d1.promise; const r2 = await d2.promise;
  check('D-both-resolve', r1.output === '/home/cltx' && r2.output === '/home/cltx', 'r1=' + JSON.stringify(r1) + ' r2=' + JSON.stringify(r2));
})();

// —— 修正2 E:取号降级(sweep settle ticket) ——
(async function ticketDegrade() {
  const clk = mkClock(); const tb = mkTable(clk, { ticketMs: 30000 });
  const d = tb.deliver('c5', 'sess-E', 'shell', { command: 'huge-scan' });
  clk.t += 30000;                        // 到 ticket 死线
  tb.sweep(clk.t);
  const r = await d.promise;
  check('E-ticket-status', r.status === 'ticket' && r.callId === 'c5', 'r=' + JSON.stringify(r));
  check('E-ticketed-state', tb.get('c5').state === TICKETED, 'state=' + tb.get('c5').state);
  check('E-ticketed-count', tb.stats.ticketed === 1, 'stats=' + JSON.stringify(tb.stats));
})();

// —— 修正2 F:取号后 output 迟到,落 stash,poll 取到,不重复 settle ——
(async function ticketThenLateOutput() {
  const clk = mkClock(); const tb = mkTable(clk, { ticketMs: 30000 });
  const d = tb.deliver('c6', 'sess-F', 'shell', { command: 'slow' });
  clk.t += 30000; tb.sweep(clk.t);
  const r = await d.promise;
  check('F-ticket-first', r.status === 'ticket', 'first settle should be ticket; r=' + JSON.stringify(r));
  clk.t += 20000;                        // Cursor 迟到 output(50s 时回来)
  const matched = tb.complete('c6', 'scan complete: 4210 files');
  check('F-late-matched', matched === true, 'late complete not matched');
  check('F-now-resolved', tb.get('c6').state === RESOLVED, 'state=' + tb.get('c6').state);
  const p = tb.poll('c6');
  check('F-poll-gets-result', p.status === 'result' && p.output === 'scan complete: 4210 files', 'poll p=' + JSON.stringify(p));
})();

// —— 修正2 G:取号后重发同 call_id → 回 ticket,不新建注入 ——
(async function resendAfterTicket() {
  const clk = mkClock(); const tb = mkTable(clk, { ticketMs: 30000 });
  tb.deliver('c7', 'sess-G', 'shell', { command: 'x' });
  clk.t += 30000; tb.sweep(clk.t);
  const d2 = tb.deliver('c7', 'sess-G', 'shell', { command: 'x' });
  check('G-resend-ticket', d2.isNew === false && d2.dedup === 'ticketed', 'd2 isNew=' + d2.isNew + ' dedup=' + d2.dedup);
  const r = await d2.promise;
  check('G-ticket-again', r.status === 'ticket', 'r=' + JSON.stringify(r));
})();

// —— 修正4 H:broken-pipe 恢复,重发命中 RESOLVED 立刻回 stash ——
(async function brokenPipeRecovery() {
  const clk = mkClock(); const tb = mkTable(clk);
  const d1 = tb.deliver('c8', 'sess-H', 'shell', { command: 'echo hi' });
  tb.complete('c8', 'hi\n(exit 0)');
  await d1.promise;                      // 首答(假设写回 ChatGPT 时 broken-pipe,结果已 stash)
  const d2 = tb.deliver('c8', 'sess-H', 'shell', { command: 'echo hi' });  // ChatGPT 重发
  check('H-resend-resolved', d2.isNew === false && d2.dedup === 'resolved', 'd2 isNew=' + d2.isNew + ' dedup=' + d2.dedup);
  const r = await d2.promise;
  check('H-stash-returned', r.status === 'result' && r.output === 'hi\n(exit 0)', 'r=' + JSON.stringify(r));
})();

// —— 透传 I:URL/长文本原样穿透(修正3) ——
(async function urlPassthrough() {
  const clk = mkClock(); const tb = mkTable(clk);
  const d = tb.deliver('c9', 'sess-I', 'shell', { command: 'cat url.txt' });
  const raw = 'see https://chat.auto-link.com.cn/mcp?s=abc&x=1#frag and http://a.b/c%20d';
  tb.complete('c9', raw);
  const r = await d.promise;
  check('I-url-intact', r.output === raw, 'url mangled: ' + r.output);
})();

// —— 孤儿 J:complete 未知 call_id ——
(async function orphanOutput() {
  const clk = mkClock(); const tb = mkTable(clk);
  const matched = tb.complete('ghost', 'whatever');
  check('J-orphan-false', matched === false && tb.stats.orphanOutputs === 1, 'stats=' + JSON.stringify(tb.stats));
})();

// —— GC K:ttl 回收 resolved;PENDING 不被 ttl 误收 ——
(async function gcSweep() {
  const clk = mkClock(); const tb = mkTable(clk, { ticketMs: 30000, ttlMs: 300000 });
  tb.deliver('c10', 'sess-K', 'shell', { command: 'a' });
  tb.complete('c10', 'r');               // → RESOLVED,ttl 死线 = t+300000
  tb.deliver('c11', 'sess-K', 'shell', { command: 'b' });   // 保持 PENDING(不 complete、不到 ticket 死线)
  clk.t += 300000;                       // 到 c10 的 ttl(但只推 300s,c11 的 ticket 死线是 t0+30s...)
  // 注意 c11 ticket 死线在 30s 前就到了,这里 sweep 会先把 c11 取号(测 PENDING 不被 *ttl* 误收:
  // 它是被 ticket 迁移带走,不是被 ttl 删)。为干净隔离,单独造一张只测 GC 的表:
  const clk2 = mkClock(); const tb2 = mkTable(clk2, { ticketMs: 1e12, ttlMs: 300000 });  // ticket 关到极大
  tb2.deliver('g1', 's', 'shell', { command: 'a' }); tb2.complete('g1', 'r');
  tb2.deliver('g2', 's', 'shell', { command: 'b' });  // PENDING,ticket 死线 1e12 后,不会迁移
  clk2.t += 300000; tb2.sweep(clk2.t);
  check('K-resolved-gc', !tb2.get('g1'), 'g1 should be GC after ttl');
  check('K-pending-survives', !!tb2.get('g2') && tb2.get('g2').state === PENDING, 'g2 wrongly removed/changed: ' + JSON.stringify(tb2.get('g2') && tb2.get('g2').state));
  check('K-gc-count', tb2.stats.gc === 1, 'stats=' + JSON.stringify(tb2.stats));
})();

// —— 会话 L:entry 记 session,配对方案 B 用 ——
(async function sessionTag() {
  const clk = mkClock(); const tb = mkTable(clk);
  tb.deliver('c12', 'sess-X', 'shell', { command: 'a' });
  tb.deliver('c13', 'sess-Y', 'shell', { command: 'b' });
  check('L-sess-x', tb.get('c12').session === 'sess-X', 'sess=' + tb.get('c12').session);
  check('L-sess-y', tb.get('c13').session === 'sess-Y', 'sess=' + tb.get('c13').session);
  check('L-independent', tb.size() === 2, 'size=' + tb.size());
})();

// —— 计数 M:一条 deliver→ticket→late output 的全生命周期计数自洽 ——
(async function statsCoherent() {
  const clk = mkClock(); const tb = mkTable(clk, { ticketMs: 30000 });
  tb.deliver('m1', 's', 'shell', { command: 'a' });   // delivered=1
  tb.deliver('m1', 's', 'shell', { command: 'a' });   // deduped=1
  clk.t += 30000; tb.sweep(clk.t);                    // ticketed=1
  tb.complete('m1', 'r');                             // resolved=1
  tb.complete('ghost', 'x');                          // orphanOutputs=1
  const s = tb.stats;
  check('M-stats', s.delivered === 1 && s.deduped === 1 && s.ticketed === 1 && s.resolved === 1 && s.orphanOutputs === 1,
    'stats=' + JSON.stringify(s));
})();

// 等所有 async IIFE 结算后汇总
setTimeout(function () {
  console.log('\n== ' + pass + '/' + (pass + fail) + ' PASS ==');
  if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1); }
  console.log('VERDICT: GO (基础往返/poll + 修正1幂等去重 + 修正2取号降级 + 修正4 broken-pipe + 修正3原样透传 + 孤儿/GC/会话/计数 全绿)');
}, 100);
