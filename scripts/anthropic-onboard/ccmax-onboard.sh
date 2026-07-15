#!/usr/bin/env bash
# ccmax-onboard.sh — end-to-end orchestrator for adding a CC Max acct to the 224
# per-acct-egress pool. Collapses the deterministic glue into 2 commands; the ONLY
# interactive part left is solving Arkose (you read screenshots + write ctl files).
#
# Runbook: skill `ccmax-acct-add`.  Deploy tail: ccmax-deploy-224.sh (called by finish).
#
#   # 0) see next free acct/ports/egress:
#   scripts/anthropic-onboard/ccmax-onboard.sh alloc
#
#   # 1) prep: builds creds, setup-token, harness, magic-link login -> stops at Arkose
#   #    NOTE: prep takes ~3-4 min (setup-token + patchright install + mail fetch).
#   #    Run it in the BACKGROUND (it exceeds a 120s foreground window and would be
#   #    killed mid-way, leaving the harness up but fill_email/goto un-sent).
#   scripts/anthropic-onboard/ccmax-onboard.sh prep \
#       --acct acct-21 --email x@y.com --sid02 sk-ant-sid02-... --mail-pw PW --egress-port 8084
#
#   # 2) SOLVE ARKOSE (vision): read /tmp/ccv-<acct>/screenshots/round-N.png on 188,
#   #    write /tmp/ccv-<acct>/ctl/round-N.json with {"actions":[{"type":"drag"|"click",...}]}
#   #    until out/code.txt appears (see skill §2). status: out/status.json
#
#   # 3) finish: code -> oat -> verify -> deploy (proxy+tunnel+entries+quota)
#   CLTX_PW='...' scripts/anthropic-onboard/ccmax-onboard.sh finish \
#       --acct acct-21 --proxy-port 3461 --tunnel-port 3471 --egress-port 8084
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JMS="$ROOT/scripts/jms"
S188="cltx@10.68.13.188"
IMG="mcr.microsoft.com/playwright/python:v1.60.0-noble"
PROXY_HOST="38.175.220.46"
PROXY_API_KEY="d89f74ccaaa55b604a010c31be8e4c05d515e102b537c819"
CLTX_PW_224="${CLTX_PW:-Rk#7mQb\$L9nX}"

CMD="${1:-}"; shift || true
ACCT="" EMAIL="" SID02="" MAILPW="" EGRESS_PORT="" PROXY_PORT="" TUNNEL_PORT=""
while [[ $# -gt 0 ]]; do case "$1" in
  --acct) ACCT="$2"; shift 2;; --email) EMAIL="$2"; shift 2;;
  --sid02) SID02="$2"; shift 2;; --mail-pw) MAILPW="$2"; shift 2;;
  --egress-port) EGRESS_PORT="$2"; shift 2;; --proxy-port) PROXY_PORT="$2"; shift 2;;
  --tunnel-port) TUNNEL_PORT="$2"; shift 2;; *) echo "unknown $1"; exit 2;; esac; done

s224() { "$JMS" ssh JSZX-AI-03 "sshpass -p '$CLTX_PW_224' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 cltx@10.68.13.224 \"$1\""; }

# ---------- alloc: scan pool, print next free acct / ports / egress ----------
if [[ "$CMD" == "alloc" ]]; then
  set +e  # tolerate per-probe timeouts; alloc is best-effort read-only
  echo "== egress ports (38.175.220.46:8080-8084) =="
  for line in $(s224 "systemctl list-units --all 2>/dev/null | grep -oE 'ccmax-proxy-acct-?[0-9]+-224' | sort -u" 2>/dev/null); do echo "  running proxy unit: $line"; done
  for p in 8080 8081 8082 8083 8084; do
    ip=$(s224 "curl -s --max-time 8 -x http://$PROXY_HOST:$p https://ipinfo.io/ip 2>/dev/null" | tr -d '\r\n ')
    # which acct .env references this egress, and is that acct's service actually running?
    acct=$(s224 "grep -l ':$p' /Data/claude-max-proxy-*/.env 2>/dev/null | head -1 | xargs -r -n1 dirname | xargs -r -n1 basename | sed 's/claude-max-proxy-//'" 2>/dev/null | tr -d '\r\n ')
    if [[ -n "$acct" ]]; then
      nodash=$(echo "$acct" | tr -d -)   # acct-19 legacy service = ccmax-proxy-acct19-224 (no dash)
      act=$(s224 "systemctl is-active ccmax-proxy-$acct-224.service ccmax-proxy-$nodash-224.service 2>/dev/null | grep -m1 -x active" | tr -d '\r\n ')
      [[ "$act" == "active" ]] && state="IN USE by $acct" || state="FREE (stale $acct)"
    else
      state="FREE"
    fi
    echo "  :$p -> ${ip:-closed}  $state"
  done
  echo "== max acct number in use =="
  s224 "ls -d /Data/claude-max-proxy-acct-* 2>/dev/null | grep -oE 'acct-[0-9]+' | sort -t- -k2 -n | tail -3"
  echo "port convention (acct-19 legacy 3456/3467/8082): PROXY=3460+(N-20) TUNNEL=3470+(N-20)"
  exit 0
