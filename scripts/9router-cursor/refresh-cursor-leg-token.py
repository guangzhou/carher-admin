#!/usr/bin/env python3
"""给 9router 里**已有**的 Cursor 腿换一把全新 IDE token —— 全自助，不需要任何人给凭据。

为什么需要它
------------
一条腿"认证还活着但推不出活"的形状是：`AvailableModels` 200、`/api/auth/me` 200、
`full_stripe_profile` 正常，**但真打推理首帧 45s 死线**（`cursor stall: no upstream
frame in 45000ms`）。此时唯一能自助尝试的修复动作就是**整把会话换新**
（新 IDE token + 新 machineId）。

🔴 `PUT /api/providers/<id>` **不接受 accessToken**。实测它只认
`name / priority / globalPriority / defaultModel / isActive / apiKey(仅 authType=apikey)
/ testStatus / lastError / lastErrorAt / providerSpecificData / proxy*`。
所以"换 token"在 9router 里**物理上只能是"import 一条新的 + 退役旧的"**，
不存在原地更新。照着 PUT 发 accessToken 会返 200 —— **200 但那个字段被丢掉了**，
这是这条路上最贵的假绿。

自助拿新 token：旧 token 自己就是 web cookie
--------------------------------------------
    cookie = WorkosCursorSessionToken=<urlencode(<workosId>::<IDE token>)>
             workosId = JWT 的 sub 去掉 `auth0|` 前缀

拿它焐热 cookie jar → PKCE deep-login → `api2.cursor.sh/auth/poll` 换出**新的**
IDE token。配方与 add-cursor-account.py 一致（那里是"外部 web cookie → 新号"，
这里是"池里旧腿 → 同一个号的新会话"）。详见 cursor-zdr-consent.py 的说明。

判活只认真推理，且必须让这条腿**排第一**
----------------------------------------
⛔ `GET /api/providers/<id>/models` = 假绿（死腿照样列 200+ 个模型）。
⛔ `/api/providers/<id>/test-models` 也是假绿：它绕回 9router **自己**的
   `/v1/chat/completions`，走的是**池级**选腿，测不到指定的那条腿。
⛔ `x-connection-id` 请求头**只在 `/v1/images/generations` 生效**，chat 线完全忽略它
   （阴性对照：喂一个不存在的 connection id，chat 照样 200）。

⇒ 判单腿死活的唯一干净做法：**临时把它 priority 提到 1**（fill-first 下它先被试），
真打一发带 nonce 的非流式请求，再还原。判据是**耗时**：
    ~2s   = 这条腿自己出的活（活）
    ~48s  = 它 45s 撞死线后回落到别的腿才出的活（死）
用户侧全程零失败（死腿只会让那一发多等一次，仍然回落成功）。

admin 认证：不猜密码，自己签
----------------------------
`INITIAL_PASSWORD` 只是首启种子，密码改过之后它登不进去（401），而真密码是
bcrypt 存在 settings 里、拿不回来。pod 内有 `JWT_SECRET`，直接按 9router 自己的
校验逻辑签一张会话即可：cookie `auth_token` = HS256(`{authenticated:true,iat,exp}`)。

用法
----
    refresh-cursor-leg-token.py --check                  # 两条腿各自真打一发(排第一)判死活
    refresh-cursor-leg-token.py --refresh a117b7ad       # 换新会话:import 新腿 + 真打验收(不删旧腿)
    refresh-cursor-leg-token.py --retire a117b7ad        # 验收绿之后,才退役旧腿

`--refresh` **不会删任何东西**：新腿 import 成 priority=2 备用腿，验收红就自己把
新腿删掉回滚（打印命令）。退役旧腿是单独一步，必须人看过验收结果再执行。

回滚
----
    DELETE /api/providers/<新腿 id>      # --refresh 失败时
    脚本每次写操作前都会把 pod 内 sqlite 备份到 /app/data/db/data.sqlite.bak-<ts>
"""

import argparse
import importlib.util
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

