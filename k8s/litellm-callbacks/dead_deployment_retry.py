"""
dead_deployment_retry —— 死号死锁修复（198 母 router）

## 治的是什么

一个 ChatGPT 账号过期后，它的叶子 litellm 一个 model 都注册不上，`/v1/models` 回
`{"data":[]}`，但 k8s 侧 `readyReplicas=1`、pod Running。母 router 照旧把请求投过去，
叶子回 400：

    You passed in model=<组名>. There are no healthy deployments for this model

母 router 池子里没有任何机制能让这个坏账号自动离开 —— 三道闸门对它全部失效：

  1. 母 router 从不探叶子的活（`background_health_checks: false`）
  2. 死号回的 400 不进 cooldown（`cooldown_handlers._is_cooldown_required`
     里写着 `# Do NOT cool down all other 4XX Errors`，400 落到 `return False`）
  3. weighted_affinity 明确不标记 BadRequest（`_NEVER_MARK_SUBSTRINGS` 第一个条目）

结果：WA 的会话钉子永远钉在死号上，TTL 每次请求还自动续期 → 死锁，直到人工摘号。
2026-09-06 carher-14 / hermestest-14 连续 400 就是这么来的。

## 怎么修

挂包装器，只对上面那句话（且 status=400、且不是 ContextWindowExceeded）改判：

  | 挂在哪                                        | 命中时              | 不命中时       |
  |----------------------------------------------|---------------------|----------------|
  | `Router.should_retry_this_error`             | 返回 True → 花重试   | 原样调原方法   |
  | `cooldown_handlers._is_cooldown_required`    | 返回 True → 拉黑 60s | 原样调原函数   |

**⚠ 2026-09-07 起，第二个钩子默认不挂**（用户拍板：「可以重试，先不要拉黑」）。
不挂 = 逐字节回到 litellm 原行为，见下面 `_COOLDOWN_HOOK_ON` 那段。要恢复设
`DEAD_ARM_COOLDOWN_HOOK=1`。

只挂 retry 的效果边界要说清楚：**钉子不会被挪走**（拉黑才是挪钉子的手），所以能不能
换到活腿取决于 WA 重摇是否摇到别的腿，而不是必然。原设计里两个钩子是配套的：
只挂 cooldown → 当前这次请求仍失败；只挂 retry → 重试可能仍打同一个死号。

第三环不用改 —— 死腿一被拉黑就掉出健康集合，WA 现成的 `pinned deployment not in
healthy set` 分支会自动把钉子挪走（该分支实测一小时触发 93 次）。**拉黑钩子关掉时
这一环也跟着不生效。**

## 为什么第 1 次失败就能拉黑（不是攒够 allowed_fails）

config 里 `allowed_fails: 3`，但 `_is_allowed_fails_set_on_router` 的判据是
`router.allowed_fails != litellm.allowed_fails`，而 litellm 库默认就是 3 —— 相等，
返回 False。所以走的是 v2 百分比分支，其最后一条 `litellm._should_retry(400) is False
→ return True` 对 400 直接成立。链路上唯一的闸门就是 `_is_cooldown_required`。

配合 `num_retries: 2`（共 3 枪）：第 1 枪死号 → 拉黑 + 判为可重试；第 2 枪 WA 重摇 →
活号 → 正常返回。用户无感。

## 判别串为什么可信

`There are no healthy deployments for this model` 是 LiteLLM 路由层自己生成的字符串
（全包唯一产生点 router.py:10979），用户 payload 再怎么写错也吐不出这句。
24h 内 gpt 系列 3715 个 400，其中 3699 个（99.6%）是这一句，真客户端错误只有 24 个。

## 隔离性

- 新增一个文件，不修改现有任何补丁模块的任何一行
- 两个挂钩点，现有 31 个补丁一个都没碰过（grep 全部 NONE）
- 两个都是包装器：不命中就原样调原函数 → 对所有其他情况行为逐字节不变
- 带版本指纹守卫：原函数源码对不上就拒绝安装并保持原状
- 整个 install 包在 try/except 里，装不上也绝不拖垮 proxy 启动

自测：`python3 dead_deployment_retry.py --selftest`（纯函数，不需要 litellm）
"""

import logging
import os
import time

_LOG = logging.getLogger("dead_deployment_retry")

