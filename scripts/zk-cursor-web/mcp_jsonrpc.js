#!/usr/bin/env node
/* mcp_jsonrpc.js — MCP Streamable-HTTP JSON-RPC 派发器(会合桥 /mcp 端点的框架层)。
 *
 * 只做 JSON-RPC 2.0 信封的解析/构造,不碰 HTTP、不碰会合表、不碰 responses.js。
 * tools/call 委托给注入的 async callTool(会合桥里=会合表 deliver+await);本模块只负责
 * 把结果包成 MCP `{content:[{type:'text',text}]}`。可离线单测(mcp_jsonrpc_offline_cases.js)。
 *
 * 响应形状**逐字镜像 Phase 0 echo server 里被 ChatGPT 后端实际接受的那份**
 * (/home/cltx/mcp-echo-server.py,Phase 0.1-0.5 全绿):
 *   initialize → {protocolVersion, capabilities:{tools:{}}, serverInfo:{name,version}}
 *   notifications/initialized → 无回(HTTP 202,本模块回 {notification:true})
 *   tools/list → {tools:[{name,description,inputSchema}]}
 *   tools/call → {content:[{type:'text',text}]};未知工具 → error -32601
 *   未知方法 → error -32601
 *
 * Phase 0 日志实录的真实调用序列(/home/cltx/mcp-echo.log):
 *   initialize(id=0,重发×5) → notifications/initialized(id=None) → server/discover
 *   (id=openai-mcp-discover,OpenAI 私有) → tools/list(id=1/2) → tools/call(id=0,重发×13)。
 *   - `server/discover`:echo 未处理 → 落未知方法 -32601,Phase 0 仍 GO → **落 error 分支是已验证可接受行为**,照抄。
 *   - tools/call id=0 重发×13 = retry-storm 现场(会合桥靠 hash(session+tool+args) 幂等去重,见会合表)。
 */
'use strict';

const PROTOCOL_VERSION_DEFAULT = '2025-06-18';

function envelope(id, result, error) {
  const r = { jsonrpc: '2.0', id: id === undefined ? null : id };
  if (error) r.error = error; else r.result = result;
  return r;
}

function errObj(code, message) { return { code: code, message: message }; }

/* 派发一条 JSON-RPC body。返回:
 *   {notification:true}                          → 通知类(无 id 回复),HTTP 层回 202 空体
 *   {status:200, json:<envelope>}                → 正常单响应
 * ctx = {
 *   serverInfo: {name, version},
 *   protocolVersion?: string,
 *   tools: [{name, description, inputSchema}],    // tools/list 回这份
 *   callTool: async (name, args, meta) => { text }  // meta={id}; 返回 {text} 或 {text, isError}
 * }
 * 本函数 async(因为 tools/call 要 await 会合)。
 */
async function dispatch(body, ctx) {
  if (!body || typeof body !== 'object') {
    return { status: 400, json: { error: 'bad json' } };
  }
  const method = body.method;
  const id = body.id;
  const params = body.params || {};

  if (method === 'initialize') {
    return { status: 200, json: envelope(id, {
      protocolVersion: params.protocolVersion || ctx.protocolVersion || PROTOCOL_VERSION_DEFAULT,
      capabilities: { tools: {} },
      serverInfo: ctx.serverInfo || { name: 'zk-mcp-bridge', version: '0.1.0' },
    }) };
  }

  // 通知类(method 以 notifications/ 开头、或 id 缺失):无回复,HTTP 202
  if (method === 'notifications/initialized' || (typeof method === 'string' && method.indexOf('notifications/') === 0) || id === undefined || id === null) {
    if (typeof method === 'string' && method.indexOf('notifications/') === 0) return { notification: true };
    // id 缺失但非 notifications/*:仍按通知处理(JSON-RPC 规范:无 id = 通知)
    if (id === undefined || id === null) return { notification: true };
  }

  if (method === 'tools/list') {
    return { status: 200, json: envelope(id, { tools: ctx.tools || [] }) };
  }

  if (method === 'tools/call') {
    const name = params.name;
    const args = params.arguments || {};
    const known = (ctx.tools || []).some(function (t) { return t && t.name === name; });
    if (!known) {
      return { status: 200, json: envelope(id, null, errObj(-32601, 'unknown tool: ' + name)) };
    }
    let out;
    try {
      out = await ctx.callTool(name, args, { id: id });
    } catch (e) {
      // callTool 抛错:回成 MCP isError 结果(而非 JSON-RPC error),让模型看到可读文本继续
      return { status: 200, json: envelope(id, {
        content: [{ type: 'text', text: 'tool error: ' + (e && e.message ? e.message : String(e)) }],
        isError: true,
      }) };
    }
    const text = (out && typeof out.text === 'string') ? out.text : String(out == null ? '' : out);
    const result = { content: [{ type: 'text', text: text }] };
    if (out && out.isError) result.isError = true;
    return { status: 200, json: envelope(id, result) };
  }

  // 未知方法(含 server/discover):落 -32601 —— Phase 0 实证此行为被 ChatGPT 接受,GO。
  return { status: 200, json: envelope(id, null, errObj(-32601, 'unknown method: ' + method)) };
}

module.exports = { dispatch: dispatch, envelope: envelope, PROTOCOL_VERSION_DEFAULT: PROTOCOL_VERSION_DEFAULT };
