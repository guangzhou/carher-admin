---
name: cli-anything-litellm
description: >-
  读懂 198 上跑着的 LiteLLM：上游源码（按线上版本读 fork tag）、我们的补丁接管了哪些上游函数（在线 pod 里按对象身份实测）、
  生产库（只读事务）。入口 `cli-anything-litellm`（CLI-Anything 方法生成，真身 `tools/cli-anything-litellm/`）的
  `src` / `patches` / `db` 三组命令。也收了读 1.105.1 源码得出的几条「它到底怎么工作」：配置谁说了算（DB 还是 yaml）、
  挑腿顺序、重试与冷却各管什么、计费写库路径；`references/v1.105.1/` 是六个子系统的逐行深读（带 file:line）和
  「哪些已在生产上实测」清单，含 14 条按影响排序的结论（SpendLogs 丢行根因、流内失败不进冷却、几个补丁已成空操作等）。
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

## 深读 1.105.1 的结论（10-20，按影响排序）

六份分区深读 + 我在生产上逐条核过的记录在 `references/v1.105.1/`：
`01-router` 重试/兜底/冷却 · `02-auth-budget` 鉴权/预算 · `03-responses-streaming` Responses 流 ·
`04-anthropic-messages` /v1/messages · `05-spend-db` 计费写库 · `06-proxy-pipeline` 启动/配置/钩子 ·
**`00-verified-on-prod.md` = 哪些在生产上量过**。下表「实测」= 在 198 pod / 生产库上验过；「读码」= 只读了代码。

| # | 结论 | 证据 | 建议（都没做） |
|---|---|---|---|
| 1 | **SpendLogs 少 ~1.3% 行 = 我们自己的两层清洗把字面 `\u0000` 删成非法转义**（详见下一节） | 实测 | 两层都改成解析后清洗 |
| 2 | **`/v1/responses` 流内失败不进冷却**：`streaming_output_backfill` 换掉 `_handle_failure` 后只跑 async 失败处理；router 冷却挂在 sync 那条上（`router.py:1180-1183`）。这个补丁防的事件循环卡死，上游 1.105.1 已自己修了（`streaming_iterator.py:886-931` 先 async 后 sync，后台任务、不 join） | 实测（pod 内该方法归属 + 函数体不调 sync）+ 离线实验 | 删掉 09-14 那段；先在 gray 用死腿做流中失败阳性对照 |
| 3 | **`chatgpt_responses_normalize` 的 deployment hook 永不触发**：传进来的是 `CallTypes` 枚举，`str()` 得 `'CallTypes.aresponses'`，门控 `str(call_type) in (...)` 恒假（`:523`）。同一个坑 `deepseek_responses_adapt` 08-03 就记过并修了，这个文件没修 ⇒ 只在入口跑一次，兜底换腿时不跑 | 实测（py3.13 pod） | 改成取 `.value` |
| 4 | **callback 上的 `async_pre_routing_hook` 是死代码**：Router 只调自己的那个方法（`router.py:13796`，跑 routing plugin），不遍历 callbacks ⇒ chatgpt_normalize / deepseek_adapt 里定义的从不执行 | 读码 | 别再往这个钩子里写东西 |
| 5 | **一个请求打上游次数没有总上限**：`max_fallbacks=5` 只限递归深度，同组 weighted failover 失败后会重置已试集合再走一遍兜底链。离线（真 Router，`router-experiments/01-router-exp7.py`，我复跑过）：A(40)→B(20)/C(10) 全 500 = **84 次**；全 429 = 66；全 400 = 14；**只有 A 坏 = 37 次后才成功**（A 打了 36 次） | 离线实验（复现）；生产 `num_retries_per_request`/`max_fallbacks` 实测未设 | 评估设 `litellm_settings.num_retries_per_request` |
| 6 | **per-key `router_settings.fallbacks` 是整体替换，不是合并**（764 把 key 有）：没列进去的组完全没有兜底；这条路还跳过「Model is blocked」的 403（`route_llm_request.py:516-546`），blocked 腿仍会被路由层过滤 | 读码（两份独立确认） | 改 key 兜底时把要保留的全局兜底一并写进去 |
| 7 | `fallbacks` / `model_group_retry_policy` 的 key 必须是**客户端发来的名字**（alias），写真实组名不生效；配了 `context_window_fallbacks` 后，没列进去的组遇到超长直接失败、不走普通兜底 | 读码 + 离线实验 | 改兜底时按 alias 写；生产 24 个 alias 未逐条核 |
| 8 | **几个补丁在 1.105.1 上已经是空操作**：`encrypted_content_degrade_strip`（上游 affinity check 不再 raise，0 个 raise 语句）· `anthropic_passthrough_pingfix`（唯一调用方已自己 catch）· `budget_notice._patch_proxy_max_budget_limiter`（目标模块不存在，`except: return` 吞了）· `streaming_output_backfill` 的空流升格（上游空流直接抛）· `responses_failed_deployment_stamp`（上游已盖章）· anthropic logging 覆盖件（上游已转 dict） | 实测（前一个）/ 读码 | 下次升级时删，减少维护面 |
| 9 | `responses_aclose` **还要留**：上游 `ResponsesAPIStreamingIterator` 仍没有 `aclose`（router 收尾靠 `hasattr(..., "aclose")`）。但 chat→responses 桥接路径的内层连接从来没人关 | 读码 | 桥接路径另查 |
| 10 | `retry_after=0` 不等于不等：单腿组照样 0.5→8s 指数退避（离线 6 次累计约 25s）；`allowed_fails: 3` 等于库默认值，被当成「没设」，走百分比分支 ⇒ 429/401/404 一次就冷却 | 读码 + 离线实验 | — |
| 11 | 原生 `/v1/messages`（kiro）**不检测截断流**：上游中途断了，客户端收到 200 + 半截消息，按部分 usage 记成功；客户端断开时输出 token 靠已收文本重估（思考/工具 token 少算） | 读码 | — |
| 12 | kiro 计费 = 全价 input × 1.3，**6 小时 15257 行 0 行报了 cache_read** ⇒ 目前缓存全按未缓存计 | 实测 | 看 kiro 上游是否真有缓存 |
| 13 | 鉴权 key 缓存 60s、**每 worker 一份、默认上限 200 条**（2121 把 key）⇒ 多数请求读库；直接改 DB 最多 60s 生效 | 读码 | — |
| 14 | `#43049` 回退补丁的代价：鉴权只看「请求名在 `key.models` 里」，alias 指向哪里不查 ⇒ alias 变成白名单外的路由表；key 所有者/团队成员能改 alias（`key_management_endpoints.py:3138-3158`），198 上谁能改未核 | 读码 | 列入 #43049 长期解的考量 |

