#!/usr/bin/env sh
# lark-cli-auto-bind.sh — bind lark-cli to OpenClaw on container start, bot identity, idempotent.
#
# Why this exists:
#   - lark-cli config (/data/.lark-cli/openclaw/) lives in container root, NOT in the user-data PVC,
#     so it is lost on every pod restart.
#   - Without bind, all lark-* skills that need bot identity (lark-im send, lark-event consume,
#     bot-as-app webhooks etc.) cannot work.
#   - Official `lark-cli config bind --source openclaw` reads /data/.openclaw/openclaw.json,
#     pulls app_id + app_secret, writes lark-cli config + keychain. Zero user interaction.
#
# Where to install:
#   Recommended: append to carher main repo's docker/entrypoint.sh before exec-ing the main process.
#   Alternative: scheduled in a per-her startup probe / sidecar.
#
# Behavior:
#   - Idempotent. If config.json already present AND auth status reports ok, skips.
#   - Logs to /data/.openclaw/logs/lark-bind.log (PVC, survives restart so you can audit history).
#   - Never blocks startup: failures only logged, never propagate (|| true at the end).
#   - identity=bot-only (safer default; user identity needs explicit admin-driven OAuth, see §5.7.2).
#
# Verified on: carher-2000 (her_id=2000) + carher-1000, 2026-05-16, see
#   docs/her-shared-skills-onboarding.md §5.7.1

set -u

LARK_CLI=/data/.openclaw/local/bin/lark-cli
OPENCLAW_JSON=/data/.openclaw/openclaw.json
LARK_CONFIG=/data/.lark-cli/openclaw/config.json
LOG_DIR=/data/.openclaw/logs
LOG=$LOG_DIR/lark-bind.log
TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
  printf '[%s] [lark-bind] %s\n' "$(TS)" "$*" >>"$LOG" 2>/dev/null
}

# 1. Pre-flight: lark-cli binary present?
if [ ! -x "$LARK_CLI" ]; then
  log "skip: $LARK_CLI not installed (npm install -g --prefix /data/.openclaw/local @larksuite/cli to fix)"
  exit 0
fi

# 2. Pre-flight: openclaw.json present and has appId?
if [ ! -f "$OPENCLAW_JSON" ]; then
  log "skip: $OPENCLAW_JSON missing (her not configured yet)"
  exit 0
fi

APP_ID=$(python3 -c "
import json, sys
try:
    d = json.load(open('$OPENCLAW_JSON'))
    print(d.get('channels', {}).get('feishu', {}).get('appId', ''))
except Exception as e:
    sys.stderr.write(f'parse error: {e}\n')
    sys.exit(1)
" 2>/dev/null)

if [ -z "$APP_ID" ]; then
  log "skip: appId not found in $OPENCLAW_JSON"
  exit 0
fi

# 3. Idempotency: if already bound to this appId AND auth status is ok, skip.
if [ -f "$LARK_CONFIG" ]; then
  BOUND_APP=$(python3 -c "
import json
try:
    d = json.load(open('$LARK_CONFIG'))
    print(d.get('apps', [{}])[0].get('appId', ''))
except Exception:
    pass
" 2>/dev/null)
  if [ "$BOUND_APP" = "$APP_ID" ]; then
    STATUS=$(OPENCLAW_HOME=/data "$LARK_CLI" auth status 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print('ok' if 'appId' in d else 'bad')
except Exception:
    print('bad')
" 2>/dev/null)
    if [ "$STATUS" = "ok" ]; then
      log "noop: already bound to $APP_ID, auth status ok"
      exit 0
    fi
  fi
fi

# 4. Bind. Capture both stdout and exit code.
log "binding lark-cli → OpenClaw, app_id=$APP_ID, identity=bot-only"
OUT=$(OPENCLAW_HOME=/data "$LARK_CLI" config bind \
  --source openclaw \
  --identity bot-only \
  --app-id "$APP_ID" 2>&1)
RC=$?

if [ $RC -ne 0 ]; then
  log "bind FAILED (rc=$RC): $OUT"
  exit 0  # never block container startup
fi

log "bind ok: $OUT"

# 5. Smoke check: auth status reports bot identity.
STATUS=$(OPENCLAW_HOME=/data "$LARK_CLI" auth status 2>&1)
log "post-bind auth status: $STATUS"

exit 0
