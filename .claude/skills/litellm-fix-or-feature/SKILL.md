---
name: litellm-fix-or-feature
description: 198 prod LiteLLM 池修 bug / 加新功能的完整 SOP。Tier 0/1/2/3 分桌（本地复现 → dev 回归 → canary 真流量 → prod 24h 长稳），每 Tier 有"能做什么 / 严禁做什么"动作矩阵。Bug 必须 T0 复现才能动 patch；改动按"配置 > callback > patch"偏好定型；副作用 4 问任一答不上回 Phase 1；ConfigMap diff 不等不准升 T2。整合三源对齐（CM/DB/router 内存）、callback 不可见陷阱、fallback 静默、alias inflation、双引擎同步等 LiteLLM 独有坑。
---

# LiteLLM 修 bug / 加新功能 SOP

> 198 prod LiteLLM 池（litellm-product ns，v1.89.x vanilla + 累积 patch）专用。从用户报障到 GA 24h 长稳的完整闭环。

## 适用范围

- ✅ LiteLLM router/proxy 行为修复（cooldown/fallback/retry/timeout）
- ✅ provider transformation patch（chatgpt/openai/wangsu/openrouter 等）
- ✅ custom callback / CustomLogger / CustomRoutingStrategy 新增
- ✅ admin API DB 改动（ProxyModelTable / VerificationToken aliases 等）
- ✅ ConfigMap router_settings / model_list 改动
- ❌ 不在范围：LiteLLM 大版本升级（v1.89→v2.x），走 ad-hoc 不走 SKILL（每次升级断点不同）
- ❌ 不在范围：上游 chatgpt-acct 增删（走 `chatgpt-quota-rebalance` SKILL）

## 核心纪律（违反任一立即停手）

1. **Bug 必须 T0 复现才能动 patch**。复现不出来 = 假根因。[[feedback_dont_fill_silence_with_fake_progress]]
2. **T0→T1→T2→T3 严格分桌不可跳**。每个 Tier 有"能做 / 严禁"动作矩阵。
3. **三源对齐**（ConfigMap / DB ProxyModelTable / router 内存）是 Phase 0 前置。不齐先齐。
4. **副作用 4 问**任一答不上 → 回 Phase 1。
5. **改动偏好**：配置 > callback > patch（生效快 / 回滚便宜 / 影响面小）。
6. **canary 通过 ≠ 全集群跑过**。SKILL 必双态：tested-on-canary vs executed-cluster-wide。[[feedback_carher_converge_cluster_wide_2026_06_26]]

---

## 环境分层

```
┌──────────────────────────────────────────────────────────┐
│ Tier 0: 本地复现 (198:/root/litellm-repro-<feature>/)     │
│   docker-compose 完全隔离，可任意破坏                       │
│   用途：bug 稳定复现 + patch 是否真生效                     │
├──────────────────────────────────────────────────────────┤
│ Tier 1: Dev (litellm-dev ns)                              │
│   独立 ConfigMap/Redis/DB/假 master key，真接 1-2 路 upstream│
│   既有：scripts/litellm-dev-gpt-products-{config,run,verify}│
│   用途：完整回归套件 + 副作用排查 + 1h 压测                  │
├──────────────────────────────────────────────────────────┤
│ Tier 2: Canary (litellm-proxy-canary @ litellm-product ns)│
│   共享 prod ConfigMap-canary/Redis/DB；独立 svc            │
│   测试 key 显式打 canary，不切 prod 用户流量                │
│   用途：真实上游 + 真撞顶 acct + 指标偏差观察              │
├──────────────────────────────────────────────────────────┤
│ Tier 3: Prod (litellm-proxy 主 4 pod)                    │
│   全量 / 24h 长稳                                          │
└──────────────────────────────────────────────────────────┘
```

### 动作矩阵

