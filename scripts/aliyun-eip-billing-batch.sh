#!/usr/bin/env bash
# aliyun-eip-billing-batch.sh — 串行批量续订 ChatGPT 账号(复用 aliyun-eip-billing-via-jms.sh)。
#
# 为什么串行: 每号一条 mail.com OTP 链(90s×2 settle + 登录 + 确认框轮询), 并行会互相抢
#   OTP 邮箱视图/EIP 节点内存(见 memory feedback_dont_add_retry_to_serial_otp_chain
#   + feedback_xvfb_display99_hostnetwork_collides_same_node)。一次一个, 等 Job 到终态再下一个。
#
# ⚠️ CSV 必须**先读进数组再遍历**, 不能 `while read < CSV`: 循环体里的 jms ssh 会读 stdin,
#   把剩余 CSV 行吞光 → 下一次 read 拿 EOF → 循环在第一个号后就退出(2026-08-20 实证:
#   acct-81 后 batch 直接 done)。所有 ssh/driver 调用也一律 </dev/null 双保险。
#
# 续订用 FORCE_OTP_LOGIN(billing 脚本已硬编码), 故 chatgpt_pw 不参与登录 → 传占位符即可。
#
# 用法:
#   bash scripts/aliyun-eip-billing-batch.sh <CSV>
#     CSV 每行: N,email,mail_pw   (chatgpt_pw 可省, 走 OTP 登录)
#   例: bash scripts/aliyun-eip-billing-batch.sh /tmp/renew-creds.csv
#
# 产物: /tmp/billing-batch-summary.txt (每号一行 state/result), 结束打印汇总表。
set -uo pipefail

