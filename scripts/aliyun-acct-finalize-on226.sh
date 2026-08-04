#!/usr/bin/env bash
# aliyun-acct-finalize-on226.sh — 在 k8s-work-226 节点上跑(不依赖本地隧道)。
# 等价于 scripts/aliyun-grinder.sh 的 C/D/F 三段:建 PVC+Deploy+Svc → auth.json 落 PVC
# → rollout → pod 内 auth 校验 → 直连流式 smoke。**不碰任何 ConfigMap**(入池是下一步)。
#
# 前提:/tmp/auth-acct-<N>.json 已推到本节点(scripts/push226.sh,带 sha 断言)。
# 用法:bash /tmp/aliyun-acct-finalize.sh 132 133 134
set -uo pipefail
NS=carher
OK=""
for N in "$@"; do
  echo "===== acct-$N ====="
  A=/tmp/auth-acct-$N.json
  python3 -c "import json,sys;d=json.load(open('$A'));sys.exit(0 if len(d.get('access_token',''))>1000 else 1)" \
    || { echo "!!!! acct-$N 节点上 auth.json 无效/缺失 — skip"; continue; }

  if ! kubectl -n $NS get deploy chatgpt-acct-$N >/dev/null 2>&1; then
    cat > /tmp/aliyun-acct-$N.yaml <<EOF
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: chatgpt-acct-$N-auth, namespace: carher, labels: {pool: chatgpt-acct, account: "$N"}}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: alibabacloud-cnfs-nas
  resources: {requests: {storage: 1Gi}}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chatgpt-acct-$N
  namespace: carher
  labels: {app: chatgpt-acct-$N, pool: chatgpt-acct, account: "$N"}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector:
    matchLabels: {app: chatgpt-acct-$N}
  template:
    metadata:
      labels: {app: chatgpt-acct-$N, pool: chatgpt-acct, account: "$N"}
    spec:
      containers:
        - name: litellm
          image: ghcr.io/berriai/litellm:v1.90.2
          imagePullPolicy: IfNotPresent
          args: ["--config", "/app/config.yaml", "--port", "4000"]
          ports: [{containerPort: 4000}]
          env:
            - {name: CHATGPT_TOKEN_DIR, value: /chatgpt-auth}
            - name: LITELLM_MASTER_KEY
              valueFrom:
                secretKeyRef: {name: chatgpt-pool-master-key, key: LITELLM_MASTER_KEY}
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: "1", memory: 2Gi}
          readinessProbe:
            httpGet: {path: /health/readiness, port: 4000}
            initialDelaySeconds: 30
            periodSeconds: 10
            failureThreshold: 12
          volumeMounts:
            - {name: config, mountPath: /app/config.yaml, subPath: config.yaml, readOnly: true}
            - {name: auth, mountPath: /chatgpt-auth}
            - {name: responses-patch, subPath: transformation.py,
                mountPath: /app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py}
      volumes:
        - {name: config, configMap: {name: chatgpt-pool-config, defaultMode: 420}}
        - {name: auth, persistentVolumeClaim: {claimName: chatgpt-acct-$N-auth}}
        - {name: responses-patch, configMap: {name: chatgpt-responses-patch, defaultMode: 420}}
---
apiVersion: v1
kind: Service
metadata:
  name: chatgpt-acct-$N
  namespace: carher
  labels: {app: chatgpt-acct-$N, pool: chatgpt-acct, account: "$N"}
spec:
  type: ClusterIP
  selector: {app: chatgpt-acct-$N}
  ports: [{port: 4000, targetPort: 4000}]
EOF
    APPLIED=0
    for t in 1 2 3; do
      kubectl apply -f /tmp/aliyun-acct-$N.yaml >/dev/null 2>&1
      kubectl -n $NS get deploy chatgpt-acct-$N >/dev/null 2>&1 && { APPLIED=1; break; }
      sleep 3
    done
    [ "$APPLIED" = 1 ] || { echo "!!!! acct-$N apply 失败 — skip(防半接入)"; continue; }
    echo "  ✓ PVC+Deployment+Service 已建"
  else
    echo "  ✓ deploy 已存在"
  fi

  PODTOK=0
  for vfix in 1 2 3; do
    POD=""
    for i in $(seq 1 30); do
      POD=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
      [ -n "$POD" ] && [ "$(kubectl -n $NS get pod $POD -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] && break
      sleep 4
    done
    [ -n "$POD" ] || { echo "  ⚠ acct-$N pod 未起, 重试"; sleep 10; continue; }
    kubectl cp $A $NS/$POD:/chatgpt-auth/auth.json -c litellm >/dev/null 2>&1
    kubectl -n $NS rollout restart deploy/chatgpt-acct-$N >/dev/null 2>&1
    kubectl -n $NS rollout status deploy/chatgpt-acct-$N --timeout=240s 2>&1 | tail -1
    sleep 10
    VPOD=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    PODTOK=$(kubectl -n $NS exec $VPOD -c litellm -- python3 -c \
      "import json;print(len(json.load(open('/chatgpt-auth/auth.json')).get('access_token','')))" 2>/dev/null | tr -dc 0-9)
    if [ "${PODTOK:-0}" -gt 1000 ]; then echo "  ✓ acct-$N pod auth 有效 (access_len=$PODTOK)"; break; fi
    echo "  ⚠ acct-$N pod auth 空壳 (access_len=${PODTOK:-0}) — 第 $vfix 轮重写"
  done
  [ "${PODTOK:-0}" -gt 1000 ] || { echo "!!!! acct-$N auth 落盘失败 — skip"; continue; }
  OK="$OK $N"
done

echo "===== smoke (直连各 acct 自己的 litellm, 必须流式) ====="
for N in $OK; do
  APOD=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  READY=$(kubectl -n $NS get pod $APOD -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)
  R=$(kubectl -n $NS exec $APOD -c litellm -- python3 -c "
import json,os,urllib.request
r=urllib.request.Request('http://localhost:4000/v1/chat/completions',
 data=json.dumps({'model':'chatgpt-gpt-5.5','messages':[{'role':'user','content':'say OK'}],
                  'max_tokens':16,'stream':True}).encode(),
 headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],'Content-Type':'application/json'})
try:
    resp=urllib.request.urlopen(r,timeout=120)
    body=resp.read(4000).decode('utf-8','replace')
    print('STATUS',resp.status,'CHUNKS',body.count('data:'),'HASCONTENT',int('\"content\"' in body))
except Exception as e:
    print('STATUS',getattr(e,'code','ERR'),'ERRBODY',(getattr(e,'read',lambda:b'')() or b'')[:200].decode('utf-8','replace'))
" 2>&1 | tr -d '\r' | tail -1)
  echo "  SMOKE acct-$N ready=$READY $R"
done
echo "FINALIZE_DONE ok=$OK"
