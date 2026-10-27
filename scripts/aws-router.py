#!/usr/bin/env python3
"""给每个 `aws-*` 组补 router_settings 里的兜底行 + 重试 0。在 198 上跑（sudo -n kubectl → litellm-db-0）。

  python3 aws-router.py plan                 # 只读：列出将加哪些行
  python3 aws-router.py apply                # 先备份整份 router_settings 再 jsonb 追加
  python3 aws-router.py restore <备份.json>  # 只把 aws-* 的兜底/重试还原成备份里的样子，别的组一字不动

兜底链 = 对应 `kiro-slots-X` 现有的链，**去掉所有 kiro-* 腿**（2026-10-27 用户定：老实例池全死，
兜到它必失败、白多一跳），直接落别家。`aws-auto` 没有对应 kiro-slots 组 ⇒ 用 sonnet-5.5 的链。
重试 0 的理由同 kiro-slots-router.py（全局 num_retries=5 会让死实例同组重试挂 120s+，兜底走不到）。
写法只准 jsonb `||` 追加；写完比对其它字段逐字未变、旧行逐字未变。
"""
import json, subprocess, sys, time

NS, DB = "litellm-product", "litellm-db-0"
BACKUP_DIR = "/home/cltx/kiro-deploy/backup"
POLICY = {"DefaultRetries": 0, "InternalServerErrorRetries": 0, "ServiceUnavailableErrorRetries": 0,
          "TimeoutErrorRetries": 0, "RateLimitErrorRetries": 0}
SOURCE = {"aws-auto": "kiro-slots-claude-sonnet-5-5"}
TOUCHED = ("fallbacks", "model_group_retry_policy")


def q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def psql(sql, flags="-tAc"):
    r = subprocess.run("sudo -n kubectl -n %s exec %s -- psql -U litellm -d litellm -v ON_ERROR_STOP=1 %s %s"
                       % (NS, DB, flags, q(sql)), shell=True, capture_output=True, text=True)
    if r.returncode:
        sys.exit("psql failed: " + r.stderr[:400])
    return r.stdout


def read():
    return json.loads(psql("select param_value from \"LiteLLM_Config\" where param_name='router_settings';").strip())


def aws_groups():
    out = psql("select distinct model_name from \"LiteLLM_ProxyModelTable\" where model_name like 'aws-%';")
    return sorted(x for x in out.split() if x)


def head(row):
    return list(row)[0]


def plan(rs, groups):
    fb = rs.get("fallbacks", [])
    have = {head(r) for r in fb}
    chain = {head(r): list(r.values())[0] for r in fb}
    rows, missing = [], []
    for g in groups:
        if g in have:
            continue
        src = SOURCE.get(g, "kiro-slots-" + g[len("aws-"):])
        legs = [x for x in chain.get(src, []) if not x.startswith("kiro-")]
        if not legs:
            missing.append((g, src)); continue
        rows.append({g: legs})
    rp = rs.get("model_group_retry_policy") or {}
    pols = {g: POLICY for g in groups if rp.get(g) != POLICY}
    return rows, pols, missing


def others_same(a, b):
    return {k: v for k, v in a.items() if k not in TOUCHED} == {k: v for k, v in b.items() if k not in TOUCHED}


def write(fb_sql, rp_sql):
    psql("update \"LiteLLM_Config\" set param_value = jsonb_set(jsonb_set(param_value,'{fallbacks}', %s), "
         "'{model_group_retry_policy}', %s) where param_name='router_settings';" % (fb_sql, rp_sql), "-c")


def backup(rs, tag):
    bk = "%s/router_settings-%s-%s.json" % (BACKUP_DIR, tag, time.strftime("%Y%m%dT%H%M%S"))
    with open(bk, "w") as f:
        json.dump(rs, f)
    subprocess.run(["chmod", "600", bk])
    return bk


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    before = read()
    if cmd == "restore":
        old = json.load(open(sys.argv[2]))
        keep = [r for r in before["fallbacks"] if not head(r).startswith("aws-")]
        back = [r for r in old["fallbacks"] if head(r).startswith("aws-")]
        rp = {k: v for k, v in (before.get("model_group_retry_policy") or {}).items() if not k.startswith("aws-")}
        rp.update({k: v for k, v in (old.get("model_group_retry_policy") or {}).items() if k.startswith("aws-")})
        print("备份当前", backup(before, "pre-aws-restore"))
        write("$J$%s$J$::jsonb" % json.dumps(keep + back), "$J$%s$J$::jsonb" % json.dumps(rp))
        after = read()
        print("restore 完成 | fallbacks", len(before["fallbacks"]), "->", len(after["fallbacks"]),
              "| 其它字段未变:", others_same(before, after))
        return
    groups = aws_groups()
    rows, pols, missing = plan(before, groups)
    print("aws 组 %d 个；缺兜底 %d 行；缺重试0 %d 个；找不到来源链 %d 个" % (len(groups), len(rows), len(pols), len(missing)))
    for r in rows:
        print("  + 兜底", r)
    for g in pols:
        print("  + 重试0", g)
    for m in missing:
        print("  ! 无来源链（不加兜底）", m)
    if cmd != "apply" or not (rows or pols):
        print("OK", cmd, "（没写）" if cmd != "apply" else "（无需改）")
        return
    print("备份", backup(before, "pre-aws-router"))
    write("(param_value->'fallbacks') || $J$%s$J$::jsonb" % json.dumps(rows),
          "coalesce(param_value->'model_group_retry_policy','{}'::jsonb) || $J$%s$J$::jsonb" % json.dumps(pols))
    after = read()
    n = len(before["fallbacks"])
    ok = others_same(before, after) and after["fallbacks"][:n] == before["fallbacks"]
    print("fallbacks", n, "->", len(after["fallbacks"]), "| retry_policy",
          len(before.get("model_group_retry_policy") or {}), "->", len(after.get("model_group_retry_policy") or {}),
          "| 旧行+其它字段逐字未变:", ok)
    print("~30s 热加载生效" if ok else "🔴 比对不一致，用 restore 回滚")


if __name__ == "__main__":
    main()
