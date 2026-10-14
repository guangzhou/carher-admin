#!/usr/bin/env bash
# Claude 桌面 App 的国内镜像补齐(198 nginx /ccdl/)。在 198 上跑,幂等,只增不删。
#
# 为什么要有它(2026-10-14 两次手工补镜像后固化):
#   App 每次自动升级都会换一个「钉死」的 Claude Code 版本,而 App 每开一个会话都要先下载它;
#   官方 downloads.claude.ai 国内连不上 ⇒ 镜像里没有这个版本时,会话一直卡在 initializing、
#   发消息很慢或不回。新同事从文档下载的 App 安装包也要跟上最新版。
#
# 做三件事:
#   1. 镜像官方最新 App 安装包(mac zip / win x64 msix / win arm64 msix)到 desktop/<ver>/,
#      并把文档链接用的稳定名 Claude-mac.zip 等软链指过去;
#   2. 读这个 App 内嵌的 manifest,得到它钉死的 Claude Code 版本;
#   3. 镜像该 Claude Code 版本的 10 个平台文件到 claude-code-releases/<ver>/。
#   另可用 --cc <ver> 直接补某个 Claude Code 版本(同事 App 是旧版时用)。
#
# 用法(198 上): sudo bash ccdl-sync.sh            # 跟到官方最新 App
#               sudo bash ccdl-sync.sh --cc 2.1.260 # 只补一个 Claude Code 版本
set -euo pipefail
ROOT=/var/www/ccdl
UA="Mozilla/5.0"
REL=https://downloads.claude.ai/releases
CCREL=https://downloads.claude.ai/claude-code-releases

mirror_cc() {
  local V="$1" D="$ROOT/claude-code-releases/$1" p
  mkdir -p "$D"
  [ -s "$D/manifest.json" ] || curl -fsSL -m 60 -A "$UA" -o "$D/manifest.json" "$CCREL/$V/manifest.json"
  for p in darwin-arm64/claude.app.tar.zst darwin-x64/claude.app.tar.zst darwin-arm64/claude.zst darwin-x64/claude.zst \
           linux-x64/claude.zst linux-arm64/claude.zst linux-x64-musl/claude.zst linux-arm64-musl/claude.zst \
           win32-x64/claude.exe.zst win32-arm64/claude.exe.zst; do
    mkdir -p "$D/$(dirname "$p")"
    [ -s "$D/$p" ] || curl -fsSL -m 900 -A "$UA" -o "$D/$p" "$CCREL/$V/$p"
  done
  echo "✔ Claude Code $V: $(find "$D" -type f | wc -l) 个文件"
}

if [ "${1:-}" = "--cc" ]; then mirror_cc "$2"; chmod -R a+rX "$ROOT"; exit 0; fi

# 1) 最新 App
J="$(curl -fsSL -m 30 -A "$UA" "$REL/darwin/universal/RELEASES.json")"
V="$(printf '%s' "$J" | python3 -c 'import json,sys;print(json.load(sys.stdin)["currentRelease"])')"
H="$(printf '%s' "$J" | python3 -c 'import json,sys,re;u=json.load(sys.stdin)["releases"][0]["updateTo"]["url"];print(re.search(r"Claude-([0-9a-f]+)\.zip",u).group(1))')"
D="$ROOT/desktop/$V"; mkdir -p "$D"
for pe in darwin/universal:zip win32/x64:msix win32/arm64:msix; do
  pp="${pe%%:*}"; ext="${pe##*:}"; f="$(echo "$pp" | tr / -)-Claude-$H.$ext"
  [ -s "$D/$f" ] || curl -fsSL -m 900 -A "$UA" -o "$D/$f" "$REL/$pp/$V/Claude-$H.$ext"
done
echo "✔ App $V 安装包已镜像"

# 2) 这个 App 钉死的 Claude Code 版本(从 app.asar 里内嵌的 manifest 读)
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
unzip -q -o "$D/darwin-universal-Claude-$H.zip" 'Claude.app/Contents/Resources/app.asar' -d "$T"
CC="$(python3 - "$T/Claude.app/Contents/Resources/app.asar" <<'PY'
import json,re,sys
b=open(sys.argv[1],'rb').read().decode('latin1')
m=re.search(r"JSON\.parse\('(\{\"version\":\"\d+\.\d+\.\d+\",\"manifest\".*?)'\)",b)
print(json.loads(m.group(1))["manifest"]["version"] if m else "")
PY
)"
[ -n "$CC" ] || { echo "✗ 读不到 App 钉死的 Claude Code 版本(App 内部结构变了?)" >&2; exit 2; }
echo "  App $V 钉的 Claude Code = $CC"

# 3) 补 Claude Code,然后才切文档用的稳定链接(顺序别反:先有组件再放新 App 出去)
mirror_cc "$CC"
cd "$ROOT/desktop"
ln -sfn "$V/darwin-universal-Claude-$H.zip" Claude-mac.zip
ln -sfn "$V/win32-x64-Claude-$H.msix" Claude-windows-x64.msix
ln -sfn "$V/win32-arm64-Claude-$H.msix" Claude-windows-arm64.msix
chmod -R a+rX "$ROOT"
echo "✔ 文档下载链接已指向 App $V"
