# LiteLLM v1.105.1 Router 深读：重试 / 兜底 / 冷却 / 选台

源码：`.work/litellm-src-study-1020/src/litellm/`（下文 `router.py:N` 都指这份）。补丁：`live-cb/*.py`。
除了读代码，我还在本机**离线**跑了 7 个实验脚本（`deep/01-router-exp*.py`）。脚本直接 import 这份 v1.105.1 源码，用 `mock_response=<异常>` 模拟上游出错，**不发任何网络请求**。凡是标「实测」的数字都出自这些脚本，标「读码」的是从代码推出来的。

复现命令：
```
cd .work/litellm-src-study-1020/src && PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=. \
  ~/.local/share/uv/tools/cli-anything-litellm/bin/python ../deep/01-router-exp.py   # exp2..exp7 同理
```
局限：mock 走的是 `litellm.acompletion` 的 chat 路径。`/v1/responses` 走的是 `_ageneric_api_call_with_fallbacks_helper`，重试和兜底的骨架一样（同一个 `async_function_with_fallbacks`），但我**没有对它单独实测次数**。Redis 多副本那部分也没法离线验证。

---

## 0. 一页结论（先说大白话）

1. **一个请求最多打多少次上游，生产里没有真正的上限。** `max_fallbacks` 默认是 5（`constants.py:11`），但它限制的是「递归深度」，不是总次数。再加上 `enable_weighted_failover` 每过一层都会再跑一次同组重选，实测 3 组（40/20/10 台）全挂 500 时**一个请求打了 84 次上游**；把兜底链拉长到 8 个组，`max_fallbacks=5` 也照样 9 个组全打了一遍（共 54 次）。真正兜底的只有墙钟时间。
2. **`retry_after=0` 不等于「重试不等待」。** 组里还有健康的台时，`_time_to_sleep_before_retry` 直接返回 0（`router.py:8029-8030`），跟 `retry_after` 无关。只有单台组才走指数退避 0.5→1→2→4→8→8 秒，实测一个单台组 6 次尝试累计等了约 25 秒，`retry_after=0` 只是退避的下限。
3. **冷却状态是 4 个 pod 共享的**（前提是 router_settings 配了 Redis，git 里的 canary 配置有 `redis_host`，**生产 DB 里是不是也配了，我没验证**）。但决定「要不要冷却」的失败计数只在本 pod 内存里（`track_deployment_metrics.py:53-75` 写的是 `local_only=True`）。所以表现是：判断冷却各 pod 各算各的，冷却生效后 4 个 pod 都能看到。
4. **`allowed_fails: 3` 等于没配。** 它跟库默认值 3 相等，`_is_allowed_fails_set_on_router` 因此返回 False（`cooldown_handlers.py:635-639`），走的是百分比分支：429 立刻冷却、失败率超过 50% 且本分钟满 5 次才冷却，以及 `_should_retry(status) is False` 的状态码（401/404 等）立刻冷却。
5. **⚠️ 新发现的回归风险：`streaming_output_backfill` 换掉 `_handle_failure` 后，/v1/responses 流内失败不再触发 router 冷却。** 实测：上游原版会调到同步 `failure_handler`（router 的 `deployment_callback_on_failure` 就注册在这里，`router.py:1180-1183`），我们的补丁只调 `async_failure_handler`。1.105.1 上游已经自己改成「有事件循环就后台跑、不阻塞」（`streaming_iterator.py:898-907`），我们这个补丁当初要修的问题在上游已经修掉，留着它反而丢了冷却。
6. **1.105.1 上游已经原生做了 `streaming_output_backfill` 的大部分工作**：流内 `response.failed` / `error` 事件转成 MidStreamFallbackError（`streaming_iterator.py:650-671`），空流或没有终止事件时也转（`:979-986`），失败的 deployment id 也会盖章（`router.py:5412-5413`）。我们的补丁还在叠加执行，**有重复，口径也不完全一致**，具体见 §6。
7. **`dead_deployment_retry` 和 1.105 新加的「重试跳过」逻辑配合时会出问题。** 在 `DEAD_ARM_COOLDOWN_HOOK=0` 的情况下，WA 把会话钉在死号上时，实测**连打 6 次死号**才换到别的号。生产现在是 `=1`，实测 2 次就换走了。但只要有人把这个开关关回 0，就会出现这个放大效应。
8. **`model_group_retry_policy` 和 `fallbacks` 都要用客户端发来的那个名字（alias）做 key**，用真实组名配置不生效（实测）。生产有 24 个 alias，如果有哪条 retry_policy 或 fallback 是按真实组名写的，对走 alias 进来的请求就是静默失效。**我没有核对生产这 11 条和 129 条配置**。
9. **配了 `context_window_fallbacks` 以后，没在里面登记的组遇到 ContextWindowExceeded 会直接失败，不会再走普通 `fallbacks`。** 实测：组 A 有普通 fallback、但 cw 列表里没有 A，结果打 1 次就报错。生产有 5 条 cw 规则，不在这 5 条里的组都受影响。

---
## 1. 一个失败请求的完整控制流

### 1.1 调用栈（chat 路径；responses 路径在 §5）

| 步 | 位置 | 做什么 |
|---|---|---|
| ① | `router.py:2779+` `acompletion` | 设 `original_function=self._acompletion`，调 `async_function_with_fallbacks` |
| ② | `router.py:7542` `async_function_with_fallbacks` | 初始化 metadata（`attempted_fallbacks=0`、`original_model_group`，7554-7569）；pop `disable_fallbacks`（7571）；调 `async_function_with_retries`（7589-7591）；异常 → `async_function_with_fallbacks_common_utils`（7599-7610） |
| ③ | `router.py:7677` `async_function_with_retries` | 第 1 枪 `make_call`（7710）；失败后算 `num_retries`、查 retry policy、调 `should_retry_this_error`、sleep，再循环 `range(num_retries)`（7794）；用尽后抛**最后一个**异常（7814、7873） |
| ④ | `router.py:3672` `_acompletion` | `async_get_available_deployment` 选台（3690）→ `litellm.acompletion`（3776）；出错时给异常盖 `failed_deployment_id`（3832-3834） |
| ⑤ | `router.py:7272` `common_utils` | 顺序：(a) disable/guardrail 直接抛（7300）；(b) **order 分层兜底**，只在组内 deployment 有 >1 种 `order` 时生效（7333）；(c) **weighted failover**（7373-7383）；(d) 非标准格式的 client-side fallback（7391）；(e) ContextWindow → `context_window_fallbacks`（7406-7428）；(f) ContentPolicy → `content_policy_fallbacks`（7439-7461）；(g) 普通 `fallbacks`（7471-7512）；全挂 → 抛**原始**异常（7539） |
| ⑥ | `fallback_event_handlers.py:582` `run_async_fallback` | 入口处 `if fallback_depth >= max_fallbacks: raise`（622-623）；按顺序遍历兜底列表，每一项 `fallback_depth+1`（675）后**递归**调 `async_function_with_fallbacks`（689）；`attempted_targets` 用来去重（636-665） |
| ⑦ | 嵌套那一跳 | 再走一遍 ②③⑤。查兜底链时按 `fallback_lookup_groups` 的顺序：pre-routing 选中的 tier → 路由到的组 → 请求的组 → **原始组**（`fallback_event_handlers.py:397-413`）。所以一个**自己没配兜底的组，会接着走原始组的那条链** |

