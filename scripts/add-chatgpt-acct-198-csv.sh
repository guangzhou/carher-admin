#!/bin/bash
# add-chatgpt-acct-198-csv.sh — 用卖号商原生 CSV 格式调 add-chatgpt-acct-198-full.sh
#
# 解决的真坑（2026-06-29 acct-31 实证）：
#   卖号商给的格式是 `email----chatgpt_pw----mail_pw`（gpt 在前 mail 在后），
#   但 driver `add-chatgpt-acct-198-full.sh` 的签名是 `N email mail_pw chatgpt_pw`
#   （mail 在前 gpt 在后） — 顺序相反！
#   人工 copy/paste 时极易把 gpt_pw 当 mail_pw 写入 .creds → mail.com OAuth
#   "invalid email/password" 失败 → 浪费 1 轮 OAuth (~3min) 才看出根因。
#
# 用法:
#   ./add-chatgpt-acct-198-csv.sh <N> 'email----chatgpt_pw----mail_pw'
#   ./add-chatgpt-acct-198-csv.sh <N> --field-order=gpt-first  'email----chatgpt_pw----mail_pw'   # 默认
#   ./add-chatgpt-acct-198-csv.sh <N> --field-order=mail-first 'email----mail_pw----chatgpt_pw'   # 反向卖号
#
# 例:
#   ./add-chatgpt-acct-198-csv.sh 31 'bud_suscipitoy@mail.com----9Tg=Kx*KU6ev6#D3----YIl3vRKXU'
#
# 解析后会 echo 拆分结果让操作者人眼复核一次（确认 gpt_pw / mail_pw 位次），
# 再 exec driver。任何字段含 `----` 都会直接报错（无法解析），不会误传。

set -euo pipefail

# ── 默认 field order：gpt-first（绝大多数 mail.com 卖号商格式）─────────
ORDER="gpt-first"

# ── 解析 flag ────────────────────────────────────────────────────────
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --field-order=*) ORDER="${arg#*=}" ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) ARGS+=("$arg") ;;
  esac
done

[ ${#ARGS[@]} -eq 2 ] || {
  echo "usage: $0 <N> 'email----field2----field3' [--field-order=gpt-first|mail-first]" >&2
  echo "       默认 gpt-first（卖号商通用格式 email----chatgpt_pw----mail_pw）" >&2
  exit 1
}

N="${ARGS[0]}"
CSV="${ARGS[1]}"

# ── 拆分 ─────────────────────────────────────────────────────────────
# CSV 字段数必须恰好 3（含 `----` 的密码会破坏解析，提前报错）
IFS='|' read -r EMAIL F2 F3 EXTRA <<<"$(echo "$CSV" | sed 's/----/|/g')"

[ -z "${EMAIL:-}" ] || [ -z "${F2:-}" ] || [ -z "${F3:-}" ] && {
  echo "FATAL: 解析失败 — CSV 必须是 'email----field2----field3' (--- 是分隔符, 4 横线)" >&2
  echo "  拿到: EMAIL='$EMAIL' F2='$F2' F3='$F3'" >&2
  exit 2
}
[ -n "${EXTRA:-}" ] && {
  echo "FATAL: 字段过多 (≥4) — 密码里疑似含 '----', 无法自动拆分" >&2
  echo "  EMAIL='$EMAIL' F2='$F2' F3='$F3' EXTRA='$EXTRA' ..." >&2
  echo "  → 改手动调 add-chatgpt-acct-198-full.sh, 密码用 '...' 单引号包裹" >&2
  exit 3
}

# ── 按 field-order 映射到 (mail_pw, chatgpt_pw) ─────────────────────
case "$ORDER" in
  gpt-first)   CHATGPT_PW="$F2"; MAIL_PW="$F3" ;;
  mail-first)  MAIL_PW="$F2";    CHATGPT_PW="$F3" ;;
  *) echo "FATAL: 未知 --field-order=$ORDER (仅 gpt-first|mail-first)" >&2; exit 4 ;;
esac

# ── 启发式 sanity check (mail.com 邮箱 vs 字段长度) ─────────────────
# 经验值: mail.com webmail 密码 8-12 字符纯字母数字; chatgpt_pw 通常 ≥13 含特殊符号
#        若位次反了（chatgpt_pw 当 mail_pw 写入），mail_pw 字段会异常长 + 含 # @ ! 等
WARN=0
if [[ "$EMAIL" =~ @mail\.com$ ]]; then
  MLEN=${#MAIL_PW}
  if [ "$MLEN" -gt 14 ] || [[ "$MAIL_PW" =~ [^a-zA-Z0-9] ]]; then
    echo "⚠️  WARN: mail_pw='$MAIL_PW' (len=$MLEN) 看起来不像 mail.com webmail 密码" >&2
    echo "         (mail.com 通常 8-12 字符纯字母数字; 当前含特殊字符或过长)" >&2
    echo "         可能 --field-order 反了, 试 --field-order=mail-first?" >&2
    WARN=1
  fi
  GLEN=${#CHATGPT_PW}
  if [ "$GLEN" -lt 10 ]; then
    echo "⚠️  WARN: chatgpt_pw='$CHATGPT_PW' (len=$GLEN) 看起来太短 (ChatGPT 通常 ≥13)" >&2
    WARN=1
  fi
fi

# ── 人眼复核 echo ────────────────────────────────────────────────────
cat <<EOF
─── 解析结果（请人眼复核位次正确再继续）───
  acct          = acct-$N
  email         = $EMAIL
  mail_pw       = $MAIL_PW          (len=${#MAIL_PW})
  chatgpt_pw    = $CHATGPT_PW       (len=${#CHATGPT_PW})
  field-order   = $ORDER
$([ $WARN = 1 ] && echo '  ⚠️  上方有 WARN, 5s 后继续, Ctrl-C 中断' || echo '  ✅ sanity check pass')
─────────────────────────────────────────
EOF

[ $WARN = 1 ] && sleep 5

# ── exec driver ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/add-chatgpt-acct-198-full.sh" "$N" "$EMAIL" "$MAIL_PW" "$CHATGPT_PW"
