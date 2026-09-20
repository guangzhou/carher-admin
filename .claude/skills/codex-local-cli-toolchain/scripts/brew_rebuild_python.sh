#!/usr/bin/env bash
# 源码重编 homebrew python，修 expat 符号错配。
#
# 三个坑全在这儿了，缺一个就卡住：
#   1. raw.githubusercontent.com 在本机 connect 挂住 ⇒ brew 拉配方会**静默卡死**，
#      看不到任何编译进程、也不报错。必须先从镜像 clone core tap。
#   2. brew 6.x 起本地 tap 默认不受信 ⇒ 不 `brew trust` 直接 Refusing to load。
#   3. brew 构建沙箱会把 configure 编出来的 conftest 在 exec 瞬间 SIGKILL，
#      报成 `cannot run C compiled programs`（config.log 里是 `Killed: 9` / 137）。
#      必须 HOMEBREW_NO_SANDBOX=1。这**不是**编译器坏，别去重装 CLT。
#
# 用法: brew_rebuild_python.sh [formula]      默认 python@3.14
#       DRY_RUN=1 brew_rebuild_python.sh      只打印将要做什么
set -euo pipefail

FORMULA="${1:-python@3.14}"
TAP_DIR=/opt/homebrew/Library/Taps/homebrew/homebrew-core
# 实测速度：aliyun 6.4s < tuna 12s。两个都可用，aliyun 曾是哑 HTTP 传输、不支持浅克隆，
# 所以这里 aliyun 不带 --depth，tuna 带。
MIRRORS=(
  "https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/homebrew-core.git|--depth=1"
  "https://mirrors.aliyun.com/homebrew/homebrew-core.git|"
)

say() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
run() { if [ "${DRY_RUN:-}" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

say "0. 前置：确认这确实是 expat 错配，而不是别的病"
if /opt/homebrew/bin/python3 -c 'import xml.parsers.expat' 2>/dev/null; then
  echo "  当前 /opt/homebrew/bin/python3 的 pyexpat 是好的。"
  echo "  如果你的症状是别的（mac_ver 空 / pip 崩），先跑 toolchain_doctor.py 看红在哪段。"
  echo "  仍要重编就设 FORCE=1。"
  [ "${FORCE:-}" = 1 ] || exit 0
fi
echo "  SDK 头文件里的 expat 版本（源码编会按这个编）："
grep -h 'XML_M\(AJOR\|INOR\|ICRO\)_VERSION' \
  /Library/Developer/CommandLineTools/SDKs/MacOSX*.sdk/usr/include/expat.h 2>/dev/null \
  | sed 's/^/    /' || echo "    (读不到 SDK 头文件)"

say "1. core tap（绕开挂住的 raw.githubusercontent.com）"
if [ -d "$TAP_DIR/Formula" ]; then
  echo "  已存在: $TAP_DIR"
else
  ok=0
  for entry in "${MIRRORS[@]}"; do
    url="${entry%%|*}"; flag="${entry##*|}"
    echo "  试 $url"
    if run "git clone $flag '$url' '$TAP_DIR'"; then ok=1; break; fi
    rm -rf "$TAP_DIR"
  done
  [ "$ok" = 1 ] || [ "${DRY_RUN:-}" = 1 ] || { echo "  所有镜像都失败"; exit 1; }
fi
FPATH="$TAP_DIR/Formula/${FORMULA:0:1}/${FORMULA}.rb"
[ -f "$FPATH" ] && echo "  配方在: $FPATH" || echo "  ⚠️ 配方没找到: $FPATH"

say "2. 信任 tap（brew 6.x 必需）"
run "HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1 brew trust homebrew/core"

say "3. 源码重编（关沙箱 —— 见文件头第 3 条）"
run "HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_FROM_API=1 HOMEBREW_NO_ENV_HINTS=1 \
     HOMEBREW_NO_SANDBOX=1 brew reinstall --build-from-source '$FORMULA'"

[ "${DRY_RUN:-}" = 1 ] && { echo; echo "dry-run 结束"; exit 0; }

say "4. 验收"
cd /tmp   # 离开仓库：repo 里的 operator/ 会遮蔽 stdlib 的 operator
/opt/homebrew/bin/python3 -c \
  "import platform,plistlib,ssl;import xml.parsers.expat as e;\
print('  ',platform.python_version(),'| mac_ver',repr(platform.mac_ver()[0]),'| expat',e.version_info)"
echo "  确认是源码编的（下面应为 0）："
echo -n "    Poured from bottle 次数: "
grep -c 'Poured from bottle' ~/Library/Logs/Homebrew/"$FORMULA"/*.log 2>/dev/null | paste -sd+ - | bc 2>/dev/null || echo 0
echo
echo "✅ 重编完成。别忘了把依赖它的包装回去，例如：/opt/homebrew/bin/python3 -m pip install --break-system-packages openai"
