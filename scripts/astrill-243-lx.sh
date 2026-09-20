#!/bin/sh
# astrill-243-lx.sh —— 在 243 的 LXD 容器里执行命令的包装器。
#
# 为什么需要这个：243 上 `lxc exec` 会抢走 stdin，`echo pw | sudo -S` 会永久挂住。
# 正解 = SUDO_ASKPASS 临时脚本 + `sudo -A` + `lxc exec ... < /dev/null`。
#
# 用法: astrill-243-lx.sh <container> <shell-command>
# 密码: 从环境变量 ASTRILL243_PW 读；不接受命令行传参（会进 ps/history）。
#
# ⚠️ 动了啥：在 243 的 /tmp 写一个 0700 的临时 askpass 脚本，执行完立刻 rm。
#    备份：无需（不覆盖任何已有文件，文件名带 pid）。
#    回滚：无（无持久副作用）。
set -e

[ -n "$ASTRILL243_PW" ] || { echo "需要环境变量 ASTRILL243_PW" >&2; exit 2; }
[ $# -ge 2 ] || { echo "用法: $0 <container> <shell-command>" >&2; exit 2; }

C="$1"; shift
AP="/tmp/.ap-$$.sh"

# 密码经 base64 传进临时脚本再解码。
# 不能直接 echo '$ASTRILL243_PW' —— 密码里含单引号会把脚本引号拆断
# （实测踩过：243 的密码末位就是一个 '，报 Unterminated quoted string）。
PW_B64=$(printf '%s' "$ASTRILL243_PW" | base64 | tr -d '\n')
umask 077
{
  echo '#!/bin/sh'
  echo "echo $PW_B64 | base64 -d"
} > "$AP"
chmod 700 "$AP"

SUDO_ASKPASS="$AP" sudo -A lxc exec "$C" -- sh -c "$*" < /dev/null
rc=$?
rm -f "$AP"
exit $rc
