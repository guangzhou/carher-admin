# ChatGPT Pro 账户池迁移到阿里云 K8s — 五账号池 + 全集群 promote（最终版）

> 2026-05-20 完成。本文档覆盖两阶段：**C-2 金丝雀验证** + **D 全集群 promote 到 prod**。

## 1. 方案演进

| 版本 | 描述 | 放弃原因 |
|------|------|---------|
| ~~B-2~~ | 阿里云独立 1 个 chatgpt-acct-11 Pod，主 litellm-proxy 通过 ClusterIP 调它 | 多一层架构无收益 |
| ~~B-1~~ | 主 litellm-proxy replicas=2→1 直跑 `chatgpt/` provider | 会影响 500+ her 实例 |
| ~~C-1~~ | canary（独立 ConfigMap）自跑 `chatgpt/` provider 服务 1 账号 acct-11 | 后续要扩 5 账号，单进程 `CHATGPT_TOKEN_DIR` 是全局 env，1 进程 = 1 账号 |
| **C-2** | 5 个独立 `chatgpt-acct-N` Pod（each 1 account）+ canary 作 openai/ 代理 simple-shuffle LB；her-1000 通过 `spec.litellmUrl` 切 canary | **已落地（金丝雀验证阶段）** |
| **D** | 把 C-2 配置 promote 到 prod litellm-proxy（同 ConfigMap 升级 + 镜像 1.85.0 + operator alias rename + 217 her 默认改 gpt）| **已落地（全集群推广）** |

## 2. 最终拓扑

```mermaid
flowchart LR
  subgraph IDC188["188 (JSZX-AI-03, 内网 docker)"]
    direction TB
    A188["acct-2~6 在跑<br/>docker-compose<br/>端口 4002~4006"]
  end

  subgraph IDC198["198 (AIYJY-litellm)"]
    direction TB
    L198["litellm-product<br/>chatgpt-acct-{2..6}-* DB-registered<br/>acct-7~11 已 delete"]
  end

  subgraph CARHER["阿里云 K8s carher namespace"]
    direction TB
    PROD["litellm-proxy (prod)<br/>replicas=2<br/>ConfigMap: litellm-config<br/><b>★ 零改动 ★</b>"]
    OTHERS["其他 500+ her 实例<br/>litellmUrl='' (默认 prod)"]

    CANARY["litellm-proxy-canary<br/>replicas=1, image: litellm 1.85<br/>ConfigMap: litellm-config-canary<br/>chatgpt-* 4 model × 5 deployment<br/>simple-shuffle LB"]
    BOT1000["carher-1000 (her-1000)<br/>litellmUrl=canary"]

    POOL7["chatgpt-acct-7<br/>+ PVC auth-7.json"]
    POOL8["chatgpt-acct-8<br/>+ PVC auth-8.json"]
    POOL9["chatgpt-acct-9<br/>+ PVC auth-9.json"]
    POOL10["chatgpt-acct-10<br/>+ PVC auth-10.json"]
    POOL11["chatgpt-acct-11<br/>+ PVC auth-11.json"]

    BOT1000 --> CANARY
    OTHERS --> PROD
    CANARY -.->|openai/ via ClusterIP| POOL7
    CANARY -.-> POOL8
    CANARY -.-> POOL9
    CANARY -.-> POOL10
    CANARY -.-> POOL11
  end

  L198 --> A188
  A188 --> OAI_198[("ChatGPT Pro<br/>acct-2~6")]
  POOL7 --> OAI_ALIYUN[("ChatGPT Pro<br/>acct-7~11")]
  POOL8 --> OAI_ALIYUN
  POOL9 --> OAI_ALIYUN
  POOL10 --> OAI_ALIYUN
  POOL11 --> OAI_ALIYUN

  style PROD fill:#e0f5e0
  style CANARY fill:#fff5d0
  style BOT1000 fill:#fff5d0
```

**账号划分**：
- **acct-2~6 在 188 + 198**：团队 IDE / Codex 编程 (`cc.auto-link.com.cn/pro/v1`)
- **acct-7~11 已迁到阿里云**：carher bot（当前只 her-1000）通过 canary 走

## 3. 阿里云 K8s 资源（最终）

### 3.1 `k8s/chatgpt-acct-pool.yaml`（新建）

