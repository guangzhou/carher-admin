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

## 6. 三个 `observations` 的量具（阳性对照 **已做成**，见 §6.4）

scheduler evidence 的三个计数 —— `duplicate_scheduler_runs`、`duplicate_background_jobs`、
`unexpected_control_writes` —— `prepare-values.py` 要求它们**全都是 0** 才放行。
也就是说：**不解决量具，gray 的 values 文件根本产不出来**。

> **没量过的 0 比没有证据更糟，它读起来是干净的绿。**

### 6.1 基于日志的尺子是瞎的（已证伪）

24 小时 29,829 行 proxy 日志里 apscheduler 相关 **0 行**。
`0 行` 在这里不是"没重复执行"，是"这把尺子看不见这件事"。

### 6.2 进程内自省：够不着

`initialize_scheduled_background_jobs` 里有 `global store_model_in_db, scheduler`，
所以 `litellm.proxy.proxy_server.scheduler` 在启动后**确实**是那个活的 `AsyncIOScheduler`，
而且每个 `add_job` 都显式带 `id=`（`reset_budget_job`、`add_deployment_job`、
`get_credentials_job`、`spend_log_cleanup_job` …）⇒ `scheduler.get_jobs()` 会给出
**这个进程里真正被调度的 job id 集合**，是一把理想的尺子。

但够不着：`kubectl exec … python3 -c` 起的是**新进程**，它的
`proxy_server.scheduler` 是模块初值 `None`（`proxy_server.py:1955`），启动流程从没跑过。
要读活对象只能靠注入（pyrasite/gdb）或加一个 in-process callback —— 前者在生产 Pod 上太侵入，
后者会让 gray 的 config 相对 prod 多出一条只为观测而存在的差异，违背冻结口径。
**⇒ 这条路记下来，不走。**

### 6.3 可行的尺子：clone C 上开 `log_statement='all'` + `%h` 归因

> **⚠️ 与本节初稿的偏差：`'mod'` 改成了 `'all'`。** 初稿写 `'mod'` 是为了少记日志，
> 但 `'mod'` **只记 DML/DDL**，而两个只读轮询 job（`add_deployment_job`、
> `get_credentials_job`）恰恰是判断"gray 的 scheduler 还活着"的唯一证据。
> 在 `'mod'` 下，**一个被抑制的 gray 和一个根本没起来的 gray 看起来一模一样**，
> 于是 `unexpected_control_writes=0` 就成了那种"读起来是干净的绿"的瞎 0。
> 代价是日志量变大 —— 这只在 clone 上开、只开 25 分钟，实测三个窗口各 1.3~2.8 MB，可接受。

真正能看见"谁写了什么"的是 PostgreSQL 自己。关键是这两个参数都是 **SIGHUP 级**，
`ALTER SYSTEM` + `pg_reload_conf()` 即可生效，**不需要重启**：

```sql
ALTER SYSTEM SET log_statement='all';                              -- 含只读，见上方偏差说明
ALTER SYSTEM SET log_line_prefix='%m [%p] app=%a host=%h db=%d ';  -- %h = 客户端 IP ⇒ 归因
SELECT pg_reload_conf();
-- 回滚：
-- ALTER SYSTEM RESET log_statement; ALTER SYSTEM RESET log_line_prefix; SELECT pg_reload_conf();
```

`%h` 给出客户端 Pod IP ⇒ **能把每一条写分给具体 release 的具体副本**，
这正是 `unexpected_control_writes` 和 `duplicate_background_jobs` 需要的归因维度。

**`%a` 是零信息，不要用。** 实测 Prisma 不设 `application_name`，
三个窗口里每一行都是 `app=[unknown]`。**归因只有 `%h` 这一条通道**，
所以两条腿的 client IP 相同时工具必须直接拒绝出数（已写成回归用例）。

`reset_budget_job` 约每 10 分钟一轮（§5），所以窗口至少要跨 **2 个完整周期（≥ 25 分钟）**
才能把"跑了一次"和"跑了两次"分开。

**⛔ 这把尺子不能开在 prod 上**：SpendLogs 的写入量在 20~30GB/天量级，
`log_statement` 会把它们逐条打进容器日志，而 198 `/Data` 有两次盘满 502 的前科
（见 skill `litellm-198-diskpressure-502`）。**只在 clone 上开。**