fi

WORK="/tmp/ccv-$ACCT"

# ---------- prep: creds -> setup-token -> harness -> magic-link login -> Arkose ----------
if [[ "$CMD" == "prep" ]]; then
  : "${ACCT:?} ${EMAIL:?} ${SID02:?} ${MAILPW:?} ${EGRESS_PORT:?}"
  [[ "$SID02" == sk-ant-sid02-* ]] || { echo "❌ --sid02 must be sk-ant-sid02-"; exit 1; }
  EGRESS="http://$PROXY_HOST:$EGRESS_PORT"

  echo "== egress $EGRESS_PORT -> exit IP =="
  EXIT_IP=$(ssh -o BatchMode=yes -o ConnectTimeout=20 "$S188" "curl -s --max-time 12 -x $EGRESS https://ipinfo.io/ip" | tr -d '\r\n ')
  echo "  $EXIT_IP"; [[ -n "$EXIT_IP" ]] || { echo "dead egress"; exit 1; }

  echo "== 188 .creds + work dirs =="
  ssh -o BatchMode=yes "$S188" "mkdir -p /Data/anthropic-auth/$ACCT $WORK/screenshots $WORK/ctl $WORK/out; \
    rm -f $WORK/ctl/* $WORK/out/* $WORK/screenshots/* 2>/dev/null; \
    printf 'email=%s\nsession_key=%s\nmail_pw=%s\nmail_provider=sessionkey\n' '$EMAIL' '$SID02' '$MAILPW' > /Data/anthropic-auth/$ACCT/.creds; chmod 600 /Data/anthropic-auth/$ACCT/.creds; \
    printf %s '$MAILPW' > $WORK/mail_pw.txt"

  echo "== setup-token via $EGRESS (tmux cc-oauth-$ACCT) =="
  OAUTH_URL=$(ssh -o BatchMode=yes "$S188" "export PATH=\$HOME/.local/bin:\$PATH; tmux kill-session -t cc-oauth-$ACCT 2>/dev/null; rm -f /tmp/cc-oauth-$ACCT.log; \
    tmux new-session -d -s cc-oauth-$ACCT \"HTTPS_PROXY=$EGRESS HTTP_PROXY=$EGRESS ALL_PROXY=$EGRESS claude setup-token 2>&1 | tee /tmp/cc-oauth-$ACCT.log\"; \
    for i in \$(seq 1 40); do U=\$(python3 -c 'import re,sys;t=open(sys.argv[1],errors=\"ignore\").read().replace(chr(13),\"\");f=\"\".join(t.splitlines());m=re.search(r\"https://claude\.com/cai/oauth/authorize\?[^ ]*?state=[A-Za-z0-9_-]+\",f);print(m.group(0) if m else \"\")' /tmp/cc-oauth-$ACCT.log); [ -n \"\$U\" ] && { echo \"\$U\"; break; }; sleep 1; done")
  [[ -n "$OAUTH_URL" ]] || { echo "❌ no OAuth URL"; exit 2; }
  STATE=$(printf '%s' "$OAUTH_URL" | grep -oE 'state=[^&]+' | cut -d= -f2)
  echo "  state=$STATE"
  ssh -o BatchMode=yes "$S188" "printf %s '$OAUTH_URL' > $WORK/oauth_url.txt; printf %s '$STATE' > $WORK/state.txt; printf %s '$EXIT_IP' > $WORK/expect_ip.txt"

  echo "== launch harness (egress-gated to $EXIT_IP) =="
  ssh -o BatchMode=yes "$S188" "cat > $WORK/env <<E
