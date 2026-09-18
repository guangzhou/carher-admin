#!/usr/bin/env bash
# 9router Cursor account-pool failover acceptance test.
#
# Uses the REAL defect as the red: leg2 (0dfdb1fc) accepts a fable stream and then
# never emits a frame. fill-first normally hides it, so we temporarily flip the
# cursor strategy back to round-robin/sticky=1 to force leg2 into rotation, then
# put it back. Expected with the stall watchdog in place:
#
#   probe #1  -> leg2 picked -> ~45s stall -> 504 -> leg2 locked for this model
#                -> retried on leg1 -> SUCCESS (slow, ~50s)
#   probe #2+ -> leg2 is modelLock'd -> leg1 only -> SUCCESS (fast, ~4s)
#
# Gates (all must hold):
#   1. 0 hangs, 4/4 nonce echoed                      (client never sees the fault)
#   2. log has "UNAVAILABLE (504)" + "NEXT ACCOUNT"    (failover really happened)
#   3. leg2 gains modelLock_claude-fable-5-1-medium    (leg finally measured bad)
#   4. opus negative control: no NEXT ACCOUNT, no lock (healthy legs not killed)
#
# Requires: the pool-* image already live. Read-only apart from the temporary
# settings flip, which is restored unconditionally by a trap.
set -uo pipefail
NS=litellm-product
H=cltx@10.68.13.198
K="sudo kubectl -n $NS"

POD=$(ssh $H "$K get pods -l app=9router -o jsonpath='{.items[0].metadata.name}'")
[ -n "$POD" ] || { echo "FATAL: no 9router pod"; exit 1; }
echo "POD=$POD"

flip() {  # $1 = round-robin | fill-first
  ssh $H "$K exec $POD -- env WANT=$1 node -e '
    const BASE=\"http://127.0.0.1:20128\";
    (async()=>{
      const lr=await fetch(BASE+\"/api/auth/login\",{method:\"POST\",
        headers:{\"Content-Type\":\"application/json\"},
        body:JSON.stringify({password:process.env.INITIAL_PASSWORD})});
      const sc=lr.headers.getSetCookie?lr.headers.getSetCookie().join(\"; \"):(lr.headers.get(\"set-cookie\")||\"\");
      const cookie=sc.split(/,\s*/).map(s=>s.split(\";\")[0]).join(\"; \");
      const cur=await (await fetch(BASE+\"/api/settings\",{headers:{cookie}})).json();
      // updateSettings() is a SHALLOW merge: resend the whole providerStrategies subtree.
      const ps=JSON.parse(JSON.stringify(cur.providerStrategies||{}));
      ps.cursor={...(ps.cursor||{}),fallbackStrategy:process.env.WANT};
      if(process.env.WANT===\"round-robin\") ps.cursor.stickyRoundRobinLimit=1;
      else delete ps.cursor.stickyRoundRobinLimit;
      const r=await fetch(BASE+\"/api/settings\",{method:\"PATCH\",
        headers:{\"Content-Type\":\"application/json\",cookie},
        body:JSON.stringify({providerStrategies:ps})});
      const back=await (await fetch(BASE+\"/api/settings\",{headers:{cookie}})).json();
      console.log(\"  strategy now=\"+JSON.stringify(back.providerStrategies)+\" http=\"+r.status);
    })().catch(e=>{console.error(\"FLIP_ERR \"+e.message);process.exit(1);});'"
}

restore() { echo "--- restoring fill-first ---"; flip fill-first; }
trap restore EXIT

T0=$(ssh $H "date -u +%Y-%m-%dT%H:%M:%SZ")
echo "--- forcing leg2 into rotation ---"
flip round-robin || exit 1

echo "--- fable x4 (TO=120s: the first one is expected to take ~50s) ---"
ssh $H "$K exec $POD -- env N=4 TO=120000 MODEL=cu/claude-fable-5-1-medium node /tmp/tmp_9r_probe8.js"
FABLE_RC=$?

echo "--- opus x2 (negative control) ---"
ssh $H "$K exec $POD -- env N=2 TO=120000 MODEL=cu/claude-opus-5-medium node /tmp/tmp_9r_probe8.js"

echo "--- log evidence since $T0 ---"
ssh $H "$K logs $POD --since-time=$T0 2>&1 | grep -E 'UNAVAILABLE|NEXT ACCOUNT|stall|POST|DONE' | tail -30"

echo "--- leg state (gate 3: leg2 must now carry modelLock_claude-fable-5-1-medium) ---"
ssh $H "$K exec $POD -- node /tmp/tmp_9r_legs.js"

echo "FABLE_PROBE_RC=$FABLE_RC"
