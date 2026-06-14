#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-dify}"
CARHER_NS="${CARHER_NS:-carher}"
STATE_DIR="${STATE_DIR:-.dify-ha-state}"
TARGET_REPLICAS="${TARGET_REPLICAS:-2}"
TAG_SUFFIX="${TAG_SUFFIX:-20260530}"
TARGET_REPOSITORY="${TARGET_REPOSITORY:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher}"
TARGET_REGISTRY_PREFIX="${TARGET_REGISTRY_PREFIX:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/}"

API_IMAGE="${API_IMAGE:-$TARGET_REPOSITORY:dify-api-1.4.2-$TAG_SUFFIX}"
WEB_IMAGE="${WEB_IMAGE:-$TARGET_REPOSITORY:dify-web-1.4.2-$TAG_SUFFIX}"
BOOTSTRAP_IMAGE="${BOOTSTRAP_IMAGE:-$TARGET_REPOSITORY:dify-python-3.12-slim-$TAG_SUFFIX}"
BOOTSTRAP_KUBECTL_IMAGE="${BOOTSTRAP_KUBECTL_IMAGE:-$TARGET_REPOSITORY:dify-bitnami-kubectl-latest-$TAG_SUFFIX}"
NGINX_IMAGE="${NGINX_IMAGE:-$TARGET_REPOSITORY:dify-nginx-latest-$TAG_SUFFIX}"

STATELESS_DEPLOYS=(dify-api dify-web dify-worker dify-bootstrap dify-nginx)
SERVICE_DEPLOYS=(dify-api dify-web dify-bootstrap dify-nginx)

usage() {
  cat <<EOF
Usage: $0 <plan|snapshot|apply|verify|rollback> [rollback.tsv]

Stages only Dify stateless HA:
  - mirrors are expected in ACR VPC before apply
  - scales api/web/worker/bootstrap/nginx to TARGET_REPLICAS
  - adds PDBs for the same deployments
  - leaves db/redis/weaviate/plugin-daemon/sandbox/ssrf-proxy unchanged

Environment:
  NS=$NS
  TARGET_REPLICAS=$TARGET_REPLICAS
  TARGET_REPOSITORY=$TARGET_REPOSITORY
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing command: $1" >&2
    exit 2
  }
}

deploy_image() {
  case "$1" in
    dify-api) echo "$API_IMAGE" ;;
    dify-web) echo "$WEB_IMAGE" ;;
    dify-worker) echo "$API_IMAGE" ;;
    dify-bootstrap) echo "$BOOTSTRAP_IMAGE" ;;
    dify-nginx) echo "$NGINX_IMAGE" ;;
    *) echo "unknown deploy: $1" >&2; exit 2 ;;
  esac
}

deploy_container() {
  case "$1" in
    dify-api) echo api ;;
    dify-web) echo web ;;
    dify-worker) echo worker ;;
    dify-bootstrap) echo bootstrap ;;
    dify-nginx) echo nginx ;;
    *) echo "unknown deploy: $1" >&2; exit 2 ;;
  esac
}

timestamp() {
  date -u +%Y%m%dT%H%M%SZ
}

