#!/usr/bin/env bash
# lane82_regress_probe.sh — 82 lane 一发回归探针(动过 LiteLLM DB/CM/deployment 后必跑)
#
# 起因(2026-08-30 事故):直改 LiteLLM_ProxyModelTable 触发 proxy 热重注册,路由劈到
# 82 pod 的 chatgpt.js 入口踩潜伏雷(user is not a function),11 连 InternalServerError。
# 铁律:动表后 rollout restart litellm-proxy,然后跑本脚本回归。
# memory: feedback_proxymodeltable_write_reroutes_deployment_needs_restart_regress
#
# 流程:mint 15m scoped key → 打 /pro/v1/responses 一发 → 验 status/text →
#       查 82 pod 近 3min 无 TypeError/[MESSAGES](= 没走错到 chatgpt.js 路)→ 删 key。
# 退出码:0 全过;非 0 = 回归失败,输出指明哪步。
set -uo pipefail

SSH="sshpass -p 'Hn8#mKLp3QxZ' ssh -o StrictHostKeyChecking=no cltx@10.68.13.198"
MODEL="${1:-cursor-web-fc-82-terra}"
ALIAS="regress-$(date +%s)"
POD_SEL="app=zero-cursor-bpi-82"

echo "== [1/4] mint scoped key ($ALIAS, 15m 自灭) =="
KEY=$(eval $SSH "\"export KUBECONFIG=/home/cltx/.kube/config && kubectl -n litellm-product exec deploy/litellm-proxy -- python3 -c \\\"
import urllib.request, json, os
data = json.dumps({'models':['$MODEL'],'key_alias':'$ALIAS','duration':'15m'}).encode()
req = urllib.request.Request('http://localhost:4000/key/generate', data=data, headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],'Content-Type':'application/json'})
print(json.load(urllib.request.urlopen(req,timeout=15))['key'])
\\\" 2>&1 | grep -v sitecustomize | tail -1\"")
[[ "$KEY" == sk-* ]] || { echo "FAIL: mint key 失败: $KEY"; exit 1; }

echo "== [2/4] 打 $MODEL 一发 =="
RESP=$(curl -s --max-time 90 https://cc.auto-link.com.cn/pro/v1/responses \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"stream\":false,\"input\":[{\"type\":\"message\",\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"只回OK两个字母\"}]}]}")
STATUS=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status'))" 2>/dev/null)
PROBE_OK=0
if [[ "$STATUS" == "completed" ]]; then echo "PASS: status=completed"; else
  echo "FAIL: status=$STATUS resp=${RESP:0:300}"; PROBE_OK=1; fi

echo "== [3/4] 82 pod 近 3min 无走错路信号 =="
BAD=$(eval $SSH "\"export KUBECONFIG=/home/cltx/.kube/config && kubectl -n litellm-product logs -l $POD_SEL --tail=-1 --since=3m 2>/dev/null | grep -cE 'user is not a function|\\[MESSAGES\\]'\"")
if [[ "${BAD:-0}" == "0" ]]; then echo "PASS: 无 TypeError/[MESSAGES]"; else
  echo "FAIL: 近 3min 有 $BAD 条走错路信号(流量进了 chatgpt.js 路)"; PROBE_OK=1; fi

echo "== [4/4] 删 key =="
eval $SSH "\"export KUBECONFIG=/home/cltx/.kube/config && kubectl -n litellm-product exec deploy/litellm-proxy -- python3 -c \\\"
import urllib.request, json, os
req = urllib.request.Request('http://localhost:4000/key/delete', data=json.dumps({'key_aliases':['$ALIAS']}).encode(), headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],'Content-Type':'application/json'})
print(urllib.request.urlopen(req,timeout=15).read().decode()[:120])
\\\" 2>&1 | grep -v sitecustomize | tail -1\""

if [[ $PROBE_OK -eq 0 ]]; then echo "== VERDICT: PASS =="; else echo "== VERDICT: FAIL =="; fi
exit $PROBE_OK
