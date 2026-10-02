# v1.103.2 升级基线（2026-10-02 起执行）

上一代 `../v1.102.0/`（只重定锚点、从未烘镜像）；生产在跑 `../v1.100.1/`
（`sha256:abdf613339ad36c21fe8b03e53792795fd1f9969ac1d4f390e37735cdb52325e`）。

198 现场工作目录：`/root/l3/up1032-20261002T034536Z/`（root 0700）。

## 镜像

| | tag | digest |
|---|---|---|
| vanilla | `127.0.0.1:5000/litellm-carher:vanilla-v1.103.2` | `sha256:5414e701cd8abce8713e7ca16d3e016345ca48a2e5cfe4efe3d2f22c42e45b77`（= ghcr 官方 amd64 manifest，逐字节） |
| 烘焙 | `vanilla-v1.103.2.capacity.sse-fix-bare-20261002-114652` | `sha256:b8b9e38d8485c9a231427fc913dad27871469e337800cc4ed4596d1d2cc839b5` |

全量 md5 join 验收：两边各 19924 个文件、零增删、只有
`exception_mapping_utils.py` / `responses/streaming_iterator.py` 两个文件不同。

## 生产补丁层不止镜像里这两个（必须一并迁）

`litellm-proxy` 挂 8 个卷，按与上游的耦合方式分三类：

| 类 | 卷 | 处置 |
|---|---|---|
| **整文件覆盖** litellm 源（最危险，盖旧文件会静默回退上游改动） | `streaming-handler-patch` / `passthrough-streaming-handler-patch` / `anthropic-logging-patch` | 按 1.103.2 vanilla 重做：把我们相对 1.100.1 的 diff 重放到 1.103.2 源上 |
| 运行时 monkey-patch（`sitecustomize.py` + 33 个 callback） | `callbacks` / `hooks` | 逐符号审计；只有 `budget_notice` 一处失效 |
| postStart 源码改写 | `deepcopy-patch`（`/patches/patch.py`） | 两个锚点在 1.100.1 **就已经 0 命中**（生产 pod 内 marker 计数 0/0）⇒ 早已是空操作，不是本轮回归 |

### 整文件覆盖三件的重放结果

| 文件 | 我们的改动（vs 1.100.1） | 重放到 1.103.2 |
|---|---|---|
| `proxy/pass_through_endpoints/streaming_handler.py` | +18/−4，3 hunk | `patch` 干净打上 |
| `litellm_core_utils/streaming_handler.py` | +7/−0（midstream 前触发 cooldown） | `patch` 干净打上；落点仍在 4xx 直抛之后、`raise MidStreamFallbackError` 之前，`failure_handler` 签名未变 |
| `proxy/pass_through_endpoints/llm_provider_handlers/anthropic_passthrough_logging_handler.py` | +12/−0（网宿 GLM `usage.server_tool_use` 是 dict → 转 SDK 对象） | **hunk 打不上**：上游把成本计算抽成 `_compute_response_cost`。手工移植到该函数 `return litellm.completion_cost(` 之前，语义不变 |

### 运行时补丁的符号审计（36 个文件、106 个引用、26 个被包装的函数签名）

- 🔴 `budget_notice._patch_proxy_max_budget_limiter`：1.103 删了
  `litellm/proxy/hooks/max_budget_limiter.py`，该函数 `ImportError` 静默 return。
  **影响面为零**：那道 hook 只管 user 级 `max_budget`，生产 1 个 user 有该值且**名下 0 把 key**。
  1.103 把同一检查并进 `common_checks._user_max_budget_check`（auth 层）。key 级预算
  （2011 把）仍走 `_virtual_key_max_budget_check` + `reserve_budget_for_request`，
  这两处签名/调用方式未变，软拦截补丁照常挂上。
- `ProxyBaseLLMRequestProcessing.get_custom_headers` 多了一个 kw 参数
  `read_timing_from_logging_obj`；`error_sanitize` 的包装是 `*args, **kwargs` 透传 ⇒ 不受影响。
- 其余 24 个被包装函数签名逐个相同。

## 1.103 新增且默认打开、会改变行为的一条

`litellm/proxy/auth/fallback_budget.py`：超预算 key 的**付费 fallback 目标被拒**。
默认 `enforce_fallback_budget: true`。为保证「以前的功能不丢、行为不变」，本轮在
cwfb config 的 `general_settings` 里显式写 `enforce_fallback_budget: false`。
以后要开，单独评估 budget_notice 软拦截与它的交互。

