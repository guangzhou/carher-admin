#!/usr/bin/env bash
# aliyun-onboard-workspace.sh — 读 onboard 工作区 PVC(chatgpt-onboard-work)里的日志/截图/auth.json
#
# 为什么需要它: aliyun-eip-onboard.sh 起的是 **Job**, 跑完 pod 进终态,
# `kubectl logs job/...` 过几分钟就 `timed out waiting for the condition` 取不回来了;
# 而真正的证据(完整日志 log-<action>-<N>.txt、截图 ss-<N>/*.png、auth-acct-<N>.json)
# 全在 RWX NAS PVC 上, 得另起一个 pod 挂载才能读。
#
# ⚠️ 这个读取 pod **不要钉 EIP 节点**: 它只读 NAS, 不发任何出网请求, 没有出口隔离要求。
#    钉上去反而会跟 onboard Job 抢那两个常年贴着驱逐阈值的节点, 自己先被 Evicted
#    (2026-09-09 实证: 钉 .122 的读取 pod 被准入拒绝, 拿到一个**空文件**;
#     当时还用 `2>/dev/null` 把报错吞了, 空文件被当成"读到了" —— 双重坑)。
#
# 用法:
#   ./scripts/aliyun-onboard-workspace.sh ls 211            # 列 ss-211/ 下的截图
#   ./scripts/aliyun-onboard-workspace.sh ls 211 bill       # 列 bill-ss-211/(续订 job 的截图)
#   ./scripts/aliyun-onboard-workspace.sh log 211 renew     # cat log-renew-211.txt
#   ./scripts/aliyun-onboard-workspace.sh cat /work/auth-acct-210.json
#   ./scripts/aliyun-onboard-workspace.sh png 211 bill-03-final   # 截图 → /tmp/ss-211-<name>.png
#   ./scripts/aliyun-onboard-workspace.sh png 211 p15a3-otp-filled-2 bill   # 取续订 job 的截图
#   ./scripts/aliyun-onboard-workspace.sh auth 210          # auth.json → /tmp/auth-acct-210.json + 自检
#   ./scripts/aliyun-onboard-workspace.sh rm                # 删掉读取 pod
#
# ⚠️ 截图目录有两套前缀, 别记混(2026-09-12 踩过: 续订 job 的图用 `ls <N>` 一律 No such file):
#     onboard/oauth job → /work/ss-<N>/        (第3参省略)
#     billing/renew job → /work/bill-ss-<N>/   (第3参传 `bill`)
#   续订卡在 `advanced=False` 时**真因只在 bill 那套图里**(见 skill chatgpt-sub-renew-eip)。
set -uo pipefail

NS=carher
POD=cgpt-workspace-reader
PVC=chatgpt-onboard-work
IMG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:zerokey-capture-aliyun-20260707-otp3
TTL="${WS_TTL:-7200}"

ACT="${1:?usage: $0 <ls|log|cat|png|auth|rm> [args]}"

drop_pod(){
  # confirmed-zero-downtime-override: 本脚本自建的一次性只读 pod, 不属于任何 Deployment
  kubectl -n $NS delete pod $POD --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1
}

if [ "$ACT" = "rm" ]; then drop_pod; echo "reader pod 已删"; exit 0; fi

ensure_pod(){
  local ph
  ph=$(kubectl -n $NS get pod $POD -o jsonpath='{.status.phase}' 2>/dev/null)
  # 终态 pod exec 不进去(报 "cannot exec into a container in a completed pod"), 直接重建。
  # sleep 到期也是这个下场, 所以 TTL 给足。
  case "$ph" in
    Running) return 0 ;;
    "") ;;
    *) drop_pod ;;
  esac
  kubectl -n $NS run $POD --restart=Never --image="$IMG" --overrides="{
    \"spec\":{\"containers\":[{\"name\":\"r\",\"image\":\"$IMG\",
      \"command\":[\"sleep\",\"$TTL\"],
      \"resources\":{\"requests\":{\"cpu\":\"50m\",\"memory\":\"64Mi\",\"ephemeral-storage\":\"32Mi\"},
                     \"limits\":{\"cpu\":\"200m\",\"memory\":\"256Mi\",\"ephemeral-storage\":\"64Mi\"}},
      \"volumeMounts\":[{\"name\":\"w\",\"mountPath\":\"/work\"}]}],
    \"volumes\":[{\"name\":\"w\",\"persistentVolumeClaim\":{\"claimName\":\"$PVC\"}}],
    \"imagePullSecrets\":[{\"name\":\"acr-vpc-secret\"},{\"name\":\"acr-secret\"}]}}" >/dev/null || return 1
  for _ in $(seq 1 60); do
    [ "$(kubectl -n $NS get pod $POD -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] && return 0
    sleep 4
  done
  echo "FATAL: reader pod 起不来: $(kubectl -n $NS get pod $POD -o wide 2>&1 | tail -1)" >&2
  return 1
}
ensure_pod || exit 1

case "$ACT" in
  ls)
    N="${2:?需要 acct 编号}"; PFX=""; [ "${3:-}" = "bill" ] && PFX="bill-"
    kubectl -n $NS exec $POD -- ls -la "/work/${PFX}ss-$N/" ;;
  log)  N="${2:?需要 acct 编号}"; A="${3:-renew}"; kubectl -n $NS exec $POD -- cat "/work/log-$A-$N.txt" ;;
  cat)  F="${2:?需要路径}";        kubectl -n $NS exec $POD -- cat "$F" ;;
  png)
    N="${2:?需要 acct 编号}"; NAME="${3:?需要截图名(不带 .png)}"
    PFX=""; [ "${4:-}" = "bill" ] && PFX="bill-"
    OUT="/tmp/ss-$N-$NAME.png"
    # base64 中转: kubectl exec 的 stdout 会做行尾/编码处理, 二进制直传会**静默损坏**
    kubectl -n $NS exec $POD -- base64 -w0 "/work/${PFX}ss-$N/$NAME.png" > "$OUT.b64" || exit 1
    python3 -c "
import base64, pathlib, sys
raw = base64.b64decode(pathlib.Path('$OUT.b64').read_text())
if len(raw) < 100: sys.exit('FATAL: 截图为空/损坏 (%d bytes)' % len(raw))
pathlib.Path('$OUT').write_bytes(raw); print('$OUT', len(raw), 'bytes')" || exit 1
    rm -f "$OUT.b64"
    ;;
  auth)
    N="${2:?需要 acct 编号}"; OUT="/tmp/auth-acct-$N.json"
    kubectl -n $NS exec $POD -- cat "/work/auth-acct-$N.json" > "$OUT" || exit 1
    # ⚠ 必须自检: 读取 pod 被驱逐时 cat 会**成功返回一个空文件**, 不自检就会拿空壳去
    #   kubectl cp, 最后在 pod 里表现为 access_len=0 的"半接入"。
    python3 -c "
import json, sys
try: d = json.load(open('$OUT'))
except Exception as e: sys.exit('FATAL: $OUT 不是合法 JSON (%s) — 多半是读取 pod 被驱逐拿到空文件' % e)
n = len(d.get('access_token',''))
if n < 1000: sys.exit('FATAL: access_token 只有 %d 字符, 是空壳' % n)
print('$OUT ok access_len=%d acct=%s rt=%s' % (n, d.get('account_id'), bool(d.get('refresh_token'))))"
    ;;
  *) echo "FATAL: 未知动作 '$ACT' (ls|log|cat|png|auth|rm)"; exit 1 ;;
esac
