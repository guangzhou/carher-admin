#!/usr/bin/env bash
# chatgpt-acct-usage.sh — per-source ChatGPT Pro /codex/usage probe.
#
# Source-of-truth rule:
#   - 188 docker accounts are queried from 188.
#   - 187 docker accounts (acct-2/15/17) are queried from 187 via SSH.
#   - Aliyun K8s accounts are queried from their own chatgpt-acct-N Pod.
#   - Do not copy auth.json between hosts and query through a different host.
#
# Usage:
#   ./scripts/chatgpt-acct-usage.sh                # table, healthy rows only
#   ./scripts/chatgpt-acct-usage.sh --all          # include error rows in table
#   ./scripts/chatgpt-acct-usage.sh --json         # raw JSON array
#   ./scripts/chatgpt-acct-usage.sh --retry 5      # retry Cloudflare/network errors
#   ./scripts/chatgpt-acct-usage.sh --skip-aliyun  # only 188 accounts

set -euo pipefail

JSON=""
RETRY="${USAGE_RETRY:-3}"
HTTP_TIMEOUT="${USAGE_HTTP_TIMEOUT:-10}"
ALIYUN_JOBS="${USAGE_ALIYUN_JOBS:-2}"
FORCE_188_ACCTS="${USAGE_FORCE_188_ACCTS:-}"
MALAYSIA_ACCTS="${USAGE_MALAYSIA_ACCTS:-2,15,17}"
MALAYSIA_SSH_SPECS="${USAGE_MALAYSIA_SSH_SPECS:-acct-2=jms:JSZX-AI-02,acct-15=jms:JSZX-AI-02,acct-17=jms:JSZX-AI-02}"
ALL=""
SKIP_ALIYUN=""
SKIP_MALAYSIA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)         JSON="1"; shift ;;
    --all)          ALL="1"; shift ;;
    --retry)        RETRY="$2"; shift 2 ;;
    --timeout)      HTTP_TIMEOUT="$2"; shift 2 ;;
    --aliyun-jobs)  ALIYUN_JOBS="$2"; shift 2 ;;
    --skip-aliyun)  SKIP_ALIYUN="1"; shift ;;
    --skip-malaysia) SKIP_MALAYSIA="1"; shift ;;
    -h|--help)
      sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_LOCAL="$SCRIPT_DIR/chatgpt-acct-usage-raw.py"
PY_REMOTE="/tmp/chatgpt-acct-usage-raw.py"
SSH_188="cltx@10.68.13.188"

if [[ ! -f "$PY_LOCAL" ]]; then
  echo "找不到 $PY_LOCAL" >&2
  exit 1
fi

tmpdir="$(mktemp -d -t chatgpt-usage.XXXXXX)"
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

log() {
  echo "$*" >&2
}

discover_aliyun_specs() {
  local pods_json="$tmpdir/aliyun-pods.json"
  if ! kubectl get pod -n carher -o json > "$pods_json" 2>"$tmpdir/kubectl-get-pods.err"; then
    if [[ -n "$SKIP_ALIYUN" ]]; then
      echo "[]"
      return
    fi
    echo "[chatgpt-acct-usage] kubectl unavailable; start ACK tunnel or pass --skip-aliyun explicitly" >&2
    cat "$tmpdir/kubectl-get-pods.err" >&2
    exit 1
  fi
  FORCE_188_ACCTS="$FORCE_188_ACCTS" python3 - "$pods_json" <<'PY'
import json, os, re, sys

def acct_numbers(raw):
    out = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        part = part.removeprefix("acct-")
        if part.isdigit():
            out.add(int(part))
    return out

force_188 = acct_numbers(os.environ.get("FORCE_188_ACCTS", ""))
d = json.load(open(sys.argv[1]))
out = []
for item in d.get("items", []):
    labels = item.get("metadata", {}).get("labels", {}) or {}
    app = labels.get("app", "")
    m = re.fullmatch(r"chatgpt-acct-(\d+)", app)
    if not m:
        continue
    acct_num = int(m.group(1))
    if acct_num in force_188:
        continue
    statuses = item.get("status", {}).get("containerStatuses", []) or []
    ready = any(s.get("name") == "litellm" and s.get("ready") for s in statuses)
    if not ready:
        continue
    pod = item["metadata"]["name"]
    acct = f"acct-{m.group(1)}"
    out.append({
        "name": acct,
        "pod": pod,
        "source": f"aliyun:{pod}",
        "auth_path": "/chatgpt-auth/auth.json",
        "container": "litellm",
    })
out.sort(key=lambda x: int(x["name"].split("-")[1]))
print(json.dumps(out, ensure_ascii=False))
PY
}

