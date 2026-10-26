#!/usr/bin/env bash
# Cursor / Codex 桌面 App 安装包的国内镜像(198 nginx /ccdl/apps/)。在 198 上跑,幂等,只增不删旧版本。
#
# 为什么要有它(2026-10-26,用户:「有 app 的都把 app 下载好,放到服务器上供用户下载」):
#   oneclick 页上 Cursor / Codex 的下载按钮指向官方:
#     · Cursor 写死了 3.24.12 的直链 ⇒ 版本一换就过期,同事装到旧版又被自动升级冲掉补丁;
#     · Codex Windows 只有微软商店在线安装器(800KB 的 ChatGPT Installer.exe),运行时再拉 1GB msix,
#       国内/内网经常卡住,而且没法离线发。
#   Claude App 已经由 claude-oneclick/ccdl-sync.sh 镜像在 /ccdl/desktop/,这里补另外两个。
#
# 布局(稳定名永远指最新一版,页面只链稳定名):
#   /var/www/ccdl/apps/cursor/<ver>/{Cursor-mac.dmg, Cursor-windows-x64.exe, Cursor-windows-arm64.exe}
#   /var/www/ccdl/apps/codex/<ver>/{Codex-mac.dmg, Codex-windows-x64.msix, Codex-windows-arm64.msix}
#   /var/www/ccdl/apps/{Cursor-mac.dmg, Cursor-windows-x64.exe, …}  → 软链到上面
#   /var/www/ccdl/apps/versions.json   页面/巡检读版本号用
#
# 校验(全部过才切软链;下载半截/被替换的文件不许对外):
#   · 字节数 == 官方 Content-Length(Cursor/Codex mac)或 == 微软目录 MaxDownloadSize(Codex win)
#   · Codex win 另比 FE3 给的 SHA1 Digest
#   · Cursor 版本跟 **latest 更新通道**(同事 Cursor 自动升级拿的就是它;stable 下载页会落后,见
#     skill cursor-client-bundle-patch「3.27 适配」)。⚠️ 这意味着页面上的 Cursor 可能比补丁台架验过的新 ——
#     安装器 VERIFIED_VERSIONS 软闸会提醒,每日巡检 bundle_audit_daily.sh 同样看 latest,两边对齐。
#
# 用法(198): sudo bash app-mirror-sync.sh        # 两个都跟到最新
#             sudo bash app-mirror-sync.sh cursor # 只跟一个
# 依赖:同目录 msstore_msix_urls.py(装到 /usr/local/bin/carher-msstore-msix-urls.py)
set -euo pipefail
ROOT=/var/www/ccdl/apps
UA="Mozilla/5.0"
MSSTORE="${MSSTORE:-/usr/local/bin/carher-msstore-msix-urls.py}"
ONLY="${1:-all}"
mkdir -p "$ROOT"

size_of() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1"; }
remote_len() { curl -fsSIL -m 60 -A "$UA" "$1" | awk 'tolower($1)=="content-length:"{v=$2} END{print v}' | tr -d '\r'; }

# 下到 <dst>.part,按期望字节数续传;够数才改名。期望值为空 ⇒ 失败(不许「不知道多大就当下完了」)
fetch() {
  local url="$1" dst="$2" want="$3" i have
  [ -n "$want" ] || { echo "✗ 拿不到 $url 的大小" >&2; return 1; }
  if [ -s "$dst" ] && [ "$(size_of "$dst")" = "$want" ]; then echo "  = 已有 $(basename "$dst")"; return 0; fi
  for i in 1 2 3 4 5 6; do
    have=$(size_of "$dst.part" 2>/dev/null || echo 0)
    [ "$have" = "$want" ] && break
    curl -fsSL -C - -m 1800 -A "$UA" -o "$dst.part" "$url" || true
  done
  have=$(size_of "$dst.part" 2>/dev/null || echo 0)
  [ "$have" = "$want" ] || { echo "✗ $(basename "$dst") 不完整:$have / $want" >&2; return 1; }
  mv -f "$dst.part" "$dst"; echo "  ✔ $(basename "$dst")  $want 字节"
}

