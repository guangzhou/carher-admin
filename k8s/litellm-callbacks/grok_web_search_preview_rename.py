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

10-16 补 ``tool_choice``: Claude Code 的 WebSearch 经 Cursor key 发
``tool_choice={"type":"web_search"}``（指名内置工具），x.ai 回 422
``tool_choice: data did not match any variant of untagged enum ModelToolChoice``，
经 sub2api 包成 502、LiteLLM 落兜底。10-15 01:12~01:16 一把 key 33/33 全失败。
A/B（sa-grok-4.7，同一 web_search+filters 工具，关重试关兜底）：
  {"type":"web_search"} → 422 ×3      "auto" → 200 ×3（真搜 1~2 次，答对）
  "required"            → 200         不带   → 200
改成 ``"required"``：保留「必须调工具」的原意，tools 里只有搜索时等价指名。
function 型的 ``{"type":"function","name":..}`` 不动（那是 x.ai 认的变体）。
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


def normalize_tool_choice(kwargs: dict) -> bool:
    """``tool_choice`` 指名内置搜索（``{"type":"web_search[_preview…]"}``）→ ``"required"``。"""
    tc = kwargs.get("tool_choice")
    if isinstance(tc, dict) and str(tc.get("type") or "").startswith("web_search"):
        kwargs["tool_choice"] = "required"
        return True
    return False


class GrokWebSearchPreviewRename(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any) -> Any:
        try:
            if isinstance(kwargs, dict) and _SUB2API_MARKER in _api_base(kwargs):
                n = rename_tools(kwargs.get("tools"))
                tc = normalize_tool_choice(kwargs)
                if n or tc:
                    _log.warning("grok_web_search_preview_rename: renamed=%d tool_choice_fixed=%s model=%s",
                                 n, tc, kwargs.get("model"))
        except Exception as exc:  # 防御：改不动也绝不能拦请求
            _log.warning("grok_web_search_preview_rename error: %r", exc)
        return kwargs


grok_web_search_preview_rename_instance = GrokWebSearchPreviewRename()
grok_web_search_preview_rename = grok_web_search_preview_rename_instance

# litellm-198-lane-callback-add.py verify 认这一行判「已加载」
_log.warning("grok_web_search_preview_rename: installed (gate api_base contains %r)", _SUB2API_MARKER)
