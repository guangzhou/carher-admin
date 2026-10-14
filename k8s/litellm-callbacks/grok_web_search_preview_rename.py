"""grok_web_search_preview_rename — 打到 sub2api(x.ai) 的 Responses 请求里，把
``{"type":"web_search_preview"}`` 改成 ``{"type":"web_search"}``。

Why（2026-10-15 实测）: Cursor 的 WebSearch 工具不在本机搜，而是向网关发一条
``/v1/responses`` + ``tools:[{"type":"web_search_preview"}]`` 的「替我搜」请求。
x.ai 只认 ``web_search``，对 ``web_search_preview`` **静默忽略**：HTTP 200、
``num_server_side_tools_used=0``，模型凭旧记忆作答并以「搜索结果」名义返回。
A/B（sa-grok-4.20 直打，问 2026 诺贝尔物理学奖）：
  web_search_preview → 0 次搜索，答「尚未公布」（错）
  web_search         → 3 次搜索，答 Francis Halzen + nobelprize.org 链接（对）
经 kiro-claude-opus-5-5 兜底到 grok 同样复现。

做法: ``async_pre_call_deployment_hook``，``api_base`` 含 sub2api 才动；只改 ``tools`` 里
type 以 ``web_search_preview`` 开头的项，换成干净的 ``{"type":"web_search"}``
（preview 专属字段如 ``search_context_size`` / ``user_location`` 一并丢掉，x.ai 不保证认）。
已有 ``web_search`` 时不重复追加。钩子自身出错原样放行。
"""
import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("grok_web_search_preview_rename")

_SUB2API_MARKER = "sub2api"
_PREVIEW_PREFIX = "web_search_preview"


def _api_base(kwargs: dict) -> str:
    for src in (kwargs, kwargs.get("litellm_params") or {}):
        if isinstance(src, dict) and src.get("api_base"):
            return str(src["api_base"])
    return ""


def rename_tools(tools: Any) -> int:
    """原地改写 tools 列表，返回改了几项。不是列表就不动。"""
    if not isinstance(tools, list):
        return 0
    has_native = any(isinstance(t, dict) and t.get("type") == "web_search" for t in tools)
    changed = 0
    out = []
    for t in tools:
        if isinstance(t, dict) and str(t.get("type") or "").startswith(_PREVIEW_PREFIX):
            changed += 1
            if has_native:
                continue          # 已有原生 web_search，preview 这项直接去重
            out.append({"type": "web_search"})
            has_native = True
            continue
        out.append(t)
    if changed:
        tools[:] = out
    return changed


class GrokWebSearchPreviewRename(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any) -> Any:
        try:
            if isinstance(kwargs, dict) and _SUB2API_MARKER in _api_base(kwargs):
                n = rename_tools(kwargs.get("tools"))
                if n:
                    _log.warning("grok_web_search_preview_rename: renamed=%d model=%s", n, kwargs.get("model"))
        except Exception as exc:  # 防御：改不动也绝不能拦请求
            _log.warning("grok_web_search_preview_rename error: %r", exc)
        return kwargs


grok_web_search_preview_rename_instance = GrokWebSearchPreviewRename()
grok_web_search_preview_rename = grok_web_search_preview_rename_instance

# litellm-198-lane-callback-add.py verify 认这一行判「已加载」
_log.warning("grok_web_search_preview_rename: installed (gate api_base contains %r)", _SUB2API_MARKER)