### 1.2 一个请求最多打多少次上游（实测）

配置用的是生产 router_settings 的取值（`01-router-exp.py` / `exp7.py`），sleep 被替换成 0：

| 场景 | 上游总次数 | 分组 |
|---|---|---|
| A(3台)→[B(2),C(2)]，全组 500 | **32** | A12 B10 C10 |
| 同上，`enable_weighted_failover=false` | 18 | 每组 6 = 1+5 |
| A(40)→[B(20),C(10)]，全组 500 | **84** | A36 B36 C12 |
| 同上，全组 503 | 84 | 同上 |
| 同上，全组 429 | 66 | A36 B20 C10，每台只打 1 次（429 一打就冷却） |
| 同上，全组 400 | 14 | A6 B6 C2（400 不重试，**但仍然走 WF 和跨组兜底**） |
| 同上，只有 A 挂 500 | 37 | A36 → B 成功 |
| 同上，`max_fallbacks=2` | 48 | A18 B18 C12 |
| 链长 8、全是单台组，全组 500 | **54** | 9 个组全打，每组 6 次（**超过 max_fallbacks=5**） |

**为什么 `max_fallbacks` 封不住次数**（读码，和上表数据对得上）：

- `max_fallbacks` 默认 5：`Router.__init__` 依次取参数、`litellm.max_fallbacks`、`ROUTER_MAX_FALLBACKS`（`router.py:1082-1087`；`constants.py:11` `int(os.getenv("ROUTER_MAX_FALLBACKS", 5))`）。
- 深度检查只在 `run_async_fallback` **入口**做（`fallback_event_handlers.py:622`）。被兜底到的那一跳**会先把自己组的 1+num_retries 次跑完**，等它自己要往下兜底时才被这道检查拦住；然后上一层的 `for mg in fallback_model_group` 循环（643）继续遍历列表里剩下的组。所以 `max_fallbacks` 限的是递归深度，**不限列表里会被尝试多少个组**（chain8 实测 depth 0..8 全都出现过，`exp3.py`）。
- **WF 失败之后会把整条链重走一遍**：`_maybe_run_weighted_failover` 接住 `openai.APIError / RouterRateLimitError` 后返回 None（`router.py:7262-7270`），common_utils 接着用新建的 `input_kwargs = {..., **kwargs}`（7303-7307）跑普通兜底。这个 kwargs 里**没有** WF 那一支用过的 `attempted_targets`（WF 用的是另一份拷贝 `failover_kwargs`，7255-7259），去重集合就丢了，B、C 会被再打一轮。`exp3.py` 的 trace 能看到第二轮用的是全新的 attempted 对象（`attempted_obj` 从 0 变成 1），而 B、C 各 10 次正好是 2 轮 × (1+4/5)。
- 每次 WF 也会占用 1 层 depth（`run_async_fallback` 的 675 行对 WF 那一项同样 +1），同时又在同组触发一轮完整的 1+num_retries。

**大致上界**：同组每次调用 `async_function_with_fallbacks` 最多 `1+num_retries = 6` 次；这样的调用次数是「列表里每一项 ×（WF 层数，≤depth 预算）× 2（WF 失败后重走）」。组越大、链越长，次数越多。**能真正按请求封顶的设置是 `litellm.num_retries_per_request`**：每次 `log_retry` 计数（`router.py:8389-8437`），`client` 包装层到上限就抛错（`utils.py:1687-1688`，`core_helpers.py:313-320`）。它的默认值是 None（`__init__.py:547`）。**生产有没有设这一项，我没验证。**

再加上 `timeout=300` 是按单次尝试算的，最坏墙钟 ≈ 次数 × 300s，基本只能靠客户端自己断开。

---

## 2. 重试判定

### 2.1 判定顺序（`async_function_with_retries`）

1. guardrail 拦截 → 直接抛（`router.py:7714`）。
2. `num_retries` 优先级：请求参数 > 异常上带的 `num_retries`（来自 deployment `litellm_params.num_retries`，3861-3878）> router 设置（7688-7692、7720-7725）。
3. **retry policy 先判**：只要请求的 `num_retries != 0`，并且有 `retry_policy` 或 `model_group_retry_policy`，就调 `get_num_retries_from_retry_policy`（7740-7754）。它返回非 None 时，`_retry_policy_applies=True`，**整个 `should_retry_this_error` 都不再调用**（7758、7835）。
4. 没有 policy 适用 → `should_retry_this_error`（7915-7977），不该重试就抛。
5. `num_retries > 0` 才重试，否则 `raise`（7771-7780）。
6. 每次重试失败后重新算健康集合，**重新调用一次** `should_retry_this_error`（7835-7846）。所以一个 429 刚开始还有健康台、重试到一半台都被冷却了，会提前退出。

### 2.2 `should_retry_this_error` 怎么分类（`router.py:7940-7977`）

| 异常 | 行为 | 行 |
|---|---|---|
| ContextWindowExceeded **且** 配了 `context_window_fallbacks`（任意组） | 立刻抛，交给兜底 | 7941-7942 |
| ContentPolicyViolation **且** 配了 `content_policy_fallbacks` | 立刻抛 | 7944-7945 |
| status 不在 `_should_retry` 内（408/409/429/≥500 之外），且不是 401/403 | 立刻抛（400、404、422……都在这里） | 7947-7951；`utils.py:7168-7194` |
| NotFoundError | 立刻抛 | 7953-7954 |
| RateLimitError，健康台=0 且配了兜底 | 立刻抛 | 7956-7962 |
| 401/403 | 组内总台数 ≤1 就抛，否则**重试**（换台） | 7964-7970 |
| 健康台=0 | 抛 | 7974-7975 |
| 其余 | 重试 | 7977 |

1.105 新加的一点：重试时如果上一枪是**不可重试**状态（`_should_retry` 为 False），会把失败的 deployment id 记进 `_retry_skipped_deployment_ids`（7660-7674、7773-7778、7848-7853）。选台时把它剔掉，**但如果剔完一台都不剩，就还用原来的集合**（`router.py:13142-13150` 末尾的 `or healthy_deployments`）。这一点和 §6.2 的 WA 问题直接相关。

### 2.3 retry policy 的坑

