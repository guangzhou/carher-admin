#!/usr/bin/env python3
"""DB 里 13 个 `kiro-*` / `kiro-slots-*` 组 → 隐藏别名（2026-10-27「后台 kiro 统一改 aws，先改名不删」）。198 上跑。

  python3 aws-legacy-db-rename.py plan
  python3 aws-legacy-db-rename.py apply
  python3 aws-legacy-db-rename.py restore <dir>     # 按备份把删掉的行 /model/new 回去 + router_settings 还原

为什么不是 UPDATE model_name：组名之外 model_info.id（`kiro-slots/...`）、字段名（`kiro_rate_multiplier`）、
api_base（服务名 `kiro-rs-slots...`）里都带 kiro，`/v2/model/info` 对普通 key 全吐。

做法（每个旧组）：
- `kiro-slots-X` 已有一模一样上游的 `aws-X`（槽位实例）⇒ 旧组直接删，名字改成隐藏别名 → `aws-X`
- `kiro-claude-haiku-5-5`（老实例，DB 行）⇒ 同样做别名 → `aws-claude-haiku-5-5`（老实例死池，指新实例只会更好）
- `model_group_alias` 用 `{"model": 目标, "hidden": true}`：旧名照样可调用（master key 直点组名的客户端不断），
  但 /v1/models、/v2/model/info、/model_group/info 都不列（10-27 实测三个接口 0 命中）
- router_settings 里以旧名为 key 的兜底行 / 重试策略删掉（别名请求按目标组的兜底走）；链里的旧名替换成目标

顺序：先加别名（此刻旧组和别名同名，router 优先 deployment，行为不变）→ 删旧行 → 改兜底。
"""
import json, os, subprocess, sys, time, urllib.request

BASE = "http://127.0.0.1:30402"
MK = os.environ["LITELLM_MASTER_KEY"]
NS, DB = "litellm-product", "litellm-db-0"
BACKUP_DIR = "/home/cltx/kiro-deploy/backup"


def api(path, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                               method="POST" if body is not None else "GET",
                               headers={"Authorization": "Bearer " + MK, "content-type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=120))


def psql(sql, flags="-tAc"):
    q = "'" + sql.replace("'", "'\\''") + "'"
    r = subprocess.run("sudo -n kubectl -n %s exec %s -- psql -U litellm -d litellm -v ON_ERROR_STOP=1 %s %s"
                       % (NS, DB, flags, q), shell=True, capture_output=True, text=True)
    if r.returncode:
        sys.exit("psql failed: " + r.stderr[:400])
    return r.stdout


def router():
    return json.loads(psql("select param_value from \"LiteLLM_Config\" where param_name='router_settings';").strip())


def write_router(rs):
    psql("update \"LiteLLM_Config\" set param_value = $J$%s$J$::jsonb where param_name='router_settings';" % json.dumps(rs), "-c")


def target(name):
    if name.startswith("kiro-slots-"):
        return "aws-" + name[len("kiro-slots-"):]
    return "aws-" + name[len("kiro-"):]


def db_rows():
    out = psql("select json_agg(t) from \"LiteLLM_ProxyModelTable\" t where model_name like 'kiro-%';").strip()
    return json.loads(out) if out and out != "" else []


def live_groups():
    out, page = set(), 1
    while True:
        d = api(f"/v2/model/info?page={page}&size=100")
        out |= {m["model_name"] for m in d.get("data", [])}
        if page >= d.get("total_pages", 1):
            return out
        page += 1


def new_router(rs, mapping):
    out = json.loads(json.dumps(rs))
    alias = dict(out.get("model_group_alias") or {})
    for old, new in mapping.items():
        cur = alias.get(old)
        assert cur is None or (isinstance(cur, dict) and cur.get("model") == new), (old, cur)
        alias[old] = {"model": new, "hidden": True}
    out["model_group_alias"] = alias
    fb = []
    for row in out.get("fallbacks", []):
        (k, v), = row.items()
        if k in mapping:
            continue  # 旧组自己的兜底行：别名请求按目标组的兜底走
        fb.append({k: [mapping.get(x, x) for x in v]})
    out["fallbacks"] = fb
    out["model_group_retry_policy"] = {k: v for k, v in (out.get("model_group_retry_policy") or {}).items()
                                       if k not in mapping}
    return out


def main():
    cmd = sys.argv[1]
    if cmd == "restore":
        d = sys.argv[2]
        rows = json.load(open(f"{d}/rows.json"))
        have = live_groups()
        for r in rows:
            if r["model_name"] in have:
                print("SKIP exists", r["model_name"]); continue
            api("/model/new", {"model_name": r["model_name"], "litellm_params": r["litellm_params"],
                               "model_info": {k: v for k, v in r["model_info"].items() if k != "db_model"}})
            print("RESTORED", r["model_name"])
        write_router(json.load(open(f"{d}/router_settings.json")))
        print("router_settings restored")
        return
    rows = db_rows()
    groups = live_groups()
    mapping = {r["model_name"]: target(r["model_name"]) for r in rows}
    miss = {k: v for k, v in mapping.items() if v not in groups}
    assert not miss, f"目标组不存在: {miss}"
    rs = router()
    after = new_router(rs, mapping)
    print(f"DB kiro 行 {len(rows)}；别名 +{len(mapping)}；兜底行 {len(rs['fallbacks'])}→{len(after['fallbacks'])}；"
          f"重试 {len(rs.get('model_group_retry_policy') or {})}→{len(after['model_group_retry_policy'])}")
    for k, v in sorted(mapping.items()):
        print("  ", k, "→ hidden alias →", v)
    if cmd != "apply":
        return
    d = f"{BACKUP_DIR}/aws-legacy-db-{time.strftime('%Y%m%dT%H%M%S')}"
    os.makedirs(d, mode=0o700)
    # 解密后的 litellm_params（含 api_key 明文）从 /model/info 取，restore 能直接 /model/new
    full = {m["model_info"]["id"]: m for m in api("/model/info")["data"]}
    keep = []
    for r in rows:
        f = full[r["model_info"]["id"]]
        keep.append({"model_name": r["model_name"], "litellm_params": f["litellm_params"], "model_info": r["model_info"]})
    for name, obj in (("rows.json", keep), ("router_settings.json", rs)):
        with open(f"{d}/{name}", "w") as fh:
            json.dump(obj, fh)
        os.chmod(f"{d}/{name}", 0o600)
    print("备份", d)
    # 1) 只加别名
    step1 = json.loads(json.dumps(rs)); step1["model_group_alias"] = after["model_group_alias"]
    write_router(step1)
    print("别名已写，等热加载 40s"); time.sleep(40)
    # 2) 删旧行
    for r in rows:
        api("/model/delete", {"id": r["model_info"]["id"]})
        print("DELETED", r["model_name"])
    # 3) 兜底/重试
    cur = router()
    assert cur["model_group_alias"] == after["model_group_alias"]
    write_router(after)
    print("兜底/重试已改；写后一致:", router() == after)


main()
