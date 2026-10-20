# LiteLLM v1.105.1 深读：鉴权 / key 模型权限 / 预算 / 限流

源码：`.work/litellm-src-study-1020/src/litellm/`（下文路径都相对它；`proxy/...` = `litellm/proxy/...`）。
补丁：`live-cb/budget_notice.py`、`live-cb/error_sanitize.py`；镜像烘焙补丁 `litellm-gray-rollout/patches/v1.105.1/litellm-patch-key-alias-auth-compat.py`。
全程只读代码，**没有连生产、没跑请求**。凡是依赖生产运行时状态的结论都标了「未验证」。

---

## 0. 一页结论

1. **有一个运行时事实我没法从本地确认，它决定下面一半结论：生产到底有没有「协调 Redis」（`redis_usage_cache`）。**
   跨 pod 的 spend 计数器、预算预留、reset 租约、缓存失效广播，这几样都只认 `redis_usage_cache`。它只有三个来源：`litellm_settings.cache`、`general_settings.coordination_redis`、`REDIS_*` 环境变量（`proxy/proxy_server.py:5862-5897`、`:6355-6375`、`:6643-6648`、`:4894-4923`）。
   而我手上的配置快照里，Redis 只出现在 `router_settings.redis_host`（`backup/cursor-web-fc-20260820/cm_litellm-config.yaml:1604`），这一项**只喂给 Router 自己的 cache**，并不会被设成 `redis_usage_cache`。反方向倒是成立：`proxy_server.py:7005-7006` 是把 usage cache 塞给 router。
   Deployment 的 env 里也看不到 `REDIS_*`，但它 `envFrom` 了 `litellm-secrets` 和 `carher-env-keys` 两个 Secret，我没读它们的内容。
   **⇒ 下一步要查：** 生产 pod 启动日志里有没有 `coordination_redis: using a standalone Redis built from REDIS_*` 这一行（`proxy_server.py:5948-5952`）；或者直接 `redis-cli --scan --pattern 'spend:key:*' | head`。下面凡是写「若无协调 Redis」的地方，都是指这一项为空时的行为。
2. **key 缓存的 TTL 是 60 秒，每个 worker 一份，一份最多 200 条。** 2121 把 key 远超 200，所以热 key 之外的请求会频繁穿透到 DB。
   `UserAPIKeyCacheTTLEnum.in_memory_cache_ttl = 60` 定义在 `proxy_server.py:1889`，传给 `UserApiKeyCache` 在 `:2611-2613`；200 条的上限来自 `caching/in_memory_cache.py:28` 和 `proxy/_types.py:2847-2853`。
   改 DB 之后多久生效：走 `/key/update` 会做本地删除 + Redis 删除 + pub/sub 广播（`auth_checks.py:3047-3081`），**前提是有协调 Redis**。直接 SQL 改 DB 则最长要等 60 秒（4 pod × 2 worker = 8 份缓存各自过期）。
3. **#43049 回退（`return None`）的效果：** 请求名本身在 `key.models` 里就放行，不再检查别名映射后的目标在不在白名单里。
   安全上的含义是：**`aliases` 变成了一个不受白名单约束的路由表**。谁能写 `aliases`，谁就能把白名单里的名字指到任意组。这里有两条依据：(a) key 的 owner 或有 `/key/update` 权限的 team 成员可以改非预算字段（`key_management_endpoints.py:3138-3158`）；(b) 管理端没有校验 alias 目标是否在白名单内（grep `aliases` 只命中序列化代码，`key_management_endpoints.py:4657,4714,4760`）。
   如果 198 只有管理员在改 key，这个风险就是理论上的，**但这一点我没验证**。
4. **预算是「事前读 + 事前预留 + 事后对账」三段**，执行顺序固定：
   builder 里的 `_virtual_key_max_budget_check`（`user_api_key_auth.py:2420-2425`）
   → `common_checks` 里的 team/user/tag 等检查（`auth_checks.py:1125-1242`）
   → `reserve_budget_for_request`（`user_api_key_auth.py:3093-3108`）
   → 调用成功后 `increment_spend_counters` 对账（`proxy_server.py:3045-3177`）。
5. **并发能不能冲破 max_budget：有协调 Redis 时基本冲不破。** 预留用的是 INCRBYFLOAT 原子加（`budget_reservation.py:1048-1086`），超额部分会缩到剩余额度（`:187-201`）。最坏情况是在途请求「预估 < 实际」的那部分差额（预估出来的 output 上限是 min(请求的 max_tokens 或 16384, 模型上限)，见 `:1641-1660`），再加上 `pricing_overlay` ×1.3 带来的偏差（见 §3.6）。
   **没有协调 Redis 时：** 计数器是每个 worker 自己内存里的，8 个 worker 各自独立放行。最坏能超出大约 8 倍的单 worker 余量，外加 DB 批量回写之前的那段滞后（`PROXY_BATCH_WRITE_AT=10s`，`constants.py:1845`）。
6. **超预算时返回的状态码由 `litellm.budget_exceeded_status_code` 决定**，这个值在 `BudgetExceededError.__init__` 时读取（`exceptions.py:1010`），然后在 `_as_proxy_exception` 里原样放进 `ProxyException.code`（`auth/auth_exception_handler.py:49-55`）。config 写成 429 能生效，因为未知的 `litellm_settings` 键会被 `setattr(litellm, ...)`（`proxy_server.py:6594`）。
7. **1d 预算的重置：** 每个 pod 每 597～627 秒跑一次 tick（`proxy_server.py:10604-10611,10629-10645`）。有协调 Redis 时用 Redis 租约选出一个 pod 执行（`common_utils/reset_budget_job.py:492-523`），没有时每个 pod 都执行（`_Lease.UNGUARDED`）。
   SQL 用的是 `spend = spend - pre_reset_spend`（decrement），不是 `spend = 0`（`repositories/unit_of_work.py:26-39`、`reset_budget_job.py:1075`）。所以多个 pod 重复执行或者和写回竞争时，**不会吞掉并发写进来的 spend**。
   重置时间点落在 `litellm.timezone`（生产是 Asia/Shanghai）次日 00:00（`common_utils/timezone_utils.py:44-71`）。实际清零有最多约 10 分钟的延迟。
8. **per-key `router_settings.fallbacks` 是整体替换全局 fallbacks，不是合并。** `route_llm_request.py:516-544` 把它当成 per-request kwarg 传入，Router 那边是 `kwargs.pop("fallbacks", self.fallbacks)`（`router.py:7680`）。所以 764 把 key 里**没写到的组，完全拿不到全局兜底**。
   冷却状态是共享的：用的是同一个 `llm_router` 实例（`route_llm_request.py:541-542`），`cooldown_cache` 只有一份（`router.py:1060`）。
   另外，这条分支**跳过了**默认分支里的 `_raise_if_model_fully_blocked`（`route_llm_request.py:545-546`），也跳过了 `map_team_model`、`is_recognized_model` 这些判断。
