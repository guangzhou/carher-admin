#!/usr/bin/env bash
# re-oauth.sh — One-shot re-OAuth a ChatGPT Pro account on 188
#
# Usage:
#   bash /Data/chatgpt-auth/re-oauth.sh acct-N
#
# Requires:
#   - /tmp/chatgpt-litellm-oauth.py  (patchright OAuth script)
#   - /tmp/chatgpt-acct-status.py    (status verification)
#   - /Data/chatgpt-auth/acct-N/.creds  with:
#         email=xxx@mail.com
#         mail_pw=<字段A webmail password>
#         chatgpt_pw=<字段B ChatGPT password>
#     (fallback: MAIL_USER / MAIL_PW / CHATGPT_PW env vars)
#
# What it does (~3-5min total):
#   1. read creds, prep secret files in /tmp
#   2. docker run patchright OAuth → /tmp/auth-<acct>.json
#   3. docker cp into litellm-chatgpt-N:/chatgpt-auth/auth.json
#   4. docker compose restart litellm-chatgpt-N
#   5. wait 10s, grep logs for "200 OK"
#   6. run chatgpt-acct-status.py | grep acct-N
#
# Exit code 0 = HEALTHY, non-zero = something failed

set -eo pipefail

ACCT="${1:-}"
if [[ -z "$ACCT" ]]; then
    echo "Usage: $0 <acct-N>  (e.g. acct-1)"
    exit 1
fi

if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
    echo "❌ invalid acct name: $ACCT (expected acct-N)"
    exit 1
fi

N="${ACCT#acct-}"
PORT=$((4000 + N))
CONTAINER="litellm-chatgpt-${N}"
AUTH_DIR="/Data/chatgpt-auth/${ACCT}"
CREDS_FILE="${AUTH_DIR}/.creds"

# ── 1. Read credentials ─────────────────────────────────────────────────
MAIL_USER=""
MAIL_PW=""
CHATGPT_PW=""

if [[ -f "$CREDS_FILE" ]]; then
    # shellcheck disable=SC1090
    source <(grep -E '^(email|mail_pw|chatgpt_pw)=' "$CREDS_FILE" | sed 's/^/declare /')
    MAIL_USER="${email:-}"
    MAIL_PW="${mail_pw:-}"
    CHATGPT_PW="${chatgpt_pw:-}"
    echo "  using credentials from $CREDS_FILE"
fi

# env override / fallback
MAIL_USER="${MAIL_USER_ENV:-${MAIL_USER:-${MAIL_USER_ENV:-}}}"
[[ -n "${MAIL_USER_OVERRIDE:-}" ]] && MAIL_USER="$MAIL_USER_OVERRIDE"
[[ -n "${MAIL_PW_OVERRIDE:-}"   ]] && MAIL_PW="$MAIL_PW_OVERRIDE"
[[ -n "${CHATGPT_PW_OVERRIDE:-}" ]] && CHATGPT_PW="$CHATGPT_PW_OVERRIDE"

# validate
if [[ -z "$MAIL_USER" || -z "$MAIL_PW" || -z "$CHATGPT_PW" ]]; then
    echo "❌ missing credentials. Provide either:"
    echo "   (a) $CREDS_FILE with email=, mail_pw=, chatgpt_pw="
    echo "   (b) MAIL_USER_OVERRIDE / MAIL_PW_OVERRIDE / CHATGPT_PW_OVERRIDE env vars"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  re-OAuth $ACCT  ($MAIL_USER → $CONTAINER on port $PORT)"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── 2. Prep secret files ────────────────────────────────────────────────
SECRET_DIR=$(mktemp -d "/tmp/reoauth-${ACCT}-XXXXXX")
trap 'rm -rf "$SECRET_DIR"' EXIT
MAIL_PW_FILE="${SECRET_DIR}/mail_pw.txt"
CHATGPT_PW_FILE="${SECRET_DIR}/chatgpt_pw.txt"
printf '%s' "$MAIL_PW"     > "$MAIL_PW_FILE"
printf '%s' "$CHATGPT_PW"  > "$CHATGPT_PW_FILE"
chmod 600 "$MAIL_PW_FILE" "$CHATGPT_PW_FILE"

SCREENSHOT_DIR="/tmp/screenshots-${ACCT}"
OUT_FILE="/tmp/auth-${ACCT}.json"
rm -rf "$SCREENSHOT_DIR" "$OUT_FILE"
mkdir -p "$SCREENSHOT_DIR"

