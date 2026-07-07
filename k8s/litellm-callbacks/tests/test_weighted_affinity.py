"""
test_weighted_affinity.py — 验证 weighted_affinity hook 的选路逻辑。
在 dev proxy pod 内跑（litellm 1.90.2 环境）。
不依赖真实上游，纯验证 async_filter_deployments 的决策。
"""
import asyncio
import sys
import importlib.util

# 动态加载被测 hook（路径由 argv 传入，默认 /tmp/weighted_affinity.py）
HOOK_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/weighted_affinity.py"
spec = importlib.util.spec_from_file_location("weighted_affinity", HOOK_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 取模块级实例（约定名 proxy_handler_instance）
handler = getattr(mod, "proxy_handler_instance", None)
assert handler is not None, "hook 缺少模块级 proxy_handler_instance"


def make_deployments():
    # 3 台同 model_name=wtest，不同 id + weight 60/30/10
    return [
        {"model_name": "wtest", "model_info": {"id": "wtest-a"}, "litellm_params": {"weight": 60}},
        {"model_name": "wtest", "model_info": {"id": "wtest-b"}, "litellm_params": {"weight": 30}},
        {"model_name": "wtest", "model_info": {"id": "wtest-c"}, "litellm_params": {"weight": 10}},
    ]


def kwargs_for(user_key_hash):
    # 模拟 LiteLLM 传入的 request_kwargs（user_api_key_hash 在 metadata）
    return {"metadata": {"user_api_key_hash": user_key_hash, "deployment_model_name": "wtest"}}


def pick_id(result):
    if not result or len(result) != 1:
        return None
    return result[0]["model_info"]["id"]


async def run():
    # ---- 测试1: MISS 按 weight 分布（1000 个不同 key，各首次请求，统计频率）----
    from collections import Counter
    c = Counter()
    N = 3000
    for i in range(N):
        dps = make_deployments()
        res = await handler.async_filter_deployments(
            model="wtest", healthy_deployments=dps,
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs=kwargs_for(f"{'%064x' % i}"),  # 每个 key 唯一 → 全 MISS
        )
        pid = pick_id(res)
        if pid:
            c[pid] += 1
        # 触发写缓存（内置是 pre_call_deployment_hook 写；这里若 hook 在 filter 里写则已生效）
    total = sum(c.values())
    print(f"[T1 MISS 分布] total={total} (期望≈{N})")
    for k in ("wtest-a", "wtest-b", "wtest-c"):
        pct = 100 * c[k] / total if total else 0
        print(f"   {k}: {c[k]:5d}  {pct:5.1f}%  (理论 {'60' if k=='wtest-a' else '30' if k=='wtest-b' else '10'}%)")
    # 判定：a 应显著 > b > c，且大致 6:3:1
    ok1 = c["wtest-a"] > c["wtest-b"] > c["wtest-c"] and c["wtest-a"] > 0.45 * total
    print(f"   T1 {'PASS' if ok1 else 'FAIL'}")

    # ---- 测试2: HIT 黏同台（同一个 key 连发 10 次，应全同台）----
    key = "%064x" % 999999
    ids = []
    for _ in range(10):
        dps = make_deployments()
        res = await handler.async_filter_deployments(
            model="wtest", healthy_deployments=dps,
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs=kwargs_for(key),
        )
        # 若 hook 靠 pre_call_deployment_hook 写缓存，这里手动调一次模拟 LiteLLM 生命周期
        if hasattr(handler, "async_pre_call_deployment_hook"):
            kw = kwargs_for(key)
            kw["metadata"]["deployment_model_name"] = "wtest"
            # 把选中的 id 塞进 kwargs 模拟 LiteLLM 已选台
            pid = pick_id(res)
            if pid:
                kw["model_info"] = {"id": pid}
                try:
                    await handler.async_pre_call_deployment_hook(kw, None)
                except Exception:
                    pass
        ids.append(pick_id(res))
    uniq = set(x for x in ids if x)
    ok2 = len(uniq) == 1
    print(f"[T2 HIT 黏性] 10 次 ids={ids}")
    print(f"   命中同台={uniq}  T2 {'PASS' if ok2 else 'FAIL'}")

    # ---- 测试3: 无 user_key → 原样返回全部候选 ----
    dps = make_deployments()
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=dps,
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={"metadata": {}},  # 无 user_api_key_hash
    )
    ok3 = isinstance(res, list) and len(res) == 3
    print(f"[T3 无key兜底] 返回台数={len(res)}  T3 {'PASS' if ok3 else 'FAIL'}")

    # ---- 测试4: previous_response_id 让路（返回全部候选，交给内置 responses 续链）----
    dps = make_deployments()
    kw = kwargs_for("%064x" % 424242)
    kw["previous_response_id"] = "resp_abc_deadbeef"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=dps,
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    ok4 = isinstance(res, list) and len(res) == 3
    print(f"[T4 previous_response_id 让路] 返回台数={len(res)} (期望3=不收窄)  T4 {'PASS' if ok4 else 'FAIL'}")

    print(f"\n=== 汇总: T1={'P' if ok1 else 'F'} T2={'P' if ok2 else 'F'} T3={'P' if ok3 else 'F'} T4={'P' if ok4 else 'F'} ===")


asyncio.run(run())
