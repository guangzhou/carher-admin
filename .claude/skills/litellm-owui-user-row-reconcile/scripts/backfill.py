#!/usr/bin/env python3
"""把「有 key 但没人行」的用户从飞书表补进 LiteLLM_UserTable。

背景（2026-09-15 量到的）：建 key 和建人行是两条独立的路。申请页建 key 时
metadata 只有 {purpose, owner_name}；人行靠飞书批量同步补，历史上只跑过 6 次
（04-14 / 06-12 / 06-18 / 08-03 / 08-25 / 09-04）。1257 对 key↔人行里人行全都
晚于 key，最短滞后 1 天，最长 57 天。夹在两次同步之间建号的人，key 能用，但
key-swap-proxy 的门禁只认 /user/info 返回 200 + keys 非空 —— 人行不存在就是
404 就是拒，OWUI 那边显示成假的 `Model '' was not found`。

为什么不做数据库 trigger：db pod 里只有 psql，python3/curl 全 MISSING，
trigger 在库里拿不到飞书的部门/职务；而且 trigger 挂在建 key 的必经路上，
一报错就建不出 key。对账器最多几分钟延迟，但碰不到建号那条路。

为什么直写库而不用 /user/new：实测（2026-09-15）API 路径产出的行跟存量 1287 行
形状不一致 —— organization_id 列不写、teams 数组被填（存量是空的）、
TeamMembership 行不建、budget_id 不带。直写库才能对齐存量。

默认 dry-run，只有 --apply 才写。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import re
import subprocess
import sys
import urllib.error
import urllib.request

BASE_TOKEN = "DlT9bsrwMad12VsogEpcK9Ptncc"
TABLE_ID = "tblJT2s6Y6xjYj5A"
NS_DB = "litellm-product"
DB_POD = "litellm-db-0"
DB_USER = "litellm"
DB_NAME = "litellm"
NS_TERM = "open-webui"
TERM_LABEL = "app=open-terminal"
TERM_USER = "u21c0ece0"

# 门禁只对这两个前缀的身份查 /user/info，所以只有这两类需要人行。
PREFIXES = ("cursor-", "claude-code-")
USER_ID_RE = re.compile(r"^(?:cursor|claude-code)-[A-Za-z0-9._-]+$")
TEAM_ID_RE = re.compile(r"^team-[0-9a-f]+$")
ORG_ID_RE = re.compile(r"^org-[0-9a-z-]+$")

EMAIL_DOMAIN = "auto-link.com.cn"
DEFAULT_ORG = "org-wuxi-chelian"


class Fail(RuntimeError):
    pass


def run(cmd: list[str], stdin: str | None = None, timeout: int = 120) -> str:
    p = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, timeout=timeout
    )
    if p.returncode != 0:
        raise Fail(f"{' '.join(cmd[:4])}... rc={p.returncode}\n{p.stderr[-800:]}")
    return p.stdout


def psql(sql: str, timeout: int = 120) -> str:
    """单条 SQL 走 stdin 喂 psql -f -。

    注意 kubectl exec 必须带 -i，否则 psql 读到空 stdin、静默 exit 0，
    看起来成功但一个字都没执行（2026-09-15 踩过）。
    """
    return run(
        [
            "kubectl", "-n", NS_DB, "exec", "-i", DB_POD, "--",
            "psql", "-U", DB_USER, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1",
            "-At", "-F", "\x1f", "-f", "-",
        ],
        stdin=sql,
        timeout=timeout,
    )


def rows(sql: str) -> list[list[str]]:
    out = psql(sql)
    return [line.split("\x1f") for line in out.splitlines() if line.strip()]


def term_pod() -> str:
    out = run([
        "kubectl", "-n", NS_TERM, "get", "pod", "-l", TERM_LABEL,
        "--field-selector=status.phase=Running", "-o", "name",
    ])
    names = [x.strip().split("/")[-1] for x in out.splitlines() if x.strip()]
    if not names:
        raise Fail(f"no Running pod for {TERM_LABEL} in {NS_TERM}")
    return names[0]


FEISHU_FIELDS = [
    "邮箱前缀", "key_alias", "姓名", "姓名.部门", "姓名.职务",
    "组织名称", "部门名称", "open_id", "状态",
]


def feishu_lookup(pod: str, owner: str) -> list[dict]:
    """按 key_alias 搜飞书表。搜 key_alias 而不是邮箱前缀，因为 hex 占位名
    （ee0661f9 那类）的邮箱前缀就是那串 hex，但 key_alias 一定对得上。"""
    cmd = [
        "kubectl", "-n", NS_TERM, "exec", pod, "--",
        "sudo", "-u", TERM_USER, "env", f"HOME=/home/{TERM_USER}",
        "lark-cli", "base", "+record-search",
        "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
        "--keyword", owner, "--search-field", "key_alias",
        "--format", "json", "--limit", "50",
    ]
    for f in FEISHU_FIELDS:
        cmd += ["--field-id", f]
    data = json.loads(run(cmd, timeout=90))
    if not data.get("ok"):
        raise Fail(f"lark-cli not ok for {owner}: {str(data)[:300]}")
    d = data.get("data") or {}
    names = d.get("fields") or []
    out = []
    for row in d.get("data") or []:
        out.append(dict(zip(names, row)))
    return out


def scalar(v):
    """飞书单选/人员字段一律是 list，取第一个;人员字段取 name。"""
    if isinstance(v, list):
        if not v:
            return None
        v = v[0]
    if isinstance(v, dict):
        return v.get("name") or v.get("text") or v.get("id")
    return v


def lit(s: str) -> str:
    """SQL 字面量。用 $tag$ 引用，先确认内容里没有该 tag（有就换）。"""
    if s is None:
        return "null"
    s = str(s)
    for tag in ("$b$", "$b1$", "$b2$", "$b3$"):
        if tag not in s:
            return f"{tag}{s}{tag}"
    raise Fail(f"cannot safely quote value: {s[:80]!r}")


def find_orphans() -> list[dict]:
    """有 key 但 LiteLLM_UserTable 里没人行的 user_id。"""
    sql = """
