#!/usr/bin/env bash
# reset-pw-run.sh — reset a ChatGPT account password to $NEW_PW via email flow.
# Usage:  NEW_PW='chat!@#2026' bash /Data/chatgpt-auth/reset-pw-run.sh acct-N
# Requires: /tmp/chatgpt-reset-password.py on 188, /Data/chatgpt-auth/acct-N/.creds
set -eo pipefail
ACCT="${1:?usage: reset-pw-run.sh acct-N}"
NEW_PW="${NEW_PW:?set NEW_PW env}"
AUTH_DIR="/Data/chatgpt-auth/${ACCT}"
CREDS="${AUTH_DIR}/.creds"
[[ -f "$CREDS" ]] || { echo "❌ no creds $CREDS"; exit 1; }
source <(grep -E '^(email|mail_pw)=' "$CREDS" | sed 's/^/declare /')
MAIL_USER="${email:-}"; MAIL_PW="${mail_pw:-}"
[[ -n "$MAIL_USER" && -n "$MAIL_PW" ]] || { echo "❌ missing email/mail_pw"; exit 1; }

SS_DIR="/tmp/reset-ss-${ACCT}"
rm -rf "$SS_DIR"; mkdir -p "$SS_DIR"
printf '%s' "$MAIL_PW" > "/tmp/mail_pw_${ACCT}.txt"
printf '%s' "$NEW_PW"  > "/tmp/new_pw_${ACCT}.txt"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  reset-password ${ACCT}  (${MAIL_USER})  → new password set"
echo "╚══════════════════════════════════════════════════════════════╝"

docker run --rm \
  -v /tmp/chatgpt-reset-password.py:/work/script.py \
  -v "/tmp/mail_pw_${ACCT}.txt:/run/mail_pw.txt" \
  -v "/tmp/new_pw_${ACCT}.txt:/run/new_pw.txt" \
  -v "${SS_DIR}:/work/screenshots" \
  -e "MAIL_USER=${MAIL_USER}" \
  -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
  -e NEW_PASSWORD_FILE=/run/new_pw.txt \
  -e SCREENSHOT_DIR=/work/screenshots \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
  -e DISPLAY=:99 \
  mcr.microsoft.com/playwright/python:v1.60.0-noble \
  bash -c "Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && \
           pip install patchright -q --root-user-action=ignore 2>&1 | tail -1 && \
           python3 /work/script.py" \
  2>&1 | grep -v "^The XKEY\|^> Warning\|^Errors from\|^\[notice\]"

echo "  screenshots: ${SS_DIR}/"
