#!/usr/bin/env sh
# Codex Desktop 启动依赖的出网探测：分清「挂住」「快速失败」「可达」三态。
#
# 关键区分（这次定因的核心）：
#   挂住(HANG)      = DNS 秒解、TCP connect 永不完成 ⇒ 客户端不会快速报错，只能重试到超时 ⇒ 病根
#   快速失败(FAIL)  = 立刻 connection refused / 立刻 DNS 失败 ⇒ 无害，statsig 会立即放行
#   可达(REACHABLE) = 有 HTTP 状态码（403/404 也算可达）⇒ 无害，**不要去封它们**
#
# 已知形状（2026-09-09 本机实测）：只有 chatgpt.com 挂住；statsig 自家域名全部可达。
set -u
T=${CONNECT_TIMEOUT:-6}

HOSTS="
chatgpt.com
api.statsig.com
featuregates.org
api.statsigcdn.com
featureassets.org
events.statsigapi.net
prodregistryv2.org
persistent.oaistatic.com
"

printf '%-28s %-6s %-8s %-9s %s\n' HOST CODE CONNECT TOTAL VERDICT
hang=0
for h in $HOSTS; do
  out=$(curl -s -o /dev/null -m "$T" \
        -w '%{http_code} %{time_connect} %{time_total}' "https://$h/" 2>&1)
  rc=$?
  set -- $out
  code=${1:-000}; tc=${2:-0}; tt=${3:-0}

  if [ "$code" != "000" ]; then
    v="REACHABLE（无害，别封）"
  elif [ "$rc" = 28 ]; then
    # 超时：再分「connect 都没握上手」还是「握手了但没响应」
    if [ "${tc%.*}" = "0" ] && [ "$(printf '%.0f' "$tc" 2>/dev/null || echo 0)" = "0" ]; then
      v="HANG ⇒ connect 挂住，这就是病根"
      hang=$((hang+1))
    else
      v="HANG ⇒ 已连上但无响应"
      hang=$((hang+1))
    fi
  elif [ "$rc" = 6 ]; then
    v="FAIL-FAST（DNS 解不出，无害）"
  elif [ "$rc" = 7 ]; then
    v="FAIL-FAST（connection refused，无害；hosts 修复后 chatgpt.com 应落这一档）"
  else
    v="curl rc=$rc"
  fi
  printf '%-28s %-6s %-8s %-9s %s\n' "$h" "$code" "$tc" "$tt" "$v"
done

echo
if [ "$hang" -gt 0 ]; then
  echo "有 $hang 个域名 connect 挂住。若其中含 chatgpt.com ⇒ 就是新窗口转圈几十秒的成因。"
  echo "修法见 SKILL.md §3；**只处理挂住的那个**，可达的域名一个都别封。"
  exit 1
fi
echo "没有挂住的域名。若仍慢，去跑 startup_timing.py 看 statsig gap 占比是否 <70%（另有其因）。"
