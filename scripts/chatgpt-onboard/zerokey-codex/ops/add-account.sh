#!/usr/bin/env bash
# add-account.sh — onboard a second (or Nth) ChatGPT web account for zerokey on 188.
#
# Each account gets its own directory, browser profile, session JSON, Docker container,
# and listen port. Shares capture/server images from the main ~/zerokey-codex install.
#
# Usage:
#   ./add-account.sh <account_id> <email> <mail.com_pw> <chatgpt_pw> [port]
#
# Example:
#   ./add-account.sh timothy timothy_mossey871@mail.com '<mail_pw>' '<gpt_pw>' 8124
#
# First capture runs with OTP_AUTO_ONLY=1 (mail.com OTP fully automated, no manual file).
set -euo pipefail

ACCOUNT="${1:?account id (e.g. timothy)}"
EMAIL="${2:?mail.com email}"
MAIL_PW="${3:?mail.com webmail password}"
CHATGPT_PW="${4:?ChatGPT login password}"
PORT="${5:-8124}"

MAIN="${ZK_MAIN:-$HOME/zerokey-codex}"
BASE="${ZK_ACCOUNTS_ROOT:-$HOME/zerokey-codex-accounts}/$ACCOUNT"
ZK_USER="$ACCOUNT"
CAPTURE_IMAGE="${CAPTURE_IMAGE:-zerokey-capture:latest}"
CAPTURE_TIMEOUT="${CAPTURE_TIMEOUT:-900}"

if [[ ! -d "$MAIN/zerokey" ]]; then
  echo "ERROR: main install missing at $MAIN — run install.sh first" >&2
  exit 1
fi

echo "[add-account] account=$ACCOUNT email=$EMAIL port=$PORT base=$BASE"

mkdir -p "$BASE"/{secrets,state/out/screenshots,state/profile,logs}
chmod 700 "$BASE/secrets"
printf '%s\n' "$MAIL_PW"    > "$BASE/secrets/mail_pw.txt"
printf '%s\n' "$CHATGPT_PW" > "$BASE/secrets/chatgpt_pw.txt"
chmod 600 "$BASE/secrets/"*.txt

# ops scripts (account-local env overrides)
cat > "$BASE/ops.env" <<EOF
MAIL_USER=$EMAIL
ZK_USER=$ACCOUNT
PORT=$PORT
SERVER_CONTAINER=zerokey-codex-$ACCOUNT
EOF

mkdir -p "$BASE/ops"
cp "$MAIN/ops/refresh.sh" "$BASE/ops/refresh.sh"
chmod +x "$BASE/ops/refresh.sh"

# docker-compose for this account
sed "s/\${ACCOUNT:-timothy}/$ACCOUNT/g; s/\${PORT:-8124}/$PORT/g; s/\${ZK_USER:-timothy}/$ACCOUNT/g" \
  "$MAIN/ops/docker-compose.account.yml" > "$BASE/docker-compose.yml"

# rebuild capture image if main capture script changed
if [[ -f "$MAIN/capture/zerokey-web-capture.py" ]]; then
  echo "[add-account] building capture image…"
  (cd "$MAIN/capture" && docker build -t "$CAPTURE_IMAGE" .)
fi

echo "[add-account] running initial capture (OTP_AUTO_ONLY, timeout=${CAPTURE_TIMEOUT}s)…"
rm -f "$BASE/state/out/zerokey-users.json"
timeout "$CAPTURE_TIMEOUT" docker run --rm \
  -e MAIL_USER="$EMAIL" \
  -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
  -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
  -e OUT_JSON=/work/out/zerokey-users.json \
  -e ZK_USER="$ZK_USER" \
  -e PROFILE_DIR=/work/profile \
  -e SCREENSHOT_DIR=/work/screenshots \
  -e OTP_AUTO_ONLY=1 \
  -e OTP_AUTO_MAX=300 \
  -e OTP_FILE_WAIT=0 \
  -v "$BASE/state/profile:/work/profile" \
  -v "$BASE/state/out:/work/out" \
  -v "$BASE/state/out/screenshots:/work/screenshots" \
  -v "$BASE/secrets/mail_pw.txt:/run/mail_pw.txt:ro" \
  -v "$BASE/secrets/chatgpt_pw.txt:/run/chatgpt_pw.txt:ro" \
  "$CAPTURE_IMAGE"
rc=$?
if [[ $rc -ne 0 ]]; then
  echo "ERROR: initial capture failed (exit $rc). Check $BASE/state/out/screenshots/" >&2
  exit "$rc"
fi

cp "$BASE/state/out/zerokey-users.json" "$BASE/state/users.json"
echo "[add-account] session captured → $BASE/state/users.json"

echo "[add-account] starting zerokey on port $PORT…"
(cd "$BASE" && docker compose up -d)

sleep 3
if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; then
  echo "[add-account] OK health=http://127.0.0.1:$PORT/health"
  curl -s "http://127.0.0.1:$PORT/v1/models" | head -c 120
  echo
else
  echo "WARN: health check failed — docker logs zerokey-codex-$ACCOUNT" >&2
  exit 1
fi

cat <<EOF

[add-account] done.
  API:  http://10.68.13.188:$PORT/v1
  raw:  Authorization: Bearer raw
  refresh cron: MAIL_USER=$EMAIL ZK_USER=$ACCOUNT PORT=$PORT $BASE/ops/refresh.sh
  LiteLLM: add model_list entries with api_base http://10.68.13.188:$PORT/v1
EOF
