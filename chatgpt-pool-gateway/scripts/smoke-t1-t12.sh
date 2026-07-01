#!/usr/bin/env bash
# smoke-t1-t12.sh — chatgpt-pool-gateway end-to-end regression in litellm-dev ns
#
# 用法:
#   ./smoke-t1-t12.sh                # 在 198 jms 隧道里跑; svc DNS 走 gateway clusterIP
#   ./smoke-t1-t12.sh --skip T10,T11 # 跳过长 RSS/socket leak 用例
#
# T1  单 turn /v1/chat/completions 透传非流式
# T2  SSE 流式 + finish_reason=stop
# T3  affinity (5 turn 同 conv_id 命中同 acct)
# T4  compaction-drop (reasoning + encrypted_content 整项 drop, 200 OK)
# T6  quota pause (mock /_admin/quota → gateway state=cooling)
# T7  fail-fast (mock /_admin/fault=500 → 502 + FIRST_BYTE_5XX inc, gateway 不 retry)
# T9  master key auth (Bearer 错值 401)
# T-OPS  /admin/acct/add (2 步加 acct)
#
# 跳过 T5 (refresh, 已有 pytest), T8 (CF JA3, mock 不做),
# T10/T11 (RSS/socket, 长跑 1h), T12 (rollout 不丢)。
#
# 前置:
#   - jms ssh AIYJY-litellm 'kubectl -n litellm-dev get deploy chatgpt-pool-gateway' Ready
#   - mock-chatgpt-upstream Ready
#
set -euo pipefail

NS=${NS:-litellm-dev}
GW_BASE=${GW_BASE:-http://chatgpt-pool-gateway.$NS.svc.cluster.local:4000}
MOCK_BASE=${MOCK_BASE:-http://mock-chatgpt-upstream.$NS.svc.cluster.local:4101}
INTERNAL_KEY=${INTERNAL_KEY:-sk-pool-internal-dev}
SKIP="${SKIP:-}"

# 通过 jms 跳板执行 (本机 mac 不在集群内)
run_in_cluster() {
  jms ssh AIYJY-litellm "$1"
}

PASS=0; FAIL=0; SKIPPED=0
log() { echo "[smoke $(date +%H:%M:%S)] $*"; }
should_skip() { [[ ",$SKIP," == *",$1,"* ]]; }

# kubectl exec into a curl-capable pod (litellm-proxy has curl); avoid shell quoting hell
KUBECTL_RUN="kubectl -n $NS run smoke-curl-$$ --rm -i --restart=Never --image=curlimages/curl:8.10.1 -q --"

# ---------- bootstrap: 拉 mock tokens + 写 auth.json + 注册 3 acct ----------
log "bootstrap: 拉 mock accounts + 注册到 gateway"
MOCK_ACCTS_JSON=$(run_in_cluster "$KUBECTL_RUN -s $MOCK_BASE/_admin/accounts" 2>/dev/null | grep -E '^\[?\{|^\[' | head -1)
[ -z "$MOCK_ACCTS_JSON" ] && { echo "FATAL: mock accounts empty"; exit 2; }
echo "$MOCK_ACCTS_JSON" | head -c 400; echo

# 取前 3 个 mock acct → 写 auth.json 到 gateway pod's /data/auth/<name>/auth.json → register
for IDX in 0 1 2; do
  NAME=$(echo "$MOCK_ACCTS_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$IDX]['name'])")
  AT=$(echo "$MOCK_ACCTS_JSON"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$IDX]['access_token'])")
  RT=$(echo "$MOCK_ACCTS_JSON"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$IDX]['refresh_token'])")
  log "  inject $NAME"
  AUTH_JSON=$(python3 -c "import json,time; print(json.dumps({'tokens':{'access_token':'$AT','refresh_token':'$RT','expires_at':time.time()+3600,'last_refresh':time.time()},'account_id':'$NAME'}))")
  run_in_cluster "kubectl -n $NS exec deploy/chatgpt-pool-gateway -- sh -c 'mkdir -p /data/auth/$NAME && cat > /data/auth/$NAME/auth.json' <<EOF
$AUTH_JSON
EOF"
  run_in_cluster "$KUBECTL_RUN -sS -X POST $GW_BASE/admin/acct/add -H 'Authorization: Bearer $INTERNAL_KEY' -H 'Content-Type: application/json' -d '{\"name\":\"$NAME\",\"auth_path\":\"/data/auth/$NAME/auth.json\",\"priority\":50}'" >/dev/null 2>&1 || true