CC_EMAIL=$EMAIL
SESSION_KEY=$SID02
CC_OAUTH_URL=$OAUTH_URL
INJECT_COOKIE=0
PROXY_SERVER=$EGRESS
EXPECT_EXIT_IP=$EXIT_IP
MAX_ROUNDS=40
ROUND_TIMEOUT_SEC=1800
E
    docker rm -f ccv-$ACCT >/dev/null 2>&1; \
    docker run -d --name ccv-$ACCT --env-file $WORK/env -v /tmp/cc-oauth-vision.py:/work/script.py:ro \
      -v $WORK/screenshots:/work/screenshots -v $WORK/ctl:/work/ctl -v $WORK/out:/work/out \
      -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 $IMG \
      bash -c 'Xvfb :99 -screen 0 1366x1000x24 >/dev/null 2>&1 & sleep 1 && pip install patchright==1.60.0 -q --root-user-action=ignore 2>&1 | tail -1 && python3 /work/script.py' >/dev/null && echo harness-up"
  echo "  waiting for egress gate + login page..."
  for i in $(seq 1 30); do
    EIP=$(ssh -o BatchMode=yes "$S188" "cat $WORK/out/exit_ip.txt 2>/dev/null" | tr -d '\r\n ')
    [[ -n "$EIP" ]] && break; sleep 4
  done
  echo "  harness exit IP: ${EIP:-?} (expect $EXIT_IP)"
  [[ "$EIP" == "$EXIT_IP" ]] || { echo "❌ egress gate mismatch — abort"; exit 3; }

  echo "== email auto-submitted by harness; waiting for magic-link email =="
  sleep 14

  echo "== fetch magic-link (mail.com lightmailer via $EGRESS) =="
  DIRECT=""
  for attempt in 1 2; do
    ssh -o BatchMode=yes "$S188" "rm -f $WORK/out/magiclink.txt; docker rm -f ccv-mail-$ACCT >/dev/null 2>&1; \
      docker run -d --name ccv-mail-$ACCT -v /tmp/cc-mailcom-code.py:/work/script.py:ro -v $WORK/mail_pw.txt:/run/mail_pw.txt:ro \
        -v $WORK/screenshots:/work/screenshots -v $WORK/out:/work/out \
        -e MAIL_USER=$EMAIL -e MAIL_PW_FILE=/run/mail_pw.txt -e PROXY_SERVER=$EGRESS \
        -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 -e HEADLESS=0 $IMG \
        bash -c 'Xvfb :99 -screen 0 1366x1000x24 >/dev/null 2>&1 & sleep 1 && pip install playwright==1.60.0 -q --root-user-action=ignore 2>&1 | tail -1 && python3 /work/script.py' >/dev/null"
    for i in $(seq 1 30); do
      ML=$(ssh -o BatchMode=yes "$S188" "cat $WORK/out/magiclink.txt 2>/dev/null" | tr -d '\r\n ')
      [[ -n "$ML" ]] && break; sleep 5
    done
    if [[ -n "$ML" ]]; then
      DIRECT=$(python3 -c "import urllib.parse,re,sys;m=re.search(r'redirectUrl=([^&]+)',sys.argv[1]);print(urllib.parse.unquote(m.group(1)) if m else sys.argv[1])" "$ML")
      [[ "$DIRECT" == *claude.ai/magic-link* ]] && break
    fi
    echo "  attempt $attempt: no magic-link yet, re-trigger email..."
    ssh -o BatchMode=yes "$S188" "echo '{\"actions\":[{\"type\":\"wait\",\"ms\":500}]}' > $WORK/ctl/round-\$(date +%s).json" 2>/dev/null || true
  done
  [[ -n "$DIRECT" ]] || { echo "❌ magic-link not found (check $WORK/screenshots/mail-*.png)"; exit 2; }
  echo "  direct magic-link: ${DIRECT:0:60}..."

  echo "== feed magic-link -> login -> Authorize (Arkose will pop) =="
  ssh -o BatchMode=yes "$S188" "echo '{\"actions\":[{\"type\":\"goto\",\"url\":\"$DIRECT\"},{\"type\":\"wait\",\"ms\":9000},{\"type\":\"authorize\"},{\"type\":\"wait\",\"ms\":6000}]}' > $WORK/ctl/round-0.json"
  sleep 26
  echo ""
  echo "======================================================================"
  echo " PREP DONE. Now SOLVE ARKOSE (vision loop):"
  echo "   status : ssh $S188 'cat $WORK/out/status.json'"
  echo "   shots  : ssh $S188 'ls -t $WORK/screenshots/round-*.png | head'  (scp to view)"
  echo "   drive  : write $WORK/ctl/round-<N>.json {\"actions\":[{drag/click...}]}"
  echo "   done   : out/code.txt appears -> run finish"
  echo "======================================================================"
  ssh -o BatchMode=yes "$S188" "cat $WORK/out/status.json 2>/dev/null; echo; cat $WORK/out/code.txt 2>/dev/null && echo ' <-- CODE ALREADY (no Arkose!)'" || true
  exit 0
