# 迁移成本实测（2026-09-13，北京时间）

这份文件回答一个问题：**v1.90.2 → v1.100.1 的 schema 迁移，到底会让线上停多久。**

结论先写：**迁移窗口里没有任何一条语句的耗时与表大小相关。**
原计划按 141 GB 数据量外推出来的「几十分钟重写窗口」是**假设，不是测量**，已被证伪。

§1–§6 是迁移窗口本身的成本；§7 是迁移**之后**的兼容性（老版本能不能对着新 schema 跑、
新旧能不能同时连同一个库），同样全部是实测。

> ⚠️ **表大小口径已于 2026-09-13 20:5x 变更**：两张日志表已按 skill
> `litellm-198-diskpressure-502` 做过一次紧急 TRUNCATE（`LiteLLM_SpendLogs` 142 GB → 18 MB，
> `LiteLLM_SpendLogToolIndex` 27 GB → 64 kB，`/Data` 264G used → 96G used）。
> **下文 §2/§3/§4/§6 里的行数与字节数是 TRUNCATE 之前的实测值，按原样保留**——它们是
> 迁移成本论证的输入，不能事后改写。对迁移结论的影响是**单向变好**：唯一那条尺寸相关语句
> （§4 的 `CREATE INDEX`）的目标表现在几乎是空的，§6 的预建索引仍 `indisvalid = true`
> （TRUNCATE 会换索引的 relfilenode 但保留索引定义），所以 migration 里那句
> `CREATE INDEX IF NOT EXISTS` **依旧是 no-op**。

---

## 1. 待执行的迁移是哪些

生产 `_prisma_migrations` 已完成 **131** 条；v1.100.1 镜像带 **161** 条。
集合差 ⇒ **31 条待执行**（完整列表见 `/tmp/imgdiff2/pending.txt` 的生成方式，下方可复现）。
对账：161 = 130 条两边都有 + 31 条待执行；131 = 130 + 1 条只在生产有。

反向差集只有一条 `20260311180521_schema_sync`——生产有、镜像没有，是历史 squash 产物，不影响向前迁移。

复现：

```bash
# 生产已应用
kubectl -n litellm-product exec -i litellm-db-0 -- psql -U litellm -d litellm -At \
  -c 'SELECT migration_name FROM "_prisma_migrations" WHERE finished_at IS NOT NULL ORDER BY 1' | sort > applied.txt
# 镜像自带（pod 跑目标 digest，需带 litellm.carher.io/role: version-test 标签）
ls -1 /app/.venv/lib/python3.13/site-packages/litellm_proxy_extras/migrations | sort > dirs.txt
comm -23 dirs.txt applied.txt      # 待执行
```

> ⚠️ migrations 目录路径带下划线且不含 "prisma" 子串：
> `litellm_proxy_extras/migrations`。按 `-name migrations -path '*prisma*'` 找会一无所获。

## 2. 31 条里只有两条碰得到大表

把 31 条的 `migration.sql` 全量 grep 一遍尺寸相关操作（`CREATE INDEX` / `ADD CONSTRAINT` /
`ALTER COLUMN` / `SET NOT NULL` / `UPDATE` / `DELETE`），命中 14 处。逐条对照生产表大小后：

| 语句 | 目标表 | 生产规模 | 是否尺寸相关 |
|---|---|---|---|
| `ADD COLUMN created_at/updated_at ... NOT NULL DEFAULT CURRENT_TIMESTAMP` | `LiteLLM_SpendLogs` | **510,798 行 / 141 GB** | **否**（见 §3） |
| `CREATE INDEX ... ON "LiteLLM_SpendLogToolIndex"("start_time")` | `LiteLLM_SpendLogToolIndex` | **24,992,272 行 / 26 GB**（heap 1,216,700 页 ≈ 9.7 GB） | **是**（见 §4） |
| 其余 12 处（`LiteLLM_ShadowEval*`、`LiteLLM_DailyGatewayRequests`、`LiteLLM_AutoRouterSession`、`LiteLLM_DailyGuardrailUsageUnits`、`LiteLLM_ModelAccessGroupBudgetTable` 等） | 本轮**新建**的空表 | 0 行 | 否 |