discover_malaysia_specs() {
  if [[ -n "$SKIP_MALAYSIA" ]]; then
    echo "[]"
    return
  fi
  MALAYSIA_SSH_SPECS="$MALAYSIA_SSH_SPECS" python3 - <<'PY'
import json, os, re

raw = os.environ.get("MALAYSIA_SSH_SPECS", "").strip()
out = []
if raw:
    # Format:
    #   acct-15=user@host:/Data/chatgpt-auth/acct-15/auth.json:litellm-chatgpt-15
    # Comma/newline separated. auth_path/container are optional.
    for part in re.split(r"[,\n]+", raw):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"invalid USAGE_MALAYSIA_SSH_SPECS item: {part}")
        name, rest = part.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"acct-\d+", name):
            raise SystemExit(f"invalid Malaysia acct name: {name}")
        fields = rest.split(":")
        # jms:asset uses : internally — consume two fields as ssh target
        if fields[0].strip() == "jms" and len(fields) >= 2:
            ssh = f"jms:{fields[1].strip()}"
            fields = fields[2:]  # remaining are auth_path, container
        else:
            ssh = fields[0].strip()
            fields = fields[1:]
        if not ssh:
            raise SystemExit(f"missing ssh target for {name}")
        n = int(name.split("-", 1)[1])
        auth_path = fields[0].strip() if len(fields) > 0 and fields[0].strip() else f"/Data/chatgpt-auth/{name}/auth.json"
        container = fields[1].strip() if len(fields) > 1 and fields[1].strip() else f"litellm-chatgpt-{n}"
        out.append({
            "name": name,
            "ssh": ssh,
            "source": f"remote:{ssh}",
            "auth_path": auth_path,
            "container": container,
        })
out.sort(key=lambda x: int(x["name"].split("-")[1]))
print(json.dumps(out, ensure_ascii=False))
PY
}

malaysia_expected_numbers() {
  if [[ -n "$SKIP_MALAYSIA" ]]; then
    echo "[]"
    return
  fi
  MALAYSIA_ACCTS="$MALAYSIA_ACCTS" python3 - <<'PY'
import json, os, re
out = set()
for part in re.split(r"[,\s]+", os.environ.get("MALAYSIA_ACCTS", "").strip()):
    if not part:
        continue
    part = part.removeprefix("acct-")
    if not part.isdigit():
        raise SystemExit(f"invalid USAGE_MALAYSIA_ACCTS item: {part}")
    out.add(int(part))
print(json.dumps(sorted(out)))
PY
}

discover_188_specs() {
  local skip_numbers_json="$1"
  ssh "$SSH_188" "SKIP_NUMBERS='$skip_numbers_json' FORCE_188_ACCTS='$FORCE_188_ACCTS' python3 -" <<'PY'
import json, os, re

def acct_numbers(raw):
    out = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        part = part.removeprefix("acct-")
        if part.isdigit():
            out.add(int(part))
    return out

force_188 = acct_numbers(os.environ.get("FORCE_188_ACCTS", ""))
skip = set(json.loads(os.environ.get("SKIP_NUMBERS", "[]"))) - force_188
base = "/Data/chatgpt-auth"
out = []
if os.path.isdir(base):
    for name in os.listdir(base):
        m = re.fullmatch(r"acct-(\d+)", name)
        if not m:
            continue
        n = int(m.group(1))
        if n in skip:
            continue
        auth_path = f"{base}/{name}/auth.json"
        if not os.path.exists(auth_path):
            continue
        container = "litellm-chatgpt" if n == 1 else f"litellm-chatgpt-{n}"
        out.append({
            "name": name,
            "source": "188:10.68.13.188",
            "auth_path": auth_path,
            "container": container,
        })
out.sort(key=lambda x: int(x["name"].split("-")[1]))
print(json.dumps(out, ensure_ascii=False))
PY
}

