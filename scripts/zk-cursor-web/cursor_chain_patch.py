#!/usr/bin/env python3
"""cursor_chain_patch.py — 第 9 补丁 @cx-chain:v3(客户端链式增量,真传输 fetch seam)。

机制定因(2026-08-30,lsof + trace + 逐 seam 实测钉死):
  - 真 composer /responses 由 **extension-host(Plugin helper)进程**发出(lsof:PID=exthost
    carher-admin,SSE 采样 7 次命中=聊天流)。shim 在该进程加载(installed×2)。
  - v1/v1b 覆盖 globalThis.fetch:**实测零 intercept**(2h 网关零真 Cursor chain 命中)——
    undici fetch 不碰 globalThis.fetch。
  - v2 包 cfe-responses-client 的 `fetch:t.fetch`:**实测零 wrap-entry**——该缝不载 composer 请求。
  - 两 undici export 探针(void 0 / t={}):**均未 fire**。
  真载荷路 = 工厂函数 `Cfe/dHt(e){const{modelId,userAgent,...customHeaders:d}=e,m=<builder>(r,..,d)...
  return(e,t)=>awaiter(function*(){ 改 body; yield m(url,init) })}`。
  **`m` = 底层标准 (url,init)->Response fetch**(ome/qqt 加 UA/header 后的);Cfe 返回的箭头改完
  body 再调 m → m 拿到的是最终全量请求体、回的是 SSE Response = 标准语义,__cxWrap 直接适配。

通用锚(逐 bundle minify 但 API 契约字段名不 minify、单字母右值 minifier 顺序分配恒等):
  `customHeaders:d}=e,m=`  两 bundle 各唯一 1×;其后 `<builder>(r,...,d)` builder 名逐 bundle 不同
  (ome/qqt)→ 用 **Python 括号配平**包住整个 `<builder>(...)` 调用,不依赖 builder 名。
  `m=X(...)` → `m=(globalThis.__cxWrap||(f=>f))(X(...))`(shim 没装时恒等,零行为差)。

契约:前缀一致→delta+previous_response_id;前缀不齐/首轮→原样全量;链式被拒(400/404
  code=previous_response_not_found)→清状态自动全量重发一次;响应侧记 resp_id。
安全阀:CX_CHAIN=0 关;幂等靠 MARKER;从 .pre-cxchain.bak 干净原始字节重打(不叠加旧版)。
trace:/tmp/cx-chain-trace.log(installed/wrap-entry/intercept/chained/passthrough-full/
  fallback-full/saved-rid/no-rid)。--revert 还原 bak。
"""
import hashlib
import shutil
import sys
from pathlib import Path

MARKER = "@cx-chain:v3"
APP = Path("/Applications/Cursor.app/Contents/Resources/app/extensions")
TARGETS = [
    APP / "cursor-agent-exec/dist/main.js",
    APP / "cursor-local-agent-runtime/dist/main.js",
]

# 通用锚:工厂解构尾 + m 赋值(两 bundle 各 count=1)。其后的 builder(...) 调用用括号配平包住。
ANCHOR = "customHeaders:d}=e,m="

SHIM = r"""/* @cx-chain:v3 — previous_response_id 链式增量(工厂底层 fetch seam;CX_CHAIN=0 关) */
;(()=>{try{
if(globalThis.__cxWrap)return;
const st=new Map();
const TRACE='/tmp/cx-chain-trace.log';
const trace=(o)=>{try{require('fs').appendFileSync(TRACE,JSON.stringify(Object.assign({ts:Date.now()},o))+'\n')}catch(_){}};
trace({ev:'installed',pid:(globalThis.process&&process.pid)||0});
const H=(s)=>{let h=5381;for(let i=0;i<s.length;i++)h=((h<<5)+h+s.charCodeAt(i))>>>0;return h.toString(36)};
const DJ=(o)=>{try{return H(JSON.stringify(o))}catch(_){return 'x'}};
globalThis.__cxWrap=function(ORIG){
  if(typeof ORIG!=='function')return ORIG;
  return async function(url,init){
    try{
      try{if(process.env.CX_CHAIN==='0')return ORIG(url,init)}catch(_){}
      const u=String(typeof url==='string'?url:(url&&url.url)||'');
      try{
        let bl=(init&&typeof init.body==='string')?init.body.length:-1, mc=-1, ic=-1, hasPrev=false, model='';
        if(bl>0){try{const jb=JSON.parse(init.body);mc=Array.isArray(jb.messages)?jb.messages.length:-1;ic=Array.isArray(jb.input)?jb.input.length:-1;hasPrev=!!jb.previous_response_id;model=String(jb.model||'')}catch(_){}}
        trace({ev:'wrap-entry',u:u.slice(0,60),bodyLen:bl,msgCount:mc,inputCount:ic,hasPrev,model});
      }catch(_){}
      if(!/\/responses(\?|$)/.test(u))return ORIG(url,init);
      if(!init||typeof init.body!=='string')return ORIG(url,init);
      let body;try{body=JSON.parse(init.body)}catch(_){return ORIG(url,init)}
      if(!Array.isArray(body.input)||body.input.length<1||body.previous_response_id)return ORIG(url,init);
      const key=DJ(body.input[0])+':'+String(body.model||'');
      const digs=body.input.map(DJ);
      const s=st.get(key);
      let chained=false,sendInit=init;
      if(s&&s.rid&&body.input.length>s.count){
        let ok=true;for(let i=0;i<s.count;i++)if(digs[i]!==s.digs[i]){ok=false;break}
        if(ok){
          const nb=Object.assign({},body,{input:body.input.slice(s.count),previous_response_id:s.rid});
          sendInit=Object.assign({},init,{body:JSON.stringify(nb)});
          chained=true;trace({ev:'chained',delta:body.input.length-s.count,total:body.input.length,rid:s.rid.slice(0,16)});
        }
      }
      if(!chained)trace({ev:'passthrough-full',inputLen:body.input.length,hadStored:!!s,u:u.slice(0,60)});
      let res=await ORIG(url,sendInit);
      if(chained&&res&&(res.status===400||res.status===404)){st.delete(key);trace({ev:'fallback-full',status:res.status});res=await ORIG(url,init)}
      if(res&&res.ok){
        try{
          res.clone().text().then((t)=>{
            try{
              const m=t.match(/"id"\s*:\s*"(resp_[^"]+)"/);
              if(m){st.set(key,{count:body.input.length,digs,rid:m[1]});if(st.size>50)st.delete(st.keys().next().value);trace({ev:'saved-rid',count:body.input.length,rid:m[1].slice(0,20)})}
              else{trace({ev:'no-rid',sample:t.slice(0,100)})}
            }catch(_){}
          }).catch(()=>{});
        }catch(_){}
      }
      return res;
    }catch(e){try{return ORIG(url,init)}catch(_){throw e}}
  };
};
}catch(_){}})();
"""


