#!/bin/bash
# add-chatgpt-acct-198-hotmail-batch.sh — batch 5 hotmail accts
# Runs hotmail driver serially; on success continues to next, on fail stops.
#
# Usage:
#   ./add-chatgpt-acct-198-hotmail-batch.sh
set -eo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DRIVER="$REPO_DIR/scripts/add-chatgpt-acct-198-hotmail.sh"

# (N, email, mail_pw) — chatgpt_pw not needed for hotmail
declare -a ACCTS=(
  "54 tonerpearlene10@hotmail.com 8FD9za4d6WFz"
  "55 trimmellbuonamici889@hotmail.com uo8KW5fGP"
  "56 mcjunkinzamborano12@hotmail.com W2Q6plyW66"
  "57 mandiasosbee782@hotmail.com Wm7Q1ve0V"
  "58 estradajavor0548@hotmail.com Syoq2WUm"
)

LOG_DIR=/tmp
SUMMARY=()

for line in "${ACCTS[@]}"; do
  read -r N EMAIL MAIL_PW <<<"$line"
  log="$LOG_DIR/acct-$N-batch.log"
  echo "════════════════════════════════════════════════════════════"
  echo "==> acct-$N $EMAIL  log=$log"
  echo "════════════════════════════════════════════════════════════"
  START=$(date +%s)
  if bash "$DRIVER" "$N" "$EMAIL" "$MAIL_PW" 2>&1 | tee "$log"; then
    DUR=$(( $(date +%s) - START ))
    SUMMARY+=("✅ acct-$N OK (${DUR}s)")
  else
    DUR=$(( $(date +%s) - START ))
    SUMMARY+=("❌ acct-$N FAIL (${DUR}s) — see $log")
    echo "BATCH STOP: acct-$N failed"
    break
  fi
done

echo ""
echo "════════════ BATCH SUMMARY ═══════════"
for s in "${SUMMARY[@]}"; do echo "$s"; done
