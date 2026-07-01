#!/usr/bin/env bash
# manual-onboard.sh — onboard a new zerokey account using MANUAL email-OTP injection.
#
# Why: mail.com auto-OTP scraper (add-account.sh, OTP_AUTO_ONLY=1) is unreliable —
# the inbox lives in a cross-origin iframe and the 6-digit code sits in the email
# BODY, so the auto-reader frequently times out ("inbox keyword never appeared").
# This helper instead screenshots the opened OTP email so a human (or vision agent)
# reads the code and drops it into otp.txt; capture then proceeds.
#
# Two phases (run start, inject OTP out-of-band, then finish):
#   ./manual-onboard.sh start  <account_id> <email> <mail_pw> <chatgpt_pw> <port>
#       → scaffolds dir/secrets/compose, launches capture in background (manual OTP),
#         prints the otpshot + otp.txt paths, returns immediately.
#       → you: read state/out/otpshot.png, then  echo <code> > state/out/otp.txt
#   ./manual-onboard.sh finish <account_id> <port>
#       → waits for zerokey-users.json, swaps live session, docker compose up, health.
#
# Idempotent: re-running start re-scaffolds (keeps profile); finish is safe to retry.
set -uo pipefail

CMD="${1:?start|finish}"; shift

MAIN="${ZK_MAIN:-$HOME/zerokey-codex}"
ACCOUNTS_ROOT="${ZK_ACCOUNTS_ROOT:-$HOME/zerokey-codex-accounts}"
CAPTURE_IMAGE="${CAPTURE_IMAGE:-zerokey-capture:latest}"

scaffold() {
  local ACCOUNT="$1" EMAIL="$2" MAIL_PW="$3" CHATGPT_PW="$4" PORT="$5"
  local BASE="$ACCOUNTS_ROOT/$ACCOUNT"
  mkdir -p "$BASE"/{secrets,state/out/screenshots,state/profile,logs,ops}
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
  cp "$MAIN/ops/refresh.sh" "$BASE/ops/refresh.sh"
  chmod +x "$BASE/ops/refresh.sh"
  sed "s/\${ACCOUNT:-timothy}/$ACCOUNT/g; s/\${PORT:-8124}/$PORT/g; s/\${ZK_USER:-timothy}/$ACCOUNT/g" \
    "$MAIN/ops/docker-compose.account.yml" > "$BASE/docker-compose.yml"
}

case "$CMD" in
  start)
    ACCOUNT="${1:?account id}"; EMAIL="${2:?email}"; MAIL_PW="${3:?mail_pw}"; CHATGPT_PW="${4:?chatgpt_pw}"; PORT="${5:?port}"
    BASE="$ACCOUNTS_ROOT/$ACCOUNT"
    [[ -d "$MAIN/zerokey" ]] || { echo "ERROR: main install missing at $MAIN" >&2; exit 1; }
    scaffold "$ACCOUNT" "$EMAIL" "$MAIL_PW" "$CHATGPT_PW" "$PORT"
    rm -f "$BASE/state/out/otp.txt" "$BASE/state/out/otpshot.png" "$BASE/state/out/zerokey-users.json" "$BASE/state/out/capture.log"
    if [[ "$EMAIL" == *@qq.com ]]; then
      OTP_ENV="-e MAIL_OTP_PROVIDER=imap_qq -e OTP_AUTO_MAX=0 -e OTP_SHOT=0 -e OTP_AUTO_ONLY=0"
      OTP_HINT="IMAP auto-fetch (QQ); no otpshot"
    else
      OTP_ENV="-e MAIL_OTP_PROVIDER=mailcom -e OTP_AUTO_MAX=0 -e OTP_SHOT=1 -e OTP_AUTO_ONLY=0"
      OTP_HINT="otpshot + manual otp.txt inject"
    fi
    setsid bash -c "docker run --rm --name cap-$ACCOUNT \
      -e MAIL_USER=$EMAIL \
      -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
      -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
      -e OUT_JSON=/work/out/zerokey-users.json \
      -e ZK_USER=$ACCOUNT \
      -e PROFILE_DIR=/work/profile \
      -e SCREENSHOT_DIR=/work/screenshots \
      $OTP_ENV -e OTP_FILE_WAIT=600 \
      -v $BASE/state/profile:/work/profile \
      -v $BASE/state/out:/work/out \
      -v $BASE/state/out/screenshots:/work/screenshots \
      -v $BASE/secrets/mail_pw.txt:/run/mail_pw.txt:ro \
      -v $BASE/secrets/chatgpt_pw.txt:/run/chatgpt_pw.txt:ro \
      $CAPTURE_IMAGE > $BASE/state/out/capture.log 2>&1" </dev/null >/dev/null 2>&1 &
    disown
    echo "[manual-onboard] $ACCOUNT capture launched (port $PORT)"
    echo "  log:     $BASE/state/out/capture.log"
    echo "  otp:     $OTP_HINT"
    if [[ "$EMAIL" != *@qq.com ]]; then
      echo "  otpshot: $BASE/state/out/otpshot.png   (appears after ~2-3min)"
      echo "  inject:  echo <6-digit-code> > $BASE/state/out/otp.txt"
    fi
    ;;
  finish)
    ACCOUNT="${1:?account id}"; PORT="${2:?port}"
    BASE="$ACCOUNTS_ROOT/$ACCOUNT"
    echo "[manual-onboard] $ACCOUNT waiting for zerokey-users.json (up to 5min)…"
    for i in $(seq 1 60); do
      [[ -s "$BASE/state/out/zerokey-users.json" ]] && break
      sleep 5
    done
    [[ -s "$BASE/state/out/zerokey-users.json" ]] || { echo "ERROR: capture did not produce users.json — tail capture.log:" >&2; tail -20 "$BASE/state/out/capture.log" >&2; exit 1; }
    cp "$BASE/state/out/zerokey-users.json" "$BASE/state/users.json"
    echo "[manual-onboard] session → $BASE/state/users.json"
    (cd "$BASE" && docker compose up -d)
    sleep 4
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; then
      echo "[manual-onboard] OK health=http://127.0.0.1:$PORT/health"
      curl -s "http://127.0.0.1:$PORT/v1/models" | head -c 120; echo
    else
      echo "WARN: health failed — docker logs zerokey-codex-$ACCOUNT" >&2; exit 1
    fi
    ;;
  *) echo "usage: $0 start|finish ..." >&2; exit 2 ;;
esac