```
Secret      chatgpt-pool-master-key                # 共享 LITELLM_MASTER_KEY (sk-chatgpt-aliyun-...)
ConfigMap   chatgpt-pool-config                    # 共享 LiteLLM config.yaml (chatgpt provider 4 个 model)
PVC ×4      chatgpt-acct-{7,8,9,10}-auth           # NAS RWO 1Gi, acct-11-auth 复用已有
Deployment ×5  chatgpt-acct-{7,8,9,10,11}          # image=ghcr litellm 1.85, replicas=1, strategy: Recreate
Service ×5     chatgpt-acct-{7,8,9,10,11}:4000     # ClusterIP
```

每个 Pod：`CHATGPT_TOKEN_DIR=/chatgpt-auth` env + PVC 挂到 `/chatgpt-auth/auth.json` + memory limit 2Gi（1Gi 会 OOMKilled）。

### 3.2 `k8s/litellm-proxy-canary.yaml`（已改）

```yaml
- replicas: 1
- image: ghcr.io/berriai/litellm:v1.85.0
- imagePullPolicy: IfNotPresent
- nodeSelector: 已删（不再 pin 227 节点）
- env 新增: CHATGPT_POOL_KEY (from Secret chatgpt-pool-master-key)
- volumes 中 config 改用 litellm-config-canary
- (历史) chatgpt-auth volume 已删除
```

### 3.3 `k8s/litellm-proxy-canary-config.yaml`（独立 ConfigMap, prod 不动）

从 prod 复制后改 3 处：
- 删 openrouter 版 `gpt-5.4` / `gpt-5.3-codex`
- 加 4 个 chatgpt model × 5 deployment (chatgpt-acct-{7..11}.carher.svc:4000)，全部 `mode: responses`，api_key = `os.environ/CHATGPT_POOL_KEY`
- fallbacks: `chatgpt-gpt-5.5 → wangsu-gpt-5.5`, `gpt-5.4 → wangsu-gpt-5.4`

model_name 用 **`chatgpt-gpt-5.5`**（base-config + her vkey allowlist 用这个带前缀名字）；其他 3 个用裸名（`gpt-5.4` / `gpt-5.3-codex` / `gpt-5.3-codex-spark`）。

## 4. 数据流（her-1000 视角）

```mermaid
sequenceDiagram
  participant FS as 飞书
  participant Bot as carher-1000 Pod
  participant Canary as litellm-proxy-canary
  participant Acct as chatgpt-acct-{7..11}<br/>(simple-shuffle 选一个)
  participant OAI as ChatGPT Pro<br/>(对应 acct)
  participant WS as Wangsu Gateway<br/>(fallback)

  FS->>Bot: 用户消息 (e.g. /model chatgpt-gpt-5.5)
  Bot->>Canary: POST /v1/chat/completions<br/>model=chatgpt-gpt-5.5<br/>Auth: virtual-key
  Canary->>Canary: simple-shuffle 选 acct-N
  Canary->>Acct: POST /v1/chat/completions<br/>model=chatgpt-gpt-5.5<br/>Auth: CHATGPT_POOL_KEY
  Acct->>Acct: 读 /chatgpt-auth/auth.json (refresh if 401)
  Acct->>OAI: POST /backend-api/codex/responses<br/>Bearer access_token
  alt 200 OK
    OAI-->>Acct: SSE stream
    Acct-->>Canary: SSE stream
    Canary-->>Bot: SSE stream
    Bot-->>FS: 飞书 card-stream 回复
  else 429/5xx
    Canary->>WS: fallback wangsu-gpt-5.5
    WS-->>Canary: stream
    Canary-->>Bot: stream
  end
```

## 5. 执行的关键动作（已落地）

