#!/usr/bin/env bash
# aliyun-acct-reauth-install.sh <N> [<N> ...]
#
# 把重新 OAuth 产出的 auth.json 装回阿里云 ACK(ns carher)的 chatgpt-acct-<N> PVC。
# 前置：./scripts/aliyun-eip-onboard.sh oauth <N> ... 已成功(job Complete)。
#
# 为什么要有这份：旧的 aliyun-acct-install-auth-on226.sh 从 **job 日志里 grep B64BEGIN/B64END**
# 抽 auth.json，而 scripts/chatgpt-onboard/chatgpt-litellm-oauth.py 现在**不再打 B64 标记块**
# (2026-09-28 实测 grep 计数 0)⇒ 那份脚本对当前 oauth.py 必然 `FAIL: 日志无 B64END`，
# 只能手工装。本脚本改从 RWX PVC 工件走(aliyun-onboard-workspace.sh auth)。
#
# 每号顺序(不许跳)：
#   1 取工件并自检(JSON 可解析 + access_token ≥1000 字符)
#   2 身份门禁：新工件 account_id == pod 内现有 account_id；邮箱 hash == 表格(可选 EXPECT_<N>)
#   3 pod 内时间戳备份 auth.json.bak-reoauth-<epoch>
#   4 kubectl cp 装回
#   5 **比 hash** 断言 refresh_token / access_token 真的换了，且新文件无 device_code_requested_at
#   6 rollout restart + status
# 验收不在本脚本里：跑完用 ./scripts/chatgpt-acct-verify-aliyun.sh <N...>(唯一判据 live will_renew)。
#
# ⚠ 判据铁律(2026-09-28 踩到)：**不许拿 access_token 长度相等/不等当装回成功的判据**。
#   209/210/211 三号新旧 token 长度恰好逐一相等；212/213 两号 token_len 都是 1685 纯属巧合。
#   长度只是长度，唯一有判别力的是 sha256 变了没有。
#
# ⚠ 本脚本**不做**镜像漂移修复。227/228/229 那次是装完 token、验收过了之后**单独一轮**
#   kubectl set image 收的 —— 混在同一次滚更里，出问题分不清是 token 还是镜像造成的。
#
# 用法：
#   ./scripts/aliyun-acct-reauth-install.sh 226
#   EXPECT_226=uxaylntbvsxfp@mail.com ./scripts/aliyun-acct-reauth-install.sh 226 227 228 229
#   DRY_RUN=1 ./scripts/aliyun-acct-reauth-install.sh 226      # 只做 1+2，不写不滚更
set -uo pipefail
NS=carher
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ $# -ge 1 ] || { echo "用法: $0 <N> [<N> ...]"; exit 64; }

FAILED=(); OK=()

for N in "$@"; do
  echo "=== acct-$N ==="
  J=/tmp/auth-acct-$N.json

  # ---- 1 取工件 + 自检(workspace.sh auth 自带 JSON/长度硬自检) ----
  if ! "$HERE/aliyun-onboard-workspace.sh" auth "$N" 2>&1 | tail -1; then
    echo "  FAIL acct-$N: 取不到 /work/auth-acct-$N.json —— OAuth 没产出工件"
    FAILED+=("$N"); continue
  fi
  [ -s "$J" ] || { echo "  FAIL acct-$N: $J 不存在或为空"; FAILED+=("$N"); continue; }

  # ---- 2 身份门禁 ----
  POD=$(kubectl -n $NS get pod -l app=chatgpt-acct-$N -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  [ -n "$POD" ] || { echo "  FAIL acct-$N: 无 pod"; FAILED+=("$N"); continue; }

  POD_ACCT=$(kubectl -n $NS exec "$POD" -c litellm -- python3 -c \
    "import json;print(json.load(open('/chatgpt-auth/auth.json')).get('account_id',''))" 2>/dev/null | tr -d '\r\n ')

  EXPECT_VAR="EXPECT_$N"; EXPECT="${!EXPECT_VAR:-}"
  if ! python3 - "$J" "$POD_ACCT" "$EXPECT" <<'PY'
import sys, json, base64, hashlib
path, pod_acct, expect = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path))
at = d.get('access_token') or ''
rt = d.get('refresh_token') or ''
if len(at) < 1000:
    print("  FAIL: access_token 太短 len=%d(空壳工件)" % len(at)); sys.exit(1)
if not rt:
    print("  FAIL: 工件没有 refresh_token"); sys.exit(1)
if d.get('device_code_requested_at') is not None:
    print("  FAIL: 新工件带 device_code_requested_at —— 这是 device-code 死循环状态，不许装回"); sys.exit(1)
