#!/usr/bin/env bash
# lane_seed_capture_queue.sh —— 在 **188 自己身上** 串行抓一批 acct 的 seed。
#
# 与 lane_seed_capture.sh 的关系：那个是单号、一次 ssh 一个号；这个是批量。
# 为什么不能简单地在本地 for 循环调那个脚本（2026-09-20 实测踩过）：
#   队列的控制流跑在本地，**每个号都重新 ssh 一次**。188 的 ssh 在 17:33 抖了
#   一分多钟，连着四个号（167/168/169/172）报 `ssh: connect ... timed out` 判 FAIL
#   —— 这四个号压根没碰过 chatgpt.com，却被记成「抓取失败」。同一次抖动还让
#   已经在跑的 165 的容器内 `Page.goto https://chatgpt.com/` 30s 超时。
#   本地网络的抖动不该算成账号的失败：一是判据被污染（FAIL 混了两种完全不同的因），
#   二是重试会拿真实的 CF 登录去补一个根本不存在的问题 —— 那是在烧号。
# 所以：**控制流必须和被控制的容器在同一台机器上**。本地只负责投递和看账本。
#
# 用法：
#   bash scripts/zk-cursor-web/lane_seed_capture_queue.sh start 165 166 167 ...
#   bash scripts/zk-cursor-web/lane_seed_capture_queue.sh status
#   bash scripts/zk-cursor-web/lane_seed_capture_queue.sh stop
#
# 账本在 188 的 /Data/zkcaps/queue.log，**断线不影响**（nohup setsid）。
# 已经有 out/zerokey-users.json 的号直接跳过，所以本脚本可以反复投同一份名单来补漏。
#
# ⚠️ 依然一次一个号（脚本头那条纪律没变：大批量并发 CF 登录会招账号风控）。
set -uo pipefail

H188="cltx@10.68.13.188"
# `-n`（stdin 接 /dev/null）不是可选项：投递 runner 那条用的是 `<<'REMOTE'` heredoc，
# 而**脚本自己也在 stdin 上**。第一条 ssh 会把 stdin 一路读到 EOF，之后任何不带 `-n`
# 的 ssh 拿到的都是空 stdin ⇒ 远端 shell 立刻收到 EOF 退出，命令一行都不跑，
# **退出码还是 0、一句回显都没有**。2026-09-20 实测：start 静默什么都没做，
# 只打印了最后那句「看进展」。同 kubectl exec -i 吃 heredoc stdin 那条纪律。
SSH="ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=25"
# 投递那条必须读 heredoc，所以单独一个不带 -n 的变量。
SSH_IN="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=25"
REMOTE_SH=/Data/zkcaps/cap-queue.sh
LEDGER=/Data/zkcaps/queue.log

# ⚠️ 远端 pgrep/pkill 的模式**必须**写成 cap[-]queue 这种自排除形式。
# 2026-09-20 踩过两次（同一个病的两面）：
#   1. 启动块里的 `pkill -f 'bash /Data/zkcaps/cap-queue.sh'` 命中的是**承载它自己的那条
#      ssh 命令串**（整串里字面含这个路径）⇒ 把自己的 shell 杀了，后面的 setsid 压根没执行，
#      而 ssh 退出码仍是 0，回显里那句「队列已起来」是硬编码的 echo ⇒ **静默失败报成功**。
#   2. `status` 的 pgrep 同样自匹配 ⇒ 队列明明没跑也永远报「▶ 队列在跑」，
#      配着空账本看了半天。假绿比红更贵。
# `cap[-]queue` 的字符类让模式本身不匹配自己那条命令行，是 ps|grep 的老办法。
PAT='/Data/zkcaps/cap[-]queue\.sh'

CMD="${1:?usage: $0 start <N...> | status | stop}"
shift || true

