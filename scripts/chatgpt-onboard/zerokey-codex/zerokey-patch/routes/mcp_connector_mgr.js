'use strict';
/*
 * mcp_connector_mgr.js — MCP 会合桥 §5.1 方案A:每会话 link/unlink 连接器生命周期管理器。
 *
 * 为什么存在(Gate-2b/2c 机制定论):MCP 连接器是**账号级**对象(owners: USER),`link`
 * (POST /connectors/links/noauth)是"对话里可调"的真开关,一旦 link 就对该账号**所有**会话
 * 可见(泄漏到 codex 的 `[]` 轮 = 上下文足迹);register-only 端不上模型(死)。故 codex-shared
 * 账号上唯一安全形态 = **只在有活跃桥会话时挂 link、无会话即拆**(不留持久连接器)。本管理器
 * 把手动 provision→round-trip→delete 流程产品化成引用计数生命周期。
 *
 * 硬约束(实测钉死,见 mcp-connector-cli.js):
 *  - 连接器 API **没有 list-by-account** → 崩溃残留的孤儿连接器无法枚举清理 → 必须把 connectorId
 *    **落盘**(store),重启时先 recoverOrphans() 删旧再起新,否则账号上连接器越堆越多且删不掉。
 *  - 没有独立 unlink:拆 = delete 连接器(级联删 link)。teardown 后 verify actions→404 复验拆干净。
 *  - register 409 = 同名已存在,响应体带 existing_connector_id,幂等复用(非失败)。
 *  - 账号级 = 无法按单条 Cursor 会话隔离 link → "活跃窗口" = 该账号上 refCount≥1。多会话叠加靠
 *    引用计数;refCount→0 后 idle-grace 才拆(避免会话快速交替时反复 provision/delete 抖动)。
 *
 * 副作用全注入(adapter + store + 注入时钟),核心状态机零网络、零磁盘,离线可证。真实 HTTP
 * 走 makeLiveAdapter(逐字镜像已验证的 mcp-connector-cli.js 请求形状);store 走 pod-local 文件。
 * 全程 ZK_MCP_BRIDGE 门控;live-enable 另需 codex 非回归验证(池恢复后)+ 运行时会话凭据供给。
 */

const crypto = require('crypto');

// ── adapter plane 常量(逐字镜像 mcp-connector-cli.js classify)────────────────
const PLANE = {
  OK: 'OK', CLOUDFLARE: 'CLOUDFLARE', FEATURE_GATE: 'FEATURE_GATE',
  NO_ROUTE: 'NO_ROUTE', ROUTE_OK_BAD_ID: 'ROUTE_OK_BAD_ID', WRONG_METHOD: 'WRONG_METHOD',
  SCHEMA: 'SCHEMA', EXISTS: 'EXISTS', NETWORK: 'NETWORK', NON_JSON: 'NON_JSON', OTHER: 'OTHER',
};
// del 后连接器已消失的 plane(actions/GET 会落这两个之一)= verify-404 判据。
const GONE_PLANES = new Set([PLANE.NO_ROUTE, PLANE.ROUTE_OK_BAD_ID]);

// ── 生命周期状态 ─────────────────────────────────────────────────────────────
const S = { IDLE: 'idle', PROVISIONING: 'provisioning', LINKED: 'linked', GRACE: 'grace', TEARDOWN: 'teardown' };

class ConnectorManager {
  constructor(opts) {
    opts = opts || {};
    this.adapter = opts.adapter;                 // { devmodeStatus, register, actions, link, del } → async
    this.store = opts.store || memStore();       // { load()->rec|null, save(rec), clear() }
    this._now = opts.now || (() => Date.now());
    this.idleGraceMs = opts.idleGraceMs == null ? 20000 : opts.idleGraceMs;  // refCount→0 后拆前宽限
    this.publicUrl = opts.publicUrl || null;     // 桥公网 /mcp URL(register 的 mcp_url)
    this.connectorName = opts.connectorName || 'zk-shell';
    this.log = opts.log || (() => {});
    this.map = new Map();                         // account → entry
  }

