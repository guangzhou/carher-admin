#!/bin/sh
# cursor_team_setup.sh — macOS/Linux 启动器:用 Cursor 自带的 Node 运行时跑 JS(零依赖)。
# 用法: ./cursor_team_setup.sh            (dry-run)
#       ./cursor_team_setup.sh --apply    (执行)
#       ./cursor_team_setup.sh --revert   (回滚)
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

exec env ELECTRON_RUN_AS_NODE=1 "$BIN" "$DIR/cursor_team_setup.js" "$@"