run_188_probe() {
  local specs="$1"
  local spec_file="$tmpdir/specs-188.json"
  local out_file="$tmpdir/results-188.json"
  printf '%s' "$specs" > "$spec_file"
  scp -q "$PY_LOCAL" "$SSH_188:$PY_REMOTE"
  scp -q "$spec_file" "$SSH_188:/tmp/chatgpt-usage-specs-188.json"
  ssh "$SSH_188" "USAGE_RETRY='$RETRY' USAGE_HTTP_TIMEOUT='$HTTP_TIMEOUT' USAGE_JSON=1 USAGE_ACCOUNT_SPECS_FILE=/tmp/chatgpt-usage-specs-188.json python3 $PY_REMOTE" > "$out_file"
  echo "$out_file"
}

run_aliyun_probe_one() {
  local spec_json="$1"
  local acct pod out_file spec_file
  acct=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$spec_json")
  pod=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pod"])' "$spec_json")
  out_file="$tmpdir/results-${acct}.json"
  spec_file="$tmpdir/spec-${acct}.json"
  python3 -c 'import json,sys; s=json.loads(sys.argv[1]); s.pop("pod", None); print(json.dumps([s], ensure_ascii=False))' "$spec_json" > "$spec_file"
  if ! kubectl cp -n carher -c litellm "$PY_LOCAL" "${pod}:$PY_REMOTE" >/dev/null 2>"$tmpdir/${acct}-copy-py.err"; then
    python3 - "$out_file" "$spec_json" "$tmpdir/${acct}-copy-py.err" <<'PY'
import json, sys
out, spec_s, err_path = sys.argv[1:4]
spec = json.loads(spec_s)
err = open(err_path).read().strip() or "kubectl cp probe script failed"
json.dump([{"acct": spec["name"], "source": spec["source"], "auth_path": spec["auth_path"], "container": spec["container"], "ok": False, "error": err, "usage": None}], open(out, "w"), ensure_ascii=False, indent=2)
PY
    echo "$out_file"
    return
  fi
  if ! kubectl cp -n carher -c litellm "$spec_file" "${pod}:/tmp/chatgpt-usage-spec.json" >/dev/null 2>"$tmpdir/${acct}-copy-spec.err"; then
    python3 - "$out_file" "$spec_json" "$tmpdir/${acct}-copy-spec.err" <<'PY'
import json, sys
out, spec_s, err_path = sys.argv[1:4]
spec = json.loads(spec_s)
err = open(err_path).read().strip() or "kubectl cp spec failed"
json.dump([{"acct": spec["name"], "source": spec["source"], "auth_path": spec["auth_path"], "container": spec["container"], "ok": False, "error": err, "usage": None}], open(out, "w"), ensure_ascii=False, indent=2)
PY
    echo "$out_file"
    return
  fi
  exec_ok=0
  for exec_attempt in 1 2 3; do
    if kubectl exec -n carher -c litellm "$pod" -- sh -lc \
      "USAGE_RETRY='$RETRY' USAGE_HTTP_TIMEOUT='$HTTP_TIMEOUT' USAGE_JSON=1 USAGE_ACCOUNT_SPECS_FILE=/tmp/chatgpt-usage-spec.json python3 $PY_REMOTE" > "$out_file" 2>"$tmpdir/${acct}-exec.err"; then
      exec_ok=1
      break
    fi
    sleep "$exec_attempt"
  done
  if [[ "$exec_ok" != "1" ]]; then
    python3 - "$out_file" "$spec_json" "$tmpdir/${acct}-exec.err" <<'PY'
import json, sys
out, spec_s, err_path = sys.argv[1:4]
spec = json.loads(spec_s)
err = open(err_path).read().strip() or "kubectl exec probe failed"
json.dump([{"acct": spec["name"], "source": spec["source"], "auth_path": spec["auth_path"], "container": spec["container"], "ok": False, "error": err, "usage": None}], open(out, "w"), ensure_ascii=False, indent=2)
PY
  fi
  echo "$out_file"
}

