#!/usr/bin/env bash
# 批量改 BuilderId 密码 + 2FA（5 个号串行）
# 每个号：1) change_builderid_pwd_2fa.py 改密码 2) aws_mfa_rotate.py 改 2FA
set -euo pipefail

VENV="$HOME/.local/kiro-auto/venv/bin/python"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PWD_SCRIPT="$SCRIPT_DIR/change_builderid_pwd_2fa.py"
MFA_SCRIPT="/Users/Liuguoxian/codes/carher-admin/.claude/skills/kiro-account-manager-ops/scripts/aws_mfa_rotate.py"
MFA_SCRIPT_DIR="$(dirname "$MFA_SCRIPT")"
RESULTS_DIR="/tmp/kiro-batch-pwd-2fa-results"
mkdir -p "$RESULTS_DIR"

# 账号列表来源（2026-10-19 起）：$1 = creds ndjson，每行
#   {"email":..,"pw":<AWS密码>,"alt_pw":..,"totp":<现有2FA seed>,"email_pw":<邮箱密码>}
# 与 kiro_batch.sh / aws_mfa_rotate.sh 同一种 creds 格式（多认一个 email_pw 字段）。
# 🔴 别再把凭据硬编码进本文件（它在 git 里）。ndjson 放 work-kiro-*/ 或 /tmp，chmod 600，跑完删。
CREDS="${1:?需要 creds ndjson 路径（见文件头字段说明）}"
[ -f "${CREDS}" ] || { echo "creds 不存在: ${CREDS}"; exit 1; }
[ -x "${VENV}" ] || { echo "venv 不存在: ${VENV}（见 SKILL.md 建 venv 一节）"; exit 1; }

ACCOUNTS=()
while IFS= read -r line; do
  [ -z "${line}" ] && continue
  # NUL 分隔逐字段读出，避免密码里的引号/元字符被 shell 吃掉（同 kiro_batch.sh）
  { IFS= read -r -d '' _e; IFS= read -r -d '' _ep
    IFS= read -r -d '' _ap; IFS= read -r -d '' _sd; } < <(
    "${VENV}" -I -c '
import json, sys
r = json.loads(sys.argv[1])
for k in ("email", "email_pw", "pw", "totp"):
    sys.stdout.write((r.get(k) or "") + "\0")
' "${line}"
  )
  [ -z "${_e}" ] && continue
  ACCOUNTS+=("${_e}|${_ep}|${_ap}|${_sd}")
done < "${CREDS}"
echo "=== 开工：${#ACCOUNTS[@]} 个号（来源 ${CREDS}）==="

SUMMARY="$RESULTS_DIR/summary.json"
echo '[' > "$SUMMARY"
FIRST=1