## DB 迁移：23 条待应用（生产 162 条，1.103.2 带 184 条）

全是加法。真正与表大小相关的只有一条：

`20260823000000_add_spend_logs_api_key_starttime_index` —— **非 CONCURRENTLY** 的
`CREATE INDEX` 打在 `LiteLLM_SpendLogs`（212 万行，heap 243651 页≈1.9 GB，总 265 GB）。
普通 `CREATE INDEX` 持 SHARE 锁 ⇒ 全程阻塞 SpendLogs 写入。
处置同 2026-09-13 先例：窗口外用**同名** `CREATE INDEX CONCURRENTLY` 预建，迁移那句按名字判重变 no-op。

其余：`ADD COLUMN ... DEFAULT <常量>`（PG16 元数据快路径，不重写表）、新建空表、
0 行表上的索引/约束（`JWTKeyMapping` / `ShadowEvalJob` / `AutoRouterSession` /
`MCPServerTable` / `PolicyAttachmentTable` 全 0 行）。

## 执行记录（全部沿用 `k8s/README.md` 第 4 步现成流程）

| 步骤 | 现成工具 | 结果 |
|---|---|---|
| 生产元数据 dump（同 MVCC snapshot、排除两张日志表数据） | `clone-dump-restore-jobs.yaml` dump 模板 | OK |
| restore clone-A | restore 模板（内置行数逐字节对账） | 162 条 / 1606 列，与生产一致 |
| clone-A 迁移 | 上轮 `migrate-clone-a.yaml` 形状（`prisma migrate deploy`，`lock_timeout=5s`）换新 digest | **31 s，162→184**；原 95 张表 relfilenode **零变化**（无表重写）；新 4 表；invalid 索引 0；唯一消失的列是 `ShadowEvalJob.api_key_id`（→`target_id` 重命名，该表生产 0 行） |
| A 迁移后 dump → restore B、C | post-migration-dump + restore 模板 | 三库均 185 条 / 1691 列 |
| 四条兼容腿 | `prepare-migration-run.py --version-only` + `compatibility-runner.py` | **4/4 PASS**（见下） |
| 大表索引预建 | `v195-concurrent-index-runner.sh`（本轮加了第二个固定目标） | 生产 `inspect`：absent、无长事务/prepared/vacuum，盘余 89.9 GB；clone 上 CONCURRENTLY 建出的定义与迁移本身建出的**逐字相同** |

| 腿 | 库 | 镜像 | 检查项 |
|---|---|---|---|
| new | clone-a | 1.103.2 `b8b9e38d` | auth, key_api, proxy_model_api, proxy_startup, spend_logs |
| old | clone-b | 1.100.1 `abdf6133`（现役） | 同上 ⇒ **迁移后的库现役版照常读写，回滚方向成立** |
| concurrent-new / old | clone-c | 两版同时 | concurrent_budget, concurrent_proxy_model, concurrent_spend_logs ⇒ 灰度期新旧共写成立 |

### 本轮对现成工具的两处修改（都有测试）

1. `v195-concurrent-index-runner.sh`：加 `GRAY_INDEX_TARGET` 封闭白名单，第二个目标
   `v1103-spendlogs-api-key-starttime`。默认值不变（v1.95 那个 Job 行为不变），未知目标直接拒；
   新测试含阳性对照（把索引名改一个字母，测试真红）。
2. `compatibility-runner.py`：并发腿的竞态是真缺陷（上轮靠「错开 20 s 起跑」规避，本轮三次复现：
   先到的腿 PASS 退出、后到的腿 120 s 后判红）。加第三个会合点 `BARRIER_ACK_KEYS`，
   两条腿**同时起跑**一次过（05:27:05/06 起、05:28:12/14 收）。

## 生产库三步（2026-10-02 北京时间 13:51–13:59，用户批准后执行）

| 步 | 做法 | 结果 |
|---|---|---|
| ① 备份 | `pg_dump -Fc --schema=public`，排除两张日志表的**数据**（结构保留） | 71 MB / 93 张表的数据 / 两张日志表 0 行数据；`pg_restore -f /dev/null` 全量解析通过；sha256 `f5287607…`；`/Data/backup/litellm-prod-pre-v1103-20261002T053928Z/`（0700，含迁移前 162 条迁移清单） |
| ② 索引预建 | 本仓 runner，目标 `v1103-spendlogs-api-key-starttime`，`maintenance_work_mem=256MB` | 05:54:15→05:54:57 **42 s**，`valid`，217 MB；定义与迁移原句逐字一致；全库 invalid 索引 0 |
| ③ 迁移 | 上轮 `migrate-PROD.yaml` 形状换新 digest | 05:56:12→05:56:52 **40 s**，应用 23 条，162→185；原 95 表 relfilenode **零变化**；唯一消失列 `ShadowEvalJob.api_key_id`（0 行表重命名）；与 clone 演练逐项相同 |

