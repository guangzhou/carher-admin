"""
复现 198 litellm-product 2026-08-10 05:11:27 中流 failover 竞态 + 跨组兜底断链。

场景（与生产事故一一对应）：
  RACE-LOST : 亲和 pin 钉在 acct-0，acct-0 流中途报 capacity（MidStreamFallbackError，
              cooldown 未登记 —— 复现里 fake 流不走 failure_handler，天然等价于
              "cooldown 还没写完"的输局时序）。期望看到：
              同组重挑 → WA HIT 又端回 acct-0 → 被 _excluded 过滤清空 →
              RouterRateLimitError("No deployments available") → 且 luna 兜底不点火。
  RACE-WON  : 同上，但先把 acct-0 打入 cooldown（赢局时序）。期望：WA re-pick acct-1 成功。
  ALL-SOL-DEAD: 全部 sol 账号中流失败 → 检验跨组兜底（luna）是否点火。

用法：在 midstream-race-repro pod 里
  python3 /repro/repro.py race-lost|race-won|all-sol-dead
"""

import asyncio
import logging
import sys

sys.path.insert(0, "/repro")

import litellm
from litellm._logging import verbose_router_logger

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
verbose_router_logger.setLevel(logging.INFO)

SOL = "chatgpt-gpt-5.6-sol"
LUNA = "chatgpt-gpt-5.6-luna"
USER_HASH = "e08d" * 16  # 64 hex，模拟 user_api_key_hash

CALLS = []  # (dep_id, outcome) 上游真实被调用的台账
BEHAVIOR = {}  # dep_id -> "fail" | "ok"


# ---------------------------------------------------------------------------
# fake 上游：返回 BaseResponsesAPIStreamingIterator 子类，迭代期才失败（=中流失败）
# ---------------------------------------------------------------------------
from litellm.responses.streaming_iterator import BaseResponsesAPIStreamingIterator


class _FakeStream(BaseResponsesAPIStreamingIterator):
    def __init__(self, dep_id: str, model: str, mode: str):
        # 刻意不调 super().__init__（它需要真实 http response）；只挂需要的属性
        self.dep_id = dep_id
        self.model = model
        self.mode = mode
        self.custom_llm_provider = "openai"
        self.litellm_metadata = {"model_info": {"id": dep_id}}
        self._hidden_params = {"model_id": dep_id}
        self.sent_first_chunk = False
        self.completed_response = None
        self._done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        from litellm.exceptions import MidStreamFallbackError, RateLimitError

        if self.mode == "fail":
            CALLS.append((self.dep_id, "MIDSTREAM-FAIL"))
            rl = RateLimitError(
                message="RateLimitError: (transient capacity, stream) - Our servers are currently overloaded. Please try again later.",
                model=self.model,
                llm_provider="openai",
            )
            exc = MidStreamFallbackError(
                message="MidStreamFallbackError: (transient capacity, stream) - Our servers are currently overloaded. Please try again later.",
                model=self.model,
                llm_provider="openai",
                original_exception=rl,
                generated_content="",
                is_pre_first_chunk=True,
            )
            exc.failed_deployment_id = self.dep_id  # 与 streaming_output_backfill 一致
            raise exc
        if self._done:
            raise StopAsyncIteration
        self._done = True
        CALLS.append((self.dep_id, "OK"))
        return {"type": "response.completed", "response": {"id": "resp_fake", "status": "completed", "served_by": self.dep_id}}

    async def aclose(self):
        return None


async def fake_aresponses(**kwargs):
    api_base = str(kwargs.get("api_base") or "")
    dep_id = api_base.rsplit("/", 1)[-1] or "unknown"
    mode = BEHAVIOR.get(dep_id, "ok")
    return _FakeStream(dep_id=dep_id, model=str(kwargs.get("model")), mode=mode)


litellm.aresponses = fake_aresponses  # 必须在 Router() 之前


# ---------------------------------------------------------------------------
# 挂 weighted_affinity（与生产同文件）
# ---------------------------------------------------------------------------
import streaming_output_backfill  # noqa: E402  # 生产同款 router 补丁
import midstream_fallback_loop  # noqa: E402  # 修复#2：中流 failover 循环
import weighted_affinity  # noqa: E402

WA = weighted_affinity.proxy_handler_instance
litellm.callbacks = [WA]

from litellm.router import Router  # noqa: E402


