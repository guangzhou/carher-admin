#!/usr/bin/env bash
set -euo pipefail

DIFY_NS="${DIFY_NS:-dify}"

echo "== Dify HA audit =="
echo "namespace=$DIFY_NS"

kubectl -n "$DIFY_NS" get deploy -o json \
  | jq -r '.items[] | [
      .metadata.name,
      ((.spec.replicas // 1) | tostring),
      ((.status.availableReplicas // 0) | tostring),
      (.spec.template.spec.containers | map(.name + "=" + .image) | join(","))
    ] | @tsv' \
  | awk 'BEGIN {print "deployment\tdesired\tavailable\tcontainers"} {print}'

echo
echo "== single-replica risk =="
kubectl -n "$DIFY_NS" get deploy -o json \
  | jq -r '.items[] | select((.spec.replicas // 1) < 2) | .metadata.name' \
  | sed 's/^/[WARN] /'

echo
echo "== pdb =="
if ! kubectl -n "$DIFY_NS" get pdb 2>/dev/null; then
  echo "[WARN] no PodDisruptionBudget found"
elif [[ -z "$(kubectl -n "$DIFY_NS" get pdb --no-headers 2>/dev/null | sed '/^$/d')" ]]; then
  echo "[WARN] no PodDisruptionBudget found"
fi

echo
echo "== services =="
kubectl -n "$DIFY_NS" get svc -o wide

cat <<'NOTE'

Interpretation:
- Current raw Dify stack is "available" if every deployment is 1/1 and healthz passes.
- It is not strict high availability while api/web/worker/bootstrap/nginx and stateful db/redis/weaviate are single replica.
- Safer next phase: scale stateless api/web/worker/bootstrap/nginx first, add PDBs, then move db/redis/vector store to managed HA or stateful HA before claiming Dify HA.
NOTE
