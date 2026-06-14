#!/usr/bin/env bash
# cc-acct-probe.sh — 完整探针测试一个或所有 Claude Max 账号的 3 个模型
#
# 用法:
#   ./cc-acct-probe.sh                 # 测所有 acct-* (汇总矩阵)
#   ./cc-acct-probe.sh acct-1          # 只测一个
#   ./cc-acct-probe.sh acct-1 -v       # 详细模式 (含完整响应 body + request_id)
#   ./cc-acct-probe.sh -p              # 同时测 188 LiteLLM 中转 (4101/4102)
#   ./cc-acct-probe.sh --watch [N]     # 每 N 秒重复 (默认 300s=5min),Opus 200 OK 时退出 0
#
# 输出含:
#   - HTTP code (200 / 429 / 401 等)
#   - 错误类型 (rate_limit_error / token_invalidated 等)
#   - request_id (给卖家做证据)
#   - 时间戳 (UTC+8)

set -eo pipefail

SSH="cltx@10.68.13.188"
ANTH_MK="${ANTHROPIC_188_MASTER_KEY:?set ANTHROPIC_188_MASTER_KEY}"
MODELS=("claude-opus-4-7" "claude-sonnet-4-6" "claude-haiku-4-5")

VERBOSE=0
PROXY=0
WATCH=0
WATCH_INTERVAL=300
ACCT_FILTER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--verbose) VERBOSE=1; shift ;;
        -p|--proxy) PROXY=1; shift ;;
        --watch)
            WATCH=1; shift
            if [[ -n "${1:-}" ]] && [[ "$1" =~ ^[0-9]+$ ]]; then
                WATCH_INTERVAL="$1"; shift
            fi
            ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# //'; exit 0 ;;
        acct-*) ACCT_FILTER="$1"; shift ;;
        *) echo "❌ unknown arg: $1"; exit 1 ;;
    esac
done

probe_one() {
    local acct="$1" model="$2" mode="$3"  # mode = direct | proxy
    local cmd_remote port

    if [[ "$mode" == "direct" ]]; then
        cmd_remote="
TOK=\$(grep ANTHROPIC_OAUTH_TOKEN /Data/anthropic-auth/$acct/.env | cut -d= -f2)
curl -sw '\n%{http_code} %{time_total}' --max-time 20 https://api.anthropic.com/v1/messages \
  -H \"Authorization: Bearer \$TOK\" \
  -H 'anthropic-beta: oauth-2025-04-20' \
  -H 'anthropic-dangerous-direct-browser-access: true' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{\"model\":\"$model\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
    else  # proxy: 188 LiteLLM 4101+N-1
        local n="${acct#acct-}"
        port=$((4100 + n))
        cmd_remote="
curl -sw '\n%{http_code} %{time_total}' --max-time 20 http://localhost:$port/v1/chat/completions \
  -H 'Authorization: Bearer $ANTH_MK' \
  -H 'Content-Type: application/json' \
  -d '{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":20}'"
    fi

    local raw
    raw=$(ssh "$SSH" "$cmd_remote" 2>/dev/null)
    local body http_time http_code
    body=$(echo "$raw" | head -n -1)
    local last_line
    last_line=$(echo "$raw" | tail -1)
    http_code=$(echo "$last_line" | awk '{print $1}')
    http_time=$(echo "$last_line" | awk '{print $2}')

    local req_id err_type reply_text
    req_id=$(echo "$body" | grep -oE 'req_[a-zA-Z0-9]+' | head -1)
    err_type=$(echo "$body" | grep -oE '"type":"[a-z_]+_error"' | head -1 | cut -d'"' -f4)
    reply_text=$(echo "$body" | grep -oE '"text":"[^"]*"' | head -1 | cut -d'"' -f4)

    local status_icon
    if [[ "$http_code" == "200" ]]; then
        status_icon="✅"
    elif [[ "$http_code" == "429" ]]; then
        status_icon="🟠"
    elif [[ "$http_code" =~ ^4 ]]; then
        status_icon="🔴"
    else
        status_icon="❓"
    fi

    local mode_label="direct"
    [[ "$mode" == "proxy" ]] && mode_label="proxy:$port"

    printf "  %s %-7s %-18s HTTP=%s  t=%ss  " "$status_icon" "$mode_label" "$model" "$http_code" "$http_time"
    if [[ "$http_code" == "200" ]]; then
        printf "reply=\"%s\"" "$reply_text"
    else
        printf "err=%s  %s" "${err_type:-?}" "${req_id:-}"
    fi
    echo

    if [[ $VERBOSE -eq 1 ]]; then
        echo "    body: $(echo "$body" | head -c 300)"
    fi

    # 返回 0 = 200, 1 = 其他
    [[ "$http_code" == "200" ]]
}

probe_acct() {
    local acct="$1"
    echo ""
    echo "── $acct ─────────────────────────────────────────"
    local opus_ok=1
    for model in "${MODELS[@]}"; do
        if probe_one "$acct" "$model" "direct"; then
            [[ "$model" == "claude-opus-4-7" ]] && opus_ok=0
        fi
    done
    if [[ $PROXY -eq 1 ]]; then
        echo "  -- 188 LiteLLM 中转 --"
        for model in "${MODELS[@]}"; do
            probe_one "$acct" "$model" "proxy" || true
        done
    fi
    return $opus_ok
}

discover_accts() {
    ssh "$SSH" 'ls /Data/anthropic-auth/ 2>/dev/null | grep -E "^acct-[0-9]+$" | sort -V'
}

run_once() {
    echo "═══════════════════════════════════════════════════════════"
    echo "  Claude Max 探针  $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "═══════════════════════════════════════════════════════════"
    local accts any_opus=1
    if [[ -n "$ACCT_FILTER" ]]; then
        accts=("$ACCT_FILTER")
    else
        readarray -t accts < <(discover_accts)
    fi
    if [[ ${#accts[@]} -eq 0 ]]; then
        echo "❌ 没找到 acct-*"; return 1
    fi
    for acct in "${accts[@]}"; do
        probe_acct "$acct" && any_opus=0 || true
    done
    echo ""
    if [[ $any_opus -eq 0 ]]; then
        echo "🎉 至少一个账号 Opus 4.7 = 200 OK"
        return 0
    else
        echo "⚠️ 所有账号 Opus 4.7 仍然 429,卖家未生效"
        return 1
    fi
}

if [[ $WATCH -eq 1 ]]; then
    while true; do
        if run_once; then
            echo ""
            echo "✅ Opus 通了,watch 退出"
            exit 0
        fi
        echo ""
        echo "💤 等 ${WATCH_INTERVAL}s 后重试..."
        sleep "$WATCH_INTERVAL"
    done
else
    run_once
fi
