// 门禁:DEFAULT_MODELS 里不许出现「Cursor 自带目录中带 context 参数」的模型名
// 因为那会让 agent runtime 的 maxTokens 变成 k 数字(500/272/300/256),
// 导致 A_() 阈值为负 → 每一轮都触发 summarization。
const fs=require("fs"), {DatabaseSync}=require("node:sqlite");
const SETUP=process.argv[2];
const P=process.env.HOME+"/Library/Application Support/Cursor/User/globalStorage/state.vscdb";
const K="src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser";

// ---- 1. 取 Cursor 目录 ----
const db=new DatabaseSync(P,{readOnly:true});
const blob=JSON.parse(String(db.prepare("select value from ItemTable where key=?").get(K).value));
db.close();
const cat=new Map();
(function walk(o){ if(!o||typeof o!=="object")return;
  if(Array.isArray(o)){o.forEach(walk);return;}
  const nm=o.serverModelName||(o.parameterDefinitions?o.name:undefined);
  if(typeof nm==="string"&&!cat.has(nm))
    cat.set(nm,(o.parameterDefinitions||[]).some(p=>p.id==="context"));
  for(const k of Object.keys(o)) walk(o[k]);
})(blob);

// ---- 2. 阳性对照:提取器必须真的数得到(空集恒绿防护) ----
const total=cat.size, withCtx=[...cat.values()].filter(Boolean).length;
console.log(`[自检] Cursor 目录解析到 ${total} 个模型,其中带 context 参数 ${withCtx} 个`);
if(total<20){console.log("*** 自检失败:目录模型数 <20,提取器可能坏了,拒绝出绿 ***");process.exit(3);}
if(withCtx===0){console.log("*** 自检失败:一个带 context 的都没数到,拒绝出绿 ***");process.exit(3);}
const CANARY="grok-4.7";
if(cat.get(CANARY)!==true){console.log(`*** 自检失败:已知阳性样本 ${CANARY} 没被判为带 context ***`);process.exit(3);}
console.log(`[自检] 阳性样本 ${CANARY} 被正确识别 → 尺子有效`);

// ---- 3. 取 DEFAULT_MODELS ----
const src=fs.readFileSync(SETUP,"utf8");
const m=/const DEFAULT_MODELS\s*=\s*\[([\s\S]*?)\n\];/.exec(src);
if(!m){console.log("*** 读不到 DEFAULT_MODELS ***");process.exit(3);}
const names=[...m[1].matchAll(/"([^"]+)"/g)].map(x=>x[1]);
const dm=/const DEFAULT_MODEL\s*=\s*"([^"]+)"/.exec(src)[1];
console.log(`[自检] 菜单解析到 ${names.length} 个名字,默认 = ${dm}`);
if(names.length<10){console.log("*** 自检失败:菜单名字 <10,解析坏了 ***");process.exit(3);}

// ---- 4. 判定 ----
const bad=names.filter(n=>cat.get(n)===true);
console.log("\n=== 判定 ===");
for(const n of names){
  const s=cat.has(n)?(cat.get(n)?"★撞目录且带context":"在目录但无context"):"不在目录(纯自定义)";
  if(cat.get(n)===true) console.log(`  RED   ${n.padEnd(26)} ${s}${n===dm?"   ←← 还是默认模型":""}`);
}
console.log(`\n受影响 ${bad.length}/${names.length}: ${bad.join(", ")||"(无)"}`);
if(bad.includes(dm)) console.log(`默认模型 ${dm} 本身就中招 —— 同事装完直接就在压缩循环里`);
process.exit(bad.length===0?0:1);