# ---------------------------------------------------------------------------
# 自动摘号（第二段功能，2026-09-07 追加）
# ---------------------------------------------------------------------------
# 拉黑 60s 只是止血：死号每 60 秒还会被重新打中一次，永远待在池子里。
# 这一段让它自己走出去 —— 连续 N 次 marker 400 → 实探叶子 → 确认死了 → /model/delete。
#
# 为什么敢自动删（两道独立的锁，都有数据支撑）：
#   1. 「连续 N 次」**加上一个显式的时间下限**。
#      注意：时间下限才是真正的安全属性，次数只是它的代理。
#      198 上是白捡的 —— 那边 `cooldown_time: 60`，一个 replica 最快每 60 秒才可能
#      再打中同一条腿（2B 实测：32 枪里死腿只被打中 1 次），N=10 ⇒ 自然 ≥10 分钟。
#      **但这个前提不可移植**：阿里云（ns carher）没配 cooldown_time，走 litellm 默认
#      `DEFAULT_COOLDOWN_TIME_SECONDS = 5`，同样 10 次最快 50 秒就攒满 —— 安全边际塌掉一个数量级。
#      所以这里把下限显式写出来（`_MIN_STREAK_SECONDS`，默认 600s），不再靠环境白送。
#      假阳风险（叶子重载时的 no-healthy 窗口）是**秒级**的，够不到十分钟。
#   2. 删之前必须过实探：直连该腿 api_base 打 `GET /v1/models`。
#      2026-09-06 摘六个过期号时在真死号上验过这把尺子：
#      健康号 12 个 model、过期号 `{"data":[]}`，同一份 byte-identical config
#      ⇒ **0 models = 账号死了，不是配置问题**。瞬态重载的叶子几秒后就有 model，过不了这关。
#
# 第三道是保命护栏：删完会让该 group 掉到 _MIN_GROUP_ARMS 以下就拒绝删，只告警。
# 09-06 人工摘号时特意确认过「无空组无单点」，这个不能靠运气。

def _env_int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_off(name):
    """env 未设/为空/0/false 视为「没关」。"""
    return (os.getenv(name, "") or "").strip().lower() not in ("", "0", "false", "no")


# 连续多少次 marker 400 才去实探
_AUTO_DELETE_THRESHOLD = _env_int("DEAD_ARM_AUTO_DELETE_THRESHOLD", 10)
# 连击必须横跨的最短时间（秒）。次数够但时间不够 → 不实探，继续攒。
# 这道门在 198 上是恒真的（cooldown 60s × 10 次 = 600s），加它是为了让阿里云
# （cooldown 走默认 5s）拿到同样的安全边际，而不是靠"恰好配得对"。
_MIN_STREAK_SECONDS = _env_int("DEAD_ARM_MIN_STREAK_SECONDS", 600)
# 一个 group 至少要留多少条腿，低于它拒绝删
_MIN_GROUP_ARMS = _env_int("DEAD_ARM_MIN_GROUP_ARMS", 5)
# 一键关掉自动摘号（拉黑/重试那两个钩子不受影响，仍然生效）
_AUTO_DELETE_DISABLED = _env_off("DEAD_ARM_AUTO_DELETE_DISABLED")
# 演练模式：走完全部判断和日志，但不真的调 /model/delete
_AUTO_DELETE_DRY_RUN = _env_off("DEAD_ARM_AUTO_DELETE_DRY_RUN")
# ---------------------------------------------------------------------------
# 拉黑钩子（`_is_cooldown_required`）默认**不挂**
# ---------------------------------------------------------------------------
# 2026-09-07 用户拍板：「可以重试，先不要拉黑」。
# 不挂 ≠ 挂上去再返回原值 —— 不挂才是逐字节回到 litellm 原行为
# （400 不进 cooldown，注释原文 `Do NOT cool down all other 4XX Errors`）。
# 挂上去哪怕只是转发，也多一层调用栈和一次判别串匹配，且指纹守卫会去读它的源码。
#
# 连带影响（这是它的语义，不是缺陷）：
#   - 死腿命中后**不再被拉黑 60s**，下一枪可能还摇到它 → 靠 num_retries 换腿，而不是靠拉黑挪钉子。
#   - 自动摘号的连击计数原本就寄生在这个钩子里 → 钩子不挂，计数也不跑，摘号自然静默。
# 想恢复：设 env `DEAD_ARM_COOLDOWN_HOOK=1`。
_COOLDOWN_HOOK_ON = _env_off("DEAD_ARM_COOLDOWN_HOOK")

# model_id -> 连续 marker-400 次数（成功即清零）。进程内，每 replica 各自一份。
_dead_streak = {}
# model_id -> 本轮连击第一次命中的时间戳，供时间下限门用。跟着 _dead_streak 一起清。
_streak_first_ts = {}
# 已经处理过的 model_id，防止重复删 / 重复告警
_delete_attempted = set()


def streak_age(model_id, now=None):
    """本轮连击已经横跨了多少秒。没有记录返回 0。"""
    first = _streak_first_ts.get(model_id)
    if first is None:
        return 0.0
    return (time.time() if now is None else now) - first


def bump_streak(model_id, threshold=None, now=None, min_seconds=None):
    """
    记一次 marker 400，返回是否可以去实探。model_id 为空则不计数。

    两个条件**同时**满足才返回 True：次数 ≥ threshold，且连击横跨 ≥ min_seconds。
    只看次数是不够的 —— 次数只是时间的代理，而这个代理的换算率
    （= cooldown_time）在 198 和阿里云上差 12 倍。
    """
    if not model_id:
        return False
    ts = time.time() if now is None else now
    n = _dead_streak.get(model_id, 0) + 1
    _dead_streak[model_id] = n
    first = _streak_first_ts.setdefault(model_id, ts)
    if n < (threshold if threshold is not None else _AUTO_DELETE_THRESHOLD):
        return False
    floor = _MIN_STREAK_SECONDS if min_seconds is None else min_seconds
    return (ts - first) >= floor


