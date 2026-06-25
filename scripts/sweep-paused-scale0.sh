#!/usr/bin/env bash
# sweep-paused-scale0.sh — patch 上线前已 paused 但 deploy 仍 replicas=1 的历史 acct
# 一次性 scale=0 释放 198 内存。patch 后新 pause 会自动缩，所以这是一次性 cleanup。
#
# **必须在 188 (JSZX-AI-03) 上跑**：依赖
#   - 188 本地 state.json: /home/cltx/.chatgpt-quota/state/state.json
#   - 188→198 SSH ControlMaster mux: /tmp/cm-quota-198-cltx@10.68.13.198:22
#     (mux 由 cron quota-rebalance 进程自动维护，缺失时先 `ssh cltx@10.68.13.198 true` 建)
#
# 用法（在 188 上）：
#   DRY_RUN=1 bash sweep-paused-scale0.sh   # 默认，只打印
#   DRY_RUN=0 bash sweep-paused-scale0.sh   # 真跑
#
# 从本地推送：
#   scp scripts/sweep-paused-scale0.sh JSZX-AI-03:/tmp/  # 或 jms scp
#   jms ssh JSZX-AI-03 'DRY_RUN=0 bash /tmp/sweep-paused-scale0.sh'
#
# 安全：只动 state.paused=True 或 manual_offline=True 的 acct，不动健康 acct
set -eu
DRY_RUN="${DRY_RUN:-1}"

ssh -o ControlPath=/tmp/cm-quota-198-cltx@10.68.13.198:22 \
  cltx@10.68.13.198 "export KUBECONFIG=\$HOME/.kube/config; \
    kubectl -n litellm-product get deploy -o json" > /tmp/dep.json

ACCTS=$(python3 -c "
import json
d = json.load(open('/tmp/dep.json'))
state = json.load(open('/home/cltx/.chatgpt-quota/state/state.json'))
out = []
for it in d['items']:
    name = it['metadata']['name']
    if not name.startswith('chatgpt-acct-'): continue
    acct = name.replace('chatgpt-', '')
    rep = it['spec']['replicas']
    s = state.get(acct, {})
    if (s.get('paused') or s.get('manual_offline')) and rep > 0:
        out.append(acct)
print(' '.join(sorted(out, key=lambda x: int(x.split('-')[1]))))
")

echo "scale=0 targets ($(echo $ACCTS | wc -w) acct): $ACCTS"

if [ "$DRY_RUN" = "1" ]; then
  echo "[DRY_RUN=1] not scaling. Set DRY_RUN=0 to apply."
  exit 0
fi

ok=0; fail=0
for a in $ACCTS; do
  out=$(ssh -o ControlPath=/tmp/cm-quota-198-cltx@10.68.13.198:22 \
    cltx@10.68.13.198 \
    "export KUBECONFIG=\$HOME/.kube/config; \
     kubectl -n litellm-product scale deploy/chatgpt-$a --replicas=0" 2>&1)
  if echo "$out" | grep -q scaled; then
    ok=$((ok+1))
    echo "  $a: $out"
  else
    fail=$((fail+1))
    echo "  $a FAIL: $out" >&2
  fi
done
echo "sweep: ok=$ok fail=$fail"
ssh -o ControlPath=/tmp/cm-quota-198-cltx@10.68.13.198:22 \
  cltx@10.68.13.198 "free -g | head -2"
