#!/usr/bin/env bash
# test-cli-contract.sh — mcp-connector-cli.js / discover-chatgpt-routes.js 契约测试
#
# 不联网,不需要真 session。只验证:
#   1. 坏输入必须给出人话错误 + 非零退出(不是 stack trace、不是静默 exit 0)
#   2. --help 不需要 session
#   3. 从任意 cwd 都能跑(之前踩过 dirname 链断裂)
#
# 为什么需要:静默 exit 0 是最危险的失败模式 —— 调用方会以为成功了。
# 本 session 真实踩过 null session 打出 stack trace 的 bug。
#
# 跑: bash testkit/test-cli-contract.sh

# 注意:不用 set -e —— 我们要逐条断言并继续
set -u

BRIDGE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="$BRIDGE/mcp-connector-cli.js"
DISC="$BRIDGE/discover-chatgpt-routes.js"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0

# 断言:命令必须以 want_exit 退出,且 stdout 含 want_text
expect() {
  local name="$1" want_exit="$2" want_text="$3"; shift 3
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" != "$want_exit" ]; then
    echo "FAIL  $name — 期望 exit=$want_exit,实际 $rc"; FAIL=$((FAIL+1)); return
  fi
  if [ -n "$want_text" ] && ! printf '%s' "$out" | grep -q "$want_text"; then
    echo "FAIL  $name — 输出缺少 '$want_text'"; FAIL=$((FAIL+1)); return
  fi
  # stack trace 泄漏视为失败,哪怕退出码对
  if printf '%s' "$out" | grep -qE '^\s+at .*\(node:internal'; then
    echo "FAIL  $name — 泄漏了 stack trace"; FAIL=$((FAIL+1)); return
  fi
  echo "PASS  $name"; PASS=$((PASS+1))
}

# --- 各种坏 session ---
printf 'not json'            > "$TMP/notjson.json"
printf 'null'                > "$TMP/null.json"
printf '[]'                  > "$TMP/arr.json"
printf '"str"'               > "$TMP/str.json"
printf '{"headers":{"cookie":"x=1"}}' > "$TMP/noauth.json"
# 形状合法的假 session:能过 loadSession,用于测命令/参数校验
# (参数校验发生在 loadSession 之后,所以这里必须给个能过的)
printf '{"headers":{"authorization":"Bearer fake","cookie":"x=1"}}' > "$TMP/ok.json"

echo "=== mcp-connector-cli.js 坏输入 ==="
expect "session 缺失"      2 "缺少 --session"  node "$CLI" devmode-status
expect "session 不存在"    2 "不存在"          node "$CLI" --session "$TMP/nope.json" devmode-status
expect "session 非 JSON"   2 "不是合法 JSON"   node "$CLI" --session "$TMP/notjson.json" devmode-status
expect "session 是 null"   2 "必须是 object"   node "$CLI" --session "$TMP/null.json" devmode-status
expect "session 是数组"    2 "必须是 object"   node "$CLI" --session "$TMP/arr.json" devmode-status
expect "session 是字符串"  2 "必须是 object"   node "$CLI" --session "$TMP/str.json" devmode-status
expect "无 authorization"  2 "authorization"   node "$CLI" --session "$TMP/noauth.json" devmode-status

echo "=== 参数校验 ==="
expect "未知命令"          2 "未知命令"        node "$CLI" --session "$TMP/ok.json" bogus
expect "register 缺 url"   2 "需要 --url"      node "$CLI" --session "$TMP/ok.json" register
expect "actions 缺 id"     2 "需要"            node "$CLI" --session "$TMP/ok.json" actions
expect "link 缺 id"        2 "需要"            node "$CLI" --session "$TMP/ok.json" link
expect "delete 缺 id"      2 "需要"            node "$CLI" --session "$TMP/ok.json" delete

echo "=== --help 不需要 session ==="
expect "--help"            0 "MCP connector"   node "$CLI" --help
expect "无参数出 help"     0 "命令:"           node "$CLI"

echo "=== discover-chatgpt-routes.js ==="
expect "缺 session"        2 "需要 --session"  node "$DISC"
expect "session 是 null"   2 "必须是 object"   node "$DISC" --session "$TMP/null.json"
expect "无 authorization"  2 "authorization"   node "$DISC" --session "$TMP/noauth.json"

echo "=== 从异地 cwd 跑 ==="
cd "$TMP" || exit 1
expect "cwd=tmpdir --help" 0 "MCP connector"   node "$CLI" --help
expect "平面分类回归"      0 "7/7"             node "$BRIDGE/testkit/test-plane-classify.js"

echo
echo "$PASS 通过 / $FAIL 失败"
[ "$FAIL" -eq 0 ]