- 类型匹配走 MRO，从最具体的类开始，404 优先匹配 `NotFoundErrorRetries`（`get_retry_from_policy.py:58-66`）。`ContextWindowExceededError` 和 `ContentPolicyViolationError` 都是 `BadRequestError` 的子类（`exceptions.py:535、619`）。所以如果某个组配了 `BadRequestErrorRetries`、没配 `ContentPolicyViolationErrorRetries`，**ContextWindow 错误也会在同组被重试 N 次**（表里没有 ContextWindow 专门的字段）。哪一类都没匹配上时用 `DefaultRetries`（66）；设了 `DefaultRetries`，**所有错误**都会绕过 `should_retry_this_error`。
- 实测（`exp`，`model_group_retry_policy={"A":{"BadRequestErrorRetries":2}}`）：400 本来 A 只打 3 次，加了 policy 后变成 9 次（每次调用 3 次 × 3 层 WF）。
- **policy 的 key 必须是客户端发来的名字**：查找用的是 `model_group or _metadata.get("model_group")`（7744），这个 `model_group` 取自 `kwargs["model"]`，也就是 alias 还没解析时的名字（7686）。实测（`exp2`）：policy 写在 `alias-a` 上会生效（3 次），写在真实组 `A` 上**不生效**（11 次）。
- policy 适用时，健康台=0 也会继续重试（不再检查）。这时每次重试都在选台阶段就抛 `RouterRateLimitError`，不会打到上游；**但会进退避等待**，因为 `all_deployments>1` 且健康台为空时会掉到退避计算里（8027-8053）。这一条是读码推断，没有实测墙钟。
- **我没有核对生产那 11 条 policy 里具体设了哪些字段、key 是不是 alias。**

### 2.4 `retry_after=0` 到底什么意思

`_time_to_sleep_before_retry`（`router.py:8010-8053`）：

- 组里不止 1 台，并且还有健康台 → **直接返回 0**，不看响应头，也不看 `retry_after`（8027-8030）。
- 组里只有 1 台，或者健康台为 0 → `_calculate_retry_after`（`utils.py:7231-7254`）：上游 `Retry-After` 在 (0,60] 之间就按它来，加 0~0.75s 抖动；否则 `0.5×2^n`，夹在 `[min_timeout=retry_after, MAX_RETRY_DELAY=8]` 之间（`constants.py:495-497`）。
- 所以 `retry_after=0` 的意思是「退避没有下限」，**不是不等**。实测单台组 500/429：6 枪之间等了 `0.66, 1.12, 2.59, 4.05, 8.6, 8.52` 秒，合计约 25s（`exp` 的 `computed_sleeps`）。

---

## 3. 冷却

### 3.1 冷却触发链

入口有四个：
- 常规失败回调 `deployment_callback_on_failure`，挂在 **同步** `litellm.failure_callback` 上（`router.py:1180-1183`、8242-8341）。
- 兜底那一跳失败时的补触发 `_trigger_cooldown_for_failed_deployment`（`fallback_event_handlers.py:46-154`）。它只在 `has_logged_async_failure` 为真时才跑（711-718）；而流式请求根本不会设这个标志（`litellm_logging.py:2273-2277`）。
- pre-call 的 rate limit 检查（`router.py:8678`）。
- proxy 的 `proxy_server.py:4457`（没有展开读）。

`_set_cooldown_deployments`（`cooldown_handlers.py:427-489`）分两道：

**A. `_should_run_cooldown_logic`（271-327）**：deployment 找不到组，或者 `time_to_cooldown≈0`（298），或者 `disable_cooldowns`，或者 `_is_cooldown_required` 为 False → 都不冷却。
`_is_cooldown_required`（218-268）：异常文本含 `APIConnectionError` → 不冷却；4xx 里只有 **429/401/408/404** 冷却，其余 4xx（400/403/422……）**不冷却**（259 行注释 `Do NOT cool down all other 4XX Errors`）；5xx 和非数字状态码全部冷却。

**B. `_should_cooldown_deployment`（330-424）**：
1. deployment 级的 `model_info.allowed_fails_policy` / `allowed_fails` 优先（364-373）。
2. router 上 **既没有 `allowed_fails_policy`、`allowed_fails` 也等于库默认值** → 走 v2 百分比分支（376-416）：
   - 429 且不是单台组 → 冷却（400-401）
   - 本分钟 100% 失败且 ≥1000 次 → 冷却（402，`SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD`）
   - 失败率 >50% 且 ≥5 次且不是单台组 → 冷却（405-411，`constants.py:87-88、150-151`）
   - `_should_retry(status) is False`（401/404 等）→ 冷却，**单台组也冷却**（413-414）
3. 否则走 v1 计数分支 `should_cooldown_based_on_allowed_fails_policy`：fail 次数 > allowed_fails，计数放在 router `DualCache` 里（561-609）。

**生产走哪个分支**：`allowed_fails: 3` 和 `litellm.allowed_fails = 3`（`__init__.py:545`）相等，`_is_allowed_fails_set_on_router` 返回 False（`cooldown_handlers.py:635-639`）。所以只要生产**没设 `allowed_fails_policy`**（给我的 router_settings 里没有这一项），就走 v2 百分比分支，`allowed_fails=3` 对计数没有任何作用。`dead_deployment_retry.py:45-48` 的注释也是这么写的，和代码一致。

### 3.2 冷却多久

优先级：deployment `model_info/litellm_params.cooldown_time` > 上游 `Retry-After` 头 > router `cooldown_time=60`（`router.py:8293-8314`；兜底路径一样，`fallback_event_handlers.py:111-138`）。

- **`Retry-After` 没有上限**：`_get_retry_after_from_exception_header` 只做 int 解析（`utils.py:7197-7228`），值多大就冷却多久。
- **`Retry-After: 0` 会直接关掉这次冷却**：`time_to_cooldown=0` 时 `_should_run_cooldown_logic` 返回 False（`cooldown_handlers.py:298`）。上游带 `retry-after: 0` 的 429 不会冷却任何 deployment（读码，没有实测上游有没有这样发）。

### 3.3 存在哪里，4 个 pod 共不共享

- 冷却条目：`CooldownCache._cooldown_store`，这是一个单独的 DualCache，Redis 用懒加载方式挂上去（`cooldown_cache.py:49-74`），key 是 `deployment:{id}:cooldown`，TTL = 冷却时长（121-125）。写入走 `set_cache`，**同时写内存和 Redis**（`dual_cache.py:150-162`）。
- 读取：`async_batch_get_cache` 内存没命中时，**同一个 key 最多每 1s 去 Redis 读一次**（`DEFAULT_COOLDOWN_REDIS_READ_INTERVAL_SECONDS=1`，`constants.py:94`；`dual_cache.py:317-323`）。从 Redis 回填到内存时会把 TTL 改成剩余时长（`cooldown_cache.py:135-157`）。
- 结论：**router 配了 Redis 的话，冷却结果在 4 个 pod / 所有 worker 之间共享，别的 pod 大约 1s 内能看到。** Router 只在 router_settings 带 `redis_host+redis_port` 或 `redis_url` 时才建 RedisCache（`router.py:947-979`）。git 里 `k8s/litellm-proxy-canary-config.yaml:1226-1227` 有 `redis_host: carher-redis...`，但 **198 生产现在生效的 router_settings 里有没有 Redis，我没验证**（给我的「已测量值」里没有这一项）。
- **决定要不要冷却的成功/失败计数不共享**：`{id}:successes` / `{id}:fails` 都是 `local_only=True`、TTL 60s（`track_deployment_metrics.py:45-75`）。所以「>50% 且 ≥5 次」这个门槛是**每个 worker 进程各算各的**。流量被 4 pod×N worker 摊开以后，每个进程的分母都变小，「≥5 次」更难凑够；429 和 401/404 不需要计数，所以不受影响。

