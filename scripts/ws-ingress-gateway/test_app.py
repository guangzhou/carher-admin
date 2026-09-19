#!/usr/bin/env python3
"""ws-ingress-gateway 离线测试：mock 内部上游(SSE)，模拟 codex 客户端两轮增量 + prewarm + 上游错。"""
import asyncio
import json
import sys

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import app as G

RESULTS = []
def check(name, cond, extra=""):
    RESULTS.append(cond)
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)[:120]) if extra and not cond else ""))

SEEN_BODIES = []

UPSTREAM_STATE = {"cancelled": False}

async def mock_upstream(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    SEEN_BODIES.append(body)
    if body.get("model") == "boom":
        return web.Response(status=429, text="rate limited")
    if body.get("model") == "slow":
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        try:
            await resp.write(b'data: {"type": "response.created", "response": {"id": "r_slow", "status": "in_progress"}}\n\n')
            for _ in range(50):
                await asyncio.sleep(0.2)
                await resp.write(b'data: {"type": "response.output_text.delta", "delta": "x"}\n\n')
        except (asyncio.CancelledError, ConnectionResetError):
            UPSTREAM_STATE["cancelled"] = True
            raise
        return resp
    rid = f"resp_up_{len(SEEN_BODIES)}"
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    async def send(ev):
        await resp.write(f"data: {json.dumps(ev)}\n\n".encode())
    await send({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
    await send({"type": "response.output_item.done",
                "item": {"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": f"ans{len(SEEN_BODIES)}"}]}})
    await send({"type": "response.completed", "response": {"id": rid, "status": "completed", "output": []}})
    await resp.write(b"data: [DONE]\n\n")
    return resp

def u(t): return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}

async def drive(ws, frame):
    await ws.send_str(json.dumps(frame))
    evs = []
    while True:
        msg = await asyncio.wait_for(ws.receive(), timeout=10)
        if msg.type != WSMsgType.TEXT:
            evs.append(("WS", str(msg.type)))
            break
        ev = json.loads(msg.data)
        evs.append(ev)
        if ev.get("type") in ("response.completed", "error"):
            break
    return evs

