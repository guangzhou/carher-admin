#!/usr/bin/env bash
# litellm-aliyun-logtable-truncate.sh
#
# 阿里云(ACK / namespace carher)LiteLLM 日志表清空 —— 主动腾容量,**不是**盘满应急。
#
# ★ 与 198 那把锤子(scripts/litellm-198-spendlog-emergency-truncate.sh)的关键区别,
#   别把两边的心智模型互套:
#
#   | 维度        | 198 (litellm-product)          | 阿里云 (carher)                      |
#   |-------------|--------------------------------|--------------------------------------|
#   | 存储        | hostPath /Data 492G,会真满     | CNFS NAS,PVC 配额 600Gi,df 显示 10P |
#   | 触发场景    | disk-pressure→驱逐→502 死锁    | 无盘压,配额水位偏高时主动清         |
#   | db pod      | **常态是已被驱逐**⇒需 offline  | 一直 Running ⇒ 只有 online          |
#   | 连接方式    | sshpass ssh + kubectl exec     | 本机 kubectl 直连(已配 context)     |
#   | 收敛等待    | 要等 kubelet 撤 taint(~5~41min)| **没有这一步**,清完即完             |
#
#   ⚠️ 因此本脚本**不实现 offline 单用户模式**。NAS 配额满不会驱逐 db pod(它只让写失败),
#      db 不 Running 时是别的病(见 skill litellm-ops),不该用这把锤子。
#
# ★ 为什么默认两张表都清:2026-09-21 实测阿里云 `LiteLLM_SpendLogToolIndex` 117GB/1.53亿行,
#   而 `LiteLLM_SpendLogs` 只有 39GB/30.4万行 —— **ToolIndex 是 SpendLogs 的 3 倍大、500 倍行数**。
#   根因:CronJob `litellm-log-retention` 覆盖 SpendLogs/ErrorLogs/AuditLog 三张表,
#   **唯独漏了 ToolIndex**(它有 start_time 列,技术上完全可以被覆盖,就是没写进去)。
#   所以只清 SpendLogs = 只清掉 25% 的问题。retention 补齐前,这里会反复涨回来。
#
# TRUNCATE 是元数据操作,瞬间释放空间且不需要临时空间(不像 DELETE+VACUUM)。
# 代价:历史计费/审计正文**不可恢复**。Daily* 汇总表不受影响(按天预聚合,报表仍可用)。
#
# 用法:
#   bash scripts/litellm-aliyun-logtable-truncate.sh                 # 交互确认
#   DRY_RUN=1 bash scripts/litellm-aliyun-logtable-truncate.sh       # 只体检不动手
#   FORCE=1 bash scripts/litellm-aliyun-logtable-truncate.sh         # 跳过确认
#   TABLES='LiteLLM_SpendLogToolIndex' FORCE=1 bash ...              # 只清一张
set -euo pipefail

NS="${NS:-carher}"
DBPOD="${DBPOD:-litellm-db-0}"
DB="${DB:-litellm}"
DBUSER="${DBUSER:-litellm}"
# 默认两张都清 —— 见文件头"为什么默认两张表都清"。顺序:先大后小。
TABLES="${TABLES:-LiteLLM_SpendLogToolIndex LiteLLM_SpendLogs}"
DRY_RUN="${DRY_RUN:-0}"

# ⚠️ 纪律(继承自 198 脚本的两次实战教训,这里同样适用):
#   1. 不许加 2>/dev/null —— 吞掉真错误会让"没清掉"被读成"清掉了"。
#   2. 管道尾不许用 `grep -v` —— psql -Atq 执行 TRUNCATE 输出 0 行,grep -v 拿空输入返回 1,
#      叠 pipefail + set -e 会让循环在第一张表之后静默中止,屏幕上一个错字都没有。
psql_q() {
  kubectl -n "$NS" exec "$DBPOD" -- psql -U "$DBUSER" -d "$DB" -v ON_ERROR_STOP=1 -Atc "$1"
}
psql_t() {
  kubectl -n "$NS" exec "$DBPOD" -- psql -U "$DBUSER" -d "$DB" -v ON_ERROR_STOP=1 -c "$1"
}

hr() { printf -- '---- %s ----\n' "$*"; }

# ---------------------------------------------------------------- 体检
hr "目标 ns=${NS} pod=${DBPOD} db=${DB} 表=[${TABLES}]"

