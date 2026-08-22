"""
acct 多账户链路 —— acct pod 内 LiteLLM 服务端合成 WS 增量传输。

目标（docs/acct-multiaccount-incremental-transport-goal.md）：砍掉 pod→上游的重复历史回放。
客户端每轮把全量历史发到 acct pod（均值 328KB，95% 是回放）；本模块在同一条持久
`wss://chatgpt.com/backend-api/codex/responses` 连接上，只把「本轮新增的 input 项 +
previous_response_id」发给上游，服务端从连接上下文拼回历史。省出网、降延迟。

结构性安全属性（硬约束 1「换号/账户异常必须扎实」的证明）：
  `try_ws_incremental()` 对**一切非成功**返回 None —— 连不上、网关关、缓存 miss、
  闸门不过、首帧 error/超时，全部返 None。调用方收到 None 就落到**原生 HTTP POST**
  （字节级不变），让 429 桶满 / 401 / 5xx 经今天久经考验的 HTTP 路径原样冒泡到外层
  路由，换号逻辑字节级同今天。地板永远是「请求自带的全量历史走 stock HTTP」，
  没有任何 pod 内状态是承重的：误判的最坏后果是一次全量回放（今天的常态）。

复用而非重造：
  - 计费 / async_success_handler / model-id 编码 / container-id 编码 —— 复用 stock
    `ResponsesAPIStreamingIterator`（通过鸭子类型 shim 喂 WS 帧当 SSE data 行）。
  - OAuth / 401 刷新 —— 复用 provider `validate_environment` 已生成的 headers。
  - 错误面 —— 一律甩回未改动的 HTTP 路径。

落点：`llm_http_handler.py :: async_response_api_handler`，在 `logging_obj.pre_call(...)`
之后、`try:/if stream:` 之前插一段锚定分支调用本模块（见配套 patcher）。

网关默认 OFF：除非 env `CHATGPT_WS_INCREMENTAL=1` 或存在 flag 文件
`/app/chatgpt_ws_incremental.flag`，否则立即返 None（全池字节级同 stock）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from litellm._logging import verbose_logger

# ---------------------------------------------------------------------------
# 常量 / 网关
# ---------------------------------------------------------------------------

_WS_BETA_HEADER = "responses_websockets=2026-02-06"
_CONNECT_BUDGET_S = float(os.getenv("CHATGPT_WS_CONNECT_BUDGET_S", "10"))
_FIRST_FRAME_BUDGET_S = float(os.getenv("CHATGPT_WS_FIRST_FRAME_BUDGET_S", "12"))
_RECV_IDLE_TIMEOUT_S = float(os.getenv("CHATGPT_WS_RECV_IDLE_S", "120"))
_SESSION_TTL_S = float(os.getenv("CHATGPT_WS_SESSION_TTL_S", "600"))
_MAX_SESSIONS = int(os.getenv("CHATGPT_WS_MAX_SESSIONS", "8"))
_FLAG_FILE = os.getenv("CHATGPT_WS_INCREMENTAL_FLAG", "/app/chatgpt_ws_incremental.flag")

# 进程级单向熔断：握手 426（上游明说不支持）→ 之后全走 HTTP，不再自动重探（codex 单点熔断纪律）。
_WS_DISABLED = False

# 模块级 registry：pck -> WsSession。num_workers 默认 1（单进程单事件循环）→ 合法。
_REGISTRY: "OrderedDict[str, WsSession]" = OrderedDict()

# 顶层易变键：canonical 比对时剥掉（对齐 codex response_items_equal_ignoring_internal_metadata）。
_VOLATILE_TOP_KEYS = ("id", "status")


def _feature_enabled() -> bool:
    if os.getenv("CHATGPT_WS_INCREMENTAL", "") == "1":
        return True
    try:
        return os.path.exists(_FLAG_FILE)
    except Exception:
        return False


def _model_allowed(model: str) -> bool:
    """可选按 model 灰度：CHATGPT_WS_INCREMENTAL_MODELS=a,b,c（缺省=全部允许）。"""
    allow = os.getenv("CHATGPT_WS_INCREMENTAL_MODELS", "").strip()
    if not allow:
        return True
    wanted = {m.strip() for m in allow.split(",") if m.strip()}
    return model in wanted or (model or "").split("/")[-1] in wanted


def _log(msg: str) -> None:
    """结构化观测行。默认打 stdout（acct pod kubectl logs 可抓，是命中率/收益的 ground truth）
    并同时进 verbose_logger.info。可用 CHATGPT_WS_INCREMENTAL_LOG=0 关 stdout。"""
    if os.getenv("CHATGPT_WS_INCREMENTAL_LOG", "1") != "0":
        try:
            print(msg, flush=True)  # noqa: T201
        except Exception:
            pass
    try:
        verbose_logger.info(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# canonical 哈希（前缀比对基石；不做按长度近似短路）
# ---------------------------------------------------------------------------

def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_volatile(v)
            for k, v in obj.items()
            if k not in _VOLATILE_TOP_KEYS
        }
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def _hash_item(item: Any) -> str:
    try:
        canon = json.dumps(
            _strip_volatile(item), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )
    except Exception:
        # 不可序列化 → 用 repr 兜底（只会降低命中率，不影响正确性）。
        canon = repr(item)
    return hashlib.sha1(canon.encode("utf-8", "replace")).hexdigest()


def _hash_items(items: List[Any]) -> List[str]:
    return [_hash_item(it) for it in items]


def _properties_hash(data: Dict[str, Any]) -> str:
    """请求「属性」哈希：allowed_keys 去掉 input / previous_response_id（这两者是增量锚点，
    本就该变）。model / instructions / tools / tool_choice / reasoning / include / store /
    stream / truncation 任一变 → 整轮全量重置。"""
    props = {
        k: v
        for k, v in data.items()
        if k not in ("input", "previous_response_id")
    }
    try:
        canon = json.dumps(props, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        canon = repr(props)
    return hashlib.sha1(canon.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------
# expected-echo 过滤（移植 chatgpt-acct-proxy compaction_drop）
#   预测「本轮 output 项里，下一轮会被客户端 + 外层 normalize 回显进 input 的部分」。
#   外层 normalize 默认整项 drop reasoning + 任何带 encrypted_content 的项。
#   预测偏差只影响命中率（前缀不匹配→全量回放），不影响正确性。
# ---------------------------------------------------------------------------

def _has_encrypted_content(obj: Any) -> bool:
    if isinstance(obj, dict):
        if "encrypted_content" in obj:
            return True
        return any(_has_encrypted_content(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_encrypted_content(v) for v in obj)
    return False


def _expected_echo(output_items: List[Any]) -> List[Any]:
    out: List[Any] = []
    for item in output_items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        itype = item.get("type")
        if itype in ("reasoning", "compaction"):
            continue
        if _has_encrypted_content(item):
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# WS 会话
# ---------------------------------------------------------------------------

class WsSession:
    """一条持久 WS 连接 + 该会话的账本。绑定单一 pck、单一账户（pod 本地）。
    previous_response_id 永不跨号（registry 是 pod 本地、每 pod 单 OAuth 身份）。"""

    def __init__(self, pck: str, ws_url: str, ws_headers: Dict[str, str]):
        self.pck = pck
        self.ws_url = ws_url
        self.ws_headers = ws_headers
        self._aio_session = None  # aiohttp.ClientSession
        self.ws = None            # aiohttp.ClientWebSocketResponse
        self.lock = asyncio.Lock()
        self.loop_id = id(asyncio.get_event_loop())
        # 账本
        self.item_hashes: List[str] = []
        self.properties_hash: Optional[str] = None
        self.last_response_id: Optional[str] = None  # 仅 response.completed 时置；发送即清（codex 收据纪律）
        self.destroyed = False
        self.created_at = time.time()
        self.last_used = time.time()
        # 计数器
        self.turns_full = 0
        self.turns_incremental = 0

    def touch(self) -> None:
        self.last_used = time.time()

    def is_ws_open(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def connect(self) -> bool:
        """新建 aiohttp WS 连接。成功 True；426 → 置全局熔断并 False；其它失败 False。"""
        global _WS_DISABLED
        import aiohttp  # pod 内已验证可用

        try:
            self._aio_session = aiohttp.ClientSession()
            self.ws = await asyncio.wait_for(
                self._aio_session.ws_connect(
                    self.ws_url,
                    headers=self.ws_headers,
                    timeout=aiohttp.ClientWSTimeout(ws_close=30),
                    autoping=True,
                    heartbeat=30,
                ),
                timeout=_CONNECT_BUDGET_S,
            )
            return True
        except aiohttp.WSServerHandshakeError as e:
            status = getattr(e, "status", None)
            if status == 426:
                _WS_DISABLED = True
                _log(f"ws_incr_fallback reason=426_handshake pck={_pck8(self.pck)} disabling_process_wide")
            else:
                _log(f"ws_incr_fallback reason=handshake_{status} pck={_pck8(self.pck)}")
            await self._safe_close_transport()
            return False
        except Exception as e:
            _log(f"ws_incr_fallback reason=connect_exc pck={_pck8(self.pck)} err={type(e).__name__}:{str(e)[:120]}")
            await self._safe_close_transport()
            return False

    async def _safe_close_transport(self) -> None:
        try:
            if self.ws is not None and not self.ws.closed:
                await self.ws.close()
        except Exception:
            pass
        try:
            if self._aio_session is not None and not self._aio_session.closed:
                await self._aio_session.close()
        except Exception:
            pass
        self.ws = None
        self._aio_session = None

    async def destroy(self) -> None:
        self.destroyed = True
        await self._safe_close_transport()
        # 从 registry 摘除（若还指向自己）
        if _REGISTRY.get(self.pck) is self:
            _REGISTRY.pop(self.pck, None)

    def reset_ledger(self) -> None:
        self.item_hashes = []
        self.properties_hash = None
        self.last_response_id = None


def _pck8(pck: str) -> str:
    return hashlib.sha1(pck.encode("utf-8", "replace")).hexdigest()[:8]


def _gc_registry() -> None:
    """LRU 上限 + idle TTL。每次调用顺带 GC。销毁是异步的，这里只标记摘除、调度关闭。"""
    now = time.time()
    # TTL 过期
    for pck in list(_REGISTRY.keys()):
        sess = _REGISTRY[pck]
        if now - sess.last_used > _SESSION_TTL_S:
            _REGISTRY.pop(pck, None)
            _schedule_close(sess)
    # LRU 超限（OrderedDict：最旧在前）
    while len(_REGISTRY) > _MAX_SESSIONS:
        _pck, sess = _REGISTRY.popitem(last=False)
        _schedule_close(sess)


def _schedule_close(sess: WsSession) -> None:
    sess.destroyed = True
    try:
        asyncio.get_event_loop().create_task(sess._safe_close_transport())
    except Exception:
        pass


def _ws_url_from_api_base(api_base: str) -> str:
    # api_base = https://chatgpt.com/backend-api/codex/responses（尊重 CHATGPT_API_BASE 覆盖）
    if api_base.startswith("https://"):
        return "wss://" + api_base[len("https://"):]
    if api_base.startswith("http://"):
        return "ws://" + api_base[len("http://"):]
    return api_base


def _build_ws_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """复用 provider 已生成的 headers（含 Authorization Bearer / ChatGPT-Account-Id /
    originator / user-agent），只加 OpenAI-Beta 开 WS。content-type/accept 对 WS 无意义，去掉。"""
    ws_headers: Dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in ("content-type", "accept"):
            continue
        ws_headers[k] = v
    ws_headers["OpenAI-Beta"] = _WS_BETA_HEADER
    return ws_headers


def _make_frame(data: Dict[str, Any], input_items: List[Any],
                previous_response_id: Optional[str]) -> Dict[str, Any]:
    """WS 帧信封（实测形状）：{"type":"response.create", <14 HTTP 字段平铺>,
    "input":..., "previous_response_id":..., "generate":true}。"""
    frame = {k: v for k, v in data.items() if k not in ("input", "previous_response_id")}
    frame["type"] = "response.create"
    frame["input"] = input_items
    if previous_response_id is not None:
        frame["previous_response_id"] = previous_response_id
    frame["generate"] = True
    return frame


# ---------------------------------------------------------------------------
# 首帧判定
# ---------------------------------------------------------------------------

def _frame_is_error(ev: Dict[str, Any]) -> bool:
    t = ev.get("type", "")
    return t in ("error", "response.failed")


def _frame_is_commit(ev: Dict[str, Any]) -> bool:
    """真正的「上游已接受、要开始生成」信号。只有收到它才提交进流态（可再回落 HTTP 的最后
    时机就在它之前）。桶满/异常账号不会走到这——它们在此之前就 error/failed/close。"""
    return ev.get("type", "") in ("response.created", "response.in_progress")


def _rate_limit_blocked(ev: Dict[str, Any]) -> bool:
    """codex.rate_limits 前导帧显式表示本账号被封顶 → 视为阻塞（换号必须扎实：早退走 HTTP）。
    只在明确 allowed=False / 顶层 limit_reached=True 时判阻塞；缺字段/其它模型的 additional
    限额不算（保守，避免假阳性误伤健康号）。"""
    if ev.get("type") != "codex.rate_limits":
        return False
    rl = ev.get("rate_limits") or {}
    if rl.get("allowed") is False:
        return True
    if rl.get("limit_reached") is True:
        return True
    return False


def _frame_terminates(ev: Dict[str, Any]) -> bool:
    return ev.get("type", "") in ("response.completed", "response.incomplete", "response.failed")


# ---------------------------------------------------------------------------
# 鸭子类型 shim：把 WS 帧喂给 stock ResponsesAPIStreamingIterator 当 SSE data 行
# ---------------------------------------------------------------------------

class _WsPseudoResponse:
    """鸭子类型顶替 httpx.Response，喂给 stock ResponsesAPIStreamingIterator。

    v1.90.2 的 stock 迭代器构造式是 `SSEDecoder().aiter_bytes(response.aiter_bytes())`
    （openai SDK 的 SSE 解码器），**用的是 aiter_bytes()，不是 aiter_lines()**。
    所以主路径 aiter_bytes() 把每帧包成 SSE 线格式字节 `data: <json>\\n\\n`，
    交 SSEDecoder 解析出 ServerSentEvent，其 .data == 帧 JSON 文本，正是 _process_chunk 期望。
    另留 aiter_lines()（逐帧 yield 帧 JSON 文本）供离线单测 / 旧版迭代器兼容。
    headers 是普通 dict。"""

    def __init__(self, headers: Dict[str, str], line_gen):
        self.headers = headers
        self._line_gen = line_gen

    def aiter_lines(self):
        return self._line_gen

    async def aiter_bytes(self):
        async for line in self._line_gen:
            yield b"data: " + line.encode("utf-8", "replace") + b"\n\n"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def try_ws_incremental(
    *,
    data: Dict[str, Any],
    headers: Dict[str, str],
    api_base: str,
    model: str,
    logging_obj: Any,
    responses_api_provider_config: Any,
    litellm_metadata: Optional[Dict[str, Any]],
    custom_llm_provider: Optional[str],
    request_context: Dict[str, Any],
):
    """
    成功 → 返回 stock ResponsesAPIStreamingIterator（读 WS 帧）。
    任何非成功 → 返回 None（调用方落到原生 HTTP POST，字节级不变）。
    """
    # --- 网关 / 前置闸门（全部返 None，静默）------------------------------------
    # 这些前置闸门在真实流量里每请求都可能命中（非 Codex 无 pck / 非流式 / 客户端自带
    # prev_id），静默返 None 走 HTTP 即可，不打日志（否则真实流量下刷屏）。真正值得观测的是
    # 「尝试了 WS 却失败」的兜底（连接/首帧/桶满）与成功的 `ws_incr mode=...`，那些照常记。
    if _WS_DISABLED or not _feature_enabled() or not _model_allowed(model):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("input"), list):
        return None

    # 闸门①：客户端自带 previous_response_id → 不混锚点，走 HTTP 透传。
    if data.get("previous_response_id"):
        return None

    # pck：无 pck 绝不用匿名/共享会话 → None 走 HTTP（非 Codex 常态，静默）。
    pck = _resolve_pck(request_context, litellm_metadata, data)
    if not pck:
        return None

    try:
        import aiohttp  # noqa: F401  确保 pod 内可用；不可用 → None
    except Exception:
        return None

    _gc_registry()

    cur_hashes = _hash_items(data["input"])
    props_hash = _properties_hash(data)
    total_items = len(data["input"])

    sess = _REGISTRY.get(pck)

    # 决定 full 还是 incremental（闸门 ②③④⑤⑥）。
    need_full = False
    if sess is None or sess.destroyed or not sess.is_ws_open() or sess.loop_id != id(asyncio.get_event_loop()):
        need_full = True
    elif not sess.last_response_id:                       # ③ 上轮未干净完成
        need_full = True
    elif sess.properties_hash != props_hash:             # ④ 属性变
        need_full = True
    elif total_items <= len(sess.item_hashes):           # ⑤ 没有严格新增
        need_full = True
    elif cur_hashes[: len(sess.item_hashes)] != sess.item_hashes:  # ⑥ 前缀不匹配
        need_full = True

    ws_url = _ws_url_from_api_base(api_base)
    ws_headers = _build_ws_headers(headers)

    if need_full:
        # 旧会话若在则销毁重建（reset = 干净重连，保证服务端上下文清白）。
        if sess is not None:
            await sess.destroy()
        sess = WsSession(pck, ws_url, ws_headers)
        await sess.lock.acquire()
        handed_off = False
        try:
            ok = await sess.connect()
            if not ok:
                return None
            frame = _make_frame(data, data["input"], previous_response_id=None)
            frame_bytes = len(json.dumps(frame).encode("utf-8", "replace"))
            first_frames = await _send_and_await_first(sess, frame)
            if first_frames is None:
                await sess.destroy()
                return None
            # 成功进流态：登记 registry，构造账本预备值，交给 shim 生成器。
            _REGISTRY[pck] = sess
            _REGISTRY.move_to_end(pck)
            sess.touch()
            pending_hashes = cur_hashes  # 本轮全部 input 项即下轮前缀基线
            it = _build_iterator(
                sess=sess, first_frames=first_frames, mode="full_ws",
                pending_hashes=pending_hashes, props_hash=props_hash,
                delta_count=total_items, total_items=total_items, frame_bytes=frame_bytes,
                model=model, logging_obj=logging_obj,
                responses_api_provider_config=responses_api_provider_config,
                litellm_metadata=litellm_metadata, custom_llm_provider=custom_llm_provider,
                request_context=request_context, headers=headers,
            )
            handed_off = True
            sess.turns_full += 1
            return it
        finally:
            if not handed_off:
                sess.lock.release()

    # ---- incremental ----
    # 抢锁：抢不到（并发同 pck）→ None 走 HTTP，绝不排队卡请求。
    if sess.lock.locked():
        _log(f"ws_incr_fallback reason=lock_busy pck={_pck8(pck)}")
        return None
    await sess.lock.acquire()
    handed_off = False
    try:
        if sess.destroyed or not sess.is_ws_open():   # 抢锁瞬间被 GC/关闭
            return None
        prev_len = len(sess.item_hashes)
        delta_items = data["input"][prev_len:]
        prev_rid = sess.last_response_id
        sess.last_response_id = None                  # 收据纪律：发送即清
        frame = _make_frame(data, delta_items, previous_response_id=prev_rid)
        frame_bytes = len(json.dumps(frame).encode("utf-8", "replace"))
        first_frames = await _send_and_await_first(sess, frame)
        if first_frames is None:
            await sess.destroy()
            return None
        sess.touch()
        _REGISTRY.move_to_end(pck)
        pending_hashes = cur_hashes
        it = _build_iterator(
            sess=sess, first_frames=first_frames, mode="incremental",
            pending_hashes=pending_hashes, props_hash=props_hash,
            delta_count=len(delta_items), total_items=total_items, frame_bytes=frame_bytes,
            model=model, logging_obj=logging_obj,
            responses_api_provider_config=responses_api_provider_config,
            litellm_metadata=litellm_metadata, custom_llm_provider=custom_llm_provider,
            request_context=request_context, headers=headers,
        )
        handed_off = True
        sess.turns_incremental += 1
        return it
    finally:
        if not handed_off:
            sess.lock.release()


def _resolve_pck(request_context, litellm_metadata, data) -> Optional[str]:
    """会话键：只认**跨轮稳定**的键——prompt_cache_key / litellm_session_id / session_id。
    与 acct provider 的 prompt_cache_key→session_id 亲和口径、外层 WA(pck) 一致。

    ⚠ 绝不用 litellm_call_id / litellm_trace_id：它们每请求唯一，用作会话键 = 每请求
    新开一条 WS 且永不复用（纯 churn），彻底抵消增量收益。找不到稳定键 → None 走 HTTP。"""
    stable_keys = ("prompt_cache_key", "litellm_session_id", "session_id")

    def _scan(src) -> Optional[str]:
        if not isinstance(src, dict):
            return None
        for k in stable_keys:
            v = src.get(k)
            if v:
                return str(v)
        meta = src.get("metadata")
        if isinstance(meta, dict):
            for k in stable_keys:
                v = meta.get(k)
                if v:
                    return str(v)
        return None

    # request_context 顶层 + 其 metadata；其 litellm_params（provider 从这里取 session_id）
    for src in (
        request_context or {},
        (request_context or {}).get("litellm_params"),
        litellm_metadata or {},
        data if isinstance(data, dict) else {},
    ):
        found = _scan(src)
        if found:
            return found
    return None


async def _send_and_await_first(sess: WsSession, frame: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """发帧 + 等**提交信号**（fail-fast，全在返回迭代器之前、预算内完成）。

    ⚠ 硬约束 1（换号必须扎实）承重点：**提交点必须是 `response.created`/`response.in_progress`，
    绝不是「第一个 TEXT 帧」。** 实测上游 WS 的第一帧恒为 `codex.rate_limits` 前导（配额元数据，
    非生命周期事件）；桶满/异常账号会在 created **之前**发 error/failed 或 rate_limits(blocked)
    或直接 close。若把 rate_limits 当作「首帧成功」提交进流态，就再也回落不到 HTTP —— 桶满号会
    把 failed 帧直接吐给客户端而非让外层换号。故这里必须：
      · 缓冲一切良性前导帧（rate_limits 等），直到收到 created/in_progress 才算提交；
      · created 之前遇到 error/failed / rate_limits(blocked) / close / 超时 → 返回 None
        （调用方销毁会话 → 同请求原样走 stock HTTP → 外层 429/换号字节级同今天）。

    返回：截至并含提交信号的帧列表（供 shim 依序先吐，保真——客户端仍收到 rate_limits）；或 None。"""
    import aiohttp

    try:
        await sess.ws.send_str(json.dumps(frame))
    except Exception as e:
        _log(f"ws_incr_fallback reason=send_exc pck={_pck8(sess.pck)} err={type(e).__name__}")
        return None

    preamble: List[Dict[str, Any]] = []
    try:
        while True:
            msg = await asyncio.wait_for(sess.ws.receive(), timeout=_FIRST_FRAME_BUDGET_S)
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    ev = json.loads(msg.data)
                except Exception:
                    continue
                if not isinstance(ev, dict):
                    continue
                if _frame_is_error(ev):
                    _log(
                        f"ws_incr_fallback reason=first_frame_error pck={_pck8(sess.pck)} "
                        f"status={ev.get('status')} err={json.dumps(ev.get('error'))[:160]}"
                    )
                    return None
                if _rate_limit_blocked(ev):
                    # 桶满/封顶前导：显式 allowed=False / limit_reached=True → 早退走 HTTP → 外层换号。
                    rl = ev.get("rate_limits") or {}
                    _log(
                        f"ws_incr_fallback reason=rate_limit_blocked pck={_pck8(sess.pck)} "
                        f"allowed={rl.get('allowed')} limit_reached={rl.get('limit_reached')}"
                    )
                    return None
                if _frame_is_commit(ev):
                    # 上游已接受、要开始生成 → 提交点。此前缓冲的良性前导 + 本帧一并回放。
                    preamble.append(ev)
                    return preamble
                # 良性前导（codex.rate_limits 等）：缓冲后继续等提交信号。
                preamble.append(ev)
                if len(preamble) > 16:
                    # 迟迟不见 created —— 异常形态，保守早退走 HTTP。
                    _log(f"ws_incr_fallback reason=no_commit_signal pck={_pck8(sess.pck)} preamble={len(preamble)}")
                    return None
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                              aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                _log(f"ws_incr_fallback reason=first_frame_closed pck={_pck8(sess.pck)} type={msg.type}")
                return None
            # 其它类型（PING/PONG）忽略，继续等 TEXT
    except asyncio.TimeoutError:
        _log(f"ws_incr_fallback reason=first_frame_timeout pck={_pck8(sess.pck)}")
        return None
    except Exception as e:
        _log(f"ws_incr_fallback reason=first_frame_exc pck={_pck8(sess.pck)} err={type(e).__name__}")
        return None


def _build_iterator(*, sess, first_frames, mode, pending_hashes, props_hash,
                    delta_count, total_items, frame_bytes, model, logging_obj,
                    responses_api_provider_config, litellm_metadata,
                    custom_llm_provider, request_context, headers):
    """构造 stock ResponsesAPIStreamingIterator，喂 WS 帧生成器。
    first_frames = _send_and_await_first 返回的前导帧列表（含提交信号 created/in_progress，
    可能前置若干 codex.rate_limits），依序先吐给客户端以保真。"""
    from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator
    from litellm.types.utils import CallTypes

    t_start = time.time()

    async def _line_gen():
        """逐帧 yield 帧 JSON 文本给 stock 迭代器（当作 SSE data 行）。
        账本仅在 response.completed 提交；其它终止一律销毁会话。lock 在 finally 释放。

        ⚠ output 项来自流式 `response.output_item.done` 事件（实测：store:false 下
        `response.completed.output` **恒为空 []**）。这里沿途累积 done 项，提交时用它算
        expected_echo；否则账本永远只含 input、下轮客户端回显 assistant 消息会被误判为
        新增 delta（错误重发已在服务端上下文里的 assistant 项）。"""
        committed = False
        collected_output: List[Any] = []

        def _collect(ev: Any) -> None:
            if isinstance(ev, dict) and ev.get("type") == "response.output_item.done":
                it = ev.get("item")
                if it is not None:
                    collected_output.append(it)

        try:
            # 先依序吐前导帧（rate_limits… + created/in_progress）。提交信号不终止，正常不会命中。
            for fr in first_frames:
                yield json.dumps(fr)
                _collect(fr)
                if _frame_terminates(fr):
                    committed = _commit_if_completed(
                        sess, fr, collected_output, pending_hashes, props_hash, mode,
                        delta_count, total_items, frame_bytes, t_start,
                    )
            if not committed:
                import aiohttp
                while True:
                    try:
                        msg = await asyncio.wait_for(sess.ws.receive(), timeout=_RECV_IDLE_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        raise RuntimeError("ws recv idle timeout")
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        yield msg.data
                        try:
                            ev = json.loads(msg.data)
                        except Exception:
                            ev = None
                        if isinstance(ev, dict):
                            _collect(ev)
                            if _frame_terminates(ev):
                                committed = _commit_if_completed(
                                    sess, ev, collected_output, pending_hashes, props_hash, mode,
                                    delta_count, total_items, frame_bytes, t_start,
                                )
                                break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                      aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        raise RuntimeError(f"ws closed mid-stream: {msg.type}")
        finally:
            # 正常完成 → 保活会话给下轮；异常/中途退出（含客户端断开 GeneratorExit）→ 销毁。
            if not committed:
                try:
                    await sess.destroy()
                except Exception:
                    pass
            if sess.lock.locked():
                try:
                    sess.lock.release()
                except Exception:
                    pass

    pseudo = _WsPseudoResponse(headers=dict(headers or {}), line_gen=_line_gen())
    return ResponsesAPIStreamingIterator(
        response=pseudo,
        model=model,
        logging_obj=logging_obj,
        responses_api_provider_config=responses_api_provider_config,
        litellm_metadata=litellm_metadata,
        custom_llm_provider=custom_llm_provider,
        request_data=request_context,
        call_type=CallTypes.responses.value,
    )


def _commit_if_completed(sess, ev, collected_output, pending_hashes, props_hash, mode,
                         delta_count, total_items, frame_bytes, t_start) -> bool:
    """response.completed → 提交账本（pending + expected_echo(输出项)），置 last_response_id
    = 原始完成 id（在 stock 迭代器把 id 改写成带 model-id 之前，这里读的是原始帧）。
    非 completed 终止（incomplete/failed）→ 不提交，返回 False（调用方会销毁会话）。

    输出项优先取沿途累积的 `response.output_item.done`（store:false 下 completed.output 恒空）；
    为跨版本鲁棒，collected 为空且 completed.output 非空时回退用后者。"""
    if ev.get("type") != "response.completed":
        _log(f"ws_incr_fallback reason=terminate_{ev.get('type')} pck={_pck8(sess.pck)}")
        return False
    resp = ev.get("response") or {}
    orig_rid = resp.get("id")
    if not orig_rid:
        _log(f"ws_incr_fallback reason=completed_no_id pck={_pck8(sess.pck)}")
        return False
    output_items = list(collected_output or [])
    if not output_items and resp.get("output"):
        output_items = resp.get("output")
    echo = _expected_echo(output_items)
    sess.item_hashes = list(pending_hashes) + _hash_items(echo)
    sess.properties_hash = props_hash
    sess.last_response_id = orig_rid
    sess.touch()
    dt_ms = int((time.time() - t_start) * 1000)
    _log(
        f"ws_incr mode={mode} pck={_pck8(sess.pck)} delta_items={delta_count} "
        f"total_input_items={total_items} out_items={len(output_items)} echo_items={len(echo)} "
        f"frame_bytes={frame_bytes} ledger_len={len(sess.item_hashes)} "
        f"prev_resp={orig_rid} elapsed_ms={dt_ms}"
    )
    return True
