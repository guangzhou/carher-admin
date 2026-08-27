#!/usr/bin/env node
/* mcp_probe_server.js — A/B 流缝合判定探针的最小 /mcp 服务(一次性,用完即拆)。
 *
 * 只为回答设计文档 §13.3 的**唯一承重未知**:ChatGPT 后端经 f/conversation 驱动、模型调用
 * MCP connector 工具时,那条 SSE 是(A)同连接挂起等 /mcp 结果再续,还是(B)结束本轮靠后续续接?
 *
 * 本服务 = mcp_jsonrpc.dispatch 的最薄 HTTP 包壳:
 *   - tools/list 报**一个**诊断工具 zk_probe;
 *   - tools/call **秒回**固定 marker(不真会合)——让本轮能尽快走完,好观测 f/conversation SSE
 *     在 tool-call 之后是继续(A)还是断(B);
 *   - 每条请求落 /tmp/mcp-probe.log(带时间戳),与裸 SSE dump 对时。
 *
 * 非会合桥生产件:不 require mcp_rendezvous / mcp_bridge_glue,callTool 是常量桩。
 * 监听 127.0.0.1:$ZK_MCP_PROBE_PORT(默认 8145),由 198 host nginx 反代到公网。
 *
 * 用法: ZK_MCP_PROBE_PORT=8145 node scripts/zk-cursor-web/mcp_probe_server.js
 *
 * Gate-1(§13.6 长挂起保活):设 ZK_MCP_PROBE_DELAY_MS=30000/45000/60000,callTool 挂起该时长再回
 *   marker(模拟真实桥等 Cursor 执行),据此观测 f/conversation SSE 在长挂起下 reader 是否保活/被掐。
 *   默认 0 = 秒回(§13.3 A/B 裁决用的形态)。
 */
'use strict';
const http = require('http');
const path = require('path');
const fs = require('fs');
const { dispatch } = require(path.join(__dirname, 'mcp_jsonrpc.js'));

const PORT = parseInt(process.env.ZK_MCP_PROBE_PORT || '8145', 10);
const HOST = process.env.ZK_MCP_PROBE_HOST || '127.0.0.1';
const LOG = process.env.ZK_MCP_PROBE_LOG || '/tmp/mcp-probe.log';
const MARKER = process.env.ZK_MCP_PROBE_MARKER || 'ZK7391';
const DELAY_MS = parseInt(process.env.ZK_MCP_PROBE_DELAY_MS || '0', 10);   // Gate-1:挂起时长

function log(obj) {
  const line = JSON.stringify(Object.assign({ ts: new Date().toISOString() }, obj));
  try { fs.appendFileSync(LOG, line + '\n'); } catch (e) { /* best-effort */ }
  console.log(line);
}

// 单一诊断工具:描述里写清"被要求跑探针时调用",让模型有明确触发条件。
const TOOLS = [{
  name: 'zk_probe',
  description: 'Diagnostic probe. When the user asks to "run the zk probe" or "call zk_probe", call this tool with an empty arguments object {}. It returns a short fixed marker string. Use it exactly once when asked.',
  inputSchema: { type: 'object', properties: {}, required: [], additionalProperties: false },
}];

const ctx = {
  serverInfo: { name: 'zk-mcp-probe', version: '0.0.1' },
  tools: TOOLS,
  // 秒回桩 / Gate-1 挂起桩:记录调用,(可选)挂 DELAY_MS 再返回 marker(不真会合)。
  callTool: async function (name, args, meta) {
    log({ ev: 'tools/call', name: name, args: args, id: meta && meta.id, delayMs: DELAY_MS });
    if (DELAY_MS > 0) {
      log({ ev: 'suspend-begin', delayMs: DELAY_MS, id: meta && meta.id });
      await new Promise(function (r) { setTimeout(r, DELAY_MS); });
      log({ ev: 'suspend-end', delayMs: DELAY_MS, id: meta && meta.id });
    }
    return { text: 'ZKPROBE_RESULT: bridge-alive marker=' + MARKER };
  },
};

const server = http.createServer(function (req, res) {
  if (req.method === 'GET') {
    log({ ev: 'GET', url: req.url, ua: req.headers['user-agent'] });
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, server: 'zk-mcp-probe' }));
    return;
  }
  if (req.method !== 'POST') { res.writeHead(405); res.end(); return; }
  const chunks = [];
  req.on('data', function (c) { chunks.push(c); });
  req.on('end', async function () {
    const raw = Buffer.concat(chunks).toString('utf8');
    let body = null;
    try { body = JSON.parse(raw); } catch (e) { /* dispatch 会回 400 */ }
    log({ ev: 'POST', url: req.url, method: body && body.method, id: body && body.id, accept: req.headers['accept'], raw_len: raw.length });
    let r;
    try { r = await dispatch(body, ctx); }
    catch (e) { log({ ev: 'dispatch-error', err: String(e) }); res.writeHead(500); res.end(); return; }
    if (r.notification) { res.writeHead(202).end(); return; }
    res.writeHead(r.status || 200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(r.json));
  });
  req.on('error', function (e) { log({ ev: 'req-error', err: String(e) }); });
});

server.listen(PORT, HOST, function () { log({ ev: 'listen', host: HOST, port: PORT, log: LOG, delayMs: DELAY_MS }); });
