# v1.102.0 补丁基线（2026-09-21 重定锚点，尚未烘镜像）

上一代是 `../v1.100.1/`，那一版已经在生产上跑（`litellm-proxy` 与 `litellm-proxy-gray`
都是 `sha256:abdf613339ad36c21fe8b03e53792795fd1f9969ac1d4f390e37735cdb52325e`）。

## 官方版本现状（2026-09-21 实测）

- **最新稳定版是 `v1.102.0`（2026-09-20）**，不是 1.101.0。
- ⚠️ **`v1.102.0` 只有 git tag + PyPI release，没有 GitHub Release 条目**。
  `GET /repos/BerriAI/litellm/releases` 里查不到它 —— 我第一次就是这么看漏的。
  判最新稳定版必须同时查 `/tags` 和 PyPI `/pypi/litellm/json` 的 `info.version`，
  **`/releases` 单独用是坏尺子**。
- 🔴 **但 `releases.atom` 里有 `v1.102.0` 条目，而且它现在还在**（2026-09-20T00:36:17Z）。
  GitHub 会为**裸 tag** 自动合成 atom 条目，靠 `<content>` 长度就能分辨真假 Release：

  | | `v1.102.0` | `v1.102.0-rc.2` | `v1.101.0` |
  |---|---|---|---|
  | `releases.atom` 条目 | **在** | 在 | 在 |
  | atom `<content>` | **90 B**（就是那条 merge commit 的标题） | 5,055 B（发布说明） | 429,388 B（发布说明） |
  | `/releases` 列表页出现 | **0 次** | 1 次 | 2 次 |
  | `/tags` 页出现 | 1 次 | — | 2 次 |
  | `/releases/tag/<t>` 网页 | 200（tag 视图，224 KB，无 Latest/Pre 标记） | 200（Pre-release ×2） | 200（Latest ×2，571 KB） |
  | API `/releases/tags/<t>` | **404** | 200 | 200 |

  ⇒ **「在 GitHub 上看到过 1.102 的 release，现在看不到了」这个观察本身是对的**：atom / tag 页面
  确实会把 `v1.102.0` 渲染成一条看着像 release 的东西，而 `/releases` 列表页从头到尾没有它。
  两边不是「东西被删了」，而是**同一个裸 tag 在不同页面上的两种呈现**。
  ⛔ 顺带钉死一句：GitHub 不提供 Release 删除审计，**「有没有被删过」这个问题无论如何都证不了**，
  所以别拿它当判据 —— 判版本死活只用 `/tags` + PyPI + 镜像仓库三处。
- `v1.103.0` 只有 rc.1 / dev.2，还没稳定版。
- ghcr 镜像都在：
  | tag | index digest | amd64 manifest digest | amd64 层数 / 压缩大小 |
  |---|---|---|---|
  | v1.100.1 | `sha256:a3715fa7ad8387941ab697259bd2881d68931657247a41984f90fae6d11c62bf` | `sha256:d85c0b36e2fdd64719e7fc2692431cd5aee0814925e96c2892d1491b13189c23` | 21 / 378.8MB |
  | v1.101.0 | `sha256:d295634e09c648dcdb72c4cc2dd226f5fb87823a73e88cbbed6f205e4deb044b` | `sha256:266180fbdfc833c0e16a8c546e1cc814e5854eefd6523afc133416388efdf3ff` | 22 / 383.0MB |
  | v1.102.0 | `sha256:32cfd7a427f6470033b4dcc9e75dfbea2aa32dbc298b0b511a40e962ce818362` | `sha256:9d60771c86a42ced1b918f23dd940a1ac9905ddb02a4fae779cfe1937477c9a0` | 23 / 388.2MB |

  v1.100.1 的 amd64 digest 与我们本地 registry 的 `vanilla-v1.100.1` 完全一致，
  说明这条取值路径是对的。

### ⚠️ 2026-09-24 复查：上面那套「裸 tag」结论已被上游动作翻面

