#!/usr/bin/env bash
# host-243-run.sh —— 把一段本地 bash 脚本拿到 243（cltx@10.68.13.243）上执行，脚本里可以用 `S <cmd>` 拿 root。
#
# 用法:
#   scripts/host-243-run.sh body.sh                    # 执行本地文件
#   scripts/host-243-run.sh - <<'EOF' ... EOF          # 从 stdin 读
#   scripts/host-243-run.sh C=astrill-4 N=3 body.sh    # 前面的 VAR=val 会以安全转义的赋值注入到脚本开头
#
# 密码: 环境变量 HOST243_PW（也认 ASTRILL243_PW / MYAI_SSH_PASSWORD）。🔴 密码末位是单引号，属于密码本身。
#       经 sshpass -e 走 env，再 base64 进远端临时 askpass（0700，退出即删）；不进 argv / history。
#
# 🔴 远端脚本是从 ssh 的 stdin 读进 `bash -s` 的 ⇒ body 里任何会读 stdin 的命令都会把后续脚本吃掉、静默截断。
#    243 上的 `lxc exec` 就会这样，必须写成 `S lxc exec ... </dev/null`。kubectl exec -i 同理。
#
# 足迹: 远端 mktemp 一个 askpass 文件，trap 删除。不改任何东西（改什么由 body 决定）。
set -euo pipefail

PW="${HOST243_PW:-${ASTRILL243_PW:-${MYAI_SSH_PASSWORD:-}}}"
[ -n "$PW" ] || { echo "需要环境变量 HOST243_PW" >&2; exit 2; }
HOST="${HOST243_ADDR:-cltx@10.68.13.243}"

ASSIGN=""
while [ $# -gt 0 ] && [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; do
  ASSIGN+="${1%%=*}=$(printf '%q' "${1#*=}")"$'\n'; shift
done
[ $# -eq 1 ] || { echo "用法: $0 [VAR=val ...] <script|->" >&2; exit 2; }
BODY_SRC="$1"

B64=$(printf '%s' "$PW" | base64 | tr -d '\n')
# 整段 payload 先放进变量：认证失败重试时 stdin 已经被读走了，要能再发一遍
PAYLOAD=$(
  printf 'set -o pipefail\n'
  printf 'AP=$(mktemp); printf "#!/bin/sh\\necho %%s | base64 -d\\n" %q > "$AP"; chmod 700 "$AP"\n' "$B64"
  printf 'export SUDO_ASKPASS="$AP"; S(){ sudo -A "$@"; }; trap '"'"'rm -f "$AP"'"'"' EXIT\n'
  printf '%s' "$ASSIGN"
  if [ "$BODY_SRC" = "-" ]; then cat; else cat "$BODY_SRC"; fi
  printf '\n'
)
# 243 的 sshd 偶尔会对正确口令回 "Failed password"（10-18~19 实测 146 次登录里有 4 次，sshd 日志里是
# preauth 阶段失败，原因没查明）。sshpass 返回 5 = 口令被拒，此时远端什么都没执行 ⇒ 只对这种情况重试。
for attempt in 1 2 3; do
  rc=0
  printf '%s' "$PAYLOAD" | SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o ServerAliveInterval=30 "$HOST" 'bash -s' || rc=$?
  [ "$rc" = 5 ] || exit "$rc"
  echo "[host-243-run] 口令被拒（第 ${attempt} 次），2s 后重试" >&2; sleep 2
done
exit 5
