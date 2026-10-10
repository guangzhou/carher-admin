#!/usr/bin/env python3
# sa_prefix_alias.py —— 给**一把** key 加上「去掉 sa- 前缀」的裸名入口。
#
# 由来(2026-09-22):用户要求 `sa-grok-4.7` 这类名字在 Cursor 里能直接写成 `grok-4.7`。
# 裸名是 Cursor 自带目录里认识的名字,写裸名菜单里才画得出 Context/Effort/Fast 那几个控件
# (⚠️ 但**控件的值走不到我们网关** —— 实测 209 条 Cursor 流量里 `fast`/`effort`/
#  `reasoning_effort` 出现 0 次,而同窗口其它客户端出现 450+ 次。裸名解决的是
#  「名字好写、菜单里有」,不是「档位能用」。别把这两件事混成一件。)
#
# ── 为什么是 per-key 而不是全局 ────────────────────────────────────────────
# 这是**加法**:只给指定的那把 key 多开一个入口,`sa-*` 原名一个都不动、继续可用。
# 不碰 router 级 alias、不碰 `LiteLLM_ProxyModelTable`、不碰任何别的 key ⇒
# 其他账号的行为**逐字节不变**。
#
# ── 两道闸是独立的(2026-09-21 实测,不是推理)──────────────────────────────
#   allowlist(`models`) 缺 → 403 `key_model_access_denied`
#   per-key alias 缺      → 400
# 且 **403 那道闸查的是请求里那个名字,发生在 alias 改写之前** ⇒ 裸名必须
# **同时**进 `models` 和 `aliases`,只加一处等于没加。
#
# ── 整表替换(踩过)────────────────────────────────────────────────────────
# `/key/update` 的 `models` 和 `aliases` 都是**整表覆盖**,不是 merge。
# 漏带一项 = 静默把那项清空。所以本脚本一律「读-合并-写」,并在写之前把
# 完整旧值落盘成备份。
#
# 用法:
#   ./sa_prefix_alias.py --alias cursor-liuguoxian-std             # 预演,不写
#   ./sa_prefix_alias.py --alias cursor-liuguoxian-std --apply     # 真写 + 回读
#   ./sa_prefix_alias.py --alias cursor-liuguoxian-std --probe     # 只跑 pre/post 探针
#   ./sa_prefix_alias.py --alias cursor-liuguoxian-std --restore F # 从备份还原
#   ./sa_prefix_alias.py --alias X --grant sa-composer-2.5 --apply # 新开一个模型 + 它的裸名
#
# 跑在 litellm-proxy pod 内(`BASE=http://localhost:4000`),master key 从 env 取。
# 环境:`LITELLM_MASTER_KEY`;key 的 token 由 `--token` 传入或 env `TOK`。
#
# 退出码:0 = PASS / 2 = 用法或环境错(尺子坏了,不是结论) / 1 = FAIL。
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://localhost:4000")
MK = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("PROXY_MASTER_KEY")
PREFIX = "sa-"


def call(path, body=None, method="GET", key=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"Authorization": "Bearer " + (key or MK),
                 "Content-Type": "application/json"},
        data=(json.dumps(body).encode() if body is not None else None))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def key_info(token):
    st, raw = call("/key/info?key=" + token)
    if st != 200:
        sys.exit("ENV: /key/info 返回 %d: %s" % (st, raw[:300]))
    return json.loads(raw)["info"]


def plan_from(models):
    """裸名 -> sa- 原名。只对 allowlist 里真的有的 sa-* 名字生成,不凭菜单猜。"""
    out = {}
    for m in sorted(models):
        if m.startswith(PREFIX):
            out[m[len(PREFIX):]] = m
    return out


def resolvable(names):
    """拿 /model/info 核一遍这些名字在模型表里解析得到。

    授权一个解析不到的名字 = 给菜单塞一个点了就报错的入口,而 `/key/update` 对
    不认识的名字照样返 200 —— http 码不是判据,所以这道检查必须在写之前。
    ⚠️ 反过来不成立:表里查不到**不等于**不能用(`sa-grok-4.20` 实测 200,走 router
    层解析)。所以这里只在「要新开一个名字」时拦,不拿它去否定已有的名字。"""
    st, raw = call("/model/info")
    if st != 200:
        sys.exit("ENV: /model/info 返回 %d" % st)
    known = {r.get("model_name") for r in json.loads(raw).get("data", [])}
    return [n for n in names if n not in known]


