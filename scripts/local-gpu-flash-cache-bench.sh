#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JMS="${JMS:-$ROOT/scripts/jms}"
ASSET="${ASSET:-local-gpu}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
MODEL="${MODEL:-deepseek-v4-flash}"
OUT_DIR="${OUT_DIR:-$ROOT/runs/local-gpu-flash-bench/$(date +%Y%m%dT%H%M%S)}"
RUN_ON="${RUN_ON:-remote}" # remote = run via JMS on local-gpu; mac = run from this Mac.

TEN_K_SERIAL="${TEN_K_SERIAL:-12}"
TEN_K_CONCURRENCY="${TEN_K_CONCURRENCY:-8}"
TEN_K_WAVES="${TEN_K_WAVES:-4}"
HUNDRED_K_SERIAL="${HUNDRED_K_SERIAL:-6}"
HUNDRED_K_CONCURRENCY="${HUNDRED_K_CONCURRENCY:-2}"
HUNDRED_K_WAVES="${HUNDRED_K_WAVES:-3}"
TIMEOUT_S="${TIMEOUT_S:-300}"
CONNECT_TIMEOUT_S="${CONNECT_TIMEOUT_S:-10}"
SKIP_CALIBRATION="${SKIP_CALIBRATION:-0}"
TEN_K_REPEATS="${TEN_K_REPEATS:-156}"
HUNDRED_K_REPEATS="${HUNDRED_K_REPEATS:-1563}"
WARMUP_ROUNDS="${WARMUP_ROUNDS:-2}"
COLLECT_METRICS="${COLLECT_METRICS:-1}"

mkdir -p "$OUT_DIR"

remote_body='
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
MODEL="${MODEL:-deepseek-v4-flash}"
TIMEOUT_S="${TIMEOUT_S:-300}"
CONNECT_TIMEOUT_S="${CONNECT_TIMEOUT_S:-10}"
SKIP_CALIBRATION="${SKIP_CALIBRATION:-0}"
TEN_K_REPEATS="${TEN_K_REPEATS:-156}"
HUNDRED_K_REPEATS="${HUNDRED_K_REPEATS:-1563}"
WARMUP_ROUNDS="${WARMUP_ROUNDS:-2}"
COLLECT_METRICS="${COLLECT_METRICS:-1}"

need() {
  command -v "$1" >/dev/null || {
    echo "missing command on remote: $1" >&2
    exit 1
  }
}
need curl
need jq
need awk
need perl

json_quote() {
  jq -Rs .
}

make_prompt() {
  local repeats="$1"
  local block="CarHer local GPU DeepSeek V4 Flash prefix-cache benchmark payload. The repeated paragraph creates a stable deterministic long prefix for vLLM cache validation. Numbers: 0123456789. Markers: alpha beta gamma delta epsilon zeta eta theta iota kappa. Instruction remains constant across every request.\n"
  awk -v n="$repeats" -v block="$block" '"'"'BEGIN { for (i = 0; i < n; i++) printf "%s", block; print "\nFinal instruction: answer exactly OK." }'"'"'
}

build_payload() {
  local repeats="$1"
  local stream="${2:-false}"
  local prompt_json
  prompt_json="$(make_prompt "$repeats" | json_quote)"
  if [[ "$stream" == "true" ]]; then
    cat <<JSON
{"model":"$MODEL","messages":[{"role":"system","content":"You are a deterministic benchmark assistant."},{"role":"user","content":$prompt_json}],"temperature":0,"max_tokens":1,"stream":true,"stream_options":{"include_usage":true}}
JSON
  else
    cat <<JSON
{"model":"$MODEL","messages":[{"role":"system","content":"You are a deterministic benchmark assistant."},{"role":"user","content":$prompt_json}],"temperature":0,"max_tokens":1}
JSON
  fi
}

post_payload() {
  local payload_file="$1"
  curl -sS --max-time "$TIMEOUT_S" \
    --connect-timeout "$CONNECT_TIMEOUT_S" \
    "$BASE_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    --data-binary "@$payload_file"
}

