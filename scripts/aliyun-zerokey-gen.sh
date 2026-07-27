#!/usr/bin/env bash
# aliyun-zerokey-gen.sh — 生成阿里云原生 zerokey pool 的 per-account manifest
# （serve Deployment + capture CronJob），并可选注册进 litellm zerokey-pool。
#
# 前提（每个 acct 一次性）：
#   1. secret zerokey-acct-<N>-creds（MAIL_USER/MAIL_PW/CHATGPT_PW[/TOTP_SECRET]）
#      ⚠️ 凭证文件格式 email----chatgpt_pw----mailbox_pw[----TOTP]：**MAIL_PW=第2段
#        （邮箱登录密码）**，CHATGPT_PW=第1段，TOTP_SECRET=末尾 base32（2FA 号才有）。
#   2. PVC zerokey-acct-<N>-state
#   3. 首次 capture 成功（users.json 落 PVC）——用 repo 脚本
#      scripts/chatgpt-onboard/zerokey-codex/capture/zerokey-web-capture.py 起一次性 Job
#      （挂本脚本 --apply 生成的 ConfigMap zerokey-capture-src），成功=日志 [CAPTURED]
#      f/conversation + ✅ wrote → cp /state/out/zerokey-users.json /state/users.json。
#
# 隔离：hostNetwork 钉 EIP 节点，走节点 EIP（与线上 codex 共享 NAT 隔离）。
#   EIP 节点：.86=47.236.200.98, .122=47.84.85.100（dify 节点）
#   端口：8100+acct（hostPort，节点内唯一）
#
# ⚠️ capture 镜像内 /capture/zerokey-web-capture.py 是 stale 7-07 版（fresh-login
#   composer bug + 无 TOTP/MFA 处理）。本脚本改为把 repo 最新脚本塞进 ConfigMap
#   zerokey-capture-src，capture 容器 override command 跑 /script/cap.py。
#
# ⚠️ litellm 注册（并入轮询）不在本脚本——见 aliyun-zerokey-join-pool.py。注册时
#   api_base 必须带 /v1 且 model_info.mode=chat（否则 gpt-5-5 默认走 /responses → 404）。
#
# 用法：
#   ./scripts/aliyun-zerokey-gen.sh 69 70 71 ...        # 打印 manifest 到 stdout
#   ./scripts/aliyun-zerokey-gen.sh --apply 70 71       # apply（并同步 CM + RBAC）
set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && { APPLY=1; shift; }
[[ $# -lt 1 ]] && { echo "usage: $0 [--apply] N1 N2 ..."; exit 1; }
ACCTS=("$@")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAP_SRC="$SCRIPT_DIR/chatgpt-onboard/zerokey-codex/capture/zerokey-web-capture.py"
REG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher
SERVE_IMG="$REG:zerokey-serve-aliyun-20260707"
CAP_IMG="$REG:zerokey-capture-aliyun-20260707-otp3"
# EIP-bearing nodes. Pin per-account by N%2 so capture+serve land on the SAME
# node (cf_clearance is egress-IP-bound → must share the node EIP) and the 8
# accounts split across the two EIPs .86/.122. Explicit case avoids shell
# array-indexing differences (bash 0-idx vs zsh 1-idx).
node_for() { case $(( $1 % 2 )) in 0) echo ap-southeast-1.172.16.0.86;; 1) echo ap-southeast-1.172.16.16.122;; esac; }

TMP=$(mktemp)
i=0
for N in "$@"; do
  NODE=$(node_for "$N")
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
                - {name: TOTP_SECRET, valueFrom: {secretKeyRef: {name: zerokey-acct-$N-creds, key: TOTP_SECRET, optional: true}}}
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
                  xvfb-run -a python /script/cap.py; rc=\$?
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
                - {name: script, mountPath: /script, readOnly: true}
          volumes:
            - name: creds
              secret: {secretName: zerokey-acct-$N-creds, items: [{key: MAIL_PW, path: mail_pw}, {key: CHATGPT_PW, path: chatgpt_pw}]}
            - {name: state, persistentVolumeClaim: {claimName: zerokey-acct-$N-state}}
            - {name: script, configMap: {name: zerokey-capture-src}}
YAML
done

if [[ $APPLY -eq 1 ]]; then
  # 1. 确保 capture 脚本 ConfigMap 是 repo 最新版（含 TOTP/MFA 补丁 + capture 修复）
  if [[ -f "$CAP_SRC" ]]; then
    kubectl -n carher create configmap zerokey-capture-src \
      --from-file=cap.py="$CAP_SRC" --dry-run=client -o yaml | kubectl apply -f -
  else
    echo "WARN: $CAP_SRC 不存在，跳过 CM 同步（capture 会 fail）" >&2
  fi
  kubectl apply -f "$TMP"
  # 2. RBAC：capture 刷新后 rollout serve 需 SA zerokey-capture 能 patch 对应 deploy。
  #    Role zerokey-capture-rollout 用 resourceNames 白名单 → merge 本次账号（否则 403）。
  if kubectl -n carher get role zerokey-capture-rollout >/dev/null 2>&1; then
    # MERGE into the existing allowlist. The read must be verified before the
    # write: `2>/dev/null` turns a jsonpath miss (field absent, or the deploy rule
    # not at index 0) into an EMPTY string, and with op=replace that silently
    # overwrote the whole allowlist with just this run's accounts — revoking
    # rollout for every previously-onboarded pod, discovered only later as 403s
    # during the next capture refresh. So: locate the rule that actually carries
    # deployments, and abort rather than shrink if the read looks wrong.
    rule_idx=$(kubectl -n carher get role zerokey-capture-rollout -o json \
      | python3 -c '
import json,sys
rules=json.load(sys.stdin).get("rules") or []
for i,r in enumerate(rules):
    if "deployments" in (r.get("resources") or []):
        print(i); break
else:
    print(-1)')
    if [[ "$rule_idx" -lt 0 ]]; then
      echo "WARN: role zerokey-capture-rollout 无 deployments 规则，跳过 RBAC merge（刷新会 403）" >&2
    else
      existing=$(kubectl -n carher get role zerokey-capture-rollout \
        -o jsonpath="{.rules[$rule_idx].resourceNames[*]}")
      # Union of existing + this run. `add` on the array path replaces the whole
      # array like `replace` does, but unlike `replace` it also succeeds when
      # resourceNames is absent (replace 422s there, and the trailing && only
      # gated the echo so the failure was silent).
      json=$( { for x in $existing; do echo "$x"; done
                for N in "${ACCTS[@]}"; do echo "zerokey-serve-$N"; done
              } | sort -u | grep . \
              | python3 -c 'import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')
      n_before=$(echo "$existing" | wc -w | tr -d ' ')
      n_after=$(printf '%s' "$json" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
      if [[ "$n_after" -lt "$n_before" ]]; then
        echo "ERROR: RBAC merge 会缩小白名单 ($n_before → $n_after)，已中止" >&2
      elif kubectl -n carher patch role zerokey-capture-rollout --type=json \
        -p "[{\"op\":\"add\",\"path\":\"/rules/$rule_idx/resourceNames\",\"value\":$json}]"; then
        echo "RBAC: zerokey-capture-rollout resourceNames ($n_before → $n_after) → $json"
      else
        echo "ERROR: RBAC patch 失败（rule_idx=$rule_idx），刷新 rollout 会 403" >&2
      fi
    fi
  else
    echo "WARN: role zerokey-capture-rollout 不存在，刷新 rollout 会 403（需先建 Role/RoleBinding）" >&2
  fi
else
  cat "$TMP"
fi
rm -f "$TMP"
