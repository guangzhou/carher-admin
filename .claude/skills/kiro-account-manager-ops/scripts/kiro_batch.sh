#!/usr/bin/env bash
# Kiro Account Manager 批量 BuilderId 登录。
#
# 用法：
#   ./kiro_batch.sh /path/to/batch-creds.ndjson
#
# 每行一个 JSON：{"email":..., "pw":..., "alt_pw":..., "totp":...}
# ⚠️ creds 文件含明文密码：放 /tmp，跑完删，禁 git add。
#
# 关键：每个账户前 cleanup_chrome()。pkill -f(SIGTERM) 不够快，
# 新 Chrome 会连到上一个账户还活着的 CDP 进程，拿到串台的会话。
set -uo pipefail

CREDS="${1:?需要 creds ndjson 路径}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${KIRO_PY:-$HOME/.local/kiro-auto/venv/bin/python}"
ACCTS="$HOME/Library/Application Support/.kiro-account-manager/accounts.json"

[[ -x "$PY" ]] || { echo "venv 不存在: $PY"; exit 1; }
[[ -f "$CREDS" ]] || { echo "creds 不存在: $CREDS"; exit 1; }

cleanup_chrome() {
  for port in 9222 9223; do
    pkill -9 -f "remote-debugging-port=$port" 2>/dev/null || true
  done
  sleep 2
  for port in 9222 9223; do
    for _ in $(seq 1 5); do
      lsof -i ":$port" -t >/dev/null 2>&1 || break
      pkill -9 -f "remote-debugging-port=$port" 2>/dev/null || true
      sleep 1
    done
  done
  rm -rf /tmp/kiro-chrome-* /tmp/kiro-outlook-* 2>/dev/null || true
  rm -f /tmp/outlook-used-codes-*.json 2>/dev/null || true
}

before=$("$PY" -c "import json,os;p=os.path.expanduser('$ACCTS');print(len(json.load(open(p))) if os.path.exists(p) else 0)")
echo "=== 开工：accounts.json 现有 $before 个 ==="

declare -a OK=() SKIP=() FAIL=()

while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  email=$("$PY" -c "import json,sys;print(json.loads(sys.argv[1]).get('email',''))" "$line")
  [[ -z "$email" ]] && continue

  echo
  echo "---------- $email ----------"
  cleanup_chrome

  TARGET_EMAIL="$email" \
  PW="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1]).get('pw',''))" "$line")" \
  ALT_PW="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1]).get('alt_pw',''))" "$line")" \
  TOTP_SEED="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1]).get('totp',''))" "$line")" \
  PYTHONPATH="$SCRIPT_DIR" \
    "$PY" "$SCRIPT_DIR/kiro_oneshot.py"
  rc=$?

  case $rc in
    0) OK+=("$email") ;;
    2) SKIP+=("$email") ;;
    *) FAIL+=("$email") ;;
  esac

  sleep 10   # 避免 AWS rate limit
done < "$CREDS"

cleanup_chrome
after=$("$PY" -c "import json,os;p=os.path.expanduser('$ACCTS');print(len(json.load(open(p))) if os.path.exists(p) else 0)")

echo
echo "================ 结果 ================"
echo "新增 ${#OK[@]}: ${OK[*]:-(none)}"
echo "跳过 ${#SKIP[@]}: ${SKIP[*]:-(none)}"
echo "失败 ${#FAIL[@]}: ${FAIL[*]:-(none)}"
echo "accounts.json: $before -> $after"
