#!/usr/bin/env bash
# reoauth-one-shot.sh <N> [csv] — 一个 acct 的 re-OAuth 全流程，**只开 1 个 jms 会话**。
#
# 为什么要这个脚本(2026-08-05):
#   aliyun-eip-onboard-via-jms.sh 把 stage / apply / verify 拆成 3 个 jms 会话，再加
#   wait 一个 = 每号 4~5 个。而 jms 会话开销才是 wall-clock 的大头(同一条命令可能秒回
#   也可能爬十几分钟，见 skill chatgpt-acct-quota-lark 的成本模型)。OAuth job 本身对
#   TOTP 号只要 ~2min。所以把 apply→poll→dump 全推到 226 上一次跑完。
#
# 三条踩过的坑(都会伪装成"成功"，别去掉对应的校验):
#   1. jms ssh --tty 回显命令原文 → marker 字面量绝不能出现在命令行，一律定义在
#      远端 rh 文件里(见 reoauth-one-shot.remote.sh 头注释)
#   2. apply 的 stderr 丢进 /dev/null → Job 没建成也看不出原因，轮询空转满 540s
#      (acct-129 实证，9 分钟全废)。现在 apply 输出原样回传 + presence 校验
#   3. b64 取回必须先比长度再比摘要。cut -d= -f2 会吃掉尾部 '==' 填充，只能用 sed
#      剥第一个 '=' 之前的前缀
#
# 用法: bash scripts/reoauth-one-shot.sh 112 [/tmp/grind-creds.csv]
# 产物: /tmp/auth-acct-<N>.json  (供 scripts/add-acct-198/grinder.sh 的 PRE 分支消费)
# 退出码: 0=拿到 auth.json  2=account_deactivated  3=job 失败/无产出  4=传输校验不过
set -uo pipefail

N="${1:?usage: $0 <acct-num> [creds.csv]}"
CSV="${2:-/tmp/grind-creds.csv}"
MAXWAIT="${REOAUTH_MAXWAIT:-780}"     # 无 TOTP 的号要走 mail.com 取码，前例 ~9.5min

REPO="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$REPO/scripts/jms"
ASSET="${KVIA_ASSET:-k8s-work-226}"
NS=carher
REG=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher
IMG="$REG:zerokey-capture-aliyun-20260707-otp3"
PVC=chatgpt-onboard-work
SRC_CM="${SRC_CM:-cgpt-onboard-src-a4df8c71tgl}"   # oauth.py+toggle.py 均 == git HEAD
JOB="cgpt-onboard-oauth-$N"
ACTION=oauth
CMD='python3 /src/oauth.py'
EXTRA_ENV='- {name: GEN_ONLY, value: "1"}'

# 出口隔离: 偶数号钉 .86 / 奇数号钉 .122。普通 pod 走共享 NAT 47.84.112.136 =
# 线上 codex acct 出口，在普通 pod 里撞 CF 会污染生产出口。
if [ $((N % 2)) -eq 0 ]; then NODE=ap-southeast-1.172.16.0.86; else NODE=ap-southeast-1.172.16.16.122; fi

# X display 必须每号唯一: Job 是 hostNetwork=true，X11 的 abstract unix socket 活在
# network namespace 里 → :99 在整个节点上全局唯一。同节点若已有 Xvfb 占着 :99，
# 新 Job 直接 "Cannot establish any listening sockets / server already running"，
# 30s 后 FATAL(2026-08-05 acct-114 实证，acct-112 跑时节点还是空的所以没暴露)。
DISPNUM=$((100 + N % 800))
DISP=":$DISPNUM"

row=$(awk -F, -v n="$N" '$1==n{print;exit}' "$CSV")
[ -n "$row" ] || { echo "FATAL: acct-$N not in $CSV"; exit 1; }
EMAIL=$(echo "$row" | cut -d, -f2); MPW=$(echo "$row" | cut -d, -f3)
GPW=$(echo "$row"   | cut -d, -f4); TSEC=$(echo "$row" | cut -d, -f5)
[ -n "$EMAIL" ] && [ -n "$MPW" ] && [ -n "$GPW" ] || { echo "FATAL: acct-$N creds incomplete"; exit 1; }
b64(){ printf '%s' "$1" | base64 | tr -d '\n'; }

