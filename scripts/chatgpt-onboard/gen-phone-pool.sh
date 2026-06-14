#!/bin/bash
# Cycle through a pool of phone numbers for one acct's GEN_ONLY OAuth until success.
# Usage: gen-phone-pool.sh acct-N START_OFFSET
# Success marker: "GEN_ONLY done"; bad-number marker: "phone rejected as invalid".
acct="$1"; start="${2:-0}"
POOL=(
"3103006944:11283c878faa1a27b9e79add4ec5a7a4"
"3106514110:2abccfdfac4f3cabbd67c55cd80f54bf"
"3104323254:ece5ccabdcb7a12fb60117e3bb747882"
"3102902898:f7dbf6fc678b2ce67b10715400ad6078"
"3104024052:5fe2007896f9defedbe83a6079ceef9f"
"3109254608:e147bc5f10de3846cdbafbef552b5248"
"3102903532:f466e9f154a64b67bdc445f3923e331e"
"3107701926:6c24cb608ef3e57dc7295ee582f8979f"
"3108675909:3c286d22dbf16bc68953af1ab4c5a8f7"
"3104615572:b5752665c6d6b0674713663222be31d7"
"3104240822:73f62323016dd96363c49e785cb7d014"
"3104246853:6c8466a1be1abdaa43a726ed8b90c273"
"3104974844:7a4171d32e88d6b049f45877f1fb2922"
"3103006702:8496d41b55a9204ae8ec5a49dd9b0d99"
"3104023442:cff0dfdaf76d661fadb2a2cf47b000aa"
"3104099398:55a5ddb45a54c8e9ab9f5dd2ea7227c4"
)
N=${#POOL[@]}
PLOG=/tmp/genpool-$acct.log
: > "$PLOG"
run_once() {
  local ph="$1" api="$2"
  rm -f /tmp/reoauth-$acct.log
  docker run --rm -v /tmp:/t alpine rm -f /t/auth-$acct.json 2>/dev/null
  GEN_ONLY=1 PHONE_NUMBER="$ph" SMS_API_URL="https://app.yuntl.cc/apisms/$api" \
    bash /Data/chatgpt-auth/re-oauth-gen.sh "$acct" >>/tmp/reoauth-$acct.log 2>&1
}
for i in $(seq 0 $((N-1))); do
  idx=$(( (start + i) % N ))
  pair="${POOL[$idx]}"; ph="${pair%%:*}"; api="${pair##*:}"
  echo "[pool] $acct try #$idx phone=$ph  ($(date +%H:%M:%S))" | tee -a "$PLOG"
  run_once "$ph" "$api"
  if grep -q "GEN_ONLY done" /tmp/reoauth-$acct.log; then
    echo "[pool] $acct SUCCESS phone=$ph" | tee -a "$PLOG"; exit 0
  fi
  if grep -q "phone rejected as invalid" /tmp/reoauth-$acct.log; then
    echo "[pool] $acct phone=$ph REJECTED -> next" | tee -a "$PLOG"; continue
  fi
  # non-phone (transient page/CF) failure: retry same number once
  echo "[pool] $acct non-phone fail on $ph, retry same once" | tee -a "$PLOG"
  run_once "$ph" "$api"
  if grep -q "GEN_ONLY done" /tmp/reoauth-$acct.log; then
    echo "[pool] $acct SUCCESS(retry) phone=$ph" | tee -a "$PLOG"; exit 0
  fi
  grep -q "phone rejected as invalid" /tmp/reoauth-$acct.log \
    && echo "[pool] $acct phone=$ph REJECTED(retry) -> next" | tee -a "$PLOG" \
    || echo "[pool] $acct still failing on $ph -> next" | tee -a "$PLOG"
done
echo "[pool] $acct EXHAUSTED pool no success" | tee -a "$PLOG"; exit 1