def reset_streak(model_id):
    """任何一次成功都清零 —— 「连续」的语义靠这个成立。"""
    if model_id:
        _dead_streak.pop(model_id, None)
        _streak_first_ts.pop(model_id, None)


def leaf_is_dead(models_payload):
    """
    叶子 `GET /v1/models` 的返回 → 是否判定账号已死。

    判据只有一条：`data` 里一个 model 都没有。
    取不到 payload（连不上/超时/非 JSON）一律返回 False —— **网络问题不许当成账号死了**，
    宁可下一轮再判。
    """
    if not isinstance(models_payload, dict):
        return False
    data = models_payload.get("data")
    if not isinstance(data, list):
        return False
    return len(data) == 0


def classify_probe(models_payload):
    """
    叶子 `GET /v1/models` 的返回 → `"dead"` / `"alive"` / `"unknown"`。

    为什么必须有 `unknown` 这一档（2026-09-07 阳性对照踩出来的）：
    叶子认的 key **不是**母 router 的 master key，是 `chatgpt-pool-master-key`。
    拿错 key 时健康叶子回 **400 `No connected db.`** —— 这既不是死也不是活。
    把它归进 alive 会**清零计数**（于是永远升级不了，日志还谎称"瞬态"）；
    归进 dead 会误删健康号。所以单独一档：不判、不清零、下一轮再说。

    实测形状（09-07，pool key）：健康叶子 200 且 `data` 有 12 个 model。
    """
    if not isinstance(models_payload, dict):
        return "unknown"
    if models_payload.get("_http") is not None:
        return "unknown"
    data = models_payload.get("data")
    if not isinstance(data, list):
        return "unknown"
    return "dead" if len(data) == 0 else "alive"


def may_delete_from_group(group_arm_count, min_arms=None):
    """删掉一条后该 group 还剩几条腿够不够。数不出来（None）时一律不许删。"""
    floor = min_arms if min_arms is not None else _MIN_GROUP_ARMS
    if not isinstance(group_arm_count, int):
        return False
    return (group_arm_count - 1) >= floor

# ---------------------------------------------------------------------------
# 判别（纯函数，不 import litellm，方便本机 selftest）
# ---------------------------------------------------------------------------

# LiteLLM 路由层唯一产生点：router.py:10979
_DEAD_MARKER = "There are no healthy deployments for this model"

# 免费保险：ContextWindowExceededError 也是 400，它的消息模板不含上面那句，
# 但万一上游把两段消息拼在一起，这里再挡一道。
_NEVER_TREAT_AS_DEAD = (
    "ContextWindowExceeded",
    "ContentPolicyViolation",
)


def _status_code_of(exc):
    """尽力取 HTTP 状态码；取不到返回 None。"""
    for attr in ("status_code", "code", "http_status"):
        val = getattr(exc, attr, None)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _status_is_400(exception_status):
    try:
        return int(exception_status) == 400
    except (TypeError, ValueError):
        return False


def _text_is_dead_deployment(text):
    """判别串命中 + 不是 ContextWindow / ContentPolicy 这类同为 400 的真错误。"""
    if not text or _DEAD_MARKER not in text:
        return False
    return not any(s in text for s in _NEVER_TREAT_AS_DEAD)


def is_dead_deployment_error(exc):
    """异常对象版判别：必须同时满足 400 + 判别串。"""
    if _status_code_of(exc) != 400:
        return False
    text = "%s %s" % (type(exc).__name__, exc)
    return _text_is_dead_deployment(text)


def is_dead_deployment_cooldown_case(exception_status, exception_str):
    """cooldown 钩子版判别：状态码和消息分开传进来。"""
    if not _status_is_400(exception_status):
        return False
    return _text_is_dead_deployment(exception_str)


# ---------------------------------------------------------------------------
# 自动摘号：动手部分（全部跑在独立线程，绝不占请求路径）
# ---------------------------------------------------------------------------

_PROBE_TIMEOUT = _env_int("DEAD_ARM_PROBE_TIMEOUT", 10)
_DELETE_TIMEOUT = _env_int("DEAD_ARM_DELETE_TIMEOUT", 20)


def _find_arm(router, model_id):
    """从 router 自己的 model_list 里找这条腿，返回 (model_name, api_base, api_key)。"""
    for dep in getattr(router, "model_list", None) or []:
        if (dep.get("model_info") or {}).get("id") != model_id:
            continue
        lp = dep.get("litellm_params") or {}
        return dep.get("model_name"), lp.get("api_base"), lp.get("api_key")
    return None, None, None


