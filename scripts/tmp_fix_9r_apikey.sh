#!/usr/bin/env bash
# Repair the api_key on the two freshly-registered 9router deployments.
#
# WHY: /model/info never returns api_key, so when I cloned the proven
# cursor-fc-composer-2.5 shape I had to guess the credential and guessed
# "router9". 9router's own apiKeys table has exactly one row, named
# "litellm-bridge" -- that is what the working deployments actually send.
#
# The key value is read inside the 9router pod and piped straight into the
# litellm-proxy pod. It is never printed and never crosses the jms hop.
set -uo pipefail
NS=litellm-product
RP=9router-6755f9f964-6jl7v
IDS='9router/claude-fable-5-1-medium 9router/claude-opus-5-medium'

LP=$(sudo kubectl -n $NS get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}' | awk '{print $1}')
echo "[pod] litellm=$LP 9router=$RP"

cat > /tmp/tmp_9r_getkey_d3.js <<'JS'
const Database = require('/app/node_modules/better-sqlite3');
const db = new Database('/app/data/db/data.sqlite', {readonly:true});
const r = db.prepare("select key from apiKeys where isActive=1 and name='litellm-bridge'").get();
if (!r) { process.stderr.write('no active litellm-bridge key\n'); process.exit(1); }
process.stdout.write(r.key);
JS
sudo kubectl -n $NS cp /tmp/tmp_9r_getkey_d3.js "$NS/$RP:/tmp/tmp_9r_getkey_d3.js" 2>/dev/null
BRIDGE_KEY=$(sudo kubectl -n $NS exec -i "$RP" -- node /tmp/tmp_9r_getkey_d3.js 2>/dev/null)
if [ -z "$BRIDGE_KEY" ]; then echo "FATAL: could not read bridge key"; exit 1; fi
echo "[key] read litellm-bridge key: len=${#BRIDGE_KEY} prefix=${BRIDGE_KEY:0:5}***"

cat > /tmp/tmp_9r_upd_d3.py <<'PY'
import json, os, urllib.request, urllib.error
BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
BK = os.environ["BRIDGE_KEY"]
IDS = os.environ["IDS"].split()

def call(path, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
        headers={"Authorization": "Bearer " + MK, "Content-Type": "application/json"},
        method=method)
    try:
        x = urllib.request.urlopen(r, timeout=115)
        return x.status, x.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")

st, raw = call("/model/info")
rows = {str((x.get("model_info") or {}).get("id")): x for x in json.loads(raw)["data"]}
for mid in IDS:
    cur = rows.get(mid)
    if not cur:
        print(f"[skip] {mid}: not registered"); continue
    lp = dict(cur.get("litellm_params") or {})
    lp["api_key"] = BK
    st, raw = call("/model/update", {"model_name": cur["model_name"],
                                     "litellm_params": lp,
                                     "model_info": {"id": mid}}, method="POST")
    print(f"[update] {mid} ({cur['model_name']}): HTTP {st} {raw[:200]}")
PY
sudo kubectl -n $NS cp /tmp/tmp_9r_upd_d3.py "$NS/$LP:/tmp/tmp_9r_upd_d3.py" 2>/dev/null
sudo kubectl -n $NS exec -i "$LP" -c litellm -- env BRIDGE_KEY="$BRIDGE_KEY" IDS="$IDS" python3 /tmp/tmp_9r_upd_d3.py
