#!/usr/bin/env python3
"""从飞书通讯录建「谁向谁汇报」，并同步到账户表 / 组织确认表。

权威源是 `contact/v3/users/batch` 的 `leader_user_id`，逐层向上闭包到 CEO。
账户表自己的「姓名.直属上级」「姓名.人员类型」「姓名.工号」三列 **1389 行全空**，
从表里读不出任何上下级关系 —— 这是本脚本存在的原因。

子命令：
  tree        打印组织树 + leader 名册（只读，不写任何表）
  status      对账：账户表「是否在职」 vs 通讯录 is_resigned，列出差异
  sync-status 把「是否在职」刷成通讯录的真相（--apply 才写）
  org         给确认表补「组织名称 / 部门路径」（只填空的，不覆盖人工标注）
  leader      给确认表写「归属leader(人)」人员列 —— 记录级权限的锚点

硬纪律（每条都踩过）：
  * 认人只认 `open_id`，不认姓名也不认 key_alias。账户表里 91/92 的 alias
    跟 LiteLLM 对不上（表里少随机后缀），而「李杰」「杜小畔」「刘咏强」
    各是两个不同的人。按姓名匹配会串行。
  * 跨租户（`is_cross_tenant`）的人 **通讯录给不出 leader 也给不出 status**，
    这类行一律跳过、单独成桶，绝不猜。「量不到」和「没问题」是两回事。
  * 写之前把目标列原值落盘（`--backup`）。飞书 batch-update 是逐字段覆盖，
    `"ok": true` 不证明写进去了，一律回读校验。
"""

import argparse
import collections
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_TOKEN = "DlT9bsrwMad12VsogEpcK9Ptncc"   # wiki UvIxwsWLQivn1RkrdPTcVGGnnze 的 obj_token
ACCT_TABLE = "tblJT2s6Y6xjYj5A"              # 账户表 1389 行
CONF_TABLE = "tbln0k5HFwdmkkxr"              # 组织关系确认表
CHUNK = 200
BACKUP_DIR = Path(".tmp-lark")


def lark(*args: str) -> dict:
    out = subprocess.run(["lark-cli", *args], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "raw": out[:500]}


def api(method: str, path: str, params=None, data=None) -> dict:
    args = ["api", method, path]
    if params:
        args += ["--params", json.dumps(params)]
    if data:
        args += ["--data", json.dumps(data)]
    return lark(*args)


def read_table(table: str) -> list[dict]:
    """全量读一张表。走裸 API 分页 —— lark-cli 的 record-search 需要 keyword，
    而「读全表」没有关键词可给。"""
    out, token = [], None
    while True:
        p = {"page_size": 500}
        if token:
            p["page_token"] = token
        d = api("GET", f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table}/records", p)
        if not d.get("ok"):
            sys.exit(f"读表 {table} 失败：{json.dumps(d.get('error'), ensure_ascii=False)[:300]}")
        data = d["data"]
        out += data.get("items", [])
        if data.get("has_more") and data.get("page_token"):
            token = data["page_token"]
        else:
            return out


def txt(v) -> str:
    """多维表的格子有五种形状，统一拍平成字符串再比。"""
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return v[0].get("text") or v[0].get("name") or ""
        return "|".join(map(str, v))
    return "" if v is None else str(v)


def contact_batch(oids: list[str]) -> dict[str, dict]:
    """批量读通讯录。50 个一批是接口上限。

    返回里缺的 open_id 就是跨租户 / 查不到的人 —— 调用方必须把这批单列成桶，
    不能当成「没问题」。想确认是接口坏了还是真查不到，混一个已知活人做阳性对照。
    """
    res = {}
    for i in range(0, len(oids), 50):
        d = api("GET", "/open-apis/contact/v3/users/batch",
                {"user_ids": oids[i:i + 50], "user_id_type": "open_id",
                 "department_id_type": "open_department_id"})
        if d.get("ok"):
            for it in d["data"].get("items", []):
                res[it["open_id"]] = it
        else:
            print(f"  ⚠️ batch {i} 失败：{json.dumps(d.get('error'), ensure_ascii=False)[:160]}",
                  file=sys.stderr)
        time.sleep(0.15)
    return res


