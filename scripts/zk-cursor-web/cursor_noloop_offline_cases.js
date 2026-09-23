#!/usr/bin/env node
/* 件D @cx-noloop:v1 的行为腿(离线,不碰真 Cursor)。
 *
 * 量的是什么:把 daemon.cjs 里**真实的** `createLocalLoopTurnRouter` 函数体抠出来,
 * 用桩喂进去真跑,断言「打完补丁之后,三种本来会走到 Cursor 自家推理 / 抛错的入参,
 * 全部改判成 `connect`」。
 *
 * 为什么用 daemon.cjs 而不是 main.js:两者是**同一份源码**的两种打包产物,
 * daemon.cjs 保住了真名(`createLocalLoopTurnRouter` / `failDecision` / …),
 * 能只靠 7 个桩就把真函数跑起来。main.js 那份全是混淆名,靠猜名字造桩反而
 * 是在测我的猜名能力,不是测补丁 ⇒ 那份在台架里只做结构断言(锚点 exactly-1 +
 * marker 紧跟在生成器体开头 + node --check),并且在输出里写清这半是结构不是行为,
 * 不许把两种强度混成一句"全绿"。
 *
 * 🔴 三个用例是**穷举**路由器里通往"非 connect"的全部出口,不是挑好看的:
 *   ① gate on + 合格            → 原版 {runtime:"managed-local",reason:"eligible"}
 *   ② gate on + 带 BYOK 凭据    → 原版 {runtime:"fail"}(Cursor 自己说 local loop 不支持 BYOK)
 *   ③ privateInference 已配置   → 原版在**gate 检查之前**就 return 了
 *      ⇒ 这一例就是"为什么补丁必须插在函数入口、不能只关 gate"的判据。
 *
 * 🔴 每个用例都带阳性对照:先断言**原版**在同样入参下确实返回非 connect。
 *    对照不成立(原版也返 connect)时整腿报红 —— 否则"打完是 connect"这句话
 *    可能只是桩造得太温柔,压根没进到那条分支,是合成绿。
 *
 * 用法:CX_NL_PRE=<pristine daemon.cjs> CX_NL_POST=<打完的 daemon.cjs> node 本文件
 * 退出码 0=全绿 / 1=有红 / 2=输入不可用(抠不出函数,算红不算跳过)
 */
"use strict";
const fs = require("fs");

const PRE = process.env.CX_NL_PRE, POST = process.env.CX_NL_POST;
if (!PRE || !POST) { console.error("需要 CX_NL_PRE / CX_NL_POST"); process.exit(2); }

let fail = 0;
const ok = (m) => console.log("  PASS  " + m);
const bad = (m) => { fail++; console.log("  FAIL  " + m); };

// 从一段源码里按大括号配平抠出 `function <name>(` 开始的整个函数。
// 命中数必须 exactly-1:0 = 换名了(抠错对象),>1 = 抠到同名的另一个,两种都不许静默继续。
function extractFn(src, name) {
  const re = new RegExp("function\\s+" + name + "\\s*\\(", "g");
  const m = [...src.matchAll(re)];
  if (m.length !== 1) throw new Error(name + " 命中=" + m.length + "(应为 1)");
  const start = m[0].index;
  const open = src.indexOf("{", start);
  let d = 0, i = open;
  for (; i < src.length; i++) {
    const c = src[i];
    if (c === "{") d++;
    else if (c === "}") { d--; if (!d) { i++; break; } }
  }
  return src.slice(start, i);
}

// 标准 __awaiter(TS/esbuild 下发的那个),名字随打包结果变 ⇒ 从函数文本里现抠。
const AWAITER = `function __cxAwaiter(thisArg,_a,P,generator){
  function adopt(v){return v instanceof Promise?v:new Promise(function(r){r(v)})}
  return new Promise(function(resolve,reject){
    function fulfilled(v){try{step(generator.next(v))}catch(e){reject(e)}}
    function rejected(v){try{step(generator["throw"](v))}catch(e){reject(e)}}
    function step(r){r.done?resolve(r.value):adopt(r.value).then(fulfilled,rejected)}
    step((generator=generator.apply(thisArg,_a||[])).next());
  });
}`;

