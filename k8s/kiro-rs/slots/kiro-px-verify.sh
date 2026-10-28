#!/bin/bash
# Verify kiro-px-<n> proxies from inside the cluster (on 242), then add each as a slot and run slot tests.
# Run on 198 as cltx:  bash kiro-px-verify.sh "18891:203.231.254.184:3" ["18892:49.157.61.223:3" ...]
# Each arg = <198 port>:<expected exit IP>:<weight>. Per port prints OK/STOP + reason; stops at the first STOP.
set -uo pipefail
K="sudo -n kubectl -n kiro-rs"
AK=$(cat ~/kiro-us-egress/.ak)
B=http://127.0.0.1:30891/api/admin
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
  IFS=: read -r PORT IP W <<<"$spec"; N=${PORT: -1}; NAME=kiro-px-$N; SLOT=
  PW=$(cat ~/kiro-px-egress/$N/pw)
  R=$($K exec -i px-egress-probe -- env PW="$PW" HOST="$NAME" WANT="$IP" python3 - <<'EOF'
import os, urllib.request, json, time
h, pw, want = os.environ["HOST"], os.environ["PW"], os.environ["WANT"]
def get(url, proxy):
    o = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy})); t = time.time()
    try: r = o.open(url, timeout=20); return r.status, r.read()[:2000].decode(), round(time.time() - t, 2)
    except urllib.error.HTTPError as e: return e.code, "", round(time.time() - t, 2)
    except Exception as e: return "ERR", str(e)[:80], round(time.time() - t, 2)
good = f"http://kiro:{pw}@{h}:7890"
ips = []
for _ in range(3):
    s, b, t = get("https://ipinfo.io/ip", good); ips.append(b.strip())
cc = (json.loads(get("https://ipinfo.io/json", good)[1] or "{}").get("country") or "??").lower()
q = get("https://q.us-east-1.amazonaws.com/", good)
a = get("https://prod.us-east-1.auth.desktop.kiro.dev/", good)
n = get("https://q.us-east-1.amazonaws.com/", f"http://{h}:7890")
bad = []
if any(i != want for i in ips): bad.append(f"exit ip {ips} != {want}")
if q[0] != 404 or a[0] != 404: bad.append(f"kiro {q[0]}/{a[0]}")
if "407" not in str(n[1]): bad.append(f"no-pw not rejected: {n[:2]}")
print(("STOP " + "; ".join(bad)) if bad else f"OK ip={want} kiro={q[2]}s/{a[2]}s no-pw=407 cc={cc}")
EOF
)
  # slot name = <country code>-<exit IP>, same as jp-/sg-/us-/uk- slots; country measured through the proxy itself
  CC=${R##* cc=}; SLOT=$CC-$IP
  echo "$SLOT probe: $R"; case "$R" in OK*) ;; *) exit 1;; esac
  [ ${#CC} = 2 ] || { echo "$IP STOP: no country code from ipinfo ($CC)"; exit 1; }
  curl -s -m 10 -H "x-api-key: $AK" $B/slots/config > /tmp/px-cur.json
  cp /tmp/px-cur.json ~/kiro-px-egress/slots-config-before-$SLOT-$(date +%Y%m%dT%H%M%S).json
  PW="$PW" SLOT="$SLOT" URL="http://$NAME.kiro-rs.svc:7890" W="$W" python3 - <<'EOF' > /tmp/px-new.json
import json, os
rows = json.load(open("/tmp/px-cur.json"))
assert all(r.get("proxyPassword") in (None, "", "******") for r in rows)
if not any(r["name"] == os.environ["SLOT"] for r in rows):
    rows.append({"name": os.environ["SLOT"], "proxyUrl": os.environ["URL"], "proxyUsername": "kiro",
                 "proxyPassword": os.environ["PW"], "enabled": True, "weight": int(os.environ["W"]),
                 "acceptNew": True, "vipAliases": []})
print(json.dumps(rows))
EOF
  c=$(curl -s -o /dev/null -w "%{http_code}" -m 20 -X PUT -H "x-api-key: $AK" -H "content-type: application/json" --data @/tmp/px-new.json $B/slots/config)
  rm -f /tmp/px-new.json /tmp/px-cur.json
  [ "$c" = 200 ] || { echo "$SLOT STOP: PUT slots/config -> $c"; exit 1; }
  I=$(curl -s -m 10 -H "x-api-key: $AK" $B/slots/config | SLOT=$SLOT python3 -c "import json,sys,os; print([i for i,r in enumerate(json.load(sys.stdin)) if r['name']==os.environ['SLOT']][0])")
  for t in "claude-opus-5-5:hello" "claude-sonnet-4-6:hello" "claude-opus-5-5:tool"; do
    m=${t%%:*}; sc=${t##*:}
    out=$(curl -s -m 200 -X POST -H "x-api-key: $AK" -H "content-type: application/json" -d "{\"model\":\"$m\",\"scenario\":\"$sc\"}" $B/slots/$I/test \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['verdict'], d['status'], str(d['ms'])+'ms', 'acct', d['accountId'], 'tools', len(d.get('toolCalls') or []), (d.get('detail') or '')[:120])")
    echo "$SLOT slot#$I $m $sc: $out"
    case "$out" in ok*) ;; *) echo "$SLOT STOP: slot test failed — disable it in Admin"; exit 1;; esac
  done
  echo "$SLOT OK"
done
