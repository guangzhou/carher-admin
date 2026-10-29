#!/usr/bin/env bash
# 198 上跑:列某个 kiro 槽上的 key(别名、24h 请求、是否固定、是否空闲)。用法: bash slot_users.sh <槽名>
set -euo pipefail
P=$(sudo -n kubectl -n kiro-rs get pod -l app=kiro-rs-slots -o name | head -1)
K=$(sudo -n kubectl -n kiro-rs exec "$P" -c kiro-rs -- cat /app/config/config.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["adminApiKey"])')
curl -s -H "x-api-key: $K" "http://127.0.0.1:30891/api/admin/slots/users?slot=$1" | python3 -c '
import json,sys
for it in sorted(json.load(sys.stdin)["items"], key=lambda x:-x["requests24h"]):
    print("%-30s req24h=%-5s pinned=%-5s idle=%s" % (it.get("keyAlias") or it["user"][:20], it["requests24h"], it["pinned"], it["idle"]))'
