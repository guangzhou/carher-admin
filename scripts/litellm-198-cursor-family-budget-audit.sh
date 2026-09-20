#!/usr/bin/env bash
# 198 prod: 审计 / 对齐 cursor-* key 的「main 系列日限额 == 每天总限额」不变量。
#
# 约定(2026-08-23 起):每个 cursor-* key 的
#   metadata.budget_family_overrides.other  ==  该 key 自己的 max_budget
# 即 "其他/main 系列(gpt-5.6/5.5/claude/deepseek/glm 共桶)"的每日系列上限
# 与这把 key 的每天总额度相等。onboarding 新造 key 常漏设 other(退回 env 默认
# $500)→ 需定期回归审计。gpt53 系列($200)不在本不变量内,本脚本不动。
#
# 用法:
#   ./scripts/litellm-198-cursor-family-budget-audit.sh              # 只审计(dry-run)
#   ./scripts/litellm-198-cursor-family-budget-audit.sh --apply      # 对齐可对齐者
#   ./scripts/litellm-198-cursor-family-budget-audit.sh --exclude canary   # 额外排除 alias 含子串
#
# 只对齐"干净"的 key:max_budget 非空 且 budget_duration 非空(有每日重置)。
# 异常另列不动,交人判断:
#   - budget_duration 为空 = 终身额度,没有"每天总额"语义(如 cursor-guran-v2sb)
#   - alias 命中 --exclude(如 canary 灰度测试残留 key)
#
# 本地跑,SSH 进 198(cltx@10.68.13.198),DB 操作走 litellm-db-0。
set -uo pipefail

APPLY=0
EXCLUDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)   APPLY=1; shift ;;
    --exclude) EXCLUDE="${2:-}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"

ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$HOST" \
    "APPLY='$APPLY' EXCLUDE='$EXCLUDE' bash -s" <<'REMOTE' 2>&1 | grep -v -E '^\[sudo\]|sitecustomize|^EOF$'
set -uo pipefail
NS=litellm-product
DB_POD=litellm-db-0

DB_URL=$(sudo kubectl -n "$NS" exec deploy/litellm-proxy -- env </dev/null 2>/dev/null | grep -m1 '^DATABASE_URL=')
PG_PW=$(echo "$DB_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
[ -z "$PG_PW" ] && { echo "ALERT: could not derive PGPASSWORD from DATABASE_URL"; exit 2; }

# 注意:不能用 kubectl exec -i(会抢外层 bash -s 的 heredoc stdin 导致脚本在首行后静默中断);
# psql -c 不需要 stdin,显式 </dev/null。
PSQL() { sudo kubectl -n "$NS" exec "$DB_POD" -- env PGPASSWORD="$PG_PW" \
           psql -U litellm -d litellm -h localhost -tAqc "$1" </dev/null; }

# 排除子句(SQL 片段):budget_duration 非空 + alias 不含 EXCLUDE 子串
EXCL_SQL=""
if [ -n "$EXCLUDE" ]; then
  EXCL_SQL="AND key_alias NOT LIKE '%${EXCLUDE}%'"
fi

# 不变量判据:other 覆盖值(缺省视作 env 默认 500) <> max_budget
MISALIGN_FILTER="key_alias LIKE 'cursor-%'
  AND max_budget IS NOT NULL
  AND coalesce((metadata->'budget_family_overrides'->>'other')::float, 500) <> max_budget"

echo "=== cursor family-budget invariant audit (other == max_budget) ==="

TOTAL=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\" WHERE key_alias LIKE 'cursor-%';")
MIS=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\" WHERE $MISALIGN_FILTER;")
echo "cursor-* total: $TOTAL   misaligned: $MIS"

if [ "${MIS:-0}" = "0" ]; then
  echo "VERDICT: OK (all cursor-* aligned)"
  exit 0
fi

echo "-- misaligned (alias | max_budget | budget_duration | other_override) --"
PSQL "SELECT key_alias || ' | ' || max_budget || ' | ' || coalesce(budget_duration,'(none)')
        || ' | ' || coalesce(metadata->'budget_family_overrides'->>'other','(unset->500)')
      FROM \"LiteLLM_VerificationToken\"
      WHERE $MISALIGN_FILTER ORDER BY key_alias;"

# 可对齐 = 干净者:有每日重置周期 + 未被排除
ALIGNABLE_FILTER="$MISALIGN_FILTER AND budget_duration IS NOT NULL $EXCL_SQL"
NALIGN=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\" WHERE $ALIGNABLE_FILTER;")
NANOM=$(( MIS - NALIGN ))
echo "alignable(clean): $NALIGN   anomalies(skip): $NANOM"

if [ "$NANOM" -gt 0 ]; then
  echo "-- anomalies (budget_duration NULL 或命中 --exclude,不自动改) --"
  PSQL "SELECT key_alias || ' | ' || max_budget || ' | ' || coalesce(budget_duration,'(none-lifetime)')
        FROM \"LiteLLM_VerificationToken\"
        WHERE $MISALIGN_FILTER AND NOT (budget_duration IS NOT NULL ${EXCL_SQL:+$EXCL_SQL})
        ORDER BY key_alias;"
fi

if [ "$APPLY" != "1" ]; then
  echo "VERDICT: DRY-RUN ($NALIGN alignable). rerun with --apply to fix."
  exit 0
fi

echo "-- applying: set other = max_budget for $NALIGN alignable keys --"
PSQL "UPDATE \"LiteLLM_VerificationToken\"
        SET metadata = jsonb_set(coalesce(metadata,'{}'::jsonb),
                                 '{budget_family_overrides,other}', to_jsonb(max_budget), true),
            updated_at = NOW()
      WHERE $ALIGNABLE_FILTER
      RETURNING key_alias || ' -> other=' || max_budget;"

REMAIN=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\" WHERE $ALIGNABLE_FILTER;")
echo "post-apply alignable-remaining: $REMAIN (expect 0; auth 缓存 ~60s 后 router 生效)"
[ "${REMAIN:-1}" = "0" ] && echo "VERDICT: OK (aligned; $NANOM anomalies left for human)" \
                         || echo "VERDICT: ALERT (still $REMAIN misaligned after UPDATE)"
REMOTE
