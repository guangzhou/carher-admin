#!/usr/bin/env bash
# claude-max-quota-probe.sh
#
# 测试一个 Claude Max OAuth token 在 Anthropic API 上是否能调 Opus / Sonnet / Haiku。
# 用于向供应商证明账号配额状态(尤其 Team workspace 共享池打满的场景)。
#
# 用法:
#   1) 把账号 OAuth token (sk-ant-oat01-xxx) 设到环境变量:
#        export CC_TOKEN='sk-ant-oat01-xxxxxxxxxxxxxxxxxxxxxxxx'
#   2) 跑:
#        bash claude-max-quota-probe.sh
#
# 或一行:
#   CC_TOKEN='sk-ant-oat01-xxx' bash claude-max-quota-probe.sh
#
# 输出每个模型:
#   HTTP 状态码 / 错误类型 / Anthropic request_id (供官方查询)
#   200 = 配额正常
#   429 = rate_limit_error (常见原因: Team workspace 周配额被共享池打满)
#
# 该脚本只读,不消耗有意义的 token (每次只发 "hi" 上限 20 输出)。

set -uo pipefail

# ── 拿 token ────────────────────────────────────────────────────
TOKEN="${CC_TOKEN:-${1:-}}"
if [[ -z "$TOKEN" ]]; then
    cat >&2 <<EOF
Usage:
  CC_TOKEN='sk-ant-oat01-xxx' bash $0
  OR
  bash $0 sk-ant-oat01-xxx

How to get an OAuth token for a Claude Pro/Max/Team account:
  1) On any machine with Claude Code CLI installed (https://code.claude.com/docs/en/setup):
       claude setup-token
  2) Follow the browser prompt to log in to claude.ai with the target account.
  3) Token format: sk-ant-oat01-xxxx (1-year lifetime, scope: user:inference).
EOF
    exit 1
fi

if [[ "${TOKEN:0:11}" != "sk-ant-oat0" ]]; then
    echo "❌ Token doesn't look like an OAuth token (must start with sk-ant-oat0)" >&2
    echo "   Got: ${TOKEN:0:20}..." >&2
    exit 1
fi

# ── 模型 + headers ──────────────────────────────────────────────
MODELS=(
    "claude-opus-4-7|Opus 4.7 (\$5/M input + \$25/M output, top tier)"
    "claude-sonnet-4-6|Sonnet 4.6 (\$3/M input + \$15/M output)"
    "claude-haiku-4-5|Haiku 4.5 (\$1/M input + \$5/M output)"
)

OAUTH_HEADERS=(
    -H "Authorization: Bearer $TOKEN"
    -H "anthropic-beta: oauth-2025-04-20"
    -H "anthropic-dangerous-direct-browser-access: true"
    -H "anthropic-version: 2023-06-01"
    -H "content-type: application/json"
)

# ── 跑 ──────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  Claude Max OAuth Quota Probe"
echo "  Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "  Token: ${TOKEN:0:24}... (len=${#TOKEN})"
echo "═══════════════════════════════════════════════════════════════"
echo

ANY_OK=0
ANY_429=0

for entry in "${MODELS[@]}"; do
    model="${entry%%|*}"
    label="${entry##*|}"

    printf "▶ %-18s  %s\n" "$model" "$label"

    body=$(curl -sS --max-time 25 -w "\n__HTTP_CODE__%{http_code}\n__TIME__%{time_total}\n" \
        https://api.anthropic.com/v1/messages \
        "${OAUTH_HEADERS[@]}" \
        -d "{\"model\":\"$model\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" 2>&1)

    http_code=$(echo "$body" | sed -n 's/^__HTTP_CODE__//p')
    http_time=$(echo "$body" | sed -n 's/^__TIME__//p')
    json_body=$(echo "$body" | sed '/^__HTTP_CODE__/,$d')

    req_id=$(echo "$json_body" | grep -oE 'req_[a-zA-Z0-9]+' | head -1)
    err_type=$(echo "$json_body" | grep -oE '"type":"[a-z_]+_error"' | head -1 | sed 's/.*:"//;s/"//')

    if [[ "$http_code" == "200" ]]; then
        reply=$(echo "$json_body" | grep -oE '"text":"[^"]*"' | head -1 | cut -d'"' -f4)
        printf "   ✅ HTTP=200  time=%ss  reply=\"%s\"\n" "$http_time" "$reply"
        ANY_OK=1
    elif [[ "$http_code" == "429" ]]; then
        printf "   🟠 HTTP=429  time=%ss  err=%s  request_id=%s\n" \
            "$http_time" "${err_type:-?}" "${req_id:-?}"
        ANY_429=1
    elif [[ "$http_code" == "401" || "$http_code" == "403" ]]; then
        printf "   🔴 HTTP=%s  time=%ss  err=%s  request_id=%s (token invalid / revoked)\n" \
            "$http_code" "$http_time" "${err_type:-?}" "${req_id:-?}"
    else
        printf "   ❓ HTTP=%s  time=%ss  err=%s  request_id=%s\n" \
            "$http_code" "$http_time" "${err_type:-?}" "${req_id:-?}"
        echo "   raw body: $(echo "$json_body" | head -c 300)"
    fi
    echo
done

# ── 诊断 ────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  Diagnosis"
echo "═══════════════════════════════════════════════════════════════"

if [[ $ANY_OK -eq 1 && $ANY_429 -eq 1 ]]; then
    cat <<EOF
⚠️  Partial: Haiku works but Opus/Sonnet are rate-limited.

This pattern indicates the Team workspace's weekly quota pool for
Opus/Sonnet has been exhausted (shared across all seats in the workspace).
Haiku has effectively unlimited quota, so it still works.

Action for supplier (you):
  1) Verify in Anthropic Console > Settings > Plans & Billing > Usage:
     - Look at the Team workspace this account belongs to
     - Weekly quota usage for Opus and Sonnet should show >= 100%
  2) Options to restore:
     a) Wait 7 days for the weekly window to reset (rolling 7-day from
        the workspace's first request)
     b) Move this seat to a new/independent workspace with fresh quota
     c) Upgrade the workspace plan (Max 20x has 5x more Opus quota than
        Max 5x)

The request IDs above can be provided to Anthropic support.
EOF
elif [[ $ANY_OK -eq 1 && $ANY_429 -eq 0 ]]; then
    echo "✅ All models OK. Account is healthy."
elif [[ $ANY_429 -eq 1 ]]; then
    echo "🔴 All models rate-limited. Workspace quota fully exhausted."
else
    echo "🔴 Token cannot reach Anthropic API. Check token validity / network."
fi
