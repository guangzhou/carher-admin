#!/usr/bin/env bash
# 逐个（不是一把梭）重启 acct deployment，让新挂载的 CM 生效，同时护住宿主机。
#
# 用法: NS=carher EXPECT=4 ./pace_restart_accts.sh            # 全部活跃 acct
#       NS=litellm-product SKIP=chatgpt-acct-135 ./pace_restart_accts.sh
#       NS=litellm-product FROM=chatgpt-acct-119 PACE=45 ./pace_restart_accts.sh   # 断点续跑
#       NS=litellm-product ONLY=chatgpt-acct-86,chatgpt-acct-87 ./pace_restart_accts.sh  # 只补这几个
#       NS=litellm-product VERIFY_ONLY=1 ./pace_restart_accts.sh   # 只验不重启（收尾清点）
#
# 纪律（skill litellm-context-window-govern §5）：
#   - **一次只滚一个**，rollout status 等到 Ready 才走下一个
#   - 每次动手前查 DiskPressure / MemoryPressure / load，超阈值就**停下**（不是跳过，是停）
#     被 load 拦下来是正常结局，不是失败：记下停在哪个，等 load 回落用 FROM= 续跑
#   - 每个滚完在容器内验 /app/config.yaml 里目标值的条数 == EXPECT（证明新 CM 真挂上了）
#   - 相邻两次之间留 PACE 秒空隙，避免镜像/网络/etcd 抖动叠加
#     198 上 PACE=8 会把 load 推到 8~10（containerd/k3s 的 pod churn，CPU 仍 ~70% idle）
#     ⇒ 想一口气跑完 60+ 个就用 PACE=45
set -uo pipefail

NS="${NS:?需要 NS}"
EXPECT="${EXPECT:-4}"
NEEDLE="${NEEDLE:-max_input_tokens: 922000}"
PACE="${PACE:-8}"
LOAD_MAX="${LOAD_MAX:-6.0}"
SKIP="${SKIP:-}"
FROM="${FROM:-}"
ONLY="${ONLY:-}"
VERIFY_ONLY="${VERIFY_ONLY:-}"
K="${KUBECTL:-kubectl}"

health_gate() {
  local bad
  bad=$($K get nodes -o json | python3 -c "
import sys,json
d=json.load(sys.stdin); bad=[]
for n in d['items']:
    cs={c['type']:c['status'] for c in n['status']['conditions']}
    if cs.get('DiskPressure')=='True' or cs.get('MemoryPressure')=='True' or cs.get('Ready')!='True':
        bad.append(n['metadata']['name'])
print(','.join(bad))
")
  if [[ -n "$bad" ]]; then echo "ABORT: 节点异常 -> $bad"; return 1; fi
  local l1
  l1=$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0)
  if python3 -c "import sys; sys.exit(0 if float('$l1')>float('$LOAD_MAX') else 1)"; then
    echo "ABORT: 本机 load $l1 > $LOAD_MAX"; return 1
  fi
  return 0
}

mapfile -t DEPLOYS < <($K -n "$NS" get deploy --no-headers \
  -o custom-columns=N:.metadata.name,R:.spec.replicas \
  | awk '$2>=1 {print $1}' | grep -E '^chatgpt-acct-[0-9]+$' | sort -V)

echo "NS=$NS 待滚 ${#DEPLOYS[@]} 个：${DEPLOYS[*]}"
echo "跳过：${SKIP:-<无>}   判据：容器内 '$NEEDLE' 出现 $EXPECT 次   间隔 ${PACE}s"
[[ -n "$FROM" ]] && echo "断点续跑：从 $FROM 开始"
[[ -n "$ONLY" ]] && echo "只处理：$ONLY"
[[ -n "$VERIFY_ONLY" ]] && echo "VERIFY_ONLY：只验不重启"
ok=0; fail=0; started=0
for D in "${DEPLOYS[@]}"; do
  [[ ",$SKIP," == *",$D,"* ]] && { echo "[$D] SKIP"; continue; }
  if [[ -n "$ONLY" && ",$ONLY," != *",$D,"* ]]; then continue; fi
  if [[ -n "$FROM" && $started -eq 0 ]]; then
    [[ "$D" == "$FROM" ]] && started=1 || continue
  fi
  if [[ -z "$VERIFY_ONLY" ]]; then
    health_gate || { echo "在 $D 之前停止（load 拦下来是正常结局，等回落用 FROM=$D 续跑），已完成 ok=$ok fail=$fail"; exit 2; }

    $K -n "$NS" rollout restart deploy/"$D" >/dev/null
    if ! $K -n "$NS" rollout status deploy/"$D" --timeout=5m >/dev/null 2>&1; then
      echo "[$D] ROLLOUT-TIMEOUT（pod 长期 0/1 的会这样；稍后用 VERIFY_ONLY=1 补验）"
      fail=$((fail+1)); sleep "$PACE"; continue
    fi
  fi
  P=$($K -n "$NS" get po --no-headers -o custom-columns=N:.metadata.name,S:.status.phase \
      | awk -v d="$D" '$1 ~ "^"d"-" && $2=="Running" {print $1}' | tail -1)
  if [[ -z "$P" ]]; then echo "[$D] NO-RUNNING-POD"; fail=$((fail+1)); sleep "$PACE"; continue
  fi
  n=$($K -n "$NS" exec "$P" -- grep -c "$NEEDLE" /app/config.yaml 2>/dev/null | tr -d '\r')
  if [[ "$n" == "$EXPECT" ]]; then echo "[$D] OK  pod=$P  needle=$n"; ok=$((ok+1))
  else echo "[$D] VERIFY-FAIL pod=$P needle=$n (期望 $EXPECT)"; fail=$((fail+1)); fi
  [[ -z "$VERIFY_ONLY" ]] && sleep "$PACE"
done
echo "---- done ok=$ok fail=$fail total=${#DEPLOYS[@]}"