其中两条 `UPDATE "LiteLLM_ShadowEvalJob" ...` 和一条 `ALTER COLUMN ... SET NOT NULL`
看着危险，但目标表是同一批迁移里刚 `CREATE TABLE` 出来的，执行时必然 0 行。

生产表大小（2026-09-13 实测，仅列 >10 MB）：

```
 LiteLLM_SpendLogs              |    510,798 | 141 GB
 LiteLLM_SpendLogToolIndex      | 24,992,272 |  26 GB
 LiteLLM_DailyTagSpend          |    187,206 | 265 MB
 LiteLLM_VerificationToken      |      1,906 |  90 MB
 ...（其余均 < 90 MB）
```

## 3. `ADD COLUMN` 不重写表 —— 实测 2.063 ms

PostgreSQL 11+ 对 `ADD COLUMN ... NOT NULL DEFAULT <非 volatile>` 走
**metadata-only 快路径**：默认值存进 `pg_attribute.attmissingval`，不碰堆页，
耗时与表大小**无关**。`CURRENT_TIMESTAMP` 是 STABLE（非 volatile）⇒ 命中快路径。

在 clone-c（PG 16.13）上按生产表形状造 **3,000,000 行 / 1181 MB**，执行 migration 原句：

```
ALTER TABLE
Time: 2.063 ms
filenode_before = 17621
filenode_after  = 17621     ← 未换 filenode ⇒ 没有重写
total_size      = 1181 MB   ← 前后一致
null_created    = 0         ← 默认值确实生效
```

**阳性对照**（第 0 步纪律：合成绿不可信，必须证明尺子能读出红）——
同样的表、同样的语句，只把默认值换成 VOLATILE 的 `random()`：

```
ALTER TABLE
Time: 21128.426 ms          ← 慢了 10000 倍
filenode_before = 17629
filenode_after  = 17635     ← 换了 filenode ⇒ 全表重写
```

尺子有判别力。所以 141 GB 的 `LiteLLM_SpendLogs` 上这条 `ADD COLUMN` 是 **O(1)**，
不是 O(表大小)。**残余风险是拿 `ACCESS EXCLUSIVE` 锁的排队，不是语句本身的时长。**

## 4. 唯一的尺寸相关语句：已在窗口外用 CONCURRENTLY 预建

`20260724000000_add_spend_log_tool_index_start_time_idx` 的原句是**普通** `CREATE INDEX`：

```sql
CREATE INDEX IF NOT EXISTS "LiteLLM_SpendLogToolIndex_start_time_idx"
  ON "LiteLLM_SpendLogToolIndex"("start_time");
```

普通 `CREATE INDEX` 持 **SHARE 锁**——读不受影响，但**该表的所有写入全程阻塞**。
在 24,992,272 行 / heap 9.7 GB 上这是分钟级的停写，而这张表每条带工具调用的
spend log 都要写。这是整个迁移窗口**唯一**的真实停机来源，此前的方案文档按
v1.95.0 时代的 13 GB / 1122 万行估算，已经翻了一倍。

处置（与方案 §2.2 既有要求一致：该索引必须拆出独立 `CONCURRENTLY` runner）：
**在迁移窗口之前、生产空闲时段单独预建**。`CREATE INDEX CONCURRENTLY` 只持
`ShareUpdateExclusive`，不阻塞 DML。建完之后，migration 里那句
`CREATE INDEX IF NOT EXISTS` 按**索引名**判重，直接变成 no-op ⇒ 尺寸相关语句
从迁移窗口里彻底消失。

执行前快照（回滚基准：不在这张表里的索引就是本次新增的，可
`DROP INDEX CONCURRENTLY` 撤销）：

```
"LiteLLM_SpendLogToolIndex_pkey"                     | valid
"LiteLLM_SpendLogToolIndex_tool_name_start_time_idx" | valid
（目标索引 _start_time_idx 不存在；全库 invalid 索引数 = 0）
```

执行参数与实测结果见 §6。

## 5. 一条硬约束：生产 postgres 没有内存余量

`litellm-db-0` 的 postgres 容器 cgroup：

```
memory.current = 3,215,441,920
memory.max     = 3,221,225,472      ← 99.82%
```

但拆开看不是要 OOM：

```
anon  =   237,408,256   (~226 MB)   ← 真正的匿名内存
file  = 2,834,071,552   (~2.7 GB)   ← page cache，可回收
memory.events: max=2592  oom=0  oom_kill=0
pod restarts=0，已连续 Running 4d21h
```

