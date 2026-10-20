---
name: cli-anything-litellm
description: >-
  读懂 198 上跑着的 LiteLLM：上游源码（按线上版本读 fork tag）、我们的补丁接管了哪些上游函数（在线 pod 里按对象身份实测）、
  生产库（只读事务）。入口 `cli-anything-litellm`（CLI-Anything 方法生成，真身 `tools/cli-anything-litellm/`）的
  `src` / `patches` / `db` 三组命令。也收了读 1.105.1 源码得出的几条「它到底怎么工作」：配置谁说了算（DB 还是 yaml）、
  挑腿顺序、重试与冷却各管什么、计费写库路径。
  Use when 用户说「看 litellm 源码 / 这个函数在哪 / 上游这段怎么写的 / 哪个补丁改了 X / 补丁到底生没生效 /
  三条车道补丁一样吗 / 某个 router_settings 改了不生效 / fallbacks 在 DB 还是 yaml / 查 litellm 库 / 库里有哪些表 /
  SpendLogs 少行 / 明细和日表对不上 / 熟悉 litellm 代码」。线上补丁分几层、各在哪见 [[litellm-198-patch-layer-map]]；
  装/换补丁见 [[litellm-198-router-patch]]；升级见 [[litellm-198-version-upgrade]]。
---

# cli-anything-litellm：198 LiteLLM 的源码 / 补丁 / 库

**一句话**：线上 litellm = fork tag + 我们四层补丁 + 一个管着大半路由配置的 DB。三样都只认现场：
源码读**线上版本那个 tag**，补丁看**在线解释器里函数对象被谁换了**，配置看 **DB 行和 yaml 谁覆盖谁**。

```bash
uv tool install --editable tools/cli-anything-litellm     # 已装则跳过；editable，改代码即生效
cli-anything-litellm src path                              # 一个请求怎么走 + 每步谁挂了补丁
```

全部只读：ssh key + `sudo -n`，psql 开 `default_transaction_read_only=on` + `statement_timeout`
（写会被 Postgres 拒，有测试守着）。不需要也不读任何密码。

## 三组命令

| 想知道 | 命令 |
|---|---|
| 这个函数在线上版本里长什么样 | `src find Router.should_retry_this_error --path litellm/router.py` → `src show litellm/router.py:7915` |
| 换个版本看（升级前对比） | `src find _is_cooldown_required --tag v1.106.0` |
| 哪个补丁接管了哪个上游函数 | `patches targets --lane prod`（27 个符号；多层叠的会列出整条链） |
| 三条车道补丁一样吗 | `patches compare` |
| 某个补丁是干嘛的 | `patches purpose dead_deployment_retry`（说明文字，**不是判据**） |
| 我连的是生产库吗 | `db anchor`（prod 523 模型行 + 129 条 DB fallbacks；dev 只有 3 行、没 fallbacks） |
| 库里有什么、和 schema 对得上吗 | `db catalog` |
| 某个 router_settings 到底谁说了算 | `db config-owner` |
| SpendLogs 有没有少行 | `db spend-reconcile 2026-10-20 [--model-group 'kiro-claude-*']` |
| 明细能查多远 | `db retention`（SpendLogs ~6 天；日表从 05-02 起） |

`src` 默认读 prod 的线上版本 tag；显式给了 `--lane` 又给了对不上的 `--tag` 会被拒（exit 1）。
`patches targets` 有 callback import 失败时 exit 1。

## 读 1.105.1 源码得出的（10-20，都在 tag 里核过）

### 配置谁说了算：yaml 盖 DB，没写进 yaml 的归 DB

`proxy/config_resolvers/settings_store.py`：**运行时 > config.yaml > `LiteLLM_Config` 表**。yaml 里出现的键，
DB 那份**完全不生效**（日志里会打 `... is set in the config file ... The value stored in the database for it is ignored`）。

prod 现状（`db config-owner`）：