**`v1.102.0` 的 GitHub Release 条目现在存在了**：`created_at` 2026-09-20T00:36:17Z（= 打 tag 那刻）、
`published_at` **2026-09-22T17:56:26Z**、`prerelease=False`、正文 59,755 字符、API
`/releases/tags/v1.102.0` 返 **200**（09-21 还是 404）。
⇒ 上游是 **tag 先打、Release 条目隔两天才补**。
**「有没有 Release 条目」是时间点快照，永远不能当版本正式与否的判据**，上面那张对照表判的是 09-21 当时。

同时上游又发了一批补丁版（截至 09-24）：

| tag | published | 正文 |
|---|---|---|
| v1.101.2 | 2026-09-24T00:17:20Z | 1,542 |
| v1.102.1 | 2026-09-23T06:12:28Z | 1,730 |
| v1.101.1 | 2026-09-23T06:12:25Z | 1,992 |
| v1.100.2 | 2026-09-23T06:12:15Z | 1,928 |

**PyPI `info.version` 现在是 `1.102.1`**（不再是 1.102.0）。
⇒ 本轮烘镜像前先决定：钉 `v1.102.0` 还是跟到 `v1.102.1`（正文只有 1,730 字符，是个小补丁版）。
**这个决定还没做，别默认沿用 1.102.0。**

### ⚠️ 「1.102.0 被官方回退了」—— 2026-09-21 复核：**没有回退**

怀疑来源是 `/releases` 里查不到它（见上）。三把尺子并查，三处全在：

| 尺子 | v1.101.0 | v1.102.0 |
|---|---|---|
| git tag（`/tags`） | 在 | **在** |
| PyPI（`releases[v]`，含 `yanked` 标志） | 8 files, `yanked=False`, 2026-09-14 | **8 files, `yanked=False`, 2026-09-20** |
| PyPI `info.version`（= 官方认的最新） | — | **`1.102.0`** |
| ghcr manifest | HTTP 200 | **HTTP 200** |
| GitHub Release 条目 | 有（正文 71,268 字符） | **当时没有**（唯一缺的一项）→ ⚠️ **09-22 上游已补上，见上一节** |

**PyPI 的 `yanked=False` 是「有没有被撤回」的直接判据** —— 真撤回会把 `yanked` 置 true
或直接删档，8 个文件一个没少。`info.version` 仍是 1.102.0，也就是 `pip install litellm`
现在装的就是它（⚠️ 09-24 起 `info.version` 已变成 `1.102.1`）。**它只是当时没建 Release 条目，不是被回退。**

⚠️ 顺带证伪一个可能的猜测：**上游没有 `-stable` 后缀这种东西**。
`v1.101.0-stable` / `v1.102.0-stable` 在 ghcr 上都是 404，最近 100 个 tag 里
也没有任何含 `stable` 的名字（只有 `main-stable` / `main-latest` 这两个滚动 tag）。
"稳定版" 在我们这边是自己的语义（谁带路由标签谁占 30402），不是上游的 tag。

## 两个补丁的重定锚点结果：**零改动**

两个脚本从 `../v1.100.1/` 逐字节复制过来，md5 未变
（`e7173f40266f842bc274d40442a0e36a` / `07a96c9cb8c23d34eec9b76358125c5e`）。

拿 v1.102.0 的真源文件干跑：

| 项 | v1.100.1 | v1.101.0 | v1.102.0 |
|---|---|---|---|
| capacity `ANCHOR` 命中 | 1 | 1 | **1** |
| sse `ANCHOR_CLASS` 命中 | 1 | 1 | **1** |
| sse `ANCHOR_INIT` 命中 | 1 | 1 | **1** |
| `exception_mapping_utils.py` 行数 | 2733 | 2733 | 2736 |
| `streaming_iterator.py` 行数 | 2514 | 2599 | 2624 |

干跑产物：`exception_mapping_utils.py` +15/−0、`streaming_iterator.py` +394/−8，
与 1.100.1 上那一版完全同形；两个文件 `py_compile` 通过；重跑得 `SKIP`（幂等）。