9. **没给 key 配 rpm/tpm 时，parallel_request_limiter_v3 每个请求做的事：** 构建描述符，结果为空（`parallel_request_limiter_v3.py:3136-3226`），`if descriptors:` 整段跳过（`:4012`），所以 pre-call **不打 Redis**。
   post-call 那边：`_collect_tpm_scope_targets` 无论有没有配限额，都会列出 api_key/user/team/team_member/model_per_key/model_per_team 这些 scope（`:4747-4772`）。没有预留时按实际用量全额加（`:4801-4805`），**所以每次成功请求都会发一次 Lua/pipeline，给约 6 个 `{scope:value}:tokens` 键做 INCR**（`:5011-5015`）。没有协调 Redis 时这些只是写进内存（`:4386-4403`）。
10. **budget_notice 有一个补丁目标在 1.105.1 已经不存在了。** `litellm.proxy.hooks.max_budget_limiter` 这个模块在 1.105.1 里没有（`find -name 'max_budget_limiter*'` 只找到 `model_max_budget_limiter.py`），所以 `_patch_proxy_max_budget_limiter`（`budget_notice.py:2725-2760`）会走 `except: return`，静默成为空操作。好在上游也没有这道闸门了，**目前不会造成漏拦**。
    另外三个补丁的签名都还对得上（§6）。
11. **budget_notice 的「超预算软拦截」在 1.105.1 上有一个确定的破口：** 软拦截能吞掉的只有 key 级的那两道检查（`_virtual_key_max_budget_check` 和 `reserve_budget_for_request`）。`_virtual_key_multi_budget_check`、team/user/team-member 预算都在 `common_checks` 里（`auth_checks.py:1184-1242`），超了会直接 429。
    这在 198 现在的用法下不触发：0 把 key 用 budget_limits，team 预算没验证过。**但 1744 把 key 带 team_id，如果哪个 team 设了 max_budget，就会绕过软拦截。** team 有没有设 max_budget，这一项还没查。

---

## 1. 一次虚拟 key 请求的完整鉴权路径

### 1.1 调用顺序（`/v1/chat/completions`，`Authorization: Bearer sk-...`）

| # | 步骤 | 位置 |
|---|---|---|
| 1 | `user_api_key_auth`：先读 body（解析失败延后抛），开 `spend_counter_batch_scope` | `proxy/auth/user_api_key_auth.py:3433-3459` |
| 2 | `_user_api_key_auth_builder`：`pre_db_read_auth_checks` → `get_api_key` 从 header 取 key | `:1512-1531` |
| 3 | custom auth（我们没配）、public route、OAuth2/JWT 分支：全部跳过 | `:1548-1856` |
| 4 | 解析 end user（body 里的 `user` 字段），预取 identity cache keys | `:1902-1915` |
| 5 | **只查缓存**：`IdentityStore(check_cache_only=True).resolve(hash_token(api_key))` | `:1963-1985` → `auth/resolvers/store.py:102-111` |
| 6 | 缓存命中且带 team_id 时：如果 team 对象（只查缓存）的 `last_refreshed_at` 比 token 新，就把 `team_*` 字段覆盖到 token 上 | `:2042-2071` |
| 7 | master key 比较（`secrets.compare_digest`） | `:2073-2116` |
| 8 | 缓存未命中：`hash_token`，然后 `IdentityStore(...).resolve()` 查 DB，结果写回缓存 | `:2135-2181` → `store.py:113-140`；写缓存在 `auth_checks.py:3026-3044` |
| 9 | `validate_resolved_virtual_key`：temp budget、**blocked 检查** | `:2233-2254` |
| 10 | **key 级模型权限** `_enforce_key_and_fallback_model_access`（顺带也查客户端传来的 fallbacks） | `:2255-2262` → `:3699-3756` |
| 11 | 预取 team/user/project 等对象和 spend counter | `:2263-2265` |
| 12 | 取 user 对象，检查 SCIM 状态 | `:2268-2299` |
| 13 | 判断零成本模型 → `skip_budget_checks` | `:2302-2315` |
| 14 | team member 预算（只在 `team_member_spend` 非空时） | `:2318-2385` |
| 15 | 过期检查 | `:2388-2406` |
| 16 | **key max_budget 检查**（只对 LLM 路由），然后是 soft budget、key/user/end-user 的 model_max_budget | `:2408-2498` |
| 17 | 取 team 对象（读缓存，miss 时查 DB）、project、global spend | `:2501-2585` |
| 18 | 回到 wrapper 里的 `_authorize_authenticated_request`：route 检查、claude 名字归一、**router_settings.model_group_alias 改写** | `:3263-3265` |
| 19 | `_run_centralized_common_checks`：并发取 team/user/project/end_user/global_spend | `:2849-2944` |
| 20 | `common_checks`：team blocked → **team 模型权限** → team member 模型范围 → agent → **user 模型权限（仅个人 key）** → project → 一组并发的预算检查 → 路由、vector store、tools | `auth/auth_checks.py:922-1291` |
| 21 | `_check_team_model_budget` | `user_api_key_auth.py:3078-3091` |
| 22 | **`reserve_budget_for_request` 预算预留** | `:3093-3108` → `:3159-3202` |

任何一步抛错，都进 `UserAPIKeyAuthExceptionHandler._handle_authentication_error`（`auth/auth_exception_handler.py:128-252`）：
先跑 `post_call_failure_hook`，所有 callback 都会收到（我们的 error_sanitize 和 budget_notice 都在里面，`:240-246`），然后 `_as_proxy_exception` 转成对外的 ProxyException。

### 1.2 模型权限在哪里判定

一共四层，全部都要过：

- **key 层**：只在 builder 第 10 步做（`user_api_key_auth.py:3720-3737`）。注释写明 *"Not included in common_checks — common_checks enforces team/user/project model access only"*（`:3709-3710`）。
  key 带 `config` 或者 models 里有 `all-team-models` 时，**整层跳过**（`:3716-3721`）。
- **team 层**：`common_checks` 第 2 步（`auth_checks.py:1004-1025`）。team 拒绝时，如果 key 自己的 `access_group_ids` 能授权，会反过来放行（`:1015-1025`）。
- **team member 层**：`auth_checks.py:1027-1041`。
- **user 层**：只在没有 team 的个人 key 上检查（`auth_checks.py:1086-1094`）。

### 1.3 key 缓存：TTL 与跨 pod 可见性

- 对象：`user_api_key_cache = UserApiKeyCache(default_in_memory_ttl=60)`（`proxy_server.py:2611-2613`，`:1888-1889`）。
  key 对象放在单独的内存分区 `key_object_cache` 里（`common_utils/user_api_key_cache.py:78-86`），键是 64 位 hex 的 hash（`:24-29`）。
- 写缓存时的 TTL 取 `get_management_object_ttl`，等于 `default_in_memory_ttl`，也就是 60 秒，可以用 `general_settings.user_api_key_cache_ttl` 覆盖（`user_api_key_cache.py:401-412`、`proxy_server.py:6709-6719`）。
  生产快照的 general_settings 里没有这一项，所以就是 60 秒（`backup/.../cm_litellm-config.yaml:4-19`）。
- **容量**：`InMemoryCache` 默认 `max_size_in_memory=200`（`caching/in_memory_cache.py:28,34`），满了按最早过期的先淘汰（`:137-141`）。生产没配 `user_api_key_cache_max_size`（同一快照）。
  key 分区自己是 200 条，team/user 等其它管理对象共享另一个 200 条的分区。按 2121 把 key 和 1744 把带 team 的量来看，**每个 worker 只能缓存约 200 把活跃 key，其余 key 的请求会持续穿透到 DB**。这条是读码推断，DB QPS 我没量过。
