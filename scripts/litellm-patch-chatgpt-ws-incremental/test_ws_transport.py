#!/usr/bin/env python3
"""
ws_transport 离线单测：用 FakeWS 回放脚本化帧，验证 7 闸门 / 账本 / delta / expected_echo，
完全不碰生产。注入 fake 模块顶掉 litellm._logging / streaming_iterator / types.utils / aiohttp。

run: python3 test_ws_transport.py
"""
import asyncio
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---- fake litellm._logging ----
_logging = types.ModuleType("litellm._logging")
class _VL:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def debug(self, *a, **k): pass
_logging.verbose_logger = _VL()
sys.modules["litellm"] = types.ModuleType("litellm")
sys.modules["litellm._logging"] = _logging

# ---- fake litellm.responses.streaming_iterator.ResponsesAPIStreamingIterator ----
# 忠实复刻 v1.90.2 stock 迭代器：用 response.aiter_bytes() 读 SSE 字节流，剥 `data: ` 前缀，
# 得到帧 JSON 文本供断言。这样离线单测能抓住 shim 接口（aiter_bytes vs aiter_lines）漂移。
resp_mod = types.ModuleType("litellm.responses.streaming_iterator")
class ResponsesAPIStreamingIterator:
    def __init__(self, *, response, model, logging_obj, responses_api_provider_config,
                 litellm_metadata, custom_llm_provider, request_data, call_type):
        self.response = response
        self._it = response.aiter_bytes()
        self.frames = []
    def __aiter__(self): return self
    async def __anext__(self):
        raw = await self._it.__anext__()           # bytes: b"data: <json>\n\n"
        line = raw.decode("utf-8")
        for ln in line.splitlines():
            if ln.startswith("data: "):
                line = ln[6:]
                break
        self.frames.append(line)
        return line
resp_mod.ResponsesAPIStreamingIterator = ResponsesAPIStreamingIterator
sys.modules["litellm.responses"] = types.ModuleType("litellm.responses")
sys.modules["litellm.responses.streaming_iterator"] = resp_mod

# ---- fake litellm.types.utils.CallTypes ----
types_utils = types.ModuleType("litellm.types.utils")
class CallTypes:
    responses = types.SimpleNamespace(value="responses")
types_utils.CallTypes = CallTypes
sys.modules["litellm.types"] = types.ModuleType("litellm.types")
sys.modules["litellm.types.utils"] = types_utils

# ---- fake aiohttp with scripted WS ----
aiohttp = types.ModuleType("aiohttp")
class WSMsgType:
    TEXT = "TEXT"; CLOSED = "CLOSED"; CLOSING = "CLOSING"; CLOSE = "CLOSE"; ERROR = "ERROR"
    PING = "PING"; PONG = "PONG"
aiohttp.WSMsgType = WSMsgType
class ClientWSTimeout:
    def __init__(self, **k): pass
aiohttp.ClientWSTimeout = ClientWSTimeout
class WSServerHandshakeError(Exception):
    def __init__(self, status=None, message=""):
        self.status = status; self.message = message; super().__init__(message)
aiohttp.WSServerHandshakeError = WSServerHandshakeError

class _Msg:
    def __init__(self, mtype, data): self.type = mtype; self.data = data

class FakeWS:
    """按 turn 脚本回放帧。每次 send_str 触发下一批帧入队。
    队空时先小睡再返 CLOSED：模拟真实 WS「无帧可读=阻塞」，让 preflight（毫秒级
    非阻塞探测）对健康连接判活；真死连接用预先入队的 CLOSED 帧模拟。"""
    def __init__(self, turns):
        self.turns = list(turns)   # list[list[dict]]  每 turn 一批帧
        self.closed = False
        self.close_code = None
        self.sent = []
        self._queue = []
    def exception(self):
        return None
    async def send_str(self, s):
        self.sent.append(json.loads(s))
        batch = self.turns.pop(0) if self.turns else []
        self._queue.extend(_Msg(WSMsgType.TEXT, json.dumps(f)) for f in batch)
    async def receive(self):
        if self._queue:
            return self._queue.pop(0)
        await asyncio.sleep(0.2)   # 健康连接队空=阻塞；preflight 0.02s 超时→判活
        if self._queue:
            return self._queue.pop(0)
        return _Msg(WSMsgType.CLOSED, None)
    async def close(self): self.closed = True

