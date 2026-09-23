#!/usr/bin/env bash
# lane_recapture_sheet_pw.sh —— 重跑抓号，**密码/邮箱取飞书表的当前值**，不读 .creds。
#
# 为什么不能直接重投 cap-queue.sh：它每轮都 `val mail_pw > "$W/mail_pw"` 从
# `/Data/chatgpt-auth/acct-<N>/.creds` 覆盖密码文件，而 A 类失败的真因正是**盘上那份是
# 轮换前的旧值**（2026-09-20 用单变量对照证实：同号同代码，只换密码，`.creds` 那份落
# `www.mail.com/logout?ls=wd` + "invalid email address / password combination"，
# 飞书表那份 `reached navigator at t=0s`）。
# 所以这份脚本**绕开 .creds**，不改它 —— `.creds` 在 188 上有 20 个别人的消费者
# （onboard-chatgpt-acct.sh / re-oauth.sh / quota-rebalance.py …），改它半径太大。
#
# 判据是「盘上值 vs 表里值」，**不是表里那栏的形状**。形状判据我写过一版，次日被自己的
# 预测双向证伪（206/207/208 是 `Mail-` 形状却全过；205 是随机串形状却 A 类失败）。
#
# 足迹（只写这些，都可回滚）：
#   188:/Data/zkcaps/zkcap-<N>/{mail_pw.sheet,chatgpt_pw,out/,screenshots/,profile/,recap.log}
#   `mail_pw.sheet` 是新文件名，**不覆盖 cap-queue.sh 用的 `mail_pw`**，两边互不影响。
#   容器 `zkrecap-<N>`（--rm，跑完自动消失）。
#   不动 .creds、不动 refresh-accts.txt、不动 cap-queue.sh、不碰任何在服务的 pod。
# 回滚：rm -rf 188:/Data/zkcaps/zkcap-<N>/out  （seed 没了就等于没抓过，可重跑）
#
# 用法：
#   ./lane_recapture_sheet_pw.sh /tmp/sheet-pw.json 196 197 198
#   第一个参数是 sheet 快照 JSON（{"<N>":{"pw":...,"email":...}}），由
#   `lark-cli base +record-list --base-token RP2dbYyyxa2lQqsTRbKckQdBn6d --table-id tblpqA4qwCTvW4Nd`
#   导出后转成的。**密码只经文件与 stdin，不进 argv、不进日志**（ps 能看见 argv）。
set -euo pipefail

SHEET="${1:?第一个参数给 sheet 快照 JSON}"; shift
[ -r "$SHEET" ] || { echo "读不到 $SHEET"; exit 2; }
[ $# -gt 0 ] || { echo "后面给要重跑的号，如 196 197 198"; exit 2; }

H188="cltx@10.68.13.188"
SSH=(ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=20 "$H188")
SSH_IN=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 "$H188")
LEDGER=/Data/zkcaps/recap.log