new_acct = d.get('account_id') or ''
if pod_acct and new_acct != pod_acct:
    print("  FAIL: account_id 不符 pod=%s new=%s(装错号会互相作废 RT)" % (pod_acct[:8], new_acct[:8])); sys.exit(1)
claims = {}
idt = d.get('id_token') or ''
if idt.count('.') >= 2:
    b = idt.split('.')[1]; b += '=' * (-len(b) % 4)
    claims = json.loads(base64.urlsafe_b64decode(b))
email = claims.get('email', '')
if expect:
    if hashlib.sha256(email.encode()).hexdigest() != hashlib.sha256(expect.encode()).hexdigest():
        print("  FAIL: 邮箱与 EXPECT 不符(mailhash new=%s expect=%s)" % (
            hashlib.sha256(email.encode()).hexdigest()[:10],
            hashlib.sha256(expect.encode()).hexdigest()[:10])); sys.exit(1)
a = claims.get('https://api.openai.com/auth', {})
print("  GATE ok acctid=%s mailhash=%s plan=%s until=%s at_len=%d rt_len=%d" % (
    new_acct[:8], hashlib.sha256(email.encode()).hexdigest()[:10],
    a.get('chatgpt_plan_type'), a.get('chatgpt_subscription_active_until'), len(at), len(rt)))
PY
  then FAILED+=("$N"); continue; fi

  if [ "${DRY_RUN:-0}" = "1" ]; then echo "  DRY_RUN: 跳过写入/滚更"; OK+=("$N"); continue; fi

  # ---- 3 备份 ----
  TS=$(date +%s)
  kubectl -n $NS exec "$POD" -c litellm -- python3 -c \
    "import shutil;shutil.copy2('/chatgpt-auth/auth.json','/chatgpt-auth/auth.json.bak-reoauth-$TS');print('  BACKUP auth.json.bak-reoauth-$TS')" \
    || { echo "  FAIL acct-$N: 备份失败，不继续写入"; FAILED+=("$N"); continue; }

  # ---- 4 装回 ----
  kubectl -n $NS cp "$J" "$NS/$POD:/chatgpt-auth/auth.json" -c litellm \
    || { echo "  FAIL acct-$N: cp 失败(旧值仍在备份里，可回滚)"; FAILED+=("$N"); continue; }

  # ---- 5 回读断言：比 hash,不比长度 ----
  if ! kubectl -n $NS exec "$POD" -c litellm -- python3 -c '
import json, glob, hashlib, os, sys
P = "/chatgpt-auth/auth.json"
cur = json.load(open(P))
baks = sorted(glob.glob(P + ".bak-reoauth-*"))
if not baks:
    print("  FAIL: 找不到本轮备份"); sys.exit(1)
old = json.load(open(baks[-1]))
h = lambda s: hashlib.sha256((s or "").encode()).hexdigest()
rt_ch = h(cur.get("refresh_token")) != h(old.get("refresh_token"))
at_ch = h(cur.get("access_token")) != h(old.get("access_token"))
dcr = cur.get("device_code_requested_at")
print("  READBACK bak=%s rt_changed=%s at_changed=%s exp=%s new_dcr=%s old_dcr=%s" % (
    os.path.basename(baks[-1]), rt_ch, at_ch, cur.get("expires_at"), dcr,
    old.get("device_code_requested_at") is not None))
if not rt_ch:
    print("  FAIL: refresh_token 没变 —— 装回没生效(长度相等 ≠ 装进去了)"); sys.exit(1)
if dcr is not None:
    print("  FAIL: 落盘文件仍带 device_code_requested_at"); sys.exit(1)
'; then
    echo "  FAIL acct-$N: 回读断言不过；回滚 = 把最新 .bak-reoauth-* 复制回 auth.json"
    FAILED+=("$N"); continue
  fi

  # ---- 6 滚更 ----
  kubectl -n $NS rollout restart deploy/chatgpt-acct-$N
  kubectl -n $NS rollout status deploy/chatgpt-acct-$N --timeout=300s 2>&1 | tail -1
  OK+=("$N")
done

rm -f /tmp/auth-acct-*.json 2>/dev/null   # 别把明文 token 留在本地

echo "---------------------------------------------"
echo "INSTALL_OK=${OK[*]:-（none）}"
echo "INSTALL_FAILED=${FAILED[*]:-（none）}"
echo "下一步(唯一判据)：./scripts/chatgpt-acct-verify-aliyun.sh ${OK[*]:-<N>}"
[ ${#FAILED[@]} -eq 0 ] || exit 1
