#!/usr/bin/env python3
"""把 key 上指向 kiro 的名字切到 aws-*（2026-10-27 用户定后台 kiro 统一改 aws）。在 198 上跑，打 127.0.0.1:30402。

  python3 aws-switch-keys.py plan    --only-file keys.txt | --prefix cursor- [--prefix carher-] [--add-auto]
  python3 aws-switch-keys.py apply   ... --backup B.json
  python3 aws-switch-keys.py restore B.json [--apply]

每把 key 只动三类东西，别的字段一字不动（read-merge-write，写后回读比对）：
1. aliases 值：`kiro-slots-X` / `kiro-X` → `aws-X`（目标组必须在线上存在，否则这一条不动并报出来）
2. models 白名单里直写的 `kiro-slots-X` / `kiro-X` → 对外名 X（并补 alias X→aws-X）；
   对外名已被别的 alias 占用且指向不同组 ⇒ 保留原名不动并报 CONFLICT
3. `--add-auto`：白名单非空且没有 `auto` 的，加 `auto`；aliases 加 `auto → aws-auto`
   （白名单为空 = 全放行的 key 只加 alias，不写白名单——写了反而把它收窄成一个模型）

守卫：
- 回读前后 token 必须相同（key 被同名重发就跳过）
- 先写备份（每把 key 改前的 models+aliases）再写库；restore 按备份还原这两个字段
- blocked 的 key 也改（解封后不能复活成 kiro 名）
"""
import json, os, sys, urllib.request, urllib.parse

BASE = "http://127.0.0.1:30402"
MK = os.environ["LITELLM_MASTER_KEY"]
# 老实例组名里 gpt 三个没有中划线（kiro-gpt5.6-sol），对外/aws 名统一成 gpt-5.6-sol 形状
SPECIAL = {"gpt5.6-sol": "gpt-5.6-sol", "gpt5.6-terra": "gpt-5.6-terra", "gpt5.6-luna": "gpt-5.6-luna"}