作用域校验（不是只数锚点）：
- 注入点落在 `_map_openai_exception`（1.102.0 里 257–513 行），补丁块引用的
  `exception_provider` / `model` / `custom_llm_provider` / `original_exception` /
  `extra_information` / `error_str` 是形参，`message` 是局部变量，`RateLimitError` 已导入，
  `exception_mapping_worked` 未被引用 ⇒ 不会 NameError。
- `ANCHOR_INIT` 仍落在 `BaseResponsesAPIStreamingIterator.__init__`（1.102.0 里 232–281）。
- 补丁块用到的 9 个 `ResponsesAPIStreamEvents` 成员在 v1.102.0 的
  `types/llms/openai.py` 里全部存在。

## 上游仍然没修，补丁仍然必需

- `"Selected model is at capacity"` / `"isn't available"`：v1.102.0 的
  `exception_mapping_utils.py` 里 0 命中。`ExceptionCheckers.is_error_str_rate_limit`
  仍只认三种形状（被 `status_code` 门住的裸 429、`rate[\s_-]*limit`、Mistral 整句
  `service tier capacity exceeded`），这句一条都不匹配。
  ⚠️ 不要改成「靠 body 里的裸数字判限流」——那正是 #36705 修掉的坑。
- `_ResponsesLifecycleGapFiller` / `_BARE_STATUS_MAP`：上游 0 命中。
  lifecycle 事件计数与 1.100.1 逐项相同（`content_part.added` 仍然是 0），
  说明 gap 还在，Codex CLI 的 `stream closed before response.completed` 没被上游修掉。

## 1.100.1 → 1.102.0 与我们相关的上游变化

- `router.py` 改了 1699 行，但 **`_pre_call_checks` 本体只改 7 行**：
  `instructions` 的取法从函数内提前计算改成把整个 `request_kwargs` 传进
  `_count_pre_call_check_tokens`，再加一段 region check。
  **`input_tokens > max_input_tokens` 这条比较、`enable_pre_call_checks` 这个开关都原样在**
  ⇒ 09-21 刚武装好的 `max_input_tokens` 闸门在 1.102.0 上继续成立。
- `update_settings` 的白名单从函数内 `_allowed_settings`（17 项）搬到
  `constants.RUNTIME_UPDATABLE_ROUTER_SETTINGS`（18 项）。逐项对比：**只多了
  `optional_pre_call_checks` 一项，零删减**（`max_retries` 在 1.100.1 的旧列表里就有）。
  生产 DB 的 `router_settings` 只有 13 个 key，全部在新白名单里 ⇒ 热加载不会有 key 被拒。
  配套改动 `e67f98f fix: reconcile runtime pre-call checks`：`optional_pre_call_checks`
  热更新从"只加不减"（`add_optional_pre_call_checks`）改成"按请求集合收敛"
  （`set_optional_pre_call_checks`，可运行时摘掉 `prompt_caching` /
  `enforce_model_rate_limits` 两个 callback）。**我们的 `router_settings` 里没有
  `optional_pre_call_checks` 这个 key，这条改动对我们是零影响**；注意它动的是
  `optional_callbacks`，不是 `enable_pre_call_checks`，与 `max_input_tokens` 闸门无关。
- `exception_mapping_utils.py` 那 11 行全是给 Bedrock 分支补 `response=` 参数，
  与我们的注入点无交集。
- DB 迁移 10 条，**全部是 `ADD COLUMN IF NOT EXISTS` / 索引重建 / 一处外键加 CASCADE**，
  唯一带 `RENAME COLUMN` 的是 `LiteLLM_ShadowEvalJob.api_key_id → target_id`。
  生产库里 `ShadowEvalJob` / `ShadowEvalAttempt` / `AutoRouterSession` /
  `JWTKeyMapping` / `MCPServerTable` / `ManagedObjectTable` /
  `DailyGuardrailUsageUnits` **全是 0 行** ⇒ 这 10 条迁移在我们库上是空操作，
  不需要额外的数据回填计划。`ObjectPermissionTable` 有 60 行，但那一条是
  `ADD COLUMN ... DEFAULT ARRAY[]`，无损。