### 3.4 一个组全部被冷却时

- `async_get_healthy_deployments` 过滤后为空 → `async_raise_no_deployment_exception` 返回 `RouterRateLimitError`（`router.py:13152-13158`；`handle_error.py:71-97`）。它是 **ValueError，不是 APIError**（`types/router.py:964`），消息是 `No deployments available ... All deployments for selected model are in cooldown`（977-988）。
- 这个异常照样进 common_utils，走普通兜底：实测 A 全部冷却、配了 A→B，**A 打了 0 次，B 直接接住**（`exp2` `all_A_cooled_then_request`）；没配兜底就把 `RouterRateLimitError` 抛给客户端（`all_A_cooled_no_fallback`）。它映射成哪个 HTTP 状态码要看 proxy 层，**我没验证**。
- 唯一会无视冷却的情况：`enable_health_check_routing` 开着**并且** `allowed_fails_policy` 不为 None（`router.py:13076-13080`），生产不满足（读码 + router_settings 没有这两项）。

---

## 4. 选台顺序

### 4.1 `async_get_available_deployment`（`router.py:13282-13431`）

1. 先确认策略在异步白名单里，simple-shuffle 在（13295-13308）。
2. **pre-routing hook**：可以改 model/messages（13317-13327），之后套用 tier 参数（13328-13335）。
3. 解析策略 → `async_get_healthy_deployments`（13341-13352）。
4. 结果是单个 dict（指定了具体 deployment）→ 直接返回（13353-13357）。
5. `_encrypted_content_affinity_pinned` 为真且只剩 1 台 → 直接返回（13360-13364）。
6. simple-shuffle → `simple_shuffle()`：先看请求级 `_router_weights`，再依次按 `weight` / `rpm` / `tpm` 做加权随机，都没有就等概率（`simple_shuffle.py:43-67`）。

### 4.2 `async_get_healthy_deployments` 过滤顺序（`router.py:12995-13160`）

| # | 过滤 | 行 |
|---|---|---|
| 1 | `_common_checks_available_deployment`：alias 解析 → routing group / pattern → 按组名取全部 → model access group 过滤 → reserved 过滤；空了就看有没有默认兜底；还空就抛 `BadRequestError "...There are no healthy deployments for this model"` | 13014-13020；12737-12908，抛出点 12896-12901 |
| 2 | team 过滤 | 13024-13027 |
| 3 | web search 过滤 | 13032-13035 |
| 4 | 后台 health-check 不健康的剔掉（`enable_health_check_routing` 关着时什么也不做） | 13050-13054 |
| 5 | **剔掉冷却中的** | 13057-13072 |
| 6 | 剔掉 admin 暂停的（blocked） | 13082 |
| 7 | **`async_callback_filter_deployments`**：按 `litellm.callbacks` 的顺序串起来，每个 CustomLogger 的 `async_filter_deployments` 都跑一遍（WA、EncryptedContentAffinity、PromptCaching、DeploymentAffinity 都在这里） | 13084-13090；8715-8757 |
| 8 | `_pre_call_checks`（`enable_pre_call_checks` 打开、有 messages/input 时）：context window、rpm、region、response_format；**一台都过不了时**按原因抛 `RouterRateLimitErrorBasic` 或 `ContextWindowExceededError` | 13092-13108；12444-12626，抛出点 12610-12620 |
| 9 | tag 过滤（`enable_tag_filtering`） | 13110-13116 |
| 10 | routing plugin 候选 | 13119-13122 |
| 11 | `_target_order`（order 分层） | 13125-13128 |
| 12 | `_excluded_deployment_ids`（WF 排除名单），**一定会执行** | 13133-13137 |
| 13 | `_retry_skipped_deployment_ids`（1.105 新加），**剔空了就退回原集合** | 13142-13150 |
| 14 | 空 → `RouterRateLimitError` | 13152-13158 |

需要注意的几点：

- **callback 过滤（第 7 步）在冷却之后、tag / WF 排除 / retry-skip 之前。** WA 在第 7 步把候选收成 1 台以后，后面任何一步把这台剔掉，候选就会变成 0 → `RouterRateLimitError`。WA 自己读 `_excluded_deployment_ids` 并提前避开（`weighted_affinity.py:631-646`），这是针对第 12 步做的补丁。**第 13 步（retry-skip）它不读**，见 §6.2。
- **`dead_deployment_retry` 匹配的那句话，是在第 1 步抛出来的。** 母 router 自己产生它的条件只有一个：按组名一个 deployment 都找不到（`router.py:12896-12901`）。死号场景里这句话是**叶子 litellm 返回的 400 body**，母 router 把它原样包成 BadRequestError。补丁注释写的是「全包唯一产生点 router.py:10979」，那是旧版本的行号，1.105.1 里这个点在 12898（字符串常量在 `types/router.py:737`）。字符串本身没变，指纹仍然成立。
- callback 的执行顺序 = `litellm.callbacks` 的注册顺序。`EncryptedContentAffinityCheck` 会被挪到 `DeploymentAffinityCheck` 前面（`router.py:2297-2298`），但它和 WA 之间谁先谁后，取决于 proxy 的 `litellm_settings.callbacks`（`proxy_server.py:6419`）和 Router 初始化（6992）谁先跑。**我没有核对 198 上实际的 `litellm.callbacks` 顺序**。

### 4.3 内置 pre-call check（生产 `optional_pre_call_checks`）

- **`responses_api_deployment_check`**：在 1.105.1 里**不会**实例化 `ResponsesApiDeploymentCheck`（那个类已标为 deprecated，`responses_api_deployment_check.py:24-31`）。它会被并进 `DeploymentAffinityCheck(enable_responses_api_affinity=True)`（`router.py:2330-2367`）。这个类只做 `previous_response_id` 粘性（`deployment_affinity_check.py:358-390`）；用户 key / session 粘性只有在 `deployment_affinity` / `session_affinity` 也打开时才生效，生产的列表里没有这两项。`weighted_affinity.py:654-668` 注释里说的「交给内置 responses_api_deployment_check」，**行为上仍然对**：有 `previous_response_id` 就钉回原 deployment，钉不上就原样放行（387-390）。
- **`prompt_caching`**：`PromptCachingDeploymentCheck` 只对 **messages** 生效（`prompt_caching_deployment_check.py:70`），而且只记录 completion / anthropic_messages 这几种 call type（120-130）。**/v1/responses（用的是 input）完全不受它影响。** chat 请求命中缓存时会把候选收成 1 台（106-110），这时它和 WA 谁说了算取决于 callback 顺序。
- **`encrypted_content_affinity`**：`input` 里的 `encitem_` / `litellm_enc:` 能解出 model_id 时，钉到那台，或者钉到同一加密边界上的几台（`encrypted_content_affinity_check.py:350-377`）。**origin 不可用时，1.105.1 自己就会剥掉密文，放行到健康池，一个 raise 都没有**（379-392；整个文件 `raise` 出现 0 次）。见 §6.3。

