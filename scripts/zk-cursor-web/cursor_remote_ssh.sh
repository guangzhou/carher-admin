#!/bin/sh
# cursor_remote_ssh.sh — macOS/Linux 启动器:用 Cursor 自带的 Node 运行时跑 JS(零依赖)。
# 引导方式与 cursor_team_setup.sh 完全一致 —— 同事机器上只要 Cursor 能跑,这个就能跑。
# 用法: ./cursor_remote_ssh.sh                      (体检)
#       ./cursor_remote_ssh.sh --host me@1.2.3.4    (预演)
#       ./cursor_remote_ssh.sh --host me@1.2.3.4 --apply
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -n "$CURSOR_BIN" ]; then
  BIN="$CURSOR_BIN"
elif [ "$(uname)" = "Darwin" ]; then
  BIN="${CURSOR_APP:-/Applications/Cursor.app}/Contents/MacOS/Cursor"
else
  for c in /usr/share/cursor/cursor /opt/cursor/cursor "$(command -v cursor 2>/dev/null)"; do
    [ -x "$c" ] && BIN="$c" && break
  done
fi

if [ ! -x "$BIN" ]; then
  echo "!! 找不到 Cursor 可执行文件(可用 CURSOR_BIN=/path/to/Cursor 指定)"
  exit 1
fi

# ELECTRON_RUN_AS_NODE=1 不能漏:漏了这台机会**弹出 Cursor 界面并挂住**,不是报错。
exec env ELECTRON_RUN_AS_NODE=1 "$BIN" "$DIR/cursor_remote_ssh.js" "$@"