- **Redis 层的 auth 缓存**：只有 `litellm_settings.enable_redis_auth_cache: true` 才会挂上（`proxy_server.py:4945-4959`）。生产快照里没有这个键，所以 key 缓存**只在进程内存里**，也就是 4 pod × 2 worker（`--num_workers 2`，`backup/.../deploy_litellm-proxy.yaml` 的 args）= 8 份独立缓存。
- **改 DB 之后多久全员可见：**
  - 走管理 API `/key/update`：`_delete_cache_key_object` 先删本 worker 的缓存（`auth_checks.py:3069`），再删 Redis 里的 auth 缓存（`:3073`，没挂 Redis 时是空操作），然后 `publish_auth_cache_invalidation` 发 pub/sub 广播（`:3081` → `common_utils/auth_cache_invalidation_pubsub.py:83-127`）。其它 worker 的订阅者收到消息后删掉自己内存里的那份（`:213-224`）。
    **但订阅者只在 `redis_usage_cache` 非空时才会启动**（`proxy_server.py:8218-8231,10740-10743`），发布端也要求 `coordination_redis_cache()` 非空（`auth_cache_invalidation_pubsub.py:107-109`）。
    ⇒ 有协调 Redis：基本立即生效，延迟是 pub/sub 的毫秒级加上订阅轮询的 1 秒（`:21`）。没有协调 Redis：只有处理这次请求的那个 worker 立即生效，**其余 7 个 worker 最长要等 60 秒**。
  - 直接 SQL 改 DB（我们很多脚本这么做，比如 `scripts/litellm-198-cursor-reset-all.sh:101-105`）：没有任何失效通知，**8 份缓存各自最长 60 秒**。这和脚本注释里写的「≤60s」一致（`scripts/litellm-198-cursor-reset-all.sh:20`）。
  - `blocked`：缓存里的 token 带着 `blocked` 字段（`user_api_key_auth.py:2253`），所以 SQL 封禁 key 同样要等最多 60 秒。
  - `max_budget`、`models`：同上，都是 token 对象上的字段。
  - `spend`：预算判断读的是 spend 计数器而不是缓存里的 `.spend`（见 §3），所以 SQL 把 spend 改成 0 **并不会立刻解封**。计数器在 Redis（或内存）里还是旧值，它的 TTL 见 §3.4。

---

## 2. 模型权限：key.models / aliases / team / all-proxy-models / 通配

### 2.1 核心判定函数 `_check_model_access_helper`（`auth_checks.py:4375-4420`）

对单个候选名 `m`：
1. 如果 `models` 里有 router 的 access group 名，并且 `m` 属于这个组，就放行（`:4387-4399`）。
2. 把 `all-team-models` 展开成 router 里全部模型名（只在有 team_id 时；`:4361-4372,4390-4394`）。
3. `m` 是 team alias 的 key，放行（`:4404-4405` → `:4643-4658`）。
4. 通配：`models` 里任何带 `*` 的模式，正则全匹配 `m`，或者匹配 `{provider}/{stripped}`，都放行（`:4407-4408` → `:6544-6600`）。
5. `models` 为空、含 `*` 或含 `all-proxy-models`，放行（`:4412-4416`）。
6. 否则，`m` 不在 `models` 里就拒绝（`:4418-4420`）。

### 2.2 候选名的产生 `_can_object_call_model`（`auth_checks.py:4467-4504`），#43049 改的就是这里

```
after_team_alias   = team_model_aliases.get(model, model)
after_key_alias    = key_model_aliases.get(after_team_alias, ...)   # ← #43049 新增
after_global_alias = litellm.model_alias_map.get(after_key_alias, ...)
dispatched_model   = key_model_aliases.get(after_global_alias, ...)
key_alias_applied  = 两次 key 别名映射里有任何一次改了名字
potential_models   = (dispatched_model,)  if key_alias_applied
                     else (model, [compaction_parent], [global/router alias target])
```

- **上游 1.105.1（#43049 生效时）**：只要 key 别名映射发生了，就**只检查映射后的目标**，请求名本身不算数（`:4485-4493`）。
- **1.103.2（#43049 之前）**：候选是 `[model, model_alias_map[model] 或 router.model_group_alias 目标]`，根本没有 key alias 这一步（`.research-litellm/litellm-1.103.2/litellm/proxy/auth/auth_checks.py:4294-4300`）。
- **我们的回退**：`key_model_aliases_for_auth_check()` 恒返回 None（补丁 recipe 里的 `REPLACEMENT`）。于是 `key_model_aliases` 是 None，`after_key_alias == after_team_alias`，`key_alias_applied=False`，走 else 分支（`:4489-4492`）。这和 1.103.2 的语义一样，**另外还多一个 compaction_parent 候选**（这是 1.105 新加的，与回退无关）。
- 所有调用点都经过这个函数（`auth_checks.py:716,1013,1023,1040,1073,1093,4583,4612,4984,5002,5061,5071,5084,5099`；`user_api_key_auth.py:477`），所以回退覆盖了 key、team、team member、user、project、agent、fallback-access-check 全部路径。
- 路由改写**不经过**这个函数：`common_request_processing.py:2100-2107` 和 `litellm_pre_call_utils.py:2687-2693` 直接读 `user_api_key_dict.aliases`；`pass_through_endpoints.py:268-274` 同理。回退不影响路由，这和 recipe 里的说法一致。

### 2.3 回退之后，哪些请求「现在放行、上游会拒绝」

设 key 的 `models=M`，`aliases={a: t}`，请求名是 `a`：

| 情形 | 上游 1.105.1 | 我们的回退 |
|---|---|---|
| `a ∈ M`，`t ∉ M`（198 的典型写法：白名单写对外名，aliases 指向内部组） | 403（只检查 `t`） | 放行 |
| `a ∉ M`，`t ∈ M` | 放行 | **403**（只检查 `a` 和全局别名目标） |
| `a ∈ M`，`t ∈ M` | 放行 | 放行 |
| team 层：`team.models` 写的是对外名 | 同样只检查 `t`，被拒 | 放行 |

第二行要注意：如果有人按 #43049 的新语义配 key（白名单写内部组名、aliases 把对外名指过去），在我们的镜像上**会被 403**。这是回退带来的反方向不兼容，只影响将来新写法的 key。

### 2.4 安全含义

- 回退之后，**`aliases` 的目标完全不受 `models` 约束**：鉴权只看请求名，路由按 aliases 改写。
  - 有人能写 key 的 `aliases` ⇒ 他能把白名单里任意一个名字指向任意组（比如更贵的 opus 组），这样就绕过了 `models`。
  - 谁能写：key 的 owner（`created_by == user_id`）、有 `/key/update` 权限的 team 成员，都可以改非预算字段（`key_management_endpoints.py:3138-3158`、`:3208-3215`）。`aliases` 不是预算字段，不需要 admin。
  - 管理端写入时**不校验** alias 目标（`key_management_endpoints.py:4657,4714` 只是 `json.dumps`）。
  - team 层也一样：team.models 只要有对外名就放行。