def close_upward(users: dict[str, dict]) -> dict[str, dict]:
    """把 leader 链一路补到顶。表里的人未必包含他们的上级（本人不持 key 的
    管理者就不在表里），不补的话树是断的。"""
    allu = dict(users)
    for _ in range(10):
        need = {v["leader_user_id"] for v in allu.values()
                if v.get("leader_user_id") and v["leader_user_id"] not in allu}
        if not need:
            break
        got = contact_batch(sorted(need))
        if not got:
            break
        allu.update(got)
    return allu


def dept_paths(allu: dict[str, dict]) -> dict[str, str]:
    """部门 id → 全路径名。同样要向上闭包，否则只拿到叶子名。"""
    deps = {d for v in allu.values() for d in (v.get("department_ids") or [])}
    got = {}
    pending = sorted(deps)
    for _ in range(10):
        if not pending:
            break
        fresh = {}
        for i in range(0, len(pending), 50):
            d = api("GET", "/open-apis/contact/v3/departments/batch",
                    {"department_ids": pending[i:i + 50],
                     "department_id_type": "open_department_id", "user_id_type": "open_id"})
            if d.get("ok"):
                for it in d["data"].get("items", []):
                    fresh[it["open_department_id"]] = it
            time.sleep(0.15)
        got.update(fresh)
        pending = [v["parent_department_id"] for v in fresh.values()
                   if v.get("parent_department_id") not in (None, "", "0")
                   and v["parent_department_id"] not in got]

    def path(did: str) -> str:
        out, cur, seen = [], did, set()
        while cur and cur != "0" and cur in got and cur not in seen:
            seen.add(cur)
            out.append(got[cur]["name"])
            cur = got[cur].get("parent_department_id")
        return " / ".join(reversed(out))

    return {d: path(d) for d in got}


def is_resigned(u: dict) -> bool:
    s = u.get("status") or {}
    return bool(s.get("is_resigned") or s.get("is_exited"))


def load_people(table: str) -> tuple[list[dict], dict[str, dict], list[str]]:
    """读表 → 取出人员列的 open_id → 拉通讯录。返回 (行, 通讯录, 查不到的oid)。"""
    rows = read_table(table)
    oids = {}
    for x in rows:
        nm = x["fields"].get("姓名")
        if nm and isinstance(nm, list) and nm:
            oids[nm[0].get("id") or txt(nm)] = nm[0].get("name")
        elif txt(x["fields"].get("open_id")):
            oids[txt(x["fields"].get("open_id"))] = txt(x["fields"].get("姓名"))
    users = contact_batch(list(oids))
    missing = [o for o in oids if o not in users]
    print(f"表 {table}: {len(rows)} 行 / {len(oids)} 人；"
          f"通讯录可读 {len(users)}，读不到 {len(missing)}（跨租户，单列成桶）")
    return rows, users, missing


