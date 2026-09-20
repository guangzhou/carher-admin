#!/usr/bin/env bash
# Regression matrix for her-facing public model names on the Aliyun LiteLLM proxy.
#
# Why this script exists
# ----------------------
# 2026-09-17: after adding `her-pro` / `her-flash` to 420 carher-* keys, every
# cheap ruler lied at least once:
#
#   * the proxy runs 2 replicas and each caches key metadata independently. A
#     write is honored by one pod immediately and by the other ~2 minutes later.
#     Probing ONE pod -> 50% chance of a false red (or a false green). So the
#     matrix is pods x keys x models, never a single shot.
#   * `/v1/models` and `/model/info` list names that 400 when actually called.
#     Only a real request whose UNIQUE NONCE comes back proves the leg works.
#   * her keys are stored hashed in the DB. The plaintext lives only in the
#     per-instance ConfigMap `carher-<uid>-user-config`. Using the DB `token`
#     as a Bearer gives 401 -- that is a bad credential, not a broken alias.
#   * reasoning models (her-flash) burn the whole budget on reasoning when
#     max_tokens is small: `finish_reason=length` and content EMPTY. Judge on
#     the parsed `choices[0].message.content`, and give it room (>=256).
#   * a negative control is mandatory. All-positive is a synthetic green: it
#     cannot tell "the alias works" from "this key is unrestricted".
#
# Footprint: read-only. Port-forwards, GETs and POSTs. Writes nothing.
#
# Usage (on a host with kubectl reaching the Aliyun cluster, e.g. k8s-work-226):
#   ./litellm-her-key-model-regress.sh --uids 425,1000 \
#       --models her-pro,her-flash,deepseek-v4-flash --negative auto
#
#   --uids       carher instance uids whose real keys get probed (>=2: one key
#                proves nothing about the fleet)
#   --models     public names expected to WORK (each must echo its nonce)
#   --negative   name expected to be REFUSED with 403 key_model_access_denied
#   --ns         default carher
#
# Exit 0 only if every positive cell echoed its nonce on every pod AND every
# negative cell was refused. A cell with zero samples is a FAIL, not a pass.
set -uo pipefail

NS=carher
UIDS=""
MODELS=""
NEGATIVE=""
MAXTOK=256
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ns) NS=$2; shift 2;;
    --uids) UIDS=$2; shift 2;;
    --models) MODELS=$2; shift 2;;
    --negative) NEGATIVE=$2; shift 2;;
    --max-tokens) MAXTOK=$2; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 64;;
  esac
done
[[ -n $UIDS && -n $MODELS ]] || { echo "need --uids and --models" >&2; exit 64; }

fail=0
note() { printf '%s\n' "$*"; }

# ---- phase 1: config convergence. A pod serving the old config explains a
# ---- 400 without any key being wrong, so establish this BEFORE probing.
note "== config convergence (per-pod sha256 of /app/config.yaml vs ConfigMap) =="
cm_sha=$(kubectl -n "$NS" get cm litellm-config -o go-template='{{index .data "config.yaml"}}' \
         | shasum -a 256 | awk '{print $1}')
pods=$(kubectl -n "$NS" get pods -l app=litellm-proxy --no-headers \
       | awk '$2=="1/1" && $3=="Running" {print $1}')
[[ -n $pods ]] || { note "FAIL: zero ready litellm-proxy pods"; exit 2; }
for pod in $pods; do
  pod_sha=$(kubectl -n "$NS" exec "$pod" -c litellm -- sha256sum /app/config.yaml 2>/dev/null | awk '{print $1}')
  n=$(kubectl -n "$NS" exec "$pod" -c litellm -- grep -c 'model_name:' /app/config.yaml 2>/dev/null)
  if [[ $pod_sha == "$cm_sha" ]]; then note "  OK   $pod  entries=$n"
  else note "  DRIFT $pod  $pod_sha != cm $cm_sha (this pod still runs the old config)"; fail=1; fi
