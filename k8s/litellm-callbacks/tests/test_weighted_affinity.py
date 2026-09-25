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

    # ---- 测试10: failover 排除名单只绕行、不改钉（2026-09-25 语义翻转）----
    #
    # 这条断言原先钉的是**反的**：老实现在排除名单命中时走 MISS 分支改写 pin，
    # 测试也就跟着断言「后续 == 重挑的台」。2026-09-25 查 cursor-buyitian-pwcf
    # 才发现那正是病根 —— 单会话 24h 被搬 31 个 acct，每搬一次砸掉整份前缀缓存
    # （session_id 逐字相同、status=success、prompt_tokens 跨切换点连续增长，
    #  即会话中途换号而不是新会话重摇）。排除名单是**请求级**的临时状态，
    # cooldown 也只有 60s，都不该留下永久后果。
    #
    # 现在的契约：这一刻绕行到替补，pin 原封不动；号一恢复就回家。
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
    # 绕行期间 pin 必须还指向原台（不是「反正下次会回来」，而是缓存里没被动过）
    ck10 = handler.get_affinity_cache_key("wtest", key10, "sess-failover")
    pin_after_detour10 = (await handler.cache.async_get_cache(key=ck10) or {}).get("model_id")
    kw = kwargs_for(key10)
    kw["prompt_cache_key"] = "sess-failover"
    res = await handler.async_filter_deployments(
        model="wtest", healthy_deployments=make_deployments(),
        messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
    )
    after10 = pick_id(res)
    ok10 = (
        repicked10 is not None
        and repicked10 != pinned10        # 这一刻确实绕开了
        and pin_after_detour10 == pinned10  # 但 pin 没被改写
        and after10 == pinned10           # 恢复后回到原台
    )
    print(f"[T10 抖动只绕行不改钉] 原={pinned10} 绕行={repicked10} "
          f"pin={pin_after_detour10} 恢复后={after10}  T10 {'PASS' if ok10 else 'FAIL'}")

    # ---- 测试11: 传输类故障标记 → 本次迁走 + 绕行复用（悬挂黑洞修复）----
    #
    # 注意「为什么通过」变了：老实现靠改写 pin 让 stay11 == moved11；
    # 现在 fail-mark 期内靠**绕行复用**返回同一个替补（pin 仍是原台）。
    # 两者外部表现一样，所以这里额外断言 pin 没被改写 —— 不然这条测试会在
    # 语义回退时继续绿。fail-mark 是 180s 短标记，过期就该回家，
    # 永久搬家得由 detour_repin_after 决定（见 T15）。
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
    ck11 = handler.get_affinity_cache_key("wtest", key11, "sess-failmark")
    pin11 = (await handler.cache.async_get_cache(key=ck11) or {}).get("model_id")
    ok11 = (
        moved11 is not None
        and moved11 != pinned11     # 标记期内确实迁走
        and stay11 == moved11       # 绕行期保持黏性（不是每请求重摇）
        and pin11 == pinned11       # 且 pin 没被改写
    )
    print(f"[T11 故障标记绕行] 原={pinned11} 迁移={moved11} 后续={stay11} "
          f"pin={pin11}  T11 {'PASS' if ok11 else 'FAIL'}")

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

    # ---- 测试14: 持续绕行期间保持黏性，且始终不改钉 ----
    #
    # 绕行本身必须也是黏的。如果绕行期每个请求都重摇，缓存会碎得比「改钉一次」
    # 更厉害 —— 那就把 buyitian 那个病从「24h 搬 31 次」改成「每请求搬一次」。
    async def detour_once(h, key, sess, exclude):
        kw = kwargs_for(key)
        kw["prompt_cache_key"] = sess
        if exclude:
            kw["_excluded_deployment_ids"] = set(exclude)
        r = await h.async_filter_deployments(
            model="wtest", healthy_deployments=make_deployments(),
            messages=[{"role": "user", "content": "hi"}], request_kwargs=kw,
        )
        return pick_id(r)

    h14 = mod.WeightedAffinityRouter(ttl_seconds=3600)
    h14.detour_repin_after = 3600  # 本测试只看「不改钉」，把改钉门槛推远
    key14 = "%064x" % 141414
    pinned14 = await detour_once(h14, key14, "sess-detour", None)
    subs14 = [await detour_once(h14, key14, "sess-detour", {pinned14}) for _ in range(5)]
    ck14 = h14.get_affinity_cache_key("wtest", key14, "sess-detour")
    pin14 = (await h14.cache.async_get_cache(key=ck14) or {}).get("model_id")
    ok14 = (
        len(set(subs14)) == 1               # 5 次绕行全落同一个替补
        and subs14[0] != pinned14
        and pin14 == pinned14               # pin 一次都没被改写
    )
    print(f"[T14 绕行期黏性] 原={pinned14} 替补={set(subs14)} pin={pin14}  "
          f"T14 {'PASS' if ok14 else 'FAIL'}")

    # ---- 测试15: 持续不可用超过 detour_repin_after → 真改钉 ----
    #
    # 反效果防线：修完「抖动不搬家」不能变成「钉的号永久死了也死守」。
    h15 = mod.WeightedAffinityRouter(ttl_seconds=3600)
    h15.detour_repin_after = 1
    key15 = "%064x" % 151515
    pinned15 = await detour_once(h15, key15, "sess-repin", None)
    await detour_once(h15, key15, "sess-repin", {pinned15})  # 第一次绕行，记下 since
    await asyncio.sleep(1.1)                                  # 病过门槛
    repinned15 = await detour_once(h15, key15, "sess-repin", {pinned15})
    ck15 = h15.get_affinity_cache_key("wtest", key15, "sess-repin")
    pin15 = (await h15.cache.async_get_cache(key=ck15) or {}).get("model_id")
    ok15 = (
        repinned15 is not None
        and repinned15 != pinned15
        and pin15 == repinned15    # pin 真的搬到了替补
    )
    print(f"[T15 持续生病才改钉] 原={pinned15} 改钉到={pin15} 返回={repinned15}  "
          f"T15 {'PASS' if ok15 else 'FAIL'}")

    # ---- 测试16: 绕行记录靠 TTL 自然过期，而不是「HIT 即清」----
    #
    # 2026-09-25 第二轮：这条原先断言 HIT 之后记录被清掉（`_clear_detour`）。
    # 生产实测那个「恢复」信号会撒谎 —— 见 T17。现在的契约：
    #   HIT 不动记录 → 记录只在一整个 detour_repin_after 窗口内没有新 DETOUR
    #   时自然过期 → 那才算真恢复，since 从下次生病重起算。
    # 要防的事没变（陈旧 since 不得攒出误改钉），只是换了个不会撒谎的判据。
    h16 = mod.WeightedAffinityRouter(ttl_seconds=3600)
    h16.detour_repin_after = 1
    h16._detour_record_ttl = lambda: 1  # 绕过 60s 下限，否则这条测试要等一分钟
    key16 = "%064x" % 161616
    pinned16 = await detour_once(h16, key16, "sess-clear", None)
    await detour_once(h16, key16, "sess-clear", {pinned16})   # 抖一下，写 since
    back16 = await detour_once(h16, key16, "sess-clear", None)  # 恢复 → HIT
    ck16 = h16.get_affinity_cache_key("wtest", key16, "sess-clear")
    dk16 = h16.get_detour_key(ck16)
    rec_after_hit16 = await h16.cache.async_get_cache(key=dk16)
    await asyncio.sleep(1.2)  # 窗口内无新 DETOUR → 记录自己过期
    rec_expired16 = await h16.cache.async_get_cache(key=dk16)
    again16 = await detour_once(h16, key16, "sess-clear", {pinned16})
    pin16 = (await h16.cache.async_get_cache(key=ck16) or {}).get("model_id")
    ok16 = (
        back16 == pinned16                     # 恢复后回原台
        and isinstance(rec_after_hit16, dict)  # HIT **不**清记录（契约已翻转）
        and not isinstance(rec_expired16, dict)  # 但窗口内无 DETOUR → 自然过期
        and again16 != pinned16                # 再抖仍会绕行
        and pin16 == pinned16                  # 且 since 重起算 ⇒ 只绕行、不改钉
    )
    print(f"[T16 记录靠TTL过期] 恢复={back16} HIT后记录={'有' if isinstance(rec_after_hit16, dict) else '无'} "
          f"过期后={rec_expired16} 再抖={again16} pin={pin16}  T16 {'PASS' if ok16 else 'FAIL'}")

    # ---- 测试17: pin「每请求第一次 HIT 然后必失败」也必须走到改钉 ----
    #
    # 这条钉的是 2026-09-25 生产现场那个 bug（key cursor-tankaiwen-lxgf，
    # pin=chatgpt-acct-184-gpt-5.6-sol）。日志形状：
    #     11:25:13 HIT a184
    #     11:25:15 DETOUR pin=a184 unusable 0s → a186
    #     11:25:17 DETOUR pin=a184 unusable 1s → a183
    #     ...
    #     11:25:29 HIT a184                      ← 下个请求
    #     11:25:30 DETOUR pin=a184 unusable 0s   ← 计时又从 0 起
    # 一分钟内 5 轮，每轮都归零 ⇒ sick_for 永远卡在 ~10s ⇒ REPIN 永不触发。
    # 老实现（`_clear_detour` 挂在 HIT 上）在这条测试下必红。
    h17 = mod.WeightedAffinityRouter(ttl_seconds=3600)
    h17.detour_repin_after = 1
    key17 = "%064x" % 171717
    pinned17 = await detour_once(h17, key17, "sess-flap", None)
    seen17 = []
    for _ in range(4):
        await detour_once(h17, key17, "sess-flap", None)        # 请求第一次尝试 → HIT
        seen17.append(await detour_once(h17, key17, "sess-flap", {pinned17}))  # 重试 → 绕行
        await asyncio.sleep(0.4)
    ck17 = h17.get_affinity_cache_key("wtest", key17, "sess-flap")
    pin17 = (await h17.cache.async_get_cache(key=ck17) or {}).get("model_id")
    ok17 = pin17 is not None and pin17 != pinned17   # 攒够 1s ⇒ 必须已改钉
    print(f"[T17 HIT不得清零生病计时] 原={pinned17} 绕行过={set(seen17)} "
          f"pin={pin17} (期望≠原)  T17 {'PASS' if ok17 else 'FAIL'}")

    oks = [ok1, ok2, ok3, ok4, ok5, ok6, ok7, ok8, ok9, ok10, ok11, ok12, ok13,
           ok14, ok15, ok16, ok17]
    print(f"\n=== 汇总: " + " ".join(f"T{i+1}={'P' if v else 'F'}" for i, v in enumerate(oks)) + " ===")
    if not all(oks):
        sys.exit(1)


asyncio.run(run())
