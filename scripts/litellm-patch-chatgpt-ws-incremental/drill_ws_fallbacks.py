#!/usr/bin/env python3
"""
ws_transport 异常路径演练器（acct pod 内跑，独立 python 进程，不碰 serving 进程）。
用本地 mock WS 上游对【已安装的真实模块】逐条打异常剧本——用户纪律：不等用户踩坑，
构造用例提前验证。覆盖 live 流量还没踩到的路径：
  first_frame_timeout / 426 熔断 / error-before-created / rate_limits(blocked) /
  中流断(created 后) / response.failed(post-commit) / preflight_dead 重连 / lock_busy。
每条断言：返回值(None=落 HTTP / iterator)、registry 状态、客户端实际收到的帧序列。
"""
import asyncio, json, os, sys, types

os.environ["CHATGPT_WS_INCREMENTAL"] = "1"
os.environ["CHATGPT_WS_INCREMENTAL_LOG"] = "1"

# ---- 轻量顶替 stock 迭代器（只为 drain；ws_transport 行为不受影响，其余全真）----
# 用 sys.modules 预注入 stub：ws_transport 运行时 `from litellm.responses.streaming_iterator
# import ...` 会命中缓存，不再真正 import（绕开 pod 内 /app 目录 responses 包的 import 劫持）。
class _DrainIter:
    def __init__(self, *, response, **kw):
        self._gen = response.aiter_lines()
        self.frames = []
    def __aiter__(self): return self
    async def __anext__(self):
        line = await self._gen.__anext__()
        self.frames.append(json.loads(line))
        return line

import litellm  # noqa: E402  先正常载入包
_stub = types.ModuleType("litellm.responses.streaming_iterator")
_stub.ResponsesAPIStreamingIterator = _DrainIter
sys.modules["litellm.responses.streaming_iterator"] = _stub

from litellm.llms.chatgpt.responses import ws_transport as W  # 已安装的真实模块

from aiohttp import web, WSMsgType

# ---------------- mock 上游：按 scenario 名回剧本 ----------------
def ev_created(rid): return {"type": "response.created", "response": {"id": rid, "status": "in_progress"}}
def ev_rl(allowed=True): return {"type": "codex.rate_limits",
                                 "rate_limits": {"allowed": allowed, "limit_reached": not allowed}}
def ev_item(rid, txt): return {"type": "response.output_item.done",
                               "item": {"type": "message", "role": "assistant", "id": rid,
                                        "content": [{"type": "output_text", "text": txt}]}}
def ev_done(rid): return {"type": "response.completed", "response": {"id": rid, "status": "completed", "output": []}}
def ev_err(): return {"type": "error", "status": 400, "error": {"message": "drill"}}
def ev_failed(): return {"type": "response.failed", "response": {"status": "failed"}}

SCEN = {"mode": "ok", "conn_n": 0}

async def ws_handler(request):
    if SCEN["mode"] == "h426":
        return web.Response(status=426, text="upgrade required")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    SCEN["conn_n"] += 1
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            break
        frame = json.loads(msg.data)
        n = len(frame.get("input") or [])
        m = SCEN["mode"]
        if m == "timeout":
            await asyncio.sleep(999)
        elif m == "err_before_created":
            await ws.send_json(ev_rl()); await ws.send_json(ev_err())
        elif m == "blocked":
            await ws.send_json(ev_rl(allowed=False))
        elif m == "close_before_created":
            await ws.close()
            break
        elif m == "midstream":
            await ws.send_json(ev_rl()); await ws.send_json(ev_created("r_mid"))
            await ws.send_json(ev_item("m1", "partial"))
            await ws.close()   # created 后、completed 前掐断
            break
        elif m == "failed_post_commit":
            await ws.send_json(ev_created("r_f")); await ws.send_json(ev_failed())
        elif m == "stall_after_created":
            await ws.send_json(ev_created("r_s")); await asyncio.sleep(3)
            await ws.send_json(ev_done("r_s"))
        else:  # ok：正常完成一轮
            rid = f"r_ok_{SCEN['conn_n']}_{n}"
            await ws.send_json(ev_rl()); await ws.send_json(ev_created(rid))
            await ws.send_json(ev_item(f"m{n}", f"ans{n}")); await ws.send_json(ev_done(rid))
    return ws

# ---------------- drill 驱动 ----------------
def u(t): return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}

def data_of(items):
    return {"model": "gpt-5.6-sol", "input": items, "stream": True, "store": False,
            "instructions": "drill", "include": ["reasoning.encrypted_content"]}

async def call(pck, items, api_base):
    return await W.try_ws_incremental(
        data=data_of(items), headers={"Authorization": "Bearer drill"},
        api_base=api_base, model="gpt-5.6-sol",
        logging_obj=types.SimpleNamespace(model_call_details={}, start_time=None,
                                          pre_call=lambda **k: None),
        responses_api_provider_config=object(), litellm_metadata={},
        custom_llm_provider="chatgpt",
        request_context={"prompt_cache_key": pck, "litellm_params": {}})

async def drain(it):
    frames = []
    try:
        async for line in it:
            frames.append(json.loads(line))
    except Exception as e:
        return frames, f"{type(e).__name__}: {str(e)[:90]}"
    return frames, None

RESULTS = []
def check(name, cond, extra=""):
    RESULTS.append(cond)
    print(("PASS " if cond else "FAIL ") + name + ((" | " + extra) if extra else ""))

