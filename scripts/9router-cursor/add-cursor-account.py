#!/usr/bin/env python3
"""把一个 Cursor 账号加进 198 的 9router 账号池（ns litellm-product, deploy/9router）。

为什么不能直接 import 浏览器 cookie
------------------------------------
Cursor 有两种会话 JWT：

  * 浏览器 cookie `WorkosCursorSessionToken` 解出来是 **web token**
    (`type:"web"`, `aud:"https://cursor.com"`)。cursor.com 网站认它，
    **api2.cursor.sh 的 gRPC 后端一律拒**（`GetUsableModels` 401）。
  * IDE token (`type:"session"`) 才是 api2/agent 接受的那把。

而 9router 的 `POST /api/oauth/cursor/import` 的 `validateImportToken`
**根本不发网络请求** —— 只查 `token.length >= 50` 和 machineId 是 32+ 位 hex，
然后无条件写 `testStatus:"active"`。所以：

  🔴 **import 返 200 只证明写了一行，什么都没证明。**

拿 web token 直接 import 的后果是一条"看起来 active、实际打不动模型"的死腿：
`GET /api/providers/<id>/models` 会返 **n=14 的静态兜底表**
（`claude-4.5-opus-*` 那一代）并带
`warning: "Cursor returned no live models; falling back to static catalog."`，
pod 日志里是 `CURSOR_MODELS Live model fetch failed: Cursor GetUsableModels returned 401`。
健康的腿返 **n=223 的实时表**、无 warning。

所以本脚本做的是：web cookie → **PKCE deep-login 换 IDE token** → import → 验收。

deep-login 无头配方（2026-09-17 实测，不需要浏览器）
----------------------------------------------------
1. `verifier H = base64url(32 随机字节)`；`challenge q = base64url(sha256(H 的 ascii))`；`uuid K`
2. 先 GET `https://cursor.com/loginDeepControl?challenge=q&uuid=K&mode=login` 把 cookie jar 焐热。
   ⚠️ **必须带 cookie jar**：该页会 307 到 `/api/auth/bootstrap-cursor-web-target`
   再 307 回来，靠它下发的 `cursor-web-target-synced-user` 收敛；不收 set-cookie
   就是无限 307（undici 报 `redirect count exceeded`），读起来像"页面挂了"。
3. 批准：`POST https://cursor.com/api/auth/loginDeepCallbackControl`
   body `{uuid, challenge, redirectTarget:null, mobile:false}`。
   ⚠️ **不要传 `selectedTeamId`**：传 `null`/`0`/`-1` 都返 `400 Invalid selected team`；
   整个字段省掉才 `200 OK`（个人号没有 team）。
4. poll `GET https://api2.cursor.sh/auth/poll?uuid=K&verifier=H`
   → 404 = pending；200 → `{accessToken(IDE token), refreshToken, authId}`。

批准这一步纯 cookie 鉴权、无 Turnstile，所以**机房 IP 可以直接做**。
本脚本整套跑在 **9router pod 内**（它本来就有到 Cursor 的出口），
不在本机发起 —— 本机在大陆，直连 cursor.com 会挂。

用法
----
    CURSOR_WEB_COOKIE='user_01XXXX::eyJhbGciOi...' \
        scripts/9router-cursor/add-cursor-account.py --apply

    scripts/9router-cursor/add-cursor-account.py --list     # 只读，看池子现状 + 每条腿死活

凭据只经 env 进来、只经 base64 进 pod、跑完即删；**任何路径都不打印 token**。

验收判据（`--apply` 自带，不过门就报红并保留原状）
    models n >= 100  且  同时含 `claude-opus-5-medium` / `claude-fable-5-1-medium`
（这两个 id 正是 litellm 侧 `cursor-fc-opus-5` / `cursor-fc-fable-5.1` 两条 lane
 真正请求的名字：`openai/cu/claude-opus-5-medium` / `openai/cu/claude-fable-5-1-medium`。）

⚠️ 新腿 import 后是 `priority = 2`（现役那把是 1）= **备用腿**，
实测 8/8 真流量全落 priority 1。所以"新腿 usageHistory 为 0"是**设计如此，不是故障**
（同 feedback_sub2api_schedulable_true_is_not_serving）。判它能用只能靠上面那条
实时 models 拉取 —— 那是一次带 token 的真上游 gRPC 调用。

回滚
----
    DELETE /api/providers/<id>          # 摘掉这条腿；或 PATCH isActive=0 先停用
    pod 内 /app/data/db/data.sqlite.bak-<ts>   # 本脚本 --apply 前自动备份
"""

