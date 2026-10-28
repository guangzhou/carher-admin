#!/bin/sh
# cursor_selfheal.sh —— macOS launchd 每 5 分钟拉一次;**只负责找到 Cursor 二进制再调 JS**。
#
# 为什么这么薄(2026-10-28):第一版在这里用 shell 重写了一遍「Cursor 开着没有」「补丁在不在」
# 「防抖」—— 都是 cursor_team_setup.js 里已有的逻辑手抄一份,迟早漂(本仓已经因为 js/py
# 两份实现吃过分叉的亏)。现在判定全在 `cursor_team_setup.js --selfheal`,mac/Windows 共用;
# 这里只做 JS 做不到的那件事:在 Cursor 自己都没起来时,找到用来跑 JS 的那个二进制。
#
# 背景:Cursor 自动升级会整个换掉 app 目录 ⇒ 补丁全没 ⇒ 件C 没了就**每条消息都压缩一次**。
# 我们**不是**要关掉压缩;补丁在时 Cursor 只在快满才压,那是对的。
set -u
SETUP="$HOME/.carher/cursor-setup/cursor_team_setup.js"
APP="${CURSOR_APP:-/Applications/Cursor.app}"
BIN="$APP/Contents/MacOS/Cursor"
[ -x "$BIN" ] || exit 0          # 没装 Cursor
[ -f "$SETUP" ] || exit 0        # 没跑过我们的安装器:不是我们的机器,不动
# 副本测试(CURSOR_APP 指到别处)时,让 JS 量的是那个副本而不是 /Applications 那个
[ -n "${CURSOR_APP:-}" ] && [ -d "$APP/Contents/Resources/app" ] && export CURSOR_APP_ROOT="$APP/Contents/Resources/app"
ELECTRON_RUN_AS_NODE=1 exec "$BIN" "$SETUP" --selfheal
