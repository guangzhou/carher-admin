'use strict';
/*
 * mcp_connector_mgr_offline_cases.js — ConnectorManager 生命周期状态机离线单测。
 * 副作用全注入:fake adapter(可编程,记录调用)+ 内存/可失败 store + 注入时钟。
 * 零网络零磁盘。证:provision 全链 / 单飞 / grace-cancel / sweep 拆+verify404 / rollback /
 * orphan 恢复 / 409-reuse / devmode 已开跳过 / release 下溢守卫 / 默认关。
 */
const assert = require('assert');
const path = require('path');
const M = require(path.join(__dirname, '..', 'chatgpt-onboard', 'zerokey-codex', 'zerokey-patch', 'routes', 'mcp_connector_mgr.js'));
const { ConnectorManager, PLANE } = M;

let pass = 0, fail = 0;
function t(name, fn) {
  Promise.resolve().then(fn).then(
    () => { console.log('  ok  ' + name); pass++; },
    (e) => { console.log('  FAIL ' + name + '  :: ' + (e && e.message || e)); fail++; },
  );
}

// ── 可编程 fake adapter ──────────────────────────────────────────────
function mkAdapter(over) {
  over = over || {};
  const calls = [];
  const rec = (k) => (...a) => { calls.push([k, ...a]); };
  const base = {
    _calls: calls,
    _count: (k) => calls.filter((c) => c[0] === k).length,
    devmodeStatus: async () => { rec('devmodeStatus')(); return over.devmodeStatus || { enabled: false, plane: PLANE.NO_ROUTE }; },
    devmodeEnable: async () => { rec('devmodeEnable')(); return over.devmodeEnable || { ok: true, plane: PLANE.OK }; },
    register: async (o) => { rec('register')(o); return over.register || { id: 'conn_1', plane: PLANE.OK }; },
    actions: async (id) => { rec('actions')(id); return (over.actionsFn ? over.actionsFn(id) : (over.actions || { list: [{ name: 'shell', is_enabled: true }], plane: PLANE.OK })); },
    link: async (id, o) => { rec('link')(id, o); return over.link || { id: 'link_1', plane: PLANE.OK }; },
    del: async (id) => { rec('del')(id); return over.del || { ok: true, plane: PLANE.OK }; },
  };
  return base;
}
// 拆后 actions 落 GONE(连接器不存在)
function goneActions() { return { list: [], plane: PLANE.NO_ROUTE }; }

// ── ① provision 全链 ────────────────────────────────────────────────
t('acquire@IDLE → devmode enable → register → actions → link → LINKED', async () => {
  const a = mkAdapter();
  const m = new ConnectorManager({ adapter: a, now: () => 1000, publicUrl: 'https://x/mcp' });
  const r = await m.acquire('acct82');
  assert.strictEqual(r.connectorId, 'conn_1');
  assert.strictEqual(r.linkId, 'link_1');
  const snap = m.snapshot();
  assert.strictEqual(snap.acct82.state, 'linked');
  assert.strictEqual(snap.acct82.refCount, 1);
  assert.strictEqual(a._count('devmodeEnable'), 1, 'devmode 关 → 开一次');
  assert.strictEqual(a._count('register'), 1);
  assert.strictEqual(a._count('link'), 1);
});

t('devmode 已开 → 跳过 devmodeEnable', async () => {
  const a = mkAdapter({ devmodeStatus: { enabled: true, plane: PLANE.OK } });
  const m = new ConnectorManager({ adapter: a, publicUrl: 'https://x/mcp' });
  await m.acquire('acct82');
  assert.strictEqual(a._count('devmodeEnable'), 0, '已开不再开');
});

t('缺 publicUrl → acquire 抛错', async () => {
  const m = new ConnectorManager({ adapter: mkAdapter() });
  let threw = false;
  try { await m.acquire('acct82'); } catch (_) { threw = true; }
  assert.strictEqual(threw, true);
});

// ── ② 引用计数 + 单飞 ────────────────────────────────────────────────
t('并发 acquire → 单飞一次 provision,refCount 累加', async () => {
  const a = mkAdapter();
  const m = new ConnectorManager({ adapter: a, publicUrl: 'https://x/mcp' });
  const [r1, r2] = await Promise.all([m.acquire('acct82'), m.acquire('acct82')]);
  assert.strictEqual(r1.connectorId, r2.connectorId);
  assert.strictEqual(a._count('register'), 1, '单飞:只 register 一次');
  assert.strictEqual(m.snapshot().acct82.refCount, 2);
});

t('第二次 acquire(已 LINKED)→ 复用不重 provision', async () => {
  const a = mkAdapter();
  const m = new ConnectorManager({ adapter: a, publicUrl: 'https://x/mcp' });
  await m.acquire('acct82');
  await m.acquire('acct82');
  assert.strictEqual(a._count('register'), 1);
  assert.strictEqual(m.snapshot().acct82.refCount, 2);
});

