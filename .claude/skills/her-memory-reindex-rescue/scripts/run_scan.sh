#!/bin/bash
# 全集群扫描 carher pod，输出 TSV 到 /tmp/her-rescue/scan.tsv
# 串行约 6 分钟（200 个 pod）

set -u
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p /tmp/her-rescue
PODS=/tmp/her-rescue/pods.txt
RESULT=/tmp/her-rescue/scan.tsv
PROGRESS=/tmp/her-rescue/scan-progress.txt

kubectl get pod -n carher 2>/dev/null \
  | awk '/^carher-[0-9]+-/{print $1}' > "$PODS"

> "$RESULT"
echo "started_at $(date -u +%FT%TZ)" > "$PROGRESS"
total=$(wc -l < "$PODS")
i=0
while IFS= read -r pod; do
  [ -z "$pod" ] && continue
  i=$((i+1))
  "$SKILL_DIR/scripts/scan_one.sh" "$pod" >> "$RESULT" 2>/dev/null
  if [ $((i % 20)) -eq 0 ]; then
    echo "$(date -u +%FT%TZ) $i/$total" >> "$PROGRESS"
  fi
done < "$PODS"
echo "$(date -u +%FT%TZ) DONE $i/$total" >> "$PROGRESS"
echo "scanned $i pods, results: $RESULT"

# 简易分类输出
echo
echo "===== status 分布 ====="
awk -F'\t' '{print $11}' "$RESULT" | sort | uniq -c

echo
echo "===== 当前有 tmp 的 pod (需要分类) ====="
echo "POD                                  RESTARTS LAST_OOM              MAIN_MB AGE_H TMP TMP_ACT TMP_MB MEM_MB"
awk -F'\t' '$11=="OK" && $7+0>0 {
  printf "%-36s %-8s %-21s %-7s %-5s %-3s %-7s %-6s %s\n",
    $1, $3, $4, $5, $6, $7, $8, $9, $10
}' "$RESULT"

echo
echo "===== 高内存 (>2500 MB) ====="
echo "POD                                  RESTARTS LAST_OOM              MAIN_MB AGE_H TMP TMP_ACT TMP_MB MEM_MB"
awk -F'\t' '$11=="OK" && $10+0>2500 {
  printf "%-36s %-8s %-21s %-7s %-5s %-3s %-7s %-6s %s\n",
    $1, $3, $4, $5, $6, $7, $8, $9, $10
}' "$RESULT" | sort -t$'\t' -k9 -nr