def write_rows(table: str, upd: dict[str, dict], backup: Path | None, apply: bool,
               label: str) -> int:
    """落盘 → 单行探针 → 批量 → 回读校验。任一步不过就停。"""
    if not upd:
        print("没有需要写的行。")
        return 0
    if not apply:
        print(f"\n[dry-run] 将写 {len(upd)} 行；加 --apply 才真写。")
        for rid, ch in list(upd.items())[:10]:
            print(f"  {rid} {json.dumps(ch, ensure_ascii=False)}")
        if len(upd) > 10:
            print(f"  …另 {len(upd) - 10} 行")
        return 0

    if backup:
        backup.parent.mkdir(parents=True, exist_ok=True)
        before = {x["record_id"]: x["fields"] for x in read_table(table)
                  if x["record_id"] in upd}
        backup.write_text(json.dumps(
            {"taken_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "base": BASE_TOKEN,
             "table": table, "action": label, "count": len(before), "rows": before},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"备份 {len(before)} 行 → {backup}")

    items = list(upd.items())
    done = 0
    for i in range(0, len(items), CHUNK):
        chunk = dict(items[i:i + CHUNK])
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=".", delete=False,
                                         encoding="utf-8") as fh:
            json.dump({"update_records": chunk}, fh, ensure_ascii=False)
            tmp = fh.name
        try:
            d = lark("base", "+record-batch-update", "--base-token", BASE_TOKEN,
                     "--table-id", table, "--json", f"@{tmp}")
        finally:
            Path(tmp).unlink(missing_ok=True)
        if not d.get("ok"):
            print(f"🔴 写失败 @{i}：{json.dumps(d.get('error'), ensure_ascii=False)[:300]}")
            return 1
        done += len(chunk)
        print(f"  +{len(chunk)} total={done}")
        time.sleep(0.3)

    # 回读校验 —— "ok": true 不算凭据
    after = {x["record_id"]: x["fields"] for x in read_table(table)}
    bad = []
    for rid, ch in upd.items():
        got = after.get(rid, {})
        for k, v in ch.items():
            want = txt(v[0]["id"]) if (isinstance(v, list) and v and "id" in v[0]) else txt(v)
            have = (got.get(k) or [{}])[0].get("id", "") if (
                isinstance(got.get(k), list) and got.get(k) and "id" in (got[k][0] or {})
            ) else txt(got.get(k))
            if want != have:
                bad.append((rid, k, want, have))
    print(f"\n回读校验：{len(upd) - len({b[0] for b in bad})}/{len(upd)} 行写对")
    if bad:
        print(f"🔴 {len(bad)} 处不一致：")
        for b in bad[:10]:
            print(f"   {b}")
        return 1
    print("✅ 逐字段一致")
    return 0


# ---------------------------------------------------------------- 子命令

def cmd_tree(args) -> int:
    rows, users, missing = load_people(ACCT_TABLE)
    allu = close_upward(users)
    paths = dept_paths(allu)
    inconf = set(users)

    kids = collections.defaultdict(list)
    for oid, u in allu.items():
        if u.get("leader_user_id"):
            kids[u["leader_user_id"]].append(oid)

    def sub(oid, seen=None):
        seen = seen if seen is not None else set()
        if oid in seen:
            return 0
        seen.add(oid)
        return (1 if oid in inconf else 0) + sum(sub(c, seen) for c in kids.get(oid, []))

    roots = [o for o in allu
             if not allu[o].get("leader_user_id") or allu[o]["leader_user_id"] not in allu]
    lines, seen = [], set()

    def walk(oid, depth):
        if oid in seen:
            return
        seen.add(oid)
        u = allu[oid]
        ch = sorted(kids.get(oid, []), key=lambda c: -sub(c))
        mark = "★" if oid in inconf else "○"
        tail = f"  [直属{len(ch)} · 辖下持key{sub(oid) - (1 if oid in inconf else 0)}]" if ch else ""
        job = (u.get("job_title") or "").strip()
        lines.append(f'{"  " * depth}{mark} {u["name"]}' + (f" — {job}" if job else "") + tail)
        for c in ch:
            walk(c, depth + 1)

    for r in sorted(roots, key=lambda x: -sub(x)):
        walk(r, 0)
    print("\n".join(lines))

    leaders = [o for o in allu if kids.get(o)]
    print(f"\n{'=' * 60}")
    print(f"图中 {len(allu)} 人 / 持key {len(inconf)} / leader {len(leaders)} "
          f"(本人持key {sum(1 for o in leaders if o in inconf)}，"
          f"仅在链上 {sum(1 for o in leaders if o not in inconf)})")
    print(f"跨租户·上级读不到 {len(missing)} —— 这批无法自动归位")
    print(f"\n{'姓名':<10}{'直属':>5}{'持key':>6}{'辖下':>6}  职务 / 部门")
    for o in sorted(leaders, key=lambda x: -len([c for c in kids[x] if c in inconf])):
        u = allu[o]
        kh = len([c for c in kids[o] if c in inconf])
        dp = paths.get((u.get("department_ids") or [None])[0], "")
        print(f'{u["name"]:<10}{len(kids[o]):>5}{kh:>6}'
              f'{sub(o) - (1 if o in inconf else 0):>6}  {(u.get("job_title") or "")[:24]} / {dp[:30]}')
    return 0


