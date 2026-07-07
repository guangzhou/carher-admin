#!/usr/bin/env bash
# aliyun-zerokey-gen.sh — 生成阿里云原生 zerokey pool 的 per-account manifest
# （serve Deployment + capture CronJob），并可选注册进 litellm zerokey-pool。
#
# 前提（每个 acct 一次性）：
#   1. secret zerokey-acct-<N>-creds（MAIL_USER/MAIL_PW/CHATGPT_PW）
#   2. PVC zerokey-acct-<N>-state
#   3. 首次 capture 成功（users.json 落 PVC）——见 aliyun-zerokey-capture-once.sh
#
# 隔离：hostNetwork 钉 EIP 节点，走节点 EIP（与线上 codex 共享 NAT 隔离）。
#   EIP 节点：.86=47.236.200.98, .122=47.84.85.100（dify 节点）
#   端口：8100+acct（hostPort，节点内唯一）
#
# 用法：
#   ./scripts/aliyun-zerokey-gen.sh 69 70 71 ...        # 打印 manifest 到 stdout
#   ./scripts/aliyun-zerokey-gen.sh --apply 70 71       # apply
set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && { APPLY=1; shift; }
[[ $# -lt 1 ]] && { echo "usage: $0 [--apply] N1 N2 ..."; exit 1; }

REG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher
SERVE_IMG="$REG:zerokey-serve-aliyun-20260707"
CAP_IMG="$REG:zerokey-capture-aliyun-20260707-otp3"
# EIP-bearing nodes (round-robin to spread load / EIP egress)
NODES=(ap-southeast-1.172.16.0.86 ap-southeast-1.172.16.16.122)

TMP=$(mktemp)
i=0
for N in "$@"; do
  NODE=${NODES[$(( i % ${#NODES[@]} ))]}
  PORT=$(( 8100 + N ))
  i=$((i+1))
  cat >> "$TMP" <<YAML
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: zerokey-serve-$N
  namespace: carher
  labels: {app: zerokey-serve-$N, pool: zerokey, account: "$N"}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: zerokey-serve-$N}}
  template:
    metadata: {labels: {app: zerokey-serve-$N, pool: zerokey, account: "$N"}}
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      nodeName: $NODE
      imagePullSecrets: [{name: acr-vpc-secret}]
      containers:
        - name: serve
          image: $SERVE_IMG
          env:
            - {name: PORT, value: "$PORT"}
            - {name: ZK_USER, value: acct$N}
            - {name: ZK_DEFAULT_MODEL, value: gpt-5-5}
          command: ["sh","-c","mkdir -p /app/temp && cp /state/users.json /app/temp/users.json && exec node zerokey-serve-codex.js"]
          ports: [{containerPort: $PORT, hostPort: $PORT}]
          resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 500m, memory: 256Mi}}
          volumeMounts: [{name: state, mountPath: /state, readOnly: true}]
      volumes:
        - {name: state, persistentVolumeClaim: {claimName: zerokey-acct-$N-state}}
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: zerokey-capture-$N
  namespace: carher
  labels: {app: zerokey-capture-$N, pool: zerokey, account: "$N"}
spec:
  schedule: "$(( (N * 7) % 60 )) */6 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 900
      template:
        spec:
          restartPolicy: Never
          hostNetwork: true
          dnsPolicy: ClusterFirstWithHostNet
          nodeName: $NODE
          serviceAccountName: zerokey-capture
          imagePullSecrets: [{name: acr-vpc-secret}]
          containers:
            - name: capture
              image: $CAP_IMG
              env:
                - {name: MAIL_USER, valueFrom: {secretKeyRef: {name: zerokey-acct-$N-creds, key: MAIL_USER}}}
                - {name: MAIL_LOGIN_PW_FILE, value: /run/creds/mail_pw}
                - {name: CHATGPT_PW_FILE, value: /run/creds/chatgpt_pw}
                - {name: OUT_JSON, value: /state/out/zerokey-users.json}
                - {name: ZK_USER, value: acct$N}
                - {name: SCREENSHOT_DIR, value: /state/screenshots}
                - {name: PROFILE_DIR, value: /state/profile}
                - {name: LOGIN_MODE, value: "otp"}
                - {name: OTP_AUTO_ONLY, value: "1"}
                - {name: OTP_AUTO_MAX, value: "240"}
                - {name: OTP_FILE_WAIT, value: "0"}
                - {name: LIVE_JSON, value: /state/users.json}
                - {name: SERVE_DEPLOY, value: zerokey-serve-$N}
              command:
                - bash
                - -lc
                - |
                  xvfb-run -a python /capture/zerokey-web-capture.py; rc=\$?
                  if [ \$rc -ne 0 ]; then echo "capture exit \$rc — keeping live"; : > /state/REFRESH_STALE; exit 1; fi
                  python -c 'import json,os,sys;p=os.environ["OUT_JSON"];u=os.environ["ZK_USER"];d=json.load(open(p));pf=(d.get("chatgpt",{}).get(u) or {}).get("parsedFetch") or {};h={k.lower() for k in (pf.get("headers") or {})};sys.exit(0 if (pf.get("body") and "authorization" in h and "cookie" in h) else 2)' || { echo invalid; : > /state/REFRESH_STALE; exit 1; }
                  cp "\$OUT_JSON" "\$LIVE_JSON.tmp" && mv "\$LIVE_JSON.tmp" "\$LIVE_JSON"; rm -f /state/REFRESH_STALE
                  TOK=\$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); NS=\$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
                  curl -sS --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer \$TOK" -H "Content-Type: application/strategic-merge-patch+json" \
                    -X PATCH "https://kubernetes.default.svc/apis/apps/v1/namespaces/\$NS/deployments/\$SERVE_DEPLOY" \
                    -d '{"spec":{"template":{"metadata":{"annotations":{"zerokey/restartedAt":"'"\$(date -u +%Y%m%dT%H%M%SZ)"'"}}}}}' -o /dev/null -w "rollout HTTP %{http_code}\n"
              volumeMounts:
                - {name: creds, mountPath: /run/creds, readOnly: true}
                - {name: state, mountPath: /state}
          volumes:
            - name: creds
              secret: {secretName: zerokey-acct-$N-creds, items: [{key: MAIL_PW, path: mail_pw}, {key: CHATGPT_PW, path: chatgpt_pw}]}
            - {name: state, persistentVolumeClaim: {claimName: zerokey-acct-$N-state}}
YAML
done

if [[ $APPLY -eq 1 ]]; then
  kubectl apply -f "$TMP"
else
  cat "$TMP"
fi
rm -f "$TMP"
