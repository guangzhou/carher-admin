#!/usr/bin/env node
/* Cursor 上下文窗口单位归一补丁  marker: @cx-ctxwin:v3
 *
 * ── 病是什么 ─────────────────────────────────────────────────────────
 * Cursor 服务端在 protobuf `InferenceExtendedUsageInfo.max_tokens` 里回的是
 * **档位名的数字部分**(500k → 500,1m → 1),不是真实 token 数。
 * 于是摘要阈值 min(maxTokens-10000, maxTokens*0.9) 变成负数,`used >= 负数`
 * 恒真 ⇒ 连发 "hi" 也每轮触发 summarization,界面百分比飙到 8146%~27706%。
 *
 * ── 实测依据(2026-09-22,本机 932 个会话 / 84 个模型) ──────────────────
 *   1. maxTokens 非 0 时取值只落在 {256,272,300,500} —— 全是真实窗口的 k 数字。
 *      同期 used 是 2 万~8.3 万 ⇒ 百分比 8146%~27706%。
 *   2. 🔴 **不是按模型分的,是按时间分的**:5/17~6/26 共 42 次全对、0 次错;
 *      8/20 和 9/14 各漏过 1 次;**9/17 是最后一个正确值,9/20 起 19 次全错**。
 *      同一个 grok-4.6:9/14、9/17 拿到 256000 ✅,9/22 拿到 0。
 *      ⇒ 服务端 9/17~9/20 之间引入的回归,任何模型都会中。
 *   3. ⛔ **不是我们的补丁造成的**:21 条坏值全部落在 08-20 ~ 09-22 15:58,
 *      本机第一次打补丁是 09-22 16:03。且 `cursor_team_setup.js` 全文
 *      grep 不到 maxTokens / context / tokenLimit / summariz。
 *   4. 🔴 **ctx 档位与 maxTokens 并非一一对应**(gpt-5.6-sol 选 272k 拿到 500;
 *      grok-4.5-latest 没有 ctx 参数却拿到 272)⇒ **不许按 ctx 档位反推**,
 *      只能对收到的那个数字做单位归一。
 *   5. maxTokens=0 = 客户端本地合成(sa-* / cr-g-* / cursor-web-fc-* 这些
 *      只在 198 上存在的名走这条路)⇒ Cursor 不知道窗口 ⇒ **永不压缩**。
 *      那是另一个病,本补丁**故意不碰**(0 原样放行)。
 *
 * ── 补在哪:三个点,缺一不可 ────────────────────────────────────────
 *   A. `createRedactedConversationTokenDetails()`
 *      tokenDetails 的唯一构造入口(实测 4 个 setTokenDetails 调用点全过它)。
 *      管:overageThreshold 阻塞判定、持久化间隙、界面百分比。
 *   B. `getBackgroundSummarizationTriggerThreshold(maxTokens, props)`
 *      **所有「该不该压缩」的判定都过它**(shouldStart / shouldPersist 都调它)。
 *      🔴 必须单独补:有一条触发路径直接用 `currentUsage.maxTokens`,
 *      **不经过 tokenDetails**,只补 A 盖不住(v2 的漏洞)。
 *   C. `shouldPersistBackgroundSummarization(used, maxTokens, props)`
 *      🔴 它自己又算了一次 `unusedTokens = maxTokens - usedTokens`,用的是**裸值**,
 *      B 的归一只活在 B 的局部变量里,盖不到它。不补 C ⇒ 一旦 start 触发,
 *      persist 因为 unusedTokens 变成大负数而必然同时触发,失去「后台摘要」档位。
 *
 *   三点是穷举出来的,不是挑的:daemon.cjs 里对 maxTokens 做算术/比较的代码行
 *   共 10 处 —— 4 处在 B 体内、5 处走 `tokenDetails.maxTokens`(A 覆盖)、
 *   剩下的就是 C 这一处。
 *
 * ── 换算规则(沿用 Cursor 自己的解析器 WS_(),k→1e3 m→1e6) ──────────
 *      1      → 1e6    ("1m" 档)
 *      0<n<4096 → n×1e3 (200/256/272/300/500 这几档)
 *      0      → 不动    ⇒ 自定义模型行为零变化
 *      ≥4096  → 不动    ⇒ 真实 token 数原样通过(200000/256000/300000/1000000 实测未被动)
 *   界 4096 的依据:最小档 200k 映射成 200,真实窗口最小 200000,4096 落在两簇之间。
 *
 * ── 用法 ──────────────────────────────────────────────────────────
 *   ELECTRON_RUN_AS_NODE=1 <Cursor可执行文件> cursor_ctxwin_patch.js [--apply|--revert]
 *   不带参数 = 空跑,只报告不写盘。任一锚点不是 exactly-1 ⇒ 整体放弃,一字节不写。
 *   备份写到 ~/.cursor-ctxwin-backup/<时间戳>/,--revert 从最近一次备份还原。
 *   ⚠️ 改完必须重启 Cursor(bundle 启动时加载)。⚠️ Cursor 升级会冲掉,升完要重打。
 */
