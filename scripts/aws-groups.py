#!/usr/bin/env python3
"""198 LiteLLM：给 kiro 槽位实例建一套对外中性的 `aws-*` 组（2026-10-27 用户定「后台 kiro 统一改 aws」）。
在生产 litellm pod 里跑（打 127.0.0.1:4000，master key 取 env）。不重启 LiteLLM。

  python3 aws-groups.py plan   <slots_api_key>   # 只打印
  python3 aws-groups.py apply  <slots_api_key>   # 加（已存在的跳过）
  python3 aws-groups.py remove                   # 回滚：删掉本脚本建的 aws-* 组（认 model_info.id 前缀 aws/）

来源 = 现有 `kiro-slots-*` 组（DB 行），一一照抄，只改四处：
- 组名 `kiro-slots-X` → `aws-X`
- api_base 写槽位实例 ClusterIP（不写服务名：`/v2/model/info` 对普通 key 也吐 api_base，
  服务名里有 kiro）；身份头 callback 已认这个 IP（kiro_slots_identity._DEFAULT_HOSTS）
- model_info.id `kiro-slots/X` → `aws/X`；`kiro_rate_multiplier` / `kiro_upstream_model_id`
  改名 `upstream_rate_multiplier` / `upstream_model_id`（这两个字段只给运维脚本看，LiteLLM 不读）
- litellm_params.model 里的 `kiro-` 逃生舱前缀（kiro-kimi-k3）去掉。它原本是躲 LiteLLM 的 OpenAI 同名表
  （`open_ai_chat_completion_models` 里有 gpt-5.6-* ⇒ 劈到 /chat/completions 404）。不带前缀的替代：
  撞表的名字改大写（`GPT-5.6-sol`），那张表大小写敏感、kiro.rs `map_model` 先 to_lowercase ⇒ 上游拿到原名。
  10-27 经 LiteLLM 直打槽位实例实测：GPT-5.6-sol/terra/luna、kimi-k3、auto 流式 + 工具调用 5/5 都通。

另加 `aws-auto`（上游 Kiro 的 auto，按任务自动选模型）：价格与 sonnet-5.5 完全一致（用户定），
其余照抄 aws-claude-sonnet-5-5，upstream_model_id=auto、倍率 1.0、窗口 1M/64K（ListAvailableModels 实测）。
🔴 model_info 不带 mode（memory feedback_model_new_mode_poisons_shared_backend_key）
"""
import json, os, sys, urllib.request

BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
SLOTS_BASE = os.environ.get("AWS_SLOTS_BASE", "http://10.43.252.245:8990")
SRC_PREFIX, DST_PREFIX = "kiro-slots-", "aws-"
RENAME_INFO = {"kiro_rate_multiplier": "upstream_rate_multiplier", "kiro_upstream_model_id": "upstream_model_id"}
# 只搬 LiteLLM 认的能力/价格字段 + 我们自己的两个运维字段；其余（db_model、litellm_provider、一堆 null）不搬
KEEP_INFO = ("input_cost_per_token", "output_cost_per_token", "cache_creation_input_token_cost",
             "cache_read_input_token_cost", "cache_creation_input_token_cost_above_1hr",
             "max_input_tokens", "max_output_tokens", "prompt_cache_min_tokens",
             "supports_vision", "supports_function_calling", "supports_tool_choice", "supports_reasoning",
             "supports_prompt_caching", "supports_pdf_input", "supports_response_schema",
             "supports_assistant_prefill", "supports_web_search", "supports_native_structured_output",
             "supports_thinking_cache_preservation", "kiro_rate_multiplier", "kiro_upstream_model_id")
AUTO_TEMPLATE = "aws-claude-sonnet-5-5"
PRICE_KEYS = ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost",
              "cache_creation_input_token_cost", "cache_creation_input_token_cost_above_1hr")


