# MYAI — 代码分析与 CLI 设计（CLI-Anything Phase 1–2）

分析对象：`davidzhu2021/ai_token_dashboard` @ `2818c0d`（= 243 线上镜像，逐文件去 CRLF 后比对 25/25 一致）。

## 后端引擎

没有 GUI 要包，真正的"软件"是一个已部署的系统。CLI 的后端就是这套系统本身，走三条只读通道：

| 通道 | 用途 | 走法 |
|---|---|---|
| 公网 HTTP | `/api/health`（唯一免登录的诊断接口） | `urllib` |
| Postgres | 同步/队列/快照/账单状态 | ssh 243 → `kubectl exec usage-db-0` → `psql --csv`，read-only 事务 |
| 上游 LiteLLM | 核对上游自己报的流量 | ssh 243 → `kubectl exec usage-sync-worker` → `python -`（用 pod 自己的 env 和 key） |

其余 141 个 HTTP 路由都要 SSO 会话 cookie（`ai_token_dashboard_session`，`require_user` / `require_admin` / `require_platform_admin`），
`/api/internal/observability/*` 要 HMAC（`OBSERVABILITY_INGEST_HMAC_SECRET`），所以不当作 CLI 后端。

## 数据模型（只列运维常用的）

| 表 | 写入方 | 含义 |
|---|---|---|
| `usage_sync_runs` | sync worker | 每次同步一行，status 取 ok/partial/failed/skipped/running |
| `usage_sync_state` | sync worker | 单行；`last_success_at` 只在 status=ok 时推进 |
| `usage_snapshot_state` | sync worker | 单行；`published_at` 每次发布都会更新（partial 也算） |
| `usage_refresh_requests` | reader 入队 / worker 消费 | 失败后指数退避重试（`USAGE_REFRESH_RETRY_BASE/MAX_SECONDS` 30/900） |
| `usage_daily` | sync worker | 历史快照 |
| `usage_realtime_daily` / `_state` / `_settlement` | realtime worker | 实时镜像与结算水位 |
| `usage_query_daily`（视图） | — | 看板实际读的：实时数据就绪且完整时盖过历史快照 |
| `usage_identity_directory` | sync worker | 账号 → 人 |
| `billing_order` / `organization_billing_order` / `billing_account` / `customer_billing_ledger` | dashboard | 个人和企业两套账单 |
| `customer_outbox` | dashboard | 异步同步到上游的任务 |

最大的两张表是 `usage_event_attribution`（9.7GB）和 `stability_attempt_events`（6.4GB），CLI 刻意不扫它们。

## 同步管线要点

- worker 每 `USAGE_SYNC_INTERVAL_SECONDS`（1800）跑一次近 `USAGE_SYNC_RECENT_DAYS`（2）天；同时轮询刷新队列（15s）
- 窗口 ≤ `USAGE_SYNC_LOG_MAX_WINDOW_DAYS`（3）天时 primary 走日志扫描；**her（`source="Her"`）永远走逐账号 daily activity**
- 稀疏门禁：尝试人数 ≥ 20，且有用量人数 < `max(1, int(尝试人数 × RATIO))` 时判 partial（RATIO=`USAGE_SYNC_FALLBACK_MIN_NONEMPTY_RATIO`，默认 0.05）
- partial 时发布通过门禁的数据源，拦下的那个丢弃；运行状态记为 partial

## 命令设计

按运维问题分组，不按表分：`status`、`diagnose`、`sync`、`queue`、`realtime`、`usage`、`identity`、`billing`、`upstream`、`db`、`logs`、`pods`、`repl`。
状态全在远端，所以不做 session/undo；没有写操作，所以 `--dry-run` 不适用。
