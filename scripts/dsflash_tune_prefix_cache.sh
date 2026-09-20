#!/usr/bin/env bash
# Re-launch the DeepSeek-V4-Flash sglang container with prefix-cache tuning.
#
# Target problem (measured 2026-08-07 on h100 / 192.168.3.205):
#   24.4% of production requests prefill with <10% prefix-cache hit. Their
#   recompute size is p90 143k / p99 280k tokens, and at the measured ~9.8k
#   tok/s prefill rate that is 14.6s / 28.6s to first token. The other 65%
#   hit the radix cache and return in 0.2-0.5s. Hence "sometimes instant,
#   sometimes half a minute".
#
# Changes vs the running baseline (everything else byte-identical):
#   + --enable-hierarchical-cache            evicted prefixes go to host RAM
#   + --hicache-ratio 2                      host KV pool = 2x the device pool
#   + --hicache-write-policy write_through   write prefixes out eagerly
#   ~ --chunked-prefill-size 1024 -> 8192    (max_prefill_tokens is 16384)
#
# NOT --hicache-size: this build rejects it on the DeepSeek V4 path with
#   "DeepSeek V4 HiCache currently does not support --hicache-size; use
#    --hicache-ratio instead"
# (hybrid_pool_assembler.py:260). The --help text claims hicache-size
# overrides hicache-ratio, but that is not true for the DSV4 stack. Learned
# the hard way: the first apply crash-looped on it.
#
# ratio is deliberately the vendor default 2.0 rather than something larger:
# _deepseek_v4_num_host_pages computes host_pages = device_pages * ratio
# linearly, so a big ratio pins a proportionally huge amount of host RAM.
# Raise it only after measuring actual host RAM use at ratio 2.
#
# mem-fraction-static is deliberately LEFT at 0.8: only 13.2 GB per GPU is
# free after allocation, and both hicache transfer buffers and the larger
# prefill chunk want some of it. Raising it is a separate, later experiment.
#
# Attribution: the two changes are measurable independently, so one restart
# still yields clean attribution.
#   chunked-prefill-size -> prefill tok/s on a NOVEL prompt (uncacheable
#                           anywhere, so hicache cannot confound it)
#   hierarchical cache   -> share of production requests with <10% cache hit
#
# Rollback is by container RENAME, not by re-assembling a run command: the
# original container object is preserved untouched as $OLD_NAME.
#
# Usage:
#   ./dsflash_tune_prefix_cache.sh apply
#   ./dsflash_tune_prefix_cache.sh reapply   # retry after a failed apply,
#                                            # reusing the preserved original
#   ./dsflash_tune_prefix_cache.sh rollback
#   ./dsflash_tune_prefix_cache.sh status

set -euo pipefail

NAME=deepseek-v4-dspark
OLD_NAME=deepseek-v4-dspark-pretune
IMAGE=local/sglang-dspark:latest
MODEL_DIR=/home/cltx/deepseek-v4-flash/models/DeepSeek-V4-Flash-0731
HEALTH_URL=http://127.0.0.1:8767/health
READY_TIMEOUT=900   # measured cold start: ~9m20s (00:31:15 -> 00:40:34)

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

