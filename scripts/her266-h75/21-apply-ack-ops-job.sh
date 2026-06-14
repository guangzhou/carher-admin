#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-carher}"
JOB_NAME="${JOB_NAME:-her266-h75-ops}"
JOB_ACTION="${JOB_ACTION:-all}"
OPS_IMAGE="${OPS_IMAGE:-alpine/k8s:1.30.14}"
OPS_SERVICE_ACCOUNT="${OPS_SERVICE_ACCOUNT:-carher-admin}"
H75_CONFIG_DIR="${H75_CONFIG_DIR:-/tmp/her266-h75-config}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DIFY_BOOTSTRAP_TOKEN_FROM_SECRET="${DIFY_BOOTSTRAP_TOKEN_FROM_SECRET:-}"
DIFY_BOOTSTRAP_TOKEN_FROM_KEY="${DIFY_BOOTSTRAP_TOKEN_FROM_KEY:-token}"
DIFY_BOOTSTRAP_SOURCE_SECRET="${DIFY_BOOTSTRAP_SOURCE_SECRET:-}"
DIFY_BOOTSTRAP_SOURCE_KEY="${DIFY_BOOTSTRAP_SOURCE_KEY:-token}"

for file in "$SCRIPT_DIR/20-ack-her266-ops.sh" "$H75_CONFIG_DIR/base.json5" "$H75_CONFIG_DIR/docker.json5"; do
  if [[ ! -f "$file" ]]; then
    echo "missing required file: $file" >&2
    exit 2
  fi
done

kubectl -n "$NS" create configmap her266-h75-ops-scripts \
  --from-file=20-ack-her266-ops.sh="$SCRIPT_DIR/20-ack-her266-ops.sh" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" create configmap her266-h75-config \
  --from-file=base.json5="$H75_CONFIG_DIR/base.json5" \
  --from-file=docker.json5="$H75_CONFIG_DIR/docker.json5" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" delete job "$JOB_NAME" --ignore-not-found

token_env=""
if [[ -n "$DIFY_BOOTSTRAP_TOKEN_FROM_SECRET" ]]; then
  token_env="$(cat <<EOF
            - name: DIFY_BOOTSTRAP_TOKEN
              valueFrom:
                secretKeyRef:
                  name: $DIFY_BOOTSTRAP_TOKEN_FROM_SECRET
                  key: $DIFY_BOOTSTRAP_TOKEN_FROM_KEY
EOF
)"
fi

source_secret_env=""
if [[ -n "$DIFY_BOOTSTRAP_SOURCE_SECRET" ]]; then
  source_secret_env="$(cat <<EOF
            - name: DIFY_BOOTSTRAP_SOURCE_SECRET
              value: "$DIFY_BOOTSTRAP_SOURCE_SECRET"
            - name: DIFY_BOOTSTRAP_SOURCE_KEY
              value: "$DIFY_BOOTSTRAP_SOURCE_KEY"
EOF
)"
fi

cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB_NAME
  namespace: $NS
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app: $JOB_NAME
    spec:
      restartPolicy: Never
      serviceAccountName: $OPS_SERVICE_ACCOUNT
      imagePullSecrets:
        - name: acr-vpc-secret
        - name: acr-secret
      containers:
        - name: ops
          image: $OPS_IMAGE
          imagePullPolicy: IfNotPresent
          command: ["/bin/sh", "-lc"]
          args:
            - |
              set -eu
              apk add --no-cache bash curl coreutils grep sed gawk ca-certificates >/tmp/her266-h75-apk.log
              cp /ops/20-ack-her266-ops.sh /tmp/20-ack-her266-ops.sh
              chmod +x /tmp/20-ack-her266-ops.sh
              bash /tmp/20-ack-her266-ops.sh "$JOB_ACTION"
          env:
            - name: ROOT
              value: /ops-state
            - name: H75_CONFIG_DIR
              value: /h75-config
$token_env
$source_secret_env
          volumeMounts:
            - name: ops-scripts
              mountPath: /ops
              readOnly: true
            - name: h75-config
              mountPath: /h75-config
              readOnly: true
            - name: ops-state
              mountPath: /ops-state
      volumes:
        - name: ops-scripts
          configMap:
            name: her266-h75-ops-scripts
            defaultMode: 0755
        - name: h75-config
          configMap:
            name: her266-h75-config
        - name: ops-state
          emptyDir: {}
EOF

cat <<EOF
started ACK ops job:
  namespace: $NS
  job:       $JOB_NAME
  action:    $JOB_ACTION

watch:
  kubectl -n $NS logs -f job/$JOB_NAME
  kubectl -n $NS get job,pod -l app=$JOB_NAME
EOF