- 镜像层数 21 → 22 → 23，`Entrypoint`/`Cmd`/`PATH`/python 3.13 全部不变。
  按 history 逐条 diff，两层是**分两版进来的**：
  - **1.101.0 多的那一层**：`RUN echo "https://packages.wolfi.dev/os" >> /etc/apk/repositories`
    （对应 changelog 里 `fix(docker): add public Wolfi apk repo to runtime image`）。
  - **1.102.0 多的那一层**：`COPY /usr/local/bin/pgbouncer /usr/local/bin/pgbouncer`，
    同时 `apk add` 那层多装了 `libevent`。
  amd64 压缩总大小 378.8MB → 383.0MB → 388.2MB（按 1e6；按 MiB 是 361.2 → 365.2 → 370.2）。
  **pgbouncer 默认关闭**（`litellm/proxy/db/pgbouncer.py` 里 `enabled: bool = False`，
  靠 `LITELLM_PGBOUNCER_*` env 开启），实测我们三个 Deployment 的 env 里一个
  `PGBOUNCER` 都没有 ⇒ 只多了个躺着的二进制，行为不变。
  ⚠️ **`pgbouncer.py` 在 v1.101.0 上是 404**（v1.100.1 也是）⇒ 这个新面是
  1.102.0 独有的，退到 1.101.0 就不带它。
- `docker/prod_entrypoint.sh` 改了：`if [ "$USE_DDTRACE" = "true" ]` 变成
  `case "$USE_DDTRACE" in [Tt][Rr][Uu][Ee])`，即 `True`/`TRUE` 现在也会启用 ddtrace。
  我们三个 Deployment 都没有 `USE_DDTRACE` env ⇒ 零影响，但**如果以后要加这个变量，
  注意 1.101.0 起大小写不敏感**。
  ⚠️ 这条**是在 1.101.0 落的**（changelog：`fix(docker): match USE_DDTRACE
  case-insensitively and route build_from_pip through prod_entrypoint.sh`），
  v1.101.0 与 v1.102.0 的 `prod_entrypoint.sh` md5 完全相同（`d3c559129113`）。
- **DB 迁移解析器默认仍是 v1**：窗口里有 `4f6fd85 default to the v2 migration resolver`，
  但随后被 `fd72a39 revert: default the proxy back to the v1 migration resolver` 撤回。
  三个 tag 的 `proxy_cli.py` 都读了，`--use_v2_migration_resolver` 全是
  `is_flag=True, default=False`，help 文字末尾都写着 "Default is the v1 resolver."
  （行号 916 / 918 / 924）⇒ 滚动升级时的迁移行为与 1.100.1 相同，不需要为它改 env。
  ⚠️ **这一对 feat+revert 都在 1.101.0 的 changelog 里**，也就是说"默认切 v2"这件事
  从来没有出现在任何一个正式版的默认值里。

## 如果改成升 1.101.0 而不是 1.102.0（2026-09-21 补充对比）

用户怀疑 1.102.0 被回退时查的，结论是 **1.102.0 没被回退**（见上），
所以这一节只作为备选路线的参考，**当前目标不变，仍是 v1.102.0**。

| 维度 | v1.101.0 | v1.102.0 |
|---|---|---|
| 官方 Release 条目 | 有，changelog 485 条 | 无（只有 tag + PyPI） |
| commit 数（自 v1.100.1） | 1620 | 3093 |
| `fix` / `feat` | 270 / 96 | 1236 / 214 |
| BREAKING | 0 | 0 |
| DB 迁移新增 | 4 条 | 10 条（含 1.101.0 那 4 条） |
| 镜像层数 | 22 | 23 |
| pgbouncer | **没有**（`pgbouncer.py` 404） | 有（默认关） |
| 两补丁锚点 | 三个全 1 命中 | 三个全 1 命中 |
| `exception_mapping_utils.py` | 2733 行，**md5 与 1.100.1 完全相同** | 2736 行（+3，Bedrock `response=`） |
| `streaming_iterator.py` | 2599 行 | 2624 行 |
| `pass_through_endpoints.py` | 3788 行 | 3793 行 |
| `RUNTIME_UPDATABLE_ROUTER_SETTINGS` | 18 项 | **与 1.101.0 逐字节相同** |

