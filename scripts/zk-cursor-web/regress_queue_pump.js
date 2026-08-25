#!/usr/bin/env node
/*
 * regress_queue_pump.js — 队列泵离线回归台架(不碰真 Cursor)。
 *
 * 原理:从 cursor_team_setup.js dump 出【真实发货 QP_SNIPPET 原文】,注入一个模拟
 * Cursor composer 队列语义的桩类(官方语义按 2026-08-25 纯原厂 bundle 逆向结论实现:
 * - 官方派发 dispatchQueueItemKeepingRowUntilOwned:置 inFlightDispatchItemIds →
 *   startup 窗口(N ms 后才登记 uuid 进 streamingAbortControllers)→ owned 后 removeFromQueue
 * - 官方派发内嵌 submitChatMaybeAbortCurrent:若有在飞轮会先掐死它(Abort 计数!)
 * - 官方闸门 Vyb:status==="generating" 不派发
 * - 完成回调:status→completed + tryDispatchNextQueueItem
 * - 僵尸病:完成时按概率不复位 status(uuid 残留)——本机实测几乎每轮必发
 * - abort:status→aborted,官方不派发下一条(设计如此)
 * )用假时钟(手动 tick)跑三个实测事故场景 + 一个长队列端到端。
 *
 * 判据(全部来自真实事故):
 *  S1 僵尸卡死:完成后 status 卡 generating → 泵须在 3-4 tick 内 heal 并派发下一条
 *  S2 起跑误杀(v3.3 折叠/漏答真凶):派发起跑窗口 uuid 未登记 → 泵绝不 heal/绝不触发 Abort
 *  S3 停止饿死(福州事故):用户 abort 后队列剩件 → 泵接管派发;泵无寿命上限
 *  S4 端到端:8 条连发+每轮必僵尸+中途 1 次用户 abort → 8 条全部各自成轮,0 误杀 Abort
 *  S5 预网络窗口不穿闸(v3.4 真凶):heal 后派发的预网络窗口内,泵不得再派下一条
 *  S6 官方接力让路(v4 真凶,3.17.19 实测):官方 turnEnded 原生接力队列(弹队首+直接写进
 *     对话+新请求,不走 dispatch/不碰 inFlightDispatchItemIds)→ 泵检测到队列被别人消费
 *     必须让路,绝不在官方起跑窗内 heal/抢发(违者=一请求两问只答后一条,甲乙丙丁戊己事故)
 */
"use strict";
const { execSync } = require("child_process");
const path = require("path");

// ── 1) 取真实发货 snippet ──
const HERE = __dirname;
const dump = JSON.parse(execSync(
  `CX_DUMP_CONSTANTS=1 node ${JSON.stringify(path.join(HERE, "cursor_team_setup.js"))}`,
  { encoding: "utf8" }));
const SNIPPET = process.env.CX_TEST_SNIPPET_FILE
  ? require("fs").readFileSync(process.env.CX_TEST_SNIPPET_FILE, "utf8")
  : dump.QP_SNIPPET;
if (!SNIPPET.includes("@cx-queue-pump:")) { console.error("!! dump 异常"); process.exit(1); }
console.log("snippet under test:", SNIPPET.match(/@cx-queue-pump:[^*]+/)[0], `(${SNIPPET.length} bytes)`);

// ── 2) 假时钟:接管 setTimeout(泵只用 1000ms 定时器)──
let now = 0, timers = [];
global.__origSetTimeout = global.setTimeout;
const fakeSetTimeout = (fn, ms) => { const t = { at: now + ms, fn }; timers.push(t); return t; };
function advance(ms) { // 前进 ms,触发到期定时器(含期间新注册的)
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const next = timers.find(t => t.at <= end);
    if (!next) break;
    now = next.at; timers.splice(timers.indexOf(next), 1); next.fn();
  }
  now = end;
}