### 4.4 WA 钉住的那台冷却 / 失败时会怎样

- 钉住的那台**被冷却**：第 5 步把它剔掉，WA 看到 `pinned not in healthy set` → **绕行，pin 保留**（`weighted_affinity.py:741-752`）。替补台加权选出来以后写进 detour 记录（1032-1057）；连续不可用超过 `detour_repin_after=300s` 才真正改钉（971-998）。
- 钉住的那台**失败但没被冷却**（比如 400/403/422，或者没到百分比门槛的 500）：
  - 可重试错误（5xx/429/408）→ WF 带上 `_excluded_deployment_ids`，WA 先把排除的剔掉（631-646）→ 绕行。
  - **不可重试错误（400 等）**→ 1.105 的 retry-skip 生效（第 13 步）。实测（`exp4.py`，4 台、pin 到 X、X 返回 400）：**2 次**（X → 别的台）。这是 WF（`_excluded_deployment_ids`）起的作用，因为 WF 对任何带 `failed_deployment_id` 的异常都会触发（`router.py:7209-7211`），并不区分能不能重试。
  - 所有实测场景里，**pin 最后都还指向那台失败的**（`pin_still_points_to_failed: true`），符合 WA 「临时不可用只绕行」的设计。
- WA 的 fail-mark（`async_log_failure_event`，`weighted_affinity.py:543-572`）挂在 **async** failure 回调上，跟 router 冷却（sync 回调）是两条独立的链。

---

## 5. 流式中途兜底（1.105 重写）

### 5.1 /v1/responses 链路

`aresponses` → `_aresponses_with_streaming_fallbacks`（`router.py:6869-6873`、5416-5428）→ `_ageneric_api_call_with_fallbacks(attempt_function=_ageneric_api_call_with_fallbacks_responses_attempt)`（5272-5303）→ 常规的 `async_function_with_fallbacks`。**每一次尝试**都是 `_..._responses_attempt`（5430-5456）：调 helper 拿到流，`stream=True` 时用 `_aresponses_streaming_iterator` 包一层，并带上**这一跳自己的** `hop_kwargs`（`mid_stream_fallback_hop_kwargs`，`fallback_event_handlers.py:338-362`；`attempted_targets` 是浅拷贝共享的，所以全链去重）。

### 5.2 什么情况算可以兜底（上游 `ResponsesAPIStreamingIterator`）

- 流内出现 `error` / `response.failed` 事件 → 映射成异常（`streaming_iterator.py:650-671`）。状态码从 `code/type` 查表：`rate_limit*`/`insufficient_quota`→429，`server_error`→500，`invalid_request_error`/`context_length_exceeded`→400……**认不出来的一律算 500**（`_status_code_for_error_fields`）。可以兜底的条件：ContentPolicy，或者 status≥500，或者 429，或者没有状态码（`_mid_stream_fallback_eligible`，281-285）→ 抛 `MidStreamFallbackError(is_pre_first_chunk=not _yielded_first_chunk, generated_content=已生成文本)`。
- 流结束却没有终止事件（`completed_response is None`）→ `APIConnectionError`。**还没开始输出**（`_output_started` 为假）时包成 MidStreamFallbackError，已经开始输出就直接抛（979-986）。
- `httpx.ReadError/RemoteProtocolError` 的处理方式相同（988-992、1079-1083）。
- `_output_started` 的含义是：已经 yield 过**除 `response.created/in_progress/queued` 之外**的事件（954-957、288）。

### 5.3 router 包装层（`_aresponses_streaming_iterator`，`router.py:3232-3420`）

- **扣住生命周期事件**：`response.created/in_progress/queued` 先不发给客户端，最多扣 200 个（`router.py:612-620`、3383-3395）。真正的输出一出现，先把扣住的发出去，再发输出。
- 捕获到 `MidStreamFallbackError`（3396）→ `_aresponses_fallback_attempt`（3422-3516）→ 用 `hop_kwargs` 调 `async_function_with_fallbacks_common_utils`，**同组 WF 和跨组兜底全都会走一遍**（3477-3487）。
  - `generated_content` 非空**并且** `not is_pre_first_chunk` → 在 input 末尾追加一条「接着写，别重复」的 developer 消息和一条 assistant 消息，内容是部分输出（3457-3461、3172-3230）。
  - 兜底流的 usage 会和部分 usage 合并（3492-3493）。
  - 兜底也失败：如果兜底还一个事件都没吐，就把扣住的事件补发出去，然后抛 `original_exception`（3499-3506）。
- **注意（读码）**：这个包装层**没有**像 chat 路径那样「已有生成内容就拒绝兜底」的检查（对比 chat 的 2956-2961）。对 responses 来说，**哪怕已经给客户端发过文字增量，只要上游后面在流里报了 5xx/429，也会兜底**，客户端在同一条 SSE 流里会接着收到兜底模型的事件（新的 `response.created`、新的 response id，等等）。上游对这种情况的唯一处理是那条 continuation prompt。**客户端（Codex/Cursor）收到同一条流里两套 response 生命周期会怎么处理，我没验证**。只有「流没有终止事件」和「传输层错误」这两种情况受 `_output_started` 的限制（984-985、990-991）。
- 客户端看到的：兜底成功 → 同一个 HTTP 200 流，`_hidden_params` / headers 换成兜底那边的（3341-3345、3488）。兜底失败 → 已经发出 200 头之后，在流里收到一个错误（proxy 层怎么渲染这个错误，我没有展开读）。

### 5.4 chat 路径（对照）

`_acompletion_streaming_iterator`（`router.py:2893-3053`）：`not is_pre_first_chunk` **并且** 已经有生成内容 → 不兜底，抛原始异常（2956-2961）；否则走 common_utils 兜底（2983-2993）。

---

## 6. 我们的补丁 vs 1.105.1 上游

安装情况实测（`exp2.py`，把 live-cb 模块叠在 1.105.1 上 import）：`should_retry_this_error` / `_is_cooldown_required`（在 `DEAD_ARM_COOLDOWN_HOOK=1` 下）/ 两处 `run_async_fallback` / `async_get_available_deployment` / `_ageneric_api_call_with_fallbacks_helper` **全部挂上了**，也就是所有指纹守卫都通过。这和 `eff-prod.txt` 里测到的 final_owner 一致。

### 6.1 `dead_deployment_retry.py`

