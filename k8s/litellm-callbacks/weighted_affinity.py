"""
weighted_affinity.py — LiteLLM v1.90 自定义路由 Hook

目标：替代内置 `deployment_affinity`，实现「weight 加权的首次分配 + 会话黏性」。

问题背景
--------
LiteLLM 内置的 `deployment_affinity` 在命中缓存时会 `return [deployment]`，把候选
砍成 1 台，导致后续 simple-shuffle 的 weight 完全不生效。而 MISS 时它把整份
`healthy_deployments` 原样返回给下游 simple-shuffle —— simple-shuffle 的 weight 语义
只在「第一次」体现，之后被 affinity 钉死，两者其实是割裂的。

本 Hook 的行为
--------------
- **affinity MISS**（该 user_api_key 第一次请求该 model group）：
  在 `async_filter_deployments` 里，直接按各 deployment 的 `litellm_params.weight`
  做加权随机选 1 台，把 user_key_hash → 选中 model_id 写进缓存（TTL 可配），并
  `return [该 deployment]`。
  —— weight 决定「新会话初始落在哪台」的概率分布。
- **affinity HIT**（该 key 已有缓存）：
  直接返回缓存钉的那台，`return [该 deployment]`。
  —— 维持「同一 key 的会话不跳台」。
- **无 user_key / 无法决策**（比如 model group 内 model_map_key 不稳定、缓存钉的台
  已不在 healthy set 里）：`return` 原始 `healthy_deployments` 不做任何过滤。

设计上把「选台 + 写缓存」都放在 `async_filter_deployments` 里（而不是像内置那样
在 `async_pre_call_deployment_hook` 里 post-select 写缓存），因为我们要的正是「由本
Hook 亲自决定初始落台」，而不是让下游 simple-shuffle 决定后再记录。这样 weight
的加权语义完全由本 Hook 控制，可复现、可观测。

接口约束（v1.90.2 源码已确认）
------------------------------
- 继承 `litellm.integrations.custom_logger.CustomLogger`。
- 实现 `async def async_filter_deployments(self, model, healthy_deployments,
  messages=None, request_kwargs=None, parent_otel_span=None) -> List[dict]`。
- `healthy_deployments[i]["model_info"]["id"]` = 唯一 deployment id（= 我们说的 model_id）。
- `healthy_deployments[i]["litellm_params"]["weight"]` = int（可能缺失，缺省视为 1）。
- user_api_key 从 `request_kwargs` 的 metadata 里取（`metadata` 或 `litellm_metadata`
  下的 `user_api_key_hash`，已是 sha256）。
"""

import hashlib
import os
import random
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

    CACHE_KEY_PREFIX = "weighted_affinity:v1"

    def __init__(
        self,
        cache: Optional[Any] = None,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """
        Args:
            cache: DualCache 实例。作为 callback 挂载时通常拿不到 router 的 DualCache，
                   此时留空，运行期从 `litellm.cache` 惰性获取；再拿不到就退化到内存 dict。
            ttl_seconds: 黏性 TTL。缺省读 env `WEIGHTED_AFFINITY_TTL`，再缺省 120s。
        """
        super().__init__()
        self._injected_cache = cache
        self._memory_cache = _InMemoryTTLCache()
        if ttl_seconds is None:
            ttl_seconds = int(os.getenv("WEIGHTED_AFFINITY_TTL", "120"))
        self.ttl_seconds = ttl_seconds
        verbose_router_logger.info(
            "WeightedAffinityRouter: initialized (ttl=%ss, injected_cache=%s)",
            self.ttl_seconds,
            "yes" if cache is not None else "no",
        )

    # ------------------------------------------------------------------
    # 缓存后端解析
    # ------------------------------------------------------------------
    @property
    def cache(self) -> Any:
        """
        缓存后端优先级：
          1. 构造时注入的 DualCache（`Router(optional_pre_call_checks=[...])` 路径能拿到）
          2. `litellm.cache`（proxy 配了 cache: type: redis 时全局可用）
          3. 内存 dict fallback（保证永不为 None，import/运行都不崩）

        每次访问都惰性解析，避免 import 期 litellm.cache 尚未初始化的时序问题。
        """
        if self._injected_cache is not None:
            return self._injected_cache
        try:
            import litellm

            if getattr(litellm, "cache", None) is not None:
                return litellm.cache
        except Exception:
            pass
        return self._memory_cache

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
    def get_affinity_cache_key(cls, model_group: str, user_key: str) -> str:
        hashed = cls._hash_user_key(user_key)
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

        cache_key = self.get_affinity_cache_key(model_group, user_key)

        # ---- 1) 尝试命中缓存（HIT → 返回钉的台）----
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

        if pinned_model_id:
            deployment = self._find_deployment_by_model_id(deployments, pinned_model_id)
            if deployment is not None:
                verbose_router_logger.info(
                    "WeightedAffinityRouter: HIT group=%s user=%s -> pinned deployment=%s",
                    model_group,
                    self._shorten_for_logs(user_key),
                    pinned_model_id,
                )
                return [deployment]
            # 钉的台已不在健康集合 → 视为失效，走 MISS 重选
            verbose_router_logger.info(
                "WeightedAffinityRouter: pinned deployment=%s not in healthy set "
                "(group=%s), re-picking",
                pinned_model_id,
                model_group,
            )

        # ---- 2) MISS → weighted 选台 + 写缓存 ----
        chosen = self._weighted_pick(deployments)
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
            "WeightedAffinityRouter: MISS group=%s user=%s -> weighted-pick "
            "deployment=%s weight=%s (total_candidates=%d, ttl=%ss)",
            model_group,
            self._shorten_for_logs(user_key),
            chosen_id,
            self._get_weight(chosen),
            len(deployments),
            self.ttl_seconds,
        )
        return [chosen]


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
