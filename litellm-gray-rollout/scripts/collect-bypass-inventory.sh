#!/usr/bin/env bash
# collect-bypass-inventory.sh — 清点所有绕过 nginx、直连 prod NodePort 30402 的活消费者。
#
# 为什么要它：方案 §5.6 第 1 条规定「执行时的自动清单才是权威输入」。nginx 100% gray
# 只能证明 cc.auto-link.com.cn 入口没流量进 prod，**证明不了 prod 没有业务流量**——
# 直连 30402 的 cron / 常驻服务 / 容器对 convergence_mode 完全免疫。
# 手工维护的清单会过期，所以这里只保留「怎么扫」，每次执行现扫现用。
#
# 动了什么：**全程只读**。只跑 crontab -l / grep / ps / docker inspect,
#           不写任何远端文件、不改任何配置。无需备份，无需回滚。
#
# 输出：原始扫描证据（`# [198] ...` / `# [188] ...` 注释行）。
#       结论行（`METHOD http://HOST:30402/path  # 分类 | ...`）由人按
#       docs/bypass-consumer-disposition.md 判读后补，再喂给
#       `collect-runtime.py --bypass-inventory`。
#       本脚本**不自动生成结论行**：把 grep 命中直接当成"活消费者"就是
#       「代码存在 ⇒ 该路径被执行」，正是 CLAUDE.md 明令禁止的那一步。
#
# 用法（密码只经环境变量,绝不进命令行/进程表/shell 历史）：
#   P198=... P188=... bash collect-bypass-inventory.sh --output /root/litellm-gray-run/bypass-raw.txt
#
# ⚠️ 三条纪律,改本文件前先读：
#   1. **禁 `2>/dev/null`**：吞掉的错误会让"没扫到"伪装成"没有",而"没有旁路消费者"
#      恰恰是本脚本唯一会被引用的结论。宁可报错也不能静默。
#   2. **过滤器禁用管道尾的 `grep`**：`grep` 空输入返回 1,叠 `pipefail` + `set -e`
#      会让"这台机器没命中"变成"脚本挂了"。一律用 `sed`/`awk`（恒返回 0）。
#   3. **远端脚本里所有 `read` 必须来自管道**,不能直接读 stdin ——
#      stdin 就是脚本自己的正文,读它等于把后半段吃掉（见 memory
#      `kubectl_exec_i_eats_heredoc_stdin`,同一个坑的另一种长相）。

set -euo pipefail

OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output) OUT="${2:?--output needs a path}"; shift 2 ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

: "${P198:?env P198 (198 cltx password) required}"
: "${P188:?env P188 (188 cltx password) required}"

H198="${H198:-10.68.13.198}"
H188="${H188:-10.68.13.188}"

# ---------------------------------------------------------------- 远端脚本正文
# 用 `read -r -d ''` 装进变量,**不要**把 heredoc 直接写在 `$( )` 里：
# case 分支的 `)` 会被 bash 的命令替换括号配对逻辑数进去,报
# `syntax error near unexpected token 'done'`（2026-09-13 实测）。
# ★ 只有**一份** body,两台机共用。曾经写成 BODY198/BODY188 两份,结果 2026-09-13
#   给 198 加了体积闸、188 那份忘了加,188 立刻卡死在一个 1.5 GB 的 cron.log 上。
#   扫描器有两个副本 ⇒ 必然漂移 ⇒ 漂移的那份会因为错误的理由报"干净"。
read -r -d '' BODY <<'REBODY' || true
set -uo pipefail
sudoq() { printf '%s\n' "$S" | sudo -S -p '' "$@"; }

# 直接读 spool 目录,不逐用户 `crontab -l -u`：后者在 198 上要跑 52 次 sudo 往返
# (≈5 分钟),而且把 50 行 "no crontab for xxx" 噪声灌进 stderr。
CRONCMDS="$(
  sudoq sh -c 'cat /var/spool/cron/crontabs/* || true'
  sudoq cat /etc/crontab || true
  sudoq sh -c 'cat /etc/cron.d/* || true'
)"

