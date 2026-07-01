# ChatGPT acct 池 reactive cooldown 方案（LiteLLM 层）

> 目标：撞顶的 acct 在 **1 次失败请求**之内被全集群所有 proxy pod 摘除，**零浪费配额**，与现有 cron 治理流水线零冲突。
>
> 作者笔记 2026-06-27 / LiteLLM v1.89.4 / 198 prod

---

## 1. 问题

198 prod LiteLLM 池里 60+ 个 chatgpt-acct deployment。当某 acct 上游 5h 或 7d 配额撞顶时，要做到：

- ✅ **及时**：下一次请求不再打到这个 acct
- ✅ **不浪费**：不能撞 95% 就摘（OpenAI 必须撞 100% 才发 banked credit）
- ✅ **全集群一致**：4 个 proxy pod 共享 cooldown 状态
- ✅ **跟现有 cron 共存**：cron 每 5min 跑 quota-rebalance.py 做 `/model/delete + /model/new` 重建

现状：cron 是唯一防线，滞后 0-5min。撞顶后这窗口里所有请求继续打到死号 → 用户感知 wangsu fallback。

---

## 2. 设计哲学

**抄 sub2api 的被动模型**：

```
不主动 probe → 不浪费配额
等上游 429 → router 立刻摘 → 1 次失败即生效
Retry-After header 决定锁多久 → 5h/7d 不同窗口自动适配
```

不写任何 custom callback，**只调三个 router 参数 + 1 个 Redis 配置 + 校验 1 个 patch**。

---

## 3. 整体架构

```
                  ┌──────────────────────────────────────┐
                  │         Client (codex/cursor)        │
                  └──────────────────┬───────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
       ┌────────────┐         ┌────────────┐         ┌────────────┐
       │ proxy pod1 │         │ proxy pod2 │   ...   │ proxy pod4 │
       │  Router    │         │  Router    │         │  Router    │
       └──────┬─────┘         └──────┬─────┘         └──────┬─────┘
              │                      │                      │
              └──────────────────────┴──────────────────────┘
                                     │
                          ┌──────────▼──────────┐
                          │  Redis (shared)     │
                          │  cooldown:<id>      │  ← cooldown 状态全集群共享
                          │  TPM/RPM counters   │
                          └─────────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
       ┌────────────┐         ┌────────────┐         ┌────────────┐
       │ acct-1     │         │ acct-2     │   ...   │ acct-68    │
       │ chatgpt    │         │ chatgpt    │         │ chatgpt    │
       │ provider   │         │ provider   │         │ provider   │
       └────────────┘         └────────────┘         └────────────┘
```

---

## 4. 触发链（撞顶到摘除的完整时序）

```
T0  请求 #N 路由到 acct-7
    │
    ▼
T1  上游 chatgpt.com 返回 ────────────────────┐
    │                                          │
    ├─[A] 标准 429 + Retry-After: <reset_sec>  │  ← 5h/7d 窗口剩余秒数（如 1823 = 还剩 30min）
    ├─[B] 200 + "at capacity"                  │  ← capacity-patch
    ├─[C] 200 + output=[] (issue #25429)       │  ← swallow bug
    └─[D] 401                                  │  ← token 失效
                                               │
    ┌──────────────────────────────────────────┘
    ▼
T2  exception_mapping_utils:
    [A] → RateLimitError(429)         原生
    [B] → RateLimitError(429)         capacity-patch 升格
    [C] → RateLimitError(429)         需补 patch（见 §6.4）
    [D] → AuthenticationError(401)    走另一条 cron 流程
    │
    ▼
T3  Router.deployment_callback_on_failure
    │   ├─ _is_cooldown_required() → True
    │   └─ allowed_fails=1 → 撞 1 次即冷
    │
    ▼
T4  _set_cooldown_deployments(
        deployment_id="chatgpt-acct-7-gpt-5.5",
        time_to_cooldown = Retry-After or 3600,
        exception_status=429,
    )
    │
    ▼
T5  Redis 写: cooldown:chatgpt-acct-7-gpt-5.5 = now + <reset_sec>
    │           TTL = <reset_sec>（如 1823s）
    │
    ▼
T6  本次请求按 fallback chain → 下一个健康 acct
    │
    ▼
T7  其他 3 个 proxy pod 下次路由 → 读 Redis cooldown → 跳过 acct-7
    │
    ▼
T8  3600s 后 cooldown 自动失效，acct-7 回池
    │   OR：cron 5min 内探到撞顶，/model/delete + /model/new 重建
    │        → 新 deployment_id 自动跳出 cooldown 表
```

**关键时序属性**：
- **T1→T7 滞后 = 0**：第二次请求开始全集群所有 pod 都跳过
- **撞顶时只浪费 1 次请求**（T0 那次拿到 429/200+空）
- **不主动 probe**：banked credit 攒满与否完全交给上游

---

## 5. cron 与 LiteLLM cooldown 的共存模型

```
                    ┌──────────────────────────────────┐
                    │      撞顶事件 (T0)               │
                    └────────────────┬─────────────────┘
                                     │
                ┌────────────────────┼────────────────────┐
                ▼                    ▼                    ▼
       LiteLLM router        cron (5min 后)        cron banked redeem
       (T0 + 0s)             quota-rebalance.py    (7d=100 时)
                │                    │                    │
                ▼                    ▼                    ▼
       cooldown:<id>        /model/delete +       wham/consume
       Redis TTL 1823s      /model/new 重建        7d → 0
                │                    │                    │
                ▼                    ▼                    ▼
       全集群跳过 acct       新 deployment_id      cron resume_acct
       (zero downtime)      自然绕过 cooldown      回池
```

**两条流水线天然不打架**，因为：
- LiteLLM cooldown key = `deployment_id`（如 `chatgpt-acct-7-gpt-5.5`）
- cron 重建用同名 `id`，**但 router 内存里是新对象**，cooldown 表里旧 entry TTL 到了自动清

**cron 职责收敛**：
| 之前 | 之后 |
|---|---|
| 5min 内摘死号 | LiteLLM 层 0s 摘 |
| `/model/delete + /model/new` 重建 | 保留：处理 401 / scale 异常 / 复活检测 |
| banked credit redeem | 保留：唯一 redeem 路径 |
| 兜底 (proxy 重启 / Redis 失联) | 保留：核心冷启动恢复 |

---

## 6. 实装步骤

### 6.1 配置 router cooldown 参数

`k8s/litellm-proxy.yaml` 同级 ConfigMap `litellm-config`：

```yaml
router_settings:
  routing_strategy: simple-shuffle      # 保持现状
  allowed_fails: 1                       # ← 撞 1 次就冷（默认 3）
  cooldown_time: 3600                    # ← Retry-After 缺失时 fallback（默认 5s）
  redis_host: litellm-redis.litellm-product.svc.cluster.local
  redis_port: 6379
  redis_password: os.environ/REDIS_PASS  # 已有
```

env 兜底（防 ConfigMap 漏 patch）：

```yaml
env:
  - name: DEFAULT_ALLOWED_FAILS
    value: "1"
  - name: DEFAULT_COOLDOWN_TIME_SECONDS
    value: "3600"
```

### 6.2 验证 Redis 共享生效

```bash
# pod1 触发一次 cooldown
jms ssh AIYJY-litellm "kubectl -n litellm-product exec deploy/litellm-proxy -- \
  redis-cli -h litellm-redis KEYS 'cooldown_models*'"

# 应看到 chatgpt-acct-N-gpt-5.5 类 key，TTL > 0
# 4 个 pod 任一 redis-cli 都应看到同样 key
```

