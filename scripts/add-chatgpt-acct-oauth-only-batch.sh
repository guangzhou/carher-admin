#!/bin/bash
# add-chatgpt-acct-oauth-only-batch.sh — 10 acct 串行 188 OAuth-only
# 后续: ./scripts/aliyun-batch-add-accts.sh 69 70 71 72 73 74 75 76 77 78
set -eo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DRIVER="$REPO_DIR/scripts/add-chatgpt-acct-oauth-only.sh"

declare -a ACCTS=(
  "69 yocwtcwvgkpke@mail.com demequ1eXd5d udgknzj2fS!q"
  "70 nbfyljzbenxkp@mail.com 4rw1kd5tbr1F Azhp5@qb2dfk"
  "71 lrlairmzmzmwr@mail.com yyf9gc7lnsM6 hA5kh_t1oq1k"
  "72 czstusjhjqzcd@mail.com voxwd4Zb8ot8 89Kco@drehh3"
  "73 jqycoegbcxoer@mail.com go1aE0jfqfuh jnrZou2pb.4s"
  "74 xxinsjoqyfzbz@mail.com dz2syUcekdfk fdv62owf_eeG"
  "75 eahtcsnpydtnw@mail.com 4dzar28heD3u evy.m4Bbzsg3"
  "76 vjufwzchyrvlo@mail.com 9x58m8w6c1hT huCe63e.vql2"
  "77 hdwxfoihimzqd@mail.com k51kMdhvsv6z 6tho6ks!Msqh"
  "78 umemylycuvpct@mail.com vbktx0Ar76pe pd89.Pcwm6h1"
)

LOG_DIR=/tmp
SUMMARY=()

for line in "${ACCTS[@]}"; do
  read -r N EMAIL MAIL_PW CHATGPT_PW <<<"$line"
  log="$LOG_DIR/acct-$N-oauth.log"
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
    echo "BATCH CONTINUE: acct-$N failed (其他 acct 不受影响, 继续)"
    # OAuth 失败的 acct 后续不会进 aliyun (aliyun script 自己 precheck auth.json)
  fi
done

echo ""
echo "════════════ BATCH SUMMARY ═══════════"
for s in "${SUMMARY[@]}"; do echo "$s"; done
echo ""
echo "下一步: 看哪些 acct-N OK 后, 跑"
echo "  ./scripts/aliyun-batch-add-accts.sh <成功的 N list>"