snapshot() {
  mkdir -p "$STATE_DIR"
  local file="${1:-$STATE_DIR/dify-stateless-ha-rollback-$(timestamp).tsv}"
  : >"$file"
  for deploy in "${STATELESS_DEPLOYS[@]}"; do
    kubectl -n "$NS" get deploy "$deploy" -o json | jq -r --arg deploy "$deploy" '
      (.spec.replicas // 1) as $replicas
      | (.spec.template.spec.containers[] | [$deploy, $replicas, "container", .name, .image] | @tsv),
        ((.spec.template.spec.initContainers // [])[] | [$deploy, $replicas, "init", .name, .image] | @tsv)
    ' >>"$file"
  done
  echo "$file"
}

copy_pull_secret() {
  local name="$1"
  if kubectl -n "$NS" get secret "$name" >/dev/null 2>&1; then
    return 0
  fi
  if ! kubectl -n "$CARHER_NS" get secret "$name" >/dev/null 2>&1; then
    echo "[WARN] pull secret $CARHER_NS/$name not found; skipping" >&2
    return 0
  fi
  kubectl -n "$CARHER_NS" get secret "$name" -o json \
    | jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.managedFields,.metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"])' \
    | kubectl -n "$NS" apply -f - >/dev/null
}

patch_pull_secrets() {
  local deploy="$1"
  kubectl -n "$NS" patch deploy "$deploy" --type merge -p \
    '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"acr-vpc-secret"},{"name":"acr-secret"}]}}}}' >/dev/null
}

patch_strategy_and_scale() {
  local deploy="$1"
  kubectl -n "$NS" patch deploy "$deploy" --type merge -p \
    "{\"spec\":{\"replicas\":$TARGET_REPLICAS,\"strategy\":{\"type\":\"RollingUpdate\",\"rollingUpdate\":{\"maxUnavailable\":0,\"maxSurge\":1}}}}" >/dev/null
}

ensure_pdb() {
  local deploy="$1"
  cat <<EOF | kubectl -n "$NS" apply -f - >/dev/null
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: ${deploy}-pdb
  labels:
    app: ${deploy}
    carher.io/dify-ha-stage: stateless
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: ${deploy}
EOF
}

wait_rollout() {
  local deploy="$1"
  kubectl -n "$NS" rollout status "deploy/$deploy" --timeout=600s
}

apply_stateless() {
  local rollback_file="${1:-}"
  if [[ -z "$rollback_file" ]]; then
    rollback_file="$(snapshot)"
  fi
  echo "[dify-ha] rollback file: $rollback_file"

  copy_pull_secret acr-vpc-secret
  copy_pull_secret acr-secret

  for deploy in "${STATELESS_DEPLOYS[@]}"; do
    local container image
    container="$(deploy_container "$deploy")"
    image="$(deploy_image "$deploy")"
    echo "[dify-ha] set $deploy $container=$image"
    kubectl -n "$NS" set image "deploy/$deploy" "$container=$image" >/dev/null
    if [[ "$deploy" = "dify-bootstrap" ]]; then
      kubectl -n "$NS" set image "deploy/$deploy" "kubectl-copy=$BOOTSTRAP_KUBECTL_IMAGE" >/dev/null
    fi
    patch_pull_secrets "$deploy"
    patch_strategy_and_scale "$deploy"
    ensure_pdb "$deploy"
  done

  for deploy in "${STATELESS_DEPLOYS[@]}"; do
    wait_rollout "$deploy"
  done
  verify_stateless
}

rollback() {
  local file="${1:-}"
  if [[ -z "$file" || ! -f "$file" ]]; then
    echo "rollback file is required" >&2
    usage >&2
    exit 2
  fi

  local seen_file
  seen_file="$(mktemp)"
  while IFS=$'\t' read -r deploy replicas _kind name image; do
    [[ -z "$deploy" ]] && continue
    echo "[dify-ha] restore $deploy $name=$image"
    kubectl -n "$NS" set image "deploy/$deploy" "$name=$image" >/dev/null
    printf '%s\t%s\n' "$deploy" "$replicas" >>"$seen_file"
  done <"$file"

  sort -u "$seen_file" | while IFS=$'\t' read -r deploy replicas; do
    [[ -z "$deploy" ]] && continue
    echo "[dify-ha] scale $deploy replicas=$replicas"
    kubectl -n "$NS" scale "deploy/$deploy" --replicas="$replicas" >/dev/null
  done
  rm -f "$seen_file"

  kubectl -n "$NS" delete pdb -l carher.io/dify-ha-stage=stateless --ignore-not-found >/dev/null

  for deploy in "${STATELESS_DEPLOYS[@]}"; do
    wait_rollout "$deploy"
  done
  echo "[dify-ha] rollback complete"
}

verify_stateless() {
  local failures=0
  echo "deployment	desired	available	image"
  for deploy in "${STATELESS_DEPLOYS[@]}"; do
    local desired available image
    desired="$(kubectl -n "$NS" get deploy "$deploy" -o jsonpath='{.spec.replicas}')"
    available="$(kubectl -n "$NS" get deploy "$deploy" -o jsonpath='{.status.availableReplicas}')"
    image="$(kubectl -n "$NS" get deploy "$deploy" -o jsonpath='{.spec.template.spec.containers[0].image}')"
    printf '%s\t%s\t%s\t%s\n' "$deploy" "${desired:-0}" "${available:-0}" "$image"
    if [[ "${desired:-0}" -lt "$TARGET_REPLICAS" || "${available:-0}" -lt "$TARGET_REPLICAS" ]]; then
      echo "[FAIL] $deploy is not at $TARGET_REPLICAS/$TARGET_REPLICAS" >&2
      failures=$((failures + 1))
    fi
  if [[ "$image" != "$TARGET_REGISTRY_PREFIX"* ]]; then
      echo "[FAIL] $deploy image is not in ACR VPC: $image" >&2
      failures=$((failures + 1))
    fi
    if ! kubectl -n "$NS" get pdb "${deploy}-pdb" >/dev/null 2>&1; then
      echo "[FAIL] missing PDB for $deploy" >&2
      failures=$((failures + 1))
    fi
  done

  echo
  echo "service	endpoints"
  for service in "${SERVICE_DEPLOYS[@]}"; do
    local endpoints
    endpoints="$(kubectl -n "$NS" get endpoints "$service" -o json | jq '[.subsets[]?.addresses[]?] | length')"
    printf '%s\t%s\n' "$service" "$endpoints"
    if [[ "$endpoints" -lt 2 ]]; then
      echo "[FAIL] $service has fewer than 2 ready endpoints" >&2
      failures=$((failures + 1))
    fi
  done

  if curl -fsS --max-time 10 https://dify-k8s.carher.net/healthz >/dev/null; then
    echo "[OK] public Dify healthz reachable"
  else
    echo "[FAIL] public Dify healthz failed" >&2
    failures=$((failures + 1))
  fi

  echo
  echo "stateful components intentionally unchanged:"
  kubectl -n "$NS" get deploy dify-db dify-redis dify-weaviate -o json \
    | jq -r '.items[] | [.metadata.name, (.spec.replicas // 1), (.status.availableReplicas // 0), ([.spec.template.spec.volumes[]? | select(.persistentVolumeClaim != null) | .persistentVolumeClaim.claimName] | join(","))] | @tsv'

  if [[ "$failures" -gt 0 ]]; then
    echo "result=FAIL failures=$failures" >&2
    exit 1
  fi
  echo "result=OK failures=0"
  echo "[NOTE] This proves stateless Dify HA only; db/redis/weaviate still need managed or stateful HA for strict end-to-end HA."
}

plan() {
  cat <<EOF
Stateless Dify HA plan:
- Copy ACR pull secrets from $CARHER_NS to $NS if missing.
- Patch images to ACR VPC:
  dify-api       -> $API_IMAGE
  dify-web       -> $WEB_IMAGE
  dify-worker    -> $API_IMAGE
  dify-bootstrap -> $BOOTSTRAP_IMAGE
  kubectl-copy   -> $BOOTSTRAP_KUBECTL_IMAGE
  dify-nginx     -> $NGINX_IMAGE
- Scale: ${STATELESS_DEPLOYS[*]} to $TARGET_REPLICAS replicas.
- Create PDBs: minAvailable=1 for each stateless deployment.
- Leave stateful single points unchanged: dify-db, dify-redis, dify-weaviate.

Rollback:
  $0 snapshot
  $0 apply <rollback.tsv>
  $0 rollback <rollback.tsv>
EOF
}

need_cmd kubectl
need_cmd jq

action="${1:-plan}"
case "$action" in
  plan)
    plan
    ;;
  snapshot)
    snapshot "${2:-}"
    ;;
  apply)
    apply_stateless "${2:-}"
    ;;
  verify)
    verify_stateless
    ;;
  rollback)
    rollback "${2:-}"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
