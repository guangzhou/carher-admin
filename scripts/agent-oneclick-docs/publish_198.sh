#!/usr/bin/env bash
# 把一键配置单页发布到 https://chat.auto-link.com.cn/oneclick/
#
# 198 上的布置(10-15 已做好,本脚本只换页面文件):
#   静态文件  /var/www/oneclick/index.html
#   nginx     /etc/nginx/sites-enabled/chat.auto-link.com.cn.conf 里 `# >>> carher-oneclick >>>` 一段
#             (/oneclick/ 走静态,其余仍反代 Open WebUI;回滚=删这一段 + reload)
#   备份      /etc/nginx/backup-oneclick/
#
# 用法: bash scripts/agent-oneclick-docs/publish_198.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
python3 scripts/agent-oneclick-docs/build_html.py
SRC=".work/oneclick-all/AI编程一键配置.html"
TMP=".work/oneclick-all/index.html"
cp "$SRC" "$TMP"
LOCAL_SHA=$(shasum -a 256 "$TMP" | cut -d' ' -f1)

scripts/jms scp "$TMP" AIYJY-litellm:/tmp/oneclick-index.html
scripts/jms ssh AIYJY-litellm "set -e
  sudo -n cp -a /var/www/oneclick/index.html /var/www/oneclick/index.html.prev 2>/dev/null || true
  sudo -n install -m 0644 /tmp/oneclick-index.html /var/www/oneclick/index.html
  rm -f /tmp/oneclick-index.html
  sha256sum /var/www/oneclick/index.html"
rm -f "$TMP"

LIVE_SHA=$(curl -s --max-time 30 https://chat.auto-link.com.cn/oneclick/ | shasum -a 256 | cut -d' ' -f1)
if [ "$LIVE_SHA" = "$LOCAL_SHA" ]; then
  echo "✅ 已上线 https://chat.auto-link.com.cn/oneclick/  sha ${LOCAL_SHA:0:12}"
else
  echo "❌ 线上内容和本地不一致:本地 ${LOCAL_SHA:0:12} / 线上 ${LIVE_SHA:0:12}" >&2
  exit 1
fi
