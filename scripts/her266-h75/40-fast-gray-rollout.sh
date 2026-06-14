#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-carher}"
RUNTIME_PROFILE="${RUNTIME_PROFILE:-h75-openclaw}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-http://carher-admin.carher.svc.cluster.local:8900}"
JMS="${JMS:-jms}"
STATE_DIR="${STATE_DIR:-./.her266-h75-state}"
ROLLBACK_FILE="${ROLLBACK_FILE:-$STATE_DIR/rollback.tsv}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage:
  scripts/her266-h75/40-fast-gray-rollout.sh plan <targets.tsv>
  scripts/her266-h75/40-fast-gray-rollout.sh snapshot <targets.tsv> [rollback.tsv]
  scripts/her266-h75/40-fast-gray-rollout.sh apply <rollback.tsv>
  scripts/her266-h75/40-fast-gray-rollout.sh verify <rollback.tsv>
  scripts/her266-h75/40-fast-gray-rollout.sh rollback <rollback.tsv>

Target file formats, tab-separated:
  ack     <her_id> <k8s_uid> <target_image_tag> <target_deploy_group>
  compose <host>   <project> <compose_files_csv> <service> <target_image_ref>

Examples:
  ack     266 92629155-7299-4e5e-acd0-566e28a4234e h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-20260530 beta-h75-266
  compose JSZX-AI-03 carher-75 /Data/carher-runtime/deploy/carher-75/compose.cicd-75.yaml carher ghcr.io/buyitsydney/carher-runtime@sha256:...

Notes:
  - snapshot is read-only and writes rollback.tsv.
  - apply/rollback are mutating and replay rollback.tsv.
  - verify is Deployment-level only; follow it with 30-post-rollout-audit.sh
    and 50-a2a-functional-probe.sh for functional readiness.
  - ACK rollback restores image, deploy_group, and runtime-profile annotation state.
  - Compose apply uses a temporary override file; rollback removes that override and starts the original compose files.
USAGE
}

die() {
  echo "[fatal] $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

redact() {
  sed -E \
    -e 's/(oc_)[A-Za-z0-9_-]+/\1REDACTED/g' \
    -e 's/(Bearer )[A-Za-z0-9._-]+/\1REDACTED/g' \
    -e 's/([A-Za-z0-9_]*(TOKEN|SECRET|KEY)[A-Za-z0-9_]*=)[^[:space:]]+/\1REDACTED/g'
}

admin_api_key() {
  if [[ -n "${ADMIN_API_KEY:-}" ]]; then
    printf '%s' "$ADMIN_API_KEY"
    return
  fi
  kubectl -n "$NS" get secret carher-admin-secrets \
    -o jsonpath='{.data.admin-api-key}' | base64 -d
}

admin_put_instance() {
  local her_id="$1"
  local image="$2"
  local group="$3"
  local key
  key="$(admin_api_key)"
  curl -fsS -X PUT "$ADMIN_BASE_URL/api/instances/$her_id" \
    -H "X-API-Key: $key" \
    -H "Content-Type: application/json" \
    -d "{\"image\":\"$image\",\"deploy_group\":\"$group\"}" >/dev/null
}

admin_ensure_group() {
  local group="$1"
  local key
  key="$(admin_api_key)"
  curl -fsS -X POST "$ADMIN_BASE_URL/api/deploy-groups" \
    -H "X-API-Key: $key" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$group\",\"priority\":5,\"description\":\"Fast H75 gray rollout\"}" >/dev/null || true
}

ack_wait_ready() {
  local her_id="$1"
  local deploy="carher-$her_id"
  local timeout="${2:-600}"
  local start
  start="$(date +%s)"
  while true; do
    local generation observed desired updated available
    read -r generation observed desired updated available < <(
      kubectl -n "$NS" get deploy "$deploy" \
        -o jsonpath='{.metadata.generation}{" "}{.status.observedGeneration}{" "}{.spec.replicas}{" "}{.status.updatedReplicas}{" "}{.status.availableReplicas}{"\n"}'
    )
    desired="${desired:-0}"
    updated="${updated:-0}"
    available="${available:-0}"
    observed="${observed:-0}"
    if [[ "$observed" -ge "$generation" && "$updated" -eq "$desired" && "$available" -eq "$desired" ]]; then
      echo "[ack] carher-$her_id ready generation=$generation replicas=$desired"
      return
    fi
    if (( $(date +%s) - start >= timeout )); then
      die "timeout waiting for carher-$her_id ready"
    fi
    sleep 5
  done
}

compose_file_flags() {
  local files_csv="$1"
  local flags=""
  local IFS=,
  for file in $files_csv; do
    flags="$flags -f '$file'"
  done
  printf '%s' "$flags"
}

remote() {
  local host="$1"
  shift
  "$JMS" ssh "$host" "$@"
}

plan_targets() {
  local targets="$1"
  local ack_count=0
  local compose_count=0
  while IFS=$'\t ' read -r env a b c d e _rest; do
    [[ -z "${env:-}" || "${env:0:1}" == "#" ]] && continue
    case "$env" in
      ack) ack_count=$((ack_count + 1)) ;;
      compose) compose_count=$((compose_count + 1)) ;;
      *) die "unknown target env: $env" ;;
    esac
  done < "$targets"
  local total=$((ack_count + compose_count))
  echo "[plan] targets=$total ack=$ack_count compose=$compose_count"
  if (( total <= 5 )); then
    echo "[plan] optimized estimate: 20-45 minutes if image/operator/Dify are already green"
  elif (( total <= 10 )); then
    echo "[plan] optimized estimate: 45-90 minutes with two or three waves"
  else
    echo "[plan] split into batches of 5-10 targets"
  fi
  echo "[plan] rollback goal: ACK single target 3-8m; Compose single target 2-6m; 3-5 target wave 10-20m"
  echo "[plan] wave shape: preflight -> 1 target -> verify -> remaining wave -> isolation audit"
}