function awaiterName(fnText) {
  const m = fnText.match(/=>\s*([A-Za-z0-9_$]+)\s*\(this,\s*void 0,\s*void 0,\s*function\*/);
  if (!m) throw new Error("抠不到 awaiter 名(路由器形状变了)");
  return m[1];
}

/* 造一个路由器实例。桩只做两件事:让控制流能走到目标出口,以及把"原版会返回什么"
 * 如实反映出来。桩里**不许**藏任何会改变判定的逻辑,否则测的是桩不是补丁。 */
function buildRouter(fnText) {
  const aw = awaiterName(fnText);
  const prelude = `
    ${AWAITER}
    const ${aw} = __cxAwaiter;
    const AgentMode = { AGENT: 1, UNSPECIFIED: 0 };
    const logger34 = { warn(){}, info(){}, error(){} };
    const AGENT_HOST_LOCAL_LOOP_GATE = "agent_host_local_loop";
    // 这一句让 ③ 能被观察到:原版一进函数就把这一轮交给私有推理那支。
    function routePrivateInferenceTurn(){ return { runtime:"managed-local", reason:"private-inference-eligible" }; }
    function readEnrollmentOrUnavailable(){ return "unavailable"; }
    function toLocalLoopTurnRouteInput(o){ return __cxInput; }
    function failDecision(reason){ return { runtime:"fail", reason }; }
    function getIneligibilityReason(){ return __cxIneligible; }
  `;
  return new Function("__cxInput", "__cxIneligible", prelude + "\n" + fnText +
    "\nreturn createLocalLoopTurnRouter;");
}

let preFn, postFn;
try {
  const preSrc = fs.readFileSync(PRE, "utf8"), postSrc = fs.readFileSync(POST, "utf8");
  preFn = extractFn(preSrc, "createLocalLoopTurnRouter");
  postFn = extractFn(postSrc, "createLocalLoopTurnRouter");
  if (!postFn.includes("@cx-noloop:v1")) { console.error("POST 里抠出来的路由器没有 marker ⇒ 补丁没打到这个函数上"); process.exit(2); }
  if (preFn.includes("@cx-noloop:v1")) { console.error("PRE 里就有 marker ⇒ 给的不是 pristine"); process.exit(2); }
} catch (e) { console.error("抠函数失败:" + e.message); process.exit(2); }

const CASES = [
  {
    name: "① gate on + 合格 ⇒ 原版 managed-local",
    opts: { managedLocalAvailable: true, runtimeCapabilities: { checkFeatureGate: () => Promise.resolve(true) } },
    input: { modelId: "grok-4.7", hasModelCredentials: false, actionCase: "userMessageAction" },
    ineligible: undefined,
    expectPreRuntime: "managed-local",
  },
  {
    name: "② gate on + 带 BYOK 凭据 ⇒ 原版 fail",
    opts: { managedLocalAvailable: true, runtimeCapabilities: { checkFeatureGate: () => Promise.resolve(true) } },
    input: { modelId: "grok-4.7", hasModelCredentials: true, actionCase: "userMessageAction" },
    ineligible: "private-model-not-supported",
    expectPreRuntime: "fail",
  },
  {
    name: "③ privateInference 已配置(gate 检查之前就 return)⇒ 原版 managed-local",
    opts: { privateInference: {}, managedLocalAvailable: true },
    input: { modelId: "grok-4.7", hasModelCredentials: false, actionCase: "userMessageAction" },
    ineligible: undefined,
    expectPreRuntime: "managed-local",
  },
];

// 阴性对照也要有:gate off 时原版本来就是 connect,打完补丁**不许把它变坏**
// (reason 会从 gate-off 变成我们自己的字面量,这是有意的 —— 日志里要能分清是谁摘的)。
const NEG = {
  name: "④ 阴性对照:gate off,原版本来就 connect,打完仍 connect",
  opts: { managedLocalAvailable: true, runtimeCapabilities: { checkFeatureGate: () => Promise.resolve(false) } },
  input: { modelId: "grok-4.7", hasModelCredentials: false, actionCase: "userMessageAction" },
  ineligible: undefined,
  expectPreRuntime: "connect",
};

(async () => {
  const mk = (txt, c) => buildRouter(txt)(c.input, c.ineligible)(c.opts)({
    ctx: {}, action: { action: { case: "userMessageAction" } },
  });
  for (const c of [...CASES, NEG]) {
    let pre, post;
    try { pre = await mk(preFn, c); } catch (e) { bad(c.name + " —— 原版跑崩:" + e.message); continue; }
    try { post = await mk(postFn, c); } catch (e) { bad(c.name + " —— 打完跑崩:" + e.message); continue; }
    // 阳性/阴性对照:原版必须落在预期出口。不落 ⇒ 桩没把控制流送到目标分支,
    // 这一例对补丁零判别力,必须报红而不是当过。
    if (pre.runtime !== c.expectPreRuntime) {
      bad(c.name + " —— 对照不成立:原版返回 " + JSON.stringify(pre) + ",期望 runtime=" + c.expectPreRuntime);
      continue;
    }
    if (post.runtime !== "connect") {
      bad(c.name + " —— 打完仍不是 connect:" + JSON.stringify(post));
      continue;
    }
    ok(c.name + " → 打完 " + JSON.stringify(post));
  }
  console.log(fail ? "\n❌ noloop 行为腿 " + fail + " 项失败" : "\n✅ noloop 行为腿全绿(3 个坏出口全改判 connect + 1 阴性对照)");
  process.exit(fail ? 1 : 0);
})();
