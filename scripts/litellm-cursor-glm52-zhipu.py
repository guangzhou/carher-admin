#!/usr/bin/env python3
"""把 cursor-* key 的 glm-5.2 系列 alias 从 openrouter-glm-5.2 改指智谱官方（zai-glm-5.2）。

背景（2026-08-04）：cursor 用户的 glm-5.2 不走任何 model group，而是靠 **per-key
`aliases`**（`LiteLLM_VerificationToken.aliases`，不是 metadata）改写成
`openrouter-glm-5.2`。`glm-5.2` 这个 model group 在 CM 和 DB 里都不存在，所以
`aliases={}` 的 key 调 glm-5.2 直接 400。

本脚本做两件事，逐 key **读-合-写**（`models`/`aliases` 在 LiteLLM 是整字段
REPLACE 不是 merge，必须客户端合并，否则会抹掉 kimi/deepseek 的经济路由 alias）：

  1. `glm-5.2{,-high,-medium,-low}` 四条 alias 一律指向 --target（默认 zai-glm-5.2）
  2. `models` allowlist 并入这四个名字 + --target + openrouter-glm-5.2
     （v1.89 allowlist 严格执行，**fallback 目标不自动放行**，兜底目标必须在册）

复用 scripts/zerokey-pioneer-key-sync.py 的 api()/list_all_keys()——`/key/list`
的 `size`（不是 page_size）、`return_full_object=true`、返回的 token 是 hash 但
`/key/update` 直接接受，这些坑都在那边踩平了。

用法：
    # 预览（默认 dry-run）
    python3 scripts/litellm-cursor-glm52-zhipu.py

    # 灰度 30 把（智谱余额没确认时先跑这个，观察 24h）
    python3 scripts/litellm-cursor-glm52-zhipu.py --limit 30 \
        --backup ~/cursor-glm52-zhipu-canary.json --apply

    # 全量
    python3 scripts/litellm-cursor-glm52-zhipu.py \
        --backup ~/cursor-glm52-zhipu-full.json --apply

    # 回滚
    python3 scripts/litellm-cursor-glm52-zhipu.py --restore ~/cursor-glm52-zhipu-full.json --apply

环境变量同 zerokey-pioneer-key-sync.py：LITELLM_BASE / LITELLM_MASTER_KEY
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

_SIB = pathlib.Path(__file__).with_name("zerokey-pioneer-key-sync.py")
_spec = importlib.util.spec_from_file_location("zk_key_sync", _SIB)
_zk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_zk)
api, list_all_keys = _zk.api, _zk.list_all_keys

GLM_ALIAS_NAMES = ["glm-5.2", "glm-5.2-high", "glm-5.2-medium", "glm-5.2-low"]
FALLBACK_TARGET = "openrouter-glm-5.2"


def plan_key(k: dict, target: str) -> dict | None:
    """Return {aliases, models} for this key, or None if nothing to change."""
    cur_models = k.get("models") or []
    if not cur_models:
        # [] means "unrestricted" in LiteLLM — writing a list here would silently
        # convert an all-access key into a narrow whitelist
        return None

    aliases = dict(k.get("aliases") or {})
    for name in GLM_ALIAS_NAMES:
        aliases[name] = target

    models = sorted(set(cur_models) | set(GLM_ALIAS_NAMES) | {target, FALLBACK_TARGET})

    if aliases == (k.get("aliases") or {}) and models == sorted(cur_models):
        return None
    return {"aliases": aliases, "models": models}


def cmd_sync(args):
    allk = list_all_keys()
    exclude = {x.strip() for x in (args.exclude or "").split(",") if x.strip()}
    cursor = [k for k in allk if str(k.get("key_alias") or "").startswith(args.prefix)]

    todo, skipped_open, unchanged = [], [], 0
    only = {x.strip() for x in (args.only or "").split(",") if x.strip()}
    for k in cursor:
        if k["key_alias"] in exclude:
            continue
        if only and k["key_alias"] not in only:
            continue
        if not (k.get("models") or []):
            skipped_open.append(k["key_alias"])
            continue
        change = plan_key(k, args.target)
        if change is None:
            unchanged += 1
            continue
        todo.append((k, change))

    print(f"{args.prefix}* 共 {len(cursor)} 把 | 已一致 {unchanged} | "
          f"models==[] 跳过 {len(skipped_open)} | 排除 {len(exclude)} | 待改 {len(todo)}")
    if skipped_open:
        print(f"  跳过（不限制 models，不动）: {skipped_open}")

    if args.limit:
        todo = todo[:args.limit]
        print(f"  --limit {args.limit} → 本轮只改前 {len(todo)} 把")

    # 改前状态落盘，逐 key 可回滚
    bak = {"target": args.target,
           "keys": [{"key_alias": k["key_alias"], "token": k["token"],
                     "aliases": k.get("aliases") or {},
                     "models": sorted(k.get("models") or [])} for k, _ in todo]}
    if args.backup:
        json.dump(bak, open(args.backup, "w"), ensure_ascii=False, indent=1)
        print(f"备份已写: {args.backup}")

    if not args.apply:
        for k, ch in todo[:15]:
            old = (k.get("aliases") or {}).get("glm-5.2", "(无)")
            print(f"  [dry] {k['key_alias']:32s} glm-5.2 {old} -> {args.target} | "
                  f"models {len(k.get('models') or [])} -> {len(ch['models'])}")
        if len(todo) > 15:
            print(f"  [dry] ... 另外 {len(todo) - 15} 把同形")
        print("\n(dry-run，未写入。加 --apply 生效)")
        return
    if not args.backup:
        sys.exit("--apply 必须同时给 --backup <path>（改前状态要落盘，否则无法回滚）")

    ok = fail = 0
    for i, (k, ch) in enumerate(todo, 1):
        try:
            api("POST", "/key/update", {"key": k["token"], **ch})
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {k['key_alias']}: {e}")
            if fail >= 2:
                print("!! 连续 2 次失败，停止并人工核查（fail>=2 stop 规程）!!")
                break
        if i % 50 == 0:
            print(f"  ...{i}/{len(todo)}")
    print(f"\n=== done: ok={ok} fail={fail} ===")
    verify(args)


def verify(args):
    """重拉 /key/list 复查（不信 HTTP 200，信回读）。"""
    fresh = [k for k in list_all_keys()
             if str(k.get("key_alias") or "").startswith(args.prefix)]
    exclude = {x.strip() for x in (args.exclude or "").split(",") if x.strip()}
    tally: dict[str, int] = {}
    missing_model = []
    for k in fresh:
        if k["key_alias"] in exclude or not (k.get("models") or []):
            continue
        tgt = (k.get("aliases") or {}).get("glm-5.2") or "(无 alias)"
        tally[tgt] = tally.get(tgt, 0) + 1
        if tgt == args.target and args.target not in (k.get("models") or []):
            missing_model.append(k["key_alias"])
    print("\n复查 glm-5.2 alias 落点分布:")
    for t, n in sorted(tally.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {t}")
    if missing_model:
        print(f"!! {len(missing_model)} 把 alias 指向 {args.target} 但 models 里没有它"
              f"（会 403）: {missing_model[:10]}")


def cmd_restore(args):
    bak = json.load(open(args.restore))
    keys = bak["keys"]
    print(f"restore {len(keys)} 把 key 到 {args.restore} 里的改前状态")
    if not args.apply:
        for k in keys[:15]:
            print(f"  [dry] {k['key_alias']:32s} glm-5.2 -> "
                  f"{k['aliases'].get('glm-5.2', '(无)')} | models {len(k['models'])}")
        print("\n(dry-run，未写入。加 --apply 生效)")
        return
    ok = fail = 0
    for k in keys:
        try:
            api("POST", "/key/update",
                {"key": k["token"], "aliases": k["aliases"], "models": k["models"]})
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {k['key_alias']}: {e}")
            if fail >= 2:
                sys.exit("!! 连续 2 次失败，停止 !!")
    print(f"=== restore done: ok={ok} fail={fail} ===")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prefix", default="cursor-")
    p.add_argument("--target", default="zai-glm-5.2",
                   help="glm-5.2 系列 alias 的落点 model group")
    p.add_argument("--exclude", default="", help="逗号分隔的 key_alias")
    p.add_argument("--only", default="", help="逗号分隔的 key_alias，只改这些（灰度点名）")
    p.add_argument("--limit", type=int, default=0, help="灰度：本轮只改前 N 把")
    p.add_argument("--backup", help="改前状态落盘路径（--apply 时必填）")
    p.add_argument("--restore", help="从备份 JSON 回滚")
    p.add_argument("--apply", action="store_true", help="真写入（默认 dry-run）")
    p.add_argument("--verify-only", action="store_true", help="只复查分布，不改")
    args = p.parse_args()

    if args.restore:
        cmd_restore(args)
    elif args.verify_only:
        verify(args)
    else:
        cmd_sync(args)


if __name__ == "__main__":
    main()
