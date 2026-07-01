# zerokey 10+ 账号机群化 × 双额度池 × 多人共享 — 整体方案 + 回归计划

> **文档性质**：可执行整体方案（10+ 账号批量上号 + 池化 + 多人共享 + 全 Agent 桥）  
> **状态**：**11 账号已上号**（kristine/timothy/zyq/owp/hgg/dvo + **2026-06-23 新增 elise/herbert/olga/tania/iheyv**，:8123–:8133，全 healthy + 128m 限制 + 6h refresh cron）；`zerokey-pool` 已 promote 到 litellm-product（30402）双协议验证通过，**prod 池 6→11**（config-managed，脚本 `prod-add-zerokey-accounts.py`，见 v2.11）；**v2 架构 P0+P1 已在 dev 重构 + 全量回归通过（见 v2.9）**；**prod 已对单 key `cursor-liuguoxian-l08v` 落地"首选 zerokey-pool"（key 级 alias + 全局兜底链 + manifest 同步，脚本 `prod-patch-key-primary-zerokey.py`，见 v2.10）**  
> **关联**：[Agent 桥落地方案](./zerokey-codex-agent-bridge-plan.md) · [索引](./zerokey-codex-artifacts.md) · [主 runbook](./chatgpt-web-to-codex-zerokey.md)

---

## ⚠️ 架构成熟化反思与设计（v2 · 2026-06-22）

> 本节是 2026-06-22 对 **zerokey-pool** 的深度复盘结论，**全部基于 198 live 配置 + pod 内 litellm 源码实测**（非文档/记忆）。它**修正了下文 §3.3 的路由设计**——`usage-based-routing-v2` / `allowed_fails` / `cooldown_time` 那套对 zerokey 并不成立，原因见 v2.2。

### v2.1 复盘：旧设计为什么"不够成熟"

zerokey-pool 之前本质是一坨**静态 model_list**，没有生命周期管理。本次实测暴露 5 个硬伤：

| # | 硬伤 | 实测证据 | 后果 |
|---|---|---|---|
| 1 | 没有生命周期管理 | 6 个 deployment 静态写死，无健康状态机 | 账号死了不会下线，只靠 6h cron 兜 |
| 2 | 健康检测靠被动 cooldown | live 配置 `background_health_checks:false`；默认 5s cooldown（`DEFAULT_COOLDOWN_TIME_SECONDS=5`） | 死账号被"打→401→冷5s→放回"无限循环，从不真正剔除 |
| 3 | 零可观测 | zerokey 返回 `usage=0`（实测 `prompt_tokens:0`），无 cost/quota 信号 | 不知道哪个账号忙/限流/将死，无法智能路由 |
| 4 | 路由无保护 | 6 个全 `rpm=None`（实测 `/v1/model/info`），均匀随机 | 随机可能短时砸某账号 → 自己把自己打 429 |
| 5 | session 与路由解耦 | 188 `refresh.sh` 写 `REFRESH_STALE`，但 LiteLLM 不知道 | 死账号留在轮询里，两套系统不通气 |

### v2.2 关键认知：zerokey ≠ acct，别硬套（这是成熟化的前提）

| 维度 | acct（成熟） | zerokey（本设计） |
|---|---|---|
| 配额信号 | `/codex/usage` 有 quota% | **无任何配额 API**，`usage=0` |
| 健康判定 | quota tier 分级 | 只能**直接探活** + 观测 401/429 |
| 缓存/上下文 | 原生 API，prompt caching 真实有效 | raw 无状态，**无缓存可言** |
| 调度策略 | usage-based-routing-v2（按水位避让） | 只能 simple-shuffle + rpm 保护 |
| 角色定位 | 主力池 | **有界溢出缓冲**，非主力 |

实测铁证（pod 内）：
- `supports_prompt_caching("openai/gpt-5-5")=False`（zerokey 真实 model 串带横杠；`openai/gpt-5.5` 带点才 `True`）→ `prompt_caching_deployment_check` 对 zerokey **完全失效**，既不记录也不路由回。
- zerokey 返回 `usage=0` → 无 `cached_tokens` 信号 → `usage-based-routing-v2` 在 zerokey 上**退化为无效**。
- live `litellm_settings` **无 `cache:true`** → LiteLLM 自身响应缓存也没开。
- `simple_shuffle.py` 实读：无 weight/rpm/tpm 时走 `random.choice` = **等概率随机**（非顺序轮询）。

→ **结论：成熟的 zerokey = acct 的「生命周期骨架」+「探活」替代「配额探测」+ 右定位为兜底缓冲。不要给 raw 硬做缓存/粘性。**

### v2.3 目标架构（分层）

```text
            ┌─────────────────────────────────────────────┐
   5min cron│  zerokey-rebalance.py（照搬 quota-rebalance 骨架）│
            │  state.json = source of truth                 │
            └─────────────────────────────────────────────┘
                 ↓ should_probe（自适应频率，省风控）
   ┌──────────────────────────────────────────────────────┐
   │ 健康探活（替代 acct 的 fetch_usage）：                    │
   │  - 轻量打 188:<port>/v1/models 或 1-token completion     │
   │  - 读 188 该账号 state/REFRESH_STALE 标记                │
   │  → classify: HEALTHY / RATE_LIMITED(429) / DEAD(401/超时) │
   └──────────────────────────────────────────────────────┘
                 ↓ diff state → 动作（LiteLLM /model/* + 188 refresh）
   ┌──────────────────────────────────────────────────────┐
   │ HEALTHY      → 确保在池（/model/new），rpm=正常上限        │
   │ RATE_LIMITED → rpm 降级 或 短暂 /model/delete            │
   │ DEAD         → /model/delete 物理摘除 + 触发该账号 refresh.sh│
   │ 恢复         → 探活通过 → /model/new 加回                  │
   │ 边沿变化     → 飞书告警（照搬 acct）                        │
   └──────────────────────────────────────────────────────┘
```

### v2.4 路由真相（修正 §3.3）

- routing_strategy 对 zerokey 应保持 **simple-shuffle（默认均匀随机）**，**不要** usage-based-routing-v2（usage=0 时无效）。
- 每个 zerokey block 设 **`rpm` 上限**保护单个网页会话，避免随机突发自伤 429。
- 选路只会落到"在池中的健康账号"——健康由 v2.5 的外部 rebalance 维护（而非 LiteLLM 主动健康检查，后者会烧网页额度）。

