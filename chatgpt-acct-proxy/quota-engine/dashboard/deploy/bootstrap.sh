#!/usr/bin/env bash
# bootstrap.sh — one-time setup on 188. Runs entirely as cltx, no sudo.
#
# Usage:
#   SSH_PASS=... bootstrap.sh          # provision .env if missing
#   SSH_PASS=... bootstrap.sh --rotate # force-regenerate the Bearer token
#
# Creates:
#   /home/cltx/quota-engine-web/               (payload dir)
#   /home/cltx/quota-engine-web/.env           (Bearer token, mode 600)
#   /home/cltx/quota-engine-web/logs/          (stdout/stderr)

set -euo pipefail
HOST="cltx@10.68.13.188"
SSH_OPTS="-o StrictHostKeyChecking=no -o LogLevel=ERROR"
: "${SSH_PASS:?SSH_PASS env var required}"

ROTATE=0
if [ "${1:-}" = "--rotate" ]; then
    ROTATE=1
fi

if ! command -v sshpass >/dev/null 2>&1; then
    echo "!! sshpass required — 'brew install sshpass'"
    exit 1
fi
export SSHPASS="$SSH_PASS"

sshpass -e ssh $SSH_OPTS "$HOST" "ROTATE=$ROTATE bash -s" <<'REMOTE'
set -euo pipefail
ROOT=/home/cltx/quota-engine-web
mkdir -p "$ROOT" "$ROOT/logs" "$ROOT/api" "$ROOT/web/dist"

need_new_token=0
if [ ! -f "$ROOT/.env" ]; then
    need_new_token=1
elif [ "${ROTATE:-0}" = "1" ]; then
    cp "$ROOT/.env" "$ROOT/.env.bak.$(date +%Y%m%d-%H%M%S)"
    need_new_token=1
fi

if [ "$need_new_token" = "1" ]; then
    TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    cat > "$ROOT/.env" <<EOF
QUOTA_ENGINE_DB=/tmp/quota-engine-run/engine.db
QUOTA_ENGINE_TOKEN=$TOKEN
QUOTA_ENGINE_ALLOW_ANON=0
QUOTA_ENGINE_WEB_DIST=$ROOT/web/dist
QUOTA_ENGINE_LITELLM_BASE=http://10.68.13.198:30402
QUOTA_ENGINE_LITELLM_KEY="${QUOTA_ENGINE_LITELLM_KEY:?需要 export QUOTA_ENGINE_LITELLM_KEY=<198 prod master key>；不再内置默认值}"
EOF
    chmod 600 "$ROOT/.env"
    echo "==> new Bearer token provisioned:"
    echo "    $TOKEN"
else
    echo "==> $ROOT/.env already exists (use --rotate to regenerate)"
    grep '^QUOTA_ENGINE_TOKEN=' "$ROOT/.env" | sed 's/=\(....\).*/=\1***redacted***/'
    # Backfill LiteLLM PATCH creds if an older .env predates the write path.
    if ! grep -q '^QUOTA_ENGINE_LITELLM_BASE=' "$ROOT/.env"; then
        echo "QUOTA_ENGINE_LITELLM_BASE=http://10.68.13.198:30402" >> "$ROOT/.env"
        echo "QUOTA_ENGINE_LITELLM_KEY=$QUOTA_ENGINE_LITELLM_KEY" >> "$ROOT/.env"
        echo "==> backfilled QUOTA_ENGINE_LITELLM_* into existing .env"
    fi
fi

if [ ! -f /tmp/quota-engine-run/engine.db ]; then
    echo "!! WARN: /tmp/quota-engine-run/engine.db not found — orchestrator not yet writing"
fi
REMOTE

echo
echo "==> bootstrap done."