prompt_tokens_for_repeats() {
  local repeats="$1"
  local payload_file
  payload_file="$(mktemp)"
  build_payload "$repeats" false > "$payload_file"
  post_payload "$payload_file" | jq -r ".usage.prompt_tokens // 0"
  rm -f "$payload_file"
}

calibrate_repeats() {
  local target="$1"
  local lo=1
  local hi=$((target / 40))
  [[ "$hi" -lt 16 ]] && hi=16

  local tokens
  while true; do
    tokens="$(prompt_tokens_for_repeats "$hi")"
    if [[ "$tokens" -ge "$target" || "$hi" -gt 20000 ]]; then
      break
    fi
    hi=$((hi * 2))
  done

  while [[ "$lo" -lt "$hi" ]]; do
    local mid=$(((lo + hi) / 2))
    tokens="$(prompt_tokens_for_repeats "$mid")"
    if [[ "$tokens" -lt "$target" ]]; then
      lo=$((mid + 1))
    else
      hi="$mid"
    fi
  done
  echo "$hi"
}

snapshot_metrics() {
  local file="$1"
  if [[ "$COLLECT_METRICS" != "1" ]]; then
    : > "$file"
    return 0
  fi
  curl -sS --max-time 30 "$BASE_URL/metrics" \
    --connect-timeout "$CONNECT_TIMEOUT_S" \
    | grep -E "^(vllm:(prefix_cache_queries_total|prefix_cache_hits_total|prompt_tokens_total|prompt_tokens_cached_total|generation_tokens_total|num_preemptions_total|num_requests_running|num_requests_waiting|kv_cache_usage_perc|time_to_first_token_seconds_(sum|count)|e2e_request_latency_seconds_(sum|count)|request_queue_time_seconds_(sum|count)|request_prefill_time_seconds_(sum|count)|request_decode_time_seconds_(sum|count)|request_prefill_kv_computed_tokens_(sum|count))(\\{| )|vllm:(time_to_first_token_seconds|request_queue_time_seconds|request_prefill_time_seconds|request_prefill_kv_computed_tokens)_bucket\\{)" \
    > "$file" || true
}

metric_sum() {
  local file="$1"
  local metric="$2"
  awk -v m="$metric" '"'"'$1 ~ ("^" m "(\\{|$)") { sum += $NF } END { printf "%.12f", sum + 0 }'"'"' "$file"
}

metric_delta() {
  local before="$1"
  local after="$2"
  local metric="$3"
  awk -v m="$metric" '"'"'
    FNR == NR {
      if ($1 ~ ("^" m "(\\{|$)")) before += $NF
      next
    }
    $1 ~ ("^" m "(\\{|$)") { after += $NF }
    END { printf "%.12f", after - before }
  '"'"' "$before" "$after"
}

hist_all_bucket_delta() {
  local before="$1"
  local after="$2"
  local metric="$3"
  if [[ "$COLLECT_METRICS" != "1" ]]; then
    echo ""
    return 0
  fi
  awk -v m="$metric" '"'"'
    function le_value(label, raw) {
      if (match(label, /le="([^"]+)"/, a)) {
        raw = a[1]
        if (raw == "+Inf") return 1e99
        return raw + 0
      }
      return -1
    }
    FNR == NR {
      if ($1 ~ ("^" m "_bucket\\{")) {
        key = le_value($1)
        b[key] += $NF
        label[key] = gensub(/^.*le="([^"]+)".*$/, "\\1", 1, $1)
      }
      next
    }
    $1 ~ ("^" m "_bucket\\{") {
      key = le_value($1)
      a[key] += $NF
      label[key] = gensub(/^.*le="([^"]+)".*$/, "\\1", 1, $1)
    }
    END {
      total = 0
      for (k in a) {
        d = a[k] - b[k]
        delta[k] = d
        if (d > total) total = d
      }
      if (total <= 0) {
        print ""
        exit
      }
      best = 1e100
      best_label = ""
      for (k in delta) {
        if (delta[k] >= total && (k + 0) < best) {
          best = k + 0
          best_label = label[k]
        }
      }
      print best_label
    }
  '"'"' "$before" "$after"
}

