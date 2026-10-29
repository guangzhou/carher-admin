#!/usr/bin/env python3
"""把 OpenRouter 的 jev 注册到 198 生产 LiteLLM，对外名 `jev`（用户 2026-09-28 点名）。

走 `/model/new` 零重启路径（`store_model_in_db: true`），不碰 `cm/litellm-config`、
不 apply、不 restart —— 198 的车道 pod 挂的是派生 CM `litellm-cwfb-<lane>-<hash>`
且 `subPath` 永不热更，patch 基础 CM 对生产零效果。

在 198 本机跑（需要 127.0.0.1:30402 和 sudo kubectl）：

    MK=$(sudo kubectl -n litellm-product get secret litellm-secrets-r2 \
          -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
    LITELLM_MASTER_KEY=$MK python3 litellm-198-add-openrouter-jev.py plan
    LITELLM_MASTER_KEY=$MK python3 litellm-198-add-openrouter-jev.py add --apply
    LITELLM_MASTER_KEY=$MK python3 litellm-198-add-openrouter-jev.py verify

要点（每条都对应一次实测）：

  * **OpenRouter 上 jev 只有一个名：`typesafe/jev-router`**。`typesafe/jev-latest`
    和 `typesafe/jev` 都 404（2026-09-28 查 `/api/v1/models`，458 个名里 1 命中）。
  * **api_key 不写死，用 `os.environ/OPENROUTER_API_KEY`**。198 的 litellm-proxy
    `envFrom` 挂了 `carher-env-keys`（不在 `litellm-secrets-r2`！），容器内已实测
    该 env 存在（len 73）。既有 18 行 db_model 的 openrouter 条目也都是靠 env
    隐式取 key（`api_key` 为 None），这里保持同一形状。
  * **不写 cost 字段，这是刻意的。** OpenRouter 对它的定价是 `prompt/completion:
    "-1"`（动态计价：真实成本取决于 jev 当轮选了哪个上游）。LiteLLM 的
    openrouter transformation 无条件注入 `usage: {"include": true}`，把响应体的
    `usage.cost` 塞进 `_hidden_params.additional_headers`，而
    `response_cost_calculator` 一旦读到它就**直接 return、跳过整个
    `completion_cost`** —— 写死的 per-token 价根本不会被用到，写了只会让
    `/model/info` 显示一个骗人的价。
    ⇒ 连带结论：**全局 `GLOBAL_COST_MULTIPLIER=1.3` overlay 也乘不到它头上**
    （overlay 包在 cost 计算上，而这条路径 return 得更早）。2026-09-28 实测
    8/8 次 `usage.cost` 恒 0。
  * **`mode: chat` 必须显式写。** `/测模型` 和别的量具按 `mode` 决定用哪个协议
    去打，判不出 mode 就跳过报「⚠️ 未测」—— 那和"模型坏了"同形。
  * **`model_info.id` 用 `openrouter/jev`**，跟**对外名**走不跟上游名走：改组名
    时 add→verify→remove 的共存窗口里新旧两行 id 不能撞。
  * master key 只从 env 读。`os.environ.get("K", "<真key>")` 那种兜底默认值
    等于把凭据提交进仓库。

## 🔴 上线前必须让用户知道的两件事（不是脚本能替他决定的）

1. **jev 是路由器不是模型。** 2026-09-28 实测 8/8 次落点全是
   `stealth/space-bunny-alpha`（provider `Stealth`）—— 用户敲 `jev`，实际跑的是
   一个 OpenRouter 替他选的、会随时变的匿名模型。判落点只认响应体的 `model`
   字段，组名和对外名都撒谎。
2. **它对某类 prompt 确定性 403。** "Prove rigorously that every finite group of
   order 255 is cyclic" 这句 3/3 稳定返
   `403 The request is prohibited due to a violation of provider Terms Of Service`，
   换 `max_tokens` 无用、去掉 nonce 无用；而同等长度的另一道难题（红黑树
   再平衡）200 clen=1529，`say PONG` ×8 全 200。
   阳性对照：同一句 prompt 打 `z-ai/glm-5.2` / `minimax/minimax-m3` 都 200
   ⇒ **不是偶发、不是我们的 key、不是 OpenRouter 整体，是 jev 背后那个 stealth
   上游的内容策略。** 这个 403 的原因不在我们这边，也修不了。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROXY = os.environ.get("LITELLM_198", "http://127.0.0.1:30402")

# 对外名（用户在客户端里看到和敲的）。2026-09-28 核过：198 现有 324 个组 /
# 1830 行 model_info 里 `jev` 零碰撞，`openrouter/jev` 这个 id 也没被占。
PUBLIC_NAME = "jev"

# 上游：OpenRouter 唯一存在的 jev 名字。
UPSTREAM = "typesafe/jev-router"

# api_key 走 env 间接引用，值不进仓库、不进 payload。
API_KEY_REF = "os.environ/OPENROUTER_API_KEY"

# OpenRouter 自报 1,000,000（`/api/v1/models` 的 `context_length`）。
# `top_provider.max_completion_tokens` 是 null ⇒ 输出上限未知，不瞎填。
CONTEXT_LENGTH = 1_000_000


def build_model_new_payload() -> dict:
    """纯函数：-> /model/new body。无副作用，可单测。

    刻意不含任何 `*_cost_*` 字段，理由见模块 docstring。
    """
    return {
        "model_name": PUBLIC_NAME,
        "litellm_params": {
            # `openrouter/` 前缀选 LiteLLM 的 openrouter provider，它才会注入
            # `usage:{include:true}` 并回读 `usage.cost`。写成 `openai/...`
            # 会走裸 OpenAI 通路、拿不到动态成本。
            "model": f"openrouter/{UPSTREAM}",
            "api_key": API_KEY_REF,
        },
        "model_info": {
            "id": f"openrouter/{PUBLIC_NAME}",
            "base_model": UPSTREAM,
            # 显式 chat：判不出 mode 的模型会被量具跳过，和"坏了"同形。
            "mode": "chat",
            "max_input_tokens": CONTEXT_LENGTH,
            # 它是路由器，能力取决于当轮落点 ⇒ 一律不声明 supports_*，
            # 声明了就是替一个会变的上游做担保。
        },
    }


def hardcoded_cost(lp: dict, mi: dict) -> dict:
    """`/model/info` 里**真的被写死**的价格字段。纯函数，可单测。

    🔴 别按"有没有 cost 这个 key"判 —— `/model/info` 对每个模型都回一整套 ~75 个
    cost 字段的完整 schema，绝大多数是 `None`。那样量到的是**响应 schema**，
    不是我们写进去的东西，结果恒 BAD（2026-09-28 第一版就是这么坏的）。

    `0` 也要放过：LiteLLM 自己会把 `input_cost_per_token` / `output_cost_per_token`
    填成 0，而 0 恰好是我们想要的形状 —— 不按 token 记账，走 provider 回报的
    `usage.cost`。
    """
    return {k: v for k, v in list(lp.items()) + list(mi.items())
            if "cost" in k and v not in (None, 0)}


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
    out: dict[str, dict] = {}
    for m in j.get("data", []):
        out.setdefault(m.get("model_name", ""), m)
    return out


def master_key() -> str:
    k = os.environ.get("LITELLM_MASTER_KEY")
    if not k:
        sys.exit("LITELLM_MASTER_KEY not set (read it from secret/litellm-secrets-r2)")
    return k


def cmd_plan(a) -> int:
    key = master_key()
    have = existing_models(key)
    cur = have.get(PUBLIC_NAME)
    print(f"proxy={PROXY}  public_name={PUBLIC_NAME}  upstream={UPSTREAM}")
    if cur:
        lp = cur.get("litellm_params", {})
        print(f"ALREADY PRESENT: model={lp.get('model')} "
              f"mode={(cur.get('model_info') or {}).get('mode')} -- add is a no-op")
    else:
        print("would ADD 1 model:")
        print(json.dumps(build_model_new_payload(), ensure_ascii=False, indent=1))
    return 0


def cmd_payload(a) -> int:
    print(json.dumps(build_model_new_payload(), ensure_ascii=False, indent=1))
    return 0


def cmd_add(a) -> int:
    key = master_key()
    have = existing_models(key)
    if PUBLIC_NAME in have:
        print(f"{PUBLIC_NAME} already exists -- refusing to duplicate. "
              f"(delete+re-add is a separate, deliberate operation)")
        return 0
    if not a.apply:
        print(f"DRY-RUN: would POST /model/new for {PUBLIC_NAME!r}. "
              f"re-run with --apply to write.")
        return 0

    st, j = _req("POST", "/model/new", build_model_new_payload(), key)
    print(f"POST /model/new -> {st} {str(j)[:200]}")
    if st != 200:
        return 1

    # 回读：200 只证明写了一行 DB。router 每 ~30s 从 DB 热加载
    # （PROXY_CONFIG_RELOAD_INTERVAL_SECONDS）⇒ 立刻读到 MISSING 是 reload
    # 滞后，不是写失败；隔一会儿再跑 verify。
    have2 = existing_models(key)
    present = PUBLIC_NAME in have2
    print(f"readback: present={present}"
          + ("" if present else "  (router reload lags ~30s; re-run verify shortly)"))
    return 0


def cmd_verify(a) -> int:
    """只读校验：落点 / mode / 没有骗人的 cost 字段。"""
    key = master_key()
    have = existing_models(key)
    m = have.get(PUBLIC_NAME)
    if not m:
        print(f"MISSING  {PUBLIC_NAME}")
        return 1
    lp = m.get("litellm_params", {})
    mi = m.get("model_info", {})
    problems = []
    want = f"openrouter/{UPSTREAM}"
    if lp.get("model") != want:
        problems.append(f"model={lp.get('model')} want={want}")
    if mi.get("mode") != "chat":
        problems.append(f"mode={mi.get('mode')} want=chat")
    # 组名会撒谎，但一个**写死的 cost 字段**也会 —— 它在这条路上永远不被用到。
    #
    # 🔴 尺子的坑（2026-09-28 第一版就栽在这）：`/model/info` 对**每个**模型都回
    # 一整套 ~75 个 cost 字段的完整 schema，绝大多数是 `None`。按"有没有 cost
    # 这个 key"去判，量到的是**响应 schema**而不是我们写进去的东西 ⇒ 恒 BAD。
    # 只认**非 None 且非 0** 的值：LiteLLM 自己会把 input/output 填成 0，
    # 那正是我们要的（0 表示不按 token 记账，走 provider 回报的 usage.cost）。
    real_cost = hardcoded_cost(lp, mi)
    if real_cost:
        problems.append(f"hardcoded cost (pricing is dynamic!): {real_cost}")
    if problems:
        print(f"BAD  {PUBLIC_NAME}  " + "; ".join(problems))
        return 1
    print(f"ok   {PUBLIC_NAME}  mode={mi.get('mode')} -> {lp.get('model')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="对比线上已有（只读）")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("payload", help="打印 /model/new payload，不发请求")
    p.set_defaults(fn=cmd_payload)

    p = sub.add_parser("add", help="注册（默认 dry-run，要 --apply 才写）")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("verify", help="只读校验落点 / mode / cost 字段")
    p.set_defaults(fn=cmd_verify)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
