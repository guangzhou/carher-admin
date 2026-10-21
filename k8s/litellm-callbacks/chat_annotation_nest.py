"""chat_annotation_nest — chat/completions 回包里平铺的 ``url_citation`` annotation
补一份 OpenAI 标准的嵌套 ``url_citation:{url,title,start_index,end_index}``。

Why（2026-10-21 实测）: 杭州 gw（litellm 1.101.0）把 ``perplexity-sonar`` 经 198
映射到 ``sa-grok-4.20-multi-agent``（chat 入口 → Responses 桥）。桥里
``_convert_annotations_to_chat_format`` 直接把 Responses 的 annotation 原样 dump：

    {"type":"url_citation","url":..,"title":..,"start_index":0,"end_index":0}

而 Chat Completions 规范（litellm 自己的 ``ChatCompletionAnnotation`` 类型也一样）是

    {"type":"url_citation","url_citation":{"url":..,"title":..,"start_index":..,"end_index":..}}

杭州 gw 用 pydantic 校验 ``Message.annotations[i].url_citation`` 必须是 dict ⇒ 平铺形
全部 ``Invalid response object`` 500。198 侧全 success、grok 真搜完计了费，用户拿不到。
10-21 杭州本机 8/8 新题全 500；只有恰好没带引用的回包能过。

hermestest-14 直连 198 不受影响，是因为 OpenClaw perplexity 插件两种都认
（``annotation.url_citation?.url ?? annotation.url``）。

做法: ``async_post_call_success_hook``，只处理非流式 chat 回包（有 ``choices[].message``）。
每条 ``type == "url_citation"`` 且没有 ``url_citation`` dict 的 annotation，用它的平铺字段
**补**一个嵌套 dict；平铺字段原样保留（加法，已有的读法都不受影响）。已是嵌套形的不动。
不看 key、不看模型：形状不合规范就是不合规范，补齐对所有 chat 客户端都只会更兼容。
钩子自身出错原样放行。
"""
import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("chat_annotation_nest")

_FIELDS = ("url", "title", "start_index", "end_index")


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def nest_annotation(ann: Any) -> Any:
    """返回补好嵌套 ``url_citation`` 的 annotation；不需要改就原样返回同一个对象。"""
    if _get(ann, "type") != "url_citation":
        return ann
    if isinstance(_get(ann, "url_citation"), dict):
        return ann
    flat = ann if isinstance(ann, dict) else (ann.model_dump() if hasattr(ann, "model_dump") else None)
    if not isinstance(flat, dict) or not flat.get("url"):
        return ann
    nested = {k: flat[k] for k in _FIELDS if flat.get(k) is not None}
    out = dict(flat)
    out["url_citation"] = nested
    return out


def nest_message_annotations(message: Any) -> int:
    """原地改写一个 message 的 annotations，返回补了几条。"""
    anns = _get(message, "annotations")
    if not isinstance(anns, list) or not anns:
        return 0
    changed = 0
    out = []
    for a in anns:
        b = nest_annotation(a)
        if b is not a:
            changed += 1
        out.append(b)
    if changed:
        if isinstance(message, dict):
            message["annotations"] = out
        else:
            setattr(message, "annotations", out)
    return changed


def nest_response(response: Any) -> int:
    choices = _get(response, "choices")
    if not isinstance(choices, list):
        return 0
    total = 0
    for ch in choices:
        msg = _get(ch, "message")
        if msg is not None:
            total += nest_message_annotations(msg)
    return total


class ChatAnnotationNest(CustomLogger):
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        try:
            n = nest_response(response)
            if n:
                _log.warning("chat_annotation_nest: nested=%d model=%s key=%s",
                             n, (data or {}).get("model"), getattr(user_api_key_dict, "key_alias", None))
        except Exception as exc:  # 防御：改不动也绝不能拦回包
            _log.warning("chat_annotation_nest error: %r", exc)
        return response


chat_annotation_nest_instance = ChatAnnotationNest()
chat_annotation_nest = chat_annotation_nest_instance

# litellm-198-lane-callback-add.py verify 认这一行判「已加载」
_log.warning("chat_annotation_nest: installed")
