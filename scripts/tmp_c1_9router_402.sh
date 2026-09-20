#!/usr/bin/env bash
# 判别力已经有了: fable-5.1 / opus-5 (都走 9router/Cursor) = 402,
# 未改动的 grok 对照组 = 200。所以病在 9router 这条腿上,不是全局,也不是 LiteLLM 鉴权。
#
# "API 异常 (req: xxxx)" 是 9router 自己的包装文案,真因在 9router 日志里。
# 402 的两个常见形状:
#   (a) Cursor 账号额度打满 / 订阅到期
#   (b) 24h IDE session token 过期(早就标过这个单点: expiresIn=86400, refreshToken=null)
# 处理方式完全不同,所以必须看到上游原文再定性,不许拿 402 直接猜额度。
#
# 上一版全 ns 扫 pod 把 jms 连接拖超时了,这版写死已查到的 pod。
set -uo pipefail
NS9=litellm-product
P9=9router-6755f9f964-6jl7v

echo "=== 1) 9router 日志里的 402 / token 线索 ==="
sudo kubectl -n $NS9 logs $P9 --tail=300 2>/dev/null \
  | grep -iE '402|unauthor|expire|quota|limit|payment|balance|error' \
  | tail -25

echo
echo "=== 2) 现场直打 9router,拿上游原文(绕开 LiteLLM 的文案包装) ==="
sudo kubectl -n $NS9 exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
const r=db.prepare(\"select key from apiKeys where isActive=1 and name='litellm-bridge'\").get();
const body=JSON.stringify({model:'claude-fable-5.1',max_tokens:20,
  messages:[{role:'user',content:'hi'}]});
fetch('http://127.0.0.1:3000/v1/chat/completions',{method:'POST',
  headers:{'Content-Type':'application/json','Authorization':'Bearer '+r.key},body})
 .then(async res=>{console.log('HTTP',res.status);console.log((await res.text()).slice(0,1200));})
 .catch(e=>console.log('EXC',e.message));
" < /dev/null 2>&1 | tail -20

echo
echo "=== 3) Cursor 账号行状态(额度/过期,凭据只打长度) ==="
sudo kubectl -n $NS9 exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
const ts=db.prepare(\"select name from sqlite_master where type='table'\").all().map(r=>r.name);
console.log('表:',ts.join(', '));
for(const t of ts){
  if(!/account|provider|channel|credential|cursor/i.test(t)) continue;
  let rows; try{rows=db.prepare('select * from \"'+t+'\" limit 5').all();}catch(e){continue;}
  if(!rows.length) continue;
  console.log('--- '+t+' ---');
  for(const r of rows){
    const o={};
    for(const [k,v] of Object.entries(r)){
      if(/key|token|secret|cookie/i.test(k)) o[k]=v?('len='+String(v).length):v;
      else o[k]=(typeof v==='string'&&v.length>100)?String(v).slice(0,100)+'..':v;
    }
    console.log(JSON.stringify(o));
  }
}
" < /dev/null 2>&1 | tail -30
