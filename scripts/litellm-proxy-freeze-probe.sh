#!/usr/bin/env bash
# litellm-proxy-freeze-probe.sh —— 判 litellm-proxy 的 pod 是"活着"还是"事件循环冻死了"。
#
# 背景：worker 事件循环一旦死锁，pod 仍然 1/1 Running、进程表形状完全正常、
# 内存不涨、CPU 不烧，只有探针连不上。kubelet 要 10×30s 才 SIGKILL ⇒
# 中间有整整 5 分钟，`kubectl get pod` 看起来一切正常。这个脚本就是那 5 分钟里的尺子。
#
# 两把尺子（实测 20 次采样无反例，互相印证）：
#   A. worker 主线程 wchan   冻死=每个 worker 都 futex_do_wait ｜ 健康=至少一个 ep_poll
#   B. /health/liveliness    冻死=恒撞超时上限          ｜ 健康=15~80ms
#
# ⛔ 三个已证伪的坏尺子，别用：
#   1. **`/proc/1` 零判别力。** PID 1 是 supervisor 不干活，健康和冻死都是
#      `State: S / Threads: 1 / wchan: futex_do_wait`。必须挑 `spawn_main` 那几个 worker。
#   2. **`/proc/<pid>/stat` 的 utime+stime 是开机以来的累计值。** 我拿它读出"冻结 worker
#      在烧 31% CPU"，据此提了"busy-blocked"假设——真按 60 秒窗口量，是 **0%**。
#      要窗口值必须自己采两次做差。
#   3. **进程表条数**（11 vs 5）。健康和冻死形状一模一样，5 那次是开机瞬间的过渡态。
#
# ⚠️ 容器里**没有 curl**，HTTP 只能用容器自带的 /app/.venv/bin/python。
# ⚠️ `kubectl exec` 一律补 `</dev/null`，否则会吃掉外层 heredoc 自己的 stdin。
#
# 用法：
#   ./litellm-proxy-freeze-probe.sh                       # 单次
#   ./litellm-proxy-freeze-probe.sh --watch 20 --interval 30   # 连采 20 轮
#   ssh cltx@10.68.13.198 'bash ~/litellm-proxy-freeze-probe.sh'   # 198 上跑（自动加 sudo）
#
# 只读脚本：不改任何对象，不删任何东西。
set -u

NS=${NS:-litellm-product}
SEL=${SEL:-app=litellm-proxy}
CTR=${CTR:-litellm}
WATCH=1
INTERVAL=30
while [ $# -gt 0 ]; do
  case "$1" in
    --ns) NS=$2; shift 2 ;;
    --selector) SEL=$2; shift 2 ;;
    --container) CTR=$2; shift 2 ;;
    --watch) WATCH=$2; shift 2 ;;
    --interval) INTERVAL=$2; shift 2 ;;
    *) echo "未知参数 $1" >&2; exit 2 ;;
  esac
done

K="kubectl"
command -v sudo >/dev/null 2>&1 && [ -e /run/k3s ] && K="sudo kubectl"

probe_one() {
  P=$1
  # 尺子 A：只取 multiprocessing spawn_main 起来的 worker，跳过 PID 1
  W=$($K -n "$NS" exec "$P" -c "$CTR" -- sh -c '
      for p in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do
        [ "$p" = "1" ] && continue
        grep -q spawn_main /proc/$p/cmdline 2>/dev/null || continue
        printf "%s " "$(cat /proc/$p/wchan 2>/dev/null || echo ?)"
      done' </dev/null 2>/dev/null)
  # 尺子 B：容器里没有 curl，用它自带的 python
  L=$($K -n "$NS" exec "$P" -c "$CTR" -- /app/.venv/bin/python -c '
import time, urllib.request
t = time.time()
try:
    c = urllib.request.urlopen("http://127.0.0.1:4000/health/liveliness", timeout=9).status
except Exception as e:
    c = type(e).__name__
print(c, round(time.time() - t, 3))' </dev/null 2>/dev/null | tail -1)

  VERDICT="?"
  case " $W " in
    *" ep_poll "*) VERDICT="活" ;;
    *futex_do_wait*) VERDICT="🔴冻死" ;;
  esac
  case "$L" in 200\ *) : ;; "") VERDICT="🔴探针打不通" ;; esac
  printf '  %-42s wchan=[%s] liveliness=[%s] → %s\n' "$P" "${W% }" "${L:-无响应}" "$VERDICT"
}

i=1
while [ "$i" -le "$WATCH" ]; do
  echo "=== 第 $i/$WATCH 轮  $(date '+%F %T')  ns=$NS ==="
  PODS=$($K -n "$NS" get pod -l "$SEL" -o name --field-selector=status.phase=Running 2>/dev/null | sed 's|pod/||')
  if [ -z "$PODS" ]; then echo "  ❌ 一个 Running pod 都没取到 —— 先确认 selector/ns 对不对，别把空读成绿"; exit 2; fi
  echo "  基数 pods=$(echo "$PODS" | wc -l | tr -d ' ')   # 基数必须 >0，0 是 FAIL 不是"都健康""
  for P in $PODS; do probe_one "$P"; done

  echo "  --- 近期 Unhealthy 事件（注意看是不是老 ReplicaSet 的残留）---"
  $K -n "$NS" get events --sort-by=.lastTimestamp 2>/dev/null \
    | grep -i unhealthy | tail -3 | sed 's/^/    /'
  echo "  --- 重启计数（>0 = 已经被 SIGKILL 过）---"
  $K -n "$NS" get pod -l "$SEL" --no-headers 2>/dev/null \
    | awk '{print "    " $1, "restarts=" $4, "age=" $5}'

  i=$((i + 1))
  [ "$i" -le "$WATCH" ] && sleep "$INTERVAL"
done