fi

# ---------- finish: code -> oat -> verify -> deploy ----------
if [[ "$CMD" == "finish" ]]; then
  : "${ACCT:?} ${EGRESS_PORT:?} ${PROXY_PORT:?} ${TUNNEL_PORT:?}"
  EGRESS="http://$PROXY_HOST:$EGRESS_PORT"
  echo "== wait for out/code.txt (solve Arkose meanwhile) =="
  CODE=""
  for i in $(seq 1 120); do
    CODE=$(ssh -o BatchMode=yes "$S188" "cat $WORK/out/code.txt 2>/dev/null" | tr -d '\r\n ')
    [[ -n "$CODE" ]] && break; sleep 10
  done
  [[ -n "$CODE" ]] || { echo "❌ no code after wait; keep solving then re-run finish"; exit 2; }
  STATE=$(ssh -o BatchMode=yes "$S188" "cat $WORK/state.txt")
  echo "  code=${CODE:0:12}... state=${STATE:0:12}..."
  # code.txt may already be the combined "code#state" (harness sometimes captures
  # it off the callback page). Don't double-append state.
  case "$CODE" in *"#"*) FULL="$CODE";; *) FULL="$CODE#$STATE";; esac

  echo "== paste code#state -> setup-token -> oat =="
  OAT=$(ssh -o BatchMode=yes "$S188" "export PATH=\$HOME/.local/bin:\$PATH; \
    tmux send-keys -t cc-oauth-$ACCT -l '$FULL'; sleep 1; tmux send-keys -t cc-oauth-$ACCT Enter; sleep 12; \
    grep -oE 'sk-ant-oat[a-zA-Z0-9_-]+' /tmp/cc-oauth-$ACCT.log | tail -1")
  [[ "$OAT" == sk-ant-oat01-* ]] || { echo "❌ no oat (code may be expired; re-prep)"; exit 2; }
  echo "  oat: ${OAT:0:24}... (len ${#OAT})"

  echo "== verify oat via $EGRESS (Haiku) =="
  HC=$(ssh -o BatchMode=yes "$S188" "curl -s --max-time 40 -x $EGRESS https://api.anthropic.com/v1/messages -H 'Authorization: Bearer $OAT' -H 'anthropic-beta: oauth-2025-04-20' -H 'anthropic-dangerous-direct-browser-access: true' -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' -d '{\"model\":\"claude-haiku-4-5\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}' -o /dev/null -w '%{http_code}'")
  echo "  Haiku HTTP $HC"; [[ "$HC" == "200" ]] || { echo "❌ token probe not 200"; exit 3; }

  echo "== cleanup 188 =="
  ssh -o BatchMode=yes "$S188" "docker rm -f ccv-$ACCT ccv-mail-$ACCT >/dev/null 2>&1; tmux kill-session -t cc-oauth-$ACCT 2>/dev/null; shred -u $WORK/mail_pw.txt 2>/dev/null || rm -f $WORK/mail_pw.txt; echo cleaned"

  echo "== deploy (proxy+tunnel+entries+quota) =="
  CLTX_PW="$CLTX_PW_224" MASTER_KEY="${MASTER_KEY:-sk-pro-litellm-ce077e2b0721bb419a633e4d}" \
    "$ROOT/scripts/anthropic-onboard/ccmax-deploy-224.sh" \
    --acct "$ACCT" --oat "$OAT" --proxy-port "$PROXY_PORT" --tunnel-port "$TUNNEL_PORT" --egress-port "$EGRESS_PORT"
  exit 0
fi

echo "usage: ccmax-onboard.sh {alloc|prep|finish} [flags]  (see header)"; exit 2
