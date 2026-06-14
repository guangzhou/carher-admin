#!/usr/bin/env bash
# her-self-restart: toggle enableLivenessProbe on HerInstance(s).
# Usage:
#   enable.sh on  <uid|all>
#   enable.sh off <uid|all>
#   enable.sh status
#
# Stage 2 (self-restart skill + admin /self/restart endpoint) is provisioned
# cluster-wide and has no per-instance toggle — it's gated only by the LLM
# choosing to invoke the skill on user prompt. See SKILL.md for caveats.
set -euo pipefail

NS=carher
LABEL_SEL="app=carher-user"
SHARED_SKILL_PATH=/Data/nas-429a1fe7-c0cb-4cf4-9349-8268e39c9acb/self-restart

usage() { sed -n '2,12p' "$0" >&2; exit 64; }

require_kubectl() {
  if ! kubectl get ns "$NS" >/dev/null 2>&1; then
    echo "❌ kubectl can't reach the cluster. Did you forget 'jms proxy laoyang 16443 ...'?" >&2
    exit 1
  fi
}

cmd=${1:-}; target=${2:-}
[[ -z "$cmd" ]] && usage

case "$cmd" in
  on|off)
    [[ -z "$target" ]] && usage
    require_kubectl
    bool=$( [[ "$cmd" == on ]] && echo true || echo false )

    if [[ "$target" == all ]]; then
      echo "🔁 setting enableLivenessProbe=$bool on ALL HerInstances …"
      kubectl get herinstance -n "$NS" -o name |
        xargs -P 5 -I{} kubectl patch {} -n "$NS" --type=merge \
          -p "{\"spec\":{\"enableLivenessProbe\":$bool}}"
    else
      kubectl patch "herinstance/her-${target}" -n "$NS" --type=merge \
        -p "{\"spec\":{\"enableLivenessProbe\":$bool}}"
    fi
    ;;

  status)
    require_kubectl
    echo "── HerInstance enableLivenessProbe ──"
    kubectl get herinstance -n "$NS" -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
on = sum(1 for h in d['items'] if h['spec'].get('enableLivenessProbe'))
print(f'enabled: {on} / {len(d[\"items\"])}')"
    echo
    echo "── Pods carrying livenessProbe + roll convergence ──"
    kubectl get pods -n "$NS" -l "$LABEL_SEL" -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
states = {}
probed = 0
for p in d['items']:
    s = p['status'].get('phase', '?')
    if p['metadata'].get('deletionTimestamp'): s = 'Terminating'
    states[s] = states.get(s, 0) + 1
    for c in p['spec']['containers']:
        if c['name']=='carher' and c.get('livenessProbe'):
            probed += 1; break
print(f'pods total: {len(d[\"items\"])} | probed: {probed} | phases: {states}')"
    echo
    echo "── Recent Liveness probe failures (last 1h) ──"
    kubectl get events -n "$NS" --field-selector reason=Unhealthy \
      --sort-by=lastTimestamp 2>/dev/null |
      grep -i "Liveness" | tail -10 || echo "  (none)"
    ;;

  skill-check)
    require_kubectl
    echo "── self-restart skill on NAS (build server view) ──"
    scripts/jms ssh k8s-work-227 "ls -la ${SHARED_SKILL_PATH}/ 2>&1 || echo 'NOT FOUND'"
    echo
    echo "── visible from a sample her pod? ──"
    SAMPLE=$(kubectl get pod -n "$NS" -l "$LABEL_SEL" -o jsonpath='{.items[0].metadata.name}')
    kubectl -n "$NS" exec "$SAMPLE" -c carher -- ls /data/.openclaw/skills/self-restart/ 2>&1 || \
      echo "  ❌ self-restart not visible to $SAMPLE"
    echo
    echo "── admin /self/restart endpoint reachable from cluster? (dry probe with bad src) ──"
    ADMIN_POD=$(kubectl get pod -n "$NS" -l app=carher-admin -o jsonpath='{.items[0].metadata.name}')
    kubectl -n "$NS" exec "$ADMIN_POD" -- \
      curl -sS -o /dev/null -w 'HTTP %{http_code} (expecting 403 from admin IP)\n' \
        -X POST http://localhost:8900/api/instances/self/restart --max-time 5 || true
    ;;

  *) usage ;;
esac
