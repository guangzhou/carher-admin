#!/bin/bash
# add-chatgpt-acct-198-mailcom-batch.sh — batch mail.com acct onboard
# 串行跑 add-chatgpt-acct-198-full.sh（4 参 N email mail_pw chatgpt_pw），
# 单 acct fail 立即 break 防雪崩。
#
# Usage:
#   ./add-chatgpt-acct-198-mailcom-batch.sh
set -eo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DRIVER="$REPO_DIR/scripts/add-chatgpt-acct-198-full.sh"

# (N, email, mail_pw, chatgpt_pw)
declare -a ACCTS=(
  '69 yocwtcwvgkpke@mail.com 7uyPYn94ETe_ udgknzj2fS!q'
  '70 nbfyljzbenxkp@mail.com t%gL-5pK8iV_ Azhp5@qb2dfk'
  '71 lrlairmzmzmwr@mail.com ghMQv=3pSdq# hA5kh_t1oq1k'
  '72 czstusjhjqzcd@mail.com VSsnrH7V+Zs^ 89Kco@drehh3'
  '73 jqycoegbcxoer@mail.com Ur&rNrJ2@rdm jnrZou2pb.4s'
  '74 xxinsjoqyfzbz@mail.com Z=f-pU6UbH_f fdv62owf_eeG'
  '75 eahtcsnpydtnw@mail.com A6cs$m5xPPxF evy.m4Bbzsg3'
  '76 vjufwzchyrvlo@mail.com mZBMnB*28chF huCe63e.vql2'
  '77 hdwxfoihimzqd@mail.com R6@CVEVSxr%7 6tho6ks!Msqh'
  '78 umemylycuvpct@mail.com bb&$9qX$x@mR pd89.Pcwm6h1'
)

LOG_DIR=/tmp
SUMMARY=()

for line in "${ACCTS[@]}"; do
  read -r N EMAIL MAIL_PW CHATGPT_PW <<<"$line"
  log="$LOG_DIR/acct-$N-batch.log"
  echo "════════════════════════════════════════════════════════════"
  echo "==> acct-$N $EMAIL  log=$log"
  echo "════════════════════════════════════════════════════════════"
  START=$(date +%s)
  if bash "$DRIVER" "$N" "$EMAIL" "$MAIL_PW" "$CHATGPT_PW" 2>&1 | tee "$log"; then
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