diff_metrics_json() {
  local before="$1"
  local after="$2"
  local q h
  q="$(metric_delta "$before" "$after" "vllm:prefix_cache_queries_total")"
  h="$(metric_delta "$before" "$after" "vllm:prefix_cache_hits_total")"
  jq -cn \
    --argjson q "$q" \
    --argjson h "$h" \
    --argjson prompt "$(metric_delta "$before" "$after" "vllm:prompt_tokens_total")" \
    --argjson cached "$(metric_delta "$before" "$after" "vllm:prompt_tokens_cached_total")" \
    --argjson gen "$(metric_delta "$before" "$after" "vllm:generation_tokens_total")" \
    --argjson queue "$(metric_delta "$before" "$after" "vllm:request_queue_time_seconds_sum")" \
    --argjson prefill "$(metric_delta "$before" "$after" "vllm:request_prefill_time_seconds_sum")" \
    --argjson decode "$(metric_delta "$before" "$after" "vllm:request_decode_time_seconds_sum")" \
    --argjson computed "$(metric_delta "$before" "$after" "vllm:request_prefill_kv_computed_tokens_sum")" \
    --argjson preempt "$(metric_delta "$before" "$after" "vllm:num_preemptions_total")" \
    --argjson running "$(metric_sum "$before" "vllm:num_requests_running")" \
    --argjson waiting "$(metric_sum "$before" "vllm:num_requests_waiting")" \
    --argjson kv "$(metric_sum "$before" "vllm:kv_cache_usage_perc")" \
    --arg hist_ttft "$(hist_all_bucket_delta "$before" "$after" "vllm:time_to_first_token_seconds")" \
    --arg hist_queue "$(hist_all_bucket_delta "$before" "$after" "vllm:request_queue_time_seconds")" \
    --arg hist_prefill "$(hist_all_bucket_delta "$before" "$after" "vllm:request_prefill_time_seconds")" \
    --arg hist_kv "$(hist_all_bucket_delta "$before" "$after" "vllm:request_prefill_kv_computed_tokens")" \
    '"'"'{
      cache_query_tokens_delta: $q,
      cache_hit_tokens_delta: $h,
      cache_hit_pct: (if $q > 0 then ($h / $q * 100) else null end),
      prompt_tokens_delta: $prompt,
      prompt_cached_delta: $cached,
      generation_tokens_delta: $gen,
      queue_sum_delta_s: $queue,
      prefill_sum_delta_s: $prefill,
      decode_sum_delta_s: $decode,
      computed_kv_tokens_sum_delta: $computed,
      preemptions_delta: $preempt,
      running_before: $running,
      waiting_before: $waiting,
      kv_usage_before: $kv,
      hist_ttft_le_all: $hist_ttft,
      hist_queue_le_all: $hist_queue,
      hist_prefill_le_all: $hist_prefill,
      hist_computed_kv_le_all: $hist_kv
    }'"'"'
}

