# gray 后台任务抑制 · 实测证据（2026-09-14）

计划书 `litellm-198-gray-upgrade-plan.md:371` 写着：

> 后台任务/scheduler 若随每个 proxy Pod 启动，必须确认双 release 不会重复执行全局任务；
> 无法证明幂等时，gray **显式关闭** scheduler/background worker，只保留请求处理路径。

本文档记录：这句承诺在改动前**只存在于这句话里**，以及把它变成 Pod spec 事实所依据的实测。

时间戳一律北京时间。

---

## 1. 证伪腿：`backgroundTasks.enabled: false` 到底做了什么

**假设**：`values-gray.yaml` 的 `backgroundTasks.enabled: false` 会让 gray 副本不跑 scheduler。

**证伪条件**：如果假设错误，渲染出来的 gray Deployment 里应当**找不到**任何能关掉 APScheduler
的东西 —— env 里没有对应开关，config 里没有对应键，容器命令没有变化。

**数据**（渲染 gray profile，读 Deployment 容器）：

```
env:    [{"name": "DISABLE_SCHEMA_UPDATE", "value": "True"}]   # 加上 prod 冻结下来的 env
envFrom: prod 的两个 secretRef（与 prod 共用，含 DATABASE_URL）
labels: {"app": "litellm-proxy-gray",
         "litellm.carher.io/background-tasks-enabled": "false"}
```

`chart/values.schema.json:358-366` 里 `backgroundTasks` 只有 `enabled` 一个布尔字段且
`additionalProperties: false` —— 没有第二个字段可能承载抑制。
`chart/templates/deployment.yaml` 里它唯一的去处是第 29 行那个 **label**。

⇒ 假设被证伪。这个开关的全部效果是一个 Pod label，**它不改变任何一行运行时行为**。

## 2. 没有总开关：guard 只有一个，而且 gray 满足它

目标镜像 `proxy_server.py:1079-1080`：

```python
if prisma_client is not None:
    await ProxyStartupEvent.initialize_scheduled_background_jobs(
```

整个 job 集合唯一的守卫就是 `prisma_client is not None`，而 gray 与 prod **共用
`DATABASE_URL`**（§1 的 envFrom 已证）。所以 gray 起来就会调度。

## 3. job 集合：两个镜像逐条比对，结论是「一样」

| | |
|---|---|
| 线上稳定镜像 | v1.90.2，digest `sha256:7286aa2de7ca…` |
| 目标镜像 | v1.95.0，digest `sha256:50e647bd…` |

读两个镜像内 `/app/.venv/lib/python3.13/site-packages/litellm/proxy/proxy_server.py` 的
`initialize_scheduled_background_jobs`（def 在 :7928，调用点 :1080），**job 集合在两版之间完全一致**。

> ⚠️ 量具坑：`grep -n "_initialize_scheduled_background_jobs"` 返回空 —— 方法名**没有**前导下划线。
> 另外 `python -c "import litellm; print(litellm.__version__)"` 会 AttributeError，
> `litellm` 没有 `__version__`；用 `importlib.metadata.version("litellm")`。
> 包也不在 `/usr/lib/python3/dist-packages`，在 `/app/.venv/lib/python3.13/site-packages`。

按 prod 当前 live config 与 live env 推算，gray 的 2 个副本会跑起来的 job：

`reset_budget_job`、`update_spend_job`、`update_daily_tag_spend_job`、`add_deployment_job`、
`get_credentials_job`、`check_batch_cost_job`、`check_responses_cost_job`，外加
`_monitor_spend_logs_queue` 任务。

其中**会改全局状态**的是三个：

| job | 由什么控制 | prod live 值（实测） |
|---|---|---|
| `reset_budget_job` | `general_settings.disable_reset_budget`（config.yaml，**没有 env var**） | 键**缺席** ⇒ 调度 |
| `check_batch_cost_job` | `PROXY_BATCH_POLLING_ENABLED`，`litellm/constants.py:1475` 默认 `"true"` | env **未设** ⇒ 调度 |
| `check_responses_cost_job` | 同上 | 同上 |