def probe_one(key, name):
    """复刻 Cursor 真实线型:chat/completions + stream + tools,**不带 reasoning_effort**
    (真 Cursor 不发它;探针补配置本该提供的字段会构造性屏蔽红)。"""
    body = {"model": name, "stream": True,
            "messages": [{"role": "user", "content": "reply with the single word: ok"}],
            "tools": [{"type": "function", "function": {
                "name": "noop", "description": "no operation",
                "parameters": {"type": "object", "properties": {}}}}],
            "tool_choice": "auto", "stream_options": {"include_usage": True},
            "max_tokens": 16}
    st, raw = call("/v1/chat/completions", body, "POST", key=key)
    tag = ""
    if st != 200:
        low = raw.lower()
        if "key_model_access_denied" in low or "not allowed to access" in low:
            tag = "403/allowlist"
        elif "not the typical" in low or "invalid model" in low or "does not exist" in low:
            tag = "400/no-such-group"
        else:
            tag = "%d" % st
    return st, (tag or "200"), raw[:160].replace("\n", " ")


def run_probe(token, names, label):
    """用**临时真 key** 打(master key 绕过 per-key gate,必假绿)。
    临时 key 直接把目标 key 的 models/aliases 照抄过去,20 分钟后自然过期,跑完即删。"""
    info = key_info(token)
    suffix = "%x" % int(time.time())
    st, raw = call("/key/generate", {
        "key_alias": "tmp-saprefix-%s-%s" % (label, suffix),
        "models": list(info.get("models") or []),
        "aliases": dict(info.get("aliases") or {}),
        "duration": "20m"}, "POST")
    if st != 200:
        sys.exit("ENV: /key/generate 失败 %d: %s" % (st, raw[:300]))
    tmp = json.loads(raw)["key"]
    res = {}
    try:
        for n in names:
            code, tag, body = probe_one(tmp, n)
            res[n] = tag
            print("   %-28s %s" % (n, tag if tag == "200" else tag + "  " + body))
        # 阴性对照:一个绝不存在的名字必须红。尺子只会绿就不是门。
        _, neg, _ = probe_one(tmp, "zzz-no-such-model-%s" % suffix)
        print("   %-28s %s   ← 阴性对照(必须非 200)" % ("(bogus)", neg))
        if neg == "200":
            print("❌ 阴性对照没红 —— 这把尺子量不出坏名字,上面的 200 不许当结论")
            res["__ruler__"] = "BROKEN"
    finally:
        call("/key/delete", {"keys": [tmp]}, "POST")
    return res


