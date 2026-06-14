#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
SNAP_DIR="$ROOT/snapshots/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/20-ack-her266-ops.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

NS="${NS:-carher}"
DIFY_NS="${DIFY_NS:-dify}"
HER_ID="${HER_ID:-266}"
HER_INSTANCE_NAME="${HER_INSTANCE_NAME:-her-$HER_ID}"
HER_DEPLOY_NAME="${HER_DEPLOY_NAME:-carher-$HER_ID}"
HER_K8S_UID_EXPECTED="${HER_K8S_UID_EXPECTED:-92629155-7299-4e5e-acd0-566e28a4234e}"

NEW_IMAGE_TAG="${NEW_IMAGE_TAG:-h75-runtime-b600887-20260530}"
ROLLBACK_IMAGE_TAG="${ROLLBACK_IMAGE_TAG:-fix-compact-eb348941}"
NEW_DEPLOY_GROUP="${NEW_DEPLOY_GROUP:-beta-her-266}"
ROLLBACK_DEPLOY_GROUP="${ROLLBACK_DEPLOY_GROUP:-stable}"
RUNTIME_PROFILE="${RUNTIME_PROFILE:-h75-openclaw}"

OPERATOR_IMAGE="${OPERATOR_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:her266-h75-profile-20260530-r11}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-http://carher-admin.carher.svc.cluster.local:8900}"
H75_CONFIG_DIR="${H75_CONFIG_DIR:-$ROOT/h75-config}"
H75_RUNTIME_SECRET="${H75_RUNTIME_SECRET:-carher-h75-runtime-secrets}"
H75_ACP_SECRET="${H75_ACP_SECRET:-carher-h75-acp-secrets}"
H75_GATEWAY_TOKEN_KEY="${H75_GATEWAY_TOKEN_KEY:-CARHER_GATEWAY_TOKEN}"

kc() {
  kubectl -n "$NS" "$@"
}

admin_api_key() {
  if [[ -n "${ADMIN_API_KEY:-}" ]]; then
    printf '%s' "$ADMIN_API_KEY"
    return
  fi
  kubectl -n "$NS" get secret carher-admin-secrets \
    -o jsonpath='{.data.admin-api-key}' | base64 -d
}

admin_get() {
  local path="$1"
  local key
  key="$(admin_api_key)"
  curl -fsS -H "X-API-Key: $key" "$ADMIN_BASE_URL$path"
}

admin_post() {
  local path="$1"
  local body="$2"
  local key
  key="$(admin_api_key)"
  curl -fsS -X POST "$ADMIN_BASE_URL$path" \
    -H "X-API-Key: $key" \
    -H "Content-Type: application/json" \
    -d "$body"
}

admin_put_instance() {
  local image="$1"
  local group="$2"
  local key
  key="$(admin_api_key)"
  curl -fsS -X PUT "$ADMIN_BASE_URL/api/instances/$HER_ID" \
    -H "X-API-Key: $key" \
    -H "Content-Type: application/json" \
    -d "{\"image\":\"$image\",\"deploy_group\":\"$group\"}"
}

wait_deploy_rollout() {
  local deploy="$1"
  local timeout_seconds="${2:-600}"
  local start
  start="$(date +%s)"
  while true; do
    local generation observed desired updated available
    read -r generation observed desired updated available < <(
      kc get deploy "$deploy" -o jsonpath='{.metadata.generation}{" "}{.status.observedGeneration}{" "}{.spec.replicas}{" "}{.status.updatedReplicas}{" "}{.status.availableReplicas}{"\n"}'
    )
    desired="${desired:-0}"
    updated="${updated:-0}"
    available="${available:-0}"
    observed="${observed:-0}"
    if [[ "$observed" -ge "$generation" && "$updated" -eq "$desired" && "$available" -eq "$desired" ]]; then
      echo "[rollout] $deploy ready generation=$generation replicas=$desired"
      return 0
    fi
    local now
    now="$(date +%s)"
    if (( now - start >= timeout_seconds )); then
      echo "[rollout] timeout waiting for $deploy generation=$generation observed=$observed desired=$desired updated=$updated available=$available" >&2
      exit 41
    fi
    sleep 5
  done
}