class FakeClientSession:
    _script = None  # class-level: 下一个 ws_connect 用的 turns
    last_kwargs = None  # class-level: 最近一次 ws_connect 收到的 kwargs（验 proxy 透传）
    def __init__(self, *a, **k):  # 真实签名含 trust_env= 等，一律吞掉
        self.closed = False
        self.init_kwargs = k
    async def ws_connect(self, url, **k):
        FakeClientSession.last_kwargs = k
        return FakeWS(FakeClientSession._script or [])
    async def close(self): self.closed = True
aiohttp.ClientSession = FakeClientSession
sys.modules["aiohttp"] = aiohttp

# asyncio.wait_for passthrough already fine.

import ws_transport as W  # noqa: E402

os.environ["CHATGPT_WS_INCREMENTAL"] = "1"
os.environ["CHATGPT_WS_INCREMENTAL_LOG"] = "0"
W._PREFLIGHT_TIMEOUT_S = 0.02  # FakeWS 队空睡 0.2s → 判活；真死用预入队 CLOSED → 判死

PCFG = object()

def _ctx(pck): return {"prompt_cache_key": pck, "litellm_params": {}}

def _u(t): return {"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": t}]}
def _a(t, rid): return {"type": "message", "role": "assistant", "id": rid,
                        "content": [{"type": "output_text", "text": t}]}
def _reason(enc): return {"type": "reasoning", "encrypted_content": enc}

def _item_done(item):
    # 实测：输出项经 response.output_item.done 逐个流出（store:false 下 completed.output 恒空）。
    return {"type": "response.output_item.done", "item": item}

def _completed(rid, output=None):
    # 忠实生产：completed.output 恒空 []（输出项走 output_item.done）。
    return {"type": "response.completed",
            "response": {"id": rid, "status": "completed", "output": output or []}}
def _created(rid):
    return {"type": "response.created", "response": {"id": rid, "status": "in_progress"}}

def _rate_limits(allowed=True, limit_reached=False, used_percent=12.0):
    # 实测：上游 WS 第一帧恒为 codex.rate_limits 前导（配额元数据，非生命周期事件）。
    return {"type": "codex.rate_limits",
            "rate_limits": {"allowed": allowed, "limit_reached": limit_reached,
                            "primary": {"used_percent": used_percent,
                                        "reset_after_seconds": 3600}}}

async def _drain(it):
    frames = []
    try:
        async for line in it:
            frames.append(line)
    except StopAsyncIteration:
        pass
    return frames

def _base_data(input_items):
    return {"model": "gpt-5.6-sol", "input": input_items, "stream": True,
            "store": False, "instructions": "x", "include": ["reasoning.encrypted_content"]}

async def _call(data, pck):
    return await W.try_ws_incremental(
        data=data, headers={"Authorization": "Bearer x", "ChatGPT-Account-Id": "acc"},
        api_base="https://chatgpt.com/backend-api/codex/responses", model="gpt-5.6-sol",
        logging_obj=types.SimpleNamespace(model_call_details={}, start_time=None,
                                          pre_call=lambda **k: None),
        responses_api_provider_config=PCFG, litellm_metadata={}, custom_llm_provider="chatgpt",
        request_context=_ctx(pck))

results = []
def check(name, cond):
    results.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name)