wait_ready() {
  local deadline=$((SECONDS + READY_TIMEOUT))
  local down_streak=0
  log "waiting for $HEALTH_URL (timeout ${READY_TIMEOUT}s)..."
  while (( SECONDS < deadline )); do
    if curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      log "READY after ${SECONDS}s"
      return 0
    fi

    # Fast-fail on a config the engine rejects outright, instead of burning the
    # full ~9min model load before finding out. With --restart always a bad
    # config otherwise crash-loops forever.
    if docker logs --tail 200 "$NAME" 2>&1 \
         | grep -qE 'ValueError|Scheduler hit an exception|torch.OutOfMemoryError'; then
      log "FATAL config error detected in $NAME logs:"
      docker logs --tail 200 "$NAME" 2>&1 \
        | grep -E 'ValueError|Scheduler hit an exception|torch.OutOfMemoryError' \
        | head -3 | sed 's/^/    /'
      return 1
    fi

    # Only believe "container is gone" after several consecutive misses: with
    # a restart policy there are legitimate brief windows where the container
    # is between exit and restart, and treating those as fatal produced a
    # false "MANUAL INTERVENTION NEEDED" on 2026-08-07.
    if [[ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" == "true" ]]; then
      down_streak=0
    else
      down_streak=$((down_streak + 1))
      if (( down_streak >= 4 )); then
        log "container not running for 4 consecutive checks; last 40 log lines:"
        docker logs --tail 40 "$NAME" 2>&1 | sed 's/^/    /'
        return 1
      fi
    fi
    sleep 10
  done
  log "TIMEOUT waiting for ready; last 40 log lines:"
  docker logs --tail 40 "$NAME" 2>&1 | sed 's/^/    /'
  return 1
}

start_tuned() {
  log "starting tuned $NAME"
  docker run -d \
    --name "$NAME" \
    --restart always \
    --gpus all \
    --network host \
    --shm-size 32g \
    -v "$MODEL_DIR:/model:ro" \
    -v /home/cltx/deepseek-v4-dspark/logs:/logs \
    -v /home/cltx/deepseek-v4-dspark/cache/deep_gemm:/root/.cache/deep_gemm \
    -e HF_HUB_OFFLINE=1 \
    -e TRANSFORMERS_OFFLINE=1 \
    -e SGLANG_RAGGED_VERIFY_MODE=compact \
    "$IMAGE" \
    python3 -m sglang.launch_server \
      --model-path /model \
      --served-model-name deepseek-v4-flash \
      --host 127.0.0.1 \
      --port 8767 \
      --tp 8 \
      --trust-remote-code \
      --tool-call-parser deepseekv4 \
      --reasoning-parser deepseek-v4 \
      --speculative-algorithm DSPARK \
      --moe-runner-backend flashinfer_mxfp4 \
      --disable-flashinfer-autotune \
      --swa-full-tokens-ratio 0.1 \
      --chunked-prefill-size 8192 \
      --cuda-graph-max-bs 64 \
      --mem-fraction-static 0.8 \
      --context-length 393216 \
      --max-running-requests 64 \
      --watchdog-timeout 300 \
      --enable-hierarchical-cache \
      --hicache-ratio 2 \
      --hicache-write-policy write_through \
      --enable-metrics \
      --log-level info >/dev/null
}

verify_effective() {
  log "verifying effective settings"
  docker logs "$NAME" 2>&1 \
    | grep -oE 'max_total_num_tokens=[0-9]+|chunked_prefill_size=[0-9]+|enable_hierarchical_cache=[A-Za-z]+|hicache_ratio=[0-9.]+' \
    | sort -u | sed 's/^/    /'
}

cmd_apply() {
  docker inspect "$OLD_NAME" >/dev/null 2>&1 \
    && die "$OLD_NAME already exists — a previous apply was not cleaned up. Use 'reapply' to retry, or 'rollback' to restore."
  docker inspect "$NAME" >/dev/null 2>&1 || die "$NAME not found; nothing to tune"
  [[ -d "$MODEL_DIR" ]] || die "model dir missing: $MODEL_DIR"

  # Guard: refuse to touch a container that is not the baseline we analysed.
  local args
  args=$(docker inspect "$NAME" --format '{{range .Args}}{{.}} {{end}}')
  grep -q -- '--chunked-prefill-size 1024' <<<"$args" \
    || die "baseline guard failed: running container does not have --chunked-prefill-size 1024. Args: $args"
  grep -q -- '--speculative-algorithm DSPARK' <<<"$args" \
    || die "baseline guard failed: not the DSPARK deployment. Args: $args"
  if grep -q -- '--enable-hierarchical-cache' <<<"$args"; then
    die "already tuned: --enable-hierarchical-cache present. Nothing to do."
  fi

  docker inspect "$NAME" > "/tmp/${NAME}.pretune.inspect.json"
  log "saved pre-change inspect to /tmp/${NAME}.pretune.inspect.json"

  log "stopping $NAME (service goes down here)"
  docker stop -t 60 "$NAME" >/dev/null
  docker rename "$NAME" "$OLD_NAME"
  log "preserved original container as $OLD_NAME (rollback target)"

  start_tuned

  if ! wait_ready; then
    log "startup FAILED — rolling back automatically"
    cmd_rollback
    return 1
  fi
  verify_effective
  log "apply OK. Original container kept as $OLD_NAME — remove it only after"
  log "the production cache-hit rate has been re-measured."
}

# Retry a tuned launch when apply already preserved the original as $OLD_NAME
# but the tuned container failed to come up. Avoids paying the ~9min model
# load twice (rollback then apply again).
cmd_reapply() {
  docker inspect "$OLD_NAME" >/dev/null 2>&1 \
    || die "no preserved $OLD_NAME — use 'apply' instead"
  if docker inspect "$NAME" >/dev/null 2>&1; then
    if curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      die "$NAME is currently HEALTHY — refusing to replace a serving container. Use rollback first if you really mean it."
    fi
    log "removing failed $NAME"
    docker rm -f "$NAME" >/dev/null
  fi

  start_tuned

  if ! wait_ready; then
    log "reapply FAILED — rolling back to $OLD_NAME"
    cmd_rollback
    return 1
  fi
  verify_effective
  log "reapply OK. Original container kept as $OLD_NAME."
}

cmd_rollback() {
  docker inspect "$OLD_NAME" >/dev/null 2>&1 || die "no $OLD_NAME to roll back to"
  log "rolling back to $OLD_NAME"
  if docker inspect "$NAME" >/dev/null 2>&1; then
    # Preserve the failed container's logs before removing it, otherwise the
    # reason it failed is destroyed along with the container.
    local failed_log="/tmp/${NAME}.failed.$(date +%Y%m%dT%H%M%S).log"
    docker logs "$NAME" > "$failed_log" 2>&1 || true
    log "saved failed container logs to $failed_log"
    docker rm -f "$NAME" >/dev/null
  fi
  docker rename "$OLD_NAME" "$NAME"
  docker start "$NAME" >/dev/null
  wait_ready || die "rollback started but never became ready — MANUAL INTERVENTION NEEDED"
  log "rollback complete, original config restored"
}

cmd_status() {
  docker ps -a --filter "name=deepseek-v4-dspark" \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo "--- effective args ---"
  docker inspect "$NAME" --format '{{range .Args}}{{.}} {{end}}' 2>/dev/null | tr ' ' '\n' | grep -E 'hicache|hierarchical|chunked|mem-fraction' || echo "(none)"
  echo "--- health ---"
  curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1 && echo "healthy" || echo "NOT healthy"
}

case "${1:-}" in
  apply)    cmd_apply ;;
  reapply)  cmd_reapply ;;
  rollback) cmd_rollback ;;
  status)   cmd_status ;;
  *) echo "usage: $0 {apply|reapply|rollback|status}" >&2; exit 2 ;;
esac