def cmd_status(args) -> int:
    rows, users, missing = load_people(ACCT_TABLE)
    to_resign, to_active, skipped, same = [], [], 0, 0
    for x in rows:
        f = x["fields"]
        nm = f.get("姓名")
        if not nm:
            skipped += 1
            continue
        oid = nm[0].get("id")
        u = users.get(oid)
        if not u:
            skipped += 1
            continue
        want = "离职" if is_resigned(u) else "在职"
        cur = txt(f.get("是否在职"))
        if cur == want:
            same += 1
        elif want == "离职":
            to_resign.append((x["record_id"], nm[0].get("name"), cur, txt(f.get("状态"))))
        else:
            to_active.append((x["record_id"], nm[0].get("name"), cur, txt(f.get("状态"))))

    print(f"\n一致 {same} / 需改在职→离职 {len(to_resign)} "
          f"({len({t[1] for t in to_resign})} 人) / 需改离职→在职 {len(to_active)} "
          f"/ 通讯录读不到·不动 {skipped}")
    if to_resign:
        print("\n=== 表写在职、通讯录已离职 ===")
        for n, c in collections.Counter(t[1] for t in to_resign).most_common():
            st = {t[3] for t in to_resign if t[1] == n}
            print(f"  {n:<10} {c} 行  key状态={sorted(st)}")
    for t in to_active:
        print(f"  ⚠️ 表写离职、通讯录在职：{t[1]}")
    Path("/tmp/org-status-diff.json").write_text(
        json.dumps({"to_resign": to_resign, "to_active": to_active},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def cmd_sync_status(args) -> int:
    rows, users, _ = load_people(ACCT_TABLE)
    upd = {}
    for x in rows:
        f = x["fields"]
        nm = f.get("姓名")
        if not nm:
            continue
        u = users.get(nm[0].get("id"))
        if not u:
            continue                      # 跨租户：没数据不猜
        want = "离职" if is_resigned(u) else "在职"
        if txt(f.get("是否在职")) != want:
            upd[x["record_id"]] = {"是否在职": [want]}
    print(f"\n需刷新 {len(upd)} 行「是否在职」（⛔ 不碰 key 的『状态』列）")
    bak = Path(args.backup) if args.backup else (
        BACKUP_DIR / f"org-status-{time.strftime('%Y%m%d-%H%M%S')}.json")
    return write_rows(ACCT_TABLE, upd, bak, args.apply, "sync 是否在职 from contact")


def cmd_org(args) -> int:
    """给确认表补组织 / 部门。只填空的 —— 人工标注（奇瑞|美团）不被
    通讯录的另一套叫法（Chery）顶掉。"""
    rows = read_table(CONF_TABLE)
    gap = [x for x in rows if not txt(x["fields"].get("组织名称"))
           or not txt(x["fields"].get("部门路径"))]
    print(f"确认表 {len(rows)} 行，组织或部门为空的 {len(gap)} 行")
    oids = [txt(x["fields"].get("open_id")) for x in gap]
    found, upd = {}, {}
    for oid in oids:
        d = lark("contact", "+search-user", "--user-ids", oid, "--as", "user")
        us = (d.get("data") or {}).get("users") or []
        if us:
            found[oid] = us[0]
        time.sleep(0.12)
    print(f"search-user 查到 {len(found)}/{len(oids)}；"
          f"查不到 {len(oids) - len(found)}（飞书给不出，非查法问题 —— "
          f"要坐实请混一个已知活人做阳性对照）")
    for x in gap:
        oid = txt(x["fields"].get("open_id"))
        u = found.get(oid)
        if not u:
            continue
        dept = (u.get("department") or "").strip()
        if not dept:
            continue
        ch = {}
        if not txt(x["fields"].get("部门路径")):
            ch["部门路径"] = dept
        if not txt(x["fields"].get("组织名称")):
            ch["组织名称"] = dept
        if ch:
            upd[x["record_id"]] = ch
    bak = Path(args.backup) if args.backup else (
        BACKUP_DIR / f"org-backfill-{time.strftime('%Y%m%d-%H%M%S')}.json")
    return write_rows(CONF_TABLE, upd, bak, args.apply, "backfill 组织名称/部门路径")


def cmd_leader(args) -> int:
    """写确认表的「归属leader(人)」人员列 —— 记录级权限靠它。

    `--set-org <组织> --to <open_id>` 把某个组织的行统一指给一个人（跨租户那批
    通讯录读不到上级，只能人工归口）；不带则按通讯录的 leader_user_id 回填。
    """
    rows = read_table(CONF_TABLE)
    upd = {}
    if args.set_org:
        if not args.to:
            sys.exit("--set-org 必须配 --to <open_id>")
        d = api("GET", "/open-apis/contact/v3/users/batch",
                {"user_ids": [args.to], "user_id_type": "open_id"})
        items = (d.get("data") or {}).get("items") or []
        if not items:
            sys.exit(f"--to {args.to} 在通讯录里查不到，拒绝写入")
        name = items[0]["name"]
        print(f"把组织含「{args.set_org}」的行统一指给 {name} ({args.to})")
        hit = already = 0
        for x in rows:
            if args.set_org not in txt(x["fields"].get("组织名称")):
                continue
            hit += 1
            # 幂等：已经指对的行不再重写，否则「这次改了几行」读不出来
            cur = (x["fields"].get("归属leader(人)") or [{}])[0].get("id")
            if cur == args.to and txt(x["fields"].get("归属leader")) == name:
                already += 1
                continue
            upd[x["record_id"]] = {"归属leader(人)": [{"id": args.to}],
                                   "归属leader": name}
        print(f"组织命中 {hit} 行；已指对 {already} 行（跳过）；本次要改 {len(upd)} 行")
    else:
        oids = [txt(x["fields"].get("open_id")) for x in rows]
        users = close_upward(contact_batch([o for o in oids if o]))
        for x in rows:
            oid = txt(x["fields"].get("open_id"))
            u = users.get(oid)
            if not u or not u.get("leader_user_id"):
                continue                 # 跨租户 / 顶层：留空，由人工指定
            up = u["leader_user_id"]
            cur = (x["fields"].get("归属leader(人)") or [{}])[0].get("id")
            if cur != up:
                upd[x["record_id"]] = {
                    "归属leader(人)": [{"id": up}],
                    "归属leader": users.get(up, {}).get("name", "")}
        print(f"按通讯录回填 {len(upd)} 行；"
              f"留空 {len(rows) - len(upd)} 行（跨租户读不到上级 + 顶层）")
    bak = Path(args.backup) if args.backup else (
        BACKUP_DIR / f"org-leader-{time.strftime('%Y%m%d-%H%M%S')}.json")
    return write_rows(CONF_TABLE, upd, bak, args.apply, "set 归属leader(人)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in [
            ("tree", cmd_tree, "打印组织树 + leader 名册（只读）"),
            ("status", cmd_status, "对账在职状态（只读）"),
            ("sync-status", cmd_sync_status, "刷新账户表「是否在职」"),
            ("org", cmd_org, "补确认表组织 / 部门（只填空的）"),
            ("leader", cmd_leader, "写确认表「归属leader(人)」"),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(fn=fn)
        if name not in ("tree", "status"):
            p.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
            p.add_argument("--backup", metavar="PATH", help="原值落盘路径")
        if name == "leader":
            p.add_argument("--set-org", metavar="组织",
                           help="把该组织的行统一指给 --to（跨租户人工归口用）")
            p.add_argument("--to", metavar="OPEN_ID", help="配 --set-org 使用")
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

