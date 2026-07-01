#!/usr/bin/env bash
# onboard-one.sh — deterministic single-account zerokey onboarding on 188.
#
# Unlike add-account.sh (in-capture mail.com auto, flaky), this drives the
# proven flow that worked for zyq:
#   1. mailread purge  — delete stale ChatGPT login-code emails
#   2. capture (FORCE_LOGIN=1, fast auto-giveup, then wait on otp.txt)
#   3. mailread read   — fetch the freshly-emailed 6-digit code → otp.txt
#   4. start per-account zerokey container + health check
#
# Mounts the patched capture + mailread scripts from $MAIN/capture at runtime,
# so no image rebuild is required.
#
# Usage: ./onboard-one.sh <account_id> <email> <mail_pw> <chatgpt_pw> <port>
set -uo pipefail

ACCOUNT="${1:?account id}"; EMAIL="${2:?email}"; MAIL_PW="${3:?mail pw}"
CHATGPT_PW="${4:?chatgpt pw}"; PORT="${5:?port}"

MAIN="${ZK_MAIN:-$HOME/zerokey-codex}"
BASE="${ZK_ACCOUNTS_ROOT:-$HOME/zerokey-codex-accounts}/$ACCOUNT"
IMG="${CAPTURE_IMAGE:-zerokey-capture:latest}"
CAP_SCRIPT="$MAIN/capture/zerokey-web-capture.py"
MR_SCRIPT="$MAIN/capture/mailread-otp.py"
LOG="$BASE/state/out/onboard.log"

log(){ echo "[onboard:$ACCOUNT] $*" | tee -a "$LOG"; }

mkdir -p "$BASE"/{secrets,state/out/screenshots,state/profile,state/mailprofile,logs,ops}
: > "$LOG"
chmod 700 "$BASE/secrets"
printf '%s\n' "$MAIL_PW"    > "$BASE/secrets/mail_pw.txt"
printf '%s\n' "$CHATGPT_PW" > "$BASE/secrets/chatgpt_pw.txt"
chmod 600 "$BASE/secrets/"*.txt

cat > "$BASE/ops.env" <<EOF
MAIL_USER=$EMAIL
ZK_USER=$ACCOUNT
PORT=$PORT
SERVER_CONTAINER=zerokey-codex-$ACCOUNT
EOF
cp "$MAIN/ops/refresh.sh" "$BASE/ops/refresh.sh"; chmod +x "$BASE/ops/refresh.sh"
sed "s/\${ACCOUNT:-timothy}/$ACCOUNT/g; s/\${PORT:-8124}/$PORT/g; s/\${ZK_USER:-timothy}/$ACCOUNT/g" \
  "$MAIN/ops/docker-compose.account.yml" > "$BASE/docker-compose.yml"

rm -f "$BASE/state/out/otp.txt" "$BASE/state/out/zerokey-users.json"

run_mail(){ # MODE=$1 ; prints stdout
  local mode="$1"
  timeout 240 docker run --rm \
    -e MAIL_USER="$EMAIL" -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
    -e PROFILE_DIR=/work/mailprofile -e SCREENSHOT_DIR=/work/screenshots \
    -e MODE="$mode" -e READ_MAX=20 \
    -v "$BASE/state/mailprofile:/work/mailprofile" \
    -v "$BASE/state/out/screenshots:/work/screenshots" \
    -v "$BASE/secrets/mail_pw.txt:/run/mail_pw.txt:ro" \
    -v "$MR_SCRIPT:/capture/mailread-otp.py:ro" \
    --entrypoint bash "$IMG" -lc "xvfb-run -a python /capture/mailread-otp.py; exit \$?" 2>&1
}

log "step1: purge stale codes"
run_mail purge | tail -5 | tee -a "$LOG" || true

log "step2: start capture (FORCE_LOGIN, file-wait)"
docker rm -f "zerokey-cap-$ACCOUNT" >/dev/null 2>&1 || true
docker run -d --name "zerokey-cap-$ACCOUNT" \
  -e MAIL_USER="$EMAIL" -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
  -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
  -e OUT_JSON=/work/out/zerokey-users.json \
  -e ZK_USER="$ACCOUNT" -e PROFILE_DIR=/work/profile -e SCREENSHOT_DIR=/work/screenshots \
  -e FORCE_LOGIN=1 -e OTP_AUTO_ONLY=0 -e OTP_AUTO_MAX=15 -e OTP_FILE_WAIT=600 -e OTP_FILE=/work/out/otp.txt \
  -v "$BASE/state/profile:/work/profile" \
  -v "$BASE/state/out:/work/out" \
  -v "$BASE/state/out/screenshots:/work/screenshots" \
  -v "$BASE/secrets/mail_pw.txt:/run/mail_pw.txt:ro" \
  -v "$BASE/secrets/chatgpt_pw.txt:/run/chatgpt_pw.txt:ro" \
  -v "$CAP_SCRIPT:/capture/zerokey-web-capture.py:ro" \
  "$IMG" >/dev/null

log "step3: wait for OTP prompt"
for i in $(seq 1 80); do
  docker logs "zerokey-cap-$ACCOUNT" 2>&1 | grep -q "OTP_WAIT_FILE" && { log "  capture reached OTP file-wait"; break; }
  if ! docker ps -q -f name="zerokey-cap-$ACCOUNT" | grep -q .; then log "  capture exited early"; break; fi
  sleep 3
done
sleep 6  # let the email settle

log "step3b: read fresh OTP"
CODE=""
for t in 1 2 3 4; do
  OUT="$(run_mail read || true)"
  CODE="$(echo "$OUT" | grep -oE 'ZKOTP=[0-9]{6}' | head -1 | cut -d= -f2)"
  [ -n "$CODE" ] && { log "  got OTP=$CODE"; break; }
  log "  no code yet (try $t)"; sleep 12
done
if [ -z "$CODE" ]; then
  log "ERROR: no OTP obtained"; docker logs "zerokey-cap-$ACCOUNT" 2>&1 | tail -20 | tee -a "$LOG"
  docker rm -f "zerokey-cap-$ACCOUNT" >/dev/null 2>&1 || true; exit 1
fi
echo "$CODE" > "$BASE/state/out/otp.txt"

log "step4: wait capture completion"
for i in $(seq 1 60); do
  docker ps -q -f name="zerokey-cap-$ACCOUNT" | grep -q . || break
  sleep 3
done
docker logs "zerokey-cap-$ACCOUNT" 2>&1 | tail -30 > "$BASE/state/out/capture.log"
docker rm -f "zerokey-cap-$ACCOUNT" >/dev/null 2>&1 || true

if [ ! -s "$BASE/state/out/zerokey-users.json" ]; then
  log "ERROR: no users.json captured"; tail -20 "$BASE/state/out/capture.log" | tee -a "$LOG"; exit 1
fi
cp "$BASE/state/out/zerokey-users.json" "$BASE/state/users.json"
log "session captured ($(wc -c < "$BASE/state/users.json") bytes)"

log "step5: start server :$PORT"
(cd "$BASE" && docker compose up -d)
sleep 5
if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; then
  log "OK health :$PORT"
else
  log "WARN health failed — docker logs zerokey-codex-$ACCOUNT"; exit 1
fi
log "DONE account=$ACCOUNT port=$PORT api=http://10.68.13.188:$PORT/v1"
