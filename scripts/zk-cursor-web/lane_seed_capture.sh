#!/usr/bin/env bash
# lane_seed_capture.sh —— 在 188 上为某个 acct 重新捕获 chatgpt.com 网页会话，产出 users.json。
#
# 这是给新 lane 造登录态的第一步。**必须在 188 上跑**：cf_clearance 绑 188 的出口 IP，
# 换个机器抓出来的 session 到了别处就是 CF 挑战页。
#
# 与 web-pool-capabilities/refresh-225.sh 的关系：那个是 cron 批量刷新已有号的，
# 这个是**单号、可交互、给新号用**的。两处必须不同的地方（refresh-225.sh 的两个已知缺陷，
# 别抄它）：
#   1. **`.creds` 的值是单引号包着的**（`email='x@mail.com'`）。refresh-225.sh 的 awk 不剥引号，
#      写进 mail_pw 文件的是 `'密码'` —— 带着引号去登录，永远登不上。这里显式剥。
#   2. **它的 FORCE_LOGIN 兜底腿从未真正执行过**（第一次 run_cap 失败后文件判断的写法让第二腿
#      形同虚设）。这里把两腿写成显式的、各自有回显的两步。
#
# 用法（在本机跑，脚本自己 ssh 到 188）：
#   bash scripts/zk-cursor-web/lane_seed_capture.sh 140          # 优先复用 profile，无需 OTP
#   bash scripts/zk-cursor-web/lane_seed_capture.sh 135 --force  # 直接走完整 OTP 登录（无 profile 的号）
#
# 产出留在 188 的 /Data/zkcaps/zkcap-<N>/out/zerokey-users.json，**本脚本不自动往 225 灌**
# —— 灌 seed 是改生产面，单独一步单独判据（见 lane_seed_install.sh）。
#
# ⚠️ 大批量 CF 登录会招账号风控（new-pod.sh 原话："canary first, don't storm 40x"）。
# 一次一个号，别并发。
set -uo pipefail

N="${1:?usage: $0 <acct-N> [--force]}"
FORCE=""
[ "${2:-}" = "--force" ] && FORCE="1"

H188="cltx@10.68.13.188"
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=25"

# 远端脚本整份走 stdin，不往 ssh 命令串里塞带引号的代码。
# 凭据全程只在 188 的 700 目录里落地，不回传本地、不进 argv。
$SSH "$H188" "N=$N FORCE=$FORCE bash -s" <<'REMOTE'
set -uo pipefail
W=/Data/zkcaps/zkcap-$N
CREDS=/Data/chatgpt-auth/acct-$N/.creds
OUT=$W/out/zerokey-users.json

[ -f "$CREDS" ] || { echo "❌ 没有 $CREDS"; exit 2; }
mkdir -p "$W/out" "$W/screenshots" "$W/profile"
chmod 700 "$W"

# 剥掉值两端的单引号 —— 这一步是 refresh-225.sh 漏掉的
val() { awk -F= -v k="$1" '$1==k{v=substr($0,length(k)+2); gsub(/^'\''|'\''$/,"",v); print v}' "$CREDS"; }
val mail_pw    > "$W/mail_pw";    chmod 600 "$W/mail_pw"
val chatgpt_pw > "$W/chatgpt_pw"; chmod 600 "$W/chatgpt_pw"
MAILU="$(val email)"
echo "acct-$N email=$MAILU  mail_pw=${#},$(wc -c < "$W/mail_pw") bytes  (只回显长度)"

run_cap() {   # $1 = 额外 env（如 -e FORCE_LOGIN=1）
  docker rm -f "zkcap-$N" >/dev/null 2>&1
  timeout 600 docker run --rm --name "zkcap-$N" --network host \
    -e MAIL_USER="$MAILU" -e MAIL_LOGIN_PW_FILE=/state/mail_pw \
    -e CHATGPT_PW_FILE=/state/chatgpt_pw \
    -e OUT_JSON=/state/out/zerokey-users.json -e ZK_USER="acct$N" \
    -e SCREENSHOT_DIR=/state/screenshots -e PROFILE_DIR=/state/profile \
    -e LOGIN_MODE=otp -e OTP_AUTO_ONLY=1 -e OTP_AUTO_MAX=180 -e OTP_FILE_WAIT=0 \
    $1 -v "$W":/state zerokey-capture:latest 2>&1 | tail -25
}

rm -f "$OUT"
if [ -z "${FORCE:-}" ]; then
  echo "── 第一腿：复用持久 profile（不需要 OTP）──"
  run_cap ""
fi
if [ ! -f "$OUT" ]; then
  echo "── 第二腿：完整 OTP 登录（FORCE_LOGIN=1）──"
  run_cap "-e FORCE_LOGIN=1"
fi

if [ -f "$OUT" ]; then
  echo "✅ 抓到 $OUT  $(stat -c %s "$OUT") bytes"
  # 只回显结构，不回显 token/cookie
  python3 - "$OUT" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
def walk(o):
    if isinstance(o,dict):
        if "headers" in o and "body" in o: return o
        for v in o.values():
            r=walk(v)
            if r: return r
    return None
u=(d.get("chatgpt") or {})
print("  users key:", list(u.keys()))
pf=walk(d) or {}
h=pf.get("headers") or {}
print("  parsedFetch: url=%s  headers=%d 个  cookie=%d 字节  sentinel=%s"
      % (str(pf.get("url"))[:60], len(h), len(h.get("cookie") or ""),
         bool(h.get("openai-sentinel-proof-token"))))
PY
else
  echo "❌ 两腿都没产出 users.json —— 看上面的容器日志和 $W/screenshots/"
  exit 1
fi
REMOTE
