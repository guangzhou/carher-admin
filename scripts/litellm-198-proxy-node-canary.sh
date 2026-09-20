#!/bin/bash
# litellm-198-proxy-node-canary.sh — 在目标节点起金丝雀 pod，验证能承载 prod proxy
#
# 在 198 host 上以 sudo 跑：
#   sudo bash litellm-198-proxy-node-canary.sh <role_label>   # 如 new-node
#
# 验证四件事（缺一不可，全绿才允许迁 proxy）：
# 1. prod 同款镜像能从本地 registry(127.0.0.1:5000→registries.yaml mirror)拉起
# 2. Pod 内出口可达全部外呼上游（与 host 出口是两回事，走 CNI SNAT）
# 3. 跨节点 Pod 网络（VXLAN + kube-proxy）：redis/db svc TCP 通
# 4. chatgpt-acct svc 可达 —— 必须挑「有 endpoint 的」acct 测：
#    connection refused 大概率是该 acct 本来就 scale=0（kube-proxy REJECT），
#    先 kubectl get endpoints 确认非空再测，别把下线号误判成节点网络故障。
set -euo pipefail

ROLE=${1:?usage: role label, e.g. new-node}
NS=litellm-product
IMAGE=$(kubectl -n $NS get deploy litellm-proxy -o jsonpath='{.spec.template.spec.containers[0].image}')

kubectl -n $NS delete pod canary-$ROLE --ignore-not-found --wait=true
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: canary-$ROLE
  namespace: $NS
spec:
  nodeSelector: {role: "$ROLE"}
  tolerations:
  - {key: dedicated, operator: Equal, value: "$ROLE", effect: NoSchedule}
  - {key: dedicated, operator: Equal, value: standby, effect: NoSchedule}
  restartPolicy: Never
  containers:
  - name: canary
    image: $IMAGE
    command: ["sleep", "600"]
    resources:
      requests: {cpu: 100m, memory: 256Mi}
      limits: {memory: 1Gi}
EOF
kubectl -n $NS wait --for=condition=Ready pod/canary-$ROLE --timeout=300s
kubectl -n $NS get pod canary-$ROLE -o wide

# 挑一个有活 endpoint 的 acct svc（避免下线号假阴性）
ACCT=$(kubectl -n $NS get endpoints -o json | python3 -c "
import json,sys
for e in json.load(sys.stdin)['items']:
    if e['metadata']['name'].startswith('chatgpt-acct-') and e.get('subsets'):
        print(e['metadata']['name']); break")

kubectl -n $NS exec canary-$ROLE -- python3 -c "
import urllib.request, socket
def probe(url):
    try:
        r = urllib.request.urlopen(url, timeout=10); print(url, '->', r.status)
    except urllib.error.HTTPError as e: print(url, '->', e.code)
    except Exception as e: print(url, '-> FAIL:', type(e).__name__, e)
probe('https://aigateway.edgecloudapp.com/')
probe('https://kuaihuiai.com/')
probe('https://openrouter.ai/api/v1/models')
probe('http://10.68.13.188:4130/health')
for host, port in [('litellm-redis.$NS.svc.cluster.local', 6379),
                   ('litellm-db.$NS.svc.cluster.local', 5432),
                   ('$ACCT.$NS.svc.cluster.local', 4000)]:
    try:
        s = socket.create_connection((host, port), timeout=5); s.close(); print(host, port, '-> TCP OK')
    except Exception as e: print(host, port, '-> FAIL:', type(e).__name__, e)
"
kubectl -n $NS delete pod canary-$ROLE --wait=false
echo "DONE: 全部 -> 200/404/307/TCP OK 才算通过；任一 FAIL 禁止迁移"
