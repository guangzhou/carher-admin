#!/usr/bin/env bash
# Retry web (gemini.google.com) login for a list of cards through the Aliyun Singapore EIP exit,
# then install the cookies into gemini-web with ONE restart. For accounts that hit reCAPTCHA on 188.
#
#   scripts/gemini-web/eip-retry-web.sh cards.txt
#
# Alternates the two EIP nodes per account. Prints one result line per account; never prints secrets.
set -uo pipefail
CARDS="${1:?cards file}"
HERE="$(cd "$(dirname "$0")" && pwd)"
H198=cltx@10.68.13.198
SSH="ssh -o ConnectTimeout=20 -o ClearAllForwardings=yes"
OUT="$HERE/../../.work/gemini-batch/eip-out"; mkdir -p "$OUT"; chmod 700 "$OUT"
NODES=(ap-southeast-1.172.16.0.86 ap-southeast-1.172.16.16.122)

ok=(); bad=(); i=0
while IFS=$'\t' read -r -u 3 name json; do
  node="${NODES[$((i % 2))]}"; i=$((i + 1))
  res="$(printf '%s' "$json" | OUTDIR="$OUT" EIP_NODE="$node" "$HERE/aliyun-eip-login.sh" login "$name" 2>&1)"
  egress="$(grep -o '^EGRESS .*' <<<"$res" | head -1)"
  if [[ -s "$OUT/$name.cookies.json" ]]; then
    ok+=("$name"); echo "OK    $name ($egress)"
  else
    bad+=("$name"); echo "FAIL  $name ($egress) $(grep -m1 '^STOP' <<<"$res" | cut -c1-90)"
  fi
done 3< <(python3 - "$CARDS" <<'PY'
import json, re, sys
for raw in open(sys.argv[1]):
    f = [x.strip() for x in raw.strip().split("----")]
    if len(f) not in (3, 4):
        continue
    email, pw, totp = f[0], f[1], f[-1]
    name = re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower())
    print("%s\t%s" % (name, json.dumps({"email": email, "password": pw, "totp_secret": totp})))
PY
)
echo "[eip-web] ok ${#ok[@]}  failed ${#bad[@]}: ${bad[*]:-none}"
[[ ${#ok[@]} -gt 0 ]] || exit 0

# Install: creds json (for future re-login) + cookies on 198, then one gemini-web rebuild/restart.
while IFS=$'\t' read -r -u 3 name json; do
  [[ -s "$OUT/$name.cookies.json" ]] || continue
  printf '%s' "$json" | $SSH $H198 "sudo sh -c 'umask 077; cat > /root/gemini-web-build/accounts/$name.json'"
  $SSH $H198 "sudo sh -c 'umask 077; cat > /root/gemini-web-build/accounts/$name.cookies.json'" < "$OUT/$name.cookies.json"
  rm -f "$OUT/$name.cookies.json"
done 3< <(python3 - "$CARDS" <<'PY'
import json, re, sys
for raw in open(sys.argv[1]):
    f = [x.strip() for x in raw.strip().split("----")]
    if len(f) not in (3, 4):
        continue
    name = re.sub(r"[^a-z0-9]", "", f[0].split("@")[0].lower())
    print("%s\t%s" % (name, json.dumps({"email": f[0], "password": f[1], "totp_secret": f[-1]})))
PY
)
scp -q "$HERE/build_config.py" "$H198:/home/cltx/build_config.py"
$SSH $H198 'set -e; TS=$(date +%Y%m%dT%H%M%S); sudo cp -p /root/gemini-web-build/config.yaml /root/gemini-web-build/config.yaml.bak-$TS
  sudo mv /home/cltx/build_config.py /root/gemini-web-build/build_config.py; sudo python3 /root/gemini-web-build/build_config.py >/dev/null
  K="sudo kubectl -n litellm-dev"
  $K create secret generic gemini-web-secrets --from-file=config.yaml=/root/gemini-web-build/config.yaml --dry-run=client -o yaml | sudo kubectl apply -f - >/dev/null
  $K rollout restart deploy/gemini-web >/dev/null; $K rollout status deploy/gemini-web --timeout=600s >/dev/null
  sleep 5; $K logs deploy/gemini-web 2>&1 | grep -o "Gemini clients initialized: .*" | sed "s/\x1b\[[0-9;]*m//g" | python3 -c "import sys,ast;s=sys.stdin.read();print(\"[eip-web] gemini-web clients:\",len(ast.literal_eval(s.split(\": \",1)[1].rstrip(\".\\n\"))))"
  echo "rollback: config.yaml.bak-$TS"'