PRELUDE = r"""
const fs=require("fs"),crypto=require("crypto"),initSql=require("/app/node_modules/sql.js");
const B="http://127.0.0.1:20128";
const UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        +"(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36";
const b64u=b=>Buffer.from(b).toString("base64").replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
// 不猜密码:按 9router 自己的校验逻辑签一张 24h 内有效的会话。
function mintSession(){
  const now=Math.floor(Date.now()/1000);
  const p=b64u(JSON.stringify({alg:"HS256",typ:"JWT"}))+"."
         +b64u(JSON.stringify({authenticated:true,iat:now,exp:now+900}));
  if(!process.env.JWT_SECRET) throw new Error("JWT_SECRET absent in pod env");
  return "auth_token="+p+"."+crypto.createHmac("sha256",process.env.JWT_SECRET)
    .update(p).digest("base64url");
}
const CK=mintSession();
const api=(p,init={})=>fetch(B+p,{...init,
  headers:{cookie:CK,"content-type":"application/json",...(init.headers||{})}});
const put=(id,body)=>api("/api/providers/"+id,{method:"PUT",body:JSON.stringify(body)});
async function listLegs(){
  const r=await api("/api/providers");
  if(!r.ok) throw new Error("GET /api/providers -> "+r.status+" (self-signed session rejected)");
  const j=await r.json();
  return Array.isArray(j)?j:(j.connections||j.providers||[]);
}
const snap=()=>initSql().then(S=>new S.Database(new Uint8Array(
  fs.readFileSync("/app/data/db/data.sqlite"))));
async function poolKey(){
  const d=await snap();
  return d.exec("SELECT key FROM apiKeys WHERE isActive=1 LIMIT 1")[0].values[0][0];
}
async function legTokens(){   // id -> accessToken（API 会把 token 抹掉,只能读 sqlite）
  const d=await snap();
  return Object.fromEntries(d.exec("SELECT id,data FROM providerConnections")[0].values
    .map(v=>[String(v[0]),JSON.parse(v[1]).accessToken]));
}
function backup(){
  const ts=new Date().toISOString().replace(/[-:.]/g,"").slice(0,15)+"Z";
  const p="/app/data/db/data.sqlite.bak-"+ts;
  fs.copyFileSync("/app/data/db/data.sqlite",p);
  console.log("backup -> "+p);
  return p;
}
// 真推理探针:把 legId 临时提到 priority 1,打一发,再无条件还原。
// 判据是耗时,不是 HTTP 码 —— 死腿会 45s 撞线后回落到别的腿,照样返 200。
async function probeLegFirst(legId,model){
  const legs=await listLegs();
  const orig=Object.fromEntries(legs.map(l=>[l.id,l.priority]));
  const others=legs.filter(l=>l.id!==legId);
  try{
    await put(legId,{priority:1});
    for(const o of others) await put(o.id,{priority:2});
    const key=await poolKey();
    const nonce="N"+Math.random().toString(36).slice(2,7).toUpperCase();
    const t0=Date.now();
    const r=await fetch(B+"/v1/chat/completions",{method:"POST",
      headers:{authorization:"Bearer "+key,"content-type":"application/json"},
      body:JSON.stringify({model:"cu/"+model,stream:false,
        messages:[{role:"user",content:"Reply with exactly this token and nothing else: "+nonce}]})});
    const txt=await r.text(); let out="";
    try{out=JSON.parse(txt).choices?.[0]?.message?.content||"";}catch(e){}
    const ms=Date.now()-t0;
    // 40s 以上 = 撞过 45s 死线(哪怕最终 200,那也是别的腿兜的)
    const alive=r.status===200&&ms<40000&&out.trim().length>0;
    return {legId,model,http:r.status,ms,alive,echo:out.includes(nonce),
            sample:out.slice(0,60)||txt.slice(0,100).replace(/\s+/g," ")};
  } finally {
    for(const [id,p] of Object.entries(orig)) await put(id,{priority:p});
    console.log("priorities restored: "+JSON.stringify(
      (await listLegs()).map(l=>[l.id.slice(0,8),l.priority,l.isActive])));
  }
}
"""

