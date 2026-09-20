#!/usr/bin/env bash
# 上一版两条腿都没拿到 402 原文:
#   * 9router 日志 grep 出来是空的(它不往 stdout 写上游错误)
#   * 直打 127.0.0.1:3000 "fetch failed" —— 端口是我猜的,先查真端口
# 但拿到了 9router 自己的表名,其中 requestDetails / usageHistory 才是权威源:
# 402 的上游响应体应该落在 requestDetails 里。
#
# 另外 providerConnections 只有 1 行(cursor / oauth / isActive=1,
# updatedAt=2026-09-16T09:40:38Z)。09:40 UTC = 17:40 北京,
# 到现在 21:23 是 3h43m,远没到 24h ——
# 所以"token 到期"这个假设已经和数据不吻合了,别再往那个方向猜。
set -uo pipefail
NS9=litellm-product
P9=9router-6755f9f964-6jl7v

echo "=== 1) 9router 真实监听端口 ==="
sudo kubectl -n $NS9 get pod $P9 -o jsonpath='{range .spec.containers[*]}{.name}{" ports="}{range .ports[*]}{.containerPort}{","}{end}{"\n"}{end}' < /dev/null
sudo kubectl -n $NS9 get svc 2>/dev/null | grep -i 9router

echo
echo "=== 2) requestDetails 最近 10 条(含上游状态码与错误体) ==="
sudo kubectl -n $NS9 exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
const cols=db.prepare('pragma table_info(requestDetails)').all().map(c=>c.name);
console.log('列:',cols.join(', '));
const rows=db.prepare('select * from requestDetails order by rowid desc limit 10').all();
for(const r of rows){
  const o={};
  for(const [k,v] of Object.entries(r)){
    if(/key|token|secret|cookie/i.test(k)){o[k]=v?('len='+String(v).length):v;continue;}
    o[k]=(typeof v==='string'&&v.length>400)?String(v).slice(0,400)+'..':v;
  }
  console.log(JSON.stringify(o));
  console.log('');
}
" < /dev/null 2>&1 | tail -45

echo
echo "=== 3) usageHistory 最近 8 条(看是否有额度/credit 线索) ==="
sudo kubectl -n $NS9 exec -i $P9 -- node -e "
const D=require('/app/node_modules/better-sqlite3');
const db=new D('/app/data/db/data.sqlite',{readonly:true});
for(const t of ['usageHistory','usageDaily']){
  const cols=db.prepare('pragma table_info('+t+')').all().map(c=>c.name);
  console.log('--- '+t+' 列: '+cols.join(', '));
  const rows=db.prepare('select * from '+t+' order by rowid desc limit 8').all();
  for(const r of rows) console.log('  '+JSON.stringify(r).slice(0,300));
}
" < /dev/null 2>&1 | tail -25