- **改了什么**：异常是 400 并且文本含 `There are no healthy deployments for this model`（排除 ContextWindow/ContentPolicy）时，(a) `should_retry_this_error` 直接返回 True（`dead_deployment_retry.py:577-583`）；(b) `DEAD_ARM_COOLDOWN_HOOK=1` 时 `_is_cooldown_required` 返回 True（601-630）。
- **指纹**：`_num_healthy_deployments` 在 `should_retry_this_error` 里出现了 4 次（`router.py:7932-7974`）；`Do NOT cool down all other 4XX Errors` 在 `cooldown_handlers.py:259`。都还在。
- **它的前提和 1.105.1 对不对得上**：
  - 「400 不进冷却」：对，`cooldown_handlers.py:257-260`。
  - 「冷却钩子一返回 True，第一枪就能冷却」：对。走 v2 分支，`_should_retry(400) is False → True`（413-414）。实测 `exp2` `dead400_patched_all_dead`：A、B 一共 5 台全部进了冷却。
  - 「`num_retries: 2`（共 3 枪）」：**生产现在是 5**，注释过时了。
  - **钩子 (a) 在多数情况下用不上**：如果生产对这些组配了 `model_group_retry_policy` 并且命中 `BadRequestErrorRetries` / `DefaultRetries`，`should_retry_this_error` **根本不会被调到**（`router.py:7758`）。生产 11 条 policy 的具体内容我没核对。
- **和 1.105 新功能的配合**：
  - 1.105 新加的 retry-skip（§2.2）对 400 本来就会换台；WF 也会把失败那台排除（`router.py:7209-7234`）。实测**不装补丁**的时候，pin 到死号上的请求也是 2 次就换走（`exp4b.py`）。对「同组还有活腿」这种情况，钩子 (a) **在 1.105.1 里已经没必要了**。
  - ⚠️ **装了钩子 (a)、但 `DEAD_ARM_COOLDOWN_HOOK=0` 的时候，会变差**：实测连打死号 6 次才换走（`exp4.py`，`PIN×6 → other`）。原因：(a) 让 400 进了同组重试循环；retry-skip 剔掉 X 后候选还剩 3 台，看起来没问题，但 **WA 在第 7 步就把候选收成了 [X]**（它不读 `_retry_skipped_deployment_ids`），第 13 步剔掉 X 后候选为空，于是退回原集合 [X]（`router.py:13145-13150` 的 `or healthy_deployments`），结果每次重试都打回 X，直到 6 次用完、WF 介入。生产现在是 `=1`，实测 2 次（冷却把 X 拿掉了），**不触发**这个问题。但这个组合很脆弱：开关一关回 0，死号放大就是 6 倍。
  - 钩子 (b) 现在仍然是**唯一**能让「叶子返回 no-healthy 400」的死腿离开池子的办法，上游不会冷却 400。这一条还有用。
- **自动摘号**：`DEAD_ARM_AUTO_DELETE_DISABLED=1`，`bump_streak` 不跑（606-607）。计数在进程内（137-142），本来就不跨 pod。

### 6.2 `weighted_affinity.py`

- **改了什么**：以 `async_filter_deployments` 的身份把候选收成 1 台，实现会话粘性，pin 存在 router 的 Redis（`weighted_affinity.py:227-266`）；另外还有 fail-mark、绕行 / 改钉的逻辑。
- **它依赖的上游行为，现在是否还成立**：
  - 「callback 在 tag 路由之前跑」：成立（第 7 步 vs 第 9 步，`router.py:13084` vs 13110）。
  - 「router 在本 hook **之后**才 pop `_excluded_deployment_ids`」：成立（13133）。
  - 「`healthy_deployments[i]['model_info']['id']`」：成立。
  - 「下游 simple-shuffle 只在这 1 台上 shuffle」：成立（`simple_shuffle.py:43-67`）。
  - 注释里的行号（`router.py:11150 / 11168`）是旧版本的。
- **新冲突**：1.105 新加的 `_retry_skipped_deployment_ids`（第 13 步）**WA 不知道它的存在**。WA 把 pin 收成 [X] 以后，retry-skip 剔空就会退回 [X]，不可重试的错误会在同一台上反复重试。只有当某个补丁或 retry policy 让 400 类错误进入同组重试时才会触发（§6.1 实测的 6 次就是这么来的）。**建议**：WA 照着 `_excluded_deployment_ids` 的做法，也读一下 `_retry_skipped_deployment_ids` 并提前剔掉（631-646 那段）。
- `pinned_returns_500` 和 `plain_400` 两种情况实测都是 2 次就换走，pin 保留，和设计一致。

### 6.3 `encrypted_content_degrade_strip.py`：⚠️ 在 1.105.1 上不再起作用

- **它的前提**（docstring 第 10-13 行）：origin 不可用时，affinity check **raise** BadRequest/RateLimit/ServiceUnavailable，请求直接失败。补丁接住这三类异常，剥掉密文后返回健康集合（`encrypted_content_degrade_strip.py:131-172`）。
- **1.105.1 的现实**：`EncryptedContentAffinityCheck.async_filter_deployments` **一个 raise 都没有**（整个文件 `raise ` 出现 0 次）。origin 不在健康集合、也没有同加密边界的台时，它**自己剥密文**（`ResponsesAPIRequestUtils.strip_encrypted_reasoning_from_input` + `strip_encrypted_reasoning_from_messages`），然后返回健康池（`encrypted_content_affinity_check.py:379-392`）。对照：1.103.2 还有 `raise await self._unavailable_origin_error(...)`（`.research-litellm/litellm-1.103.2/.../encrypted_content_affinity_check.py:393`）。
- **结论**：这个补丁的 `except` 分支**在 1.105.1 上永远走不到**，包装层只是原样转发，属于**多余但无害**的代码。它没有指纹守卫，所以不会报错，只会继续以「已安装」的状态出现在 eff-prod 里。
- **行为差异**（仅供参考，因为补丁本来就不会执行到）：上游剥除时，有可读 summary 的 reasoning item **会保留下来**，只去掉 `encrypted_content` / `id`（`responses/utils.py:581-595`），并且**不会 pop `previous_response_id`**。补丁的做法是整条删掉 `reasoning`/`compaction` item，并 pop `previous_response_id`（`encrypted_content_degrade_strip.py:74-101`）。依赖「降级时 previous_response_id 被删掉」的地方，在 1.105.1 上**已经拿不到这个行为了**。
- **可以删**：先在 198 上 grep `[degrade_strip] origin unavailable` 这条日志，确认最近没有出现过，再删。我不能 ssh，**这一步没做**。

### 6.4 `chatgpt_responses_normalize.py` 的 `run_async_fallback` 包装

