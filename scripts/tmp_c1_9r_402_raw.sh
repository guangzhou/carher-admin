#!/usr/bin/env bash
# 已排除的假设(用数据,不是感觉):
#   * "24h token 过期" —— providerConnections 只有 1 行,updatedAt=2026-09-16T09:40:38Z
#     = 北京 17:40,到现在 21:2x 才 3h4x,远没到 24h。假设与数据不吻合,弃掉。
#   * requestDetails 是空表(只有列名没有行),9router 不往那儿存上游错误。
#
# 新线索(还只是线索,不是结论):
#   usageHistory 最后一条 = 12:48:36 UTC,之后**一条都没有**。
#   我 21:2x 打的那两发 402 没进这张表 => 402 是在到达 Cursor 之前就被拒的,
#   或者 9router 对失败请求不记账。这两种情况分不开,所以要直打拿原文。
#   usageDaily: 今天 cost=1.9479 / 107 requests,而 09-12~09-14 全是 cost=0。
#   今天突然开始计费 => 指向"用量计费额度"这个方向,但**没有额度上限读数,不许定性**。
#
# 本轮唯一目的: 拿到 402 的上游响应体原文。端口 20128(已从 pod spec 读到,不再猜)。
set -uo pipefail
NS9=litellm-product
P9=9router-6755f9f964-6jl7v

echo "=== 1) 直打 9router:20128,打印完整错误体 ==="
sudo kubectl -n $NS9 exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
const r=db.prepare(\"select key from apiKeys where isActive=1 and name='litellm-bridge'\").get();
(async()=>{
  for (const m of ['claude-fable-5-1-medium','composer-2.5']) {
    try{
      const res=await fetch('http://127.0.0.1:20128/v1/chat/completions',{method:'POST',
        headers:{'Content-Type':'application/json','Authorization':'Bearer '+r.key},
        body:JSON.stringify({model:m,max_tokens:20,messages:[{role:'user',content:'hi'}]})});
      const t=await res.text();
      console.log('--- '+m+' HTTP '+res.status+' ---');
      console.log(t.slice(0,1500));
    }catch(e){console.log('--- '+m+' EXC '+e.message);}
  }
})();
" < /dev/null 2>&1 | tail -40

echo
echo "=== 2) Cursor 账号额度实探(9router 存的 token 直问 Cursor) ==="
sudo kubectl -n $NS9 exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
const c=db.prepare(\"select data from providerConnections where provider='cursor' and isActive=1\").get();
const d=JSON.parse(c.data);
const tok=d.accessToken||d.sessionToken||d.token;
console.log('token 字段:',Object.keys(d).join(', '));
(async()=>{
  for(const u of ['https://api2.cursor.sh/auth/full_stripe_profile',
                  'https://api2.cursor.sh/dashboard/get-hard-limit']){
    try{
      const res=await fetch(u,{method:u.includes('hard-limit')?'POST':'GET',
        headers:{'Authorization':'Bearer '+tok,'Content-Type':'application/json'},
        body:u.includes('hard-limit')?'{}':undefined});
      console.log('--- '+u.split('/').slice(-1)[0]+' HTTP '+res.status);
      console.log('    '+(await res.text()).slice(0,600));
    }catch(e){console.log('--- '+u+' EXC '+e.message);}
  }
})();
" < /dev/null 2>&1 | tail -25

echo
echo "=== 3) settings 表里的额度/限额配置 ==="
sudo kubectl -n $NS9 exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
for(const r of db.prepare('select * from settings').all()){
  const s=JSON.stringify(r);
  if(/limit|quota|credit|budget|cost|max/i.test(s)) console.log(s.slice(0,400));
}
" < /dev/null 2>&1 | tail -15
