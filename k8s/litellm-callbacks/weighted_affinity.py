"""
weighted_affinity.py — LiteLLM v1.90 自定义路由 Hook（v3：session 亲和 + Redis 共享 + 滑动 TTL）

目标：替代内置 `deployment_affinity`，实现「weight 加权的首次分配 + 会话黏性」，
最大化上游（ChatGPT org 级）prompt cache 命中率。

本 Hook 的行为
--------------
黏性键（按优先级）：
1. **session 级**：请求体带 `prompt_cache_key`（Codex CLI 每个会话发一个 UUID）或
   metadata 带 `session_id`（`x-litellm-session-id` 头）时，黏性键 =
   (model_group, user_key_hash, session_fp)。同一会话所有请求钉同一 acct；
   同一用户的**不同会话**各自独立加权选台 → 负载在会话粒度上分散。
2. **key 级兜底**：没有 session 指纹的客户端（chat-completions 路径等）退回
   (model_group, user_key_hash)，行为同 v2。

- **affinity MISS**：按 `litellm_params.weight` 加权随机选 1 台，写缓存并 `return [该台]`。
- **affinity HIT**：返回缓存钉的那台，并**滑动续期**（重写 TTL）——活跃会话永不过期，
  只有 idle 超过 TTL 才重新摇号。（v2 不续期，pin 创建 120s 后必过期重摇，是
  「同 session 落点随机」的根因之一。）
- **无 user_key / 无法决策**：原样返回，不做过滤。

缓存后端（v3 修复重点）
----------------------
v2 的解析链 `litellm.cache → 内存 dict` 在 198 生产退化为**每 worker 一份内存缓存**
（litellm_settings 没配全局 cache），4 pod × 2 worker = 8 份互相矛盾的 pin，同一
用户 30 秒内被 4 个不同 acct 接走（2026-08-12 实测）。v3 优先取 router 的
RedisCache（router_settings.redis_host 已配）：
1. 构造注入的 cache
2. `litellm.proxy.proxy_server.llm_router.cache.redis_cache`（纯 Redis，跨 pod/worker
   强一致；刻意绕过 DualCache 的 in-memory 层，避免 failover 改 pin 后其它 worker
   读到本地陈旧 pin）
3. `llm_router.cache`（DualCache，redis 未配时）
4. `litellm.cache`
5. 内存 dict（永不崩兜底，仅单 worker 黏性）

接口约束（v1.90.2 源码已确认）
------------------------------
- 继承 `litellm.integrations.custom_logger.CustomLogger`。
- 实现 `async def async_filter_deployments(self, model, healthy_deployments,
  messages=None, request_kwargs=None, parent_otel_span=None) -> List[dict]`。
- `healthy_deployments[i]["model_info"]["id"]` = 唯一 deployment id（= 我们说的 model_id）。
- `healthy_deployments[i]["litellm_params"]["weight"]` = int（可能缺失，缺省视为 1）。
- user_api_key 从 `request_kwargs` 的 metadata 里取（`metadata` 或 `litellm_metadata`
  下的 `user_api_key_hash`，已是 sha256）。
- `/v1/responses` 路径请求体顶层字段（`previous_response_id`、`prompt_cache_key`）
  原样出现在 `request_kwargs` 顶层（proxy 端点 `llm_router.aresponses(**data)` 直传）。
"""

import hashlib
import os
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, cast

from litellm._logging import verbose_router_logger
from litellm.integrations.custom_logger import CustomLogger, Span
from litellm.types.llms.openai import AllMessageValues


# ---------------------------------------------------------------------------
# 内存 TTL 缓存（DualCache 不可用时的 fallback）
# ---------------------------------------------------------------------------
class _InMemoryTTLCache:
    """
    进程内 dict + 时间戳，实现最小可用的 TTL 缓存。

    注意：这是「单进程」缓存。LiteLLM proxy 若跑多 worker（gunicorn/uvicorn workers>1），
    每个 worker 各有一份，黏性只在 worker 内生效。生产环境优先用 DualCache（Redis 后端）
    才能跨 worker 共享。本 fallback 仅保证「有 Redis 拿 Redis，没 Redis 也不崩、单 worker
    内仍有黏性」。

    接口刻意对齐 DualCache 的 async_get_cache / async_set_cache，便于上层统一调用。
    """

    def __init__(self) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}  # key -> (expire_at_epoch, value)
        self._lock = threading.Lock()

    async def async_get_cache(self, key: str, **kwargs: Any) -> Optional[Any]:
        now = time.time()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expire_at, value = item
            if expire_at < now:
                # 惰性过期
                self._store.pop(key, None)
                return None
            return value

    async def async_set_cache(
        self, key: str, value: Any, ttl: Optional[int] = None, **kwargs: Any
    ) -> None:
        expire_at = time.time() + (ttl if ttl is not None else 0)
        with self._lock:
            self._store[key] = (expire_at, value)