`shared_buffers` 只有 **128 MB**，`work_mem` 4 MB，`maintenance_work_mem` 64 MB。
即 postgres 几乎完全依赖 OS page cache 顶着。

**结论与操作约束**：

- 迁移 Job / 索引 runner **禁止**把 `maintenance_work_mem` 调到几百 MB 以上。
  多出来的匿名内存会把 page cache 挤掉，换来的是全库查询变慢；调过头则真的撞
  3 Gi 上限 ⇒ postgres 容器被 OOMKilled ⇒ **这才是真正的生产中断**。
- 本次 CONCURRENTLY 预建只设到 `256MB`（约 page cache 的 9%），且是 **session 级**，
  不改实例配置。
- 「99.8%」这个读数**本身不是告警**——它是 page cache 占满，属正常。判 OOM 风险只
  看 `anon` 和 `oom_kill`，不看 `memory.current/memory.max`。

## 6. 预建索引的执行记录

```sql
SET lock_timeout = '30s';       -- 拿不到锁快速失败，不排队阻塞业务
SET statement_timeout = 0;      -- 构建本身不设上限
SET maintenance_work_mem = '256MB';
CREATE INDEX CONCURRENTLY IF NOT EXISTS "LiteLLM_SpendLogToolIndex_start_time_idx"
  ON "LiteLLM_SpendLogToolIndex"("start_time");
```

开始时刻：`2026-09-13 11:05:12.903933+00`（北京时间 19:05:12）。
执行前生产负载：`active_backends=1`、`idle_in_xact=0`、`longest_xact_s=0`、`total_backends=18`。

**不阻塞写入的直接证据**（构建全程逐 25 s 采样）：

| 阶段 | blocks_done/total | 等锁 backends | `LiteLLM_SpendLogToolIndex` 累计插入 |
|---|---|---|---|
| building index: scanning table | 310,958 / 1,216,700 | 0 | 2,517,227 |
| building index: scanning table | 688,702 / 1,216,700 | 0 | 2,517,254 |
| building index: scanning table | 917,629 / 1,216,700 | 0 | 2,517,261 |
| building index: scanning table | 1,126,894 / 1,216,700 | 0 | 2,517,287 |
| index validation: scanning table | 25,615 / 1,216,705 | 0 | 2,517,310 |
| index validation: scanning table | 157,478 / 1,216,705 | 0 | 2,517,330 |

判据不是「没报错」，是**插入计数在构建全程持续增长且等锁 backends 恒为 0**。
若 CONCURRENTLY 真的阻塞了 DML，插入计数会冻结、等锁 backends 会 > 0。

**完成结果**：

```
CREATE INDEX
Time: 574712.909 ms (09:34.713)        ← 9 分 34.7 秒
finished: 2026-09-13 11:14:47.617759+00   （北京时间 19:14:47）
```

执行后核验（全部通过）：

```
invalid_indexes_whole_db = 0            ← 没有残留 invalid 索引
target_index_valid       = true         ← indisvalid AND indisready
target_index_def         = CREATE INDEX "LiteLLM_SpendLogToolIndex_start_time_idx"
                             ON public."LiteLLM_SpendLogToolIndex" USING btree (start_time)
                                        ↑ 与 migration 原句逐字一致 ⇒ IF NOT EXISTS 必然 no-op

"LiteLLM_SpendLogToolIndex_pkey"                     | valid | 15 GB
"LiteLLM_SpendLogToolIndex_start_time_idx"           | valid | 213 MB   ← 新增
"LiteLLM_SpendLogToolIndex_tool_name_start_time_idx" | valid | 1822 MB
```

副作用核账：

| 项 | 前 | 后 |
|---|---|---|
| 库大小 | 168 GB | 169 GB（+213 MB，即新索引本身） |
| 数据盘 | 263G used / 204G avail | 264G used / 203G avail |
| postgres `anon` | 237 MB | 232 MB |
| `oom_kill` | 0 | 0 |
| `litellm-proxy` 4 副本 | Running | Running（无新增重启） |