### 6.3 验证 Retry-After 优先级（PR #12037）

```bash
# 找一个已撞 5h 顶的 acct，手动触发 429
ACCT=N  # 撞顶的 acct 编号
jms ssh AIYJY-litellm "kubectl -n litellm-product exec deploy/litellm-proxy -- \
  curl -i http://chatgpt-acct-$ACCT.litellm-product.svc.cluster.local:4000/v1/responses \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"chatgpt-gpt-5.5\",\"input\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"

# 看响应头 Retry-After（应是 5h 剩余秒数）
# 然后 redis-cli 看 cooldown TTL 是不是接近 Retry-After 值（而不是固定 3600）
```

### 6.4 校验 capacity-patch 抛的异常类型 ⚠️ 关键

```bash
jms ssh AIYJY-litellm "kubectl -n litellm-product exec deploy/litellm-proxy -- \
  grep -B2 -A10 'at capacity' /app/litellm/llms/chatgpt/responses/transformation.py"
```

**必须看到**：

```python
raise litellm.RateLimitError(
    message="...",
    model=...,
    llm_provider="chatgpt",
)
```

如果是裸 `HTTPException(429, ...)` 或 `Exception(...)`，router 不会识别。改成 `litellm.RateLimitError` 走 router 异常路径才会冷却。

### 6.5 swallow bug (issue #25429) 补 patch

`response.completed.output=[]` 这种 200 当前 capacity-patch **可能没覆盖**。检查现有 patch：

```bash
jms ssh AIYJY-litellm "kubectl -n litellm-product exec deploy/litellm-proxy -- \
  grep -n 'output.*\[\]\|len.*output.*0\|empty.*output' \
  /app/litellm/llms/chatgpt/responses/transformation.py"
```

如果没匹配，在 transformation.py 加：

```python
# 在 ChatGPTResponsesAPIConfig.transform_response 末尾
if response_obj.status == "completed" and not response_obj.output:
    raise litellm.RateLimitError(
        message="ChatGPT returned empty output (likely quota exhausted, issue #25429)",
        model=model,
        llm_provider="chatgpt",
    )
```

### 6.6 灰度上线

```
phase 1: 改 1 个 canary pod（litellm-proxy-canary deploy）观察 1h
phase 2: 全量 rollout
phase 3: 观察 24h cron.log，确认 cron 仍能 redeem，没死循环
```

---

## 7. 验收指标

| 指标 | 验收线 |
|---|---|
| 撞顶到全集群摘除的滞后 | ≤ 1 次失败请求（≤ 5s） |
| 单次撞顶浪费的请求数 | = 1（之前 = ~N 次/5min） |
| cooldown 时长是否匹配 reset | 看 Redis TTL ≈ Retry-After |
| cron 冲突 | cron resume 后 router 立刻可用（cooldown 通过 model_id 重建绕过） |
| wangsu fallback 次数 | 应降到接近 0（除非 pool 真全空） |
| **切 acct 后老会话不报错** | turn-N 跨 acct HTTP 200（上下文可丢，但不能 400/503）— 见 TC-E10a |

---

## 8. 失败回滚

### 8.1 配置回滚（router params）

```yaml
# ConfigMap litellm-config router_settings
router_settings:
  allowed_fails: 3        # 默认
  cooldown_time: 5        # 默认
  # 删 allowed_fails_policy 整段
```

reloader sidecar 5s 内热加载，不必 rollout restart。

### 8.2 patch 回滚（image 层）

§6.4 / §6.5 / §9.2 / §9.5 都改了 `transformation.py`。如果要回滚：

```bash
# 改 deploy/litellm-proxy image 回 vanilla 上一稳态
# 当前 capacity-patch image 见 [[litellm-198-pro-capacity-patch]]
jms ssh AIYJY-litellm "kubectl -n litellm-product set image \
  deploy/litellm-proxy litellm-proxy=<vanilla-image-tag>"
jms ssh AIYJY-litellm "kubectl -n litellm-product rollout status deploy/litellm-proxy"
```

vanilla 回滚后 [B/C/D/G] 形态全部回到失败行为（200+空当成功 / 401 不冷），cron 重新成为唯一防线。

### 8.3 部分回滚（保留配置只回 patch / 反之）

- 只回配置：`allowed_fails:3 + cooldown_time:5`，patch 仍生效但 cooldown 触发条件回到旧默认
- 只回 patch：`allowed_fails:1` 仍冷，但只对原生 429（A 形态）生效，[B/C/D] 失去 cooldown

退化后 cron 兜底，**不会丢服务**。

---

## 9. 异常流处理

### 9.1 异常分类矩阵

