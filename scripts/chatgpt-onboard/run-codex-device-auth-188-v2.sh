#!/usr/bin/env bash
# run-codex-device-auth-188-v2.sh — 在 188 上跑 Codex Device OAuth
#
# 改动：用 playwright（Docker 自带）替换 patchright（版本不兼容）
# 脚本：codex-device-oauth-simple.py（已用 from playwright.sync_api）
#
# 用法：bash /tmp/run-codex-device-auth-188-v2.sh acct-115 [acct-116 ...]
set -euo pipefail

if [[ "$#" -lt 1 ]]; then
  echo "usage: $0 acct-51 [acct-52 ...]" >&2
  exit 2
fi

SCRIPT=/tmp/codex-device-oauth-simple.py
IMAGE=mcr.microsoft.com/playwright/python:v1.60.0-noble

[[ -s "$SCRIPT" ]] || { echo "FATAL: missing $SCRIPT (scp codex-device-oauth-simple.py to 188:/tmp/)" >&2; exit 3; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "FATAL: missing docker image $IMAGE" >&2; exit 4; }

printf "%-9s %-32s %s\n" "acct" "email" "result"
printf "%-9s %-32s %s\n" "--------" "-------------------------------" "----------------"

for acct in "$@"; do
  if [[ ! "$acct" =~ ^acct-[0-9]+$ ]]; then
    printf "%-9s %-32s %s\n" "$acct" "-" "INVALID_ACCT"
    continue
  fi

  dir="/Data/chatgpt-auth/$acct"
  creds="$dir/.creds"
  if [[ ! -s "$creds" ]]; then
    printf "%-9s %-32s %s\n" "$acct" "-" "MISSING_CREDS"
    continue
  fi

  email="$(grep -E '^email=' "$creds" | head -1 | cut -d= -f2- || true)"
  mail_pw="$(grep -E '^mail_pw=' "$creds" | head -1 | cut -d= -f2- || true)"
  chatgpt_pw="$(grep -E '^chatgpt_pw=' "$creds" | head -1 | cut -d= -f2- || true)"
  [[ -n "$mail_pw" ]] || mail_pw="$chatgpt_pw"
  if [[ -z "$email" || -z "$chatgpt_pw" ]]; then
    printf "%-9s %-32s %s\n" "$acct" "${email:-?}" "BAD_CREDS_FIELDS"
    continue
  fi

  secret_dir="$(mktemp -d "/tmp/codex-device-${acct}-XXXXXX")"
  ss_dir="/tmp/codex-device-ss-${acct}-$(date +%s)"
  out_json="/tmp/auth-${acct}.json"
  log="/tmp/codex-device-${acct}.log"
  mkdir -p "$ss_dir"
  chmod 700 "$secret_dir" "$ss_dir"
  printf "%s" "$chatgpt_pw" > "$secret_dir/chatgpt_pw.txt"
  printf "%s" "$mail_pw" > "$secret_dir/mail_pw.txt"
  chmod 600 "$secret_dir/chatgpt_pw.txt" "$secret_dir/mail_pw.txt"
  rm -f "$out_json"

  set +e
  output="$(
    docker run --rm \
      -v "$SCRIPT:/work/script.py:ro" \
      -v "$secret_dir/chatgpt_pw.txt:/run/chatgpt_pw.txt:ro" \
      -v "$secret_dir/mail_pw.txt:/run/mail_pw.txt:ro" \
      -v "$ss_dir:/work/screenshots" \
      -v /tmp:/work/out \
      -e "MAIL_USER=$email" \
      -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
      -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
      -e "AUTH_JSON_OUTPUT=/work/out/auth-${acct}.json" \
      -e SCREENSHOT_DIR=/work/screenshots \
      -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
      -e HEADLESS=1 \
      -e DISPLAY=:99 \
      "$IMAGE" \
      bash -c 'Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && python3 /work/script.py' \
      2>&1
  )"
  rc=$?
  set -e
  rm -rf "$secret_dir"

  printf "%s\n" "$output" > "$log"
  if [[ "$rc" -eq 0 && -s "$out_json" ]] && grep -q '"access_token"' "$out_json"; then
    result="AUTH_READY"
  else
    result="FAILED_RC_$rc"
  fi
  printf "%-9s %-32s %s\n" "$acct" "$email" "$result"
  printf "  log=%s screenshots=%s auth=%s\n" "$log" "$ss_dir" "$out_json"
done