**1.101.0 的好处只有两条**：`exception_mapping_utils.py` 与 1.100.1 **md5 一致**
（capacity 那个补丁连"文件变了"这个风险都没有），以及少 6 条迁移、少一个 pgbouncer 面。
**代价是这 1473 个 commit 的 fix 要等下一轮才吃到**，而 1.102.0 这边所有风险项都已经
逐条验完是零影响 —— 两个版本的补丁工作量完全一样（锚点都是 1/1/1）。
⇒ **没有理由退到 1.101.0。**

另外 1.101.0 的 changelog 印证了两件之前靠读源码得出的结论：
`feat(streaming): carry final response cost on streamed usage by default` 和
`fix(router): count tools and Anthropic system prompt in context-window pre-call check`
**都是 1.101.0 就落的**，不是 1.102.0 引入的。

## 新能力（2026-09-21 逐条读源码确认，**全部与本轮切换无关，都是切完之后再另议**）

判据统一是：**在 v1.102.0 的源码里读到默认值**，不是 changelog 的措辞。

### A. 值得后续采纳的两条（都要显式配置才生效）

1. **`--prometheus_metrics_port`**（`litellm/proxy/proxy_cli.py:940-951`，`default=None`，
   `envvar=PROMETHEUS_METRICS_PORT`）—— 把 `/metrics` 的抓取和多 worker 聚合搬到**独立进程**，
   不再占推理 worker 的 event loop。`/metrics` 在主端口上仍然挂着，不是二选一。
   🔴 **上游 help 原文就写了这个端口没有 virtual-key 鉴权**（"the separate port has no
   virtual-key auth, so keep it off public ingress"）⇒ **只能给集群内 Prometheus 打，
   绝不能进公网 ingress**。绑定失败时启动直接失败（不是静默降级）。
   ⚠️ 配套的 `_maybe_setup_prometheus_multiproc_dir`（`:634`）会在设了该端口时**无条件**
   自动建 `PROMETHEUS_MULTIPROC_DIR`（默认 `<tmpdir>/litellm_prometheus_multiproc`）并 `wipe_directory`。
   **我们当前的形状（`--num_workers 2` + `callbacks: [prometheus]`）在 1.100.1 和 1.102.0 上
   行为完全一样**：两版都因为"多 worker + prometheus 回调"这个条件自动建目录，逐行比过，
   ⇒ **不设新端口就是零变化**。

2. **per-worker admission control**（PR 39352，2026-09-04 合入；
   `litellm/proxy/middleware/admission_control_middleware.py`，v1.102.0 上 309 行）。
   配置在 `general_settings` 下，**不写 `max_in_flight_requests_per_worker` 就整个不装**
   （`get_admission_control_settings()` 见到它是 `None` 直接 `return None`，
   中间件在第 150 行 `if settings is None or ...` 短路）：
   ```yaml
   general_settings:
     max_in_flight_requests_per_worker: 64   # 每个 worker 进程同时放进来的请求数
     max_queued_requests_per_worker: 64      # 排队等位的上限，缺省 = in-flight 上限
     admission_queue_timeout_seconds: 1.0    # 排超过这个时间就拒
   ```
   超限返回 `503` + `retry-after: 1` + `type: "overloaded_error"`，
   message 是 `Worker at capacity: {admitted} in-flight, {queued} queued requests. Retry later.`
   `_EXEMPT_PATHS`（第 14 行 frozenset）放过 6 条 `/health/*` 和 `/metrics`、`/metrics/`
   ⇒ **饱和时存活探针仍然答得上，正是我们踩过的"探针与推理抢同一个 event loop ⇒ k8s
   重启过载 Pod ⇒ 负载雪崩"那个形状的对症解**。它不需要 Redis，和
   `global_max_parallel_requests` 不同（PR 正文说后者被 v3 限流器忽略，LIT-5460）。
   🔴 **上限是按 worker 进程算的**：我们三个 Deployment 全是 `--num_workers 2`
   ⇒ 单 Pod 实际放行 2×，配数的时候别按 Pod 算。

