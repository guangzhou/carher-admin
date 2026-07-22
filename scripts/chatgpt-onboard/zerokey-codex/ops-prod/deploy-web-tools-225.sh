#!/usr/bin/env bash
# deploy-web-tools-225.sh — deploy/refresh the web tool-injection layer onto the
# 225 K3s web-only zerokey pods (zero-87..99). Idempotent.
#
# Mechanism: pods copy /patch/*.js (ConfigMap zk-image-patch) → /app on startup.
# We update the CM with the 3 patched route files, ensure the startup command
# copies them, then rollout. See ops-prod/WEB-TOOLS-README.md for the full story.
#
# Access 225 only via 198's `sudo k3s kubectl` (direct kubectl context = aliyun).
#
# Usage:
#   ./deploy-web-tools-225.sh                # all zero-* pods
#   ./deploy-web-tools-225.sh 93             # canary a single pod
#   POD_HOST=cltx@10.68.13.198 ./deploy-web-tools-225.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"        # zerokey-codex/
ROUTES="$HERE/zerokey-patch/routes"
POD_HOST="${POD_HOST:-cltx@10.68.13.198}"
K="sudo k3s kubectl -n litellm-product"
FILES=(web-tools.js raw.js responses.js)

echo "[1/4] pushing route files to $POD_HOST"
for f in "${FILES[@]}"; do
  base64 -i "$ROUTES/$f" | ssh -o ConnectTimeout=8 "$POD_HOST" "mkdir -p ~/zk-webtools && base64 -d > ~/zk-webtools/$f"
done

echo "[2/4] merge-patching ConfigMap zk-image-patch (other keys preserved)"
ssh -o ConnectTimeout=15 "$POD_HOST" "
python3 - <<'PY'
import json
data={k:open('/home/cltx/zk-webtools/'+k).read() for k in ['web-tools.js','raw.js','responses.js']}
open('/tmp/cm-patch.json','w').write(json.dumps({'data':data}))
PY
$K patch cm zk-image-patch --type merge --patch-file /tmp/cm-patch.json"

# which pods
TARGETS="${*:-}"
if [ -z "$TARGETS" ]; then
  TARGETS=$(ssh -o ConnectTimeout=10 "$POD_HOST" "$K get deploy -o name 2>/dev/null | grep -oE 'zero-[0-9]+' | sed 's/zero-//' | sort -n | tr '\n' ' '")
fi
echo "[3/4] ensuring startup command copies the 3 routes + rollout: $TARGETS"

read -r -d '' NEWARG <<'CMD' || true
cp /patch/zerokey-serve-codex.js /app/zerokey-serve-codex.js
cp /patch/images.js /app/routes/images.js
cp /patch/api.js /app/core/chatgpt/api.js
cp /patch/web-tools.js /app/routes/web-tools.js
cp /patch/raw.js /app/routes/raw.js
cp /patch/responses.js /app/routes/responses.js
exec node /app/zerokey-serve-codex.js
CMD

ssh -o ConnectTimeout=120 "$POD_HOST" "
NEWARG=\$(cat <<'CMD'
$NEWARG
CMD
)
python3 - \"\$NEWARG\" <<'PY'
import json,sys
open('/tmp/arg-patch.json','w').write(json.dumps(
  {'spec':{'template':{'spec':{'containers':[{'name':'zerokey','args':['-c',sys.argv[1]]}]}}}}))
PY
for n in $TARGETS; do
  $K patch deploy zero-\$n --type strategic --patch-file /tmp/arg-patch.json >/dev/null 2>&1 && echo \"  zero-\$n patched\" || echo \"  zero-\$n FAIL\"
done
echo '[4/4] waiting for rollouts...'
for n in $TARGETS; do
  $K rollout status deploy/zero-\$n --timeout=120s >/dev/null 2>&1 && echo \"  zero-\$n ready\" || echo \"  zero-\$n TIMEOUT\"
done
"
echo "done. smoke: see ops-prod/WEB-TOOLS-README.md step 4"
