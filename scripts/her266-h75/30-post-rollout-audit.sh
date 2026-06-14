#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-carher}"
DIFY_NS="${DIFY_NS:-dify}"
HER_ID="${HER_ID:-266}"
HER_INSTANCE_NAME="${HER_INSTANCE_NAME:-her-$HER_ID}"
HER_DEPLOY_NAME="${HER_DEPLOY_NAME:-carher-$HER_ID}"
EXPECTED_UID="${EXPECTED_UID:-92629155-7299-4e5e-acd0-566e28a4234e}"
EXPECTED_GROUP="${EXPECTED_GROUP:-beta-her-266}"
EXPECTED_PROFILE="${EXPECTED_PROFILE:-h75-openclaw}"
EXPECTED_IMAGE_TAG="${EXPECTED_IMAGE_TAG:-h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-20260530}"
EXPECTED_H75_IDS="${EXPECTED_H75_IDS:-$HER_ID}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-http://carher-admin.carher.svc.cluster.local:8900}"
DIFY_PUBLIC_HEALTHZ="${DIFY_PUBLIC_HEALTHZ:-https://dify-k8s.carher.net/healthz}"
DIFY_CLUSTER_HEALTHZ="${DIFY_CLUSTER_HEALTHZ:-http://dify-bootstrap.dify.svc.cluster.local:5688/healthz}"
EXPECTED_HERMES_LITELLM_TRANSPORT="${EXPECTED_HERMES_LITELLM_TRANSPORT:-chat_completions}"

failures=0
warnings=0

