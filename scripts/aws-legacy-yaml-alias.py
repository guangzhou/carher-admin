#!/usr/bin/env python3
"""车道 config.yaml 里 21 个 `kiro-*` 组改名为 `aws-legacy-*` 之前/之后，处理 router_settings。198 上跑。

  python3 aws-legacy-yaml-alias.py plan
  python3 aws-legacy-yaml-alias.py pre      # 改 yaml 前：兜底行/重试策略复制一份到新名（旧名行先留着）
  python3 aws-legacy-yaml-alias.py post     # 三车道都换完 yaml 后：旧名 → 隐藏别名 → aws-X；删旧名兜底/重试行
  python3 aws-legacy-yaml-alias.py restore <备份.json>

旧名的去向（hidden alias，不进任何模型列表）：
- 有 `aws-X` 的（槽位实例同上游）⇒ 指 `aws-X`（老实例池全死，指新实例只会更好；直点旧名的客户端不断）
- `kiro-auto` ⇒ `aws-auto`；`kiro-gpt5.6-*` ⇒ `aws-gpt-5.6-*`
兜底链值里的 `kiro-X`（如 claude-haiku-4-5 → kiro-claude-haiku-4-5）改成 `aws-X`。
"""
import json, subprocess, sys, time

NS, DB = "litellm-product", "litellm-db-0"
BACKUP_DIR = "/home/cltx/kiro-deploy/backup"
NAMES = ["kiro-auto", "kiro-gpt5.6-sol", "kiro-gpt5.6-terra", "kiro-gpt5.6-luna", "kiro-deepseek-3.2",
         "kiro-minimax-m2.5", "kiro-minimax-m2.1", "kiro-glm-5", "kiro-kimi-k3", "kiro-qwen3-coder-next",
         "kiro-claude-opus-5", "kiro-claude-opus-5-5", "kiro-claude-sonnet-5", "kiro-claude-sonnet-5-5",
         "kiro-claude-opus-4-8", "kiro-claude-opus-4-7", "kiro-claude-opus-4-6", "kiro-claude-sonnet-4-6",
         "kiro-claude-opus-4-5", "kiro-claude-sonnet-4-5", "kiro-claude-haiku-4-5"]


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


def legacy(n):
    return "aws-legacy-" + n[len("kiro-"):]


def aws(n):
    return "aws-" + n[len("kiro-"):].replace("gpt5.6-", "gpt-5.6-")


def groups():
    return set(psql("select distinct model_name from \"LiteLLM_ProxyModelTable\";").split())


def backup(rs, tag):
    bk = "%s/router_settings-%s-%s.json" % (BACKUP_DIR, tag, time.strftime("%Y%m%dT%H%M%S"))
    json.dump(rs, open(bk, "w")); subprocess.run(["chmod", "600", bk])
    return bk


def main():
    cmd = sys.argv[1]
    rs = router()
    if cmd == "restore":
        print("当前备份", backup(rs, "pre-legacy-yaml-restore"))
        write_router(json.load(open(sys.argv[2]))); print("restored"); return
    g = groups()
    miss = [aws(n) for n in NAMES if aws(n) not in g]
    assert not miss, f"aws 目标组不在 DB: {miss}"
    fb = rs.get("fallbacks", [])
    rp = rs.get("model_group_retry_policy") or {}
    if cmd in ("plan", "pre"):
        add_fb = [{legacy(k): v} for row in fb for k, v in row.items() if k in NAMES]
        have = {list(r)[0] for r in fb}
        add_fb = [r for r in add_fb if list(r)[0] not in have]
        add_rp = {legacy(k): v for k, v in rp.items() if k in NAMES and legacy(k) not in rp}
        print(f"pre: 兜底行 +{len(add_fb)}，重试 +{len(add_rp)}")
        for r in add_fb:
            print("   +", json.dumps(r, ensure_ascii=False))
        if cmd == "pre":
            print("备份", backup(rs, "pre-legacy-yaml-pre"))
            out = json.loads(json.dumps(rs)); out["fallbacks"] = fb + add_fb
            out["model_group_retry_policy"] = {**rp, **add_rp}
            write_router(out); print("写后一致:", router() == out)
        return
    if cmd == "post":
        alias = dict(rs.get("model_group_alias") or {})
        for n in NAMES:
            assert n not in alias or (isinstance(alias[n], dict) and alias[n].get("model") == aws(n)), (n, alias[n])
            alias[n] = {"model": aws(n), "hidden": True}
        out = json.loads(json.dumps(rs))
        out["model_group_alias"] = alias
        out["fallbacks"] = [{k: [aws(x) if x in NAMES else x for x in v]} for row in fb for k, v in row.items()
                            if k not in NAMES]
        out["model_group_retry_policy"] = {k: v for k, v in rp.items() if k not in NAMES}
        blob = json.dumps({k: out[k] for k in ("fallbacks", "model_group_retry_policy")})
        assert '"kiro-' not in blob, "兜底/重试里还有 kiro- 名"
        print(f"post: 别名 +{len(NAMES)}；兜底 {len(fb)}→{len(out['fallbacks'])}；重试 {len(rp)}→{len(out['model_group_retry_policy'])}")
        print("备份", backup(rs, "pre-legacy-yaml-post"))
        write_router(out); print("写后一致:", router() == out)


main()
