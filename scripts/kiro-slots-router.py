#!/usr/bin/env python3
"""给每个 `kiro-slots-*` 组补齐 router_settings 里的两样东西：兜底行 + 重试 0。在 198 上跑（sudo -n kubectl → litellm-db-0）。

  python3 kiro-slots-router.py plan                 # 只读：列出缺哪几行
  python3 kiro-slots-router.py apply                # 先备份整份 router_settings 再 jsonb 追加
  python3 kiro-slots-router.py restore <备份.json>  # 只把 kiro-slots-* 的兜底/重试还原成备份里的样子，别的组一字不动

为什么要两样都有（10-24 演练实测）：
- 兜底行：`kiro-slots-X` → `kiro-X`（老实例）→ 老 `kiro-X` 原来的兜底链（去掉 kiro-slots-* 防环）。
- 重试 0：全局 `num_retries=5` 会让死掉的新实例同组重试，请求挂 120s+ 也不兜底；
  model_group_retry_policy 全 0 后立刻落老实例。
- 例外：`kiro-slots-claude-haiku-5-5` 先兜 `kiro-claude-haiku-4-5`（老 kiro.rs 不认 5.5，会悄悄落 4.5）。

组名从 DB 现读（/model/new 加的组都在 LiteLLM_ProxyModelTable）。写法只准 jsonb `||` 追加；写完比对其它字段逐字未变、旧兜底行逐字未变。
"""
import json, subprocess, sys, time

NS, DB = "litellm-product", "litellm-db-0"
BACKUP_DIR = "/home/cltx/kiro-deploy/backup"
POLICY = {"DefaultRetries": 0, "InternalServerErrorRetries": 0, "ServiceUnavailableErrorRetries": 0,
          "TimeoutErrorRetries": 0, "RateLimitErrorRetries": 0}
FIRST_FALLBACK = {"kiro-slots-claude-haiku-5-5": "kiro-claude-haiku-4-5"}
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


def slots_groups():
    out = psql("select distinct model_name from \"LiteLLM_ProxyModelTable\" where model_name like 'kiro-slots-%';")
    return sorted(x for x in out.split() if x)


def head(row):
    return list(row)[0]


def plan(rs, groups):
    fb = rs.get("fallbacks", [])
    have = {head(r) for r in fb}
    chain = {head(r): list(r.values())[0] for r in fb}
    rows = []
    for g in groups:
        if g in have:
            continue
        first = FIRST_FALLBACK.get(g, "kiro-" + g[len("kiro-slots-"):])
        rest = [x for x in chain.get(first, []) if not x.startswith("kiro-slots-")]
        rows.append({g: [first] + rest})
    rp = rs.get("model_group_retry_policy") or {}
    pols = {g: POLICY for g in groups if rp.get(g) != POLICY}
    return rows, pols


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
        keep = [r for r in before["fallbacks"] if not head(r).startswith("kiro-slots-")]
        back = [r for r in old["fallbacks"] if head(r).startswith("kiro-slots-")]
        rp = {k: v for k, v in (before.get("model_group_retry_policy") or {}).items() if not k.startswith("kiro-slots-")}
        rp.update({k: v for k, v in (old.get("model_group_retry_policy") or {}).items() if k.startswith("kiro-slots-")})
        print("备份当前", backup(before, "pre-slots-restore"))
        write("$J$%s$J$::jsonb" % json.dumps(keep + back), "$J$%s$J$::jsonb" % json.dumps(rp))
        after = read()
        print("restore 完成 | fallbacks", len(before["fallbacks"]), "->", len(after["fallbacks"]),
              "| 其它字段未变:", others_same(before, after))
        return
    groups = slots_groups()
    rows, pols = plan(before, groups)
    print("kiro-slots 组 %d 个；缺兜底 %d 行；缺重试0 %d 个" % (len(groups), len(rows), len(pols)))
    for r in rows:
        print("  + 兜底", r)
    for g in pols:
        print("  + 重试0", g)
    if cmd != "apply" or not (rows or pols):
        print("OK", cmd, "（没写）" if cmd != "apply" else "（无需改）")
        return
    print("备份", backup(before, "pre-slots-router"))
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