# ── 3. Sanity checks ────────────────────────────────────────────────────
if [[ ! -f /tmp/chatgpt-litellm-oauth.py ]]; then
    echo "❌ /tmp/chatgpt-litellm-oauth.py not found — re-upload from carher-admin/scripts/chatgpt-onboard/"
    exit 1
fi
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "❌ container $CONTAINER not found"
    exit 1
fi

# ── 4. Run OAuth ────────────────────────────────────────────────────────
echo ""
echo "[1/4] Running patchright OAuth (~3-5min)..."
docker run --rm \
  -v /tmp/chatgpt-litellm-oauth.py:/work/chatgpt-litellm-oauth.py:ro \
  -v "${MAIL_PW_FILE}:/run/mail_pw.txt:ro" \
  -v "${CHATGPT_PW_FILE}:/run/chatgpt_pw.txt:ro" \
  -v "${SCREENSHOT_DIR}:/work/screenshots" \
  -v /tmp:/work/out \
  -e "MAIL_USER=${MAIL_USER}" \
  -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \
  -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \
  -e "AUTH_JSON_OUTPUT=/work/out/auth-${ACCT}.json" \
  -e SCREENSHOT_DIR=/work/screenshots \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
  -e DISPLAY=:99 \
  mcr.microsoft.com/playwright/python:v1.60.0-noble \
  bash -c "Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && \
           pip install patchright -q --root-user-action=ignore 2>&1 | tail -1 && \
           python3 /work/chatgpt-litellm-oauth.py" \
  2>&1 | grep -v "^The XKEY\|^> Warning\|^Errors from\|^\[notice\]" \
       | tail -80

if [[ ! -s "$OUT_FILE" ]]; then
    echo ""
    echo "❌ OAuth failed — no auth.json produced. Check screenshots:"
    echo "   $SCREENSHOT_DIR/"
    ls -la "$SCREENSHOT_DIR/" | tail -10
    exit 1
fi

# verify auth.json shape
python3 -c "
import sys, json
d = json.load(open('$OUT_FILE'))
required = ['access_token', 'refresh_token', 'id_token', 'expires_at', 'account_id']
missing = [k for k in required if not d.get(k)]
if missing:
    print(f'❌ auth.json missing fields: {missing}'); sys.exit(1)
print(f'  ✅ auth.json valid: account_id={d[\"account_id\"]}, expires_at={d[\"expires_at\"]}')
"

# ── 5. Deploy ──────────────────────────────────────────────────────────
echo ""
echo "[2/4] Deploying auth.json into container..."
docker cp "$OUT_FILE" "${CONTAINER}:/chatgpt-auth/auth.json"

echo ""
echo "[3/4] Restarting $CONTAINER..."
( cd /Data/chatgpt-auth && docker compose restart "$CONTAINER" )
sleep 10

# ── 6. Verify ──────────────────────────────────────────────────────────
echo ""
echo "[4/4] Verifying..."

# 6a) log check
LOG_LINES=$(docker logs "$CONTAINER" --tail 30 2>&1 || true)
if echo "$LOG_LINES" | grep -qE 'ageneric_api_call.*200 OK|"POST /responses HTTP/1.1" 200'; then
    echo "  ✅ container logs show 200 OK"
elif echo "$LOG_LINES" | grep -qE 'token_invalidated|401 Unauthorized'; then
    echo "  ⚠️  container still showing 401 — auth.json may not have taken effect"
    echo "$LOG_LINES" | tail -10
    exit 2
else
    echo "  ⓘ  no decisive 200 OK in tail; checking /codex/usage instead..."
fi

# 6b) chatgpt-acct-status.py
if [[ -f /tmp/chatgpt-acct-status.py ]]; then
    STATUS_LINE=$(python3 /tmp/chatgpt-acct-status.py 2>/dev/null | grep -E "^${ACCT}\b" | tail -1)
    echo ""
    echo "  status: $STATUS_LINE"
    if echo "$STATUS_LINE" | grep -q 'HEALTHY'; then
        echo ""
        echo "🎉 $ACCT re-OAuth SUCCESS — HEALTHY"
        # cache auth.json for re-deploy convenience
        cp "$OUT_FILE" "${AUTH_DIR}/.last-auth.json" 2>/dev/null || true
        exit 0
    elif echo "$STATUS_LINE" | grep -qE 'TOKEN-ERR|ERROR'; then
        echo ""
        echo "❌ status check shows $ACCT still TOKEN-ERR — something else is wrong"
        exit 3
    fi
else
    echo "  ⓘ  /tmp/chatgpt-acct-status.py not on 188 — skip full status check"
fi

echo ""
echo "🎉 $ACCT re-OAuth done (status verification inconclusive — manually check)"
