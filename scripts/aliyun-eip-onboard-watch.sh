#!/usr/bin/env bash
# aliyun-eip-onboard-watch.sh — 轮询 aliyun-eip-onboard-via-jms.sh 起的 Job,
# 成功后把 auth.json 从工作区 PVC 取回本地 /tmp/auth-acct-<N>.json。
#
# kubectl 同样走 `jms ssh --tty <worker>`(本地隧道不可用时的通道,见 via-jms 脚本头注释)。
# 取回手法: 起一个挂同 PVC 的 busybox pod 把文件 base64 打到 stdout,本地在 marker 之间截取
# —— 因为 KoKo SFTP 对 token 会话只读,`kubectl cp` 也依赖不可用的非 TTY 通道。
#
# 用法: bash scripts/aliyun-eip-onboard-watch.sh <N> [oauth|toggle] [max_wait_sec]
set -uo pipefail

N="${1:?usage: $0 <N> [oauth|toggle] [max_wait_sec]}"
ACTION="${2:-oauth}"
MAXWAIT="${3:-900}"

ASSET="${KVIA_ASSET:-k8s-work-226}"
NS=carher
PVC=chatgpt-onboard-work
JOB="cgpt-onboard-${ACTION}-${N}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$REPO/scripts/jms"
OUT="/tmp/auth-acct-$N.json"

kx(){ local to="$1"; shift; "$JMS" ssh --tty --timeout "$to" "$ASSET" "$1" 2>&1; }

echo "[watch] acct-$N job=$JOB (最多等 ${MAXWAIT}s)"
DEADLINE=$(( $(date +%s) + MAXWAIT ))
STATE=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  OUT_S=$(kx 60 "kubectl -n $NS get job $JOB -o jsonpath='S={.status.succeeded} F={.status.failed} A={.status.active}' 2>/dev/null; echo; kubectl -n $NS logs job/$JOB --tail=3 2>/dev/null | tr -d '\r' | tail -3")
  LINE=$(echo "$OUT_S" | grep -oE 'S=[0-9]* F=[0-9]* A=[0-9]*' | tail -1)
  TAIL=$(echo "$OUT_S" | grep -vE 'S=|kubectl|^\s*$|@iZ' | tail -2 | sed 's/^/    | /')
  echo "  [$(date +%H:%M:%S)] $LINE"
  [ -n "$TAIL" ] && echo "$TAIL"
  case "$LINE" in
    *"S=1"*) STATE=succeeded; break ;;
    *"F=1"*) STATE=failed; break ;;
  esac
  sleep 25
done

if [ "$STATE" = "failed" ]; then
  echo "[watch] acct-$N job FAILED — 末 40 行日志:"
  kx 90 "kubectl -n $NS logs job/$JOB --tail=40 2>/dev/null" | tr -d '\r' | tail -40
  exit 2
fi
if [ "$STATE" != "succeeded" ]; then
  echo "[watch] acct-$N 超时未结束(${MAXWAIT}s)。当前日志末 20 行:"
  kx 90 "kubectl -n $NS logs job/$JOB --tail=20 2>/dev/null" | tr -d '\r' | tail -20
  exit 3
fi

[ "$ACTION" = "toggle" ] && { echo "[watch] toggle 完成(无 auth.json 产物)"; exit 0; }

# ── 取回 auth.json: 直接从 job 日志里的 B64BEGIN/B64END 段截取 ────────────────
# job 命令末尾自己 base64 打了一份, 所以不用再起挂 PVC 的 reader pod。
echo "[watch] job succeeded → 从 job 日志取回 auth-acct-$N.json"
RAW=$(kx 200 "kubectl -n $NS logs job/$JOB --tail=200 2>/dev/null")
echo "$RAW" | sed -n '/B64BEGIN/,/B64END/p' | grep -vE 'B64BEGIN|B64END|@iZ|^\s*$' \
  | tr -d ' \r\n' | base64 -d > "$OUT" 2>/dev/null

# 形状校验(空壳/截断在这里就必须暴露,别等到写进 198 PVC)
python3 - "$OUT" <<'PY'
import json, sys, time
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception as e:
    print(f"  ❌ auth.json 不是合法 JSON: {e}"); sys.exit(1)
miss = [k for k in ('access_token','refresh_token','id_token','expires_at','account_id') if not d.get(k)]
if miss:
    print(f"  ❌ auth.json 缺字段: {miss}"); sys.exit(1)
al = len(d['access_token'])
if al < 1000:
    print(f"  ❌ access_token 太短({al}) — 疑似空壳"); sys.exit(1)
exp = d.get('expires_at', 0)
exp_s = exp/1000 if exp > 1e11 else exp
print(f"  ✅ access_len={al} account_id={d['account_id']} expires_in={int((exp_s-time.time())/60)}min")
PY
RC=$?
[ $RC -eq 0 ] && echo "[watch] acct-$N auth.json → $OUT" || { echo "[watch] acct-$N 取回失败"; exit 4; }
