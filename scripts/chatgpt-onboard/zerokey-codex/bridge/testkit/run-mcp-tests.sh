#!/usr/bin/env bash
# run-mcp-tests.sh — MCP connector 工具链全部测试
#
# 默认只跑离线测试(不联网、不需要 session、不碰账号)。
# 带 --session <file> 才额外跑真实端到端 probe(会在账号上临时建 connector,自动删除)。
#
# 跑: bash testkit/run-mcp-tests.sh
#     bash testkit/run-mcp-tests.sh --session /path/sess.json

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="$(cd "$HERE/.." && pwd)"
SESSION=""
[ "${1:-}" = "--session" ] && SESSION="${2:-}"

RC=0

echo "########## 1. 平面分类回归(离线) ##########"
node "$HERE/test-plane-classify.js" || RC=1

echo
echo "########## 2. CLI 契约测试(离线) ##########"
bash "$HERE/test-cli-contract.sh" || RC=1

echo
echo "########## 3. bridge 单元测试(离线) ##########"
if node "$HERE/test-provision-rollback.js" >/tmp/_r.out 2>&1; then
  echo "PASS  test-provision-rollback.js  $(tail -1 /tmp/_r.out)"
else
  echo "FAIL  test-provision-rollback.js"; tail -3 /tmp/_r.out; RC=1
fi
for t in test_decay.py test_fanout.py test_struct_stats.py test_nudge.py test_refusal_corpus.py; do
  if python3 "$HERE/$t" >/tmp/_t.out 2>&1; then
    echo "PASS  $t  $(tail -1 /tmp/_t.out)"
  else
    echo "FAIL  $t"; tail -3 /tmp/_t.out; RC=1
  fi
done

echo
echo "########## 4. 语法自检 ##########"
for f in "$BRIDGE/mcp-connector-cli.js" "$BRIDGE/discover-chatgpt-routes.js"; do
  if node --check "$f" 2>/dev/null; then
    echo "PASS  $(basename "$f")"
  else
    echo "FAIL  $(basename "$f") 语法错误"; RC=1
  fi
done
for f in "$BRIDGE/zerokey-codex-responses-bridge.py" "$BRIDGE/mcp-provision-pool.py"; do
  [ -f "$f" ] || continue
  if python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null; then
    echo "PASS  $(basename "$f")"
  else
    echo "FAIL  $(basename "$f") 语法错误"; RC=1
  fi
done

if [ -n "$SESSION" ]; then
  echo
  echo "########## 5. 端到端 probe(真实账号) ##########"
  if [ ! -f "$SESSION" ]; then
    echo "FAIL  session 不存在: $SESSION"; RC=1
  else
    node "$BRIDGE/mcp-connector-cli.js" --session "$SESSION" probe || RC=1
  fi
else
  echo
  echo "(跳过端到端 probe —— 加 --session <file> 才跑)"
fi

echo
if [ "$RC" -eq 0 ]; then echo "===== 全部通过 ====="; else echo "===== 有失败 ====="; fi
exit "$RC"
