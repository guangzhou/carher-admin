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
# 下面会 cd /tmp，所以这两个必须先定成绝对路径
[[ "$CREDS" = /* ]] || CREDS="$PWD/$CREDS"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${KIRO_PY:-$HOME/.local/kiro-auto/venv/bin/python}"
ACCTS="$HOME/Library/Application Support/.kiro-account-manager/accounts.json"

[[ -x "$PY" ]] || { echo "venv 不存在: $PY（见 SKILL.md 建 venv 一节）"; exit 1; }
[[ -f "$CREDS" ]] || { echo "creds 不存在: $CREDS"; exit 1; }

# 🔴 一律在仓库外执行 python：carher-admin 仓根有 `operator/__init__.py`，
# 会 shadow 标准库 operator，`import enum` 直接 ImportError。
# 本轮没炸只因为我手动 cd /tmp 过；换人从仓根跑必挂。
# ⚠️ 这一步必须在**依赖自检之前**：否则自检自己就撞上 shadow，
# 把「依赖齐全」误报成「依赖不全」（实测踩过）。
cd /tmp || exit 1

# 🔴 「python 在」不等于「依赖齐」：本轮踩过 venv 建坏（ensurepip 失败、
# 连 pip 都没有）但 bin/python3 软链还在，跑到一半才 ModuleNotFoundError。
# 开跑前一次性验全依赖，别让 10 个号跑一半才炸。
if ! "$PY" -c "import playwright,pyotp,httpx,ddddocr,PIL" 2>/dev/null; then
  echo "venv 依赖不全（需 playwright pyotp httpx ddddocr Pillow）："
  "$PY" -c "import playwright,pyotp,httpx,ddddocr,PIL" 2>&1 | tail -3
  exit 1
fi

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

  # 一次解析出四个字段（NUL 分隔，避免密码里的换行/引号/shell 元字符出事）。
  # 之前每个字段起一个 python 进程解同一行，4 倍开销且密码多过一遍命令行。
  { IFS= read -r -d '' email; IFS= read -r -d '' pw
    IFS= read -r -d '' alt_pw; IFS= read -r -d '' totp; } < <(
    "$PY" -c '
import json, sys
r = json.loads(sys.argv[1])
for k in ("email", "pw", "alt_pw", "totp"):
    sys.stdout.write((r.get(k) or "") + "\0")
' "$line"
  )
  [[ -z "$email" ]] && continue

  echo
  echo "---------- $email ----------"
  cleanup_chrome

  TARGET_EMAIL="$email" PW="$pw" ALT_PW="$alt_pw" TOTP_SEED="$totp" \
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