async def main():
    app = web.Application()
    app.router.add_get("/drill", ws_handler)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18922); await site.start()
    base = "http://127.0.0.1:18922/drill"

    # 1. 首帧超时 → None，registry 干净
    W._REGISTRY.clear(); SCEN["mode"] = "timeout"
    old_budget = W._FIRST_FRAME_BUDGET_S; W._FIRST_FRAME_BUDGET_S = 1.0
    it = await call("d_timeout", [u("q")], base)
    check("first_frame_timeout → None + registry clean", it is None and not W._REGISTRY)
    W._FIRST_FRAME_BUDGET_S = old_budget

    # 2. 426 → 进程级熔断 + 后续直接跳过 WS（本 drill 进程私有，不影响 serving 进程）
    W._REGISTRY.clear(); SCEN["mode"] = "h426"; W._WS_DISABLED = False
    it = await call("d_426", [u("q")], base)
    disabled_after = W._WS_DISABLED
    SCEN["mode"] = "ok"
    it2 = await call("d_426b", [u("q")], base)   # 熔断后即使上游正常也不再试 WS
    check("426 → None + process-wide disable + skip afterwards",
          it is None and disabled_after and it2 is None and not W._REGISTRY)
    W._WS_DISABLED = False

    # 3. created 前 error → None
    W._REGISTRY.clear(); SCEN["mode"] = "err_before_created"
    it = await call("d_err", [u("q")], base)
    check("error-before-created → None + clean", it is None and not W._REGISTRY)

    # 4. rate_limits(blocked) 前导 → None（live 从未触发过的路径）
    W._REGISTRY.clear(); SCEN["mode"] = "blocked"
    it = await call("d_blk", [u("q")], base)
    check("rate_limits(blocked) → None + clean", it is None and not W._REGISTRY)

    # 5. created 前 close → None
    W._REGISTRY.clear(); SCEN["mode"] = "close_before_created"
    it = await call("d_cls", [u("q")], base)
    check("close-before-created → None + clean", it is None and not W._REGISTRY)

    # 6. 中流断（created+partial 后 close）→ 客户端拿到已发帧 + 异常终止；会话销毁；下轮全量成功
    W._REGISTRY.clear(); SCEN["mode"] = "midstream"
    it = await call("d_mid", [u("q")], base)
    check("midstream: iterator committed (created seen)", it is not None)
    frames, err = await drain(it)
    types_seen = [f.get("type") for f in frames]
    check("midstream: client got preamble+created+partial then hard error",
          "response.created" in types_seen and err is not None and "mid-stream" in (err or ""),
          f"frames={types_seen} err={err}")
    check("midstream: session destroyed (no poisoned reuse)", not W._REGISTRY)
    SCEN["mode"] = "ok"
    it = await call("d_mid", [u("q")], base)   # 下轮：全量重建成功
    frames, err = await drain(it)
    check("midstream: next turn recovers full_ws", err is None and W._REGISTRY.get("d_mid") is not None)

    # 7. response.failed post-commit → failed 帧透传给客户端（官方语义：客户端轮级重试），会话销毁
    W._REGISTRY.clear(); SCEN["mode"] = "failed_post_commit"
    it = await call("d_fail", [u("q")], base)
    frames, err = await drain(it)
    check("failed post-commit → failed frame delivered + session destroyed",
          it is not None and any(f.get("type") == "response.failed" for f in frames) and not W._REGISTRY)

    # 8. preflight_dead：正常一轮后上游关连接 → 下轮判死 → 新连接全量（不掉 HTTP）
    W._REGISTRY.clear(); SCEN["mode"] = "ok"
    it = await call("d_pf", [u("q1")], base); await drain(it)
    sess = W._REGISTRY["d_pf"]
    await sess.ws.close()   # 模拟上游闲断（CLOSE 已入本地队列）
    conn_before = SCEN["conn_n"]
    it = await call("d_pf", [u("q1"), {"type": "message", "role": "assistant", "id": "m1",
                                       "content": [{"type": "output_text", "text": "ans1"}]}, u("q2")], base)
    frames, err = await drain(it)
    check("preflight_dead → rebuilt on NEW ws connection, stayed on WS",
          it is not None and SCEN["conn_n"] == conn_before + 1 and err is None
          and W._REGISTRY.get("d_pf") is not None and W._REGISTRY["d_pf"] is not sess)

    # 9. lock_busy：同 pck 并发第二请求 → None（不排队不卡）
    W._REGISTRY.clear(); SCEN["mode"] = "ok"
    it = await call("d_lock", [u("q1")], base); await drain(it)
    SCEN["mode"] = "stall_after_created"
    t1 = asyncio.create_task(call("d_lock", [u("q1"), {"type": "message", "role": "assistant", "id": "m1",
                                                       "content": [{"type": "output_text", "text": "ans1"}]},
                                             u("q2")], base))
    await asyncio.sleep(0.5)   # t1 已持锁在流态中
    it2 = await call("d_lock", [u("qX")], base)
    check("lock_busy concurrent same-pck → None (no queueing)", it2 is None)
    it1 = await t1
    if it1 is not None:
        await drain(it1)

    await runner.cleanup()
    ok = sum(1 for r in RESULTS if r)
    print(f"\n{ok}/{len(RESULTS)} drills passed")
    sys.exit(0 if ok == len(RESULTS) else 1)

asyncio.run(main())