- 实际风险取决于 198 有没有非 admin 用户能调 `/key/update`。**这一项没查**，下一步查 `LiteLLM_UserTable.user_role` 的分布，以及有多少 key 的 `created_by` 等于自己的 `user_id`。
- 还有一个关联：预算、定价相关的 `get_model_from_request` 读的是改写前还是改写后的名字。鉴权阶段 `_get_model_from_request_context` 读的是**请求原名**（`user_api_key_auth.py:206-221`），key aliases 的改写在之后的 `common_request_processing.py:2100-2107`。所以零成本判断（`_is_model_cost_zero`）和预留的价格估算（`budget_reservation.py:1520-1528`）用的都是**对外名的 model_group_info**。
  对外名如果只是 key alias（router 里没有这个组），`get_model_group_info` 返回 None，零成本判定为 False（`auth_checks.py:486-492`）。预留则退到 `litellm.get_model_info(model=对外名)`，查不到会抛异常，被 `_get_model_cost_infos` 吞掉返回 []（`budget_reservation.py:1543-1553`），**于是 `reservation_cost=None`，这次请求不做预留**（`:302-303`），只剩事前读检查。
  这一条是读码推断，没有实测。如果确实如此，那么所有「请求名只是 key alias」的请求，**都没有并发超支保护**。下一步：从 SpendLogs 看 `metadata.user_api_key_budget_reservation` 是否为空，或者在灰度 pod 打一条 debug 确认。

### 2.5 model_group_alias（router_settings 的）

- 全局的 `router_settings.model_group_alias` 在 `_can_object_call_model` 里作为额外候选（`auth_checks.py:4468-4476,4491`）：别名或目标任意一个在白名单里就放行。
- key 级或 team 级 `router_settings.model_group_alias`：在鉴权**之前**就把 body 改写掉了（`user_api_key_auth.py:258-293`，调用在 `:3713` 和 `:3265`）。所以白名单检查的是改写后的组名。这和 key aliases 正好相反，注意区分。

---

## 3. 预算

### 3.1 什么时候检查

1. **事前读（builder）**：`_virtual_key_max_budget_check`（`auth_checks.py:5479-5556`），只对 LLM 路由（`user_api_key_auth.py:2420`）。
   读的是 `get_current_spend(counter_key="spend:key:{token}", fallback_spend=token.spend, max_budget)`（`:5494-5501`）。条件是 `spend >= max_budget` 就抛错（`:5538-5556`）。
2. **事前读（common_checks）**：team max/multi/soft、key multi-window、org、tag、model access group、user、team member、end user，都在 `asyncio.gather` 里并发跑，取**第一个**异常抛出（`auth_checks.py:1184-1242`）。
3. **事前预留**：`reserve_budget_for_request`（`user_api_key_auth.py:3176-3199`）。前提是没有 `disable_budget_reservation`，模型也不是零成本。
4. **事后对账**：成功的回调里 `increment_spend_counters` → `_reconcile_budget_reservation_for_counter_update`，把预留的部分按实际成本调平（`proxy_server.py:3111-3123,3358-3391`），没预留的计数器按全额加。
5. **fallback 目标的预算**：`enforce_fallback_budget` 默认 True（`auth/fallback_budget.py:53`）。只读 key 和 user 的计数器，不做预留（`:100-137`）。

### 3.2 `get_current_spend` 读哪个数（`proxy_server.py:2797-2875`）

先读 Redis，再读内存，再从 DB reseed，最后用调用方给的 fallback（`:2998-3042`）。注意：**Redis 干净未命中时不读内存**（`:3001-3003`）。
对于主计数器（key/team/user/org），如果 `fallback_spend > current`（缓存对象上的 `.spend` 比计数器大），就去读 DB 的权威 floor（5 秒进程内缓存，`:2771,2962-2995`），用 `async_set_max` 单调抬高计数器（`:2878-2912`）。

### 3.3 `reserve_budget_for_request` 怎么工作（`spend_tracking/budget_reservation.py:248-347`）

- 前置条件：必须是 LLM 路由、不在 `_UNBILLED_ROUTES` 里、能解析出 model（`:264-269`）。
- **计数器列表** `_get_budget_counters`（`:481-608`）：
  - key：`spend:key:{token}`（`max_budget > 0` 时），以及 `spend:key:{token}:window:{dur}`（budget_limits，生产 0 把）。
  - team：`spend:team:{team_id}`（team.max_budget > 0 时）。
  - user：`spend:user:{uid}`，只对个人 key，或开了 `apply_user_budget_to_team_keys` 时（`:540-557`）。
  - end user、tag、model access group、team member、org、project。
- **预估成本** `estimate_request_max_cost`（`:1299-1318`）：input token 计数 × input 单价 + output 上限 × max(output, reasoning 单价) × n。
  output 上限取 `min(请求的 max_tokens 或 16384, 模型 max_output_tokens 或 16384)`（`:1638-1660`）。会遍历组内每个 deployment 的 tiered 价格取最大值（`:1531-1556`）。
  估不出来（返回 None）或 ≤0 时**不预留**（`:302-303`）。
- **初始化计数器** `_initialize_reservation_counter`（`:937-980`）：冷计数器从 DB reseed（`proxy_server.py:3620-3637`）。初始化失败的计数器会被跳过（fail-open），除非开了 `fail_closed_budget_enforcement`（`:927-934`）。
- **预留** `_reserve_reservable_counters`（`:983-1027`）：
  - 先批量读当前值（`:992-994`）。如果所有计数器都满足「当前值 + 预估 ≤ max」，就用一个 INCRBYFLOAT pipeline 全加（`:1030-1045` → `:1048-1086`，后者经 `proxy_server.py:3826-3850`）。
  - 加完以后，计数器值 > max 的，按 `_apply_over_budget_reservation_policy` 处理（`:165-201`）：
    - 加之前的余额 `max - (当前值 - 预留)` ≤ 0：抛 `BudgetExceededError`（`:187-189`）。
    - 余额还有一部分：把预留**缩到剩余额度**放行（`:196-201`）。所以最后一个请求总能用掉剩下的那点额度。
    - key 开了 `throttle_on_budget_exceeded`：释放预留，交给限流处理（`:182-185`）。
- **lease**：有协调 Redis 时，后台任务每隔 `default_ttl/2`（默认 60 秒 TTL，所以是 30 秒）`EXPIRE` 一次预留过的计数器，直到请求结束或超过 `litellm.request_timeout`（生产 300 秒）（`:112-148`；TTL 默认值在 `caching/base_cache.py:23`、`caching/redis_cache.py:671-674`）。
- **释放**：失败、取消、没绑定回调这几种情况分别走 `release_or_invalidate_budget_reservation`、`release_budget_reservation_on_cancel`（按 input 成本结算，`:396-426`）、`release_unbound_budget_reservation`。
- **没有协调 Redis 时**：`run_spend_counter_pipeline` 走 `SpendCounterReseed.increment_in_memory`（`proxy_server.py:3833-3842`），**只在本 worker 内存里原子**，lease 也不续期（`budget_reservation.py:118-119`）。

### 3.4 计数器的生命周期

