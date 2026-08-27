#!/usr/bin/env node
/* mcp_jsonrpc_offline_cases.js — /mcp 端点 JSON-RPC 派发器离线单测。
 *
 * 直接 require ./mcp_jsonrpc.js。断言每个响应信封形状**逐字对齐 Phase 0 echo server 里
 * 被 ChatGPT 后端实际接受的那份**(/tmp/mcp-echo-server.py,Phase 0.1-0.5 全绿):
 *   initialize / notifications/initialized / tools/list / tools/call(known/unknown) / 未知方法。
 * callTool 注入桩(异步),验证会合委托边界(本模块只包信封,不碰会合表)。
 *
 * 用法: node scripts/zk-cursor-web/mcp_jsonrpc_offline_cases.js
 */
'use strict';
const path = require('path');
const { dispatch, envelope, PROTOCOL_VERSION_DEFAULT } = require(path.join(__dirname, 'mcp_jsonrpc.js'));

let pass = 0, fail = 0;
const fails = [];
function check(name, cond, why) {
  if (cond) { pass++; console.log('[PASS] ' + name); }
  else { fail++; fails.push({ name: name, why: why }); console.log('[FAIL] ' + name + ' — ' + why); }
}

// 与 echo server TOOLS 同形(name/description/inputSchema),验 tools/list 原样回传
const TOOLS = [
  { name: 'shell', description: 'Run a shell command', inputSchema: { type: 'object', properties: { command: { type: 'string' } }, required: ['command'] } },
];
function mkCtx(over) {
  return Object.assign({
    serverInfo: { name: 'zk-mcp-bridge', version: '0.1.0' },
    tools: TOOLS,
    callTool: async function (name, args) { return { text: 'ran ' + name + ' ' + JSON.stringify(args) }; },
  }, over || {});
}

