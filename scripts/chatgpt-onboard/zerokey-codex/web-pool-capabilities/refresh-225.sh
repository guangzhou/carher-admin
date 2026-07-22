#!/bin/bash
# zk-refresh-225.sh — refresh web sessions for 225 zerokey-web pods.
# Re-capture on 188 (reuse persisted profile; FORCE_LOGIN fallback) -> seed 225 hostPath -> rollout restart.
# Randomized jitter (0-3h) + per-acct gaps to avoid a detectable fixed cadence. Lock prevents overlap.
exec 9>/Data/zkcaps/.refresh.lock
flock -n 9 || { echo "$(date +%F_%H:%M) another refresh running, skip" >> /Data/zkcaps/refresh.log; exit 0; }

ACCTS_FILE=/Data/zkcaps/refresh-accts.txt
ACCTS=$( [ -f "$ACCTS_FILE" ] && cat "$ACCTS_FILE" || echo "87 88 89 90 91 92 93 94 95 96 97 98 99" )
LOG=/Data/zkcaps/refresh.log
# 188->198 key-based SSH + passwordless `sudo k3s kubectl` (no password in file).
# 225 is NOT reached directly (its SSH password rotates); seeding goes via 198's
# kubectl-cp into the running zero-N pod (whose /app/temp IS the 225 hostPath).
P198="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 cltx@10.68.13.198"

sleep $(( RANDOM % 10800 ))   # 0-3h random start -> effective 3-6h cadence with the 3h cron base

for N in $ACCTS; do
  ZK=acct$N
  W=/Data/zkcaps/zkcap-$N
  CREDS=/Data/chatgpt-auth/acct-$N/.creds
  [ -f "$CREDS" ] || { echo "$(date +%F_%H:%M) zero-$N no-creds skip" >> $LOG; continue; }
  mkdir -p "$W/out" "$W/screenshots" "$W/profile" 2>/dev/null
  awk -F= '/^mail_pw=/{sub(/^mail_pw=/,"");print}' "$CREDS" > "$W/mail_pw"
  awk -F= '/^chatgpt_pw=/{sub(/^chatgpt_pw=/,"");print}' "$CREDS" > "$W/chatgpt_pw"
  MAILU=$(awk -F= '/^email=/{sub(/^email=/,"");print}' "$CREDS")
  run_cap(){
    docker rm -f zkref-$N >/dev/null 2>&1
    docker run --rm --name zkref-$N --network host \
      -e MAIL_USER="$MAILU" -e MAIL_LOGIN_PW_FILE=/state/mail_pw -e CHATGPT_PW_FILE=/state/chatgpt_pw \
      -e OUT_JSON=/state/out/zerokey-users.json -e ZK_USER="$ZK" \
      -e SCREENSHOT_DIR=/state/screenshots -e PROFILE_DIR=/state/profile \
      $1 -e LOGIN_MODE=otp -e OTP_AUTO_ONLY=1 -e OTP_AUTO_MAX=180 -e OTP_FILE_WAIT=0 \
      -v "$W":/state zerokey-capture:latest >/dev/null 2>&1
  }
  rm -f "$W/out/zerokey-users.json"
  run_cap ""                          # try persisted-session reuse (fast, no OTP)
  [ -f "$W/out/zerokey-users.json" ] || run_cap "-e FORCE_LOGIN=1"   # fallback: full OTP login
  if [ -f "$W/out/zerokey-users.json" ]; then
    # Seed via kubectl-cp into the running pod (its /app/temp IS the 225 hostPath).
    # 225 SSH password rotates -> sshpass is dead; 188->198 has passwordless sudo k3s kubectl.
    base64 -w0 "$W/out/zerokey-users.json" | $P198 "base64 -d > /tmp/zkref-$N.json" 2>/dev/null
    if $P198 "POD=\$(sudo k3s kubectl -n litellm-product get pod -l app=zero-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); [ -n \"\$POD\" ] && sudo k3s kubectl -n litellm-product cp /tmp/zkref-$N.json \$POD:/app/temp/users.json && sudo k3s kubectl -n litellm-product rollout restart deploy/zero-$N" >/dev/null 2>&1; then
      echo "$(date +%F_%H:%M) zero-$N refreshed OK (kubectl-cp)" >> $LOG
    else
      echo "$(date +%F_%H:%M) zero-$N seed FAIL (kubectl)" >> $LOG
    fi
  else
    echo "$(date +%F_%H:%M) zero-$N refresh FAIL (mail/otp)" >> $LOG
  fi
  sleep $(( RANDOM % 150 + 30 ))      # random gap between accts
done
echo "$(date +%F_%H:%M) refresh cycle done" >> $LOG