usage() {
  cat <<'USAGE'
Usage: scripts/her266-h75/30-post-rollout-audit.sh [--help]

Read-only post-rollout audit for her-266 H75/Dify alignment.

Environment:
  NS                    Kubernetes namespace, default carher
  DIFY_NS               Dify namespace, default dify
  HER_ID                Her id, default 266
  EXPECTED_UID          HerInstance metadata.uid guard
  EXPECTED_IMAGE_TAG    Expected H75 runtime tag
  EXPECTED_GROUP        Expected deploy_group, default beta-her-266
  EXPECTED_PROFILE      Expected runtime profile annotation, default h75-openclaw
  EXPECTED_H75_IDS      Comma-separated allowed H75 Her ids, default HER_ID
  EXPECTED_HERMES_LITELLM_TRANSPORT
                        Expected Hermes chatgpt-pro transport, default chat_completions
  ADMIN_BASE_URL        Admin API base URL; cluster service URL by default

This script does not patch, apply, delete, restart, or call Admin update APIs.
USAGE
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

ok() {
  printf '[OK] %s\n' "$*"
}

warn() {
  warnings=$((warnings + 1))
  printf '[WARN] %s\n' "$*" >&2
}

fail() {
  failures=$((failures + 1))
  printf '[FAIL] %s\n' "$*" >&2
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "missing command: $1"
    return 1
  fi
}

redact() {
  sed -E \
    -e 's/(oc_)[A-Za-z0-9_-]+/\1REDACTED/g' \
    -e 's/(Bearer )[A-Za-z0-9._-]+/\1REDACTED/g' \
    -e 's/([A-Za-z0-9_]*(TOKEN|SECRET|KEY)[A-Za-z0-9_]*=)[^[:space:]]+/\1REDACTED/g'
}

jsonpath() {
  kubectl -n "$NS" get "$1" "$2" -o "jsonpath=$3"
}

admin_api_key() {
  if [[ -n "${ADMIN_API_KEY:-}" ]]; then
    printf '%s' "$ADMIN_API_KEY"
    return 0
  fi
  kubectl -n "$NS" get secret carher-admin-secrets \
    -o jsonpath='{.data.admin-api-key}' 2>/dev/null | base64 -d
}

admin_get_instance() {
  local key
  key="$(admin_api_key || true)"
  if [[ -z "$key" ]]; then
    warn "cannot read Admin API key; skipping Admin instance check"
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    warn "curl missing; skipping Admin instance check"
    return 0
  fi
  if ! curl -fsS -m 10 -H "X-API-Key: $key" "$ADMIN_BASE_URL/api/instances/$HER_ID" \
      | redact \
      | sed -n '1,40p'; then
    warn "Admin API instance check skipped or unreachable at $ADMIN_BASE_URL"
  else
    ok "Admin API instance endpoint reachable"
  fi
}

pod_name() {
  kubectl -n "$NS" get pods -l "app=carher-user,user-id=$HER_ID" \
    -o jsonpath='{range .items[*]}{.metadata.creationTimestamp}{"\t"}{.metadata.name}{"\t"}{.metadata.deletionTimestamp}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
    | awk -F '\t' '$3 == "" && $4 == "True" {print $1 "\t" $2}' \
    | sort -r \
    | awk -F '\t' 'NR == 1 {print $2}'
}

audit_k8s_identity() {
  local uid image group phase profile deploy_image pod_count ready_count
  uid="$(jsonpath herinstance "$HER_INSTANCE_NAME" '{.metadata.uid}')"
  image="$(jsonpath herinstance "$HER_INSTANCE_NAME" '{.spec.image}')"
  group="$(jsonpath herinstance "$HER_INSTANCE_NAME" '{.spec.deployGroup}')"
  phase="$(jsonpath herinstance "$HER_INSTANCE_NAME" '{.status.phase}')"
  profile="$(jsonpath herinstance "$HER_INSTANCE_NAME" '{.metadata.annotations.carher\.io/runtime-profile}')"
  deploy_image="$(kubectl -n "$NS" get deploy "$HER_DEPLOY_NAME" -o jsonpath='{.spec.template.spec.containers[?(@.name=="carher")].image}')"
  pod_count="$(kubectl -n "$NS" get pods -l "app=carher-user,user-id=$HER_ID" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  ready_count="$(kubectl -n "$NS" get pods -l "app=carher-user,user-id=$HER_ID" -o jsonpath='{range .items[*]}{.status.containerStatuses[?(@.name=="carher")].ready}{"\n"}{end}' | grep -c '^true$' || true)"

  [[ "$uid" == "$EXPECTED_UID" ]] && ok "HerInstance UID matches $HER_INSTANCE_NAME" || fail "UID mismatch: $uid"
  [[ "$image" == "$EXPECTED_IMAGE_TAG" ]] && ok "HerInstance image matches $EXPECTED_IMAGE_TAG" || fail "unexpected HerInstance image: $image"
  [[ "$group" == "$EXPECTED_GROUP" ]] && ok "deploy_group matches $EXPECTED_GROUP" || fail "unexpected deploy_group: $group"
  [[ "$phase" == "Running" ]] && ok "HerInstance phase is Running" || fail "unexpected phase: $phase"
  [[ "$profile" == "$EXPECTED_PROFILE" ]] && ok "runtime profile is $EXPECTED_PROFILE" || fail "unexpected profile: ${profile:-<empty>}"
  [[ "$deploy_image" == *":$EXPECTED_IMAGE_TAG" ]] && ok "Deployment image uses expected tag" || fail "unexpected Deployment image: $deploy_image"
  [[ "$pod_count" -ge 1 && "$ready_count" -ge 1 ]] && ok "ready pod found for her-$HER_ID" || fail "no ready carher container found for her-$HER_ID"
}

audit_dify() {
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS -m 15 "$DIFY_PUBLIC_HEALTHZ" >/dev/null; then
      ok "public Dify healthz is reachable"
    else
      fail "public Dify healthz failed: $DIFY_PUBLIC_HEALTHZ"
    fi
  else
    warn "curl missing; skipping public Dify healthz"
  fi

  local pod
  pod="$(pod_name || true)"
  if [[ -z "$pod" ]]; then
    fail "cannot find ready her-$HER_ID pod for in-pod Dify checks"
    return
  fi

  if kubectl -n "$NS" exec "$pod" -c carher -- sh -lc "
set -eu
test \"\${CARHER_DIFY_ENABLED:-}\" = \"1\"
test \"\${CARHER_DIFY_BOT_ID:-}\" = \"carher-$HER_ID\"
test -x /data/.openclaw/local/bin/dify-bootstrap-init
test -x /data/.openclaw/local/bin/her-workflow-dify-creator
test -x /data/.openclaw/local/bin/her-workflow-dify-mcp
test -f /data/.openclaw/workflow/dify-config.json
grep -q '\"bot_id\"' /data/.openclaw/workflow/dify-config.json
grep -q 'carher-$HER_ID' /data/.openclaw/workflow/dify-config.json
curl -fsS -m 15 '$DIFY_CLUSTER_HEALTHZ' >/dev/null
" >/dev/null; then
    ok "in-pod Dify env/tools/config/bootstrap checks pass"
  else
    fail "in-pod Dify env/tools/config/bootstrap checks failed"
  fi
}

audit_engine_a2a() {
  local pod
  pod="$(pod_name || true)"
  if [[ -z "$pod" ]]; then
    fail "cannot find ready her-$HER_ID pod for engine/A2A checks"
    return
  fi

  if kubectl -n "$NS" exec "$pod" -c carher -- sh -lc "
set -eu
active=\"\$(cat /data/.engine/active 2>/dev/null || true)\"
test -n \"\$active\"
case \"\$active\" in openclaw|hermes) ;; *) exit 41 ;; esac
test \"\${HERMESTEST_A2A_ENABLED:-}\" = \"1\"
test \"\${HERMESTEST_A2A_HOST:-}\" = \"0.0.0.0\"
test \"\${HERMESTEST_A2A_PORT:-}\" = \"18800\"
test \"\${HERMESTEST_A2A_AUTH:-}\" = \"none\"
test -f /opt/data/.hermes/config.yaml
grep -q 'api_mode: \"$EXPECTED_HERMES_LITELLM_TRANSPORT\"' /opt/data/.hermes/config.yaml
grep -q 'transport: \"$EXPECTED_HERMES_LITELLM_TRANSPORT\"' /opt/data/.hermes/config.yaml
for _ in \$(seq 1 24); do
  if curl -fsS -m 5 http://127.0.0.1:18800/.well-known/agent-card.json | grep -q '\"protocolVersion\"'; then
    break
  fi
  sleep 5
done
curl -fsS -m 10 http://127.0.0.1:18800/.well-known/agent-card.json | grep -q '\"protocolVersion\"'
if [ \"\$active\" = \"hermes\" ]; then
  curl -fsS -m 10 http://127.0.0.1:18800/healthz | grep -q '\"ok\"'
fi
" >/dev/null; then
    ok "engine marker and A2A endpoint checks pass"
  else
    fail "engine marker or A2A endpoint checks failed"
  fi
}

audit_isolation() {
  local matches total
  matches="$(
    kubectl -n "$NS" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\t"}{.metadata.annotations.carher\.io/pod-spec-key}{"\n"}{end}' \
      | awk '/^carher-[0-9]+/ && /h75|profile=h75|dualswitch|chatidfix/ {print}'
  )"
  total="$(
    kubectl -n "$NS" get deploy -o name \
      | awk -F / '/deployment.apps\/carher-[0-9]+$/ {count++} END {print count + 0}'
  )"
  printf '%s\n' "$matches" | redact
  local count
  count="$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l | tr -d ' ')"
  local expected_names expected_count
  expected_names="$(
    printf '%s\n' "$EXPECTED_H75_IDS" \
      | tr ',' '\n' \
      | sed -E 's/^[[:space:]]+|[[:space:]]+$//g; /^$/d; s/^/carher-/' \
      | sort
  )"
  expected_count="$(printf '%s\n' "$expected_names" | sed '/^$/d' | wc -l | tr -d ' ')"
  local actual_names
  actual_names="$(printf '%s\n' "$matches" | sed '/^$/d' | cut -f1 | sort)"
  if [[ "$count" == "$expected_count" && "$actual_names" == "$expected_names" ]]; then
    ok "isolation holds: HER_DEPLOYS=$total H75_HER=$count expected=$EXPECTED_H75_IDS"
  else
    fail "unexpected H75/profile matches: HER_DEPLOYS=$total H75_HER=$count expected=$EXPECTED_H75_IDS"
  fi
}

main() {
  need_cmd kubectl
  if (( failures > 0 )); then
    exit 2
  fi

  echo "== her-$HER_ID H75/Dify post-rollout audit =="
  audit_k8s_identity
  admin_get_instance
  audit_dify
  audit_engine_a2a
  audit_isolation

  echo "== summary =="
  if (( failures > 0 )); then
    echo "result=FAIL failures=$failures warnings=$warnings"
    exit 1
  fi
  echo "result=OK failures=0 warnings=$warnings"
}

main "$@"