- **TTL**：`spend_counter_cache` 的 Redis TTL 是 `litellm.default_redis_ttl`（生产没设，所以 RedisCache 默认 60 秒，`caching/redis_cache.py:671-674`）。每次 increment 都会刷新 TTL（`refresh_ttl=True`，`proxy_server.py:3734-3738`；pipeline 里也 expire，`caching/redis_cache.py:1946`）。
  ⇒ **一把 key 空闲 60 秒以上，计数器就过期了**，下一次读会从 DB 的 `spend` 列 reseed（`proxy_server.py:3033-3040`）。DB 的 spend 由批量写回更新，有约 10 秒滞后。
- **含义（SQL 把 spend 改成 0 之后）**：计数器还保留旧值，直到距离最后一次**成功** increment 满 60 秒才过期。被拦截的请求在 builder 阶段就抛错了，不会 increment；`get_current_spend` 只读不 EXPIRE；floor 修复 `async_set_max` 只往上抬（`proxy_server.py:2884-2888`），DB 是 0 时不会触发。所以计数器过期后从 DB reseed 成 0，key 解封。
  ⇒ SQL 清零后，最长约 60 秒解封（计数器 TTL），叠加最长 60 秒的 token 缓存。缓存里旧的 `.spend` 作为 fallback 时，会触发 DB floor 读，读到 0，不会抬高（`:2852-2867`）。这条是读码结论，没实测。
  `/key/update` 带 `spend` 参数会同时把计数器 set 成新值（`key_management_endpoints.py:3570-3584`），可以**立即**解封。前提是有协调 Redis；没有的话只对处理这次请求的那个 worker 生效。
- **reset job 的处理**：DB 提交之后才 `_invalidate_spend_counter("spend:key:{token}")`（`reset_budget_job.py:1086-1091,573-596`），删的是**本 worker 内存 + Redis**。其它 worker 的内存副本只在 Redis 不可读时才会被用到（`proxy_server.py:3001-3021`），所以有 Redis 时不会出问题。
  没有协调 Redis 时：reset 只删掉执行这次 tick 的那个 worker 的内存计数器，**其余 7 个 worker 的内存计数器要等它们自己的 TTL 过期**。InMemoryCache 写入时没带 ttl 参数，`spend_counter_cache` 的 default_in_memory_ttl 是 60 秒（`proxy_server.py:2614`），所以也是 60 秒级别。

### 3.5 1d 预算重置的完整流程

- **调度**：每个进程启动 `initialize_scheduled_background_jobs`（`proxy_server.py:1505,10563-10645`）。间隔是 `597 + randint(0, min(30, 605-597))`，也就是 597～605 秒（`:10604-10611`，`constants.py:1814,1844`）。
  条件是 `general_settings.disable_reset_budget` 不为 True（`:10629`）。生产快照里没有这个键，所以**每个 worker 都调度**（4 pod × 2 worker = 8 个调度器，这一点没实测，读码如此）。
- **选主**：`_acquire_lease`（`reset_budget_job.py:492-523`）用 `pod_lock_manager`（Redis 锁，TTL 900 秒，`constants.py:1821`）。它的 `redis_cache` 来自 `ProxyLogging.update_values(redis_cache=redis_usage_cache)`（`proxy/utils.py:1343-1346`）。⇒ 没有协调 Redis 时是 `UNGUARDED`，8 个 worker 各跑各的。
- **查询**：`expires is None 或 > now`，并且 `budget_reset_at < now`，并且 `budget_duration` 不为 NULL，每批 500 条（`proxy/utils.py:4935-4946`，`constants.py:1815`）。已过期的 key 不会被重置（`litellm-budget-mgmt` skill 里也记录过这一点）。
- **写入**：`spend: {decrement: pre_reset_spend - new_spend}`，`budget_reset_at = 下一个 tz 午夜`（`unit_of_work.py:26-39`；`reset_budget_job.py:1065-1077,1577-1583`；rollover 默认关，`litellm.budget_rollover`，`:130-143`）。
  - **多 pod 竞争**：两个 worker 读到同一批行，都做 decrement。第二个 decrement 会把 spend 减成负数吗？第一个写入后 `budget_reset_at` 已经推到明天，但第二个 worker 是**在第一个提交前读到的**行，`where` 只按 token 匹配（`unit_of_work.py:37-39`），所以会**再减一次** `pre_reset_spend`。
    ⇒ **无租约时，spend 可能被减成 `-pre_reset_spend`。** 负 spend 会让 key 当天多出一份额度（相当于额度翻倍）。这一条是读码推断，前提是没有协调 Redis 且两个调度器的 tick 撞在同一批读写窗口里。tick 间隔各自随机、错开，所以撞上的概率不高，但不为零。
    下一步：`SELECT count(*) FROM "LiteLLM_VerificationToken" WHERE spend < 0`。
- **时间点**：`compute_budget_reset_at` → `get_next_standardized_reset_time("1d", now, tz="Asia/Shanghai")`，结果是次日 00:00（`common_utils/timezone_utils.py:44-71`、`litellm_core_utils/duration_parser.py:109-166`）。实际清零发生在午夜之后的第一个 tick，最多晚约 10 分钟。

### 3.6 超额时到底发生什么

- 抛出 `litellm.BudgetExceededError(current_cost, max_budget, ...)`，其中 `status_code = litellm.budget_exceeded_status_code`（`exceptions.py:998-1024`）。上游默认是 422（`__init__.py:409`），生产 config 写 429，通过 `setattr` 生效（`proxy_server.py:6594`）。
- 在鉴权阶段被 `_handle_authentication_error` 接住：先补 `llm_provider`（`auth_exception_handler.py:222-233`），再跑 `post_call_failure_hook`（所有 callback 都跑，返回 HTTPException 的话取第一个，`proxy/utils.py:3282-3313`），最后 `_as_proxy_exception`：`code = e.status_code`，`type = budget_exceeded`（`auth_exception_handler.py:49-55`）。
- **并发能超多少：**
  - 有协调 Redis：预留是原子的，额度不会被预估值冲破。实际超出 = Σ(在途请求的实际成本 − 预估成本)⁺。预估已经按 output 上限和最贵的 deployment 价格取最大值，**一般会高估**。
    低估的来源：(a) `pricing_overlay` 把**记账价 ×1.3**（`live-cb/pricing_overlay.py:32,95-117`），但预留读的是 `litellm.model_cost` 和 router 的 model_group_info，**没有 ×1.3**（`budget_reservation.py:1520-1528`）。所以每个请求最多可能低估 23%（1 − 1/1.3），对账时补上，这部分就是超出量。
    (b) output 超过 16384 的模型、调用中途 retry/fallback 到更贵的组（预留只按请求名的组估价）。
    (c) 不做预留的请求（见 §2.4 末尾，请求名只是 key alias 时）**完全没有并发保护**，只靠事前读：N 个并发请求都看到「没超」，全部放行，超出量 = Σ 这 N 个请求的成本。
  - 无协调 Redis：每个 worker 有自己的计数器（冷启动从 DB reseed，然后只累加本 worker 的增量），8 个 worker 互不知道对方。最坏情况下每个 worker 都能花到接近 max_budget − DB 里的 spend，DB 每 10 秒批量回写，各 worker 的计数器过期后会重新 reseed。⇒ 超出量的数量级是「10 秒内 8 个 worker 的并发花费」。读码推断，没实测。

---

## 4. per-key router_settings（764 把 key）