const fs=require("fs"), path=require("path"), os=require("os");
const MARK="@cx-ctxwin:v3";
const OLD_MARKS=["@cx-ctxwin:v1","@cx-ctxwin:v2"];
const ROOT=process.env.CURSOR_APP_ROOT ||
  path.resolve(path.dirname(process.execPath),"../Resources/app");
const BK=path.join(os.homedir(),".cursor-ctxwin-backup");

const TARGETS=[
  {f:"extensions/cursor-local-agent-runtime/dist/main.js", kind:"min"},
  {f:"extensions/cursor-agent-host/dist/main.js",          kind:"min"},
  {f:"extensions/cursor-agent-exec/dist/main.js",          kind:"min"},
  {f:"extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs", kind:"src"},
];

// 🔴 名字位一律用 ID 捕获,不许写死 —— 函数名、**参数名、局部变量名**都算名字位。
// 3.20.21 栽在函数名($7y),3.21.18 栽在局部变量名与参数名(见锚点 B/C 注释)。
const ID="[A-Za-z0-9_$]+";

// ── 锚点 A:tokenDetails 构造器 ──
const A_MIN=new RegExp("function\\s+("+ID+")\\(("+ID+"),("+ID+")\\)\\{return\\{"
  +"usedTokens:0,maxTokens:0,breakdown:void 0,promptContextUsageTree:void 0,"
  +"promptContextUsageSnapshotBlobId:void 0,\\.\\.\\.\\3,_privacyMode:\\2\\}\\}","g");
const A_SRC=`function createRedactedConversationTokenDetails(privacyMode, partial2) {
  return {
    usedTokens: 0,
    maxTokens: 0,
    breakdown: void 0,
    promptContextUsageTree: void 0,
    promptContextUsageSnapshotBlobId: void 0,
    ...partial2,
    _privacyMode: privacyMode
  };
}`;
// ── 锚点 B:摘要触发阈值函数 ──
// 🔴 2026-09-23:3.21.18 把局部变量 `const n=[]` 摇成 `const r=[]` ⇒ 写死名字的锚点命中 0。
// 参数名与局部变量名一律用 ID 捕获 + 反向引用绑定语义,不许出现字面量 e/t/n/r。
const B_MIN=new RegExp("function\\s+("+ID+")\\(("+ID+"),("+ID+")\\)\\{if\\(\\2<=0\\)return;"
  +"const ("+ID+")=\\[\\];return void 0!==\\3\\.unusedTokensThresholdToStartBackgroundSummarization","g");
const B_SRC=`function getBackgroundSummarizationTriggerThreshold(maxTokens, props) {
  if (maxTokens <= 0) {`;
// ── 锚点 C:persist 判定(它自己又拿裸 maxTokens 算了一次 unusedTokens) ──
// 🔴 3.21.18 里第三参数与局部变量互换(`(e,t,n)/const r` → `(e,t,r)/const n`)⇒ 同样必须全捕获。
const C_MIN=new RegExp("function\\s+("+ID+")\\(("+ID+"),("+ID+"),("+ID+")\\)\\{"
  +"const ("+ID+")=\\3-\\2;return\\s+"+ID+"\\(\\2,\\3,\\4\\)&&"
  +"\\(void 0!==\\4\\.unusedTokensThresholdToPersistBackgroundSummarization","g");