| 上游返回 | LiteLLM 识别 | router 行为 | 本方案处理 | 兜底 |
|---|---|---|---|---|
| **A. 标准 429 + Retry-After** | `RateLimitError(429)` | cooldown = Retry-After | ✅ 原生 | — |
| **B. 200 + "at capacity"** | 需 capacity-patch 升格 | cooldown = 3600s（无 header） | ✅ §6.4 | cron 5min |
| **C. 200 + output=[]** (issue #25429) | 需补 patch 升格 | cooldown = 3600s | ✅ §6.5 | cron 5min |
| **D. 401 token 失效** | `AuthenticationError(401)` | **不冷却**（status_code != 429） | ⚠️ §9.2 | cron `manual_offline` |
| **E. 500/502/503/504 上游错** | `APIError(5xx)` | cooldown 触发（5xx 算失败） | ⚠️ §9.3 误伤风险 | — |
| **F. httpx ReadTimeout / ConnectError** | 包成 `APIConnectionError` | **不冷却**（issue #24366） | ⚠️ §9.4 | cron / 看下游 svc |
| **G. 200 + status=incomplete** (context cap) | 当成功 | 不冷却 | ⚠️ §9.5 不是 quota 问题 | — |
| **H. 200 + 完全正常** | 当成功 | TPM/RPM++ | ✅ 无需处理 | — |
| **I. fallback chain 跑空** | `NoDeploymentsAvailable` | 抛给 client | ⚠️ §9.6 | cron resume |

### 9.2 401 token 失效 — 路由风暴风险

**为什么不能用 LiteLLM cooldown 处理**：
- 401 → `AuthenticationError`，router `_is_cooldown_required()` 默认不冷却（只对 429/5xx 冷）
- 即使配 `allowed_fails_policy.AuthenticationErrorAllowedFails`，401 通常是 token 永久死了 → 应该 `manual_offline` 而不是临时 cooldown

**风险**：token 死了的 acct 每次请求都返 401，**没有 cooldown 屏障** → 还是会被 simple-shuffle 选中浪费请求

**处理**：
- cron 走 `manual_offline=true` 路径，下次 reconcile 时 `/model/delete` 永久从 router 拔掉
- 在 capacity-patch 同文件**额外**补一段（让 401 也走 RateLimitError 路径，至少能短期 cooldown 不浪费）：

```python
# transformation.py transform_response 入口
if response.status_code == 401:
    raise litellm.RateLimitError(
        message=f"401 token invalid (will be moved to manual_offline by cron)",
        model=model, llm_provider="chatgpt",
    )
```

→ 撞 1 次 401 即 cooldown 3600s，5min 后 cron 收尾 `/model/delete` 永久摘除。

### 9.3 上游 5xx 误伤防护

**问题**：5xx 触发 cooldown，但 chatgpt.com 偶尔抖动（502/503），健康 acct 被冷 3600s = 浪费

**处理（推荐）**：用 `allowed_fails_policy` 区分 — 5xx 给宽容次数，429 撞 1 次即冷。**保持 `cooldown_time: 3600`**（5xx 抖动 3 次浪费可接受，避免双策略撕扯）。

```yaml
router_settings:
  cooldown_time: 3600              # 主策略：429 Retry-After 缺失时锁 1h
  allowed_fails: 1                  # 默认：429 撞 1 次冷
  allowed_fails_policy:
    InternalServerErrorAllowedFails: 3   # 5xx 容忍 3 次（chatgpt.com 偶尔抖动）
    BadRequestErrorAllowedFails: 100     # 4xx 业务错不冷
    AuthenticationErrorAllowedFails: 1   # 401 配合 9.2
    RateLimitErrorAllowedFails: 1        # 429 撞 1 次冷
    TimeoutErrorAllowedFails: 3          # 超时容忍 3 次
```

> ⚠️ canary 必须实测：上 §10.5 TC-E3 跑一遍，confirm `allowed_fails_policy` 字段被 router 接收（v1.89.4 `types/router.py` `AllowedFailsPolicy` 存在，但配置加载路径偶尔不读子字段，要打日志确认）。

### 9.4 httpx 层网络错误（最坑）

**问题**：
- LiteLLM `_is_cooldown_required()`: `if 'APIConnectionError' in exception_str: return False`
- 下游 svc 真挂时（pod evicted / OOM / NetworkPolicy 卡），httpx 抛 `ConnectError` → 包成 `APIConnectionError` → **永不冷却**
- router 会对同一 deployment 重试 `num_retries` 次全失败 → 用户看到长延迟

**处理**：让 svc 层先挡住（不靠 LiteLLM）

```yaml
# k8s/chatgpt-acct-N-svc.yaml
spec:
  # service 只在 endpoints 非空时才接 traffic（默认行为）
  # readinessProbe 必须严格，pod 不 ready 时 endpoint 自动摘
```

cron 的 §0b 已经覆盖：探到 `spec.replicas=0` → 主动 `/model/delete`。LiteLLM 这层无能为力，**接受 num_retries 重试浪费**，但 cron 5min 内会清。

**优化**：把 `num_retries` 调低（默认 2），减少浪费

```yaml
router_settings:
  num_retries: 1   # 改 2→1，svc 真挂时少试一次
```

### 9.5 200 + status=incomplete（context cap，非 quota）

**这不是 quota 问题，不该冷却**：用户输入超过 model context window（gpt-5.5 实测 ~250K 触发 [[chatgpt_gpt_5_5_400k_real_cap]]）

**capacity-patch 升格时必须区分**：

```python
# transformation.py 升格条件
if response_obj.status == "completed" and not response_obj.output:
    # quota：升格 RateLimitError
    raise litellm.RateLimitError(...)
elif response_obj.status == "incomplete":
    # context cap：升格 ContextWindowExceededError（不冷却）
    raise litellm.ContextWindowExceededError(
        message=f"Context window exceeded: {response_obj.incomplete_details}",
        model=model, llm_provider="chatgpt",
    )
```

`ContextWindowExceededError` 在 router 的 `_is_cooldown_required()` 里默认 return False，不会冷 acct，只把错抛给 client。

### 9.6 fallback chain 跑空 — pool 全冷

**场景**：60 个 acct 全撞顶 → 所有 deployment 都在 cooldown → router 抛 `NoDeploymentsAvailable` → fallback chain 走到 wangsu/zerokey

**当前 fallback 配置**（[[feedback_198_pro_zerokey_pool_fallback_chain]]）：

```yaml
router_settings:
  fallbacks:
    - chatgpt-gpt-5.5: [wangsu-gpt-5.5, openrouter-gpt-5.5]
    - zerokey-pool: [deepseek-v4-pro, chatgpt-gpt-5.5, wangsu-gpt-5.5]
```

**异常流验收**：
- pool 全冷时确保 fallback target 没在 cooldown 表里（target 是 `wangsu-*` 不同 deployment_id）
- 监控 wangsu 调用量 spike：≥ 池子规模 × 50% / 5min 就告警

```bash
# 加监控（飞书 webhook）
jms ssh JSZX-AI-03 "tail -F /home/cltx/.chatgpt-quota/cron.log | \
  grep -E 'router-drift|wangsu.*fallback' | \
  while read l; do curl -X POST <feishu-webhook> -d \"...\"; done" &
```

### 9.7 Redis 失联 — cooldown 退化为单 pod

**场景**：litellm-redis pod 重启 / NetworkPolicy 误配

**LiteLLM 行为**：
- 写 cooldown 失败 → 退化为本地内存 cooldown
- 单 pod 撞 1 次冷，**4 pod 要撞 4 次**（每 pod 各 1 次浪费）
- Redis 恢复后自动重连，cooldown 状态从 0 重建

**监控**：

```bash
# proxy logs grep redis 报错
kubectl -n litellm-product logs -l app=litellm-proxy --tail=100 | \
  grep -iE 'redis.*(timeout|connect|refus)' && \
  echo "REDIS DEGRADED — cooldown is local-only now"
```

**应急**：临时把 `allowed_fails` 调到 0（每次失败立刻冷）减少浪费

```bash
kubectl -n litellm-product set env deploy/litellm-proxy DEFAULT_ALLOWED_FAILS=0
# Redis 恢复后改回 1
```

### 9.8 proxy 重启后冷启动

**场景**：rollout restart 后 Redis 里 cooldown key 还在但 router 内存空

**LiteLLM 行为**：
- router 启动时从 Redis 读 `cooldown_models*` key 重建（`cooldown_cache.py` `_get_cooldown_deployments`）
- **不会丢冷却状态**
- 唯一例外：rollout 同时清了 Redis（运维操作错误）

**操作约束**：
- 严禁 `redis-cli FLUSHDB`（cooldown 表在那）
- 严禁 `redis-cli DEL 'cooldown_models*'`（除非确认要全集群重新探）

### 9.9 LiteLLM cooldown 与 cron 重建冲突

**场景**：LiteLLM 已 cooldown acct-7，cron 同时跑 `/model/delete + /model/new` 重建

**为什么不打架**：
- cron `/model/delete` → router 内存里 deployment 对象消失 → 对应 cooldown key 失去引用（TTL 到后自然清）
- cron `/model/new` 同名 id → router 收到新 deployment 对象 → cooldown 表 lookup miss → 直接可用

**唯一坑**：cron 重建顺序必须 delete → new（中间间隔 ≥ 1s 等 router 内存同步）
- ✅ 现 `resume_acct` 是 `/model/new`，对应 `pause_acct` 是 `/model/delete`，顺序对
- ⚠️ 如果手动操作必须严格按这个顺序，否则 router 会出现"两份同名 deployment"短暂状态

### 9.10 异常流测试

详见 §10 回归用例集合（TC-E1..E9 是异常流子集）。

---

## 10. 回归用例集合

> 上线前在 canary 全跑一遍。每个用例：**前置 → 触发 → 断言 → 清理**。

### 10.0 测试环境约定

```bash
# 所有命令在跳板 JSZX-AI-03 上跑，198/redis 都从那触达
# 本地 Mac 不可达 10.68.13.198，文档里所有 curl/kubectl 都假定在跳板内

# 用一个 ssh-shell 持续在跳板里跑，不要每次 jms ssh 包一层（嵌套 pipe 不工作）
jms ssh JSZX-AI-03
# 进入跳板后：
export PROXY_SVC=litellm-proxy.litellm-product.svc:4000   # cluster-internal
export PROXY_POD                                          # 下面 export
export REDIS_POD                                          # 下面 export
export NS=litellm-product
export KEY=sk-xxx                                          # 测试 key，allowlist 含 chatgpt-gpt-5.5
                                                           # 用 [[carher-key-allowlist-derive]] 单独建一把
export TEST_ACCT=acct-7                                    # 撞顶的备选 acct
export ACCT_DEPLOY=chatgpt-$TEST_ACCT                     # k8s deployment 名

# 取首个 proxy/redis pod 名
export PROXY_POD=$(kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
export REDIS_POD=$(kubectl -n $NS get pod -l app=litellm-redis -o jsonpath='{.items[0].metadata.name}')
echo "PROXY_POD=$PROXY_POD REDIS_POD=$REDIS_POD"
```

> ⚠️ **第一次跑要先调研 cooldown key 真实名字** —— LiteLLM v1.89.4 实际 key 可能不是猜的 `cooldown_models:<id>`。先 grep 实测：
>
> ```bash
> kubectl -n $NS exec $PROXY_POD -- grep -rE 'cooldown.*key|cooldown_models|cache_key' \
>   /usr/lib/python3*/site-packages/litellm/router_utils/cooldown_cache.py | head
> # 拿到真实前缀，在下文所有 redis-cli KEYS / DEL 命令里替换
> export COOLDOWN_KEY_PATTERN="cooldown_models:*"   # ← 用实测结果替换
> ```

### 10.0.1 通用辅助（在跳板 shell 里 source）

```bash
# 拿某 acct 的 LiteLLM router model_id
get_model_id() {
  local acct=$1   # e.g. chatgpt-acct-7
  kubectl -n $NS exec $PROXY_POD -- sh -c "curl -s http://localhost:4000/v1/model/info \
    -H 'Authorization: Bearer $KEY'" | \
    jq -r ".data[] | select(.model_name==\"chatgpt-gpt-5.5\" and .litellm_params.api_base|contains(\"$acct\")) | .model_info.id"
}

# 看一次请求落到哪个 deployment + fallback 状态
trace_call() {
  kubectl -n $NS exec $PROXY_POD -- sh -c "curl -sD- -o /tmp/body http://localhost:4000/v1/responses \
    -H 'Authorization: Bearer $KEY' \
    -H 'Content-Type: application/json' \
    -d '{\"model\":\"chatgpt-gpt-5.5\",\"input\":\"ping\",\"max_output_tokens\":16}'" | \
    grep -iE "^HTTP|x-litellm-(model-id|attempted-fallbacks)"
}

# 强制路由到指定 deployment（绕开 simple-shuffle 随机）
trace_pinned() {
  local target_id=$1
  kubectl -n $NS exec $PROXY_POD -- sh -c "curl -sD- -o /tmp/body http://localhost:4000/v1/responses \
    -H 'Authorization: Bearer $KEY' \
    -H 'X-Litellm-Specific-Deployment: $target_id' \
    -d '{\"model\":\"chatgpt-gpt-5.5\",\"input\":\"ping\",\"max_output_tokens\":16}'" | \
    grep -iE "^HTTP|x-litellm-(model-id|attempted-fallbacks)"
}
# 注：X-Litellm-Specific-Deployment header 在 v1.89.x router 支持；若不支持改成临时把其他 acct cooldown 写满

# Redis cooldown 表全清（canary 环境豁免 §9.8 警告）
flush_cooldown() {
  kubectl -n $NS exec $REDIS_POD -- redis-cli --scan --pattern "$COOLDOWN_KEY_PATTERN" | \
    xargs -r -I{} kubectl -n $NS exec $REDIS_POD -- redis-cli DEL {}
}

# 看当前 cooldown 状态
dump_cooldown() {
  kubectl -n $NS exec $REDIS_POD -- redis-cli --scan --pattern "$COOLDOWN_KEY_PATTERN" | \
    while read k; do
      ttl=$(kubectl -n $NS exec $REDIS_POD -- redis-cli TTL "$k")
      printf "%-60s ttl=%ss\n" "$k" "$ttl"
    done
}
```

### 10.0.2 fail-fast 跑序

```
canary phase:
  1. P0 烟雾：H1 → 失败立停（核心机制不通后面没意义）
  2. Happy 全跑：H2, H3
  3. 异常流：E5 (context cap 不冷) → E6 (Redis 失联) → E7 (proxy 重启) → E8 (cron resume)
  4. 升格 patch：E1 (swallow 用 mock) → E2 (401 用 mock) — 任一 fail 不阻塞 promote，
     但要确认 cron 兜底有效
  5. 边界：B1, B2, B3
  6. 性能：P1

GA phase（promote 全集群后）:
  7. B4 真打 rollout
  8. P2 24h 长稳
  9. E4 / B5 选做（已知缺陷验证）

跨 TC 之间运行 `flush_cooldown` 清状态。
```

---

### 10.1 Happy path 用例

#### TC-H1：标准 429 — 撞顶 acct 1 次失败即冷 ★P0

**前置**：必须有一个 5h `used_percent ≥ 100% / allowed=false` 的 acct。canary 环境没有就先 §10.6 临时制造一个。

```bash
# 跳板上确认
bash /home/cltx/scripts/chatgpt-acct-usage.sh $TEST_ACCT | jq '.rate_limit.primary_window'
# 期望：used_percent: 100, allowed: false
```

**触发**：

```bash
MODEL_ID=$(get_model_id chatgpt-$TEST_ACCT)
echo "Testing model_id=$MODEL_ID"
flush_cooldown                  # 清干净起步

trace_pinned "$MODEL_ID"        # 第一次：强制路由，预期 429 给 client
sleep 1
dump_cooldown | grep "$MODEL_ID"    # Redis 应已出现 key

# 后续随机路由不应再打到这个 acct
for i in 1 2 3 4 5; do trace_call; done | grep "x-litellm-model-id"
```

**断言**：

| 断言 | 命令 | 期望 |
|---|---|---|
| 第一次 429 | `trace_pinned` HTTP 行 | `HTTP/1.1 429` |
| Redis 出现 cooldown key | `dump_cooldown` | 至少 1 行含 $MODEL_ID |
| TTL ≈ Retry-After | `dump_cooldown` ttl 列 | 5h 撞顶 ≈ 该窗口剩秒数（500-18000） |
| 后续 5 次都绕开 | `trace_call` x5 model-id | 全 ≠ $MODEL_ID |
| 没走 wangsu fallback | `trace_call` attempted-fallbacks | 0 |

**清理**：`flush_cooldown`

#### TC-H2：Retry-After 优先于 cooldown_time

**触发**：用 TC-H1 同 acct（5h 撞顶），看 Retry-After header 与 TTL 关系

```bash
# 抓 raw response 看 Retry-After header
flush_cooldown
trace_pinned "$MODEL_ID" 2>&1 | grep -i "retry-after"     # 记 X 秒
sleep 1
dump_cooldown | grep "$MODEL_ID"                          # 记 TTL Y 秒
echo "Retry-After=X TTL=Y  应 |X-Y| < 60"
```

**断言**：`|TTL - Retry-After| < 60s`（PR #12037 生效）。如果 TTL = 3600 而 Retry-After ≠ 3600 → patch 没生效。

#### TC-H3：全集群 1 pod 触发 → 4 pod 全摘 ★P0

```bash
flush_cooldown
PODS=($(kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[*].metadata.name}'))
echo "${#PODS[@]} pods: ${PODS[*]}"

# pod0 触发
kubectl -n $NS exec ${PODS[0]} -- sh -c "curl -s http://localhost:4000/v1/responses \
  -H 'Authorization: Bearer $KEY' -H 'X-Litellm-Specific-Deployment: $MODEL_ID' \
  -d '{\"model\":\"chatgpt-gpt-5.5\",\"input\":\"x\"}'" > /dev/null
sleep 2

# pod1/2/3 不触发，直接随机路由
for i in 1 2 3; do
  for j in 1 2 3 4 5; do
    kubectl -n $NS exec ${PODS[$i]} -- sh -c "curl -sD- -o /dev/null http://localhost:4000/v1/responses \
      -H 'Authorization: Bearer $KEY' \
      -d '{\"model\":\"chatgpt-gpt-5.5\",\"input\":\"x\"}'" | grep -i "x-litellm-model-id"
  done
done | sort | uniq -c
```

**断言**：所有 15 行 model-id 都 ≠ $MODEL_ID。

---

### 10.2 异常流用例

> §10.2 共用：所有"升格 patch 验证"用例靠 mock server，不要去改 prod auth.json 或 scale 真 acct。
> mock 起在跳板的 `127.0.0.1:9999`，临时改 router 把一个 entry 的 api_base 指向 mock。

#### TC-E0：起 mock server（其他 E1/E2 共用前置）

```bash
# 跳板上跑一个 socat-based mock，按 request body 分类返不同假响应
cat > /tmp/mock-chatgpt.py <<'PY'
import http.server, json, sys
PORT = 9999
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length', 0))
        body = self.rfile.read(n).decode()
        mode = sys.argv[1] if len(sys.argv) > 1 else 'swallow'
        if mode == 'swallow':         # E1 issue #25429
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({"id":"resp_x","object":"response","status":"completed","output":[],"usage":{}}).encode())
        elif mode == 'unauthorized':  # E2 token 失效
            self.send_response(401); self.end_headers()
            self.wfile.write(b'{"error":{"code":"unauthorized"}}')
        elif mode == 'context_cap':   # E5
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({"id":"r","object":"response","status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output":[],"usage":{}}).encode())
        elif mode == 'fivehundred':   # E3
            self.send_response(502); self.end_headers()
        elif mode == 'forward_compat':# B5
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({"error":{"code":"new_quota_error","type":"capacity"}}).encode())
    def log_message(self, *a): pass
http.server.HTTPServer(('0.0.0.0', PORT), H).serve_forever()
PY

# 后台跑（每个 TC 切 mode）
python3 /tmp/mock-chatgpt.py swallow > /tmp/mock.log 2>&1 &
MOCK_PID=$!
echo "mock at 188:9999 pid=$MOCK_PID"

# 临时往 router 加一个 mock entry（用 admin /model/new）
curl -s -X POST http://$PROXY_SVC/model/new \
  -H "Authorization: Bearer <admin-key>" \
  -d '{
    "model_name":"chatgpt-gpt-5.5",
    "litellm_params":{
      "model":"chatgpt/gpt-5.5",
      "api_base":"http://10.68.13.188:9999/v1",
      "api_key":"mock",
      "custom_llm_provider":"chatgpt"
    },
    "model_info":{"id":"chatgpt-acct-MOCK-gpt-5.5"}
  }'
export MOCK_MID="chatgpt-acct-MOCK-gpt-5.5"
```

**清理（每个 E 用例跑完都做）**：

```bash
curl -s -X POST http://$PROXY_SVC/model/delete \
  -H "Authorization: Bearer <admin-key>" \
  -d "{\"id\":\"$MOCK_MID\"}"
kill $MOCK_PID
flush_cooldown
```

#### TC-E1：200 + 空 output (#25429) — swallow 升格 ★

**前置**：§6.5 patch 已部署 + TC-E0 mock mode=swallow

**触发**：

```bash
flush_cooldown
trace_pinned "$MOCK_MID"
sleep 1
dump_cooldown | grep "$MOCK_MID"
```

**断言**：
- HTTP 状态行 = `429`（不是 200）
- Redis 有 $MOCK_MID 的 cooldown key
- 没 patch 时此 TC fail，patch 后 pass

#### TC-E2：401 token 失效 — §9.2 patch ★

**前置**：§9.2 patch 部署 + TC-E0 mock 改 mode=unauthorized

```bash
kill $MOCK_PID
python3 /tmp/mock-chatgpt.py unauthorized > /tmp/mock.log 2>&1 & MOCK_PID=$!
```

**触发**：`trace_pinned "$MOCK_MID"`

**断言**：
- HTTP = 429（升格后），不是 401
- Redis 有 cooldown key TTL ≈ 3600（无 Retry-After，走默认）

#### TC-E3：5xx 误伤防护 — allowed_fails_policy

**前置**：§9.3 配置生效 + TC-E0 mock mode=fivehundred

```bash
kill $MOCK_PID
python3 /tmp/mock-chatgpt.py fivehundred > /tmp/mock.log 2>&1 & MOCK_PID=$!
```

**触发**：连发 5 次 pinned

```bash
flush_cooldown
for i in 1 2 3 4 5; do
  echo "=== request $i ==="
  trace_pinned "$MOCK_MID"
  dump_cooldown | grep "$MOCK_MID" || echo "still hot"
done
```

**断言**：
- 第 1-3 次：HTTP 5xx，**Redis 无 cooldown key**（容忍内）
- 第 4 次：仍 5xx，**Redis 出现 cooldown key**（撞过 `InternalServerErrorAllowedFails=3` 阈值）
- 如果第 1 次就冷 → `allowed_fails_policy` 没读到，回 §9.3 排查

#### TC-E4：APIConnectionError 不冷却（issue #24366）

**前置**：临时停 mock server 但保留 router entry（让连接直接 refused）

```bash
kill $MOCK_PID
# router 的 mock entry 还在，但 9999 端口关了
```

**触发**：`trace_pinned "$MOCK_MID"` ×3

**断言**：
- HTTP = 500 / connection refused
- **Redis 没出现 cooldown key**（验证 issue #24366）
- 说明这种错误形态必须靠 cron `/model/delete` 收尾，不能依赖 LiteLLM

→ 这个 TC 的"fail"反而是"pass"（验证已知缺陷仍存在）

#### TC-E5：context cap 不冷却 — §9.5 patch

**前置**：§9.5 patch 部署 + TC-E0 mock mode=context_cap

```bash
kill $MOCK_PID
python3 /tmp/mock-chatgpt.py context_cap > /tmp/mock.log 2>&1 & MOCK_PID=$!
```

**触发**：`trace_pinned "$MOCK_MID"`

**断言**：
- HTTP ≠ 429（应是 400/422 `ContextWindowExceededError`）
- Redis **无** cooldown key
- 该 deployment 仍可服务下一个正常请求

#### TC-E6：Redis 失联 — 退化为本地 cooldown

**前置**：先 TC-H1 让 Redis 有 cooldown key

**触发**：

```bash
kubectl -n $NS delete pod $REDIS_POD --grace-period=0 --force
# 立刻发请求，此时 redis 还没起新 pod
trace_pinned "$MODEL_ID"
sleep 5
trace_pinned "$MODEL_ID"
sleep 5
# 等 redis 起来
kubectl -n $NS wait pod -l app=litellm-redis --for=condition=ready --timeout=60s
```

**断言**：
- proxy logs 有 redis 报错但不 crash：
  `kubectl -n $NS logs $PROXY_POD --tail=200 | grep -iE 'redis.*(timeout|refus|connect)'`
- redis down 期间每个 pod 各自撞 1 次冷（本地内存）
- redis 恢复后 cooldown 从 0 开始重建（不会自动同步 down 期间状态）

#### TC-E7：proxy 重启 — cooldown 从 Redis 恢复 ★

**前置**：先 TC-H1 让 Redis 有 cooldown key

```bash
TTL_BEFORE=$(kubectl -n $NS exec $REDIS_POD -- redis-cli TTL "cooldown_models:$MODEL_ID")
kubectl -n $NS rollout restart deploy/litellm-proxy
kubectl -n $NS rollout status deploy/litellm-proxy --timeout=120s

# 拿新 pod
export PROXY_POD=$(kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')

# 立刻 trace
TTL_AFTER=$(kubectl -n $NS exec $REDIS_POD -- redis-cli TTL "cooldown_models:$MODEL_ID")
for i in 1 2 3 4 5; do trace_call; done | grep "model-id" | sort -u
```

**断言**：
- TTL_AFTER < TTL_BEFORE 且 |TTL_BEFORE - TTL_AFTER| ≈ 重启耗时（30-90s）— 不被 reset
- 5 次随机路由全绕开 $MODEL_ID

#### TC-E8：cron resume 撞 cooldown — new model_id 绕过

**前置**：先 TC-H1 让 $TEST_ACCT 在 cooldown

```bash
OLD_MID=$MODEL_ID

# 手动跑 quota-rebalance 的 resume_acct 流程
bash /home/cltx/scripts/cc-max-quota-rebalance.py --action=resume --acct=$TEST_ACCT --apply
# ⚠️ TODO: 实际脚本路径以 [[chatgpt-quota-rebalance]] SKILL.md 为准，cc-max 是 Anthropic 线，
#         ChatGPT 应是 /home/cltx/.chatgpt-quota/quota-rebalance.py 类似名字
#         上线人按当前 prod 真实脚本替换这一行

sleep 5
NEW_MID=$(get_model_id chatgpt-$TEST_ACCT)
echo "OLD=$OLD_MID NEW=$NEW_MID"

# 强制路由到新 model_id 应该 200
trace_pinned "$NEW_MID"
```

**断言**：
- NEW_MID ≠ OLD_MID
- 新 deployment 立刻可用，HTTP 200
- 旧 OLD_MID 的 cooldown key 还在但 router 已无对应 entry（自然 TTL 失效）

#### TC-E9：pool 全冷 — fallback chain

**触发**：手动给 router 里所有 chatgpt-gpt-5.5 deployment 写 cooldown key

```bash
# 拿全部 model_id
ALL_MIDS=$(kubectl -n $NS exec $PROXY_POD -- sh -c "curl -s http://localhost:4000/v1/model/info \
  -H 'Authorization: Bearer $KEY'" | \
  jq -r '.data[] | select(.model_name=="chatgpt-gpt-5.5") | .model_info.id')

for mid in $ALL_MIDS; do
  kubectl -n $NS exec $REDIS_POD -- redis-cli SET "cooldown_models:$mid" 1 EX 3600
done

trace_call    # 应走 fallback
```

**断言**：
- `x-litellm-attempted-fallbacks` ≥ 1
- `x-litellm-model-id` 落在 `wangsu-gpt-5.5` 或 `openrouter-gpt-5.5` deployment

**清理**：`flush_cooldown`

#### TC-E10：cooldown 切 acct 后老会话能否继续 ★关键

> **背景**：chatgpt provider `/v1/responses` 用 `previous_response_id` + `encrypted_content` 维持会话上下文。
> 这俩字段是 **per-acct 加密**：acct-7 颁发的 `encrypted_content` 只能 acct-7 解密。
> 当 acct-7 被 cooldown 摘除 → router 把后续请求路由到 acct-12 → acct-12 解 acct-7 的密文会失败。
>
> 已知历史问题：
> - [[feedback_litellm_degrade_strip_fallback_gap]] — affinity check 在 fallback model_group 二跑漏 strip → wangsu 也 503
> - [[project_litellm_198_cooldown_tune]] — cooldown 300→60s 是为了缓解 stateful Codex 钉死
> - [[litellm-chatgpt-compaction-drop]] — `_normalize_item` 对 compaction 只 pop `encrypted_content` 不删整项 → 400 missing param

##### TC-E10a：单 turn 切 acct（degrade strip 应触发）

**场景**：用户发了 turn-1（落 acct-7 拿到 `previous_response_id=resp_A` + `encrypted_content=enc_A`）→ acct-7 撞顶冷掉 → 用户发 turn-2（带着 resp_A + enc_A）

**触发**：

```bash
flush_cooldown

# turn-1: 让流量自然路由到一个 acct（记下 model-id 和 response_id）
RESP1=$(kubectl -n $NS exec $PROXY_POD -- sh -c "curl -s http://localhost:4000/v1/responses \
  -H 'Authorization: Bearer $KEY' \
  -d '{\"model\":\"chatgpt-gpt-5.5\",\"input\":[{\"role\":\"user\",\"content\":\"记住 magic=42\"}],\"store\":true}'")
RESP_ID=$(echo "$RESP1" | jq -r '.id')
LANDED_MID=$(echo "$RESP1" | jq -r '._litellm_model_id // empty')   # 部分版本字段
# 兜底：再 trace 一次看 header
[ -z "$LANDED_MID" ] && LANDED_MID=$(trace_call | awk '/x-litellm-model-id/{print $2}')
echo "turn-1 landed on $LANDED_MID, response_id=$RESP_ID"

# 拿 turn-1 返回里的 encrypted_content（如果上游返了）
ENC=$(echo "$RESP1" | jq -r '.output[]?.encrypted_content // empty' | head -1)

# 把 $LANDED_MID 强制 cooldown，模拟撞顶
kubectl -n $NS exec $REDIS_POD -- redis-cli SET "cooldown_models:$LANDED_MID" 1 EX 3600

# turn-2: 同会话续接（带 previous_response_id + encrypted_content）
TURN2_PAYLOAD=$(jq -nc --arg pid "$RESP_ID" --arg enc "$ENC" '{
  model: "chatgpt-gpt-5.5",
  previous_response_id: $pid,
  input: [{role:"user", content:"magic 是多少？"}],
  store: true
}')

kubectl -n $NS exec $PROXY_POD -- sh -c "curl -sD- http://localhost:4000/v1/responses \
  -H 'Authorization: Bearer $KEY' \
  -d '$TURN2_PAYLOAD'"
```

**断言**（按 §9.3 / [[feedback_litellm_degrade_strip_fallback_gap]] callback 已 patch 的预期）：

| 断言 | 期望 | 失败说明 |
|---|---|---|
| turn-2 HTTP 状态 | **200** | 200=callback 已 strip previous_response_id 重路由<br>400=encrypted_content 未 strip 漏到 acct-12<br>503=affinity check 二跑 fallback 也 raise |
| turn-2 落 acct | model-id ≠ $LANDED_MID | 确认 cooldown 生效 |
| 回答语义 | 上下文丢失（"magic=42" 记忆没了） | **预期行为** — 跨 acct 必丢上下文，degrade strip 是用"丢上下文"换"能返 200" |
| proxy logs 有 strip 记录 | `grep "_degrade_stripped\|stripping.*previous_response_id" $PROXY_POD logs` | 至少一条 |

> ⚠️ 关键：用户级体验是"**会话能继续但失忆**"，不是"会话彻底报错"。如果断言挂在 HTTP 400/503，回 §9.3 + [[feedback_litellm_degrade_strip_fallback_gap]] 排查 callback 双跑 flag。

**清理**：`flush_cooldown`

##### TC-E10b：fallback 链上 affinity 二跑（GA 阶段补）

**场景**：pool 全冷 → fallback 到 wangsu，wangsu 不认 chatgpt 的 encrypted_content

**触发**：

```bash
# 同 TC-E9 让全 pool 冷
for mid in $ALL_MIDS; do
  kubectl -n $NS exec $REDIS_POD -- redis-cli SET "cooldown_models:$mid" 1 EX 3600
done

# turn-2 带 chatgpt 颁发的 enc 打过去
kubectl -n $NS exec $PROXY_POD -- sh -c "curl -sD- http://localhost:4000/v1/responses \
  -H 'Authorization: Bearer $KEY' \
  -d '$TURN2_PAYLOAD'"
```

**断言**：
- HTTP 200（degrade_strip 在 wangsu 这条 fallback 上也跑通）
- model-id = `wangsu-gpt-5.5`
- 不应出现 500/503（如出现 → [[feedback_litellm_degrade_strip_fallback_gap]] 描述的二跑 gap 复发）

##### TC-E10c：cooldown 后期 acct 复活，老 response_id 是否还能用

**场景**：acct-7 cooldown 3600s 到期回池 → 之前钉在 acct-7 的会话 turn-N 能不能继续？

**触发**：

```bash
# 先走 TC-E10a 让 turn-1 落 acct-7（$LANDED_MID）
# 让 cooldown 自然过期（或手动）
kubectl -n $NS exec $REDIS_POD -- redis-cli EXPIRE "cooldown_models:$LANDED_MID" 5
sleep 7

# 强制路由回 acct-7 续接老会话
TURN_LATE=$(jq -nc --arg pid "$RESP_ID" '{
  model: "chatgpt-gpt-5.5",
  previous_response_id: $pid,
  input: [{role:"user", content:"还记得 magic 吗？"}]
}')
kubectl -n $NS exec $PROXY_POD -- sh -c "curl -sD- http://localhost:4000/v1/responses \
  -H 'Authorization: Bearer $KEY' \
  -H 'X-Litellm-Specific-Deployment: $LANDED_MID' \
  -d '$TURN_LATE'"
```

**断言**：
- HTTP 200
- 上下文**保留**（回答里有"42"或类似）
- 验证：上游 ChatGPT 的 response store 不因 cooldown 时长清掉 — 24h extended retention 内 response_id 仍可解（[[feedback_openai_cache_org_scoped_not_account_id]]）

> 如果失败：说明 OpenAI 后端清掉了过期 response store，那 cooldown_time:3600 对长会话用户不友好，要考虑改回 cooldown_time:60（[[project_litellm_198_cooldown_tune]]）

##### TC-E10d：compaction drop 在切 acct 时的形态

**场景**：turn-N 触发 compaction → request body 里包含 compaction item → 切 acct 路由到新 deployment

**触发**：构造一个带 compaction item 的 request

```bash
# compaction item 形态：{"type":"compaction", "encrypted_content":"...", ...}
TURN_COMPACT=$(jq -nc --arg enc "$ENC" '{
  model: "chatgpt-gpt-5.5",
  input: [
    {role:"user", content:"hi"},
    {type:"compaction", encrypted_content:$enc, summary:"earlier conversation"},
    {role:"user", content:"继续"}
  ]
}')

# 强制路由到不同 acct
kubectl -n $NS exec $PROXY_POD -- sh -c "curl -sD- http://localhost:4000/v1/responses \
  -H 'Authorization: Bearer $KEY' \
  -H 'X-Litellm-Specific-Deployment: <某个其他 acct 的 mid>' \
  -d '$TURN_COMPACT'"
```

**断言**：
- HTTP 200
- proxy logs 有 `compaction.*drop` 类记录（[[litellm-chatgpt-compaction-drop]] patch 应已生效）
- 没有 400 `missing required parameter` — 如出现，说明 `_normalize_item` 还在只 pop `encrypted_content` 字段不删整项，需重跑 SKILL §"3 行修复"

---

### 10.3 边界用例

#### TC-B1：Retry-After 解析失败 — fallback 到 cooldown_time

**前置**：mock 改 mode，让 swallow 形态触发同时上游返 `Retry-After: not-a-number`

```bash
# 在 mock-chatgpt.py 里 swallow 分支加 self.send_header('Retry-After','garbage')
```

**断言**：cooldown TTL = router.cooldown_time（3600），不是 0 / 不是无限。

#### TC-B2：高并发 4 pod 同时撞同一 acct

```bash
flush_cooldown
for pod in "${PODS[@]}"; do
  kubectl -n $NS exec $pod -- sh -c "for i in \$(seq 1 50); do \
    curl -s http://localhost:4000/v1/responses \
      -H 'Authorization: Bearer $KEY' \
      -H 'X-Litellm-Specific-Deployment: $MODEL_ID' \
      -d '{\"model\":\"chatgpt-gpt-5.5\",\"input\":\"x\"}' > /dev/null & \
  done; wait" &
done
wait

dump_cooldown
```

**断言**：
- Redis 只有 1 个 cooldown key 对应 $MODEL_ID（4 pod 共享）
- 总浪费请求 ≤ 4（每 pod 撞 1 次后即冷）— 看 proxy logs `grep "Cooldown set"` 计数

#### TC-B3：cooldown 中 deployment 被 cron 删除

**前置**：先 TC-H1

```bash
OLD_MID=$MODEL_ID
# 模拟 cron pause
bash /home/cltx/scripts/quota-rebalance.py --action=pause --acct=$TEST_ACCT --apply  # 路径同 E8 TODO
sleep 5

# router 应已无此 deployment
kubectl -n $NS exec $PROXY_POD -- sh -c "curl -s http://localhost:4000/v1/model/info \
  -H 'Authorization: Bearer $KEY'" | jq -r '.data[].model_info.id' | grep "$OLD_MID" || echo "removed from router OK"

# Redis cooldown key 还在
dump_cooldown | grep "$OLD_MID"
```

**断言**：
- router /model/info 不再列出 OLD_MID
- Redis 残留 cooldown key（TTL 内自然失效，不影响新请求）

#### TC-B4：rollout 中途撞顶（GA 阶段才跑）

**触发**：起 rollout，同时持续打撞顶请求

```bash
# 后台持续打
(for i in $(seq 1 200); do trace_pinned "$MODEL_ID"; sleep 0.5; done) > /tmp/rollout-trace.log 2>&1 &
TRACE_PID=$!

kubectl -n $NS rollout restart deploy/litellm-proxy
kubectl -n $NS rollout status deploy/litellm-proxy --timeout=180s

wait $TRACE_PID
grep -c "HTTP/1.1 429" /tmp/rollout-trace.log
grep -c "x-litellm-model-id: $MODEL_ID" /tmp/rollout-trace.log
```

**断言**：rollout 期间 429 出现 ≤ pod 数 × 1（新 pod 启动会各自从 Redis 读到 cooldown，不应再撞）

#### TC-B5：capacity-patch 未覆盖的新形态（known gap）

**前置**：TC-E0 mock mode=forward_compat

**断言**：LiteLLM 当成功（HTTP 200，TPM/RPM++），Redis 无 cooldown key — **承认盲区**，cron 5min 内通过 `/codex/usage` 兜底。

---

### 10.4 性能 / 长稳

#### TC-P1：cooldown 状态写入延迟

```bash
flush_cooldown
START=$(date +%s%N)
trace_pinned "$MODEL_ID" > /dev/null
END=$(date +%s%N)
# 立刻读 Redis
kubectl -n $NS exec $REDIS_POD -- redis-cli EXISTS "cooldown_models:$MODEL_ID"
echo "request+cooldown 写入耗时: $(( (END-START)/1000000 )) ms"
```

**断言**：< 200ms（含请求 RTT，cooldown 写本身应 < 10ms）

#### TC-P2：24h 长稳（promote 后跑）

```bash
# 上线前抓基线
PRE_WANGSU=$(kubectl -n $NS exec $PROXY_POD -- sh -c "curl -s http://localhost:4000/metrics" | \
  grep -E "litellm_deployment.*wangsu-gpt-5.5" | awk '{print $NF}')

# 24h 后再抓对比
sleep 86400
POST_WANGSU=$(kubectl -n $NS exec $PROXY_POD -- sh -c "curl -s http://localhost:4000/metrics" | \
  grep -E "litellm_deployment.*wangsu-gpt-5.5" | awk '{print $NF}')

echo "wangsu fallback: $PRE_WANGSU → $POST_WANGSU"
# cron 日志看 cooldown_pre_action_count
tail -n 1000 /home/cltx/.chatgpt-quota/cron.log | grep -E "would_pause|already_offline" | wc -l
```

**断言**：
- 24h 内 wangsu fallback delta ≤ 上线前同时长基线 1/10
- cron 5min 跑时大部分撞顶 acct 已在 cooldown 表里（避免 cron 又删一次）
- Redis 内存增量 < 10MB

#### TC-P3：cooldown TTL 自然过期

```bash
flush_cooldown
trace_pinned "$MODEL_ID"
kubectl -n $NS exec $REDIS_POD -- redis-cli EXPIRE "cooldown_models:$MODEL_ID" 10
sleep 12
# 应该可以路由回这个 acct
trace_pinned "$MODEL_ID"
```

**断言**：12s 后请求 HTTP 200（如果 acct 真已 reset）或重新 429（如果还撞顶 → 重新 cooldown）。两种结果都说明 TTL 机制正常。

---

### 10.5 用例执行总表

| ID | 名称 | 类别 | 优先级 | 预期耗时 | fail 是否阻塞 promote |
|---|---|---|---|---|---|
| TC-H1 | 标准 429 1 次失败即冷 | Happy | P0 | 2min | ★阻塞 |
| TC-H2 | Retry-After 优先 | Happy | P1 | 1min | 阻塞 |
| TC-H3 | 全集群 1→4 一致 | Happy | P0 | 3min | ★阻塞 |
| TC-E1 | swallow 升格 (mock) | 异常 | P1 | 5min | 不阻塞（patch 漏给 cron 兜） |
| TC-E2 | 401 升格 (mock) | 异常 | P2 | 5min | 不阻塞 |
| TC-E3 | 5xx 误伤防护 | 异常 | P1 | 3min | 阻塞 |
| TC-E4 | ConnectionError 不冷却 | 异常 | P3 | 2min | 不阻塞（验证已知缺陷） |
| TC-E5 | context cap 不冷却 | 异常 | P1 | 2min | 阻塞（防误冷正常用户） |
| TC-E6 | Redis 失联 | 异常 | P1 | 5min | 阻塞 |
| TC-E7 | proxy 重启恢复 | 异常 | P0 | 5min | ★阻塞 |
| TC-E8 | cron resume 撞 cooldown | 异常 | P1 | 3min | 阻塞 |
| TC-E9 | pool 全冷 fallback | 异常 | P1 | 3min | 阻塞 |
| TC-E10a | 切 acct 老会话单 turn | 异常 | **P0** | 5min | **★阻塞** |
| TC-E10b | fallback 链 affinity 二跑 | 异常 | P1 | 5min | 阻塞 |
| TC-E10c | cooldown 复活后续接老会话 | 异常 | P1 | 10min | 阻塞 |
| TC-E10d | compaction drop 跨 acct | 异常 | P1 | 3min | 阻塞 |
| TC-B1 | Retry-After 解析失败 | 边界 | P2 | 2min | 不阻塞 |
| TC-B2 | 高并发同 acct | 边界 | P1 | 5min | 阻塞 |
| TC-B3 | cooldown 中被删 | 边界 | P2 | 2min | 不阻塞 |
| TC-B4 | rollout 中撞顶 | 边界 | P2 | 10min | GA 阶段才跑 |
| TC-B5 | forward compat | 边界 | P3 | 5min | 不阻塞 |
| TC-P1 | cooldown 写延迟 | 性能 | P1 | 1min | 阻塞 |
| TC-P2 | 24h 长稳 | 性能 | P0 | 24h | ★GA gate |
| TC-P3 | TTL 自然过期 | 性能 | P2 | 2min | 不阻塞 |

**Canary gate**（约 75min）：H1 + H3 + E7 + **E10a** 全过 + H2 + E3 + E5 + E6 + E8 + E9 + E10b/c/d + B2 + P1 全过

**GA gate**（promote 后）：P2 24h + B4 实跑 rollout

---

### 10.6 附录：手工制造一个撞顶 acct 用于测试

如果 canary 环境没自然撞顶的 acct，临时把一个 acct 的 quota state 改成 100%：

```bash
# 改 188 host 上 /tmp/auth-acct-N.json 旁边的 state 文件
# 或直接用 mock 端点替代（不推荐，state 文件路径更稳）
# 详见 [[chatgpt-quota-rebalance]] SKILL §"测试用造数"
```

或最稳：拿一把 5.3-Codex-Spark 子配额已撞顶的 acct（不影响主配额），用 `X-Litellm-Specific-Deployment` 指到那个子 model name。

---

## 11. 关键依据（源码 / issue）

| 依据 | 链接 |
|---|---|
| router cooldown 只看异常 | `litellm/router.py` `_set_cooldown_deployments` |
| Retry-After 优先级 | [PR #12037](https://github.com/BerriAI/litellm/pull/12037) |
| swallow bug | [Issue #25429](https://github.com/BerriAI/litellm/issues/25429) |
| 默认参数 | `litellm/constants.py` `DEFAULT_ALLOWED_FAILS=3 / DEFAULT_COOLDOWN_TIME_SECONDS=5` |
| 不走 callback 的原因 | [Issue #8842](https://github.com/BerriAI/litellm/issues/8842) async hook firing 不可靠 |
| Redis 共享 cooldown | `litellm/router_utils/cooldown_cache.py` 用 `redis_cache` |
| 设计参考 sub2api | [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) |

---

## 12. TL;DR

```yaml
router_settings:
  allowed_fails: 1
  cooldown_time: 3600
  redis_host: litellm-redis...
```

+ 校验 capacity-patch 抛 `litellm.RateLimitError`
+ 补 swallow bug 升格 patch
+ rollout restart

完事。cron 不动。
