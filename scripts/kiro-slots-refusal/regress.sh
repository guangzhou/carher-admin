#!/usr/bin/env bash
# opus-5.5 安全审查自动换模型（补丁 0013）的上线前回归。全在 198 上跑，不碰生产 deploy。
#
#   scripts/kiro-slots-refusal/regress.sh <新tag> [--control <旧tag>] [--fallback claude-opus-5] [--mock] [--litellm]
#
# 做什么
#   1. 从生产 30891 现读凭据，自动挑一个**冷备**号（启用、不在槽、没退役、accessToken 剩得最多），
#      refreshToken 换假值 ⇒ 隔离实例永远不刷新，不会轮转线上 token。
#      🔴 10-27 写死 #74 ⇒ 它后来额度用尽被退役，隔离实例 9 例全 529「没有可用槽位」——是夹具坏了不是代码坏了。
#   2. 起隔离 ns（单槽直连）跑 cases.py：真敏感 prompt × 流式/非流式/thinking//cc 缓冲 + 中性/非 opus 阴性 + 会话粘住。
#   3. --control <旧tag>：同样起一份旧版当阴性对照（旧版应该在敏感用例上 STOP）。
#   4. --mock：用 mock 上游测「备用也空」「号被限换号」「非 opus 不动」三条真上游难以稳定触发的分支。
#   5. --litellm：起一个生产同镜像的一次性 LiteLLM，测 anthropic / chat 流式非流式 / responses 四种线型。
#   退出时删 ns、容器、临时文件（trap）。每段打 OK/STOP；任一段失败 rc=1。
#
# 真 Claude Code 端到端（本机跑，不在本脚本里）：🔴 ~/.claude/settings.json 的 env 会盖掉命令行 env，
#   必须 CLAUDE_CONFIG_DIR=<空目录> 且该目录 settings.json 里写 ANTHROPIC_BASE_URL/API_KEY，否则打到生产网关 403。
#   🔴 `--disallowedTools A B "prompt"` 会把 prompt 吞成工具名 ⇒ prompt 放在 -p 后面第一个。
set -euo pipefail
TAG="${1:?用法: $0 <新tag> [--control <旧tag>] [--fallback <上游模型>] [--mock] [--litellm]}"; shift
CONTROL=""; FB="claude-opus-5"; MOCK=0; LL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --control) CONTROL="$2"; shift 2 ;;
    --fallback) FB="$2"; shift 2 ;;
    --mock) MOCK=1; shift ;;
    --litellm) LL=1; shift ;;
    *) echo "未知参数 $1"; exit 2 ;;
  esac
done
PROD="${KIRO_PROD_HOST:-cltx@10.68.13.198}"
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
ssh "$PROD" "mkdir -m 700 -p ~/kiro-refusal-regress"
scp -q "$HERE/cases.py" "$HERE/mock_upstream.py" "$HERE/litellm_wire.py" "$REPO/k8s/kiro-rs/slots/kiro-rs-slots.yaml" "$PROD:~/kiro-refusal-regress/"