迁移后对现役 1.100.1 的验证：4/4 Ready、0 重启；真实请求 `claude-haiku-4-5` / `chatgpt-gpt-6-sol`
带 nonce 各 200 且原样回显，两条都在 SpendLogs 落 `success`；迁移后 10 分钟
**539 请求 0 失败**（迁移前 10 分钟 1433 / 12）。

⚠️ 迁移完成那一刻每个 pod 打了 2 次 `postgres_cached_plan_error` → `Prisma DB reconnect succeeded`
（共 8 次，05:56:43–05:57:11，之后为 0）。这是 PostgreSQL 在表结构变化后作废缓存的查询计划，
LiteLLM 自带「重连并用同一查询重试」处理（日志原话 *This may occur during rolling…*）。
判据是用户面：该窗口 SpendLogs 0 失败。**下次迁移照样会出现，是预期，不是故障。**

⚠️ 量具坑：`/v1/responses` 桥接的请求在 SpendLogs 里的 `request_id` 是上游 `resp_…` 编码 id，
**不是**客户端拿到的 `chatcmpl-…`。按客户端 id 查会得到 0 行的假红。

### 生产 DB 连接串注意

`litellm-secrets/DATABASE_URL` 带 Prisma 专用参数 `connection_limit`，`psql` 直接拒
（`invalid URI query parameter`）。Job 里先剥掉 Prisma 参数再交给 runner。

## 🔴 2026-10-02 灰度事故与修复：worker 被心跳杀 ⇒ 502

**现象**：20% 指名灰度（254 把）期间，gray 每 pod 3h 内 worker 子进程被 uvicorn 杀约 38 次，
`Child process [N] died`；pod 不重启（重启数恒 0，是坏尺子）。buyitian 的 cursor key 2h 72 次 502，
每一波都与 worker 被杀同一分钟。生产 1.100.1 同期 0 次。已全量回退（17:58）。

**根因（实测，不是推断）**：`litellm_settings.log_raw_request_response: true`。
- 1.100.1 里这个开关**从未生效**：`litellm_logging.py` 在模块加载时 `from litellm import log_raw_request_response`
  按值拷贝了默认 `False`，config 后来改 `litellm.log_raw_request_response` 不影响它。
- 1.103.2 改成读活值 `litellm.log_raw_request_response`，于是每次上游调用都对整个请求体拼 curl 串再跑
  `redact_string`（20+ 条正则），在事件循环主线程里同步执行。
- py-spy 抓到被卡住的 worker 栈：`redact_string ← _redact_string ← pre_call ← async_response_api_handler`，
  外面套 8 层 `run_async_fallback / _maybe_run_weighted_failover`（兜底链每跳各跑一次）。
- 量具：500KB codex 形状请求，`pre_call` 单次 0.29s（旧版 0.000s）；6 并发 × 8 跳，事件循环最长卡 **5.32s**，
  超过 uvicorn `timeout_worker_healthcheck` 默认 5s ⇒ 心跳没回 ⇒ 父进程 kill worker ⇒ 在途请求 502。
- 没有任何消费者读 `raw_request`（SpendLogs 30 分钟 3070 行 0 行带它）。

**修复**：gray config 里 `log_raw_request_response: false`（= 旧版实际行为）。gray rev9。
修后同一量具：pre_call 0.000s，6×8 跳事件循环最长卡 **0.00s**。

**回归（修复后，生产/灰度并排）**：24 组×3 协议 72/72 vs 72/72；三类 key 行为一致；预算软拦截 9/9 vs 9/9；
kiro 19 条 19/19 vs 19/19；her 逐 pod 全过；worker 被杀 0。`glm-5.3-flash`/`her-flash` 偶发「200 空内容」
两边同频出现（推理模型吃光小 max_tokens，探针已知坑），非版本差异。

⚠️ 转正 prod 时，prod 的 config 也必须带 `log_raw_request_response: false`，否则同样会炸。
✅ 教训已写进灰度回归检验项：`docs/litellm-198-gray-operator-manual.md` §5.5（R1~R8）；`prepare-values.py` 冻结时自动拒 `log_raw_request_response: true` 和 DB 自管的路由键。