// ── ③ release → grace → sweep 拆 + verify404 ─────────────────────────
t('refCount→0 → grace;sweep 未到期不拆,到期拆(del + verify404 + 清盘 + 删条目)', async () => {
  let now = 1000;
  const a = mkAdapter({ actionsFn: (id) => (a._delDone ? goneActions() : { list: [{ name: 'shell', is_enabled: true }], plane: PLANE.OK }) });
  // del 后把 actions 切成 gone
  a.del = async (id) => { a._calls.push(['del', id]); a._delDone = true; return { ok: true, plane: PLANE.OK }; };
  const store = M.memStore();
  const m = new ConnectorManager({ adapter: a, store, now: () => now, idleGraceMs: 20000, publicUrl: 'https://x/mcp' });
  await m.acquire('acct82');
  m.release('acct82');
  assert.strictEqual(m.snapshot().acct82.state, 'grace');
  now += 10000; m.sweep(now);                 // 未到期
  assert.strictEqual(m.snapshot().acct82.state, 'grace', '10s<20s 不拆');
  now += 15000; m.sweep(now);                 // 到期 → teardown
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(a._count('del'), 1, 'del 一次');
  assert.strictEqual(m.snapshot().acct82, undefined, '拆后条目删除');
  assert.strictEqual(store.load(), null, '拆后清盘');
});

t('grace 中 re-acquire → grace-cancel 复用,不重 provision/不 del', async () => {
  let now = 1000;
  const a = mkAdapter();
  const m = new ConnectorManager({ adapter: a, now: () => now, idleGraceMs: 20000, publicUrl: 'https://x/mcp' });
  await m.acquire('acct82');
  m.release('acct82');
  assert.strictEqual(m.snapshot().acct82.state, 'grace');
  const r = await m.acquire('acct82');       // grace-cancel
  assert.strictEqual(r.connectorId, 'conn_1');
  assert.strictEqual(m.snapshot().acct82.state, 'linked');
  assert.strictEqual(a._count('register'), 1, '未重 provision');
  assert.strictEqual(a._count('del'), 0, '未拆');
  now += 999999; m.sweep(now);
  assert.strictEqual(m.snapshot().acct82.state, 'linked', 'refCount≥1 不被 sweep 拆');
});

t('teardown verify:del 后 actions 仍在(未消失)→ verified=false 但仍清条目', async () => {
  let now = 1000;
  const a = mkAdapter();                       // actions 恒返 OK(连接器"没删掉")
  const m = new ConnectorManager({ adapter: a, now: () => now, idleGraceMs: 1, publicUrl: 'https://x/mcp' });
  await m.acquire('acct82');
  m.release('acct82');
  now += 10; m.sweep(now);
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(m.snapshot().acct82, undefined, '即便 verify 失败也删条目(不卡死)');
});

// ── ④ rollback ───────────────────────────────────────────────────────
t('provision rollback:actions 空 → del 回滚 + 清盘 + 抛错 + 回 IDLE', async () => {
  const a = mkAdapter({ actions: { list: [], plane: PLANE.OK } });
  const store = M.memStore();
  const m = new ConnectorManager({ adapter: a, store, publicUrl: 'https://x/mcp' });
  let threw = false;
  try { await m.acquire('acct82'); } catch (_) { threw = true; }
  assert.strictEqual(threw, true);
  assert.strictEqual(a._count('del'), 1, 'register 成功后 rollback 删连接器');
  assert.strictEqual(store.load(), null, '回滚清盘');
  assert.strictEqual(m.snapshot().acct82.state, 'idle');
  assert.strictEqual(m.snapshot().acct82.refCount, 0);
});

t('provision rollback:link 失败 → del 回滚', async () => {
  const a = mkAdapter({ link: { id: null, plane: PLANE.SCHEMA } });
  const m = new ConnectorManager({ adapter: a, publicUrl: 'https://x/mcp' });
  let threw = false;
  try { await m.acquire('acct82'); } catch (_) { threw = true; }
  assert.strictEqual(threw, true);
  assert.strictEqual(a._count('del'), 1);
});

t('register 失败(非 EXISTS)→ 抛错,不 del(没建成无可删)', async () => {
  const a = mkAdapter({ register: { id: null, plane: PLANE.FEATURE_GATE } });
  const m = new ConnectorManager({ adapter: a, publicUrl: 'https://x/mcp' });
  let threw = false;
  try { await m.acquire('acct82'); } catch (_) { threw = true; }
  assert.strictEqual(threw, true);
  assert.strictEqual(a._count('del'), 0);
});