CSV="${1:?usage: $0 <creds.csv: N,email,mail_pw>}"
[ -f "$CSV" ] || { echo "FATAL: $CSV not found"; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DRIVER="$REPO/scripts/aliyun-eip-billing-via-jms.sh"
JMS="$REPO/scripts/jms"
ASSET="${KVIA_ASSET:-k8s-work-226}"
NS=carher
SUMMARY=/tmp/billing-batch-summary.txt
: > "$SUMMARY"

kx(){ "$JMS" ssh --tty "$ASSET" "$1" </dev/null 2>&1; }  # --tty: 无 tty 时 KoKo exec 通道会半死挂住(实证 acct-91 卡 47min), 强制 PTY 根治

# ── CSV 先全部读进数组(见头部注释: 边读边跑会被 ssh 吞 stdin) ──────────────────
ROWS=()
while IFS= read -r _line || [ -n "$_line" ]; do
  _t="$(echo "$_line" | tr -d '[:space:]')"
  [ -n "$_t" ] || continue
  case "$_t" in \#*) continue;; esac
  ROWS+=("$_line")
done < "$CSV"
echo "[batch] start $(date '+%F %T')  csv=$CSV  accounts=${#ROWS[@]}"

# ── 预建**共享** src CM 一次(所有号同一个 oauth.py; 每号重推是 jms 抖动主来源) ────
SHARED_CM=cgpt-billing-src-shared
echo "[batch] 预建共享 src CM=$SHARED_CM ..."
built=0
for attempt in 1 2 3; do
  # ⚠️ 不能 `driver | grep -q`: grep -q 命中即关管道 → driver 收 SIGPIPE(141) →
  #   pipefail 把整条判成非零 → 成功误读成失败(2026-08-20 实证)。先捕获再 grep。
  _out="$(SRC_CM="$SHARED_CM" SKIP_JOB=1 bash "$DRIVER" renew 0 x@x x x </dev/null 2>&1)"
  if echo "$_out" | grep -qE 'src CM ok'; then
    built=1; echo "[batch] 共享 CM 建成(第 $attempt 次)"; break
  fi
  echo "[batch] 共享 CM 第 $attempt 次失败, 重试..."; sleep 5
done
[ "$built" = 1 ] || { echo "FATAL: 共享 src CM 三次都没建成 — jms 通道可能挂了, 见 memory feedback_jms_relay_dead_use_tty_kubeconfig_on_226"; exit 1; }

for _row in "${ROWS[@]}"; do
  N="$(echo "$_row"     | cut -d, -f1 | tr -d '[:space:]')"
  EMAIL="$(echo "$_row" | cut -d, -f2 | tr -d '[:space:]')"
  MPW="$(echo "$_row"   | cut -d, -f3 | tr -d '[:space:]')"
  GPW="$(echo "$_row"   | cut -d, -f4 | tr -d '[:space:]')"
  [ -n "$N" ] || continue
  [ -n "$GPW" ] || GPW=otp-login-placeholder
  JOB="cgpt-billing-renew-$N"

  echo ""
  echo "════════ acct-$N  $EMAIL ════════"
  # 提交(复用共享 CM, 只建 Secret+Job); jms 偶发抖动 → 重试最多 3 次
  submitted=0
  for attempt in 1 2 3; do
    if SRC_CM="$SHARED_CM" SKIP_SRC_PUSH=1 bash "$DRIVER" renew "$N" "$EMAIL" "$MPW" "$GPW" </dev/null 2>&1 | tail -6; then
      submitted=1; break
    fi
    echo "[batch] acct-$N 提交第 $attempt 次失败, 重试..."; sleep 5
  done
  if [ "$submitted" != 1 ]; then
    echo "acct-$N  SUBMIT_FAIL" | tee -a "$SUMMARY"; continue
  fi

  # 等终态(complete 或 failed), 最长 ~22min。
  # ⚠️ 不能用 `kubectl wait --timeout=1200s`: 它长时间静默无输出, jms ssh 隧道空闲超时会
  #   把远端命令掐断 → kx 提前返回 → 根本没等(2026-08-21 实证: acct-121 <90s 就被判
  #   STILL_RUNNING, batch 背靠背发 job → 122 立刻 OutOfcpu)。必须**节点侧轮询 + 每 15s
  #   打心跳行保活隧道**, 命中 Complete/Failed 才 break。
  kx "for i in \$(seq 1 88); do
        cond=\$(kubectl -n $NS get job $JOB -o jsonpath='{.status.conditions[*].type}' 2>/dev/null);
        case \"\$cond\" in *Complete*|*Failed*) echo \"[wait] terminal cond=\$cond\"; break;; esac;
        echo \"[wait] $JOB tick \$i cond=\${cond:-none}\";
        sleep 15;
      done" >/dev/null

  # 抓日志(jms 偶发返回空, 最多重试 3 次)
  LOG=""
  for attempt in 1 2 3; do
    LOG="$(kx "kubectl -n $NS logs job/$JOB --tail=-1 2>&1")"
    echo "$LOG" | grep -q 'BILLING' && break
    sleep 6
  done
  # pod 状态: 准入被拒(OutOfcpu)或仍 Running 都**不是登录失败**, 单独标记别混进
  #   NO_BILLING_OUTPUT(见 memory feedback_billing_verdict_must_read_dump...)。
  PODINFO="$(kx "kubectl -n $NS get pods -l job-name=$JOB -o jsonpath='{.items[*].status.phase}|{.items[*].status.reason}' 2>/dev/null")"
  STATE="$(echo "$LOG"  | grep -oE '\[BILLING-STATE\].*'  | tail -1)"
  LINE="$(echo "$LOG"   | grep -oE '\[BILLING-LINE\].*'   | tail -1)"
  PLAN="$(echo "$LOG"   | grep -oE '\[BILLING-PLAN\].*'   | tail -1)"
  RESULT="$(echo "$LOG" | grep -oE '\[BILLING-RESULT\].*' | tail -1)"
  VERDICT="$(echo "$LOG" | grep -oE '\[BILLING\] (✅ RENEW ENABLED|⚠[^\n]*|already auto-renewing[^\n]*|✗[^\n]*)' | tail -1)"

  if echo "$LOG" | grep -q 'RENEW ENABLED'; then TAG=RENEWED
  elif echo "$LOG" | grep -q 'already auto-renewing'; then TAG=ALREADY_OK
  elif echo "$PODINFO" | grep -qi 'OutOfcpu'; then TAG=RETRY_NODE_OUTOFCPU   # 节点没排上, 补跑
  elif echo "$PODINFO" | grep -q 'Running'; then TAG=STILL_RUNNING           # 还在跑, 等它/单独拉
  elif [ -n "$RESULT$STATE" ]; then TAG=NEEDS_CHECK
  else TAG=NO_BILLING_OUTPUT; fi

  printf 'acct-%s  %s  | %s | %s | %s | %s\n' \
    "$N" "$TAG" "${PLAN:-?}" "${STATE:-?}" "${RESULT:-?}" "${VERDICT:-?}" | tee -a "$SUMMARY"
done

echo ""
echo "════════════════ 汇总 ════════════════"
cat "$SUMMARY"
echo "[batch] done $(date '+%F %T')  summary=$SUMMARY"