set_version() {   # versions.json 里改一项(python 读改写,原子替换)
  python3 - "$ROOT/versions.json" "$1" "$2" <<'PY'
import json,os,sys,datetime
p,k,v=sys.argv[1:4]
try: d=json.load(open(p))
except Exception: d={}
d[k]={"version":v,"synced_at":datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
open(p+".tmp","w").write(json.dumps(d,ensure_ascii=False,indent=1)); os.replace(p+".tmp",p)
PY
}

sync_cursor() {
  local api=https://api2.cursor.sh/updates/api/download/latest j ver url D
  j="$(curl -fsS -m 30 -A "$UA" "$api/darwin-universal/cursor")"
  ver="$(printf '%s' "$j" | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')"
  D="$ROOT/cursor/$ver"; mkdir -p "$D"
  echo "→ Cursor $ver"
  url="$(printf '%s' "$j" | python3 -c 'import json,sys;print(json.load(sys.stdin)["downloadUrl"])')"
  fetch "$url" "$D/Cursor-mac.dmg" "$(remote_len "$url")"
  for a in x64 arm64; do
    j="$(curl -fsS -m 30 -A "$UA" "$api/win32-$a-user/cursor")"
    [ "$(printf '%s' "$j" | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')" = "$ver" ] \
      || { echo "✗ Cursor win32-$a 版本和 mac 不一致,先不切" >&2; return 1; }
    url="$(printf '%s' "$j" | python3 -c 'import json,sys;print(json.load(sys.stdin)["downloadUrl"])')"
    fetch "$url" "$D/Cursor-windows-$a.exe" "$(remote_len "$url")"
  done
  ( cd "$ROOT" && for f in Cursor-mac.dmg Cursor-windows-x64.exe Cursor-windows-arm64.exe; do ln -sfn "cursor/$ver/$f" "$f"; done )
  set_version cursor "$ver"; echo "✔ Cursor 稳定链接 → $ver"
}

sync_codex() {
  local mac=https://persistent.oaistatic.com/codex-app-prod/Codex.dmg ver json D a url want sha
  # Windows 版本号以微软目录为准(mac 的 Codex.dmg 是固定名、不带版本 ⇒ 用 Last-Modified 区分)
  json="$(python3 "$MSSTORE" 9PLM9XGG6VKS)"
  ver="$(printf '%s' "$json" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["version"])')"
  D="$ROOT/codex/$ver"; mkdir -p "$D"
  echo "→ Codex $ver"
  for a in x64 arm64; do
    url="$(printf '%s' "$json" | python3 -c "import json,sys;print(next(x['url'] for x in json.load(sys.stdin) if x['arch']=='$a'))")"
    want="$(printf '%s' "$json" | python3 -c "import json,sys;print(next(x['size'] for x in json.load(sys.stdin) if x['arch']=='$a'))")"
    sha="$(printf '%s' "$json" | python3 -c "import json,sys;print(next(x['sha1_b64'] for x in json.load(sys.stdin) if x['arch']=='$a'))")"
    fetch "$url" "$D/Codex-windows-$a.msix" "$want"
    got="$(openssl dgst -sha1 -binary "$D/Codex-windows-$a.msix" | base64)"
    [ "$got" = "$sha" ] || { echo "✗ Codex-windows-$a.msix SHA1 对不上($got ≠ $sha),删掉不发" >&2; rm -f "$D/Codex-windows-$a.msix"; return 1; }
    echo "    SHA1 与微软目录一致"
  done
  # mac:固定 URL 会被官方原地覆盖 ⇒ 每次比 Content-Length + Last-Modified,变了就进新目录名
  local lm; lm="$(curl -fsSI -m 60 -A "$UA" "$mac" | awk -F': ' 'tolower($1)=="last-modified"{print $2}' | tr -d '\r')"
  echo "    Codex.dmg Last-Modified: $lm"
  fetch "$mac" "$D/Codex-mac.dmg" "$(remote_len "$mac")"
  ( cd "$ROOT" && for f in Codex-mac.dmg Codex-windows-x64.msix Codex-windows-arm64.msix; do ln -sfn "codex/$ver/$f" "$f"; done )
  set_version codex "$ver"; echo "✔ Codex 稳定链接 → $ver"
}

rc=0
case "$ONLY" in
  all) sync_cursor || rc=1; sync_codex || rc=1 ;;
  cursor) sync_cursor || rc=1 ;;
  codex) sync_codex || rc=1 ;;
  *) echo "用法: $0 [all|cursor|codex]" >&2; exit 64 ;;
esac
chmod -R a+rX "$ROOT"
exit "$rc"