(async function main() {

  // —— initialize:回 {protocolVersion(回显), capabilities:{tools:{}}, serverInfo} ——
  {
    const r = await dispatch({ jsonrpc: '2.0', id: 0, method: 'initialize', params: { protocolVersion: '2025-06-18' } }, mkCtx());
    check('init-status', r.status === 200, 'status=' + r.status);
    check('init-envelope', r.json.jsonrpc === '2.0' && r.json.id === 0 && !!r.json.result && !r.json.error, 'env=' + JSON.stringify(r.json));
    check('init-protoVersion-echo', r.json.result.protocolVersion === '2025-06-18', 'pv=' + r.json.result.protocolVersion);
    check('init-capabilities', JSON.stringify(r.json.result.capabilities) === JSON.stringify({ tools: {} }), 'caps=' + JSON.stringify(r.json.result.capabilities));
    check('init-serverInfo', r.json.result.serverInfo && r.json.result.serverInfo.name === 'zk-mcp-bridge', 'si=' + JSON.stringify(r.json.result.serverInfo));
  }
  // initialize 无 protocolVersion → 用默认 2025-06-18(echo server 同款默认)
  {
    const r = await dispatch({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }, mkCtx());
    check('init-default-pv', r.json.result.protocolVersion === PROTOCOL_VERSION_DEFAULT && PROTOCOL_VERSION_DEFAULT === '2025-06-18', 'pv=' + r.json.result.protocolVersion);
  }

  // —— notifications/initialized:无回复,HTTP 202(本模块 {notification:true}) ——
  {
    const r = await dispatch({ jsonrpc: '2.0', method: 'notifications/initialized' }, mkCtx());
    check('notif-initialized', r.notification === true && r.status === undefined, 'r=' + JSON.stringify(r));
  }
  // 任意 notifications/* 前缀同样按通知
  {
    const r = await dispatch({ jsonrpc: '2.0', method: 'notifications/cancelled', params: {} }, mkCtx());
    check('notif-prefix', r.notification === true, 'r=' + JSON.stringify(r));
  }
  // id 缺失(JSON-RPC 规范:无 id = 通知)→ 通知
  {
    const r = await dispatch({ jsonrpc: '2.0', method: 'some/thing' }, mkCtx());
    check('notif-no-id', r.notification === true, 'r=' + JSON.stringify(r));
  }

  // —— tools/list:原样回 ctx.tools ——
  {
    const r = await dispatch({ jsonrpc: '2.0', id: 2, method: 'tools/list' }, mkCtx());
    check('list-status', r.status === 200 && r.json.id === 2, 'r=' + JSON.stringify(r.json));
    check('list-tools', JSON.stringify(r.json.result.tools) === JSON.stringify(TOOLS), 'tools=' + JSON.stringify(r.json.result.tools));
  }

  // —— tools/call 已知工具:回 {content:[{type:text,text}]},且 await 了 callTool ——
  {
    let seenName = null, seenArgs = null, seenMeta = null;
    const ctx = mkCtx({ callTool: async function (name, args, meta) { seenName = name; seenArgs = args; seenMeta = meta; return { text: 'file1\nfile2\n(exit 0)' }; } });
    const r = await dispatch({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'shell', arguments: { command: 'ls' } } }, ctx);
    check('call-status', r.status === 200 && r.json.id === 3, 'r=' + JSON.stringify(r.json));
    check('call-content-shape', Array.isArray(r.json.result.content) && r.json.result.content[0].type === 'text' && r.json.result.content[0].text === 'file1\nfile2\n(exit 0)', 'content=' + JSON.stringify(r.json.result.content));
    check('call-delegated-name-args', seenName === 'shell' && JSON.stringify(seenArgs) === JSON.stringify({ command: 'ls' }), 'name=' + seenName + ' args=' + JSON.stringify(seenArgs));
    check('call-meta-id', seenMeta && seenMeta.id === 3, 'meta=' + JSON.stringify(seenMeta));
    check('call-no-isError', r.json.result.isError === undefined, 'isError leaked=' + r.json.result.isError);
  }

  // —— tools/call 未知工具:error -32601(echo server 同款) ——
  {
    const r = await dispatch({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'nope', arguments: {} } }, mkCtx());
    check('call-unknown-tool', r.status === 200 && r.json.error && r.json.error.code === -32601 && !r.json.result, 'r=' + JSON.stringify(r.json));
  }

  // —— tools/call callTool 抛错:回 MCP isError 结果(而非 JSON-RPC error),让模型看到可读文本 ——
  {
    const ctx = mkCtx({ callTool: async function () { throw new Error('rendezvous timeout'); } });
    const r = await dispatch({ jsonrpc: '2.0', id: 5, method: 'tools/call', params: { name: 'shell', arguments: { command: 'x' } } }, ctx);
    check('call-throw-isError', r.status === 200 && r.json.result && r.json.result.isError === true && /rendezvous timeout/.test(r.json.result.content[0].text) && !r.json.error, 'r=' + JSON.stringify(r.json));
  }

  // —— tools/call callTool 返回 isError:透传 isError,包成 content ——
  {
    const ctx = mkCtx({ callTool: async function () { return { text: 'ticket #abc — poll later', isError: true }; } });
    const r = await dispatch({ jsonrpc: '2.0', id: 6, method: 'tools/call', params: { name: 'shell', arguments: {} } }, ctx);
    check('call-isError-passthrough', r.json.result.isError === true && r.json.result.content[0].text === 'ticket #abc — poll later', 'r=' + JSON.stringify(r.json));
  }

  // —— 未知方法(含 OpenAI 私有 server/discover):-32601(Phase 0 实证 ChatGPT 接受此行为) ——
  {
    const r = await dispatch({ jsonrpc: '2.0', id: 'openai-mcp-discover', method: 'server/discover' }, mkCtx());
    check('discover-32601', r.status === 200 && r.json.error && r.json.error.code === -32601 && r.json.id === 'openai-mcp-discover', 'r=' + JSON.stringify(r.json));
  }
  {
    const r = await dispatch({ jsonrpc: '2.0', id: 9, method: 'wat/ever' }, mkCtx());
    check('unknown-method-32601', r.json.error && r.json.error.code === -32601, 'r=' + JSON.stringify(r.json));
  }

  // —— envelope 工具:result 与 error 互斥;id 缺失 → null ——
  {
    const e1 = envelope(7, { ok: 1 });
    check('env-result-only', e1.jsonrpc === '2.0' && e1.id === 7 && JSON.stringify(e1.result) === '{"ok":1}' && e1.error === undefined, 'e1=' + JSON.stringify(e1));
    const e2 = envelope(8, null, { code: -1, message: 'x' });
    check('env-error-only', e2.error && e2.error.code === -1 && e2.result === undefined, 'e2=' + JSON.stringify(e2));
    const e3 = envelope(undefined, { ok: 1 });
    check('env-id-null', e3.id === null, 'e3=' + JSON.stringify(e3));
  }

  // —— 坏 body:400 ——
  {
    const r = await dispatch(null, mkCtx());
    check('bad-body-400', r.status === 400, 'r=' + JSON.stringify(r));
  }

  console.log('\n== ' + pass + '/' + (pass + fail) + ' PASS ==');
  if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1); }
  console.log('VERDICT: GO (initialize 回显/默认 + notifications 三形态 202 + tools/list 原样 + tools/call content/未知工具/抛错 isError/isError 透传 + server/discover -32601 + envelope 互斥/坏 body,全对齐 Phase 0 验证过的 echo 契约)');
})();
