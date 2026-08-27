#!/usr/bin/env node
/* mcp_rendezvous.js — MCP 会合桥承重原语:会合表状态机(Phase 1)。
 *
 * 拓扑(见 docs/zerokey-bridge/mcp-rendezvous-bridge-design.md §3):
 *   ChatGPT 后端 --MCP tools/call(HTTP 挂起)--> 网关 /mcp
 *     --> 会合表配对到某条在挂的 Cursor /v1/responses 流
 *     --> 以 function_call 下发 Cursor 执行
 *     --> Cursor 下一轮 function_call_output 回来 --> 按 call_id 唤醒挂起的 MCP 请求。
 *
 * 本模块只做"会合"这一件承重事,不碰 HTTP、不碰 responses.js,可离线单测(mcp_rendezvous_offline_cases.js)。
 * 验过的字节再嫁接进 responses.js(全部锁在 ZK_MCP_BRIDGE==='1' 后,默认关=零行为差)。
 *
 * Phase 0 数据强制的四条设计修正(设计文档 §12,逐条落地):
 *   1. call_id 幂等去重:ChatGPT 后端对同 call_id 每 ~60s 重发 tools/call。重发命中在途 entry
 *      直接复用同一 resolve/promise,绝不新建第二个 function_call → 命令不会被 Cursor 重复执行。
 *   2. 应答 ≤30s(ticketMs)不吃满 60s:挂起 30s 内没等到 Cursor output → 立即"取号降级"
 *      (MCP 回 {status:'ticket'},模型稍后用 poll action 查),避免撞 ChatGPT 60s 超时触发 retry-storm。
 *   3. url-safe 回填 MCP 路免做(0.5 实测 URL 原样穿透)—— 本模块不含任何 url 改写,原样透传 output。
 *   4. broken-pipe 容错:ChatGPT 可能在网关应答前掐连接。结果始终 stash 在 entry.result;
 *      /mcp 写回 try/catch,写失败不崩,下次同 call_id 重发时直接从 stash 返回。
 *
 * 时钟:本表**不内置 setTimeout**。ticket/GC 迁移全靠显式 sweep(nowMs) 驱动 ——
 *   生产侧 setInterval(()=>table.sweep(Date.now()), 1000) 打点;测试侧手打时间戳 = 完全确定性。
 */
'use strict';

const PENDING = 'pending';    // 已(或即将)下发 function_call,等 Cursor output,ticket 计时中
const TICKETED = 'ticketed';  // 过 ticketMs 未回,已用取号应答 MCP;仍留着等 output 落 stash
const RESOLVED = 'resolved';  // Cursor output 已回,结果 stash;留 ttlMs 供 poll / 重发 / broken-pipe 读

class RendezvousTable {
  constructor(opts) {
    opts = opts || {};
    this.ticketMs = opts.ticketMs != null ? opts.ticketMs : 30000;   // ≤30s 取号降级(修正 2)
    this.ttlMs = opts.ttlMs != null ? opts.ttlMs : 300000;           // resolved/ticketed 结果保活 5min
    this._now = opts.now || function () { return Date.now(); };
    this.map = new Map();                                            // call_id -> entry
    this.stats = { delivered: 0, deduped: 0, resolved: 0, ticketed: 0, orphanOutputs: 0, gc: 0 };
  }

  _mk(callId, session, tool, args, t) {
    let resolveFn;
    const promise = new Promise(function (r) { resolveFn = r; });
    const entry = {
      callId: callId, session: session, tool: tool, args: args,
      state: PENDING,
      createdAt: t,
      ticketDeadline: t + this.ticketMs,
      ttlDeadline: null,
      resolve: resolveFn,   // settle 挂起的 MCP 请求(只 settle 一次)
      settled: false,
      result: null,         // stash 的 Cursor output(poll / 重发 / broken-pipe 回读)
      promise: promise,
      injected: false,      // responses.js 是否已就此 entry 发出 function_call
    };
    this.map.set(callId, entry);
    return entry;
  }