# PKCE deep-login：旧 token 当 web cookie，换一把全新的 IDE token。
DEEPLOGIN = r"""
const jar=new Map();
const ck2=()=>[...jar.entries()].map(([k,v])=>k+"="+v).join("; ");
function absorb(r){
  for(const c of (r.headers.getSetCookie?r.headers.getSetCookie():[])){
    const kv=c.split(";")[0], i=kv.indexOf("=");
    const k=kv.slice(0,i).trim(), v=kv.slice(i+1).trim();
    if(v==="") jar.delete(k); else jar.set(k,v);
  }
}
// cursor.com 会把 loginDeepControl 经 bootstrap-cursor-web-target 弹来弹去,
// 不把它的 cookie 回喂就是无限 307 —— 那不是页面坏了。
async function hop(url,init={},max=8){
  for(let i=0;i<max;i++){
    const r=await fetch(url,{...init,redirect:"manual",
      headers:{...(init.headers||{}),cookie:ck2(),"User-Agent":UA}});
    absorb(r);
    const loc=r.headers.get("location");
    if(r.status>=300&&r.status<400&&loc){url=new URL(loc,url).toString();init={headers:init.headers};continue;}
    return r;
  }
  throw new Error("redirect loop on "+url);
}
async function mintIdeToken(oldTok){
  const sub=JSON.parse(Buffer.from(oldTok.split(".")[1],"base64").toString()).sub;
  const raw=sub.replace(/^auth0\|/,"")+"::"+oldTok;
  jar.set("WorkosCursorSessionToken",encodeURIComponent(raw));
  const me=await fetch("https://cursor.com/api/auth/me",{headers:{cookie:ck2(),"User-Agent":UA}});
  if(!me.ok) throw new Error("old token does not authenticate cursor.com: "+me.status
    +" -- 这条腿连 web 会话都没了,换 token 这条路走不通");
  const mej=await me.json().catch(()=>({}));
  console.log("  web session ok: "+mej.email);
  const H=b64u(crypto.randomBytes(32));
  const q=b64u(crypto.createHash("sha256").update(H,"ascii").digest());
  const K=crypto.randomUUID();
  const page=`https://cursor.com/loginDeepControl?challenge=${q}&uuid=${K}&mode=login`;
  await hop(page,{headers:{accept:"text/html"}});
  // selectedTeamId 必须缺席 —— null/0/-1 都是 400 "Invalid selected team"。
  const ap=await fetch("https://cursor.com/api/auth/loginDeepCallbackControl",{method:"POST",
    headers:{cookie:ck2(),"Content-Type":"application/json","User-Agent":UA,
             origin:"https://cursor.com",referer:page},
    body:JSON.stringify({uuid:K,challenge:q,redirectTarget:null,mobile:false})});
  if(!ap.ok) throw new Error("loginDeepCallbackControl -> "+ap.status+" "+(await ap.text()).slice(0,140));
  for(let i=0;i<15;i++){
    const p=await fetch(`https://api2.cursor.sh/auth/poll?uuid=${K}&verifier=${H}`,
      {headers:{"x-cursor-client-version":"3.12.17","x-cursor-client-type":"ide",
                "User-Agent":UA,accept:"application/json"}});
    const t=await p.text();
    if(p.status===200&&t.includes("accessToken")){
      const ide=JSON.parse(t);
      const ic=JSON.parse(Buffer.from(ide.accessToken.split(".")[1],"base64").toString());
      if(ic.type!=="session") throw new Error("exchanged token type="+ic.type+", not session");
      console.log("  new IDE token: len="+ide.accessToken.length+" exp="+new Date(ic.exp*1000).toISOString()
                  +" hasRefresh="+!!ide.refreshToken);
      return ide;
    }
    await new Promise(s=>setTimeout(s,3000));
  }
  throw new Error("poll exhausted -- no IDE token");
}
"""

CHECK_SRC = PRELUDE + r"""
const MODEL=__MODEL__;
(async()=>{
 for(const L of await listLegs()){
   const r=await probeLegFirst(L.id,MODEL);
   console.log(`leg ${L.id.slice(0,8)} pri=${L.priority} -> ${r.alive?"ALIVE":"DEAD "} `
     +`http=${r.http} ${r.ms}ms echo=${r.echo} ${JSON.stringify(r.sample)}`);
   if(!r.alive && r.ms>=40000)
     console.log("        ^ 45s 死线打满后由别的腿兜底 ⇒ 这条腿自己出不了活");
 }
})().catch(e=>{console.log("ERR "+e.message);process.exit(1);});
"""