async def main():
    W._REGISTRY.clear()

    # === T1 full → T2 incremental happy path ===
    # T1: server 流 rate_limits 前导 + created(提交信号) + output_item.done(assistant)
    #     + output_item.done(reasoning-enc) + completed(output=[] 恒空)。
    #     输出项只在 done 事件里，completed.output 为空；rate_limits 前导须被 shim 透传。
    FakeClientSession._script = [[_rate_limits(),
                                  _created("resp_1"),
                                  _item_done(_a("55", "msg_1")),
                                  _item_done(_reason("gAAA")),
                                  _completed("resp_1")]]
    it1 = await _call(_base_data([_u("17*3+4?")]), "pckA")
    check("T1 returns iterator", it1 is not None)
    frames1 = await _drain(it1)
    check("T1 shim yields rate_limits preamble to client",
          any('"codex.rate_limits"' in f for f in frames1))
    sess = W._REGISTRY.get("pckA")
    check("T1 session registered", sess is not None)
    check("T1 last_response_id set", sess.last_response_id == "resp_1")
    # ledger = hash([u1]) + expected_echo([assistant]) (reasoning-enc dropped)
    check("T1 ledger len == 2 (u1 + assistant, reasoning dropped)", len(sess.item_hashes) == 2)

    # T2: client echoes [u1, assistant, u2]; expect incremental, delta=[u2], prev_id=resp_1
    FakeClientSession._script = None  # reuse existing WS
    sess.ws.turns = [[_created("resp_2"), _item_done(_a("110", "msg_2")), _completed("resp_2")]]
    data2 = _base_data([_u("17*3+4?"), _a("55", "msg_1"), _u("*2?")])
    it2 = await _call(data2, "pckA")
    check("T2 returns iterator (incremental)", it2 is not None)
    await _drain(it2)
    sent2 = sess.ws.sent[-1]
    check("T2 sent only delta (1 input item)", len(sent2.get("input", [])) == 1)
    check("T2 delta is the new user msg", sent2["input"][0]["content"][0]["text"] == "*2?")
    check("T2 carried previous_response_id=resp_1", sent2.get("previous_response_id") == "resp_1")
    check("T2 frame has generate=true", sent2.get("generate") is True)
    check("T2 frame type=response.create", sent2.get("type") == "response.create")
    check("T2 last_response_id advanced to resp_2", sess.last_response_id == "resp_2")

    # === gate: client-supplied previous_response_id → None (HTTP passthrough) ===
    W._REGISTRY.clear()
    d = _base_data([_u("hi")]); d["previous_response_id"] = "resp_x"
    it = await _call(d, "pckB")
    check("client previous_response_id → None", it is None)

    # === gate: no pck → None ===
    itn = await W.try_ws_incremental(
        data=_base_data([_u("hi")]), headers={}, api_base="https://chatgpt.com/backend-api/codex/responses",
        model="gpt-5.6-sol",
        logging_obj=types.SimpleNamespace(model_call_details={}, start_time=None, pre_call=lambda **k: None),
        responses_api_provider_config=PCFG, litellm_metadata={}, custom_llm_provider="chatgpt",
        request_context={"litellm_params": {}})
    check("no pck → None", itn is None)

    # === gate: feature off → None ===
    os.environ["CHATGPT_WS_INCREMENTAL"] = "0"
    W._REGISTRY.clear()
    itoff = await _call(_base_data([_u("hi")]), "pckC")
    check("feature off → None", itoff is None)
    os.environ["CHATGPT_WS_INCREMENTAL"] = "1"

    # === gate: properties change → full reset (not incremental) ===
    W._REGISTRY.clear()
    FakeClientSession._script = [[_created("resp_p1"), _completed("resp_p1", [_a("ok", "m1")])]]
    it = await _call(_base_data([_u("q1")]), "pckD"); await _drain(it)
    sessD = W._REGISTRY["pckD"]
    sessD.ws.turns = [[_created("resp_p2"), _completed("resp_p2", [_a("ok2", "m2")])]]
    # change model in props → properties_hash mismatch → full replay (input full, prev_id None)
    d2 = _base_data([_u("q1"), _a("ok", "m1"), _u("q2")]); d2["model"] = "gpt-5.6-terra"
    # model change also means _model_allowed still true; provider config same
    it = await _call(d2, "pckD"); await _drain(it)
    # after props change we destroy+recreate; new session is fresh
    newD = W._REGISTRY.get("pckD")
    check("props change created fresh session", newD is not None and newD is not sessD)

    # === gate: prefix break → full reset ===
    W._REGISTRY.clear()
    FakeClientSession._script = [[_created("resp_x1"), _completed("resp_x1", [_a("a", "m1")])]]
    it = await _call(_base_data([_u("q1")]), "pckE"); await _drain(it)
    sessE = W._REGISTRY["pckE"]
    # T2 with DIFFERENT first item (prefix break)
    FakeClientSession._script = [[_created("resp_x2"), _completed("resp_x2", [_a("b", "m2")])]]
    d = _base_data([_u("DIFFERENT"), _a("a", "m1"), _u("q2")])
    it = await _call(d, "pckE"); await _drain(it)
    newE = W._REGISTRY.get("pckE")
    check("prefix break → fresh session, full replay", newE is not None and newE is not sessE)
    check("prefix-break full frame sent all items + no prev_id",
          newE is not None and len(newE.ws.sent[-1]["input"]) == 3
          and newE.ws.sent[-1].get("previous_response_id") is None)

    # === first-frame error → None (HTTP fallback), session destroyed ===
    W._REGISTRY.clear()
    FakeClientSession._script = [[{"type": "error", "status": 429,
                                   "error": {"message": "usage_limit_reached"}}]]
    it = await _call(_base_data([_u("hi")]), "pckF")
    check("first-frame error → None (HTTP fallback)", it is None)
    check("errored session not left in registry", W._REGISTRY.get("pckF") is None)

    # === 硬约束1: rate_limits(blocked) 前导 → None (桶满早退 → HTTP → 外层换号) ===
    W._REGISTRY.clear()
    FakeClientSession._script = [[_rate_limits(allowed=False, limit_reached=True, used_percent=100.0)]]
    it = await _call(_base_data([_u("hi")]), "pckG")
    check("rate_limits(blocked) preamble → None (fail closed)", it is None)
    check("blocked session not left in registry", W._REGISTRY.get("pckG") is None)

    # === 硬约束1 承重: rate_limits 前导 → error(桶满) 在 created 之前 → None，绝不进流态 ===
    # 这是修复前的致命 bug：旧代码把 rate_limits 当首帧成功提交，桶满 error 会被吐给客户端
    # 而非让外层换号。现在必须等 created 提交信号，created 前的 error → None → HTTP。
    W._REGISTRY.clear()
    FakeClientSession._script = [[_rate_limits(),   # 良性前导（allowed）
                                  {"type": "error", "status": 429,
                                   "error": {"message": "usage_limit_reached"}}]]
    it = await _call(_base_data([_u("hi")]), "pckH")
    check("rate_limits→error before created → None (no premature commit)", it is None)
    check("late-error session not left in registry", W._REGISTRY.get("pckH") is None)

    # === rate_limits 前导 → close 在 created 之前 → None ===
    W._REGISTRY.clear()
    FakeClientSession._script = [[_rate_limits()]]  # 之后 FakeWS.receive 返 CLOSED
    it = await _call(_base_data([_u("hi")]), "pckI")
    check("rate_limits→close before created → None", it is None)
    check("closed session not left in registry", W._REGISTRY.get("pckI") is None)

    # === preflight：闲置期上游 CLOSE 已入队 → 判死 → 重建全量 WS（留在 WS 上，不掉 HTTP）===
    W._REGISTRY.clear()
    FakeClientSession._script = [[_created("resp_j1"), _item_done(_a("7", "mj1")), _completed("resp_j1")]]
    it = await _call(_base_data([_u("q1")]), "pckJ"); await _drain(it)
    sessJ = W._REGISTRY["pckJ"]
    sessJ.ws._queue.append(_Msg(WSMsgType.CLOSED, None))   # 模拟闲断：CLOSE 已在本地队列
    FakeClientSession._script = [[_created("resp_j2"), _item_done(_a("8", "mj2")), _completed("resp_j2")]]
    d = _base_data([_u("q1"), _a("7", "mj1"), _u("q2")])
    it = await _call(d, "pckJ")
    check("preflight-dead → rebuilt on WS (iterator returned)", it is not None)
    await _drain(it)
    newJ = W._REGISTRY.get("pckJ")
    check("preflight-dead → fresh session replaces old", newJ is not None and newJ is not sessJ)
    check("preflight rebuild sent FULL input, no prev_id",
          newJ is not None and len(newJ.ws.sent[-1]["input"]) == 3
          and newJ.ws.sent[-1].get("previous_response_id") is None)
    check("preflight-dead old session destroyed", sessJ.destroyed)
    check("rebuilt session ledger correct (3 input + 1 echo)",
          newJ is not None and len(newJ.item_hashes) == 4)

    # === GC 豁免 in-flight：TTL 过期但 lock 被持有 → 绝不驱逐（否则杀正在流式的会话）===
    W._REGISTRY.clear()
    s1 = W.WsSession("p1", "wss://x", {}); s2 = W.WsSession("p2", "wss://x", {})
    W._REGISTRY["p1"] = s1; W._REGISTRY["p2"] = s2
    s1.last_used = 0; s2.last_used = 0          # 双双"古老"
    await s1.lock.acquire()                      # s1 in-flight
    W._gc_registry()
    check("GC TTL evicts idle, spares in-flight", "p1" in W._REGISTRY and "p2" not in W._REGISTRY)
    s1.lock.release()

    # === GC LRU 超限：最旧但 in-flight → 跳过，驱逐下一个空闲的 ===
    W._REGISTRY.clear()
    old_cap = W._MAX_SESSIONS; W._MAX_SESSIONS = 1
    sa = W.WsSession("pa", "wss://x", {}); sb = W.WsSession("pb", "wss://x", {})
    W._REGISTRY["pa"] = sa; W._REGISTRY["pb"] = sb   # pa 最旧
    await sa.lock.acquire()
    W._gc_registry()
    check("GC LRU spares locked oldest, evicts idle next", "pa" in W._REGISTRY and "pb" not in W._REGISTRY)
    sa.lock.release(); W._MAX_SESSIONS = old_cap
    W._REGISTRY.clear()

    # === 超限闸门：帧 > _WS_MAX_FRAME_B → None 走 HTTP，不连接不入册 ===
    # 依据 2026-08-23 实测：上游 WS 单消息上限 16MiB(1009)；分块引导退役
    # (prev 链绕过服务端 HTTP 截断 → context_length_exceeded 必炸 + 200× token)。
    W._REGISTRY.clear()
    old_limit = W._WS_MAX_FRAME_B
    W._WS_MAX_FRAME_B = 2600
    big_items = [_u(f"item-{i}-" + "z" * 500) for i in range(7)]
    FakeClientSession._script = [[_created("resp_never"), _completed("resp_never")]]
    it = await _call(_base_data(big_items), "pckK")
    check("oversize full frame → None (straight HTTP)", it is None)
    check("oversize session not in registry", W._REGISTRY.get("pckK") is None)
    # 超限 delta：先建正常会话，再喂超限追加
    W._WS_MAX_FRAME_B = old_limit
    FakeClientSession._script = [[_created("resp_m1"), _item_done(_a("ok", "mm1")), _completed("resp_m1")]]
    it = await _call(_base_data([_u("q1")]), "pckM"); await _drain(it)
    sessM = W._REGISTRY["pckM"]
    W._WS_MAX_FRAME_B = 2600
    d = _base_data([_u("q1"), _a("ok", "mm1")] + big_items)
    it = await _call(d, "pckM")
    check("oversize delta → None (straight HTTP)", it is None)
    check("oversize delta destroys session", W._REGISTRY.get("pckM") is None and sessM.destroyed)
    W._WS_MAX_FRAME_B = old_limit
    W._REGISTRY.clear()

    # === 并发同 pck + need_full：绝不销毁 in-flight 会话（drill 抓出的真实 bug）===
    # 请求 A 正在流式（lock 持有），请求 B 同 pck 但输入更短（触发 need_full 闸⑤）：
    # B 必须返 None 走 HTTP，A 的会话不被销毁、流不被掐。
    W._REGISTRY.clear()
    FakeClientSession._script = [[_created("resp_n1"), _item_done(_a("a1", "mn1")), _completed("resp_n1")]]
    it = await _call(_base_data([_u("q1")]), "pckN"); await _drain(it)
    sessN = W._REGISTRY["pckN"]
    await sessN.lock.acquire()          # 模拟 A 在流式中
    itB = await _call(_base_data([_u("SHORTER")]), "pckN")   # B: 前缀断+更短 → need_full
    check("need_full while in-flight → None (no destroy of live stream)", itB is None)
    check("in-flight session survives concurrent need_full",
          W._REGISTRY.get("pckN") is sessN and not sessN.destroyed)
    sessN.lock.release()
    W._REGISTRY.clear()

    # === reasoning(summary) 回显预测：镜像 normalize 真实变换（prod8 差异快照实锤）===
    # 上游输出 reasoning{summary+enc} → normalize 剥 enc 字段保留项 → 客户端回显剥后版。
    # 预测须一致，否则该会话每轮 prefix_break 全量。
    W._REGISTRY.clear()
    rsn_full = {"type": "reasoning", "encrypted_content": "gAAA",
                "summary": [{"type": "summary_text", "text": "thinking..."}]}
    rsn_echoed = {"type": "reasoning",
                  "summary": [{"type": "summary_text", "text": "thinking..."}]}
    FakeClientSession._script = [[_created("resp_r1"), _item_done(rsn_full),
                                  _item_done(_a("77", "mr1")), _completed("resp_r1")]]
    it = await _call(_base_data([_u("q1")]), "pckR"); await _drain(it)
    sessR = W._REGISTRY["pckR"]
    check("reasoning(summary) kept in ledger (stripped form)", len(sessR.item_hashes) == 3)
    # T2: 客户端回显 [u1, reasoning(剥enc), assistant, u2] → 应命中增量
    sessR.ws.turns = [[_created("resp_r2"), _item_done(_a("154", "mr2")), _completed("resp_r2")]]
    d = _base_data([_u("q1"), rsn_echoed, _a("77", "mr1"), _u("*2?")])
    it = await _call(d, "pckR")
    check("echoed stripped-reasoning matches prediction → incremental",
          it is not None and sessR.ws.sent[-1].get("previous_response_id") == "resp_r1"
          and len(sessR.ws.sent[-1]["input"]) == 1)
    await _drain(it)
    W._REGISTRY.clear()

    # === canonical 空值/缺省字段不敏感（prod9 快照实锤的回显省略形态）===
    # 上游原样: content 带 annotations:[]/logprobs:[] + type:"message"; 客户端回显全省略。
    W._REGISTRY.clear()
    a_full = {"type": "message", "role": "assistant", "id": "mv1", "phase": "commentary",
              "content": [{"type": "output_text", "text": "done",
                           "annotations": [], "logprobs": []}]}
    a_echo = {"role": "assistant", "phase": "commentary",
              "content": [{"type": "output_text", "text": "done"}]}
    rsn_v = {"type": "reasoning", "content": [], "encrypted_content": "gAAA",
             "summary": [{"type": "summary_text", "text": "think"}]}
    rsn_e = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "think"}]}
    FakeClientSession._script = [[_created("resp_v1"), _item_done(rsn_v), _item_done(a_full),
                                  _completed("resp_v1")]]
    it = await _call(_base_data([_u("q1")]), "pckV"); await _drain(it)
    sessV = W._REGISTRY["pckV"]
    sessV.ws.turns = [[_created("resp_v2"), _item_done(_a("ok", "mv2")), _completed("resp_v2")]]
    d = _base_data([_u("q1"), rsn_e, a_echo, _u("q2")])
    it = await _call(d, "pckV")
    check("empty-field/default-type omission still matches → incremental",
          it is not None and sessV.ws.sent[-1].get("previous_response_id") == "resp_v1"
          and len(sessV.ws.sent[-1]["input"]) == 1)
    await _drain(it)
    W._REGISTRY.clear()

    # === internal_* 客户端注入元数据不参与比对（prod11 分歧快照实锤）===
    W._REGISTRY.clear()
    FakeClientSession._script = [[_created("resp_i1"), _item_done(_a("hi", "mi1")), _completed("resp_i1")]]
    it = await _call(_base_data([_u("q1")]), "pckI2"); await _drain(it)
    sessI = W._REGISTRY["pckI2"]
    sessI.ws.turns = [[_created("resp_i2"), _item_done(_a("ok", "mi2")), _completed("resp_i2")]]
    echoed = {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}],
              "internal_chat_message_metadata_passthrough": {"turn_id": "01a02cf4-xxx"}}
    it = await _call(_base_data([_u("q1"), echoed, _u("q2")]), "pckI2")
    check("internal_* metadata injection still matches → incremental",
          it is not None and sessI.ws.sent[-1].get("previous_response_id") == "resp_i1")
    await _drain(it)
    W._REGISTRY.clear()

    # === 出口代理必须显式透传给 ws_connect ===
    # 背景（2026-09-16 实测）：ws_connect **不认** trust_env——同一 session 下 session.get()
    # 走代理，ws_connect 却直连 chatgpt.com:443。漏出时直连是通的、握手 200、日志安静，
    # 没有任何信号，所以这两个用例是防回归的唯一闸门。
    _saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "no_proxy")}
    def _restore():
        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    for k in ("NO_PROXY", "no_proxy"):
        os.environ.pop(k, None)
    os.environ["HTTPS_PROXY"] = "http://u:p@10.68.13.243:8118"
    os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"]
    W._REGISTRY.clear()
    FakeClientSession.last_kwargs = None
    FakeClientSession._script = [[_created("resp_x1"), _item_done(_a("hi", "mx1")), _completed("resp_x1")]]
    it = await _call(_base_data([_u("q1")]), "pckPX1"); await _drain(it)
    check("env proxy → ws_connect 收到 proxy=",
          (FakeClientSession.last_kwargs or {}).get("proxy") == "http://u:p@10.68.13.243:8118")
    W._REGISTRY.clear()

    # NO_PROXY 命中该 host → 不许传 proxy（走直连，与 urllib bypass 口径一致）
    os.environ["NO_PROXY"] = "chatgpt.com"
    os.environ["no_proxy"] = "chatgpt.com"
    FakeClientSession.last_kwargs = None
    FakeClientSession._script = [[_created("resp_x2"), _item_done(_a("hi", "mx2")), _completed("resp_x2")]]
    it = await _call(_base_data([_u("q1")]), "pckPX2"); await _drain(it)
    check("NO_PROXY 命中 → ws_connect 不带 proxy",
          "proxy" not in (FakeClientSession.last_kwargs or {}))
    W._REGISTRY.clear()

    # 无代理 env → 不许传 proxy（保持 stock 行为）
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "no_proxy"):
        os.environ.pop(k, None)
    FakeClientSession.last_kwargs = None
    FakeClientSession._script = [[_created("resp_x3"), _item_done(_a("hi", "mx3")), _completed("resp_x3")]]
    it = await _call(_base_data([_u("q1")]), "pckPX3"); await _drain(it)
    check("无代理 env → ws_connect 不带 proxy",
          "proxy" not in (FakeClientSession.last_kwargs or {}))
    W._REGISTRY.clear()
    _restore()

    print("\n%d/%d passed" % (sum(1 for _, c in results if c), len(results)))
    if not all(c for _, c in results):
        sys.exit(1)

asyncio.run(main())
