#!/bin/bash
# Watch whether the in-cluster mihomo egress proxies (every kiro 30891 slot on http://<svc>.kiro-rs.svc:7890, iGuge nodes,
# enabled AND disabled) keep a FIXED exit IP.
# Run on 198 as cltx (detached, survives ssh logout):
#   nohup setsid bash kiro-egress-ip-watch.sh --rounds 20 --interval 1800 > ~/kiro-egress-watch/run.log 2>&1 &
#   bash kiro-egress-ip-watch.sh --rounds 1 --interval 0          # one round now (smoke test)
#   bash kiro-egress-ip-watch.sh --summary [~/kiro-egress-watch/ip-watch-*.tsv]   # table per slot
# Which proxies: every Admin slot whose proxyUrl is http://<svc>.kiro-rs.svc:7890 (read live, nothing hardcoded);
# expected IP = the slot name suffix (us-170.178.173.10 -> 170.178.173.10). Aliyun squid slots are skipped on purpose.
# Each round: a throwaway probe pod on 242 (carries the probe labels the NetworkPolicies allow), one ipinfo + one
# Kiro endpoint request through each proxy, one TSV line per slot; IP != expected => ALERT line in run.log.
# Proxy passwords come from ~/kiro-*-egress/pw and ~/kiro-px-egress/<n>/pw and only go into the pod's env.
set -uo pipefail
ROUNDS=20 INTERVAL=1800 SUMMARY=
while [ $# -gt 0 ]; do
  case "$1" in
    --rounds) ROUNDS=$2; shift 2;;
    --interval) INTERVAL=$2; shift 2;;
    --summary) SUMMARY=${2:-$(ls -t ~/kiro-egress-watch/ip-watch-*.tsv 2>/dev/null | head -1)}; shift $(( $# >= 2 ? 2 : 1 ));;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done
W=~/kiro-egress-watch; umask 077; mkdir -p "$W"

if [ -n "$SUMMARY" ]; then
  python3 - "$SUMMARY" <<'EOF'
import sys, collections
rows = [l.rstrip("\n").split("\t") for l in open(sys.argv[1]) if l.strip() and not l.startswith("ts\t")]
by = collections.OrderedDict()
# 9 列（10-31 前）或 10 列（多一列 enabled=on/off）都认
for ts, rnd, slot, svc, want, got, ok, kc, ks, *rest in rows:
    by.setdefault(slot, []).append((ts, got, ok, kc, ks, rest[0] if rest else "on"))
print(f"{sys.argv[1]}  rounds={len({r[1] for r in rows})}")
print(f"{'slot':22} {'en':3} {'checks':6} {'same_ip':7} {'fail':4} {'changed_to':30} {'kiro_ok':7} first..last")
for slot, v in by.items():
    same = sum(1 for x in v if x[2] == "OK"); fail = sum(1 for x in v if x[1] == "FAIL")
    other = sorted({x[1] for x in v if x[2] == "CHANGED"})
    kok = sum(1 for x in v if x[3] == "404")
    print(f"{slot:22} {v[-1][5]:3} {len(v):<6} {same:<7} {fail:<4} {','.join(other) or '-':30} {kok}/{len(v):<5} {v[0][0]}..{v[-1][0]}")
EOF
  exit 0
fi

K="sudo -n kubectl -n kiro-rs"
# admin key：从 pod 现读（轮换后不会静默过期）；读不到再用 198 上的副本 ~/kiro-us-egress/.ak
AK=$($K exec deploy/kiro-rs-slots -c kiro-rs -- sh -c 'cat /app/config/config.json' 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["adminApiKey"])' 2>/dev/null || cat ~/kiro-us-egress/.ak)
B=http://127.0.0.1:30891/api/admin
IMG=127.0.0.1:5000/litellm-carher@sha256:92410b94ce1f43245d59152d77b26eca02490523417dc9d30242d2c9e7359fbe
POD=egress-ip-watch-probe
OUT=$W/ip-watch-$(date +%Y%m%dT%H%M%S).tsv
printf 'ts\tround\tslot\tsvc\texpected\tgot\tstatus\tkiro_code\tkiro_s\tenabled\n' > "$OUT"
echo "START $(date '+%F %T') rounds=$ROUNDS interval=${INTERVAL}s out=$OUT"

pwfile() {  # svc -> password file
  case "$1" in
    kiro-px-*) echo ~/kiro-px-egress/${1#kiro-px-}/pw;;
    *) echo ~/$1/pw;;
  esac
}

probe_pod() {
  $K delete pod $POD --ignore-not-found --wait=true >/dev/null 2>&1
  cat <<EOF | $K apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: kiro-rs
  labels: {kiro-px-egress-probe: "1", kiro-us-egress-probe: "1", kiro-uk-egress-probe: "1"}
spec:
  nodeSelector: {kubernetes.io/hostname: aiyjy-litellm-242}
  tolerations: [{key: dedicated, operator: Equal, value: new-node, effect: NoSchedule}]
  restartPolicy: Never
  containers: [{name: c, image: "$IMG", imagePullPolicy: IfNotPresent, command: ["sleep","900"]}]
EOF
  $K wait --for=condition=Ready pod/$POD --timeout=120s >/dev/null 2>&1
}
trap '$K delete pod $POD --wait=false >/dev/null 2>&1' EXIT

for r in $(seq 1 "$ROUNDS"); do
  # live slot list: name<TAB>svc for every in-cluster proxy slot
  TARGETS=$(curl -s -m 15 -H "x-api-key: $AK" $B/slots/config | python3 -c '
import json, sys, re
for s in json.load(sys.stdin):
    m = re.match(r"http://([a-z0-9-]+)\.kiro-rs\.svc:7890$", s.get("proxyUrl") or "")
    # 🔴 停用槽也要盯：它们的出口 IP 一样会漂，等哪天启用时槽名已经不对了
    #    （10-31 实测：tw-114.41.8.146 实际已是 1.168.186.21，因为它停用而被原来的 enabled 过滤漏掉）
    if m: print(s["name"] + "\t" + m.group(1) + "\t" + ("on" if s.get("enabled", True) else "off"))')
  if [ -z "$TARGETS" ]; then echo "ROUND $r STOP: no in-cluster slots from Admin"; else
    SPEC=$(while IFS=$'\t' read -r slot svc en; do
      f=$(pwfile "$svc"); [ -s "$f" ] || { echo "ROUND $r STOP $slot: no password file $f" >&2; continue; }
      printf '%s\t%s\t%s\t%s\t%s\n' "$slot" "$svc" "${slot#*-}" "$(cat "$f")" "${en:-on}"
    done <<<"$TARGETS")
    if probe_pod; then
      $K exec -i $POD -- env SPEC="$SPEC" ROUND="$r" python3 - <<'EOF' | tee -a "$OUT" | awk -F'\t' '{print "  "$3, $6, $7, "kiro="$8"/"$9"s", ($10=="off"?"(停用槽)":"")} $7=="CHANGED"{print "ALERT " $3 " exit IP changed: " $5 " -> " $6 ($10=="off"?"  (停用槽，启用前必须改槽名)":"  🔴 在役槽！")}'
import os, time, urllib.request, urllib.error
def get(url, proxy):
    o = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy})); t = time.time()
    try: r = o.open(url, timeout=20); return str(r.status), r.read()[:200].decode().strip(), round(time.time() - t, 2)
    except urllib.error.HTTPError as e: return str(e.code), "", round(time.time() - t, 2)
    except Exception: return "000", "", round(time.time() - t, 2)
ts = time.strftime("%Y-%m-%d %H:%M:%S")
for line in os.environ["SPEC"].splitlines():
    slot, svc, want, pw, en = line.split("\t")
    px = f"http://kiro:{pw}@{svc}:7890"
    code, got, _ = get("https://ipinfo.io/ip", px)
    got = got if code == "200" and got else "FAIL"
    status = "OK" if got == want else ("FAIL" if got == "FAIL" else "CHANGED")
    kc, _, ks = get("https://q.us-east-1.amazonaws.com/", px)
    print("\t".join([ts, os.environ["ROUND"], slot, svc, want, got, status, kc, str(ks), en]), flush=True)
EOF
      $K delete pod $POD --wait=false >/dev/null 2>&1
      echo "ROUND $r done $(date '+%F %T')"
    else
      echo "ROUND $r STOP: probe pod not ready"
    fi
  fi
  [ "$r" -lt "$ROUNDS" ] && sleep "$INTERVAL"
done
echo "END $(date '+%F %T') -> bash $0 --summary $OUT"