### v2.5 健康与自愈（核心补强 · 照搬 acct `quota-rebalance.py` 骨架）

- 新增 `ops/zerokey-rebalance.py`：5min cron、`state.json` 为 source of truth、`should_probe` 自适应频率（参照 [`quota-rebalance-design.md`](./quota-rebalance-design.md)）。
- **与 acct 的本质差异**：acct 探 `/codex/usage` 拿 quota%；**zerokey 无配额 API**，改为**直接探活**（`:port/v1/models` 或 1-token completion）+ 读 188 `REFRESH_STALE` + 观测 401/429。
- 状态机 → 动作见 v2.3 图。物理摘除走 `/model/delete`（`quota-rebalance-design.md` 已验证：cooldown 必须 `/model/delete` 才彻底）。

### v2.6 角色定位（结论）

zerokey-pool = **「有界溢出缓冲」**：raw 无状态、无缓存、`rpm` 上限有界、撞满自然漏到 wangsu。
**不要**给它做缓存/上下文复用（与 raw 介质对着干）；真要缓存/上下文 = **vscode 有状态池 + 同会话→同账号粘性**（另起架构，见 P3，默认不做）。

### v2.7 分阶段（取代 §6 中 zerokey 路由/健康相关部分）

| 阶段 | 内容 | 价值 | 成本 | 状态 |
|---|---|---|---|---|
| **P0** | `gpt-5.5` fallback 加 zerokey 为**二级**（先于 wangsu）+ 每账号加 `rpm` 上限 | 立即可用 + 防自伤 429 | ~10min | ✅ **dev 已验证**（见 v2.9） |
| **P1（核心）** | `zerokey-rebalance.py`：探活 → DEAD 自动 `/model/delete`+触发 refresh → 恢复自动加回 + 飞书告警 | 补齐 #1/#2/#5，真·自愈 | ~0.5d | ✅ **dev 已验证**（见 v2.9） |
| **P2** | per-account 请求/失败/429 埋点（因 usage=0 要自埋）+ 健康加权 + 容量看板 | 可观测可调度 | ~0.5d | 待做 |
| **P3（可选，不建议）** | vscode 有状态池 + 粘性路由，换缓存/上下文 | 仅当 zerokey 当主力 | 大，另起架构 | 不做 |

### v2.8 待决策（已定）

1. 角色 = **有界溢出缓冲** ✅（确认；不当主力，故 P3 不做）
2. `zerokey-rebalance.py` 落点 = **188 cron** ✅（与 refresh.sh / acct quota-cron 并存；dev 已装 `*/5`）
3. P1 立即做 ✅（已在 dev 落地 + 回归通过，见 v2.9）

### v2.9 dev 回归验证结果（2026-06-22，litellm-dev / NodePort 30400）

> 全程**只动 dev**，prod（litellm-product / 30402）路由与池**零改动**（见末项），线上真实用户不受影响。

**重构落地**：

- `zerokey-pool` 从 cm 静态块**迁移为 DB-managed**（`zerokey-dev-cm-patch.py --apply`：删 4 个 cm 块 + 滚动重启），改由 `zerokey-rebalance.py` 经 `/model/new`·`/model/delete` 动态持有（稳定 id `zk-pool-<port>`，幂等）。
- dev fallback 接二级兜底：`gpt-5.5` / `chatgpt-gpt-5.5` / `chatgpt-pool-gpt-5.5` → `[zerokey-pool, wangsu-gpt-5.5]`（网宿降为三级）。
- 每账号 `rpm=10` 上限（`simple-shuffle` 默认）。

**健康分级实测**（端口可达 + `REFRESH_STALE` 标记 + 可选真实 1-token 深探，深探按账号 30min 节流省额度）：6 账号深探全 `HEALTHY (deep 200)`。

**回归矩阵结果**：

| 用例 | 结果 |
|---|---|
| 池可达 / 服务 | `zerokey-pool` chat → 200 |
| 幂等 | 二次 reconcile：0 transitions，无误增删 |
| 负载分摊 | **5 个不同 key → 5 个不同账号**（8124/25/26/27/28）；同 key 12 连发恒定命中 8124 |
| 缓存/粘性 | 同 key 经 `deployment_affinity`(ttl 600s) 黏同一账号 → prompt cache 可命中；跨 key 才分摊（每实例自带 key → 500 实例天然摊到 6 号上，单实例黏一号） |
| DEAD 检测 + 防抖 | 模拟 `dvo` 会话死（touch `REFRESH_STALE`）：x1 anti-flap 保留（池仍 6）→ x2 `/model/delete zk-pool-8128`（池 5） |
| 降级仍服务 | 池=5 时 chat → 200（无用户面中断） |
| 自愈恢复 | 删标记后 reconcile → `dvo` 复判 HEALTHY → 自动加回（池 6） |
| **prod 隔离** | prod `gpt-5.5` fallback = `[wangsu-gpt-5.5]`（**无 zerokey**）；prod `zerokey-pool` 仍 6 条 config-managed（db=False）未动 |

**运维落点**（188 cron）：`~/.zerokey-rebalance/run-dev.sh`（source `dev.env` 注入 dev MK，权限 600）每 `*/5` 跑一次，`DEEP_PROBE=1 DEEP_INTERVAL=1800 REFRESH=0 REVIVE=0`。`REFRESH=0` 是刻意的——re-auth 仍**独占**给 6h refresh.sh，避免两个 capture 并发抢同一 browser profile。

**源码**（仓库 source-of-truth）：`scripts/chatgpt-onboard/zerokey-codex/ops/zerokey-rebalance.py`、`scripts/chatgpt-onboard/zerokey-codex/ops/zerokey-dev-cm-patch.py`。

**未做（刻意留给 prod 推进时决策，避免影响线上）**：

- prod（30402）尚未接 zerokey 二级兜底、尚未迁 DB-managed、cron 仍只有 dev 一份。
- `liuguoxian` key 直连 `gpt-5.5→zerokey-pool` 主用试用：属 prod key 改动，待 prod 阶段单独执行。

---

## 0. 一句话目标

把 **10+ 个 ChatGPT 账号** 机群化：同一批号**同时**产出 **Codex 5h/7d 额度**（chatgpt-acct 池）和 **网页 Chat 额度**（zerokey 池），对外暴露成**少量负载均衡模型名 + 全 Agent 桥**，支持**多人按 key 共享**，并具备**故障自愈与回归保障**。