import argparse
import base64
import json
import os
import pathlib
import re
import subprocess
import sys

NS = "litellm-product"
LABEL = "app=9router"
JMS_HOST = "10.68.13.198"
JMS = str(pathlib.Path(__file__).resolve().parents[1] / "jms")


def jms_bash(script: str, timeout: int = 600) -> str:
    """Run a bash script on 198 over jms, retrying the known auth flake."""
    last = ""
    for _ in range(3):
        # the remote command must be ONE argv element -- `"bash", "-s"` makes
        # jms itself reject `-s` as an unknown flag.
        p = subprocess.run([JMS, "ssh", JMS_HOST, "bash -s"],
                           input=script, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        if "Permission denied (password,publickey)" not in out:
            return out
        last = out
    raise SystemExit("jms auth kept failing:\n" + last[-800:])


def in_pod(node_src: str, timeout: int = 600) -> str:
    """Ship a node program into the 9router pod and run it there.

    base64 because nesting JS regexes/`${}` through
    ssh -> sh -c -> node -e gets eaten (`bad substitution`).
    """
    b64 = base64.b64encode(node_src.encode()).decode()
    return jms_bash(
        'set -u\n'
        f'P=$(sudo kubectl -n {NS} get pods -l {LABEL} '
        "-o jsonpath='{.items[0].metadata.name}')\n"
        'echo "POD=$P"\n'
        f'sudo kubectl -n {NS} exec $P -- sh -c '
        f'"echo \'{b64}\' | base64 -d > /tmp/_9r.js && node /tmp/_9r.js; rm -f /tmp/_9r.js"\n',
        timeout=timeout,
    )


# ── the node payloads ────────────────────────────────────────────────────────
PRELUDE = r"""
const fs=require("fs"),crypto=require("crypto"),initSql=require("/app/node_modules/sql.js");
const B="http://127.0.0.1:20128";
const PW=process.env.INITIAL_PASSWORD||"123456";
const UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        +"(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36";
const LANE_MODELS=["claude-opus-5-medium","claude-fable-5-1-medium"];
async function login(){
  const r=await fetch(B+"/api/auth/login",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({password:PW})});
  if(!r.ok) throw new Error("9router admin login failed: "+r.status);
  return (r.headers.get("set-cookie")||"").split(";")[0];
}
async function listLegs(ck){
  const r=await fetch(B+"/api/providers",{headers:{cookie:ck}});
  const j=await r.json().catch(()=>({}));
  return Array.isArray(j)?j:(j.connections||j.providers||[]);
}
async function legHealth(ck,id){
  const r=await fetch(`${B}/api/providers/${id}/models`,{headers:{cookie:ck}});
  const j=await r.json().catch(()=>({}));
  const arr=j.models||[];
  return {status:r.status,n:arr.length,warning:j.warning||null,
          lanes:LANE_MODELS.map(m=>[m,arr.some(x=>x.id===m)]),
          live:!j.warning&&arr.length>=100&&LANE_MODELS.every(m=>arr.some(x=>x.id===m))};
}
"""

LIST_SRC = PRELUDE + r"""
(async()=>{
  const ck=await login();
  const legs=await listLegs(ck);
  console.log("LEGS n="+legs.length);
  for(const l of legs){
    const h=await legHealth(ck,l.id);
    console.log(`  ${l.id}  pri=${l.priority} active=${l.isActive} `
      +`sub=${l.email}\n     models=${h.n} live=${h.live} `
      +`warning=${JSON.stringify(h.warning)} lanes=${JSON.stringify(h.lanes)}`);
  }
})().catch(e=>{console.log("ERR "+e.message);process.exit(1);});
"""

APPLY_SRC = PRELUDE + r"""
const b64url=b=>Buffer.from(b).toString("base64")
  .replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
const jar=new Map();
const ck2=()=>[...jar.entries()].map(([k,v])=>k+"="+v).join("; ");
function absorb(r){
  for(const c of (r.headers.getSetCookie?r.headers.getSetCookie():[])){
    const kv=c.split(";")[0], i=kv.indexOf("=");
    const k=kv.slice(0,i).trim(), v=kv.slice(i+1).trim();
    if(v==="") jar.delete(k); else jar.set(k,v);
  }
}
// cursor.com bounces loginDeepControl through bootstrap-cursor-web-target and
// only settles once its cookies are echoed back -- without the jar this is an
// infinite 307 loop, NOT a broken page.
async function hop(url,init={},max=8){
  for(let i=0;i<max;i++){
    const r=await fetch(url,{...init,headers:{...(init.headers||{}),cookie:ck2(),"User-Agent":UA},
                            redirect:"manual"});
    absorb(r);
    const loc=r.headers.get("location");
    if(r.status>=300&&r.status<400&&loc){url=new URL(loc,url).toString();init={headers:init.headers};continue;}
    return r;
  }
  throw new Error("redirect loop on "+url);
}
(async()=>{
  const raw=(process.env.CURSOR_WEB_COOKIE||"").trim();
  const m=raw.match(/^(user_[A-Za-z0-9]+)::(.+)$/);
  if(!m) throw new Error("CURSOR_WEB_COOKIE must look like user_xxx::<jwt>");
  const uid=m[1], webJwt=m[2];
  const claims=JSON.parse(Buffer.from(webJwt.split(".")[1],"base64").toString());
  console.log("web token: type="+claims.type+" sub="+claims.sub+" exp="+claims.exp);
  jar.set("WorkosCursorSessionToken",encodeURIComponent(raw));

  // 0) positive control: does this cookie authenticate cursor.com at all?
  const me=await fetch("https://cursor.com/api/auth/me",{headers:{cookie:ck2(),"User-Agent":UA}});
  const mej=await me.json().catch(()=>({}));
  if(!me.ok) throw new Error("cookie does not authenticate cursor.com: "+me.status);
  console.log("cursor.com/api/auth/me -> 200 email="+mej.email+" verified="+mej.email_verified);

  const ck=await login();
  const before=await listLegs(ck);
  console.log("legs before="+before.length);
  if(before.some(l=>(l.email||"").endsWith(claims.sub.replace(/^auth0\|/,""))))
    throw new Error("this account is already in the pool -- refusing to duplicate");

  // 1) PKCE deep-login: web token -> IDE token
  const H=b64url(crypto.randomBytes(32));
  const q=b64url(crypto.createHash("sha256").update(H,"ascii").digest());
  const K=crypto.randomUUID();
  const page=`https://cursor.com/loginDeepControl?challenge=${q}&uuid=${K}&mode=login`;
  await hop(page,{headers:{accept:"text/html"}});
  // NOTE: selectedTeamId must be ABSENT -- null/0/-1 all give 400 "Invalid selected team".
  const ap=await fetch("https://cursor.com/api/auth/loginDeepCallbackControl",{method:"POST",
    headers:{cookie:ck2(),"Content-Type":"application/json","User-Agent":UA,
             origin:"https://cursor.com",referer:page},
    body:JSON.stringify({uuid:K,challenge:q,redirectTarget:null,mobile:false})});
  const apTxt=(await ap.text()).slice(0,160);
  console.log("loginDeepCallbackControl -> "+ap.status+" "+apTxt.replace(/\s+/g," "));
  if(!ap.ok) throw new Error("deep-login approval refused");

  let ide=null;
  for(let i=0;i<15&&!ide;i++){
    const p=await fetch(`https://api2.cursor.sh/auth/poll?uuid=${K}&verifier=${H}`,
      {headers:{"x-cursor-client-version":"3.12.17","x-cursor-client-type":"ide",
                "User-Agent":UA,accept:"application/json"}});
    const t=await p.text();
    if(p.status===200&&t.includes("accessToken")) ide=JSON.parse(t);
    else { console.log("poll#"+i+" "+p.status); await new Promise(s=>setTimeout(s,3000)); }
  }
  if(!ide) throw new Error("poll exhausted -- no IDE token");
  const ic=JSON.parse(Buffer.from(ide.accessToken.split(".")[1],"base64").toString());
  console.log("IDE token: type="+ic.type+" len="+ide.accessToken.length
              +" hasRefresh="+!!ide.refreshToken);
  if(ic.type!=="session") throw new Error("exchanged token is type="+ic.type+", not session");

  // 2) import (fresh machineId -- do NOT reuse another account's)
  const machineId=crypto.randomBytes(32).toString("hex");
  const imp=await fetch(B+"/api/oauth/cursor/import",{method:"POST",
    headers:{cookie:ck,"Content-Type":"application/json"},
    body:JSON.stringify({accessToken:ide.accessToken,
                         refreshToken:ide.refreshToken||undefined,machineId})});
  const ib=await imp.json().catch(()=>({}));
  const id=ib?.connection?.id;
  console.log("import -> "+imp.status+" id="+id);
  if(!id) throw new Error("import did not return a connection id");

  // 3) the only judge that touches the upstream: live model fetch
  await new Promise(s=>setTimeout(s,2000));
  const h=await legHealth(ck,id);
  console.log("VERIFY models="+h.n+" warning="+JSON.stringify(h.warning)
              +" lanes="+JSON.stringify(h.lanes));
  const after=await listLegs(ck);
  console.log("legs after="+after.length);
  for(const l of after) console.log("   "+l.id+" pri="+l.priority+" active="+l.isActive);
  if(!h.live){
    console.log("RED: leg registered but Cursor did not return a live model list "
      +"(a static n=14 catalog + warning means the token is rejected upstream). "
      +"Roll back with DELETE /api/providers/"+id);
    process.exit(1);
  }
  console.log("GREEN: leg "+id+" serves a live catalog incl. both lane models. "
    +"It is priority "+(after.find(l=>l.id===id)||{}).priority
    +" = standby; real traffic stays on priority 1 until that one is unavailable.");
})().catch(e=>{console.log("ERR "+e.message);process.exit(1);});
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="只读：列出池里每条腿，并对每条腿做一次实时 models 拉取判死活")
    g.add_argument("--apply", action="store_true",
                   help="deep-login 换 IDE token → import → 验收（先自动备份 pod sqlite）")
    a = ap.parse_args()

    if a.list:
        print(in_pod(LIST_SRC))
        return

    cookie = os.environ.get("CURSOR_WEB_COOKIE", "").strip()
    if not re.match(r"^user_[A-Za-z0-9]+::[\w-]+\.[\w-]+\.[\w-]+$", cookie):
        raise SystemExit("set CURSOR_WEB_COOKIE='user_xxx::<jwt>' "
                         "(the raw WorkosCursorSessionToken cookie value)")

    # back the pod's sqlite up before the app writes a row
    print(jms_bash(
        'set -u\n'
        f'P=$(sudo kubectl -n {NS} get pods -l {LABEL} '
        "-o jsonpath='{.items[0].metadata.name}')\n"
        'TS=$(date -u +%Y%m%dT%H%M%SZ)\n'
        f'sudo kubectl -n {NS} exec $P -- sh -c '
        '"cp /app/data/db/data.sqlite /app/data/db/data.sqlite.bak-$TS '
        '&& ls -la /app/data/db/data.sqlite.bak-$TS"\n'))

    b64 = base64.b64encode(APPLY_SRC.encode()).decode()
    # the cookie rides in as an env var on the exec, never as a file in the repo
    out = jms_bash(
        'set -u\n'
        f'P=$(sudo kubectl -n {NS} get pods -l {LABEL} '
        "-o jsonpath='{.items[0].metadata.name}')\n"
        'echo "POD=$P"\n'
        f'sudo kubectl -n {NS} exec $P -- sh -c '
        f'"echo \'{b64}\' | base64 -d > /tmp/_9r.js && '
        f'CURSOR_WEB_COOKIE={json.dumps(cookie)} node /tmp/_9r.js; '
        'rc=$?; rm -f /tmp/_9r.js; exit $rc"\n')
    print(out)
    if "GREEN:" not in out:
        sys.exit(1)


if __name__ == "__main__":
    main()
