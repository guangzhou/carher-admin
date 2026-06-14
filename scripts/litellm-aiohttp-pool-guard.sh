#!/bin/bash
# Watch LiteLLM proxy aiohttp pool dead-conn leak, auto-restart on threshold.
# Triggers (any one):
#   - any pod CLOSE_WAIT >= 100
#   - any pod CLOSE_WAIT/ESTABLISHED >= 1.5
#   - aggregate CLOSE_WAIT >= 180
# Cooldown: skip restart if previous restart < 30min ago.
# Doc: ~/.claude/skills/chatgpt-pro-litellm/SKILL.md "aiohttp pool 死链泄漏排查"
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
NS=litellm-product
DEPLOY=litellm-proxy
LABEL=app=litellm-proxy
COOLDOWN_MIN=30
THR_PER_POD=100
THR_RATIO=15        # 1.5 * 10 (integer arithmetic)
THR_AGG=180
STATE=/var/lib/litellm-ops/last-restart
LOG=/var/log/litellm-ops/aiohttp-pool-guard.log
mkdir -p "$(dirname "$STATE")" "$(dirname "$LOG")"

ts(){ date +"%Y-%m-%d %H:%M:%S"; }
log(){ echo "[$(ts)] $*" >>"$LOG"; }

PODS=$(kubectl get pods -n "$NS" -l "$LABEL" -o jsonpath="{range .items[?(@.status.phase==\"Running\")]}{.metadata.name}{\"\n\"}{end}" 2>/dev/null)
[ -z "$PODS" ] && { log "no running pods, skip"; exit 0; }

agg_cw=0; agg_est=0; worst_cw=0; worst_ratio=0; worst_pod=""; trigger=""
detail=""
for POD in $PODS; do
  COUNTS=$(kubectl exec -n "$NS" "$POD" -- sh -c "awk \"NR>1{print \\\$4}\" /proc/net/tcp | sort | uniq -c" 2>/dev/null)
  CW=$(echo "$COUNTS" | awk "/ 08\$/ {print \$1}"); CW=${CW:-0}
  ES=$(echo "$COUNTS" | awk "/ 01\$/ {print \$1}"); ES=${ES:-0}
  RATIO10=0
  [ "$ES" -gt 0 ] && RATIO10=$(( CW * 10 / ES ))
  [ "$ES" -eq 0 ] && [ "$CW" -gt 0 ] && RATIO10=999
  detail="$detail  $POD: CW=$CW EST=$ES ratio=$(awk "BEGIN{printf \"%.2f\", $RATIO10/10}")\n"
  agg_cw=$(( agg_cw + CW )); agg_est=$(( agg_est + ES ))
  [ "$CW" -gt "$worst_cw" ] && worst_cw=$CW && worst_pod=$POD
  [ "$RATIO10" -gt "$worst_ratio" ] && worst_ratio=$RATIO10
  [ "$CW" -ge "$THR_PER_POD" ] && trigger="per-pod CW=$CW>=$THR_PER_POD on $POD"
  [ -z "$trigger" ] && [ "$RATIO10" -ge "$THR_RATIO" ] && trigger="ratio=$RATIO10/10 on $POD (CW=$CW EST=$ES)"
done
[ -z "$trigger" ] && [ "$agg_cw" -ge "$THR_AGG" ] && trigger="aggregate CW=$agg_cw>=$THR_AGG"

if [ -z "$trigger" ]; then
  log "OK agg CW=$agg_cw EST=$agg_est worst_pod=$worst_pod worst_cw=$worst_cw worst_ratio=$(awk "BEGIN{printf \"%.2f\", $worst_ratio/10}")"
  exit 0
fi

# Cooldown
if [ -f "$STATE" ]; then
  AGE=$(( $(date +%s) - $(stat -c %Y "$STATE") ))
  if [ "$AGE" -lt $(( COOLDOWN_MIN * 60 )) ]; then
    log "TRIGGER $trigger but cooldown ${AGE}s<$(( COOLDOWN_MIN*60 ))s, skip restart"
    exit 0
  fi
fi

log "TRIGGER $trigger -- restarting"
log "$(echo -e "$detail")"
# Verify strategy patched (must be maxSurge=0 for single-node 7c)
STRAT=$(kubectl get deploy "$DEPLOY" -n "$NS" -o jsonpath="{.spec.strategy.rollingUpdate.maxSurge}" 2>/dev/null)
if [ "$STRAT" != "0" ]; then
  log "WARN maxSurge=$STRAT (expect 0), patching first"
  kubectl patch deploy "$DEPLOY" -n "$NS" --type=strategic -p "{\"spec\":{\"strategy\":{\"rollingUpdate\":{\"maxSurge\":0,\"maxUnavailable\":1}}}}" >>"$LOG" 2>&1
fi
kubectl rollout restart deployment/"$DEPLOY" -n "$NS" >>"$LOG" 2>&1
kubectl rollout status deployment/"$DEPLOY" -n "$NS" --timeout=300s >>"$LOG" 2>&1
RC=$?
touch "$STATE"
if [ $RC -eq 0 ]; then
  sleep 5
  POST=""
  for POD in $(kubectl get pods -n "$NS" -l "$LABEL" -o jsonpath="{range .items[?(@.status.phase==\"Running\")]}{.metadata.name}{\"\n\"}{end}"); do
    C=$(kubectl exec -n "$NS" "$POD" -- sh -c "awk \"NR>1{print \\\$4}\" /proc/net/tcp | grep -c \" 08\$\"" 2>/dev/null)
    POST="$POST $POD:CW=$C"
  done
  log "RESTART DONE rc=0 post=$POST"
else
  log "RESTART FAILED rc=$RC -- check kubectl rollout status manually"
fi