| 动作 | T0 | T1 | T2 | T3 |
|---|---|---|---|---|
| Mock 上游 | ✅主战场 | ✅ | ❌污染 prod | ❌ |
| scale=0 真 acct | ✅自起 | ✅ | ⚠️仅非 prod-active acct | ❌ |
| 全池 cooldown 注入 | ✅ | ✅ | ❌共享 Redis | ❌ |
| 真撞顶 acct 测 429 | ❌没真号 | ⚠️需真 key | ✅主战场 | ✅监控 |
| flush_cooldown / FLUSHDB | ✅ | ✅ | ❌严禁 | ❌ |
| 改 router_settings 配置 | ✅ | ✅先来 | ✅真流量 | ✅promote |
| 上线 patch image | ✅ | ✅先来 | ✅真流量 | ✅promote |
| 24h 长稳 | ❌ | ⚠️流量小 | ⚠️ | ✅ |

### 升 Tier 三条铁律

详见底部"通用 Stop 信号"表（更全）。这里只列入门门槛：T0 复现不出不进 T1；T1 P0 fail 不进 T2；T1→T2 升级前必须 ConfigMap diff。

---

## Phase 0：现状确认（≤ 30min，必跑）

### 0.1 三源对齐

| 源 | 命令 | 作用 |
|---|---|---|
| ConfigMap `litellm-config` | `kubectl -n litellm-product get cm litellm-config -o yaml` | 静态配置 |
| DB `LiteLLM_ProxyModelTable` | `kubectl exec -n litellm-product <db-pod> -- psql -c 'select model_name,model_id from "LiteLLM_ProxyModelTable"'` | admin `/model/new` 动态注册 |
| router 内存 | `curl http://litellm-proxy.litellm-product:4000/v1/model/info` | 真路由用的 |

**三源不一致 → 先对齐再开干**。[[feedback_litellm_router_alias_inflation]]：`/model/info` alias 源/目标都展示一次，行数 ≠ 真 deployment 数。

### 0.2 版本/patch 清单

```bash
kubectl -n litellm-product get deploy litellm-proxy \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
# 期望：vanilla-vX.Y.Z.<patch-name>-<timestamp>
```

记录已叠 patch（capacity-patch / compaction-drop / degrade-strip / 其他）。

### 0.3 上游真实行为对齐

**不要凭记忆描述上游**，先抓真 response：

```bash
# 直连上游（绕 LiteLLM）
kubectl exec -n carher chatgpt-acct-N-... -- \
  curl -i https://chatgpt.com/backend-api/codex/responses ...
```

### 0.4 影响面清单

```bash
# 哪些 key 走这条路径
kubectl exec -n litellm-product <db-pod> -- psql -c "
  select count(*) from \"LiteLLM_VerificationToken\"
  where models @> '...' or aliases::text like '%...%'
"
```

[[feedback_litellm_models_column_text_array]]：跨集群 schema 不同，先 `SELECT pg_typeof(models)` 实测。

**产物**：0.x 全勾的现状清单。**没勾完不进 Phase 1**。

---

## Phase 1：根因诊断（bug 修复必跑，新功能跳到 Phase 2）

### 1.1 三段式（CLAUDE.md 硬规则）

每个"X 导致 Y"必须展开：
1. **假设**：明文写出 X→Y
2. **证伪条件**：如果假设错，数据应该长什么样
3. **数据**：实际数据落在哪

数据与假设矛盾 → 抛弃假设重起。**禁止事后兜底**。

### 1.2 老 patch 审计

提新 patch 前先审已有的。

```bash
kubectl exec -n litellm-product <proxy-pod> -- grep -rn 'def transform_response\|def _normalize_item' \
  /usr/lib/python3*/site-packages/litellm/llms/chatgpt/
```

每个已有 patch 三段式确认是否仍成立。**老 patch 失效 ≠ 加新 patch**。

### 1.3 错误形态矩阵

画一张表（见 `docs/chatgpt-pool-reactive-cooldown-plan.md` §9.1 示例）：

| 上游返回 | LiteLLM 识别 | router 行为 | 处理 | 兜底 |

典型踩坑：
- [[feedback_litellm_alias_ok_but_wangsu_check_tunnel]] — fallback 把 tunnel 挂当配置错
- [[feedback_chatgpt_dead_end_judge_by_allowed_not_pct]] — 多维数据压一维标签
- [[feedback_dont_collapse_multidim_data_into_one_bucket]]