done
note "  cm sha=$cm_sha"

# ---- phase 2: pull each uid's PLAINTEXT key out of its instance ConfigMap.
declare -A KEY
for uid in ${UIDS//,/ }; do
  k=$(kubectl -n "$NS" get cm "carher-${uid}-user-config" \
        -o go-template='{{index .data "openclaw.json"}}' 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["models"]["providers"]["litellm"]["apiKey"])' 2>/dev/null)
  if [[ -z $k ]]; then note "FAIL: no plaintext key in cm carher-${uid}-user-config"; fail=1; continue; fi
  KEY[$uid]=$k
done

# probe <pod> <key> <model> -> prints "HTTP <code> id=<model-id> nonce=<yes|no>"
probe() {
  local pod=$1 key=$2 model=$3 nonce=$4 port=$((20000 + RANDOM % 20000))
  kubectl -n "$NS" port-forward "pod/$pod" "$port:4000" >/dev/null 2>&1 &
  local pf=$!
  for _ in $(seq 30); do curl -sf -o /dev/null "http://127.0.0.1:$port/health/liveliness" && break; sleep 0.3; done
  local body code
  body=$(curl -s -w '\n%{http_code}' -m 180 "http://127.0.0.1:$port/v1/chat/completions" \
    -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
    -D /tmp/hdr.$$ -d "{\"model\":\"$model\",\"max_tokens\":$MAXTOK,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly this and nothing else: $nonce\"}]}")
  code=$(tail -1 <<<"$body")
  local id; id=$(awk 'tolower($1)=="x-litellm-model-id:"{print $2}' /tmp/hdr.$$ | tr -d '\r')
  # content, not the raw body: the nonce also appears in the echoed prompt on
  # some error shapes, and grepping the whole body would read that as success.
  local content; content=$(head -n -1 <<<"$body" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit
c=(d.get("choices") or [{}])[0]
print((c.get("message") or {}).get("content") or "", "|finish=", c.get("finish_reason"))' 2>/dev/null)
  kill $pf 2>/dev/null; rm -f /tmp/hdr.$$
  printf 'HTTP %s id=%s content=%s\n' "$code" "${id:--}" "${content:--}"
  [[ $content == *"$nonce"* ]] && return 0 || return 1
}

note ""
note "== positive cells (must echo the nonce on EVERY pod) =="
for pod in $pods; do
  for uid in ${UIDS//,/ }; do
    [[ -n ${KEY[$uid]:-} ]] || continue
    for m in ${MODELS//,/ }; do
      nonce="rg-$uid-$m-$RANDOM$RANDOM"
      out=$(probe "$pod" "${KEY[$uid]}" "$m" "$nonce"); rc=$?
      note "  $( ((rc==0)) && echo PASS || { echo FAIL; fail=1; } )  pod=$pod uid=$uid model=$m  $out"
    done
  done
done

if [[ -n $NEGATIVE ]]; then
  note ""
  note "== negative control ($NEGATIVE must be refused 403; a 200 here voids every green above) =="
  for pod in $pods; do
    for uid in ${UIDS//,/ }; do
      [[ -n ${KEY[$uid]:-} ]] || continue
      out=$(probe "$pod" "${KEY[$uid]}" "$NEGATIVE" "neg-$RANDOM")
      if [[ $out == *"HTTP 403"* || $out == *"HTTP 400"* ]]; then note "  PASS  pod=$pod uid=$uid  $out"
      else note "  FAIL  pod=$pod uid=$uid refused-expected  $out"; fail=1; fi
    done
  done
fi

note ""
if ((fail)); then
  note "RESULT: FAIL. If a fresh key write is <2 min old, the lagging pod is the key"
  note "        metadata cache, not a write failure -- re-run before concluding."
  exit 1
fi
note "RESULT: PASS (all positive cells echoed their nonce, negative control refused)"