def build_router():
    model_list = []
    for i in range(3):
        model_list.append(
            {
                "model_name": SOL,
                "litellm_params": {
                    "model": f"openai/{SOL}",
                    "api_key": "sk-fake",
                    "api_base": f"http://127.0.0.1:1/acct-{i}-sol",
                    "weight": 1,
                },
                "model_info": {"id": f"acct-{i}-sol"},
            }
        )
    model_list.append(
        {
            "model_name": LUNA,
            "litellm_params": {
                "model": f"openai/{LUNA}",
                "api_key": "sk-fake",
                "api_base": "http://127.0.0.1:1/acct-luna",
                "weight": 1,
            },
            "model_info": {"id": "acct-luna"},
        }
    )
    return Router(
        model_list=model_list,
        routing_strategy="simple-shuffle",
        num_retries=2,
        allowed_fails=3,
        cooldown_time=60,
        enable_weighted_failover=True,
        fallbacks=[{"gpt-5.6-sol": [LUNA]}],
        enable_pre_call_checks=True,
        optional_pre_call_checks=["responses_api_deployment_check", "prompt_caching"],
        model_group_alias={"gpt-5.6-sol": SOL},
    )


async def seed_pin(router, dep_id: str):
    """把亲和 pin 预置到 dep_id（等价生产的 sticky 缓存命中态）。"""
    # model_map_key 必须与 WA 运行时推导一致 —— 用 WA 自己的函数推
    deployments = [
        {"model_name": SOL, "litellm_params": {"model": f"openai/{SOL}"}, "model_info": {"id": f"acct-{i}-sol"}}
        for i in range(3)
    ]
    mk = WA._get_stable_model_map_key_from_deployments(deployments)
    key = WA.get_affinity_cache_key(mk, USER_HASH)
    await WA.cache.async_set_cache(key, {"model_id": dep_id}, ttl=300)
    print(f"[seed] pinned {dep_id} @ cache_key={key}")


async def put_in_cooldown(router, dep_id: str):
    from litellm.router_utils.cooldown_handlers import _set_cooldown_deployments

    ok = _set_cooldown_deployments(
        litellm_router_instance=router,
        original_exception=Exception("repro manual cooldown"),
        exception_status=429,
        deployment=dep_id,
        time_to_cooldown=60,
    )
    print(f"[seed] cooldown {dep_id} -> {ok}")


async def drive(router):
    CALLS.clear()
    outcome = None
    try:
        resp = await router.aresponses(
            model="gpt-5.6-sol",
            input="hi",
            stream=True,
            litellm_metadata={"user_api_key_hash": USER_HASH},
        )
        items = []
        async for item in resp:
            items.append(item)
        outcome = ("SUCCESS", items[-1] if items else None)
    except Exception as e:
        outcome = ("CLIENT-ERROR", f"{type(e).__name__}: {str(e)[:180]}")
    return outcome


async def main():
    scenario = sys.argv[1] if len(sys.argv) > 1 else "race-lost"
    router = build_router()

    if scenario == "race-lost":
        BEHAVIOR.update({"acct-0-sol": "fail", "acct-1-sol": "ok", "acct-2-sol": "ok", "acct-luna": "ok"})
        await seed_pin(router, "acct-0-sol")
    elif scenario == "race-won":
        BEHAVIOR.update({"acct-0-sol": "fail", "acct-1-sol": "ok", "acct-2-sol": "ok", "acct-luna": "ok"})
        await seed_pin(router, "acct-0-sol")
        await put_in_cooldown(router, "acct-0-sol")
    elif scenario == "happy":
        BEHAVIOR.update({"acct-0-sol": "ok", "acct-1-sol": "ok", "acct-2-sol": "ok", "acct-luna": "ok"})
    elif scenario == "all-sol-dead":
        BEHAVIOR.update({"acct-0-sol": "fail", "acct-1-sol": "fail", "acct-2-sol": "fail", "acct-luna": "ok"})
        await seed_pin(router, "acct-0-sol")
    else:
        raise SystemExit(f"unknown scenario {scenario}")

    outcome = await drive(router)

    print("\n========== RESULT ==========")
    print(f"scenario     : {scenario}")
    print(f"client sees  : {outcome}")
    print(f"upstream log : {CALLS}")
    luna_called = any(d == "acct-luna" for d, _ in CALLS)
    same_group_recovered = any(d.endswith("-sol") and o == "OK" for d, o in CALLS)
    print(f"same-group recovered: {same_group_recovered}")
    print(f"luna fallback fired : {luna_called}")


if __name__ == "__main__":
    asyncio.run(main())
