"""Gemini-FastAPI carher patch p3: retry once when a tool-enabled chat request comes back as prose
that should have been a tool call (Gemini web's consumer persona: "I can't access your machine",
"run `ls -R` yourself", or describing the repo without looking).

Mechanism: the original /v1/chat/completions handler is renamed `_carher_chat_inner` (route
decorator removed) and a thin wrapper route takes its place.
  * no tools / tool_choice == "none"  -> passthrough, untouched (real streaming kept)
  * with tools -> call inner NON-streaming; if the reply has no tool_calls and its text matches
    the refusal pattern, resend once with a nudge appended to the last user turn.
    If the client asked for stream, the final payload is replayed as SSE chunks.
Applied after patch_namedmodel.py and patch_pool_failover.py. Refuses to apply twice.
"""
SRC = "/root/gemini-web-build/src/app/server/chat.py"
s = open(SRC).read()
assert "carher p3" not in s, "already patched"

old_head = '''@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,'''
assert s.count(old_head) == 1, "chat route anchor"
s = s.replace(old_head, '''async def _carher_chat_inner(
    request: ChatCompletionRequest,''', 1)

wrapper = r'''

# carher p3 ---------------------------------------------------------------------
_CARHER_REFUSAL_RE = re.compile(
    r"(无法|不能|没有办法|没办法)(直接)?(访问|读取|查看|执行|连接|运行|看到)|"
    r"(你|您)可以(直接)?(在|使用|运行|执行|打开)|请(你|您)?(把|将|复制|粘贴|提供|告诉)|"
    r"(cannot|can't|can not|unable to|don't have)\s+(directly\s+)?(access|read|see|run|execute|view)|"
    r"you can (run|use|execute)|please (paste|share|provide)|"
    r"(代码|目录|项目)结构(通常|一般|如下)",
    re.IGNORECASE,
)
_CARHER_NUDGE = (
    "\n\n[Tool use is mandatory for this turn] You DO have access to the user's machine, but only "
    "through the tools listed in the system instructions. Do not describe what the user could do and "
    "do not guess file contents. Reply now with ONLY a [ToolCalls] block that calls the most relevant tool."
)


def _carher_wants_tools(request: ChatCompletionRequest) -> bool:
    return bool(request.tools) and request.tool_choice != "none"


def _carher_nudged(request: ChatCompletionRequest) -> ChatCompletionRequest:
    req = request.model_copy(deep=True)
    for msg in reversed(req.messages):
        if msg.role != "user" or msg.content is None:
            continue
        if isinstance(msg.content, str):
            msg.content = msg.content + _CARHER_NUDGE
        else:
            for part in reversed(msg.content):
                if getattr(part, "type", None) == "text":
                    part.text = (part.text or "") + _CARHER_NUDGE
                    break
            else:
                msg.content.append(ContentItem(type="text", text=_CARHER_NUDGE.strip()))
        break
    return req


def _carher_needs_retry(payload: dict) -> bool:
    try:
        msg = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False
    if msg.get("tool_calls"):
        return False
    return bool(_CARHER_REFUSAL_RE.search(msg.get("content") or ""))


def _carher_replay_as_stream(payload: dict) -> StreamingResponse:
    async def gen():
        base = {"id": payload["id"], "object": "chat.completion.chunk", "created": payload["created"], "model": payload["model"]}
        choice = payload["choices"][0]
        msg = choice["message"]
        yield f"data: {orjson.dumps({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]}).decode()}\n\n"
        if msg.get("reasoning_content"):
            yield f"data: {orjson.dumps({**base, 'choices': [{'index': 0, 'delta': {'reasoning_content': msg['reasoning_content']}, 'finish_reason': None}]}).decode()}\n\n"
        if msg.get("content"):
            yield f"data: {orjson.dumps({**base, 'choices': [{'index': 0, 'delta': {'content': msg['content']}, 'finish_reason': None}]}).decode()}\n\n"
        for i, tc in enumerate(msg.get("tool_calls") or []):
            delta = {"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                     "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}
            yield f"data: {orjson.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}).decode()}\n\n"
        final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]}
        if payload.get("usage"):
            final["usage"] = payload["usage"]
        yield f"data: {orjson.dumps(final).decode()}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    raw_request: Request,
    api_key: str = Depends(verify_api_key),
    tmp_dir: Path = Depends(get_temp_dir),
    image_store: Path = Depends(get_image_store_dir),
):
    if not _carher_wants_tools(request):
        return await _carher_chat_inner(request, raw_request, api_key, tmp_dir, image_store)
    wants_stream = bool(request.stream)
    first = request.model_copy(update={"stream": False})
    payload = await _carher_chat_inner(first, raw_request, api_key, tmp_dir, image_store)
    if isinstance(payload, dict) and _carher_needs_retry(payload):
        logger.warning("[carher p3] tool request answered in prose; retrying once with nudge")
        retry = _carher_nudged(first)
        try:
            second = await _carher_chat_inner(retry, raw_request, api_key, tmp_dir, image_store)
            if isinstance(second, dict):
                payload = second
                logger.info(f"[carher p3] retry tool_calls={bool(second['choices'][0]['message'].get('tool_calls'))}")
        except HTTPException as exc:
            logger.warning(f"[carher p3] retry failed ({exc.status_code}); keeping first answer")
    if wants_stream and isinstance(payload, dict):
        return _carher_replay_as_stream(payload)
    return payload

'''
anchor = '\n\n@router.post("/v1/responses")'
assert s.count(anchor) == 1, "responses anchor"
s = s.replace(anchor, wrapper + anchor[1:], 1)
open(SRC, "w").write(s)
print("patched chat.py (p3)")