def _arm_is_db_model(router, model_id):
    """
    这条腿是 DB 模型（`/model/delete` 删得动）还是 config.yaml 模型（删不动）。

    判据是 litellm 自己在加载时打的 `model_info.db_model`。
    2026-09-07 实测：198 的 acct 腿全 True；阿里云 84 条 acct 腿全 False。
    取不到就当 False —— 宁可走内存摘（可逆），也不要对着 DB 空删一场然后以为删掉了。
    """
    for dep in getattr(router, "model_list", None) or []:
        if (dep.get("model_info") or {}).get("id") == model_id:
            return bool((dep.get("model_info") or {}).get("db_model"))
    return False


def _count_group_arms(router, model_name):
    """该 group 现在有多少条腿。数不出来返回 None（上层会拒绝删除）。"""
    if not model_name:
        return None
    try:
        return sum(
            1
            for dep in (getattr(router, "model_list", None) or [])
            if dep.get("model_name") == model_name
        )
    except Exception:  # noqa: BLE001
        return None


def _http_json(url, method="GET", token=None, payload=None, timeout=10):
    """极简 JSON 请求。任何异常返回 None —— 拿不到数据一律当「不确定」，不当「死了」。"""
    import json
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return {"_http": exc.code, "_body": exc.read().decode()[:300]}
        except Exception:  # noqa: BLE001
            return {"_http": exc.code}
    except Exception:  # noqa: BLE001
        return None


def _auto_retire(router, model_id):
    """
    连续 N 次 marker 400 之后的收口动作：实探 → 护栏 → 删。
    这个函数在独立线程里跑，**任何异常都只记日志**，不允许影响主流程。
    """
    from litellm._logging import verbose_router_logger as vlog

    model_name, api_base, api_key = _find_arm(router, model_id)
    if not api_base:
        vlog.warning(
            "dead_deployment_retry: AUTO-RETIRE abort id=%s —— 在 model_list 里找不到"
            "这条腿的 api_base（可能已被删）",
            model_id,
        )
        return

    # ---- 闸门：实探叶子。0 models = 账号死了（09-06 在真死号上验过的判据）----
    # 注意 token 必须是**这条腿自己的 api_key**（= chatgpt-pool-master-key）。
    # 母 router 的 master key 在叶子上不被认，会拿到 400 `No connected db.`（09-07 实测）。
    if not api_key:
        vlog.warning(
            "dead_deployment_retry: AUTO-RETIRE abort id=%s base=%s —— 这条腿没有 "
            "api_key，实探认不过去，不判死",
            model_id,
            api_base,
        )
        _delete_attempted.discard(model_id)
        return

    payload = _http_json(
        api_base.rstrip("/") + "/v1/models", token=api_key, timeout=_PROBE_TIMEOUT
    )
    verdict = classify_probe(payload)
    if verdict == "unknown":
        # 连不上 / 超时 / 401 / 400 / 非 JSON —— 一律不判死，也**不清零**，下一轮再来
        vlog.warning(
            "dead_deployment_retry: AUTO-RETIRE hold id=%s base=%s —— 实探读不出 "
            "model 列表（%r），**既不判死也不判活**，等下一轮",
            model_id,
            api_base,
            (payload or {}).get("_http") if isinstance(payload, dict) else payload,
        )
        _delete_attempted.discard(model_id)
        return
    if verdict == "alive":
        vlog.warning(
            "dead_deployment_retry: AUTO-RETIRE veto id=%s base=%s —— 叶子实探有 %s 个 "
            "model，判为瞬态不是死号，计数清零，不删",
            model_id,
            api_base,
            len(payload.get("data") or []),
        )
        _delete_attempted.discard(model_id)
        _dead_streak.pop(model_id, None)
        _streak_first_ts.pop(model_id, None)
        return

    # ---- 护栏：不能把 group 掏空 ----
    n_arms = _count_group_arms(router, model_name)
    if not may_delete_from_group(n_arms):
        vlog.error(
            "dead_deployment_retry: AUTO-RETIRE BLOCKED id=%s group=%s arms=%s "
            "floor=%s —— 叶子确认 0 models 但删了会让组低于下限，**需要人工处理**",
            model_id,
            model_name,
            n_arms,
            _MIN_GROUP_ARMS,
        )
        return

    # ---- 备份行：重新 onboard 靠这一行（api_key 不落日志）----
    vlog.error(
        "dead_deployment_retry: AUTO-RETIRE manifest model_name=%s model_id=%s "
        "api_base=%s group_arms_before=%s",
        model_name,
        model_id,
        api_base,
        n_arms,
    )

    if _AUTO_DELETE_DRY_RUN:
        vlog.error(
            "dead_deployment_retry: AUTO-RETIRE dry-run id=%s —— 判定成立但按 "
            "DEAD_ARM_AUTO_DELETE_DRY_RUN 不真删",
            model_id,
        )
        return

    master = os.getenv("LITELLM_MASTER_KEY")
    if not master:
        vlog.error(
            "dead_deployment_retry: AUTO-RETIRE abort id=%s —— 取不到 "
            "LITELLM_MASTER_KEY，无法调 /model/delete",
            model_id,
        )
        return

    # ---- 分流：这条腿是 DB 模型还是 config.yaml 模型 ----
    # `/model/delete` 删的是 DB 里的 ProxyModelTable 行。198 的 acct 腿全在 DB 里，
    # 所以那边删完是**持久**的，4 个副本靠轮询收敛。
    # 阿里云（ns carher）不一样：84 条 acct 腿 `db_model=False`，全部由 config.yaml 声明，
    # DB 里根本没有对应行 —— 对它们调 `/model/delete` 删不掉任何东西。
    # 这种腿只能从**运行中 router 的内存 model_list** 里摘（`delete_deployment`），
    # 语义是「摘到本进程下次重启为止」，且每个 replica 各摘各的。
    # 这不是退化，是这类腿唯一可能的语义：配置在 config.yaml 里，重启本来就会把它带回来；
    # 号要是还死着，下一轮连击会重新把它摘掉。
    if _arm_is_db_model(router, model_id):
        res = _http_json(
            "http://localhost:4000/model/delete",
            method="POST",
            token=master,
            payload={"id": model_id},
            timeout=_DELETE_TIMEOUT,
        )
        vlog.error(
            "dead_deployment_retry: AUTO-RETIRE deleted id=%s group=%s base=%s "
            "mode=db-persistent result=%r",
            model_id,
            model_name,
            api_base,
            res,
        )
        return

    try:
        router.delete_deployment(id=model_id)
    except Exception as exc:  # noqa: BLE001
        vlog.error(
            "dead_deployment_retry: AUTO-RETIRE abort id=%s —— config 模型，"
            "delete_deployment 失败: %r",
            model_id,
            exc,
        )
        return
    vlog.error(
        "dead_deployment_retry: AUTO-RETIRE deleted id=%s group=%s base=%s "
        "mode=in-memory（config.yaml 声明的腿，本副本重启后会回来；号若仍死会被再次摘掉）",
        model_id,
        model_name,
        api_base,
    )