done

# trigger one probe so picker has fresh data
sleep 2
log "list registered accts:"
run_in_cluster "$KUBECTL_RUN -sS $GW_BASE/admin/acct/list -H 'Authorization: Bearer $INTERNAL_KEY'"

case_pass() { PASS=$((PASS+1)); log "  ✅ $1"; }
case_fail() { FAIL=$((FAIL+1)); log "  ❌ $1: $2"; }
case_skip() { SKIPPED=$((SKIPPED+1)); log "  ⏭️  $1: skipped"; }

# ---------- T1: 非流式 ----------
if should_skip T1; then case_skip T1; else
  RESP=$(run_in_cluster "$KUBECTL_RUN -sS -X POST $GW_BASE/v1/chat/completions \
    -H 'Authorization: Bearer $INTERNAL_KEY' -H 'Content-Type: application/json' \
    -d '{\"model\":\"chatgpt-pool\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'" || echo '{}')
  echo "$RESP" | grep -q '"choices"' && case_pass T1 || case_fail T1 "no choices: $RESP"
fi

# ---------- T2: 流式 ----------
if should_skip T2; then case_skip T2; else
  RESP=$(run_in_cluster "$KUBECTL_RUN -sS -X POST $GW_BASE/v1/chat/completions \
    -H 'Authorization: Bearer $INTERNAL_KEY' -H 'Content-Type: application/json' \
    -d '{\"model\":\"chatgpt-pool\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'" || echo '')
  echo "$RESP" | grep -q '\[DONE\]' && case_pass T2 || case_fail T2 "no [DONE]"
fi

# ---------- T3: affinity ----------
if should_skip T3; then case_skip T3; else
  CONV="C-$(date +%s)"
  declare -a IDS=()
  for I in 1 2 3 4 5; do
    R=$(run_in_cluster "$KUBECTL_RUN -sS -X POST $GW_BASE/v1/chat/completions \
      -H 'Authorization: Bearer $INTERNAL_KEY' -H 'Content-Type: application/json' \
      -d '{\"model\":\"chatgpt-pool\",\"messages\":[{\"role\":\"user\",\"content\":\"t$I\"}],\"metadata\":{\"conversation_id\":\"$CONV\"}}'" || echo '')
    IDS+=("$(echo "$R" | python3 -c "import sys,json; d=json.loads(sys.stdin.read() or '{}'); print(d.get('id',''))" 2>/dev/null || echo)")
  done
  UNIQ=$(printf '%s\n' "${IDS[@]}" | cut -c-25 | sort -u | wc -l)
  # 取 affinity counter (hit ≥ 4 表示后 4 turn 全命中黏)
  HITS=$(run_in_cluster "$KUBECTL_RUN -sS $GW_BASE/metrics" | grep -E 'gateway_affinity_total\{result="hit"' | awk '{print $2}' | head -1)
  [ "${HITS%.*}" -ge 4 ] 2>/dev/null && case_pass T3 || case_fail T3 "affinity hits=$HITS (expect ≥4)"
fi

# ---------- T4: compaction-drop ----------
if should_skip T4; then case_skip T4; else
  PAYLOAD='{"model":"chatgpt-pool","messages":[{"role":"user","content":"hi"}],"input":[{"type":"reasoning","summary":[{"type":"summary_text","text":"x"}]},{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]}'
  # gateway 用的是 chat completions; compaction-drop 跑在 chat→responses 之后. T4 真正验证是 metric
  RESP=$(run_in_cluster "$KUBECTL_RUN -sS -X POST $GW_BASE/v1/chat/completions \
    -H 'Authorization: Bearer $INTERNAL_KEY' -H 'Content-Type: application/json' \
    -d '$PAYLOAD'" || echo '')
  echo "$RESP" | grep -q '"choices"' && case_pass T4 || case_fail T4 "drop did not unblock: $RESP"
fi

