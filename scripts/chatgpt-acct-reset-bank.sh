#!/usr/bin/env bash
# chatgpt-acct-reset-bank.sh — batch driver for ChatGPT banked rate-limit reset.
#
# Reads/writes per-pod via kubectl exec; will:
#   probe   ACCT...    one-line credit/usage summary per acct (no mutation)
#   redeem  ACCT...    POST /wham/.../consume only when credits>=1 AND 7d>=80
#   sweep              probe every running deploy in 198 K3s
#   rescue             scale=0 → probe → redeem-if-100% → leave scale=1 / scale-back-0
#
# Pre-conds:
#   - jms target AIYJY-litellm reachable (198 K3s).
#   - Pods have /chatgpt-auth/auth.json + /app/.venv/bin/python3.
#   - This script ships chatgpt-acct-reset-bank.py into the pod via kubectl cp.
#
# Memory guard: 198 prod node sometimes sits at 60+G/63G. `rescue` mode runs
# serially and refuses to start the next deploy if `free -g` reports avail<3G.
#
# Examples:
#   ./scripts/chatgpt-acct-reset-bank.sh sweep
#   ./scripts/chatgpt-acct-reset-bank.sh probe 17 22 35 36
#   ./scripts/chatgpt-acct-reset-bank.sh redeem 29
#   ./scripts/chatgpt-acct-reset-bank.sh rescue 32 34 39 41 42 43 44

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JMS="$SCRIPT_DIR/jms"
[[ -x "$JMS" ]] || JMS="jms"

NS="${CHATGPT_ACCT_NS:-litellm-product}"
HOST="${CHATGPT_ACCT_HOST:-AIYJY-litellm}"
REMOTE_PY="/tmp/chatgpt-acct-reset-bank.py"
LOCAL_PY="$SCRIPT_DIR/chatgpt-acct-reset-bank.py"
MIN_AVAIL_GB="${MIN_AVAIL_GB:-3}"
WAIT_READY_SECS="${WAIT_READY_SECS:-120}"

usage() {
  cat <<EOF >&2
Usage: $0 <mode> [ACCT...]
Modes:
  sweep             probe every running chatgpt-acct deploy
  probe  ACCT...    one-line credit/usage summary per acct
  redeem ACCT...    in-place redeem if credits>=1 && 7d>=80
  rescue ACCT...    scale=0→probe→redeem-or-scale-back
Env:
  CHATGPT_ACCT_NS=$NS
  CHATGPT_ACCT_HOST=$HOST
  MIN_AVAIL_GB=$MIN_AVAIL_GB
EOF
  exit 2
}

ssh198() { "$JMS" ssh "$HOST" "$@"; }

ship_py() {
  local pod="$1"
  "$JMS" scp "$LOCAL_PY" "$HOST:$REMOTE_PY" >/dev/null 2>&1 || true
  ssh198 "kubectl -n $NS cp $REMOTE_PY $pod:/tmp/reset.py" >/dev/null 2>&1
}

pod_of() {
  local n="$1" pod=""
  for _try in 1 2 3; do
    pod=$(ssh198 "kubectl -n $NS get pod -l app=chatgpt-acct-$n -o jsonpath='{.items[0].metadata.name}'" 2>/dev/null)
    [[ -n "$pod" ]] && { echo "$pod"; return 0; }
    sleep 3
  done
  return 1
}

exec_py() {
  local pod="$1" mode="$2"
  ssh198 "kubectl -n $NS exec $pod -- /app/.venv/bin/python3 /tmp/reset.py $mode" 2>&1
}

mem_ok() {
  local avail
  avail=$(ssh198 "free -g | awk 'NR==2 {print \$7}'" 2>/dev/null | tr -d ' ')
  [[ -n "$avail" && "$avail" -ge "$MIN_AVAIL_GB" ]] 2>/dev/null
}