PHASE="$(kubectl -n "$NS" get pod "$DBPOD" -o jsonpath='{.status.phase}' 2>&1 || true)"
echo "db pod phase = ${PHASE}"
if [ "$PHASE" != "Running" ]; then
  echo "!! ${DBPOD} 不是 Running(='${PHASE}')。"
  echo "!! 这把锤子只做 online。NAS 配额满**不会**驱逐 db pod,所以这是别的病 —— 先查 skill litellm-ops,别在这里硬来。"
  exit 1
fi

hr "PVC 配额 / NAS 水位"
kubectl -n "$NS" get pvc "litellm-db-data-${DBPOD}" \
  -o custom-columns='PVC:.metadata.name,QUOTA:.spec.resources.requests.storage,PHASE:.status.phase' 2>&1 || true
# NAS 上 df 恒显示 10P,量不出真实占用 —— 判水位只认 pg_database_size 和表体积。
echo "(注:CNFS NAS 的 df 恒显示 10P,量不出配额水位 —— 判据用下面的 pg_database_size)"

hr "库总大小"
psql_q "SELECT pg_size_pretty(pg_database_size('${DB}'));"

hr "日志表体积 TOP8"
psql_t "SELECT relname,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
               c.reltuples::bigint AS est_rows
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
        ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 8;"

hr "retention CronJob 覆盖面(证明 ToolIndex 是否仍是缺口)"
COVERED="$(kubectl -n "$NS" get cronjob litellm-log-retention \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].command}' 2>/dev/null \
  | grep -o 'delete_by_ctid_batches [A-Za-z_]*' | awk '{print $2}' | sort -u | tr '\n' ' ' || true)"
echo "CronJob litellm-log-retention 按时间列清理的表: ${COVERED:-<读不到>}"
case "$COVERED" in
  *SpendLogToolIndex*) echo "  ✓ ToolIndex 已被 retention 覆盖" ;;
  "")                  echo "  ? 读不到 CronJob(可能不存在或 RBAC 不足),手动确认" ;;
  *)                   echo "  ✗ ToolIndex **未**被覆盖 —— 清完会重新无限增长,这是根因不是本脚本能修的" ;;
esac

if [ "$DRY_RUN" = "1" ]; then echo "== DRY_RUN,到此为止 =="; exit 0; fi

if [ "${FORCE:-0}" != "1" ]; then
  echo
  echo "!! 即将 TRUNCATE: ${TABLES}"
  echo "!! 历史计费/审计正文全部不可恢复(Daily* 汇总表不受影响)。"
  printf '确认清空请输入大写 TRUNCATE: '
  read -r ans
  [ "$ans" = "TRUNCATE" ] || { echo "已取消"; exit 1; }
fi

# ---------------------------------------------------------------- 执行
hr "TRUNCATE"
# 逐表打回执:psql -Atq 对 TRUNCATE 不输出任何东西,不自己打一行就看不出循环走到第几张表。
for t in $TABLES; do
  psql_q "TRUNCATE TABLE \"${t}\";" >/dev/null
  echo "  [ok] TRUNCATE ${t}"
done

# ---------------------------------------------------------------- 验收
hr "清空后库总大小"
psql_q "SELECT pg_size_pretty(pg_database_size('${DB}'));"

hr "清空后行数(非 0 是正常的 —— TRUNCATE 之后这几秒的新流量,恰好证明写入链路没断)"
for t in $TABLES; do
  printf '  %-34s rows=%s\n' "$t" "$(psql_q "SELECT count(*) FROM \"${t}\";")"
done

hr "proxy 存活(必须 RESTARTS 不变 —— 清表不该碰到 proxy)"
kubectl -n "$NS" get pod -l app=litellm-proxy --no-headers 2>&1 || true

cat <<'EOF'

== 善后 ==
1. TRUNCATE 只止血,不改增长几何。真正的修法是把 ToolIndex 补进 retention:
     k8s/litellm-log-retention-cronjob.yaml 里加一行
       delete_by_ctid_batches LiteLLM_SpendLogToolIndex start_time
     ⚠️ 注意该函数的 ORDER BY 写死了 request_id —— ToolIndex 有 request_id 列,
        所以能直接复用;但它的主键是 (request_id, tool_name) 复合键,
        排序列不唯一不影响正确性(ctid 批删),只影响批次边界稳定性。
2. 阿里云**没有** disk-pressure 那套收敛等待(NAS 配额满只让写失败,不驱逐 pod),
   所以这里不需要等 taint 消失。清完即完。
3. 判容量水位只认 pg_database_size / 表体积,**别看 df**(NAS 恒显示 10P)。
EOF
