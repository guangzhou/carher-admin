#!/usr/bin/env python3
"""router_settings 里所有 `kiro-*` 组名改成新名（配合组改名，2026-10-27「后台 kiro 统一改 aws」）。198 上跑。

  python3 aws-router-rename.py plan
  python3 aws-router-rename.py apply
  python3 aws-router-rename.py restore <备份.json>   # 整份 router_settings 还原成备份

改名规则（与 aws-lane-config-rename.py / DB 行改名一致）：
  kiro-slots-X → aws-legacy-slots-X
  kiro-X       → aws-legacy-X
只碰 fallbacks（行 key 和链里的值）与 model_group_retry_policy（key）；其它字段逐字不动（写后比对）。
"""
import json, subprocess, sys, time

NS, DB = "litellm-product", "litellm-db-0"
BACKUP_DIR = "/home/cltx/kiro-deploy/backup"


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


def new_name(n):
    if n.startswith("kiro-slots-"):
        return "aws-legacy-slots-" + n[len("kiro-slots-"):]
    if n.startswith("kiro-"):
        return "aws-legacy-" + n[len("kiro-"):]
    return n


def transform(rs):
    out = json.loads(json.dumps(rs))
    out["fallbacks"] = [{new_name(k): [new_name(x) for x in v] for k, v in row.items()} for row in rs.get("fallbacks", [])]
    out["model_group_retry_policy"] = {new_name(k): v for k, v in (rs.get("model_group_retry_policy") or {}).items()}
    return out


def write(rs):
    psql("update \"LiteLLM_Config\" set param_value = $J$%s$J$::jsonb where param_name='router_settings';"
         % json.dumps(rs), "-c")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    before = read()
    if cmd == "restore":
        old = json.load(open(sys.argv[2]))
        bk = "%s/router_settings-pre-aws-rename-restore-%s.json" % (BACKUP_DIR, time.strftime("%Y%m%dT%H%M%S"))
        json.dump(before, open(bk, "w")); subprocess.run(["chmod", "600", bk])
        write(old)
        print("restore 完成，当前备份", bk, "| 一致:", read() == old)
        return
    after = transform(before)
    keys = ("fallbacks", "model_group_retry_policy")
    assert {k: v for k, v in before.items() if k not in keys} == {k: v for k, v in after.items() if k not in keys}
    assert len(after["fallbacks"]) == len(before["fallbacks"])
    assert len(after["model_group_retry_policy"]) == len(before.get("model_group_retry_policy") or {}), "改名撞 key"
    ch = [(b, a) for b, a in zip(before["fallbacks"], after["fallbacks"]) if a != b]
    rp = [k for k in (before.get("model_group_retry_policy") or {}) if new_name(k) != k]
    print("fallbacks 改 %d 行；retry_policy 改 %d 个 key" % (len(ch), len(rp)))
    for b, a in ch[:40]:
        print("  ", json.dumps(b, ensure_ascii=False), "->", json.dumps(a, ensure_ascii=False))
    assert "kiro" not in json.dumps({k: after[k] for k in keys})
    if cmd != "apply":
        print("OK plan（没写）"); return
    bk = "%s/router_settings-pre-aws-rename-%s.json" % (BACKUP_DIR, time.strftime("%Y%m%dT%H%M%S"))
    json.dump(before, open(bk, "w")); subprocess.run(["chmod", "600", bk])
    write(after)
    got = read()
    print("备份", bk, "| 写后与预期一致:", got == after)


if __name__ == "__main__":
    main()
