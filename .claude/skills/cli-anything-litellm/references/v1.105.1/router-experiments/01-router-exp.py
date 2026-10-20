"""Offline router experiment against the exact v1.105.1 source tree (no network).

Every upstream call goes through the real litellm.acompletion with mock_response=<exception>,
so the real failure-logging / cooldown callbacks fire. We count calls per (group, deployment).

Run:
  cd .work/litellm-src-study-1020/src && PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
    PYTHONPATH=. <py3.12> ../deep/01-router-exp.py
"""
import asyncio
import collections
import json
import sys

import litellm
from litellm import Router
from litellm.router_utils.cooldown_handlers import _async_get_cooldown_deployments

litellm.suppress_debug_info = True

CALLS = []
SLEEPS = []
ERROR_FOR_GROUP = {}

_orig_acompletion = litellm.acompletion


async def counting_acompletion(*args, **kwargs):
    meta = kwargs.get("metadata") or {}
    group = meta.get("model_group")
    dep = (kwargs.get("model_info") or {}).get("id", "")[:6]
    CALLS.append((group, dep))
    err = ERROR_FOR_GROUP.get(group)
    if err is not None:
        kwargs["mock_response"] = err()
    else:
        kwargs["mock_response"] = "ok"
    return await _orig_acompletion(*args, **kwargs)


litellm.acompletion = counting_acompletion

_orig_sleep_calc = Router._time_to_sleep_before_retry


def recording_sleep_calc(self, *a, **kw):
    v = _orig_sleep_calc(self, *a, **kw)
    SLEEPS.append(round(v, 2))
    return 0


Router._time_to_sleep_before_retry = recording_sleep_calc


def mk_err(cls, status):
    def f():
        if cls is litellm.BadRequestError:
            return cls(message=f"mock {status}", model="m", llm_provider="openai")
        return cls(message=f"mock {status}", llm_provider="openai", model="m")

    return f


ERRS = {
    "500": mk_err(litellm.InternalServerError, 500),
    "429": mk_err(litellm.RateLimitError, 429),
    "400": mk_err(litellm.BadRequestError, 400),
    "401": mk_err(litellm.AuthenticationError, 401),
    "503": mk_err(litellm.ServiceUnavailableError, 503),
}


def reset_callbacks():
    for name in ("callbacks", "success_callback", "failure_callback", "_async_success_callback", "_async_failure_callback"):
        setattr(litellm, name, [])


def model_list(groups, tag):
    out = []
    for g, n in groups.items():
        for i in range(n):
            out.append(
                {
                    "model_name": g,
                    "litellm_params": {
                        "model": "openai/gpt-x",
                        "api_key": "sk-fake",
                        "api_base": f"http://127.0.0.1:9/{tag}/{g}/{i}",
                    },
                }
            )
    return out


async def run(name, groups, fallbacks, errors, **router_kw):
    reset_callbacks()
    CALLS.clear()
    SLEEPS.clear()
    ERROR_FOR_GROUP.clear()
    ERROR_FOR_GROUP.update({g: ERRS[e] for g, e in errors.items()})
    settings = dict(
        routing_strategy="simple-shuffle",
        num_retries=5,
        timeout=300,
        allowed_fails=3,
        cooldown_time=60,
        retry_after=0,
        enable_weighted_failover=True,
        enable_pre_call_checks=True,
    )
    settings.update(router_kw)
    r = Router(model_list=model_list(groups, name), fallbacks=fallbacks, **settings)
    outcome = "ok"
    try:
        await r.acompletion(model="A", messages=[{"role": "user", "content": "hi"}])
    except Exception as e:  # noqa: BLE001
        outcome = f"{type(e).__name__}({getattr(e, 'status_code', '?')})"
    await asyncio.sleep(0.3)  # let async failure callbacks land
    cd = await _async_get_cooldown_deployments(litellm_router_instance=r, parent_otel_span=None)
    per_group = collections.Counter(g for g, _ in CALLS)
    per_dep = collections.Counter(CALLS)
    print(
        json.dumps(
            {
                "scenario": name,
                "outcome": outcome,
                "total_upstream_calls": len(CALLS),
                "per_group": dict(per_group),
                "max_calls_on_one_deployment": max(per_dep.values()) if per_dep else 0,
                "sequence": [f"{g}:{d}" for g, d in CALLS],
                "computed_sleeps": SLEEPS,
                "cooled_down_after": len(cd),
            },
            ensure_ascii=False,
        )
    )
    sys.stdout.flush()


async def main():
    three = {"A": 3, "B": 2, "C": 2}
    fb = [{"A": ["B", "C"]}]
    await run("500_all_groups_fail", three, fb, {"A": "500", "B": "500", "C": "500"})
    await run("429_all_groups_fail", three, fb, {"A": "429", "B": "429", "C": "429"})
    await run("400_all_groups_fail", three, fb, {"A": "400", "B": "400", "C": "400"})
    await run("401_all_groups_fail", three, fb, {"A": "401", "B": "401", "C": "401"})
    await run("500_A_fails_B_ok", three, fb, {"A": "500"})
    await run(
        "500_with_mgrp_policy_A_ISE1",
        three,
        fb,
        {"A": "500", "B": "500", "C": "500"},
        model_group_retry_policy={"A": {"InternalServerErrorRetries": 1}},
    )
    await run(
        "400_with_mgrp_policy_A_BadReq2",
        three,
        fb,
        {"A": "400", "B": "400", "C": "400"},
        model_group_retry_policy={"A": {"BadRequestErrorRetries": 2}},
    )
    await run("500_weighted_failover_off", three, fb, {"A": "500", "B": "500", "C": "500"}, enable_weighted_failover=False)
    singles = {g: 1 for g in "ABCDEFGHI"}
    await run(
        "500_chain_of_8_single_dep_groups",
        singles,
        [{"A": list("BCDEFGHI")}],
        {g: "500" for g in "ABCDEFGHI"},
    )
    await run("500_single_dep_A_no_fallback", {"A": 1}, None, {"A": "500"})
    await run("429_single_dep_A_no_fallback", {"A": 1}, None, {"A": "429"})


asyncio.run(main())