strip_direct_anthropic_provider() {
  local src="$1"
  local dest="$2"
  awk '
    BEGIN { skip=0; depth=0; removed=0 }
    skip == 0 && $0 ~ /^[[:space:]]+anthropic:[[:space:]]*\{/ {
      skip=1
      removed=1
      line=$0
      gsub(/[^{}]/, "", line)
      for (i=1; i<=length(line); i++) {
        ch=substr(line,i,1)
        if (ch=="{") depth++
        else if (ch=="}") depth--
      }
      if (depth <= 0) { skip=0; depth=0 }
      next
    }
    skip == 1 {
      line=$0
      gsub(/[^{}]/, "", line)
      for (i=1; i<=length(line); i++) {
        ch=substr(line,i,1)
        if (ch=="{") depth++
        else if (ch=="}") depth--
      }
      if (depth <= 0) { skip=0; depth=0 }
      next
    }
    { print }
    END { if (removed != 1 || skip == 1) exit 42 }
  ' "$src" > "$dest"
}

add_h75_runtime_plugin_overlay() {
  local file="$1"
  python3 - "$file" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()

path_anchor = '        "/data/.openclaw/extensions/node_modules/@larksuite/openclaw-lark",\n'
path_additions = [
    '        "/data/.openclaw/runtime-plugins/acpx",\n',
    '        "/data/.openclaw/runtime-plugins/carher-engine-swap",\n',
]
if path_anchor not in text:
    raise SystemExit("plugin load path anchor not found")
if all(line.strip().strip('",') in text for line in path_additions):
    pass
else:
    insert = path_anchor + "".join(line for line in path_additions if line.strip().strip('",') not in text)
    text = text.replace(path_anchor, insert, 1)

acpx_old = '      acpx: { enabled: true },\n'
acpx_new = '''      acpx: {
        enabled: true,
        config: {
          cwd: "/data/.openclaw/workspace",
          stateDir: "/data/.openclaw/acpx-state",
          permissionMode: "approve-reads",
          nonInteractivePermissions: "fail",
          timeoutSeconds: 120,
          agents: {
            claude: { command: "/data/.openclaw/local/bin/claude-agent-acp" },
            codex: { command: "/data/.openclaw/local/bin/codex-acp" },
          },
        },
      },
'''
if acpx_old in text:
    text = text.replace(acpx_old, acpx_new, 1)

engine_swap_entry = '      "carher-engine-swap": { enabled: true },\n'
entry_anchor = '      "openclaw-lark": { enabled: true },\n'
if '"carher-engine-swap"' not in text:
    if entry_anchor not in text:
        raise SystemExit("plugin entry anchor not found")
    text = text.replace(entry_anchor, entry_anchor + engine_swap_entry, 1)

path.write_text(text)
PY
}

ensure_h75_runtime_secret() {
  if [[ -n "${CARHER_GATEWAY_TOKEN:-}" ]]; then
    kubectl -n "$NS" create secret generic "$H75_RUNTIME_SECRET" \
      --from-literal="$H75_GATEWAY_TOKEN_KEY=$CARHER_GATEWAY_TOKEN" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "[profile] h75 runtime secret applied from CARHER_GATEWAY_TOKEN env"
    return
  fi
  if kubectl -n "$NS" get secret "$H75_RUNTIME_SECRET" >/dev/null 2>&1; then
    echo "[profile] h75 runtime secret already exists"
    return
  fi
  local generated_token
  generated_token="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
  kubectl -n "$NS" create secret generic "$H75_RUNTIME_SECRET" \
    --from-literal="$H75_GATEWAY_TOKEN_KEY=$generated_token" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "[profile] h75 runtime secret generated"
}

ensure_h75_acp_secret() {
  if [[ -n "${H75_ACP_ENV_FILE:-}" ]]; then
    kubectl -n "$NS" create secret generic "$H75_ACP_SECRET" \
      --from-env-file="$H75_ACP_ENV_FILE" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "[profile] h75 ACP secret applied from H75_ACP_ENV_FILE"
    return
  fi
  if kubectl -n "$NS" get secret "$H75_ACP_SECRET" >/dev/null 2>&1; then
    echo "[profile] h75 ACP secret already exists"
    return
  fi
  echo "[profile] create $H75_ACP_SECRET first or set H75_ACP_ENV_FILE with ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL" >&2
  exit 38
}

require_target_identity() {
  local actual_uid
  actual_uid="$(kc get herinstance "$HER_INSTANCE_NAME" -o jsonpath='{.metadata.uid}')"
  if [[ "$actual_uid" != "$HER_K8S_UID_EXPECTED" ]]; then
    echo "[guard] $HER_INSTANCE_NAME metadata.uid mismatch: got $actual_uid expected $HER_K8S_UID_EXPECTED" >&2
    exit 30
  fi
}

ensure_deploy_group() {
  echo "[admin] ensure deploy group $NEW_DEPLOY_GROUP"
  if ! admin_post "/api/deploy-groups" \
    "{\"name\":\"$NEW_DEPLOY_GROUP\",\"priority\":5,\"description\":\"Single-instance H75 rollout for her-$HER_ID\"}" \
    > "$ROOT/admin-ensure-deploy-group.json"; then
    echo "[admin] deploy group create returned non-2xx; assuming it may already exist and continuing after list check"
    admin_get "/api/deploy-groups" > "$ROOT/admin-deploy-groups.json"
    if ! grep -q "$NEW_DEPLOY_GROUP" "$ROOT/admin-deploy-groups.json"; then
      echo "[admin] deploy group $NEW_DEPLOY_GROUP not found after create failure" >&2
      exit 35
    fi
  fi
}

preflight() {
  echo "[preflight] $HER_INSTANCE_NAME / $HER_DEPLOY_NAME"
  require_target_identity
  local image group phase
  image="$(kc get herinstance "$HER_INSTANCE_NAME" -o jsonpath='{.spec.image}')"
  group="$(kc get herinstance "$HER_INSTANCE_NAME" -o jsonpath='{.spec.deployGroup}')"
  phase="$(kc get herinstance "$HER_INSTANCE_NAME" -o jsonpath='{.status.phase}')"
  printf 'deployGroup=%s image=%s phase=%s\n' "$group" "$image" "$phase"
  if [[ "$image" != "$ROLLBACK_IMAGE_TAG" && "$image" != "$NEW_IMAGE_TAG" ]]; then
    echo "[preflight] unexpected image $image; refusing to proceed" >&2
    exit 33
  fi
  if [[ "$group" != "$ROLLBACK_DEPLOY_GROUP" && "$group" != "$NEW_DEPLOY_GROUP" ]]; then
    echo "[preflight] unexpected deployGroup $group; refusing to proceed" >&2
    exit 34
  fi
  kc get deploy "$HER_DEPLOY_NAME" -o jsonpath='deploymentImage={.spec.template.spec.containers[?(@.name=="carher")].image}{"\n"}'
  kc get pods -l "app=carher-user,user-id=$HER_ID" -o wide
  if ! kubectl -n "$DIFY_NS" get deploy,svc,pods | sed -n '1,160p'; then
    echo "[preflight] warn: cannot list $DIFY_NS resources; continuing with public healthz"
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 15 https://dify-k8s.carher.net/healthz >/dev/null
    echo "[preflight] dify public healthz ok"
  fi
  echo "[preflight] ok"
}

snapshot() {
  echo "[snapshot] $SNAP_DIR"
  mkdir -p "$SNAP_DIR"
  require_target_identity
  kc get herinstance "$HER_INSTANCE_NAME" -o yaml > "$SNAP_DIR/herinstance.yaml"
  kc get deploy "$HER_DEPLOY_NAME" -o yaml > "$SNAP_DIR/deployment.yaml" 2>/dev/null || true
  kc get svc "$HER_DEPLOY_NAME" -o yaml > "$SNAP_DIR/service.yaml" 2>/dev/null || true
  kc get pods -l "app=carher-user,user-id=$HER_ID" -o yaml > "$SNAP_DIR/pods.yaml" 2>/dev/null || true
  kc get cm "$HER_DEPLOY_NAME-user-config" -o yaml > "$SNAP_DIR/user-config.yaml" 2>/dev/null || true
  admin_get "/api/instances/$HER_ID" > "$SNAP_DIR/admin-instance.json"
  admin_get "/api/instances?offset=0&limit=0" > "$SNAP_DIR/admin-instances-all.json"
  kc get herinstances -o custom-columns='NAME:.metadata.name,UID:.spec.userId,IMAGE:.spec.image,GROUP:.spec.deployGroup,PHASE:.status.phase' \
    > "$SNAP_DIR/herinstances.tsv"
  echo "$SNAP_DIR" > "$ROOT/latest-snapshot-dir.txt"
  echo "[snapshot] done"
}

apply_profile() {
  echo "[profile] apply H75 ConfigMap, token Secret, operator image"
  for file in "$H75_CONFIG_DIR/base.json5" "$H75_CONFIG_DIR/docker.json5"; do
    if [[ ! -f "$file" ]]; then
      echo "[profile] missing $file; copy H75 pinned config into $H75_CONFIG_DIR first" >&2
      exit 31
    fi
  done

  local tmp
  tmp="$(mktemp -d)"
  cp "$H75_CONFIG_DIR/base.json5" "$tmp/shared-config.json5"
  sed 's#\./base\.json5#\./shared-config.json5#g; s#"\./base\.json5"#"./shared-config.json5"#g' \
    "$H75_CONFIG_DIR/docker.json5" > "$tmp/carher-config.raw.json"
  strip_direct_anthropic_provider "$tmp/carher-config.raw.json" "$tmp/carher-config.json"
  if grep -Eq 'ANTHROPIC_(AUTH_TOKEN|BASE_URL)' "$tmp/carher-config.json"; then
    echo "[profile] direct Anthropic env refs remain in H75 config; refusing to apply" >&2
    exit 37
  fi
  add_h75_runtime_plugin_overlay "$tmp/carher-config.json"
  kubectl -n "$NS" create configmap carher-base-config-h75 \
    --from-file=shared-config.json5="$tmp/shared-config.json5" \
    --from-file=carher-config.json="$tmp/carher-config.json" \
    --dry-run=client -o yaml | kubectl apply -f -

  local token="${DIFY_BOOTSTRAP_TOKEN:-}"
  if [[ -z "$token" && -n "${DIFY_BOOTSTRAP_SOURCE_SECRET:-}" ]]; then
    token="$(kubectl -n "$DIFY_NS" get secret "$DIFY_BOOTSTRAP_SOURCE_SECRET" \
      -o "jsonpath={.data.${DIFY_BOOTSTRAP_SOURCE_KEY:-token}}" | base64 -d)"
  fi
  if [[ -z "$token" ]]; then
    echo "[profile] set DIFY_BOOTSTRAP_TOKEN or DIFY_BOOTSTRAP_SOURCE_SECRET before applying profile" >&2
    exit 32
  fi
  kubectl -n "$NS" create secret generic carher-dify-bootstrap-token \
    --from-literal=token="$token" \
    --dry-run=client -o yaml | kubectl apply -f -
  ensure_h75_runtime_secret
  ensure_h75_acp_secret

  kc set image deploy/carher-operator "operator=$OPERATOR_IMAGE"
  wait_deploy_rollout carher-operator 180
  echo "[profile] done"
}

upgrade() {
  echo "[upgrade] annotate $HER_INSTANCE_NAME, then Admin API update"
  require_target_identity
  ensure_deploy_group
  kc annotate herinstance "$HER_INSTANCE_NAME" "carher.io/runtime-profile=$RUNTIME_PROFILE" --overwrite
  if ! admin_put_instance "$NEW_IMAGE_TAG" "$NEW_DEPLOY_GROUP" > "$ROOT/admin-update-her266.json"; then
    echo "[upgrade] Admin update failed after annotation; removing runtime profile before exit" >&2
    kc annotate herinstance "$HER_INSTANCE_NAME" carher.io/runtime-profile- --overwrite || true
    exit 36
  fi
  echo "[upgrade] Admin update sent for her-$HER_ID image=$NEW_IMAGE_TAG group=$NEW_DEPLOY_GROUP"
}

target_pod() {
  local pod
  pod="$(
    kc get pods -l "app=carher-user,user-id=$HER_ID" \
      -o jsonpath='{range .items[*]}{.metadata.creationTimestamp}{"\t"}{.metadata.name}{"\t"}{.metadata.deletionTimestamp}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\t"}{range .spec.containers[?(@.name=="carher")]}{.image}{end}{"\n"}{end}' \
      | awk -F '\t' -v suffix=":$NEW_IMAGE_TAG" '$3 == "" && $4 == "True" && $5 ~ suffix "$" {print $1 "\t" $2}' \
      | sort -r \
      | awk -F '\t' 'NR == 1 {print $2}'
  )"
  if [[ -z "$pod" ]]; then
    echo "no pod using image tag $NEW_IMAGE_TAG" >&2
    return 1
  fi
  printf '%s\n' "$pod"
}

