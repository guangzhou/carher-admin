#!/usr/bin/env bash
# new-pod.sh — build brand-new web-only zerokey pod(s) on 225 for onboarded
# chatgpt accounts. Run from a machine with SSH to BOTH 188 (capture) and 198
# (kubectl to the 225 cluster).  Pipeline per acct N:
#   1. capture chatgpt.com web session on 188 (patchright + mail OTP)
#   2. relay users.json 188 -> local -> 198
#   3. seed 225 hostPath /Data/zerokey-sessions/zero-N via a kubectl bootstrap pod
#      (225 SSH is NOT used — password rotates; kubectl-cp is the reliable path)
#   4. apply Deployment+Service (with the capability cp lines) on 198
#   5. register into LiteLLM zerokey-pool-gpt-5.5 + 5.6-{sol,terra,luna}
#
# Prereq: acct creds at 188 /Data/chatgpt-auth/acct-<N>/.creds; zerokey-capture
# image on 188; zk-image-patch CM present on 225.
#
# Usage:  ./new-pod.sh 100 101 102        (REGISTER=0 to skip LiteLLM step)
# ⚠️ Mass CF logins risk holds — canary first, don't storm 40x. Captures here
# are serial by nature (one acct at a time).
set -u
H188="cltx@10.68.13.188"; H198="cltx@10.68.13.198"
K="sudo k3s kubectl -n litellm-product"
MK="${LITELLM_MK:-sk-pro-litellm-ce077e2b0721bb419a633e4d}"
REGISTER="${REGISTER:-1}"
SC="ssh -o ConnectTimeout=20"

manifest() { local N="$1"; cat <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: zero-$N, namespace: litellm-product, labels: {account: "$N", app: zero-$N, pool: zerokey-web}}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: zero-$N}}
  template:
    metadata: {labels: {app: zero-$N, pool: zerokey-web}}
    spec:
      nodeName: aiyjy-litellm-standby
      tolerations: [{effect: NoSchedule, key: dedicated, value: standby}]
      dnsPolicy: None
      dnsConfig: {nameservers: ["1.1.1.1","8.8.8.8"]}
      containers:
      - name: zerokey
        image: docker.io/library/zerokey-codex:latest
        imagePullPolicy: Never
        command: ["sh","-c"]
        args:
        - |
          cp /patch/zerokey-serve-codex.js /app/zerokey-serve-codex.js
          cp /patch/images.js /app/routes/images.js
          cp /patch/api.js /app/core/chatgpt/api.js
          cp /patch/web-tools.js /app/routes/web-tools.js
          cp /patch/raw.js /app/routes/raw.js
          cp /patch/responses.js /app/routes/responses.js
          exec node /app/zerokey-serve-codex.js
        env:
        - {name: PORT, value: "8200"}
        - {name: ZK_USER, value: acct$N}
        - {name: ZK_DEFAULT_MODEL, value: gpt-5-5}
        ports: [{containerPort: 8200}]
        readinessProbe: {httpGet: {path: /health, port: 8200}, initialDelaySeconds: 5, periodSeconds: 15}
        resources: {requests: {cpu: 20m, memory: 64Mi}, limits: {cpu: 500m, memory: 192Mi}}
        volumeMounts:
        - {mountPath: /app/temp, name: session-data}
        - {mountPath: /patch, name: patch-files, readOnly: true}
      volumes:
      - {name: session-data, hostPath: {path: /Data/zerokey-sessions/zero-$N, type: Directory}}
      - {name: patch-files, configMap: {name: zk-image-patch}}
---
apiVersion: v1
kind: Service
metadata: {name: zero-$N, namespace: litellm-product, labels: {app: zero-$N, pool: zerokey-web}}
spec: {type: ClusterIP, selector: {app: zero-$N}, ports: [{port: 8200, targetPort: 8200, protocol: TCP}]}
YAML
}

register() { local N="$1"; $SC $H198 "python3 - <<PY
import json,urllib.request
def api(m,p,d):
    r=urllib.request.Request('http://10.68.13.198:30402'+p,data=json.dumps(d).encode(),headers={'Authorization':'Bearer $MK','Content-Type':'application/json'},method=m)
    urllib.request.urlopen(r,timeout=20).read()
V={'5.5':'gpt-5.5','5.6-sol':'gpt-5.6-sol','5.6-terra':'gpt-5.6-terra','5.6-luna':'gpt-5.6-luna'}
for v,slug in V.items():
    g='zerokey-pool-gpt-'+v
    try: api('POST','/pro/model/new',{'model_name':g,'litellm_params':{'model':'openai/'+slug,'api_base':'http://zero-$N.litellm-product.svc.cluster.local:8200/v1','api_key':'raw','rpm':30,'input_cost_per_token':5e-6,'output_cost_per_token':3e-5},'model_info':{'id':'zk-$N-gpt-'+v,'mode':'responses'}}); print('  registered zk-$N-gpt-'+v)
    except Exception as e: print('  zk-$N-gpt-'+v,'FAIL',str(e)[:40])
PY"; }

for N in "$@"; do
  echo "=== zero-$N: capture on 188 ==="
  $SC $H188 "bash /tmp/zk-capture-one.sh $N acct$N" 2>&1 | tail -1
  if ! $SC $H188 "test -f /Data/zkcaps/zkcap-$N/out/zerokey-users.json"; then
    echo "zero-$N SKIP: capture failed"; continue
  fi
  echo "=== zero-$N: relay session 188->198 ==="
  $SC $H188 "base64 -w0 /Data/zkcaps/zkcap-$N/out/zerokey-users.json" | \
    $SC $H198 "base64 -d > /tmp/zk$N-users.json"
  echo "=== zero-$N: seed 225 hostPath via kubectl bootstrap ==="
  $SC $H198 "
$K delete pod zkseed-$N --ignore-not-found >/dev/null 2>&1
cat > /tmp/zkseed-$N.yaml <<Y
apiVersion: v1
kind: Pod
metadata: {name: zkseed-$N, namespace: litellm-product}
spec:
  nodeName: aiyjy-litellm-standby
  tolerations: [{effect: NoSchedule, key: dedicated, value: standby}]
  restartPolicy: Never
  containers:
  - {name: b, image: docker.io/library/zerokey-codex:latest, imagePullPolicy: Never, command: [sh,-c,'mkdir -p /sess/zero-$N && sleep 300'], volumeMounts: [{mountPath: /sess, name: s}]}
  volumes: [{name: s, hostPath: {path: /Data/zerokey-sessions, type: Directory}}]
Y
$K apply -f /tmp/zkseed-$N.yaml >/dev/null 2>&1
$K wait --for=condition=Ready pod/zkseed-$N --timeout=60s >/dev/null 2>&1
$K cp /tmp/zk$N-users.json zkseed-$N:/sess/zero-$N/users.json
$K delete pod zkseed-$N --ignore-not-found >/dev/null 2>&1
"
  echo "=== zero-$N: apply Deployment+Service ==="
  manifest "$N" | $SC $H198 "cat > /tmp/zero-$N.yaml && $K apply -f /tmp/zero-$N.yaml" 2>&1 | sed 's/^/  /'
  $SC $H198 "$K rollout status deploy/zero-$N --timeout=120s" 2>&1 | tail -1 | sed 's/^/  /'
  [ "$REGISTER" = "1" ] && register "$N"
  echo "zero-$N DONE"
done