#### 基线与回滚（实测）

三个参数动手前都是 `source=default`，所以 `ALTER SYSTEM RESET` 能**精确**还原：

| 参数 | 基线值 | `source` |
|---|---|---|
| `log_statement` | `none` | `default` |
| `log_line_prefix` | `%m [%p] ` | `default` |
| `log_min_duration_statement` | `-1` | `default` |

#### 已经验证到哪一步（2026-09-14）

| 项 | 结果 |
|---|---|
| clone A/B/C 是否在线 | ✅ `litellm-clone` ns，三个 sts 各 1/1，已跑 39 天 |
| clone C 的 `log_destination` / `logging_collector` | `stderr` / `off` ⇒ 日志直接进容器 stdout，`kubectl logs` 可取 |
| `ALTER SYSTEM` 两个参数能否热生效 | ✅ 实测生效（`log_statement`、prefix 带 `%a %h %d`），**已按原值回滚**，现场无残留 |
| `ALTER SYSTEM` 不能进事务块 | ⚠️ `psql -c "A; B;"` 会被当成一个事务块 ⇒ 报 `ALTER SYSTEM cannot run inside a transaction block`。必须**一条 `-c` 一条语句** |
| 阳性对照（远端写 → 日志出现带 `%h` 的行） | ✅ **已做成**，窗口 2，见 §6.4 |

阳性对照没做成的原因已经查清，**不是** postgres 只听 socket ——
`listen_addresses` 实测是 `*`。是 `litellm-clone` 这个 ns 有 `default-deny-all` NetworkPolicy，
只有带 `litellm.carher.io/role in (migration,network-probe,restore,version-test)` 标签的
Pod 才被 `allow-qualified-clients-egress-to-clone-db` 放行。我用 `litellm-clone-a-0`
（没有这个标签）去连 clone C，得到 `Connection refused`。

⇒ 阳性对照必须由一个**打了 `role=version-test` 标签的客户端** Pod 来打，
而那正是 runbook「Clone C concurrent stable/target writes」那一行要搭的双版本台子。

#### 因此，执行顺序是（写进 runbook Clone C 那一行）

1. 按 `role=version-test` 起 stable(v1.90.2) 与 target(v1.95.0) 两个 release，都指向 clone C。
2. **先打阳性对照**：从其中一个 release 发一条已知的写，确认 clone C 日志里出现带该 Pod IP
   的 `%h` 行。**看不见就说明尺子坏了，此时的任何 0 都不可信。**
3. 开着尺子跑 ≥ 25 分钟（≥2 个 `reset_budget` 周期）。
4. 按 `%h` 分组数：同一个 job id 的特征语句在**两个 release 的 IP 上都出现** ⇒
   `duplicate_background_jobs` 非 0。
5. 再把 gray 侧的抑制（§4）打开重跑一遍，确认计数归零 —— 这一步同时是抑制生效的**行为级**证据，
   比读 Pod spec 的 env 更硬。
6. 收尾 `ALTER SYSTEM RESET` 两个参数 + `pg_reload_conf()`。

**在第 2 步的阳性对照做出来之前，这三个计数一律保持未填。**
—— 这句话已在 2026-09-14 10:38（北京时间）解除，见 §6.4。

### 6.4 三个窗口的实测（2026-09-14）

尺子的执行体是 `scripts/measure-scheduler-observations.py`，回归用例在
`tests/test_scheduler_observations.py`，fixture 是三个窗口日志的**逐字子采样**
（`tests/fixtures/scheduler-observations/`）—— 不是我编的样例，因为
**合成红与合成绿同样不可信**。

配对语义：`concurrent-old`（stable v1.90.2）替身 prod，**持有 scheduler**；
`concurrent-new`（target v1.95.0）替身 gray，被抑制。
**两条腿都抑制是错的**：那样没有任何一条腿在跑 scheduler，"零重复"平凡成立、什么也没证明。
工具对这一形状直接报红。

