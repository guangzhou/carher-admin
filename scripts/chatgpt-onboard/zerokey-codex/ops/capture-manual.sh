#!/usr/bin/env bash
# capture-manual.sh — interactive (re)capture when the web login has fully
# expired and an OTP is required (cron refresh.sh can't do this unattended).
#
# It runs the same capture container but waits up to 10 min for you to drop the
# 6-digit email OTP into state/out/otp.txt, e.g.:
#     echo 123456 > ~/zerokey-codex/state/out/otp.txt
# On success it swaps the live session and restarts the server, same as refresh.
set -uo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$BASE/state/out"
rm -f "$BASE/state/out/otp.txt"
echo "OTP drop file: $BASE/state/out/otp.txt   (echo <code> > that file)"
CAPTURE_TIMEOUT="${CAPTURE_TIMEOUT:-720}" OTP_FILE_WAIT="${OTP_FILE_WAIT:-600}" \
  exec "$BASE/ops/refresh.sh"
