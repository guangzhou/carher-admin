#!/usr/bin/env python3
"""查/开 Cursor 账号的 **no-ZDR 模型数据保留同意**——fable 族能不能用，只由它决定。

2026-09-22 之前踩了整整一天的坑
--------------------------------
现象：9router 两条腿，leg2 打 `cu/claude-fable-5-1-medium` **从来没成功过一次**，
形状是首帧 45s 死线 → 504 → 换腿；leg1 一死，`cursor-fc-fable-5.1` 车道零活腿。

⛔ 一路上骗过我的尺子（都别再用）：
  * `AvailableModels` 的**模型名清单**：两条腿都 n=225、fable 20 个一个不少 ⇒
    "没开通"这个假设就是被它证伪的。**差别不在有没有这个名字，在条目里的字段。**
  * `full_stripe_profile` = pro/ultra active、token exp 还有两个月 ⇒ 全假绿。
  * `x-ghost-mode` 请求头：**两个方向都无效**（leg1 强制 `true` 照样不被封、
    leg2 给 `false` 照样解不开）⇒ 这是**服务端账号态，不是请求态**，改客户端没用。
  * `get-user-privacy-mode`：两条腿**同为** `PRIVACY_MODE_NO_TRAINING` ⇒
    隐私模式不是区分点。**"privacy mode 关掉就好了"是错的，别去动它。**
  * cursor.com 上**任何不存在的路由都返 200 + Next.js 的 HTML 404 页**。
    拿 HTTP 码判路由存不存在 = 满屏假绿。判据只能是"**响应体是不是 `<!DOCTYPE`**"。

✅ 真正的判据，在 `AvailableModels` 每个条目的两个字段上：

        "requiresDataRetention": true,
        "reasonForZdrConsentBlock": "individual_settings_blocked"

  leg2 有 20 条带这两个字段，leg1 **一条都没有** —— 这就是唯一的差异，
  也是 leg1 能跑 fable、leg2 从来跑不了的全部原因。

机制：fable 族落在 Cursor 的 ZDR（零数据保留）协议之外，必须账号显式同意
"该模型的请求/输出会被模型提供方保留（用于自动+必要时人工的滥用防护），
**不受 Cursor Privacy Mode 约束**"，才放行。**这是一次性的账号级开关。**

接口（从 cursor.com dashboard 的 Next.js chunk 里挖出来的，不是猜的）
------------------------------------------------------------------
    POST /api/dashboard/set-user-no-zdr-model-consent
         {modelId, enabled, acknowledged, consentVersion}
    POST /api/dashboard/get-no-zdr-model-consent-status
         {scope: "USER"|"TEAM"|"TEAM_MEMBER", teamId}

`modelId` 是**族级**名（`claude-fable-5-1` / `claude-fable-5`），不是
`claude-fable-5-1-medium` 这种 effort 变体名；开一次，该族 low/medium/high/xhigh
20 个变体一起放行。`consentVersion` 现行值 `fable-data-retention-v1`。

⚠️ `get-no-zdr-model-consent-status` 目前对个人号恒返
`400 "Unknown consent status scope"`（三个 scope 值都试过）。**这是上游的毛病，
不是没同意**——所以本脚本判"开没开"不看它，只看 `AvailableModels` 里
`reasonForZdrConsentBlock` 的条数。那是唯一一把带判别力的尺子。

不用 web cookie：session token 自己就是 web cookie
--------------------------------------------------
🔴 **9router 池里存的 IDE token（`type:"session"`）直接当
`WorkosCursorSessionToken` 用，cursor.com 就认**（`/api/auth/me` 返 200）：

    cookie = WorkosCursorSessionToken=<urlencode(<workosId>::<IDE token>)>
    其中 workosId = JWT 里的 `sub` 去掉 `auth0|` 前缀

所以**不需要再找人要浏览器 cookie**——池里已经有的号可以自助改自己的账号设置。
（add-cursor-account.py 的注释说"web token 和 IDE token 是两把、api2 拒 web token"
 仍然成立；**反方向能走**是这次新发现的，两句话不矛盾。）

用法
----
    scripts/9router-cursor/cursor-zdr-consent.py --status     # 只读：每条腿被挡了几个模型
    scripts/9router-cursor/cursor-zdr-consent.py --enable     # 给所有被挡的腿开 fable 同意
    scripts/9router-cursor/cursor-zdr-consent.py --enable --leg 0dfdb1fc   # 只开一条

全程不打印 token、不落盘；所有网络请求都在 9router pod 内发起（本机在大陆直连不了 cursor.com）。

⚠️ 这是**放宽数据保留**的动作，含义见上。新号入池后如果要跑 fable，必须跑一次
   `--enable`，否则表现就是"莫名其妙 45s 超时"，而所有便宜尺子都是绿的。
"""

import argparse
import importlib.util
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