def _schedule_auto_retire(router, model_id):
    """把收口动作丢到后台线程。这个函数本身必须不抛、不阻塞。"""
    import threading

    try:
        t = threading.Thread(
            target=_auto_retire,
            args=(router, model_id),
            name="dead-arm-retire-%s" % model_id,
            daemon=True,
        )
        t.start()
    except Exception as exc:  # noqa: BLE001
        _LOG.error("dead_deployment_retry: schedule auto-retire failed: %r", exc)


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------

# 版本指纹：原函数源码里必须还有这些串，对不上说明镜像升级过、语义可能变了，拒绝安装
_FP_SHOULD_RETRY = "_num_healthy_deployments"
_FP_IS_COOLDOWN_REQUIRED = "Do NOT cool down all other 4XX Errors"


def _install():
    import inspect

    from litellm.router import Router
    from litellm.router_utils import cooldown_handlers
    from litellm._logging import verbose_router_logger

    if getattr(Router, "_carher_dead_deploy_patched", False):
        verbose_router_logger.info(
            "dead_deployment_retry: already installed, skip"
        )
        return

    # ---- 指纹守卫 ----------------------------------------------------------
    # 只守要挂的钩子。拉黑钩子关掉时连它的源码都不读 —— 守一个不挂的东西，
    # 唯一效果是将来 litellm 升级时凭一个用不到的串把整个补丁挡在门外。
    _guards = [
        (Router.should_retry_this_error, _FP_SHOULD_RETRY, "should_retry_this_error"),
    ]
    if _COOLDOWN_HOOK_ON:
        _guards.append(
            (
                cooldown_handlers._is_cooldown_required,
                _FP_IS_COOLDOWN_REQUIRED,
                "_is_cooldown_required",
            )
        )
    for target, fingerprint, label in _guards:
        try:
            src = inspect.getsource(target)
        except Exception as exc:  # noqa: BLE001
            verbose_router_logger.error(
                "dead_deployment_retry: cannot read source of %s, NOT patching: %r",
                label,
                exc,
            )
            return
        if fingerprint not in src:
            verbose_router_logger.error(
                "dead_deployment_retry: fingerprint mismatch on %s "
                "(image upgraded?), NOT patching",
                label,
            )
            return

    _orig_should_retry = Router.should_retry_this_error
    _orig_is_cooldown_required = cooldown_handlers._is_cooldown_required

    # ---- 钩子 1：让死号 400 花掉重试次数 -----------------------------------
    def _patched_should_retry_this_error(
        self,
        error,
        healthy_deployments=None,
        all_deployments=None,
        context_window_fallbacks=None,
        content_policy_fallbacks=None,
        regular_fallbacks=None,
    ):
        if is_dead_deployment_error(error):
            verbose_router_logger.warning(
                "dead_deployment_retry: dead-deployment 400 detected, "
                "forcing retry (healthy_deployments=%s)",
                len(healthy_deployments) if isinstance(healthy_deployments, list) else "?",
            )
            return True
        return _orig_should_retry(
            self,
            error,
            healthy_deployments,
            all_deployments,
            context_window_fallbacks,
            content_policy_fallbacks,
            regular_fallbacks,
        )

    # ---- 钩子 2：让死号 400 进 cooldown ------------------------------------
    def _patched_is_cooldown_required(
        litellm_router_instance,
        model_id,
        exception_status,
        exception_str=None,
    ):
        if is_dead_deployment_cooldown_case(exception_status, exception_str):
            # 顺手计数。连续到阈值就去实探叶子，确认死了才摘。
            # 整段包在 try 里：自动摘号出任何问题都不许影响拉黑本身。
            hit = False
            try:
                if not _AUTO_DELETE_DISABLED:
                    hit = bump_streak(model_id)
            except Exception as exc:  # noqa: BLE001
                _LOG.error("dead_deployment_retry: bump_streak error: %r", exc)
            verbose_router_logger.warning(
                "dead_deployment_retry: cooling down dead deployment id=%s "
                "(400 no-healthy-deployments) streak=%s/%s age=%ds/%ds",
                model_id,
                _dead_streak.get(model_id, 0),
                _AUTO_DELETE_THRESHOLD,
                int(streak_age(model_id)),
                _MIN_STREAK_SECONDS,
            )
            try:
                if hit and model_id not in _delete_attempted:
                    _delete_attempted.add(model_id)
                    verbose_router_logger.error(
                        "dead_deployment_retry: id=%s 连续 %s 次 marker 400，开始实探叶子",
                        model_id,
                        _dead_streak.get(model_id),
                    )
                    _schedule_auto_retire(litellm_router_instance, model_id)
            except Exception as exc:  # noqa: BLE001
                _LOG.error("dead_deployment_retry: retire error: %r", exc)
            return True
        return _orig_is_cooldown_required(
            litellm_router_instance=litellm_router_instance,
            model_id=model_id,
            exception_status=exception_status,
            exception_str=exception_str,
        )

    Router.should_retry_this_error = _patched_should_retry_this_error
    if _COOLDOWN_HOOK_ON:
        cooldown_handlers._is_cooldown_required = _patched_is_cooldown_required
    Router._carher_dead_deploy_patched = True

    _hooks = ["should_retry_this_error"]
    if _COOLDOWN_HOOK_ON:
        _hooks.append("_is_cooldown_required")
    verbose_router_logger.info(
        "dead_deployment_retry: installed (%s), marker=%r%s",
        " + ".join(_hooks),
        _DEAD_MARKER,
        "" if _COOLDOWN_HOOK_ON else "  [cooldown hook OFF：死腿只重试不拉黑，"
                                     "自动摘号连带静默]",
    )


