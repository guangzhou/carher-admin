#!/usr/bin/env python3
"""
zerokey-dev-cm-patch.py — 把 dev LiteLLM 的 zerokey-pool 改成 DB-managed + 接二级兜底

仅作用于 litellm-dev（NS 硬编码），不碰 litellm-product。两件事，幂等：
  1. 从 cm config.yaml 删掉所有 `- model_name: zerokey-pool` 块
     → zerokey-pool 不再由 cm 定义，改由 zerokey-rebalance.py 经 /model/new 动态注入(DB)。
     (个人账号 zerokey-zyq/owp/hgg/dvo 等保留，用于直连测试。)
  2. router_settings.fallbacks 里给 gpt-5.5 / chatgpt-gpt-5.5 / chatgpt-pool-gpt-5.5
     在 wangsu-gpt-5.5 之前插入 zerokey-pool（二级兜底，网宿降为三级）。

用法（198 上 jms ssh AIYJY-litellm 后）：
  python3 zerokey-dev-cm-patch.py            # dry-run，打印 diff 摘要
  python3 zerokey-dev-cm-patch.py --apply    # replace cm + rollout restart litellm-dev
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys

NS = "litellm-dev"
CM = "litellm-config"
DEPLOY = "litellm-proxy"
POOL = "zerokey-pool"
FALLBACK_KEYS = ["gpt-5.5", "chatgpt-gpt-5.5", "chatgpt-pool-gpt-5.5"]


def kubectl_json(args):
    return json.loads(subprocess.check_output(["kubectl", *args]))


def drop_pool_blocks(cfg: str) -> tuple[str, int]:
    """删除 model_list 中所有 `- model_name: zerokey-pool` 块。"""
    lines = cfg.split("\n")
    kept, skip, removed = [], False, 0
    for line in lines:
        if line.startswith("- model_name:"):
            if line.strip() == f"- model_name: {POOL}":
                skip = True
                removed += 1
                continue
            skip = False
        elif skip:
            # 块内续行：缩进行(以空格开头) 继续跳过；顶格新键结束跳过
            if line.startswith(" ") or line == "":
                continue
            skip = False
        if not skip:
            kept.append(line)
    return "\n".join(kept), removed


def insert_fallback(cfg: str) -> tuple[str, int]:
    """在 fallbacks 的目标 key 下，wangsu 之前插入 zerokey-pool（幂等）。"""
    lines = cfg.split("\n")
    out, inserted = [], 0
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = re.match(r"^(\s*)- (\S+):\s*$", line)
        if m:
            indent, key = m.group(1), m.group(2)
            if key in FALLBACK_KEYS:
                # 收集该 key 下的列表项（更深缩进的 "- xxx"）
                item_indent = indent + "  "
                j = i + 1
                items = []
                while j < len(lines) and lines[j].startswith(item_indent + "- "):
                    items.append(lines[j].strip()[2:])
                    j += 1
                if POOL not in items:
                    out.append(f"{item_indent}- {POOL}")
                    inserted += 1
        i += 1
    return "\n".join(out), inserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cm = kubectl_json(["get", "cm", CM, "-n", NS, "-o", "json"])
    old = cm["data"]["config.yaml"]
    new, removed = drop_pool_blocks(old)
    new, inserted = insert_fallback(new)

    print(f"[{NS}] zerokey-pool cm blocks removed: {removed}")
    print(f"[{NS}] zerokey-pool fallback lines inserted: {inserted}")
    if old == new:
        print("no change needed (already DB-managed + fallback present)")
        return 0

    # 展示 fallback 区域的新内容
    for ln in new.split("\n"):
        if POOL in ln or any(k in ln for k in FALLBACK_KEYS) or "wangsu-gpt-5.5" in ln:
            print("   " + ln)

    if not args.apply:
        print("\nDRY-RUN — pass --apply to replace cm + rollout restart")
        return 0

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    bdir = f"/tmp/zerokey-dev-cm-backups"
    subprocess.run(["mkdir", "-p", bdir], check=True)
    open(f"{bdir}/litellm-config-dev-{ts}.json", "w").write(json.dumps(cm))
    print(f"backup: {bdir}/litellm-config-dev-{ts}.json")

    cm["data"]["config.yaml"] = new
    path = "/tmp/litellm-config-dev-new.json"
    open(path, "w").write(json.dumps(cm))
    subprocess.run(["kubectl", "replace", "-f", path], check=True)
    print("cm replaced; rolling restart litellm-dev…")
    subprocess.run(["kubectl", "rollout", "restart", f"deployment/{DEPLOY}", "-n", NS], check=True)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