  _entry(account) {
    let e = this.map.get(account);
    if (!e) { e = { account, state: S.IDLE, refCount: 0, connectorId: null, linkId: null, idleSince: 0, provisioning: null, teardown: null }; this.map.set(account, e); }
    return e;
  }

  snapshot() {
    const out = {};
    for (const [k, e] of this.map) out[k] = { state: e.state, refCount: e.refCount, connectorId: e.connectorId, linkId: e.linkId };
    return out;
  }

  // 一个桥会话开始:确保该账号有 link 的连接器(有则复用/引用计数++,无则 provision)。
  // 并发/重入安全:provisioning 单飞(共享 promise);grace 中取回则取消拆(足迹本未落地)。
  async acquire(account, opts) {
    opts = opts || {};
    const url = opts.url || this.publicUrl;
    const name = opts.name || this.connectorName;
    if (!url) throw new Error('acquire: 缺 publicUrl(桥 /mcp 公网地址)');
    const e = this._entry(account);
    e.refCount += 1;

    if (e.state === S.LINKED) return { connectorId: e.connectorId, linkId: e.linkId, name };
    if (e.state === S.GRACE) {                     // 取消拆:足迹还在,直接复用
      e.state = S.LINKED; e.idleSince = 0;
      this.log('[conn-mgr] grace-cancel reuse account=' + account + ' connector=' + e.connectorId);
      return { connectorId: e.connectorId, linkId: e.linkId, name };
    }
    if (e.state === S.PROVISIONING) return e.provisioning;   // 单飞:并发 acquire 合流
    if (e.state === S.TEARDOWN) {                  // 正在拆:等拆完再重新 provision
      try { await e.teardown; } catch (_) {}
      e.refCount = Math.max(0, e.refCount - 1);     // 撤销本次预 ++,交给重入(fresh entry)统一计数
      return this.acquire(account, opts);
    }
    // IDLE → provision
    e.state = S.PROVISIONING;
    e.provisioning = this._provision(e, url, name).then(
      (r) => { e.provisioning = null; return r; },
      (err) => { e.provisioning = null; e.state = S.IDLE; e.refCount = Math.max(0, e.refCount - 1); throw err; },
    );
    return e.provisioning;
  }

  // 桥会话结束:引用计数--;归零则进 grace(idle 计时),由 sweep 到期拆。
  release(account) {
    const e = this.map.get(account);
    if (!e) return;
    e.refCount = Math.max(0, e.refCount - 1);
    if (e.refCount === 0 && e.state === S.LINKED) {
      e.state = S.GRACE; e.idleSince = this._now();
      this.log('[conn-mgr] release→grace account=' + account + ' graceMs=' + this.idleGraceMs);
    }
  }

  // 外部时钟:GRACE 且 refCount==0 过 idleGraceMs → 拆(del + verify 404 + 清盘)。
  sweep(nowMs) {
    for (const e of this.map.values()) {
      if (e.state === S.GRACE && e.refCount === 0 && nowMs - e.idleSince >= this.idleGraceMs) {
        e.state = S.TEARDOWN;
        e.teardown = this._teardown(e).then(() => { e.teardown = null; }, () => { e.teardown = null; });
      }
    }
  }