| 归 DB（改 DB 才生效） | 归 yaml |
|---|---|
| `fallbacks`(129) `model_group_alias`(24) `model_group_retry_policy` `context_window_fallbacks` `num_retries`(5) `timeout`(300) `retry_after` `routing_strategy`(simple-shuffle) `enable_weighted_failover` | `cooldown_time`(60) `allowed_fails`(3) `optional_pre_call_checks` `enable_pre_call_checks` `enable_tag_filtering` redis `deployment_affinity_ttl_seconds` |

🔴 **往 yaml 的 `router_settings` 里加 `fallbacks` = 把 DB 里 129 条整个盖掉**，rollout 全绿看不出来。

### 挑腿顺序（`router.py:async_get_healthy_deployments`）

team 过滤 → web-search 过滤 → 健康检查 → **冷却过滤** → blocked → **`async_filter_deployments` 回调**
（weighted_affinity 钉 session、encrypted_content 系列）→ `_pre_call_checks` → tag → order → 策略（simple-shuffle）。
⇒ 亲和钉子只会在**没冷却的**腿里选；被冷却的腿从这一步起就看不见。

### 重试和冷却是两件事

- `should_retry_this_error`：状态码不可重试就直接抛（401/403 例外，多腿时可重试）；没健康腿也直接抛。
- `cooldown_handlers._is_cooldown_required`：4xx 里只冷却 401/404/408/429，其余 4xx 不冷却；5xx 和未知都冷却。
- ⇒ **400 既不重试也不冷却**。死号叶子回的 400「There are no healthy deployments」就卡在这——
  `dead_deployment_retry` 补的正是这两处（生产 `DEAD_ARM_COOLDOWN_HOOK=1` 两个都挂；`DEAD_ARM_AUTO_DELETE_DISABLED=1`）。
  它的两个指纹串（`_num_healthy_deployments`、`Do NOT cool down all other 4XX Errors`）1.105.1 里都还在。
- `allowed_fails: 3` 和 `litellm.allowed_fails` 默认值相等 ⇒ `_is_allowed_fails_set_on_router` 判为「没设」，走的是百分比分支。

### 请求主干（`src path` 全文）

endpoint → `user_api_key_auth`（鉴权/预算；**烘焙补丁回退了 #43049 别名收窄**）→ `common_processing_pre_call_logic`
（别名改写、`pre_call_hook`）→ `route_request` → `async_function_with_fallbacks` → 挑腿 → provider
（`async_pre_call_deployment_hook`，**兜底每一跳都会过**）→ `_process_chunk` 出流 → 计费写库。

### 计费写库：丢明细不丢钱

callback → `db_spend_update_writer.update_database`（同一个 task 里**分别**入队 SpendLogs 行和日表增量）→
`proxy/utils.py:update_spend_logs` 每 10s 一批 `create_many`。某行被 prisma 拒（`DataError`）时上游会二分到单行**丢掉它**
（`_create_spend_logs_with_poison_isolation`，日志 `dropping spend log row Postgres rejected`）。
日表另走一条路 ⇒ **SpendLogs 少行时计费照记**。判「那天到底有多少请求」以 `LiteLLM_DailyUserSpend` 为准。

## 补丁层：谁接管了谁（`patches targets`，10-20 三车道实测）

27 个被替换的上游符号，prod/gray 完全一致，guarded-old（1.103.2）只差 anthropic bridge 那一条路径改名。
几处**多层叠**的（顺序 = callbacks 列表顺序，每层都存了 `_orig` 往下调，链底是上游原函数）：

```
BaseResponsesAPIStreamingIterator._process_chunk : codex_compaction_v2 → streaming_output_backfill → deepseek_id_prefix
proxy_server._format_streaming_sse_chunk         : codex_compaction_v2 → deepseek_id_prefix
PrismaClient.jsonify_object                      : sitecustomize → null_byte_sanitize
```

几个容易看错的：

- `anthropic_bridge_error_surface` 在 1.105 上**指纹守卫拒装**（上游 `__anext__` 已自带异常→error 事件）——设计如此，
  不是坏了。在 1.103.2 保险丝上是装上的。