// ── 3) 模拟 composer(官方语义桩)──
const PRENET_MS = 2100;    // 实测预网络窗口:派发→status 翻 generating(此窗官方闸门失守!)
const STARTUP_MS = 2500;   // 派发→uuid 登记进 streamingAbortControllers
const TURN_MS = 8000;      // 一轮生成时长
const RELAY_DELAY_MS = 1500; // 流结束→turnEnded 事件被处理→官方接力弹队(实测事件尾延迟)
class SimComposer {
  constructor(opts = {}) {
    this.composerId = "sim";
    this.opts = { zombieOnComplete: true, officialTurnEnded: false, ...opts };
    this.data = { status: "completed", chatGenerationUUID: undefined, generatingBubbleIds: [] };
    this.queue = []; this._qid = 0;
    this.inFlightDispatchItemIds = new Set();
    this.streamingAbortControllers = new Map();
    this.composerChatService = { _aiService: { streamingAbortControllers: this.streamingAbortControllers } };
    this.composerDataService = {
      getComposerData: () => this.data,
      updateComposerData: (_h, patch) => {
        Object.assign(this.data, patch);
        this.log.push(`${now}ms HEAL applied (${JSON.stringify(patch.status)})`); this.stats.heals++;
      },
    };
    this.structuredLogService = { info: (_k, msg) => this.log.push(`${now}ms LOG ${msg}`) };
    this.log = [];
    this.stats = { heals: 0, aborts: 0, turnsStarted: 0, turnsCompleted: 0, questionsPerTurn: [] };
    this._pendingQuestions = []; // 已进对话但还没轮答的问题(折叠计数用)
    this._prenet = null;         // 当前处于预网络窗口的 submit token(被新 submit 顶掉=静默 kill)
  }
  // —— 官方件 ——
  isValidQueueItem() { return true; }
  getComposerHandleIfLoaded() { return {}; }
  getQueueItems() { return this.queue.slice(); }
  tryDispatchNextQueueItem() { // 官方:reconcile略;qserial 在飞守卫;Vyb 闸门;派发首个无 delivery 项
    if (this.queue.length === 0) return;
    if (this.inFlightDispatchItemIds.size > 0) return; // @cxteam-qserial(已发货补丁,属线上形态)
    if (this.data.status === "generating") return;
    const item = this.queue.find(it => !it._dispatched); // RBt: delivery===void 0
    if (item) this._dispatch(item);
  }
  _killPrenet() { // submitChatMaybeAbortCurrent 对预网络轮的顶替(bookkeeping abort,不算误杀)
    if (this._prenet && !this._prenet.registered) this._prenet.cancelled = true;
  }
  _dispatch(item) { // 官方 dispatchQueueItemKeepingRowUntilOwned + submitChatMaybeAbortCurrent
    if (item._dispatched) return; item._dispatched = true; // 官方 delivery 标记等价物
    this.inFlightDispatchItemIds.add(item.id);            // 官方:同步置位,持有到 owned
    this._killPrenet();
    if (this.data.chatGenerationUUID && this.streamingAbortControllers.has(this.data.chatGenerationUUID)) {
      // 在飞轮被掐死 = 误杀("Aborted current chat")
      this.stats.aborts++;
      this.log.push(`${now}ms !! ABORT current chat (friendly fire)`);
      this._finishTurn(/*aborted=*/true);
    }
    this.stats.turnsStarted++;
    const uuid = "u" + item.id;
    const carried = this._pendingQuestions.length + 1; // 本轮可见的问题数(折叠口径)
    this._pendingQuestions.push(item.q);
    const t = { cancelled: false, registered: false }; this._prenet = t;
    // status 同步翻 generating(实测:闸门拦截早于流启动 → submit 链早期置位);
    // uuid 登记仍延迟 STARTUP_MS = 僵尸误判窗口(pre_network 实测 2.1-3.4s)
    this.data.status = "generating"; this.data.chatGenerationUUID = undefined;
    // PRENET_MS 保留给 S5:极端假设下 status 延迟翻转,泵也不得穿闸(纵深防御)
    if (this.opts.lateStatusFlip) { this.data.status = "completed";
      fakeSetTimeout(() => { if (!t.cancelled) this.data.status = "generating"; }, PRENET_MS); }
    fakeSetTimeout(() => { // startup 完成:登记 uuid,owned → removeFromQueue
      const i = this.queue.indexOf(item); if (i >= 0) this.queue.splice(i, 1);
      this.inFlightDispatchItemIds.delete(item.id);
      if (t.cancelled) return; // 预网络被顶掉:出队但不成轮(其问题折叠进顶替者那轮)
      t.registered = true;
      this.data.chatGenerationUUID = uuid;
      this.streamingAbortControllers.set(uuid, { abort() {} });
    }, STARTUP_MS);
    fakeSetTimeout(() => { // 轮完成
      if (t.cancelled) return;
      if (this.data.chatGenerationUUID !== uuid) return; // 已被掐死
      this.stats.turnsCompleted++;
      this.stats.questionsPerTurn.push(carried);
      this._pendingQuestions = [];
      this.streamingAbortControllers.delete(uuid);
      this._turnEnded();
    }, STARTUP_MS + TURN_MS);
  }
  _officialRelay(item) { // 官方 3.17 turnEnded 接力:立即出队+直接写进对话+新请求;不碰 inFlight!
    const i = this.queue.indexOf(item); if (i >= 0) this.queue.splice(i, 1);
    this.log.push(`${now}ms OFFICIAL turnEnded relay dispatch`);
    this._killPrenet();
    this.stats.turnsStarted++;
    const uuid = "o" + item.id;
    const carried = this._pendingQuestions.length + 1;
    this._pendingQuestions.push(item.q);
    const t = { cancelled: false, registered: false }; this._prenet = t;
    // 实测:接力起跑期间上一轮僵尸 status=generating 残留、旧 uuid 已出表 → 泵僵尸误判窗口
    fakeSetTimeout(() => {
      if (t.cancelled) return;
      t.registered = true;
      this.data.status = "generating";
      this.data.chatGenerationUUID = uuid;
      this.streamingAbortControllers.set(uuid, { abort() {} });
    }, STARTUP_MS);
    fakeSetTimeout(() => {
      if (t.cancelled) return;
      if (this.data.chatGenerationUUID !== uuid) return;
      this.stats.turnsCompleted++;
      this.stats.questionsPerTurn.push(carried);
      this._pendingQuestions = [];
      this.streamingAbortControllers.delete(uuid);
      this._turnEnded();
    }, STARTUP_MS + TURN_MS);
  }
  _turnEnded() { // 轮完成后的官方尾处理
    if (this.opts.officialTurnEnded) { // 3.17:turnEnded 事件原生接力队列(带事件尾延迟)
      const nxt = this.queue.find(it => !it._dispatched);
      if (this.opts.zombieOnComplete) this.log.push(`${now}ms turn done (ZOMBIE: status stuck)`);
      else if (!nxt) { this.data.status = "completed"; this.data.chatGenerationUUID = undefined; }
      if (nxt) fakeSetTimeout(() => this._officialRelay(nxt), RELAY_DELAY_MS);
      return;
    }
    if (this.opts.zombieOnComplete) {
      // 僵尸病:status 卡 generating + uuid 残留(实测形态)
      this.log.push(`${now}ms turn done (ZOMBIE: status stuck)`);
    } else {
      this.data.status = "completed"; this.data.chatGenerationUUID = undefined;
      this.tryDispatchNextQueueItem(); // 官方完成回调
    }
  }
  _finishTurn(aborted) {
    const u = this.data.chatGenerationUUID;
    if (u) this.streamingAbortControllers.delete(u);
    if (aborted) { this.data.status = "aborted"; this.data.chatGenerationUUID = undefined; }
  }
  userAbort() { // 用户点停止:官方置 aborted+掐掉在飞 submit(含预网络链),不派发下一条(设计如此)
    this.stats.aborts++; this._killPrenet(); this._finishTurn(true);
    this.log.push(`${now}ms USER ABORT`);
  }
}
// 注入真实 snippet 到 addToQueue(与真实锚点同位置)
const cls = `SimComposer.prototype.addToQueue = function(t){if(!this.isValidQueueItem(t))return;${SNIPPET
  .replace(/setTimeout/g, "fakeSetTimeout")}
  this.queue.push(t); this.tryDispatchNextQueueItem();};`;
