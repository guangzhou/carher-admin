#!/usr/bin/env bash
# 桌面 App「严格沙箱」受控对照(10-25 沉淀):不碰 App,用 claude -p 喂 App 会下发的同一份 managed settings。
#
# 背景:App 3P 配置里 coworkEgressAllowedHosts 缺省 ⇒ App 按 failClosed 当 [] ⇒ 给 Code/Cowork 会话下发
#   sandbox{enabled, allowUnsandboxedCommands:false, network.allowedDomains:[], allowManagedDomainsOnly:true}
#   (App 代码 aJt():o=egressAllowedDomains 不含 "*" ⇒ c.network+c.enabled+allowUnsandboxedCommands=false)
# 对照只变一个量(有没有这份沙箱),同一条 npm 命令:
#   A 严格沙箱 ⇒ 期望 E403 "user denied" / curl=000      B 不加 ⇒ 期望 added N packages / curl=200
# 用法: app-sandbox-ab.sh [工作目录,默认仓库 .work/sbx]
set -uo pipefail
W="${1:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.work/sbx}"; mkdir -p "$W"; cd "$W" || exit 1
STRICT='{"sandbox":{"enabled":true,"allowUnsandboxedCommands":false,"network":{"allowedDomains":[],"allowManagedDomainsOnly":true}}}'
P="Run exactly once: cd \"$W\" && rm -rf node_modules package.json package-lock.json && npm init -y >/dev/null && npm install is-odd@3.0.1 --prefer-online --registry=https://registry.npmjs.org/ 2>&1 | grep -E 'added|error code|403|EPERM' | head -3 ; curl -sS -o /dev/null -w 'curl=%{http_code}\n' https://registry.npmjs.org/ 2>&1 | tail -1 . Reply only its output verbatim."
echo "== A 严格沙箱(= App 缺 coworkEgressAllowedHosts 时下发的)"
claude -p "$P" --model haiku --permission-mode bypassPermissions --managed-settings "$STRICT" </dev/null 2>&1 | grep -v "auto mode" | tail -4
echo "== B 不加沙箱(= 写了 [\"*\"] 后)"
claude -p "$P" --model haiku --permission-mode bypassPermissions </dev/null 2>&1 | grep -v "auto mode" | tail -4
echo
echo "真 App 上验(要退出重开 App):改 configLibrary/<appliedId>.json 的 coworkEgressAllowedHosts → 完全退出 App →"
echo "  open 'claude://code/new?folder=<URL编码目录>&q=<URL编码提示词>' → osascript 回车发送 → 看命令写出的 npm-out.txt"