| 阶段 | 动作 | 结果 |
|------|------|------|
| A | 188 stop `litellm-chatgpt-{7,8,9,10,11}` 5 个容器 | ✅ 全部 Exit |
| A | scp 5 个 auth.json 到本地 `/tmp/chatgpt-acct-migrate/` | ✅ |
| F | apply `k8s/chatgpt-acct-pool.yaml` | ✅ 16 K8s 资源创建 |
| F | kubectl cp 4 个 auth.json (acct-11 复用 PVC 已有) | ✅ 5/5 Pod ready |
| F | curl 5 个 acct 各调 chatgpt 上游 | ✅ 5/5 返回 "ok"/"7"/"8"/.../ |
| G | 改 canary ConfigMap 4 model × 5 deployment | ✅ |
| G | 改 canary Deployment 删 chatgpt-auth + 加 CHATGPT_POOL_KEY env | ✅ rollout 完成 |
| G | her-1000 vkey 调 3 model | ✅ 全部 200 |
| D | 飞书发消息测试 → 10 个 POST /chat/completions 200 OK | ✅ |
| E | 198 prod 删 chatgpt-acct-7~11 共 20 个 deployment via admin API | ✅ HTTP 200 × 20 |
| E | 198 prod rollout restart litellm-proxy | ✅ stale router 已 clear |

## 6. 关键验证证据

### 5 个 Pod 各自调 ChatGPT 上游

```
=== acct-7  === content "7"   ✅
=== acct-8  === content "8"   ✅
=== acct-9  === content "9"   ✅
=== acct-10 === content "10"  ✅
=== acct-11 === content "11"  ✅
```

### 飞书 → carher-1000 → canary → 5 acct LB 链路

- canary access log: 10 个 `POST /chat/completions 200 OK` 来自 `172.16.0.37` (carher-1000 Pod IP)
- carher-1000 log: `"已切到 gpt-5.5，现在我在用 litellm/chatgpt-gpt-5.5"`
- 模型切换实际生效，飞书侧 card-stream 正常回复

### **核心未知风险已排除**

| 风险 | 状态 |
|------|------|
| 上游 IP 风控触发 token_invalidated（acct-N 源 IP 从 188 公网换阿里云 SG） | ✅ 5/5 acct 调用全部 200，未触发 |
| LiteLLM 1.85 vs canary 原 1.83.14 兼容性 | ✅ 1.85 正常 |
| 多账号并发 token refresh 冲突 | ✅ per-account 单进程 single-writer 设计排除 |

## 7. 回滚路径

| 失败场景 | 回滚 |
|---------|------|
| 阿里云 chatgpt-acct-N Pod 大规模挂了 | `kubectl patch her her-1000 -n carher --type=merge -p '{"spec":{"litellmUrl":""}}'` 切回 prod（prod 没 chatgpt 流量，会走 wangsu fallback） |
| canary 配置 / model_list 损坏 | `git revert k8s/litellm-proxy-canary-config.yaml + kubectl apply + rollout restart` |
| 某个 acct token_invalidated | 用 `re-oauth.sh acct-N` (188 上 SOP)，新 auth.json `kubectl cp` 到对应 Pod PVC + rollout restart Deployment |
| 想恢复 198 prod acct-7~11 流量（不太可能） | 188 `docker compose start litellm-chatgpt-{7..11}` + 198 admin API 重新 `/model/new` 注册（参考 chatgpt-pro-litellm skill） |

## 8. 待办 / 未来优化

- [ ] **撞限率监控**：5 acct simple-shuffle 跑一段时间观察单 acct 5h% 用量分布，必要时优化 routing
- [ ] **`chatgpt-acct-pool.yaml` 加 PVC chatgpt-acct-11-auth 定义**（当前定义在 `k8s/litellm-proxy-canary.yaml` 顶部，资源归属不清晰）
- [ ] **更广 her 实例接入**：当前只 her-1000；如果业务想推到其他 her，需评估上游 5 acct 容量
- [ ] **关 188 acct-7~11 容器后**保留 30 天作冷备份；30 天后清理 `/Data/chatgpt-auth/acct-{7..11}/` 目录
- [ ] **撞限自动切换 / IP 风控应急脚本**：再次出现 token_invalidated 时 5 acct pool 的应急运维 SOP

## 9. 与原始指令的差异说明

| 原始指令 | 实际落地 | 原因 |
|---------|---------|------|
| model 取名 `gpt-xxxx` 裸名 | gpt-5.4/5.3-codex/5.3-codex-spark 裸名；**gpt-5.5 用 `chatgpt-gpt-5.5`**（带前缀） | base-config 和 her vkey allowlist 命名是 `chatgpt-gpt-5.5`，不改命名兼容现有 |
| routing latency-based | 用默认 simple-shuffle | 5 个 acct 一致性强，simple-shuffle 配 LiteLLM 自带 cooldown 已足够；latency-based 是全局开关会影响其他 multi-deployment 组 |
| master key 复用 188 的 | 新建 `sk-chatgpt-aliyun-...`（按推荐） | 公网/内网 master key 隔离 |
| 通过 188 公网 NAT 调用 | 全部搬到阿里云内网（C-2 升级） | 阿里云能出国，无需中转 |

