"""budget_notice 文案 demo：走真实 hook（② 流式注入 / ④ 系列拦截 / ③ 总额拦截 / ① cye），
只把 litellm 运行时换成单测的桩。一把 cursor key 从早到晚走完一天（8 个场景）。

用法（本机，不碰集群）：
    python3 scripts/budget-notice/demo.py > /tmp/x.txt     # 或直接看 stdout

为什么有它（2026-10-20）：文案改了 6 轮，每轮都要先给用户看「真用户会看到什么」。
fixtures 是 198 只读拉的一把真 cursor key（白名单 46 / aliases 34）+ 这些名字在
ProxyModelTable 里的 mode/上限 —— 🔴 别换成手写假 key：没有 aliases 时产品名反查落空，
`kiro-claude-opus-5-5` 这种落点名会漏到表里，用户第一眼就当成 bug（10-20 实际发生过）。
花费金额是编的；SpendLogs 行按落库真实形态写成落点名（kiro-*/chatgpt-*）。

改了 budget_notice 的函数签名（比如 query_raw 多一个参数）这里的桩也要跟着改，
否则表格那段会退化成「查询暂不可用」—— 那是 demo 坏了，不是功能坏了。
"""
import asyncio
import os
import sys
import types
import logging

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../k8s/litellm-callbacks/tests"))
FIXTURES = os.path.join(HERE, "fixtures")
logging.disable(logging.CRITICAL)

import test_budget_notice as T  # noqa: E402  复用测试里的 litellm 桩

M = T.M
ENV = {"BUDGET_NOTICE_KEY_PREFIXES": "claude-code-,cursor-", "BUDGET_FAMILY_ENABLED": "1",
       "BUDGET_NOTICE_PROBE_ENABLED": "1"}
for k in ("BUDGET_FAMILY_PREMIUM_KEY_PREFIXES", "BUDGET_NOTICE_WARN_RATIO",
          "BUDGET_NOTICE_WARN_RATIOS"):
    os.environ.pop(k, None)
os.environ.update(ENV)

# ---- 198 真数据（10-20 只读拉取）：cursor-liuguoxian-std 的白名单 46 个 + aliases 34 条，
#      以及这些名字/落点在 LiteLLM_ProxyModelTable 里的 mode。kiro-* 落点在 config.yaml、
#      不在 DB 表里 ⇒ 这里没有它的行（mode 判不出 = 按聊天模型算，和生产同一条规则）。
import json
FIX = json.load(open(os.path.join(FIXTURES, "cursor-key-allowlist.json")))
ROWS = json.load(open(os.path.join(FIXTURES, "deployment-modes.json")))
ps = types.ModuleType("litellm.proxy.proxy_server")
sys.modules["litellm.proxy.proxy_server"] = ps
ps.llm_router = types.SimpleNamespace(model_group_alias={}, model_list=[
    {"model_name": r["model_name"],
     "model_info": {k: v for k, v in (("mode", r["mode"]),
                                       ("max_input_tokens", r["max_input_tokens"]),
                                       ("max_output_tokens", r["max_output_tokens"]))
                    if v is not None},
     "litellm_params": {"model": "openai/" + r["model_name"]}} for r in ROWS])

# ---- 假 SpendLogs：今日按模型花费，随剧情推进改 ----
TODAY = []


class DB:
    async def query_raw(self, q, v, *args):
        total = sum(r[1] for r in TODAY)
        return [{"g": g, "calls": c, "n": c, "cost": cost, "total_cost": total,
                 "p50": p50, "p90": p90} for g, cost, c, p50, p90 in TODAY]


ps.prisma_client = types.SimpleNamespace(db=DB())

TOKEN = "demo-token"


def key(spend):
    k = T._Key(alias="cursor-liuguoxian-std", spend=spend, max_budget=1000.0,
               token=TOKEN,
               reset_at=__import__("datetime").datetime(2026, 10, 20, 16, 0))
    k.models = list(FIX["models"])
    k.aliases = dict(FIX["aliases"])
    k.metadata = {}
    return k


def set_day(premium, rows):
    M._fam_local[M._family_spend_key("premium", TOKEN)] = premium
    TODAY[:] = sorted(rows, key=lambda r: -r[1])   # 真 SQL 按花费降序
    M._TTFT_CACHE.clear()
    M._TODAY_USED.clear()


async def stream(k, model):
    items = [T._Chunk("（模型的正常回答……）"), T._Chunk(None, finish="stop")]
    out = []
    async for x in M.budget_notice.async_post_call_streaming_iterator_hook(
            k, T._agen(items), {"model": model}):
        out.append(x)
    return "".join(x.choices[0].delta.content or "" for x in out)


async def pre(k, model, text="帮我重构这个函数"):
    d = {"model": model, "messages": [{"role": "user", "content": text}]}
    out = await M.budget_notice.async_pre_call_hook(k, None, d, T._CallType("acompletion"))
    return out.get("mock_response") or "（放行，正常打上游）"


