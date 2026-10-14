#!/usr/bin/env bash
# Add Google AI Pro accounts (password + 2FA secret) to every 198 Gemini pool, fully automatic.
#
#   scripts/gemini-web/add-account.sh 'Email----Password----TOTPSecret'               # one
#   scripts/gemini-web/add-account.sh -f cards.txt                                      # batch, one card per line
#   STAGES=web scripts/gemini-web/add-account.sh -f cards.txt                           # subset
#
# Card formats (spaces around ---- and blank lines are ignored):
#   email----password----totp            email----password----recovery_email----totp
#
# Stages (default "web antigravity"):
#   web          per account: 188 browser login -> cookies; then ONE config rebuild + ONE gemini-web restart
#   antigravity  per account: OAuth consent -> exchange -> (validation) -> pinned creds;
#                then ONE cli-proxy-api install (canary + svc swap, zero-downtime) + ONE sub2api apply
#   media        NOT automated: deploy/gemini-media takes one account (skill gemini-web-pool-198).
#
# Accounts that fail a stage are reported and skipped; the rest still go in. Re-running is safe
# (existing cookie / creds files are simply refreshed).
# Browser work runs on 188 in its playwright image + Xvfb + patchright. Credentials live root-600 on 198;
# nothing secret is printed or written to the repo.
set -euo pipefail

STAGES="${STAGES:-web antigravity}"
H198=cltx@10.68.13.198
H188=cltx@10.68.13.188
SSH="ssh -o ClearAllForwardings=yes"
HERE="$(cd "$(dirname "$0")" && pwd)"
IMG=mcr.microsoft.com/playwright/python:v1.60.0-noble
D188=/tmp/gemini-login

if [[ "${1:-}" == "-f" ]]; then
  CARDS="$(cat "${2:?usage: $0 -f cards.txt}")"
else
  CARDS="${1:?usage: $0 'email----password----[recovery----]totp' | -f cards.txt}"
fi

# Parse cards -> TSV lines: name<TAB>email_lc<TAB>json   (json carries the original-case email)
PARSED="$(printf '%s\n' "$CARDS" | python3 -c '
import json, re, sys
for raw in sys.stdin:
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    f = [x.strip() for x in line.split("----")]
    if len(f) == 3:   email, pw, totp = f
    elif len(f) == 4: email, pw, _, totp = f
    else:
        print("BAD\t%s" % line.split("----")[0].strip(), flush=True); continue
    name = re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower())
    print("%s\t%s\t%s" % (name, email.lower(), json.dumps({"email": email, "password": pw, "totp_secret": totp.replace(" ", "")})))
')"
if grep -q "^BAD" <<<"$PARSED"; then echo "FATAL: unparseable card(s): $(grep '^BAD' <<<"$PARSED" | cut -f2 | tr '\n' ' ')" >&2; exit 2; fi
NAMES=($(cut -f1 <<<"$PARSED"))
echo "accounts=${#NAMES[@]} stages=[$STAGES]: ${NAMES[*]}"

# Stage creds on both hosts (root-600 on 198, 700 dir on 188).
$SSH $H198 "sudo mkdir -p /root/gemini-web-build/accounts && sudo chmod 700 /root/gemini-web-build/accounts"
$SSH $H188 "mkdir -p $D188/acct $D188/out; chmod 700 $D188 $D188/acct $D188/out"
while IFS=$'\t' read -r -u 3 name _ json; do
  printf '%s' "$json" | $SSH $H198 "sudo sh -c 'umask 077; cat > /root/gemini-web-build/accounts/$name.json'"
  printf '%s' "$json" | $SSH $H188 "umask 077; cat > $D188/acct/$name.json"
done 3<<<"$PARSED"
for f in google_login.py google_oauth_consent.py google_validate.py; do scp -q "$HERE/$f" "$H188:$D188/$f"; done
cleanup188() { $SSH $H188 "rm -f $D188/acct/* $D188/out/*.cookies.json $D188/out/*.callback" || true; }
trap cleanup188 EXIT

