#!/usr/bin/env node
/**
 * mcp-connector-cli.js — ChatGPT 网页版 MCP connector 管理
 *
 * 网页版有协议级原生 tool call:注册一个远程 MCP server 成 connector,
 * OpenAI 服务端会主动抓它的 JSON Schema。不需要提示词诱导。
 * 背景与实测记录:docs/zerokey-bridge/mcp-connector-native-toolcall.md
 *
 * 用法(session bundle 见 --help):
 *   node mcp-connector-cli.js --session sess.json devmode-status
 *   node mcp-connector-cli.js --session sess.json devmode-enable
 *   node mcp-connector-cli.js --session sess.json register --url https://x/mcp --name lark
 *   node mcp-connector-cli.js --session sess.json actions <connector_id>
 *   node mcp-connector-cli.js --session sess.json delete <connector_id>
 *   node mcp-connector-cli.js --session sess.json probe          # 端到端自检(自动清理)
 *
 * 两个反复踩到的坑,已在本文件内固化:
 *   1. 必须发 x-openai-target-path / x-openai-target-route,否则拿到
 *      Cloudflare 的 403 + HTML,极易误读成"账号没权限"。
 *   2. custom_headers 必须是 list,auth_request 必须是 object(不能 null)。
 */

'use strict';

const fs = require('fs');

const BASE = 'https://chatgpt.com';
const SURFACE = 'CONNECTOR_SETTING';

// ---------------------------------------------------------------- session