const C_SRC=`function shouldPersistBackgroundSummarization(usedTokens, maxTokens, props) {
  const unusedTokens = maxTokens - usedTokens;`;

const NORM_MIN='function __cxN(x){return "number"==typeof x&&x>0&&x<4096?(1===x?1e6:1e3*x):x}';

function planFile(p, kind){
  let s; try{ s=fs.readFileSync(p,"utf8"); }catch(e){ return {err:"读不到: "+e.code}; }
  if(s.includes(MARK)) return {skip:true};
  for(const m of OLD_MARKS) if(s.includes(m))
    return {err:`带着旧补丁 ${m} —— 先跑 --revert 还原,再打 v3`};
  const notes=[];
  if(kind==="min"){
    A_MIN.lastIndex=0; const a=[...s.matchAll(A_MIN)];
    B_MIN.lastIndex=0; const b=[...s.matchAll(B_MIN)];
    C_MIN.lastIndex=0; const c=[...s.matchAll(C_MIN)];
    if(a.length!==1) return {err:`锚点A命中=${a.length}(须 1)`};
    if(b.length!==1) return {err:`锚点B命中=${b.length}(须 1)`};
    if(c.length!==1) return {err:`锚点C命中=${c.length}(须 1)`};
    const af=a[0][1], bf=b[0][1], cf=c[0][1];
    // A:参数名也是捕获来的(2=privacyMode,3=partial)
    const aPriv=a[0][2], aPart=a[0][3];
    const aTo=`function ${af}(${aPriv},${aPart}){/*${MARK}*/${NORM_MIN}`
      +`const __cxR={usedTokens:0,maxTokens:0,breakdown:void 0,promptContextUsageTree:void 0,`
      +`promptContextUsageSnapshotBlobId:void 0,...${aPart},_privacyMode:${aPriv}};`
      +`__cxR.maxTokens=__cxN(__cxR.maxTokens);`
      +`if(void 0!==__cxR.breakdown&&"object"==typeof __cxR.breakdown)__cxR.breakdown={...__cxR.breakdown,maxTokens:__cxN(__cxR.breakdown.maxTokens)};`
      +`return __cxR}`;
    // B/C:归一语句用**捕获到的那个参数名**生成;插入点 = 参数表后的第一个 `{`
    //     (参数表里不可能有 `{`,所以 replace 第一个 `{` 就是函数体开括号,与空格/换行无关)
    const bMax=b[0][2];                     // B 的第 1 参 = maxTokens
    const cMax=c[0][3];                     // C 的第 2 参 = maxTokens
    const norm=v=>`${v}="number"==typeof ${v}&&${v}>0&&${v}<4096?(1===${v}?1e6:1e3*${v}):${v};`;
    const bPatched=b[0][0].replace("{", `{/*${MARK}*/`+norm(bMax));
    const cPatched=c[0][0].replace("{", `{/*${MARK}*/`+norm(cMax));
    s=s.replace(a[0][0], aTo);
    s=s.replace(b[0][0], bPatched);
    s=s.replace(c[0][0], cPatched);
    notes.push(`A=${af}(${aPriv},${aPart})@${a[0].index}`,
               `B=${bf}(${bMax},..)@${b[0].index}`,
               `C=${cf}(..,${cMax},..)@${c[0].index}`);
  } else {
    const ca=s.split(A_SRC).length-1, cb=s.split(B_SRC).length-1, cc=s.split(C_SRC).length-1;
    if(ca!==1) return {err:`锚点A命中=${ca}(须 1)`};
    if(cb!==1) return {err:`锚点B命中=${cb}(须 1)`};
    if(cc!==1) return {err:`锚点C命中=${cc}(须 1)`};
    s=s.replace(A_SRC, `function createRedactedConversationTokenDetails(privacyMode, partial2) {
  /*${MARK}*/
  const __cxN = (x) => typeof x === "number" && x > 0 && x < 4096 ? (x === 1 ? 1e6 : x * 1e3) : x;
  const __cxR = {
    usedTokens: 0,
    maxTokens: 0,
    breakdown: void 0,
    promptContextUsageTree: void 0,
    promptContextUsageSnapshotBlobId: void 0,
    ...partial2,
    _privacyMode: privacyMode
  };
  __cxR.maxTokens = __cxN(__cxR.maxTokens);
  if (__cxR.breakdown !== void 0 && typeof __cxR.breakdown === "object") {
    __cxR.breakdown = { ...__cxR.breakdown, maxTokens: __cxN(__cxR.breakdown.maxTokens) };
  }
  return __cxR;
}`);
    s=s.replace(B_SRC, `function getBackgroundSummarizationTriggerThreshold(maxTokens, props) {
  /*${MARK}*/
  maxTokens = typeof maxTokens === "number" && maxTokens > 0 && maxTokens < 4096 ? (maxTokens === 1 ? 1e6 : maxTokens * 1e3) : maxTokens;
  if (maxTokens <= 0) {`);
    s=s.replace(C_SRC, `function shouldPersistBackgroundSummarization(usedTokens, maxTokens, props) {
  /*${MARK}*/
  maxTokens = typeof maxTokens === "number" && maxTokens > 0 && maxTokens < 4096 ? (maxTokens === 1 ? 1e6 : maxTokens * 1e3) : maxTokens;
  const unusedTokens = maxTokens - usedTokens;`);
    notes.push("A=createRedactedConversationTokenDetails()", "B=getBackgroundSummarizationTriggerThreshold()", "C=shouldPersistBackgroundSummarization()");
  }
  return {out:s, notes};
}

