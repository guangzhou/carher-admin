#!/usr/bin/env bash
set -euo pipefail

# Run from a Mac against the public GPU IP. This is intentionally one request
# per port, so it verifies exposure and serving without generating load.
GPU_IP="${GPU_IP:-36.151.241.10}"
PORTS="${PORTS:-8765 8766}"
MODEL="${MODEL:-deepseek-v4-flash}"
CONNECT_TIMEOUT_S="${CONNECT_TIMEOUT_S:-5}"
REQUEST_TIMEOUT_S="${REQUEST_TIMEOUT_S:-30}"
OUT_DIR="${OUT_DIR:-$(pwd)/runs/local-gpu-smoke/$(date +%Y%m%dT%H%M%S)}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 2
  }
}

need curl
need jq

mkdir -p "$OUT_DIR"
RESULTS="$OUT_DIR/results.jsonl"
: > "$RESULTS"

request() {
  local port="$1"
  local name="$2"
  local path="$3"
  local method="$4"
  local body_file="${5:-}"
  local base_url="http://${GPU_IP}:${port}"
  local raw="$OUT_DIR/${port}-${name}.body"
  local meta="$OUT_DIR/${port}-${name}.meta"
  local curl_args=(
    -sS
    --connect-timeout "$CONNECT_TIMEOUT_S"
    --max-time "$REQUEST_TIMEOUT_S"
    -X "$method"
    "${base_url}${path}"
    -w '%{http_code} %{time_namelookup} %{time_connect} %{time_starttransfer} %{time_total}'
    -o "$raw"
  )

  if [[ -n "$body_file" ]]; then
    curl_args+=(-H 'Content-Type: application/json' --data-binary "@$body_file")
  fi

  local curl_error=""
  if ! curl "${curl_args[@]}" > "$meta" 2> "$raw.err"; then
    curl_error="$(tr '\n' ' ' < "$raw.err")"
  fi

  local http_code dns_s connect_s ttft_s total_s
  read -r http_code dns_s connect_s ttft_s total_s < "$meta" || true
  http_code="${http_code:-0}"
  dns_s="${dns_s:-0}"
  connect_s="${connect_s:-0}"
  ttft_s="${ttft_s:-0}"
  total_s="${total_s:-0}"

  jq -cn \
    --arg port "$port" \
    --arg check "$name" \
    --arg path "$path" \
    --argjson http_code "$http_code" \
    --argjson dns_s "$dns_s" \
    --argjson connect_s "$connect_s" \
    --argjson ttft_s "$ttft_s" \
    --argjson total_s "$total_s" \
    --arg error "$curl_error" \
    '{port:($port|tonumber),check:$check,path:$path,http_code:$http_code,dns_s:$dns_s,connect_s:$connect_s,ttft_s:$ttft_s,total_s:$total_s,error:(if $error == "" then null else $error end)}' \
    | tee -a "$RESULTS"
}

PAYLOAD="$OUT_DIR/chat-payload.json"
jq -cn --arg model "$MODEL" \
  '{model:$model,messages:[{role:"user",content:"Reply with exactly OK."}],temperature:0,max_tokens:2,stream:false}' \
  > "$PAYLOAD"

echo "Mac IP smoke test: ${GPU_IP}; ports: ${PORTS}; model: ${MODEL}" >&2
for port in $PORTS; do
  request "$port" "models" "/v1/models" "GET"
  request "$port" "completion" "/v1/chat/completions" "POST" "$PAYLOAD"
done

jq -s \
  --arg gpu_ip "$GPU_IP" \
  --arg model "$MODEL" \
  --arg ts "$(date -Iseconds)" \
  '{timestamp:$ts,gpu_ip:$gpu_ip,model:$model,results:.,passed:(all(.[]; .http_code == 200))}' \
  "$RESULTS" > "$OUT_DIR/summary.json"

jq -r '
  "port  check       http  connect_s  ttft_s  total_s  result",
  (.results[] | [
    (.port|tostring),
    (.check + "       ")[0:10],
    (.http_code|tostring),
    (.connect_s|tostring),
    (.ttft_s|tostring),
    (.total_s|tostring),
    (if .http_code == 200 then "PASS" else "FAIL" end)
  ] | @tsv)
' "$OUT_DIR/summary.json" | column -t

if [[ "$(jq -r '.passed' "$OUT_DIR/summary.json")" != "true" ]]; then
  echo "Smoke test failed. Details: $OUT_DIR/summary.json" >&2
  exit 1
fi

echo "Smoke test passed. Details: $OUT_DIR/summary.json" >&2