| 窗口 | 时间 (UTC) | 配置 | `--expect` | 结果 |
|---|---|---|---|---|
| 1 pilot | 01:44:10 → 02:09:10 | 两腿均不抑制 | `duplicates` | **FAIL**（exit 1）—— 见下方"假绿"一段 |
| 2 阳性对照 | 02:13:30 → 02:38:30 | 两腿均不抑制 + 2s 重新播种 | `duplicates` | **PASS**（exit 0），`2 / 1 / 2` |
| 3 真正的测量 | 02:41:11 → 03:07 | gray 三个 env 全 `false` + `disable_reset_budget` | `none` | 见下 |

#### 窗口 1 为什么是红的 —— 一次差点发生的假绿

窗口 1 里**什么都没坏**：两个 proxy 都在跑，两边 scheduler 都在轮询
（各 55 次只读 poller），尺子抓到 gray 1203 行 / prod 1153 行。
但三个计数全是 **0**。如果当时把它当成"没有重复"，后面那个抑制窗口的 0 就等于没量。

真因是两条：

1. **clone C 上压根没有一行 `budget_duration` 非空**，`reset_budget_job` 永远无事可做。
   ⇒ 先播种一行 `key_alias='scheduler-ruler-probe'` 的过期预算行。
2. **`reset_budget_job` 是 first-poller-wins 的竞态**。两条腿同时启动，调度相位只差 ~0.2s，
   那一行过期行只可能被**其中一条**领走；输的那条读到 `budget_reset_at` 已经被推到明天，
   于是一个字都不写。⇒ 看起来像"没有重复"，而原因与版本无关。
   ⇒ 修法：**每 2 秒**把那行重新播种成过期。
   `PROXY_BUDGET_RESCHEDULER_MIN_TIME=597 / MAX=605` 给每轮 ±8s 的随机相位漂移，
   2s 的播种间隔保证两条腿落在**同一轮**里。

窗口 2 就是这么做出来的：gray 在 `02:23:20.294`、`02:33:17.295` 写，
prod 在 `02:33:22.430` 写 —— 两条腿同一轮、相隔 **5.1 秒**。

#### 尺子本身的三条活性腿（任何一条不成立，0 就不作数）

1. 尺子在窗口内确实抓到了 gray 那个 `%h` 的行；
2. gray 跑了只读轮询 job ⇒ **gray 的 scheduler 活着**
   （否则"gray 什么都没写"和"gray 根本没起来"分不开）；
3. prod 在窗口内确实跑了控制类 job ⇒ **有东西可供重复**（否则 gray 的 0 未经检验）。

外加：`--expect duplicates` 下，**干净的 0 本身就是 FAIL** ——
看不见重复的尺子，也没资格为"没有重复"作证。

#### 顺手踩到的三个坑（都已修，记下来免得再踩）

| 坑 | 形状 | 修法 |
|---|---|---|
| `$$` 被 pod 里的 bash 展开 | `interval 8319152 hours` ⇒ `syntax error at or near "8319152"`。比已知的那层嵌套**又多一层**：Bash 工具 → `printf %q` → 远端 bash → **`kubectl exec` → pod bash** | 去掉 pod 层的 `bash -c`，复用宿主侧已验证过的脚本 |
| `pkill -f reseed-budget-probe.sh` | 把 helper **自己那条 ssh/sudo 命令行**也匹配上，复合命令从那一行起静默中止，唯一症状是少了一个 JSON 文件 | 按显式 PID 杀，并加 `ps -p` 守卫。与「收尾清理禁宽选择器」同一族 |
| `awk "/^ts-a/,/ts-b/"` 取时间区间 | 返回 0 行 | 改成 python 按解析出的时间戳做显式字符串比较 |


## 7. 相关

- `scripts/prepare-values.py` —— `BACKGROUND_TASK_SUPPRESSORS`、`suppress_background_tasks()`、
  `disable_reset_budget_in_config()`
- `scripts/measure-scheduler-observations.py` —— 三个 `observations` 的唯一合法产出路径（§6.4）
- `tests/test_scheduler_observations.py` + `tests/fixtures/scheduler-observations/` ——
  真实日志逐字子采样的回归用例（真红一份、真绿一份）
- `tests/test_litellm_gray_chart.py` —— 13 条回归测试（含上述 4 条 RED 用例）
- `docs/litellm-198-gray-upgrade-plan.md:343,371` —— 被兑现的那两句承诺
