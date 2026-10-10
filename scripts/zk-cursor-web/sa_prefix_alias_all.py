#!/usr/bin/env python3
# sa_prefix_alias_all.py —— 车队批量:给**所有** cursor-* key 补「去掉 sa- 前缀」的裸名入口。
#
# 由来(2026-09-22):用户决定全员改用裸名(`sa-grok-4.6` → `grok-4.6`),
# 「以后大家就不要用 sa- 开头的模型了」。
#
# ── 顺序是硬的:先补 key,再换菜单,最后才换文档 ─────────────────────────────
# 改名是**加法**:add → verify → cutover → 最后 remove。
# 反过来做(菜单/文档先改成裸名、key 还没补)的后果不是"有点问题",是**全员每点一个
# 模型报一个错** —— 而且报的是 403/400 这种看起来像"没权限"的错,同事会去找 IT 而不是
# 找我们。所以这个脚本必须跑在装机包重打之前。
#
# ── 为什么裸名集合锚定在菜单而不是各 key 的 allowlist ──────────────────────
# allowlist 里躺着一堆历史名(`sa-grok-imagine*` 是 image 模型,走 chat 线恒 500;
# `sa-kimi-k3` 只在 1 把 key 上)。照 allowlist 全量去前缀 = 给每把 key 塞几条
# **永远是坏的**裸名,以后有人点了就报错,而我们还得解释"这个名字本来就不能用"。
# 锚定菜单 = 只补同事真的会点到的那些。
#
# ── 两道闸独立(实测,不是推理)────────────────────────────────────────────
#   allowlist(`models`)缺 → 403;per-key `aliases` 缺 → 400。
#   且 403 那道闸查的是**请求里那个名字**、发生在 alias 改写**之前**
#   ⇒ 裸名必须同时进两处,只加一处等于没加。
#
# ── /key/update 是整表覆盖 ────────────────────────────────────────────────
# `models` 和 `aliases` 都不是 merge。漏带一项 = 静默清空那项。所以一律读-合并-写,
# 且**整份旧值先落盘**再动手。
#
# 用法(在 litellm-proxy pod 内跑):
#   TOKENS_FILE=/tmp/cursor-tokens.tsv ./sa_prefix_alias_all.py            # 预演全量
#   TOKENS_FILE=... ./sa_prefix_alias_all.py --limit 5 --apply             # 试点 5 把
#   TOKENS_FILE=... ./sa_prefix_alias_all.py --apply                       # 全量
#   ./sa_prefix_alias_all.py --restore /tmp/fleet-<ts>-pre.json            # 回滚
#
# TOKENS_FILE 格式:每行 `key_alias<TAB>token`。
# 退出码:0 = PASS / 1 = 有 key 失败 / 2 = 用法或环境错。
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://localhost:4000")
MK = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("PROXY_MASTER_KEY")

# 菜单里那 7 个 sa-* + sa-grok-4.7(不在菜单但 668 把 key 有、用户本人在用)。
# ⚠️ 改这张表前先想清楚:每加一个名字,687 把 key 的菜单里就多一个点得到的入口。
#    只有「同事真的会点」的名字才配进来。
MENU_SA = [
    "sa-grok-4.6-latest", "sa-grok-4.6", "sa-grok-4.5-latest", "sa-grok-4.5",
    "sa-grok-4.20", "sa-grok-4.20-0309-reasoning", "sa-composer-2.5-fast",
    "sa-grok-4.7",
]
# 刻意排除:`sa-grok-imagine` / `sa-grok-imagine-image-2.0`(image 模型,Cursor 只走
# /v1/chat/completions ⇒ 恒 500)、`sa-kimi-k3`(只在 1 把 key 上,不在菜单)。
EXCLUDED = ["sa-grok-imagine", "sa-grok-imagine-image-2.0", "sa-kimi-k3"]

BARE = {s: s[3:] for s in MENU_SA}


def call(path, body=None, method="GET"):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"Authorization": "Bearer " + MK, "Content-Type": "application/json"},
        data=(json.dumps(body).encode() if body is not None else None))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:                      # 网络抖动不该让整轮停,记账继续
        return 0, str(e)


def load_tokens(path):
    out = []
    for line in open(path):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            sys.exit("ENV: TOKENS_FILE 第 %r 行不是 alias<TAB>token" % line[:60])
        out.append((parts[0], parts[1]))
    if not out:
        sys.exit("ENV: TOKENS_FILE 一行都没有")
    return out


