'use strict';
/*
 * mcp_bridge_v2_offline_cases.js — bridge-v2 新机制离线单测(零网络、注入时钟、fake sink)。
 *
 * 背景:mcp_bridge_offline_cases.js(19 例)只覆盖 v1 机械件(deriveCallId/inject/finishPlain/
 * remap/降级);bridge-v2 引入的核心新行为没有断言。本套件补齐,防它们静默漂移:
 *   ①⟦ 冻结:turn-1/turn-2 一出现 U+27E6 就停流,sentN 只记净前缀,streamedN 留全文,frozenN=true;
 *   ②verdict2 三态(finishContinuation):kind:'call'→兜底续链发 function_call+forget;
 *     kind:'text'→剥净 prose 收口;缺省 null→原样交付(零行为差,back-compat);
 *   ③finishPlainText:按净文本 sent1-前缀 reconcile,只补发余量;
 *   ④finishWithCall:发 message+function_call+completed,且**不** registerCallId
 *     (⟦cmd¦run⟧ 兜底轮的 turn-2 走常规 tool-feed,不做会合);
 *   ⑤ticketText 覆盖 = 安全红线:无活跃桥(callId==null)可 steer;真超时(callId 非空=inject 已发、
 *     Cursor 执行中)必须保"稍后重试"语义,**绝不** steer 重发(杜绝双执行)。
 * fake sink 解析出 [{event,data}] 序列,逐事件断言形状(与 v1 套件同骨架)。
 */
const assert = require('assert');
const path = require('path');
const M = require(path.join(__dirname, '..', 'chatgpt-onboard', 'zerokey-codex', 'zerokey-patch', 'routes', 'mcp_bridge.js'));

let pass = 0, fail = 0;
function t(name, fn) {
  try {
    const r = fn();
    if (r && typeof r.then === 'function') {
      return r.then(() => { console.log('  ok  ' + name); pass++; })
        .catch((e) => { console.log('  FAIL ' + name + '  :: ' + (e && e.message || e)); fail++; });
    }
    console.log('  ok  ' + name); pass++;
  } catch (e) { console.log('  FAIL ' + name + '  :: ' + (e && e.message || e)); fail++; }
}

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
    deltas: function () {
      return this.events().filter((e) => e.event === 'response.output_text.delta').map((e) => e.data.delta);
    },
    fc: function () {
      return this.events().find((e) => e.event === 'response.output_item.done' && e.data.item && e.data.item.type === 'function_call');
    },
    completedText: function () {
      const c = this.events().find((e) => e.event === 'response.completed');
      const out = c && c.data.response.output;
      const msg = out && out.find((o) => o.type === 'message');
      return msg ? msg.content[0].text : null;
    },
  };
}

const runTests = [];
function T(name, fn) { runTests.push([name, fn]); }

// ── ① ⟦ 冻结:turn-1 ──────────────────────────────────────────────
T('freeze turn-1:出现 ⟦ 后停流,只下发净前缀,streamed 留全文,frozen1=true', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink, respId: 'r', created: 1, model: 'm' });
  ctx.onText('hello ⟦cmd¦run=ls /tmp⟧ world');
  assert.strictEqual(sink.deltas().join(''), 'hello ', '只下发 ⟦ 之前的净前缀');
  assert.strictEqual(ctx.streamed1, 'hello ⟦cmd¦run=ls /tmp⟧ world', 'streamed1 保留全文');
  assert.strictEqual(ctx.sent1, 'hello ', 'sent1 只记净前缀');
  assert.strictEqual(ctx.frozen1, true, 'frozen1 置位');
  // 冻结后追加不再泄漏
  ctx.onText('MORE ⟦x⟧');
  assert.strictEqual(sink.deltas().join(''), 'hello ', '冻结后零新增下发');
});

T('freeze turn-1:纯净文本(无 ⟦)照常全流,不冻结', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink, respId: 'r', created: 1, model: 'm' });
  ctx.onText('just prose ');
  ctx.onText('more prose');
  assert.strictEqual(sink.deltas().join(''), 'just prose more prose');
  assert.strictEqual(ctx.frozen1, false);
  assert.strictEqual(ctx.sent1, 'just prose more prose');
});