select t.user_id,
       coalesce(t.metadata->>'owner_name', ''),
       min(t.created_at)::text
from "LiteLLM_VerificationToken" t
left join "LiteLLM_UserTable" u on u.user_id = t.user_id
where (t.user_id like 'cursor-%' or t.user_id like 'claude-code-%')
  and u.user_id is null
group by 1,2 order by 3;
"""
    out = []
    for r in rows(sql):
        if len(r) < 3:
            continue
        uid, owner, first_key = r[0], r[1], r[2]
        if not USER_ID_RE.match(uid):
            print(f"  skip (user_id 形状不认): {uid!r}", file=sys.stderr)
            continue
        if not owner:
            for p in PREFIXES:
                if uid.startswith(p):
                    owner = uid[len(p):]
                    break
        out.append({"user_id": uid, "owner": owner, "first_key": first_key})
    return out


def team_index() -> dict[str, tuple[str, str]]:
    """team_alias -> (team_id, organization_id)。

    同别名有 71 组重复（06-12 建的老壳 + 09-04 同步建的主力）。规则取
    created_at 最新 —— 实测 92 个别名里 79 个直接命中有人的那个，落选的 6 个
    都是老壳（各 1~4 人），剩下 13 个是压根没人的空 team。
    """
    sql = """
select distinct on (team_alias) team_alias, team_id, coalesce(organization_id,'')
from "LiteLLM_TeamTable"
where team_alias is not null and team_alias <> ''
order by team_alias, created_at desc, team_id desc;
"""
    idx = {}
    for r in rows(sql):
        if len(r) >= 3:
            idx[r[0]] = (r[1], r[2])
    return idx


def org_budget_ids() -> set[str]:
    return {r[0] for r in rows('select budget_id from "LiteLLM_BudgetTable";') if r[0]}


def resolve(orphan: dict, recs: list[dict], teams: dict, budgets: set[str],
            run_id: str) -> dict:
    """把飞书记录 + team 索引拍成一行待插数据。查不到的字段留空，不编。"""
    uid = orphan["user_id"]
    rec = None
    for r in recs:
        if scalar(r.get("key_alias")) == uid:
            rec = r
            break
    if rec is None and recs:
        rec = recs[0]          # 同一个人的两条记录部门/职务一致，取任意一条
    rec = rec or {}

    dept = scalar(rec.get("姓名.部门")) or scalar(rec.get("部门名称"))
    org_alias = scalar(rec.get("组织名称"))
    display = scalar(rec.get("姓名"))
    job = scalar(rec.get("姓名.职务"))
    open_id = scalar(rec.get("open_id"))
    status = scalar(rec.get("状态"))
    prefix = scalar(rec.get("邮箱前缀")) or orphan["owner"]

    team_id, org_id = (None, None)
    if dept and dept in teams:
        team_id, org_id = teams[dept]
    if not org_id:
        org_id = DEFAULT_ORG
    if team_id and not TEAM_ID_RE.match(team_id):
        raise Fail(f"team_id 形状不对: {team_id!r}")
    if not ORG_ID_RE.match(org_id):
        raise Fail(f"organization_id 形状不对: {org_id!r}")

    budget_id = f"{org_id}:metadata-only"
    if budget_id not in budgets:
        budget_id = None       # 没这行就别插，FK 会拦

    email = f"{prefix}@{EMAIL_DOMAIN}" if prefix else None
    meta = {
        "source": "reconcile_user_row_backfill",
        "backfill_run": run_id,
        "backfill_reason": (
            "key existed without LiteLLM_UserTable row; key-swap-proxy gate "
            "denies (/user/info 404) until the row exists"
        ),
        "first_key_created": orphan["first_key"],
    }
    for k, v in (("display_name", display), ("department", dept),
                 ("team_alias", dept), ("job_title", job),
                 ("lark_open_id", open_id), ("organization_alias", org_alias),
                 ("feishu_status", status)):
        if v:
            meta[k] = v
    if email:
        meta["email_source"] = "enterprise_email"
    meta["source_table_id"] = TABLE_ID
    meta["source_base_token"] = BASE_TOKEN

    return {
        "user_id": uid, "user_alias": display, "team_id": team_id,
        "organization_id": org_id, "user_email": email, "budget_id": budget_id,
        "metadata": meta, "found_in_feishu": bool(rec),
        "department": dept, "job_title": job,
    }


def build_sql(plans: list[dict], run_id: str) -> str:
    """一个事务;所有 insert 带 on conflict do nothing，重复跑是安全的。"""
    parts = ["\\set ON_ERROR_STOP on", "begin;"]
    tids = sorted({p["team_id"] for p in plans if p["team_id"]})
    if tids:
        parts.append(
            f'create table if not exists "BACKUP_reconcile_{run_id}_team" as '
            'select * from "LiteLLM_TeamTable" where team_id in ('
            + ", ".join(lit(t) for t in tids) + ");")
    else:
        parts.append("-- 没有 team 行被改，不需要快照")
    for p in plans:
        uid = lit(p["user_id"])
        parts.append(f"""