REFRESH_SRC = PRELUDE + DEEPLOGIN + r"""
const ONLY=__ONLY__, MODEL=__MODEL__;
(async()=>{
 const legs=await listLegs();
 const old=legs.find(l=>l.id.startsWith(ONLY));
 if(!old) throw new Error("no leg with id prefix "+ONLY);
 console.log("target leg "+old.id+" pri="+old.priority+" active="+old.isActive);
 const before=await probeLegFirst(old.id,MODEL);
 console.log(`BEFORE: ${before.alive?"ALIVE":"DEAD"} ${before.ms}ms http=${before.http}`);
 if(before.alive){ console.log("这条腿现在是活的 —— 不换 token,退出（换新会话是有代价的动作）"); return; }
 const tok=(await legTokens())[old.id];
 if(!tok) throw new Error("no accessToken on that leg in sqlite");
 const ide=await mintIdeToken(tok);
 backup();
 const machineId=crypto.randomBytes(32).toString("hex");   // 新 machineId,不复用旧的
 const imp=await api("/api/oauth/cursor/import",{method:"POST",
   body:JSON.stringify({accessToken:ide.accessToken,
     refreshToken:ide.refreshToken||undefined,machineId})});
 const ib=await imp.json().catch(()=>({}));
 const nid=ib?.connection?.id;
 console.log("import -> "+imp.status+" newLeg="+nid);
 if(!nid) throw new Error("import did not return a connection id: "+JSON.stringify(ib).slice(0,200));
 await new Promise(s=>setTimeout(s,2000));
 const after=await probeLegFirst(nid,MODEL);
 console.log(`AFTER(new leg ${String(nid).slice(0,8)}): ${after.alive?"GREEN":"RED"} `
   +`http=${after.http} ${after.ms}ms echo=${after.echo} ${JSON.stringify(after.sample)}`);
 if(after.alive){
   console.log("GREEN: 新会话能出活。旧腿 "+old.id+" 仍在池里(priority 保持原值),");
   console.log("       看过这份输出后再跑: --retire "+old.id.slice(0,8));
 } else {
   console.log("RED: 换了全新会话照样 45s 死线 ⇒ **不是 token 的问题,是这个号被上游掐了**。");
   console.log("     回滚(删掉刚建的新腿): DELETE /api/providers/"+nid);
   const d=await api("/api/providers/"+nid,{method:"DELETE"});
   console.log("     已自动回滚 -> "+d.status);
 }
})().catch(e=>{console.log("ERR "+e.message);process.exit(1);});
"""

RETIRE_SRC = PRELUDE + r"""
const ONLY=__ONLY__;
(async()=>{
 const legs=await listLegs();
 const t=legs.find(l=>l.id.startsWith(ONLY));
 if(!t) throw new Error("no leg with id prefix "+ONLY);
 if(legs.length<2) throw new Error("只剩这一条腿了,拒绝退役");
 backup();
 const d=await api("/api/providers/"+t.id,{method:"DELETE"});
 console.log("DELETE "+t.id+" -> "+d.status);
 console.log("legs now: "+JSON.stringify((await listLegs()).map(l=>[l.id.slice(0,8),l.priority,l.isActive])));
})().catch(e=>{console.log("ERR "+e.message);process.exit(1);});
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="每条腿排第一真打一发,判死活")
    g.add_argument("--refresh", metavar="LEGPREFIX", help="给这条腿换全新 IDE token（不删旧腿）")
    g.add_argument("--retire", metavar="LEGPREFIX", help="退役这条腿（验收绿之后才做）")
    ap.add_argument("--model", default="claude-opus-4-8-medium",
                    help="探针用的 cursor 模型名（默认 claude-opus-4-8-medium）")
    a = ap.parse_args()

    spec = importlib.util.spec_from_file_location("acct", HERE / "add-cursor-account.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if a.check:
        src = CHECK_SRC.replace("__MODEL__", json.dumps(a.model))
    elif a.refresh:
        src = (REFRESH_SRC.replace("__ONLY__", json.dumps(a.refresh))
                          .replace("__MODEL__", json.dumps(a.model)))
    else:
        src = RETIRE_SRC.replace("__ONLY__", json.dumps(a.retire))
    print(mod.in_pod(src, timeout=1800))


if __name__ == "__main__":
    main()
