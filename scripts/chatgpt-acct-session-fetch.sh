#!/usr/bin/env bash
# chatgpt-acct-session-fetch.sh — 一条命令拿 chatgpt.com 的 next-auth session JSON。
#
#   ./scripts/chatgpt-acct-session-fetch.sh <N> [email] [mail_pw] [chatgpt_pw]
#
# 省略后三个参数时**自动从飞书密码表查**(表 FRVJsbGsTh9kWNtp7uycrPSYnzc / sheet 数据表,
# 列序 acct,email,chatgpt_pw,mail_pw)。所以日常只需 `... session-fetch.sh 207`。
#
# 产物: /tmp/session-acct-<N>.json (格式化) + /tmp/session-acct-<N>.raw.txt (页面原样一行)
# 退出码: 0 成功; 1 参数/凭据问题; 2 job 失败(含手机墙); 3 取回或门禁失败
#
# ## 为什么必须走浏览器(别再试 curl/Bearer)
#
# `/api/auth/session` 认的是 **`__Secure-next-auth.session-token` cookie**, 不认 OAuth Bearer。
# 2026-10-05 实测 acct-216(在扛 12k req/24h、凭据确定活): 从 acct pod 出口打
# `/api/auth/session`、`/backend-api/me`、`/backend-api/accounts/check/v4` **全 CF 403 html**,
# 而同一出口打 `/backend-api/wham/usage` 却 200 ⇒ **CF 只挡 web 路径, 放过 codex API 路径**。
# ⇒ 这个端点只能靠 EIP 节点的 headed 浏览器登录后 goto 拿, 没有 HTTP 捷径。
#
# ## 它和 auth.json 是两套东西, 别混
#
# | | auth.json (OAuth device grant) | session.json (本脚本) |
# |---|---|---|
# | 认证 | Bearer access_token + refresh_token | cookie `__Secure-next-auth.session-token` |
# | client_id | `app_EMoamEEZ73f0CkXaXp7hrann` (codex) | `app_X8zY6vW2pQ9tR3dE7nK1jL5gH` (web) |
# | 能续期 | 能(refresh grant) | **不能**, 过期只能重登 |
# | 有效期 | AT ~10d / RT 轮转 | session 3 个月, 内含 AT ~10d |
# | 用途 | litellm acct pod 上游凭据 | chatgpt.com web 端桥接(zerokey) |
#
# **本脚本只读, 不碰任何 acct pod 的 auth.json, 不改 deployment, 不 scale。**
#
# ## 已知失败面
#
# - **`/add-phone` 手机墙**: 登录成功但 OpenAI 要求绑手机 ⇒ job Failed, 日志
#   `MANUAL_PHONE_REQUIRED`。2026-10-05 实测 226-229 四个号 4/4 撞墙。本脚本会把它
#   识别出来单独报, 别误读成"脚本坏了"。接码通道见 `scripts/chatgpt-onboard/gen-phone-pool.sh`
#   (env `PHONE_NUMBER` + `SMS_API_URL`)。
# - **`account_deactivated`**: 号被停用, 救不回。
# - 取回时 `cat` 可能**成功返回空文件**(读取 pod 被驱逐) ⇒ 必须 sha256 两端对账,
#   比长度没有判别力(feedback_token_length_equality_is_not_an_install_ruler)。
set -uo pipefail

N="${1:?用法: $0 <acct编号> [email] [mail_pw] [chatgpt_pw]}"
EMAIL="${2:-}"; MPW="${3:-}"; GPW="${4:-}"
NS=carher
JOB="cgpt-onboard-session-$N"
CRED_SHEET="${CRED_SHEET:-FRVJsbGsTh9kWNtp7uycrPSYnzc}"
TIMEOUT_SEC="${SESSION_TIMEOUT:-900}"
cd "$(dirname "$0")/.."

say(){ echo "[session-fetch] $*"; }
die(){ echo "[session-fetch] FATAL $*" >&2; exit "${2:-1}"; }

# ── 0. 隧道 ────────────────────────────────────────────────────────────────
# jms proxy 的 connection-token 5min 过期但会自动续; 断了的症状是
# `TLS handshake timeout` / `connection refused`, 重起即可。
if ! kubectl get nodes >/dev/null 2>&1; then
  say "kubectl 不通, 起 jms 隧道..."
  pkill -f 'jms.*proxy' 2>/dev/null; sleep 1
  nohup scripts/jms proxy k8s-work-226 16443 172.16.1.163 6443 >/tmp/jms-session-fetch.log 2>&1 &
  for _ in $(seq 1 10); do sleep 3; kubectl get nodes >/dev/null 2>&1 && break; done
  kubectl get nodes >/dev/null 2>&1 || die "隧道起不来, 见 /tmp/jms-session-fetch.log"
fi

# ── 1. 凭据: 没给就查飞书密码表 ────────────────────────────────────────────
# ⚠ 权威源是**电子表格**那张, 不是 bitable 的密码列(旧)。
if [ -z "$EMAIL" ] || [ -z "$MPW" ] || [ -z "$GPW" ]; then
  say "凭据未给全, 查飞书密码表 acct-$N ..."
  command -v lark-cli >/dev/null || die "缺 lark-cli 且未给凭据"
  lark-cli sheets +csv-get --spreadsheet-token "$CRED_SHEET" --sheet-name '数据表' \
    --json --output-path /tmp/.sf-cred-$N.json >/dev/null 2>&1 \
    || die "读密码表失败"
  read -r EMAIL GPW MPW <<<"$(python3 - "$N" <<'PY'