async def main():
    up = web.Application()
    up.router.add_post("/v1/responses", mock_upstream)
    up_server = TestServer(up)
    await up_server.start_server()
    G.LITELLM_URL = f"http://127.0.0.1:{up_server.port}"

    gw_server = TestServer(G.make_app())
    await gw_server.start_server()
    base = f"http://127.0.0.1:{gw_server.port}"

    async with ClientSession() as s:
        # 0. HTTP POST 回落必须真正可服务(透传上游) —— 405 只属于 WS 升级探测语义
        async with s.post(f"{base}/v1/responses",
                          json={"model": "m", "input": [u("fallback")], "stream": True},
                          headers={"Authorization": "Bearer sk-test"}) as r:
            body = await r.text()
            check("plain POST -> proxied to upstream (200 + stream)",
                  r.status == 200 and "response.completed" in body)
        # 0b. 无 Bearer → 401
        try:
            await s.ws_connect(f"{base}/v1/responses")
            check("no-auth ws rejected", False)
        except Exception:
            check("no-auth ws rejected", True)

        ws = await s.ws_connect(f"{base}/v1/responses",
                                headers={"Authorization": "Bearer sk-test"})
        # 1. prewarm 本地合成（不许打上游）
        n0 = len(SEEN_BODIES)
        evs = await drive(ws, {"type": "response.create", "generate": False,
                               "model": "m", "input": [u("boot")]})
        check("prewarm synthesized locally (created+completed)",
              [e.get("type") for e in evs] == ["response.created", "response.completed"])
        check("prewarm NOT forwarded upstream", len(SEEN_BODIES) == n0)
        prewarm_rid = evs[-1]["response"]["id"]

        # 2. T1: prev=prewarm_rid + delta → full = prewarm 账本 + delta
        evs = await drive(ws, {"type": "response.create", "generate": True, "model": "m",
                               "input": [u("q1")], "previous_response_id": prewarm_rid})
        check("T1 completed", evs[-1]["type"] == "response.completed")
        check("T1 upstream got FULL (boot+q1) with stream/store set",
              len(SEEN_BODIES[-1]["input"]) == 2 and SEEN_BODIES[-1]["stream"] is True
              and "previous_response_id" not in SEEN_BODIES[-1] and "generate" not in SEEN_BODIES[-1])
        rid1 = evs[-1]["response"]["id"]

        # 3. T2: delta-only + prev=rid1 → full = boot+q1+assistant+q2 = 4 项
        evs = await drive(ws, {"type": "response.create", "generate": True, "model": "m",
                               "input": [u("q2")], "previous_response_id": rid1})
        check("T2 completed", evs[-1]["type"] == "response.completed")
        check("T2 reconstructed full history (4 items, delta was 1)",
              len(SEEN_BODIES[-1]["input"]) == 4)

        # 4. prev 不识 → **关连接**(绝不拿 delta 当全量=静默失忆); 客户端将重连全量
        n4 = len(SEEN_BODIES)
        evs = await drive(ws, {"type": "response.create", "generate": True, "model": "m",
                               "input": [u("A"), u("B")], "previous_response_id": "resp_bogus"})
        check("unreconstructable prev -> connection closed, NOT forwarded",
              len(SEEN_BODIES) == n4 and evs and evs[-1][0] == "WS")
        # 重连后客户端按纪律发全量 → 正常
        ws = await s.ws_connect(f"{base}/v1/responses",
                                headers={"Authorization": "Bearer sk-test"})
        evs = await drive(ws, {"type": "response.create", "generate": True, "model": "m",
                               "input": [u("A"), u("B")]})
        check("reconnect + client-full works", evs[-1]["type"] == "response.completed"
              and len(SEEN_BODIES[-1]["input"]) == 2)

        # 5. 上游 429 → error 帧 + 账本清空(下一轮全量)
        evs = await drive(ws, {"type": "response.create", "generate": True, "model": "boom",
                               "input": [u("x")]})
        check("upstream 429 -> error frame to client", evs[-1]["type"] == "error"
              and evs[-1].get("status") == 429)
        await ws.close()

        # 6. 超限帧(>WS_MAX_MSG) → 服务端关连接(客户端将回落 HTTP)
        G_ws = await s.ws_connect(f"{base}/v1/responses",
                                  headers={"Authorization": "Bearer sk-test"},
                                  max_msg_size=0)
        big = {"type": "response.create", "generate": True, "model": "m",
               "input": [{"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": "x" * (17 * 1024 * 1024)}]}]}
        try:
            await G_ws.send_str(json.dumps(big))
            msg = await asyncio.wait_for(G_ws.receive(), timeout=10)
            check("oversize frame -> connection closed", msg.type != WSMsgType.TEXT)
        except Exception:
            check("oversize frame -> connection closed", True)
        # 7. 账本上限: cap=3 → 提交后超限清空 → 下一轮 prev 命中但 ledger 空 → 走全量
        G.LEDGER_MAX_ITEMS = 3
        ws2 = await s.ws_connect(f"{base}/v1/responses",
                                 headers={"Authorization": "Bearer sk-test"})
        evs = await drive(ws2, {"type": "response.create", "generate": True, "model": "m",
                                "input": [u("a"), u("b"), u("c")]})
        ridc = evs[-1]["response"]["id"]
        n7 = len(SEEN_BODIES)
        evs = await drive(ws2, {"type": "response.create", "generate": True, "model": "m",
                                "input": [u("d")], "previous_response_id": ridc})
        check("ledger-cap reset -> delta+prev CLOSED not amnesia-forwarded",
              len(SEEN_BODIES) == n7 and evs and evs[-1][0] == "WS")
        G.LEDGER_MAX_ITEMS = 20000
        await ws2.close()
        # 7b. 字节上限（2026-09-19 补：OOMKill 46 次而条数上限一次没触发过）。
        #     条数留在默认 20000 不动 —— 必须证明是**字节**这根轴单独把闸门拉响的。
        G.LEDGER_MAX_BYTES = 4096
        ws2b = await s.ws_connect(f"{base}/v1/responses",
                                  headers={"Authorization": "Bearer sk-test"})
        evs = await drive(ws2b, {"type": "response.create", "generate": True, "model": "m",
                                 "input": [u("y" * 8192)]})   # 1 项，远低于 20000 条
        ridb = evs[-1]["response"]["id"]
        n7b = len(SEEN_BODIES)
        evs = await drive(ws2b, {"type": "response.create", "generate": True, "model": "m",
                                 "input": [u("z")], "previous_response_id": ridb})
        check("byte-cap alone (items far under cap) -> CLOSED not amnesia-forwarded",
              len(SEEN_BODIES) == n7b and evs and evs[-1][0] == "WS")
        await ws2b.close()
        # 7c. 阴性对照：同样的两轮，账本没超字节上限 ⇒ 必须照常走 incremental。
        #     少了这条，把 LEDGER_MAX_BYTES 设成 0 也能让 7b 变绿（合成绿）。
        G.LEDGER_MAX_BYTES = 48 * 1024 * 1024
        ws2c = await s.ws_connect(f"{base}/v1/responses",
                                  headers={"Authorization": "Bearer sk-test"})
        evs = await drive(ws2c, {"type": "response.create", "generate": True, "model": "m",
                                 "input": [u("y" * 8192)]})
        ridc2 = evs[-1]["response"]["id"]
        n7c = len(SEEN_BODIES)
        await drive(ws2c, {"type": "response.create", "generate": True, "model": "m",
                           "input": [u("z")], "previous_response_id": ridc2})
        check("negative control: under byte cap -> still incremental (forwarded, 3 items)",
              len(SEEN_BODIES) == n7c + 1 and len(SEEN_BODIES[-1]["input"]) == 3)
        await ws2c.close()
        # 8. 双连接隔离: 各自账本互不串
        wa = await s.ws_connect(f"{base}/v1/responses", headers={"Authorization": "Bearer sk-a"})
        wb = await s.ws_connect(f"{base}/v1/responses", headers={"Authorization": "Bearer sk-b"})
        ea = await drive(wa, {"type": "response.create", "generate": True, "model": "m", "input": [u("A1")]})
        eb = await drive(wb, {"type": "response.create", "generate": True, "model": "m", "input": [u("B1")]})
        ra, rb = ea[-1]["response"]["id"], eb[-1]["response"]["id"]
        await drive(wa, {"type": "response.create", "generate": True, "model": "m",
                         "input": [u("A2")], "previous_response_id": ra})
        check("connection isolation: A's full has A-history only (3 items)",
              len(SEEN_BODIES[-1]["input"]) == 3 and "A1" in json.dumps(SEEN_BODIES[-1]))
        await wa.close(); await wb.close()

        # 9. 客户端中途断线 → 上游转发被取消(不悬挂不泄漏)
        UPSTREAM_STATE["cancelled"] = False
        wc = await s.ws_connect(f"{base}/v1/responses", headers={"Authorization": "Bearer sk-c"})
        await wc.send_str(json.dumps({"type": "response.create", "generate": True,
                                      "model": "slow", "input": [u("x")]}))
        msg = await asyncio.wait_for(wc.receive(), timeout=10)   # 收到首帧后掐线
        await wc.close()
        await asyncio.sleep(1.5)
        check("client disconnect mid-stream -> upstream forward cancelled",
              UPSTREAM_STATE["cancelled"])

    await gw_server.close(); await up_server.close()
    ok = sum(1 for r in RESULTS if r)
    print(f"\n{ok}/{len(RESULTS)} passed")
    sys.exit(0 if ok == len(RESULTS) else 1)

asyncio.run(main())