stream_request() {
  local case_name="$1"
  local mode="$2"
  local idx="$3"
  local payload_file="$4"
  local out_dir="$5"
  local raw="$out_dir/raw-${case_name}-${mode}-${idx}.sse"
  local meta="$out_dir/curl-${case_name}-${mode}-${idx}.txt"

  local start_ns end_ns total_s ttft_s status wall_s
  start_ns="$(date +%s%N)"
  curl -sS --max-time "$TIMEOUT_S" \
    --connect-timeout "$CONNECT_TIMEOUT_S" \
    "$BASE_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    --data-binary "@$payload_file" \
    -w "\n__CURL_HTTP_CODE__=%{http_code}\n__CURL_TIME_STARTTRANSFER__=%{time_starttransfer}\n__CURL_TIME_TOTAL__=%{time_total}\n" \
    > "$raw" 2> "$raw.err" || true
  end_ns="$(date +%s%N)"

  grep "^__CURL_" "$raw" > "$meta" || true
  status="$(grep "__CURL_HTTP_CODE__=" "$meta" | tail -1 | cut -d= -f2)"
  ttft_s="$(grep "__CURL_TIME_STARTTRANSFER__=" "$meta" | tail -1 | cut -d= -f2)"
  total_s="$(grep "__CURL_TIME_TOTAL__=" "$meta" | tail -1 | cut -d= -f2)"
  wall_s="${total_s:-0}"
  grep -v "^__CURL_" "$raw" > "$raw.body" || true
  mv "$raw.body" "$raw"

  local usage_prompt
  usage_prompt="$(grep "^data: " "$raw" | sed "s/^data: //" | grep -v "^\[DONE\]$" | jq -r "select(.usage? != null) | .usage.prompt_tokens" 2>/dev/null | tail -1 || true)"
  [[ -z "$usage_prompt" ]] && usage_prompt="null"
  local error_json
  error_json="null"
  if [[ "${status:-000}" != "200" || "${ttft_s:-0}" == "0.000000" ]]; then
    error_json="$(cat "$raw.err" 2>/dev/null | jq -Rs .)"
  fi

  jq -cn \
    --arg case "$case_name" \
    --arg mode "$mode" \
    --argjson idx "$idx" \
    --arg status "${status:-000}" \
    --argjson ttft "${ttft_s:-0}" \
    --argjson total "${total_s:-0}" \
    --argjson wall "$wall_s" \
    --argjson usage_prompt "$usage_prompt" \
    --argjson error "$error_json" \
    '"'"'{
      event: "request",
      case: $case,
      mode: $mode,
      idx: $idx,
      status: ($status | tonumber? // 0),
      curl_ttft_s: $ttft,
      curl_total_s: $total,
      wall_total_s: $wall,
      usage_prompt_tokens: $usage_prompt,
      error: $error
    }'"'"'
}

