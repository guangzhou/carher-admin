"""Second offline experiment: our live patches loaded on top of v1.105.1, plus edge cases.

1. Import live-cb dead_deployment_retry / encrypted_content_degrade_strip / chatgpt_responses_normalize /
   streaming_output_backfill / weighted_affinity and report whether each install path took effect.
2. Alias: does model_group_retry_policy keyed by an alias apply?
3. All deployments of A cooled -> what error / does fallback still run?
4. 'There are no healthy deployments' 400 with dead_deployment_retry -> attempts.
"""
import asyncio
import collections
import json
import os
import sys

os.environ.setdefault("DEAD_ARM_COOLDOWN_HOOK", "1")
os.environ.setdefault("DEAD_ARM_AUTO_DELETE_DISABLED", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live-cb"))

import litellm  # noqa: E402
from litellm import Router  # noqa: E402
from litellm.router_utils import cooldown_handlers  # noqa: E402
import litellm.router_utils.fallback_event_handlers as feh  # noqa: E402

litellm.suppress_debug_info = True
report = {}

orig_should_retry = Router.should_retry_this_error
orig_is_cd = cooldown_handlers._is_cooldown_required
orig_raf = feh.run_async_fallback
orig_pick = Router.async_get_available_deployment
orig_helper = Router._ageneric_api_call_with_fallbacks_helper

import dead_deployment_retry  # noqa: E402,F401
import encrypted_content_degrade_strip  # noqa: E402,F401
import chatgpt_responses_normalize  # noqa: E402,F401
import streaming_output_backfill  # noqa: E402,F401
import weighted_affinity  # noqa: E402,F401
import litellm.router as router_mod  # noqa: E402

report["dead_deployment_retry.should_retry_patched"] = Router.should_retry_this_error is not orig_should_retry
report["dead_deployment_retry.is_cooldown_required_patched"] = cooldown_handlers._is_cooldown_required is not orig_is_cd
report["chatgpt_guard.fallback_mod_patched"] = feh.run_async_fallback is not orig_raf
report["chatgpt_guard.router_mod_patched"] = router_mod.run_async_fallback is not orig_raf
report["stamp.pick_patched"] = Router.async_get_available_deployment is not orig_pick
report["stamp.helper_patched"] = Router._ageneric_api_call_with_fallbacks_helper is not orig_helper
print(json.dumps({"install": report}))

CALLS = []
ERROR_FOR_GROUP = {}
_orig_acompletion = litellm.acompletion


async def counting_acompletion(*args, **kwargs):
    # group = real deployment group, taken from api_base (metadata.model_group holds the alias)
    group = str(kwargs.get("api_base") or "").rstrip("/").split("/")[-2]
    dep = (kwargs.get("model_info") or {}).get("id", "")[:6]
    CALLS.append((group, dep))
    err = ERROR_FOR_GROUP.get(group)
    kwargs["mock_response"] = err() if err is not None else "ok"
    return await _orig_acompletion(*args, **kwargs)


litellm.acompletion = counting_acompletion
Router._time_to_sleep_before_retry = lambda self, *a, **kw: 0


def dead400():
    return litellm.BadRequestError(
        message="You passed in model=leaf. There are no healthy deployments for this model",
        model="m",
        llm_provider="openai",
    )


def ise():
    return litellm.InternalServerError(message="mock 500", llm_provider="openai", model="m")


def ml(groups):
    return [
        {
            "model_name": g,
            "litellm_params": {"model": "openai/gpt-x", "api_key": "sk-fake", "api_base": f"http://127.0.0.1:9/{g}/{i}"},
        }
        for g, n in groups.items()
        for i in range(n)
    ]


def reset():
    for name in ("callbacks", "success_callback", "failure_callback", "_async_success_callback", "_async_failure_callback"):
        setattr(litellm, name, [])
    CALLS.clear()
    ERROR_FOR_GROUP.clear()


async def run(name, groups, fallbacks, errors, req_model="A", pre=None, **kw):
    reset()
    ERROR_FOR_GROUP.update(errors)
    s = dict(routing_strategy="simple-shuffle", num_retries=5, allowed_fails=3, cooldown_time=60, retry_after=0,
             enable_weighted_failover=True, enable_pre_call_checks=True)
    s.update(kw)
    r = Router(model_list=ml(groups), fallbacks=fallbacks, **s)
    if pre:
        await pre(r)
    outcome = "ok"
    try:
        await r.acompletion(model=req_model, messages=[{"role": "user", "content": "hi"}])
    except Exception as e:  # noqa: BLE001
        outcome = f"{type(e).__name__}({getattr(e, 'status_code', '?')}): {str(e)[:140]}"
    await asyncio.sleep(0.3)
    print(json.dumps({"scenario": name, "outcome": outcome, "total": len(CALLS),
                      "per_group": dict(collections.Counter(g for g, _ in CALLS))}, ensure_ascii=False))
    sys.stdout.flush()


async def cool_all_A(r):
    for d in r.get_model_list(model_name="A"):
        r.cooldown_cache.add_deployment_to_cooldown(
            model_id=d["model_info"]["id"], original_exception=ise(), exception_status=500, cooldown_time=60)


async def main():
    three = {"A": 3, "B": 2}
    fb = [{"A": ["B"]}]
    await run("dead400_patched_A_dead_B_ok", three, fb, {"A": dead400})
    await run("dead400_patched_all_dead", three, fb, {"A": dead400, "B": dead400})
    await run("alias_policy_keyed_by_alias", three, fb, {"A": ise, "B": ise}, req_model="alias-a",
              model_group_alias={"alias-a": "A"},
              model_group_retry_policy={"alias-a": {"InternalServerErrorRetries": 0}})
    await run("alias_policy_keyed_by_real_group", three, fb, {"A": ise, "B": ise}, req_model="alias-a",
              model_group_alias={"alias-a": "A"},
              model_group_retry_policy={"A": {"InternalServerErrorRetries": 0}})
    await run("all_A_cooled_then_request", three, fb, {}, pre=cool_all_A)
    await run("all_A_cooled_no_fallback", three, None, {}, pre=cool_all_A)


asyncio.run(main())