---

## 1. 额度模型澄清（关键前提）

### 1.1 两套额度是独立的桶

| 维度 | Codex 5h/7d | zerokey 网页额度 |
|------|-------------|------------------|
| 后端 | `/backend-api/codex/responses`（官方 Codex） | `/backend-api/f/conversation`（网页 Chat） |
| 接入 | chatgpt-acct 池（OAuth token） | zerokey 回放浏览器请求（188 :812x） |
| 计数 | 每账号 5h 滚动窗 + 周 cap | 每账号 网页 Chat 配额 |
| Agent 完整度 | 原生最高 | 经 ToolCompiler↔桥 翻译 |

**事实**：两个桶**互不消耗、不可合并成一个计数器**。用网页不扣 5h/7d，反之亦然。

### 1.2 但同一账号可同时供两池

每个 ChatGPT 账号**本身就有两个桶**。因此 10+ 账号可以：

- **chatgpt-acct 池**：吃 10×（5h/7d）→ 原生全 Agent
- **zerokey 池**：吃 10× 网页 Chat → 对话 / Agent 溢出

→ **同一批号，额度利用率翻倍。**

### 1.3 「5h/7d 与网页共用」的正确实现 = fallback

无法合并计数，但可做**优先级路由**实现「事实上的共用」：

```text
Codex 请求
  → 优先 chatgpt-acct 池（5h/7d，原生 Agent）
  → 429 / 额度耗尽 → 自动回落 zerokey 桥（网页额度）
```

由 LiteLLM `fallbacks` 或桥层实现。**有效 Agent 容量 = Σ(5h/7d) + 网页溢出**。

---

## 2. 目标架构

```text
                         ┌─────────────── 10+ ChatGPT 账号 ───────────────┐
                         │  每账号 = Codex 5h/7d 桶  +  网页 Chat 桶        │
                         └───────────────┬───────────────┬────────────────┘
                                         │               │
                  ┌──────────────────────┘               └───────────────────────┐
                  ▼ (OAuth → 官方 Codex)                                           ▼ (浏览器回放 → 网页)
        ┌───────────────────────┐                              ┌──────────────────────────────┐
        │ chatgpt-acct 池        │                              │ zerokey 池 (188 :8123..:813x)  │
        │ (Codex 5h/7d, 已在 198) │                              │ 每账号一个容器/端口             │
        └───────────┬───────────┘                              └───────────────┬────────────────┘
                    │                                                           │
                    ▼                                                           ▼
        ┌─────────────────────────────────────────────────────────────────────────────────┐
        │ 198 LiteLLM Pro (litellm-product, :30402)                                          │
        │   • model: codex-pool        → chatgpt-acct 负载均衡组 (5h/7d)                     │
        │   • model: zerokey-pool      → zerokey 负载均衡组 (网页，对话)                      │
        │   • fallbacks: codex-pool → zerokey-pool（额度溢出）                                │
        │   • per-user key + allowlist + budget                                              │
        └───────────────┬───────────────────────────────────────────────┬───────────────────┘
                        │ 对话 / 补代码                                    │ 全 Agent
                        ▼                                                  ▼
                   任意 OpenAI 客户端                          zerokey-bridge (198, :30788, 鉴权)
                   (Cursor / Codex chat)                       upstream = zerokey-pool（轮询账号）
                                                               wire: /v1/responses + 流式 apply_patch
                                                                       │
                                                                       ▼
                                                                 Mac/多人 Codex Agent
```

---

## 3. 批量上号方案（10+ 账号）

### 3.1 先固化本次验证踩出的两个修复（必做）

本次最小闭环验证发现 onboarding 有两处会卡住无人值守流程，已临时绕过，需**合并回脚本**：

| 修复 | 问题 | 方案 |
|------|------|------|
| `FORCE_LOGIN=1` | 干净 profile 在 CF/`/auth/login` 页被误判"已登录"，跳过登录 | capture 脚本新增 env，强制走密码+OTP |
| OTP 取旧码 / 限流 | 收件箱多封历史 code 邮件，提取器取到旧码 → `Incorrect` → 触发 ChatGPT 限流 | onboarding 前 `mailread-otp.py MODE=purge` 清旧码；读后即删 |

> 这两项已在仓库 `capture/zerokey-web-capture.py`（FORCE_LOGIN）和 `capture/mailread-otp.py` 实现，需**回灌进 `add-account.sh` 默认流程 + 重建 capture 镜像**。

### 3.2 批量驱动器（TSV → 全自动）

新增 `ops/batch-onboard.sh`，读取一份 TSV：

```text
# accounts.tsv : id  email  mail_pw  chatgpt_pw  [port]
zk03   a@mail.com   ****   ****
zk04   b@mail.com   ****   ****
...（10+ 行）
```

行为：

1. **自动端口分配**：扫描已用 `812x/813x`，取下一个空闲端口
2. **限并发**（建议 2–3）：mail.com / CF 同 IP 并发过高会风控
3. 每账号：`purge 旧码 → FORCE_LOGIN capture → 注入新码 → 起容器 → 健康校验`
4. 失败留 `REFRESH_STALE`，单独重试，不阻塞其他账号
5. 全部成功后调用 §3.3 注册池

### 3.3 池化注册（升级 `litellm-register-zerokey.py`）

从「每账号 4 个独立模型名」升级为 **1 个负载均衡组**：

```yaml
# 所有账号共用一个 model_name → LiteLLM 自动分摊 + 429 failover
- model_name: zerokey-pool
  litellm_params: { model: openai/gpt-5-5, api_base: http://10.68.13.188:8123/v1, api_key: raw, use_chat_completions_api: true }
- model_name: zerokey-pool
  litellm_params: { model: openai/gpt-5-5, api_base: http://10.68.13.188:8124/v1, api_key: raw, use_chat_completions_api: true }
# ... 每账号一行，10+ 行
```

可并行保留 `zerokey-pool-thinking` / `-pro` / `zerokey-o3-pool` 等组。

router 配置：

> ⚠️ **已被 v2.4 修正（2026-06-22）**：下面这份 `usage-based-routing-v2` / `allowed_fails` / `cooldown_time`
> 对 zerokey **不成立**——zerokey `usage=0`，usage-based 路由无信号、退化无效。正确做法见 v2.4/v2.5：
> **simple-shuffle（默认）+ 每账号 `rpm` 上限 + 外部 `zerokey-rebalance.py` 探活自愈**。此块仅留作历史对照。