case "$CMD" in
status)
  $SSH "$H188" "
    if pgrep -f '$PAT' >/dev/null; then echo \"▶ 队列在跑 (pid \$(pgrep -f '$PAT' | tr '\n' ' '))\"; else echo '⏹ 队列没在跑'; fi
    docker ps --format '   我的容器: {{.Names}} {{.Status}}' | grep zkcap- || echo '   我的容器: 无'
    # 别人占着 CF 通道也要看见 —— zkref-* 是 zk-refresh-225.sh 的批量刷新
    docker ps --format '   ⚠️ 他人占用: {{.Names}} {{.Status}}' | grep zkref- || true
    echo '--- 账本 ---'; tail -40 $LEDGER 2>/dev/null || echo '(账本还不存在)'
  "
  exit $?
  ;;
stop)
  $SSH "$H188" "pkill -f '$PAT'; docker ps --format '{{.Names}}' | grep '^zkcap-' | xargs -r docker rm -f; echo '已停'"
  exit $?
  ;;
start) : ;;
*) echo "未知命令 $CMD"; exit 2 ;;
esac

[ $# -gt 0 ] || { echo "start 需要账号列表"; exit 2; }
LIST="$*"

# ---- 把远端 runner 送过去（每次覆盖，保证 188 上跑的就是仓库这份）----
$SSH_IN "$H188" "cat > $REMOTE_SH && chmod 700 $REMOTE_SH" <<'REMOTE'
#!/usr/bin/env bash
# 由 lane_seed_capture_queue.sh 投递，在 188 本地跑。别手改这份，改仓库里那份。
set -uo pipefail
LEDGER=/Data/zkcaps/queue.log
say() { echo "$(date +%H:%M:%S) $*" | tee -a "$LEDGER"; }

for N in "$@"; do
  W=/Data/zkcaps/zkcap-$N
  OUT=$W/out/zerokey-users.json
  CREDS=/Data/chatgpt-auth/acct-$N/.creds

  if [ -s "$OUT" ]; then say "⏭  acct-$N 已有 seed（$(stat -c %s "$OUT") bytes），跳过"; continue; fi
  if [ ! -f "$CREDS" ]; then say "❌ acct-$N 没有 $CREDS"; continue; fi

  # 188 上还有另一个 CF 登录的消费者：cron 的 zk-refresh-225.sh（容器名 zkref-*，
  # 一轮能跑 5 小时以上）。两边同时登录 = 同一出口 IP 上的并发 CF 登录，正是
  # 「一次一个号别并发」要防的。它不是我的任务，不能杀，只能等。
  # **不限时等待**（2026-09-20 刘国现定的方案 A）。refresh-225 是 3-6 小时一轮的常驻任务
  # （`sleep RANDOM%10800` 随机启动 + 每号间 `sleep RANDOM%150+30`，56 个号一轮跑几小时），
  # 所以「等 2 小时就放弃」会让整批号在同一处反复放弃、账本全是 ⏸ 而一个都不抓。
  # 宁可慢：并发 CF 登录招的是**新号**的风控，而这 39 个全是新号，最经不起。
  # 每 10 分钟报一次还在等，避免「没动静」和「卡死」同形。
  waited=0
  while docker ps --format '{{.Names}}' | grep -q '^zkref-'; do
    if [ $((waited % 10)) -eq 0 ]; then
      say "⏸  等 CF 通道（refresh-225: $(docker ps --format '{{.Names}}' | grep '^zkref-' | tr '\n' ' ')）已等 ${waited} 分钟 — acct-$N 排着"
    fi
    sleep 60; waited=$((waited+1))
  done
  [ $waited -gt 0 ] && say "▶  通道空了（等了 ${waited} 分钟），继续 acct-$N"

  say "══ acct-$N 开始"
  mkdir -p "$W/out" "$W/screenshots" "$W/profile"; chmod 700 "$W"

  # 剥掉 .creds 值两端的单引号（refresh-225.sh 漏掉的那一步）
  val() { awk -F= -v k="$1" '$1==k{v=substr($0,length(k)+2); gsub(/^'\''|'\''$/,"",v); print v}' "$CREDS"; }
  val mail_pw    > "$W/mail_pw";    chmod 600 "$W/mail_pw"
  val chatgpt_pw > "$W/chatgpt_pw"; chmod 600 "$W/chatgpt_pw"
  MAILU="$(val email)"

  # 新号没有 profile，第一腿（复用 profile）必然白烧满 600s ⇒ 直接 FORCE_LOGIN
  docker rm -f "zkcap-$N" >/dev/null 2>&1
  timeout 900 docker run --rm --name "zkcap-$N" --network host \
    -e MAIL_USER="$MAILU" -e MAIL_LOGIN_PW_FILE=/state/mail_pw \
    -e CHATGPT_PW_FILE=/state/chatgpt_pw \
    -e OUT_JSON=/state/out/zerokey-users.json -e ZK_USER="acct$N" \
    -e SCREENSHOT_DIR=/state/screenshots -e PROFILE_DIR=/state/profile \
    -e LOGIN_MODE=otp -e OTP_AUTO_ONLY=1 -e OTP_AUTO_MAX=180 -e OTP_FILE_WAIT=0 \
    -e FORCE_LOGIN=1 -v "$W":/state zerokey-capture:latest > "$W/last-run.log" 2>&1
  rc=$?

  if [ -s "$OUT" ]; then
    # 判据不是退出码，是 seed 里真的有 sentinel token
    py=$(python3 - "$OUT" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
def walk(o):
    if isinstance(o,dict):
        if "headers" in o and "body" in o: return o
        for v in o.values():
            r=walk(v)
            if r: return r
    return None
h=(walk(d) or {}).get("headers") or {}
print("%d %d %s" % (len(h), len(h.get("cookie") or ""),
                    bool(h.get("openai-sentinel-proof-token"))))
PY
)
    set -- $py
    if [ "${3:-False}" = "True" ]; then
      say "✅ acct-$N OK  $(stat -c %s "$OUT") bytes  headers=$1 cookie=$2 sentinel=True"
    else
      say "❌ acct-$N seed 缺 sentinel token（headers=$1 cookie=$2）—— 不可用，已保留待查"
    fi
  else
    say "❌ acct-$N FAIL rc=$rc  $(tail -3 "$W/last-run.log" | tr '\n' ' | ')"
  fi
