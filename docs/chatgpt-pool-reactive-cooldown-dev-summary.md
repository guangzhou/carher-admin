# Reactive cooldown POC — dev 改动总结（2026-06-28）

> **状态**：T1 dev 全过。**HOLD：未升 T2 canary，未升 T3 prod**。
> 用户指令"全程自主控制 + 只在 dev 上回归" → 走完 Phase 4a/4b 即停。

---

## 1. 改动文件清单（仓内）

| 文件 | 类型 | 作用 |
|---|---|---|
| `docs/chatgpt-pool-reactive-cooldown-plan.md` | new | 完整方案 + 9 段形态矩阵 + 10 类 TC |
| `scripts/litellm-dev-reactive-cooldown-config.py` | new | dev `litellm-config` ConfigMap + `LiteLLM_Config.router_settings` 写入 reactive cooldown 字段 + 注册 `mock-pool-gpt-5.5`（5 entry 接 `mock-chatgpt-upstream`），有 `--apply`/`--restore` 双向 |
| `scripts/litellm-dev-reactive-cooldown-verify.py` | new | T1 回归套件：TC-A / TC-H1 / TC-E1-429 / TC-E1-500 / TC-E5 |
| `scripts/litellm-dev-reactive-cooldown-stress.py` | new | T1 5min/20 worker 混合故障压测 + 内存/cooldown/restart 快照 |
| `docs/chatgpt-pool-reactive-cooldown-dev-summary.md` | new | 本文档 |

**未动任何 prod 文件**：`k8s/litellm-proxy.yaml` / `k8s/litellm-proxy-canary-config.yaml` 等不在本次改动里（之前他人已 modified，与本工作无关）。

---

## 2. 远端 dev ns（`litellm-dev`）改动

### 2.1 ConfigMap `litellm-dev/litellm-config`
追加 `model_list` 5 条 `mock-pool-gpt-5.5` deployment（id = `mock-pool/mock-1..mock-5`），后端走 `http://mock-chatgpt-upstream.litellm-dev.svc.cluster.local:4101/v1`，`mode: responses`。

### 2.2 DB `LiteLLM_Config.router_settings`
注入：
```yaml
allowed_fails: 1        # 之前可能没有 / 默认更高
cooldown_time: 3600     # 之前可能更短
```
（其他 router_settings 字段不动：optional_pre_call_checks、fallbacks 等保留。）

### 2.3 备份（198 上）
- `/root/litellm-dev/litellm-config.cm.bak-rc-20260628-{012719,013232,015717}.json`
- `/root/litellm-dev/litellm-config.db-router_settings.bak-rc-20260628-{012719,013232,015717}.json`

回滚命令：
```bash
python3 scripts/litellm-dev-reactive-cooldown-config.py \
  --restore /root/litellm-dev/litellm-config.cm.bak-rc-20260628-015717.json
# 同样 --restore 会识别 db_router_settings 备份格式自动还原 DB
```

### 2.4 Prod ns（`litellm-product`）
**零改动**。已通过 `kubectl get cm -n litellm-product litellm-config -o yaml | grep -c mock-pool = 0` 验证。

---

## 3. 验证结果（T1 dev）

### 3.1 TC-A 路由可见性
`/v1/model/info` 返回 5 条 `mock-pool-gpt-5.5` entry，与 ConfigMap 完全一致。**PASS**

### 3.2 TC-H1 Happy path
25 次连续调用全 200。受 `deployment_affinity` 影响 25 次全落 `mock-1` 单 deployment（**这是 prod 期望的 sticky 行为**，本 POC 不挑战之）。**PASS**

### 3.3 TC-E1-429 / TC-E1-500 reactive cooldown 触发
**核心断言**：`allowed_fails=1` 下，每个 deployment 一次失败立即进 cooldown。
- 5 mock 全注入 429/500 → 每次 call 前 flush affinity，4 次 call 内 cooldown set 从 ∅ 增至 {mock-1..mock-5}。
- 增长序列示例（429）：
  ```
  iter=0 status=429 dep=mock-4 +cd=[mock-5]
  iter=1 status=429 dep=mock-3 +cd=[mock-3, mock-4]
  iter=2 status=429 dep=mock-2 +cd=[mock-1]
  iter=3 status=429 dep=mock-2 +cd=[mock-2]
  → final {mock-1..mock-5}
  ```
- Redis key 形式确认：`deployment:<id>:cooldown`（与 LiteLLM v1.89.4 实现一致）
- **PASS**（429/500 各一）