class WeightedAffinityRouter(CustomLogger):
    """
    weight 加权首次分配 + 会话黏性 的路由 Hook。

    作为 proxy callback 挂载：`callbacks: ["weighted_affinity.proxy_handler_instance"]`
    """

    CACHE_KEY_PREFIX = "weighted_affinity:v2"
    FAIL_MARK_KEY_PREFIX = "weighted_affinity:fail:v1"
    DETOUR_KEY_PREFIX = "weighted_affinity:detour:v1"

    # ---- [2026-08-13] 故障标记分类（修「悬挂黑洞」）----
    # 背景：CrashLoop/悬挂型 zerokey 成员失败节奏 ~1次/90s，永远凑不够
    # allowed_fails=3/min 的冷却阈值 → 成员始终"健康" → 亲和 pin 反复把
    # session 送回黑洞（实测 3×180s 全超时零换号）。
    # 修法：本 hook 自己听失败事件（async_log_failure_event 每次尝试都触发），
    # 传输/服务端类故障给 deployment 打短 TTL 标记；选路时 pin 命中带标记
    # 成员 → 立即 re-pick 迁移，MISS 加权选台也避开带标记成员。
    # 分类红线（同 08-10 sob 哲学）：4xx 客户端/鉴权/内容类**绝不标记**——
    # 否则一个坏请求会把无辜 deployment 上的所有 session 都赶走（缓存踩踏）。
    _NEVER_MARK_SUBSTRINGS = (
        "BadRequest",
        "Authentication",
        "PermissionDenied",
        "NotFound",
        "ContentPolicy",
        "InvalidRequest",
        "UnprocessableEntity",
        "UnsupportedParams",
        # 2026-08-15：请求级失败 ≠ 节点故障。gpt 长会话兜底落 deepseek 时，
        # 超大 body 被上游边缘 openresty 拒收（413 HTML）被 litellm 包成
        # APIError（命中 TRANSIENT 的 "APIError"），把 deepseek deployment
        # 误标 180s，殃及其他用户的兜底。NEVER 先于 TRANSIENT 判定，故在
        # 此排除。ContextWindowExceeded 通常带 BadRequestError 文本已被
        # 排除，仍显式列出防止上游改包装。
        "Request Entity Too Large",
        "ContextWindowExceeded",
        "maximum context length",
        # 2026-08-16：responses_aclose.py 的中断流补记账会以 APIError(499,
        # "AbortedStream: ...") 走 failure 路径落 SpendLogs。客户端取消是
        # 请求级事件，与节点健康无关 —— 绝不能因此标记 deployment
        # （否则用户一次 Ctrl-C 就赶走该 acct 上所有 session，缓存踩踏）。
        "AbortedStream",
    )
    _TRANSIENT_MARK_SUBSTRINGS = (
        "Timeout",
        "APIConnection",
        "ConnectionError",
        "InternalServerError",
        "ServiceUnavailable",
        "RateLimit",
        "MidStreamFallback",
        "APIError",
    )

    # ---- [2026-08-16] 配额撞顶单独判（修 fallback 风暴）----
    # codex 上游 7d 窗口打满时返回 429 body:
    #   {"error":{"type":"usage_limit_reached","message":"The usage limit has
    #    been reached","resets_in_seconds":534428,...}}
    # 原逻辑把它当 TRANSIENT("RateLimit") 只标 180s → 撞顶号每 3 分钟回池
    # 挨打一轮。2026-08-15/16 实测 17 个撞顶号 + 4 proxy 副本打出 ~66 次/分钟
    # 的 fallback 风暴（storm 采样: 15min 内 984 次 "Falling back"）。
    # 修法：命中下面签名 → 标记 TTL 直接用 body 里的 resets_in_seconds
    # （+60s 缓冲，封顶 7d）；解析不到时退 env
    # WEIGHTED_AFFINITY_QUOTA_MARK_TTL（默认 6h，quota 引擎 scale0 是
    # 更持久的兜底）。签名足够特异（codex backend 专属 JSON），且撞顶号上
    # 任何请求都必失败，不存在"殃及无辜"，故先于 NEVER 判定。
    _QUOTA_CAP_SUBSTRINGS = (
        "usage_limit_reached",
        "The usage limit has been reached",
    )
    # 生产实测（2026-08-16 acct-204 SpendLogs error_information）：异常是双层
    # 包裹的转义 JSON —— `\"resets_in_seconds\":534428`（引号前带反斜杠），
    # 裸引号正则匹配不上会静默退 6h 默认 TTL。故引号/反斜杠都按可选杂质处理。
    _QUOTA_RESET_RE = re.compile(r'resets_in_seconds[\\"\s]*:[\\"\s]*(\d+)')

    def __init__(
        self,
        cache: Optional[Any] = None,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """
        Args:
            cache: DualCache 实例。作为 callback 挂载时通常拿不到 router 的 DualCache，
                   此时留空，运行期从 router 的 RedisCache / litellm.cache 惰性获取；
                   再拿不到就退化到内存 dict。
            ttl_seconds: 黏性 idle TTL（HIT 会滑动续期）。缺省读 env
                   `WEIGHTED_AFFINITY_TTL`，再缺省 120s。
        """
        super().__init__()
        self._injected_cache = cache
        self._memory_cache = _InMemoryTTLCache()
        self._resolved_backend_name: Optional[str] = None
        if ttl_seconds is None:
            ttl_seconds = int(os.getenv("WEIGHTED_AFFINITY_TTL", "120"))
        self.ttl_seconds = ttl_seconds
        self.fail_mark_ttl = int(os.getenv("WEIGHTED_AFFINITY_FAIL_MARK_TTL", "180"))
        self.quota_mark_ttl_default = int(
            os.getenv("WEIGHTED_AFFINITY_QUOTA_MARK_TTL", "21600")
        )
        # ---- [2026-09-25] 绕行 / 改钉分离（修「一次抖动永久搬家」）----
        # pin 指向的号只要有一瞬间不可用（router cooldown 60s、或本次请求内被
        # weighted-failover 排除），旧实现走 MISS 分支**改写 pin**，号恢复后没有
        # 任何回归逻辑 → 一天下来同一个会话被搬 31 次，每次砸掉整份前缀缓存
        # （实测 cursor-buyitian-pwcf：单 session 2451 req / 31 个 acct / 24h，
        #  prompt_tokens 跨切换点连续增长，即会话中途换号）。
        # 现在：临时不可用 → 绕行（原 pin 保留，绕行目标自身也复用，避免绕行期
        # 间每请求重摇把缓存碎得更细）；持续不可用超过 detour_repin_after 才
        # 真正改钉，避开「钉的号永久死了却死守」的反效果。
        self.detour_repin_after = int(
            os.getenv("WEIGHTED_AFFINITY_DETOUR_REPIN_AFTER", "300")
        )
        verbose_router_logger.info(
            "WeightedAffinityRouter: initialized (ttl=%ss, injected_cache=%s, "
            "quota_mark_ttl_default=%ss, detour_repin_after=%ss)",
            self.ttl_seconds,
            "yes" if cache is not None else "no",
            self.quota_mark_ttl_default,
            self.detour_repin_after,
        )

    # ------------------------------------------------------------------
    # 缓存后端解析
    # ------------------------------------------------------------------
    @property
    def cache(self) -> Any:
        """
        缓存后端优先级：
          1. 构造时注入的实例
          2. router 的 RedisCache（`llm_router.cache.redis_cache`）——纯 Redis，
             跨 pod/worker 强一致。刻意绕过 DualCache 的 in-memory 层：failover
             re-pick 改写 pin 后，其它 worker 的本地层会继续吐陈旧 pin 直到 TTL 过期。
          3. router 的 DualCache（redis 未配时聊胜于无）
          4. `litellm.cache`（proxy 配了 cache: type: redis 时全局可用）
          5. 内存 dict fallback（保证永不为 None，import/运行都不崩）

        每次访问都惰性解析，避免 import 期 proxy router 尚未初始化的时序问题。
        首次解析成功打一条 INFO 注明落在哪个后端（看门狗可断言 backend=redis）。
        """
        if self._injected_cache is not None:
            return self._injected_cache
        try:
            from litellm.proxy.proxy_server import llm_router

            router_cache = getattr(llm_router, "cache", None)
            if router_cache is not None:
                redis_cache = getattr(router_cache, "redis_cache", None)
                if redis_cache is not None:
                    self._log_backend_once("redis")
                    return redis_cache
                self._log_backend_once("dualcache")
                return router_cache
        except Exception:
            pass
        try:
            import litellm

            if getattr(litellm, "cache", None) is not None:
                self._log_backend_once("litellm.cache")
                return litellm.cache
        except Exception:
            pass
        self._log_backend_once("memory")
        return self._memory_cache

    def _log_backend_once(self, name: str) -> None:
        if self._resolved_backend_name != name:
            self._resolved_backend_name = name
            verbose_router_logger.info(
                "WeightedAffinityRouter: cache backend resolved -> %s", name
            )

    # ------------------------------------------------------------------
    # user_key 提取（兼容 metadata / litellm_metadata 两处，照抄内置写法）
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_metadata_dicts(request_kwargs: dict) -> List[dict]:
        """
        返回 request 上所有可用的 metadata dict。

        不同 endpoint 下 Router 可能把元数据放在 `metadata` 或 `litellm_metadata`；
        用户也可能两处都传，所以两处都检查（而不是 `or` 短路只看一处）。
        """
        metadata_dicts: List[dict] = []
        for key in ("litellm_metadata", "metadata"):
            md = request_kwargs.get(key)
            if isinstance(md, dict):
                metadata_dicts.append(md)
            # 有些调用路径把 metadata 嵌在 litellm_params 下
            lp = request_kwargs.get("litellm_params")
            if isinstance(lp, dict):
                nested = lp.get(key)
                if isinstance(nested, dict):
                    metadata_dicts.append(nested)
        return metadata_dicts

    @staticmethod
    def _get_user_key_from_request_kwargs(request_kwargs: dict) -> Optional[str]:
        """
        从 request kwargs 提取稳定的 affinity key（proxy 侧的 API key hash）。

        来源：`metadata.user_api_key_hash`（已是 sha256）。
        注意：OpenAI 的 `user` 参数是终端用户标识，刻意不用于 deployment 黏性。
        """
        for metadata in WeightedAffinityRouter._iter_metadata_dicts(request_kwargs):
            user_key = metadata.get("user_api_key_hash")
            if user_key is not None:
                return str(user_key)
        return None

    @staticmethod
    def _get_session_fingerprint(request_kwargs: dict) -> Optional[str]:
        """
        提取会话指纹（优先级）：
        1. 请求体顶层 `prompt_cache_key` —— Codex CLI 每个会话发一个 UUID，
           /v1/responses 路径 proxy 直传 kwargs（与 previous_response_id 同层）。
        2. metadata / litellm_metadata 的 `session_id` —— proxy 从
           `x-litellm-session-id` / `x-litellm-trace-id` 头填充。

        两者都没有 → None，退回 key 级黏性。
        """
        pck = request_kwargs.get("prompt_cache_key")
        if isinstance(pck, str) and pck:
            return pck
        for metadata in WeightedAffinityRouter._iter_metadata_dicts(request_kwargs):
            session_id = metadata.get("session_id")
            if session_id is not None and str(session_id):
                return str(session_id)
        return None

    # ------------------------------------------------------------------
    # model_map_key 派生（照抄内置：只在整组 key 稳定一致时才用于 scoping）
    # ------------------------------------------------------------------
    @staticmethod
    def _get_model_map_key_from_litellm_model_name(
        litellm_model_name: str,
    ) -> Optional[str]:
        if not litellm_model_name:
            return None
        if "/" not in litellm_model_name:
            return litellm_model_name
        provider_prefix, remainder = litellm_model_name.split("/", 1)
        if provider_prefix == "azure":
            # azure/ 后面常是 per-deployment 名，不稳定，跳过
            return None
        return remainder

    @staticmethod
    def _get_model_map_key_from_deployment(deployment: dict) -> Optional[str]:
        model_name = deployment.get("model_name")
        if isinstance(model_name, str) and model_name:
            return model_name

        model_info = deployment.get("model_info")
        if isinstance(model_info, dict):
            base_model = model_info.get("base_model")
            if isinstance(base_model, str) and base_model:
                return base_model

        litellm_params = deployment.get("litellm_params")
        if isinstance(litellm_params, dict):
            base_model = litellm_params.get("base_model")
            if isinstance(base_model, str) and base_model:
                return base_model
            litellm_model_name = litellm_params.get("model")
            if isinstance(litellm_model_name, str) and litellm_model_name:
                return WeightedAffinityRouter._get_model_map_key_from_litellm_model_name(
                    litellm_model_name
                )
        return None

    @staticmethod
    def _get_stable_model_map_key_from_deployments(
        healthy_deployments: List[dict],
    ) -> Optional[str]:
        """
        只有当整组 deployment 派生出的 model_map_key 完全一致时才返回它。
        否则返回 None（表示 scoping 不稳定，本 Hook 放弃决策，原样返回）。
        """
        if not healthy_deployments:
            return None
        keys: List[str] = []
        for deployment in healthy_deployments:
            key = WeightedAffinityRouter._get_model_map_key_from_deployment(deployment)
            if key is None:
                return None
            keys.append(key)
        unique_keys = set(keys)
        if len(unique_keys) != 1:
            return None
        return keys[0]

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_sha256_hex(value: str) -> bool:
        if len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return True

    @classmethod
    def _hash_user_key(cls, user_key: str) -> str:
        """
        proxy 通常已给 sha256（user_api_key_hash），直接沿用避免二次哈希、便于关联排查；
        万一拿到的是原始 key，再本地 sha256，避免把明文写进缓存 key/日志。
        """
        if cls._looks_like_sha256_hex(user_key):
            return user_key.lower()
        return hashlib.sha256(user_key.encode("utf-8")).hexdigest()

    @classmethod
    def get_affinity_cache_key(
        cls, model_group: str, user_key: str, session_fp: Optional[str] = None
    ) -> str:
        """
        session_fp 存在 → session 级键（同一会话钉台，不同会话独立选台）；
        否则 → key 级键（v2 语义）。session_fp 先 sha256 截 32 位：
        指纹可能是任意客户端字符串，避免把原文写进 Redis key / 日志。
        """
        hashed = cls._hash_user_key(user_key)
        if session_fp:
            session_hash = hashlib.sha256(session_fp.encode("utf-8")).hexdigest()[:32]
            return f"{cls.CACHE_KEY_PREFIX}:{model_group}:{hashed}:s:{session_hash}"
        return f"{cls.CACHE_KEY_PREFIX}:{model_group}:{hashed}"

    @staticmethod
    def _shorten_for_logs(value: str, keep: int = 8) -> str:
        return value if len(value) <= keep else f"{value[:keep]}..."

    @staticmethod
    def _get_model_id(deployment: dict) -> Optional[str]:
        model_info = deployment.get("model_info")
        if not isinstance(model_info, dict):
            return None
        model_id = model_info.get("id")
        return None if model_id is None else str(model_id)

    @staticmethod
    def _get_weight(deployment: dict) -> int:
        """
        读 `litellm_params.weight`，缺失/非法视为 1。weight<=0 归零（不参与加权）。
        """
        litellm_params = deployment.get("litellm_params")
        if not isinstance(litellm_params, dict):
            return 1
        raw = litellm_params.get("weight", 1)
        try:
            w = int(raw)
        except (TypeError, ValueError):
            return 1
        return w if w > 0 else 0

    @staticmethod
    def _find_deployment_by_model_id(
        healthy_deployments: List[dict], model_id: str
    ) -> Optional[dict]:
        for deployment in healthy_deployments:
            if WeightedAffinityRouter._get_model_id(deployment) == str(model_id):
                return deployment
        return None

    # ------------------------------------------------------------------
    # 故障标记（2026-08-13：修悬挂黑洞，见类头注释）
    # ------------------------------------------------------------------
    @classmethod
    def get_fail_mark_key(cls, model_id: str) -> str:
        return f"{cls.FAIL_MARK_KEY_PREFIX}:{model_id}"

    @classmethod
    def get_detour_key(cls, affinity_cache_key: str) -> str:
        """
        绕行记录键 —— 与 pin 一一对应，只换前缀，便于按会话对账／单独 flush。
        刻意**不**复用 pin 的键空间：`litellm-wa-flush-affinity.py` 按
        `weighted_affinity:v2:*` 清 pin，绕行记录不该被那个手法连带清掉
        （清了只是丢一次绕行上下文，不影响正确性，但会让统计失真）。
        """
        suffix = affinity_cache_key
        if suffix.startswith(cls.CACHE_KEY_PREFIX + ":"):
            suffix = suffix[len(cls.CACHE_KEY_PREFIX) + 1:]
        return f"{cls.DETOUR_KEY_PREFIX}:{suffix}"

    @staticmethod
    def _get_deployment_id_from_failure_kwargs(kwargs: dict) -> Optional[str]:
        litellm_params = kwargs.get("litellm_params")
        if isinstance(litellm_params, dict):
            model_info = litellm_params.get("model_info")
            if isinstance(model_info, dict) and model_info.get("id"):
                return str(model_info["id"])
        model_info = kwargs.get("model_info")
        if isinstance(model_info, dict) and model_info.get("id"):
            return str(model_info["id"])
        return None

    @classmethod
    def _failure_should_mark(cls, exception: Any) -> bool:
        """
        只标记传输/服务端类故障；4xx 客户端/鉴权/内容类绝不标记。
        未知类型不标记（保守：宁可漏标靠冷却兜底，不误标踩踏无辜成员缓存）。
        """
        if exception is None:
            return False
        text = f"{type(exception).__name__} {exception}"
        if any(s in text for s in cls._NEVER_MARK_SUBSTRINGS):
            return False
        return any(s in text for s in cls._TRANSIENT_MARK_SUBSTRINGS)

    def _failure_mark_ttl(self, exception: Any) -> Optional[Tuple[int, str]]:
        """
        返回 (fail-mark TTL, 原因标签)；None = 不标记。

        [2026-08-16] 配额撞顶（见 _QUOTA_CAP_SUBSTRINGS 注释）优先判定：
        TTL 用 body 的 resets_in_seconds+60（封顶 7d），解析不到退
        quota_mark_ttl_default。其余沿用 _failure_should_mark 的
        NEVER/TRANSIENT 语义，TTL = fail_mark_ttl（短标记）。
        """
        if exception is None:
            return None
        text = f"{type(exception).__name__} {exception}"
        if any(s in text for s in self._QUOTA_CAP_SUBSTRINGS):
            m = self._QUOTA_RESET_RE.search(text)
            if m:
                return (min(int(m.group(1)) + 60, 7 * 86400), "quota-cap")
            return (self.quota_mark_ttl_default, "quota-cap")
        if not self._failure_should_mark(exception):
            return None
        return (self.fail_mark_ttl, "transient")

    async def _is_fail_marked(self, model_id: str) -> bool:
        try:
            return bool(
                await self.cache.async_get_cache(key=self.get_fail_mark_key(str(model_id)))
            )
        except Exception:
            return False

    async def async_log_failure_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        """
        每次上游调用失败（含 router 重试的每一轮）都会触发。
        传输/服务端类故障 → 短 TTL fail-mark（不等 allowed_fails 阈值冷却）；
        配额撞顶 → 长 TTL（到官方 reset），让撞顶号立即退出选路。
        """
        try:
            exception = kwargs.get("exception")
            mark = self._failure_mark_ttl(exception)
            if mark is None:
                return
            ttl, reason = mark
            model_id = self._get_deployment_id_from_failure_kwargs(kwargs)
            if model_id is None:
                return
            await self.cache.async_set_cache(
                self.get_fail_mark_key(model_id), "1", ttl=ttl
            )
            verbose_router_logger.info(
                "WeightedAffinityRouter: fail-marked deployment=%s for %ss (%s, %s)",
                model_id,
                ttl,
                type(exception).__name__,
                reason,
            )
        except Exception:
            # 日志钩子绝不影响主流程
            pass

    # ------------------------------------------------------------------
    # 加权随机选台
    # ------------------------------------------------------------------
    def _weighted_pick(self, healthy_deployments: List[dict]) -> Optional[dict]:
        """
        按 `litellm_params.weight` 做加权随机选 1 台。

        算法：累积权重 + random.uniform 落点。O(n) 一次遍历，不需要密码学随机
        （这是选路，不是安全场景，random 足够）。

        边界：
          - 全部 weight<=0（总权重为 0）→ 退化为等概率 random.choice，不返回 None，
            保证只要有健康台就能选出一台。
        """
        weights = [self._get_weight(d) for d in healthy_deployments]
        total = sum(weights)
        if total <= 0:
            # 全零权重：等概率兜底
            chosen = random.choice(healthy_deployments)
            verbose_router_logger.debug(
                "WeightedAffinityRouter: all weights <=0, fallback to uniform pick -> %s",
                self._get_model_id(chosen),
            )
            return chosen

        r = random.uniform(0, total)
        upto = 0.0
        for deployment, w in zip(healthy_deployments, weights):
            upto += w
            if r <= upto:
                return deployment
        # 浮点兜底（理论上不会走到）
        return healthy_deployments[-1]

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: List,
        messages: Optional[List[AllMessageValues]] = None,
        request_kwargs: Optional[dict] = None,
        parent_otel_span: Optional[Span] = None,
    ) -> List[dict]:
        request_kwargs = request_kwargs or {}
        deployments = cast(List[dict], healthy_deployments)

        # ---- [2026-08-10] 尊重 weighted-failover 的排除名单（修中流竞态）----
        # 同组重挑时 router 在 request_kwargs 上带 _excluded_deployment_ids
        # （router.py 在本 hook **之后**才 pop 并做排除过滤）。若不在这里避开，
        # 当亲和 pin 恰好指向刚失败的账号（其 cooldown 尚未登记完成）时，本 hook
        # 会把候选钉成 [失败账号]，随后 router 的排除过滤把它清空 → 误报
        # "No deployments available"，组里明明还有几十个健康账号却一个没试。
        # 生产事故：2026-08-10 05:11:27 carher-13 (proxy 554cj)，同秒对照请求
        # cursor-xingtianxing 因 cooldown 已登记而重挑成功。只读不 pop —— pop
        # 是 router 的事；全部被排除时保持原样，让 router 报准确错误。
        _excluded_raw = request_kwargs.get("_excluded_deployment_ids")
        if _excluded_raw:
            _excluded = {str(x) for x in _excluded_raw}
            _kept = [
                d
                for d in deployments
                if str(self._get_model_id(d)) not in _excluded
            ]
            if _kept and len(_kept) < len(deployments):
                verbose_router_logger.info(
                    "WeightedAffinityRouter: dropped %d excluded deployment(s) "
                    "(weighted-failover retry) before pin/pick for model=%s",
                    len(deployments) - len(_kept),
                    model,
                )
                deployments = _kept

        # 没有候选或只有 1 台，无需决策
        if not deployments:
            return deployments
        if len(deployments) == 1:
            return deployments

        # ---- 让路：Responses API 续链 ----
        # 有 previous_response_id 时，这是 stateful Responses 续会话，必须钉回生成
        # 原 response 的那台（rs_xxx 加密 reasoning 只能被 originating org decrypt，
        # 跨 acct 会 400 invalid_encrypted_content）。这一维交给内置
        # responses_api_deployment_check 处理，本 hook 原样放行，绝不用 weight 覆盖它。
        # 本 hook 与内置串联调用（router 遍历 litellm.callbacks 链式 filter），
        # 只要 prod 保留内置 responses_api_deployment_check、关掉内置 deployment_affinity，
        # 续链归内置、weighted 首次分配归本 hook，两者各管一维不打架。
        if request_kwargs.get("previous_response_id") is not None:
            verbose_router_logger.debug(
                "WeightedAffinityRouter: previous_response_id present -> yield to "
                "builtin responses_api_deployment_check, returning all %d deployments",
                len(deployments),
            )
            return deployments

        # ---- 让路：候选里有 tag_regex（按 User-Agent 分流的组）----
        # 2026-08-07 实测：本 hook 在 router 里跑在 tag 路由**之前**
        # （router.py:11150 async_callback_filter_deployments -> :11168
        # get_deployments_for_tag）。它把候选钉成 1 台之后，tag 过滤只能在这 1 台
        # 上做取舍，UA 分流直接失效：
        #   * Desktop 用户被钉在 zk 台 -> 那台带 tags:["default"] -> 照样返回
        #     -> 该走 acct 的流量落到了 zerokey（实测 8 发里 5 发）。
        #   * 反过来非 Desktop 被钉在 acct 台 -> 既不匹配 regex 又没有 default
        #     -> 候选清空 -> raise no_deployments_with_tag_routing。
        # 亲和性的缓存键是 (group, user)，没有 UA 这一维，补上也救不了第一发。
        # 所以这种组直接让路，由 tag 路由决定，本 hook 不参与。
        # 对其他组完全无感：全库只有按 UA 分流的那个组带 tag_regex。
        if any(
            isinstance(d, dict)
            and isinstance(d.get("litellm_params"), dict)
            and d["litellm_params"].get("tag_regex")
            for d in deployments
        ):
            verbose_router_logger.info(
                "WeightedAffinityRouter: tag_regex present in candidates for model=%s "
                "-> yield to tag routing, returning all %d deployments",
                model,
                len(deployments),
            )
            return deployments

        # scoping：model_map_key 必须整组稳定一致，否则放弃决策原样返回
        model_group = self._get_stable_model_map_key_from_deployments(deployments)
        if model_group is None:
            verbose_router_logger.debug(
                "WeightedAffinityRouter: unstable model_map_key for model=%s, "
                "returning all %d deployments unchanged",
                model,
                len(deployments),
            )
            return deployments

        # 提取 user_key；没有就无法做黏性 —— 交给下游默认策略
        user_key = self._get_user_key_from_request_kwargs(request_kwargs)
        if user_key is None:
            verbose_router_logger.debug(
                "WeightedAffinityRouter: no user_api_key_hash in request "
                "(group=%s), returning all %d deployments unchanged",
                model_group,
                len(deployments),
            )
            return deployments

        # session 指纹（prompt_cache_key / session_id）；None → key 级黏性
        session_fp = self._get_session_fingerprint(request_kwargs)
        cache_key = self.get_affinity_cache_key(model_group, user_key, session_fp)

        # ---- 1) 尝试命中缓存（HIT → 返回钉的台 + 滑动续期）----
        try:
            cache_result = await self.cache.async_get_cache(key=cache_key)
        except Exception as e:
            verbose_router_logger.debug(
                "WeightedAffinityRouter: cache get failed key=%s err=%s", cache_key, e
            )
            cache_result = None

        pinned_model_id: Optional[str] = None
        if isinstance(cache_result, dict):
            pinned_model_id = cast(Optional[str], cache_result.get("model_id"))
        elif isinstance(cache_result, str):
            pinned_model_id = cache_result  # 兼容裸字符串

        # pin 存在但这一刻用不了的原因；None = 本来就没 pin（真 MISS）。
        # 有值 → 下方走「绕行」而不是「改钉」，见 __init__ 里 detour_repin_after 注释。
        pin_unusable_reason: Optional[str] = None

        if pinned_model_id:
            deployment = self._find_deployment_by_model_id(deployments, pinned_model_id)
            if deployment is None:
                # 钉的台不在健康集合（router cooldown，或本次请求内被
                # _excluded_deployment_ids 排除）→ 临时绕行，**不改钉**。
                pin_unusable_reason = "unhealthy"
                verbose_router_logger.info(
                    "WeightedAffinityRouter: pinned deployment=%s not in healthy set "
                    "(group=%s), detouring (pin kept)",
                    pinned_model_id,
                    model_group,
                )
            elif await self._is_fail_marked(pinned_model_id):
                # pin 指向刚报过传输类故障的成员 → 本次迁走（悬挂黑洞修复），
                # 但同样只绕行不改钉：fail-mark 是 180s 的短标记，标记过期后
                # 会话应该回到原号，而不是被永久搬走。
                pin_unusable_reason = "fail-marked"
                verbose_router_logger.info(
                    "WeightedAffinityRouter: pinned deployment=%s fail-marked "
                    "(group=%s), detouring (pin kept)",
                    pinned_model_id,
                    model_group,
                )
                _kept = [
                    d for d in deployments
                    if str(self._get_model_id(d)) != str(pinned_model_id)
                ]
                if _kept:
                    deployments = _kept
            else:
                # 滑动续期：活跃会话的 pin 永不过期，idle 超 TTL 才重摇。
                # 失败不阻断（等同这次没续上）。
                try:
                    await self.cache.async_set_cache(
                        cache_key,
                        {"model_id": pinned_model_id},
                        ttl=self.ttl_seconds,
                    )
                except Exception as e:
                    verbose_router_logger.debug(
                        "WeightedAffinityRouter: sliding-refresh failed key=%s err=%s",
                        cache_key,
                        e,
                    )
                # pin 又能用了 → 清掉绕行记录，让 detour_repin_after 的计时从
                # 下一次真的生病时重新起算（否则陈旧 since 会让日后一次抖动
                # 被误判成「持续不健康」而直接改钉）。
                await self._clear_detour(cache_key)
                verbose_router_logger.info(
                    "WeightedAffinityRouter: HIT group=%s user=%s session=%s -> "
                    "pinned deployment=%s",
                    model_group,
                    self._shorten_for_logs(user_key),
                    "yes" if session_fp else "no",
                    pinned_model_id,
                )
                return [deployment]

        # ---- 2) 选台（避开 fail-marked 成员）----
        chosen = await self._pick_avoiding_fail_marks(deployments)
        if chosen is None:
            # 理论上不会发生（_weighted_pick 有兜底），保险起见原样返回
            return deployments

        chosen_id = self._get_model_id(chosen)
        if chosen_id is None:
            verbose_router_logger.debug(
                "WeightedAffinityRouter: chosen deployment has no model_info.id, "
                "returning all deployments unchanged"
            )
            return deployments

        # ---- 2a) pin 本来就不存在 → 真 MISS：选台 + 写 pin（原语义）----
        if pin_unusable_reason is None:
            try:
                await self.cache.async_set_cache(
                    cache_key,
                    {"model_id": chosen_id},
                    ttl=self.ttl_seconds,
                )
            except Exception as e:
                # 写缓存失败不阻断请求，只是这次没黏上而已
                verbose_router_logger.debug(
                    "WeightedAffinityRouter: cache set failed key=%s err=%s", cache_key, e
                )

            verbose_router_logger.info(
                "WeightedAffinityRouter: MISS group=%s user=%s session=%s -> weighted-pick "
                "deployment=%s weight=%s (total_candidates=%d, ttl=%ss)",
                model_group,
                self._shorten_for_logs(user_key),
                "yes" if session_fp else "no",
                chosen_id,
                self._get_weight(chosen),
                len(deployments),
                self.ttl_seconds,
            )
            return [chosen]

        # ---- 2b) pin 存在但这一刻用不了 → 绕行；只有持续生病才改钉 ----
        return await self._detour(
            cache_key=cache_key,
            model_group=model_group,
            user_key=user_key,
            session_fp=session_fp,
            pinned_model_id=cast(str, pinned_model_id),
            reason=pin_unusable_reason,
            deployments=deployments,
            fresh_pick=chosen,
            fresh_pick_id=chosen_id,
        )

    # ------------------------------------------------------------------
    # 绕行 / 改钉（2026-09-25，见 __init__ 里 detour_repin_after 注释）
    # ------------------------------------------------------------------
    async def _pick_avoiding_fail_marks(
        self, deployments: List[dict]
    ) -> Optional[dict]:
        """weighted 选台，最多重摇 3 次避开 fail-marked 成员；绝不返回空。"""
        pick_pool = list(deployments)
        for _ in range(3):
            candidate = self._weighted_pick(pick_pool)
            if candidate is None:
                break
            candidate_id = self._get_model_id(candidate)
            if (
                candidate_id is not None
                and len(pick_pool) > 1
                and await self._is_fail_marked(candidate_id)
            ):
                pick_pool = [
                    d for d in pick_pool
                    if str(self._get_model_id(d)) != str(candidate_id)
                ]
                continue
            return candidate
        # 兜底：全被标记/重摇耗尽 → 从原始候选里直接选
        return self._weighted_pick(deployments)

    async def _clear_detour(self, cache_key: str) -> None:
        """pin 恢复可用时清掉绕行记录。失败静默 —— 绝不阻断主流程。"""
        try:
            cache = self.cache
            detour_key = self.get_detour_key(cache_key)
            deleter = getattr(cache, "async_delete_cache", None)
            if deleter is not None:
                await deleter(detour_key)
            else:
                # 退化后端（内存 dict / 老 DualCache）没有 delete → 写空值+极短 TTL
                await cache.async_set_cache(detour_key, None, ttl=1)
        except Exception:
            pass

    async def _detour(
        self,
        cache_key: str,
        model_group: str,
        user_key: str,
        session_fp: Optional[str],
        pinned_model_id: str,
        reason: str,
        deployments: List[dict],
        fresh_pick: dict,
        fresh_pick_id: str,
    ) -> List[dict]:
        """
        pin 存在但这一刻不可用时的处置。三种结局：

        - **绕行复用**：已有绕行记录且那台还能用 → 继续用它（绕行期间也保持
          黏性，不然每个请求重摇会把缓存碎得比改钉更细），`since` 不变。
        - **绕行新建**：没有记录 / 记录里那台也不能用了 → 新选一台记下来；
          `since` 沿用已有值（生病是从第一次绕行起算的，不能被刷新，否则
          永久死号会因为一直刷新而永远走不到改钉）。
        - **改钉**：`now - since >= detour_repin_after` → pin 指向的号不是抖动
          而是真的长期不可用，这时才把 pin 改写成替补并清掉绕行记录。
        """
        detour_key = self.get_detour_key(cache_key)
        record: Optional[dict] = None
        try:
            raw = await self.cache.async_get_cache(key=detour_key)
            if isinstance(raw, dict):
                record = raw
        except Exception as e:
            verbose_router_logger.debug(
                "WeightedAffinityRouter: detour get failed key=%s err=%s", detour_key, e
            )

        now = time.time()
        since = now
        if record is not None:
            try:
                since = float(record.get("since", now))
            except (TypeError, ValueError):
                since = now

        sick_for = max(0.0, now - since)

        # ---- 改钉：病得够久，不是抖动 ----
        if record is not None and sick_for >= self.detour_repin_after:
            try:
                await self.cache.async_set_cache(
                    cache_key,
                    {"model_id": fresh_pick_id},
                    ttl=self.ttl_seconds,
                )
            except Exception as e:
                verbose_router_logger.debug(
                    "WeightedAffinityRouter: repin set failed key=%s err=%s",
                    cache_key,
                    e,
                )
            await self._clear_detour(cache_key)
            verbose_router_logger.info(
                "WeightedAffinityRouter: REPIN group=%s user=%s session=%s -> old=%s "
                "unusable for %.0fs (%s, >=%ss), pin moved to %s",
                model_group,
                self._shorten_for_logs(user_key),
                "yes" if session_fp else "no",
                pinned_model_id,
                sick_for,
                reason,
                self.detour_repin_after,
                fresh_pick_id,
            )
            return [fresh_pick]

        # ---- 绕行复用：上次的替补还能用就继续用它 ----
        if record is not None:
            prev_id = record.get("model_id")
            if prev_id:
                prev = self._find_deployment_by_model_id(deployments, str(prev_id))
                if prev is not None and not await self._is_fail_marked(str(prev_id)):
                    try:
                        await self.cache.async_set_cache(
                            detour_key,
                            {"model_id": str(prev_id), "since": since},
                            ttl=max(self.detour_repin_after * 2, 60),
                        )
                    except Exception as e:
                        verbose_router_logger.debug(
                            "WeightedAffinityRouter: detour refresh failed key=%s "
                            "err=%s",
                            detour_key,
                            e,
                        )
                    verbose_router_logger.info(
                        "WeightedAffinityRouter: DETOUR group=%s user=%s session=%s -> "
                        "pin=%s unusable %.0fs (%s), reusing detour=%s (pin kept)",
                        model_group,
                        self._shorten_for_logs(user_key),
                        "yes" if session_fp else "no",
                        pinned_model_id,
                        sick_for,
                        reason,
                        prev_id,
                    )
                    return [prev]

        # ---- 绕行新建：since 沿用已有值，不刷新 ----
        try:
            await self.cache.async_set_cache(
                detour_key,
                {"model_id": fresh_pick_id, "since": since},
                ttl=max(self.detour_repin_after * 2, 60),
            )
        except Exception as e:
            verbose_router_logger.debug(
                "WeightedAffinityRouter: detour set failed key=%s err=%s",
                detour_key,
                e,
            )
        verbose_router_logger.info(
            "WeightedAffinityRouter: DETOUR group=%s user=%s session=%s -> pin=%s "
            "unusable %.0fs (%s), new detour=%s (pin kept, repin_after=%ss)",
            model_group,
            self._shorten_for_logs(user_key),
            "yes" if session_fp else "no",
            pinned_model_id,
            sick_for,
            reason,
            fresh_pick_id,
            self.detour_repin_after,
        )
        return [fresh_pick]