# 族级 modelId -> consentVersion。新 fable 代际出来时在这里加一行。
CONSENTS = [("claude-fable-5-1", "fable-data-retention-v1"),
            ("claude-fable-5",   "fable-data-retention-v1")]

PRELUDE = r"""
const fs=require("fs"),initSql=require("/app/node_modules/sql.js");
const UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        +"(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36";
const D="https://cursor.com/api/dashboard/";
// IDE token -> web cookie。sub 去掉 auth0| 前缀就是 workosId。
const ckOf=t=>"WorkosCursorSessionToken="+encodeURIComponent(
  JSON.parse(Buffer.from(t.split(".")[1],"base64").toString()).sub.replace(/^auth0\|/,"")+"::"+t);
const post=(ck,n,b)=>fetch(D+n,{method:"POST",headers:{cookie:ck,"User-Agent":UA,
  "content-type":"application/json",origin:"https://cursor.com",
  referer:"https://cursor.com/dashboard/settings"},body:JSON.stringify(b)});
// 唯一有判别力的尺子：被 ZDR 挡住的条目数。0 = 已同意/无需同意。
async function blocked(tok){
  const r=await fetch("https://api2.cursor.sh/aiserver.v1.AiService/AvailableModels",
    {method:"POST",headers:{authorization:"Bearer "+tok,"content-type":"application/json",
     "connect-protocol-version":"1"},body:"{}"});
  const ms=(await r.json()).models||[];
  const b=ms.filter(x=>x.reasonForZdrConsentBlock);
  const reasons={}; b.forEach(x=>reasons[x.reasonForZdrConsentBlock]=(reasons[x.reasonForZdrConsentBlock]||0)+1);
  return {total:ms.length,blocked:b.length,reasons,
          sample:b.slice(0,3).map(x=>x.name)};
}
function legs(){
  return initSql().then(SQL=>{
    const db=new SQL.Database(new Uint8Array(fs.readFileSync("/app/data/db/data.sqlite")));
    return db.exec("SELECT id,priority,data FROM providerConnections ORDER BY priority")[0]
      .values.map(v=>({id:String(v[0]),pri:v[1],tok:JSON.parse(v[2]).accessToken}));
  });
}
"""

STATUS_SRC = PRELUDE + r"""
(async()=>{
 for(const L of await legs()){
   const b=await blocked(L.tok);
   const me=await (await post(ckOf(L.tok),"get-me",{})).json().catch(()=>({}));
   console.log(`leg ${L.id.slice(0,8)} pri=${L.pri} email=${me.email||"?"} `
     +`models=${b.total} zdrBlocked=${b.blocked} ${JSON.stringify(b.reasons)} ${JSON.stringify(b.sample)}`);
 }
})().catch(e=>{console.log("ERR "+e.message);process.exit(1);});
"""

ENABLE_SRC = PRELUDE + r"""
const ONLY=__ONLY__, CONSENTS=__CONSENTS__;
(async()=>{
 let touched=0;
 for(const L of await legs()){
   if(ONLY && !L.id.startsWith(ONLY)) continue;
   const before=await blocked(L.tok);
   if(before.blocked===0){ console.log(`leg ${L.id.slice(0,8)} already clear (blocked=0) -- skipped`); continue; }
   console.log(`leg ${L.id.slice(0,8)} pri=${L.pri} BEFORE blocked=${before.blocked} ${JSON.stringify(before.reasons)}`);
   for(const [modelId,ver] of CONSENTS){
     const r=await post(ckOf(L.tok),"set-user-no-zdr-model-consent",
       {modelId,enabled:true,acknowledged:true,consentVersion:ver});
     console.log(`   set ${modelId} -> ${r.status} ${(await r.text()).slice(0,120).replace(/\s+/g," ")}`);
   }
   const after=await blocked(L.tok);
   console.log(`leg ${L.id.slice(0,8)} AFTER  blocked=${after.blocked} `
     +(after.blocked===0?"GREEN":"STILL BLOCKED "+JSON.stringify(after.reasons)));
   touched++;
 }
 if(!touched) console.log("nothing to do");
})().catch(e=>{console.log("ERR "+e.message);process.exit(1);});
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="只读：每条腿被 ZDR 挡住几个模型")
    g.add_argument("--enable", action="store_true", help="对被挡的腿开 fable 数据保留同意")
    ap.add_argument("--leg", help="只处理 id 以此开头的那条腿")
    a = ap.parse_args()

    spec = importlib.util.spec_from_file_location("acct", HERE / "add-cursor-account.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if a.status:
        print(mod.in_pod(STATUS_SRC, timeout=600))
        return
    src = (ENABLE_SRC.replace("__ONLY__", json.dumps(a.leg) if a.leg else "null")
                     .replace("__CONSENTS__", json.dumps(CONSENTS)))
    print(mod.in_pod(src, timeout=900))


if __name__ == "__main__":
    main()