**这 9 分 34 秒全部发生在迁移窗口之外，且全程零停写。** 如果放任 migration 用普通
`CREATE INDEX` 执行，同一份工作会以 SHARE 锁的形式落在窗口内——由于普通
`CREATE INDEX` 只扫一遍表（CONCURRENTLY 扫两遍且要等事务），预计是**数分钟量级的
该表全量停写**。这是本次预建换掉的东西。

> 注意：`CREATE INDEX CONCURRENTLY` 要扫两遍表（build + validate），总耗时约为普通
> `CREATE INDEX` 的两倍。用两倍的**后台**时长换零停写，是这里唯一划算的交易。

> ⚠️ 预建期间**禁止**对同一个库开长事务（例如 `pg_dump`）。CONCURRENTLY 在两个
> 阶段之间要等待所有更早的事务结束，一个长事务会把它无限期拖住。

失败处置：若 CIC 中断，会留下 `indisvalid = false` 的残index。它**仍会被写入维护**
（增加写开销）但不会被查询使用，**必须清掉**，不能放任：

```sql
SELECT indexrelid::regclass, indisvalid FROM pg_index
WHERE indrelid = '"LiteLLM_SpendLogToolIndex"'::regclass;   -- 确认
DROP INDEX CONCURRENTLY "LiteLLM_SpendLogToolIndex_start_time_idx";  -- 撤销后重来
```

## 7. 灰度期双版本共存：不是推理，是实测

迁移窗口只是第一关。真正决定灰度能不能做的是**另一个问题**：迁完之后，
老版本 v1.90.2 还能不能对着 v1.100.1 的表正常写。灰度期两个版本同时连同一个库，
回滚时更是只剩老版本对着新 schema 跑——这一段此前只有「读代码觉得没问题」，没有数据。

判据定死：**不看启动日志有没有报错，只看 `LiteLLM_SpendLogs` 里有没有落行。**
mock 模型 `gate-mock` 回固定串 `gate-ok`，每条请求的 response `id` 就是 `request_id`，
拿它反查 DB 就能逐行认领是哪个版本写的。

### 7.1 老版本对着新 schema 跑（回滚方向）

clone-b（已迁到 162 条），起 v1.90.2 原生产 digest `7286aa2d`：

```
UP after 39s            ← /health/liveliness 200
HTTP_OK=1  CONTENT_OK=1
response_id = chatcmpl-1c7dedf7-2029-4a73-9550-253b341bad58
```

启动阶段老版本自己跑了 `migrate deploy`，输出 "No pending migrations"，
`All necessary views exist!`，policies / attachments / MCP servers / search tools
四类注册表全部同步成功——**老 Prisma client 读新表没有任何问题**。

写的证据（30 s flush 之后查 clone-b）：

```
spendlogs_total = 1
request_id  = chatcmpl-1c7dedf7-2029-4a73-9550-253b341bad58   ← 与 response id 逐字一致
model       = openai/gpt-4o-mini        model_group = gate-mock
created_at  = 2026-09-13 11:32:52.985   ← 本轮迁移新增的列
updated_at  = 2026-09-13 11:32:52.985   ← 老版本完全不知道它存在，DB 默认值自动填上
errorlogs   = 0                         DB 路径报错 = 0
```

**老版本不写新列，新列靠 `DEFAULT CURRENT_TIMESTAMP` 自己填。** 这正是 §3 那条
metadata-only `ADD COLUMN` 的另一面：它既没重写表，也没让老代码的 INSERT 失效。

### 7.2 新旧同时连同一个库（灰度方向）

clone-c 上**同时**拉起 v1.90.2（`7286aa2d`）和 v1.100.1 补丁版（`abdf6133`），
各打 3 条请求。按 `request_id` 逐行认领，落库顺序：

| startTime | 写入方 | request_id | created_at/updated_at |
|---|---|---|---|
| 11:39:01.298 | **NEW** v1.100.1 | `chatcmpl-ac83cc82…` | 已填 |
| 11:39:03.326 | **NEW** v1.100.1 | `chatcmpl-e79e637a…` | 已填 |
| 11:39:04.482 | **OLD** v1.90.2 | `chatcmpl-8a1e59fa…` | 已填 |
| 11:39:05.336 | **NEW** v1.100.1 | `chatcmpl-d82c5b91…` | 已填 |
| 11:39:06.512 | **OLD** v1.90.2 | `chatcmpl-57e063f7…` | 已填 |
| 11:39:08.525 | **OLD** v1.90.2 | `chatcmpl-8037c5f8…` | 已填 |