def wrap_builder_call(src: str):
    """在唯一锚 `customHeaders:d}=e,m=` 之后,用括号配平包住 `<builder>(...)` 调用。
    返回 (patched_src, ok)。找不到锚/括号不配平 → ok=False 拒绝改。"""
    n = src.count(ANCHOR)
    if n != 1:
        return src, False, f"锚 count={n}≠1"
    i = src.index(ANCHOR)
    j = i + len(ANCHOR)          # builder 名起点
    k = src.index("(", j)        # builder 名后第一个 (
    depth = 0
    p = k
    while p < len(src):
        c = src[p]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        p += 1
    if depth != 0:
        return src, False, "括号不配平"
    call = src[j:p + 1]          # 整个 <builder>(...)
    wrapped = ANCHOR + "(globalThis.__cxWrap||(f=>f))(" + call + ")"
    return src[:i] + wrapped + src[p + 1:], True, call[:40]


def main():
    revert = "--revert" in sys.argv
    for f in TARGETS:
        if not f.exists():
            print(f"SKIP(不存在): {f}")
            continue
        bak = f.with_suffix(".js.pre-cxchain.bak")
        if revert:
            if bak.exists():
                shutil.copy2(bak, f)
                print(f"REVERTED: {f}")
            else:
                print(f"SKIP(无备份): {f}")
            continue
        # 从干净原始字节重打:优先用 bak(v1/v2 之前的原始),不叠加旧补丁/探针
        if bak.exists():
            base = bak.read_text(encoding="utf-8", errors="replace")
        else:
            base = f.read_text(encoding="utf-8", errors="replace")
            bak.write_text(base, encoding="utf-8")
        if MARKER in base:
            print(f"SKIP(bak 已含 v3?): {f}")
            continue
        patched_body, ok, why = wrap_builder_call(base)
        if not ok:
            print(f"FAIL({why},拒绝改): {f}")
            continue
        # 追加:responses SDK 客户端的 fetch:t.fetch(count=1)也包上,覆盖 /responses 传输
        r_old = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:t.fetch})"
        r_new = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:(globalThis.__cxWrap||(f=>f))(t.fetch)})"
        rc = patched_body.count(r_old)
        if rc == 1:
            patched_body = patched_body.replace(r_old, r_new)
            why += " +resp:t.fetch"
        else:
            why += f" +resp:SKIP(count={rc})"
        # 强制端点走 responses:ufe 判定尾 ?"responses":"chat_completions" → 两半都 responses
        # (字符串字面量锚,两 bundle 变量名不同也命中;仅当 cfg.endpoint 未显式指定时生效)
        uf_old = '?"responses":"chat_completions"'
        uf_new = '?"responses":"responses"'
        uc = patched_body.count(uf_old)
        if uc == 1:
            patched_body = patched_body.replace(uf_old, uf_new)
            why += " +force-responses"
        else:
            why += f" +force-responses:SKIP(count={uc})"
        patched = SHIM + "\n" + patched_body
        f.write_text(patched, encoding="utf-8")
        md5 = hashlib.md5(f.read_bytes()).hexdigest()[:8]
        print(f"PATCHED(v3): {f}  md5={md5}  bak={bak.name}  wrapped={why}")


if __name__ == "__main__":
    main()