insert into "LiteLLM_UserTable"
  (user_id, user_alias, team_id, organization_id, teams, spend, user_email,
   metadata, allowed_cache_controls, model_spend, model_max_budget, policies,
   created_at, updated_at)
values ({uid}, {lit(p["user_alias"])}, {lit(p["team_id"])},
   {lit(p["organization_id"])}, ARRAY[]::text[], 0, {lit(p["user_email"])},
   {lit(json.dumps(p["metadata"], ensure_ascii=False))}::jsonb,
   ARRAY[]::text[], '{{}}'::jsonb, '{{}}'::jsonb, ARRAY[]::text[], now(), now())
on conflict (user_id) do nothing;

insert into "LiteLLM_OrganizationMembership"
  (user_id, organization_id, user_role, spend, budget_id, created_at, updated_at)
values ({uid}, {lit(p["organization_id"])}, 'internal_user', 0,
        {lit(p["budget_id"])}, now(), now())
on conflict (user_id, organization_id) do nothing;""".rstrip())
        if p["team_id"]:
            tid = lit(p["team_id"])
            parts.append(f"""
insert into "LiteLLM_TeamMembership" (user_id, team_id, spend, budget_id, total_spend)
values ({uid}, {tid}, 0, {lit(p["budget_id"])}, 0)
on conflict (user_id, team_id) do nothing;

