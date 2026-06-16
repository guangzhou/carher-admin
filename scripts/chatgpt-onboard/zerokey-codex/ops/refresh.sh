#!/usr/bin/env bash
# refresh.sh — re-capture a fresh chatgpt.com web session and reload the server.
#
# Cron-safe: reuses the persistent browser profile, so NO OTP is needed as long
# as the web login is still alive. If the capture fails (profile fully expired →
# OTP required, or CF block), the OLD session is kept untouched, a STALE flag is
# written, and (optionally) an alert webhook is pinged. The running server is
# never disrupted on failure.
#
# Layout (all under $BASE):
#   capture/zerokey-web-capture.py   capture script (baked into capture image)
#   secrets/{mail_pw,chatgpt_pw}.txt credentials
#   state/profile/                   persistent browser profile (reused)
#   state/out/                       capture scratch (zerokey-users.json, otp.txt)
#   state/users.json                 LIVE session mounted into the server
#   logs/refresh-*.log               per-run logs
#
# Env (optional):
#   ZK_ALERT_WEBHOOK   POST {text} here on failure (e.g. Feishu bot)
#   CAPTURE_TIMEOUT    hard timeout for the capture container (default 300s)
#   OTP_FILE_WAIT      seconds the script waits for a manual OTP (default 45;
#                      keep short for cron so it fails fast instead of hanging)
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="$BASE/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/refresh-$TS.log"
exec > >(tee -a "$LOG") 2>&1

MAIL_USER="${MAIL_USER:-kristine_free517@mail.com}"
ZK_USER="${ZK_USER:-kristine}"
CAPTURE_TIMEOUT="${CAPTURE_TIMEOUT:-300}"
OTP_FILE_WAIT="${OTP_FILE_WAIT:-45}"
SERVER_CONTAINER="zerokey-codex"
CAPTURE_IMAGE="zerokey-capture:latest"

OUT_DIR="$BASE/state/out"; mkdir -p "$OUT_DIR" "$BASE/state/profile"
OUT_JSON="$OUT_DIR/zerokey-users.json"
LIVE_JSON="$BASE/state/users.json"

log()  { echo "[$(date -u +%H:%M:%S)] $*"; }
alert() {
  local msg="$1"
  log "ALERT: $msg"
  : > "$BASE/state/REFRESH_STALE"
  echo "$TS $msg" >> "$BASE/state/REFRESH_STALE"
  if [[ -n "${ZK_ALERT_WEBHOOK:-}" ]]; then
    curl -fsS -m 10 -X POST "$ZK_ALERT_WEBHOOK" \
      -H 'Content-Type: application/json' \
      -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"[zerokey-188] $msg\"}}" >/dev/null 2>&1 || true
  fi
}

log "refresh start (user=$ZK_USER)"

rm -f "$OUT_JSON"

# Run capture (reuses profile → usually no OTP). Fail fast on OTP wait.
timeout "$CAPTURE_TIMEOUT" docker run --rm \
  -e MAIL_USER="$MAIL_USER" \
  -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
  -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
  -e OUT_JSON=/work/out/zerokey-users.json \
  -e ZK_USER="$ZK_USER" \
  -e PROFILE_DIR=/work/profile \
  -e SCREENSHOT_DIR=/work/screenshots \
  -e OTP_FILE=/work/out/otp.txt \
  -e OTP_FILE_WAIT="$OTP_FILE_WAIT" \
  -v "$BASE/state/profile:/work/profile" \
  -v "$OUT_DIR:/work/out" \
  -v "$OUT_DIR/screenshots:/work/screenshots" \
  -v "$BASE/secrets/mail_pw.txt:/run/mail_pw.txt:ro" \
  -v "$BASE/secrets/chatgpt_pw.txt:/run/chatgpt_pw.txt:ro" \
  "$CAPTURE_IMAGE"
rc=$?

if [[ $rc -ne 0 ]]; then
  alert "capture container exited $rc (timeout/login/CF). Live session kept."
  exit 1
fi

# Validate the captured JSON has the required browser headers.
node -e '
  const fs=require("fs");
  const f=process.argv[1], u=process.argv[2];
  const j=JSON.parse(fs.readFileSync(f,"utf8"));
  const pf=j?.chatgpt?.[u]?.parsedFetch;
  if(!pf||!pf.headers||!pf.body) { console.error("no parsedFetch"); process.exit(2); }
  const hk=Object.keys(pf.headers).map(s=>s.toLowerCase());
  for(const need of ["openai-sentinel-proof-token","cookie","authorization"]) {
    if(!hk.includes(need)) { console.error("missing header "+need); process.exit(3); }
  }
  console.error("captured OK ("+hk.length+" headers)");
' "$OUT_JSON" "$ZK_USER"
if [[ $? -ne 0 ]]; then
  alert "captured JSON invalid/incomplete. Live session kept."
  exit 1
fi

# Atomic swap of the live session, then reload the server.
cp "$OUT_JSON" "$LIVE_JSON.tmp" && mv "$LIVE_JSON.tmp" "$LIVE_JSON"
rm -f "$BASE/state/REFRESH_STALE"
log "live session updated → $LIVE_JSON"

if docker restart "$SERVER_CONTAINER" >/dev/null 2>&1; then
  log "server container restarted"
else
  alert "session refreshed but '$SERVER_CONTAINER' restart failed."
  exit 1
fi

# prune logs older than 14 days
find "$LOG_DIR" -name 'refresh-*.log' -mtime +14 -delete 2>/dev/null || true
log "refresh done OK"
