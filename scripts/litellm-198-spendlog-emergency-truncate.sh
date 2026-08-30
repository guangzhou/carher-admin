#!/usr/bin/env bash
# litellm-198-spendlog-emergency-truncate.sh
#
# 198 (aiyjy-litellm k3s / namespace litellm-product) LiteLLM_SpendLogs 应急清空。
#
# 何时用:SpendLogs 撑爆 /Data(disk-pressure)导致 db/redis 被驱逐、proxy 502 死锁。
#   TRUNCATE 是元数据操作,瞬间释放全部空间(不像 DELETE+VACUUM 需要临时空间/长锁),
#   是盘已满时唯一能立刻回收的手段。计费/审计的历史正文会全部丢失 —— 仅应急止血用。
#   日常按 10 天保留请依赖 CronJob spendlog-retention(k8s/litellm-198-spendlog-retention-cronjob.yaml)。
#
# 根因与防死锁几何见 k8s/litellm-198-infra-priority.yaml。
#
# 依赖:sshpass;198 直连凭据(cltx)。用法:
#   bash scripts/litellm-198-spendlog-emergency-truncate.sh            # 交互确认
#   FORCE=1 bash scripts/litellm-198-spendlog-emergency-truncate.sh    # 跳过确认(慎用)
set -euo pipefail

SSH_HOST="${SSH_HOST:-10.68.13.198}"
SSH_USER="${SSH_USER:-cltx}"
SSH_PASS="${SSH_PASS:-Hn8#mKLp3QxZ}"
NS="${NS:-litellm-product}"
TABLE="${TABLE:-LiteLLM_SpendLogs}"

ssh198() {
  sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "${SSH_USER}@${SSH_HOST}" \
    "echo '$SSH_PASS' | sudo -S sh -c \"$1\"" 2>&1 | grep -v '^\[sudo'
}

# 在 db pod 内跑一段 SQL(SQL 经 base64 传输,彻底避开多层 SSH/sudo 引号与 stdin 冲突)
psql198() {
  local sql_b64; sql_b64="$(printf '%s' "$1" | base64 | tr -d '\n')"
  ssh198 "kubectl -n ${NS} exec ${DBPOD} -- sh -c 'echo ${sql_b64} | base64 -d | psql -U litellm -d litellm -Atq'"
}

echo "== 目标: ${SSH_HOST} ns=${NS} 表=${TABLE} =="
echo "== 磁盘 / 表 现状 =="
ssh198 "df -h /Data | tail -1"
DBPOD="$(ssh198 "kubectl -n ${NS} get pod -l app=litellm-db -o jsonpath='{.items[0].metadata.name}'")"
echo "db pod: ${DBPOD}"
psql198 "SELECT pg_size_pretty(pg_total_relation_size('\"${TABLE}\"')) AS total, count(*) AS rows FROM \"${TABLE}\";" || true

if [ "${FORCE:-0}" != "1" ]; then
  echo
  echo "!! 即将 TRUNCATE ${TABLE} —— 全部历史 spend 日志正文将不可恢复 !!"
  printf '确认清空请输入大写 TRUNCATE: '
  read -r ans
  [ "$ans" = "TRUNCATE" ] || { echo "已取消"; exit 1; }
fi

echo "== 执行 TRUNCATE =="
psql198 "TRUNCATE TABLE \"${TABLE}\";"

echo "== 清空后磁盘 =="
ssh198 "df -h /Data | tail -1"
echo "== 完成。若节点仍带 disk-pressure taint(kubelet 有 ~5min 缓冲),可手动摘: =="
echo "   kubectl taint node aiyjy-litellm node.kubernetes.io/disk-pressure-"
