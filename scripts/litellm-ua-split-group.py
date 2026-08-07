#!/usr/bin/env python3
"""litellm-ua-split-group.py — 按 User-Agent 把一个 model group 劈成两个落点。

场景：同一把 key、同一个模型名，**Codex Desktop 走真 chatgpt-acct 池，其余一切
走 zerokey**。用的是 LiteLLM 原生 tag 路由（`router_strategy/tag_based_routing.py`），
不写业务代码。

**必须在 198 上以 root 执行**（需要 kubectl）::

    scp scripts/litellm-ua-split-group.py cltx@10.68.13.198:/tmp/
    ssh cltx@10.68.13.198 'sudo python3 /tmp/litellm-ua-split-group.py build'
    ssh cltx@10.68.13.198 'sudo python3 /tmp/litellm-ua-split-group.py verify'
    ssh cltx@10.68.13.198 'sudo python3 /tmp/litellm-ua-split-group.py rollback'

机制（读源码确认，2026-08-07）
-----------------------------
* deployment 上挂 ``litellm_params.tag_regex``，服务端用 ``re.search`` 匹配
  ``"User-Agent: <ua>"`` 字符串；UA 由 ``litellm_pre_call_utils.py:1773`` 填进
  ``metadata["user_agent"]``。
* 不匹配 regex 的落到带 ``tags:["default"]`` 的 deployment；**两边都没有会
  raise ``no_deployments_with_tag_routing``** —— 所以 zk 那批必须打 default。
* Codex 的 UA 形如 ``{originator}/{version} ({OS} {ver}; {arch}) ...``
  （``codex-rs/login/src/auth/default_client.rs:159``），开头就是 originator。
  线上实测取值：``Codex Desktop`` / ``codex-tui`` / ``codex_vscode`` /
  ``codex_exec`` / ``OpenAI`` / ``claude-cli`` …

三个已经踩过的坑
----------------
1. **``enable_tag_filtering`` 是 Router 构造参数，不在热加载白名单**
   （``router.py:10468`` 的 ``_allowed_settings``）—— 写 DB 不生效，得进 CM 配置。
   198 上它**本来就已经是 true**，无需改动。
2. **路由时的 ``metadata["tags"]`` 只有客户端自己发的**。spend log 里 97% 的
   ``User-Agent: xxx`` tag 是**落日志时**才 extend 进去的
   （``litellm_logging.py:5587`` ``_get_request_tags``），不参与路由。别拿它
   评估开关风险。
3. **``weighted_affinity`` 跑在 tag 路由之前**（``router.py:11150`` vs ``:11168``），
   把候选钉成 1 台会让 UA 分流失效（实测 Desktop 8 发里 5 发落到 zerokey），
   反向还会清空候选触发 raise。已在 ``weighted_affinity.py`` 里加「候选含
   tag_regex 就整组让路」，本脚本依赖那个改动。

只影响一把 key：新组是**复制**出来的行，既有 group 一行不动；只有目标 key 的
per-key alias 指向新组。
"""
import json
import os
import subprocess
import sys

NS = os.environ.get("LITELLM_NS", "litellm-product")
GROUP = os.environ.get("UA_SPLIT_GROUP", "ua-split-gpt-5.6-terra")
ACCT_SRC = os.environ.get("UA_SPLIT_ACCT_SRC", "chatgpt-gpt-5.6-terra")
ZK_SRC = os.environ.get("UA_SPLIT_ZK_SRC", "zerokey-pool-gpt-5.6-terra")
PREFIX = os.environ.get("UA_SPLIT_PREFIX", "uasplit-")
# 命中即走 acct。两种 Desktop 标识都算（用户 2026-08-07 确认）。
DESKTOP_REGEX = json.loads(os.environ.get(
    "UA_SPLIT_DESKTOP_REGEX",
    '["^User-Agent: Codex Desktop/", "^User-Agent: codex_work_desktop/"]'))


def psql(sql, quiet=False):
    r = subprocess.run(
        ["kubectl", "-n", NS, "exec", "litellm-db-0", "--",
         "psql", "-U", "litellm", "-d", "litellm", "-A", "-t", "-c", sql],
        capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("psql FAILED:\n" + r.stderr[:800])
    if not quiet:
        print("  " + r.stdout.strip().replace("\n", "\n  "))
    return r.stdout.strip()


def build():
    q = lambda s: "'" + s.replace("'", "''") + "'"
    regex_json = json.dumps({"tag_regex": DESKTOP_REGEX})
    for src, extra, role in ((ACCT_SRC, regex_json, "desktop->acct"),
                             (ZK_SRC, '{"tags":["default"]}', "other->zerokey")):
        print(f"[build] {src} -> {GROUP} ({role})")
        psql(f"""
INSERT INTO "LiteLLM_ProxyModelTable"
  (model_id, model_name, litellm_params, model_info, created_at, created_by, updated_at, updated_by, blocked)
SELECT {q(PREFIX)} || model_id, {q(GROUP)},
       litellm_params || {q(extra)}::jsonb,
       jsonb_set(coalesce(model_info,'{{}}'::jsonb), '{{id}}', to_jsonb({q(PREFIX)} || model_id)),
       now(), 'ua-split', now(), 'ua-split', false
FROM "LiteLLM_ProxyModelTable"
WHERE model_name = {q(src)} AND coalesce(blocked,false) = false
  AND NOT EXISTS (SELECT 1 FROM "LiteLLM_ProxyModelTable" t2
                  WHERE t2.model_id = {q(PREFIX)} || "LiteLLM_ProxyModelTable".model_id);""")
    verify()


def verify():
    print(f"[verify] {GROUP} 成员构成")
    psql(f"""
SELECT CASE WHEN litellm_params ? 'tag_regex' THEN 'desktop->acct'
            WHEN litellm_params->'tags' ? 'default' THEN 'other->zerokey'
            ELSE 'UNTAGGED(BUG)' END, count(*)
FROM "LiteLLM_ProxyModelTable" WHERE model_name='{GROUP}' GROUP BY 1 ORDER BY 1;""")
    print("[verify] 既有 group 是否被打上标签（必须为 0）")
    psql(f"""
SELECT count(*) FROM "LiteLLM_ProxyModelTable"
WHERE model_name <> '{GROUP}'
  AND (litellm_params ? 'tag_regex' OR litellm_params->'tags' ? 'default');""")
    print("[verify] fallbacks 里有没有这个组（没有的话上游一挂就是硬 500）")
    psql(f"""
SELECT count(*) FROM "LiteLLM_Config"
WHERE param_name='router_settings' AND param_value::text LIKE '%{GROUP}%';""")


def rollback():
    print(f"[rollback] 删除 {GROUP} 的全部行")
    psql(f"DELETE FROM \"LiteLLM_ProxyModelTable\" WHERE model_name='{GROUP}';")
    print("  ⚠️ key 的 per-key alias 和 router_settings.fallbacks 需另行撤回")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"build": build, "verify": verify, "rollback": rollback}.get(
        cmd, lambda: sys.exit(__doc__))()