run_aliyun_inline_usage_fallback() {
  local spec_json="$1"
  local acct pod out_file spec_b64
  acct=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$spec_json")
  pod=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pod"])' "$spec_json")
  out_file="$tmpdir/results-${acct}.json"
  spec_b64=$(printf '%s' "$spec_json" | base64 | tr -d '\n')
  if kubectl exec -i -n carher -c litellm "$pod" -- \
    env SPEC_B64="$spec_b64" USAGE_RETRY="$RETRY" USAGE_HTTP_TIMEOUT="$HTTP_TIMEOUT" python3 - \
    > "$out_file" 2>"$tmpdir/${acct}-inline-exec.err" <<'PY'
import base64, json, os, time, urllib.error, urllib.request

spec = json.loads(base64.b64decode(os.environ["SPEC_B64"]).decode())
retry = int(os.environ.get("USAGE_RETRY", "3"))
timeout = int(os.environ.get("USAGE_HTTP_TIMEOUT", "10"))
usage_url = "https://chatgpt.com/backend-api/codex/usage"
ua = "codex_cli_rs/0.30.0 (Linux; x86_64)"

def emit(ok, usage=None, error=None):
    print(json.dumps([{
        "acct": spec["name"],
        "source": spec["source"],
        "auth_path": spec["auth_path"],
        "container": spec.get("container", "litellm"),
        "ok": ok,
        "error": error,
        "usage": usage,
    }], ensure_ascii=False, indent=2))

try:
    with open(spec["auth_path"]) as f:
        auth = json.load(f)
    token = auth.get("access_token") or ""
    account_id = auth.get("account_id") or auth.get("accountId") or ""
    if not token:
        emit(False, error="auth.json missing access_token")
        raise SystemExit(0)
    req = urllib.request.Request(usage_url, headers={
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-ID": account_id,
        "Originator": "codex_cli_rs",
        "User-Agent": ua,
    })
    last = None
    for attempt in range(1, retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                emit(True, usage=json.loads(r.read()))
                raise SystemExit(0)
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode(errors="ignore")
            if e.code == 401:
                emit(False, error="401 token_invalidated")
                raise SystemExit(0)
            last = f"{e.code}: {body[:80]}"
        except Exception as e:
            last = str(e)[:160]
        if attempt < retry:
            time.sleep(2 * attempt)
    emit(False, error=f"inline usage probe failed: {last}")
except SystemExit:
    raise
except Exception as e:
    emit(False, error=f"inline usage probe failed: {str(e)[:160]}")
PY
  then
    echo "$out_file"
    return
  fi
  python3 - "$out_file" "$spec_json" "$tmpdir/${acct}-inline-exec.err" <<'PY'
import json, sys
out, spec_s, err_path = sys.argv[1:4]
spec = json.loads(spec_s)
err = open(err_path).read().strip() or "kubectl exec inline usage probe failed"
json.dump([{"acct": spec["name"], "source": spec["source"], "auth_path": spec["auth_path"], "container": spec["container"], "ok": False, "error": f"probe_channel_eof: {err}", "usage": None}], open(out, "w"), ensure_ascii=False, indent=2)
PY
  echo "$out_file"
}

_malaysia_scp() {
  local ssh_target="$1" local_path="$2" remote_path="$3"
  if [[ "$ssh_target" == jms:* ]]; then
    local asset="${ssh_target#jms:}"
    jms scp "$local_path" "$asset:$remote_path" </dev/null 2>&1
  else
    scp -q "$local_path" "$ssh_target:$remote_path" </dev/null 2>&1
  fi
}

_malaysia_ssh() {
  local ssh_target="$1"; shift
  if [[ "$ssh_target" == jms:* ]]; then
    local asset="${ssh_target#jms:}"
    jms ssh "$asset" "$@" </dev/null
  else
    ssh "$ssh_target" "$@" </dev/null
  fi
}

run_malaysia_probe_batch() {
  local ssh_target="$1" specs_json="$2"
  local out_file="$tmpdir/results-malaysia-${ssh_target//[:@.]/_}.json"
  local spec_file="$tmpdir/specs-malaysia-${ssh_target//[:@.]/_}.json"
  local remote_spec="/tmp/chatgpt-usage-specs-malaysia.json"
  printf '%s' "$specs_json" > "$spec_file"
  if ! _malaysia_scp "$ssh_target" "$PY_LOCAL" "$PY_REMOTE" >"$tmpdir/malaysia-batch-scp-py.err" 2>&1; then
    python3 - "$out_file" "$specs_json" "$tmpdir/malaysia-batch-scp-py.err" <<'PY'
import json, sys
out, specs_s, err_path = sys.argv[1:4]
specs = json.loads(specs_s)
err = open(err_path).read().strip() or "scp probe script to remote node failed"
json.dump([{"acct": s["name"], "source": s["source"], "auth_path": s["auth_path"], "container": s["container"], "ok": False, "error": err, "usage": None} for s in specs], open(out, "w"), ensure_ascii=False, indent=2)
PY
    echo "$out_file"
    return
  fi
  if ! _malaysia_scp "$ssh_target" "$spec_file" "$remote_spec" >"$tmpdir/malaysia-batch-scp-spec.err" 2>&1; then
    python3 - "$out_file" "$specs_json" "$tmpdir/malaysia-batch-scp-spec.err" <<'PY'
import json, sys
out, specs_s, err_path = sys.argv[1:4]
specs = json.loads(specs_s)
err = open(err_path).read().strip() or "scp spec to remote node failed"
json.dump([{"acct": s["name"], "source": s["source"], "auth_path": s["auth_path"], "container": s["container"], "ok": False, "error": err, "usage": None} for s in specs], open(out, "w"), ensure_ascii=False, indent=2)
PY
    echo "$out_file"
    return
  fi
  if ! _malaysia_ssh "$ssh_target" "USAGE_RETRY='$RETRY' USAGE_HTTP_TIMEOUT='$HTTP_TIMEOUT' USAGE_JSON=1 USAGE_ACCOUNT_SPECS_FILE='$remote_spec' python3 '$PY_REMOTE'" > "$out_file" 2>"$tmpdir/malaysia-batch-ssh.err"; then
    python3 - "$out_file" "$specs_json" "$tmpdir/malaysia-batch-ssh.err" <<'PY'
import json, sys
out, specs_s, err_path = sys.argv[1:4]
specs = json.loads(specs_s)
err = open(err_path).read().strip() or "ssh remote usage probe failed"
json.dump([{"acct": s["name"], "source": s["source"], "auth_path": s["auth_path"], "container": s["container"], "ok": False, "error": err, "usage": None} for s in specs], open(out, "w"), ensure_ascii=False, indent=2)
PY
  fi
  echo "$out_file"
}

run_malaysia_probe_one() {
  local spec_json="$1"
  local acct ssh out_file spec_file remote_spec
  acct=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$spec_json")
  ssh=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["ssh"])' "$spec_json")
  out_file="$tmpdir/results-${acct}.json"
  spec_file="$tmpdir/spec-${acct}.json"
  remote_spec="/tmp/chatgpt-usage-spec-${acct}.json"
  python3 -c 'import json,sys; s=json.loads(sys.argv[1]); s.pop("ssh", None); print(json.dumps([s], ensure_ascii=False))' "$spec_json" > "$spec_file"
  if ! _malaysia_scp "$ssh" "$PY_LOCAL" "$PY_REMOTE" >"$tmpdir/${acct}-malaysia-scp-py.err" 2>&1; then
    python3 - "$out_file" "$spec_json" "$tmpdir/${acct}-malaysia-scp-py.err" <<'PY'
import json, sys
out, spec_s, err_path = sys.argv[1:4]
spec = json.loads(spec_s)
err = open(err_path).read().strip() or "scp probe script to Malaysia node failed"
json.dump([{"acct": spec["name"], "source": spec["source"], "auth_path": spec["auth_path"], "container": spec["container"], "ok": False, "error": err, "usage": None}], open(out, "w"), ensure_ascii=False, indent=2)
PY
    echo "$out_file"
    return
  fi
  if ! _malaysia_scp "$ssh" "$spec_file" "$remote_spec" >"$tmpdir/${acct}-malaysia-scp-spec.err" 2>&1; then
    python3 - "$out_file" "$spec_json" "$tmpdir/${acct}-malaysia-scp-spec.err" <<'PY'
import json, sys
out, spec_s, err_path = sys.argv[1:4]
spec = json.loads(spec_s)
err = open(err_path).read().strip() or "scp usage spec to Malaysia node failed"
json.dump([{"acct": spec["name"], "source": spec["source"], "auth_path": spec["auth_path"], "container": spec["container"], "ok": False, "error": err, "usage": None}], open(out, "w"), ensure_ascii=False, indent=2)
PY
    echo "$out_file"
    return
  fi
  if ! _malaysia_ssh "$ssh" "USAGE_RETRY='$RETRY' USAGE_HTTP_TIMEOUT='$HTTP_TIMEOUT' USAGE_JSON=1 USAGE_ACCOUNT_SPECS_FILE='$remote_spec' python3 '$PY_REMOTE'" > "$out_file" 2>"$tmpdir/${acct}-malaysia-ssh.err"; then
    python3 - "$out_file" "$spec_json" "$tmpdir/${acct}-malaysia-ssh.err" <<'PY'
import json, sys
out, spec_s, err_path = sys.argv[1:4]
spec = json.loads(spec_s)
err = open(err_path).read().strip() or "ssh Malaysia usage probe failed"
json.dump([{"acct": spec["name"], "source": spec["source"], "auth_path": spec["auth_path"], "container": spec["container"], "ok": False, "error": err, "usage": None}], open(out, "w"), ensure_ascii=False, indent=2)
PY
  fi
  echo "$out_file"
}

