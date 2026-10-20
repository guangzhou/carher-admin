"""Trace run_async_fallback invocations (depth, list, attempted-set identity) on v1.105.1. Offline."""
import asyncio, collections, importlib.util, json, os, sys
import litellm
import litellm.router as router_mod
import litellm.router_utils.fallback_event_handlers as feh
from litellm import Router

litellm.suppress_debug_info = True
spec = importlib.util.spec_from_file_location("exp1", os.path.join(os.path.dirname(__file__), "01-router-exp.py"))
TRACE = []
orig = feh.run_async_fallback

async def traced(*a, **kw):
    att = kw.get("attempted_targets")
    TRACE.append({"orig": kw.get("original_model_group"), "list": [x if isinstance(x, str) else "WF(" + str(x.get("model")) + ")" for x in kw.get("fallback_model_group") or []],
                  "depth": kw.get("fallback_depth"), "max": kw.get("max_fallbacks"), "attempted_obj": id(att) if att is not None else None})
    return await orig(*a, **kw)

feh.run_async_fallback = traced
router_mod.run_async_fallback = traced
sys.argv = ["x"]
# reuse exp1 helpers but only run selected scenarios
src = open(os.path.join(os.path.dirname(__file__), "01-router-exp.py")).read().split("async def main():")[0]
g = {"__name__": "exp1"}
exec(compile(src, "exp1", "exec"), g)

async def main():
    three = {"A": 3, "B": 2, "C": 2}
    fb = [{"A": ["B", "C"]}]
    for name, errs, extra in [("500_all", "500", {}), ("400_all", "400", {}), ("500_all_wf_off", "500", {"enable_weighted_failover": False})]:
        TRACE.clear()
        await g["run"](name, three, fb, {k: errs for k in "ABC"}, **extra)
        ids = {}
        for t in TRACE:
            t["attempted_obj"] = ids.setdefault(t["attempted_obj"], len(ids)) if t["attempted_obj"] else None
        print(json.dumps({"trace_for": name, "run_async_fallback_calls": TRACE}))
    singles = {x: 1 for x in "ABCDEFGHI"}
    TRACE.clear()
    await g["run"]("chain8", singles, [{"A": list("BCDEFGHI")}], {x: "500" for x in "ABCDEFGHI"})
    print(json.dumps({"trace_for": "chain8", "depths": [t["depth"] for t in TRACE], "max": TRACE[0]["max"] if TRACE else None}))

asyncio.run(main())
