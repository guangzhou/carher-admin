#!/usr/bin/env bash
# ccmax-deploy-224.sh — deterministic deploy tail for adding a CC Max acct to the
# 224 per-acct-egress pool. Run AFTER you already have the sk-ant-oat01 token
# (see the token-acquisition flow in skill `ccmax-acct-add`: magic-link login +
# vision-solved Arkose via cc-oauth-vision.py / cc-mailcom-code.py).
#
# What it automates (Steps 3-6 of the runbook):
#   3. 224: /Data/claude-max-proxy-<acct>/ proxy.py + .env + systemd, start
#   4. 198: SSH tunnel systemd  198:<TUNNEL_PORT> -> 224:<PROXY_PORT>, start
#   5. 198 LiteLLM: register 4 entries ccmax-<acct>-compat-{opus,sonnet,haiku,fable5}
#      (model_name claude-max-{opus,sonnet,haiku} + fable5, api_base 198:<TUNNEL_PORT>)
#   6. 224 quota-rebalance: append POOL_ACCOUNTS[<acct>] {api_base, egress_proxy}
#   + verify: local proxy Haiku 200 / tunnel health 200 / routing model-id
#
# EGRESS BINDING RULE: the acct's serving egress = its own Cogent LA port. The
# token MUST have been minted from that same IP (cc-oauth-vision EXPECT_EXIT_IP).
#
# Egress pool on 38.175.220.46 (Cogent, Los Angeles):
#   8080->.46  8081->.113  8082->.105(acct-19)  8083->.31  8084->.147
# Port convention for new accts (acct-19 is legacy 3456/3467/8082):
#   PROXY_PORT  = 3460 + (N-20)   TUNNEL_PORT = 3470 + (N-20)
#
# Usage:
#   CLTX_PW='<224 cltx pw>' MASTER_KEY='sk-pro-litellm-...' \
#   scripts/anthropic-onboard/ccmax-deploy-224.sh \
#       --acct acct-20 --oat sk-ant-oat01-xxx \
#       --proxy-port 3460 --tunnel-port 3470 --egress-port 8083
#
set -euo pipefail

ACCT="" OAT="" PROXY_PORT="" TUNNEL_PORT="" EGRESS_PORT=""
PROXY_HOST="38.175.220.46"
PROXY_API_KEY="${PROXY_API_KEY:-d89f74ccaaa55b604a010c31be8e4c05d515e102b537c819}"
MASTER_KEY="${MASTER_KEY:-sk-pro-litellm-ce077e2b0721bb419a633e4d}"
CLTX_PW="${CLTX_PW:?set CLTX_PW to 224 cltx password}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JMS="$ROOT/scripts/jms"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --acct) ACCT="$2"; shift 2;;
    --oat) OAT="$2"; shift 2;;
    --proxy-port) PROXY_PORT="$2"; shift 2;;
    --tunnel-port) TUNNEL_PORT="$2"; shift 2;;
    --egress-port) EGRESS_PORT="$2"; shift 2;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done
: "${ACCT:?--acct}" "${OAT:?--oat}" "${PROXY_PORT:?--proxy-port}" "${TUNNEL_PORT:?--tunnel-port}" "${EGRESS_PORT:?--egress-port}"
[[ "$OAT" == sk-ant-oat01-* ]] || { echo "❌ OAT must be sk-ant-oat01- (sid02 will 401)"; exit 1; }

s224() { "$JMS" ssh JSZX-AI-03 "sshpass -p '$CLTX_PW' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 cltx@10.68.13.224 \"$1\""; }
# sudo over the jms->sshpass->224 chain: base64 BOTH the password and the command
# so no '$' in either survives-and-expands through the intermediate (JSZX) shell's
# double quotes. (Bug fixed 2026-07-15: raw pw 'Rk#7mQb$L9nX' had $L9nX eaten.)
s224_sudo() {
  local cmd_b64 pw_b64
  cmd_b64=$(printf '%s' "$1" | base64 | tr -d '\n')
  pw_b64=$(printf '%s' "$CLTX_PW" | base64 | tr -d '\n')
  "$JMS" ssh JSZX-AI-03 "sshpass -p '$CLTX_PW' ssh -tt -o StrictHostKeyChecking=no cltx@10.68.13.224 'echo $cmd_b64 | base64 -d > /tmp/.ccx.\$\$.sh; echo $pw_b64 | base64 -d | sudo -S -p \"\" bash /tmp/.ccx.\$\$.sh; rm -f /tmp/.ccx.\$\$.sh'" 2>&1 | grep -vE 'tcgetattr|Connection to'
}

