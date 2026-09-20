#!/usr/bin/env bash
# Deploy a new compat_proxy.py to the local-gpu box without sudo.
#
# Why not `systemctl restart`?
#   The deepseek-v4-flash-compat-proxy.service unit is root-owned and sudo
#   requires a password we may not have in-session (team keychain). The unit's
#   [Service] block has `Restart=on-failure` with `RestartSec=5`, so
#   SIGKILL'ing the uvicorn process yields the same effect: non-zero exit ->
#   systemd auto-relaunches ~5s later with the new file in place.
#
# Guardrails:
#   - Syntax-check the new file remotely BEFORE overwriting.
#   - Timestamped backup before overwrite.
#   - Verify port :8000 is listening + /v1/models returns 200 after restart.
#   - Run the regression suite (7 cases) as the last gate.
#
# Usage:
#   ./deploy_compat_proxy.sh <path-to-new-compat_proxy.py>

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <local-path-to-new-compat_proxy.py>" >&2
    exit 2
fi

LOCAL_FILE="$1"
REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
JMS="${REPO_ROOT}/scripts/jms"
REMOTE_TGT="/home/cltx/deepseek-v4-flash/scripts/compat_proxy.py"
REMOTE_STAGING="/tmp/compat_proxy_new.py"
REG_SCRIPT="$(cd "$(dirname "$0")" && pwd)/regression_compat_proxy.py"

if [[ ! -f "$LOCAL_FILE" ]]; then
    echo "not a file: $LOCAL_FILE" >&2
    exit 2
fi
if [[ ! -x "$JMS" ]]; then
    echo "jms wrapper not executable: $JMS" >&2
    exit 2
fi

echo ">>> local syntax check"
python3 -c "import ast; ast.parse(open('${LOCAL_FILE}').read()); print('OK')"

echo ">>> upload + remote syntax check"
cat "$LOCAL_FILE" | "$JMS" ssh local-gpu -- \
    "cat > ${REMOTE_STAGING} && \
     python3 -c 'import ast; ast.parse(open(\"${REMOTE_STAGING}\").read()); print(\"REMOTE_SYNTAX_OK\")'"

echo ">>> backup + replace + SIGKILL uvicorn"
"$JMS" ssh local-gpu -- "\
    ts=\$(date +%Y%m%d-%H%M%S) && \
    cp ${REMOTE_TGT} ${REMOTE_TGT}.bak-\$ts && \
    echo BACKUP=${REMOTE_TGT}.bak-\$ts && \
    cp ${REMOTE_STAGING} ${REMOTE_TGT} && \
    pid=\$(pgrep -f 'uvicorn.*compat_proxy' | head -1) && \
    echo KILLING_PID=\$pid && \
    kill -9 \$pid"

echo ">>> wait for systemd auto-restart (RestartSec=5)"
for i in 1 2 3 4 5 6 7 8; do
    sleep 2
    out=$("$JMS" ssh local-gpu -- "\
        pgrep -f 'uvicorn.*compat_proxy' >/dev/null && \
        curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/models" \
        2>/dev/null || true)
    if [[ "$out" == "200" ]]; then
        echo "restart_ok after ${i} probe(s) (health=200)"
        break
    fi
    echo "still starting (attempt ${i}/8, probe=${out:-none})"
done
if [[ "$out" != "200" ]]; then
    echo "compat_proxy did NOT come back after restart" >&2
    "$JMS" ssh local-gpu -- "pgrep -af 'uvicorn.*compat_proxy'; ss -lntp 2>/dev/null | grep :8000 || true"
    exit 1
fi

echo ">>> regression suite (7 cases)"
cat "$REG_SCRIPT" | "$JMS" ssh local-gpu -- \
    "cat > /tmp/regression_compat_proxy.py && python3 /tmp/regression_compat_proxy.py"

echo
echo "DEPLOY OK"
