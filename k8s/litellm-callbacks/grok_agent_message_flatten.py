"""grok_agent_message_flatten — 把 Responses ``input`` 里的 ``agent_message`` 摊平成普通 user message，只对打到 sub2api(grok) 的请求。

Why（2026-10-14 实测）: Codex Desktop 0.162 起，子代理回传在 ``input`` 里是
``{"type":"agent_message","author":"/root","recipient":...,"content":[input_text, encrypted_content]}``。
x.ai 的 Responses 解码器不认这个 item type，**每个号**都回 422；sub2api 把 422 当账号问题，
5 条腿依次换完才给 502，LiteLLM 再按 ``num_retries=5`` 重打 6 次 ⇒ 一个请求 = 6 行 502 + 30 次上游调用 +
约 30 秒，最后静默兜底到 deepseek。面板上 15~27% 的「失败率」就是这个放大出来的。

A/B 探针（``work-grok-422/probe_agent_message.py``，经 litellm 打 ``sa-grok-4.7``，关重试关兜底，每格 3 次）:
  A 纯 user 消息                         200 200 200   ← 阳性对照
  B + agent_message(只有 input_text)     502 502 502   ← sub2api 同秒 6 条 422×5 腿
  C + agent_message(+encrypted_content)  502 502 502
  D = C 经本函数摊平                      200 200 200
⇒ 拒的是 item type 本身，与 encrypted_content 无关（B 也拒）。

做法: ``async_pre_call_deployment_hook``（兜底换 deployment 也会再跑），按 deployment 的 ``api_base``
含 sub2api 门控；只动 ``input`` 里 type == agent_message 的项：可见文本拼成一条
``[agent message from <author>]`` 的 user message 原位替换，丢掉 ``encrypted_content``（别家解码上下文的
密文，x.ai 解不了）。没有可见文本的 agent_message 直接丢。其他 item 一律不碰；出错原样放行。
"""
import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("grok_agent_message_flatten")

# 198 上 sa-grok-* 的 api_base 是 http://sub2api.litellm-dev.svc.cluster.local:8080/v1
_SUB2API_MARKER = "sub2api"
_AGENT_TYPE = "agent_message"
_TEXT_PART_TYPES = ("input_text", "output_text", "text")
_RESPONSE_CALL_TYPES = {"responses", "aresponses"}


def _api_base(kwargs: dict) -> str:
    for src in (kwargs, kwargs.get("litellm_params") or {}):
        if isinstance(src, dict):
            ab = src.get("api_base")
            if ab:
                return str(ab)
    return ""


def _is_responses_call(call_type: Any) -> bool:
    # CallTypes 是 (str, Enum)，str() 得到 'CallTypes.aresponses'，必须取 .value
    v = getattr(call_type, "value", call_type)
    return str(v) in _RESPONSE_CALL_TYPES


def _flatten_item(item: dict) -> dict | None:
    texts = []
    for part in item.get("content") or []:
        if isinstance(part, dict) and part.get("type") in _TEXT_PART_TYPES:
            t = part.get("text")
            if isinstance(t, str) and t:
                texts.append(t)
    if not texts:
        return None
    author = item.get("author") or "agent"
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": f"[agent message from {author}]\n" + "\n".join(texts)}],
    }


def flatten_input(items: Any) -> tuple[Any, int, int]:
    """返回 (新 input, 摊平数, 丢弃数)。不是 list 或没有 agent_message 时原样返回同一个对象。"""
    if not isinstance(items, list) or not any(
        isinstance(i, dict) and i.get("type") == _AGENT_TYPE for i in items
    ):
        return items, 0, 0
    out, flat, dropped = [], 0, 0
    for it in items:
        if isinstance(it, dict) and it.get("type") == _AGENT_TYPE:
            repl = _flatten_item(it)
            if repl is None:
                dropped += 1
            else:
                out.append(repl)
                flat += 1
            continue
        out.append(it)
    return out, flat, dropped


class GrokAgentMessageFlatten(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any) -> Any:
        try:
            if not isinstance(kwargs, dict) or not _is_responses_call(call_type):
                return kwargs
            if _SUB2API_MARKER not in _api_base(kwargs):
                return kwargs
            new_input, flat, dropped = flatten_input(kwargs.get("input"))
            if flat or dropped:
                kwargs["input"] = new_input
                _log.warning(
                    "grok_agent_message_flatten: flattened=%d dropped=%d model=%s",
                    flat, dropped, kwargs.get("model"),
                )
        except Exception as exc:  # 防御：改不动也绝不能拦请求
            _log.warning("grok_agent_message_flatten error: %r", exc)
        return kwargs


grok_agent_message_flatten_instance = GrokAgentMessageFlatten()
grok_agent_message_flatten = grok_agent_message_flatten_instance

# litellm-198-lane-callback-add.py verify 认这一行判「已加载」
_log.warning("grok_agent_message_flatten: installed (gate api_base contains %r)", _SUB2API_MARKER)
