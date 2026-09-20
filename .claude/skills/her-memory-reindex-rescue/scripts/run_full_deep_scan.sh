#!/bin/bash
# 全集群跑 deep_scan_one.sh，分析风险并输出 Markdown 报告。
#
# 优化策略（两阶段，比单纯 deep 快 3x）:
#   Phase 1: 用 scan_one.sh 跑全集群（1.7s/pod，~6 min）拿基础信息
#   Phase 2: 只对"嫌疑实例"（TMP_COUNT≥1 OR RESTARTS≥1 OR MAIN_AGE_H≥24）
#            跑 deep_scan_one.sh（5-10s/pod，~3 min），对其他直接补 0
#   Phase 3: classify_risk.py 给 Markdown 报告
#
# 用法:
#   run_full_deep_scan.sh              # 默认两阶段
#   run_full_deep_scan.sh --full       # 全部 deep（慢但完整）
#   run_full_deep_scan.sh --quick      # 只跑 Phase 1（不深扫，最快但缺 sqlite 内部数据）

set -u
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p /tmp/her-rescue
PODS=/tmp/her-rescue/deep-pods.txt
BASE_TSV=/tmp/her-rescue/scan.tsv
DEEP_TSV=/tmp/her-rescue/deep-scan.tsv
PROGRESS=/tmp/her-rescue/deep-scan-progress.txt
REPORT=/tmp/her-rescue/deep-scan-report.md

MODE="${1:-default}"

kubectl get pod -n carher 2>/dev/null \
  | awk '/^carher-[0-9]+-/{print $1}' > "$PODS"

total=$(wc -l < "$PODS")
echo "===== Phase 0: 准备 ====="
echo "pods: $total"
echo "started_at $(date -u +%FT%TZ)" > "$PROGRESS"

if [ "$MODE" = "--quick" ]; then
  echo
  echo "===== Phase 1: 基础扫描（quick mode）====="
  bash "$SKILL_DIR/scripts/run_scan.sh"
  echo
  echo "Phase 1 完成。请手工筛选可疑实例后跑 deep_scan_one.sh"
  exit 0
fi

if [ "$MODE" = "--full" ]; then
  echo
  echo "===== Phase 1+2 合并：所有实例做 deep scan（约 25-30 min）====="
  > "$DEEP_TSV"
  i=0
  START=$(date +%s)
  while IFS= read -r pod; do
    [ -z "$pod" ] && continue
    i=$((i+1))
    "$SKILL_DIR/scripts/deep_scan_one.sh" "$pod" >> "$DEEP_TSV" 2>/dev/null
    if [ $((i % 10)) -eq 0 ]; then
      NOW=$(date +%s)
      eta=$(awk -v t="$total" -v ix="$i" -v s="$START" -v n="$NOW" \
        'BEGIN{ if (ix==0) {print "?"} else {printf "%dm", ((n-s)/ix)*(t-ix)/60} }')
      echo "$(date -u +%FT%TZ) $i/$total eta=$eta" | tee -a "$PROGRESS"
    fi
  done < "$PODS"
  echo "$(date -u +%FT%TZ) DONE $i/$total" >> "$PROGRESS"

else
  # default: 两阶段
  echo
  echo "===== Phase 1: 基础扫描（${total} 个 pod, ~6 min）====="
  if [ ! -s "$BASE_TSV" ] || [ "$(find "$BASE_TSV" -mmin -30 -print 2>/dev/null)" = "" ]; then
    bash "$SKILL_DIR/scripts/run_scan.sh" > /tmp/her-rescue/run_scan.out 2>&1
    echo "  base scan 完成: $BASE_TSV"
  else
    echo "  复用 30 min 内的 $BASE_TSV"
  fi

  echo
  echo "===== Phase 2: 筛嫌疑实例（her-68 模式必要条件）====="
  # 嫌疑判定收紧: TMP_COUNT>=1（孤儿，her-68 模式核心）OR RESTARTS>=3（反复 OOM）
  # MAIN_AGE_H>=24 不再当 candidate —— 因为 providerKey 没变时 main mtime 自然
  # 多天不更新（增量插入只 update 行 mtime 不 update 文件 mtime），实测 99/201 实例
  # 都是这种"健康但 main 旧"的，深扫只会浪费 15+ min。
  CANDIDATES=/tmp/her-rescue/deep-candidates.txt
  awk -F'\t' '
    $11=="OK" && ($7+0 >= 1 || $3+0 >= 3) {print $1}
  ' "$BASE_TSV" > "$CANDIDATES"
  ncand=$(wc -l < "$CANDIDATES")
  echo "  candidates: ${ncand} / ${total}  (TMP_COUNT>=1 OR RESTARTS>=3)"

  echo
  echo "===== Phase 2: deep scan candidates in parallel (-P 4) ====="
  > "$DEEP_TSV"
  # 对其他 OK 实例：直接从 base TSV 投影到 deep TSV 列序，跳过 sqlite 内部探查
  awk -F'\t' '
    BEGIN{OFS="\t"}
    $11=="OK" {
      tmp_count = ($7+0)
      restarts  = ($3+0)
      if (tmp_count >= 1 || restarts >= 3) next  # 嫌疑，等下深扫
      # POD HID RESTARTS LAST_OOM POD_AGE_M WS_READY MAIN_MB MAIN_AGE_H TMP_COUNT TMP_ACTIVE TMP_MB TMP_OLDEST_AGE_H CHUNKS EMB_MB EC_ROWS EC_MB TMP_CHUNKS TMP_HAS_META PROV PK MEM_MB STATUS
      print $1, $2, $3, $4, "-", "UNKNOWN", $5, $6, $7, $8, $9, "-", "-", "-", "-", "-", "-", "-", "-", "-", $10, "OK"
    }
    $11!="OK" {
      print $1, $2, $3, $4, "-", "UNKNOWN", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", $11
    }
  ' "$BASE_TSV" >> "$DEEP_TSV"

  # 并发 4 个 deep_scan 同时跑
  if [ "$ncand" -gt 0 ]; then
    START=$(date +%s)
    cat "$CANDIDATES" | xargs -n1 -P 4 -I{} \
      "$SKILL_DIR/scripts/deep_scan_one.sh" "{}" >> "$DEEP_TSV" 2>/dev/null
    NOW=$(date +%s)
    elapsed=$((NOW - START))
    echo "  并发 deep 完成: $ncand 实例, ${elapsed}s"
  fi
fi

echo
echo "===== Phase 3: 风险分类 + Markdown 报告 ====="
python3 "$SKILL_DIR/scripts/classify_risk.py" "$DEEP_TSV" "$REPORT"

echo
echo "===== top dangerous (CRITICAL/HIGH) ====="
awk '/^## (CRITICAL|HIGH|MED)/,/^## /{print}' "$REPORT" | head -100
echo
echo "完整报告: $REPORT"
echo "原始数据: $DEEP_TSV"
