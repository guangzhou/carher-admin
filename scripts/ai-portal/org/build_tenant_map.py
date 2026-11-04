#!/usr/bin/env python3
"""合并三个来源 ⇒ tenant_map.json(门户 TENANT_MAP_PATH 读它)。只读本地文件,不调飞书。

  (a) trust_party.json      关联组织(collect_trust_party.py,user 身份)—— 权威,可自动刷新
  (b) manual_tenants.json   实测 tenant_key + 公司名证据,人工维护
  (c) 拿不到的(个人版/未知公司)—— 不进映射 ⇒ 门户拒绝

输出 {tenant_key: {name, org, full_name, source, verified_at}} + "_meta"(含 key 表「组织名称」选项快照)。
org = 写进 key 表「组织名称」的值,**必须是表里已有选项**;关联组织里没有对应选项的公司 org=null
(门户把 org=null 当「还没开通」拒绝,等用户拍板选哪个选项后写进 TRUST_ORG)。

  python3 build_tenant_map.py            # 打印覆盖率,写 tenant_map.json
"""
import collections, json, sys, time
from pathlib import Path

D = Path(__file__).resolve().parent
OUT = D / "tenant_map.json"
OUR_TENANT = "1088406f1c155740"

# 关联租户 → key 表选项。只填有证据的:上海车联 = 可见 131 人名下 261 行,表上组织名称 100% 是「上海车联」。
# 其余 4 个(无锡车联广州/深圳分公司、无锡图灵、芜湖车联)表里没有对应选项、名下也没有 key 行 ⇒ 等用户定。
TRUST_ORG = {"153f0ee88f0a575e": "上海车联"}
SHORT = {"153f0ee88f0a575e": "上海车联"}

tp = json.loads((D / "trust_party.json").read_text())
kt = json.loads((D / "key_table.json").read_text())
manual = {k: v for k, v in json.loads((D / "manual_tenants.json").read_text()).items() if not k.startswith("_")}
options = list(dict.fromkeys(kt["org_options"]))

if not tp.get("tenants"):
    sys.exit("关联组织列表是空的(多半 user token 失效或走成了 bot 身份)⇒ 不覆盖旧映射")

out = {"_meta": {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "org_options": options,
                 "trust_party_collected_at": tp["collected_at"], "key_table_read_at": kt["read_at"],
                 "note": "org=null ⇒ 门户拒绝(公司已识别但 key 表没有对应选项)"}}
for t in tp["tenants"]:
    tk = t["tenant_key"]
    org = TRUST_ORG.get(tk)
    out[tk] = {"name": SHORT.get(tk) or t["name"], "org": org, "full_name": t["name"], "source": "trust_party",
               "verified_at": tp["collected_at"][:10], "visible_users": t["users"]}
for tk, v in manual.items():
    if tk in out:
        continue                         # 关联组织优先(权威)
    out[tk] = {"name": v["name"], "org": v["org"], "full_name": v["name"], "source": "manual",
               "verified_at": v["verified_at"]}
for tk, v in out.items():
    if tk == "_meta":
        continue
    assert tk != OUR_TENANT, "本租户不该进映射"
    assert v["org"] is None or v["org"] in options, (tk, v["org"], "不是 key 表里已有的选项")

# ── 覆盖率:key 表里外部人的行(组织名称非空且 ≠ 无锡车联)按持有人实测 tenant_key 判 ──
p2p = json.loads((D / "../../baic-friends/p2pall.json").read_text())["data"]["chats"]
srch = json.loads((D / "unmeasured-search.json").read_text())
oid_tk = {}
for u in tp["users"].values():
    oid_tk[u["open_id"]] = u["tenant_key"]
for c in p2p:
    oid_tk.setdefault(c.get("owner_id"), c["tenant_key"])
for oid, v in srch.items():
    if v.get("tenant_key"):
        oid_tk.setdefault(oid, v["tenant_key"])
oid_tk.setdefault("ou_10c952023d08e525e32b7f21b78e2d6e", "2e9e916eff8f1650")   # 周奇烽(群成员实测)

rows = [r for r in kt["rows"] if r.get("组织名称") and r["组织名称"] != "无锡车联"]
cov = collections.defaultdict(collections.Counter)
for r in rows:
    ids = [p["id"] for p in (r["person"] or []) if isinstance(p, dict) and p.get("id")]
    tks = {oid_tk[i] for i in ids if i in oid_tk}
    if not tks:
        b = "未测到 tenant_key(关联组织里看不见/没聊过)"
    elif tks == {OUR_TENANT}:
        b = "持有人是本租户(内部人代持)"
    elif any(out.get(t, {}).get("org") for t in tks):
        b = "放行:映射命中"
    else:
        b = "拒绝:租户不在映射"
    cov[b][r["组织名称"]] += 1
print("外部人行(组织名称≠无锡车联且非空) = %d" % len(rows))
for b, c in sorted(cov.items()):
    print("  %-40s %4d  %s" % (b, sum(c.values()), dict(c)))
out["_meta"]["coverage"] = {b: dict(c) for b, c in cov.items()}
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
print("写入 %s:%d 个租户(org 有效 %d)" % (OUT, len(out) - 1, sum(1 for k, v in out.items() if k != "_meta" and v["org"])))
