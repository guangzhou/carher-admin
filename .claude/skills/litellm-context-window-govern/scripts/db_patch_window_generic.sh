#!/usr/bin/env bash
# 198（DB 模式）外科式给一批 model_name 补上 max_input_tokens —— 参数化版。
# 用法: TARGETS="'chatgpt-gpt-5.6-sol','chatgpt-gpt-5.6-luna'" EXPECT=88 NEW=922000 \
#       bash db_patch_window_generic.sh [--apply]
# 五道门：备份+断言行数 / 旧值条件(仅改键不存在的行) / UPDATE N 对上 / 回读目标行+抽样看其余键未动 /
#         回读证明不该动的两组（cursor 的 10000000、astra）没被动。
# ⚠️ DB 里 <none> 不等于「继承到了合理值」，改前改后都要用 modelinfo_per_replica.sh 看 /model/info。
# ⚠️ DATABASE_URL 带 ?connection_limit=，psql 不认，已 ${URL%%\?*} 掉。
# 2026-09-06 用它给 chatgpt-gpt-5.6-{sol,terra,luna} 132 行补 922000，100s 内 4 副本自行收敛，零 rollout。
set -uo pipefail
NS=litellm-product
TARGETS="${TARGETS:?需要 TARGETS，形如 \"'a','b'\"}"
EXPECT="${EXPECT:?需要 EXPECT 行数}"
NEW="${NEW:-922000}"
P=$(kubectl -n $NS get po -l app=litellm-proxy --no-headers -o name | head -1 | cut -d/ -f2)
URL=$(kubectl -n $NS exec $P -- printenv DATABASE_URL 2>/dev/null | tr -d '\r'); URL=${URL%%\?*}
q(){ kubectl -n $NS exec litellm-db-0 -- psql "$URL" -A -F'|' -c "$1"; }

TS=$(date +%Y%m%d-%H%M%S); BAK=/tmp/gpt56-rows-$TS.tsv
q "copy (select model_id, model_name, model_info from \"LiteLLM_ProxyModelTable\"
     where model_name in ($TARGETS) order by model_id) to stdout" > $BAK
N=$(wc -l < $BAK)
echo "== 备份 $BAK 行数=$N sha256=$(sha256sum $BAK | cut -d' ' -f1)"
[ "$N" = "$EXPECT" ] || { echo "ABORT: 备份行数 $N != $EXPECT"; exit 1; }

echo "== 门1：确认这 $EXPECT 行当前全都没有 max_input_tokens 键（旧值条件）"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name in ($TARGETS) group by 1;"

echo "== 门2：记下不该动的对照基线"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name like 'cursor%' group by 1 order by 2 desc;"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name='chatgpt-gpt-6-astra' group by 1;"

if [ "${1:-}" != "--apply" ]; then echo "dry-run，未写入。"; exit 0; fi

echo "== 写入（带旧值条件：仅改 max_input_tokens 键不存在的行）"
q "update \"LiteLLM_ProxyModelTable\"
   set model_info = jsonb_set(model_info::jsonb,'{max_input_tokens}',to_jsonb($NEW::bigint), true)
   where model_name in ($TARGETS)
     and model_info->>'max_input_tokens' is null;"

echo "== 回读：目标行"
q "select model_name, coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name in ($TARGETS) group by 1,2 order by 1;"
echo "== 回读：其余键没被动（抽一行看完整 model_info）"
q "select model_info from \"LiteLLM_ProxyModelTable\" where model_name='chatgpt-gpt-5.6-luna' limit 2;"
echo "== 回读：不该动的两组"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name like 'cursor%' group by 1 order by 2 desc;"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name='chatgpt-gpt-6-astra' group by 1;"