- **读取**：`ProxyConfig._get_hierarchical_router_settings`，优先级 key > team，**不和全局合并**（`proxy_server.py:7681-7725`）。key 级直接用 `user_api_key_dict.router_settings`（已经在缓存里的 token 对象上），解析 dict 或 YAML/JSON 字符串（`:7646-7671`）。
- **应用**：`common_request_processing.py:2109-2143` 把整份设置放进 `data["router_settings_override"]`，同时应用其中的 `model_group_alias`。
  `route_llm_request.py:516-544` 只把下面这些键作为 per-request kwargs 合并进 data：`fallbacks, context_window_fallbacks, content_policy_fallbacks, num_retries, timeout, model_group_retry_policy, routing_strategy, enable_tag_filtering`，并且只在 data 里还没有这个键时才合并（`:536-538`）。然后直接调用 `llm_router.<route_type>(**data)`。
- **替换还是合并**：`router.py:7680` 是 `fallbacks = kwargs.pop("fallbacks", self.fallbacks)`，流式中途 fallback 也一样（`:2973,3446,3577`）。⇒ **key 级 fallbacks 整体替换全局列表**。
  key 的 fallbacks 里没写的组，失败后**没有任何兜底**，连全局的 `"*"` 通配兜底也拿不到。`context_window_fallbacks`、`content_policy_fallbacks` 只在 key 设置里出现时才替换，没出现就继续用全局的（`:7682-7683` 的 pop 默认值）。
  本地限流命中时的 fallback 选择也是同样的取法（`common_request_processing.py:2343-2347`）。
- **冷却状态**：同一个 Router 实例，同一个 `cooldown_cache`（`router.py:1060`），所以 per-key 请求**共享**全局冷却，不会绕开。
  `routing_strategy` 如果出现在 key 设置里，会作为 kwarg 传进 Router。这个 kwarg 在 Router 内部怎么生效，我没追，**未验证**。
- **跳过的检查**：`router_settings_override` 分支是 `elif` 链里排在前面的一支（`route_llm_request.py:516`），所以它**跳过**了 `elif llm_router is not None:` 那支里的这些逻辑（`:545-647`）：
  - `_raise_if_model_fully_blocked`（`:58-73`）：模型全部被 block 时本来应该返回 403。
  - `map_team_model`（`:638-641`）：team 专属模型名的映射。
  - `is_recognized_model`、通配、`deployment_names` 等分支（`:643-660`）：对未知模型本来应该返回 `ProxyModelNotFoundError`。
  
  ⇒ 764 把带 router_settings 的 key 请求一个不存在的模型时，报错形状由 Router 决定，不是 proxy 的 404（**未实测**）。被 block 的模型对这些 key 也不会在 proxy 层被拦截。
- **fallback 目标的权限**：`enforce_fallback_model_access` 默认 False（`auth/fallback_model_access.py:28`），所以 key 级 fallbacks 指向的组**不检查 key 有没有权限**。生产快照里没有这个键。也就是说 key 可以兜底到它白名单之外的组，这是有意设计还是疏漏，取决于我们自己的配置意图。
- **fallback 目标的预算**：`enforce_fallback_budget` 默认 True（`auth/fallback_budget.py:53`），只检查 key 和 user 的 max_budget（`:100-137`）。

---

## 5. 没配 rpm/tpm 时，parallel_request_limiter_v3 每个请求做什么

装的是 v3：`PROXY_HOOKS["parallel_request_limiter"] = _PROXY_MaxParallelRequestsHandler_v3`（`proxy/hooks/__init__.py:20-21`），除非设了 `LEGACY_MULTI_INSTANCE_RATE_LIMITING=true`（`:33-34`）。注册在 `proxy/utils.py:1349-1371`。

**pre-call（`parallel_request_limiter_v3.py:3968-4191`）**：
1. `claim_request_stash_for_data`（ContextVar，纯内存，`:3982`）。
2. `_build_request_rate_limit_descriptors`（`:3865-3910`）：
   - api_key、user、team、team_member、end_user 描述符都要求对应的 rpm/tpm/max_parallel 非空（`:3136-3226`），我们这里全部为空。
   - model_per_key、tag_per_key、team/project model 描述符从 metadata 里读，没配就是空。
   - `_create_tag_rate_limit_descriptors`：请求带 tags 时会调 `get_tag_objects_batch`（走 user_api_key_cache，miss 时查 DB，`:681-698,3107-3116`）。我们的 key 是不是给请求注入 tags（`enable_tag_filtering`），**未验证**。如果注入了，每个请求都会多一次 tag 查询（有缓存）。
   - `_check_model_has_recent_failures` 只在开启 dynamic rate limit 时才会跑（`:3880-3887`）。
3. `if descriptors:` 为假，**整段跳过**（`:4012`）。⇒ pre-call **零 Redis 调用**。

**post-call 成功（`:4989-5033`）**：
- `_release_stashed_parallel_slot`：没有 slot，直接返回（`:1900-1918`）。
- `_build_success_event_pipeline_operations`（`:4882-4987`）：`_collect_tpm_scope_targets` **不看有没有配限额**，从 metadata 里列出 api_key、user、team、team_member、end_user、organization，以及 model_per_key、model_per_team、model_per_organization、model_per_project（`:4747-4772`）。
  没有预留时，每个 scope 按实际 token 全额 +（`:4801-4805`）。
  ⇒ 对一把带 team 和 user 的 key，**每次成功请求会产生约 6 个 `{scope:value}:tokens` 键的 INCR**，TTL 是 `window_size`=60 秒（`:811`）。
- 执行方式：优先合并进请求的 post-call Redis batch（`_defer_token_increment_script`，`:4356-4384`），否则单独一次 EVALSHA（`:4315-4354`）。没有 Redis 时写本地内存（`:4396-4403`）。
- 这些写入**没有任何读者**（没配限额，pre-call 不读）。纯开销：每个请求 1 次 Lua 脚本，6 个键。2121 把 key × 模型组数 × team 数，量级是几千到几万个 60 秒 TTL 的键。**Redis 内存和 QPS 的实际值没有测**。
  如果 `internal_usage_cache.dual_cache.redis_cache` 为空（`proxy/utils.py:1343-1344`，同样取决于协调 Redis），这部分就只是本地内存写入，开销可以忽略。

**post-call 失败（`:5105-`）**：释放 slot。没有预留的话也没有退款可做。

---

## 6. budget_notice.py 和 error_sanitize.py 对照 1.105.1 逐条核对

### 6.1 budget_notice.py 的四个 monkey-patch（安装在 `budget_notice.py:2803-2806`）

