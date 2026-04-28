# CarHer Admin 改进事项清单

> 仅做诊断，不修改代码。基于对 `backend/`、`frontend/`、`operator-go/`、`k8s/` 全量阅读整理。

---

## 〇、优先级总览

| 等级 | 含义 | 数量 |
|------|------|------|
| 🔴 P0 | 影响安全/数据完整性，需立即修复 | 8 |
| 🟠 P1 | 影响可用性/性能，近期排期 | 12 |
| 🟡 P2 | 工程质量/可维护性 | 14 |
| 🔵 P3 | 长期优化 | 6 |

---

## 一、Backend（FastAPI / Python）

### 🔴 P0 安全与可靠性

| # | 问题 | 位置 | 影响 | 建议 |
|---|------|------|------|------|
| B1 | **Webhook 仅做明文 secret 比对，无 HMAC 签名校验** | `main.py` `/api/deploy/webhook` | 第三方知道明文即可伪造部署事件，触发任意镜像下发 | 校验 `X-Hub-Signature-256`，HMAC-SHA256 比对 raw body |
| B2 | **async 路由内同步调用阻塞 K8s SDK** | `main.py` 多处、`agent.py:_execute_tool` | 单个 restart/stop 阻塞事件循环，并发请求堆积导致响应雪崩 | 统一通过 `run_in_executor` 或 `kubernetes_asyncio`，避免在 async 函数内直接调用阻塞方法 |
| B3 | **Agent 工具无权限分级** | `agent.py` 工具表 | 单一登录态可让 LLM 触发 restart/delete，prompt-injection 即可放大破坏 | 工具按 read / mutate / destructive 三级；destructive 强制人工确认；批量 > N 实例需二次确认 |
| B4 | **Secret/敏感信息泄露**：config-preview 接口返回解码后的 appSecret | `main.py` config-preview、`crd_ops.py:59` | 拥有控台账号即可读所有飞书 appSecret | 接口屏蔽敏感字段；K8s 启用 etcd encryption-at-rest |
| B5 | **动态 SQL 字段拼接** | `database.py:444、665`（update / update_deploy_group） | 白名单一旦遗漏即任意字段写入 | 显式字段映射或 ORM；杜绝外部 key 直接拼 SQL |

### 🟠 P1 可靠性 / 性能

| # | 问题 | 位置 | 影响 | 建议 |
|---|------|------|------|------|
| B6 | **SQLite 并发写竞态**：`next_id()` 非事务 | `database.py:376` | 高并发创建实例可能撞 uid，唯一约束失败 | `INSERT ... RETURNING` 或事务内 SELECT FOR UPDATE 等价做法 |
| B7 | **CRD/DB 双写无补偿**：CRD 失败但 DB 已写入或反之 | `main.py` 创建/删除路径 | 状态长期不一致，需要人工修 | 先 CRD 后 DB（CRD 失败回滚 DB），或 reconcile 任务定期校对 |
| B8 | **错误吞没**：`except Exception` 仅 log，HTTP 返回 500 无 detail | `main.py:705、837` 等 | 客户端无法定位失败原因 | 捕获 `ApiException` 透出 `e.body`/`e.reason` |
| B9 | **aiohttp Session 资源泄漏** | `agent.py:_get_llm_session` | 长跑后连接池耗尽，LLM 调用超时 | shutdown hook 中 close；或每次请求级 session |
| B10 | **deploy 并发无 Semaphore**，BATCH_SIZE=50 但无全局闸门 | `deployer.py:200` | 大批量部署同时打 K8s API，触发限流 | `asyncio.Semaphore` + Pod 状态 TTL 缓存 |
| B11 | **超时/重试策略散落不一**（15s/30s/10s 各处） | 全局 | 行为不可预测 | 抽 `config.timeouts`，区分 connect/read |

### 🟡 P2 可维护性

| # | 问题 | 建议 |
|---|------|------|
| B12 | `main.py` 单文件巨大（~2k 行 60+ 路由），承担路由 + 业务 + 编排 | 按域拆 router：`routers/instances.py`、`deploy.py`、`metrics.py`、`agent.py` |
| B13 | 硬编码常量（namespace、镜像前缀、阈值）散落各文件 | 集中到 `config.py`，从 env 读取 |
| B14 | 类型注解残缺，无 mypy/pyright | 全量补齐；CI 加 type-check |
| B15 | 日志无 request_id、级别混乱 | 统一 structlog/loguru，注入 correlation id |
| B16 | 无 pytest 套件 | 至少为 deployer、crd_ops、agent 工具表写关键路径测试 |
| B17 | 输入校验薄弱（uid `int(...)` 不 catch、name 仅 alphanumeric 检查） | 统一 Pydantic v2 模型；Agent params 也走 schema |

