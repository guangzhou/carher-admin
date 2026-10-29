#!/usr/bin/env python3
"""litellm-198-key-error-triage.py — 198 某把 key 报错 / 某家上游挂了，一条命令查清。只读。

为什么有这个脚本
================
2026-10-20 一轮里手搓了十几条 SQL：用户报 ``402 Payment Required: API 异常 (req: c7cd902a)``，
要依次查 key 预算 → SpendLogs 原文 → 落到哪个组 → 那个组接的哪家上游；接着「OpenRouter 欠费
影响多大」又要把 /model/info、全库 key（models/aliases/per-key fallbacks）、全局 router_settings、
7 天流量交叉一遍。两个结论差点被尺子带偏：

1. **报错里出现某供应商 ≠ 用户选了它。** 4 把 cursor key 的 400 落在 ``openrouter-glm-5.3-flash``，
   实际请求的是 ``deepseek-v4.1-flash``，是兜底链一路兜到末腿；真因是空 input（10-22 查实：Codex
   ``view_image`` 截图几 MB 但 token 极少，我们 deepseek 超限裁剪按字节把它删成 ``[]``，10-25 已修）。
   ⇒ 必须把「请求名」(``proxy_server_request.model``) 和「落点」(``model_group``) 分开报。
2. **死组流量 95% 是巡检。** ``carher-hangzhou``（杭州 gw 凭据）每组每小时约 10 发
   "What's 1+1"，不剔掉会把「没人用」读成「几万次失败」。

用法
====
    # 用户只给了 API 异常 (req: xxxxxxxx)
    python3 scripts/litellm-198-key-error-triage.py rid c7cd902a

    # 某家上游（按 litellm_params.model / api_base 子串匹配）挂了，影响哪些 key
    python3 scripts/litellm-198-key-error-triage.py upstream openrouter
    python3 scripts/litellm-198-key-error-triage.py upstream 10.68.13.188:4130 --no-probe

``upstream`` 会用 master key 对每个匹配组直打一发（``disable_fallbacks`` + ``num_retries=0``），
活组会产生一笔极小费用；``--no-probe`` 跳过，此时把全部匹配组当「待判」处理。

相关：``.claude/skills/litellm-198-key-error-triage/SKILL.md``
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import re
import subprocess
import sys

HOST = "10.68.13.198"
USER = "cltx"
NS = "litellm-product"
PROXY = "http://127.0.0.1:30402"
# 不是人：杭州 gw 的上游凭据（健康巡检扫全部组）、每小时稳定性探针
SYNTHETIC_KEYS = ("carher-hangzhou", "stability-probe")


# ---------------------------------------------------------------- 纯函数（有单测）

def matches_upstream(deployment: dict, pattern: str) -> bool:
    """部署的上游是否命中 pattern（看 model / api_base / custom_llm_provider，大小写不敏感）。

    ⚠️ 只看 model 前缀会漏：``openrouter-gpt-5.6-*`` 写成 ``openai/gpt-5.6-sol`` + api_base=openrouter.ai。
    """
    lp = deployment.get("litellm_params") or {}
    hay = " ".join(str(lp.get(f) or "") for f in ("model", "api_base", "custom_llm_provider"))
    return pattern.lower() in hay.lower()


def classify_error(code: str | None, message: str, body_len: int | None = None) -> str:
    """把上游/网关原文翻成一句人话的「是谁的事」。"""
    m = message or ""
    if "Insufficient credits" in m or "insufficient_quota" in m:
        return "上游账号欠费（换模型就能用；要恢复得充值或换腿）"
    if "budget_exceeded" in m or "ExceededBudget" in m or "Budget has been exceeded" in m:
        return "key 自己额度用完（看 spend/max_budget/budget_reset_at）"
    if ("key_model_access_denied" in m or "not allowed to access model" in m
            or "is not available for this API key" in m):
        return "key 白名单里没有这个名字"
    if "Input items array must not be empty" in m or "messages: at least one message" in m:
        # 2026-10-22：进来 7.6MB、出去 input=[] —— 是我们的 deepseek 超限裁剪删空的，
        # 不是客户端。先看进网关时的 body 大小再归因。
        if (body_len or 0) > 100_000:
            return (f"我们网关把请求删空了（进来 {body_len/1e6:.1f}MB，出去 0 条）"
                    "——看上一行「前一轮调的工具」；10-22 那批是 view_image 截图按字节被删，"
                    "10-25 起 deepseek_responses_adapt 不再算图片字节，再出现=新形状")
        return "客户端发了空请求（进网关时 body 也很小；换哪个模型都失败）"
    if "Connection error" in m or "ConnectError" in m:
        return "网关连不上上游地址（上游服务没起/端口关了）"
    if "Invalid model name" in m or "ProxyModelNotFoundError" in m:
        return "模型名在网关上不存在"
    if str(code) == "429":
        return "上游限流"
    return "未归类，看原文"


def fallback_chain(group: str, key_rs: dict | None, global_fb: list[dict]) -> list[str]:
    """这把 key 调 group 时生效的兜底链。per-key 表**整表替换**全局表，不是合并。"""
    rows = ((key_rs or {}).get("fallbacks") or []) or global_fb
    for row in rows:
        if group in row:
            return list(row[group])
    return []


def key_exposure(keys: list[dict], dead: set[str], global_alias: dict) -> dict:
    """死组在 key 上的三种挂法，按 key 前缀族计数。blocked 的 key 不算（认证不过）。

    返回 {"allow": {(name, target): Counter}, "pkfb": {(src, legs, first_dead): Counter},
          "unrestricted": int}
    - allow：白名单里的名字经 per-key alias → 全局 alias → 同名 解析后落在死组
    - pkfb：per-key fallbacks 行含死腿；first_dead=第一跳就死（主腿一挂就多等一次再报错）
    - unrestricted：models=[] 全放行，摘不了白名单，理论上都能调到
    """
    allow: dict = collections.defaultdict(collections.Counter)
    pkfb: dict = collections.defaultdict(collections.Counter)
    unrestricted = 0
    for k in keys:
        if k.get("blocked"):
            continue
        fam = (k.get("key_alias") or "<null>").split("-")[0]
        models = k.get("models") or []
        aliases = k.get("aliases") or {}
        if not models:
            unrestricted += 1
        for name in models:
            target = aliases.get(name) or global_alias.get(name) or name
            if target in dead:
                allow[(name, target)][fam] += 1
        for row in ((k.get("router_settings") or {}).get("fallbacks") or []):
            for src, legs in row.items():
                hit = tuple(l for l in legs if l in dead)
                if hit:
                    pkfb[(src, hit, bool(legs) and legs[0] in dead)][fam] += 1
    return {"allow": dict(allow), "pkfb": dict(pkfb), "unrestricted": unrestricted}


def global_exposure(global_fb: list[dict], global_alias: dict, dead: set[str],
                    existing: set[str]) -> list[tuple[str, str]]:
    """全局 fallbacks / model_group_alias 里碰到死组的行。源组已不存在的单独标出（不会触发）。"""
    out = []
    for name, target in sorted(global_alias.items()):
        if target in dead:
            out.append(("alias", f"{name} -> {target}"))
    for row in global_fb:
        for src, legs in row.items():
            hit = [l for l in legs if l in dead]
            if src in dead or hit:
                tag = "源组已不存在" if src not in existing else ("源组本身是死组" if src in dead else "")
                out.append(("fallback", f"{src} {legs} {tag}".rstrip()))
    return out


def traffic(rows: list[dict], alias_of: dict, synthetic: tuple[str, ...]) -> dict:
    """死组流量拆三份：巡检 / 真人直选 / 真人经兜底落进来。

    rows: [{model_group, req_model, status, api_key, n, last}]
    「直选」= 请求名就是这个组，或请求名经 alias 改写到它（req_model 记的是改写后的名）；
    「兜底」= 请求名是别的组，被兜底链带进来——这种失败不该算到该组用户头上。
    请求名为空（老行没记 proxy_server_request.model）归「直选」，打印成 ``?``。
    ⚠️ 库里查不到的 api_key（master key 记作 ``litellm_proxy_master_key``、探针用完就删的临时 key）
    单独计数为 ``unknown`` —— 10-20 实测每个死组都有十几把这种 key 各打 2 发，混进真人会把探针读成用户。
    """
    out: dict = {"synthetic": collections.Counter(), "unknown": collections.Counter(),
                 "direct": collections.defaultdict(list), "via_fallback": collections.defaultdict(list)}
    for r in rows:
        who = alias_of.get(r["api_key"])
        if who is None:
            out["unknown"][r["model_group"]] += r["n"]
            continue
        if who in synthetic:
            out["synthetic"][r["model_group"]] += r["n"]
            continue
        bucket = "direct" if r.get("req_model") in (None, "", r["model_group"]) else "via_fallback"
        out[bucket][r["model_group"]].append(
            (who, r.get("req_model"), r["status"], r["n"], str(r.get("last"))[:16]))
    return out


# ---------------------------------------------------------------- 198 本机执行

def _sh(cmd: list[str], stdin: str | None = None, timeout: int = 300) -> str:
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[:6]} rc={p.returncode}: {p.stderr.strip()[:400]}")
    return p.stdout


def psql_json(sql: str):
    """SQL 走 stdin 进 pod（避开多层引号）。查询必须 select 出一列 json。"""
    out = _sh(["sudo", "-n", "kubectl", "-n", NS, "exec", "-i", "litellm-db-0", "--",
               "psql", "-U", "litellm", "-d", "litellm", "-At", "-v", "ON_ERROR_STOP=1", "-f", "-"],
              stdin="SET statement_timeout='240s';\n" + sql)
    # json_agg(record) 在元素之间带换行 ⇒ 不能只取最后一行；去掉 SET 的回显后整体解析
    body = "\n".join(l for l in out.splitlines() if l.strip() != "SET").strip()
    return json.loads(body) if body else None


def master_key() -> str:
    raw = _sh(["sudo", "-n", "kubectl", "-n", NS, "get", "secret", "litellm-secrets-r2",
               "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"])
    mk = base64.b64decode(raw).decode()
    if len(mk) < 20:
        raise RuntimeError(f"master key 读空了 len={len(mk)}")
    return mk


def http(mk: str, method: str, path: str, body: dict | None = None, timeout: int = 120):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(PROXY + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + mk,
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def router_settings() -> tuple[list[dict], dict]:
    rs = psql_json("select param_value::text from \"LiteLLM_Config\" where param_name='router_settings';") or {}
    galias = {k: (v.get("model") if isinstance(v, dict) else v)
              for k, v in (rs.get("model_group_alias") or {}).items()}
    return list(rs.get("fallbacks") or []), galias


def all_keys() -> list[dict]:
    return psql_json("""select json_agg(json_build_object('token',token,'key_alias',key_alias,
      'blocked',coalesce(blocked,false),'models',models,'aliases',aliases,'router_settings',router_settings))::text
      from "LiteLLM_VerificationToken";""") or []


def prev_round_tools(api_key: str, t: str) -> str:
    """同一把 key 出错前 2 分钟内成功轮次里模型调用的工具名。

    10-22 破案就靠这一行：空 input 前一轮全是 ``view_image``（截图 base64 几 MB、token 极少）。
    SpendLogs 的文本列被截到 2048 字符，量不出 item 大小；工具名在 response.output 里是完整的。
    """
    rows = psql_json(f"""select coalesce(json_agg(o->>'name'),'[]')::text
      from "LiteLLM_SpendLogs" p, jsonb_array_elements(case when jsonb_typeof(p.response->'output')='array'
           then p.response->'output' else '[]'::jsonb end) o
      where p.api_key='{api_key}' and p.status='success'
        and p."startTime" between timestamp '{t}' - interval '2 minutes' and timestamp '{t}'
        and o->>'type' in ('function_call','custom_tool_call');""") or []
    if not rows:
        return "（2 分钟内没有成功轮次）"
    c = collections.Counter(rows)
    return " ".join(f"{k}×{v}" for k, v in c.most_common()) + ("  ⚠️ 有 view_image：截图" if "view_image" in c else "")


def run_rid(rid: str) -> int:
    if not re.fullmatch(r"[0-9a-f-]{6,36}", rid):
        print("⛔ rid 只能是十六进制前缀（报错里 req: 后面那 8 位）")
        return 2
    # request_id 是主键，但前缀 like 不加时间窗会全表扫
    rows = psql_json(f"""select json_agg(r)::text from (
      select s.request_id, s."startTime" t, s.status, s.call_type, s.model_group,
             s.proxy_server_request->>'model' req_model, s.model,
             case when jsonb_typeof(s.proxy_server_request->'input')='array'
                  then jsonb_array_length(s.proxy_server_request->'input') end n_input,
             (s.proxy_server_request->'litellm_metadata'->'headers'->>'content-length')::bigint body_len,
             s.metadata->'error_information'->>'error_code' code,
             left(s.metadata->'error_information'->>'error_message', 600) msg,
             s.request_tags, s.api_key, k.key_alias, coalesce(k.blocked,false) blocked, round(k.spend::numeric,2) spend,
             k.max_budget, k.budget_duration, k.budget_reset_at, k.router_settings,
             k.metadata->'budget_family_overrides' fam
      from "LiteLLM_SpendLogs" s left join "LiteLLM_VerificationToken" k on k.token=s.api_key
      where s.request_id like '{rid}%' and s."startTime" > now() - interval '7 days') r;""")
    if not rows:
        print(f"查不到 req {rid}：超过 7 天 / SpendLogs 被清过（先看 min(startTime)）/ 请求没进到网关")
        return 1
    global_fb = router_settings()[0]
    for r in rows:
        print(f"=== req {r['request_id']}  {r['t']}")
        if r["key_alias"] is None:
            print("key       库里查不到（key 已删 / master key / 临时探针 key）")
        else:
            print(f"key       {r['key_alias']}  blocked={r['blocked']}  今日已用 ${r['spend']} / ${r['max_budget']}"
                  f"（{r['budget_duration']}，重置 {r['budget_reset_at']}，分族 {r['fam']}）")
        print(f"请求的名  {r['req_model']}   →  落点 {r['model_group']}（上游 {r['model']}）  {r['call_type']}  {r['status']}")
        if r["req_model"] and r["req_model"] != r["model_group"]:
            print("          ⚠️ 落点≠请求名：是兜底链带过去的，报错里的供应商不是用户选的")
            print(f"          {r['req_model']} 的兜底链：{fallback_chain(r['req_model'], r['router_settings'], global_fb)}")
        if r["n_input"] == 0:
            bl = r.get("body_len") or 0
            print(f"          ⚠️ input 是空数组（进网关时 body {bl/1e6:.1f}MB）")
            if r.get("api_key"):
                print(f"          前一轮调的工具：{prev_round_tools(r['api_key'], r['t'])}")
        print(f"客户端    {r['request_tags']}")
        print(f"错误      code={r['code']}  {(r['msg'] or '').splitlines()[0][:300] if r['msg'] else ''}")
        print(f"判定      {classify_error(r['code'], r['msg'] or '', r.get('body_len'))}")
    return 0


def run_upstream(pattern: str, probe: bool, synthetic: tuple[str, ...]) -> int:
    import concurrent.futures as cf
    mk = master_key()
    st, info = http(mk, "GET", "/model/info", timeout=180)
    if st != 200 or not isinstance(info, dict):
        print(f"⛔ /model/info {st}")
        return 2
    deps = info["data"]
    by_group = collections.defaultdict(list)
    for d in deps:
        by_group[d["model_name"]].append(d)
    hit = {g: ds for g, ds in by_group.items() if any(matches_upstream(d, pattern) for d in ds)}
    print(f"部署 {len(deps)} / 组 {len(by_group)}；上游含「{pattern}」的组 {len(hit)}")
    if not hit:
        return 0

    def one(g: str):
        ds = by_group[g]
        others = sum(1 for d in ds if not matches_upstream(d, pattern))
        if not probe:
            return g, None, "未探测", others
        emb = any((d.get("model_info") or {}).get("mode") == "embedding" for d in ds)
        body = ({"model": g, "input": "hi"} if emb else
                {"model": g, "messages": [{"role": "user", "content": "say pong"}], "max_tokens": 16})
        body.update(disable_fallbacks=True, num_retries=0)
        code, j = http(mk, "POST", "/v1/embeddings" if emb else "/v1/chat/completions", body, timeout=90)
        text = j if isinstance(j, str) else json.dumps(j, ensure_ascii=False)
        return g, code, ("ok" if code == 200 else text[:300]), others

    with cf.ThreadPoolExecutor(8) as ex:
        res = sorted(ex.map(one, sorted(hit)))
    # 网关 error_sanitize 把原文换成「API 异常 (req: xxxxxxxx)」，响应体里分不出欠费还是连不上
    # ⇒ 按 req 前缀回 SpendLogs 取原文再归类（SpendLogs 异步落库，等几秒）
    rids = {g: m.group(1) for g, _, why, _ in res if (m := re.search(r"req: ([0-9a-f]{8})", why or ""))}
    if rids:
        import time
        time.sleep(8)
        likes = " or ".join(f"request_id like '{r}%'" for r in rids.values())
        orig = psql_json(f"""select coalesce(json_object_agg(left(request_id,8),
              metadata->'error_information'->>'error_message'),'{{}}')::text from "LiteLLM_SpendLogs"
              where "startTime" > now()-interval '30 minutes' and ({likes});""") or {}
        def verdict(g, why):
            if g not in rids:
                return why
            if rids[g] in orig:
                return classify_error(None, orig[rids[g]])
            # 10-20 实测：连不上上游的 500 不落 SpendLogs，原文只在 pod 日志
            return f"SpendLogs 无原文 ⇒ pod 日志 grep litellm_call_id={rids[g]}"
        res = [(g, code, verdict(g, why), o) for g, code, why, o in res]
    print("\n== 1. 逐组直打（关兜底）")
    for g, code, why, others in res:
        print(f"  {g:42s} {code}  {why}" + (f"  ⚠️组内还有 {others} 个别家部署" if others else ""))
    dead = {g for g, code, _, _ in res if code != 200}

    keys = all_keys()
    global_fb, galias = router_settings()
    ex_ = key_exposure(keys, dead, galias)
    print(f"\n== 2. key 白名单解析到死组（未封 key；models=[] 全放行 {ex_['unrestricted']} 把摘不了）")
    for (name, tgt), c in sorted(ex_["allow"].items(), key=lambda x: -sum(x[1].values())):
        print(f"  {name:36s} -> {tgt:36s} {sum(c.values()):5d} 把 {dict(c.most_common(4))}")
    print("\n== 3. per-key 兜底行含死腿（★=第一跳就死）")
    for (src, legs, first), c in sorted(ex_["pkfb"].items(), key=lambda x: -sum(x[1].values())):
        print(f"  {'★' if first else ' '} {src:32s} 死腿 {list(legs)}  {sum(c.values())} 把 {dict(c)}")
    print("\n== 4. 全局 fallbacks / model_group_alias")
    for kind, line in global_exposure(global_fb, galias, dead, set(by_group)):
        print(f"  [{kind}] {line}")

    names = ",".join("'" + g.replace("'", "''") + "'" for g in sorted(dead)) or "''"
    rows = psql_json(f"""select coalesce(json_agg(r),'[]')::text from (
      select model_group, proxy_server_request->>'model' req_model, status, api_key, count(*) n, max("startTime") last
      from "LiteLLM_SpendLogs" where "startTime" > now()-interval '7 days' and model_group in ({names})
      group by 1,2,3,4) r;""") or []
    since = psql_json('select to_json(min("startTime"))::text from "LiteLLM_SpendLogs";')
    tr = traffic(rows, {k["token"]: k["key_alias"] for k in keys}, synthetic)
    print(f"\n== 5. 死组 7 天流量（SpendLogs 最早 {since}；巡检 key {list(synthetic)} 已剔）")
    print(f"  巡检合计 {sum(tr['synthetic'].values())} 次")
    print(f"  库里查不到的 key（master key/已删的临时探针 key）合计 {sum(tr['unknown'].values())} 次——不算真人")
    for label, bucket in (("真人直选", tr["direct"]), ("真人经兜底落进来（不是他选的）", tr["via_fallback"])):
        print(f"  -- {label}")
        for g, lst in sorted(bucket.items(), key=lambda x: -sum(y[3] for y in x[1])):
            print(f"     {g}  共 {sum(y[3] for y in lst)} 次")
            for who, req, status, n, last in sorted(lst, key=lambda y: -y[3])[:8]:
                print(f"        {who:32s} 请求 {req or '?':30s} {status:7s} {n:5d}  最后 {last}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("rid", help="按报错里的 req id 反查")
    a1.add_argument("rid")
    a2 = sub.add_parser("upstream", help="某家上游挂了的影响面")
    a2.add_argument("pattern", help="litellm_params.model / api_base 子串，如 openrouter")
    a2.add_argument("--no-probe", action="store_true", help="不直打，匹配组全当死组算")
    a2.add_argument("--synthetic", action="append", default=[], help="额外当巡检剔除的 key_alias")
    ap.add_argument("--on-host", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if not a.on_host:
        # 本机 → 把自己整份送到 198 跑（psql/kubectl/30402 都只在那边可达）
        src = open(__file__, encoding="utf-8").read()
        argv = ["--on-host"] + sys.argv[1:]   # 顶层开关必须在子命令前，否则子解析器报 unrecognized
        b64 = base64.b64encode(src.encode()).decode()
        remote = f"echo {b64} | base64 -d > /tmp/_ket_$$.py; python3 /tmp/_ket_$$.py {' '.join(map(_q, argv))}; rc=$?; rm -f /tmp/_ket_$$.py; exit $rc"
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", f"{USER}@{HOST}", remote],
                           stdin=subprocess.DEVNULL)
        return p.returncode
    if a.cmd == "rid":
        return run_rid(a.rid)
    return run_upstream(a.pattern, not a.no_probe, SYNTHETIC_KEYS + tuple(a.synthetic))


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    sys.exit(main())