```yaml
# ❌ 历史版本（勿照抄，见 v2.4）
router_settings:
  routing_strategy: usage-based-routing-v2   # zerokey usage=0 → 无效
  num_retries: 2
  allowed_fails: 2
  cooldown_time: 60          # 429 账号冷却 60s
litellm_settings:
  fallbacks:
    - { codex-pool: ["zerokey-pool"] }   # 5h/7d 用尽 → 网页溢出
```

---

## 4. 全 Agent 桥（池化 + 多人）

基于已验证的 [桥 PoC](../scripts/chatgpt-onboard/zerokey-codex/bridge/zerokey-responses-bridge.py)，生产化：

| 项 | PoC（已验证） | 生产目标 |
|----|---------------|----------|
| 部署 | 188 loopback :8788 | **198 Deployment**，NodePort 30788（内网/VPN） |
| upstream | 单账号 :8125 | **zerokey-pool**（轮询多账号，429 摘除） |
| 协议 | 非流式 round-trip | **流式** `response.custom_tool_call_input.delta` |
| 鉴权 | 无 | **独立 bridge key**（非 LiteLLM master key） |
| 工具映射 | create_file/write→apply_patch | + replace_string_in_file→Update、run_in_terminal→shell |

> 镜像在构建服务器 build → push ACR **VPC** → 198 拉取（遵守镜像拉取规则）。**不**写进 LiteLLM model_list。

---

## 5. 多人共享

### 5.1 对话场景（成熟可铺开）

- 198 LiteLLM **per-user key + allowlist `zerokey-pool`**（沿用 `litellm-pro-ops`）
- 每 key 配 **budget + rpm/tpm 限速**，可审计
- 人数↑ → 账号池扩容（账号数 ≈ 峰值并发）

### 5.2 Agent 场景

- 桥读 **bridge key**（独立），upstream 接 zerokey-pool
- 仅内网 / VPN / cloudflared，**禁止**公网裸暴露
- 多人 Agent → 桥 + 池自动分流

### 5.3 容量 / 限流 / 风控（务必知道）

| 约束 | 说明 | 对策 |
|------|------|------|
| 单账号单会话 | 网页端有并发/频率限制 | 池大小 ≈ 峰值并发；usage-based 路由 |
| 429 风控 | 频繁触发会临时封 | `cooldown_time` 摘除 + 自动恢复 |
| ToS/封号 | 网页自动化有风险 | 用可丢的号；**严禁接生产 bot** |
| cf_clearance 绑 IP | 换出口即失效 | capture 必须在 188 |

---

## 6. 分阶段实施

| Phase | 内容 | 预估 |
|-------|------|------|
| 0 | 三账号 + 桥 PoC 闭环（kristine/timothy/zyq） | **已完成** |
| 1 | 固化 FORCE_LOGIN + purge/mailread 进 add-account.sh + 重建镜像 | 0.5d |
| 2 | `batch-onboard.sh` 批量上号 10+ 账号 | 0.5d + 上号时间 |
| 3 | `litellm-register-zerokey.py` 升级 zerokey-pool 负载均衡组 | 0.5d |
| 4 | 198 部署流式桥（pool upstream + 鉴权） | 2–3d |
| 5 | codex-pool→zerokey-pool fallback + 多人 key | 0.5d |
| 6 | refresh 机群化（统一 sweeper cron）+ 监控 | 1d |

---

## 7. 回归计划（详细矩阵）

> 原则：每次变更后跑对应层；**任何对 198 的改动必须先跑 L4 共享回归**，确认其他用户零影响。

### L1 — 单账号（每个新账号上号后）

| 用例 | 命令 | 通过条件 |
|------|------|----------|
| 健康 | `curl :PORT/health` | 200 healthy |
| 模型列表 | `curl :PORT/v1/models` | 含 gpt-5-5 等 slug |
| 原始对话 | raw chat `reply: OK-PORT` | 返回 `OK-PORT` |
| ToolCompiler | vscode stream "create file" | SSE 含 `tool_calls: create_file` |
| session 头 | 校验 users.json | 含 authorization/cookie/openai-sentinel-proof-token |

### L2 — 池（zerokey-pool 注册后）

| 用例 | 通过条件 |
|------|----------|
| 池可达 | `curl :30402/v1/models` 含 `zerokey-pool` |
| 负载分摊 | 连发 N 次，后端命中多个账号端口（看 zerokey 日志） |
| 429 failover | 摘一个账号（停容器），请求仍成功走其他 |
| 冷却恢复 | 恢复账号后重新进入轮询 |

### L3 — Agent 桥

| 用例 | 通过条件 |
|------|----------|
| 桥健康 | `curl bridge/health` |
| responses→apply_patch | `/v1/responses` 创建文件 → 返回 `custom_tool_call apply_patch` + V4A（**已验证 3/3**） |
| 流式 | SSE 出 `custom_tool_call_input.delta` |
| Update 映射 | replace_string_in_file → `*** Update File` |
| shell 映射 | run_in_terminal → `shell` |
| 端到端 | `codex -p zerokey-agent` 空目录建文件，TUI 出 diff |
| 鉴权 | 无 key 401；有 key 200 |

### L4 — 198 共享隔离（**每次动 198 必跑**）

| 用例 | 通过条件 |
|------|----------|
| 其他模型 | wangsu / chatgpt-pool 等非 zerokey 模型正常 |
| zerokey responses | `zerokey-pool` `/v1/responses` 正常（**本次已验证 kristine/timothy OK**） |
| per-user key | 普通 key allowlist 生效，未授权 key 拿不到 |
| cm 一致性 | live cm 与 manifest 同步（防 stale apply 回滚） |

### L5 — fallback / 多人

| 用例 | 通过条件 |
|------|----------|
| 5h/7d 溢出 | codex-pool 限额 → 自动落 zerokey-pool |
| 多 key 并发 | M 用户并发，预算/限速各自生效 |
| 审计 | spend/activity 可按 key 查 |

### L6 — 故障注入

| 注入 | 期望 |
|------|------|
| 停 1 账号容器 | 池自动摘除，整体不挂 |
| session 过期 | refresh 重抓；失败留 STALE + 告警，旧会话不受损 |
| 桥宕 | Agent 失败但对话/5h7d 不受影响 |
| 198 cm 误改 | 从 `~/zerokey-litellm-backups/` 恢复 |