watch_verify() {
  echo "[verify] rollout"
  wait_deploy_rollout "$HER_DEPLOY_NAME" 600
  local pod
  pod="$(target_pod)"
  kc wait "pod/$pod" --for=condition=Ready --timeout=10m
  kc get pod "$pod" -o jsonpath='pod={.metadata.name} phase={.status.phase} restarts={.status.containerStatuses[?(@.name=="carher")].restartCount} ready={.status.containerStatuses[?(@.name=="carher")].ready}{"\n"}'

  local offenders
  offenders="$(kc get deploy -l app=carher-user -o jsonpath="{range .items[?(@.metadata.name!=\"$HER_DEPLOY_NAME\")]}{.metadata.name}{' '}{.spec.template.spec.containers[?(@.name=='carher')].image}{'\\n'}{end}" 2>/dev/null | grep -F "$NEW_IMAGE_TAG" || true)"
  if [[ -n "$offenders" ]]; then
    echo "[verify] new image appeared outside $HER_DEPLOY_NAME:" >&2
    echo "$offenders" >&2
    exit 40
  fi

  kc exec "$pod" -c carher -- sh -lc '
set -eu
test -x /data/.openclaw/local/bin/dify-bootstrap-init
test -x /data/.openclaw/local/bin/her-workflow-dify-creator
test -x /data/.openclaw/local/bin/her-workflow-dify-mcp
test -f /data/.openclaw/workflow/dify-config.json
grep -q "carher-'"$HER_ID"'" /data/.openclaw/workflow/dify-config.json
test "${CARHER_DIFY_ENABLED:-}" = "1"
test "${CARHER_DIFY_BOT_ID:-}" = "carher-'"$HER_ID"'"
test "${HERMESTEST_A2A_ENABLED:-}" = "1"
test "${HERMESTEST_A2A_HOST:-}" = "0.0.0.0"
test "${HERMESTEST_A2A_PORT:-}" = "18800"
test "${HERMESTEST_A2A_AUTH:-}" = "none"
test -n "${CARHER_GATEWAY_TOKEN:-}"
touch /opt/data/.her266-h75-mount-check
test -f /data/.openclaw/.her266-h75-mount-check
rm -f /opt/data/.her266-h75-mount-check
active="$(cat /data/.engine/active 2>/dev/null || true)"
if [ "$active" = "openclaw" ]; then
  curl -fsS -m 15 http://127.0.0.1:18789/healthz >/dev/null
fi
curl -fsS -m 15 http://127.0.0.1:18800/.well-known/agent-card.json | grep -q '"protocolVersion"'
if [ "$active" = "hermes" ]; then
  curl -fsS -m 15 http://127.0.0.1:18800/healthz | grep -q '"ok"'
fi
curl -fsS -m 15 https://dify-k8s.carher.net/healthz >/dev/null
'
  echo "[verify] her-$HER_ID H75/Dify checks ok"
}

rollback() {
  echo "[rollback] $HER_INSTANCE_NAME / $HER_DEPLOY_NAME -> $ROLLBACK_IMAGE_TAG / $ROLLBACK_DEPLOY_GROUP"
  require_target_identity
  kc annotate herinstance "$HER_INSTANCE_NAME" carher.io/runtime-profile- --overwrite || true
  admin_put_instance "$ROLLBACK_IMAGE_TAG" "$ROLLBACK_DEPLOY_GROUP" > "$ROOT/admin-rollback-her266.json"
  wait_deploy_rollout "$HER_DEPLOY_NAME" 600
  echo "[rollback] done"
}

case "${1:-all}" in
  preflight) preflight ;;
  snapshot) snapshot ;;
  apply-profile) apply_profile ;;
  upgrade) upgrade ;;
  watch-verify) watch_verify ;;
  rollback) rollback ;;
  all)
    preflight
    snapshot
    apply_profile
    upgrade
    watch_verify
    ;;
  *)
    echo "usage: $0 {preflight|snapshot|apply-profile|upgrade|watch-verify|rollback|all}" >&2
    exit 2
    ;;
esac