try:
    _install()
except Exception as _exc:  # noqa: BLE001
    _LOG.error("dead_deployment_retry: install failed, skipped: %r", _exc)


# ---------------------------------------------------------------------------
# callbacks 挂载壳（逻辑全在 import 副作用里）
# ---------------------------------------------------------------------------

try:
    from litellm.integrations.custom_logger import CustomLogger

    class DeadDeploymentRetryCallback(CustomLogger):
        """
        callbacks 挂载入口。两个钩子的逻辑在 import 副作用里，这里只做一件事：
        **成功即清零** —— 「连续 N 次」的语义完全靠它成立，缺了它就会跨小时攒够次数误摘。
        """

        @staticmethod
        def _model_id_of(kwargs):
            try:
                info = (kwargs.get("litellm_params") or {}).get("model_info") or {}
                if info.get("id"):
                    return info["id"]
                meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
                return meta.get("model_info", {}).get("id") or meta.get("model_id")
            except Exception:  # noqa: BLE001
                return None

        def _reset(self, kwargs):
            try:
                reset_streak(self._model_id_of(kwargs))
            except Exception as exc:  # noqa: BLE001
                _LOG.error("dead_deployment_retry: reset_streak failed: %r", exc)

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
            self._reset(kwargs)

        def log_success_event(self, kwargs, response_obj, start_time, end_time):
            self._reset(kwargs)

    dead_deployment_retry = DeadDeploymentRetryCallback()
except Exception as _exc:  # noqa: BLE001
    _LOG.error("dead_deployment_retry: callback shell unavailable: %r", _exc)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------