### 1.4 callback 可见性陷阱

[[feedback_litellm_callback_print_invisible_in_workers]]：`num_workers≥2` worker stdout 不 attach 父进程。

**信号必须 `verbose_router_logger`，不是 `print`**。

**产物**：根因 + 形态矩阵 + 老 patch 审计。**无此文档不进 Phase 2**。

---

## Phase 2：改动定型（关键决策门）

### 2.1 改动类型决策树

```
要改的行为是 router 内置已有的？（cooldown/fallback/timeout/retry）
├─ 是 → router_settings 配置（最便宜，无 patch）
│
└─ 否，是 provider 层请求/响应改写？
   ├─ 改 request → callback (pre_call_hook)
   │   ⚠️ async hook firing 不可靠 issue #8842，避免响应路径
   ├─ 改 response 识别（升格异常）→ transformation.py patch（必须 image rebuild）
   ├─ 改路由策略 → CustomRoutingStrategyBase 子类
   └─ 改 DB schema / admin API
       ⚠️ [[feedback_litellm_models_column_text_array]]：跨集群 schema 不同
       ⚠️ [[feedback_litellm_model_new_no_api_key_field]]：v1.89 字段差异
```

### 2.2 改动 vs 副作用对照

| 维度 | 配置 | callback | patch |
|---|---|---|---|
| 生效 | reloader 5s 热加载 | 同 ConfigMap | image rebuild + rollout restart |
| 回滚 | 改回值 | 删注册段 | image 切回 |
| 影响范围 | 全 router | 注册段 | provider 全部流量 |
| 风险 | 低 | 中 | 高 |

**默认偏好**：配置 > callback > patch。

### 2.3 副作用 4 问

1. 影响除当前 use case 外的哪些 key/model_group？
2. 同 transformation.py 跨 provider 共享的有谁？（openai/chatgpt/azure 经常共用）
3. fallback chain 上的 deployment 行为是否被连带改变？
4. cron / monitor / batch 脚本（quota-rebalance、metrics 等）是否因 router 行为变化跑偏？

参考踩坑：
- [[feedback_model_group_alias_for_db_pools]]：alias 一行影响整池
- [[feedback_litellm_fallback_bypasses_allowlist]]：fallback target 不进 allowlist 但能被路由
- [[feedback_pool_accounts_must_align_with_tmp_auth]]：脚本侧/router 侧必须同步
- [[feedback_dual_engine_legacy_model_names_in_pvc]]：openclaw ConfigMap ≠ hermes PVC

**4 问任一答不上 → 回 Phase 1。**

**产物**：改动类型 + 决策依据 + 副作用清单。

---

## Phase 3a：T0 本地复现（bug 必跑，新功能跳）

**目的**：在不接 prod 的环境**稳定复现** bug。**复现不出来不准动 patch**。

```bash
# T0 默认在 198 上（能直连 chatgpt.com 真上游回放）
ssh 198
mkdir /root/litellm-repro-<feature>
cd /root/litellm-repro-<feature>

cat > docker-compose.yml <<'EOF'
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-v1.89.4   # 同 prod 基础版本
    ports: ["4000:4000"]
    volumes:
      - ./config.yaml:/app/config.yaml
    command: --config /app/config.yaml --port 4000
  redis:
    image: redis:7
  mock-upstream:
    image: python:3.11-slim
    volumes:
      - ./mock-chatgpt.py:/mock.py   # 长期版在 carher-admin/scripts/litellm-mock/mock-chatgpt.py，首跑前 scp 到 198
    command: python /mock.py swallow
    ports: ["9999:9999"]
EOF

docker compose up -d

# 重放触发请求
curl http://localhost:4000/v1/responses ...
```

**断言**：3 次/3 次稳定观察到与 prod 同形态错误。

Mock server 长期维护位置：`scripts/litellm-mock/mock-chatgpt.py`（5 mode：swallow/unauthorized/context_cap/fivehundred/forward_compat）。

---