summary_json() {
  local case_name="$1"
  local mode="$2"
  local prompt_tokens="$3"
  local rows_file="$4"
  local metrics_json="$5"
  jq -cn \
    --arg case "$case_name" \
    --arg mode "$mode" \
    --argjson prompt_tokens "$prompt_tokens" \
    --slurpfile rows "$rows_file" \
    --argjson metrics "$metrics_json" \
    '"'"'
    def pct($xs; $p):
      ($xs | sort) as $s
      | ($s | length) as $n
      | if $n == 0 then null
        else (($n - 1) * $p / 100) as $k
        | ($k | floor) as $f
        | ([$f + 1, $n - 1] | min) as $c
        | if $f == $c then $s[$f]
          else ($s[$f] * ($c - $k) + $s[$c] * ($k - $f))
          end
        end;
    ($rows // []) as $r
    | ($r | map(select(.status == 200))) as $ok
    | ($ok | map(.curl_ttft_s)) as $ttft
    | ($ok | map(.curl_total_s)) as $total
    | {
        event: "summary",
        case: $case,
        mode: $mode,
        prompt_tokens: $prompt_tokens,
        ok: ($ok | length),
        n: ($r | length),
        ttft_min_s: ($ttft | min),
        ttft_p50_s: pct($ttft; 50),
        ttft_p95_s: pct($ttft; 95),
        ttft_max_s: ($ttft | max),
        total_p50_s: pct($total; 50),
        total_max_s: ($total | max),
        slow_gt_3s: ($ttft | map(select(. > 3)) | length),
        slow_gt_10s: ($ttft | map(select(. > 10)) | length)
      } + $metrics
    '"'"'
}

run_case() {
  local case_name="$1"
  local target="$2"
  local serial_rounds="$3"
  local concurrency="$4"
  local waves="$5"
  local out_dir="$6"

  mkdir -p "$out_dir"
  echo "{\"event\":\"case_start\",\"case\":\"$case_name\",\"target_tokens\":$target,\"serial_rounds\":$serial_rounds,\"concurrency\":$concurrency,\"waves\":$waves}"

  local repeats prompt_tokens payload payload_stream
  if [[ "$SKIP_CALIBRATION" == "1" ]]; then
    if [[ "$case_name" == "10k" ]]; then
      repeats="$TEN_K_REPEATS"
    elif [[ "$case_name" == "100k" ]]; then
      repeats="$HUNDRED_K_REPEATS"
    else
      echo "unknown case for skipped calibration: $case_name" >&2
      exit 1
    fi
    prompt_tokens="$target"
  else
    repeats="$(calibrate_repeats "$target")"
    prompt_tokens="$(prompt_tokens_for_repeats "$repeats")"
  fi
  payload="$out_dir/payload-${case_name}.json"
  payload_stream="$out_dir/payload-${case_name}-stream.json"
  build_payload "$repeats" false > "$payload"
  build_payload "$repeats" true > "$payload_stream"
  echo "{\"event\":\"calibrated\",\"case\":\"$case_name\",\"target_tokens\":$target,\"repeats\":$repeats,\"prompt_tokens\":$prompt_tokens}"

  if [[ "$WARMUP_ROUNDS" -gt 0 ]]; then
    for i in $(seq 1 "$WARMUP_ROUNDS"); do
      local start total status usage
      start="$(date +%s%N)"
      local body="$out_dir/warmup-${case_name}-${i}.json"
      post_payload "$payload" > "$body" 2> "$body.err" || true
      total="$(awk -v s="$start" -v e="$(date +%s%N)" '"'"'BEGIN { printf "%.6f", (e - s) / 1000000000 }'"'"')"
      status="$(jq -r "if .id then 200 else 0 end" "$body" 2>/dev/null || true)"
      usage="$(jq -r ".usage.prompt_tokens // null" "$body" 2>/dev/null || true)"
      [[ "$status" =~ ^[0-9]+$ ]] || status=0
      [[ "$usage" =~ ^[0-9]+$ ]] || usage=null
      jq -cn --arg case "$case_name" --argjson idx "$i" --argjson status "$status" --argjson total "$total" --argjson usage "$usage" \
        '"'"'{event:"warmup", case:$case, idx:$idx, status:$status, total_s:$total, prompt_tokens:$usage}'"'"'
      sleep 0.8
    done
  fi

  local serial_rows="$out_dir/${case_name}-serial-rows.json"
  : > "$serial_rows"
  snapshot_metrics "$out_dir/${case_name}-serial-before.prom"
  for i in $(seq 1 "$serial_rounds"); do
    stream_request "$case_name" "serial" "$i" "$payload_stream" "$out_dir" | tee -a "$serial_rows"
    sleep 0.8
  done
  snapshot_metrics "$out_dir/${case_name}-serial-after.prom"
  local serial_metrics
  serial_metrics="$(diff_metrics_json "$out_dir/${case_name}-serial-before.prom" "$out_dir/${case_name}-serial-after.prom")"
  summary_json "$case_name" "serial" "$prompt_tokens" "$serial_rows" "$serial_metrics"

  local concurrent_rows="$out_dir/${case_name}-concurrent-rows.json"
  : > "$concurrent_rows"
  snapshot_metrics "$out_dir/${case_name}-concurrent-before.prom"
  local idx=0
  for wave in $(seq 1 "$waves"); do
    local tmp_dir="$out_dir/tmp-${case_name}-wave-${wave}"
    mkdir -p "$tmp_dir"
    for slot in $(seq 1 "$concurrency"); do
      idx=$((idx + 1))
      stream_request "$case_name" "concurrent" "$idx" "$payload_stream" "$out_dir" > "$tmp_dir/$slot.json" &
    done
    wait
    jq -s --arg case "$case_name" --argjson wave "$wave" --argjson concurrency "$concurrency" \
      '"'"'{
        event:"wave",
        case:$case,
        wave:$wave,
        concurrency:$concurrency,
        ok:(map(select(.status == 200)) | length),
        n:length,
        ttft_min_s:(map(select(.status == 200) | .curl_ttft_s) | min),
        ttft_max_s:(map(select(.status == 200) | .curl_ttft_s) | max)
      }'"'"' "$tmp_dir"/*.json
    cat "$tmp_dir"/*.json >> "$concurrent_rows"
    rm -rf "$tmp_dir"
    sleep 1
  done
  snapshot_metrics "$out_dir/${case_name}-concurrent-after.prom"
  local concurrent_metrics
  concurrent_metrics="$(diff_metrics_json "$out_dir/${case_name}-concurrent-before.prom" "$out_dir/${case_name}-concurrent-after.prom")"
  summary_json "$case_name" "concurrent" "$prompt_tokens" "$concurrent_rows" "$concurrent_metrics"
}

echo "{\"event\":\"bench_start\",\"base_url\":\"$BASE_URL\",\"model\":\"$MODEL\",\"remote_host\":\"$(hostname)\",\"ts\":\"$(date -Iseconds)\"}"
run_case "10k" 10000 "$TEN_K_SERIAL" "$TEN_K_CONCURRENCY" "$TEN_K_WAVES" "$REMOTE_OUT_DIR"
run_case "100k" 100000 "$HUNDRED_K_SERIAL" "$HUNDRED_K_CONCURRENCY" "$HUNDRED_K_WAVES" "$REMOTE_OUT_DIR"
echo "{\"event\":\"bench_done\",\"ts\":\"$(date -Iseconds)\"}"
'

REMOTE_OUT_DIR="/tmp/local-gpu-flash-bench-$(date +%Y%m%dT%H%M%S)"
LOG_FILE="$OUT_DIR/bench.jsonl"
remote_script="$(cat <<EOF
BASE_URL=$(printf '%q' "$BASE_URL")
MODEL=$(printf '%q' "$MODEL")
TIMEOUT_S=$(printf '%q' "$TIMEOUT_S")
CONNECT_TIMEOUT_S=$(printf '%q' "$CONNECT_TIMEOUT_S")
SKIP_CALIBRATION=$(printf '%q' "$SKIP_CALIBRATION")
TEN_K_REPEATS=$(printf '%q' "$TEN_K_REPEATS")
HUNDRED_K_REPEATS=$(printf '%q' "$HUNDRED_K_REPEATS")
WARMUP_ROUNDS=$(printf '%q' "$WARMUP_ROUNDS")
COLLECT_METRICS=$(printf '%q' "$COLLECT_METRICS")
TEN_K_SERIAL=$(printf '%q' "$TEN_K_SERIAL")
TEN_K_CONCURRENCY=$(printf '%q' "$TEN_K_CONCURRENCY")
TEN_K_WAVES=$(printf '%q' "$TEN_K_WAVES")
HUNDRED_K_SERIAL=$(printf '%q' "$HUNDRED_K_SERIAL")
HUNDRED_K_CONCURRENCY=$(printf '%q' "$HUNDRED_K_CONCURRENCY")
HUNDRED_K_WAVES=$(printf '%q' "$HUNDRED_K_WAVES")
REMOTE_OUT_DIR=$(printf '%q' "$REMOTE_OUT_DIR")
$remote_body
EOF
)"

if [[ "$RUN_ON" == "mac" ]]; then
  echo "Running shell benchmark on this Mac; local log: $LOG_FILE" >&2
  bash -s <<< "$remote_script" | tee "$LOG_FILE"
else
  echo "Running shell benchmark on $ASSET; local log: $LOG_FILE" >&2
  "$JMS" ssh "$ASSET" "bash -s" <<< "$remote_script" | tee "$LOG_FILE"
fi

echo "Saved JSONL result to $LOG_FILE" >&2