### 回归基线脚本（建议沉淀）

`ops/regression.sh [L1|L2|L3|L4|all]` 一键跑对应层并输出 PASS/FAIL 汇总。

---

## 8. 风险与回滚

| 风险 | 缓解 |
|------|------|
| 批量上号触发 mail.com/CF 风控 | 限并发 2–3，账号间隔；失败重试 |
| 网页模型 Agent 成功率低于官方 | codex-pool 优先，zerokey 仅溢出 |
| 账号被封 | 可丢号；不接生产 bot；池冗余 |
| 198 回归失败 | 改 zerokey 只用注册脚本；cm 备份恢复 |

回滚：桥宕→Codex 改回 `-p zerokey`(对话) 或 chatgpt-acct(5h/7d)；池坏→退回单账号模型名；cm→备份恢复。

---

## v2.10 prod 灰度：单 key 透明切流（liuguoxian · 2026-06-23）

> 目标：把 `zerokey-pool` 接进 **prod（litellm-product / 30402）**，但**只对 `cursor-liuguoxian-l08v` 一把 key 生效**，其他 key 与全局路由零影响。

### 选型：为什么用 key 级 `aliases` 而不是全局 fallback

| 方案 | 影响面 | 是否需要 rollout | 结论 |
|---|---|---|---|
| 改全局 `model_group_alias` / `fallbacks` | **所有 key** 的 gpt-5.5 | 需 cm edit + rollout | ✗ 违反"不影响其他 key" |
| LiteLLM **key 级 `aliases`** | 仅该 key | 纯 `/key/update` 运行时，**无 rollout** | ✓ 采用 |

LiteLLM v1.89.2 的虚拟 key 支持 `aliases:{公开名→实际模型}`，在 auth 层做名字翻译，**优先级高于全局 `model_group_alias`**。

### 验证（throwaway key 实测，已删）

- **假设**：key 级 `aliases:{gpt-5.5→zerokey-pool}` 能让该 key 的 `gpt-5.5` 走 zerokey，且不影响别人。
- **证伪条件**：若不成立，带 alias 的 key 调 `gpt-5.5` 应仍落到 `chatgpt-acct-*`，或 master 调 `gpt-5.5` 落到 zerokey。
- **数据**：
  - 带 alias 的临时 key 调 `gpt-5.5` → `x-litellm-model-id=e450402e…`（属 6 个 zerokey-pool deployment 之一）✓
  - master 调 `gpt-5.5`（无 alias）→ `chatgpt-acct-28-gpt-5.5` ✓（隔离成立）
  - → 假设成立。临时 key 已 `/key/delete`。

### 最终链路（已落地 2026-06-23）

```
liuguoxian 发 gpt-5.5 / chatgpt-gpt-5.5
  → zerokey-pool (per-key alias，首选)
  →(挂) chatgpt-gpt-5.5 (16 个 chatgpt-acct-*，全局 fallback)
  →(再挂) wangsu-gpt-5.5 (全局 fallback)
```

- **首选**：liuguoxian key 级 `aliases={gpt-5.5:zerokey-pool, chatgpt-gpt-5.5:zerokey-pool}` + allowlist 含 `zerokey-pool`（纯 `/key/update`，无 rollout）。
- **兜底**：全局 `router_settings.fallbacks` 加 `zerokey-pool → [chatgpt-gpt-5.5, wangsu-gpt-5.5]`（一次 cm + rolling，仅 liuguoxian 触发）。
- 实测：throwaway key 调 `gpt-5.5`、`chatgpt-gpt-5.5` 均落 zerokey deployment（`e450402e…`）；master 调 `gpt-5.5` 仍落 `chatgpt-acct-28`（隔离成立）。

### 关键认知（实测定型，写进 skill `litellm-key-provider-swap`）

1. **没有"每 key 一个路由文件"**：路由 = 全局 `litellm-config`（源文件 `/root/litellm-product-manifests/30-cm-litellm-config.yaml`）+ 每 key 的 `models`/`aliases`。buyitian 等默认 key 走全局 `gpt-5.5 →alias→ chatgpt-gpt-5.5 →fallback→ wangsu`。
2. **per-key fallback 不可靠**（litellm-product 实测 2026-06-23）：兜底链只能写全局 `fallbacks`；靠"只有该 key 调 zerokey-pool"保证隔离。
3. **manifest 漂移**：直接 `kubectl apply` live cm 后必须同步回写 manifest 源文件，否则下次重 apply 会冲掉。脚本已自动同步。
4. **prod `zerokey-pool` 是 config-managed**（在 `litellm-config` cm 的 `config.yaml model_list` 里，实测 `/v1/model/info` 全部 `db_model=False`）——**不是** DB-managed（dev 才是 DB-managed）。chatgpt 账户池另算。查真实 deployment 一律以 `GET /v1/model/info` 为准。

### 脚本（幂等，一键执行/回滚）

```bash
python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian            # 预览
python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian --apply    # 执行
python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian --rollback --apply  # 回滚
```

### 已知权衡（留给后续）

- **prod 池仍 config-managed**：无 rebalancer 自愈；死 session 靠 6h refresh cron + LiteLLM cooldown 兜。扩面前再做 DB-managed 迁移 + prod rebalancer cron。
- 首选 zerokey 会持续消耗 ChatGPT web 配额（单 key 试用可接受）。

---

## v2.11 批量扩容 6→11 + 手动 OTP 上号流程（acct-40~44 · 2026-06-23）

> 目标：把 acct-40~44（elise/herbert/olga/tania/iheyv）上号进 188 zerokey 机群并加入 prod `zerokey-pool`。

### 凭据来源（无需手输）

每个 chatgpt-acct 的 `email / mail_pw / chatgpt_pw` 已存在 188：`/Data/chatgpt-auth/acct-<N>/.creds`（`add-chatgpt-acct-198-full.sh` step[2] 写的）。上号脚本直接 `source` 即可。
**注意**：acct-39（kristine_free517@mail.com）就是现有 zerokey `kristine`（:8123）的同一 ChatGPT 账号（`refresh.sh` 默认 `MAIL_USER`），**不要重复上号**。

### 关键坑：mail.com auto-OTP 不可靠 → 改手动截图读码

`add-account.sh` 默认 `OTP_AUTO_ONLY=1`，靠脚本爬 mail.com 收件箱拿 6 位码。实测**必失败**：mail.com 收件箱是跨域 iframe（"inbox keyword never appeared"），且码在邮件**正文**里。

