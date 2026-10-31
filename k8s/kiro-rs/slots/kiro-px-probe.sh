#!/bin/bash
# Read-only probe of kiro-px-<n> proxies from inside the cluster (node 242). No Admin writes.
# Run on 198 as cltx:  bash kiro-px-probe.sh <n>:<expected exit IP> [...]   e.g. 19:64.118.149.168
# Per proxy: exit IP (3x, must equal expected), country code, Kiro q.us-east-1 + auth (404 = reachable), no-password = 407.
# Prints one TSV line per proxy: n ip cc OK|STOP kiro_s detail
set -uo pipefail
K="sudo -n kubectl -n kiro-rs"
IMG=127.0.0.1:5000/litellm-carher@sha256:92410b94ce1f43245d59152d77b26eca02490523417dc9d30242d2c9e7359fbe
$K delete pod px-egress-probe --ignore-not-found --wait=true >/dev/null
cat <<EOF | $K apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: px-egress-probe, namespace: kiro-rs, labels: {kiro-px-egress-probe: "1"}}
spec:
  nodeSelector: {kubernetes.io/hostname: aiyjy-litellm-242}
  tolerations: [{key: dedicated, operator: Equal, value: new-node, effect: NoSchedule}]
  restartPolicy: Never
  containers: [{name: c, image: "$IMG", imagePullPolicy: IfNotPresent, command: ["sleep","1800"]}]
EOF
$K wait --for=condition=Ready pod/px-egress-probe --timeout=120s >/dev/null
trap '$K delete pod px-egress-probe --wait=false >/dev/null 2>&1' EXIT
for spec in "$@"; do
  N=${spec%%:*}; IP=${spec#*:}
  PW=$(cat ~/kiro-px-egress/$N/pw)
  $K exec -i px-egress-probe -- env PW="$PW" N="$N" WANT="$IP" python3 - <<'EOF'
import os, urllib.request, json, time
n, pw, want = os.environ["N"], os.environ["PW"], os.environ["WANT"]
h = f"kiro-px-{n}"
def get(url, proxy):
    o = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy})); t = time.time()
    try: r = o.open(url, timeout=20); return r.status, r.read()[:2000].decode(), round(time.time() - t, 2)
    except urllib.error.HTTPError as e: return e.code, "", round(time.time() - t, 2)
    except Exception as e: return "ERR", str(e)[:80], round(time.time() - t, 2)
good = f"http://kiro:{pw}@{h}:7890"
ips = [get("https://ipinfo.io/ip", good)[1].strip() for _ in range(3)]
info = get("https://ipinfo.io/json", good)[1]
cc = (json.loads(info or "{}").get("country") or "??").lower()
q = get("https://q.us-east-1.amazonaws.com/", good)
a = get("https://prod.us-east-1.auth.desktop.kiro.dev/", good)
nopw = get("https://q.us-east-1.amazonaws.com/", f"http://{h}:7890")
bad = []
if any(i != want for i in ips): bad.append(f"exit {ips}")
if q[0] != 404 or a[0] != 404: bad.append(f"kiro {q[0]}/{a[0]}")
if "407" not in str(nopw[1]): bad.append(f"no-pw {nopw[:2]}")
print("\t".join([n, want, cc, "STOP" if bad else "OK", str(q[2]), "; ".join(bad)]))
EOF
done