### 3.4 TC-E5 整组耗尽
5 mock 全 fault=429 → 连续 30 call 全 429，0 个 200。整组进入"无可用 deployment"状态，符合 reactive cooldown 设计：宁可拒服务也不打已知坏 deployment（上层应靠 fallback 接住）。**PASS**

### 3.5 Phase 4b 5min/20-worker 混合故障压测
| 指标 | before | after | delta |
|---|---|---|---|
| proxy mem (sum across pods) | 1014Mi | 1077Mi | **+63Mi** |
| redis used | 1234KiB | 1291KiB | **+57KiB** |
| cooldown_count | 0 | 4 | +4（稳态轮转） |
| proxy restartCount | 0 | 0 | **0** |
| ERROR/CRITICAL（去 mock 故障噪声后）| - | 0 | **0** |

请求分布：3580 total / 200=147 / 429=3429 / 500=4 / 0 网络错=0。  
高 429 比例是预期：故障注入概率 20%，5 mock 全程几乎都至少有几个 cooldown 中，整组吞吐被压到健康 deployment 数。

外推 prod（5h/7d quota 撞顶 = 持续性故障，**不**会像测试这样高频抖动）：
- cooldown set 触发为单向（撞顶 → 锁 1h）→ Redis 增量 ≤ 撞顶 deployment 数 × ~50B
- proxy mem 增量微不足道（POC 在故障风暴下仅 +63Mi）

---

## 4. 已知不确定 / T2 升级前要验的事

| # | 项 | 风险 | T2 验证手段 |
|---|---|---|---|
| 1 | prod ChatGPT provider 撞 5h/7d quota 真实返回是 `429` 还是 `200+usage={}` 还是 `200+rate_limit.*.used_percent>=100` | LiteLLM 内置 cooldown 只识别 `RateLimitError`，后两种形态走 [[project_litellm_198_pro_capacity_patch]] 已叠的 capacity-patch 升格 | T2 canary 用真撞顶 acct（[[feedback_chatgpt_acct_probe_without_pod_via_188_tmp]] 已确认 acct-26..30/37/40 撞顶可控）|
| 2 | `cooldown_time=3600` 对 5h quota OK，对 7d quota 不够长 | 7d 撞顶的 acct 1h 后会被重新放回路由池，触发再次失败 + 再次 cooldown → 1h 一波抖动 | 改 `cooldown_time` 走 `reset_at`-aware callback（plan §6）；或保守 24h |
| 3 | `allowed_fails=1` 是否对正常瞬时网络抖动过激 | 单次 timeout 就锁 1h，可能误伤健康 acct | T2 看 fallback 量 + 用户面 5xx 率，<1.2× baseline 才升 T3 |
| 4 | T1 mock 流量不涉及真 fallback chain（mock-pool 没配 fallbacks）| reactive cooldown 触发后 prod 真 chain `chatgpt-pool-gpt-5.5 → wangsu-gpt-5.5 → ...` 行为未验证 | T2 真链路烧 1h，看 `x-litellm-attempted-fallbacks` |
| 5 | dev mock 走 `openai/` provider，prod chatgpt-acct 走 `chatgpt/` provider | transformation.py 路径不同 → 错误识别代码路径不同 | T2 必须用真 chatgpt-acct 而非 mock |

---

## 5. 后续动作（**用户决策门**）

> 用户指令明确不让我自己升 T2/T3。本文档作为 hand-off：

**升 T2 canary** 需要的前提：
1. 用户 OK 在 prod ns 创建/启用 `litellm-proxy-canary` 独立 svc（如未存在）
2. 用户挑 1-2 个测试 key 显式打 canary endpoint（不切 prod 用户流量）
3. 准备 1-2 个真撞顶 acct（来源：上次 7d=100% 的 acct-26..30）作为 cooldown 触发源
4. Prometheus 配 cooldown_count / fallback_count delta 面板

**升 T3 prod** 需要的前提：
1. T2 canary 1h 真撞顶验证 PASS（5 指标全过）
2. ConfigMap diff（T1 dev ↔ canary）业务字段全等
3. Image rebuild + ACR VPC 推送（如有 patch；本 POC **配置-only** 不需要新 image）

---

## 6. 一句话结论

POC 在 dev 证明：**LiteLLM v1.89.4 内置 `allowed_fails=1 + cooldown_time=3600` 可在单次失败后立即把该 deployment 从可路由池摘掉，5min 高并发故障注入下 proxy 0 restart / mem 增 6%**。下一步是 T2 用真 chatgpt-acct 验证错误识别路径（5h/7d 撞顶的具体响应形态）—— **等用户决策**。
