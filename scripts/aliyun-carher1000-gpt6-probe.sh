#!/usr/bin/env bash
# 阿里云 LiteLLM：carher-1000 key 的 GPT-6 可用性探针（只读）
# 用法：bash scripts/aliyun-carher1000-gpt6-probe.sh
set -uo pipefail

NS=carher
PORT=4000

cleanup() { [ -n "${PF_PID:-}" ] && kill "$PF_PID" 2>/dev/null; }
trap cleanup EXIT

# 1) 建立到 litellm-proxy 的端口转发（本机默认没有 4000 监听）
kubectl -n "$NS" port-forward svc/litellm-proxy "$PORT:4000" >/tmp/pf-litellm.log 2>&1 &
PF_PID=$!
for _ in $(seq 1 30); do
  curl -sS -m 2 "http://127.0.0.1:$PORT/health/liveliness" >/dev/null 2>&1 && break
  sleep 0.5
done

# 2) 从 HerInstance CRD 取 key（不回显）
KEY=$(kubectl -n "$NS" get her her-1000 -o jsonpath='{.spec.litellmKey}')
[ -n "$KEY" ] || { echo "FATAL: her-1000 未取到 litellmKey"; exit 1; }
BASE="http://127.0.0.1:$PORT"
AUTH=(-H "Authorization: Bearer $KEY" -H "Content-Type: application/json")

# 3) 目录 + key 授权面
echo "=== /v1/models (key 可见模型) ==="
curl -sS "${AUTH[@]}" "$BASE/v1/models" | python3 -c 'import sys,json;print("\n".join(sorted(d["id"] for d in json.load(sys.stdin)["data"])))'
echo "=== /key/info (allowlist / aliases / budget) ==="
curl -sS "${AUTH[@]}" "$BASE/key/info" | python3 -c 'import sys,json;i=json.load(sys.stdin)["info"];print(json.dumps({k:i.get(k) for k in("key_alias","max_budget","spend","expires","models","aliases")},ensure_ascii=False,indent=2))'

# 4) 五发探针：把「名字不在白名单」「协议不对」「请求体不对」「真可用」分开
probe() {
  local label="$1" endpoint="$2" payload="$3"
  echo "--- $label -> $endpoint ---"
  curl -sS -o /tmp/probe.out -w 'HTTP %{http_code}  %{time_total}s\n' \
    "${AUTH[@]}" -X POST "$BASE$endpoint" -d "$payload"
  head -c 600 /tmp/probe.out; echo; echo
}

probe "A 字面名 gpt-6" /v1/chat/completions \
  '{"model":"gpt-6","messages":[{"role":"user","content":"Reply with exactly OK."}],"max_tokens":8,"stream":false}'

probe "B 短名 gpt-6-astra 走 chat" /v1/chat/completions \
  '{"model":"gpt-6-astra","messages":[{"role":"user","content":"Reply with exactly OK."}],"max_tokens":8,"stream":false}'

probe "C responses + 字符串 input" /v1/responses \
  '{"model":"chatgpt-gpt-6-astra","input":"Reply with exactly OK.","max_output_tokens":8,"stream":false}'

probe "D responses + 结构化 input（预期通过）" /v1/responses \
  '{"model":"chatgpt-gpt-6-astra","input":[{"role":"user","content":[{"type":"input_text","text":"Reply with exactly OK."}]}],"max_output_tokens":8,"stream":false}'

probe "E responses + 简写 content（预期通过）" /v1/responses \
  '{"model":"chatgpt-gpt-6-astra","input":[{"role":"user","content":"Reply with exactly OK."}],"max_output_tokens":8,"stream":false}'
