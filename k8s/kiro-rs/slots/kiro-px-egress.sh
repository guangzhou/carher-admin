#!/bin/bash
# Build + apply one in-cluster egress proxy per kiro slot for the acct2 mihomo ports (10-28).
# Run on 198 as cltx:  bash kiro-px-egress.sh <198 local port> <expected exit IP>
#   e.g. bash kiro-px-egress.sh 18891 203.231.254.184
# Same shape as kiro-us-egress.yaml: separate Deployment/Service/NetworkPolicy `kiro-px-<n>` per port, so
# re-pinning one never restarts another. The node is whatever 198:~/mihomo-harness/acct2 pins that port to
# (single node ⇒ fixed IP). Proxy password per port in ~/kiro-px-egress/<n>/pw (0600), node credentials only
# in the Secret — nothing secret goes to git. Slot in Admin: http://kiro-px-<n>.kiro-rs.svc:7890, user kiro.
# Slot name = <country code>-<exit IP> (kr-/ph-/sg-/de-…), set by kiro-px-verify.sh from ipinfo through the proxy.
# 🔴 Renaming a slot later clears its user bindings (bindings are keyed by slot name) ⇒ get the name right first time.
set -euo pipefail
PORT=$1 IP=$2
# Optional 3rd arg = acct2 node name, for a node that has no listener port on 198 (10-31: the 4 nodes muse2api
# also uses). The port is then only the numbering key (kiro-px-<port-18890>), nothing listens on it.
NODE=${3:-}
# n = port - 18890 (18891 -> 1 … 18895 -> 5, 18906 -> 16); the 1..5 names stay what they were on 10-28
N=$((PORT - 18890))
[ "$N" -ge 1 ] && [ "$N" -le 99 ] || { echo "STOP port $PORT: expected 18891..18989"; exit 1; }
NAME=kiro-px-$N
# never repoint an existing proxy to a different exit: a slot on it would silently change IP
OLD_IP=$(sudo -n kubectl -n kiro-rs get deploy $NAME -o jsonpath='{.metadata.labels.kiro-egress-ip}' 2>/dev/null || true)
[ -z "$OLD_IP" ] || [ "$OLD_IP" = "$IP" ] || { echo "STOP $NAME already exists for $OLD_IP, not $IP"; exit 1; }
D=~/kiro-px-egress/$N
umask 077; mkdir -p "$D"; cd "$D"
[ -s pw ] || python3 -c "import secrets;print(secrets.token_urlsafe(24))" > pw
PORT=$PORT NODE="$NODE" python3 - <<'EOF'
import os, yaml
src = yaml.safe_load(open(os.path.expanduser("~/mihomo-harness/acct2/config.yaml")))
px = {p["name"]: p for p in src["proxies"]}
node = os.environ["NODE"] or next(l["proxy"] for l in src["listeners"] if l["port"] == int(os.environ["PORT"]))
assert node in px, f"port {os.environ['PORT']} pinned to a group, not a single node: {node!r}"
pw = open("pw").read().strip()
cfg = {"mixed-port": 0, "allow-lan": True, "bind-address": "*", "mode": "rule", "log-level": "warning", "ipv6": False,
       "find-process-mode": "off", "authentication": ["kiro:" + pw], "skip-auth-prefixes": [],
       "profile": {"store-selected": False}, "proxies": [px[node]],
       "listeners": [{"name": "kiro-px", "type": "mixed", "listen": "0.0.0.0", "port": 7890, "proxy": node}],
       "rules": ["MATCH," + node]}
open("config.yaml", "w").write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
EOF
sudo -n docker run --rm --user 0 -v "$PWD":/c:ro --tmpfs /w 127.0.0.1:5000/mihomo:v1.19.33-carher1 -t -d /w -f /c/config.yaml 2>&1 | tail -1 | grep -q successful \
  || { echo "STOP $NAME: config test failed"; exit 1; }
K="sudo -n kubectl -n kiro-rs"
$K create secret generic $NAME --from-file=config.yaml=config.yaml --dry-run=client -o yaml | $K apply -f - >/dev/null
cat <<YAML | $K apply -f - >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata: {name: $NAME, namespace: kiro-rs, labels: {app: $NAME, kiro-egress-ip: "$IP"}}
spec:
  replicas: 1
  strategy: {type: RollingUpdate, rollingUpdate: {maxUnavailable: 0, maxSurge: 1}}
  selector: {matchLabels: {app: $NAME}}
  template:
    metadata: {labels: {app: $NAME}}
    spec:
      nodeSelector: {kubernetes.io/hostname: aiyjy-litellm-242}
      tolerations: [{key: dedicated, operator: Equal, value: new-node, effect: NoSchedule}]
      securityContext: {runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001, runAsNonRoot: true}
      containers:
        - name: mihomo
          image: 127.0.0.1:5000/mihomo:v1.19.33-carher1
          imagePullPolicy: IfNotPresent
          args: ["-d", "/work", "-f", "/cfg/config.yaml"]
          ports: [{name: proxy, containerPort: 7890}]
          readinessProbe: {tcpSocket: {port: 7890}, periodSeconds: 5}
          livenessProbe: {tcpSocket: {port: 7890}, periodSeconds: 20, failureThreshold: 3}
          resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {memory: 256Mi}}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          volumeMounts: [{name: cfg, mountPath: /cfg, readOnly: true}, {name: work, mountPath: /work}]
      volumes:
        - {name: cfg, secret: {secretName: $NAME, defaultMode: 0440}}
        - {name: work, emptyDir: {}}
---
apiVersion: v1
kind: Service
metadata: {name: $NAME, namespace: kiro-rs}
spec:
  type: ClusterIP
  selector: {app: $NAME}
  ports: [{name: proxy, port: 7890, targetPort: 7890}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: $NAME-ingress, namespace: kiro-rs}
spec:
  podSelector: {matchLabels: {app: $NAME}}
  policyTypes: [Ingress]
  ingress:
    - from: [{podSelector: {matchLabels: {app: kiro-rs-slots}}}, {podSelector: {matchLabels: {kiro-px-egress-probe: "1"}}}]
      ports: [{port: 7890, protocol: TCP}]
YAML
$K rollout status deploy/$NAME --timeout=120s >/dev/null && echo "OK $NAME applied"
