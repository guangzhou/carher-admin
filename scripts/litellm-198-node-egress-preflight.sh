#!/bin/bash
# litellm-198-node-egress-preflight.sh — 验证某台机器的出口能否承载 litellm-proxy
#
# 在待验机器上直接跑（scp 过去执行），然后在 198 上跑同一份作对照组。
# 纪律：两边结果必须逐行同型才算通过 —— 单边的 404/307 不能自行解读，
# 只有对照组同码才能证明"端点本来就返回这个码"。
# （memory: feedback_http200_wrong_result_needs_horizontal_control）
#
# 端点清单来源：litellm-product/litellm-config CM 里全部外呼 api_base（2026-08-25 口径）。
# CM 加了新上游后要同步更新这里 —— 更新方法：
#   kubectl -n litellm-product get cm litellm-config -o yaml | grep api_base | sort -u
#
# 注意：proxy 本身不直连 openai/google/chatgpt.com —— chatgpt 出口在 acct pod
# （acct pod 永远不能迁到无 openai 出口的节点）。
set -uo pipefail

ENDPOINTS=(
  "https://aigateway.edgecloudapp.com/"        # wangsu 网关（claude/gpt/gemini/ds/glm 全系）
  "https://kuaihuiai.com/"                     # anthropic.claude-* 直连系
  "https://openrouter.ai/api/v1/models"        # openrouter 系 + bge-m3
  "https://open.feishu.cn/"                    # 预算通知 webhook
  "http://10.68.13.188:4130/health"            # openrouter-gpt-5.5 内网上游
)

echo "HOST=$(hostname) IP=$(hostname -I | awk '{print $1}')"
for u in "${ENDPOINTS[@]}"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$u" 2>/dev/null)
  echo "$code  $u"
done
echo "# 通过判据：与 198 上同一脚本输出逐行同码（2026-08-25 基线：404/307|200/200/404/200）"