// ── ⑤ store 落盘时机 + orphan 恢复 ───────────────────────────────────
t('register 一成功立即落盘(link 前崩也留可回收记录)', async () => {
  const saves = [];
  const store = { load: () => null, save: (r) => saves.push(r), clear: () => {} };
  const a = mkAdapter();
  const m = new ConnectorManager({ adapter: a, store, now: () => 7, publicUrl: 'https://x/mcp' });
  await m.acquire('acct82');
  assert.ok(saves.length >= 2, '至少两次 save(register 后 + link 后)');
  assert.strictEqual(saves[0].connectorId, 'conn_1');
  assert.strictEqual(saves[0].linkId, undefined, '首存无 linkId');
  assert.strictEqual(saves[saves.length - 1].linkId, 'link_1', '末存有 linkId');
});

t('recoverOrphans:盘上有孤儿 → del + verify + 清盘', async () => {
  const store = M.memStore();
  store.save({ account: 'acct82', connectorId: 'orphan_9', at: 1 });
  const a = mkAdapter({ actions: goneActions() });
  const m = new ConnectorManager({ adapter: a, store });
  const r = await m.recoverOrphans();
  assert.strictEqual(r.recovered, true);
  assert.strictEqual(r.connectorId, 'orphan_9');
  assert.strictEqual(r.verified, true);
  assert.strictEqual(a._calls.find((c) => c[0] === 'del')[1], 'orphan_9', 'del 打到孤儿 id');
  assert.strictEqual(store.load(), null, '清盘');
});

t('recoverOrphans:空盘 → no-op', async () => {
  const a = mkAdapter();
  const m = new ConnectorManager({ adapter: a, store: M.memStore() });
  const r = await m.recoverOrphans();
  assert.strictEqual(r.recovered, false);
  assert.strictEqual(a._count('del'), 0);
});

// ── ⑥ 409-reuse ──────────────────────────────────────────────────────
t('register 409 EXISTS 复用 existing id(幂等)', async () => {
  const a = mkAdapter({ register: { id: 'existing_42', plane: PLANE.EXISTS } });
  const m = new ConnectorManager({ adapter: a, publicUrl: 'https://x/mcp' });
  const r = await m.acquire('acct82');
  assert.strictEqual(r.connectorId, 'existing_42', '409 → 复用已存在连接器');
});

// ── ⑦ release 守卫 + 多账号隔离 ──────────────────────────────────────
t('release 未知账号 → no-op 不崩;release 过多不下溢', async () => {
  const a = mkAdapter();
  const m = new ConnectorManager({ adapter: a, publicUrl: 'https://x/mcp' });
  m.release('never');                          // no-op
  await m.acquire('acct82');
  m.release('acct82'); m.release('acct82');     // 第二次多余
  assert.strictEqual(m.snapshot().acct82.refCount, 0);
  assert.strictEqual(m.snapshot().acct82.state, 'grace');
});

t('多账号隔离:acct82 与 acct93 各自 provision/refCount', async () => {
  const a = mkAdapter();
  const m = new ConnectorManager({ adapter: a, publicUrl: 'https://x/mcp' });
  await m.acquire('acct82');
  await m.acquire('acct93');
  assert.strictEqual(a._count('register'), 2);
  assert.strictEqual(m.snapshot().acct82.refCount, 1);
  assert.strictEqual(m.snapshot().acct93.refCount, 1);
});

// ── ⑧ 默认关 ─────────────────────────────────────────────────────────
t('isEnabled:未设 ZK_MCP_BRIDGE → false', () => {
  const saved = process.env.ZK_MCP_BRIDGE;
  delete process.env.ZK_MCP_BRIDGE;
  assert.strictEqual(M.isEnabled(), false);
  process.env.ZK_MCP_BRIDGE = '1';
  assert.strictEqual(M.isEnabled(), true);
  if (saved === undefined) delete process.env.ZK_MCP_BRIDGE; else process.env.ZK_MCP_BRIDGE = saved;
});

t('fileStore:save→load 往返 + clear + 缺失容错', () => {
  const files = {};
  const fakeFs = {
    readFileSync: (p) => { if (!(p in files)) { const e = new Error('ENOENT'); throw e; } return files[p]; },
    writeFileSync: (p, d) => { files[p] = d; },
    renameSync: (a, b) => { files[b] = files[a]; delete files[a]; },
    unlinkSync: (p) => { delete files[p]; },
  };
  const s = M.fileStore('/x/conn.json', fakeFs);
  assert.strictEqual(s.load(), null, '缺失 → null 容错');
  s.save({ connectorId: 'c9' });
  assert.strictEqual(s.load().connectorId, 'c9', '往返');
  s.clear();
  assert.strictEqual(s.load(), null, 'clear 后 null');
});

setTimeout(() => {
  console.log('\nmcp_connector_mgr offline: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
}, 400);
