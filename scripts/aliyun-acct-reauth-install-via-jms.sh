#!/usr/bin/env bash
# aliyun-acct-reauth-install-via-jms.sh <N> [auth.json]
#   把**本地已取回的** auth.json 灌回池内 chatgpt-acct-N pod,并安全处理 paused deploy。
#
# 为什么不用 aliyun-acct-install-auth-on226.sh:
#   那个脚本在 226 上现抓 `job/cgpt-onboard-oauth-N` 日志抽 b64 —— 但 job 日志走 PTY 常
#   "取不到日志"(2026-08-24 实证 126 FAIL)。auth.json 其实已由 aliyun-eip-onboard-watch.sh
#   落到本地 /tmp/auth-acct-N.json(sha 已校验),直接推这份即可,不必再碰 job 日志。
#
# 号池不变量: 所有 chatgpt-acct-N deploy 常态 **paused=true**(防 EIP 超卖节点误滚)。
#   `rollout restart` 会被拒("can't restart paused deployment")。本脚本:
#     resume → restart → wait rollout → **re-pause**(恢复不变量)→ 回查新 pod token。
#   为什么要 restart: 密码 reset 后旧 refresh_token 被 OpenAI 作废,litellm 进程内存里
#   还攥着旧 refresh 会逐渐失效 → 必须重启加载新 auth.json 的 refresh_token。
#
# 用法:
#   bash scripts/aliyun-acct-reauth-install-via-jms.sh 126
#   bash scripts/aliyun-acct-reauth-install-via-jms.sh 126 /tmp/auth-acct-126.json
set -uo pipefail
N="${1:?usage: $0 <N> [auth.json]}"
AUTHJ="${2:-/tmp/auth-acct-$N.json}"
ASSET="${KVIA_ASSET:-k8s-work-226}"
NS=carher
JMS="$(cd "$(dirname "$0")/.." && pwd)/scripts/jms"
kx(){ "$JMS" ssh "$ASSET" "$1" 2>&1; }

[ -s "$AUTHJ" ] || { echo "FATAL: $AUTHJ 不存在/空 —— 先 aliyun-eip-onboard-watch.sh $N oauth 取回"; exit 1; }

# 本地先校验 shape(access_token>1000)+ 打印套餐/到期,避免灌进坏 token
python3 - "$AUTHJ" <<'PYEOF' || { echo "FATAL: auth.json 校验不过"; exit 2; }
import sys,json,base64
d=json.load(open(sys.argv[1]))
at=d.get('access_token','') or ''
assert len(at)>1000, f"access_token too short: {len(at)}"
idt=d.get('id_token','')
try:
    p=idt.split('.')[1]; p+='='*(-len(p)%4)
    c=json.loads(base64.urlsafe_b64decode(p)); a=c.get('https://api.openai.com/auth',{})
    print(f"  [auth] email={c.get('email')} plan={a.get('chatgpt_plan_type')} sub_until={a.get('chatgpt_subscription_active_until')}")
except Exception as e:
    print("  [auth] id_token 解析跳过:",e)
print(f"  [auth] access_len={len(at)} rt_len={len(d.get('refresh_token') or '')} acct={d.get('account_id')}")
PYEOF

# 推到 226(sha 核对,不信 PTY)
LOCAL_SHA=$(shasum -a256 "$AUTHJ" | cut -d' ' -f1)
B64=$(base64 < "$AUTHJ" | tr -d '\n')
REMOTE_SHA=$(kx "printf '%s' '$B64' > /tmp/auth-acct-$N.b64; base64 -d /tmp/auth-acct-$N.b64 > /tmp/auth-acct-$N.json; sha256sum /tmp/auth-acct-$N.json | cut -d' ' -f1" | tr -d '[:space:]' | tail -c 64)
[ "$REMOTE_SHA" = "$LOCAL_SHA" ] || { echo "FATAL: 推送 sha mismatch local=$LOCAL_SHA remote=$REMOTE_SHA"; exit 3; }
echo "  [push] sha ok=$LOCAL_SHA"

# 灌进 pod → 回查落盘 → resume/restart/wait/re-pause → 回查新 pod token
kx "set -e
P=\$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}')
[ -n \"\$P\" ] || { echo 'FATAL: acct-$N 无 pod'; exit 5; }
echo \"  [cp] pod=\$P\"
kubectl -n $NS cp /tmp/auth-acct-$N.json $NS/\$P:/chatgpt-auth/auth.json -c litellm
GOT=\$(kubectl -n $NS exec \$P -c litellm -- python3 -c 'import json;print(len(json.load(open(\"/chatgpt-auth/auth.json\")).get(\"access_token\",\"\")))' 2>/dev/null | tr -dc 0-9)
echo \"  [cp] pod_access_len=\$GOT\"
[ \"\${GOT:-0}\" -gt 1000 ] || { echo 'FATAL: 落盘校验不过'; exit 7; }
WASPAUSED=\$(kubectl -n $NS get deploy chatgpt-acct-$N -o jsonpath='{.spec.paused}')
echo \"  [roll] was_paused=\$WASPAUSED\"
[ \"\$WASPAUSED\" = true ] && kubectl -n $NS rollout resume deploy/chatgpt-acct-$N
kubectl -n $NS rollout restart deploy/chatgpt-acct-$N
kubectl -n $NS rollout status deploy/chatgpt-acct-$N --timeout=240s 2>&1 | tail -1
[ \"\$WASPAUSED\" = true ] && kubectl -n $NS rollout pause deploy/chatgpt-acct-$N
echo \"  [roll] re_paused=\$(kubectl -n $NS get deploy chatgpt-acct-$N -o jsonpath='{.spec.paused}')\"
NP=\$(kubectl -n $NS get pod -l app=chatgpt-acct-$N --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
NL=\$(kubectl -n $NS exec \$NP -c litellm -- python3 -c 'import json;print(len(json.load(open(\"/chatgpt-auth/auth.json\")).get(\"access_token\",\"\")))' 2>/dev/null | tr -dc 0-9)
echo \"  [verify] new_pod=\$NP access_len=\$NL\"
[ \"\${NL:-0}\" -gt 1000 ] && echo \"INSTALL_OK acct-$N\" || { echo 'FATAL: 新 pod token 校验不过'; exit 8; }"