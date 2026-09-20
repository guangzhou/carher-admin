#!/usr/bin/env python3
"""把 sub2api 的 grok 全家注册到 198 生产 LiteLLM。

用户点的是 24 个名字，其中 **22 个走这里**（model_list / `/model/new`），
**2 个 video 走不了 model_list**、只能 pass-through，见文件下半部分
`VIDEO_NAMES_VIA_PASSTHROUGH` 那段注释和
`scripts/litellm-198-sub2api-video-passthrough.py`。

只走 `/model/new` 这条零重启路径（`store_model_in_db: true`），不碰
`cm/litellm-config`、不 apply、不 restart。参照
`scripts/litellm-add-deepseek-v4-pro-official.py` 的形状：
纯 payload builder + 默认 dry-run + `--apply` + 写完探针回读。

在 198 本机跑（需要 127.0.0.1:30402 和 sudo kubectl）：

    MK=$(sudo kubectl -n litellm-product get secret litellm-secrets \
          -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
    LITELLM_MASTER_KEY=$MK python3 litellm-198-add-sub2api-grok-family.py plan
    LITELLM_MASTER_KEY=$MK python3 litellm-198-add-sub2api-grok-family.py add --apply

要点（每一条都是踩过的坑）：
  * 价格一律写**官方 list price**。198 上挂了全局 `pricing_overlay`
    callback，`GLOBAL_COST_MULTIPLIER=1.3` 会再乘一遍；这里如果预乘就翻倍。
  * cost 字段**双写** `litellm_params` 和 `model_info` —— 只写一处的话
    `/model/info` 读出来是对的，但真实计费走另一处。
  * master key 只从 env 读。`os.environ.get("K","<真key>")` 那种兜底默认值
    等于把凭据提交进仓库。
  * `sa-grok-4.5` / `sa-grok-4.6` / `sa-grok-4.20` 已存在，且 `sa-grok-4.6`
    是 cursor gpt 家族的线上 fallback 落点 —— 默认**跳过**，不 delete+re-add。
  * 组名会撒谎，判落点只认 `litellm_params.model` + `api_base`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROXY = os.environ.get("LITELLM_198", "http://127.0.0.1:30402")
SUB2API_BASE = "http://sub2api.litellm-dev.svc.cluster.local:8080/v1"
PREFIX = "sa-"

# sub2api 侧的上游 key（明文存在 sub2api 自己的 postgres api_keys.key，
# name='grok-litellm'）。不在本文件里写死，运行时从 env 读。
API_KEY_ENV = "SUB2API_GROK_KEY"

# ---------------------------------------------------------------------------
# 官方 list price，取自 LiteLLM 内置 model_prices_and_context_window_backup.json
# 的 `xai/*` 条目（1.100.1）。imagine-video 两个名字内置表没有，用实测单价：
# 8s 片子 $0.40 => $0.05/s（sub2api usage_logs.total_cost 实测）。
# ---------------------------------------------------------------------------
CHAT_1M = dict(
    input_cost_per_token=1.25e-06,
    output_cost_per_token=2.5e-06,
    cache_read_input_token_cost=2e-07,
    input_cost_per_token_above_200k_tokens=2.5e-06,
    output_cost_per_token_above_200k_tokens=5e-06,
    cache_read_input_token_cost_above_200k_tokens=4e-07,
    ctx=1000000,
)
CHAT_45 = dict(
    input_cost_per_token=2e-06,
    output_cost_per_token=6e-06,
    cache_read_input_token_cost=3e-07,
    input_cost_per_token_above_200k_tokens=4e-06,
    output_cost_per_token_above_200k_tokens=1.2e-05,
    cache_read_input_token_cost_above_200k_tokens=6e-07,
    ctx=500000,
)
CHAT_46 = dict(
    input_cost_per_token=2e-06,
    output_cost_per_token=6e-06,
    cache_read_input_token_cost=5e-07,
    input_cost_per_token_above_200k_tokens=4e-06,
    output_cost_per_token_above_200k_tokens=1.2e-05,
    cache_read_input_token_cost_above_200k_tokens=1e-06,
    ctx=500000,
)
CHAT_BUILD = dict(
    input_cost_per_token=1e-06,
    output_cost_per_token=2e-06,
    cache_read_input_token_cost=2e-07,
    input_cost_per_token_above_200k_tokens=2e-06,
    output_cost_per_token_above_200k_tokens=4e-06,
    cache_read_input_token_cost_above_200k_tokens=4e-07,
    ctx=256000,
)

# upstream_name -> (mode, price dict / image or video cost, 实测落点)
# `lands` = 实测该请求最终落在哪个真模型上（response body 的 model 字段）。
# 只做记录用，不进 payload —— 别拿它当路由依据。
SPEC: list[dict] = [
    # ---- chat (/v1/chat/completions 实测 200) ----
    dict(name="grok-4.6", mode="chat", price=CHAT_46, lands="grok-4.6"),
    dict(name="grok-4.5", mode="chat", price=CHAT_45, lands="grok-4.5"),
    dict(name="grok-4.3", mode="chat", price=CHAT_1M, lands="grok-4.3"),
    dict(name="grok-4.6-latest", mode="chat", price=CHAT_46, lands="grok-4.6"),
    dict(name="grok-4.5-latest", mode="chat", price=CHAT_45, lands="grok-4.5"),
    dict(name="grok-4.3-latest", mode="chat", price=CHAT_1M, lands="grok-4.3"),
    dict(name="grok-latest", mode="chat", price=CHAT_46, lands="grok-4.6"),
    dict(name="grok-build-0.1", mode="chat", price=CHAT_BUILD, lands="grok-build-0.1"),
    dict(name="grok-build-latest", mode="chat", price=CHAT_BUILD, lands="grok-build-0.1"),
    dict(name="composer-2.5", mode="chat", price=CHAT_45, lands="grok-4.5"),
    # 用户 2026-09-20 点名：组名去掉 grok-，和既有 `sa-composer-2.5` 一族对齐。
    # `public` 覆盖默认的 PREFIX+name —— 上游名仍是 grok-composer-2.5-fast。
    dict(name="grok-composer-2.5-fast", public="sa-composer-2.5-fast",
         mode="chat", price=CHAT_45, lands="grok-4.5"),
    dict(name="grok-4.20-0309-reasoning", mode="chat", price=CHAT_1M,
         lands="grok-4.20-0309-reasoning"),
    dict(name="grok-4.20-0309-non-reasoning", mode="chat", price=CHAT_1M,
         lands="grok-4.20-0309-non-reasoning"),
    dict(name="grok-4.20-reasoning", mode="chat", price=CHAT_1M,
         lands="grok-4.20-0309-reasoning"),
    dict(name="grok-4.20-non-reasoning", mode="chat", price=CHAT_1M,
         lands="grok-4.20-0309-non-reasoning"),
    # ---- responses (chat 返 400，/v1/responses 返 200；内置表也标 responses) ----
    dict(name="grok-4.20-multi-agent", mode="responses", price=CHAT_1M,
         lands="grok-4.20-multi-agent"),
    dict(name="grok-4.20-multi-agent-0309", mode="responses", price=CHAT_1M,
         lands="grok-4.20-multi-agent-0309"),
    dict(name="grok-4.20-multi-agent-latest", mode="responses", price=CHAT_1M,
         lands="grok-4.20-multi-agent"),
    # ---- image_generation (/v1/images/generations 实测 200) ----
    # cost 取内置 xai/* 表；grok-imagine 内置表没有，按 grok-imagine-image 同价。
    dict(name="grok-imagine", mode="image_generation", cost_per_image=0.02),
    dict(name="grok-imagine-image", mode="image_generation", cost_per_image=0.02),
    dict(name="grok-imagine-image-quality", mode="image_generation", cost_per_image=0.05),
    dict(name="grok-imagine-image-2.0", mode="image_generation", cost_per_image=0.06),
]

# ---------------------------------------------------------------------------
# 🔴 video 的两个名字**不在 SPEC 里**，而且不是漏了 —— 它们进不了 model_list。
#
# 2026-09-20：`sa-grok-imagine-video` / `sa-grok-imagine-video-1.5` 曾经按
# `mode=video_generation` 注册过，写入返 200、`/model/info` 读得到，但一发都打不通。
# 根因是 LiteLLM 的 `/v1/videos` 通路和 sub2api **三处各自独立地不兼容**：
#
#   1. 编码：`OpenAIVideoConfig.use_multipart_form_data()` 硬编码 True ⇒ 无条件发
#      multipart；sub2api 只吃 JSON，multipart 一律 415。
#   2. 路径：`get_complete_url()` 硬编码 `{api_base}/videos`；sub2api 真身是
#      `/v1/videos/generations`（裸 `/v1/videos` 恰好也通，所以这条是次要的）。
#   3. 响应：`transform_video_create_response` 走 `VideoObject.model_validate`，
#      强制 `id` + `object` + `status`；sub2api 返 `{"request_id": …}`，必炸。
#
# ⛔ **升级修不好。** upstream PR #38104（2026-08-24 合入）就是**故意**把
# `/v1/videos` 从 JSON 改成无条件 multipart，去对齐官方 OpenAI SDK 的线上格式。
# 它早于我们跑的 v1.100.1 ⇒ multipart 现在是 intended behavior，不是 bug。
# 同类诉求见 issue #36493（仍 open）。
#
# ✅ 正解是 pass-through，纯配置零代码：
#      scripts/litellm-198-sub2api-video-passthrough.py
#    `POST /config/pass_through_endpoint` 建 `/sa-video` → sub2api `/v1/videos`，
#    `include_subpath: true` 让异步三段式（/generations、/{rid}、/{rid}/content）
#    全部转发过去，`auth: true` 保住 LiteLLM 侧 key 认证。
#    代价：pass-through **不进 LiteLLM 计费/SpendLogs**（`cost_per_request` 是
#    单发定额，而 video 按秒计），账在 sub2api 自己的 ledger 上。
#
# 回滚料（两行 `LiteLLM_ProxyModelTable` 的 row_to_json）：
#   198:~/grok-onboard/backups/video-models-20260920-140413.json
# ---------------------------------------------------------------------------
VIDEO_NAMES_VIA_PASSTHROUGH = ("grok-imagine-video", "grok-imagine-video-1.5")

# 已存在、默认不动的 public name。sa-grok-4.6 是 cursor 家族线上 fallback 落点。
PREEXISTING = {"sa-grok-4.6", "sa-grok-4.5", "sa-grok-4.20"}


def public_name(spec: dict) -> str:
    """对外组名。默认 PREFIX+上游名，spec 里给了 `public` 就用它。

    组名和上游名是两件事 —— 用户可以要一个不含 `grok-` 的对外名，而
    `litellm_params.model` 仍必须是上游认的那个名。
    """
    return spec.get("public") or (PREFIX + spec["name"])


def model_info_id(pub: str) -> str:
    """`model_info.id` 跟着**对外名**走，不跟上游名走。

    改组名时新旧两行会短暂共存（add→verify→remove），id 相同会撞。
    `sa-x` -> `sa/x`，对既有 22 行是恒等变换（实测逐行核过）。
    """
    return pub.replace(PREFIX, "sa/", 1) if pub.startswith(PREFIX) else pub


def build_model_new_payload(spec: dict, api_key: str) -> dict:
    """纯函数：spec -> /model/new body。没有副作用，可单测。"""
    upstream = spec["name"]
    mode = spec["mode"]
    pub = public_name(spec)

    params: dict = {
        # openai/ 前缀：已验证这 24 个名字与 litellm.open_ai_chat_completion_models
        # 零碰撞，不会被劈成内置 OpenAI provider。
        "model": f"openai/{upstream}",
        "api_base": SUB2API_BASE,
        "api_key": api_key,
    }
    info: dict = {
        "id": model_info_id(pub),
        "base_model": upstream,
        "mode": mode,
    }

    if mode in ("chat", "responses"):
        p = spec["price"]
        costs = {k: v for k, v in p.items() if k != "ctx"}
        params.update(costs)
        info.update(costs)
        ctx = p["ctx"]
        info.update(
            max_tokens=ctx,
            max_input_tokens=ctx,
            max_output_tokens=ctx,
            supports_function_calling=True,
            supports_tool_choice=True,
            supports_vision=True,
            supports_prompt_caching=True,
            supports_response_schema=True,
            supports_reasoning=True,
        )
    elif mode == "image_generation":
        costs = {"input_cost_per_image": spec["cost_per_image"]}
        params.update(costs)
        info.update(costs)
        info["supported_output_modalities"] = ["image"]
    elif mode == "video_generation":
        # 故意 raise：`mode=video_generation` 在这条路上注册得进去但一发打不通
        # （见 VIDEO_NAMES_VIA_PASSTHROUGH 那段）。让它在 builder 里就炸，
        # 而不是写进 DB 之后变成一行永远 400 的死条目。
        raise ValueError(
            f"{upstream}: LiteLLM `/v1/videos` 与 sub2api 结构性不兼容"
            "（multipart/路径/VideoObject 三处），且 PR #38104 让升级也修不好。"
            "走 scripts/litellm-198-sub2api-video-passthrough.py")
    else:  # pragma: no cover - SPEC 是闭集
        raise ValueError(f"unknown mode {mode!r} for {upstream}")

    return {"model_name": pub, "litellm_params": params, "model_info": info}


def _req(method: str, path: str, body: dict | None, key: str, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        PROXY + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:600]}


def existing_models(key: str) -> dict[str, dict]:
    st, j = _req("GET", "/model/info", None, key)
    if st != 200:
        sys.exit(f"GET /model/info -> {st} {str(j)[:300]}")
    out = {}
    for m in j.get("data", []):
        out.setdefault(m.get("model_name", ""), m)
    return out


def master_key() -> str:
    k = os.environ.get("LITELLM_MASTER_KEY")
    if not k:
        sys.exit("LITELLM_MASTER_KEY not set (read it from secret/litellm-secrets)")
    return k


def sub2api_key() -> str:
    k = os.environ.get(API_KEY_ENV)
    if not k:
        sys.exit(f"{API_KEY_ENV} not set (sub2api api_keys.key where name='grok-litellm')")
    return k


def cmd_plan(a) -> int:
    key = master_key()
    have = existing_models(key)
    todo, skip = [], []
    for s in SPEC:
        pub = public_name(s)
        (skip if pub in have else todo).append(s)
    print(f"proxy={PROXY}  spec={len(SPEC)}  to_add={len(todo)}  already_present={len(skip)}")
    print("\n-- would ADD --")
    for s in todo:
        print(f"  {public_name(s):34s} mode={s['mode']:17s} -> openai/{s['name']}")
    print("\n-- already present, left alone --")
    for s in skip:
        pub = public_name(s)
        cur = have[pub].get("litellm_params", {}).get("model")
        note = "  (live cursor fallback target)" if pub == "sa-grok-4.6" else ""
        print(f"  {pub:34s} current model={cur}{note}")
    return 0


def cmd_payload(a) -> int:
    ak = "REDACTED" if a.redact else sub2api_key()
    for s in SPEC:
        if a.only and s["name"] not in a.only:
            continue
        print(json.dumps(build_model_new_payload(s, ak), ensure_ascii=False, indent=1))
    return 0


def cmd_add(a) -> int:
    key = master_key()
    ak = sub2api_key()
    have = existing_models(key)
    todo = [s for s in SPEC if public_name(s) not in have]
    if a.only:
        todo = [s for s in todo if s["name"] in a.only]
    if a.limit:
        todo = todo[: a.limit]

    if not a.apply:
        print(f"DRY-RUN: would POST /model/new for {len(todo)} model(s). "
              f"re-run with --apply to write.")
        for s in todo:
            print(f"  + {public_name(s)}  (mode={s['mode']})")
        return 0

    ok, fail = [], []
    for s in todo:
        pub = public_name(s)
        if pub in PREEXISTING:
            print(f"  ! {pub} is in PREEXISTING, refusing to touch")
            continue
        st, j = _req("POST", "/model/new", build_model_new_payload(s, ak), key)
        if st == 200:
            ok.append(pub)
            print(f"  + {pub:34s} 200")
        else:
            fail.append((pub, st, str(j)[:200]))
            print(f"  x {pub:34s} {st} {str(j)[:200]}")

    print(f"\nadded={len(ok)} failed={len(fail)}")

    # 回读：/model/new 返 200 只证明写了一行，判生效读 /model/info。
    have2 = existing_models(key)
    missing = [p for p in ok if p not in have2]
    print(f"readback: present={len(ok) - len(missing)}/{len(ok)}"
          + (f"  MISSING={missing}" if missing else ""))
    return 1 if fail or missing else 0


def cmd_verify(a) -> int:
    """只读校验：每个 public name 的落点 / mode / 价格是否如预期。"""
    key = master_key()
    have = existing_models(key)
    bad = 0
    for s in SPEC:
        pub = public_name(s)
        m = have.get(pub)
        if not m:
            print(f"  MISSING  {pub}")
            bad += 1
            continue
        lp = m.get("litellm_params", {})
        mi = m.get("model_info", {})
        want_model = f"openai/{s['name']}"
        problems = []
        if lp.get("model") != want_model:
            problems.append(f"model={lp.get('model')} want={want_model}")
        if lp.get("api_base") != SUB2API_BASE:
            problems.append(f"api_base={lp.get('api_base')}")
        if pub not in PREEXISTING and mi.get("mode") != s["mode"]:
            problems.append(f"mode={mi.get('mode')} want={s['mode']}")
        if problems:
            bad += 1
            print(f"  BAD      {pub:34s} " + "; ".join(problems))
        else:
            print(f"  ok       {pub:34s} mode={mi.get('mode')}")
    print(f"\nbad={bad}/{len(SPEC)}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="对比线上已有，列出要加哪些（只读）")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("payload", help="打印 /model/new payload，不发请求")
    p.add_argument("--only", nargs="*", help="只打印这些 upstream 名字")
    p.add_argument("--redact", action="store_true", help="不读 key，打 REDACTED")
    p.set_defaults(fn=cmd_payload)

    p = sub.add_parser("add", help="POST /model/new（默认 dry-run）")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--only", nargs="*")
    p.add_argument("--limit", type=int, help="只做前 N 个（canary）")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("verify", help="只读校验落点/mode/价格")
    p.set_defaults(fn=cmd_verify)

    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