snapshot_targets() {
  local targets="$1"
  local output="${2:-$ROLLBACK_FILE}"
  mkdir -p "$(dirname "$output")"
  : > "$output"
  while IFS=$'\t ' read -r env a b c d e _rest; do
    [[ -z "${env:-}" || "${env:0:1}" == "#" ]] && continue
    case "$env" in
      ack)
        local her_id="$a"
        local expected_uid="$b"
        local target_image="$c"
        local target_group="$d"
        local instance="her-$her_id"
        local uid image group profile
        uid="$(kubectl -n "$NS" get herinstance "$instance" -o jsonpath='{.metadata.uid}')"
        [[ "$uid" == "$expected_uid" ]] || die "UID mismatch for $instance: $uid != $expected_uid"
        image="$(kubectl -n "$NS" get herinstance "$instance" -o jsonpath='{.spec.image}')"
        group="$(kubectl -n "$NS" get herinstance "$instance" -o jsonpath='{.spec.deployGroup}')"
        profile="$(kubectl -n "$NS" get herinstance "$instance" -o jsonpath='{.metadata.annotations.carher\.io/runtime-profile}' 2>/dev/null || true)"
        printf 'ack\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$her_id" "$uid" "$image" "$group" "${profile:-__none__}" "$target_image" "$target_group" "$TS" >> "$output"
        ;;
      compose)
        local host="$a"
        local project="$b"
        local compose_files="$c"
        local service="$d"
        local target_image="$e"
        local override="/tmp/carher-h75-${project}-${service}-${TS}.override.yaml"
        local flags old_image
        flags="$(compose_file_flags "$compose_files")"
        old_image="$(
          remote "$host" "docker compose -p '$project' $flags images '$service' 2>/dev/null | awk 'NR==2 {print \$2\":\"\$3}'" \
            | sed -n '1p'
        )"
        [[ -n "$old_image" ]] || old_image="$(
          remote "$host" "docker inspect '$service' --format '{{.Config.Image}}' 2>/dev/null" | sed -n '1p'
        )"
        [[ -n "$old_image" ]] || die "cannot resolve current image for $host/$project/$service"
        printf 'compose\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$host" "$project" "$compose_files" "$service" "$old_image" "$target_image" "$override" "$TS" >> "$output"
        ;;
      *) die "unknown target env: $env" ;;
    esac
  done < "$targets"
  echo "[snapshot] wrote $output"
  redact < "$output"
}

