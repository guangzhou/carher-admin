#!/usr/bin/env bash
# 198（DB 模式）外科式改 LiteLLM_ProxyModelTable 的窗口字段。
# 四道门：备份目标行(copy to stdout)+断言行数 / update 带旧值条件 / UPDATE N 对上期望 / 回读
#          + 显式回读证明不该动的行（cursor-* 的 10000000）没被动。
# 用法: bash db_patch_window.sh          # dry-run，只备份+盘点
#       bash db_patch_window.sh --apply
# 注意 DATABASE_URL 带 ?connection_limit=... ，psql 不认，脚本里已 ${URL%%\?*} 掉。
# 改完先跑 modelinfo_per_replica.sh 等 DB 轮询收敛，收敛不了再 rollout。见 SKILL.md §5。
set -uo pipefail
NS=litellm-product
P=$(kubectl -n $NS get po -l app=litellm-proxy --no-headers -o name | head -1 | cut -d/ -f2)
URL=$(kubectl -n $NS exec $P -- printenv DATABASE_URL 2>/dev/null | tr -d '\r'); URL=${URL%%\?*}
q(){ kubectl -n $NS exec litellm-db-0 -- psql "$URL" -A -F'|' -c "$1"; }

# ⚠️ `<none>` 不等于「继承 922000，不用动」——198 上 chatgpt-gpt-5.6-{sol,terra,luna} 的 132 行
#    DB 里都是 <none>，运行时却读到 10,000,000（=闸门关掉）。**判据只能是 /model/info，不是 DB。**
echo "== 5.6 三档现状（DB 层；<none> 必须再去 /model/info 复核运行时读到什么）"
q "select model_name, coalesce(model_info->>'max_input_tokens','<none>'), count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name like '%5.6%' group by 1,2 order by 1;"

TS=$(date +%Y%m%d-%H%M%S); BAK=/tmp/astra-rows-$TS.tsv
q "copy (select model_id, model_name, model_info from \"LiteLLM_ProxyModelTable\"
     where model_name='chatgpt-gpt-6-astra') to stdout" > $BAK
echo "== 备份 $BAK 行数=$(wc -l < $BAK) sha256=$(sha256sum $BAK | cut -d' ' -f1)"
[ "$(wc -l < $BAK)" = "26" ] || { echo "ABORT: 备份行数 != 26"; exit 1; }

if [ "${1:-}" != "--apply" ]; then echo "dry-run，未写入。"; exit 0; fi
q "update \"LiteLLM_ProxyModelTable\"
   set model_info = jsonb_set(model_info::jsonb,'{max_input_tokens}','922000'::jsonb)
   where model_name='chatgpt-gpt-6-astra'
     and model_info->>'max_input_tokens'='1050000';"
echo "== 回读"
q "select coalesce(model_info->>'max_input_tokens','<none>'), count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name='chatgpt-gpt-6-astra' group by 1;"
echo "== 确认 cursor 那批没被动"
q "select coalesce(model_info->>'max_input_tokens','<none>'), count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name like 'cursor%' group by 1 order by 2 desc;"