def show(title, body):
    print("\n" + "=" * 78 + f"\n▶ {title}\n" + "=" * 78)
    print(body.strip("\n"))


async def main():
    # 10:00 —— 刚开始用
    set_day(120.0, [("kiro-claude-opus-5-5", 120.0, 8, 1.31, 3.02),
                    ("chatgpt-gpt-6-sol", 30.0, 22, 0.74, 1.40)])
    show("场景0  10:00 总额15%、高配24% —— 正常回答，不提醒",
         await stream(key(150.0), "kiro-claude-opus-5-5"))

    # 13:30 —— 高配桶 82%（$410/$500），总额 52%
    set_day(410.0, [("kiro-claude-opus-5-5", 352.0, 31, 1.28, 3.40),
                    ("gpt-6-astra", 58.0, 6, 2.10, 4.85),
                    ("chatgpt-gpt-6-sol", 96.0, 70, 0.71, 1.38),
                    ("deepseek-v4.1-flash", 14.0, 41, 0.52, 0.96)])
    show("场景1  13:30 用 Opus，高配桶跨 80% —— 第一次提醒（总额 52% 陪同展示）",
         await stream(key(520.0), "kiro-claude-opus-5-5"))
    show("场景1b 13:31 紧接着再打一发 Opus —— 80% 档今天已提醒过，不再打扰",
         await stream(key(525.0), "kiro-claude-opus-5-5"))

    # 15:10 —— 总额跨 80%（$812），高配 92%
    set_day(462.0, [("kiro-claude-opus-5-5", 391.0, 35, 1.30, 3.38),
                    ("chatgpt-gpt-6-sol", 230.0, 160, 0.70, 1.41),
                    ("gpt-6-astra", 71.0, 7, 2.07, 4.90),
                    ("gpt-5.5", 96.0, 58, 0.88, 1.92),
                    ("deepseek-v4.1-flash", 24.0, 70, 0.51, 0.97)])
    show("场景2  15:10 用 gpt-6-sol，总额跨 80% —— 总额提醒（高配桶不在本次模型上，不读）",
         await stream(key(812.0), "gpt-6-sol"))

    # 15:40 —— 高配跨 95%
    set_day(481.0, [("kiro-claude-opus-5-5", 405.0, 37, 1.29, 3.35),
                    ("chatgpt-gpt-6-sol", 236.0, 165, 0.70, 1.40),
                    ("gpt-6-astra", 76.0, 8, 2.05, 4.88),
                    ("gpt-5.5", 98.0, 60, 0.87, 1.90),
                    ("deepseek-v4.1-flash", 25.0, 73, 0.51, 0.97)])
    show("场景3  15:40 用 gpt-6-astra，高配跨 95% —— 第二档提醒",
         await stream(key(840.0), "gpt-6-astra"))

    # 16:20 —— 高配用完
    set_day(503.0, [("kiro-claude-opus-5-5", 425.0, 39, 1.29, 3.36),
                    ("chatgpt-gpt-6-sol", 240.0, 168, 0.70, 1.40),
                    ("gpt-6-astra", 78.0, 8, 2.05, 4.88),
                    ("gpt-5.5", 99.0, 61, 0.87, 1.90),
                    ("deepseek-v4.1-flash", 26.0, 75, 0.51, 0.97)])
    show("场景4  16:20 再发 Opus，高配 $503/$500 用完 —— 拦截（不打上游、不计费）",
         await pre(key(868.0), "kiro-claude-opus-5-5"))
    show("场景4b 16:21 照提示切到 claude-sonnet-5-5 —— 放行",
         await pre(key(868.0), "claude-sonnet-5-5"))

    # 18:00 —— 总额跨 95%
    set_day(503.0, [("kiro-claude-opus-5-5", 425.0, 39, 1.29, 3.36),
                    ("chatgpt-gpt-6-sol", 262.0, 181, 0.70, 1.41),
                    ("kiro-claude-sonnet-5-5", 68.0, 31, 0.95, 2.20),
                    ("gpt-6-astra", 78.0, 8, 2.05, 4.88),
                    ("gpt-5.5", 101.0, 62, 0.87, 1.90),
                    ("deepseek-v4.1-flash", 27.0, 77, 0.51, 0.97)])
    show("场景5  18:00 用 sonnet，总额跨 95% —— 总额第二档提醒",
         await stream(key(961.0), "claude-sonnet-5-5"))

    show("场景6  18:05 用户单独发一行 cye —— 完整明细（零上游、零计费）",
         await pre(key(965.0), "claude-sonnet-5-5", text="cye"))

    # 19:30 —— 总额用完：patch 层打 mark，pre_call 出友好文案
    M._mark_over_budget(TOKEN, 1004.6, 1000.0)
    show("场景7  19:30 总额 $1004.60/$1000 用完 —— 所有模型都拦（不附提额联系人）",
         await pre(key(1004.6), "gpt-5.5"))


asyncio.run(main())
