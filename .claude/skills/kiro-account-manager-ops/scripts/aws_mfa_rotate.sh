#!/usr/bin/env bash
# AWS Builder ID 批量换 TOTP seed（加→验→切→删）。
#
# 用法：
#   ./aws_mfa_rotate.sh /tmp/rotate-creds.ndjson            # 只加新设备，不删旧的（默认）
#   DELETE_OLD=1 ./aws_mfa_rotate.sh /tmp/rotate-creds.ndjson   # 加成功后再删旧的
#
# creds 每行：{"email":..,"pw":..,"alt_pw":..,"totp":"<现有 seed，用于登录>"}
# ⚠️ 含明文密码：放 /tmp、chmod 600、跑完删、禁 git add。
#
# 新 seed 写到 /tmp/aws-mfa-rotate-<user>.json（chmod 600），**不进日志**。
# 换完要把新 seed 抄进飞书表 `2fa` 列，并用 kiro_batch.sh 跑一次确认能登。
set -uo pipefail

CREDS="${1:?需要 creds ndjson 路径}"
[[ "$CREDS" = /* ]] || CREDS="$PWD/$CREDS"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${KIRO_PY:-$HOME/.local/kiro-auto/venv/bin/python}"
DELETE_OLD="${DELETE_OLD:-}"

[[ -x "$PY" ]] || { echo "venv 不存在: ${PY}（见 SKILL.md 建 venv 一节）"; exit 1; }
[[ -f "$CREDS" ]] || { echo "creds 不存在: $CREDS"; exit 1; }

# 必须在依赖自检之前 cd 走：仓根的 operator/ 会 shadow 标准库（同 kiro_batch.sh）
cd /tmp || exit 1

if ! "$PY" -c "import playwright,pyotp,httpx,ddddocr,PIL" 2>/dev/null; then
  echo "venv 依赖不全："; "$PY" -c "import playwright,pyotp,httpx,ddddocr,PIL" 2>&1 | tail -3
  exit 1
fi

if [[ "$DELETE_OLD" == "1" ]]; then
  echo "⚠️  DELETE_OLD=1：新设备绑定成功后会删除旧设备。"
else
  echo "ℹ️  默认模式：只加新设备，**不删旧的**（旧 seed 仍可用）。要删加 DELETE_OLD=1"
fi

cleanup_chrome() {
  for port in 9222 9223; do pkill -9 -f "remote-debugging-port=$port" 2>/dev/null || true; done
  sleep 2
  for port in 9222 9223; do
    for _ in $(seq 1 5); do
      lsof -i ":$port" -t >/dev/null 2>&1 || break
      pkill -9 -f "remote-debugging-port=$port" 2>/dev/null || true
      sleep 1
    done
  done
  rm -rf /tmp/kiro-chrome-mfa-* 2>/dev/null || true
}

declare -a OK=() FAIL=()

while IFS= read -r line; do
  [[ -z "$line" ]] && continue
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
  DELETE_OLD="$DELETE_OLD" PYTHONPATH="$SCRIPT_DIR" \
    "$PY" "$SCRIPT_DIR/aws_mfa_rotate.py"

  if [[ $? -eq 0 ]]; then OK+=("$email"); else FAIL+=("$email"); fi
  sleep 10
done < "$CREDS"

cleanup_chrome

echo
echo "================ 结果 ================"
echo "成功 ${#OK[@]}: ${OK[*]:-(none)}"
echo "失败 ${#FAIL[@]}: ${FAIL[*]:-(none)}"
echo
echo "新 seed 在 /tmp/aws-mfa-rotate-*.json（chmod 600）"
echo "下一步：抄进飞书表 2fa 列 → 用 kiro_batch.sh 跑一次确认新 seed 能登 → 再考虑删旧设备"