**10-20 下午车道又变了**（别人在推，`patches compare` 现量 24/28 一致）：
- gray（08:41 滚）新增 `same_group_failover_cap`：包 `Router._maybe_run_weighted_failover`，同组已排除 3 条腿就停，
  把 `max_fallbacks` 预算留给跨组兜底——**正好针对上表第 5 条**（它自己的实测：60 分钟里放弃 191 次、跨组到 grok 只有 9 次）。
  文件在仓里还是未跟踪（`k8s/litellm-callbacks/same_group_failover_cap.py`）。
- gray 去掉了 `anthropic_passthrough_pingfix`、`log_truncate`（呼应第 8 条：pingfix 已是空操作）。
- prod（08:48 滚）只换了 `budget_notice.py`，和工作区未提交的那份对得上。
⇒ 这些都没进 git；照 repo 重建车道会静默回退。核 drift 用 `scripts/litellm-198-callbacks-drift.py`。

**核过、不成立的**（别再当风险报）：每 30s 重载往 callbacks 里追加字符串（实测 40s 前后长度不变，success/failure 列表 0 个裸字符串）·
预算重置并发导致负 spend（实测只有 3 把 -5.7e-14 的浮点尘）· keepalive 让每个 chunk 都丢 contextvar（实测生产 keepalive 关着，只剩首块那一种情况，未实测）。

## SpendLogs 少行 = 我们自己的清洗补丁（10-20 归因，未修）

当天日表 84578 次 vs SpendLogs 83456 行，缺 ~1.3%，集中在少数长会话 key。计费不受影响（日表另走一条路）。

**原因**：`PrismaClient.jsonify_object` 上的两层清洗（`sitecustomize.py` 的 spendlog sanitizer、`null_byte_sanitize.py`）
都在**已经序列化好的 JSON 文本**上用正则删 `\u0000` 和孤立代理对。对话里一旦出现**字面的**反斜杠 u
（比如代码 `'\u0000'`、正则 `r'[\ud800-\udfff]'`），序列化后是 `\\u0000`，正则删掉后 6 个字符，剩下一个孤零零的 `\`
⇒ 非法转义 ⇒ prisma 拒收 ⇒ 上游把这一行丢掉。会话之后每一轮都带着这段历史 ⇒ 之后全丢。

**证据**（三段式全文在 `.work/litellm-src-study-1020/finding-spendlog-drop.md`）：
- gray pod 逐层跑同一个列：上游原版合法；加 sitecustomize 就 `Invalid \escape`；加 null_byte_sanitize 也是 ⇒ **两层都要改**
- 一个会话：主线程 03:01:28Z 第一次出现字面 `\u0000`，SpendLogs 里主线程的行停在 03:00:58；该 key 该组当天
  日表比 SpendLogs 多 421，正好是 03:01 之后主线程的消息数 421
- 生产日志里能看到 `dropping spend log row Postgres rejected ... LiteLLM_SpendLogsCreateManyInput`，
  但点名字段那段被 `MAX_STRING_LENGTH_STDOUT_LOG=4096` 截掉了，光看日志找不到原因

**修法候选（没做，要拍板）**：在解析后的值上清洗、再序列化；或只删真正的 `\x00` 和代理对码点，不动文本里的 `\\u....`。
两层都要改，只改一层另一层照样把它删坏。

⇒ 在修好之前：**判请求量/计费只认 `LiteLLM_DailyUserSpend`**；某个 request_id 在 SpendLogs 查不到，先想到这条，
别判成「请求没到网关」。

## 扩展这个 CLI

- 真身 `tools/cli-anything-litellm/`；设计说明 `LITELLM.md`；测试计划/结果 `cli_anything/litellm/tests/TEST.md`。
- 加新的被 patch 符号：改 `probes/patch_targets.py` 的 `TARGETS`，并在 `core/patches.py` 的 `PURPOSE` 补一行。
  新 callback 上线后 `test_198_core.py::_LIVE_198_MODULES` 也要加（字面量，故意不从 PURPOSE 推）。
- 新的 DB 视图：在 `core/db.py` 加函数，用户输入一律过 `_lit()` 白名单；先 `explain` 确认走索引。
- 测试：`CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/litellm/tests -v`（`test_198_e2e.py` 连真 198，连不上就失败，不跳过）。
- 🔴 别用 cli-anything 的 `skill_generator.py` 重生成包内 `skills/SKILL.md`，会冲掉手写内容（sub2api 那次踩过）。
