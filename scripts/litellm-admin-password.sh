#!/usr/bin/env bash
# Fetch LiteLLM Admin UI credentials from K8s on 198 (AIYJY-litellm).
# UI login uses username "admin" + LITELLM_MASTER_KEY as password.
#
# Usage:
#   ./litellm-admin-password.sh           # show both dev and prod
#   ./litellm-admin-password.sh dev       # only dev
#   ./litellm-admin-password.sh prod|pro  # only prod

set -euo pipefail

target="${1:-all}"

fetch() {
  local env_name="$1" ns="$2" url="$3"
  local key
  key=$(jms ssh AIYJY-litellm \
    "kubectl exec -n $ns deploy/litellm-proxy -- printenv LITELLM_MASTER_KEY 2>/dev/null" \
    | tr -d '\r' | tail -1)

  if [[ -z "$key" ]]; then
    echo "[$env_name] ERROR: LITELLM_MASTER_KEY not found in $ns/litellm-proxy" >&2
    return 1
  fi

  printf '== %s ==\n' "$env_name"
  printf '  URL      : %s\n' "$url"
  printf '  Username : admin\n'
  printf '  Password : %s\n\n' "$key"
}

case "$target" in
  dev)        fetch "dev"  "litellm-dev"     "http://10.68.13.198:30400/dev/ui"  ;;
  prod|pro)   fetch "prod" "litellm-product" "https://litellm.carher.net/ui"      ;;
  all)        fetch "dev"  "litellm-dev"     "http://10.68.13.198:30400/dev/ui"
              fetch "prod" "litellm-product" "https://litellm.carher.net/ui"      ;;
  *)          echo "Usage: $0 [dev|prod|all]" >&2; exit 2 ;;
esac