- **假设**：auto-OTP 爬取能拿到码。
- **证伪条件**：若可靠，capture 日志应出现 `got OTP`，不应出现 `OTP auto failed`。
- **数据**：elise 首跑 auto 模式 → `inbox keyword never appeared` → `OTP not yet, retry`×N → 必然 `OTP auto failed (OTP_AUTO_ONLY=1, no manual fallback)`。→ 假设证伪，弃用 auto。

正确组合（`zerokey-web-capture.py` 实测）：`OTP_AUTO_MAX=0`（跳过脆弱爬虫）+ `OTP_SHOT=1`（**打开** OTP 邮件并截图正文）+ `OTP_FILE_WAIT=600`（等人工把码写进 `otp.txt`）。沉淀成脚本 `ops/manual-onboard.sh`（两阶段 `start` / `finish`）。

### 上号流程（每账号 ~5min）

```bash
# 188：start（脚手架 + 后台 capture，~3min 后出 otpshot）
set -a; source /Data/chatgpt-auth/acct-40/.creds; set +a
~/zerokey-codex/ops/manual-onboard.sh start elise "$email" "$mail_pw" "$chatgpt_pw" 8129
# 本机：拉 state/out/otpshot.png 读 6 位码（vision agent 直接读图）
# 188：注入码 → capture 自动继续
echo <code> > ~/zerokey-codex-accounts/elise/state/out/otp.txt
# 188：finish（等 users.json → compose up → health + /v1/models）
~/zerokey-codex/ops/manual-onboard.sh finish elise 8129
```

端口分配：elise 8129 / herbert 8130 / olga 8131 / tania 8132 / iheyv 8133。每个容器 `mem_limit/memswap_limit=128m`（compose 模板已带）。
注：新容器首次 chat 偶发返回空 content（预热），重试即返回正常出词——非故障。

### 加入 prod zerokey-pool（一次 cm 编辑 + 一次滚动重启）

5 个容器全部 healthy 后，**一次性**把 8129–8133 加进 prod config-managed 池（避免 5 次滚动重启）：

```bash
python3 scripts/prod-add-zerokey-accounts.py --ports 8129-8133            # 预览
python3 scripts/prod-add-zerokey-accounts.py --ports 8129-8133 --apply    # 执行
```

脚本幂等：复刻现有 zerokey-pool 块全部字段、只改 `api_base` 端口；`yaml.dump` 写回 cm + `kubectl apply` + 零中断滚动重启 + **同步回写 manifest** 源文件（防漂移）+ 校验 `/v1/model/info` 计数。
**注意**：LiteLLM ~90s initialDelay × 4 副本滚动会超 300s，`rollout status` 超时是**正常**现象（脚本已改 600s + 容错，超时不影响后续 manifest 同步/校验；如旧版崩了需手动补 manifest 同步）。

### 6h refresh cron（错峰）

11 账号同时 minute 0 刷新会有资源尖峰，新 5 个错峰到 min 5/15/25/35/45：`<min> */6 * * * ~/zerokey-codex-accounts/<acct>/ops/refresh.sh`。

### 验证结果

- 188：11 个 `zerokey-codex-*` 容器全 healthy、全 `mem=128MiB`；5 新账号单端口 smoke `pong` 通过。
- prod：`/v1/model/info` `zerokey-pool` = **11 deployments**（8123–8133 全 `db_model=False`）；30402 e2e `zerokey-pool` chat → 200 真实出词。
- manifest 已同步（`30-cm-litellm-config.yaml`，含 11 池块 + 1 fallback 引用）。
- liuguoxian key **无需改动**：其 per-key alias 已指向 `zerokey-pool` 组，池扩容自动生效。

### 新增脚本

- `scripts/chatgpt-onboard/zerokey-codex/ops/manual-onboard.sh` — 手动 OTP 上号（start/finish 两阶段）。
- `scripts/prod-add-zerokey-accounts.py` — 幂等把端口加入 prod config-managed zerokey-pool（cm + 滚动 + manifest 同步 + 校验）。

---

## 9. 附录：命令速查

```bash
# 跳板
./scripts/jms ssh JSZX-AI-03      # 188 zerokey
./scripts/jms ssh AIYJY-litellm   # 198 LiteLLM

# 批量上号（规划中）
cd ~/zerokey-codex/ops && ./batch-onboard.sh accounts.tsv

# 池注册（升级后）
./scripts/jms scp scripts/chatgpt-onboard/zerokey-codex/ops/litellm-register-zerokey.py AIYJY-litellm:/tmp/
./scripts/jms ssh AIYJY-litellm 'python3 /tmp/litellm-register-zerokey.py --apply --sync-manifest'

# 回归（规划中）
./scripts/jms ssh JSZX-AI-03 'bash ~/zerokey-codex/ops/regression.sh all'
```

---

## v2.12 阿里云 her 灰度接入 zerokey-pool（her-1000 · 2026-06-23）

> 目标：让阿里云的 `her` 实例能用上 188 的 zerokey-pool，先灰度 **her-1000 一个**，**绝不影响其他用户**（prod 269 个 her 与其他 canary key 一律不动）。

### 网络事实（先证伪再定方案）

- **假设**：zerokey 可直接跑在阿里云。**证伪条件**：阿里云 IP 访问 `chatgpt.com` 应 200。**数据**：`laoyang`(43.98.160.216) `curl chatgpt.com` → **403 `cf-mitigated: challenge`**（Cloudflare 地域封锁）→ 假设证伪，zerokey 必须留在 188。
- 旧 `节点` SSH 反向隧道（188→阿里云 172.16.0.228）已退役。
- 选定 **Direction 3 代理链**（零 IT 依赖、已实测 200）：
  `阿里云 her → litellm-proxy-canary → zerokey-pool(api_base=https://cc.auto-link.com.cn/pro/v1) → 198 公网入口 → 198 zerokey-pool → 188 zerokey 容器`

### 落地动作（全部热生效，**canary 零重启、零 CM 改动**）

