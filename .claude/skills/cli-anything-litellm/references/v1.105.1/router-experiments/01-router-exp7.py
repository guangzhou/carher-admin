"""Prod-sized groups: A=40 deployments, fallbacks to B=20, C=10. Offline mock."""
import asyncio, os
src = open(os.path.join(os.path.dirname(__file__), "01-router-exp.py")).read().split("async def main():")[0]
g = {"__name__": "exp1"}; exec(compile(src, "exp1", "exec"), g)
async def main():
    big = {"A": 40, "B": 20, "C": 10}
    fb = [{"A": ["B", "C"]}]
    for e in ("500", "400", "429", "503"):
        await g["run"](f"big_{e}_all_fail", big, fb, {k: e for k in "ABC"})
    await g["run"]("big_500_A_fail_only", big, fb, {"A": "500"})
    await g["run"]("big_500_all_fail_max_fallbacks_2", big, fb, {k: "500" for k in "ABC"}, max_fallbacks=2)
asyncio.run(main())
