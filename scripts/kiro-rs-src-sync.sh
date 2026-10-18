#!/usr/bin/env bash
# kiro.rs 源码对账环境同步（管 Mac 本地 ~/codes/kiro.rs；改码仍在 188 构建树 + §C 流水线）。
# 🔴 本地 clone = 上游源码，**不等于线上跑的镜像**。10-15 起线上 198(242) 跑
#    `v2026.10.1-carher-*`（现役 tag 见 skill kiro-rs-ops §C）= 上游 22f36e69 + 本仓 k8s/kiro-rs/patches/*.patch。
#    clone 里 grep 不到 PASSTHROUGH_MODELS / sonnet-5.5 / search_loop.rs 是**预期**，不是补丁丢了。
#    构建/升级走 scripts/kiro-rs-build.sh（skill kiro-rs-ops §C）。
#
# 用法：
#   scripts/kiro-rs-src-sync.sh            # clone 或 pull 到 ~/codes/kiro.rs
#   scripts/kiro-rs-src-sync.sh --check    # 只报状态不动手
set -euo pipefail

DEST="${HOME}/codes/kiro.rs"
UPSTREAM="https://github.com/hank9999/kiro.rs"

if [[ "${1:-}" == "--check" ]]; then
  if [[ -d "$DEST/.git" ]]; then
    echo "local: $DEST ($(git -C "$DEST" log --oneline -1))"
  else
    echo "local: 不存在（跑一次不带 --check 的本脚本 clone）"
  fi
  echo "upstream: $UPSTREAM"
  echo "patched-image: 198 节点242 镜像 $(ssh -o ConnectTimeout=5 cltx@10.68.13.198 'sudo -n kubectl -n kiro-rs get deploy kiro-rs -o jsonpath="{.spec.template.spec.containers[0].image}"' 2>/dev/null || echo '<198 读不到，见 skill kiro-rs-ops §C>')（上游 22f36e69 + k8s/kiro-rs/patches/*.patch）"
  exit 0
fi

if [[ -d "$DEST/.git" ]]; then
  git -C "$DEST" pull --ff-only
else
  git clone --depth 50 "$UPSTREAM" "$DEST"
fi

echo
echo "✅ 源码在 ${DEST}（上游 $(git -C "$DEST" log --oneline -1)）"
echo "⚠️  线上镜像 = 上游 + k8s/kiro-rs/patches/*.patch；对账线上行为看 188:/Data/kiro-build/kiro-up/。"
echo "    改码/升级走 scripts/kiro-rs-build.sh → kiro-rs-ship.sh → kiro-rs-e2e.sh（skill kiro-rs-ops §C）。"