### B. 白拿的（不用配，升上去就有）

- 价格表：Claude Fable 5.1 全家（Anthropic/Bedrock/Vertex/Azure AI）、`gpt-6-astra`
  与 `azure/gpt-6-astra`、gemini-3.8-flash、`azure_ai/grok-4.6`。
- Prometheus 新增 per-key / per-team 的限流 gauge
  （`litellm_api_key_rate_limit_allowed_metric` / `_used_metric` 与 team 两支，`prometheus.py:491-512`）。
  ⚠️ 我们的 `prometheus_metrics_config` 是**白名单分组**的，不在组里的指标不会吐
  ⇒ 想用得往组里显式加名字，**不是自动出现**。
- `RetryPolicy` 多两个字段：`ServiceUnavailableErrorRetries`、`DefaultRetries`
  （`litellm/types/router.py:110-111`，1.100.1 只有 6 个，都是 `None` 默认）。
  实测我们 `router_settings` 里**根本没有 `retry_policy`**（只有 `num_retries: 2`
  / `allowed_fails: 3` / `cooldown_time: 60`）⇒ 这条对我们当前配置是零影响。
- `fix(responses): encrypt the response id on every streamed event`。

### C. 已确认与我们自有补丁**不冲突**的一条（本来最该担心）

`fix(proxy): stop leaking internal exception details to clients` 落在
`redact_internal_details_from_client_message()`（`litellm/_logging.py:96`），
在 `common_request_processing.py:3534` 和 `:3748` 两处对**出站给客户端的 message** 生效，
规则见 `litellm_core_utils/secret_redaction.py:108`：砍掉 traceback、
再按正则抹掉 unix/windows 路径、私网与回环 IPv4、`.internal/.local/.corp/.lan/.intra/.private` 主机名。

**拿我们 capacity 补丁真正注入的那句话直接跑过这个函数（把 `litellm.constants` 打桩后
本地 import 该模块实测）：**

| 输入 | 结果 |
|---|---|
| `the selected model is at capacity. please retry shortly.` | **原样不变** |
| `Worker at capacity: 64 in-flight, 64 queued requests. Retry later.` | 原样不变 |
| `... api_base=http://10.68.13.198:4000/v1 returned 429` | IP 被替换成 `REDACTED` |

⇒ **我们注入的那句用户可见文案不会被它改写**，门禁里 `grep "selected model is at capacity"`
这条判据继续成立。顺带一提它和我们自己的 `error_sanitize.py`（689 行，`_LEAK_RE` 在第 151 行）
是两层独立的脱敏，叠加只会更严，不会互相抵消。
⚠️ 可用 `LITELLM_DISABLE_REDACT_SECRETS=true` 整体关掉（`_logging.py:64`），**我们不设**。

### D. 已定位但对我们没用，不采纳

- `semantic_cache_scope`（`caching/caching.py:104/280/317`，默认 `"key"`，1.100.1 没有这个参数）——
  只在语义缓存（redis/qdrant/valkey semantic）下才有意义；实测生产 config 里
  `cache_params` / `redis_semantic` **各 0 处** ⇒ 我们没开语义缓存，零关系。
- `off_peak_pricing`（`llm_cost_calc/utils.py:298-469`，按 UTC 时窗改单价）——
  生产 config 里 `off_peak_pricing` **0 处**，且它要写进每个 model 的 `model_info`
  才生效，属于计价侧可选优化，本轮不动。

## 待办（烘镜像时）

1. 把 ghcr `v1.102.0` 拉进 198 本地 registry，tag `vanilla-v1.102.0` 并 push。
2. 按本目录 `Dockerfile` 烘 `vanilla-v1.102.0.capacity.sse-fix-bare-<ts>`。
3. 全量 md5 join 验收：只允许那两个文件不同、不允许文件增删；
   文件总数以新 base 自己的 `find | wc -l` 为基准（**不再是 1.100.1 的 19524**）。
4. `../../chart/Chart.yaml` 的 `appVersion` 仍写着 `"1.95.0"`，与实际跑的 1.100.1 就已经不符，
   本轮一并改成 `"1.102.0"`。
