#!/bin/bash
# 新 run 进 normal_gray 之后、放任何比例之前跑：把「不是真用户」的 key 钉在旧版（protect-prod）。
#
# 为什么（2026-10-17 v1.105.1 灰度两次被它们误伤回退）：
#   * carher-hangzhou —— 杭州 gw 的上游凭据，持续扫 ~110 个已坏模型组，新旧版同样 40%+ 失败；
#     占凌晨流量大半，一放到 50% 就让 USER_FAILURE_RATE 硬触发回退。
#   * stability-probe —— 巡检号，扫死组，失败率恒 ~31%。
# 它们的失败不是版本造成的，但监控按 pool 统计，分到哪边就算哪边的。钉在旧版 = 不进灰度统计。
#
# 明文永不落盘、不上屏：
#   stability-probe   从 secret litellm-probe-key/PROBE_KEY 直接读进 stdin
#   carher-hangzhou   明文只在杭州机（/opt/llm-gateway/.env 的 REMOTE_LITELLM_API_KEY），本机用不到。
#                     在 Mac 上跑这一条管道（jms 读、ssh 写，中间不落盘）：
#     scripts/jms ssh vllm 'cd /opt/llm-gateway && set -a && . ./.env >/dev/null 2>&1 && set +a && printf %s "$REMOTE_LITELLM_API_KEY"' 2>/dev/null \
#       | ssh cltx@10.68.13.198 'sudo -n bash -c "D=$(cat /root/l3/.gray-run-latest); . $D/target.env; set -a; . $D/nginx/frozen-env.sh; set +a; bash $SRC/scripts/gray-key-route.sh protect-prod"'
#   跑完用 sid 核对：carher-hangzhou = aa820a9360ad（2026-10-17）。
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); . $D/target.env
set -a; . $D/nginx/frozen-env.sh; set +a
kubectl -n litellm-product get secret litellm-probe-key -o jsonpath='{.data.PROBE_KEY}' | base64 -d \
  | bash $SRC/scripts/gray-key-route.sh protect-prod
echo "protected-prod now: $(grep -cvE '^\s*(#|$)' /etc/nginx/gray-route/active/protected-prod.map) 条（还要在 Mac 上跑上面那条杭州管道）"
