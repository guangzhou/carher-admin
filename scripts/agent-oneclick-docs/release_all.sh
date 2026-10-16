#!/usr/bin/env bash
# 一键配置「发版」总入口:改了任何安装器 / 自检 / 页面 / 飞书正文后跑这一条。
#
#   1. 重新构建 Codex、Claude 两套(各自 build.sh 先跑 pytest,不过就停)
#   2. 单页网页上线 198(publish_198.sh,比线上 sha)
#   3. 飞书三篇的附件换成新构建(swap_attachment.py,已是新版就跳过)
#   4. 飞书门禁(agent-oneclick-docs-audit.py:附件 sha 回环 + 正文规则)
#
# 为什么要一条命令:10-16 修完 Codex 默认模型 / Node 同步后只发了网页,飞书 Codex 文档里挂的还是旧脚本,
# 是收尾跑门禁才发现的。网页和飞书是同一批文件的两个出口,必须一起发。
# Cursor 装机包不在这里重建(它有自己的 package_team_setup.sh 和更重的回归),这里只同步它已构建好的 zip。
# 用法: bash scripts/agent-oneclick-docs/release_all.sh  [--skip-feishu]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
D=scripts/agent-oneclick-docs

echo "== 1/4 构建 Codex / Claude =="
bash codex-oneclick/build.sh  | tail -3
bash claude-oneclick/build.sh | tail -3

echo "== 2/4 网页上线 =="
bash "$D/publish_198.sh" | tail -1

if [ "${1:-}" = "--skip-feishu" ]; then echo "(跳过飞书)"; exit 0; fi

echo "== 3/4 飞书附件 =="
python3 "$D/swap_attachment.py" OQCPdLd4MovEVoxGzdMcD3CJnCf scripts/zk-cursor-web/cursor-mac.zip scripts/zk-cursor-web/cursor-windows.zip
python3 "$D/swap_attachment.py" HMj6d1ttlorc5KxbFnlcVuopntf codex-oneclick/codex-mac.command codex-oneclick/codex-windows.bat
python3 "$D/swap_attachment.py" MeLad7oMiobwf6xnpA2cWy0nnSg claude-oneclick/claude-mac.command claude-oneclick/claude-windows.bat

echo "== 4/4 飞书门禁 =="
python3 scripts/agent-oneclick-docs-audit.py | tail -4
