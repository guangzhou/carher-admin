"""
mock-chatgpt-upstream — dev 沙箱用，模拟 chatgpt.com /backend-api/* 行为。

支持多账号、quota 状态、refresh_token rotation、fault injection。
不发任何真实流量到外网。
"""
import asyncio
import json
import os
import time
import uuid
from collections import defaultdict
from aiohttp import web

# ============ 配置 ============
N_DELTAS = int(os.environ.get("N_DELTAS", "5"))
DELAY_MS = int(os.environ.get("DELAY_MS", "5"))
# 假账号集合：env MOCK_ACCOUNTS=mock-1,mock-2,...
MOCK_ACCOUNTS = os.environ.get("MOCK_ACCOUNTS", "mock-1,mock-2,mock-3,mock-4,mock-5").split(",")

# ============ 账号状态（内存）============
# 每个 account: {access_token, refresh_token, primary_used, secondary_used, fault}
def _make_acct(name):
    return {
        "name": name,
        "access_token": f"sk-mock-at-{name}-{uuid.uuid4().hex[:8]}",
        "refresh_token": f"sk-mock-rt-{name}-{uuid.uuid4().hex[:8]}",
        "primary_used": 0.0,    # 5h window %
        "secondary_used": 0.0,  # 7d window %
        "primary_reset_at": time.time() + 5 * 3600,
        "secondary_reset_at": time.time() + 7 * 86400,
        "consecutive_401": 0,
        "fault": None,   # None | "429" | "500" | "sse_truncate" | "slow" | "auth_invalidated"
        "refresh_used": set(),  # 已用过的 refresh_token，模拟一次性
    }

ACCOUNTS = {n: _make_acct(n) for n in MOCK_ACCOUNTS}

# ============ 通用 ============
def sse(event_type: str, data: dict) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()