apply_state() {
  local state="$1"
  while IFS=$'\t' read -r env a b c d e f g h; do
    [[ -z "${env:-}" || "${env:0:1}" == "#" ]] && continue
    case "$env" in
      ack)
        local her_id="$a"
        local uid="$b"
        local target_image="$f"
        local target_group="$g"
        local instance="her-$her_id"
        local current_uid
        current_uid="$(kubectl -n "$NS" get herinstance "$instance" -o jsonpath='{.metadata.uid}')"
        [[ "$current_uid" == "$uid" ]] || die "UID mismatch for $instance during apply"
        admin_ensure_group "$target_group"
        kubectl -n "$NS" annotate herinstance "$instance" "carher.io/runtime-profile=$RUNTIME_PROFILE" --overwrite
        admin_put_instance "$her_id" "$target_image" "$target_group"
        echo "[ack] apply sent her-$her_id image=$target_image group=$target_group"
        ;;
      compose)
        local host="$a"
        local project="$b"
        local compose_files="$c"
        local service="$d"
        local target_image="$f"
        local override="$g"
        local flags
        flags="$(compose_file_flags "$compose_files")"
        remote "$host" "cat > '$override' <<EOF
services:
  $service:
    image: $target_image
EOF
docker compose -p '$project' $flags -f '$override' up -d --no-deps '$service'"
        echo "[compose] apply sent $host/$project/$service image=$target_image override=$override"
        ;;
      *) die "unknown state env: $env" ;;
    esac
  done < "$state"
}

verify_state() {
  local state="$1"
  while IFS=$'\t' read -r env a b c d e f g h; do
    [[ -z "${env:-}" || "${env:0:1}" == "#" ]] && continue
    case "$env" in
      ack)
        local her_id="$a"
        local target_image="$f"
        ack_wait_ready "$her_id" 600
        kubectl -n "$NS" get deploy "carher-$her_id" \
          -o jsonpath='image={.spec.template.spec.containers[?(@.name=="carher")].image}{"\n"}' \
          | grep -F ":$target_image" >/dev/null || die "target image not active for her-$her_id"
        echo "[ack] verify ok her-$her_id"
        ;;
      compose)
        local host="$a"
        local project="$b"
        local service="$d"
        local target_image="$f"
        remote "$host" "docker compose -p '$project' ps '$service' && docker inspect '$service' --format '{{.Config.Image}}'" \
          | tee /tmp/carher-compose-verify.$$.log | grep -F "$target_image" >/dev/null || die "target image not active for $host/$project/$service"
        echo "[compose] verify ok $host/$project/$service"
        ;;
      *) die "unknown state env: $env" ;;
    esac
  done < "$state"
}

rollback_state() {
  local state="$1"
  while IFS=$'\t' read -r env a b c d e f g h; do
    [[ -z "${env:-}" || "${env:0:1}" == "#" ]] && continue
    case "$env" in
      ack)
        local her_id="$a"
        local uid="$b"
        local old_image="$c"
        local old_group="$d"
        local old_profile="$e"
        local instance="her-$her_id"
        local current_uid
        current_uid="$(kubectl -n "$NS" get herinstance "$instance" -o jsonpath='{.metadata.uid}')"
        [[ "$current_uid" == "$uid" ]] || die "UID mismatch for $instance during rollback"
        if [[ "$old_profile" == "__none__" ]]; then
          kubectl -n "$NS" annotate herinstance "$instance" carher.io/runtime-profile- --overwrite || true
        else
          kubectl -n "$NS" annotate herinstance "$instance" "carher.io/runtime-profile=$old_profile" --overwrite
        fi
        admin_put_instance "$her_id" "$old_image" "$old_group"
        echo "[ack] rollback sent her-$her_id image=$old_image group=$old_group profile=$old_profile"
        ;;
      compose)
        local host="$a"
        local project="$b"
        local compose_files="$c"
        local service="$d"
        local override="$g"
        local flags
        flags="$(compose_file_flags "$compose_files")"
        remote "$host" "rm -f '$override'; docker compose -p '$project' $flags up -d --no-deps '$service'"
        echo "[compose] rollback sent $host/$project/$service removed_override=$override"
        ;;
      *) die "unknown state env: $env" ;;
    esac
  done < "$state"
}

cmd="${1:-}"
case "$cmd" in
  plan)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    plan_targets "$2"
    ;;
  snapshot)
    [[ $# -ge 2 && $# -le 3 ]] || { usage; exit 2; }
    need_cmd kubectl
    snapshot_targets "$2" "${3:-$ROLLBACK_FILE}"
    ;;
  apply)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    need_cmd kubectl
    need_cmd curl
    apply_state "$2"
    ;;
  verify)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    need_cmd kubectl
    verify_state "$2"
    ;;
  rollback)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    need_cmd kubectl
    need_cmd curl
    rollback_state "$2"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
