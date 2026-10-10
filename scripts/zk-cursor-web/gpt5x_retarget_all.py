#!/usr/bin/env python3
# gpt5x_retarget_all.py —— 把全部 cursor-* key 的 gpt-5.5/5.4/5.2 统一指到 grok 4.7。
#
# 由来(2026-09-22):用户「所有 cursor key 的 gpt-5.5/5.4/5.2 都映射到 grok-4.7 统一改」。
#
# ── 目标名为什么写 `sa-grok-4.7` 而不是裸名 `grok-4.7` ─────────────────────
# 因为**裸名在 router 层不存在**。09-22 四臂实测:
#   D 臂 aliases 全空直打 `grok-4.7`/`grok-4.6`/`composer-2.5-fast` → **全 400**
#   C 臂 `gpt-5.5 -> grok-4.7` 但抽掉 `grok-4.7` 自己那条 alias    → **400**
#   B 臂 同上但保留 `grok-4.7 -> sa-grok-4.7`                      → 200(**两跳链式**)
# 全库 687 把里有 `grok-4.7` 这条裸名 alias 的只有 1 把 ⇒ 写裸名 = 另外 686 把全 400。
# 即便都补齐了,写裸名也只是多一跳到同一个终点,还把 gpt-5.x 的命运绑在另一条 alias 上。
#
# ── /key/update 是整表覆盖 ────────────────────────────────────────────────
# `aliases` 不是 merge,漏带一项 = 静默清空。一律读-合并-写,整份旧值先落盘。
# **只改这三个键,别的 alias 逐字带回去。**
#
# 用法(在 litellm-proxy pod 内跑):
#   TOKENS_FILE=/tmp/cursor-tokens.tsv ./gpt5x_retarget_all.py              # 预演
#   TOKENS_FILE=... ./gpt5x_retarget_all.py --limit 5 --apply               # 试点
#   TOKENS_FILE=... ./gpt5x_retarget_all.py --apply                         # 全量
#   ./gpt5x_retarget_all.py --restore /tmp/fleet-<ts>-pre-gpt5x.json        # 回滚
import argparse, json, os, sys, time, urllib.error, urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://localhost:4000")
MK = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("PROXY_MASTER_KEY")
KEYS = ["gpt-5.5", "gpt-5.4", "gpt-5.2"]
TARGET = "sa-grok-4.7"


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
    except Exception as e:
        return 0, str(e)


def load_tokens(path):
    out = []
    for line in open(path):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        p = line.split("\t")
        if len(p) != 2:
            sys.exit("ENV: TOKENS_FILE 有行不是 alias<TAB>token")
        out.append((p[0], p[1]))
    if not out:
        sys.exit("ENV: TOKENS_FILE 一行都没有")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--restore")
    ap.add_argument("--backup-dir", default="/tmp")
    a = ap.parse_args()
    if not MK:
        sys.exit("ENV: 没有 LITELLM_MASTER_KEY")

    if a.restore:
        snap = json.load(open(a.restore))
        bad = 0
        for i, row in enumerate(snap["keys"], 1):
            st, raw = call("/key/update", {"key": row["token"], "aliases": row["aliases"]}, "POST")
            if st != 200:
                bad += 1
                print("❌ %s -> %d %s" % (row["key_alias"], st, raw[:120]))
            if i % 100 == 0:
                print("   … %d/%d" % (i, len(snap["keys"])))
        print("回滚完成:%d 把,失败 %d" % (len(snap["keys"]), bad))
        return 1 if bad else 0

    tf = os.environ.get("TOKENS_FILE")
    if not tf:
        sys.exit("ENV: 没有 TOKENS_FILE")
    rows = load_tokens(tf)
    if a.limit:
        rows = rows[:a.limit]
    print("待处理 %d 把;把 %s 统一指到 %s\n" % (len(rows), "/".join(KEYS), TARGET))

    snapshot, todo, noop, from_cnt = [], [], 0, {}
    for alias, tok in rows:
        st, raw = call("/key/info?key=" + tok)
        if st != 200:
            print("❌ %s /key/info %d %s" % (alias, st, raw[:100]))
            continue
        al = dict((json.loads(raw)["info"].get("aliases") or {}))
        snapshot.append({"key_alias": alias, "token": tok, "aliases": al})
        # 只动这三个键,且**只动本来就存在的那些** —— 这把 key 没有 gpt-5.2,
        # 凭空给它加一个 = 扩权,不是"统一映射"。
        chg = {k: TARGET for k in KEYS if k in al and al[k] != TARGET}
        for k in chg:
            from_cnt[(k, al[k])] = from_cnt.get((k, al[k]), 0) + 1
        if not chg:
            noop += 1
            continue
        todo.append((alias, tok, al, chg))

    print("读完 %d 把;已达标 %d 把;要改 %d 把" % (len(snapshot), noop, len(todo)))
    print("改动分布(从哪来 -> %s):" % TARGET)
    for (k, v), n in sorted(from_cnt.items()):
        print("   %-10s %-18s %d 把" % (k, v, n))

    if not a.apply:
        print("\n以上是**预演**,一个字都没写。确认后加 --apply。")
        if todo:
            print("样例(%s):%s" % (todo[0][0], todo[0][3]))
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(a.backup_dir, "fleet-%s-pre-gpt5x.json" % stamp)
    with open(bak, "w") as f:
        json.dump({"stamp": stamp, "keys": snapshot}, f, ensure_ascii=False)
    print("\n车队备份(整份 aliases):%s (%d 把)" % (bak, len(snapshot)))

    ok = bad = 0
    t0 = time.time()
    for i, (alias, tok, al, chg) in enumerate(todo, 1):
        new_al = dict(al)
        new_al.update(chg)          # ⚠️ 整表覆盖:其余 alias 原样带回去
        st, raw = call("/key/update", {"key": tok, "aliases": new_al}, "POST")
        if st == 200:
            ok += 1
        else:
            bad += 1
            print("❌ %s -> %d %s" % (alias, st, raw[:130]))
        if i % 100 == 0:
            print("   … %d/%d (%.0fs)" % (i, len(todo), time.time() - t0))
    print("\n写完:成功 %d,失败 %d,耗时 %.0fs" % (ok, bad, time.time() - t0))
    print("回滚:./gpt5x_retarget_all.py --restore %s" % bak)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