for N in "$@"; do
  echo "==================== acct-$N ===================="

  # 表里的值：密码走 stdin 灌到 188，邮箱可以印（不是凭据）。
  PW=$(python3 -c "import json,sys;print(json.load(open('$SHEET'))['$N']['pw'])" 2>/dev/null) || {
    echo "  ❌ 表里没有 acct-$N 这一行，跳过"; continue; }
  EM=$(python3 -c "import json,sys;print(json.load(open('$SHEET'))['$N']['email'])")
  [ -n "$PW" ] || { echo "  ❌ 表里 acct-$N 的邮箱密码是空的，跳过"; continue; }
  echo "  表里: email=$EM  pw 长度=${#PW}"

  # 盘上的值只比 hash，用来说明这一轮到底改了什么（密码不出机器）。
  DH=$("${SSH[@]}" "awk -F= '/^mail_pw=/{v=substr(\$0,9);gsub(/^'\''|'\''\$/,\"\",v);printf \"%s\",v}' /Data/chatgpt-auth/acct-$N/.creds | sha256sum | cut -c1-12")
  SH=$(printf '%s' "$PW" | shasum -a 256 | cut -c1-12)
  if [ "$DH" = "$SH" ]; then
    echo "  ⚠️  盘上值与表里一致（${SH}）—— 这号的失败不是 A 类，密码换不换都一样"
  else
    echo "  盘 $DH ≠ 表 $SH ⇒ 正是 A 类，这一轮用表里的值"
  fi

  # 上一轮日志必读（踩过：日志里写着"判死号别重跑"我还是重投，白占 EIP 节点）。
  PREV=$("${SSH[@]}" "tail -3 /Data/zkcaps/zkcap-$N/last-run.log 2>/dev/null | tr '\n' '|'" || true)
  echo "  上轮结尾: ${PREV:-（无日志）}"

  # CF 通道：188 上另有 cron 的 zk-refresh-225.sh（容器 zkref-*）也在登录，
  # 同一出口 IP 上并发 CF 登录正是要防的。不限时等（方案 A），它不是我的任务不能杀。
  W=0
  while "${SSH[@]}" "docker ps --format '{{.Names}}' | grep -q '^zkref-'"; do
    [ $((W % 10)) -eq 0 ] && echo "  ⏸  等 CF 通道（refresh-225 在跑）已等 ${W} 分钟"
    sleep 60; W=$((W+1))
  done
  [ $W -gt 0 ] && echo "  ▶  通道空了（等了 ${W} 分钟）"

  # 灌密码：走 stdin，落 600 的独立文件名，不碰 cap-queue.sh 用的那份。
  printf '%s' "$PW" | "${SSH_IN[@]}" "mkdir -p /Data/zkcaps/zkcap-$N/{out,screenshots,profile} && chmod 700 /Data/zkcaps/zkcap-$N && cat > /Data/zkcaps/zkcap-$N/mail_pw.sheet && chmod 600 /Data/zkcaps/zkcap-$N/mail_pw.sheet"
  # chatgpt_pw 照旧从 .creds 取（那一栏没有证据说它也漂了，别顺手多改一个变量）。
  "${SSH[@]}" "awk -F= '/^chatgpt_pw=/{v=substr(\$0,13);gsub(/^'\''|'\''\$/,\"\",v);print v}' /Data/chatgpt-auth/acct-$N/.creds > /Data/zkcaps/zkcap-$N/chatgpt_pw && chmod 600 /Data/zkcaps/zkcap-$N/chatgpt_pw"

  # 重跑前把旧 out 挪走：cap-queue 判"已有 seed 就跳过"，留着会让下面的判据读到旧文件。
  "${SSH[@]}" "if [ -s /Data/zkcaps/zkcap-$N/out/zerokey-users.json ]; then mv /Data/zkcaps/zkcap-$N/out/zerokey-users.json /Data/zkcaps/zkcap-$N/out/zerokey-users.json.prev-\$(date +%s); fi"

  # 🔴 截图也必须挪走：下面的失败分类**只看 screenshots/ 里有没有 mailcom-fail.png**，
  # 而这个目录以前从不清。2026-09-23 实踩两次：acct-167 与 acct-175 都被判成
  # 「A（mail.com 拒登）」，翻 last-run.log 才知道真因分别是「ChatGPT 登录页 email 输入框
  # 30s 没出来」和「OTP 邮件一直没到」—— mailcom-fail.png 是上一轮留下的。
  # 分类读的是**目录状态**不是本轮事实，不清就等于拿旧证据给新失败定性。
  "${SSH[@]}" "S=/Data/zkcaps/zkcap-$N/screenshots; if [ -n \"\$(ls -A \$S 2>/dev/null)\" ]; then D=\$S.prev-\$(date +%s); mkdir -p \$D && mv \$S/* \$D/; fi"

  "${SSH[@]}" "echo \"\$(date +%H:%M:%S) ══ recap acct-$N 开始（表里密码）\" | tee -a $LEDGER"

  # 参数与 cap-queue.sh 逐项一致，只把 MAIL_LOGIN_PW_FILE 指向 .sheet 那份、MAIL_USER 用表里的。
  "${SSH[@]}" "docker rm -f zkrecap-$N >/dev/null 2>&1; timeout 900 docker run --rm --name zkrecap-$N --network host \
    -e MAIL_USER='$EM' -e MAIL_LOGIN_PW_FILE=/state/mail_pw.sheet \
    -e CHATGPT_PW_FILE=/state/chatgpt_pw \
    -e OUT_JSON=/state/out/zerokey-users.json -e ZK_USER='acct$N' \
    -e SCREENSHOT_DIR=/state/screenshots -e PROFILE_DIR=/state/profile \
    -e LOGIN_MODE=otp -e OTP_AUTO_ONLY=1 -e OTP_AUTO_MAX=180 -e OTP_FILE_WAIT=0 \
    -e FORCE_LOGIN=1 -v /Data/zkcaps/zkcap-$N:/state zerokey-capture:latest \
    > /Data/zkcaps/zkcap-$N/last-run.log 2>&1; echo rc=\$?" || true

  # 判据不是退出码，是 seed 里真有 sentinel token（照 cap-queue.sh 的判法）。
  RES=$("${SSH_IN[@]}" "python3 - /Data/zkcaps/zkcap-$N/out/zerokey-users.json" <<'PY'
import json,sys,os
p=sys.argv[1]
if not (os.path.exists(p) and os.path.getsize(p)>0):
    print("NOSEED"); raise SystemExit
d=json.load(open(p))
def walk(o):
    if isinstance(o,dict):
        if "headers" in o and "body" in o: return o
        for v in o.values():
            r=walk(v)
            if r: return r
    return None
h=(walk(d) or {}).get("headers") or {}
print("%s %d %d %s" % (os.path.getsize(p), len(h), len(h.get("cookie") or ""),
                       bool(h.get("openai-sentinel-proof-token"))))
PY
)
  if [ "$RES" = "NOSEED" ]; then
    # 失败要分类，别只说 still logged out（那句话底下藏三类，处置完全不同）。
    SHOTS=$("${SSH[@]}" "ls /Data/zkcaps/zkcap-$N/screenshots/ 2>/dev/null | tr '\n' ' '")
    OTP=$("${SSH[@]}" "grep -oE 'OTP=[0-9]+' /Data/zkcaps/zkcap-$N/last-run.log | tail -1 || true")
    WARN=$("${SSH[@]}" "grep -c 'inbox keyword never appeared' /Data/zkcaps/zkcap-$N/last-run.log || true")
    # 🔴 分类顺序：**日志里的终局信号优先，截图只能兜底**。
    # 2026-09-23 acct-175/204 实踩：`mailcom-fail.png` 是登录 mail.com 过程中某次重试留下的
    # 中间态截图，**后面的 `mailcom-inbox.png` 时间更晚** —— 收件箱明明进去了，真因是
    # 「OTP 邮件一直没到」。可老分类把 `*mailcom-fail*` 摆在第一位，于是输出
    # 「表里的密码也不对，要人工核表」，把人送去核一个根本没问题的密码。
    # 「目录里有这张图」是**过程痕迹**，不是判据；判据要取只在终局出现一次的那句话。
    # 🔴 A 类的终局信号是**那行 URL**，不是页面上那句话。"invalid email address /
    # password combination" 只印在 mailcom-fail.png 的页面上，**从来不进 last-run.log**
    # （2026-09-23 把 175/204 的截图拉回来肉眼核实：页面明写这句，日志里 grep 不到）。
    # 日志里能拿到的唯一终局证据是 `mail.com login may have failed url=…/logout?ls=wd`
    # —— 登录被打回登出页。拿页面文案当 grep 锚点 = 这条分支永远不触发。
    MAILBAD=$("${SSH[@]}" "grep -cE 'mail.com login may have failed url=.*mail\.com/logout' /Data/zkcaps/zkcap-$N/last-run.log || true")
    OTPFAIL=$("${SSH[@]}" "grep -c 'OTP fetch failed' /Data/zkcaps/zkcap-$N/last-run.log || true")
    NAVFAIL=$("${SSH[@]}" "grep -cE 'Page.goto: Timeout|wait_for_selector: Timeout' /Data/zkcaps/zkcap-$N/last-run.log || true")
    if [ "${MAILBAD:-0}" != "0" ]; then
      CLS="A（mail.com 拒登：日志里有 invalid password）—— 表里的密码也不对，要人工核表"
    elif [ "${OTPFAIL:-0}" != "0" ]; then
      if [ "${WARN:-0}" != "0" ]; then
        CLS="B'（OTP 邮件没到：inbox keyword never appeared）—— 密码没问题，重跑或等 OpenAI 发信"
      else
        CLS="B'（OTP 邮件没到：OTP fetch failed，6 次轮询都没取到）—— 密码没问题，重跑"
      fi
    elif [ -n "$OTP" ]; then
      CLS="B（有码 $OTP 但 OpenAI 侧不放行）"
    elif [ "${NAVFAIL:-0}" != "0" ]; then
      # C 还要再分一刀：**卡在 ChatGPT 登录页的 email 输入框**和「别处导航抖了」处置不同。
      # 2026-09-23 acct-167 连跑三轮同一形状：`clear_cf` 点满 23 次 turnstile，然后
      # `wait_for_selector("input[type='email']")` 30s 超时、**screenshots/ 全空**
      # （抓图那步在登录流程更后面，压根没走到）。这不是"抖了重跑就好"——同一出口 IP 上
      # CF 一直没放行，重跑只是复现。要换出口或等 CF 冷却，不是加重试次数。
      CFSTUCK=$("${SSH[@]}" "grep -c \"autocomplete='username'\" /Data/zkcaps/zkcap-$N/last-run.log || true")
      CFCLICK=$("${SSH[@]}" "grep -c 'clear_cf: clicked turnstile' /Data/zkcaps/zkcap-$N/last-run.log || true")
      if [ "${CFSTUCK:-0}" != "0" ]; then
        CLS="C1（CF 没放行：turnstile 点了 ${CFCLICK} 次，ChatGPT 登录页 email 框始终没出现）—— 换出口/等冷却，重跑无用"
      else
        CLS="C（导航/选择器超时，走 CF 那条链路抖了）—— 重跑"
      fi
    else
      case "$SHOTS" in
        *mailcom-fail*) CLS="（兜底猜测）mail.com 那步有失败截图，但日志没给终局信号 —— 自己去看 last-run.log" ;;
        *) CLS="C（原因不明）—— 自己去看 last-run.log" ;;
      esac
    fi
    echo "  ❌ 仍失败 — $CLS"
    echo "     截图: $SHOTS"
    "${SSH[@]}" "echo \"\$(date +%H:%M:%S) ❌ recap acct-$N FAIL $CLS\" | tee -a $LEDGER"
  else
    set -- $RES
    if [ "${4:-False}" = "True" ]; then
      echo "  ✅ OK  $1 bytes  headers=$2 cookie=$3 sentinel=True"
      "${SSH[@]}" "echo \"\$(date +%H:%M:%S) ✅ recap acct-$N OK $1 bytes\" | tee -a $LEDGER"
    else
      echo "  ❌ seed 缺 sentinel token（headers=$2 cookie=$3）—— 不可用"
      "${SSH[@]}" "echo \"\$(date +%H:%M:%S) ❌ recap acct-$N 缺 sentinel\" | tee -a $LEDGER"
    fi
  fi
done

echo
echo "跑完。灌装/建腿/入池仍走 lane_seed_install.sh → clone_lane_from_live.py →"
echo "lane_model_catalog.py → crg_pool_register.py（proxy pod 内）→ 独占直名实打。"