---

## 二、Frontend（React 19）

### 🔴 P0 安全

| # | 问题 | 位置 | 影响 | 建议 |
|---|------|------|------|------|
| F1 | **Token 明文存 localStorage** | `api.js:4` | 任一 XSS 即盗号 | 改 HttpOnly Cookie + CSRF token；或至少 sessionStorage + 严格 CSP |
| F2 | **危险操作用浏览器 `confirm()`** | `InstanceList.jsx:79`、`InstanceDetail.jsx:49` | 误删/误重启零保护 | 自定义 ConfirmModal，需输入实例名 + 倒计时撤销 |
| F3 | **Login 绕过统一 api 层手写 fetch** | `LoginPage.jsx:15` | 容易遗漏拦截器、错误处理 | 收敛入 `api.login()` |

### 🟠 P1 性能与体验

| # | 问题 | 位置 | 影响 | 建议 |
|---|------|------|------|------|
| F4 | **实例列表无分页/虚拟列表** | `InstanceList.jsx` | 500+ 实例渲染明显卡 | 后端已有 offset/limit；前端用 `react-window`/TanStack Table |
| F5 | **轮询无退避/合并**：DeployPage、LogViewer 5s 死循环 | 多处 | 服务端无意义压力 | 切 SSE/WebSocket，或指数退避；页面隐藏时停 |
| F6 | **状态爆炸**：DeployPage 16 个 useState | `DeployPage.jsx` | 易出状态不一致 bug | `useReducer`；少量全局态用 Zustand |
| F7 | API base URL 硬编码 `/api` | `api.js:1` | 多环境部署受限 | `import.meta.env.VITE_API_BASE` |

### 🟡 P2 质量

| # | 问题 | 建议 |
|---|------|------|
| F8 | 零 PropTypes / 无 TS | 至少加 PropTypes，长期迁 TypeScript |
| F9 | 重复 fetch + try/catch 模板 | 抽 `useAsync`/`useApi` Hook，统一 loading/error |
| F10 | 全局 `alert()` 错误提示 | 引入 Toast + ErrorBoundary |
| F11 | useEffect 依赖不全（潜在死循环） | 启用 `eslint-plugin-react-hooks` |
| F12 | 无 404 / 网络异常页 | 加 ErrorBoundary + Suspense fallback |
| F13 | 表格在小屏溢出 | 响应式或卡片视图 |

---

## 三、Operator-go

### 🔴 P0 正确性

| # | 问题 | 位置 | 影响 | 建议 |
|---|------|------|------|------|
| O1 | **Status 更新用 `Update` 不处理 Conflict** | `reconciler.go:119/200/231` | 与 health-checker 并发改 status 时丢更新 | 全部改 `Status().Patch(MergeFrom)`；冲突时 retry |
| O2 | **缺 Finalizer**：删除时直接删依赖资源 | `reconciler.go:103` | operator crash 留孤儿 ConfigMap/Pod | 加 `carher.io/finalizer`，按序清理 |
| O3 | **Status update 错误降到 V(1)** | 同上 | 状态长期偏差不会被告警 | 改 `logger.Error` + metrics counter |

### 🟠 P1 可靠性

| # | 问题 | 位置 | 建议 |
|---|------|------|------|
| O4 | ConfigMap 检查-获取-写存在 TOCTOU | `reconciler.go:293` | Get → 比对 hash → CreateOrUpdate；交给 OwnerReference 清理 |
| O5 | Requeue 不区分错误类型 | `reconciler.go:128/133/139` | NotFound 直接终止；Conflict 立即重试；网络错误短退避；其他长退避 |
| O6 | Health Patch 无 retry / 无幂等保护 | `health.go:211` | 加 retry + uid 校验 |
| O7 | KnownBots 失败无指数退避 | `known_bots.go:148` | 失败计数 + 告警阈值 |

### 🟡 P2 可观测性