## Phase 3b：T0 patch 本地验证（patch/callback 类必跑）

```bash
# 同 compose 加 transformation.py mount
volumes:
  - ./transformation.py:/usr/lib/python3.X/site-packages/litellm/llms/chatgpt/responses/transformation.py

docker compose restart litellm
# 重跑 3a 触发，验证 bug 修了
```

⚠️ **T0 mount 单文件方便迭代，但最终交付必须是 image rebuild**。  
[[project_litellm_198_pro_capacity_patch]]：dev 用 prod 文件覆盖会 ModuleNotFoundError，必须从 vanilla 整 build。

---

## Phase 4a：T1 dev 完整回归（必跑）

```bash
# 既有 dev 环境复用：
cd /Users/Liuguoxian/codes/carher-admin
./scripts/litellm-dev-gpt-products-run.sh        # 部署改动到 litellm-dev ns
./scripts/litellm-dev-gpt-products-verify.py     # 既有回归（产品名维度）

# 本次改动专属回归套件（参考 docs/chatgpt-pool-reactive-cooldown-plan.md §10）：
export NS=litellm-dev
#   Happy ≥ 2
#   异常流 ≥ 5（§1.3 形态矩阵每行一个）
#   边界 ≥ 2
#   性能 ≥ 1
```

### 4a.1 回归套件最低标准

每个 TC 必须：**前置 → 触发命令（可拷贝跑）→ 断言（命令不是描述）→ 清理**。

工具坑预防（[[feedback_smoke_tool_pitfalls]]）：
- `kubectl exec` 多容器选错 → 加 `-c <container>`
- proxy pod 没 curl → 借 her bot pod 或 mock server
- HTTP/1.1 100 Continue 行误判 → curl 加 `-H "Expect:"`
- cache TTL UPDATE ≠ 立即生效 → until-loop 等 200
- macOS bash 3.2 `${arr[@]:i:n}` 静默失效 → for-seq 写法
- jms ssh `|| true` 撞 set -e → polling 段 `set +e`

### 4a.2 DRY_RUN 不算数

[[feedback_dry_run_doesnt_catch_real_db_conflict]] + [[feedback_test_must_rollback_router_state]]：DRY_RUN 不真打 admin API，DB schema / cache 类只在 real run 暴露。**回归必须真打 + 测后回滚 router state 到原数**。

**断言**：H 全过 + E ≥ 80% + B 全过 + P1 过。**fail 任一 P0 → 回 Phase 1**。

---

## Phase 4b：T1 短压测 / 副作用

```bash
# dev 流量小，跑 1h 压测外推
hey -z 1h -c 10 -m POST -H "Authorization: Bearer $DEV_KEY" \
  -d '{"model":"chatgpt-gpt-5.5","input":"x"}' \
  http://litellm-dev.litellm-dev.svc:4000/v1/responses

# 看曲线：proxy 内存 / dev redis 内存 / cooldown_count / error_count
# 外推到 prod 量级估算 24h 资源消耗
```

**产物**：1h 压测报告 + 资源消耗外推。

---

## Phase 5a：T2 canary 部署

### 5a.1 Image 走 ACR VPC

[[CLAUDE.md K8s 镜像拉取规则]]：T0/T1 image 通常在 dev registry，必须重 tag 推 prod ACR。

```bash
# 构建必须在 47.84.112.136 上 nerdctl
ssh 47.84.112.136
cd /path/to/litellm-build
nerdctl build -t litellm-proxy:vanilla-v1.89.4.<feature>-<ts> .
nerdctl tag litellm-proxy:vanilla-v1.89.4.<feature>-<ts> \
  cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/litellm-proxy:vanilla-v1.89.4.<feature>-<ts>
nerdctl push cltx-her-ck-registry-vpc.../litellm-proxy:vanilla-v1.89.4.<feature>-<ts>

# 部署到 canary
kubectl -n litellm-product set image deploy/litellm-proxy-canary \
  litellm-proxy=cltx-her-ck-registry-vpc.../litellm-proxy:vanilla-v1.89.4.<feature>-<ts>
```

