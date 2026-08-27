#!/usr/bin/env node
/* mcp_bridge_glue.js — 会合桥编排胶水:把 JSON-RPC 的 callTool 接到会合表(Phase 1)。
 *
 * 本件是**分支无关**的那层:无论流缝合走 A(挂起-续流)还是 B(轮末-续轮),
 * callTool 的编排都一样 —— 派会话、铸稳定 call_id、deliver、await、格式化 result/ticket。
 * 唯一分支相关的"把命令送到 Cursor"用注入的 `inject(session, callId, tool, args)` 缝合口占位,
 * 测试用桩替身,生产侧才填真缝合(见设计文档 §13.3 A/B)。不碰 responses.js,可离线单测。
 *
 * 关键正确性(Phase 0 日志实录强制,见设计文档 §13.1):
 *   JSON-RPC `id` **不能**当会合键 —— 它是连接内计数器,retry-storm 重发全复用 0/1。
 *   会合键必须是 `deriveCallId(session, tool, args)` = 对 (session, tool, 规范化args) 的稳定哈希:
 *   - 同一逻辑调用的 ~60s 重发 → 同哈希 → 命中在途 entry 幂等去重(修正 1),命令只下发一次。
 *   - args 键序不同但语义相同 → 规范化 JSON → 同哈希(客户端重排 key 不误判成新调用)。
 *
 * ttl 与"合法重复调用"的张力(如实记,Phase 2 调参):RESOLVED entry 留 ttlMs 供 broken-pipe
 * 重发回读 stash(修正 4);但若模型在 ttl 内**真的**又想跑同 (session,tool,args),会误得旧 stash。
 * 缓解 = ttlMs 收到"略大于 retry 周期"(~90s)而非 5min;retry 周期 ~60s,合法重复同参同会话在
 * 90s 内极罕见。这是 tuning,不是本件逻辑缺陷;ttlMs 由 RendezvousTable 构造参数控制。
 */
'use strict';
const crypto = require('crypto');

/* 规范化 JSON:对象键排序,保证 {a:1,b:2} 与 {b:2,a:1} 同串。数组保序(顺序有语义)。 */
function canonical(v) {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  const keys = Object.keys(v).sort();
  return '{' + keys.map(function (k) { return JSON.stringify(k) + ':' + canonical(v[k]); }).join(',') + '}';
}

/* 稳定会合键:session\x00tool\x00canonical(args) 的 sha1 前 16 hex。确定性,无时钟/随机。 */
function deriveCallId(session, tool, args) {
  const h = crypto.createHash('sha1');
  h.update(String(session)); h.update('\x00'); h.update(String(tool)); h.update('\x00'); h.update(canonical(args == null ? {} : args));
  return h.digest('hex').slice(0, 16);
}

/* 造 callTool(name, args, meta):供 mcp_jsonrpc.dispatch 的 ctx.callTool 用。
 * opts = {
 *   rendezvous,                         // RendezvousTable 实例
 *   pickSession: () => sessionId,       // 方案 B:取最近在挂的 /v1/responses 会话
 *   inject: (session, callId, tool, args) => void,  // 缝合口(分支相关);isNew 时调用一次
 *   ticketText?: (callId) => string,    // 取号降级回给模型的文本
 * }
 * 返回 async (name, args, meta) => { text, isError? }(mcp_jsonrpc 会包成 MCP content)。 */
function makeCallTool(opts) {
  const rv = opts.rendezvous;
  const pickSession = opts.pickSession;
  const inject = opts.inject;
  const ticketText = opts.ticketText || function (callId) {
    return '命令已下发执行中(call_id=' + callId + ')。如需结果,请用 poll 查询该 call_id;不要重复下发同一命令。';
  };
  return async function callTool(name, args, meta) {
    const session = pickSession();
    const callId = deriveCallId(session, name, args);
    const d = rv.deliver(callId, session, name, args);
    if (d.isNew) {
      // 缝合口:把命令送到 Cursor(分支 A/B 各自实现)。注入失败不崩 ——
      // 结果要么被 Cursor 迟到 output 补上,要么 sweep 到 ticketMs 走取号降级。
      try { inject(session, callId, name, args); rv.markInjected(callId); }
      catch (e) { /* broken injection 容错:留给 ticket 降级兜底,不抛 */ }
    }
    const r = await d.promise;   // {status:'result', output} | {status:'ticket'}
    if (r && r.status === 'result') return { text: String(r.output == null ? '' : r.output) };
    return { text: ticketText(callId), isError: true };
  };
}

module.exports = { deriveCallId: deriveCallId, makeCallTool: makeCallTool, canonical: canonical };