6 行全部认领成功，`UNMATCHED = 0`，两个版本的写在 7.2 秒窗口里**真实交错**——
不是一前一后跑完，是同时在写。

共存前后核账：

```
clone-c schema 指纹  前 = 1085|86|162|ba58039648be3e85d7267ce48524ad26
clone-c schema 指纹  后 = 1085|86|162|ba58039648be3e85d7267ce48524ad26   ← 逐字一致
null_created = 0   null_updated = 0
invalid_indexes = 0     LiteLLM_ErrorLogs = 0     DB-path 报错 = 0
```

**指纹前后一致是这一节最关键的一个数**：老版本在启动时确实跑了 schema 更新逻辑
（生产没设 `DISABLE_SCHEMA_UPDATE`，走的就是这条路），但它对已迁移的库是 no-op，
**没有把 schema 往回拽**。历史上担心的 "schema thrashing" 在这条路径上不成立。

### 7.3 新版本正向 + 补丁存活

clone-a（已迁到 162 条），起 v1.100.1 补丁版 `abdf6133`：

```
PATCH_capacity = 1      ← exception_mapping_utils.py 里 "selected model is at capacity" 在
PATCH_baremap  = 1      ← responses/streaming_iterator.py 里 _BARE_STATUS_MAP 在
UP after 35s   ROUNDTRIPS_OK = 3/3
clone-a SpendLogs = 3 行，最后一条 2026-09-13 11:39:06.084
```

两个补丁锚点在**运行时镜像内**仍然命中（不是构建期 grep 的复述），且新版本读写新
schema 正常。

> 量具纪律备注：第一版 gate 用 `curl` 探活，Job 报 Failed——但日志里 proxy 明明
> `Application startup complete`。那是**量具坏了**（镜像里 curl 的行为不符合预期），
> 不是被测对象坏了。换成 `python3 urllib` 后同一个镜像立刻 3/3 绿。
> 拿这种红去下"不兼容"的结论，就是典型的合成红。

## 8. 对迁移窗口的最终结论

- 迁移窗口内**不存在**与数据量相关的语句（前提：§6 的索引已预建完成并 valid）。
- 31 条迁移全部是建新表 / metadata-only `ADD COLUMN` / 空表上的 index 与 constraint。
- 因此**不需要**按 141 GB 外推的长维护窗口。
- 剩下的唯一风险是 `ACCESS EXCLUSIVE` **锁获取**排队 ⇒ migration Job 必须带短
  `lock_timeout`（方案 §2.2 第 2 条已要求，如 `5s`），拿不到锁就快速失败重试，
  绝不排队把业务写入堵在后面。
- 失败后**禁止**自动重跑整个 Job（DDL 可能已部分提交），按方案 §2.2 的
  partial-DDL 分支：先导出实际 schema 与 ledger 对账，只补确认未落地的幂等语句。
- 迁移**之后**的兼容性也已实测（§7）：老版本对新 schema 可读可写、不回拽 schema，
  新旧可同时连同一个库。⇒ **灰度和回滚都不依赖 schema 层面的假设。**

---

## 附：本文件所有数字的采集位置

生产只做**只读**查询（`pg_stat_user_tables` / `pg_index` / `pg_settings` /
`pg_stat_activity` / `pg_stat_progress_create_index` / cgroup 文件），
唯一的写操作是 §6 的 `CREATE INDEX CONCURRENTLY`，回滚方式同 §6。
§3 的 `ALTER TABLE` 与阳性对照全部在 **clone-c** 上执行，未触碰生产。
§7 的三个运行时 gate 分别跑在 **clone-b**（老版本回滚方向）、**clone-c**（新旧共存）、
**clone-a**（新版本正向），全部在 `litellm-clone` 命名空间内，未触碰生产。

三个 clone 在 gate 之前已由生产 `pg_dump --schema-only` 重置并补齐 `_prisma_migrations`，
各自独立跑过一次迁移演练：**每个 clone 都恰好应用 31 条**（独立复现了 §1 的待执行集合），
耗时 11–12 s，终态均为 1085 列 / 86 表 / 162 条迁移。