update "LiteLLM_TeamTable" set
  members = array(select distinct e from unnest(
      coalesce(members, ARRAY[]::text[]) || ARRAY[{uid}]) e),
  members_with_roles = case
    when members_with_roles::jsonb @> jsonb_build_array(
         jsonb_build_object('role','user','user_id',{uid}))
      then members_with_roles::jsonb
    else members_with_roles::jsonb || jsonb_build_array(
         jsonb_build_object('role','user','user_id',{uid})) end,
  updated_at = now()
where team_id = {tid};""".rstrip())
    parts.append("\ncommit;")
    return "\n".join(parts) + "\n"


def gate_probe(user_ids: list[str], control: str = "cursor-wanglihua") -> dict:
    """从 key-swap-proxy pod 里实打上游 /user/info。

    control 是阳性对照 —— 它必须 200，否则说明是我的探针坏了、不是这些人坏了。
    """
    ids = [u for u in user_ids if USER_ID_RE.match(u)]
    pod = run([
        "kubectl", "-n", NS_TERM, "get", "pod", "-l", "app=key-swap-proxy",
        "--field-selector=status.phase=Running", "-o", "name",
    ]).splitlines()
    pod = [x.strip().split("/")[-1] for x in pod if x.strip()]
    if not pod:
        raise Fail("no Running key-swap-proxy pod")
    script = (
        "import os,urllib.request,json,sys\n"
        "U=os.environ['LITELLM_URL']; K=os.environ['LITELLM_MASTER_KEY']\n"
        f"ids={json.dumps(ids + [control])}\n"
        "out={}\n"
        "for uid in ids:\n"
        "    try:\n"
        "        r=urllib.request.urlopen(urllib.request.Request(\n"
        "            U+'/user/info?user_id='+uid,\n"
        "            headers={'Authorization':'Bearer '+K}),timeout=10)\n"
        "        d=json.loads(r.read())\n"
        "        out[uid]=[r.status,len(d.get('keys') or [])]\n"
        "    except Exception as e:\n"
        "        out[uid]=[getattr(e,'code','ERR'),0]\n"
        "print(json.dumps(out))\n"
    )
    raw = run(["kubectl", "-n", NS_TERM, "exec", "-i", pod[0], "--",
               "python3", "-"], stdin=script, timeout=120)
    res = json.loads(raw.strip().splitlines()[-1])
    ctrl = res.get(control)
    if not ctrl or ctrl[0] != 200:
        raise Fail(f"阳性对照 {control} 不是 200（{ctrl}）—— 探针本身坏了，结果不可信")
    return res


def clear_gate_cache() -> None:
    """门禁把「拒」缓存 600 秒，不清的话补完还是读到假红。
    RollingUpdate + maxUnavailable=0，滚动重启零中断。"""
    run(["kubectl", "-n", NS_TERM, "rollout", "restart", "deploy/key-swap-proxy"])
    run(["kubectl", "-n", NS_TERM, "rollout", "status", "deploy/key-swap-proxy",
         "--timeout=180s"], timeout=200)


def ship_and_run(sql: str, run_id: str) -> str:
    """SQL 走 kubectl cp 进 db pod，两头对 md5 再执行。

    绝不用 `kubectl exec ... <<EOF` 把 SQL 喂进去：没有 -i 时 psql 读到空 stdin，
    静默 exit 0，看起来成功但一个字都没执行（2026-09-15 在生产上踩过一次）。
    走文件 + md5 双端核对，是唯一能证明「它真的读到了我写的东西」的方式。
    """
    local = f"/tmp/reconcile-{run_id}.sql"
    with open(local, "w", encoding="utf-8") as fh:
        fh.write(sql)
    md5_local = hashlib.md5(sql.encode()).hexdigest()
    remote = f"/tmp/reconcile-{run_id}.sql"
    run(["kubectl", "-n", NS_DB, "cp", local, f"{DB_POD}:{remote}"], timeout=120)
    got = run(["kubectl", "-n", NS_DB, "exec", DB_POD, "--",
               "md5sum", remote]).split()[0]
    if got != md5_local:
        raise Fail(f"md5 不一致：本地 {md5_local} pod 内 {got} —— 传输坏了，不执行")
    print(f"  SQL 已就位 {remote}  md5={md5_local}")
    return run(["kubectl", "-n", NS_DB, "exec", DB_POD, "--",
                "psql", "-U", DB_USER, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1",
                "-f", remote], timeout=300)


def print_plan(plans: list[dict], skipped: list[dict]) -> None:
    print("\n计划补的人行（查不到的字段留空，不编）：")
    print(f"  {'user_id':34} {'姓名':8} {'部门':16} {'team_id':26} org")
    for p in plans:
        print("  {:34} {:8} {:16} {:26} {}".format(
            p["user_id"], p["user_alias"] or "-", p["department"] or "-",
            p["team_id"] or "(空)", p["organization_id"]))
    # 没 team_id 有两种成因，混在一起说会把「部门没建 team」误报成「飞书没填部门」。
    no_dept = [p for p in plans if not p["team_id"] and not p["department"]]
    no_team = [p for p in plans if not p["team_id"] and p["department"]]
    if no_dept:
        print(f"\n  {len(no_dept)} 个飞书那边部门就是空的 —— team 留空，不编：")
        for p in no_dept:
            print(f"    - {p['user_id']}")
    if no_team:
        print(f"\n  {len(no_team)} 个部门有，但 LiteLLM 里没有同名 team 行 —— "
              "同样留空，不新建 team：")
        for p in no_team:
            print(f"    - {p['user_id']:34} 部门={p['department']}")
    if no_dept or no_team:
        print("  这些人 TeamMembership 不插，人行照样建 —— 门禁只看人行。")
    if skipped:
        ids = {p["user_id"] for p in plans}
        for p in skipped:
            tag = "已按 --include-unknown 补" if p["user_id"] in ids else "跳过"
            print(f"\n  飞书表里搜不到 key_alias：{p['user_id']}（{tag}）")


def report_and_apply(plans, skipped, run_id, args) -> int:
    if not plans:
        # 有孤儿但一个都补不了（飞书表里搜不到，比如 debug4 这种测试号）。
        # 定时跑的时候必须在这儿退出：否则每 10 分钟就空转一次事务
        # 并且白白滚动重启一遍 key-swap-proxy。
        print(f"有 {len(skipped)} 个孤儿身份在飞书表里搜不到，没有可补的字段，"
              "本轮不写库也不重启。")
        for p in skipped:
            print(f"    - {p['user_id']}")
        return 0
    print_plan(plans, skipped)
    sql = build_sql(plans, run_id)
    if args.sql_out:
        with open(args.sql_out, "w", encoding="utf-8") as fh:
            fh.write(sql)
        print(f"\nSQL 写到 {args.sql_out}（{len(sql)} 字节）")

    if not args.apply:
        print("\n=== DRY-RUN，一个字都没写库 ===")
        print(f"要真写：{sys.argv[0]} --apply")
        return 0

    # 第 0 步：阳性对照。先证明探针本身是好的，再相信它报的红。
    ids = [p["user_id"] for p in plans]
    before = gate_probe(ids)
    red_before = [u for u in ids if before.get(u, [0])[0] != 200]
    print(f"\n写之前门禁实测：{len(red_before)}/{len(ids)} 是红的（预期全红）")

    print(f"\n动了啥 —— 事务内：{len(plans)} 行 LiteLLM_UserTable、"
          f"{len(plans)} 行 OrganizationMembership、"
          f"{len([p for p in plans if p['team_id']])} 行 TeamMembership + 对应 team 的成员数组。")
    print(f"备份在哪 —— 被改的 team 行快照在 BACKUP_reconcile_{run_id}_team。")
    print("怎么回滚 —— 按 backfill_run 标记删人行和两张成员表的行，"
          f"team 表从 BACKUP_reconcile_{run_id}_team 还原。")

    out = ship_and_run(sql, run_id)
    print(out.strip()[-2000:])

    if not args.no_restart:
        print("\n清门禁的 600 秒拒绝缓存（滚动重启，零中断）...")
        clear_gate_cache()
        time.sleep(3)

    after = gate_probe(ids)
    ok = [u for u in ids if after.get(u, [0])[0] == 200]
    bad = [u for u in ids if after.get(u, [0])[0] != 200]
    print(f"\n写之后门禁实测：{len(ok)}/{len(ids)} 绿")
    for u in bad:
        print(f"  ! 还是红：{u} -> {after.get(u)}")
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="真写库。不加只打印计划（dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试用）")
    ap.add_argument("--only", default="", help="只处理这些 user_id，逗号分隔")
    ap.add_argument("--no-restart", action="store_true",
                    help="写完不重启 key-swap-proxy（那就得等 600 秒缓存自然过期）")
    ap.add_argument("--sql-out", default="", help="把生成的 SQL 也写到这个文件")
    ap.add_argument("--include-unknown", action="store_true",
                    help="连飞书表里搜不到的身份也一起补（默认跳过，见下）")
    args = ap.parse_args()

    run_id = os.environ.get("RUN_ID") or time.strftime("%Y%m%dT%H%M%S")

    orphans = find_orphans()
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        orphans = [o for o in orphans if o["user_id"] in want]
    if args.limit:
        orphans = orphans[: args.limit]

    if not orphans:
        print("没有孤儿 key —— 每个 key 都有对应人行，无事可做。")
        return 0

    print(f"发现 {len(orphans)} 个有 key 没人行的身份"
          f"（{len({o['owner'] for o in orphans})} 个人）")

    teams = team_index()
    budgets = org_budget_ids()
    print(f"team 索引 {len(teams)} 条别名，budget 行 {len(budgets)} 条")

    pod = term_pod()
    cache: dict[str, list[dict]] = {}
    plans, skipped = [], []
    for o in orphans:
        owner = o["owner"]
        if owner not in cache:
            try:
                cache[owner] = feishu_lookup(pod, owner)
            except Fail as e:
                print(f"  ! 飞书查询失败 {owner}: {e}", file=sys.stderr)
                cache[owner] = []
        recs = cache[owner]
        p = resolve(o, recs, teams, budgets, run_id)
        if not p["found_in_feishu"]:
            # 飞书表是这批人的权威源。搜不到 key_alias 的，我手里没有任何
            # 真实字段可填 —— 补出来只会是个连姓名都没有的空壳。默认跳过，
            # 要补得显式 --include-unknown。
            skipped.append(p)
            if not args.include_unknown:
                continue
        plans.append(p)
    return report_and_apply(plans, skipped, run_id, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