const mode=process.argv[2]||"--dry";

if(mode==="--revert"){
  let dirs=[]; try{dirs=fs.readdirSync(BK).filter(d=>/^\d{14}$/.test(d)).sort();}catch(e){}
  if(!dirs.length){ console.log("没有备份可回滚"); process.exit(1); }
  const from=path.join(BK, dirs[dirs.length-1]);
  console.log("从备份回滚: "+from);
  for(const t of TARGETS){
    const b=path.join(from, t.f.replace(/\//g,"__"));
    if(fs.existsSync(b)){ fs.copyFileSync(b, path.join(ROOT,t.f)); console.log("  已还原 "+t.f); }
    else console.log("  !! 备份里没有 "+t.f);
  }
  process.exit(0);
}

let hardFail=false; const plan=[];
for(const t of TARGETS){
  const p=path.join(ROOT,t.f);
  const r=planFile(p,t.kind);
  if(r.err){ console.log(`XX ${t.f}\n     ${r.err}`); hardFail=true; continue; }
  if(r.skip){ console.log(`== ${t.f}  已是 ${MARK},跳过`); plan.push({p,t,skip:true}); continue; }
  console.log(`OK ${t.f}\n     ${r.notes.join("  ")}  三锚点均 exactly-1`);
  plan.push({p,t,out:r.out,orig:fs.readFileSync(p,"utf8")});
}
const todo=plan.filter(x=>!x.skip);
console.log(`\n待改 ${todo.length} 个文件,已打过 ${plan.length-todo.length} 个`);
if(hardFail){ console.log("\n⛔ 有文件不满足条件 ⇒ 全部放弃,未写盘"); process.exit(2); }
if(mode!=="--apply"){ console.log("\n(空跑,未写盘。加 --apply 才真改)"); process.exit(0); }
if(!todo.length){ console.log("无事可做"); process.exit(0); }

const ts=new Date().toISOString().replace(/[-:T]/g,"").replace(/\..*$/,"").slice(0,14);
const bdir=path.join(BK,ts); fs.mkdirSync(bdir,{recursive:true});
for(const x of plan) fs.writeFileSync(path.join(bdir, x.t.f.replace(/\//g,"__")), x.orig!==undefined?x.orig:fs.readFileSync(x.p));
for(const x of todo) fs.writeFileSync(x.p, x.out);
console.log(`已改 ${todo.length} 个文件`);
console.log("备份: "+bdir);
console.log("回滚: 同一命令加 --revert");
console.log("⚠️ 需要重启 Cursor 才生效");