eval(cls);

// ── 4) 场景 ──
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log(`  ✅ ${name}`); }
  else { fail++; console.log(`  ❌ ${name} — ${detail}`); }
}

console.log("\n═ S1 僵尸卡死自愈(v3 老病)═");
{
  const c = new SimComposer({ zombieOnComplete: true });
  c.addToQueue({ id: ++c._qid, q: "A" });
  advance(STARTUP_MS + TURN_MS + 500);       // 第一轮完成,status 卡死
  const stuckAt = now;
  c.addToQueue({ id: ++c._qid, q: "B" });    // B 进队,闸门关着
  advance(6000);
  check("僵尸被 heal", c.stats.heals >= 1, `heals=${c.stats.heals}`);
  check("B 被自动派发", c.stats.turnsStarted >= 2, `turns=${c.stats.turnsStarted}`);
  check("heal 及时(≤5s)", c.log.some(l => l.includes("HEAL") && parseInt(l) - stuckAt <= 5000), c.log.filter(l=>l.includes("HEAL")).join("|"));
}

console.log("\n═ S2 起跑窗口不误杀(v3.3 折叠/漏答真凶)═");
{
  const c = new SimComposer({ zombieOnComplete: false }); // 健康完成路径
  c.addToQueue({ id: ++c._qid, q: "A" });
  advance(300);                               // A 起跑中(uuid 未登记)
  c.addToQueue({ id: ++c._qid, q: "B" });    // B 进队,泵开始 tick,撞起跑窗口
  advance(STARTUP_MS + TURN_MS + 2000);       // A 应完整跑完,B 接着跑
  advance(STARTUP_MS + TURN_MS + 2000);
  check("零误杀 Abort", c.stats.aborts === 0, `aborts=${c.stats.aborts}`);
  check("零 heal(健康轮不该碰)", c.stats.heals === 0, `heals=${c.stats.heals}`);
  check("A、B 各自成轮", c.stats.turnsCompleted === 2 && c.stats.questionsPerTurn.every(n => n === 1),
        `completed=${c.stats.turnsCompleted} qpt=${JSON.stringify(c.stats.questionsPerTurn)}`);
}