TPL="$REPO/scripts/reoauth-one-shot.manifest.tpl"
RH="$REPO/scripts/reoauth-one-shot.remote.sh"
[ -s "$TPL" ] || { echo "FATAL: manifest 模板缺失 $TPL"; exit 1; }
[ -s "$RH" ]  || { echo "FATAL: 远端驱动缺失 $RH"; exit 1; }

YAML=$(N="$N" NS="$NS" JOB="$JOB" NODE="$NODE" IMG="$IMG" PVC="$PVC" SRC_CM="$SRC_CM" \
       ACTION="$ACTION" CMD="$CMD" EXTRA_ENV="$EXTRA_ENV" DISP="$DISP" DISPNUM="$DISPNUM" \
       EMAIL="$(b64 "$EMAIL")" MPW="$(b64 "$MPW")" GPW="$(b64 "$GPW")" TSEC="$(b64 "$TSEC")" \
       envsubst '$N $NS $JOB $NODE $IMG $PVC $SRC_CM $ACTION $CMD $EXTRA_ENV $DISP $DISPNUM $EMAIL $MPW $GPW $TSEC' < "$TPL")
printf '%s\n' "$YAML" > /tmp/reoauth-$N.yaml

# 渲染自检: base64 必须能解回原始明文。抓的是「模板里残留 $(b64 ...) 本地求值语法」
# 这一类 —— 原 via-jms 脚本靠 unquoted heredoc 求值，抽成模板后 $( ) 变字面量，
# 渲染出的 YAML 第 8 行直接让 kubectl 解析失败，而失败信息当时被 /dev/null 吃掉，
# 表现为「Job 建不成 + 轮询空转 540s」(acct-129 实证，9 分钟全废)。
python3 - "/tmp/reoauth-$N.yaml" "$EMAIL" "$MPW" "$GPW" "$TSEC" <<'PY' || exit 1
import sys,re,base64
path,email,mpw,gpw,tsec=sys.argv[1:6]
t=open(path).read()
bad=[]
for key,want in (('EMAIL',email),('MAIL_PW',mpw),('CHATGPT_PW',gpw),('TOTP_SECRET',tsec)):
    m=re.search(rf'^\s+{key}: "?([A-Za-z0-9+/=]*)"?\s*$',t,re.M)
    if not m: bad.append(f"{key}:行未匹配"); continue
    try: got=base64.b64decode(m.group(1)).decode()
    except Exception as e: bad.append(f"{key}:b64解码失败({e})"); continue
    if got!=want: bad.append(f"{key}:回解不符")
if bad:
    print("!!!! 渲染自检失败: "+", ".join(bad)); sys.exit(1)
print("  ✓ 渲染自检通过 (4 字段 base64 往返一致)")
PY

YSHA=$(shasum -a 256 /tmp/reoauth-$N.yaml | cut -d' ' -f1)
RSHA=$(shasum -a 256 "$RH" | cut -d' ' -f1)
YB64=$(gzip -c /tmp/reoauth-$N.yaml | base64 | tr -d '\n')
RB64=$(gzip -c "$RH" | base64 | tr -d '\n')
echo "[one-shot] acct-$N node=$NODE display=$DISP src_cm=$SRC_CM maxwait=${MAXWAIT}s yaml=$(wc -c < /tmp/reoauth-$N.yaml | tr -d ' ')B"

# ── 单会话: 下发驱动+YAML → 校 sha(裸 hex，本地按 hex 匹配) → run ────────────
# jms ssh 退出码不可信(正常完成也返非零)，成败只看产物标记 → 一律 || true
#
# 传输层重试的边界: 本地看不到 sha **不等于**远端没执行 —— jms PTY 的输出捕获会整段
# 丢失(2026-08-05 acct-52 实证: 判定"未执行"后重试，实际第一次已建 job、pod 已起
# Xvfb，重建的 pod 撞上还活着的旧 Xvfb → "server already running" 42s FATAL)。
# 所以重试的安全性**不靠本地判断**，靠远端 rh.sh run 的幂等守卫:
#   已有 B64 产出 → 直接 dump 不重跑；job 还在跑 → attach 接着等；都没有 → 删 job
#   并等 pod 真的消失再 apply。
RUN_REMOTE(){
  "$JMS" ssh --tty --timeout $((MAXWAIT + 300)) "$ASSET" "
printf '%s' '$RB64' | base64 -d | gunzip > /tmp/rh.sh
printf '%s' '$YB64' | base64 -d | gunzip > /tmp/reoauth-$N.yaml
sha256sum /tmp/rh.sh /tmp/reoauth-$N.yaml
bash /tmp/rh.sh run $N $MAXWAIT
" 2>&1 | tr -d '\r' || true
}
OUT=""
for att in 1 2 3; do
  OUT=$(RUN_REMOTE)
  echo "$OUT" | grep -aq "$RSHA" && break
  echo "  ⚠ acct-$N 第 $att 次会话输出未回来($(printf '%s' "$OUT" | wc -c | tr -d ' ')B，无 sha) — 远端可能已在跑，靠幂等守卫兜，重试"
  [ "$att" = 3 ] && break
  # 清本地残留 ssh: 卡死的会话会占着 JumpServer 席位，让后续会话直接 rc=255
  for p in $(lsof -nP -iTCP:2222 2>/dev/null | awk '/ssh/{print $2}' | sort -u); do kill -9 "$p" 2>/dev/null; done
  sleep 10