- **改了什么**：原始组命中 `_TARGET_MODEL_PARTS`（`chatgpt_responses_normalize.py:31-32`），兜底目标是含 `wangsu` 且含 `gpt-5.5` 的组，并且原始异常属于「请求形状错误」（400/401/403/404/422，带 encrypted 字样的除外）时，不去兜底，直接抛（613-628）。
- **上游行为还是不是它假设的那样**：`run_async_fallback` 的签名、`original_model_group` / `fallback_model_group` / `original_exception` 这几个 kwargs 都还在（`fallback_event_handlers.py:582-592`）；`router.py:218` 用的是 `from ... import run_async_fallback`，所以只替换 module 属性对 router 不起作用。补丁同时替换了 `router_mod.run_async_fallback`（633），实测两处都已替换。
- **1.105 带来的变化**：
  - WF 那一跳也会调 `run_async_fallback`（`router.py:7261`），它的 `fallback_model_group=[{"model": 原组, "_excluded_deployment_ids": ...}]` 里是 dict，`_targets_chatgpt_to_wangsu` 只看 `isinstance(group, str)`，所以**不拦截**。符合预期（同组换台允许）。
  - **漏拦的情况**：守卫只检查**这一次调用**的 `fallback_model_group`。如果 chatgpt 组的兜底链是 `[X, wangsu-gpt-5.5]`，`X` 自己没配兜底，那么 X 失败后会**接着走原始组的链**（`fallback_lookup_groups` 带上了 `original_model_group`，`fallback_event_handlers.py:397-413`）。这时 `original_model_group=X`，`_is_target_model(X)` 可能为假，守卫就会放行。而且 `original_exception` 每一跳都换成新的异常，不一定还是最初那个形状错误。
  - 另外，`run_async_fallback` 内部会把兜底列表里已经尝试过的跳过，并且有 `fallback_access_check` / `fallback_budget_check` 这两道检查（646-656），守卫在它们之前执行，没有冲突。
- **它和 1.105 原生的 4xx 处理是什么关系**：上游 common_utils **不按状态码拦截跨组兜底**（400 也会走普通兜底，实测 `big_400_all_fail` 打了 A6 B6 C2）。所以这个守卫**仍然有用**，不重复。

### 6.5 `streaming_output_backfill.py`（只看 router 相关的三段）

**(a) `_install_responses_failed_deployment_stamp`**（`streaming_output_backfill.py:463-568`）
- 前提（注释 464-470）：「`_ageneric_api_call_with_fallbacks_helper` 没有调用 `_set_failed_deployment_id_on_exception`」。**在 1.105.1 上已经不成立**：helper 的 except 里有 `if deployment is not None: self._stamp_failed_deployment_id_with_effective_model_info(e, deployment, kwargs)`（`router.py:5412-5413`），1.103.2 也一样。
- 现在实际的效果：上游**不管错误类型都会盖章**。补丁的包装在 helper **外层**（`_patched_helper` → `_orig_helper`），等它的 except 执行时，上游已经盖过章了，`if not getattr(exc, "failed_deployment_id", None)`（542）永远是假，**补丁什么也不做**。它原本想要的「只对可重试错误盖章，避免对 400 白白多打一枪」，**在 1.105.1 上也已经失效**：上游给 400 也盖了章，WF 也就会对 400 换台再试一次。chat 路径的实测是 `big_400_all_fail` 里 A=6 次，全部来自 WF 的换台；responses 路径是读码（5413 和 `_acompletion` 的 3834 是同一套盖章逻辑），没有单独实测。
- 结论：**多余**；补丁注释里声称的「400 不会触发 WF」**在生产上不成立**。

**(b) `_install_streaming_output_backfill` 里的 capacity 升格**（199-337）
- 1.105.1 上游已经原生处理流内的 `response.failed` / `error` 事件（`streaming_iterator.py:650-671`）。两者执行顺序：补丁先在 `_process_chunk` 里 raise，上游的 `_maybe_raise_for_error_event` 根本跑不到。
- **分类口径不同**：上游认不出来的 code 一律算 500，然后兜底；补丁的 `_classify_stream_failure` 对 `invalid_request_error` / `context_length_exceeded` 等返回 `surface`（119-121），不 raise，交给上游处理。上游把它映射成 400，不在兜底范围内，直接抛。两边结果基本一致。
- **差别 1：补丁在 `_bp_content_started` 之后不 raise，把失败事件透传给客户端**（260-265）。上游会继续把它包成 MidStreamFallbackError（`is_pre_first_chunk=False` 也照样包，router 照样兜底，§5.3）。补丁在这里**把上游会做的兜底挡掉了**，对「已有输出不要重放」这个目标来说是更保守的做法。
- **差别 2：补丁 raise 的 `MidStreamFallbackError(is_pre_first_chunk=True)` 是硬编码的**（313），上游是按 `_yielded_first_chunk` 算的。
- **差别 3：补丁的 raise 发生在 `_process_chunk` 内部。** 上游 `_next_event` 的 `except Exception`（`streaming_iterator.py:1089-1092`）会调 `_handle_failure`，这里又会碰到 (c)。
- 结论：大部分**和上游重复**；剩下的价值只有「已出内容就不兜底」这一条策略差异。如果要删，需要先决定 responses 路径是否接受上游的「已出内容也兜底」行为。

**(c) `_install_empty_stream_escalation`**（381-460）
- 上游已经原生处理：流结束且 `completed_response is None` → `_raise_if_ended_without_terminal_event`，没开始输出就抛 MidStreamFallbackError（`streaming_iterator.py:979-986`、1054-1057）。补丁包在 `__anext__` 外层，等着接 `StopAsyncIteration`。但上游在这种情况下抛的是 MidStreamFallbackError，不再是 StopAsyncIteration，**所以补丁这条路径基本走不到了**。只剩一种情况可能走到：`_bp_saw_terminal` 为假，但上游的 `completed_response` 不为空（例如收到的是 `response.failed`）。**这种情况会不会发生，我没验证**。

**(d) `_install_nonblocking_responses_failure_handler`**（587-651）⚠️ **这是目前最需要处理的一处**
- 补丁把 `_handle_failure` 改成：有运行中的事件循环时，只 `create_task(async_failure_handler)`（626-635），**不再提交同步的 `failure_handler`**。
- 上游 1.105.1 的 `_handle_failure`（`streaming_iterator.py:886-918`）：有事件循环时，在后台 task 里**先**跑 `async_failure_handler`，**然后**跑 `_submit_sync_failure_handler`（920-931），而且**本来就不会阻塞**。补丁要修的那个问题（`run_async_function` join 线程池导致事件循环死锁，注释 588-599），在 1.103.2 上确实存在（1.103.2 的 `_handle_failure` 直接调 `run_async_function`），**在 1.105.1 上已经被上游修好了**。
- 实测（`exp6.py`）：上游原版会调到 `async_failure_handler` 和 `failure_handler` 两个；打上补丁后**只有 `async_failure_handler`**。
- router 的冷却回调 `deployment_callback_on_failure` 注册在**同步**的 `litellm.failure_callback` 上（`router.py:1180-1183`），`async_failure_handler` 只遍历 `litellm._async_failure_callback`（`litellm_logging.py:3918-3921`）。所以：**生产上 /v1/responses 流内失败（`response.failed`、空流、读流断开、补丁 (b) 主动升格的那些）都不会让 router 冷却这个 deployment。** 那条兜底补触发路径 `_trigger_cooldown_for_failed_deployment` 也帮不上忙：它只在 `has_logged_async_failure` 为真时才跑（`fallback_event_handlers.py:711-718`），而流式请求不会设置这个标志（`litellm_logging.py:2273-2277`）。另外它只会在 `run_async_fallback` 的 except 里跑，而走中途兜底的那一跳通常是**成功**的。
- 影响：坏号在流内失败后不冷却，下一个请求还可能选到它。WA 的 fail-mark 走的是 async 链，**仍然有效**（`weighted_affinity.py:543-572`），所以有 WA 的组还能靠 fail-mark 绕开坏号；没有 WA 的组就只能靠 WF 的单请求排除。这是读码加 `exp6` 得出的结论，**生产日志里有没有「流内失败但 cooldown 列表为空」的现象，我没查**。
- **建议**：1.105.1 上直接**删掉 (d)**，上游的实现已经同时做到了不阻塞和冷却记账。删之前在 198 上验证：制造一次流内 `response.failed`，看 `/cooldown` 列表或日志里有没有出现这个 deployment。

