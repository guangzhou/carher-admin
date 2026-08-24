#!/usr/bin/env python3
"""
WS ingress 网关（#24 Phase A 原型）——补"客户端→网关"腿的增量。

codex 客户端（≥0.118，supports_websockets=true）对 provider 走 responses-over-WebSocket：
T1 全量 → 后续轮只发 delta + previous_response_id。本服务终结这条 WS，
按连接维护账本重建全量，转内部 HTTP 打外层 litellm（计费/路由/换号全不动），
SSE 回程逐帧转 WS。

协议红线（调研实锤，见 memory project_codex_harness_ws_v2_research_2026_08_23）：
  - prewarm（generate:false）**本地合成** response.created+completed，绝不转发上游
    （CLIProxyAPI #1901 教训：上游拒 generate 参数且白烧 token）；
  - WS 语义下客户端可省略 store/stream（默认 store+stream）——转 HTTP 时补上；
  - 客户端 WS 失败自动回落 HTTP 全量（协议内建兜底）——本服务一切异常直接关连接即安全。

账本纪律（照 ws_transport 反向）：
  - 每连接独立账本（items + last_rid）；prev_id 匹配 → full = ledger + delta；
    不匹配/无 prev → 按客户端已发全量对待；
  - 轮完成后 ledger = 本轮 full_input + 本轮 output items（客户端下轮的前缀即此）；
  - 非 completed 终止 → 账本清空（下轮全量）。

运行：LITELLM_URL=http://litellm-proxy:4000 python3 app.py  （监听 :8799）
离线测试：python3 test_app.py（mock 内部上游，不碰生产）
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm-proxy.litellm-product.svc:4000")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8799"))
TURN_TIMEOUT_S = float(os.getenv("TURN_TIMEOUT_S", "600"))


def _now_rid() -> str:
    return "resp_ing_" + secrets.token_hex(12)


def _ev(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False)


class ConnState:
    def __init__(self) -> None:
        self.ledger: List[Any] = []
        self.last_rid: Optional[str] = None
        self.turns = 0


async def _forward_turn(ws: web.WebSocketResponse, st: ConnState, frame: Dict[str, Any],
                        auth: str, http: ClientSession) -> None:
    """一轮：重建全量 → 内部 HTTP → SSE 逐帧转 WS → 提交账本。"""
    delta = frame.get("input") or []
    prev = frame.get("previous_response_id")
    if prev and prev == st.last_rid and st.ledger:
        full = st.ledger + list(delta)
        mode = "incremental"
    else:
        full = list(delta)
        mode = "full"
    body = {k: v for k, v in frame.items()
            if k not in ("type", "generate", "previous_response_id", "input")}
    body["input"] = full
    body["stream"] = True
    body.setdefault("store", False)

    st.last_rid = None  # 收据纪律：发送即清
    outputs: List[Any] = []
    completed_rid: Optional[str] = None
    t0 = time.time()
    async with http.post(f"{LITELLM_URL}/v1/responses", json=body,
                         headers={"Authorization": auth},
                         timeout=ClientTimeout(total=TURN_TIMEOUT_S)) as resp:
        if resp.status != 200:
            text = (await resp.text())[:500]
            await ws.send_str(_ev({"type": "error", "status": resp.status,
                                   "error": {"message": text}}))
            st.ledger = []
            return
        buf = b""
        async for chunk in resp.content.iter_any():
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                for line in block.decode("utf-8", "replace").splitlines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        continue
                    await ws.send_str(data)
                    try:
                        ev = json.loads(data)
                    except Exception:
                        continue
                    et = ev.get("type")
                    if et == "response.output_item.done" and ev.get("item") is not None:
                        outputs.append(ev["item"])
                    elif et == "response.completed":
                        completed_rid = (ev.get("response") or {}).get("id")
    if completed_rid:
        st.ledger = full + outputs
        st.last_rid = completed_rid
        st.turns += 1
        print(f"ws_ingress turn mode={mode} in={len(delta)} full={len(full)} "
              f"out={len(outputs)} rid={completed_rid[:20]} dt={time.time()-t0:.1f}s", flush=True)
    else:
        st.ledger = []
        print(f"ws_ingress turn mode={mode} NOT-completed -> ledger reset", flush=True)


async def ws_handler(request: web.Request) -> web.StreamResponse:
    if os.getenv("WS_INGRESS_DEBUG") == "1":
        print("== HANDSHAKE ==", request.method, request.path_qs, flush=True)
        for k, v in request.headers.items():
            vv = (v[:12] + "...") if k.lower() == "authorization" else v[:200]
            print(f"  H {k}: {vv}", flush=True)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return web.Response(status=401, text="missing bearer")
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    st = ConnState()
    async with ClientSession() as http:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            if os.getenv("WS_INGRESS_DEBUG") == "1":
                print(f"== FRAME ({len(msg.data)}B) ==", msg.data[:1500], flush=True)
            try:
                frame = json.loads(msg.data)
            except Exception:
                await ws.send_str(_ev({"type": "error", "error": {"message": "bad json"}}))
                continue
            if frame.get("type") != "response.create":
                await ws.send_str(_ev({"type": "error", "error": {"message": "unsupported frame"}}))
                continue
            if frame.get("generate") is False:
                # prewarm：本地合成，绝不转发（红线）。
                rid = _now_rid()
                await ws.send_str(_ev({"type": "response.created",
                                       "response": {"id": rid, "status": "in_progress"}}))
                await ws.send_str(_ev({"type": "response.completed",
                                       "response": {"id": rid, "status": "completed", "output": []}}))
                st.last_rid = rid
                st.ledger = list(frame.get("input") or [])
                continue
            try:
                await _forward_turn(ws, st, frame, auth, http)
            except Exception as e:
                # 一切异常：告知后关连接——客户端协议内建回落 HTTP 全量。
                try:
                    await ws.send_str(_ev({"type": "error",
                                           "error": {"message": f"gateway: {type(e).__name__}"}}))
                except Exception:
                    pass
                break
    return ws


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/v1/responses", ws_handler)
    app.router.add_get("/responses", ws_handler)
    # 非 WS 客户端打到这里 → 405，codex 秒回落 HTTP（实测我们外层现行为同款）。
    # HTTP 回落路径必须**真正可服务**（官方语义: 405 只属于 WS 升级探测; 把 POST 也 405
    # 会顶死 codex 的回落重试 → 客户端假死）。透明透传到内部 litellm。
    async def _post_proxy(r: web.Request) -> web.StreamResponse:
        if os.getenv("WS_INGRESS_DEBUG") == "1":
            print(f"== HTTP-POST fallback == len={r.content_length}", flush=True)
        body = await r.read()
        async with ClientSession() as http:
            async with http.post(f"{LITELLM_URL}/v1/responses", data=body,
                                 headers={"Authorization": r.headers.get("Authorization", ""),
                                          "Content-Type": "application/json"},
                                 timeout=ClientTimeout(total=TURN_TIMEOUT_S)) as up:
                resp = web.StreamResponse(status=up.status, headers={
                    "Content-Type": up.headers.get("Content-Type", "text/event-stream")})
                await resp.prepare(r)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
    app.router.add_post("/v1/responses", _post_proxy)
    app.router.add_get("/healthz", lambda r: web.Response(text="ok"))
    if os.getenv("WS_INGRESS_DEBUG") == "1":
        # 捕获打到别的 path 的握手尝试（K1）。必须最后注册,否则吞掉上面的路由。
        async def _unmatched(r):
            print(f"== UNMATCHED == {r.method} {r.path_qs} upgrade={r.headers.get('Upgrade')}", flush=True)
            return web.Response(status=405)
        app.router.add_route("*", "/{tail:.*}", _unmatched)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=LISTEN_PORT)