另两个按默认关闭的：`LITELLM_KEY_ROTATION_ENABLED`（默认 `false`）、
`LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_ENABLED`（默认 `false`）。
**「默认关」不等于「按约定关」**：两者都读一个普通 env var，而 gray 的两个 `secretRef` 与 prod 共用，
窗口期内改一次 Secret 会同时在所有 release 上把它们打开。钉死是为了让 gray 的答案不依赖那件事。

### 3.1 prod 四个副本的 env 实测（2026-09-14 02:5x）

```
litellm-proxy-677b5474-68jjb / -bjm2g / -m9f66 / -zbcjb
  PROXY_BATCH_POLLING_ENABLED=[]
  LITELLM_KEY_ROTATION_ENABLED=[]
  LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_ENABLED=[]
  DISABLE_SCHEMA_UPDATE=[]
```

⚠️ **第四行是给迁移 gate 的**：Path A 要求迁移 Job 之前所有在服务的旧 Pod 都已设
`DISABLE_SCHEMA_UPDATE=True`，而现在**四个副本一个都没设**。这条要在迁移前单独处理。

## 4. 落地位置：为什么改 `prepare-values.py` 而不是改 chart

`chart/templates/_helpers.tpl` 会从 `.Values.config.data["config.yaml"]` 重算 `configSha256`、
从 `{args, command, extraEnv, secretRefs}` 重算 `runtimeSha256`，不一致就 `fail`。
⇒ 冻结产物**手改就渲染不出来**。所以把性质写进 `prepare-values.py` 既充分又防篡改，
而且不必动 chart 模板（动模板会把几十个走 `frozen_scheduler_overrides()` /
`_render_gray_with_overrides()` 的 helm 渲染测试全部搅红）。

改动（`scripts/prepare-values.py`）：

- `BACKGROUND_TASK_SUPPRESSORS` —— 三个能**通过 Pod spec** 关掉的 job，非 prod profile 一律钉死
  `=false`，**追加**到 prod 冻结下来的 env 尾部，不重排（gate 2c 要 diff 这份 Pod spec，
  整块重排会把 3 条可命名的新增变成没法审批的整块 diff）。同名条目替换而不是并存 —— 并存能渲染，
  kubelet 静默取最后一条，读不出答案。
- `disable_reset_budget_in_config()` —— `reset_budget_job` 没有 env var，只能用一行 overlay
  `general_settings.disable_reset_budget: true`。**单行文本插入**而不是 YAML round-trip：
  重新 dump 一个 500 模型的 config 会重写每一行，而冻结它的全部意义就是 diff 可读。
  锚点必须在列 0 恰好出现一次，非唯一直接报红；插完再 `safe_load` 语义比对，
  证明「恰好多了一个键、别的一个字没动」—— 缩进错会静默落进嵌套 mapping，那也能 parse、也读绿。
- prod 一律**只检查不改写**：prod 若已经把某个 suppressor 钉成 `false`、或 config 已带
  `disable_reset_budget`，说明「prod 拥有 scheduler」这个前提本身是错的 —— 报红，不糊弄。
- run report 新增 `background_tasks: {enabled, pinned_env, reset_budget_overlaid}`。
  非 prod 上 `pinned_env: []` 表示**冻结输入本来就带着抑制**，绝不可读成「没什么要抑制的」——
  后者正是改动前那个静默行为。

### 4.1 gate 2c 无附带影响

`check-pod-spec-shape.py:505-520`：`needs_approval = sorted((set(removed) | set(changed)) - inert)`
—— **新增**的 shape 项从不需要审批，而 env 的 shape key 是 `{prefix}/env/{name} = "set"`（:334）。
prod 的形状一个字没动。⇒ 已签字的 10 项 `k8s/prod-pod-spec-approval.json` 仍然匹配。