done

printf '%s\n' "$OUT" > /tmp/reoauth-$N.out
echo "$OUT" | grep -aoE "RH_[A-Z0-9]+=[^~]*" | grep -avE "^RH_LAST=" | head -20
echo "$OUT" | grep -aoE "RH_LAST=.*" | head -1 | tr '~' '\n' | sed 's/^/  log| /'

# 下发完整性: 两个 sha 都必须在输出里出现(裸 hex，不含 marker 字面量 → 不受回显影响)
echo "$OUT" | grep -aq "$RSHA" || { echo "!!!! acct-$N 远端驱动下发校验不过"; exit 4; }
echo "$OUT" | grep -aq "$YSHA" || { echo "!!!! acct-$N YAML 下发校验不过"; exit 4; }
echo "$OUT" | grep -aq "RH_JOBOK=$JOB" || { echo "!!!! acct-$N Job 未建成 — 看上面 RH_APPLYOUT"; exit 3; }

if echo "$OUT" | grep -aqE "RH_DEACT=[1-9]"; then
  echo "!!!! acct-$N ACCOUNT_DEACTIVATED — 救不回，走退役流程，别重跑"
  exit 2
fi
echo "$OUT" | grep -aq "RH_HASB64=1" || { echo "!!!! acct-$N 无 auth.json 产出(看 RH_REASON / RH_LAST)"; exit 3; }

# ── 取回 b64: 先比长度(并印出差多少字符)再比摘要，最后才解码 ─────────────────
BLEN=$(echo "$OUT" | grep -aoE "RH_BLEN=[0-9]+" | tail -1 | sed 's/^[^=]*=//')
BSHA=$(echo "$OUT" | grep -aoE "RH_BSHA=[0-9a-f]{64}" | tail -1 | sed 's/^[^=]*=//')
echo "$OUT" | grep -a '^@@' | sed 's/^@@//; s/##$//' | tr -d '\n' > /tmp/reoauth-$N.b64
GOTLEN=$(wc -c < /tmp/reoauth-$N.b64 | tr -d ' ')
GOTSHA=$(shasum -a 256 /tmp/reoauth-$N.b64 | cut -d' ' -f1)
if [ "$GOTLEN" != "$BLEN" ]; then
  echo "!!!! acct-$N b64 长度不符: 收到 $GOTLEN 远端 $BLEN (差 $((GOTLEN - BLEN)) 字符)"; exit 4
fi
[ "$GOTSHA" = "$BSHA" ] || { echo "!!!! acct-$N b64 摘要不符 got=$GOTSHA want=$BSHA"; exit 4; }
base64 -d < /tmp/reoauth-$N.b64 | gunzip > /tmp/auth-acct-$N.json || { echo "!!!! acct-$N 解码失败"; exit 4; }

python3 - "/tmp/auth-acct-$N.json" <<'PY' || exit 4
import sys,json
d=json.load(open(sys.argv[1]))
at=d.get('access_token','') or (d.get('tokens') or {}).get('access_token','')
assert len(at)>1000, f"access_token too short: {len(at)}"
print(f"  ✓ 本地落盘校验通过 access_len={len(at)} rt_len={len(d.get('refresh_token') or '')}")
PY
echo "REOAUTH_OK acct-$N -> /tmp/auth-acct-$N.json"