### 5a.2 ConfigMap diff 校验

```bash
# T1 与 canary 业务字段必须全等（除环境特定段：redis/db/upstream/key）
diff \
  <(kubectl -n litellm-dev get cm litellm-config -o yaml | yq .data) \
  <(kubectl -n litellm-product get cm litellm-config-canary -o yaml | yq .data)
```

不一致 = T2 验证的不是 T1 验证过的东西，**禁止升 Tier**。

### 5a.3 流量策略

**canary 独立 svc，测试 key 显式打 canary endpoint**：
- prod 用户 → `litellm-proxy.litellm-product.svc:4000`
- 测试 key → `litellm-proxy-canary.litellm-product.svc:4000`

不切 prod 用户流量。让 5b 验证矩阵能清晰归因（出错只可能是测试 key 的请求，不是 prod 用户）。

---

## Phase 5b：T2 真实流量验证

### 5b.1 验证矩阵

| 检查项 | 来源 | 阈值 |
|---|---|---|
| canary error rate | prometheus | ≤ prod baseline × 1.2 |
| cooldown_count / fallback_count delta | prometheus | 符合预期方向 |
| cron.log 异常 | `tail 188:/home/cltx/.chatgpt-quota/cron.log` | 无新增 ERROR |
| 用户面 smoke | 真测试 key 真跑 | HTTP 200 + 上下文正确 |
| 飞书告警 | webhook | 0 |

### 5b.2 跨 deployment 副作用回归

`X-Litellm-Specific-Deployment` header 强 pin 到具体 acct 跑回归，绕开 router 随机选 acct 的不确定性（learn from `docs/chatgpt-pool-reactive-cooldown-plan.md` §10.0.1 `trace_pinned`）。

### 5b.3 失败回滚（≤ 5min）

| 类型 | 回滚 |
|---|---|
| 配置 | `git revert + kubectl apply`，reloader 5s 热加载 |
| Image | `kubectl set image deploy/litellm-proxy-canary X=<old-tag>` |
| Callback | ConfigMap 删 `callbacks` 段 + rollout restart |
| Admin API | `/model/delete` + 反向 `/model/new` |

**产物**：canary 1h 报告（指标 + TC PASS 表）+ 回滚演练记录。

---

## Phase 6a：T3 promote

```bash
# 复用 canary 已验证的 image tag 到 prod deploy
kubectl -n litellm-product set image deploy/litellm-proxy \
  litellm-proxy=<canary-tag-verified>
kubectl rollout status -n litellm-product deploy/litellm-proxy --timeout=300s
```

[[feedback_kubectl_rollout_empty_arg_restarts_all]]：批量脚本必带 `set -euo pipefail` + 文件断言 + count echo。

[[CLAUDE.md 零中断]]：禁止手动 `kubectl delete pod` 正在服务的 Pod，依赖 Deployment 滚动更新。

---

## Phase 6b：T3 24h 长稳

| 指标 | 阈值 |
|---|---|
| 用户面 5xx/4xx 率 | ≤ 上线前 1.2× |
| wangsu/openrouter fallback 量 | 符合预期方向 |
| cron.log | 无死循环 |
| 飞书告警 | 0 |
| Redis 内存增量 | ≤ T1 外推估算 + 20% buffer |

### 双引擎 / 多集群一致性

[[feedback_dual_engine_legacy_model_names_in_pvc]]：openclaw ConfigMap 改 ≠ hermes PVC 改。  
涉及产品名/路由的改动必须同步 [[carher-litellm-product-name-converge]] §"hermes PVC 5 文件"。

**产物**：24h 报告。

---

## Phase 7：文档化 / 沉淀

| 产物 | 位置 |
|---|---|
| 方案文档 | `docs/<feature>.md` |
| 回归套件 | docs/ §回归 或 `scripts/litellm-<feature>/` |
| Mock server | `scripts/litellm-mock/mock-chatgpt.py`（共享长期维护） |
| Memory | `~/.claude/projects/.../memory/<topic>.md` |
| SKILL | 本 SKILL（流程） + 专题 SKILL（如 `litellm-key-provider-swap`） |