echo "== [0/6] egress-port -> exit IP =="
EXIT_IP=$(s224 "curl -s --max-time 12 -x http://$PROXY_HOST:$EGRESS_PORT https://ipinfo.io/ip" | tr -d '\r\n ')
echo "  $PROXY_HOST:$EGRESS_PORT -> $EXIT_IP"
[[ -n "$EXIT_IP" ]] || { echo "❌ egress port dead"; exit 1; }

echo "== [3/6] 224 proxy $ACCT on :$PROXY_PORT via egress $EGRESS_PORT =="
SETUP=$(cat <<EOF
set -e
mkdir -p /Data/claude-max-proxy-$ACCT
cp -f /Data/claude-max-proxy-acct-19/proxy.py /Data/claude-max-proxy-$ACCT/proxy.py
cat > /Data/claude-max-proxy-$ACCT/.env <<E2
ACCT_TOKENS=$ACCT::$OAT
PORT=$PROXY_PORT
API_KEYS=$PROXY_API_KEY
UPSTREAM_SOCKS5_PROXY=http://$PROXY_HOST:$EGRESS_PORT
RATE_LIMIT_RPM=30
E2
chmod 600 /Data/claude-max-proxy-$ACCT/.env
chown -R cltx:cltx /Data/claude-max-proxy-$ACCT
mkdir -p /Data/anthropic-auth/$ACCT
echo ANTHROPIC_OAUTH_TOKEN=$OAT > /Data/anthropic-auth/$ACCT/.env
chmod 600 /Data/anthropic-auth/$ACCT/.env
chown -R cltx:cltx /Data/anthropic-auth/$ACCT
cat > /etc/systemd/system/ccmax-proxy-$ACCT-224.service <<U
[Unit]
Description=CC Max proxy $ACCT via US egress $EGRESS_PORT ($EXIT_IP) on 224
After=network.target
[Service]
Type=simple
User=root
WorkingDirectory=/Data/claude-max-proxy-$ACCT
EnvironmentFile=/Data/claude-max-proxy-$ACCT/.env
ExecStart=/usr/bin/python3 /Data/claude-max-proxy-$ACCT/proxy.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload
systemctl stop ccmax-proxy-$ACCT-224.service 2>/dev/null || true
fuser -k $PROXY_PORT/tcp 2>/dev/null || true
sleep 2
systemctl enable --now ccmax-proxy-$ACCT-224.service
sleep 4
systemctl is-active ccmax-proxy-$ACCT-224.service
EOF
)
B64=$(printf '%s' "$SETUP" | base64 | tr -d '\n')
s224 "echo $B64 | base64 -d > /tmp/setup-$ACCT.sh"
s224_sudo "bash /tmp/setup-$ACCT.sh; shred -u /tmp/setup-$ACCT.sh" | tail -3
sleep 3  # let proxy settle before probe (avoids activating-race 401)
echo "  local Haiku probe :$PROXY_PORT ->"
s224 "curl -s --max-time 45 -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:$PROXY_PORT/v1/messages -H 'x-api-key: $PROXY_API_KEY' -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' -d '{\\\"model\\\":\\\"claude-haiku-4-5\\\",\\\"max_tokens\\\":8,\\\"messages\\\":[{\\\"role\\\":\\\"user\\\",\\\"content\\\":\\\"hi\\\"}]}'" | tail -1