console.log("\n═ S3 用户停止后队列不饿死(福州事故)═");
{
  const c = new SimComposer({ zombieOnComplete: false });
  c.addToQueue({ id: ++c._qid, q: "A" });
  advance(STARTUP_MS + 1000);                 // A 生成中
  c.addToQueue({ id: ++c._qid, q: "B" });
  c.addToQueue({ id: ++c._qid, q: "C" });
  c.userAbort();                              // 用户点停止:官方不派发
  advance(4000);
  check("停止后 B 被泵派发(≤4s)", c.stats.turnsStarted >= 2, `turns=${c.stats.turnsStarted}`);
  advance(200000);                            // 远超 v3 的 120s 寿命
  check("B、C 全部消化(泵无寿命上限)", c.queue.length === 0 && c.stats.turnsCompleted >= 2,
        `queue=${c.queue.length} completed=${c.stats.turnsCompleted}`);
}

console.log("\n═ S4 端到端:8 连发+每轮必僵尸+中途用户停止 ═");
{
  const c = new SimComposer({ zombieOnComplete: true }); // 最恶劣:每轮完成都卡死
  for (let i = 1; i <= 4; i++) c.addToQueue({ id: ++c._qid, q: "Q" + i });
  advance(15000);
  c.userAbort();                              // 中途停止一次
  for (let i = 5; i <= 8; i++) c.addToQueue({ id: ++c._qid, q: "Q" + i });
  advance(300000);                            // 5 分钟耗尽队列
  const folded = c.stats.questionsPerTurn.filter(n => n > 1).length;
  const maxCarry = Math.max(...c.stats.questionsPerTurn);
  check("队列全消化", c.queue.length === 0, `queue=${c.queue.length}`);
  // 用户 abort 语义(官方):被停轮的问题已进对话、无答案,下一轮必然看见它 → 允许恰一次
  // 2-问携带(纯官方同样如此);除此之外必须严格 1 问 1 轮。
  check("零泵致折叠(仅允许用户abort那一次2问携带)", folded <= 1 && maxCarry <= 2,
        `qpt=${JSON.stringify(c.stats.questionsPerTurn)}`);
  check("零误杀 Abort(仅用户那 1 次)", c.stats.aborts === 1, `aborts=${c.stats.aborts}`);
  console.log(`  (turns=${c.stats.turnsCompleted}, heals=${c.stats.heals} — 每轮僵尸都被治)`);
}