# ---------------------------------------------------------------------------
# 模块级实例：LiteLLM callback 注册引用点
# ---------------------------------------------------------------------------
# config.yaml 里用 `weighted_affinity.proxy_handler_instance` 引用到这个对象。
proxy_handler_instance = WeightedAffinityRouter()


# ===========================================================================
# 如何在 config.yaml 挂载与引用
# ===========================================================================
#
# 1) 把本文件放到 proxy 能 import 到的路径（比如挂进容器的 /app/weighted_affinity.py，
#    并确保该目录在 PYTHONPATH / 当前工作目录下）。
#
# 2) config.yaml 里通过 callbacks 段引用模块级实例：
#
#    litellm_settings:
#      callbacks: ["weighted_affinity.proxy_handler_instance"]
#
#    # 可选：TTL 也可用环境变量控制（缺省 120s）
#    #   environment_variables:
#    #     WEIGHTED_AFFINITY_TTL: "300"
#
# 3) （强烈建议）配 Redis 作为 DualCache 后端，才能跨 worker/多副本共享黏性；
#    否则退化到进程内内存 dict，黏性只在单 worker 内生效：
#
#    litellm_settings:
#      cache: true
#      cache_params:
#        type: redis
#        host: <redis-host>
#        port: 6379
#
#    本 Hook 会自动从 litellm.cache 拿到该 DualCache（见 `cache` property 的解析顺序）。
#
# 4) deployment 上配 weight（决定新会话初始落台的概率分布）：
#
#    model_list:
#      - model_name: my-pool
#        litellm_params:
#          model: openai/gpt-x
#          weight: 7            # 70% 概率
#      - model_name: my-pool
#        litellm_params:
#          model: openai/gpt-x
#          weight: 3            # 30% 概率
#
# 注意：本 Hook 通过 `async_filter_deployments` 在路由早期就把候选收敛到 1 台，
# 因此下游 simple-shuffle 只是「在这 1 台上 shuffle」= 直接用它，weight 的加权语义
# 完全由本 Hook 的首次分配决定，不再被 simple-shuffle 二次覆盖。
# ===========================================================================