# run188 <script.py> <name>: one patchright run on 188; prints only step/STOP/OK lines.
run188() {
  $SSH $H188 "timeout 420 docker run --rm --ipc=host -v $D188:/work:ro -v $D188/acct:/acct:ro -v $D188/out:/out \
    -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 $IMG \
    bash -c 'Xvfb :99 -screen 0 1440x1000x24 >/dev/null 2>&1 & sleep 1 && pip install patchright==1.60.0 -q --root-user-action=ignore >/dev/null 2>&1 && python3 /work/$1 $2' 2>&1 | grep -E '^STOP|^OK|^SETTLED' | cut -c1-220" || true
}

stage_web() {
  local ok=() bad=()
  for name in "${NAMES[@]}"; do
    echo "[web] $name: browser login"
    run188 google_login.py "$name" | sed 's/^/    /'
    if $SSH $H188 "test -s $D188/out/$name.cookies.json"; then
      $SSH $H188 "cat $D188/out/$name.cookies.json; rm -f $D188/out/$name.cookies.json" \
        | $SSH $H198 "sudo sh -c 'umask 077; cat > /root/gemini-web-build/accounts/$name.cookies.json'"
      ok+=("$name")
    else
      bad+=("$name")
    fi
  done
  echo "[web] logged in ${#ok[@]}/${#NAMES[@]}  failed: ${bad[*]:-none}  (screenshots 188:$D188/out/<name>.*.png)"
  [[ ${#ok[@]} -gt 0 ]] || return 0
  scp -q "$HERE/build_config.py" "$H198:/home/cltx/build_config.py"
  echo "[web] rebuild config + ONE restart of gemini-web"
  $SSH $H198 'set -e; TS=$(date +%Y%m%dT%H%M%S); sudo cp -p /root/gemini-web-build/config.yaml /root/gemini-web-build/config.yaml.bak-$TS
    sudo mv /home/cltx/build_config.py /root/gemini-web-build/build_config.py; sudo python3 /root/gemini-web-build/build_config.py
    K="sudo kubectl -n litellm-dev"
    $K create secret generic gemini-web-secrets --from-file=config.yaml=/root/gemini-web-build/config.yaml --dry-run=client -o yaml | sudo kubectl apply -f - >/dev/null
    $K rollout restart deploy/gemini-web >/dev/null; $K rollout status deploy/gemini-web --timeout=600s
    sleep 5; $K logs deploy/gemini-web 2>&1 | grep -v -i psid | grep -E "Gemini clients initialized|Failed to initialize" | tail -3
    echo "rollback: /root/gemini-web-build/config.yaml.bak-$TS"'
}

stage_antigravity() {
  local ok=() bad=()
  scp -q "$HERE/ag_exchange_probe.py" "$H198:/home/cltx/ag_exchange_probe.py"
  $SSH $H198 "sudo mv /home/cltx/ag_exchange_probe.py /root/cliproxy-manifests/"
  # Read the card list on fd 3: every ssh in the loop body would otherwise eat the remaining lines from stdin.
  while IFS=$'\t' read -r -u 3 name email_lc _; do
    echo "[ag] $name: OAuth consent"
    $SSH $H198 'sudo sh -c "cd /root/cliproxy-manifests && python3 cliproxy-antigravity-add-account.py authurl" 2>&1 | grep -E "^https://accounts" | head -1' \
      | $SSH $H188 "umask 077; cat > $D188/acct/$name.authurl"
    run188 google_oauth_consent.py "$name" | sed 's/^/    /'
    if ! $SSH $H188 "test -s $D188/out/$name.callback"; then bad+=("$name"); continue; fi
    $SSH $H188 "cat $D188/out/$name.callback; rm -f $D188/out/$name.callback" \
      | $SSH $H198 "sudo sh -c 'cd /root/cliproxy-manifests && python3 ag_exchange_probe.py $name'" 2>&1 | grep -E "ok=True|^email=|failed" | sed 's/^/    /'
    if $SSH $H198 "sudo test -s /root/cliproxy-manifests/pending/$name.validation_url"; then
      $SSH $H198 "sudo cat /root/cliproxy-manifests/pending/$name.validation_url" | $SSH $H188 "umask 077; cat > $D188/acct/$name.validation_url"
      run188 google_validate.py "$name" | sed 's/^/    /'
      $SSH $H198 "sudo rm -f /root/cliproxy-manifests/pending/$name.validation_url"
    fi
    # 10-14: even after 'Authentication successful' the hub UA stayed 403 VALIDATION_REQUIRED, so pin the
    # legacy UA + the -tiered alias exactly like the older accounts (skill cliproxy-antigravity-ops §model_aliases).
    if $SSH $H198 "sudo python3 - '$name' '$email_lc'" <<'PY'
import json, os, sys
name, email = sys.argv[1], sys.argv[2]
p = f"/root/cliproxy-manifests/pending/{name}.creds.json"
if not os.path.exists(p):
    sys.exit(1)
c = json.load(open(p))
c["user_agent"] = "antigravity/1.0.0 windows/amd64"
c["model_aliases"] = [{"name": "gemini-3.8-flash-tiered", "alias": "gemini-3.8-flash-high"}]
out = f"/root/cliproxy-manifests/antigravity-{email}.json"
json.dump(c, open(out, "w")); os.chmod(out, 0o600); os.remove(p)
PY
    then ok+=("$email_lc"); else bad+=("$name"); fi
  done 3<<<"$PARSED"
  echo "[ag] creds ready ${#ok[@]}/${#NAMES[@]}  failed: ${bad[*]:-none}"
  [[ ${#ok[@]} -gt 0 ]] || return 0
  local creds_args=""
  for e in "${ok[@]}"; do creds_args+=" --creds /root/cliproxy-manifests/antigravity-$e.json"; done
  echo "[ag] ONE cli-proxy-api install behind a canary"
  $SSH $H198 "set -e; K='sudo kubectl -n litellm-dev'
    \$K get deploy cli-proxy-api -o json | python3 -c '
import sys,json
d=json.load(sys.stdin); md=d[\"metadata\"]; d[\"metadata\"]={\"name\":\"cli-proxy-api-canary\",\"namespace\":md[\"namespace\"],\"labels\":{\"app\":\"cli-proxy-api-canary\"}}
d.pop(\"status\",None); s=d[\"spec\"]; s[\"replicas\"]=1; s[\"selector\"]={\"matchLabels\":{\"app\":\"cli-proxy-api-canary\"}}; s[\"template\"][\"metadata\"][\"labels\"]={\"app\":\"cli-proxy-api-canary\"}
print(json.dumps(d))' | sudo kubectl apply -f - >/dev/null
    \$K rollout status deploy/cli-proxy-api-canary --timeout=240s
    \$K patch svc cli-proxy-api -p '{\"spec\":{\"selector\":{\"app\":\"cli-proxy-api-canary\"}}}' >/dev/null
    trap \"\$K patch svc cli-proxy-api -p '{\\\"spec\\\":{\\\"selector\\\":{\\\"app\\\":\\\"cli-proxy-api\\\"}}}' >/dev/null; \$K delete deploy cli-proxy-api-canary --ignore-not-found >/dev/null\" EXIT
    sudo sh -c 'cd /root/cliproxy-manifests && python3 cliproxy-antigravity-add-account.py install $creds_args --apply' 2>&1 | tail -6"
  echo "[ag] sub2api"
  $SSH $H198 "sudo sh -c 'cd /root/cliproxy-manifests && python3 sub2api-antigravity-add-account.py apply --yes' 2>&1 | tail -${#ok[@]} | tail -15"
}

for s in $STAGES; do
  case "$s" in
    web) stage_web ;;
    antigravity) stage_antigravity ;;
    *) echo "unknown stage $s" >&2; exit 2 ;;
  esac
done
echo "done. next: user-facing check (skill gemini-web-pool-198 §回归)"