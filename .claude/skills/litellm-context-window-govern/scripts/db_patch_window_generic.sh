#!/usr/bin/env bash
# 198（DB 模式）外科式给一批 model_name 写 max_input_tokens —— 参数化版。
# 用法: TARGETS="'chatgpt-gpt-5.6-sol','chatgpt-gpt-5.6-luna'" EXPECT=88 NEW=872000 \
#       [OLD=922000] [BASELINE_LIKE='cursor%'] bash db_patch_window_generic.sh [--apply]
# 两种模式（都幂等，重跑 UPDATE 0）：
#   OLD 不给 ⇒ 条件 `is null`，**只补空键**（原行为，向后兼容）
#   OLD 给了 ⇒ 条件 `= '$OLD'`，**替换已知旧值**（改错值必须走这个 —— is null 改不动已有值）
# 五道门：备份+断言行数 / 旧值条件 / UPDATE N 对上 / 回读目标行+抽样看其余键未动 /
#         回读证明不该动的对照组没动。
# ⚠️ DB 里 <none> 不等于「继承到了合理值」，改前改后都要用 modelinfo_per_replica.sh 看 /model/info。
#    2026-09-25 实测：zerokey-pool-gpt-5.6-* 在 DB 里全 <none>，运行时读到的是 10,000,000。
# ⚠️ DATABASE_URL 带 ?connection_limit=，psql 不认，已 ${URL%%\?*} 掉。
# 2026-09-06 用它给 chatgpt-gpt-5.6-{sol,terra,luna} 132 行补 922000，100s 内 4 副本自行收敛，零 rollout。
set -uo pipefail
NS=litellm-product
# 198 上 kubectl 要 sudo；以前这里焊死裸 kubectl，在 198 上直接跑不起来。
if [ -z "${KUBECTL:-}" ]; then KUBECTL="kubectl"; fi
TARGETS="${TARGETS:?需要 TARGETS，形如 \"'a','b'\"}"
EXPECT="${EXPECT:?需要 EXPECT 行数}"
NEW="${NEW:-872000}"
OLD="${OLD:-}"
# 不该动的对照组：以前这里硬编 cursor%（因为那 26 行的 10000000 当时被认为是有意的）。
# 但 cursor 那批本身也可能是要改的对象 ⇒ 基线组必须可传入，不能焊死。
BASELINE_LIKE="${BASELINE_LIKE:-cursor%}"
BASELINE_NAME="${BASELINE_NAME:-chatgpt-gpt-6-astra}"
SAMPLE_NAME="${SAMPLE_NAME:-}"
if [ -n "$OLD" ]; then COND="model_info->>'max_input_tokens' = '$OLD'"; MODE="替换旧值 $OLD -> $NEW";
else COND="model_info->>'max_input_tokens' is null";            MODE="只补空键 -> $NEW"; fi
echo "== 模式：$MODE"
P=$($KUBECTL -n $NS get po -l app=litellm-proxy --no-headers -o name | head -1 | cut -d/ -f2)
URL=$($KUBECTL -n $NS exec $P -- printenv DATABASE_URL 2>/dev/null | tr -d '\r'); URL=${URL%%\?*}
q(){ $KUBECTL -n $NS exec litellm-db-0 -- psql "$URL" -A -F'|' -c "$1"; }

TS=$(date +%Y%m%d-%H%M%S); BAK=/tmp/mit-rows-$TS.tsv
q "copy (select model_id, model_name, model_info from \"LiteLLM_ProxyModelTable\"
     where model_name in ($TARGETS) order by model_id) to stdout" > $BAK
N=$(wc -l < $BAK)
echo "== 备份 $BAK 行数=$N sha256=$(sha256sum $BAK | cut -d' ' -f1)"
[ "$N" = "$EXPECT" ] || { echo "ABORT: 备份行数 $N != $EXPECT"; exit 1; }

echo "== 门1：确认这 $EXPECT 行当前的 max_input_tokens 分布（旧值条件是 $COND）"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name in ($TARGETS) group by 1;"
echo "== 门1b：将被这条件命中的行数（应等于你期望改的条数）"
q "select count(*) from \"LiteLLM_ProxyModelTable\"
   where model_name in ($TARGETS) and $COND;"

echo "== 门2：记下不该动的对照基线"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name like '$BASELINE_LIKE' group by 1 order by 2 desc;"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name='$BASELINE_NAME' group by 1;"

if [ "${1:-}" != "--apply" ]; then echo "dry-run，未写入。"; exit 0; fi

echo "== 写入（$MODE，带旧值条件 $COND）"
q "update \"LiteLLM_ProxyModelTable\"
   set model_info = jsonb_set(model_info::jsonb,'{max_input_tokens}',to_jsonb($NEW::bigint), true),
       updated_at = now()
   where model_name in ($TARGETS)
     and $COND;"

echo "== 回读：目标行"
q "select model_name, coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name in ($TARGETS) group by 1,2 order by 1;"
echo "== 回读：其余键没被动（抽一行看完整 model_info）"
if [ -n "$SAMPLE_NAME" ]; then SAMPLE="'$SAMPLE_NAME'"; else SAMPLE="$TARGETS"; fi
q "select model_info from \"LiteLLM_ProxyModelTable\"
   where model_name in ($SAMPLE) limit 2;"
echo "== 回读：不该动的对照组"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name like '$BASELINE_LIKE' group by 1 order by 2 desc;"
q "select coalesce(model_info->>'max_input_tokens','<none>') mit, count(*)
   from \"LiteLLM_ProxyModelTable\" where model_name='$BASELINE_NAME' group by 1;"