# 逐个 cron 命令回溯到脚本文件,再在文件里找 3040x 端点。
# 只看 cron/systemd/进程：「仓库里存在调用 30402 的脚本」不等于「它在跑」。
printf '%s\n' "$CRONCMDS" | sed -e 's/#.*//' -e '/^[[:space:]]*$/d' | while read -r line; do
  for tok in $line; do
    case "$tok" in
      /*)
        # cron 行里以 `/` 开头的 token 不只有脚本,还有**目录**和**重定向目标**：
        #   - `run-parts --report /etc/cron.hourly` ⇒ 目录,grep/cat 它只会刷
        #     "Is a directory" 噪声;
        #   - `>> /Data/quota-engine-run/engine.db` ⇒ 2.3 GB,不设闸会被整个
        #     cat 过 ssh(2026-09-13 实测把扫描卡死在 188)。
        # 一次 stat 同时拿类型和体积,两个闸都用它。脚本不会超过 1 MB。
        meta="$(sudoq stat -c '%s|%F' "$tok" || true)"
        sz="${meta%%|*}"; kind="${meta#*|}"
        case "$kind" in regular*file) ;; *) continue ;; esac
        case "$sz" in ''|*[!0-9]*) continue ;; esac
        # 跳过要**打印出来**:静默跳过会让"没扫到"再次伪装成"没有"。
        [ "$sz" -lt 1048576 ] || { echo "SKIP-LARGE $tok (${sz} bytes)"; continue; }

        sudoq grep -HnE 'https?://[^[:space:]]*:3040[0-9]|127\.0\.0\.1:3040[0-9]|localhost:3040[0-9]' "$tok" || true

        # 跟进一层：cron 脚本调用的 py/sh 子文件,以及它 source 的 .env。
        # ⚠️ 禁用 `sed 's#.*\(/...\)#\1#'` 抓路径：前导 `.*` 贪婪,会把
        #    `/home/cltx/x.py` 截成 `/x.py`(不存在)⇒ test -f 失败 ⇒ 消费者被静默丢掉。
        #    2026-09-13 实测就是这条让 198 扫出"干净",而它明明有活消费者。
        #    改用 `tr -c` 按字符集切词,每个 token 完整,不存在截断。
        TOKENS="$(sudoq cat "$tok" | tr -c 'A-Za-z0-9_./-' '\n' | sort -u)"

        printf '%s\n' "$TOKENS" | sed -n '/^\/.*\.py$/p;/^\/.*\.sh$/p' | while read -r sub; do
          sudoq test -f "$sub" && sudoq grep -HnE 'https?://[^[:space:]]*:3040[0-9]|127\.0\.0\.1:3040[0-9]' "$sub" || true
        done

        # env 文件里的 base 覆盖 —— 决定它打 dev 30400 还是 prod 30402,
        # 是区分"在范围内"与"证伪腿"的唯一依据,不查就会把 dev 消费者误列进来。
        printf '%s\n' "$TOKENS" | sed -n '/^\/.*\.env$/p' | while read -r ef; do
          sudoq test -f "$ef" && sudoq sed -n "/3040[0-9]/s#^#ENVFILE $ef: #p" "$ef" || true
        done
        ;;
    esac
  done
done

echo "@@SYSTEMD@@"
for d in /etc/systemd/system /usr/lib/systemd/system; do
  sudoq test -d "$d" && sudoq grep -rlE ':3040[0-9]' "$d" || true
done

echo "@@PROC@@"
# 必须锚 `:3040[0-9]` 而不是裸 `3040[0-9]` —— 裸数字会把 playwright/chrome 的
# `--metrics-shmem-handle=4,i,1636920796996455457,...` 这类随机长整数捞进来
# (2026-09-13 实测 188 上捞出 5 个 chrome 进程)。端口前面一定有冒号。
# `sed -n '/sed -n/!p'` 去掉扫描命令自己 —— 否则量具会把自己算成消费者。
sudoq ps -eo args | sed -n '/:3040[0-9]/p' | sed -n '/sed -n/!p'

echo "@@DOCKER@@"
# 容器化消费者的 base 从 env 注入,脚本里看不到,必须单独问 docker。
if command -v docker >/dev/null; then
  for c in $(sudoq docker ps --format '{{.Names}}' || true); do
    sudoq docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" \
      | sed -n "/3040[0-9]/s#^#CONTAINER $c: #p"
  done
else
  echo "(no docker on this host)"
fi
REBODY

# remote <password> <host> <body>
# 密码走 stdin 第一行,远端 `read` 进 $S 后 export ——
# 不进命令行、不进远端进程表、不进 shell 历史。随后 `bash -s` 吃剩下的正文。
remote() {
  local pw="$1" host="$2" body="$3"
  { printf '%s\n' "$pw"; printf '%s\n' "$body"; } \
    | sshpass -p "$pw" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 \
        "cltx@${host}" 'IFS= read -r S; export S; bash -s'
}

scan() {
  printf '# bypass inventory (raw evidence) — captured_at=%s\n' "$(date -u '+%FT%TZ')"
  echo "# 只读扫描：198/188 的 crontab、/etc/cron.d、systemd unit、进程表、docker env。"
  echo "# 判读与处置见 docs/bypass-consumer-disposition.md。"

  echo "# ---- 198 ----"
  # `2>&1` 是为了让远端 stderr 也带上 `# [198] ` 前缀,**不是**为了隐藏它 ——
  # 不合并的话 stderr 会裸奔到终端,既分不清来自哪台机,也不会进 --output 文件。
  remote "$P198" "$H198" "$BODY" 2>&1 | sed 's/^/# [198] /'

  echo "# ---- 188 ----"
  remote "$P188" "$H188" "$BODY" 2>&1 | sed 's/^/# [188] /'
}

if [ -n "$OUT" ]; then
  d="$(dirname "$OUT")"; mkdir -p "$d"; chmod 700 "$d"
  umask 077
  scan > "$OUT"
  chmod 600 "$OUT"
  echo "wrote $OUT"
else
  scan
fi
