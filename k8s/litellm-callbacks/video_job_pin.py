"""视频查进度/下载必须回到提交它的那个成员 pod（2026-10-31）。

问题（三段式）
--------------
- **假设**：`web-gemini-video` 扩到 40 腿后，一把 key 同时提交多个视频，只有一个能查得回来。
- **证伪条件**：3 路并发提交如果都能查到 `completed` 并下载到 mp4，假设就不成立。
- **数据**（10-31 02:58，经公开名 `gemini-video` → `web-gemini-video`，一把 key 3 路并发）：
  提交落在 **3 个不同成员**（alisawainwright612 / leamonquaranta / linsenmeyerrabisuge，
  SpendLogs `model_id` 三个值），但**只有 1 个**能查到 `completed`，另外 2 个在第一次或第二次
  轮询就 `404`。零额度复测把机制钉死了：同一把 key、**两个不同的 video id** 交替各打 5 次，
  10/10 全落在**同一条腿**（`breutzmannmcinally`）；20 次单 id 轮询同样 1 条腿。

机制
----
1. 视频 job 只存在**提交它的那个 pod 的进程内存**里（`gemini_video.go` `videoJobStore`，
   `map[string]*videoJobEntry`，没有任何共享存储）。
2. `GET /v1/videos/{id}` 请求里没有模型名。LiteLLM 从 video id 里解出的是
   **上游模型名**（`model_id:gemini-advanced`，`transform_video_create_response` 传进去的是
   `model` 而不是 deployment id），`resolve_model_name_from_model_id` 把它反查成**组名**
   `web-gemini-video`。
3. 于是轮询就是一次普通的组路由 ⇒ 由 `weighted_affinity` 的 pin 决定落哪条腿，
   而 pin 是 **(组, key) → 一条腿**，`WEIGHTED_AFFINITY_TTL=3600` 且活跃就滑动续期。
   ⇒ 一把 key 在一小时里只能查回**一条腿**上的 job；它并发提交到别的腿上的视频**永久查不回来**
   （额度已经花掉）。扩号前只有 1 条腿，这个缺陷不可见。

做法
----
提交成功时把**真实 deployment id** 记进缓存（`video_job_pin:<去前缀的 video id>` → model_id），
轮询/下载/删除时把它取出来写进 `data["model"]`。router 支持直接拿 deployment id 当 model 路由
（`router.py`: `elif model not in self.model_names and self.has_model_id(model)` →
`get_deployment(model_id=model)`，单腿路由），10-31 零额度实测：连点 3 个不同成员 id，
`SpendLogs` 里 3 条腿各命中 1 次，不串台。

- 缓存走 router 的 **Redis**（和 weighted_affinity 同一条解析链），跨 pod/worker 一致；
  Redis 拿不到时退回进程内 dict（单 worker 仍有效，多 worker 退化为现状，不会更差）。
- TTL 默认 24h（`VIDEO_JOB_PIN_TTL`）：远大于一次生成（实测 80~300s），短于 job 在 pod 里的保留期。
- **查不到 pin 就什么都不做**（老 id、Redis 挂了、别的供应商的视频）⇒ 退回现在的行为，不会把
  原本能查的查成不能查。
- 只认 `model_id` 在 router 当前 deployment 表里存在的值；成员被 `remove` 掉后 pin 自动失效。
- kill switch：`VIDEO_JOB_PIN_DISABLED=1`。

🔴 它不改权限：`data["model"]` 是在鉴权**之后**的 `async_pre_call_hook` 里改的，
`video_id_alias_auth` 那一套（按对外名验权）照旧先跑完。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

from litellm.integrations.custom_logger import CustomLogger

# 🔴 必须用 litellm 自己的 logger，不能用 logging.getLogger("...") 自建的。
#    11-01 实测：自建 logger 在服务进程里没有 handler ⇒ 这个模块的所有 info 日志
#    **一行都不出现**，而 `weighted_affinity` 用 verbose_router_logger 就能打出
#    `initialized`。当时 4/4 副本的 `installed` 都是 0，看着像补丁没装上，
#    实际是日志被丢了（import 和两个 hook 都正常、功能实测 10/10 命中原腿）。
#    ⇒ 验收判据「日志里 installed ≥ 1 行」要能成立，就得挂在 litellm 配好的 logger 上。
try:
    from litellm._logging import verbose_router_logger as _log
except Exception:  # 上游挪了位置也不要让补丁崩
    _log = logging.getLogger("litellm.video_job_pin")

_PREFIX = "video_job_pin:v1:"
_SUBMIT_CALL_TYPES = {"avideo_generation", "video_generation", "create_video", "acreate_video"}
_LOOKUP_CALL_TYPES = {"avideo_status", "video_retrieve", "avideo_content", "video_content",
                      "avideo_remix", "avideo_delete", "video_delete"}


def _disabled() -> bool:
    return os.environ.get("VIDEO_JOB_PIN_DISABLED") == "1"


def _ttl() -> int:
    try:
        return max(60, int(os.environ.get("VIDEO_JOB_PIN_TTL", "86400")))
    except ValueError:
        return 86400


class _MemoryTTL:
    """进程内兜底缓存。多 worker 下只在本 worker 内有效 —— 退化为现状，不会更差。"""

    def __init__(self) -> None:
        self._d: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    async def async_get_cache(self, key: str, **_: Any) -> Optional[Any]:
        with self._lock:
            item = self._d.get(key)
            if not item:
                return None
            exp, val = item
            if exp < time.time():
                self._d.pop(key, None)
                return None
            return val

    async def async_set_cache(self, key: str, value: Any, ttl: Optional[int] = None, **_: Any) -> None:
        with self._lock:
            self._d[key] = (time.time() + (ttl or 0), value)


class VideoJobPin(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self._memory = _MemoryTTL()

    # ---------------- cache / router plumbing (same resolution chain as weighted_affinity)
    @property
    def _router(self):
        try:
            from litellm.proxy.proxy_server import llm_router
            return llm_router
        except Exception:
            return None

    @property
    def _cache(self):
        r = self._router
        for cand in (getattr(getattr(r, "cache", None), "redis_cache", None),
                     getattr(r, "cache", None)):
            if cand is not None and hasattr(cand, "async_get_cache"):
                return cand
        return self._memory

    @staticmethod
    def _raw_video_id(encoded: str) -> Optional[str]:
        """提交时返回的 id 是 base64 包过的；查进度时传回来的也是同一个串。
        用**解码后的上游 id** 做 key，这样两边一定对得上（包装里带模型名，解码后只剩 job id）。"""
        try:
            from litellm.types.videos.utils import extract_original_video_id
            out = extract_original_video_id(encoded)
            return str(out) if out else None
        except Exception:
            return None

    def _known_deployment(self, model_id: str) -> bool:
        r = self._router
        try:
            return bool(model_id) and bool(r) and r.has_model_id(str(model_id))
        except Exception:
            return False

    # ---------------- 1) 查进度/下载：把 pin 取出来，钉死那条腿
    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type: str):
        if _disabled() or not isinstance(data, dict):
            return None
        if call_type not in _LOOKUP_CALL_TYPES:
            return None
        vid = data.get("video_id")
        if not isinstance(vid, str) or not vid:
            return None
        raw = self._raw_video_id(vid)
        if not raw:
            return None
        try:
            pinned = await self._cache.async_get_cache(key=_PREFIX + raw)
        except Exception as e:
            _log.debug("video_job_pin: cache get failed: %s", e)
            return None
        model_id = pinned.get("model_id") if isinstance(pinned, dict) else pinned
        if not isinstance(model_id, str) or not self._known_deployment(model_id):
            return None
        data["model"] = model_id          # router 支持 deployment id 直路由
        data["_video_job_pin"] = model_id  # 只为日志/排障留痕
        _log.info("video_job_pin: %s -> deployment %s (call_type=%s)", raw[:12], model_id, call_type)
        return None

    # ---------------- 2) 提交成功：记下它真正落在哪条腿
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        if _disabled():
            return
        try:
            call_type = (kwargs.get("call_type") or kwargs.get("original_function") or "")
            if call_type not in _SUBMIT_CALL_TYPES:
                return
            model_id = None
            for src in (kwargs.get("litellm_params") or {}, kwargs):
                md = src.get("metadata") or src.get("litellm_metadata") or {}
                if isinstance(md, dict) and md.get("model_info"):
                    model_id = (md["model_info"] or {}).get("id")
                    if model_id:
                        break
            if not model_id:
                md = (kwargs.get("litellm_params") or {}).get("model_info") or {}
                model_id = md.get("id")
            if not model_id or not self._known_deployment(str(model_id)):
                return
            vid = getattr(response_obj, "id", None) or (
                response_obj.get("id") if isinstance(response_obj, dict) else None)
            raw = self._raw_video_id(vid) if isinstance(vid, str) else None
            if not raw:
                return
            await self._cache.async_set_cache(_PREFIX + raw, {"model_id": str(model_id)}, ttl=_ttl())
            _log.info("video_job_pin: stored %s -> %s", raw[:12], model_id)
        except Exception as e:
            _log.debug("video_job_pin: store failed: %s", e)


proxy_handler_instance = VideoJobPin()

# 🔴 启动即打一行，否则验收只能核 `sha256sum /app/video_job_pin.py`，而 sha 只证明
#    **文件装载了**、不证明**这个 callback 被 config 真的加载了**（文件在、但 callbacks 列表里
#    漏了那一行时，两者在 sha 这把尺子下完全同形）。skill litellm-198-router-patch 的验收判据
#    第 2 条就是为此存在：日志里 `installed` ≥ 1 行，逐副本核，不抽样。
_log.info("video_job_pin: installed (disabled=%s, ttl=%ss)",
          _disabled(), os.environ.get("VIDEO_JOB_PIN_TTL", "86400"))
