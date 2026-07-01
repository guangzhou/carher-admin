#!/usr/bin/env bash
# batch-retry.sh — serial onboard of multiple zerokey accounts on 188.
#
# Resume-safe: skips accounts that already have state/users.json.
# Cooldown-aware: 60s sleep between accounts to lower OpenAI submission rate-limit
# signal; capture.py fails fast on max_check_attempts (cooldown ~10min).
# Profile reuse: NEVER clears state/profile, so cookie-cache hits skip OTP (~75s/acct).
#
# Usage:
#   batch-retry.sh acct50 acct51 acct52 ...
#
# Each <acct> must already have:
#   ~/zerokey-codex-accounts/<acct>/ops.env              (MAIL_USER, PORT)
#   ~/zerokey-codex-accounts/<acct>/secrets/mail_pw.txt
#   ~/zerokey-codex-accounts/<acct>/secrets/chatgpt_pw.txt
#
# Env overrides:
#   CAPTURE_TIMEOUT=600   (default 600s; add-account.sh default is 900s but profile-reuse
#                          completes ~75s, so 600 catches genuinely-stuck capture early)
#   COOLDOWN=60           (sleep between accounts)
#   SKIP_OK=1             (default; skip accounts whose users.json already exists)
#                          set SKIP_OK=0 to force re-capture
#
# Output:
#   /tmp/zk-batch-<acct>.log   per-acct full capture log
#   /tmp/zk-batch.summary      table written at end (also echoed)

set -uo pipefail

ACCTS=("$@")
if [[ ${#ACCTS[@]} -eq 0 ]]; then
  echo "usage: $0 <acct1> [acct2 ...]"
  exit 2
fi

OPS_DIR="${ZK_MAIN:-$HOME/zerokey-codex}/ops"
ACCT_ROOT="${ZK_ACCOUNTS_ROOT:-$HOME/zerokey-codex-accounts}"
CAPTURE_TIMEOUT="${CAPTURE_TIMEOUT:-600}"
COOLDOWN="${COOLDOWN:-60}"
SKIP_OK="${SKIP_OK:-1}"

mkdir -p /tmp
SUMMARY=/tmp/zk-batch.summary
: > "$SUMMARY"

declare -A RESULT

for ACCT in "${ACCTS[@]}"; do
  BASE="$ACCT_ROOT/$ACCT"
  LOG="/tmp/zk-batch-$ACCT.log"

  if [[ ! -f "$BASE/ops.env" ]]; then
    echo "=== $ACCT: missing ops.env, skip"
    RESULT[$ACCT]="MISS_OPS_ENV"
    continue
  fi
  EMAIL=$(grep ^MAIL_USER= "$BASE/ops.env" | cut -d= -f2-)
  PORT=$(grep ^PORT= "$BASE/ops.env" | cut -d= -f2-)
  MAIL_PW=$(cat "$BASE/secrets/mail_pw.txt" 2>/dev/null | tr -d '\r\n ')
  CHATGPT_PW=$(cat "$BASE/secrets/chatgpt_pw.txt" 2>/dev/null | tr -d '\r\n ')

  if [[ -z $MAIL_PW || -z $CHATGPT_PW ]]; then
    echo "=== $ACCT: missing creds, skip"
    RESULT[$ACCT]="MISS_CREDS"
    continue
  fi

  if [[ "$SKIP_OK" == "1" && -f "$BASE/state/users.json" ]]; then
    # already captured; just ensure container is up
    if docker ps --filter "name=zerokey-codex-$ACCT" --format '{{.Names}}' | grep -q "zerokey-codex-$ACCT"; then
      RESULT[$ACCT]="ALREADY_OK"
      echo "=== $ACCT: users.json exists + container up, skip"
    else
      echo "=== $ACCT: users.json exists but container down → docker compose up -d"
      (cd "$BASE" && docker compose up -d 2>&1) | tee -a "$LOG"
      sleep 3
      if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; then
        RESULT[$ACCT]="COMPOSED_UP"
      else
        RESULT[$ACCT]="COMPOSE_FAIL"
      fi
    fi
    continue
  fi

  echo ""
  echo "==================================================="
  echo "==> $ACCT email=$EMAIL port=$PORT timeout=${CAPTURE_TIMEOUT}s log=$LOG"
  echo "==================================================="
  START=$(date +%s)
  CAPTURE_TIMEOUT="$CAPTURE_TIMEOUT" bash "$OPS_DIR/add-account.sh" \
    "$ACCT" "$EMAIL" "$MAIL_PW" "$CHATGPT_PW" "$PORT" > "$LOG" 2>&1
  RC=$?
  DUR=$(( $(date +%s) - START ))

  if [[ -f "$BASE/state/users.json" ]] && \
     docker ps --filter "name=zerokey-codex-$ACCT" --format '{{.Names}}' | grep -q "zerokey-codex-$ACCT"; then
    RESULT[$ACCT]="OK (${DUR}s)"
  elif [[ -f "$BASE/state/users.json" ]]; then
    RESULT[$ACCT]="CAPTURED_NO_COMPOSE (${DUR}s)"
  elif grep -q max_check_attempts "$LOG" 2>/dev/null; then
    RESULT[$ACCT]="COOLDOWN_MAX_CHECK (${DUR}s)"
  elif grep -qE "OTP fetch failed|OTP auto failed" "$LOG" 2>/dev/null; then
    RESULT[$ACCT]="OTP_NOT_DELIVERED (${DUR}s)"
  elif [[ $RC -eq 124 ]]; then
    RESULT[$ACCT]="TIMEOUT (${DUR}s)"
  else
    RESULT[$ACCT]="FAIL rc=$RC (${DUR}s)"
  fi
  echo "RESULT $ACCT ${RESULT[$ACCT]}"

  # cooldown unless last
  if [[ "$ACCT" != "${ACCTS[-1]}" ]]; then
    sleep "$COOLDOWN"
  fi
done

echo ""
echo "=========== SUMMARY ==========="
{
  for ACCT in "${ACCTS[@]}"; do
    printf "  %-12s %s\n" "$ACCT" "${RESULT[$ACCT]:-NOT_RUN}"
  done
} | tee "$SUMMARY"
