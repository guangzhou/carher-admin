"""weighted_affinity + v1.105 retry-skip interaction. Offline, mocked upstream.

Pin a session to deployment X; X returns 400 (non-retryable) or 500. Count attempts and
which deployment each attempt lands on. Also: does WA's pin survive (detour vs repin)?
"""
import asyncio, collections, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live-cb"))
import litellm
from litellm import Router
import weighted_affinity as wa_mod

litellm.suppress_debug_info = True
CALLS = []
BAD = {}
_orig = litellm.acompletion

async def fake(*a, **kw):
    mid = (kw.get("model_info") or {}).get("id")
    CALLS.append(mid)
    err = BAD.get(mid)
    kw["mock_response"] = err() if err else "ok"
    return await _orig(*a, **kw)

litellm.acompletion = fake
Router._time_to_sleep_before_retry = lambda self, *a, **kw: 0

def e400():
    return litellm.BadRequestError(message="mock 400 tool_choice invalid", model="m", llm_provider="openai")
def e500():
    return litellm.InternalServerError(message="mock 500", llm_provider="openai", model="m")
def dead400():
    return litellm.BadRequestError(message="You passed in model=leaf. There are no healthy deployments for this model", model="m", llm_provider="openai")

async def scenario(name, errfn, patch_dead=False):
    for n in ("callbacks", "success_callback", "failure_callback", "_async_success_callback", "_async_failure_callback"):
        setattr(litellm, n, [])
    CALLS.clear(); BAD.clear()
    wa = wa_mod.WeightedAffinityRouter(cache=wa_mod._InMemoryTTLCache())
    litellm.callbacks = [wa]
    ml = [{"model_name": "A", "litellm_params": {"model": "openai/gpt-x", "api_key": "sk", "api_base": f"http://127.0.0.1:9/A/{i}"}} for i in range(4)]
    r = Router(model_list=ml, routing_strategy="simple-shuffle", num_retries=5, allowed_fails=3, cooldown_time=60,
               retry_after=0, enable_weighted_failover=True, enable_pre_call_checks=True)
    ids = [d["model_info"]["id"] for d in r.get_model_list(model_name="A")]
    pinned = ids[0]
    key = wa.get_affinity_cache_key("A", "a" * 64, "sess1")
    await wa.cache.async_set_cache(key, {"model_id": pinned}, ttl=600)
    BAD[pinned] = errfn
    out = "ok"
    try:
        await r.acompletion(model="A", messages=[{"role": "user", "content": "hi"}],
                            metadata={"user_api_key_hash": "a" * 64, "session_id": "sess1"})
    except Exception as ex:  # noqa: BLE001
        out = f"{type(ex).__name__}({getattr(ex,'status_code','?')})"
    await asyncio.sleep(0.3)
    seq = ["PIN" if c == pinned else "other" for c in CALLS]
    pin_after = await wa.cache.async_get_cache(key=key)
    print(json.dumps({"scenario": name, "outcome": out, "attempts": len(CALLS), "sequence": seq,
                      "pin_still_points_to_failed": (pin_after or {}).get("model_id") == pinned}))

async def main():
    await scenario("pinned_returns_500", e500)
    await scenario("pinned_returns_plain_400", e400)
    import dead_deployment_retry  # noqa: F401  installs patch (cooldown hook per env)
    await scenario("pinned_returns_dead400_with_patch", dead400)

asyncio.run(main())
