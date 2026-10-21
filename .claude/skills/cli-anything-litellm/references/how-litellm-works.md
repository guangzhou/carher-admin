# 198 上的 LiteLLM 1.105.1 怎么工作（读 tag 得出，10-20）

每条都在 v1.105.1 源码里核过；标「实测」的在 198 pod / 生产库上验过。逐行出处见 `v1.105.1/` 下六份深读。

## 配置谁说了算：yaml 盖 DB，yaml 没写的归 DB

`proxy/config_resolvers/settings_store.py`：**config.yaml > 运行时 > `LiteLLM_Config` 表**。yaml 里出现的键，
DB 那份完全不生效，往这个键写会报 `ConfigOwnedKeyError` → HTTP 400。DB 行每 30s 随 reconcile 重新应用。

prod 现状（`db config-owner`，和 proxy 自己 `/router/settings` 的 `source` 19/19 一致，实测）：

| 归 DB（改 DB 才生效） | 归 yaml |
|---|---|
| `fallbacks`(129) `model_group_alias`(24) `model_group_retry_policy` `context_window_fallbacks` `num_retries`(5) `timeout`(300) `retry_after` `routing_strategy`(simple-shuffle) `enable_weighted_failover` | `cooldown_time`(60) `allowed_fails`(3) `optional_pre_call_checks` `enable_pre_call_checks` `enable_tag_filtering` redis `deployment_affinity_ttl_seconds` |

🔴 往 yaml 的 `router_settings` 加 `fallbacks` = 把 DB 里 129 条整个盖掉，rollout 全绿看不出来。

## 一个请求的主干（`src path` 全文）

endpoint → `user_api_key_auth`（鉴权/预算；烘焙补丁回退了 #43049 别名收窄）→ `common_processing_pre_call_logic`
（别名改写、key/team router_settings、`pre_call_hook`）→ `route_request` → `async_function_with_fallbacks` → 挑腿 →
provider（`async_pre_call_deployment_hook`，兜底每一跳都会过）→ `_process_chunk` 出流 → 计费写库。

钩子出错：pre_call / during_call / post_call_success / 流式 iterator / deployment hook / `async_filter_deployments`
**抛了就让请求失败**；失败钩子、logging 事件、header 钩子**吞掉**。一个 callback import 失败 = **pod 起不来**（不是跳过）。

## 挑腿顺序（`router.py:async_get_healthy_deployments`）

team 过滤 → web-search 过滤 → 健康检查 → **冷却过滤** → blocked → **`async_filter_deployments` 回调**
（weighted_affinity 钉 session、encrypted_content 系列）→ `_pre_call_checks` → tag → order → 策略。
⇒ 亲和钉子只会在**没冷却的**腿里选。

## 重试、冷却、兜底是三件事

- `should_retry_this_error`：状态码不可重试就直接抛（401/403 例外，多腿时可重试）；没健康腿也直接抛。
- `cooldown_handlers._is_cooldown_required`：4xx 里只冷却 401/404/408/429；5xx 和未知都冷却。⇒ **400 既不重试也不冷却**，
  `dead_deployment_retry` 补的就是死号叶子那句 400（生产 `DEAD_ARM_COOLDOWN_HOOK=1`）。
- 冷却**只挂在同步失败处理**上（`router.py:1180-1183`），async 那条只记 RPM。
- `allowed_fails: 3` = 库默认值 ⇒ 被当成「没设」，走百分比分支 ⇒ 429/401/404 一次就冷却。
- `retry_after=0` 不等于不等：单腿组照样 0.5→8s 指数退避。
- **次数**：`max_fallbacks`（`ROUTER_MAX_FALLBACKS`）只限递归深度；同组 weighted failover 和跨组兜底共用它。
  10-20 起 prod/gray `same_group_failover_cap`（同组≤3）+ `ROUTER_MAX_FALLBACKS=10`。`num_retries_per_request` 没设 ⇒ 总次数仍无上限。
- `fallbacks` / `model_group_retry_policy` 的 key 必须是**客户端发来的名字**（alias）。配了 `context_window_fallbacks` 后，
  没列进去的组遇到超长直接失败、不走普通兜底。
- **per-key `router_settings.fallbacks` 整体替换全局兜底**（不合并；764 把 key 有），这条路还跳过「Model is blocked」的 403。

## 模型注册

yaml model_list + `LiteLLM_ProxyModelTable` 按 deployment id 合并（同名就并成一个组；同 id DB 赢）。每 worker 每 30s
从 DB 重同步；`/model/new|update` 当场重载收到请求的那个 worker，其它 worker 等下一次同步。blocked 行在每条路由路径上被滤掉。

## 鉴权与预算

- key 缓存 60s，**每 worker 一份、默认上限 200 条**（2121 把 key）⇒ 多数请求读库；直接改 DB 最多 60s 全员生效。
- 顺序：缓存 → DB → blocked → key 模型 → key `max_budget` → team/user 预算 → 预算预留（Redis 原子增量）。
- 超预算状态码读 `litellm.budget_exceeded_status_code`（上游默认 422，我们 config 写 429）。
- `#43049` 回退后：鉴权只看「请求名在 `key.models` 里」，alias 指向哪里不查。

## 计费写库：丢明细不丢钱

callback → `db_spend_update_writer.update_database`（同一 task 里**分别**入队 SpendLogs 行和日表增量）→
`proxy/utils.py:update_spend_logs` 每 10s 一批 `create_many`。某行被 prisma 拒时上游二分到单行**丢掉它**
（日志 `dropping spend log row Postgres rejected`）。日表另走一条路 ⇒ **判请求量/花费以 `LiteLLM_DailyUserSpend` 为准**。
计价按实际落点的 deployment（不是 alias），`pricing_overlay` 再 ×1.3；kiro 6 小时 0 行报 cache_read（实测）⇒ 全按未缓存计。

## 库（`db catalog`，10-20）

- 76 GiB，SpendLogs 75 GiB（堆 1.4G + 索引 1.3G，其余是 TOAST 里的 messages/response），约 146 万行、保留 6 天。
- schema 90 个 model 全在；196 个 migration、0 未完成；proxy 启动时建 8 个 view。
- `public` 里 31 张我们的备份表 + schema `litellm_ops_backups` 66 张，不是 schema 漂移。
- 共享库（casdoor/owui/myai 也连），`max_connections=100`，平时 26~41 连接。

## Responses / Anthropic 流

- `/v1/responses` 流中途出错：上游 1.105.1 会在已出内容后也兜底（同一条流里可能看到第二个 `response.created`）；`ReadTimeout` 不兜底。
- 原生 `/v1/messages`（kiro）**不检测截断流**：上游断了，客户端收到 200 + 半截消息，按部分 usage 记成功。
- `/v1/messages` 的路由：provider 有 Anthropic-messages 配置就原生转发（anthropic、deepseek），`openai/*` 桥到 Responses，其余桥到 chat。