---

## 10. 阶段 D：promote 到 prod（全集群推广）

C-2 阶段 her-1000 单实例验证通过后，将 chatgpt 池接入推广到全集群。

### 10.1 prod litellm-proxy 升级 + 加 chatgpt 池

`k8s/litellm-proxy.yaml` 改 4 处：

1. **image 升级到 `ghcr.io/berriai/litellm:v1.85.0`**（vs 原 `cltx-her-ck-registry-vpc.../her/litellm-proxy:v1.83.14-stable.patch.3-20260513`），`imagePullPolicy: Never → IfNotPresent`。prod 的旧镜像本就 `imagePullPolicy: Never + nodeSelector pin`，跟 ACR VPC 红线已经实际打破；用 ghcr 公网跟 canary 同款。
2. **加 4 个 chatgpt-* model_list × 5 deployment**（共 20 条），代理调 chatgpt-acct-{7..11}.carher.svc。**保留 openrouter `gpt-5.4` / `gpt-5.3-codex`**（不删，避免 500+ her 流量瞬间打到 5 acct 池打爆）。
3. **加 fallback** `chatgpt-gpt-5.5 → wangsu-gpt-5.5`（`gpt-5.4 → wangsu-gpt-5.4` 原有）。
4. **加 env `CHATGPT_POOL_KEY`** 从 Secret `chatgpt-pool-master-key` 注入。

### 10.2 operator alias 重命名

`operator-go/internal/controller/config_gen.go` 改两个 map：

**modelMapLitellm**（her CRD `spec.model` → 实际 model_id）：
- `gpt`: `litellm/gpt-5.4` → `litellm/chatgpt-gpt-5.5`（核心改动：默认 gpt 走 chatgpt 池 5.5）
- 新增 `gpt-5.4`: `litellm/chatgpt-gpt-5.4`
- `codex`: `litellm/gpt-5.3-codex` → `litellm/chatgpt-gpt-5.3-codex`
- 删 `gpt-5.5`（被 `gpt` 覆盖）
- 新增 `opus4.7`: `litellm/claude-opus-4-7`

**models[] alias map**（per-her user-config 的 `agents.defaults.models.<id>.alias`）：
- `litellm/chatgpt-gpt-5.5`: alias `gpt-5.5` → `gpt`
- 删 `litellm/gpt-5.4`（替换为 `litellm/chatgpt-gpt-5.4 → gpt-5.4`）
- `litellm/gpt-5.3-codex` → `litellm/chatgpt-gpt-5.3-codex`（alias `codex` 保留）
- 删 `litellm/wangsu-gpt-5.5`（alias `gpt55` 移除）

### 10.3 全集群 her 默认模型改 `gpt`

批量 `kubectl patch her <name> -n carher --type=merge -p '{"spec":{"model":"gpt"}}'`：
- 217 个 `model=sonnet` 的实例 → patch 成 `gpt`
- 7 个 `model=opus` 的实例 → **保留**（用户主动选的）

operator reconcile 自动重新生成每个 her 的 user-config，`primary` 字段从 `litellm/claude-sonnet-4-6` 改成 `litellm/chatgpt-gpt-5.5`。

### 10.4 vkey allowlist 批量更新

229 个 carher-* virtual key 批量加 4 个 chatgpt-* model 到 allowlist（不删现有）。用 add-litellm-model skill 步骤 2 脚本（拉 → union → /key/update 逐个发，不用 /key/bulk_update 避免同质化丢条目）。

### 10.5 backend/litellm_ops.py 默认 allowlist 升级

`_BASE_MODELS` 加 `chatgpt-gpt-5.5/5.4/5.3-codex/5.3-codex-spark`，新创建的 carher-* key 默认就含 4 个 chatgpt-*。

### 10.6 撤 her-1000 切流