wait_ready() {
  local n="$1"
  local i
  for ((i=0; i<WAIT_READY_SECS; i+=4)); do
    local ready
    ready=$(ssh198 "kubectl -n $NS get pod -l app=chatgpt-acct-$n -o jsonpath='{.items[0].status.containerStatuses[0].ready}'" 2>/dev/null)
    [[ "$ready" == "true" ]] && return 0
    sleep 4
  done
  return 1
}

mode_probe_acct() {
  local n="$1"
  local pod
  pod=$(pod_of "$n")
  if [[ -z "$pod" ]]; then
    local repl
    repl=$(ssh198 "kubectl -n $NS get deploy chatgpt-acct-$n -o jsonpath='{.spec.replicas}'" 2>/dev/null)
    printf 'acct-%-3s NO_POD scale=%s\n' "$n" "${repl:-?}"
    return
  fi
  ship_py "$pod"
  local out
  out=$(exec_py "$pod" probe)
  printf 'acct-%-3s %s\n' "$n" "$out"
}

mode_redeem_acct() {
  local n="$1"
  local pod
  pod=$(pod_of "$n")
  if [[ -z "$pod" ]]; then
    echo "acct-$n NO_POD"; return
  fi
  ship_py "$pod"
  echo "=== acct-$n ==="
  exec_py "$pod" redeem
}

mode_sweep() {
  local deploys
  deploys=$(ssh198 "kubectl -n $NS get deploy -o name 2>&1" | grep chatgpt-acct- | sed 's|.*/chatgpt-acct-||' | sort -n)
  for n in $deploys; do
    mode_probe_acct "$n"
  done
}

mode_rescue_acct() {
  local n="$1"
  if ! mem_ok; then
    echo "acct-$n SKIP mem<${MIN_AVAIL_GB}G"
    return
  fi
  local repl
  repl=$(ssh198 "kubectl -n $NS get deploy chatgpt-acct-$n -o jsonpath='{.spec.replicas}'" 2>/dev/null)
  if [[ "$repl" != "0" ]]; then
    echo "acct-$n already scale=$repl - probing in place"
    mode_probe_acct "$n"
    return
  fi
  ssh198 "kubectl -n $NS scale deploy chatgpt-acct-$n --replicas=1" >/dev/null
  if ! wait_ready "$n"; then
    echo "acct-$n NOT_READY in ${WAIT_READY_SECS}s — leaving scale=1"
    return
  fi
  local pod
  pod=$(pod_of "$n")
  ship_py "$pod"
  local probe
  probe=$(exec_py "$pod" probe)
  echo "acct-$n $probe"
  local creds seven
  creds=$(printf '%s' "$probe" | python3 -c "import json,sys;
try: d=json.loads(sys.stdin.read().strip()); print(d.get('credits',0) or 0)
except: print(0)")
  seven=$(printf '%s' "$probe" | python3 -c "import json,sys;
try: d=json.loads(sys.stdin.read().strip()); print(d.get('7d',0) or 0)
except: print(0)")
  if [[ "${creds:-0}" -ge 1 ]] 2>/dev/null && [[ "${seven:-0}" -ge 80 ]] 2>/dev/null; then
    echo "→ REDEEM credits=$creds 7d=$seven"
    exec_py "$pod" redeem
    echo "→ leaving scale=1 for router"
  else
    echo "→ scale back to 0 (credits=$creds 7d=$seven)"
    ssh198 "kubectl -n $NS scale deploy chatgpt-acct-$n --replicas=0" >/dev/null
  fi
}

main() {
  [[ $# -ge 1 ]] || usage
  local mode="$1"; shift
  case "$mode" in
    sweep)  mode_sweep ;;
    probe)  for n in "$@"; do mode_probe_acct "$n"; done ;;
    redeem) for n in "$@"; do mode_redeem_acct "$n"; done ;;
    rescue) for n in "$@"; do mode_rescue_acct "$n"; done ;;
    *) usage ;;
  esac
}

main "$@"