def api(path, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                               method="POST" if body is not None else "GET",
                               headers={"Authorization": f"Bearer {MK}", "content-type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=120))


def all_deployments():
    out, page = [], 1
    while True:
        d = api(f"/v2/model/info?page={page}&size=100")
        out += d.get("data", [])
        if page >= d.get("total_pages", 1):
            return out
        page += 1


def neutral_model(model):
    """`anthropic/kiro-X` → `anthropic/X`；X 撞 LiteLLM OpenAI 表就改成大写开头的 GPT-（见文件头）。"""
    import litellm
    prov, _, mid = model.partition("/")
    mid = mid.removeprefix("kiro-")
    if mid.lower() in litellm.open_ai_chat_completion_models:
        assert mid.lower().startswith("gpt-"), mid
        mid = "GPT-" + mid[4:]
    assert mid not in litellm.open_ai_chat_completion_models, mid
    return f"{prov}/{mid}"


def convert(m, slots_key):
    name = m["model_name"]
    # 价格字段写在 litellm_params 里才会覆盖 LiteLLM 自带价目（kiro-slots-kimi-k3 的缓存价就写在这）
    p = {k: v for k, v in m["litellm_params"].items() if k == "model" or k in PRICE_KEYS}
    p["api_base"], p["api_key"] = SLOTS_BASE, slots_key
    p["model"] = neutral_model(p["model"])
    src = m.get("model_info") or {}
    info = {RENAME_INFO.get(k, k): v for k, v in src.items() if k in KEEP_INFO and v is not None}
    info["id"] = "aws/" + str(src.get("id", name)).split("/", 1)[-1].removeprefix("kiro-")
    # yaml 行把价格只写在 model_info 里（litellm_params 没有缓存价）⇒ 也搬进 litellm_params，和 DB 行一致
    for k in PRICE_KEYS:
        if k not in p and src.get(k) is not None:
            p[k] = src[k]
    return {"model_name": DST_PREFIX + name[len(SRC_PREFIX):], "litellm_params": p, "model_info": info}


def legacy_only(deps):
    """老实例 `kiro-X`（config.yaml）里有、槽位实例没有 `kiro-slots-X` 的组 ⇒ 也照抄成 aws-X
    （10-27：4.5 两个、deepseek-3.2、glm-5、minimax×2、gpt-5.6×3；直打槽位实例工具调用都通）。
    老实例组名 kiro-gpt5.6-sol 少个中划线，aws 名统一成 aws-gpt-5.6-sol。"""
    have = {m["model_name"][len(SRC_PREFIX):] for m in deps if m["model_name"].startswith(SRC_PREFIX)}
    out = []
    for m in deps:
        n = m["model_name"]
        if n.startswith("kiro-") and not n.startswith(SRC_PREFIX) and n[len("kiro-"):] not in have \
                and n != "kiro-auto" and not str((m.get("model_info") or {}).get("id", "")).startswith("kiro-slots/"):
            base = n[len("kiro-"):].replace("gpt5.6-", "gpt-5.6-")
            out.append(dict(m, model_name=SRC_PREFIX + base))
    return out


def planned(deps, slots_key):
    rows = [convert(m, slots_key) for m in deps if m["model_name"].startswith(SRC_PREFIX)]
    rows += [convert(m, slots_key) for m in legacy_only(deps)]
    names = [r["model_name"] for r in rows]
    assert len(names) == len(set(names)), "同名 kiro-slots 组有多个 deployment，先人工看"
    tmpl = next(r for r in rows if r["model_name"] == AUTO_TEMPLATE)
    auto = json.loads(json.dumps(tmpl))
    auto["model_name"] = "aws-auto"
    auto["litellm_params"]["model"] = "anthropic/auto"
    # `auto` 不在 LiteLLM 自带价目里 ⇒ sonnet-5.5 从价目表继承的缓存价要显式写进来，否则缓存 token 记 0
    sonnet = next(m for m in deps if m["model_name"] == SRC_PREFIX + AUTO_TEMPLATE[len(DST_PREFIX):])
    for k in PRICE_KEYS:
        v = (sonnet.get("model_info") or {}).get(k)
        if v is not None:
            auto["litellm_params"][k] = v
            auto["model_info"][k] = v
    auto["model_info"].update({"id": "aws/auto", "upstream_model_id": "auto", "upstream_rate_multiplier": 1.0,
                               "max_input_tokens": 1000000, "max_output_tokens": 64000})
    rows.append(auto)
    for r in rows:
        blob = json.dumps({"n": r["model_name"], "i": r["model_info"], "b": r["litellm_params"]["api_base"],
                           "m": r["litellm_params"]["model"]})
        assert "kiro" not in blob.lower(), r["model_name"]
        assert "mode" not in r["model_info"], r["model_name"]
    return rows


def main():
    cmd = sys.argv[1]
    deps = all_deployments()
    have = {}
    for m in deps:
        if m["model_name"].startswith(DST_PREFIX):
            have.setdefault(m["model_name"], []).append((m.get("model_info") or {}).get("id"))
    if cmd == "remove":
        n = 0
        for name, ids in have.items():
            for i in ids:
                if str(i).startswith("aws/"):
                    api("/model/delete", {"id": i}); n += 1
        print("OK removed", n, sorted(have))
        return
    rows = planned(deps, sys.argv[2])
    only = [x for x in os.environ.get("ONLY_GROUPS", "").split(",") if x]
    if only:
        rows = [r for r in rows if r["model_name"] in only]
    for r in rows:
        if r["model_name"] in have:
            print("SKIP exists", r["model_name"]); continue
        if cmd == "apply":
            api("/model/new", r)
        print("ADD " if cmd == "apply" else "PLAN", r["model_name"], r["litellm_params"]["model"], r["model_info"]["id"],
              {k: v for k, v in r["model_info"].items() if k in ("input_cost_per_token", "output_cost_per_token",
                                                             "upstream_model_id", "max_input_tokens")})
    print("OK", cmd, len(rows), "groups")


main()