- `responses_aclose` 不是靠 callbacks 列表装上的，是 `sitecustomize.py` 启动时 import 的。
- `sitecustomize.py` 另有 4 个补丁不在 callbacks 列表里（spendlog 清洗 / 批量救援 / prisma 传输误判 / UI 日志渲染）。

## 库（`db catalog`，10-20）

- 库 76 GiB，其中 SpendLogs **75 GiB**（堆 1.4G + 索引 1.3G，其余全是 TOAST 里的 messages/response 大字段）、
  146 万行、正好 6 天；其余表都在几百 MB 以内。
- schema 90 个 model 全在库里；196 个 migration、0 未完成；proxy 启动时建的 8 个 view。
- `public` 里另有 31 张我们的备份表（`BACKUP_*`、`org_team_sync_*` 等，合计不到 1MB），另有 schema
  `litellm_ops_backups` 66 张。不是 schema 漂移。
- SpendLogs 只按索引查：`startTime`、`(api_key, startTime)`、`request_id`（主键）、`session_id`、`litellm_call_id`、`end_user`。
  **别按 `status` / `model_group` 单独过滤大时间窗**（没索引，会顶到超时）。
- 共享库（casdoor/owui/myai 也连），`max_connections=100`，平时 ~26 连接；一次查询一个会话。

## 坑

1. **整天 `group by api_key` 扫 SpendLogs 会超时**：`(api_key, startTime)` 索引的第二列才是时间，按天聚合要走全索引。
   `db spend-reconcile` 先从日表挑出最忙的 N 个 key，再对每个 key 走索引点查（LATERAL）——要扩展照这个写。
2. **判「补丁装上没有」别看 `__module__`**：`functools.wraps` 会把原函数的 `__module__` 抄过来，
   `anthropic_passthrough_pingfix` 就因此看起来像「没装」。`patches targets` 比的是对象身份。
3. **绑定方法每次取都是新对象**：比身份要比 `__func__`，否则所有方法都像「被换过」。
4. **sitecustomize 在探针之前就跑了**：它换掉的东西已经是基线，只能靠哨兵属性 / `__wrapped__` 认出来。
5. **本机 `cli-anything-litellm backend status` 报的 1.79 是本机装的包**，不是 198 的版本；198 的版本看
   `patches targets` 第一行或 `src find` 用的 tag。

## 已知未结（10-20）

**SpendLogs 少行**（假设 / 证伪条件 / 数据都在 `.work/litellm-src-study-1020/finding-spendlog-drop.md`，本仓不入 git）：
当天日表 84578 次 vs SpendLogs 83456 行，缺 ~1.3%，集中在少数长上下文 key（`cursor-af4fac3d-g07c` 210、
`cursor-liuguoxian-std` 210、`cursor-zhanglei-8cql` 186，按 key 跨组合计仍缺 ⇒ 不是兜底换组记错组）。
被拒的 request_id 在 SpendLogs 主键查 0 行。报错是 prisma 客户端参数校验
（`Unable to match input value ... LiteLLM_SpendLogsCreateManyInput`），**具体哪个字段被 stdout 截断挡住了，根因未定**。
计费不受影响。下一步：在 gray pod 里用同形 payload 跑 prisma builder 复现，拿到完整报错再定字段。

## 扩展这个 CLI

- 真身 `tools/cli-anything-litellm/`；设计说明 `LITELLM.md`；测试计划/结果 `cli_anything/litellm/tests/TEST.md`。
- 加新的被 patch 符号：改 `probes/patch_targets.py` 的 `TARGETS`，并在 `core/patches.py` 的 `PURPOSE` 补一行。
  新 callback 上线后 `test_198_core.py::_LIVE_198_MODULES` 也要加（字面量，故意不从 PURPOSE 推）。
- 新的 DB 视图：在 `core/db.py` 加函数，用户输入一律过 `_lit()` 白名单；先 `explain` 确认走索引。
- 测试：`CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/litellm/tests -v`（`test_198_e2e.py` 连真 198，连不上就失败，不跳过）。
- 🔴 别用 cli-anything 的 `skill_generator.py` 重生成包内 `skills/SKILL.md`，会冲掉手写内容（sub2api 那次踩过）。