---

## 7. 运维容易误解的点

| 看起来像 | 实际上 | 证据 |
|---|---|---|
| `allowed_fails: 3` = 3 次失败才冷却 | 和库默认值相等 ⇒ 当没设，走百分比分支；429/401/404 一次就冷却 | `cooldown_handlers.py:376-378、635-639` |
| `max_fallbacks=5` = 最多兜底 5 次 | 只限递归深度；列表里的组都会被尝试；WF 失败后整条链再走一遍 | §1.2；`fallback_event_handlers.py:622、643`；`router.py:7262-7270、7303` |
| `num_retries: 5` = 一个请求最多 6 次 | 每个组每次进入都是 6 次，乘上 WF 层数和兜底组数；实测 84 次 | §1.2 |
| `retry_after: 0` = 不等待 | 多台组本来就不等（和这个参数无关）；单台组仍然按 0.5→8s 指数退避 | `router.py:8027-8030`；`utils.py:7246-7254` |
| `timeout: 300` = 请求最多 5 分钟 | 每次尝试单独算 300s | 读码（作用在每次 `litellm.acompletion` 调用上） |
| `enable_weighted_failover` = 同组换一台 | 每一跳失败都触发；占用 depth；它失败后的跨组兜底会丢掉去重集合；对 400 也会换台 | `router.py:7195-7270、7373-7383` |
| 400 不会兜底 | 400 不重试、不冷却，**但会 WF 换台，也会跨组兜底** | 实测 `big_400_all_fail` = 14 次 |
| retry policy 用真实组名配置 | key 必须是客户端发来的名字（alias）；`fallbacks` 也一样 | 实测 `exp2`、`exp5`；`router.py:7686、7744` |
| 配了几条 `context_window_fallbacks` 是锦上添花 | 一旦配了，**没在里面的组**遇到 ContextWindow 错误就不重试、也不走普通兜底，直接失败 | `router.py:7941-7942、7406-7415`；实测 `exp5` |
| 冷却是全集群统一判定 | 判定用的计数在本进程，结果写进 Redis 后才共享 | `track_deployment_metrics.py:53-75`；`cooldown_cache.py:121-125` |
| `Retry-After` 头只是参考 | 它直接决定冷却时长，没有上限；`0` 会让这次不冷却 | `router.py:8298-8314`；`cooldown_handlers.py:298` |
| `responses_api_deployment_check` 是一个独立 check | 1.105 里被并进 `DeploymentAffinityCheck`，只做 previous_response_id 粘性 | `router.py:2330-2367` |
| `prompt_caching` 会让 Codex 会话粘住 | 只看 messages，/v1/responses 不受影响 | `prompt_caching_deployment_check.py:70、120-130` |
| `BadRequestErrorRetries` 只影响普通 400 | ContextWindow/ContentPolicy 都是它的子类，会一起被重试（除非单独配了 CPV） | `exceptions.py:535、619`；`get_retry_from_policy.py:62-66` |
| `DefaultRetries` 是「其他错误」的兜底值 | 设了以后所有错误都绕过 `should_retry_this_error`（连 404/400 都会重试） | `router.py:7740-7766`；`get_retry_from_policy.py:66` |
| 流式请求已经出过字就不会兜底 | chat 路径是这样；**/v1/responses 只要后面在流里报 5xx/429 就会兜底**，同一条流里会出现第二个 response | `router.py:2956-2961` vs `3396-3403、3457-3461` |
| 补丁「已安装」= 在起作用 | `encrypted_content_degrade_strip`、`responses_failed_deployment_stamp` 都装上了，但执行不到 / 不产生任何效果 | §6.3、§6.5(a) |

## 8. 没有验证的部分（下一步可以查）

1. 198 生产 router_settings 里有没有 Redis（决定 §3.3 的跨 pod 共享是否成立），以及 `litellm.num_retries_per_request` 有没有设：`/router/settings` 或 `cli-anything-litellm`。
2. 生产那 11 条 `model_group_retry_policy`、129 条 fallback、5 条 cw fallback 的 key，是不是落在 24 个 alias 上；有没有 `DefaultRetries` / `BadRequestErrorRetries`。
3. 198 日志里 `[degrade_strip] origin unavailable` 和 `responses_failed_deployment_stamp: stamped` 的出现次数（预期都是 0，可以用来确认 §6.3 和 §6.5(a)）。
4. 在 198 上让流内返回一次 `response.failed`，看冷却列表有没有那台 deployment（验证 §6.5(d)）。
5. Codex/Cursor 收到同一条 SSE 流里两个 `response.created` 时的表现（§5.3）。
6. 198 上 `litellm.callbacks` 的实际顺序（WA / EncryptedContent / PromptCaching / DeploymentAffinity 的先后）。
7. /v1/responses 路径的实际尝试次数（这次只实测了 chat 路径）。

## 附：实验脚本

| 文件 | 内容 |
|---|---|
| `deep/01-router-exp.py` | 基础次数矩阵（500/429/400/401、policy、WF 开关、链长 8、单台退避） |
| `deep/01-router-exp2.py` | 装上 live 补丁后的安装检查、死号 400、alias policy、全组冷却 |
| `deep/01-router-exp3.py` | `run_async_fallback` 调用 trace（depth / 列表 / attempted 集合是不是同一个） |
| `deep/01-router-exp4.py` / `4b.py` | WA 钉住的台失败时的尝试序列（有 / 无 dead_deployment_retry，HOOK=0/1） |
| `deep/01-router-exp5.py` | alias 下的 fallback 查找、context_window_fallbacks 的影响范围 |
| `deep/01-router-exp6.py` | `_handle_failure` 上游版 vs 补丁版各调到哪些 failure handler |
| `deep/01-router-exp7.py` | 生产规模组（40/20/10 台）的次数 |
