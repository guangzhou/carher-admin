#!/usr/bin/env bash
# 198 prod: 一次性重置 **全部** cursor-* key 的两套额度账本 —— 又快又准。
#
# 背景(2026-08-22 起 198 两套独立记账,拦截提示几乎一样但清法不同):
#   1) 总额度       —— DB LiteLLM_VerificationToken.spend vs max_budget
#   2) 系列日额度 ④ —— redis budget_notice:fam:{gpt53|other}:{token}:{北京日}
# 只清一套 → key 仍被另一套拦(实证 cursor-zhuge:总 $18/$500 却被 other 桶 $544/$500 拦)。
# 本脚本两套一起清。
#
# 为什么另起脚本(不用 litellm-key-budget-reset.py cursor- --like):
#   那个逐把 key 打 585 次 /key/update HTTP + 585 次 redis --scan,串行几分钟。
#   本脚本改**集合运算**:
#     · DB   —— 单条 UPDATE ... WHERE key_alias LIKE 'cursor-%' AND spend>0(1 次,不是 585 次)
#     · redis—— 全表 scan 一遍 budget_notice:fam:* + 按 cursor token 集过滤 + 批量 DEL
#   585 把从几分钟降到几秒。
#
# 正确性依据(与 audit 脚本同款直写 DB 模式):
#   · litellm 落账是 `UPDATE SET spend = spend + delta`(相对增量),把 DB spend 直接置 0
#     不会被后续 flush 冲掉,只是从 0 重新累加。
#   · 各 proxy 副本 in-memory auth 缓存 ≤60s 后按 DB 刷新;redis DEL 立即生效。
#   · fam 桶键第 4 段(冒号分隔)= token,与 DB token 列全等 → 可集合过滤,
#     绝不误删非 cursor(claude-code-* 等)的桶。
#
# 用法(本地跑,SSH 进 198):
#   ./scripts/litellm-198-cursor-reset-all.sh            # dry-run(只报范围,不动)
#   ./scripts/litellm-198-cursor-reset-all.sh --apply    # 执行
#   ./scripts/litellm-198-cursor-reset-all.sh --pattern 'cursor-%' --apply
set -uo pipefail

APPLY=0
PAT='cursor-%'
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)   APPLY=1; shift ;;
    --pattern) PAT="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"

ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$HOST" \
    "APPLY='$APPLY' PAT='$PAT' bash -s" <<'REMOTE' 2>&1 | grep -v -E '^\[sudo\]|sitecustomize|^EOF$'
set -uo pipefail
NS=litellm-product
DB_POD=litellm-db-0
REDIS_POD=litellm-redis-0

DB_URL=$(sudo kubectl -n "$NS" exec deploy/litellm-proxy -- env </dev/null 2>/dev/null | grep -m1 '^DATABASE_URL=')
PG_PW=$(echo "$DB_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
[ -z "$PG_PW" ] && { echo "ALERT: could not derive PGPASSWORD from DATABASE_URL"; exit 2; }

# 不能用 kubectl exec -i(抢外层 bash -s 的 heredoc stdin → 静默中断);psql/redis-cli 显式 </dev/null。
PSQL() { sudo kubectl -n "$NS" exec "$DB_POD" -- env PGPASSWORD="$PG_PW" \
           psql -U litellm -d litellm -h localhost -tAqc "SET statement_timeout='90s'; $1" </dev/null; }
RCLI() { sudo kubectl -n "$NS" exec "$REDIS_POD" -- redis-cli "$@" </dev/null; }

echo "=== cursor 全量额度重置 (pattern='$PAT') ==="
TOTAL=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\" WHERE key_alias LIKE '$PAT';")
NZ=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\" WHERE key_alias LIKE '$PAT' AND spend>0;")
echo "匹配 key 总数: $TOTAL   其中 DB spend>0: $NZ"

# cursor token 集(全 hash),用于 redis 桶过滤
TOKN=/tmp/_cursor_tokens.txt
PSQL "SELECT token FROM \"LiteLLM_VerificationToken\" WHERE key_alias LIKE '$PAT';" | sort -u > "$TOKN"
NTOK=$(wc -l < "$TOKN" | tr -d ' ')

# 全表 fam 桶 scan(所有用户/日期)→ 过滤出属于 cursor token 的桶
FAMALL=/tmp/_fam_all.txt
FAMCUR=/tmp/_fam_cursor.txt
RCLI --scan --pattern 'budget_notice:fam:*' | sort -u > "$FAMALL"
# fam 键 = budget_notice:fam:{fkey}:{token}:{date} → 冒号第 4 段是 token
awk -F: 'NR==FNR{t[$1]=1;next} ($4 in t){print}' "$TOKN" "$FAMALL" > "$FAMCUR"
NFAMALL=$(wc -l < "$FAMALL" | tr -d ' ')
NFAMCUR=$(wc -l < "$FAMCUR" | tr -d ' ')
echo "cursor token: $NTOK   fam 桶(全) $NFAMALL → 属 cursor $NFAMCUR"

if [ "$APPLY" != "1" ]; then
  echo "-- 将执行(dry-run) --"
  echo "  DB : UPDATE spend=0 WHERE key_alias LIKE '$PAT' AND spend>0   ($NZ 行)"
  echo "  redis DEL: $NFAMCUR 个 cursor 系列桶"
  echo "VERDICT: DRY-RUN. 加 --apply 执行。"
  rm -f "$TOKN" "$FAMALL" "$FAMCUR"
  exit 0
fi

echo "-- 1) DB spend → 0 --"
# CTE + RETURNING 让更新行数走 SELECT 出来(-q 会吞掉裸 UPDATE 的 'UPDATE n' 状态标签)
UPD=$(PSQL "WITH u AS (UPDATE \"LiteLLM_VerificationToken\" SET spend=0, updated_at=NOW()
            WHERE key_alias LIKE '$PAT' AND spend>0 RETURNING 1) SELECT count(*) FROM u;")
echo "  DB spend 置零: $UPD 行"

echo "-- 2) redis DEL cursor 系列桶(批量) --"
DEL=0
if [ "$NFAMCUR" -gt 0 ]; then
  # 分批 DEL,每批 ≤400 键一次 kubectl exec,减少 exec 次数
  DELOUT=$(xargs -n 400 sudo kubectl -n "$NS" exec "$REDIS_POD" -- redis-cli DEL < "$FAMCUR" 2>/dev/null)
  # 每次 DEL 返回删除计数,累加
  DEL=$(echo "$DELOUT" | awk '{s+=$1} END{print s+0}')
fi
echo "  redis DEL 命中: $DEL/$NFAMCUR"

echo "-- 3) 复核(注意:DB spend ≤60s 各副本刷新;新流量会立刻重新累计) --"
sleep 2
BLK=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\"
            WHERE key_alias LIKE '$PAT' AND max_budget IS NOT NULL AND spend>=max_budget;")
echo "  当前 spend>=max(被挡): $BLK  (期望 0;>0 多半是重置后新流量瞬时到顶)"
rm -f "$TOKN" "$FAMALL" "$FAMCUR"
[ "$BLK" = "0" ] && echo "VERDICT: OK (全部 cursor 额度已清零)" \
                 || echo "VERDICT: WARN (仍有 $BLK 把到顶,复查是否新流量)"
REMOTE
