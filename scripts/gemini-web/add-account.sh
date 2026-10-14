#!/usr/bin/env bash
# Add one Google AI Pro account to every 198 Gemini pool, fully automatic (password + 2FA secret).
#
#   scripts/gemini-web/add-account.sh 'Email----Password----TOTPSecret'
#   scripts/gemini-web/add-account.sh 'Email----Password----RecoveryEmail----TOTPSecret'
#   STAGES="web antigravity" scripts/gemini-web/add-account.sh '...'     # subset
#
# Stages (default: all):
#   web          gemini.google.com cookies -> deploy/gemini-web (chat: gemini-4-argon / 3.8-flash / 3.1-pro / 3.5-flash-lite)
#   antigravity  Antigravity OAuth -> cli-proxy-api (canary + svc swap, zero-downtime) -> sub2api
#   media        is NOT automated here: deploy/gemini-media takes one account via GEMINI_COOKIES (no pool).
#                To switch it, see skill gemini-web-pool-198 §媒体网关.
#
# Browser work runs on 188 in its existing playwright image + Xvfb + patchright (do not build another
# browser env on 198). Credentials live root-600 on 198; nothing secret is printed or written to the repo.
set -euo pipefail

LINE="${1:?usage: $0 'email----password----[recovery----]totp'}"
STAGES="${STAGES:-web antigravity}"
H198=cltx@10.68.13.198
H188=cltx@10.68.13.188
SSH="ssh -o ClearAllForwardings=yes"
HERE="$(cd "$(dirname "$0")" && pwd)"
IMG=mcr.microsoft.com/playwright/python:v1.60.0-noble
D188=/tmp/gemini-login

IFS=$'\n' read -r -d '' -a F < <(printf '%s' "$LINE" | awk -F'----' '{for(i=1;i<=NF;i++)print $i}' && printf '\0') || true
case "${#F[@]}" in
  3) EMAIL="${F[0]}"; PW="${F[1]}"; TOTP="${F[2]}" ;;
  4) EMAIL="${F[0]}"; PW="${F[1]}"; TOTP="${F[3]}" ;;
  *) echo "FATAL: expected 3 or 4 fields separated by ----" >&2; exit 2 ;;
esac
NAME="$(printf '%s' "${EMAIL%@*}" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')"
EMAIL_LC="$(printf '%s' "$EMAIL" | tr 'A-Z' 'a-z')"
echo "account=$EMAIL id=$NAME stages=[$STAGES]"
ACCT_JSON="$(python3 -c 'import json,sys;print(json.dumps({"email":sys.argv[1],"password":sys.argv[2],"totp_secret":sys.argv[3]}))' "$EMAIL" "$PW" "$TOTP")"

printf '%s' "$ACCT_JSON" | $SSH $H198 "sudo mkdir -p /root/gemini-web-build/accounts && sudo chmod 700 /root/gemini-web-build/accounts && sudo sh -c 'umask 077; cat > /root/gemini-web-build/accounts/$NAME.json'"
printf '%s' "$ACCT_JSON" | $SSH $H188 "mkdir -p $D188/acct $D188/out; chmod 700 $D188 $D188/acct $D188/out; umask 077; cat > $D188/acct/$NAME.json"
for f in google_login.py google_oauth_consent.py google_validate.py; do scp -q "$HERE/$f" "$H188:$D188/$f"; done
cleanup188() { $SSH $H188 "rm -f $D188/acct/$NAME.* $D188/out/$NAME.cookies.json $D188/out/$NAME.callback" || true; }
trap cleanup188 EXIT

# run188 <script.py>: one patchright run on 188; prints only step/STOP/OK lines.
run188() {
  $SSH $H188 "timeout 420 docker run --rm --ipc=host -v $D188:/work:ro -v $D188/acct:/acct:ro -v $D188/out:/out \
    -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 $IMG \
    bash -c 'Xvfb :99 -screen 0 1440x1000x24 >/dev/null 2>&1 & sleep 1 && pip install patchright==1.60.0 -q --root-user-action=ignore >/dev/null 2>&1 && python3 /work/$1 $NAME' 2>&1 | grep -E '^\[|^STOP|^OK|^SETTLED' | cut -c1-200" || true
}

stage_web() {
  echo "[web 1/3] browser login -> gemini.google.com cookies"
  run188 google_login.py
  $SSH $H188 "test -s $D188/out/$NAME.cookies.json" || { echo "FAILED web: no cookies; screenshots 188:$D188/out/$NAME.*.png" >&2; return 1; }
  echo "[web 2/3] install cookies on 198"
  $SSH $H188 "cat $D188/out/$NAME.cookies.json" | $SSH $H198 "sudo sh -c 'umask 077; cat > /root/gemini-web-build/accounts/$NAME.cookies.json'"
  scp -q "$HERE/build_config.py" "$H198:/home/cltx/build_config.py"
  echo "[web 3/3] rebuild config + restart gemini-web"
  $SSH $H198 'set -e; TS=$(date +%Y%m%dT%H%M%S); sudo cp -p /root/gemini-web-build/config.yaml /root/gemini-web-build/config.yaml.bak-$TS
    sudo mv /home/cltx/build_config.py /root/gemini-web-build/build_config.py; sudo python3 /root/gemini-web-build/build_config.py
    K="sudo kubectl -n litellm-dev"
    $K create secret generic gemini-web-secrets --from-file=config.yaml=/root/gemini-web-build/config.yaml --dry-run=client -o yaml | sudo kubectl apply -f - >/dev/null
    $K rollout restart deploy/gemini-web >/dev/null; $K rollout status deploy/gemini-web --timeout=300s
    $K logs deploy/gemini-web 2>&1 | grep -v -i psid | grep -E "Gemini clients initialized|Failed to initialize|abuse status|credits remaining" | tail -6
    echo "rollback: config.yaml.bak-$TS"'
}

