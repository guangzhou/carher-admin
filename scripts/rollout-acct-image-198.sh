#!/usr/bin/env bash
# rollout-acct-image-198.sh — 把 198 Pro 池全部在跑的 chatgpt-acct 号滚动更新到指定镜像 tag。
#
# 沉淀自 2026-08-19 全池升 vanilla-v1.90.2.cache-session-fix-v2 的实操。
# 关联记忆：project_198pro_chatgpt_cache_session_fix_2026_08_17
#          feedback_198pro_acct_2gi_default_ooms_as_sole_upstream
#
# 为什么要有这个脚本（不要裸 for + set image）：
#   1) 灰度先行——先滚 2 个号观察 90s（ready + rc=0 + mem 远低于 limit），
#      拿到"新镜像在正常流量下稳定"的正面证据，再铺全池。单一样本（如 226）
#      长期 OOM，无法证明镜像本身稳。
#   2) 逐个滚 + rollout status 等 ready 再下一个——deploy 的 strategy 是
#      **Recreate**（不是 RollingUpdate！先杀旧再起新，单号有几十秒 503 空窗）。
#      逐个滚可避免同节点多个号同时空窗；WA 会把流量切到别的号。
#   3) 幂等——已在目标 tag 的号自动 SKIP。
#
# 前置事实（2026-08-19 核实，变了要重查）：
#   - 池跨两节点：aiyjy-litellm(198 主) + aiyjy-litellm-standby(225)。
#   - 镜像走 127.0.0.1:5000 本地 registry；**225 的 registries.yaml mirror 指向
#     http://10.68.13.198:5000**，两节点共享同一镜像源 → 落 225 的号也能拉到，
#     无需在 225 单独 push。滚之前仍建议 verify 一次 tag 在 198 registry。
#   - acct-82 跑 acct-stable（模板/别用途号），默认不在滚动范围。
#
# ⚠ 只滚镜像，不动 resources/env。高流量粘死的号（如 226）在 4Gi 下会 OOM 循环，
#   那是流量分布问题不是镜像问题（本次全池滚动后其余号 rc=0 已反证）——别拿这个
#   脚本去"修" OOM。OOM 处置见 feedback_198pro_acct_2gi_default_ooms_as_sole_upstream。
#
# 用法：
#   ssh cltx@10.68.13.198，然后在 198 上（需要 sudo 读 k3s.yaml）：
#     sudo bash rollout-acct-image-198.sh <IMAGE_TAG> [--canary N,M] [--all] [--dry-run]
#   例：
#     sudo bash rollout-acct-image-198.sh vanilla-v1.90.2.cache-session-fix-v2-20260817-103630
#
# 参数：
#   IMAGE_TAG       必填。只给 tag（脚本自动补 127.0.0.1:5000/litellm-carher: 前缀），
#                   或给完整 image 引用也可。
#   --canary N,M    灰度号（默认自动挑前两个 198 主节点、rc=0 的稳定号）。
#   --all           跳过灰度观察，直接全池滚（不推荐；仅当该镜像已验证过）。
#   --dry-run       只打印计划，不执行。
set -uo pipefail

export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
NS=litellm-product
REG_PREFIX="127.0.0.1:5000/litellm-carher:"
CANARY_WAIT=${CANARY_WAIT:-90}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-150s}

die(){ echo "ERR: $*" >&2; exit 1; }

