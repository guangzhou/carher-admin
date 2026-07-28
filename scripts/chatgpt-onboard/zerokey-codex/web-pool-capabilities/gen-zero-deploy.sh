#!/bin/bash
N="$1"
cat <<YAML
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
              until [ -f /app/temp/users.json ]; do echo "[init] waiting for users.json"; sleep 5; done
              cp /patch/zerokey-serve-codex.js /app/zerokey-serve-codex.js
              cp /patch/images.js /app/routes/images.js
              cp /patch/api.js /app/core/chatgpt/api.js
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
        - {name: session-data, hostPath: {path: /Data/zerokey-sessions/zero-$N, type: DirectoryOrCreate}}
        - {name: patch-files, configMap: {name: zk-image-patch}}
---
apiVersion: v1
kind: Service
metadata: {name: zero-$N, namespace: litellm-product, labels: {app: zero-$N, pool: zerokey-web}}
spec:
  selector: {app: zero-$N}
  ports: [{port: 8200, targetPort: 8200, protocol: TCP}]
YAML
