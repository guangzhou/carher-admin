#!/usr/bin/env bash
# 198 prod: reset every claude-code-* key's DB spend and Redis family buckets.
# The default is a dry-run; pass --apply to modify production.
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  cat <<'USAGE'
198 全量 Claude 额度重置

用法:
  litellm-198-claude-reset-all.sh              # dry-run，只查看范围
  litellm-198-claude-reset-all.sh --apply      # 清 DB spend + Redis 系列桶
  litellm-198-claude-reset-all.sh --pattern 'claude-code-%' --apply

默认只匹配 key_alias LIKE 'claude-code-%'，不会处理 Cursor 或其他 key。
USAGE
  exit 0
fi

APPLY=0
PAT='claude-code-%'
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)   APPLY=1; shift ;;
    --pattern) PAT="${2:?missing SQL LIKE pattern}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# The pattern is interpolated into a remote SQL string; keep it intentionally narrow.
if ! [[ "$PAT" =~ ^[A-Za-z0-9._%-]+$ ]]; then
  echo "--pattern may contain only letters, digits, '.', '_', '-', and SQL LIKE '%'" >&2
  exit 2
fi

HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"
JMS_ASSET="${LITELLM_198_JMS_ASSET:-AIYJY-litellm}"
JMS_BIN="${JMS_BIN:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/jms}"

# Keep one remote payload so a connection-level SSH failure can retry through
# JumpServer without rebuilding or partially replaying the reset logic.
REMOTE_SCRIPT=$(mktemp)
trap 'rm -f "$REMOTE_SCRIPT"' EXIT

cat > "$REMOTE_SCRIPT" <<'REMOTE'
set -euo pipefail
NS=litellm-product
DB_POD=litellm-db-0
REDIS_POD=litellm-redis-0

DB_URL=$(sudo -n kubectl -n "$NS" exec deploy/litellm-proxy -- env </dev/null 2>/dev/null \
  | grep -m1 '^DATABASE_URL=' || true)
PG_PW=$(echo "$DB_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
[ -n "$PG_PW" ] || { echo "FATAL: cannot derive database password" >&2; exit 2; }

PSQL() {
  sudo -n kubectl -n "$NS" exec "$DB_POD" -- env PGPASSWORD="$PG_PW" \
    psql -U litellm -d litellm -h localhost -tAqc "SET statement_timeout='90s'; $1" </dev/null
}
RCLI() {
  sudo -n kubectl -n "$NS" exec "$REDIS_POD" -- redis-cli --raw "$@" </dev/null
}

TMP_DIR=$(mktemp -d)
TOKN="$TMP_DIR/claude_tokens"
FAMALL="$TMP_DIR/fam_all"
FAMKEYS="$TMP_DIR/fam_claude"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "=== Claude 全量额度重置 (pattern='$PAT') ==="
TOTAL=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\" WHERE key_alias LIKE '$PAT';")
NZ=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\" WHERE key_alias LIKE '$PAT' AND spend>0;")
echo "匹配 key 总数: $TOTAL   其中 DB spend>0: $NZ"

# Build the exact token set first, then filter Redis family buckets by token.
PSQL "SELECT token FROM \"LiteLLM_VerificationToken\" WHERE key_alias LIKE '$PAT';" \
  | sort -u > "$TOKN"
NTOK=$(wc -l < "$TOKN" | tr -d ' ')
RCLI --scan --pattern 'budget_notice:fam:*' | sort -u > "$FAMALL"
awk -F: 'NR==FNR{t[$1]=1;next} ($4 in t){print}' "$TOKN" "$FAMALL" > "$FAMKEYS"
NFAMALL=$(wc -l < "$FAMALL" | tr -d ' ')
NFAM=$(wc -l < "$FAMKEYS" | tr -d ' ')
echo "Claude token: $NTOK   fam 桶(全) $NFAMALL → 属 Claude $NFAM"

if [ "$APPLY" != "1" ]; then
  echo "-- 将执行(dry-run) --"
  echo "  DB : UPDATE spend=0 WHERE key_alias LIKE '$PAT' AND spend>0   ($NZ 行)"
  echo "  redis DEL: $NFAM 个 Claude 系列桶"
  echo "VERDICT: DRY-RUN. 加 --apply 执行。"
  exit 0
fi

echo "-- 1) DB spend → 0 --"
UPD=$(PSQL "WITH u AS (UPDATE \"LiteLLM_VerificationToken\" SET spend=0, updated_at=NOW()
             WHERE key_alias LIKE '$PAT' AND spend>0 RETURNING 1) SELECT count(*) FROM u;")
echo "  DB spend 置零: $UPD 行"

echo "-- 2) redis DEL Claude 系列桶(批量) --"
DEL=0
if [ "$NFAM" -gt 0 ]; then
  DELOUT=$(xargs -n 400 sudo -n kubectl -n "$NS" exec "$REDIS_POD" -- redis-cli DEL \
    < "$FAMKEYS" 2>/dev/null)
  DEL=$(echo "$DELOUT" | awk '{s+=$1} END{print s+0}')
fi
echo "  redis DEL 命中: $DEL/$NFAM"

echo "-- 3) 复核(新流量可能重新累计) --"
sleep 2
BLK=$(PSQL "SELECT count(*) FROM \"LiteLLM_VerificationToken\"
             WHERE key_alias LIKE '$PAT' AND max_budget IS NOT NULL AND spend>=max_budget;")
echo "  当前 spend>=max(被挡): $BLK  (期望 0;>0 多半是重置后新流量)"
[ "$BLK" = "0" ] && echo "VERDICT: OK (全部 Claude 额度已清零)" \
                    || echo "VERDICT: WARN (仍有 $BLK 把到顶,复查新流量)"
REMOTE

set +e
echo "connection: direct ssh $HOST"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
  "$HOST" "APPLY='$APPLY' PAT='$PAT' bash -s" < "$REMOTE_SCRIPT"
rc=$?
set -e
if [ "$rc" -ne 255 ]; then
  exit "$rc"
fi

echo "direct ssh unavailable; fallback: jms $JMS_ASSET" >&2
"$JMS_BIN" ssh "$JMS_ASSET" "APPLY='$APPLY' PAT='$PAT' bash -s" < "$REMOTE_SCRIPT"