console.log("\n═ S5 预网络窗口不穿闸(v3.4 折叠真凶,本机 16:28 实测形态)═");
{
  const c = new SimComposer({ zombieOnComplete: true, lateStatusFlip: true }); // 最恶劣:僵尸+status 迟翻
  c.addToQueue({ id: ++c._qid, q: "A" });
  advance(STARTUP_MS + TURN_MS + 500);        // A 完成,status 卡死(僵尸)
  c.addToQueue({ id: ++c._qid, q: "B" });
  c.addToQueue({ id: ++c._qid, q: "C" });     // B、C 排队,泵开始 tick
  advance(3500);                               // 泵 heal → 派发 B → B 处于预网络窗口
  advance(1500);                               // 泵继续 tick,若无窗口防护此刻会派发 C 掐死 B
  advance(STARTUP_MS + TURN_MS + 5000);        // B 应完整跑完
  advance(STARTUP_MS + TURN_MS + 8000);        // C 接着跑完(C 完成也僵尸,queue 空泵停,正常)
  check("零误杀 Abort", c.stats.aborts === 0, `aborts=${c.stats.aborts} log=${c.log.filter(l=>l.includes("ABORT")).join("|")}`);
  check("B、C 各自成轮(零折叠)", c.stats.questionsPerTurn.every(n => n === 1) && c.stats.turnsCompleted === 3,
        `completed=${c.stats.turnsCompleted} qpt=${JSON.stringify(c.stats.questionsPerTurn)}`);
}

console.log("\n═ S6 官方接力让路(v4 真凶,3.17.19 甲乙丙丁戊己实测形态)═");
{
  // 3.17 官方在 turnEnded 原生接力队列(弹队首+直写对话+新请求,不碰 inFlight)。
  // v3.5 事故:上一轮攒的僵尸计数让泵在接力起跑窗内 heal+抢发下一条 → 一请求两问只答后一条。
  const c = new SimComposer({ zombieOnComplete: true, officialTurnEnded: true });
  for (let i = 1; i <= 6; i++) { c.addToQueue({ id: ++c._qid, q: "Q" + i }); advance(1300); }
  advance(300000);
  check("队列全消化", c.queue.length === 0, `queue=${c.queue.length}`);
  check("6 问各自成轮(零折叠)", c.stats.turnsCompleted === 6 && c.stats.questionsPerTurn.every(n => n === 1),
        `completed=${c.stats.turnsCompleted} qpt=${JSON.stringify(c.stats.questionsPerTurn)}`);
  check("零误杀 Abort", c.stats.aborts === 0, `aborts=${c.stats.aborts}`);
  check("泵对官方接力让路(yield 踩中)", c.log.some(l => l.includes("queue consumed externally")),
        c.log.slice(-6).join(" | "));
}

console.log(`\n═══ 结果:${pass} PASS / ${fail} FAIL ═══`);
process.exit(fail ? 1 : 0);