write_missing_malaysia_results() {
  local configured_numbers_json="$1"
  local expected_numbers_json="$2"
  local out_file="$tmpdir/results-malaysia-missing.json"
  python3 - "$out_file" "$configured_numbers_json" "$expected_numbers_json" <<'PY'
import json, sys
out_path, configured_s, expected_s = sys.argv[1:4]
configured = set(json.loads(configured_s))
expected = set(json.loads(expected_s))
items = []
for n in sorted(expected - configured):
    name = f"acct-{n}"
    auth_path = f"/Data/chatgpt-auth/{name}/auth.json"
    items.append({
        "acct": name,
        "source": "remote:unconfigured",
        "auth_path": auth_path,
        "container": f"litellm-chatgpt-{n}",
        "ok": False,
        "error": "malaysia ssh spec not configured; set USAGE_MALAYSIA_SSH_SPECS",
        "usage": None,
    })
json.dump(items, open(out_path, "w"), ensure_ascii=False, indent=2)
PY
  if [[ -s "$out_file" ]] && python3 - "$out_file" <<'PY'
import json, sys
raise SystemExit(0 if json.load(open(sys.argv[1])) else 1)
PY
  then
    echo "$out_file"
  fi
}

ALIYUN_SPECS="$(discover_aliyun_specs)"
ALIYUN_NUMBERS="$(python3 -c 'import json,sys; print(json.dumps([int(x["name"].split("-")[1]) for x in json.loads(sys.stdin.read())]))' <<<"$ALIYUN_SPECS")"
MALAYSIA_SPECS="$(discover_malaysia_specs)"
MALAYSIA_NUMBERS="$(python3 -c 'import json,sys; print(json.dumps([int(x["name"].split("-")[1]) for x in json.loads(sys.stdin.read())]))' <<<"$MALAYSIA_SPECS")"
MALAYSIA_EXPECTED_NUMBERS="$(malaysia_expected_numbers)"
SKIP_188_NUMBERS="$(python3 - "$ALIYUN_NUMBERS" "$MALAYSIA_EXPECTED_NUMBERS" <<'PY'
import json, sys
print(json.dumps(sorted(set(json.loads(sys.argv[1])) | set(json.loads(sys.argv[2])))))
PY
)"
SPECS_188="$(discover_188_specs "$SKIP_188_NUMBERS")"