for entry in "${ACCOUNTS[@]}"; do
  IFS='|' read -r EMAIL EMAIL_PWD AWS_PWD MFA_SECRET <<< "$entry"
  SAFE=$(echo "$EMAIL" | cut -d@ -f1 | tr '.' '_')
  echo ""
  echo "================================================================"
  echo "[$SAFE] Starting pwd+2fa change for $EMAIL"
  echo "================================================================"

  NEW_PWD=""
  NEW_2FA=""
  PWD_OK="false"
  TFA_OK="false"

  # Step 1: Change password (DO_PWD=1 DO_2FA=0)
  echo "[$SAFE] Step 1: Change password..."
  cd /tmp
  set +e
  EMAIL="$EMAIL" OLD_PWD="$AWS_PWD" MFA_SECRET="$MFA_SECRET" BINUS_PW="$EMAIL_PWD" \
    DO_PWD=1 DO_2FA=0 CDP_PORT=9222 \
    "$VENV" -I "$PWD_SCRIPT" 2>&1 | tail -30
  PWD_EXIT=$?
  set -e

  PWD_RESULT="/tmp/kiro-pwd-2fa-${SAFE}.json"
  if [ -f "$PWD_RESULT" ]; then
    NEW_PWD=$("$VENV" -I -c "import json; d=json.load(open('$PWD_RESULT')); print(d.get('new_pwd',''))")
    PWD_OK_VAL=$("$VENV" -I -c "import json; d=json.load(open('$PWD_RESULT')); print(d.get('pwd_ok',''))")
    if [ "$PWD_OK_VAL" = "True" ]; then
      PWD_OK="true"
      echo "[$SAFE] Password changed → $NEW_PWD"
    else
      echo "[$SAFE] WARN: pwd_ok=$PWD_OK_VAL, exit=$PWD_EXIT"
      NEW_PWD="$AWS_PWD"
    fi
  else
    echo "[$SAFE] WARN: no result file, using old password"
    NEW_PWD="$AWS_PWD"
  fi

  # Step 2: Change 2FA (aws_mfa_rotate.py with new/old password)
  echo "[$SAFE] Step 2: Change 2FA..."
  sleep 3
  set +e
  # 🔴 DELETE_OLD=1（delete-before-register）：10-18 四号实证，避免「注册设备」触发重认证 SPA 白屏。
  #    BINUS_PW 必传——删旧 MFA 后 AWS 可能登出，重登时已无 TOTP，要走邮箱验证码。
  (cd "$MFA_SCRIPT_DIR" && TARGET_EMAIL="$EMAIL" PW="$NEW_PWD" ALT_PW="$AWS_PWD" TOTP_SEED="$MFA_SECRET" \
    DELETE_OLD="${DELETE_OLD:-1}" BINUS_PW="$EMAIL_PWD" PYTHONPATH="$MFA_SCRIPT_DIR" \
    "$VENV" "$MFA_SCRIPT" 2>&1) | tail -30
  TFA_EXIT=${PIPESTATUS[0]}
  set -e

  MFA_PREFIX=$(echo "$EMAIL" | cut -d@ -f1)
  MFA_RESULT=$(ls -t /tmp/aws-mfa-rotate-${MFA_PREFIX}-*.json 2>/dev/null | head -1)
  if [ -n "$MFA_RESULT" ] && [ -f "$MFA_RESULT" ]; then
    NEW_2FA=$("$VENV" -I -c "import json; d=json.load(open('$MFA_RESULT')); print(d.get('new_seed',''))")
    TFA_OK_VAL=$("$VENV" -I -c "import json; d=json.load(open('$MFA_RESULT')); print(d.get('ok',''))")
    if [ "$TFA_OK_VAL" = "True" ]; then
      TFA_OK="true"
      echo "[$SAFE] 2FA rotated → $NEW_2FA"
    else
      echo "[$SAFE] WARN: 2fa ok=$TFA_OK_VAL, exit=$TFA_EXIT"
    fi
  else
    echo "[$SAFE] WARN: no MFA result file"
  fi

  # Append to summary
  if [ "$FIRST" = "1" ]; then FIRST=0; else echo ',' >> "$SUMMARY"; fi
  cat >> "$SUMMARY" << ENDJSON
  {
    "email": "$EMAIL",
    "old_pwd": "$AWS_PWD",
    "new_pwd": "$NEW_PWD",
    "pwd_ok": $PWD_OK,
    "old_2fa": "$MFA_SECRET",
    "new_2fa": "$NEW_2FA",
    "2fa_ok": $TFA_OK
  }
ENDJSON

  echo "[$SAFE] DONE: pwd_ok=$PWD_OK 2fa_ok=$TFA_OK"
  echo ""

  # cleanup chrome between accounts
  pkill -9 -f 'remote-debugging-port=922' 2>/dev/null || true
  sleep 3
done

echo ']' >> "$SUMMARY"
chmod 600 "$SUMMARY"

echo ""
echo "================================================================"
echo "BATCH COMPLETE — summary: $SUMMARY"
echo "================================================================"
cat "$SUMMARY"
