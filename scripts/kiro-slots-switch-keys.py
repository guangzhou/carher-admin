#!/usr/bin/env python3
"""把一批 key 的 kiro 别名切到 kiro-slots-*（或切回）。在 198 上跑，打 127.0.0.1:30402。

  python3 kiro-slots-switch-keys.py to-slots  keys.txt  --backup B.json   # kiro-X -> kiro-slots-X
  python3 kiro-slots-switch-keys.py to-old    keys.txt  --backup B.json   # kiro-slots-X -> kiro-X
  python3 kiro-slots-switch-keys.py restore   B.json                      # 按备份逐 key 还原 aliases

- 只改「值以 kiro- 开头且目标 kiro-slots-X 组存在」的别名；其它别名一字不动（read-merge-write）。
- 守卫：某个别名当前值不是预期旧值就跳过它（别人刚改过）。
- 每 key 写完回读比对；先写备份再动手。dry-run 默认。
"""
import json, os, sys, urllib.request

BASE = "http://127.0.0.1:30402"
MK = os.environ["LITELLM_MASTER_KEY"]


def api(path, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                               method="POST" if body is not None else "GET",
                               headers={"Authorization": "Bearer " + MK, "content-type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=60))


def key_by_alias(alias):
    d = api("/key/list?key_alias=%s&return_full_object=true&size=5" % urllib.request.quote(alias))
    ks = [k for k in d.get("keys", []) if isinstance(k, dict) and k.get("key_alias") == alias]
    return ks[0] if len(ks) == 1 else None


def live_groups():
    d = api("/v1/model/info")["data"]
    return {m["model_name"] for m in d}


def plan(aliases, direction, groups):
    out = {}
    for n, t in aliases.items():
        t = str(t)
        if direction == "to-slots" and t.startswith("kiro-") and not t.startswith("kiro-slots-"):
            new = "kiro-slots-" + t[len("kiro-"):]
        elif direction == "to-old" and t.startswith("kiro-slots-"):
            new = "kiro-" + t[len("kiro-slots-"):]
        else:
            continue
        if new in groups:
            out[n] = (t, new)
    return out


def main():
    mode = sys.argv[1]
    apply = "--apply" in sys.argv
    if mode == "restore":
        snap = json.load(open(sys.argv[2]))
        for alias, rec in snap.items():
            if apply:
                api("/key/update", {"key": rec["token"], "aliases": rec["aliases"]})
            print(("RESTORE " if apply else "PLAN-RESTORE ") + alias)
        return
    names = [l.strip() for l in open(sys.argv[2]) if l.strip()]
    bk = sys.argv[sys.argv.index("--backup") + 1] if "--backup" in sys.argv else None
    if apply and not bk:
        sys.exit("--apply 需要 --backup")
    groups = live_groups()
    snap, ok, skip, fail = {}, 0, 0, []
    for a in names:
        k = key_by_alias(a)
        if not k:
            fail.append((a, "找不到/不唯一")); continue
        cur = dict(k.get("aliases") or {})
        p = plan(cur, mode, groups)
        if not p:
            skip += 1; continue
        snap[a] = {"token": k["token"], "aliases": cur}
        if not apply:
            ok += 1; continue
        if bk:
            json.dump(snap, open(bk, "w"), ensure_ascii=False); os.chmod(bk, 0o600)
        merged = dict(cur)
        for n, (old, new) in p.items():
            if merged.get(n) == old:
                merged[n] = new
        api("/key/update", {"key": k["token"], "aliases": merged})
        back = dict((key_by_alias(a) or {}).get("aliases") or {})
        if back == merged:
            ok += 1
        else:
            fail.append((a, "回读不一致"))
    print("%s 计划/完成=%d 无需改=%d 失败=%d %s" % (mode + (" APPLY" if apply else " DRY"), ok, skip, len(fail), fail[:5]))
    if bk and apply:
        print("备份", bk)


main()
