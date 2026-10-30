#!/usr/bin/env python3
"""给已有 per-key 兜底表的 key 追加一行：<group>: [targets...]（只追加，不动其他行）。

为什么需要：key 的 router_settings.fallbacks 非空时**整张替换**全局表，全局新加的行
对这些 key 不生效（2026-10-15：550 把 claude-code key 的表里没有 deepseek 行）。

只处理同时满足的 key：per-key 表非空 + 这把 key 真能路由到 <group>
（aliases 的某个值 == group，或 group 在 models 里）+ 表里还没有 <group> 行。
已有该行 = 跳过（幂等）；空表 key 不碰（它们读全局表，全局行已经生效）。

「先补兜底、再改映射」时，此刻还没有 key 路由到新组，用 ``--for-alias-source <对外名>``
按「有这个 alias 的 key」选（10-15 her-pro 改指新官方组就是这样做的）。

在 198 上跑（127.0.0.1:30402）::

    export LITELLM_MASTER_KEY=...
    python3 S --audit --group G                               # 只读：谁能路由到 G 但 per-key 表缺 G 行
    python3 S --group G --target T1 --target T2               # dry-run
    python3 S --group G --target T1 ... --limit 1 --apply --backup f.json
    python3 S --group G --target T1 ... --apply --backup f.json
    python3 S --restore f.json --apply                        # 回滚（整表恢复为快照）

全局行写完后必跑 ``--audit``：缺行的 key 在 G 挂掉时**零兜底且零报错**。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {MASTER}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def all_keys() -> list[dict]:
    out, page = [], 1
    while True:
        st, j = api("GET", f"/key/list?page={page}&size=100&return_full_object=true")
        if st != 200 or not isinstance(j, dict):
            sys.exit(f"FATAL: /key/list page {page} -> {st} {j!r:.200}")
        rows = j.get("keys") or []
        out += [r for r in rows if isinstance(r, dict)]
        if page >= int(j.get("total_pages") or 1):
            break
        page += 1
    if not out:
        sys.exit("FATAL: /key/list returned zero keys -- broken read, refusing")
    return out


def table_of(k: dict) -> list:
    rs = k.get("router_settings") or {}
    return list(rs.get("fallbacks") or [])


def reaches(k: dict, group: str) -> bool:
    return group in (k.get("models") or []) or group in (k.get("aliases") or {}).values()


def plan(keys: list[dict], group: str, targets: list[str], alias_source: str | None = None) -> list[tuple[dict, list]]:
    # alias_source：要先补兜底、再把某个对外名改指到 group 时用（此刻还没有 key 路由到 group）
    todo = []
    for k in keys:
        t = table_of(k)
        hit = alias_source in (k.get("aliases") or {}) if alias_source else reaches(k, group)
        if not t or not hit or any(group in row for row in t if isinstance(row, dict)):
            continue
        todo.append((k, t + [{group: targets}]))
    return todo


def audit(keys: list[dict], group: str) -> dict[str, list[str]]:
    """能路由到 group 的 key 按「读哪张表」分组；missing = per-key 表非空却没有 group 行。"""
    out: dict[str, list[str]] = {"global_table": [], "per_key_has_row": [], "missing": []}
    for k in keys:
        if not reaches(k, group):
            continue
        t = table_of(k)
        if not t:
            out["global_table"].append(k.get("key_alias") or "?")
        elif any(group in row for row in t if isinstance(row, dict)):
            out["per_key_has_row"].append(k.get("key_alias") or "?")
        else:
            out["missing"].append(k.get("key_alias") or "?")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group")
    ap.add_argument("--target", action="append", default=[])
    ap.add_argument("--for-alias-source", help="选有这个对外名 alias 的 key（改映射之前先补兜底）")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only-alias", action="append", default=[],
                    help="只处理这些 key_alias（可重复）；其余符合条件的 key 一把不碰")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup")
    ap.add_argument("--restore")
    ap.add_argument("--audit", action="store_true", help="只读：按读哪张表分组，列出缺行的 key，有缺行 exit 1")
    a = ap.parse_args()
    if not MASTER:
        sys.exit("FATAL: LITELLM_MASTER_KEY not set")

    if a.audit:
        if not a.group:
            sys.exit("FATAL: --audit needs --group")
        r = audit(all_keys(), a.group)
        print(f"group={a.group} reach: global_table={len(r['global_table'])} "
              f"per_key_has_row={len(r['per_key_has_row'])} missing={len(r['missing'])}")
        for alias in r["missing"][:20]:
            print(f"  MISSING {alias}")
        return 1 if r["missing"] else 0

    if a.restore:
        snap = json.load(open(a.restore))
        print(f"restore {len(snap)} keys from {a.restore}" + ("" if a.apply else " (dry-run)"))
        if not a.apply:
            return 0
        bad = 0
        for alias, rs in snap.items():
            st, j = api("POST", "/key/update", {"key_alias": alias, "router_settings": rs})
            bad += st != 200
            if st != 200:
                print(f"  {alias}: {st} {j!r:.200}")
        print(f"restored ok={len(snap) - bad} fail={bad}")
        return 1 if bad else 0

    if not a.group or not a.target:
        sys.exit("FATAL: --group and at least one --target required")
    keys = all_keys()
    todo = plan(keys, a.group, a.target, a.for_alias_source)
    if a.only_alias:
        todo = [(k, new) for k, new in todo if k.get("key_alias") in set(a.only_alias)]
        missing = set(a.only_alias) - {k.get("key_alias") for k, _ in todo}
        if missing:
            sys.exit(f"FATAL: --only-alias not eligible (no per-key table / cannot reach group / row exists): {sorted(missing)}")
    if a.limit:
        todo = todo[: a.limit]
    with_tbl = sum(1 for k in keys if table_of(k))
    print(f"keys={len(keys)} with_per_key_table={with_tbl} to_append={len(todo)} row={{{a.group!r}: {a.target}}}")
    for k, new in todo[:3]:
        print(f"  sample {k.get('key_alias')}: rows {len(new) - 1} -> {len(new)}")
    if not a.apply:
        print("dry-run: no writes")
        return 0
    if not a.backup:
        sys.exit("FATAL: --apply requires --backup")
    snap = {k["key_alias"]: (k.get("router_settings") or {}) for k, _ in todo}
    json.dump(snap, open(a.backup, "w"), ensure_ascii=False, indent=1)
    print(f"backup={a.backup} keys={len(snap)}")
    ok = bad = 0
    for k, new in todo:
        rs = dict(k.get("router_settings") or {})
        rs["fallbacks"] = new
        st, j = api("POST", "/key/update", {"key_alias": k["key_alias"], "router_settings": rs})
        if st == 200:
            ok += 1
        else:
            bad += 1
            print(f"  {k['key_alias']}: {st} {j!r:.200}")
    print(f"applied_ok={ok} applied_fail={bad}")
    # 换尺子回读：重新拉全量，逐把核对「旧表原样 + 恰好多这一行」
    after = {k.get("key_alias"): table_of(k) for k in all_keys()}
    mism = [k["key_alias"] for k, new in todo if after.get(k["key_alias"]) != new]
    print(f"readback_ok={len(todo) - len(mism)}/{len(todo)} mismatches={len(mism)} {mism[:5]}")
    return 1 if (bad or mism) else 0


if __name__ == "__main__":
    sys.exit(main())