stage_antigravity() {
  echo "[ag 1/5] OAuth consent on 188"
  $SSH $H198 'sudo sh -c "cd /root/cliproxy-manifests && python3 cliproxy-antigravity-add-account.py authurl" 2>&1 | grep -E "^https://accounts" | head -1' \
    | $SSH $H188 "umask 077; cat > $D188/acct/$NAME.authurl"
  run188 google_oauth_consent.py
  $SSH $H188 "test -s $D188/out/$NAME.callback" || { echo "FAILED antigravity: no callback; screenshots 188:$D188/out/$NAME.oauth-*.png" >&2; return 1; }
  echo "[ag 2/5] exchange + per-model probe (both UAs)"
  scp -q "$HERE/ag_exchange_probe.py" "$H198:/home/cltx/ag_exchange_probe.py"
  $SSH $H188 "cat $D188/out/$NAME.callback; rm -f $D188/out/$NAME.callback" \
    | $SSH $H198 "sudo mv /home/cltx/ag_exchange_probe.py /root/cliproxy-manifests/; sudo sh -c 'cd /root/cliproxy-manifests && python3 ag_exchange_probe.py $NAME'"
  if $SSH $H198 "sudo test -s /root/cliproxy-manifests/pending/$NAME.validation_url"; then
    echo "[ag 3/5] account wants Google validation -> walking it in the browser"
    $SSH $H198 "sudo cat /root/cliproxy-manifests/pending/$NAME.validation_url" | $SSH $H188 "umask 077; cat > $D188/acct/$NAME.validation_url"
    run188 google_validate.py
    $SSH $H198 "sudo rm -f /root/cliproxy-manifests/pending/$NAME.validation_url"
  fi
  # 10-14: even after 'Authentication successful' the hub UA stayed 403 VALIDATION_REQUIRED, so pin the
  # legacy UA + the -tiered alias exactly like the older accounts (skill cliproxy-antigravity-ops §model_aliases).
  echo "[ag 4/5] write creds (pinned 1.0.0 UA + 3.8 alias) and install into cli-proxy-api (canary first)"
  $SSH $H198 "sudo python3 - '$NAME' '$EMAIL_LC'" <<'PY'
import json, os, sys
name, email = sys.argv[1], sys.argv[2]
c = json.load(open(f"/root/cliproxy-manifests/pending/{name}.creds.json"))
c["user_agent"] = "antigravity/1.0.0 windows/amd64"
c["model_aliases"] = [{"name": "gemini-3.8-flash-tiered", "alias": "gemini-3.8-flash-high"}]
out = f"/root/cliproxy-manifests/antigravity-{email}.json"
json.dump(c, open(out, "w")); os.chmod(out, 0o600); print("wrote", out)
PY
  $SSH $H198 "set -e; K='sudo kubectl -n litellm-dev'
    \$K get deploy cli-proxy-api -o json | python3 -c '
import sys,json
d=json.load(sys.stdin); md=d[\"metadata\"]; d[\"metadata\"]={\"name\":\"cli-proxy-api-canary\",\"namespace\":md[\"namespace\"],\"labels\":{\"app\":\"cli-proxy-api-canary\"}}
d.pop(\"status\",None); s=d[\"spec\"]; s[\"replicas\"]=1; s[\"selector\"]={\"matchLabels\":{\"app\":\"cli-proxy-api-canary\"}}; s[\"template\"][\"metadata\"][\"labels\"]={\"app\":\"cli-proxy-api-canary\"}
print(json.dumps(d))' | sudo kubectl apply -f - >/dev/null
    \$K rollout status deploy/cli-proxy-api-canary --timeout=240s
    \$K patch svc cli-proxy-api -p '{\"spec\":{\"selector\":{\"app\":\"cli-proxy-api-canary\"}}}' >/dev/null
    trap \"\$K patch svc cli-proxy-api -p '{\\\"spec\\\":{\\\"selector\\\":{\\\"app\\\":\\\"cli-proxy-api\\\"}}}' >/dev/null; \$K delete deploy cli-proxy-api-canary --ignore-not-found >/dev/null\" EXIT
    sudo sh -c 'cd /root/cliproxy-manifests && python3 cliproxy-antigravity-add-account.py install --creds /root/cliproxy-manifests/antigravity-$EMAIL_LC.json --apply' 2>&1 | tail -6"
  echo "[ag 5/5] per-account matrix + sub2api"
  $SSH $H198 "sudo sh -c 'cd /root/cliproxy-manifests && python3 cliproxy-antigravity-model-matrix.py --only $EMAIL_LC -n 1' 2>&1 | tail -12
    sudo sh -c 'cd /root/cliproxy-manifests && python3 sub2api-antigravity-add-account.py apply --yes' 2>&1 | tail -6"
}

for s in $STAGES; do
  case "$s" in
    web) stage_web ;;
    antigravity) stage_antigravity ;;
    *) echo "unknown stage $s" >&2; exit 2 ;;
  esac
done
echo "done. next: user-facing check (skill gemini-web-pool-198 §回归)"