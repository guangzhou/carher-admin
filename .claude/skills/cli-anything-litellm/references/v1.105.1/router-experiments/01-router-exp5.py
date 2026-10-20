"""Alias fallback lookup + context_window_fallbacks gating. Offline."""
import asyncio, collections, json
import litellm
from litellm import Router
litellm.suppress_debug_info = True
CALLS = []; ERR = {}
_o = litellm.acompletion
async def fake(*a, **kw):
    g = str(kw.get("api_base")).rstrip("/").split("/")[-2]
    CALLS.append(g)
    f = ERR.get(g)
    kw["mock_response"] = f() if f else "ok"
    return await _o(*a, **kw)
litellm.acompletion = fake
Router._time_to_sleep_before_retry = lambda self, *a, **k: 0
ise = lambda: litellm.InternalServerError(message="mock 500", llm_provider="openai", model="m")
cwe = lambda: litellm.ContextWindowExceededError(message="mock ctx", model="m", llm_provider="openai")
def ml(gs): return [{"model_name": g, "litellm_params": {"model": "openai/gpt-x", "api_key": "sk", "api_base": f"http://127.0.0.1:9/{g}/{i}"}} for g, n in gs.items() for i in range(n)]
async def run(name, req, err, **kw):
    for n in ("callbacks","success_callback","failure_callback","_async_success_callback","_async_failure_callback"): setattr(litellm, n, [])
    CALLS.clear(); ERR.clear(); ERR.update(err)
    s = dict(routing_strategy="simple-shuffle", num_retries=5, allowed_fails=3, cooldown_time=60, retry_after=0, enable_weighted_failover=True, enable_pre_call_checks=True); s.update(kw)
    r = Router(model_list=ml({"A": 2, "B": 2, "X": 2}), **s)
    out = "ok"
    try: await r.acompletion(model=req, messages=[{"role": "user", "content": "hi"}])
    except Exception as e: out = f"{type(e).__name__}: " + str(e).split("LiteLLM:")[-1][:260].replace("\n", " ")
    print(json.dumps({"scenario": name, "outcome": out, "calls": dict(collections.Counter(CALLS))}))
async def main():
    await run("alias_req_fallbacks_keyed_by_real_group", "alias-a", {"A": ise}, model_group_alias={"alias-a": "A"}, fallbacks=[{"A": ["B"]}])
    await run("alias_req_fallbacks_keyed_by_alias", "alias-a", {"A": ise}, model_group_alias={"alias-a": "A"}, fallbacks=[{"alias-a": ["B"]}])
    await run("ctxwin_group_without_cw_entry_but_has_regular_fb", "A", {"A": cwe}, fallbacks=[{"A": ["B"]}], context_window_fallbacks=[{"X": ["B"]}])
    await run("ctxwin_no_cw_fallbacks_at_all", "A", {"A": cwe}, fallbacks=[{"A": ["B"]}])
    await run("ctxwin_with_cw_entry", "A", {"A": cwe}, fallbacks=[{"A": ["X"]}], context_window_fallbacks=[{"A": ["B"]}])
asyncio.run(main())