log "[chatgpt-acct-usage] source discovery: 188=$(python3 -c 'import json,sys; print(len(json.loads(sys.stdin.read())))' <<<"$SPECS_188") aliyun=$(python3 -c 'import json,sys; print(len(json.loads(sys.stdin.read())))' <<<"$ALIYUN_SPECS") malaysia=$(python3 -c 'import json,sys; print(len(json.loads(sys.stdin.read())))' <<<"$MALAYSIA_SPECS")"

result_files=()
if [[ "$SPECS_188" != "[]" ]]; then
  result_files+=("$(run_188_probe "$SPECS_188")")
fi

missing_malaysia_result="$(write_missing_malaysia_results "$MALAYSIA_NUMBERS" "$MALAYSIA_EXPECTED_NUMBERS" || true)"
if [[ -n "$missing_malaysia_result" ]]; then
  result_files+=("$missing_malaysia_result")
fi

if [[ -z "$SKIP_MALAYSIA" && "$MALAYSIA_SPECS" != "[]" ]]; then
  # Group by SSH target, batch all accounts on the same host into one SSH call
  while IFS= read -r group_line; do
    [[ -z "$group_line" ]] && continue
    ssh_target=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["ssh"])' "$group_line")
    group_specs=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["specs"])' "$group_line")
    group_names=$(python3 -c 'import json,sys; print(",".join(x["name"] for x in json.loads(sys.argv[1])))' "$group_specs")
    log "[chatgpt-acct-usage] probing $group_names from remote:$ssh_target"
    result_files+=("$(run_malaysia_probe_batch "$ssh_target" "$group_specs")")
  done < <(python3 -c '
import json, sys
from collections import defaultdict
specs = json.loads(sys.stdin.read())
groups = defaultdict(list)
for s in specs:
    groups[s["ssh"]].append(s)
for ssh, items in groups.items():
    clean = [dict(s, **{"ssh": None}) for s in items]
    for c in clean:
        c.pop("ssh", None)
    print(json.dumps({"ssh": ssh, "specs": json.dumps(clean, ensure_ascii=False)}, ensure_ascii=False))
' <<<"$MALAYSIA_SPECS")
fi

if [[ -z "$SKIP_ALIYUN" && "$ALIYUN_SPECS" != "[]" ]]; then
  pids=()
  aliyun_specs_for_retry=()
  while IFS= read -r spec; do
    [[ -z "$spec" ]] && continue
    acct=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$spec")
    source=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source"])' "$spec")
    log "[chatgpt-acct-usage] probing $acct from $source"
    result_files+=("$tmpdir/results-${acct}.json")
    aliyun_specs_for_retry+=("$spec")
    run_aliyun_probe_one "$spec" >/dev/null &
    pids+=("$!")
    while (( $(jobs -pr | wc -l | tr -d ' ') >= ALIYUN_JOBS )); do
      sleep 0.2
    done
  done < <(python3 -c 'import json,sys; [print(json.dumps(x, ensure_ascii=False)) for x in json.loads(sys.stdin.read())]' <<<"$ALIYUN_SPECS")
  for pid in "${pids[@]}"; do
    wait "$pid"
  done
  for spec in "${aliyun_specs_for_retry[@]}"; do
    acct=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$spec")
    out_file="$tmpdir/results-${acct}.json"
    if python3 - "$out_file" <<'PY'
import json, sys
try:
    item = json.load(open(sys.argv[1]))[0]
except Exception:
    sys.exit(0)
err = item.get("error") or ""
sys.exit(0 if "EOF" in err else 1)
PY
    then
      log "[chatgpt-acct-usage] retrying $acct serially after kubectl EOF"
      run_aliyun_probe_one "$spec" >/dev/null
      if python3 - "$out_file" <<'PY'
import json, sys
try:
    item = json.load(open(sys.argv[1]))[0]
except Exception:
    sys.exit(0)
err = item.get("error") or ""
sys.exit(0 if "EOF" in err else 1)
PY
      then
        log "[chatgpt-acct-usage] falling back to inline usage probe for $acct after repeated kubectl EOF"
        run_aliyun_inline_usage_fallback "$spec" >/dev/null
      fi
    fi
  done
fi

combined="$tmpdir/results-combined.json"
python3 - "$combined" "${result_files[@]}" <<'PY'
import json, sys
out_path = sys.argv[1]
items = []
for path in sys.argv[2:]:
    with open(path) as f:
        items.extend(json.load(f))
def acct_num(item):
    return int(item["acct"].split("-")[1])

def source_group(item):
    return (item.get("source") or "").split(":", 1)[0]

items.sort(key=lambda x: (source_group(x), acct_num(x)))
with open(out_path, "w") as f:
    json.dump(items, f, ensure_ascii=False, indent=2)
PY

if [[ -n "$JSON" ]]; then
  cat "$combined"
else
  USAGE_RENDER_INPUT="$combined" USAGE_ALL="$ALL" python3 "$PY_LOCAL"
fi
