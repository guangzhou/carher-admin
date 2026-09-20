#!/usr/bin/env bash
# 编译报 `cannot run C compiled programs` / 进程莫名 137 时的三腿分诊。
#
# 137 = 128+SIGKILL = 二进制在 exec 瞬间被杀，**根本没跑**。这台机上至少有三种来源，
# 长得一模一样，猜哪个都是错的，所以这里把三个阳性对照一次跑完。
#
#   腿 A  编译器/SDK 本身坏     → 在干净目录用同一套 flag 编 hello world 再跑
#   腿 B  某个目录/路径被针对   → 换到可疑目录里再编再跑
#   腿 C  brew 构建沙箱         → 只有走 brew 时才死，裸跑活
#
# 已知历史：
#   - brew configure 的 conftest 137 = 腿 C（HOMEBREW_NO_SANDBOX=1 解决）
#   - Codex GPU helper 在 /Applications/Codex.app 下 137 = 腿 B（换包名解决）
#   ⚠️ 两者形状相同来源不同，别互相套用结论。
#
# 用法: exec_kill_triage.sh [可疑目录]
set -uo pipefail

SUSPECT="${1:-}"
SDK=$(ls -d /Library/Developer/CommandLineTools/SDKs/MacOSX*.sdk 2>/dev/null | tail -1)
SRC=$(mktemp -d)/t.c
mkdir -p "$(dirname "$SRC")"
printf 'int main(void){return 42;}\n' > "$SRC"

probe() {  # probe <标签> <工作目录> [额外 flag...]
  local label="$1" dir="$2"; shift 2
  mkdir -p "$dir" 2>/dev/null || { printf '  %-34s 建不了目录，跳过\n' "$label"; return; }
  cp "$SRC" "$dir/t.c"
  if ! (cd "$dir" && clang -Os "$@" t.c -o t 2>/dev/null); then
    printf '  %-34s \033[33m编译就失败\033[0m（不是 exec 被杀）\n' "$label"; return
  fi
  ( cd "$dir" && ./t ); local rc=$?
  if [ "$rc" = 42 ]; then
    printf '  %-34s \033[32m42 = 能跑\033[0m\n' "$label"
  elif [ "$rc" = 137 ]; then
    printf '  %-34s \033[31m137 = exec 瞬间被 SIGKILL\033[0m\n' "$label"
  else
    printf '  %-34s 退出码 %s（意外）\n' "$label" "$rc"
  fi
}

echo "腿 A —— 编译器/SDK 本身（期望全 42）"
probe "裸 flag，/tmp" "$(mktemp -d)"
[ -n "$SDK" ] && probe "带 -isysroot $(basename "$SDK")" "$(mktemp -d)" \
  -isysroot "$SDK" --sysroot="$SDK" -isystem/opt/homebrew/include -L/opt/homebrew/lib
probe "家目录" "$HOME/.exec_triage_$$"
rm -rf "$HOME/.exec_triage_$$"

if [ -n "$SUSPECT" ]; then
  echo
  echo "腿 B —— 可疑目录（若这里 137 而腿 A 全 42，就是路径被针对）"
  probe "$SUSPECT" "$SUSPECT/.exec_triage_$$"
  rm -rf "$SUSPECT/.exec_triage_$$"
fi

echo
echo "腿 C —— brew 构建沙箱"
if command -v brew >/dev/null; then
  echo "  brew 的 conftest 死没死，看它自己的 config.log，别靠猜："
  for f in ~/Library/Logs/Homebrew/*/config.log; do
    [ -f "$f" ] || continue
    if grep -q 'Killed: 9' "$f" 2>/dev/null; then
      # ⚠️ config.log 不会自己清。一次失败留下的日志会永远亮红，
      #    所以必须把 mtime 打出来，让人自己判断这是"现在还在死"还是"上次的尸体"。
      printf '    \033[31m%s\033[0m ← 有 Killed: 9（该日志写于 %s）\n' \
        "$f" "$(stat -f '%Sm' -t '%m-%d %H:%M' "$f")"
      grep -m1 -B1 -A1 'Killed: 9' "$f" | sed 's/^/      /'
    fi
  done
  echo "  ⇒ 若上面有 Killed: 9 而腿 A 全 42：是沙箱。重编时加 HOMEBREW_NO_SANDBOX=1。"
  echo "  ⇒ 判"现在还死不死"只认新跑一次的日志，别读旧 config.log。"
else
  echo "  brew 不在 PATH，跳过"
fi

echo
echo "腿 D（只读）—— 本机有没有能在 exec 期发 SIGKILL 的东西"
systemextensionsctl list 2>/dev/null | grep -i 'endpoint\|\[activated' | sed 's/^/  /' || echo "  (读不到)"
echo "  注：ES 扩展存在 ≠ 它干的。要坐实必须停掉它再复测同一条腿。"