echo "== [4/6] 198 tunnel 198:$TUNNEL_PORT -> 224:$PROXY_PORT =="
TUNIT="[Unit]
Description=SSH tunnel 198 LiteLLM -> 224 CC Max $ACCT proxy
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=/usr/bin/ssh -i /root/.ssh/ccmax-acct16-224-tunnel -o BatchMode=yes -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -N -L 10.68.13.198:$TUNNEL_PORT:127.0.0.1:$PROXY_PORT cltx@10.68.13.224
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target"
TB64=$(printf '%s' "$TUNIT" | base64 | tr -d '\n')
"$JMS" ssh AIYJY-litellm "echo '$TB64' | base64 -d | sudo tee /etc/systemd/system/ccmax-$ACCT-224-tunnel.service >/dev/null && sudo systemctl daemon-reload && sudo systemctl enable --now ccmax-$ACCT-224-tunnel.service && sleep 3 && curl -s --max-time 10 http://10.68.13.198:$TUNNEL_PORT/health" | tail -1

echo "== [5/6] register 4 LiteLLM entries (api_base 198:$TUNNEL_PORT) =="
for spec in "opus:claude-max-opus:anthropic/claude-opus-4-8" \
            "sonnet:claude-max-sonnet:anthropic/claude-sonnet-4-6" \
            "haiku:claude-max-haiku:anthropic/claude-haiku-4-5" \
            "fable5:fable5:anthropic/claude-fable-5"; do
  IFS=: read -r suf mname model <<<"$spec"
  "$JMS" ssh AIYJY-litellm "curl -s --max-time 15 -X POST http://localhost:30402/model/new -H 'Authorization: Bearer $MASTER_KEY' -H 'content-type: application/json' -d '{\"model_name\":\"$mname\",\"litellm_params\":{\"model\":\"$model\",\"api_base\":\"http://10.68.13.198:$TUNNEL_PORT\",\"api_key\":\"$PROXY_API_KEY\"},\"model_info\":{\"id\":\"ccmax-$ACCT-compat-$suf\",\"mode\":\"chat\"}}' -w ' [ccmax-$ACCT-compat-$suf HTTP %{http_code}]\n' -o /dev/null" | tail -1
done

echo "== [6/6] quota-rebalance POOL_ACCOUNTS += $ACCT =="
PATCHER="import sys
p='/home/cltx/cc-max-quota-rebalance.py'; s=open(p).read()
line='    \"$ACCT\": {\"api_base\": \"http://10.68.13.198:$TUNNEL_PORT\", \"egress_proxy\": \"http://$PROXY_HOST:$EGRESS_PORT\"},'
if '\"$ACCT\":' in s: print('already present'); sys.exit(0)
anchor='POOL_ACCOUNTS = {\n'
s=s.replace(anchor, anchor+line+'\n',1)
open(p,'w').write(s); import py_compile; py_compile.compile(p,doraise=True); print('POOL_ACCOUNTS patched')"
PB64=$(printf '%s' "$PATCHER" | base64 | tr -d '\n')
s224 "cp -f /home/cltx/cc-max-quota-rebalance.py /home/cltx/cc-max-quota-rebalance.py.bak-\$(date +%s); echo $PB64 | base64 -d > /tmp/pool_patch.py && python3 /tmp/pool_patch.py && rm -f /tmp/pool_patch.py" | tail -2
echo "  dry-run:"
s224 "set -a; source /home/cltx/.ccmax-quota/env; set +a; DRY_RUN=1 REBALANCE_JITTER=0 python3 /home/cltx/cc-max-quota-rebalance.py 2>&1 | grep -E '$ACCT|tunnel|done'" | tail -4

echo "✅ $ACCT deployed. Verify routing:"
echo "   $JMS ssh AIYJY-litellm \"curl -sD- -o/dev/null http://localhost:30402/v1/messages -H 'Authorization: Bearer $MASTER_KEY' -H 'anthropic-version: 2023-06-01' -d '{\\\"model\\\":\\\"claude-max-haiku\\\",\\\"max_tokens\\\":8,\\\"messages\\\":[{\\\"role\\\":\\\"user\\\",\\\"content\\\":\\\"hi\\\"}]}' | grep -i x-litellm-model-id\""
