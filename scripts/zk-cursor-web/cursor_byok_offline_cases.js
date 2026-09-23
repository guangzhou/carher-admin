// 行为腿:直接把 patch 前/后的 bOd 与 FREE 监听器抽出来真跑。
const fs=require('fs'),path=require('path'),vm=require('vm');
// 输入由台架给:CX_BYOK_PRE=pristine bundle,CX_BYOK_POST=打完的产物。
// 🔴 缺输入必须崩,不许回落到某个写死路径 —— 那会让这腿在别人机器上假绿。
const SRC=process.env.CX_BYOK_PRE, OUT=process.env.CX_BYOK_POST;
if(!SRC||!OUT||!fs.existsSync(SRC)||!fs.existsSync(OUT)){
  console.error('需要 CX_BYOK_PRE / CX_BYOK_POST 指向存在的文件');process.exit(2);
}
const ID="[A-Za-z0-9_$]+";
const RX_F=new RegExp("function ("+ID+")\\(("+ID+"),("+ID+")\\)\\{return ("+ID+")\\(\\2\\)\\?\\3\\.useClaudeKey\\?\"anthropic\":void 0:("+ID+")\\(\\2\\)\\?\\3\\.useGoogleKey\\?\"google\":void 0:\\3\\.useOpenAIKey\\?\"openai\":void 0\\}","g");
const RX_K=new RegExp("("+ID+")\\(\\{isLocalMode:("+ID+")\\.localMode\\}\\)&&("+ID+")===("+ID+")\\.FREE&&("+ID+")!==\\4\\.FREE&&this\\.setUseOpenAIKey\\(!1\\)","g");

function extract(src,rx,label){
  rx.lastIndex=0; const hits=[...src.matchAll(rx)];
  if(hits.length!==1) throw new Error(label+' 命中='+hits.length+' != 1 ⇒ 尺子不可信');
  return hits[0][0];
}
const pre=fs.readFileSync(SRC,'utf8');
const post=fs.readFileSync(OUT,'utf8');

// --- bOd ---
const preF=extract(pre,RX_F,'pre bOd');
const postF=extract(post,new RegExp("function ("+ID+")\\(("+ID+"),("+ID+")\\)\\{/\\*@cxteam-byokforce\\*/[\\s\\S]{0,700}?\\.useOpenAIKey\\?\"openai\":void 0\\}","g"),'post bOd');

function runF(code){
  const ctx={out:null};
  const name=code.match(/^function ([A-Za-z0-9_$]+)/)[1];
  // 依赖的两个前缀判定函数用真实语义补桩
  const shim='function __cl(e){return e.startsWith("claude-")}function __ge(e){return e.startsWith("gemini-")}';
  const body=code.replace(/\b([A-Za-z0-9_$]+)\((\w+)\)\?(\w+)\.useClaudeKey/, '__cl($2)?$3.useClaudeKey')
                 .replace(/\b([A-Za-z0-9_$]+)\((\w+)\)\?(\w+)\.useGoogleKey/, '__ge($2)?$3.useGoogleKey');
  vm.runInNewContext(shim+body+';out='+name+';',ctx);
  return ctx.out;
}
const fPre=runF(preF), fPost=runF(postF);

const STORE_OFF={useOpenAIKey:false,useClaudeKey:false,useGoogleKey:false,
  aiSettings:{userAddedModels:["grok-4.7","gpt-6-luna","glm-5.3-flash"]}};
const STORE_ON ={useOpenAIKey:true, useClaudeKey:false,useGoogleKey:false,
  aiSettings:{userAddedModels:["grok-4.7"]}};

let fail=0;
const T=(name,got,want)=>{const ok=got===want;if(!ok)fail++;console.log((ok?'  PASS ':'  FAIL ')+name+'  got='+JSON.stringify(got)+' want='+JSON.stringify(want));};

console.log('--- 阳性对照:原版在 useOpenAIKey=false 下必须判"不走 BYOK" ---');
T('pre  grok-4.7 / 开关off',            fPre('grok-4.7',STORE_OFF), undefined);
T('pre  grok-4.7 / 开关on',             fPre('grok-4.7',STORE_ON),  'openai');
console.log('--- 打完:我们装的名字在开关 off 下也必须走 BYOK ---');
T('post grok-4.7 / 开关off',            fPost('grok-4.7',STORE_OFF),'openai');
T('post gpt-6-luna / 开关off',          fPost('gpt-6-luna',STORE_OFF),'openai');
T('post glm-5.3-flash / 开关off',       fPost('glm-5.3-flash',STORE_OFF),'openai');
console.log('--- 不许越界:用户没装的名字维持原判 ---');
T('post 未装的 gpt-5.5 / 开关off',       fPost('gpt-5.5',STORE_OFF), undefined);
T('post 未装的 claude-5-sonnet/开关off', fPost('claude-5-sonnet',STORE_OFF), undefined);
T('post 未装的 gemini-3-pro / 开关off',  fPost('gemini-3-pro',STORE_OFF), undefined);
T('post 未装的 gpt-5.5 / 开关on',        fPost('gpt-5.5',STORE_ON), 'openai');
console.log('--- 存储结构缺失时不许抛 ---');
T('post 无 aiSettings / 开关on',        fPost('grok-4.7',{useOpenAIKey:true}), 'openai');
// storage=null:原版本身就抛(t.useOpenAIKey on null)⇒ 判据是"与原版同形",不是"不抛"。
const th=f=>{try{f('grok-4.7',null);return 'no-throw'}catch(e){return 'throw'}};
T('storage=null 与原版同形', th(fPost), th(fPre));

console.log('--- byok-keepon:FREE 降级那条腿摘没摘 ---');
const preK=extract(pre,RX_K,'pre keepon');
console.log('  pre  :',preK);
if(!post.includes('/*@cxteam-byokkeepon*/void 0')){console.log('  FAIL 打完后没有 keepon marker');fail++;}
else console.log('  PASS post : /*@cxteam-byokkeepon*/void 0 (setUseOpenAIKey(!1) 已不可达)');
RX_K.lastIndex=0;
const still=[...post.matchAll(RX_K)].length;
T('打完后原腿残留数',still,0);
// ⚠️ 不能裸数 setUseOpenAIKey(!1):它是 _setUseOpenAIKey(!1) 的子串,
// 而后者是 setter 自己的内部实现(用户手动关 / keychain 里没 key 时回落),属于合法路径。
// 只数**外部调用点** this.setUseOpenAIKey(!1)。
T('外部调用点 this.setUseOpenAIKey(!1) 残留数', (post.match(/this\.setUseOpenAIKey\(!1\)/g)||[]).length, 0);
T('setter 内部 _setUseOpenAIKey(!1) 保留(不许被我们动)', (post.match(/_setUseOpenAIKey\(!1\)/g)||[]).length, (pre.match(/_setUseOpenAIKey\(!1\)/g)||[]).length);
console.log(fail?('\n❌ '+fail+' 项失败'):'\n✅ 行为腿全绿(含阳性对照)');
process.exit(fail?1:0);
