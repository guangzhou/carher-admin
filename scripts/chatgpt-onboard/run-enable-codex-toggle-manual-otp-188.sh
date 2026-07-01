#!/usr/bin/env bash
# Run on 188: 手动 OTP 变体 — 跟 run-enable-codex-toggle-188.sh 同框架, 但 OTP
# 不再从 mail.com 拉, 改成读 /tmp/codex-toggle-manual-otp-${acct}/manual_otp.txt.
#
# 用法 (188 上): bash /tmp/run-enable-codex-toggle-manual-otp-188.sh acct-79
# 然后另开终端在 188:
#   echo 123456 > /tmp/codex-toggle-manual-otp-acct-79/manual_otp.txt
#
# 见 [[feedback-chatgpt-otp-manual-fallback]].

set -euo pipefail

if [[ "$#" -lt 1 ]]; then
  echo "usage: $0 acct-29 [acct-30 ...]" >&2
  exit 2
fi

# 跟 run-enable-codex-toggle-188.sh 同框架, 但: 1) 多挂一个 manual-otp dir
# 2) 容器内 entrypoint 跑 manual-otp 变体脚本 (import 原脚本 monkey-patch)
ORIG_SCRIPT=/tmp/chatgpt-enable-codex-toggle.py
MANUAL_SCRIPT=/tmp/chatgpt-enable-codex-toggle-manual-otp.py
IMAGE=mcr.microsoft.com/playwright/python:v1.60.0-noble

[[ -s "$ORIG_SCRIPT" ]] || { echo "FATAL: missing $ORIG_SCRIPT (driver 应自动同步)" >&2; exit 3; }
[[ -s "$MANUAL_SCRIPT" ]] || { echo "FATAL: missing $MANUAL_SCRIPT (此 launcher 配套脚本)" >&2; exit 3; }
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

  email="$(grep -E '^email=' "$creds" | head -1 | cut -d= -f2- | sed "s/^'\\(.*\\)'$/\\1/; s/^\"\\(.*\\)\"$/\\1/" || true)"
  pw="$(grep -E '^chatgpt_pw=' "$creds" | head -1 | cut -d= -f2- | sed "s/^'\\(.*\\)'$/\\1/; s/^\"\\(.*\\)\"$/\\1/" || true)"
  mail_pw="$(grep -E '^mail_pw=' "$creds" | head -1 | cut -d= -f2- | sed "s/^'\\(.*\\)'$/\\1/; s/^\"\\(.*\\)\"$/\\1/" || true)"
  [[ -n "$mail_pw" ]] || mail_pw="$pw"
  if [[ -z "$email" || -z "$pw" ]]; then
    printf "%-9s %-32s %s\n" "$acct" "${email:-?}" "BAD_CREDS_FIELDS"
    continue
  fi

  secret_dir="$(mktemp -d "/tmp/codex-toggle-${acct}-XXXXXX")"
  ss_dir="/tmp/codex-toggle-ss-${acct}-$(date +%s)"
  # ⚠ 关键: manual-otp dir 固定路径 (无随机 suffix), 方便用户 echo 写 OTP
  manual_dir="/tmp/codex-toggle-manual-otp-${acct}"
  mkdir -p "$ss_dir" "$manual_dir"
  chmod 700 "$secret_dir" "$ss_dir"
  chmod 777 "$manual_dir"  # 用户可能用 non-root 写 OTP, 放开
  : > "$manual_dir/manual_otp.txt"  # 清空旧 OTP, 避免容器读到上轮残留
  chmod 666 "$manual_dir/manual_otp.txt"
  printf "%s" "$pw" > "$secret_dir/chatgpt_pw.txt"
  printf "%s" "$mail_pw" > "$secret_dir/mail_pw.txt"
  chmod 600 "$secret_dir/chatgpt_pw.txt" "$secret_dir/mail_pw.txt"

  echo ""
  echo "================================================================"
  echo "  📮 手动 OTP 模式 — 容器即将启动并等 OTP"
  echo "  当容器日志出现 '[manual-otp] waiting for OTP' 时,"
  echo "  另开 188 终端执行 (替换 123456 为真实 6 位码):"
  echo ""
  echo "      echo 123456 > $manual_dir/manual_otp.txt"
  echo ""
  echo "  容器内文件 = /run/manual_otp.txt (默认 15min timeout)"
  echo "================================================================"
  echo ""

  set +e
  output="$(
    docker run --rm \
      -v "$ORIG_SCRIPT:/work/orig.py:ro" \
      -v "$MANUAL_SCRIPT:/work/script.py:ro" \
      -v "$secret_dir/chatgpt_pw.txt:/run/chatgpt_pw.txt:ro" \
      -v "$secret_dir/mail_pw.txt:/run/mail_pw.txt:ro" \
      -v "$manual_dir/manual_otp.txt:/run/manual_otp.txt:ro" \
      -v "$ss_dir:/work/screenshots" \
      -e "CHATGPT_EMAIL=$email" \
      -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
      -e MAIL_PW_FILE=/run/mail_pw.txt \
      -e SCREENSHOT_DIR=/work/screenshots \
      -e "ACTION=${ACTION:-enable-codex-toggle}" \
      -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
      -e DISPLAY=:99 \
      -e MANUAL_OTP_FILE=/run/manual_otp.txt \
      -e "MANUAL_OTP_DIR=$manual_dir" \
      -e "MANUAL_OTP_TIMEOUT=${MANUAL_OTP_TIMEOUT:-900}" \
      "$IMAGE" \
      bash -c 'Xvfb :99 -screen 0 1440x1000x24 >/dev/null 2>&1 & sleep 1 && pip install "patchright==1.60.0" -q --root-user-action=ignore >/dev/null 2>&1 && python3 /work/script.py' \
      2>&1
  )"
  rc=$?
  set -e
  rm -rf "$secret_dir"
  # manual_dir 留着方便事后看 OTP 写入历史, 24h 后自然 /tmp 清

  result="$(printf "%s\n" "$output" | sed -n 's/^RESULT=//p' | tail -1)"
  [[ -n "$result" ]] || result="FAILED_RC_$rc"
  log="/tmp/codex-toggle-${acct}.log"
  printf "%s\n" "$output" > "$log"
  printf "%-9s %-32s %s\n" "$acct" "$email" "$result"
  if [[ "$result" != "ENABLED" ]]; then
    printf "  log=%s screenshots=%s manual_otp_dir=%s\n" "$log" "$ss_dir" "$manual_dir"
  fi
done