| 补丁 | 替换了上游什么 | 1.105.1 里的签名 / 位置 | 是否仍然成立 |
|---|---|---|---|
| `_patch_proxy_streaming_choke`（`:2643-2667`） | 包装 `ProxyLogging.async_post_call_streaming_iterator_hook`：遇到非迭代的 `ResponsesAPIResponse`，先用 `CachedResponsesAPIStreamingIterator` 转成事件流 | `proxy/utils.py:3905-3910`：`(self, response, user_api_key_dict, request_data)`，是 async generator。调用方都用关键字传参（`common_request_processing.py:3912-3916`、`proxy_server.py:9873-9877`）。`CachedResponsesAPIStreamingIterator(response, logging_obj, request_data)` 在 `responses/streaming_iterator.py:1263-1267`，`OutputTokensDetails` 在 `types/llms/openai.py:1318` | ✅ 签名一致。⚠️ 新问题：上游加了快路径，`if not caps.iterator_overrides and not post_call_pipelines` 时直接 `async for chunk in response`（`utils.py:3925-3936`）。budget_notice 自己定义了 `async_post_call_streaming_iterator_hook`（`:2362`），所以 `iterator_overrides` 非空，快路径不会走到，补丁仍然在链路上。另外 `proxy_server.py:9866-9877` 有一个 `needs_iterator_wrap()` 门，为假时**根本不调用**这个 hook，同样因为我们有 override 所以为真（读码推断） |
| `_patch_virtual_key_budget`（`:2709-2722`） | 包装 `_virtual_key_max_budget_check`：gated key 超额时吞掉异常、打 mark、返回 None | 定义在 `auth_checks.py:5479-5483`：`(valid_token, proxy_logging_obj, user_obj=None)`；按名字 import 到 `user_api_key_auth.py:55`，调用在 `:2421-2425`（关键字传参）。补丁两个命名空间都 patch 了 | ✅ 成立（参数绑定见 6.1.1）。注意：这个函数还被 `management_endpoints/auto_router_endpoints.py:333` 和 `integrations/shadow_eval_logger.py:647` 按名字 import 并调用，auto_router 是模块顶层 import（`auto_router_endpoints.py:29-32`），它拿到的是不是 patch 后的版本，取决于这个模块和 budget_notice 谁先 import，**未验证**；shadow_eval 是函数内 local import（`shadow_eval_logger.py:634-638`），拿到的一定是 patch 后的版本。在这两条路径上，gated key 超额会被软吞成 None、打 mark，但这两条路径**没有 pre_call 来出 200 mock**。⇒ auto-router 端点（`:333-340` 本来要把超额转成 ProxyException）可能放行超额的 gated key。读码推断，没实测 |
| `_patch_proxy_max_budget_limiter`（`:2725-2760`） | `_PROXY_MaxBudgetLimiter.async_pre_call_hook` | **1.105.1 没有 `proxy/hooks/max_budget_limiter.py`**（只有 `model_max_budget_limiter.py`），`from ... import` 抛异常后 `return` | ⚠️ 静默空操作。上游这道闸门也没有了（`fallback_budget.py:124` 的注释还提到它，但类已经不存在），所以**没有漏拦**。建议删掉，或者改成日志里明确打出 SKIP |
| `_patch_budget_reservation`（`:2763-2800`） | 包装 `reserve_budget_for_request`：gated key 抛 BudgetExceededError 时吞掉、打 mark、返回 None（等同于不预留） | 定义在 `budget_reservation.py:248-263`，全部是关键字参数。调用在 `user_api_key_auth.py:3177-3199`，**函数内 local import**，所以 patch 模块属性能生效。`vt = kwargs.get("valid_token")` 对得上 | ✅ 成立 |

#### 6.1.1 `_make_budget_soft_wrapper` 的参数接法（`budget_notice.py:2684-2706`）：没有 bug

```python
async def patched(valid_token, *args, **kwargs):
    token = getattr(valid_token, "token", "") or ""
    result = await orig(valid_token, *args, **kwargs)
```
调用方：`await _virtual_key_max_budget_check(valid_token=valid_token, proxy_logging_obj=..., user_obj=...)`（`user_api_key_auth.py:2421-2425`）。
Python 会把关键字参数 `valid_token=` 绑定到同名形参 `valid_token`，所以形参能拿到对象，`kwargs` 里只剩另外两个。
`orig(valid_token, *args, **kwargs)` 把它作为位置参数传给原函数，第一个形参也叫 `valid_token`，同样对得上。

#### 6.1.2 软拦截在 1.105.1 上的覆盖范围

- 能吞掉的：key 的 `max_budget`（builder 里那一次）和 reservation（key、team、user 等**所有**计数器里任何一个超额抛出的 BudgetExceededError，补丁不区分是谁抛的，`budget_notice.py:2779-2796`）。
- **吞不掉的**（直接按 429 + error_sanitize 的友好文案返回）：
  - `common_checks` 里那组并发检查（`auth_checks.py:1184-1242`）：team max、team multi、**key multi-window**（`_virtual_key_multi_budget_check`，`:5559-5600`）、org、tag、user、team member、end user。
  - builder 里 team member 预算（`user_api_key_auth.py:2318-2385`）。
  - model_max_budget（`model_max_budget_limiter`）。
  - 198 现状：key 级 budget_limits 是 0 把，所以 multi-window 不触发。team 有没有设 `max_budget`、team member 有没有预算，**没查**。只要有，gated key 撞的就是原生 429 而不是 200 软提示。
- **还有一个顺序问题**：common_checks 在 builder 之后、reservation 之前跑。如果 key 自己的 max_budget 已经被软吞掉、打了 mark，但 team 预算也超了，common_checks 就会先抛 429。用户看到 429，mark 留着，120 秒后过期（`budget_notice.py:1475`）。无害。

#### 6.1.3 mark 机制的假设

- mark 存在**本 worker 内存**里（`_OVER_BUDGET_MARKS`，`budget_notice.py:1474-1504`）。auth 和 pre_call 在同一个请求、同一个 worker 里执行，所以能读到。✅
- 「auth 成功就清 mark」（`:2701-2703`）：只在 `_virtual_key_max_budget_check` 没抛异常时才清。如果 key 没设 max_budget，这个函数直接返回（`auth_checks.py:5490`），也会清 mark。✅
- **双计数风险**：mark 存在时，pre_call 设置 `data["mock_response"]`（`budget_notice.py:2117`）。mock 走 `mock_completion`，usage 是 `prompt_tokens=预留时的 input_tokens 或 10`、`completion_tokens=20`（`main.py:5748-5766,1022-1032`；`constants.py:441-442`）。
  这些 usage 会**按模型单价算出 response_cost 并计入 spend**（我没找到 mock 跳过计费的代码，grep `mock_response` 在 cost_calculator 和 logging 里都没有命中）。
  ⇒ **超额 key 的每一条 200 软提示，都会被记一笔「input_tokens × 单价 + 20 output」的费用。** 对于大上下文（10 万 token）× opus 单价，单条可能有几毛钱，而且是在已经超额的 key 上**继续累加**。
  这条是读码推断，下一步查 SpendLogs：超额 key 那些 `mock_response` 请求的 `spend` 是否 > 0。
  另外，reservation 被软吞以后返回 None（`budget_notice.py:2796`），这次请求没有预留，计数器只在对账时按全额加，**不会双重扣**。✅
- **`_take_over_budget_mark` 不 pop**（`:1492-1504`），一个 mark 会覆盖 120 秒内的所有请求，直到 auth 成功清掉。如果 key 在这 120 秒内被重置（比如管理员 `/key/update spend=0`），下一次 auth 会成功并清 mark。✅

### 6.2 error_sanitize.py