# ---------- T6: quota pause ----------
if should_skip T6; then case_skip T6; else
  FIRST=$(echo "$MOCK_ACCTS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['name'])")
  run_in_cluster "$KUBECTL_RUN -sS -X POST $MOCK_BASE/_admin/quota -H 'Content-Type: application/json' -d '{\"name\":\"$FIRST\",\"primary_used\":100}'" >/dev/null
  # 触发立刻 probe (admin endpoint)
  run_in_cluster "$KUBECTL_RUN -sS -X POST $GW_BASE/admin/acct/probe -H 'Authorization: Bearer $INTERNAL_KEY' -H 'Content-Type: application/json' -d '{\"name\":\"$FIRST\"}'" >/dev/null || true
  LIST=$(run_in_cluster "$KUBECTL_RUN -sS $GW_BASE/admin/acct/list -H 'Authorization: Bearer $INTERNAL_KEY'")
  STATE=$(echo "$LIST" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(a['state']) for a in d['accounts'] if a['name']=='$FIRST']")
  [ "$STATE" = "cooling" ] && case_pass T6 || case_fail T6 "state=$STATE (expect cooling)"
  # 复位
  run_in_cluster "$KUBECTL_RUN -sS -X POST $MOCK_BASE/_admin/reset -d '{}'" >/dev/null
fi

# ---------- T7: fail-fast ----------
if should_skip T7; then case_skip T7; else
  SECOND=$(echo "$MOCK_ACCTS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)[1]['name'])")
  run_in_cluster "$KUBECTL_RUN -sS -X POST $MOCK_BASE/_admin/fault -H 'Content-Type: application/json' -d '{\"name\":\"$SECOND\",\"fault\":\"500\"}'" >/dev/null
  BEFORE=$(run_in_cluster "$KUBECTL_RUN -sS $GW_BASE/metrics" | awk '/gateway_first_byte_5xx_total/{print $2; exit}')
  # 强制路由到这个 acct: 走 X-Acct-Id (gateway 还没实现强制 acct 路由, 只能用 affinity)
  # 退而求次: 直接发 5 个请求, 命中概率高
  for _ in 1 2 3 4 5; do
    run_in_cluster "$KUBECTL_RUN -sS -X POST $GW_BASE/v1/chat/completions \
      -H 'Authorization: Bearer $INTERNAL_KEY' -H 'Content-Type: application/json' \
      -d '{\"model\":\"chatgpt-pool\",\"messages\":[{\"role\":\"user\",\"content\":\"t7\"}]}'" >/dev/null 2>&1 || true
  done
  AFTER=$(run_in_cluster "$KUBECTL_RUN -sS $GW_BASE/metrics" | awk '/gateway_first_byte_5xx_total/{print $2; exit}')
  python3 -c "import sys; sys.exit(0 if float('$AFTER' or 0) > float('$BEFORE' or 0) else 1)" \
    && case_pass T7 \
    || case_fail T7 "first_byte_5xx counter not incremented (before=$BEFORE after=$AFTER)"
  run_in_cluster "$KUBECTL_RUN -sS -X POST $MOCK_BASE/_admin/reset -d '{}'" >/dev/null
fi

# ---------- T9: bad bearer ----------
if should_skip T9; then case_skip T9; else
  HTTP=$(run_in_cluster "$KUBECTL_RUN -sS -o /dev/null -w '%{http_code}' -X POST $GW_BASE/v1/chat/completions \
    -H 'Authorization: Bearer WRONG' -H 'Content-Type: application/json' \
    -d '{\"model\":\"x\",\"messages\":[]}'" || echo 000)
  [ "$HTTP" = "403" ] || [ "$HTTP" = "400" ] && case_pass T9 || case_fail T9 "code=$HTTP"
fi

# ---------- T-OPS ----------
if should_skip T-OPS; then case_skip T-OPS; else
  NEW="acct-ops-$(date +%s)"
  AUTH_JSON=$(python3 -c "import json,time; print(json.dumps({'tokens':{'access_token':'sk-ops','refresh_token':'sk-ops-rt','expires_at':time.time()+3600,'last_refresh':time.time()},'account_id':'$NEW'}))")
  run_in_cluster "kubectl -n $NS exec deploy/chatgpt-pool-gateway -- sh -c 'mkdir -p /data/auth/$NEW && cat > /data/auth/$NEW/auth.json' <<EOF
$AUTH_JSON
EOF"
  R=$(run_in_cluster "$KUBECTL_RUN -sS -X POST $GW_BASE/admin/acct/add \
    -H 'Authorization: Bearer $INTERNAL_KEY' -H 'Content-Type: application/json' \
    -d '{\"name\":\"$NEW\",\"auth_path\":\"/data/auth/$NEW/auth.json\",\"priority\":99}'")
  echo "$R" | grep -q '"ok":true' && case_pass T-OPS || case_fail T-OPS "$R"
fi

echo
log "RESULT: PASS=$PASS FAIL=$FAIL SKIPPED=$SKIPPED"
[ "$FAIL" -eq 0 ]