def drop_sa(a, models, aliases):
    """cutover 最后一步:把 sa-* 从 allowlist 摘掉。**aliases 一个字不动** ——
    裸名正是靠 `aliases[裸名] = sa-原名` 才找得到上游组,摘了 alias 才是真断。

    为什么摘 allowlist 不会断(09-22 实测,不是推理):临时 key 把 10 个 sa-* 全抽掉后,
    5 个裸名仍全 200,而 sa- 原名全 403(阳性对照,证明这刀真落下了)。机制 = 403 那道闸
    查的是**请求里那个名字**(裸名,在表里),alias 改写发生在**之后**,改写后的名字
    **不再回查 allowlist**。"""
    sa_names = [m for m in models if m.startswith(PREFIX)]
    if not sa_names:
        print("✅ allowlist 里已经没有 %s 开头的名字了。" % PREFIX)
        return 0
    # 闸:每个要摘的 sa-* 都必须有裸名接着。没有接盘的那个摘掉 = 真吊销,不是换名。
    covered = set(aliases.values())
    orphan = [s for s in sa_names if s not in covered]
    print("allowlist %d 个,其中 %s* %d 个" % (len(models), PREFIX, len(sa_names)))
    if orphan:
        print("❌ 下列 %s* 没有任何裸名指向它,摘掉就是**真吊销访问**,拒绝执行:" % PREFIX)
        for s in orphan:
            print("   %s" % s)
        print("   先用 --apply 给它们补上裸名,再回来摘。")
        return 1
    for s in sorted(sa_names):
        b = [k for k, v in aliases.items() if v == s]
        print("   摘 %-30s ← 由裸名 %s 接着" % (s, ", ".join(sorted(b))))

    if not a.apply:
        print("\n以上是**预演**,一个字都没写。确认后加 --apply。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(a.backup_dir, "key-%s-%s-pre-dropsa.json" % (a.alias, stamp))
    with open(bak, "w") as f:
        json.dump({"key_alias": a.alias, "models": models, "aliases": aliases},
                  f, ensure_ascii=False, indent=2, sort_keys=True)
    print("\n备份:%s" % bak)

    new_models = [m for m in models if not m.startswith(PREFIX)]
    st, raw = call("/key/update", {"key": a.token, "models": new_models,
                                   "aliases": aliases}, "POST")
    print("/key/update -> %d" % st)
    if st != 200:
        print(raw[:500])
        return 1

    back = key_info(a.token)
    bm, ba = set(back.get("models") or []), dict(back.get("aliases") or {})
    fail = 0
    left = sorted(m for m in bm if m.startswith(PREFIX))
    if left:
        print("❌ 还剩 %s*:%s" % (PREFIX, ", ".join(left))); fail = 1
    for m in new_models:                   # 非 sa- 的名字一个都不许被顺手带走
        if m not in bm:
            print("❌ 非 %s 名字丢失:%s" % (PREFIX, m)); fail = 1
    if ba != aliases:                      # aliases 必须逐字不变
        print("❌ aliases 被改动了(应逐字不变)"); fail = 1
    print("回读:models %d -> %d,aliases %d -> %d  ⇒ %s"
          % (len(models), len(bm), len(aliases), len(ba), "FAIL" if fail else "PASS"))
    print("回滚:./sa_prefix_alias.py --alias %s --token <tok> --restore %s" % (a.alias, bak))
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True, help="key_alias,仅作显示与备份命名")
    ap.add_argument("--token", default=os.environ.get("TOK", ""), help="该 key 的 token")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--probe", action="store_true", help="只跑探针,不改任何东西")
    ap.add_argument("--restore", help="从备份 json 还原 models/aliases")
    ap.add_argument("--grant", default="",
                    help="逗号分隔的 sa-* 名:先把它们加进 allowlist,再照常派生裸名。"
                         "用于给这把 key 新开一个模型(会先用 /model/info 核对能否解析)")
    ap.add_argument("--drop-sa", action="store_true",
                    help="cutover 的最后一步:把 sa-* 从 allowlist 摘掉(aliases 不动)。"
                         "只有每个 sa-* 都已有裸名接着才允许")
    ap.add_argument("--backup-dir", default="/tmp")
    a = ap.parse_args()

    if not MK:
        sys.exit("ENV: 没有 LITELLM_MASTER_KEY")
    if not a.token:
        sys.exit("ENV: 没给 --token(也没有 env TOK)")

    if a.restore:
        old = json.load(open(a.restore))
        st, raw = call("/key/update", {"key": a.token, "models": old["models"],
                                       "aliases": old["aliases"]}, "POST")
        print("restore -> %d" % st)
        back = key_info(a.token)
        ok = (sorted(back.get("models") or []) == sorted(old["models"])
              and (back.get("aliases") or {}) == old["aliases"])
        print("回读核对:", "PASS" if ok else "FAIL")
        return 0 if ok else 1

    info = key_info(a.token)
    models = list(info.get("models") or [])
    aliases = dict(info.get("aliases") or {})

    if a.drop_sa:
        print("key_alias = %s" % a.alias)
        return drop_sa(a, models, aliases)

    # --grant:先把新的 sa-* 名并进"想要的 allowlist",裸名派生照常走同一条路
    # (派生只看 allowlist ⇒ 不这样并进来,新开的名字就派生不出裸名)。
    grants = [g.strip() for g in a.grant.split(",") if g.strip()]
    bad = [g for g in grants if not g.startswith(PREFIX)]
    if bad:
        sys.exit("ENV: --grant 只收 %s 开头的名字,收到 %s" % (PREFIX, bad))
    new_grants = [g for g in grants if g not in models]
    if new_grants:
        unknown = resolvable(new_grants)
        if unknown:
            sys.exit("❌ 模型表里解析不到,拒绝授权:%s" % ", ".join(unknown))
        print("--grant:新开 %d 个 sa-* 名 -> %s" % (len(new_grants), ", ".join(new_grants)))
    want_models = models + new_grants

    plan = plan_from(want_models)
    print("key_alias = %s" % a.alias)
    print("现状:models n=%d,aliases n=%d,其中 sa-* n=%d" % (len(models), len(aliases), len(plan)))

    add_alias = {b: s for b, s in plan.items() if aliases.get(b) != s}
    add_model = [b for b in plan if b not in models] + new_grants
    conflict = {b: aliases[b] for b, s in plan.items()
                if b in aliases and aliases[b] != s}
    print("\n要加的 alias(%d 条):" % len(add_alias))
    for b in sorted(add_alias):
        print("   %-28s -> %s" % (b, add_alias[b]))
    print("要加进 allowlist 的名字(%d 个,含 --grant 的 sa- 原名): %s"
          % (len(add_model), ", ".join(sorted(add_model))))
    if conflict:
        # 已经指向别的地方的裸名不许静默改写 —— 那是改语义不是加法。
        print("\n⚠️ 下列裸名已被占用,**本脚本不动它们**(要改请人工裁决):")
        for b in sorted(conflict):
            print("   %-28s 现指向 %s,而非 %s" % (b, conflict[b], plan[b]))
        for b in conflict:
            add_alias.pop(b, None)

    if a.probe:
        print("\n— 探针:裸名 —")
        run_probe(a.token, sorted(plan), "bare")
        print("\n— 探针:sa- 原名(对照)—")
        run_probe(a.token, sorted(plan.values()), "sa")
        return 0

    if not add_alias and not add_model:
        print("\n✅ 已经是目标状态,什么都不用改。")
        return 0

    if not a.apply:
        print("\n以上是**预演**,一个字都没写。确认后重跑并加 --apply。")
        return 0

    # ── 写之前先落备份。破坏性动作的备份必须贴在写库正前方 ──────────────
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(a.backup_dir, "key-%s-%s-pre-saprefix.json" % (a.alias, stamp))
    with open(bak, "w") as f:
        json.dump({"key_alias": a.alias, "models": models, "aliases": aliases},
                  f, ensure_ascii=False, indent=2, sort_keys=True)
    print("\n备份:%s" % bak)

    new_models = models + [b for b in sorted(add_model)]
    new_aliases = dict(aliases)
    new_aliases.update(add_alias)
    # ⚠️ models/aliases 都是整表覆盖 —— 原样带回去,漏了就把授权清空。
    st, raw = call("/key/update", {"key": a.token, "models": new_models,
                                   "aliases": new_aliases}, "POST")
    print("/key/update -> %d" % st)
    if st != 200:
        print(raw[:500])
        return 1

    back = key_info(a.token)
    bm, ba = set(back.get("models") or []), dict(back.get("aliases") or {})
    fail = 0
    for m in models:                       # 老名字一个都不许丢
        if m not in bm:
            print("❌ 原有 model 丢失:%s" % m); fail = 1
    for k, v in aliases.items():
        if ba.get(k) != v:
            print("❌ 原有 alias 变了:%s %s -> %s" % (k, v, ba.get(k))); fail = 1
    for b in add_model:
        if b not in bm:
            print("❌ 裸名没进 allowlist:%s" % b); fail = 1
    for b, s in add_alias.items():
        if ba.get(b) != s:
            print("❌ alias 没落:%s -> %s(读回 %s)" % (b, s, ba.get(b))); fail = 1
    print("回读:models %d -> %d,aliases %d -> %d  ⇒ %s"
          % (len(models), len(bm), len(aliases), len(ba), "FAIL" if fail else "PASS"))
    print("回滚:./sa_prefix_alias.py --alias %s --token <tok> --restore %s" % (a.alias, bak))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