| 依赖 | 1.105.1 实际情况 | 结论 |
|---|---|---|
| `ProxyBaseLLMRequestProcessing.get_custom_headers` 是 `@staticmethod`，全部关键字参数，返回 dict | `common_request_processing.py:1727-1745`，调用点 20 多处，全部 `ProxyBaseLLMRequestProcessing.get_custom_headers(...)`（`:1886,2383,2834,...`；`proxy_server.py:12471,...`） | ✅ 补丁 `staticmethod(patched)` 用 `*args, **kwargs` 透传，成立 |
| `StandardLoggingPayloadSetup.get_error_information(original_exception, traceback_str=None)` 是 staticmethod，返回 dict（TypedDict） | `litellm_core_utils/litellm_logging.py:6104-6108`。调用方：`:6176`（关键字 `original_exception=`）、`proxy_track_cost_callback.py:185`（关键字）、otel/datadog | ✅ 补丁先 `kwargs.get("original_exception")`，再回退到 `args[0]`，两种都能取到 |
| `post_call_failure_hook` 遍历所有 callback，返回 HTTPException 会替换对外错误 | `proxy/utils.py:3197-3313`。补丁永远返回 None，只就地改写 | ✅ |
| `async_post_call_response_headers_hook` 带 `litellm_call_info` 参数 | 上游通过签名检测是否传这个参数（`proxy/utils.py:487-492`） | ✅ 补丁签名里有这个参数 |
| 鉴权阶段的 BudgetExceededError 也会进 failure hook | `auth_exception_handler.py:240-246`，`user_api_key_dict` 是 `resolved_identity.model_copy()`，带 `budget_reset_at`（`models/verification_token.py:37`） | ✅ `budget_public_message` 能拿到 reset 时间 |
| 改写 `.message` / `.args` 后，对外的 ProxyException 取的是改写后的文案 | `_as_proxy_exception` 用 `e.message`（`auth_exception_handler.py:49-55`），**在 hook 之后才调用**（`:240-251`） | ✅ 预算 429 的友好文案确实能到客户端 |
| `request_id` 取 `request_data["litellm_call_id"]` | 鉴权阶段 `litellm_call_id` 还没生成，它在 `common_request_processing.py:2145` 才设 | ⚠️ **鉴权阶段的报错（预算 429、403 模型权限、401），rid 恒为 `-`**（`error_sanitize.py:318-325`），用户报障拿不到可关联的 id。读码推断。客户端 header 里如果带了 `x-litellm-call-id` 也不会进 request_data |
| ModifyResponseException 豁免 | 按类名匹配（`error_sanitize.py:632-634`），类在 `exceptions.py:1201` | ✅ |

**双重处理 / 冲突：**
- budget_notice 的 `async_post_call_failure_hook` 和 error_sanitize 的同名 hook 都会被调用，顺序取决于 `litellm.callbacks` 里的顺序：budget_notice 在前（`cm_litellm-config.yaml` 第 48、49 行）。
  budget_notice 改的是 `.headers` 和 `.type`，error_sanitize 改的是 `.message/.args/.detail`，并且 `type` 只在命中泄漏正则时才改。`usage_limit_reached` 不命中，所以两者**不冲突**（`budget_notice.py:2310-2316` 的注释与此一致）。✅
- `get_error_information` 补丁会把**原文**写进 SpendLogs，原文里可能有 `api_base`、key hash，这是有意的设计（`error_sanitize.py:467-488`）。DB 的访问控制决定这算不算泄漏。

---

## 7. 意外、风险、容易被误解的地方

1. **「4 个 pod 共享预算」这件事依赖协调 Redis，而 `router_settings.redis_host` 不算。** 这是整篇最需要先核实的一项（§0.1）。如果生产没有协调 Redis：
   - 计数器、预留、reset 租约、pub/sub 失效都只在本 worker 生效（8 份）。
   - 无租约时，reset job 的 decrement 有机会被重复执行，把 spend 减成负数（§3.5）。
   - `/key/update` 只有处理请求的那个 worker 立即生效。
2. **in-memory 缓存上限 200 条**，在 2121 把 key 的规模下，大部分 key 的请求会去查 DB（§1.3）。上游文档自己写了「Raise this if you have more active keys than that」（`_types.py:2847-2853`）。
3. **per-key fallbacks 整体替换全局 fallbacks**，key 里没写到的组完全没有兜底（§4）。如果有人以为「key 级只是在全局之上加几条」，那 764 把 key 的兜底覆盖面会比预期小得多。
   这和 `litellm-198-fallback-chain` 等 skill 里的记录是否一致，我没核对。下一步：随便挑一把有 per-key fallbacks 的 key，看它的 fallbacks 里有没有 `"*"`，有没有覆盖它白名单里的所有组。
4. **`router_settings_override` 分支跳过 `_raise_if_model_fully_blocked` 和 `map_team_model`**（§4）。model block、team 专属模型名这两项功能对 764 把 key 失效。
5. **key alias 名的请求可能根本不做预算预留**（§2.4）。如果属实，那 1634 把带 aliases 的 key 走对外名请求时，并发超支只靠事前读拦截。
6. **pricing_overlay 的 ×1.3 不在预留估价里**（§3.6），预留系统性低估 23%。
7. **budget_notice 的 200 软提示很可能也被计费**（§6.1.3）。
8. **鉴权阶段报错的 rid 恒为 `-`**（§6.2）。
9. **#43049 回退后 aliases 不受白名单约束**（§2.4）。是否构成实际风险，取决于非 admin 能不能改 key。
10. **parallel_request_limiter_v3 在没配任何限额时，每个成功请求仍然写约 6 个 tokens 计数器**（§5），没有任何读者。
11. **`_patch_proxy_max_budget_limiter` 静默空操作**（§6.1），建议删掉或者明确打日志。
13. **budget_notice 对 `_virtual_key_max_budget_check` 的 patch 也作用到 auto-router 和 shadow-eval 路径**，那两条路径没有 pre_call 出 mock，gated key 超额时可能直接放行（§6.1 表格）。
12. 容易误解的一点：**SQL 改 `blocked=true` / `models` / `max_budget` 之后，最长 60 秒才对所有 worker 生效**，不是立即生效。走 `/key/update` 在有协调 Redis 时才能做到立即生效（§1.3）。

---

## 附：建议的下一步查询（按优先级）

1. 生产 pod 日志 grep `coordination_redis:` 和 `enable_redis_auth_cache`；或者 `redis-cli --scan --pattern 'spend:key:*' | head -3`，再看 `cronjob_lock:*`。这一项决定 §0.1、§3、§1.3、§3.5 选哪个分支。
2. `SELECT count(*) FROM "LiteLLM_VerificationToken" WHERE spend < 0;`（验证 §3.5 的重复 decrement）。
3. `SELECT team_id, max_budget FROM "LiteLLM_TeamTable" WHERE max_budget IS NOT NULL;`（验证 §6.1.2 的软拦截缺口）。
4. SpendLogs 里 gated、超额 key 的 mock 请求 `spend > 0` 的条数（验证 §6.1.3）。
5. SpendLogs `metadata->'user_api_key_budget_reservation'` 在「请求名只是 key alias」的请求上是否为空（验证 §2.4、§7.5）。
6. 统计 per-key `router_settings.fallbacks` 里有 `"*"` 的占比，以及白名单组的覆盖率（验证 §4、§7.3）。
7. `SELECT user_role, count(*) FROM "LiteLLM_UserTable" GROUP BY 1;`（评估 §2.4 的实际风险）。
