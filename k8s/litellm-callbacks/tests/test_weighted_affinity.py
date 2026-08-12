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

    # ---- 测试5: 候选里有 tag_regex 时让路（按 UA 分流的组，本 hook 不参与）----
    # 2026-08-07 实测：本 hook 跑在 tag 路由之前，钉成 1 台会让 UA 分流失效
    # （Desktop 落 zerokey 8 发中 5 发），反向还会把候选清空触发
    # no_deployments_with_tag_routing。故这种组必须整组让路。
    dps = make_deployments()
    dps[0]["litellm_params"]["tag_regex"] = ["^User-Agent: Codex Desktop/"]
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=dps,
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs_for("%064x" % 515151),
    )
    ok5 = isinstance(res, list) and len(res) == 3
    print(f"[T5 tag_regex 让路] 返回台数={len(res)} (期望3=不收窄)  T5 {'PASS' if ok5 else 'FAIL'}")

    # ---- 测试6: 没有 tag_regex 的组不受影响（仍然钉单台）----
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs_for("%064x" % 616161),
    )
    ok6 = isinstance(res, list) and len(res) == 1
    print(f"[T6 无 tag_regex 照旧] 返回台数={len(res)} (期望1=收窄)  T6 {'PASS' if ok6 else 'FAIL'}")

    # ---- 测试7: session 黏性（同 user + 同 prompt_cache_key 连发 10 次全同台）----
    key7 = "%064x" % 700700
    ids7 = []
    for _ in range(10):
        kw = kwargs_for(key7)
        kw["prompt_cache_key"] = "sess-uuid-alpha"
        res = await handler.async_filter_deployments(
            model="wtest", healthy_deployments=make_deployments(),
            messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
        )
        ids7.append(pick_id(res))
    ok7 = len(set(x for x in ids7 if x)) == 1 and all(ids7)
    print(f"[T7 session 黏性] 10 次 ids={ids7}  T7 {'PASS' if ok7 else 'FAIL'}")

    # ---- 测试8: 不同 session 独立选台（40 个 session ≥2 台；已有 session pin 不被扰动）----
    key8 = "%064x" % 800800
    first_pins = {}
    for i in range(40):
        kw = kwargs_for(key8)
        kw["prompt_cache_key"] = f"sess-{i}"
        res = await handler.async_filter_deployments(
            model="wtest", healthy_deployments=make_deployments(),
            messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
        )
        first_pins[f"sess-{i}"] = pick_id(res)
    spread = set(first_pins.values())
    # 重放 sess-0..4，pin 不因其它 session 改变
    stable = True
    for i in range(5):
        kw = kwargs_for(key8)
        kw["prompt_cache_key"] = f"sess-{i}"
        res = await handler.async_filter_deployments(
            model="wtest", healthy_deployments=make_deployments(),
            messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
        )
        if pick_id(res) != first_pins[f"sess-{i}"]:
            stable = False
    ok8 = len(spread) >= 2 and stable
    print(f"[T8 多 session 分散+互不扰动] 40 session 落 {len(spread)} 台, 重放稳定={stable}  T8 {'PASS' if ok8 else 'FAIL'}")

    # ---- 测试9: 滑动续期（ttl=1s：0.7s 间隔连打 3 次超过原始 TTL 仍 HIT 同台；静默 1.3s 后过期）----
    import time as _time
    h9 = mod.WeightedAffinityRouter(ttl_seconds=1)
    key9 = "%064x" % 900900
    ids9 = []
    for _ in range(3):
        kw = kwargs_for(key9)
        kw["prompt_cache_key"] = "sess-sliding"
        res = await h9.async_filter_deployments(
            model="wtest", healthy_deployments=make_deployments(),
            messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
        )
        ids9.append(pick_id(res))
        await asyncio.sleep(0.7)  # 3 次跨 1.4s > ttl=1s，只有续期才能全 HIT 同台
    same9 = len(set(ids9)) == 1
    await asyncio.sleep(1.3)  # 静默超 ttl → pin 应过期
    ck9 = h9.get_affinity_cache_key("wtest", key9, "sess-sliding")
    expired = (await h9.cache.async_get_cache(key=ck9)) is None
    ok9 = same9 and expired
    print(f"[T9 滑动续期] 跨TTL连打同台={same9} ids={ids9}, 静默后过期={expired}  T9 {'PASS' if ok9 else 'FAIL'}")

    # ---- 测试10: failover 排除名单改写 session pin（排除钉的台 → 换台并保持新台）----
    key10 = "%064x" % 101010
    kw = kwargs_for(key10)
    kw["prompt_cache_key"] = "sess-failover"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    pinned10 = pick_id(res)
    kw = kwargs_for(key10)
    kw["prompt_cache_key"] = "sess-failover"
    kw["_excluded_deployment_ids"] = {pinned10}  # 模拟 weighted-failover 重试
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    repicked10 = pick_id(res)
    kw = kwargs_for(key10)
    kw["prompt_cache_key"] = "sess-failover"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    after10 = pick_id(res)
    ok10 = repicked10 is not None and repicked10 != pinned10 and after10 == repicked10
    print(f"[T10 failover 改写 pin] 原={pinned10} 重挑={repicked10} 后续={after10}  T10 {'PASS' if ok10 else 'FAIL'}")

    # ---- 测试11: 传输类故障标记 → pin 立即迁移 + 改写（悬挂黑洞修复）----
    class Timeout(Exception):
        pass

    key11 = "%064x" % 111111
    kw = kwargs_for(key11)
    kw["prompt_cache_key"] = "sess-failmark"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    pinned11 = pick_id(res)
    await handler.async_log_failure_event(
        {"exception": Timeout("hang"), "litellm_params": {"model_info": {"id": pinned11}}},
        None, None, None,
    )
    kw = kwargs_for(key11)
    kw["prompt_cache_key"] = "sess-failmark"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    moved11 = pick_id(res)
    kw = kwargs_for(key11)
    kw["prompt_cache_key"] = "sess-failmark"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    stay11 = pick_id(res)
    ok11 = moved11 is not None and moved11 != pinned11 and stay11 == moved11
    print(f"[T11 故障标记迁移] 原={pinned11} 迁移={moved11} 后续={stay11}  T11 {'PASS' if ok11 else 'FAIL'}")

    # ---- 测试12: 4xx 客户端错误不标记 → pin 不动 ----
    class BadRequestError(Exception):
        pass

    key12 = "%064x" % 121212
    kw = kwargs_for(key12)
    kw["prompt_cache_key"] = "sess-badreq"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    pinned12 = pick_id(res)
    await handler.async_log_failure_event(
        {"exception": BadRequestError("bad tools"), "litellm_params": {"model_info": {"id": pinned12}}},
        None, None, None,
    )
    kw = kwargs_for(key12)
    kw["prompt_cache_key"] = "sess-badreq"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    ok12 = pick_id(res) == pinned12
    print(f"[T12 4xx不标记] 原={pinned12} 后续={pick_id(res)}  T12 {'PASS' if ok12 else 'FAIL'}")

    # ---- 测试13: 全部成员被标记 → 仍返回 1 台不为空 ----
    for dep in ("wtest-a", "wtest-b", "wtest-c"):
        await handler.async_log_failure_event(
            {"exception": Timeout("hang"), "litellm_params": {"model_info": {"id": dep}}},
            None, None, None,
        )
    kw = kwargs_for("%064x" % 131313)
    kw["prompt_cache_key"] = "sess-allmarked"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    ok13 = isinstance(res, list) and len(res) == 1
    print(f"[T13 全标记兜底] 返回台数={len(res)} (期望1)  T13 {'PASS' if ok13 else 'FAIL'}")

    oks = [ok1, ok2, ok3, ok4, ok5, ok6, ok7, ok8, ok9, ok10, ok11, ok12, ok13]
    print(f"\n=== 汇总: " + " ".join(f"T{i+1}={'P' if v else 'F'}" for i, v in enumerate(oks)) + " ===")
    if not all(oks):
        sys.exit(1)


asyncio.run(run())