done
say "═══ 队列跑完 ═══"
REMOTE

# ---- 起队列：nohup + setsid，本地断线不影响 ----
# 起队列和查结果**分两次 ssh**：把 `setsid ... &` 和回显放同一条命令串里时，
# ssh 会等那个后台子进程的 fd 关闭，实测整条串的 stdout 被吞光（回显一句不剩、
# 退出码仍是 0）。所以第一次只负责放手，判断留给第二次。
# 启动命令走 heredoc 而不是 ssh 的命令参数：把 `setsid ... &` 塞进 `ssh host "..."`
# 的那个双引号串里，远端 shell 对后台作业的处理时机让它在会话拆除时连带被收走
# （实测 queue.out 全空、pgrep 空，退出码却是 0）。用 `bash -s` 读 heredoc，
# 远端就是一个正常的脚本执行环境，后台作业行为与手工敲一致。
$SSH_IN "$H188" "bash -s" >/dev/null 2>&1 <<EOF
pkill -f '$PAT' 2>/dev/null
: > $LEDGER
rm -f /Data/zkcaps/queue.out
setsid bash $REMOTE_SH $LIST >> /Data/zkcaps/queue.out 2>&1 < /dev/null &
disown
exit 0
EOF
sleep 4
# 「队列已起来」这句必须由 pgrep 的真实结果决定，不许无条件 echo：
# 17:44 那次是 pkill 自匹配杀掉了整条远端 shell、setsid 从未执行，而回显照样说成功。
if $SSH "$H188" "pgrep -f '$PAT' >/dev/null"; then
  $SSH "$H188" "echo \"▶ 队列已在 188 上起来 pid=\$(pgrep -f '$PAT' | tr '\n' ' ')（本地断线不影响）\""
else
  echo "❌ 队列没起来 —— 远端 queue.out 尾部："
  $SSH "$H188" "tail -5 /Data/zkcaps/queue.out 2>/dev/null || echo '(空)'"
  exit 1
fi
echo "看进展： bash $0 status"
