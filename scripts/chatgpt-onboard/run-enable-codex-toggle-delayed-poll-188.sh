#!/usr/bin/env bash
# Run on 188: 诊断变体 — fetch_mailcom_otp 入口先 sleep FETCH_PRE_SLEEP 秒再走原 mail.com 抓取.
# 用途: 验证假设"mail.com 邮件 1-2min 才落 inbox"是 acct-79 自动路径失败的真因.
#
# 用法 (188 上):
#   FETCH_PRE_SLEEP=120 bash /tmp/run-enable-codex-toggle-delayed-poll-188.sh acct-79
#
# 跟 run-enable-codex-toggle-manual-otp-188.sh 同框架, 区别:
#   - 多挂 /tmp/chatgpt-enable-codex-toggle-delayed-poll.py 作 /work/script.py
#   - 多传 FETCH_PRE_SLEEP env
#   - 不挂 manual_otp 文件 (这次走原 mail.com 自动抓, 只是延后 2 min)

set -euo pipefail

if [[ "$#" -lt 1 ]]; then
  echo "usage: $0 acct-29 [acct-30 ...]" >&2
  exit 2
fi

ORIG_SCRIPT=/tmp/chatgpt-enable-codex-toggle.py
DELAY_SCRIPT=/tmp/chatgpt-enable-codex-toggle-delayed-poll.py
IMAGE=mcr.microsoft.com/playwright/python:v1.60.0-noble

[[ -s "$ORIG_SCRIPT" ]] || { echo "FATAL: missing $ORIG_SCRIPT" >&2; exit 3; }
[[ -s "$DELAY_SCRIPT" ]] || { echo "FATAL: missing $DELAY_SCRIPT" >&2; exit 3; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "FATAL: missing docker image $IMAGE" >&2; exit 4; }

FETCH_PRE_SLEEP="${FETCH_PRE_SLEEP:-120}"

printf "%-9s %-32s %-10s %s\n" "acct" "email" "pre_sleep" "result"
printf "%-9s %-32s %-10s %s\n" "--------" "-------------------------------" "---------" "----------------"

for acct in "$@"; do
  if [[ ! "$acct" =~ ^acct-[0-9]+$ ]]; then
    printf "%-9s %-32s %-10s %s\n" "$acct" "-" "-" "INVALID_ACCT"
    continue
  fi

  dir="/Data/chatgpt-auth/$acct"
  creds="$dir/.creds"
  if [[ ! -s "$creds" ]]; then
    printf "%-9s %-32s %-10s %s\n" "$acct" "-" "-" "MISSING_CREDS"
    continue
  fi

  email="$(grep -E '^email=' "$creds" | head -1 | cut -d= -f2- | sed "s/^'\\(.*\\)'$/\\1/; s/^\"\\(.*\\)\"$/\\1/" || true)"
  pw="$(grep -E '^chatgpt_pw=' "$creds" | head -1 | cut -d= -f2- | sed "s/^'\\(.*\\)'$/\\1/; s/^\"\\(.*\\)\"$/\\1/" || true)"
  mail_pw="$(grep -E '^mail_pw=' "$creds" | head -1 | cut -d= -f2- | sed "s/^'\\(.*\\)'$/\\1/; s/^\"\\(.*\\)\"$/\\1/" || true)"
  [[ -n "$mail_pw" ]] || mail_pw="$pw"
  if [[ -z "$email" || -z "$pw" ]]; then
    printf "%-9s %-32s %-10s %s\n" "$acct" "${email:-?}" "-" "BAD_CREDS_FIELDS"
    continue
  fi

  secret_dir="$(mktemp -d "/tmp/codex-toggle-${acct}-XXXXXX")"
  ss_dir="/tmp/codex-toggle-ss-${acct}-$(date +%s)"
  mkdir -p "$ss_dir"
  chmod 700 "$secret_dir" "$ss_dir"
  printf "%s" "$pw" > "$secret_dir/chatgpt_pw.txt"
  printf "%s" "$mail_pw" > "$secret_dir/mail_pw.txt"
  chmod 600 "$secret_dir/chatgpt_pw.txt" "$secret_dir/mail_pw.txt"

  echo ""
  echo "================================================================"
  echo "  🧪 诊断模式: 容器内首次 fetch_mailcom_otp 前 sleep ${FETCH_PRE_SLEEP}s"
  echo "     screenshots → $ss_dir"
  echo "================================================================"
  echo ""

  set +e
  output="$(
    docker run --rm \
      -v "$ORIG_SCRIPT:/work/orig.py:ro" \
      -v "$DELAY_SCRIPT:/work/script.py:ro" \
      -v "$secret_dir/chatgpt_pw.txt:/run/chatgpt_pw.txt:ro" \
      -v "$secret_dir/mail_pw.txt:/run/mail_pw.txt:ro" \
      -v "$ss_dir:/work/screenshots" \
      -e "CHATGPT_EMAIL=$email" \
      -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
      -e MAIL_PW_FILE=/run/mail_pw.txt \
      -e SCREENSHOT_DIR=/work/screenshots \
      -e "ACTION=${ACTION:-enable-codex-toggle}" \
      -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
      -e DISPLAY=:99 \
      -e "FETCH_PRE_SLEEP=$FETCH_PRE_SLEEP" \
      "$IMAGE" \
      bash -c 'Xvfb :99 -screen 0 1440x1000x24 >/dev/null 2>&1 & sleep 1 && pip install "patchright==1.60.0" -q --root-user-action=ignore >/dev/null 2>&1 && python3 /work/script.py' \
      2>&1
  )"
  rc=$?
  set -e
  rm -rf "$secret_dir"

  result="$(printf "%s\n" "$output" | sed -n 's/^RESULT=//p' | tail -1)"
  [[ -n "$result" ]] || result="FAILED_RC_$rc"
  log="/tmp/codex-toggle-delayed-${acct}.log"
  printf "%s\n" "$output" > "$log"
  printf "%-9s %-32s %-10s %s\n" "$acct" "$email" "$FETCH_PRE_SLEEP" "$result"
  if [[ "$result" != "ENABLED" ]]; then
    printf "  log=%s screenshots=%s\n" "$log" "$ss_dir"
  fi
done