def plan_for(models, aliases):
    """返回 (要加的 alias dict, 要加的 model list, 冲突 dict)。

    只对这把 key **已经有** sa- 原名的那些生成裸名 —— 它没被授权的模型,
    给它开裸名入口等于替管理员扩权。"""
    add_alias, add_model, conflict = {}, [], {}
    for sa in MENU_SA:
        if sa not in models:
            continue
        b = BARE[sa]
        if b in aliases and aliases[b] != sa:
            conflict[b] = aliases[b]        # 已指向别处,不静默改写
            continue
        if aliases.get(b) != sa:
            add_alias[b] = sa
        if b not in models:
            add_model.append(b)
    return add_alias, add_model, conflict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 把(试点)")
    ap.add_argument("--restore", help="从车队备份 json 回滚")
    ap.add_argument("--backup-dir", default="/tmp")
    a = ap.parse_args()

    if not MK:
        sys.exit("ENV: 没有 LITELLM_MASTER_KEY")

    if a.restore:
        snap = json.load(open(a.restore))
        bad = 0
        for i, row in enumerate(snap["keys"], 1):
            st, raw = call("/key/update", {"key": row["token"], "models": row["models"],
                                           "aliases": row["aliases"]}, "POST")
            if st != 200:
                bad += 1
                print("❌ %s -> %d %s" % (row["key_alias"], st, raw[:120]))
            if i % 50 == 0:
                print("   … %d/%d" % (i, len(snap["keys"])))
        print("回滚完成:%d 把,失败 %d" % (len(snap["keys"]), bad))
        return 1 if bad else 0

    tf = os.environ.get("TOKENS_FILE")
    if not tf:
        sys.exit("ENV: 没有 TOKENS_FILE")
    rows = load_tokens(tf)
    if a.limit:
        rows = rows[:a.limit]
    print("待处理 %d 把 key;裸名集合 %d 个:%s" % (len(rows), len(BARE), ", ".join(sorted(BARE.values()))))
    print("刻意排除(坏名/孤名):%s\n" % ", ".join(EXCLUDED))

    snapshot, todo = [], []
    n_noop = 0
    conflicts_all = {}
    for alias, tok in rows:
        st, raw = call("/key/info?key=" + tok)
        if st != 200:
            print("❌ %s /key/info %d %s" % (alias, st, raw[:120]))
            continue
        info = json.loads(raw)["info"]
        models = list(info.get("models") or [])
        aliases = dict(info.get("aliases") or {})
        snapshot.append({"key_alias": alias, "token": tok, "models": models, "aliases": aliases})
        add_alias, add_model, conflict = plan_for(models, aliases)
        if conflict:
            conflicts_all[alias] = conflict
        if not add_alias and not add_model:
            n_noop += 1
            continue
        todo.append((alias, tok, models, aliases, add_alias, add_model))

    print("读完:%d 把;已是目标状态 %d 把;要改 %d 把" % (len(snapshot), n_noop, len(todo)))
    if conflicts_all:
        print("⚠️ %d 把 key 有裸名已指向别处,**本脚本不动它们**:" % len(conflicts_all))
        for k in sorted(conflicts_all)[:10]:
            print("   %s: %s" % (k, conflicts_all[k]))

    if not a.apply:
        print("\n以上是**预演**,一个字都没写。确认后加 --apply。")
        if todo:
            alias, _, m, al, aa, am = todo[0]
            print("样例(%s):+alias %s ;+models %s" % (alias, aa, am))
        return 0

    # ── 写之前先把整份车队旧值落盘。687 把 key 没有快照就动手 = 没有退路 ──
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(a.backup_dir, "fleet-%s-pre-saprefix.json" % stamp)
    with open(bak, "w") as f:
        json.dump({"stamp": stamp, "keys": snapshot}, f, ensure_ascii=False)
    print("\n车队备份:%s (%d 把)" % (bak, len(snapshot)))

    ok = bad = 0
    t0 = time.time()
    for i, (alias, tok, models, aliases, add_alias, add_model) in enumerate(todo, 1):
        new_models = models + sorted(add_model)
        new_aliases = dict(aliases)
        new_aliases.update(add_alias)
        st, raw = call("/key/update", {"key": tok, "models": new_models,
                                       "aliases": new_aliases}, "POST")
        if st == 200:
            ok += 1
        else:
            bad += 1
            print("❌ %s -> %d %s" % (alias, st, raw[:150]))
        if i % 50 == 0:
            print("   … %d/%d  (%.0fs)" % (i, len(todo), time.time() - t0))
    print("\n写完:成功 %d,失败 %d,耗时 %.0fs" % (ok, bad, time.time() - t0))
    print("回滚:./sa_prefix_alias_all.py --restore %s" % bak)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