  /* ChatGPT 后端 MCP tools/call 到达。返回 {entry, promise, isNew, dedup}。
   * isNew=true → 调用方(/mcp handler)须在配对的 Cursor /v1/responses 流上注入一个 function_call;
   * isNew=false → 幂等重发/已就绪,不得重复注入。调用方 await promise 拿 {status:'result'|'ticket', ...}。 */
  deliver(callId, session, tool, args) {
    const ex = this.map.get(callId);
    if (ex) {
      this.stats.deduped++;                                          // 修正 1:幂等去重
      if (ex.state === RESOLVED) {
        // broken-pipe 恢复 / 迟到重发:立刻回 stash 的结果(修正 4)
        return { entry: ex, promise: Promise.resolve({ status: 'result', output: ex.result, callId: callId }), isNew: false, dedup: 'resolved' };
      }
      if (ex.state === TICKETED) {
        // 已取号应答过,结果还没落 → 继续回取号,让模型走 poll
        return { entry: ex, promise: Promise.resolve({ status: 'ticket', callId: callId }), isNew: false, dedup: 'ticketed' };
      }
      // PENDING:复用同一 resolve/promise,绝不二次注入(修正 1 核心)
      return { entry: ex, promise: ex.promise, isNew: false, dedup: 'pending' };
    }
    const entry = this._mk(callId, session, tool, args, this._now());
    this.stats.delivered++;
    return { entry: entry, promise: entry.promise, isNew: true, dedup: null };
  }

  /* /mcp handler 成功在 Cursor 流上注入 function_call 后标记(便于诊断 + 防漏注入统计)。 */
  markInjected(callId) {
    const e = this.map.get(callId);
    if (e) e.injected = true;
    return !!e;
  }

  /* Cursor 下一轮 function_call_output 到达。按 call_id 唤醒挂起的 MCP 请求。返回是否命中。 */
  complete(callId, output) {
    const e = this.map.get(callId);
    if (!e) { this.stats.orphanOutputs++; return false; }           // 非本表的 call_id,忽略
    const t = this._now();
    e.result = output;                                              // 始终 stash(修正 4)
    if (e.state === PENDING) {
      e.state = RESOLVED;
      e.ttlDeadline = t + this.ttlMs;
      if (!e.settled) { e.settled = true; e.resolve({ status: 'result', output: output, callId: callId }); }
      this.stats.resolved++;
      return true;
    }
    if (e.state === TICKETED) {
      // MCP 请求早已被取号应答,这里只把结果落 stash 供 poll 取
      e.state = RESOLVED;
      e.ttlDeadline = t + this.ttlMs;
      this.stats.resolved++;
      return true;
    }
    // 已 RESOLVED:幂等重复 output,留最新并续 ttl
    e.ttlDeadline = t + this.ttlMs;
    return true;
  }

  /* poll action:取号后模型回来查结果。resolved→回结果;还没落→pending;不认识→unknown。 */
  poll(callId) {
    const e = this.map.get(callId);
    if (!e) return { status: 'unknown', callId: callId };
    if (e.state === RESOLVED) return { status: 'result', output: e.result, callId: callId };
    return { status: 'pending', callId: callId };
  }

  /* 驱动 ticket + GC 迁移。生产侧 setInterval 每 ~1s 打点;测试侧显式打时间戳。 */
  sweep(nowMs) {
    const t = nowMs != null ? nowMs : this._now();
    for (const pair of this.map) {
      const callId = pair[0], e = pair[1];
      if (e.state === PENDING && t >= e.ticketDeadline) {
        e.state = TICKETED;
        e.ttlDeadline = t + this.ttlMs;                            // 取号后也进 ttl,永不落 output 也能 GC
        if (!e.settled) { e.settled = true; e.resolve({ status: 'ticket', callId: callId }); }
        this.stats.ticketed++;                                     // 修正 2:≤ticketMs 取号
      } else if ((e.state === RESOLVED || e.state === TICKETED) && e.ttlDeadline != null && t >= e.ttlDeadline) {
        this.map.delete(callId);
        this.stats.gc++;
      }
    }
  }

  size() { return this.map.size; }
  get(callId) { return this.map.get(callId); }
}

module.exports = { RendezvousTable: RendezvousTable, PENDING: PENDING, TICKETED: TICKETED, RESOLVED: RESOLVED };
