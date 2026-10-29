#!/usr/bin/env bash
# 198 上跑:各槽当前状态 + 指定 key 别名落在哪个槽。用法: bash slots_map.sh alias1 alias2 ...
set -euo pipefail
P=$(sudo -n kubectl -n kiro-rs get pod -l app=kiro-rs-slots -o name | head -1)
K=$(sudo -n kubectl -n kiro-rs exec "$P" -- cat /app/config/config.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["adminApiKey"])')
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
curl -s -H "x-api-key: $K" http://127.0.0.1:30891/api/admin/slots > "$T/slots.json"
for n in $(python3 -c 'import json,sys;[print(x["name"]) for x in json.load(open(sys.argv[1]))["slots"]]' "$T/slots.json"); do
  curl -s -H "x-api-key: $K" "http://127.0.0.1:30891/api/admin/slots/users?slot=$n"; echo
done > "$T/users.jsonl"
python3 - "$T/slots.json" "$T/users.jsonl" "$@" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); want = sys.argv[3:]
for s in d["slots"]:
    print("%-20s tier=%s limit=%s inflight=%s queued=%s users=%s weight=%s latencyMs=%s acceptNew=%s egress=%s"
          % (s["name"], s["accountTier"], s["limit"], s["inFlight"], s["queued"], s["users"], s["weight"],
             s["latencyMs"], s["acceptNew"], s["egress"]))
for l in open(sys.argv[2]):
    l = l.strip()
    if not l: continue
    for it in json.loads(l)["items"]:
        a = it.get("keyAlias") or ""
        if any(w in a for w in want):
            print("  %-20s %-28s req24h=%s pinned=%s" % (it["slot"], a, it["requests24h"], it["pinned"]))
PY