  async _provision(e, url, name) {
    const a = this.adapter;
    // devmode 是注册前置(账号级、持久)。off 则开。
    const dm = await a.devmodeStatus();
    if (!dm || !dm.enabled) {
      const en = await a.devmodeEnable();
      if (!en || !en.ok) throw new Error('provision: 无法开启 developer_mode');
    }
    const reg = await a.register({ url, name });
    if (!reg || !reg.id) throw new Error('provision: 注册失败 plane=' + (reg && reg.plane));
    const id = reg.id;
    // 关键:register 一成功立刻落盘 —— 崩在 link 前也留可回收的孤儿记录(recoverOrphans 兜)。
    this.store.save({ account: e.account, connectorId: id, name, at: this._now() });
    e.connectorId = id;

    const rollback = async (why) => {
      this.log('[conn-mgr] ' + why + ' → 回滚 connector=' + id);
      try { await a.del(id); } catch (_) {}
      this.store.clear();
      e.state = S.IDLE; e.connectorId = null; e.linkId = null; e.refCount = Math.max(0, e.refCount - 1);
      throw new Error('provision: ' + why);
    };

    const acts = await a.actions(id);
    const list = (acts && acts.list) || [];
    if (!list.length) return rollback('没抓到 action');
    const actionNames = list.filter((x) => x.is_enabled !== false).map((x) => x.name);
    if (!actionNames.length) return rollback('无 enabled action');

    const lk = await a.link(id, { name: name + '_link', actions: actionNames });
    if (!lk || !lk.id) return rollback('建 link 失败 plane=' + (lk && lk.plane));

    e.linkId = lk.id; e.state = S.LINKED;
    this.store.save({ account: e.account, connectorId: id, linkId: lk.id, name, at: this._now() });
    this.log('[conn-mgr] provisioned account=' + e.account + ' connector=' + id + ' link=' + lk.id);
    return { connectorId: id, linkId: lk.id, name };
  }

  async _teardown(e) {
    const id = e.connectorId;
    const account = e.account;
    let verified = false;
    if (id) {
      try { await this.adapter.del(id); } catch (err) { this.log('[conn-mgr] del error: ' + (err && err.message)); }
      // verify-404:del 后 actions 应落 GONE_PLANES(连接器不存在)。
      try {
        const chk = await this.adapter.actions(id);
        verified = !!(chk && GONE_PLANES.has(chk.plane));
        if (!verified) this.log('[conn-mgr] WARN teardown verify: connector ' + id + ' 未确认消失 plane=' + (chk && chk.plane));
      } catch (_) { verified = true; }   // actions 抛异常也视作已消失(网络/404)
    }
    this.store.clear();
    this.map.delete(account);
    this.log('[conn-mgr] teardown account=' + account + ' connector=' + id + ' verified404=' + verified);
    return verified;
  }

  // 启动兜底:上个进程崩溃可能留下已 register/link 但未 delete 的孤儿(无 list-by-account 只能靠盘)。
  // 起新桥前先删旧、复验、清盘。
  async recoverOrphans() {
    const rec = this.store.load();
    if (!rec || !rec.connectorId) return { recovered: false };
    const id = rec.connectorId;
    this.log('[conn-mgr] recoverOrphans: 发现落盘孤儿 connector=' + id + ' → 删除');
    let verified = false;
    try { await this.adapter.del(id); } catch (err) { this.log('[conn-mgr] recover del error: ' + (err && err.message)); }
    try {
      const chk = await this.adapter.actions(id);
      verified = !!(chk && GONE_PLANES.has(chk.plane));
    } catch (_) { verified = true; }
    this.store.clear();
    this.log('[conn-mgr] recoverOrphans done connector=' + id + ' verified404=' + verified);
    return { recovered: true, connectorId: id, verified };
  }
}

// ── 内存 store(离线默认;live 用 fileStore)────────────────────────────────
function memStore() {
  let rec = null;
  return { load: () => rec, save: (r) => { rec = r; }, clear: () => { rec = null; } };
}

// ── pod-local 文件 store(orphan-recovery 的物理骨干;仿 conv-persist 原子写)──
function fileStore(fp, fs) {
  fs = fs || require('fs');
  return {
    load() { try { return JSON.parse(fs.readFileSync(fp, 'utf8')); } catch (_) { return null; } },
    save(rec) { try { const tmp = fp + '.tmp'; fs.writeFileSync(tmp, JSON.stringify(rec)); fs.renameSync(tmp, fp); } catch (_) {} },
    clear() { try { fs.unlinkSync(fp); } catch (_) {} },
  };
}