// ── ①b ⟦ 冻结:turn-2(续流)────────────────────────────────────────
T('freeze turn-2:续流出现 ⟦ 后停流,sent2 净前缀,frozen2=true', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink1 = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink: sink1, respId: 'r', created: 1, model: 'm' });
  ctx.inject('cid16chars000001', 'shell', { cmd: 'ls' });
  const sink2 = mkSink();
  ctx.beginContinuation(sink2, { respId: 'r2', created: 2, model: 'm' });
  ctx.onText('ok ⟦cmd¦run=x⟧ tail');
  assert.strictEqual(sink2.deltas().join(''), 'ok ', 'turn-2 只下发 ⟦ 前净前缀');
  assert.strictEqual(ctx.streamed2, 'ok ⟦cmd¦run=x⟧ tail', 'streamed2 保留全文');
  assert.strictEqual(ctx.sent2, 'ok ', 'sent2 净前缀');
  assert.strictEqual(ctx.frozen2, true, 'frozen2 置位');
});

// ── ② verdict2 三态(finishContinuation)────────────────────────────
T('verdict2 kind:call:兜底续链发 message+function_call,forget 清理', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink1 = mkSink();
  const verdict2 = (full) => ({ kind: 'call', callId: 'cc_next', name: 'shell',
    argumentsJson: JSON.stringify({ command: 'ls' }), leadingText: 'running ls' });
  const ctx = reg.openTurn({ session: 'c', sink: sink1, respId: 'r', created: 1, model: 'm', verdict2 });
  ctx.inject('cid16chars000002', 'shell', { cmd: 'a' });
  const injectedCallId = ctx.callId;
  const sink2 = mkSink();
  ctx.beginContinuation(sink2, { respId: 'r2', created: 2, model: 'm' });
  ctx.onText('running ls');       // sent2 = 'running ls'
  ctx.markUpstreamDone();          // phase turn2 → finishContinuation
  assert.strictEqual(sink2.completedText(), 'running ls', 'lead 文本收口');
  const fc = sink2.fc();
  assert.ok(fc, 'function_call 已发');
  assert.strictEqual(fc.data.item.call_id, 'cc_next', '兜底续链 call_id 用 verdict2 给的');
  assert.strictEqual(fc.data.item.name, 'shell');
  assert.strictEqual(fc.data.item.arguments, JSON.stringify({ command: 'ls' }));
  assert.strictEqual(reg.byCallId(injectedCallId), null, 'forget 清理注册表');
});

T('verdict2 kind:text:剥净 prose 收口,无 function_call', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink1 = mkSink();
  const verdict2 = () => ({ kind: 'text', text: 'clean answer' });
  const ctx = reg.openTurn({ session: 'c', sink: sink1, respId: 'r', created: 1, model: 'm', verdict2 });
  ctx.inject('cid16chars000003', 'shell', { cmd: 'a' });
  const sink2 = mkSink();
  ctx.beginContinuation(sink2, { respId: 'r2', created: 2, model: 'm' });
  ctx.onText('clean ans');          // sent2='clean ans',净文本 'clean answer' 以此为前缀
  ctx.markUpstreamDone();
  assert.strictEqual(sink2.completedText(), 'clean answer', '净文本收口');
  assert.ok(sink2.deltas().join('').endsWith('wer'), '只补发余量 tail(reconcile)');
  assert.strictEqual(sink2.fc(), undefined, 'text 态无 function_call');
});

T('verdict2 缺省(null):原样交付 streamed2,零行为差(back-compat)', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink1 = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink: sink1, respId: 'r', created: 1, model: 'm' }); // 无 verdict2
  assert.strictEqual(ctx.verdict2, null, '缺省 verdict2=null');
  ctx.inject('cid16chars000004', 'shell', { cmd: 'a' });
  const sink2 = mkSink();
  ctx.beginContinuation(sink2, { respId: 'r2', created: 2, model: 'm' });
  ctx.onText('raw text');
  ctx.markUpstreamDone();
  assert.strictEqual(sink2.completedText(), 'raw text', '原样交付全文');
  assert.strictEqual(sink2.fc(), undefined, '无裁决则不发 function_call');
});

// ── ③ finishPlainText 按净文本 reconcile ──────────────────────────
T('finishPlainText:冻结轮按净文本 sent1-前缀 reconcile,只补发余量', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink, respId: 'r', created: 1, model: 'm' });
  ctx.onText('answer ⟦cmd¦run=x⟧');   // 冻结:sent1='answer ',streamed1 含方言残片
  assert.strictEqual(ctx.frozen1, true);
  ctx.finishPlainText('answer clean');  // 调用方剥净后的文本
  assert.strictEqual(sink.completedText(), 'answer clean', 'done 文本=净文本(非 streamed1 残片)');
  assert.strictEqual(sink.deltas().join(''), 'answer clean', 'tail=clean 补在净前缀后');
  assert.strictEqual(ctx.injected, false);
});

