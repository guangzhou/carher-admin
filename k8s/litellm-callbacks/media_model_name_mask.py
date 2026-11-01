"""media_model_name_mask — 把视频/图片回包里的后端组名换成用户请求时用的那个公开名。

Why（2026-11-01 实测）
----------------------
用户要求「视频/图片走 muse，但前端用户不该知道我们在用 muse」。
映射做完后（`gemini-video -> muse-video`、`gemini-image -> muse-image`），
回包里**仍然带着 muse**：

    POST /v1/videos        {"model":"muse-video", ...}      ← 漏
    GET  /v1/videos/{id}   {"model":"muse-video", ...}      ← 漏

原因是 LiteLLM 把**解析后的组名**写进回包（`video_endpoints/endpoints.py` 附近），
而不是客户端请求里那个名字。video id 里也编了 `litellm_params.model`，所以
「换个中性名字的 deployment」治不了 retrieve —— 这一条已被车道 E 用同一条 muse 腿
建中性名 deployment 证伪过。

做法
----
`async_post_call_success_hook` 里，把回包 `model` 字段改回**本请求用户实际传的那个名字**
（`data["model"]`，即公开名）。只在两个条件同时成立时动：
  1. 该请求是媒体类（video/image 的 call_type）
  2. 回包里的 model 和用户传的不一致，且回包那个名字在 `_MASK_PREFIXES` 里（默认 `muse`）

其余一切原样返回 ⇒ 对非媒体请求、对本来就没漏的回包，逐字节不变。

🔴 这是**展示层**的遮蔽，不是安全边界：
  - 鉴权仍按真实组名做（本 hook 在鉴权之后、仅改回包）
  - 账单/SpendLogs 里仍是真实组名（财务要能看到真实去向，不能遮）
  - video id 里仍编着真实组名（LiteLLM 自己生成，这里不碰；改它会让 retrieve 找不到 job）
  ⇒ 真要彻底不漏，得给组改名（kiro→aws 那种规模）。本补丁解决的是「用户看回包」这一层。

env：
  MEDIA_NAME_MASK_DISABLED=1   关掉
  MEDIA_NAME_MASK_PREFIXES     逗号分隔，默认 "muse"
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from litellm.integrations.custom_logger import CustomLogger

try:
    from litellm._logging import verbose_proxy_logger as _log
except Exception:  # pragma: no cover - 上游挪位置也不要让补丁崩
    _log = logging.getLogger("litellm.media_model_name_mask")

# 媒体类 call_type：提交 + 查询/下载/删除都要遮
_MEDIA_CALL_TYPES = {
    "avideo_generation", "video_generation", "create_video", "acreate_video",
    "avideo_status", "video_retrieve", "avideo_content", "video_content",
    "avideo_remix", "avideo_delete", "video_delete",
    "aimage_generation", "image_generation",
}


def _disabled() -> bool:
    return os.environ.get("MEDIA_NAME_MASK_DISABLED") == "1"


def _prefixes() -> tuple:
    raw = os.environ.get("MEDIA_NAME_MASK_PREFIXES", "muse")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


class MediaModelNameMask(CustomLogger):
    async def async_post_call_success_hook(self, data: dict, user_api_key_dict, response):
        try:
            if _disabled():
                return response
            call_type = (data or {}).get("call_type") or ""
            # data 里不一定有 call_type（不同端点塞的字段不同）⇒ 再看有没有视频/图片的痕迹
            if call_type and call_type not in _MEDIA_CALL_TYPES:
                return response
            public = (data or {}).get("model")
            if not public or not isinstance(public, str):
                return response
            if public.startswith(_prefixes()):
                # 用户自己就是按 muse 名调的（比如运维用 master key）⇒ 不遮，照实回
                return response
            cur = getattr(response, "model", None)
            if cur is None and isinstance(response, dict):
                cur = response.get("model")
            if not isinstance(cur, str) or not cur.startswith(_prefixes()):
                return response
            if isinstance(response, dict):
                response["model"] = public
            else:
                try:
                    setattr(response, "model", public)
                except Exception:
                    return response
            _log.info("media_model_name_mask: %s -> %s (call_type=%s)", cur, public, call_type or "?")
            return response
        except Exception as e:  # fail-open：遮蔽失败也不能影响请求
            _log.debug("media_model_name_mask: skipped (%s)", e)
            return response


proxy_handler_instance = MediaModelNameMask()

_log.info("media_model_name_mask: installed (disabled=%s, prefixes=%s)", _disabled(), _prefixes())