Memory 纪律：feedback 含 **Why** + **How to apply**；project 用绝对日期；索引行 < 200 chars；`[[name]]` 互链。

---

## 通用 Stop 信号（任意 Phase 触发即停）

| 信号 | 含义 | 行动 |
|---|---|---|
| 0.1 三源不一致 | router-drift | 修对齐再开干 |
| 1.3 形态矩阵漏行 | 根因没看全 | 补抓数据 |
| 2.3 副作用 4 问任一答不上 | 不懂自己在改什么 | 回 Phase 1 |
| 3a 复现失败 ≥ 3/3 | 假根因 / 触发条件错 | 回 Phase 1 |
| 4a 任一 P0 fail | 改动有副作用 | 回 Phase 2 |
| 4b 资源外推 > prod 容量 | 不可上线 | 回 Phase 2 |
| 5a ConfigMap diff 业务字段不等 | T2 验证物 ≠ T1 验证物 | 回 T1 |
| 5b prometheus 反向 | canary 把指标搞坏了 | 立刻回滚 |
| 6b 24h 报告异常 | 没稳定 | 不算 GA |

---

## 镜像命名规范（强制）

`vanilla-vX.Y.Z.<feature>-<timestamp>`

- `vX.Y.Z`：vanilla 基础版本（v1.89.4 等）
- `<feature>`：patch 短名（capacity / compaction-drop / cooldown-promote 等）
- `<timestamp>`：`YYYYMMDD-HHMMSS`

**不合规拒绝 promote 到 T3**。出问题能立刻 `kubectl set image` 回上一稳态。

---

## LiteLLM 独有坑（普通项目没有）

| 坑 | 防 |
|---|---|
| 三源（CM/DB/router 内存）漂移 | Phase 0.1 强制对齐 |
| ConfigMap reloader 5s 热加载，monkey-patch 不 hot-reload | 改 callback/patch 必 rollout restart |
| callback `print` 在 workers 不可见 | 用 `verbose_router_logger` |
| fallback 静默吞错（tunnel 挂表现像配置错）| 看响应头 `x-litellm-attempted-fallbacks` |
| `/model/info` alias inflation | 真行数看 DB ProxyModelTable + SpendLogs.model_group |
| router fallback 绕过 per-key allowlist | fallback target 不进 allowlist 也能被路由 |
| token cache UPDATE 10-60s 延迟 | smoke 用 until-loop |
| 双引擎（openclaw/hermes）PVC 硬编码 | 必同步 [[carher-litellm-product-name-converge]] §hermes PVC |
| DRY_RUN ≠ real run | DB schema / cache 类只在 real run 暴露 |
| 跨集群 schema 不同（jsonb vs text[]）| 写前 `SELECT pg_typeof(<col>)` |

---

## 待运维补完

1. **`scripts/litellm-mock/mock-chatgpt.py`** 长期维护版（5 mode：swallow/unauthorized/context_cap/fivehundred/forward_compat）。本次先放占位，下个 LiteLLM bug 第一次需要时落地。
2. **`litellm-dev` ns 部署说明**：现 `scripts/litellm-dev-gpt-products-*.{py,sh}` 是 dev 环境用法的一个 use case，T1 通用 dev 用法（如何为新 feature 加 1-2 路 upstream）补到本 SKILL 或单独 doc。
3. **canary deploy/svc 实际 manifest**：本 SKILL 假设 `litellm-proxy-canary` deploy + 独立 svc 已经在 prod ns。如未创建，第一次升 T2 时按 prod deploy 复制 + 改 label + 加独立 svc 一次建好。

---

## 一句话总览

Bug：T0 复现 → 三源对齐 → 形态矩阵 → 改动定型（配置/callback/patch）→ T0 验证 patch 真生效 → T1 完整回归 + 1h 压测 → ConfigMap diff → T2 canary 独立 svc + 真撞顶 + 5 指标 → image promote T3 → 24h 长稳。  
新功能：跳过 Phase 1/3a，从 Phase 2 改动定型直接开始，其他流程不变。
