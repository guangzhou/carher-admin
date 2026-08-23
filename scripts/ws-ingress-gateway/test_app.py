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

async def mock_upstream(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    SEEN_BODIES.append(body)
    if body.get("model") == "boom":
        return web.Response(status=429, text="rate limited")
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
        # 0. 非 WS POST → 405（codex 回落 HTTP 的触发条件）
        async with s.post(f"{base}/v1/responses") as r:
            check("plain POST -> 405 (http fallback trigger)", r.status == 405)
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

        # 4. prev 不匹配 → 按全量对待
        evs = await drive(ws, {"type": "response.create", "generate": True, "model": "m",
                               "input": [u("A"), u("B")], "previous_response_id": "resp_bogus"})
        check("mismatched prev -> treated as client-full (2 items)",
              len(SEEN_BODIES[-1]["input"]) == 2)

        # 5. 上游 429 → error 帧 + 账本清空(下一轮全量)
        evs = await drive(ws, {"type": "response.create", "generate": True, "model": "boom",
                               "input": [u("x")]})
        check("upstream 429 -> error frame to client", evs[-1]["type"] == "error"
              and evs[-1].get("status") == 429)
        await ws.close()

    await gw_server.close(); await up_server.close()
    ok = sum(1 for r in RESULTS if r)
    print(f"\n{ok}/{len(RESULTS)} passed")
    sys.exit(0 if ok == len(RESULTS) else 1)

asyncio.run(main())