ssh "$PROD" "TAG='$TAG' CONTROL='$CONTROL' FB='$FB' MOCK='$MOCK' LL='$LL' bash -s" <<'EOF'
set -uo pipefail; umask 077
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
W=~/kiro-refusal-regress; KC="sudo -n -E kubectl"; D="sudo -n docker"; RC=0
NSS=()
cleanup() {
  for n in "${NSS[@]}"; do $KC delete ns "$n" --wait=false >/dev/null 2>&1; done
  $D rm -f kiro-refusal-ll >/dev/null 2>&1; pkill -f "mock_upstream.py 18777" 2>/dev/null; rm -rf $W
}
trap cleanup EXIT
P=$($KC -n kiro-rs get pod -l app=kiro-rs-slots -o name | head -1)
$KC -n kiro-rs exec $P -c kiro-rs -- sh -c 'cat /app/config/credentials.json; echo @@@; cat /app/config/kiro_slots.json' > $W/state.txt
CRED=$(python3 - "$W/state.txt" <<'PY'
import json, re, sys, datetime as dt
a, b = open(sys.argv[1]).read().split("@@@"); cs = json.loads(a); s = json.loads(b)
busy = set(s["slotAccounts"].values()); ret = set(s.get("retired", {}))
now = dt.datetime.now(dt.timezone.utc)
def left(e): return (dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", e.replace("Z", "+00:00"))) - now).total_seconds()
# 🔴 FREE 号跑不了 opus（kiro.rs 选号时过滤掉）⇒ 隔离实例「没有可用槽位」529。10-27 #94 就是 KIRO FREE
rows = sorted(((left(c["expiresAt"]), c["id"]) for c in cs if not c.get("disabled") and c["id"] not in busy
               and str(c["id"]) not in ret and c.get("expiresAt")
               and "FREE" not in (c.get("subscriptionTitle") or "").upper()), reverse=True)
if not rows or rows[0][0] < 600: sys.exit("no non-FREE spare with >=10min token")
print(rows[0][1])
PY
) || { echo "🔴 STOP: 没有 accessToken 还剩 ≥10 分钟的冷备号（等下一次刷新后再跑）"; exit 1; }
echo "== 夹具：冷备号 #${CRED}（refreshToken 已换假值）"

# up <tag> <port> <fallback> [mock]：起隔离实例，打印 READY 或返回 1
up() {
  local tag=$1 port=$2 fb=$3 mock=${4:-} ns=kiro-refusal-$2
  NSS+=("$ns")
  $KC delete ns $ns --ignore-not-found --wait=true >/dev/null 2>&1; $KC create ns $ns >/dev/null
  for i in $(seq 1 30); do $KC get ns $ns -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Active && break; sleep 1; done
  python3 - "$W" "$CRED" "$fb" "$mock" <<'PY'
import json, sys
w, cid, fb, mock = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
cfg = {"host": "0.0.0.0", "port": 8990, "apiKey": "t", "adminApiKey": "a", "loadBalancingMode": "slots",
       "slots": [{"name": "s1", "proxyUrl": None, "enabled": True}], "slotRefusalFallbackModel": fb}
if mock:
    creds = [{"id": i, "authMethod": "social", "refreshToken": "f", "accessToken": "f",
              "expiresAt": "2099-01-01T00:00:00Z", "profileArn": f"arn:{i}"} for i in (1, 2)]
    cfg["testUpstreamOverride"] = {"baseUrl": mock, "accountIds": [1, 2]}
else:
    a, _ = open(f"{w}/state.txt").read().split("@@@")
    c = dict([x for x in json.loads(a) if x["id"] == cid][0])
    c.update(refreshToken="fake-no-rotate", id=1, priority=0); creds = [c]
json.dump(cfg, open(f"{w}/b.json", "w")); json.dump(creds, open(f"{w}/c.json", "w"))
PY
  $KC -n $ns create secret generic kiro-rs-slots-bootstrap --from-file=config.json=$W/b.json --from-file=credentials.json=$W/c.json >/dev/null
  rm -f $W/b.json $W/c.json
  sed -e "s/namespace: kiro-rs$/namespace: $ns/g" -e "s/namespace: kiro-rs}/namespace: $ns}/g" \
      -e "s/nodePort: 30891/nodePort: $port/" -e "s/v2026.10.1-carher-slots9/$tag/g" $W/kiro-rs-slots.yaml | $KC apply -f - >/dev/null
  for i in $(seq 1 90); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' -m 2 http://127.0.0.1:$port/readyz)" = 200 ] && { echo "   READY $ns $tag fallback=${fb:-<关>}"; return 0; }
    sleep 2
  done
  echo "🔴 STOP: $ns 起不来"; $KC -n $ns logs deploy/kiro-rs-slots --tail=20; return 1
}

echo "== 1. 新版 $TAG 真上游用例"
up "$TAG" 30893 "$FB" && python3 $W/cases.py 30893 || RC=1
reasons=$(curl -s -H 'x-api-key: a' http://127.0.0.1:30893/api/admin/slots/empty-events | python3 -c 'import json,sys,collections;print(dict(collections.Counter(i["reason"] for i in json.load(sys.stdin)["items"])))')
echo "   空回复明细原因: $reasons"

if [ -n "$CONTROL" ]; then
  echo "== 2. 阴性对照 ${CONTROL}（期望敏感用例 STOP、中性/非 opus OK）"
  up "$CONTROL" 30894 "$FB" && { python3 $W/cases.py 30894 && { echo "🔴 STOP: 旧版也全过 ⇒ 用例没量到改动"; RC=1; } || echo "   OK 旧版如期失败"; }
fi

if [ "$MOCK" = 1 ]; then
  echo "== 3. mock 上游三条分支"
  HOSTIP=$(hostname -I | awk '{print $1}')
  # mock 每收一发打一行 `<path> model=<modelId>`；换 MODE 要重启 mock
  mock_run() { pkill -f "mock_upstream.py 18777" 2>/dev/null; sleep 1; (MODE=$1 python3 $W/mock_upstream.py 18777 > $W/mock.log 2>&1 &); sleep 1; }
  ask() { curl -s -m 90 http://127.0.0.1:30896/v1/messages -H 'x-api-key: t' -H 'content-type: application/json' \
          -d "{\"model\":\"$1\",\"max_tokens\":100,\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"x\"}]}"; }
  # 🔴 判「发没发备用模型」必须整词匹配行尾：model=claude-opus-5 是 model=claude-opus-5.5 的前缀
  sent_fb() { grep -qx ".* model=${FB}" $W/mock.log; }
  sent_runtime() { grep -q '^/runtime' $W/mock.log; }
  ok() { echo "   OK   $1"; }; bad() { echo "   STOP $1"; sed 's/^/        mock: /' $W/mock.log; RC=1; }
  mock_run fine; up "$TAG" 30896 "$FB" "http://$HOSTIP:18777" >/dev/null || RC=1
  out=$(ask claude-opus-5-5)
  if echo "$out" | grep -q '都没有作答' && sent_fb; then ok "备用也空 ⇒ 回「都没有作答」提示"; else bad "备用也空 ⇒ 回「都没有作答」提示"; fi
  mock_run fine; out=$(ask claude-sonnet-5)
  if echo "$out" | grep -q '"content":\[\]' && ! sent_runtime; then ok "非 opus 空 ⇒ 原样空、不探测不换模型"; else bad "非 opus 空 ⇒ 原样空、不探测不换模型"; fi
  mock_run restricted; out=$(ask claude-opus-5-5)
  if sent_runtime && ! sent_fb; then ok "号被限 ⇒ 走换号、不换模型"; else bad "号被限 ⇒ 走换号、不换模型"; fi
fi

if [ "$LL" = 1 ]; then
  echo "== 4. 经 LiteLLM（生产同镜像）四种线型"
  IMG=$($KC -n litellm-product get deploy litellm-proxy -o jsonpath='{.spec.template.spec.containers[0].image}')
  printf 'model_list:\n  - model_name: kiro-slots-claude-opus-5-5\n    litellm_params: {model: anthropic/claude-opus-5-5, api_base: "http://127.0.0.1:30893", api_key: t}\ngeneral_settings: {master_key: sk-refusal-test}\nlitellm_settings: {num_retries: 0}\n' > $W/ll.yaml
  $D run -d --name kiro-refusal-ll --network host -v $W/ll.yaml:/app/config.yaml:ro $IMG --config /app/config.yaml --port 14999 >/dev/null
  for i in $(seq 1 60); do curl -s -m 2 http://127.0.0.1:14999/health/liveliness >/dev/null && break; sleep 3; done
  python3 $W/litellm_wire.py || RC=1
fi
[ $RC = 0 ] && echo "✅ 全部通过" || echo "🔴 有失败（见上面 STOP）"
exit $RC
EOF
