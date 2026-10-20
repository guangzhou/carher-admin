# ai_token_dashboard 上游修复（未进 GitHub）

`davidzhu2021/ai_token_dashboard` 我们没有推送权限。0001/0002 是 243 sync worker 正在跑的
`ai-token-dashboard:fix-9f250f8` 相对线上 `2818c0d` 的两个提交；0003~0008（2026-10-20，**均已上线**）
叠在 9f250f8 上，修全员看板慢、刷新队列卡死、发布时 CrashLoop。等有权限或交给项目负责人合并：

| 文件 | 内容 |
|---|---|
| `0001-*.patch` | 日志扫描：上游计数封顶时按时间二分读全；原始日志被清空（扫描 < 日汇总 50%）时判不完整，不再用 0 覆盖快照 |
| `0002-*.patch` | 按 key 补齐用户表之外的用量（`key:<alias>` 单列、只显示不计费）+ 发布前与上游总花费对账 |
| `0003-admin-dept-names-groupby.patch` | 全员看板：部门名查询去掉整表窗口函数（1.2s → 0.3s，生产库 5 种组合 md5 一致） |
| `0004-admin-skip-spend-scan-stable-cache-key.patch` | 全员看板：活动汇总已有金额时不再扫被 10000 封顶的明细（每次白等约 2s）；缓存 key 去掉 Redis 实时计数（命中率 27%） |
| `0005-refresh-queue-fewer-attempts-first.patch` | 刷新队列 `ORDER BY attempts, requested_at`：老请求重试上千次时新日期不再被饿死 |
| `0006-sparse-gate-upstream-near-zero.patch` | 「非空用户过少」门禁：上游全局汇总 ≤ 每天 100 请求时放行（her 迁走后的零流量日不再无限重试）。`USAGE_SYNC_SPARSE_MAX_DAILY_REQUESTS=0` 恢复旧行为 |
| `0007-skip-stability-scan-on-historical-refresh.patch` | 历史日期的刷新不再陪跑「今天」的稳定性扫描（约 23 分钟，导致刷新撞 1800s 超时被杀） |
| `0008-schema-apply-retry-on-lock-conflict.patch` | 启动时建表遇到锁冲突（LockNotAvailable / Deadlock）等锁重试，不再 CrashLoop |
| `DESIGN-log-scan-cap.md` / `DESIGN-key-coverage.md` | 0001/0002 的设计与实测数据 |
| `litellm_spendlog_cap_report.md` | LiteLLM 10000 计数封顶的来源调研（PR #31825 等） |

应用：`git am --keep-cr tools/cli-anything-myai/upstream-fixes/000*.patch`（基于 `2818c0d`；0001/0002 对 master `b13f58c` 两个文件无冲突）。
🔴 **必须带 `--keep-cr`**：`backend/main.py` 是 CRLF/LF 混合行尾，不带会在 0004 报 `补丁未应用`。

测试：改前已有 15 个失败（前端/静态资源断言，与这几个补丁无关），0003~0008 每个提交单独跑都是这 15 个、不新增；
新加 23 条测试，其中 10 条在改前代码上失败（阳性对照）。本地分支在 `.work/myai-admin-perf/ai_token_dashboard`（`perf/admin-board-latency`）。
发布：`scripts/myai-release.sh build|deploy|rollback`（skill `cli-anything-myai` §发布）。