import json, re, sys
n = sys.argv[1]
d = json.load(open(f'/tmp/.sf-cred-{n}.json'))
txt = d.get('annotated_csv') or d.get('data', {}).get('annotated_csv', '')
for ln in txt.split('\n'):
    # 行形如: [row=103] acct-207,email,chatgpt_pw,Mail-207-xxxx,...
    body = re.sub(r'^\[row=\d+\]\s*', '', ln)
    f = body.split(',')
    if len(f) >= 4 and f[0].strip() == f'acct-{n}':
        print(f[1].strip(), f[2].strip(), f[3].strip())
        break
PY
)"
  rm -f /tmp/.sf-cred-$N.json
  [ -z "${EMAIL:-}" ] && die "密码表里没有 acct-$N 这一行, 请显式传 email/mail_pw/chatgpt_pw"
  say "凭据已取: email=$EMAIL (密码不回显)"
fi

# ── 2. 起 job ──────────────────────────────────────────────────────────────
say "起 OAuth 登录 job (约 5~8min, 走 mail.com 取码)..."
FORCE_OTP=1 ./scripts/aliyun-eip-onboard.sh session "$N" "$EMAIL" "$MPW" "$GPW" \
  > "/tmp/session-fetch-$N.log" 2>&1 \
  || { tail -5 "/tmp/session-fetch-$N.log"; die "job 没起来" 2; }
grep -E 'eip-onboard|job\.batch' "/tmp/session-fetch-$N.log" | tail -2

# ── 3. 等 job 落地 ─────────────────────────────────────────────────────────
# ⚠ launcher 退出 ≠ job 完成; 判据只认 .status.succeeded/.failed
# (feedback_onboard_launcher_exit_is_not_job_completion)。
say "等 job 落地 (判据 .status.succeeded, 不看 launcher 退出)..."
DEADLINE=$(( $(date +%s) + TIMEOUT_SEC ))
while :; do
  S=$(kubectl -n $NS get job "$JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null)
  F=$(kubectl -n $NS get job "$JOB" -o jsonpath='{.status.failed}' 2>/dev/null)
  [ "${S:-0}" = "1" ] && { say "job Complete"; break; }
  if [ "${F:-0}" != "0" ] && [ -n "${F:-}" ]; then
    LOG=$(kubectl -n $NS logs "job/$JOB" --tail=25 2>/dev/null)
    if grep -q MANUAL_PHONE_REQUIRED <<<"$LOG"; then
      echo "$LOG" | tail -6
      die "acct-$N 撞**手机号墙**(/add-phone): 登录成功但 OpenAI 要求绑手机。
      自动化过不去 —— 需 PHONE_NUMBER + SMS_API_URL(见 scripts/chatgpt-onboard/gen-phone-pool.sh)。
      这不是脚本故障。" 2
    fi
    grep -qE 'account_deactivated' <<<"$LOG" && { echo "$LOG" | tail -4; die "acct-$N 账号已被 OpenAI 停用, 救不回" 2; }
    echo "$LOG" | tail -10
    die "job Failed, 原因见上" 2
  fi
  [ "$(date +%s)" -gt "$DEADLINE" ] && die "等了 ${TIMEOUT_SEC}s job 仍未落地" 2
  sleep 12
done

# ── 4. 页面原文 + 取回文件 ─────────────────────────────────────────────────
RAW="/tmp/session-acct-$N.raw.txt"
kubectl -n $NS logs "job/$JOB" 2>/dev/null \
  | sed -n '/SESSION_RAW_BEGIN/,/SESSION_RAW_END/p' | sed '1d;$d' > "$RAW"
[ -s "$RAW" ] && say "页面原文 → $RAW ($(wc -c < "$RAW" | tr -d ' ') bytes)"

say "从共享 PVC 取回 + sha256 两端对账..."
./scripts/aliyun-onboard-workspace.sh session "$N" || die "取回/门禁失败" 3

# 页面原文 ↔ 文件 逐字段比对(证明没有少给/被截断)
if [ -s "$RAW" ]; then
  python3 - "$N" "$RAW" <<'PY' || exit 3
import json, sys
n, rawp = sys.argv[1], sys.argv[2]
pg = json.loads(open(rawp).read().strip())
fl = json.load(open(f'/tmp/session-acct-{n}.json'))
bad = [k for k in set(pg) | set(fl) if pg.get(k) != fl.get(k)]
print('[session-fetch] 页面原文 ↔ 文件: %d 字段%s'
      % (len(pg), ' 全部逐字符相同 ✅' if not bad else ' 不符: %s ❌' % bad))
sys.exit(1 if bad else 0)
PY
fi

say "完成。产物:"
echo "  /tmp/session-acct-$N.json      (格式化)"
[ -s "$RAW" ] && echo "  $RAW   (页面原样一行)"
say "⚠️ 两者都是明文凭据(可直接访问账号)。PVC 上那份在 /work/session-acct-$N.json;"
say "   清理: ./scripts/aliyun-onboard-workspace.sh rm  +  kubectl -n $NS delete job $JOB"