def _auth_account(request):
    """从 Authorization: Bearer <access_token> 反查 account；返 (acct_dict, None) 或 (None, error_resp)"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, web.json_response({"error": {"code": "missing_token"}}, status=401)
    token = auth[7:]
    for acct in ACCOUNTS.values():
        if acct["access_token"] == token:
            if acct["fault"] == "auth_invalidated":
                acct["consecutive_401"] += 1
                return None, web.json_response({"error": {"code": "invalid_token"}}, status=401)
            return acct, None
    return None, web.json_response({"error": {"code": "unknown_token"}}, status=401)

# ============ /v1/responses SSE ============
async def responses_handler(request: web.Request) -> web.StreamResponse:
    acct, err = _auth_account(request)
    if err:
        return err

    # quota 检查
    if acct["primary_used"] >= 100:
        return web.json_response(
            {"error": {"code": "rate_limit_exceeded", "type": "5h_window"}},
            status=429,
            headers={"Retry-After": "300"},
        )
    if acct["secondary_used"] >= 100:
        return web.json_response(
            {"error": {"code": "quota_exceeded", "type": "7d_window"}},
            status=429,
            headers={"Retry-After": "86400"},
        )

    # fault injection
    if acct["fault"] == "429":
        return web.json_response(
            {"error": {"code": "rate_limit_exceeded"}}, status=429,
            headers={"Retry-After": "30"},
        )
    if acct["fault"] == "500":
        return web.json_response({"error": {"code": "internal_error"}}, status=500)
    if acct["fault"] == "slow":
        await asyncio.sleep(300)  # 模拟 stall

    # quota 计量
    acct["primary_used"] = min(100.0, acct["primary_used"] + 0.5)
    acct["secondary_used"] = min(100.0, acct["secondary_used"] + 0.1)

    body = await request.json()
    rid = f"resp_mock_{uuid.uuid4().hex[:12]}"
    msg_id = f"msg_{uuid.uuid4().hex[:10]}"

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Mock-Account": acct["name"],
    })
    await resp.prepare(request)
    sleep = DELAY_MS / 1000.0

    async def w(et, data):
        await resp.write(sse(et, data))
        await asyncio.sleep(sleep)

    await w("response.created", {"type": "response.created",
        "response": {"id": rid, "status": "in_progress", "model": "mock"}})
    await w("response.in_progress", {"type": "response.in_progress",
        "response": {"id": rid, "status": "in_progress", "model": "mock"}})

    msg_item = {
        "id": msg_id, "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "", "annotations": []}],
        "status": "in_progress",
    }
    await w("response.output_item.added", {"type": "response.output_item.added",
        "output_index": 0, "item": msg_item})

    accumulated = ""
    for i in range(N_DELTAS):
        delta = f"chunk{i} "
        accumulated += delta
        await w("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": msg_id,
            "output_index": 0, "content_index": 0, "delta": delta,
        })
        # SSE 中段断流注入：第 3 个 chunk 后强制关连接
        if acct["fault"] == "sse_truncate" and i == 2:
            await resp.write_eof()
            return resp

    await w("response.output_text.done", {
        "type": "response.output_text.done", "item_id": msg_id,
        "output_index": 0, "content_index": 0, "text": accumulated,
    })

    msg_done = dict(msg_item, status="completed",
        content=[{"type": "output_text", "text": accumulated, "annotations": []}])
    await w("response.output_item.done", {"type": "response.output_item.done",
        "output_index": 0, "item": msg_done})

    await w("response.completed", {
        "type": "response.completed",
        "response": {
            "id": rid, "status": "completed", "model": "mock",
            "output": [msg_done],
            "usage": {"input_tokens": 10, "output_tokens": N_DELTAS,
                      "total_tokens": 10 + N_DELTAS},
        },
    })
    await resp.write_eof()
    return resp

# ============ /backend-api/wham/usage ============
async def wham_usage_handler(request):
    acct, err = _auth_account(request)
    if err:
        return err
    now = time.time()
    return web.json_response({
        "rate_limit": {
            "primary_window": {
                "used_percent": acct["primary_used"],
                "resets_at": acct["primary_reset_at"],
                "duration_seconds": 5 * 3600,
            },
            "secondary_window": {
                "used_percent": acct["secondary_used"],
                "resets_at": acct["secondary_reset_at"],
                "duration_seconds": 7 * 86400,
            },
        },
        "account_id": acct["name"],
        "checked_at": now,
    })

# ============ /oauth/token (refresh) ============
async def oauth_token_handler(request):
    """模拟 refresh_token rotation：旧 token 一次性，rotate 后旧的失效。"""
    data = await request.post() if request.content_type.startswith("application/x-www-form-urlencoded") else await request.json()
    grant_type = data.get("grant_type")
    refresh_token = data.get("refresh_token")

    if grant_type != "refresh_token":
        return web.json_response({"error": "unsupported_grant_type"}, status=400)

    # 找到对应账号
    for acct in ACCOUNTS.values():
        if acct["refresh_token"] == refresh_token:
            # rotate
            old_rt = acct["refresh_token"]
            if old_rt in acct["refresh_used"]:
                return web.json_response({"error": "refresh_token_reused"}, status=401)
            acct["refresh_used"].add(old_rt)
            acct["access_token"] = f"sk-mock-at-{acct['name']}-{uuid.uuid4().hex[:8]}"
            acct["refresh_token"] = f"sk-mock-rt-{acct['name']}-{uuid.uuid4().hex[:8]}"
            acct["consecutive_401"] = 0
            return web.json_response({
                "access_token": acct["access_token"],
                "refresh_token": acct["refresh_token"],
                "token_type": "Bearer",
                "expires_in": 3600,
            })
    return web.json_response({"error": "invalid_grant"}, status=401)

# ============ admin endpoints（仅 mock 内部用，验证用）============
async def admin_list(request):
    return web.json_response({
        n: {
            "name": a["name"],
            "access_token": a["access_token"],
            "refresh_token": a["refresh_token"],
            "primary_used": a["primary_used"],
            "secondary_used": a["secondary_used"],
            "fault": a["fault"],
        }
        for n, a in ACCOUNTS.items()
    })

async def admin_set_fault(request):
    data = await request.json()
    name = data["name"]
    fault = data.get("fault")
    if name not in ACCOUNTS:
        return web.json_response({"error": "unknown"}, status=404)
    ACCOUNTS[name]["fault"] = fault
    return web.json_response({"name": name, "fault": fault})

async def admin_set_quota(request):
    data = await request.json()
    name = data["name"]
    a = ACCOUNTS.get(name)
    if not a:
        return web.json_response({"error": "unknown"}, status=404)
    if "primary_used" in data: a["primary_used"] = float(data["primary_used"])
    if "secondary_used" in data: a["secondary_used"] = float(data["secondary_used"])
    return web.json_response({"name": name, "primary_used": a["primary_used"], "secondary_used": a["secondary_used"]})

async def admin_reset(request):
    for a in ACCOUNTS.values():
        a["primary_used"] = 0.0
        a["secondary_used"] = 0.0
        a["fault"] = None
        a["consecutive_401"] = 0
    return web.json_response({"reset": list(ACCOUNTS.keys())})

# ============ health ============
async def health(request):
    return web.Response(text="ok")

def make_app():
    app = web.Application()
    app.router.add_post("/v1/responses", responses_handler)
    app.router.add_get("/backend-api/wham/usage", wham_usage_handler)
    app.router.add_post("/oauth/token", oauth_token_handler)
    app.router.add_get("/health", health)
    # admin
    app.router.add_get("/_admin/accounts", admin_list)
    app.router.add_post("/_admin/fault", admin_set_fault)
    app.router.add_post("/_admin/quota", admin_set_quota)
    app.router.add_post("/_admin/reset", admin_reset)
    return app

if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=4101, access_log=None)