def api(path, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                               method="POST" if body is not None else "GET",
                               headers={"Authorization": "Bearer " + MK, "content-type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=120))


def live_groups():
    out, page = set(), 1
    while True:
        d = api(f"/v2/model/info?page={page}&size=100")
        out |= {m["model_name"] for m in d.get("data", [])}
        if page >= d.get("total_pages", 1):
            return out
        page += 1


def base_of(name):
    """kiro-slots-X / kiro-X → X（对外名形状）；不是 kiro 名返回 None"""
    for p in ("kiro-slots-", "kiro-"):
        if name.startswith(p):
            x = name[len(p):]
            return SPECIAL.get(x, x)
    return None


def list_keys(prefixes, only):
    out, page = [], 1
    while True:
        d = api(f"/key/list?return_full_object=true&include_team_keys=true&page={page}&size=100")
        for k in d.get("keys", []):
            if not isinstance(k, dict):
                continue
            a = k.get("key_alias") or ""
            if (only and a in only) or (not only and any(a.startswith(p) for p in prefixes)):
                out.append(k)
        if page >= d.get("total_pages", 1):
            break
        page += 1
    return out


def plan_key(k, groups, add_auto):
    models = list(k.get("models") or [])
    aliases = dict(k.get("aliases") or {})
    new_m, new_a, notes = list(models), dict(aliases), []
    for n, v in aliases.items():
        b = base_of(str(v))
        if b is None:
            continue
        tgt = "aws-" + b
        if tgt not in groups:
            notes.append(f"NOGROUP {n}->{v}"); continue
        new_a[n] = tgt
    for i, m in enumerate(models):
        b = base_of(m)
        if b is None:
            continue
        tgt = "aws-" + b
        if tgt not in groups:
            notes.append(f"NOGROUP models:{m}"); continue
        cur = new_a.get(b)
        if cur is not None and cur != tgt:
            notes.append(f"CONFLICT models:{m} 对外名 {b} 已映射 {cur}"); continue
        new_m[i] = b
        new_a[b] = tgt
    # 去重保序（kiro-X 和 kiro-slots-X 可能都换成 X）
    seen, dedup = set(), []
    for m in new_m:
        if m not in seen:
            seen.add(m); dedup.append(m)
    new_m = dedup
    if add_auto and "aws-auto" in groups:
        if new_a.get("auto") not in (None, "aws-auto"):
            notes.append(f"CONFLICT auto 已映射 {new_a['auto']}")
        else:
            new_a["auto"] = "aws-auto"
            if new_m and "auto" not in new_m and "all-team-models" not in new_m and "all-proxy-models" not in new_m:
                new_m.append("auto")
    return new_m, new_a, notes


def find_token(alias, token):
    d = api("/key/list?return_full_object=true&size=5&key_alias=" + urllib.parse.quote(alias))
    ks = [k for k in d.get("keys", []) if isinstance(k, dict) and k.get("token") == token]
    return ks[0] if len(ks) == 1 else None


def main():
    a = sys.argv[1:]
    cmd = a[0]
    apply = cmd == "apply" or (cmd == "restore" and "--apply" in a)
    if cmd == "restore":
        snap = json.load(open(a[1]))
        for rec in snap:
            if apply:
                api("/key/update", {"key": rec["token"], "models": rec["models"], "aliases": rec["aliases"]})
                back = find_token(rec["key_alias"], rec["token"]) or {}
                ok = (back.get("models") or []) == rec["models"] and (back.get("aliases") or {}) == rec["aliases"]
                print(("RESTORED " if ok else "🔴 MISMATCH ") + str(rec["key_alias"]))
            else:
                print("PLAN-RESTORE", rec["key_alias"])
        return
    prefixes = [a[i + 1] for i, x in enumerate(a) if x == "--prefix"]
    only = set()
    if "--only-file" in a:
        only = {l.strip() for l in open(a[a.index("--only-file") + 1]) if l.strip() and not l.startswith("#")}
    only |= {a[i + 1] for i, x in enumerate(a) if x == "--only"}
    add_auto = "--add-auto" in a
    bk = a[a.index("--backup") + 1] if "--backup" in a else None
    if apply and not bk:
        sys.exit("apply 需要 --backup")
    if not prefixes and not only:
        sys.exit("要 --prefix 或 --only/--only-file")
    groups = live_groups()
    keys = list_keys(prefixes, only)
    if only:
        miss = only - {k.get("key_alias") for k in keys}
        if miss:
            sys.exit(f"only 里 {len(miss)} 个没匹配上，拒跑: {sorted(miss)[:10]}")
    snap, changed, same, bad, notes_all = [], 0, 0, [], []
    for k in keys:
        new_m, new_a, notes = plan_key(k, groups, add_auto)
        for n in notes:
            notes_all.append(f"{k.get('key_alias')}: {n}")
        if new_m == list(k.get("models") or []) and new_a == dict(k.get("aliases") or {}):
            same += 1; continue
        changed += 1
        rec = {"token": k["token"], "key_alias": k.get("key_alias"),
               "models": list(k.get("models") or []), "aliases": dict(k.get("aliases") or {})}
        if not apply:
            if changed <= 3:
                dm = [(x, y) for x, y in zip(rec["models"], new_m) if x != y] + [("+", x) for x in new_m[len(rec["models"]):]]
                da = {n: (rec["aliases"].get(n), v) for n, v in new_a.items() if rec["aliases"].get(n) != v}
                print("PLAN", rec["key_alias"], "models", dm, "aliases", json.dumps(da, ensure_ascii=False)[:600])
            continue
        snap.append(rec)
        with open(bk, "w") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.chmod(bk, 0o600)
        cur = find_token(rec["key_alias"], rec["token"])
        if not cur or (cur.get("models") or []) != rec["models"] or (cur.get("aliases") or {}) != rec["aliases"]:
            bad.append((rec["key_alias"], "改前回读变了，跳过")); snap.pop(); continue
        api("/key/update", {"key": rec["token"], "models": new_m, "aliases": new_a})
        back = find_token(rec["key_alias"], rec["token"]) or {}
        if (back.get("models") or []) != new_m or (back.get("aliases") or {}) != new_a:
            bad.append((rec["key_alias"], "写后回读不一致"))
    left = 0
    if apply:
        for k in list_keys(prefixes, only):
            blob = json.dumps({"m": k.get("models"), "a": k.get("aliases")})
            left += "kiro" in blob
    print(f"{cmd} keys={len(keys)} 要改={changed} 无需改={same} 失败={len(bad)} {bad[:5]}"
          + (f" | 改后仍含kiro={left}" if apply else ""))
    for n in notes_all[:40]:
        print("  NOTE", n)
    if len(notes_all) > 40:
        print(f"  NOTE ... 共 {len(notes_all)} 条")
    if apply:
        print("备份", bk)


main()