function loadSession(p) {
  if (!p) die('缺少 --session <sess.json>。用 --help 看怎么取。');
  if (!fs.existsSync(p)) die(`session 文件不存在: ${p}`);
  let s;
  try {
    s = JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch (e) {
    die(`session 不是合法 JSON: ${e.message}`);
  }
  const h = s.headers || s;
  if (!h.authorization) die('session 里没有 authorization 头 —— 不是有效的 bundle。');
  if (!h.cookie) warn('session 里没有 cookie 头,可能会被 Cloudflare 拦。');
  return h;
}

/** 组装请求头。路由头是必需的,少了就会撞 Cloudflare。 */
function headersFor(sessionHeaders, path, hasBody) {
  const h = Object.assign({}, sessionHeaders);
  const bare = path.split('?')[0];

  h['accept'] = '*/*';
  h['oai-client-surface'] = SURFACE;
  h['x-openai-target-path'] = bare;   // ← 坑 1
  h['x-openai-target-route'] = bare;

  if (hasBody) h['content-type'] = 'application/json';
  else delete h['content-type'];

  return h;
}

// -------------------------------------------------------------- transport

/**
 * 发请求并把"打到哪个平面"分类出来 —— 这是本文件最有价值的部分。
 * 状态码单独看会骗人:403+HTML(Cloudflare)和 403+JSON(功能门)含义完全不同。
 */
async function call(sessionHeaders, method, path, body) {
  const headers = headersFor(sessionHeaders, path, !!body);
  let res, text;
  try {
    res = await fetch(BASE + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    text = await res.text();
  } catch (e) {
    return { plane: 'NETWORK', status: 0, raw: '', json: null, detail: e.message };
  }

  const ctype = (res.headers.get('content-type') || '').split(';')[0].trim();
  const isJson = ctype === 'application/json';

  let json = null;
  if (isJson) { try { json = JSON.parse(text); } catch (e) { /* 留 null */ } }

  return {
    plane: classify(res.status, isJson, json),
    status: res.status,
    isJson,
    json,
    raw: text,
    detail: json && typeof json.detail === 'string' ? json.detail : null,
  };
}

/** 见 docs/zerokey-bridge/mcp-connector-native-toolcall.md §6 的判定表 */
function classify(status, isJson, json) {
  if (!isJson) return status === 403 ? 'CLOUDFLARE' : 'NON_JSON';

  const detail = json && json.detail;

  if (status === 403 && typeof detail === 'string' && /required/i.test(detail)) {
    return 'FEATURE_GATE';       // 路由存在,只是没开 —— 最容易误读成没权限
  }
  if (status === 404 && detail === 'Not Found') return 'NO_ROUTE';
  if (status === 404) return 'ROUTE_OK_BAD_ID'; // 如 "Connector not found"
  if (status === 405) return 'WRONG_METHOD';
  if (status === 422) return 'SCHEMA';          // 已达 handler,看 loc 改字段
  if (status >= 200 && status < 300) return 'OK';
  return 'OTHER';
}

function explain(r) {
  const hint = {
    OK: '成功',
    CLOUDFLARE: '没到 API(Cloudflare)—— 检查是否漏发 x-openai-target-path',
    FEATURE_GATE: '路由存在,是功能门 —— 需要先开启对应 feature',
    NO_ROUTE: '路由不存在',
    ROUTE_OK_BAD_ID: '路由存在,路径段被当成 ID 解析了',
    WRONG_METHOD: '路由存在,method 不对',
    SCHEMA: '已到达目标 handler,只是字段不对(看 loc)',
    NETWORK: '网络层失败',
  }[r.plane] || '未分类';
  return `${r.status} [${r.plane}] ${hint}`;
}

// --------------------------------------------------------------- commands

async function devmodeStatus(h) {
  // tunnels 是最干净的探针:没开 → FEATURE_GATE,开了 → OK
  const r = await call(h, 'GET', '/backend-api/aip/connectors/mcp/tunnels');
  log(explain(r));
  if (r.plane === 'OK') { log('developer_mode: 已开启'); return true; }
  if (r.plane === 'FEATURE_GATE') { log(`developer_mode: 未开启 (${r.detail})`); return false; }
  log('无法判定,原始响应: ' + r.raw.slice(0, 200));
  return false;
}

async function devmodeEnable(h) {
  // 参数必须走 query;放 body 会 422 且 loc 会指向 ["query","feature"]
  const r = await call(
    h, 'PATCH',
    '/backend-api/settings/account_user_setting?feature=developer_mode&value=true'
  );
  log(explain(r));
  if (r.plane === 'OK') { log('结果: ' + r.raw.slice(0, 120)); return true; }
  log('失败: ' + r.raw.slice(0, 300));
  return false;
}

async function register(h, opts) {
  if (!opts.url) die('register 需要 --url https://<host>/mcp');
  const body = {
    mcp_url: opts.url,
    name: opts.name || 'connector',
    description: opts.description || opts.name || 'connector',
    custom_headers: [],              // ← 坑 2:必须 list
    auth_request: { type: 'none' },  // ← 坑 2:必须 object
  };
  const r = await call(h, 'POST', '/backend-api/aip/connectors/mcp', body);
  log(explain(r));

  if (r.plane !== 'OK') {
    if (r.plane === 'FEATURE_GATE') log('提示:先跑 devmode-enable');
    if (r.plane === 'SCHEMA') log('schema 报错: ' + JSON.stringify(r.json && r.json.detail));
    return null;
  }
  const c = (r.json && r.json.connector) || {};
  log(`已注册 id=${c.id}  type=${c.connector_type}  base_url=${c.base_url}`);
  return c.id || null;
}

async function actions(h, id) {
  if (!id) die('actions 需要 <connector_id>');
  const r = await call(h, 'GET', `/backend-api/aip/connectors/${encodeURIComponent(id)}/actions`);
  log(explain(r));
  if (r.plane !== 'OK') { log(r.raw.slice(0, 300)); return []; }

  const list = (r.json && r.json.actions) || [];
  log(`OpenAI 抓到 ${list.length} 个 action:`);
  for (const a of list) {
    const req = (a.params && a.params.required) || [];
    log(`  - ${a.name}  enabled=${a.is_enabled}  consequential=${a.is_consequential}`);
    log(`      required: [${req.join(', ')}]`);
    if (a.description) log(`      ${String(a.description).slice(0, 100)}`);
  }
  return list;
}

async function del(h, id) {
  if (!id) die('delete 需要 <connector_id>');
  const r = await call(h, 'DELETE', `/backend-api/aip/connectors/${encodeURIComponent(id)}`);
  log(explain(r));
  return r.plane === 'OK';
}

/** 端到端自检:注册 → 读 schema → 删除。用公开 MCP server,不动生产。 */
async function probe(h) {
  const URL = 'https://mcp.deepwiki.com/mcp';
  log('=== MCP connector 通路自检 ===');

  log('\n[1/4] developer mode');
  if (!(await devmodeStatus(h))) {
    log('  → 尝试开启');
    if (!(await devmodeEnable(h))) return fail('无法开启 developer mode');
    if (!(await devmodeStatus(h))) return fail('开启后仍未生效');
  }

  log(`\n[2/4] 注册探针 ${URL}`);
  const id = await register(h, { url: URL, name: 'probe_mcp', description: 'probe' });
  if (!id) return fail('注册失败');

  let ok = false;
  try {
    log('\n[3/4] 读取 action schema');
    const list = await actions(h, id);
    ok = list.length > 0;
    if (!ok) log('  ⚠ 注册成功但没抓到 action');
  } finally {
    // 无论成败都清理,别在账号上留垃圾
    log('\n[4/4] 清理探针');
    const gone = await del(h, id);
    if (!gone) log(`  ⚠ 删除失败,请手动清理: ${id}`);
  }

  log('\n=== ' + (ok ? '通路可用(schema 由服务端抓取,协议级)' : '通路不完整') + ' ===');
  return ok;
}

// ------------------------------------------------------------------ shell

function log(s) { process.stdout.write(s + '\n'); }
function warn(s) { log('警告: ' + s); }
function die(s) { log('错误: ' + s); process.exit(2); }
function fail(s) { log('失败: ' + s); return false; }

const HELP = `mcp-connector-cli.js — ChatGPT 网页版 MCP connector 管理

命令:
  devmode-status              查 developer mode 是否开启
  devmode-enable              开启 developer mode(注册的前置条件)
  register --url U [--name N] 注册远程 MCP server
  actions <connector_id>      列出 OpenAI 抓到的 action schema
  delete <connector_id>       删除 connector
  probe                       端到端自检(注册→读schema→删除,自动清理)

选项:
  --session <sess.json>       必需。ChatGPT 会话头 bundle
  --url / --name / --description

取 session bundle:
  从 zerokey pod 的 users.json 里取
  chatgpt → <acct> → parsedFetch,含 {url, headers, body}。
  headers 必须含 authorization 与 cookie。

前置条件:
  账号 plan_type=pro 且 enabledConnectors 含 mcp_connector
  (可从 /backend-api/accounts/check/v4-2023-04-27 确认)

MCP server 硬要求:远程 HTTPS(Streamable HTTP / SSE)。
不支持本地 stdio —— 所以"在用户本机跑 shell"仍需 exec-harvest。

背景: docs/zerokey-bridge/mcp-connector-native-toolcall.md`;

async function main() {
  const argv = process.argv.slice(2);
  if (!argv.length || argv.includes('--help') || argv.includes('-h')) {
    log(HELP);
    return 0;
  }

  const opts = {};
  const pos = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) opts[a.slice(2)] = argv[++i];
    else pos.push(a);
  }

  const cmd = pos[0];
  const h = loadSession(opts.session);

  switch (cmd) {
    case 'devmode-status': return (await devmodeStatus(h)) ? 0 : 1;
    case 'devmode-enable': return (await devmodeEnable(h)) ? 0 : 1;
    case 'register':       return (await register(h, opts)) ? 0 : 1;
    case 'actions':        return (await actions(h, pos[1])).length ? 0 : 1;
    case 'delete':         return (await del(h, pos[1])) ? 0 : 1;
    case 'probe':          return (await probe(h)) ? 0 : 1;
    default: die(`未知命令: ${cmd}。用 --help 看用法。`);
  }
}

main()
  .then(c => process.exit(c))
  .catch(e => { log('未捕获异常: ' + (e && e.stack || e)); process.exit(3); });
