#!/usr/bin/env bash
# Run google_login.py / google_oauth_consent.py on an Aliyun Singapore EIP node instead of 188.
# Use when 188's egress gets "Confirm you're not a robot" before the password step (10-15 batch4).
#
#   scripts/gemini-web/aliyun-eip-login.sh login <name> < account.json
#   scripts/gemini-web/aliyun-eip-login.sh oauth <name> <authurl_file> < account.json
#   EIP_NODE=ap-southeast-1.172.16.16.122 scripts/gemini-web/aliyun-eip-login.sh login ...
#
# Prints the script's STOP/OK lines; on success writes ./<name>.cookies.json or ./<name>.callback
# into OUTDIR (default .work/gemini-batch/eip-out, mode 700). Nothing secret is echoed.
#
# Same isolation rules as scripts/aliyun-eip-onboard.sh (read it before changing this):
#   hostNetwork + nodeName on an EIP node (normal pods share the prod NAT egress 47.84.112.136);
#   /tmp on tmpfs (EIP nodes hover near the eviction line); resources declared (never BestEffort);
#   DISPLAY unique per Job (hostNetwork shares the X socket namespace); image already has
#   patchright+chromium 1.60.0 — do NOT pip install.
set -euo pipefail
ACTION="${1:?login|oauth}"; NAME="${2:?name}"
NS=carher
IMG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:zerokey-capture-aliyun-20260707-otp3
NODE="${EIP_NODE:-ap-southeast-1.172.16.0.86}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="${OUTDIR:-$HERE/../../.work/gemini-batch/eip-out}"
mkdir -p "$OUTDIR"; chmod 700 "$OUTDIR"
K="kubectl --request-timeout=60s -n $NS"
SAFE="$(printf '%s' "$NAME" | tr -cd 'a-z0-9' | cut -c1-30)"
JOB="gweb-${ACTION}-${SAFE}"
DISPNUM=$(( 500 + $(printf '%s' "$SAFE" | cksum | cut -d' ' -f1) % 400 ))

ACCT_JSON="$(cat)"
[[ -n "$ACCT_JSON" ]] || { echo "FATAL: account json on stdin"; exit 2; }
AUTHURL=""
if [[ "$ACTION" == "oauth" ]]; then AUTHURL="$(cat "${3:?authurl file}")"; fi

cleanup(){ $K delete job "$JOB" --ignore-not-found --wait=false >/dev/null 2>&1 || true
           $K delete secret "$JOB" cm "$JOB" --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup; $K wait --for=delete job/"$JOB" --timeout=60s >/dev/null 2>&1 || true

$K create configmap "$JOB" --from-file=google_login.py="$HERE/google_login.py" \
   --from-file=google_oauth_consent.py="$HERE/google_oauth_consent.py" >/dev/null
TMPD=$(mktemp -d); chmod 700 "$TMPD"; printf '%s' "$ACCT_JSON" > "$TMPD/acct.json"; printf '%s' "$AUTHURL" > "$TMPD/authurl"
$K create secret generic "$JOB" --from-file=acct.json="$TMPD/acct.json" --from-file=authurl="$TMPD/authurl" >/dev/null
rm -rf "$TMPD"

SCRIPT=google_login.py; [[ "$ACTION" == "oauth" ]] && SCRIPT=google_oauth_consent.py
cat <<YAML | kubectl --request-timeout=60s apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata: {name: $JOB, namespace: $NS}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      nodeName: $NODE
      restartPolicy: Never
      volumes:
        - {name: src, configMap: {name: $JOB}}
        - {name: sec, secret: {secretName: $JOB, defaultMode: 0400}}
        - {name: shm, emptyDir: {medium: Memory, sizeLimit: 1Gi}}
        - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 3Gi}}
      containers:
        - name: run
          image: $IMG
          resources:
            requests: {cpu: 200m, memory: 1Gi, ephemeral-storage: 256Mi}
            limits: {cpu: "2", memory: 6Gi, ephemeral-storage: 512Mi}
          volumeMounts:
            - {name: src, mountPath: /src}
            - {name: sec, mountPath: /sec}
            - {name: shm, mountPath: /dev/shm}
            - {name: tmp, mountPath: /tmp}
          env:
            - {name: DISPLAY, value: ":$DISPNUM"}
            - {name: PLAYWRIGHT_BROWSERS_PATH, value: /ms-playwright}
            - {name: GW_ACCT_DIR, value: /tmp/acct}
            - {name: GW_OUT_DIR, value: /tmp/out}
            - {name: GW_WORK_DIR, value: /tmp/work}
          command:
            - bash
            - -c
            - |
              set -o pipefail
              mkdir -p /tmp/work /tmp/acct /tmp/out
              cp /src/*.py /tmp/work/
              cp /sec/acct.json /tmp/acct/$NAME.json; cp /sec/authurl /tmp/acct/$NAME.authurl
              echo "EGRESS \$(python3 -c "import urllib.request;print(urllib.request.urlopen('https://api.ipify.org',timeout=15).read().decode())" 2>/dev/null)"
              Xvfb :$DISPNUM -screen 0 1440x1000x24 >/tmp/xvfb.log 2>&1 &
              for i in \$(seq 1 30); do [ -S /tmp/.X11-unix/X$DISPNUM ] && break; sleep 1; done
              cd /tmp/work && python3 /tmp/work/$SCRIPT $NAME
              echo "RC=\$?"
              [ -s /tmp/out/$NAME.cookies.json ] && { echo "BEGIN_COOKIES"; base64 -w0 /tmp/out/$NAME.cookies.json; echo; echo "END_COOKIES"; }
              [ -s /tmp/out/$NAME.callback ] && { echo "BEGIN_CALLBACK"; base64 -w0 /tmp/out/$NAME.callback; echo; echo "END_CALLBACK"; }
              exit 0
YAML

$K wait --for=condition=complete job/"$JOB" --timeout=420s >/dev/null 2>&1 || true
LOG="$($K logs job/"$JOB" --tail=-1 2>/dev/null || true)"
grep -E "^EGRESS|^STOP|^OK|^RC=|^\[[0-9]+\]" <<<"$LOG" | cut -c1-220 || true
b64="$(sed -n '/^BEGIN_COOKIES$/,/^END_COOKIES$/p' <<<"$LOG" | sed '1d;$d')"
[[ -n "$b64" ]] && { umask 077; printf '%s' "$b64" | base64 -d > "$OUTDIR/$NAME.cookies.json"; echo "saved $OUTDIR/$NAME.cookies.json"; }
b64="$(sed -n '/^BEGIN_CALLBACK$/,/^END_CALLBACK$/p' <<<"$LOG" | sed '1d;$d')"
[[ -n "$b64" ]] && { umask 077; printf '%s' "$b64" | base64 -d > "$OUTDIR/$NAME.callback"; echo "saved $OUTDIR/$NAME.callback"; }
exit 0