1. **198 mint 专用 vkey**（最小权限链路 key）：`/key/generate` models=`[zerokey-pool, chatgpt-gpt-5.5, wangsu-gpt-5.5]`（含后两者是为了让 **198 侧已有的全局 fallback** `zerokey-pool→[chatgpt-gpt-5.5, wangsu-gpt-5.5]` 对此 key 生效自愈；198 CM 未改，prod 不受影响）。
2. **canary 热加 zerokey-pool**：canary `STORE_MODEL_IN_DB=True` → `POST /model/new` 注册 1 个 deployment（`openai/zerokey-pool` → `cc.auto-link.com.cn/pro/v1`，`mode:responses`，id=`zerokey-pool-198-link`）。**不改 CM、不重启**，`/v1/model/info` 即时可见。
3. **仅对 her-1000 的 key 做 per-key alias**：`carher-1000`(共享 DB，canary 可见) `/key/update` → `aliases:{chatgpt-gpt-5.5→zerokey-pool}` + allowlist 加 `zerokey-pool`。**未加任何全局 model_group_alias**，故其他 canary key 的 `chatgpt-gpt-5.5` 仍走原 Aliyun chatgpt-acct 池。
4. **her-1000 指向 canary**：`kubectl patch herinstance her-1000 spec.litellmUrl=http://litellm-proxy-canary.carher.svc:4000`，**model 保持 sonnet 不变**（用户手动 `/model gpt` 验证）。reloader sidecar 5s 热注入，pod 不重建。

### 验证（her-1000 的 key 实测）

| 请求 | 结果 | 含义 |
|---|---|---|
| `/v1/responses` model=`chatgpt-gpt-5.5` | `x-litellm-model-id: zerokey-pool-198-link`，200，出词 `pong` | gpt 路径整链通到 188 zerokey ✓ |
| `/v1/chat/completions` model=`claude-sonnet-4-6` | `x-litellm-model-id: wangsu-direct/claude-sonnet-4-6`，200 | 默认 sonnet 仍走 claude，未被污染 ✓ |
| her-1000 live config | `litellm.baseUrl=http://litellm-proxy-canary.carher.svc:4000`，primary=`litellm/claude-sonnet-4-6` | 配置已热更新 ✓ |

### 隔离保证（为何不影响其他用户）

- prod `litellm-proxy`（269 个 her 用）**全程未触碰**。
- canary **未加全局 alias**，新 zerokey-pool deployment untagged 但**只有 carher-1000 这一个 key allowlist 里有它**，其他 key 既无 alias 也无 allowlist → 调不到。
- 198 仅放宽了那个 mint 出来的专用 link key，未动任何全局配置（fallback 链早已存在）。

### 回滚（任一步独立可逆）

```bash
# 1. her-1000 回 prod litellm（最快回滚）
kubectl -n carher patch herinstance her-1000 --type merge -p '{"spec":{"litellmUrl":""}}'
# 2. 清 her-1000 key 的 alias（canary master key）
curl .../key/update -d '{"key":"<carher-1000 key>","aliases":{}}'
# 3. 删 canary 上的 zerokey-pool deployment
curl .../model/delete -d '{"id":"zerokey-pool-198-link"}'
# 4. 删/禁用 198 link vkey（可选）
```

---

## v2.13 阿里云 prod 50 个 her 批量接入 zerokey-pool（默认切 gpt · 2026-06-23）

> 目标：在**正式环境**让 50 个 her（carher-2/3/5/7/17/20/22/25/47/50/52/57/62/79/80/82/85/87/94/116/126/127/130/138/143/145/147/155/157/171/173/178/179/181/183/185/187/194/195/204/207/217/218/220/223/246/253/258/262/269）**默认走 zerokey-pool（gpt 真额度）**，其余 219 个 prod her + 其他 key 零影响。
> 与 v2.12 区别：v2.12 是 canary 单测 + 保持 sonnet 默认；本次是 **prod litellm-proxy + 批量 50 + 默认切 gpt**。用户三选确认：默认切 gpt / 一次性 50 / prod 侧加全局 fallback。

### 前置事实（只读勘察）

- 50 个 her 全部：`litellmUrl` 空（=prod `litellm-proxy`）、`provider=litellm`、`model=sonnet`、各有 `litellmKey`。
- `provider=litellm` 时 operator 映射 `spec.model=gpt → litellm/chatgpt-gpt-5.5`（`config_gen.go` modelMapLitellm）→ 正好接上 per-key alias。
- prod `litellm-proxy`：`STORE_MODEL_IN_DB=True`（可热加，免 CM 改）、2 副本、原无 zerokey-pool；router_settings 已有 `chatgpt-gpt-5.5→[wangsu-gpt-5.5]`，无 zerokey-pool fallback。

### 链路

```
prod her → litellm-proxy(Aliyun prod, 192.168.35.175:4000) → zerokey-pool(api_base cc.auto-link.com.cn/pro/v1)
         → 198 zerokey-pool → 188 zerokey 容器
```

### 落地（脚本 `scripts/prod-aliyun-her-zerokey.py`，幂等可回滚，密钥走 env，不入库）

在 `k8s-work-226`（有 carher kubectl；无 svc DNS，故脚本用 ClusterIP）执行：

| 步 | 命令 | 效果 |
|---|---|---|
| A register | `TARGETS=… LINK_KEY=sk-… pz.py register --apply` | 热加 zerokey-pool deployment（id=`zerokey-pool-198-link`，复用 198 link key），不重启 |
| B fallback | `pz.py fallback --apply` | CM 加全局 `zerokey-pool→[chatgpt-gpt-5.5, wangsu-gpt-5.5]` + **零中断滚动重启**（2 副本，~350s）；重启后两副本从 DB 重载 zerokey-pool |
| C keys | `TARGETS=… pz.py keys --apply` | 50 个 `carher-N` key 设 `aliases:{chatgpt-gpt-5.5→zerokey-pool}` + allowlist += zerokey-pool（50/50） |
| D switch | `TARGETS=… pz.py switch --apply` | 50 个 her `spec.model=gpt`（cutover；reloader 5s 热注入，pod 不重建） |

> 顺序要点：A/C 是 additive（默认仍 sonnet，无流量）；B 完成 fallback 上线后再 D cutover，保证切过去时兜底已就绪。

### 验证

| 检查 | 结果 |
|---|---|
| zerokey-pool 两副本（3 探针） | 均 =1 ✓ |
| 全局 fallback live | `zerokey-pool→[chatgpt-gpt-5.5, wangsu-gpt-5.5]` ✓ |
| 目标 her-2 key /responses `chatgpt-gpt-5.5` | `x-litellm-model-id=zerokey-pool-198-link`，`pong` ✓ |
| 目标切换 | 50/50 `spec.model=gpt`；her-2 live config primary=`litellm/chatgpt-gpt-5.5` ✓ |
| 非目标 her-10 | aliases 空、无 zerokey；`chatgpt-gpt-5.5→chatgpt-acct-9/chatgpt-gpt-5.5`（原 Aliyun 池）✓ 零影响 |