def _selftest():
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append("%s: got %r, want %r" % (name, got, want))
            print("  FAIL %-52s got=%r want=%r" % (name, got, want))
        else:
            print("  ok   %-52s -> %r" % (name, got))

    class _Exc(Exception):
        def __init__(self, message, status_code):
            super().__init__(message)
            self.status_code = status_code

    class BadRequestError(_Exc):
        pass

    class ContextWindowExceededError(_Exc):
        pass

    class RateLimitError(_Exc):
        pass

    dead = BadRequestError(
        "litellm.BadRequestError: OpenAIException - {\"error\":{\"message\":"
        "\"You passed in model=chatgpt-gpt-5.6-sol. There are no healthy "
        "deployments for this model\",\"code\":\"400\"}}",
        400,
    )
    real_client_400 = BadRequestError(
        "litellm.BadRequestError: OpenAIException - Invalid value for "
        "'tool_choice': no tool named 'foo' in tools",
        400,
    )
    ctx_400 = ContextWindowExceededError(
        "litellm.ContextWindowExceededError: ContextWindowExceededError: "
        "openai - This model's maximum context length is 922000 tokens. "
        "There are no healthy deployments for this model",
        400,
    )
    rate_429 = RateLimitError(
        "litellm.RateLimitError: No deployments available for selected model, "
        "Try again in 5 seconds. cooldown_list=[...]",
        429,
    )

    print("[1] is_dead_deployment_error —— 异常对象判别")
    check("死号 400 -> 判为死号", is_dead_deployment_error(dead), True)
    check("真客户端 400 -> 不判死号", is_dead_deployment_error(real_client_400), False)
    check("ContextWindowExceeded 400 -> 不判死号", is_dead_deployment_error(ctx_400), False)
    check("429 -> 不判死号", is_dead_deployment_error(rate_429), False)

    print("[2] is_dead_deployment_cooldown_case —— cooldown 判别")
    check(
        "死号 400 -> 要拉黑",
        is_dead_deployment_cooldown_case(400, str(dead)),
        True,
    )
    check(
        "死号消息但状态非 400 -> 不拉黑",
        is_dead_deployment_cooldown_case(500, str(dead)),
        False,
    )
    check(
        "真客户端 400 -> 不拉黑",
        is_dead_deployment_cooldown_case(400, str(real_client_400)),
        False,
    )
    check(
        "ContextWindow 400 -> 不拉黑",
        is_dead_deployment_cooldown_case(400, str(ctx_400)),
        False,
    )
    check("429 -> 不拉黑", is_dead_deployment_cooldown_case(429, str(rate_429)), False)
    check("exception_str 为 None -> 不拉黑", is_dead_deployment_cooldown_case(400, None), False)
    check(
        "状态码是字符串 '400' 也要认",
        is_dead_deployment_cooldown_case("400", str(dead)),
        True,
    )

    print("[3] 直通性 —— 不命中时必须原样调用原函数（逐字节不变）")
    calls = []

    def _fake_orig_should_retry(
        self, error, healthy_deployments, all_deployments,
        context_window_fallbacks, content_policy_fallbacks, regular_fallbacks,
    ):
        calls.append(
            ("should_retry", error, healthy_deployments, all_deployments,
             context_window_fallbacks, content_policy_fallbacks, regular_fallbacks)
        )
        return "ORIG_RESULT"

    def _wrapper(self, error, healthy_deployments=None, all_deployments=None,
                 context_window_fallbacks=None, content_policy_fallbacks=None,
                 regular_fallbacks=None):
        if is_dead_deployment_error(error):
            return True
        return _fake_orig_should_retry(
            self, error, healthy_deployments, all_deployments,
            context_window_fallbacks, content_policy_fallbacks, regular_fallbacks,
        )

    res = _wrapper(None, rate_429, healthy_deployments=["a", "b"],
                   all_deployments=["a", "b", "c"], regular_fallbacks=["g1"])
    check("429 走原函数", res, "ORIG_RESULT")
    check("429 只调了原函数一次", len(calls), 1)
    check("原函数收到的 error 原样透传", calls[0][1] is rate_429, True)
    check("原函数收到的 healthy_deployments 原样透传", calls[0][2], ["a", "b"])
    check("原函数收到的 all_deployments 原样透传", calls[0][3], ["a", "b", "c"])
    check("原函数收到的 regular_fallbacks 原样透传", calls[0][6], ["g1"])

    calls.clear()
    res = _wrapper(None, dead, healthy_deployments=["a", "b"])
    check("死号 400 直接返回 True", res, True)
    check("死号 400 不调原函数", len(calls), 0)

    print("[4] 自动摘号 —— 连续计数（min_seconds=0，本节只测次数这一维）")
    _dead_streak.clear()
    _streak_first_ts.clear()
    for i in range(1, 10):
        check("第 %d 次还不到阈值" % i, bump_streak("armA", threshold=10, min_seconds=0), False)
    check("第 10 次到阈值", bump_streak("armA", threshold=10, min_seconds=0), True)
    check("中途成功清零", (reset_streak("armA"), _dead_streak.get("armA")), (None, None))
    check("清零也清掉起始时间戳", _streak_first_ts.get("armA"), None)
    check("清零后重新从 1 数起", bump_streak("armA", threshold=10, min_seconds=0), False)
    check("model_id 为空不计数", bump_streak("", threshold=10, min_seconds=0), False)
    check("model_id 为 None 不计数", bump_streak(None, threshold=10, min_seconds=0), False)
    _dead_streak.clear()
    _streak_first_ts.clear()
    check("另一条腿不受影响", bump_streak("armB", threshold=1, min_seconds=0), True)
    check("armB 计数与 armA 隔离", _dead_streak.get("armA"), None)

    print("[4b] 自动摘号 —— **时间下限门**（次数够≠可以删）")
    # 为什么单独一节：198 靠 cooldown_time=60 白送了 10 分钟下限，阿里云 cooldown 默认 5s，
    # 同样 10 次最快 50 秒就攒满。次数只是时间的代理，代理换算率不可移植。
    _dead_streak.clear()
    _streak_first_ts.clear()
    t = 1_000_000.0
    for i in range(1, 10):
        bump_streak("armT", threshold=10, now=t + i, min_seconds=600)
    check("10 次但只跨了 10 秒 -> 不许删",
          bump_streak("armT", threshold=10, now=t + 10, min_seconds=600), False)
    check("次数继续涨，时间仍不够 -> 还是不许删",
          bump_streak("armT", threshold=10, now=t + 60, min_seconds=600), False)
    check("跨过 600 秒 -> 放行",
          bump_streak("armT", threshold=10, now=t + 601, min_seconds=600), True)
    check("streak_age 按起始戳算（首次命中是 t+1，故 601-1=600）",
          int(streak_age("armT", now=t + 601)), 600)
    _dead_streak.clear()
    _streak_first_ts.clear()
    check("时间够但次数不够 -> 不许删",
          bump_streak("armU", threshold=10, now=t + 9999, min_seconds=0), False)
    check("清零后起始戳重置（时间不再从旧起点算）",
          (reset_streak("armU"),
           bump_streak("armU", threshold=1, now=t + 10000, min_seconds=600))[1], False)

    print("[4c] 自动摘号 —— 删除手段分流（db 模型 vs config.yaml 模型）")

    class _R:
        def __init__(self, arms):
            self.model_list = arms

    db_arm = {"model_name": "g", "litellm_params": {},
              "model_info": {"id": "dbarm", "db_model": True}}
    cfg_arm = {"model_name": "g", "litellm_params": {},
               "model_info": {"id": "cfgarm", "db_model": False}}
    bare_arm = {"model_name": "g", "litellm_params": {}, "model_info": {"id": "barearm"}}
    check("db_model=True -> 走 /model/delete", _arm_is_db_model(_R([db_arm]), "dbarm"), True)
    check("db_model=False -> 走内存摘", _arm_is_db_model(_R([cfg_arm]), "cfgarm"), False)
    check("没有 db_model 字段 -> 当 config 模型（可逆的那条路）",
          _arm_is_db_model(_R([bare_arm]), "barearm"), False)
    check("腿不在 model_list -> False", _arm_is_db_model(_R([]), "ghost"), False)

    print("[5] 自动摘号 —— 实探判据（0 models = 死）")
    check("data 空 -> 判死", leaf_is_dead({"data": []}), True)
    check("有 1 个 model -> 不判死", leaf_is_dead({"data": [{"id": "m"}]}), False)
    check("有 12 个 model -> 不判死", leaf_is_dead({"data": [{}] * 12}), False)
    check("连不上(None) -> 不判死", leaf_is_dead(None), False)
    check("没有 data 字段 -> 不判死", leaf_is_dead({"object": "list"}), False)
    check("data 不是 list -> 不判死", leaf_is_dead({"data": {}}), False)
    check("HTTP 错误体 -> 不判死", leaf_is_dead({"_http": 401}), False)
    check("整个 payload 不是 dict -> 不判死", leaf_is_dead("[]"), False)

    print("[6] 自动摘号 —— 实探三档（dead / alive / unknown）")
    # unknown 这一档是 09-07 阳性对照逼出来的：拿错 key 的健康叶子回 400 `No connected db.`，
    # 归进 alive 会清零计数（永远升级不了），归进 dead 会误删健康号。
    check("data 空 -> dead", classify_probe({"data": []}), "dead")
    check("12 个 model -> alive", classify_probe({"data": [{}] * 12}), "alive")
    check("HTTP 400 No-connected-db -> unknown",
          classify_probe({"_http": 400, "_body": "No connected db."}), "unknown")
    check("HTTP 401 -> unknown", classify_probe({"_http": 401}), "unknown")
    check("连不上(None) -> unknown", classify_probe(None), "unknown")
    check("没有 data 字段 -> unknown", classify_probe({"object": "list"}), "unknown")
    check("data 不是 list -> unknown", classify_probe({"data": {}}), "unknown")
    check("payload 不是 dict -> unknown", classify_probe("[]"), "unknown")

    print("[7] 自动摘号 —— 组内下限护栏（floor=5）")
    check("38 条腿 -> 可删", may_delete_from_group(38, min_arms=5), True)
    check("6 条腿 -> 可删（删完剩 5）", may_delete_from_group(6, min_arms=5), True)
    check("5 条腿 -> 拒删（删完剩 4）", may_delete_from_group(5, min_arms=5), False)
    check("1 条腿 -> 拒删", may_delete_from_group(1, min_arms=5), False)
    check("数不出来(None) -> 拒删", may_delete_from_group(None, min_arms=5), False)

    print()
    if failures:
        print("SELFTEST FAILED (%d):" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