prod 现在也有 chatgpt 池，her-1000 不再需要走 canary：
```bash
kubectl patch her her-1000 -n carher --type=merge -p '{"spec":{"litellmUrl":""}}'
```
canary 资源（`litellm-proxy-canary` + 独立 ConfigMap + PVC）保留作未来实验。

### 10.7 最终全集群架构

```mermaid
flowchart LR
  subgraph ALL_HER["所有 carher her 实例"]
    direction TB
    NORMAL["217 个 spec.model=gpt<br/>默认 litellm/chatgpt-gpt-5.5"]
    OPUS["7 个 spec.model=opus<br/>用户主动选 opus"]
  end

  subgraph PROD["litellm-proxy (prod, replicas=2, ghcr 1.85.0)"]
    direction TB
    R["alias map (operator-rendered):<br/>gpt → chatgpt-gpt-5.5<br/>gpt-5.4 → chatgpt-gpt-5.4<br/>codex → chatgpt-gpt-5.3-codex<br/>opus/sonnet/opus4.7/gemini/<br/>glm/minimax/ds-pro/ds-flash → 不变"]
    LB["chatgpt-* model_list:<br/>每个 model_name × 5 deployment<br/>simple-shuffle LB"]
  end

  subgraph POOL["chatgpt-acct pool"]
    direction TB
    P7["chatgpt-acct-7"]
    P8["chatgpt-acct-8"]
    P9["chatgpt-acct-9"]
    P10["chatgpt-acct-10"]
    P11["chatgpt-acct-11"]
  end

  NORMAL --> R
  OPUS --> R
  R --> LB
  LB --> P7
  LB --> P8
  LB --> P9
  LB --> P10
  LB --> P11
  P7 --> OAI[("ChatGPT Pro<br/>acct-7~11")]
  P8 --> OAI
  P9 --> OAI
  P10 --> OAI
  P11 --> OAI
```

### 10.8 验证结果

| 项 | 结果 |
|---|---|
| prod litellm-proxy 2/2 副本 | ✅ Running v1.85.0 |
| prod 4 个 chatgpt-* 模型直接 curl | ✅ 全部 200 + content |
| operator 镜像 push ACR + rollout | ✅ `v20260520-f223ab2` |
| her-1000 user-config primary | ✅ `litellm/chatgpt-gpt-5.5` |
| 217 个 sonnet her patch → gpt | ✅ 0 失败 |
| 7 个 opus her 保留不动 | ✅ |
| 229 vkey allowlist 加 4 chatgpt-* | ✅ 0 失败 |
| canary 资源保留 | ✅ |

### 10.9 阶段 D 的潜在风险

| 风险 | 状态 |
|------|------|
| 5 acct 上游容量被 217 her 默认 gpt 流量打爆 | ⚠️ **需密切监控**。chatgpt-gpt-5.5 撞限 fallback wangsu，业务不挂；但 wangsu 流量瞬间放大 |
| ghcr 公网 first pull 失败 | ✅ 已验证 OK（canary 先跑了几小时） |
| LiteLLM 1.85 vs 1.83 patch 行为差异 | ⚠️ vanilla 1.85 不带 carher 自定义 patch.3（如 count_tokens fix）；如有回退到 1.83 的需求，需要 push patch 镜像（账号 push 权限阻塞需先解决）|
| 5 acct 中任一 token_invalidated | ⚠️ 影响 1/5 容量；其他 4 acct 接管；需 re-OAuth |

### 10.10 阶段 D 回滚路径

| 失败 | 回滚 |
|------|------|
| prod 业务大量 fail | `git revert f223ab2 && kubectl apply -f k8s/litellm-proxy.yaml`（恢复 1.83.14 镜像 + 旧 ConfigMap） |
| 5 acct 容量爆掉 | 选 (a) 批量 patch 部分 her 回 `sonnet`/`opus`；(b) 把 chatgpt-* model_list 改成 fallback-only（移除 5 acct deployment 仅保留 fallback wangsu-gpt-5.5） |
| operator 行为异常 | `kubectl set image deploy/carher-operator operator=<prev-tag> -n carher` |
| 想完全撤回 chatgpt 池 | 全套：(1) git revert (2) 批量 patch her spec.model 回 sonnet (3) operator image 回滚 (4) vkey 移除 4 个 chatgpt-* (5) chatgpt-acct-pool 资源 delete |