### 容量风险（按诊断纪律：风险非定论）

11 个 zerokey 账号定位"有界溢出缓冲非主力"；50 个 prod her **默认** gpt 会持续压 zerokey。无当前 gpt 请求速率数据，无法断言是否过载。**安全网**：prod 全局 fallback `zerokey-pool→[chatgpt-gpt-5.5(Aliyun chatgpt-acct 池), wangsu-gpt-5.5(付费)]` + 198 侧 zerokey-pool 自身 fallback（link key 已 scope 三件套）→ zerokey 饱和时自动溢出到 acct/wangsu，用户不会报错，但**成本会向 wangsu 漂移**。建议观察 429/fallback 命中率与 wangsu 用量。

### 追加：her-1000 也并入 prod（2026-06-23）

> 用户后续要求 carher-1000 也在正式环境部署。**关键发现：prod 与 canary 的 LiteLLM DB 不共享**——v2.12 给 carher-1000 设的 per-key alias 是用 **canary** master key 写的，prod 侧该 key `aliases={}`、无 zerokey。
> 修正动作：① `her-1000 spec.litellmUrl=""`（回 prod litellm-proxy）+ `model=gpt`；② 用 **prod** master key 重新给 `carher-1000` 设 alias（`TARGETS=1000 pz.py keys --apply`）。验证 `x-litellm-model-id=zerokey-pool-198-link`、`pong`，live config `baseUrl=http://litellm-proxy.carher.svc:4000` + primary=`litellm/chatgpt-gpt-5.5`。canary 上的旧 alias 留着无害（her-1000 已不走 canary）。
> → 正式环境 zerokey 默认池总数 **50 → 51**（含 her-1000）。

### 回滚（幂等，任一步独立可逆）

```bash
# 全量回滚（在 k8s-work-226）；her-1000 一并加入 TARGETS
TARGETS=2,3,…,269,1000 python3 pz.py switch   --rollback --apply   # 切回 sonnet（最快止血）
TARGETS=2,3,…,269 python3 pz.py keys     --rollback --apply   # 清 50 个 key 的 alias
python3 pz.py fallback --rollback --apply                     # 撤全局 fallback + 滚动重启
python3 pz.py register --rollback --apply                     # 删 zerokey-pool deployment
```

---

## 10. 当前进度快照（2026-06-22）

| 项 | 状态 |
|----|------|
| kristine :8123 / timothy :8124 | 运行中（healthy，真实出词回归通过） |
| zyq :8125 / owp :8126 / hgg :8127 / dvo :8128 | 运行中（healthy），6h refresh cron 已装 |
| **elise :8129 / herbert :8130 / olga :8131 / tania :8132 / iheyv :8133（acct-40~44）** | ✅ **2026-06-23 新增上号**：5 个容器 healthy + 单端口 smoke `pong` 通过；mem_limit 128m；6h refresh cron 错峰装好（min 5/15/25/35/45）。详见 v2.11 |
| 198 zerokey-pool | **已 promote 到 litellm-product（30402）**，chat/completions + responses 双协议回归通过（2026-06-21）；**2026-06-23 池从 6→11**（8123–8133，全 config-managed db=False），live `/v1/model/info` 11 deployments，e2e 真实出词通过 |
| 桥 PoC responses→apply_patch | 闭环 3/3（孤儿进程 :8788 仍健康，代码在仓库） |
| FORCE_LOGIN / mailread-otp 修复 | 代码已写，待回灌 add-account.sh + 重建镜像 |
| **架构成熟化（v2）P0+P1 — dev** | ✅ **dev 已重构 + 全量回归通过**（DB-managed 池 + 二级兜底 fallback + rpm + `zerokey-rebalance.py` 5min cron 自愈），详见 v2.9 |
| **prod（30402）灰度** | ✅ **已对单 key `cursor-liuguoxian-l08v` 落地首选 zerokey-pool**：key 级 `aliases:{gpt-5.5→zerokey-pool, chatgpt-gpt-5.5→zerokey-pool}` + 全局 `fallbacks: zerokey-pool→[chatgpt-gpt-5.5, wangsu-gpt-5.5]` + manifest 已同步。实测两模型名均落 zerokey、master 隔离 OK。其他 key 不受影响。脚本 `scripts/prod-patch-key-primary-zerokey.py`，详见 v2.10 |
| **阿里云 her 灰度** | ✅ **2026-06-23 her-1000 接入 zerokey-pool**（Direction 3 代理链：canary→cc.auto-link/pro→198 zerokey-pool）：canary 热加 zerokey-pool（`/model/new`，零重启）+ her-1000 key per-key alias `chatgpt-gpt-5.5→zerokey-pool` + `litellmUrl→canary`，model 保持 sonnet。实测 gpt 落 `zerokey-pool-198-link`、sonnet 仍 claude。prod 269 her 与其他 canary key 零影响。详见 v2.12 |
| **阿里云 prod 50 her 批量** | ✅ **2026-06-23 50 个 prod her 默认切 gpt→zerokey-pool**（prod litellm-proxy 热加 zerokey-pool + 全局 fallback 滚动重启 + 50 key per-key alias + 50 her `model=gpt`）。实测目标落 `zerokey-pool-198-link`、非目标走原 acct 池零影响。脚本 `scripts/prod-aliyun-her-zerokey.py`（幂等可回滚）。**容量观察中**（饱和自动溢出到 acct/wangsu，成本可能向 wangsu 漂移）。详见 v2.13 |
| **prod DB-managed + cron** | 待做：prod 池目前仍 config-managed（11 静态块，无 rebalancer 自愈）；如灰度扩面再做 DB-managed 迁移 + prod rebalancer cron |
| 批量驱动 / 流式桥 / P2 埋点 | 待实施（v2.7 P2 + 本方案 Phase 1–6） |

---

*文档版本：2026-06-22 (v2，含架构成熟化反思) · 与 [Agent 桥落地方案](./zerokey-codex-agent-bridge-plan.md) 配套（前者讲单桥协议，本文讲机群+池+多人+回归）*
