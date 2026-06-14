#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-carher}"
FROM_HER_ID="${FROM_HER_ID:-268}"
PEER_URL="${PEER_URL:-}"
EXPECT_TEXT="${EXPECT_TEXT:-A2A_OK}"
MESSAGE="${MESSAGE:-请只回复：$EXPECT_TEXT}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-240}"

usage() {
  cat <<'USAGE'
Usage:
  FROM_HER_ID=268 \
  PEER_URL=http://carher-266-svc.carher.svc.cluster.local:18800 \
  EXPECT_TEXT=A2A_OK \
  scripts/her266-h75/50-a2a-functional-probe.sh

Runs a real A2A JSON-RPC message from one Her pod to a peer URL and verifies
the expected text appears in the peer response. This is intentionally stronger
than checking /.well-known/agent-card.json.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "$PEER_URL" ]]; then
  echo "PEER_URL is required" >&2
  usage >&2
  exit 2
fi

pod="$(
  kubectl -n "$NS" get pods -l "app=carher-user,user-id=$FROM_HER_ID" \
    -o jsonpath='{range .items[*]}{.metadata.creationTimestamp}{"\t"}{.metadata.name}{"\t"}{.metadata.deletionTimestamp}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
    | awk -F '\t' '$3 == "" && $4 == "True" {print $1 "\t" $2}' \
    | sort -r \
    | awk -F '\t' 'NR == 1 {print $2}'
)"

if [[ -z "$pod" ]]; then
  echo "no ready source pod for her-$FROM_HER_ID" >&2
  exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

kubectl -n "$NS" exec "$pod" -c carher -- \
  timeout "$TIMEOUT_SECONDS" \
  python3 /opt/hermestest/scripts/a2a-send-hermes.py \
    --peer-url "$PEER_URL" \
    --message "$MESSAGE" \
  | tee "$tmp"

if grep -Fq "$EXPECT_TEXT" "$tmp"; then
  echo "[OK] A2A functional probe matched $EXPECT_TEXT"
else
  echo "[FAIL] A2A functional probe did not match $EXPECT_TEXT" >&2
  exit 1
fi