TAG_ARG="${1:-}"; shift || true
[ -n "$TAG_ARG" ] || die "缺少 IMAGE_TAG。用法：$0 <IMAGE_TAG> [--canary N,M] [--all] [--dry-run]"
case "$TAG_ARG" in
  */*|*:*) IMG="$TAG_ARG" ;;              # 已是完整引用
  *)       IMG="${REG_PREFIX}${TAG_ARG}" ;;
esac
SHORT="${IMG##*:}"

CANARY=""; ALL=0; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --canary) CANARY="$2"; shift 2 ;;
    --all)    ALL=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) die "未知参数 $1" ;;
  esac
done

echo "== 目标镜像：$IMG"

# 0) verify tag 在 198 registry（落 225 的号也从这拉）
if ! curl -s http://127.0.0.1:5000/v2/litellm-carher/tags/list \
     | python3 -c "import sys,json;t='$SHORT';sys.exit(0 if t in json.load(sys.stdin).get('tags',[]) else 3)"; then
  die "registry 127.0.0.1:5000 里没有 tag=$SHORT。先 docker push，或检查 tag 拼写。"
fi
echo "   registry tag 确认存在 ✓"

# 1) 枚举在跑的 chatgpt-acct 号（replicas>0），排除已在目标 tag 的
mapfile -t ALL_DN < <(kubectl -n $NS get deploy -o name | grep -oE 'chatgpt-acct-[0-9]+' | sort -u -t- -k3 -n)
TODO=(); SKIP=(); CANDIDATE_CANARY=()
for dn in "${ALL_DN[@]}"; do
  rep=$(kubectl -n $NS get deploy "$dn" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  [ "$rep" = "0" ] && continue
  cur=$(kubectl -n $NS get deploy "$dn" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
  case "$cur" in "$IMG") SKIP+=("$dn"); continue ;; esac
  # acct-82 acct-stable 等非本系不滚（只滚 image 名里含 litellm-carher 的普通 acct）
  TODO+=("$dn")
  # 挑灰度候选：198 主节点 + rc=0
  node=$(kubectl -n $NS get pods -l app="$dn" -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)
  rc=$(kubectl -n $NS get pods -l app="$dn" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null)
  if [ "$node" = "aiyjy-litellm" ] && [ "${rc:-9}" = "0" ]; then CANDIDATE_CANARY+=("$dn"); fi
done

echo "== 待滚 ${#TODO[@]} 个：${TODO[*]}"
echo "== 已在目标 tag（SKIP）${#SKIP[@]} 个：${SKIP[*]}"
[ ${#TODO[@]} -eq 0 ] && { echo "全部已在目标 tag，无需操作。"; exit 0; }

# 2) 确定灰度集
if [ "$ALL" = "1" ]; then
  CANARY_SET=()
elif [ -n "$CANARY" ]; then
  CANARY_SET=(); IFS=',' read -ra CS <<< "$CANARY"; for n in "${CS[@]}"; do CANARY_SET+=("chatgpt-acct-$n"); done
else
  CANARY_SET=("${CANDIDATE_CANARY[@]:0:2}")   # 默认前两个稳定号
fi
echo "== 灰度集：${CANARY_SET[*]:-<无，--all 直接全滚>}"

if [ "$DRY" = "1" ]; then echo "[dry-run] 到此为止，未执行。"; exit 0; fi

roll_one(){
  local dn="$1"
  kubectl -n $NS set image deploy/"$dn" litellm="$IMG" >/dev/null 2>&1
  kubectl -n $NS rollout status deploy/"$dn" --timeout="$ROLLOUT_TIMEOUT" 2>&1 | tail -1
}
report_one(){
  local dn="$1"; local pod ready rc mem
  pod=$(kubectl -n $NS get pods -l app="$dn" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  ready=$(kubectl -n $NS get pod "$pod" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)
  rc=$(kubectl -n $NS get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
  mem=$(kubectl -n $NS top pod "$pod" --no-headers 2>/dev/null | awk '{print $3}')
  echo "   $dn ready=$ready rc=$rc mem=${mem:-?}"
}

# 3) 灰度
CANARY_INSET=""
for dn in "${CANARY_SET[@]}"; do
  # 灰度号必须在待滚集合里
  for t in "${TODO[@]}"; do [ "$t" = "$dn" ] && CANARY_INSET+="$dn "; done
done
if [ -n "$CANARY_INSET" ]; then
  echo "== [灰度] 滚 $CANARY_INSET"
  for dn in $CANARY_INSET; do echo "-- $dn"; roll_one "$dn"; done
  echo "== [灰度] 观察 ${CANARY_WAIT}s ..."
  sleep "$CANARY_WAIT"
  bad=0
  for dn in $CANARY_INSET; do
    report_one "$dn"
    r=$(kubectl -n $NS get pods -l app="$dn" -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null)
    c=$(kubectl -n $NS get pods -l app="$dn" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null)
    [ "$r" = "true" ] || bad=1
    [ "${c:-0}" -gt 0 ] && bad=1
  done
  [ "$bad" = "1" ] && die "灰度不健康（not ready 或已重启）。停止全池滚动，先查根因。"
  echo "== [灰度] 通过 ✓（ready + rc=0）"
fi

# 4) 全池滚其余
echo "== [全池] 滚其余号"
for dn in "${TODO[@]}"; do
  skip=0; for c in $CANARY_INSET; do [ "$c" = "$dn" ] && skip=1; done
  [ "$skip" = "1" ] && continue
  echo "-- $dn"; roll_one "$dn"
done

# 5) 最终核对
echo
echo "== 最终核对 =="
total=0; ontarget=0; notready=0
for dn in "${ALL_DN[@]}"; do
  rep=$(kubectl -n $NS get deploy "$dn" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  [ "$rep" = "0" ] && continue
  total=$((total+1))
  pod=$(kubectl -n $NS get pods -l app="$dn" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  tag=$(kubectl -n $NS get deploy "$dn" -o jsonpath='{.spec.template.spec.containers[0].image}')
  ready=$(kubectl -n $NS get pod "$pod" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)
  rc=$(kubectl -n $NS get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
  mark="OK"; case "${tag##*:}" in "$SHORT") ontarget=$((ontarget+1));; *) mark="NOT-TARGET";; esac
  [ "$ready" != "true" ] && { mark="$mark NOTREADY"; notready=$((notready+1)); }
  echo "$dn ready=$ready rc=$rc ${tag##*:}  $mark"
done
echo "SUMMARY: total_running=$total on_target=$ontarget not_ready=$notready"