### 4.2 阳性对照（第 0 步）

把两个新函数改回旧的静默行为（`suppress_background_tasks` 直接 `return []`，
`disable_reset_budget_in_config` 直接原样返回），13 条新回归测试里 **9 条转红**。
另 4 条保持绿，是「prod 永不被改写 / 已抑制则无操作」那几条 —— 一个 no-op 本来就满足它们，
这是预期，不是漏网。恢复后 13/13 绿。

## 5. 遗留缺口（**已量过，不是未定因**）：prod 离线期没人 reset budget

`_helpers.tpl` 禁止任何非 prod release 取 `schedulerSafety.mode: primary`，
schema 里的 `externally-coordinated` 目前对所有 release 都不可达。
⇒ `prod_offline_upgrading` 阶段 gray 是唯一在服务的 release，而它的 `reset_budget_job` 被关了，
**这段时间没有任何 release 在 reset budget**。

量一下这个缺口有多大（2026-09-14 02:40，直连 `litellm-db-0`，`-n litellm-product`）：

```
PROXY_BUDGET_RESCHEDULER_MIN_TIME = 597     # 目标镜像内实测
PROXY_BUDGET_RESCHEDULER_MAX_TIME = 605     # ⇒ 约每 10 分钟一轮
```

| 表 | 有 `budget_duration` 的行 | 已过期待重置 | 下一次 `budget_reset_at` |
|---|---|---|---|
| `LiteLLM_VerificationToken` | **1526**（1513 个 `1d` + 13 个 `24h`） | **1** | `2026-09-14 16:00:00 UTC` |
| `LiteLLM_UserTable` | 1 | 0 | `2026-09-14 16:00:00 UTC` |
| `LiteLLM_TeamTable` | 0 | — | — |

⇒ 除一个早已落后到 `2026-08-12`、显然没在被使用的 key 之外，**全部 1526 行都排在同一个每日边界
`2026-09-14 16:00:00 UTC`（= 北京时间 2026-09-15 00:00）**。

**结论**：只要窗口不跨过那个整点，缺口的实际影响是 **0 行**；即使跨过，延迟上界是
（窗口剩余时间 + 约 10 分钟），因为 prod 一回来就会补上这一轮。日额度晚重置十几分钟是噪声。

**因此不实现 `externally-coordinated`**：那要动 `_helpers.tpl` + schema + 一种新的 evidence mode
+ 配套测试，等于在生产窗口前夜新开一条**没跑过的渲染路径**，风险高于一个已量为 0 的缺口。

**执行期要做的**（写进 runbook，不是靠记性）：

- 窗口安排在北京时间 **00:00 之前收尾**，或明确接受一次 ≤ 15 分钟的日额度重置延迟。
- 若窗口确实跨过 16:00 UTC：prod 恢复后**不必手工干预**，下一轮 `reset_budget_job` 自动补；
  但要在恢复后 15 分钟复查上表 `overdue` 计数回到 ≤1，作为收敛判据。

## 6. 还没量的东西（**禁止填 0**）

scheduler evidence 的三个 `observations`（`duplicate_scheduler_runs`、
`duplicate_background_jobs`、`unexpected_control_writes`）**至今没有能看见它们的量具**：
基于日志的尺子是瞎的 —— 24 小时 29,829 行日志里 apscheduler 相关 **0 行**。

> **没量过的 0 比没有证据更糟，它读起来是干净的绿。**

在拿到真能看见这三件事的量具之前，不许把 0 写进 evidence 文件。

## 7. 相关

- `scripts/prepare-values.py` —— `BACKGROUND_TASK_SUPPRESSORS`、`suppress_background_tasks()`、
  `disable_reset_budget_in_config()`
- `tests/test_litellm_gray_chart.py` —— 13 条回归测试（含上述 4 条 RED 用例）
- `docs/litellm-198-gray-upgrade-plan.md:343,371` —— 被兑现的那两句承诺