// ── live adapter:逐字镜像 mcp-connector-cli.js 的 call/headersFor/classify ──
// sessionHeaders 需含 authorization + cookie(pod 内从 /seed/users.json 建,ephemeral)。
// NOTE: live-enable 受 codex 非回归 gate 阻塞(池 429/401),本工厂在 gate 解除前不接线。
function makeLiveAdapter(sessionHeaders, deps) {
  deps = deps || {};
  const BASE = deps.base || 'https://chatgpt.com';
  const SURFACE = 'CONNECTOR_SETTING';
  const _fetch = deps.fetch || (typeof fetch !== 'undefined' ? fetch : null);
  if (!_fetch) throw new Error('makeLiveAdapter: 无 fetch');

  function headersFor(path, hasBody) {
    const h = Object.assign({}, sessionHeaders);
    const bare = path.split('?')[0];
    h['accept'] = '*/*';
    h['oai-client-surface'] = SURFACE;
    h['x-openai-target-path'] = bare;
    h['x-openai-target-route'] = bare;
    if (hasBody) h['content-type'] = 'application/json'; else delete h['content-type'];
    return h;
  }
  function classify(status, isJson, json) {
    if (!isJson) return status === 403 ? PLANE.CLOUDFLARE : PLANE.NON_JSON;
    const detail = json && json.detail;
    if (status === 403 && typeof detail === 'string' && /required/i.test(detail)) return PLANE.FEATURE_GATE;
    if (status === 404 && detail === 'Not Found') return PLANE.NO_ROUTE;
    if (status === 404) return PLANE.ROUTE_OK_BAD_ID;
    if (status === 405) return PLANE.WRONG_METHOD;
    if (status === 422) return PLANE.SCHEMA;
    if (status === 409) return PLANE.EXISTS;
    if (status >= 200 && status < 300) return PLANE.OK;
    return PLANE.OTHER;
  }
  async function call(method, path, body) {
    let res, text;
    try {
      res = await _fetch(BASE + path, { method, headers: headersFor(path, !!body), body: body ? JSON.stringify(body) : undefined });
      text = await res.text();
    } catch (e) { return { plane: PLANE.NETWORK, status: 0, json: null, raw: '' }; }
    const ctype = (res.headers.get('content-type') || '').split(';')[0].trim();
    const isJson = ctype === 'application/json';
    let json = null; if (isJson) { try { json = JSON.parse(text); } catch (_) {} }
    return { plane: classify(res.status, isJson, json), status: res.status, json, raw: text };
  }
  return {
    async devmodeStatus() {
      const r = await call('GET', '/backend-api/aip/connectors/mcp/tunnels');
      return { enabled: r.plane === PLANE.OK, plane: r.plane };
    },
    async devmodeEnable() {
      const r = await call('PATCH', '/backend-api/settings/account_user_setting?feature=developer_mode&value=true');
      return { ok: r.plane === PLANE.OK, plane: r.plane };
    },
    async register(o) {
      const r = await call('POST', '/backend-api/aip/connectors/mcp', {
        mcp_url: o.url, name: o.name, description: o.name, custom_headers: [], auth_request: { type: 'none' },
      });
      if (r.plane === PLANE.EXISTS) {
        const ex = r.json && r.json.detail && r.json.detail.existing_connector_id;
        return { id: ex || null, plane: r.plane };
      }
      if (r.plane !== PLANE.OK) return { id: null, plane: r.plane };
      const c = (r.json && r.json.connector) || {};
      return { id: c.id || null, plane: r.plane };
    },
    async actions(id) {
      const r = await call('GET', '/backend-api/aip/connectors/' + encodeURIComponent(id) + '/actions');
      return { list: (r.json && r.json.actions) || [], plane: r.plane };
    },
    async link(id, o) {
      const r = await call('POST', '/backend-api/aip/connectors/links/noauth', {
        connector_id: id, name: o.name || 'link', action_names: o.actions || [],
      });
      return { id: (r.json && r.json.id) || null, plane: r.plane };
    },
    async del(id) {
      const r = await call('DELETE', '/backend-api/aip/connectors/' + encodeURIComponent(id));
      return { ok: r.plane === PLANE.OK, plane: r.plane };
    },
  };
}

function isEnabled() { return process.env.ZK_MCP_BRIDGE === '1'; }

module.exports = {
  ConnectorManager, memStore, fileStore, makeLiveAdapter, isEnabled,
  PLANE, GONE_PLANES, STATES: S,
};