| # | 问题 | 建议 |
|---|------|------|
| O8 | **零 Event 记录** | 注入 EventRecorder，关键路径 `recorder.Event(her, ...)` |
| O9 | 日志非结构化、无 trace id | 全链路 `logger.WithValues("uid",..., "action",...)`，JSON encoder |
| O10 | Operator 自身 metrics 缺：reconcile_errors / status_conflicts / queue_depth | 在 `metrics.go` 新增 |
| O11 | Config hash 用 MD5 截断 12 字符 | 改 SHA256 截 16 字符以上 |
| O12 | HealthChecker 30s 太长，KnownBots 10s 偏慢 | 健康检查降到 10–15s；KnownBots 改事件驱动（reconcile 内直接更新） |

---

## 四、K8s 清单

### 🔴 P0 安全

| # | 问题 | 位置 | 建议 |
|---|------|------|------|
| K1 | **Operator/Admin Pod 无 securityContext** | `operator-deployment.yaml`、`deployment.yaml` | `runAsNonRoot`、`readOnlyRootFilesystem`、`drop ALL caps`、`allowPrivilegeEscalation:false` |
| K2 | **Secret 明文存 etcd**（master key、postgres 密码） | `litellm-secrets.yaml` | etcd encryption-at-rest；或 SealedSecrets/ExternalSecrets |
| K3 | **无 NetworkPolicy** | 全局 | default-deny + 白名单（admin↔postgres、operator↔apiserver、proxy↔redis） |

### 🟠 P1 高可用

| # | 问题 | 建议 |
|---|------|------|
| K4 | 无 PodDisruptionBudget | 至少为 operator(2)、postgres、redis、proxy 加 `minAvailable:1` |
| K5 | `imagePullPolicy: Always` + 可变 tag 风险 | 镜像 tag 强制带 commit hash；改 `IfNotPresent` |
| K6 | postgres 无 livenessProbe（仅 readiness） | 加 `pg_isready` liveness |
| K7 | 无 HPA | litellm-proxy / carher-admin 加 CPU/Mem HPA |
| K8 | Leader Election 用默认 lease(15s) | 显式配置 lease/renew/retry |

### 🟡 P2

| # | 问题 | 建议 |
|---|------|------|
| K9 | RBAC 未做最小化（多处 `*` verbs 嫌疑） | 按需收敛 |
| K10 | CRD 长期 `v1alpha1` | 准备 v1beta1，提供 conversion |
| K11 | `automountServiceAccountToken` 默认开启 | 不需 API 的 Pod 显式关闭 |

---

## 五、跨层 / 工程治理

| # | 问题 | 建议 |
|---|------|------|
| X1 | 无 CI（lint/test/build/镜像签名） | GitHub Actions：ruff + mypy + pytest + go test + golangci-lint + eslint + 镜像 SBOM |
| X2 | 无 e2e（kind + Helm/Kustomize 验证 reconcile 幂等） | 加 envtest / kind 集成测试 |
| X3 | 文档分散（docs/ 含临时 dmg、ppt、csv 等无关文件） | 清理仓库根，建立 docs/architecture, docs/runbook, docs/adr 目录 |
| X4 | 无 ADR（架构决策记录），operator 选型/灰度策略等历史无据可查 | 引入 `docs/adr/` |
| X5 | 备份/恢复路径未文档化（NAS、SQLite、Postgres） | 写 runbook + 定期演练 |
| X6 | 无 SLO / 错误预算定义 | 选 1–2 个核心指标（部署成功率、Pod 恢复 P95）定 SLO |

---

## 六、推荐实施顺序

1. **第 1 周（堵安全口）**：B1 / B3 / B4 / F1 / F2 / K1 / K2
2. **第 2 周（修正确性）**：O1 / O2 / O3 / B6 / B7 / O4
3. **第 3 周（提稳定性）**：B2 / B10 / O5 / O6 / K3 / K4
4. **第 4 周（看得见）**：O8 / O9 / O10 / B15 / F10
5. **持续推进**：CI、测试、TS 化、文档治理

---

> 备注：以上诊断基于当前 main 分支代码静态阅读，部分"潜在"问题需结合线上实际运行情况二次确认（例如 SQLite 写竞态是否真触发、postgres liveness 是否实际卡死）。建议落地修复时先补监控/日志再动代码。
