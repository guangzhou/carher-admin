#!/usr/bin/env bash
# 看「真实的」Claude Code /model 菜单长什么样(10-25 沉淀)。
#
# 为什么要这个:settings.json 里 modelPicker 的行数 ≠ 用户看到的菜单行数。
#   10-25 用户截图 74 行(10 + 64),刷新脚本却说 26 个 —— 因为菜单 = 自带 + 网关发现 + 我们的 modelPicker 三拨叠加。
#   只数 JSON 会报假数字;唯一可信的是真起一个 claude、按 /model、往下滚到底。
#
# 用法:
#   cc-model-menu.sh                       # 用本机 ~/.claude(你自己的真实配置,只读)
#   cc-model-menu.sh <CLAUDE_CONFIG_DIR>   # 用隔离配置目录(先在里面放 settings.json)
# 输出:每行「序号. 名字 说明」+ 总行数 + 重复名字。只读:不改任何配置,结束时 Esc + kill。
set -euo pipefail
CFG="${1:-}"
BIN="$(python3 -c 'import os,shutil;print(os.path.realpath(shutil.which("claude") or ""))')"
[ -x "$BIN" ] || { echo "找不到 claude"; exit 1; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ccmenu.XXXXXX")"; trap 'tmux kill-session -t ccmenu 2>/dev/null || true; rm -rf "$WORK"' EXIT
# 新目录会弹「信任此文件夹」;用一个固定空目录 + --session-id 避开 resume 选择页
mkdir -p "$WORK/proj"
ENVP=""; [ -n "$CFG" ] && ENVP="CLAUDE_CONFIG_DIR=$CFG"
tmux kill-session -t ccmenu 2>/dev/null || true
tmux new-session -d -s ccmenu -x 200 -y 60 -c "$WORK/proj" "env $ENVP '$BIN' --session-id $(uuidgen | tr A-Z a-z)"
sleep 15
# 首次进目录会问信任:默认 No ⇒ 先 Down 再 Enter
if tmux capture-pane -t ccmenu -p | grep -qi "trust"; then tmux send-keys -t ccmenu Down; sleep 0.5; tmux send-keys -t ccmenu Enter; sleep 4; fi
tmux send-keys -t ccmenu "/model"; sleep 2; tmux send-keys -t ccmenu Enter; sleep 4
tmux capture-pane -t ccmenu -p | grep -q "Select model" || { echo "没打开 /model(看下面的屏幕)"; tmux capture-pane -t ccmenu -p | tail -15; exit 2; }
rows=""
for _ in $(seq 1 120); do
  tmux send-keys -t ccmenu Down; sleep 0.2
  rows="$rows
$(tmux capture-pane -t ccmenu -p | grep -E '^\s+[❯ ]\s*[0-9]+\.\s')"
done
tmux send-keys -t ccmenu Escape
printf '%s\n' "$rows" | sed -E 's/^[[:space:]]*❯?[[:space:]]*//; s/ ✔//' | awk 'NF' | sort -t. -k1,1n -u > "$WORK/rows"
cat "$WORK/rows"
echo "---- 共 $(wc -l < "$WORK/rows" | tr -d ' ') 行"
# 重复 = 同一个真实模型 id 出现两次(括号里那个);没括号的按名字
dup=$(sed -E 's/^[0-9]+\. +//' "$WORK/rows" | grep -oE '\(([^)]+)\)' | sort | uniq -d || true)
[ -n "$dup" ] && echo "⚠ 重复:$dup" || echo "无重复"
