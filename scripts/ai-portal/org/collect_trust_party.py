#!/usr/bin/env python3
"""只读：用 lark-cli 主 profile（user 身份）列全部关联租户 + BFS 可见成员。
输出 trust_party.json：{tenants: [{tenant_key, name, connect_time, depts, users, sample_has_open_id, sample_has_union_id}],
                        users: {union_id: {open_id, name, tenant_key}}}
不打印 token。"""
import json, subprocess, sys, time
from pathlib import Path

OUT = Path(__file__).with_name("trust_party.json")


def lark(*args, tries=4):
    for i in range(tries):
        r = subprocess.run(["lark-cli", *args], capture_output=True, text=True)
        s = r.stdout if "{" in r.stdout else r.stderr
        if "{" in s:
            return json.loads(s[s.index("{"):])
        time.sleep(1.5 * (i + 1))
    return {"ok": False, "raw": f"empty rc={r.returncode}"}


def get(path, params):
    d = lark("api", "GET", path, "--params", json.dumps(params), "--as", "user")
    if not d.get("ok"):
        sys.exit(f"失败 {path}: {json.dumps(d.get('error'), ensure_ascii=False)[:300]}")
    return d["data"]


tenants = get("/open-apis/trust_party/v1/collaboration_tenants", {"page_size": 50}).get("target_tenant_list") or []
res = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "identity": "user(lark-cli 主 profile)",
       "tenants": [], "users": {}}
for t in tenants:
    tk, tn = t["tenant_key"], t.get("tenant_name") or t.get("tenant_short_name")
    queue, seen, n_users, n_open, n_union = ["0"], set(), 0, 0, 0
    while queue:
        dept = queue.pop(0)
        if dept in seen:
            continue
        seen.add(dept)
        tok = None
        while True:
            p = {"target_department_id": dept, "page_size": 100, "department_id_type": "open_department_id"}
            if tok:
                p["page_token"] = tok
            data = get(f"/open-apis/trust_party/v1/collaboration_tenants/{tk}/visible_organization", p)
            for e in data.get("collaboration_entity_list") or []:
                if e["collaboration_entity_type"] == "department":
                    queue.append(e["open_department_id"])
                elif e["collaboration_entity_type"] == "user":
                    n_users += 1
                    n_open += bool(e.get("open_user_id"))
                    n_union += bool(e.get("union_user_id"))
                    key = e.get("union_user_id") or e.get("open_user_id")
                    res["users"][key] = {"open_id": e.get("open_user_id"), "union_id": e.get("union_user_id"),
                                         "name": e.get("user_name"), "tenant_key": tk}
            tok = data.get("page_token")
            if not data.get("has_more") or not tok:
                break
    res["tenants"].append({"tenant_key": tk, "name": tn, "connect_time": t.get("connect_time"),
                           "depts": len(seen), "users": n_users, "with_open_id": n_open, "with_union_id": n_union})
    print(f"{tk} {tn}: 部门 {len(seen)} 可见 {n_users} 人 (open_id {n_open} / union_id {n_union})")
OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
print(f"写入 {OUT}，去重后 {len(res['users'])} 人")