T('finishPlainText:null 入参回落 streamed1(零参形态兼容)', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink, respId: 'r', created: 1, model: 'm' });
  ctx.onText('plain answer');
  ctx.finishPlainText(null);
  assert.strictEqual(sink.completedText(), 'plain answer');
});

// ── ④ finishWithCall 不 registerCallId ────────────────────────────
T('finishWithCall:发 message+function_call+completed,且不 registerCallId(turn-2 走常规流)', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink, respId: 'r', created: 1, model: 'm' });
  ctx.onText('let me run ');          // sent1='let me run '
  ctx.finishWithCall({ callId: 'zz_bpi', name: 'shell',
    argumentsJson: JSON.stringify({ command: 'ls' }), leadingText: 'let me run it' });
  assert.strictEqual(sink.completedText(), 'let me run it', 'lead 收口(rest=it 补发)');
  const fc = sink.fc();
  assert.ok(fc, 'function_call 已发');
  assert.strictEqual(fc.data.item.call_id, 'zz_bpi');
  assert.strictEqual(fc.data.item.arguments, JSON.stringify({ command: 'ls' }));
  // 关键:不 registerCallId —— byCallId 查不到,turn-2 回流按常规 tool-feed 轮
  assert.strictEqual(reg.byCallId('zz_bpi'), null, 'finishWithCall 不做会合登记');
  assert.strictEqual(reg.pickActiveBridge(), null, 'deactivate 后不再活跃');
  assert.strictEqual(ctx.injected, false, 'finishWithCall 不置 injected(与 inject 分道)');
});

T('finishWithCall:无 leadingText 只发 function_call(idx 0),不发 message', () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const sink = mkSink();
  const ctx = reg.openTurn({ session: 'c', sink, respId: 'r', created: 1, model: 'm' });
  ctx.finishWithCall({ callId: 'q', name: 'shell', argumentsJson: '{"command":"pwd"}' });
  assert.strictEqual(sink.completedText(), null, '无 lead 不发 message item');
  const fc = sink.fc();
  assert.ok(fc, 'function_call 已发');
  assert.strictEqual(fc.data.item.call_id, 'q');
});

// ── ⑤ ticketText 覆盖 = 安全红线 ──────────────────────────────────
T('ticketText 覆盖:无活跃桥(callId==null)→ steer 文本', async () => {
  const reg = new M.BridgeRegistry({ now: () => 0,
    ticketText: (cid) => (cid == null ? 'STEER_FALLBACK' : 'RETRY(' + cid + ')') });
  const r = await reg.makeCallTool()('shell', { cmd: 'x' });
  assert.strictEqual(r.isError, true);
  assert.strictEqual(r.text, 'STEER_FALLBACK', '无活跃桥用 ticketText(null)=steer');
});

T('ticketText 覆盖:真超时(callId 非空)→ 保"稍后重试",绝不 steer(杜绝双执行)', async () => {
  let now = 1000;
  // 注入时钟必须同时给 rendezvous:deliver 用 rendezvous._now() 写 createdAt,
  // sweep(now) 才能算出 now-createdAt>=ticketMs;否则 createdAt=真实 Date.now() → 永不超时挂起。
  const reg = new M.BridgeRegistry({ rendezvous: { ticketMs: 30000, now: () => now }, now: () => now,
    ticketText: (cid) => (cid == null ? 'STEER_FALLBACK' : 'RETRY(' + cid + ')') });
  const ctx = reg.openTurn({ session: 'c', sink: mkSink(), respId: 'r', created: 1, model: 'm' });
  const p = reg.makeCallTool()('shell', { cmd: 'slow' });
  await new Promise((r) => setImmediate(r));   // inject 跑完,callId 已发给 Cursor
  now += 30000;
  reg.rendezvous.sweep(now);                    // TICKETED
  const r = await p;
  assert.strictEqual(r.isError, true);
  assert.ok(/^RETRY\(/.test(r.text), '超时轮 callId 非空 → ticketText(callId)=稍后重试,不 steer');
  assert.strictEqual(r.text.indexOf('STEER'), -1, '绝不 steer(inject 已发,steer 会致双执行)');
});

T('默认 ticketText:null 与 callId 都走"稍后重试"文法(steer 语义由 responses.js 覆盖注入)', async () => {
  const reg = new M.BridgeRegistry({ now: () => 0 });
  const r = await reg.makeCallTool()('shell', { cmd: 'x' });   // 无活跃桥,ticketText(null)
  assert.ok(/执行中|call_id/.test(r.text), '模块默认不 steer(保守),覆盖钩在 responses.js');
});

(async () => {
  for (const [name, fn] of runTests) { await t(name, fn); }
  console.log('\nmcp_bridge v2 offline: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
