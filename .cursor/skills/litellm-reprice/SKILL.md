---
name: litellm-reprice
description: >-
  Re-price LiteLLM models in `k8s/litellm-proxy.yaml` safely and reproducibly.
  Spec-driven (one YAML file → multiply existing cost fields OR add new ones),
  with built-in audit / dry-run / SpendLogs verification scripts. Use when the
  user mentions "调价 / 调整 LiteLLM 价格 / 改 input_cost / 改 output_cost / 涨/降 某模型成本 / chatgpt 加 cost 字段 / 鼓励/限流某模型用 budget 信号 / fallback 价格 / 价格信号 / 价格策略 / claude 涨价 / chatgpt 降价".
  本 skill 仅覆盖 aliyun carher namespace 的 `litellm-config` ConfigMap，不动 198 prod/dev。
metadata:
  openclaw:
    emoji: "💵"
---

# LiteLLM Model Repricing

通过修改 `k8s/litellm-proxy.yaml` 里每条 deployment 的 4 个 cost 字段，调节 LiteLLM 给 virtual key 累计 budget 的速率——是引导用户在多个 model 之间迁移的**唯一价格信号**。

## 何时使用

- 涨某个 model 价（鼓励用户避开 / 防止上游被打满）
- 降某个 model 价（鼓励用户优先用，例如有空闲容量的订阅池）
- 给从来没标 cost 的 model（如 chatgpt-* 共享池）**显式标价**——不标 ≠ 不扣，LiteLLM 会 fallback 到内置 `model_prices.json` 按 list price 全额扣
- 价格策略调整（如 chatgpt pool 加账号后单价同比降）

不适合：
- 198 prod / dev 环境的价格——它们在 `litellm-product` / `litellm-dev` namespace，配置走 DB（`LiteLLM_ProxyModelTable`）不是 yaml，见 `litellm-pro-ops`
- 调 LiteLLM key 的 `max_budget`——见 `litellm-budget-mgmt`

## 关键陷阱（不读会跌坑）

0. **审/汇总报价时必须双扫 `litellm_params` + `model_info`**：本 skill 的 `add` op 默认把 cost 字段插到 `model_info`（chatgpt-* 全池都是这样标价的）；而 `multiply` op 改的是已有字段位置不变。审计脚本若只扫 `litellm_params` 会把 model_info 标价的池子全报"未标价 / None"，反过来只扫 model_info 会漏掉手写在 litellm_params 的条目。审计 / 截图前必须 `val = p.get(k) or mi.get(k)` 两位置取并集。运行时 LiteLLM 自动把 litellm_params 镜像到 model_info，所以两边都"对"，但静态扫只看一边一定漏。2026-06-29 阿里云 carher chatgpt-acct × 30 行误判"未标价"实证。详见 `feedback_litellm_pricing_dual_location_scan.md` / `feedback_litellm_pro_pricing_via_litellm_params.md`（DB-registered 模型反过来必须 litellm_params，是 special case）。

1. **`SpendLogs.model` 带 provider 前缀**：yaml 里写 `model_name: chatgpt-gpt-5.5`，但 SpendLogs 真实行是 `openai/chatgpt-gpt-5.5`。查询用 `model LIKE '%X%'` 或先 `SELECT DISTINCT model FROM "LiteLLM_SpendLogs" WHERE "startTime" > NOW() - INTERVAL '1 hour'`，否则会漏所有流量再下错结论（2026-05-20 实战教训）。
2. **cache_read_input_token_cost / cache_creation_input_token_cost 也是真 cost 字段**——它们决定 Anthropic prompt cache 命中部分的折扣价。重定价时漏掉这两个，cache 折扣比例会失真。本 skill 默认 4 字段同步动。
3. **多 model 共享同价数值很常见**（Opus input 0.000005 跟 Haiku output 0.000005 同值，Sonnet 跟 gpt-5.3-codex 同 0.000003/0.000015）。**禁止 sed/replace_all**，必须按 model_name context 逐行处理——本 skill `litellm-reprice.py` 已实现。
4. **未标 cost ≠ 不扣费**：LiteLLM fallback 到内置 `litellm/model_prices_and_context_window_backup.json`，按 model 名（strip provider prefix 后）匹配 list price 全额计费。chatgpt-pool 2026-05-21 之前的 30 天累计 $1645 spend 全部按这条 fallback 算出来的。
5. **改 ConfigMap 后必须 rollout restart proxy**——容器内 yaml 是 mount 来的，但 LiteLLM 启动时一次性读入内存，不监听变化。`scripts/litellm-reprice-deploy.sh` 已包含 rollout + 300s timeout。

## Workflow（7 步）

### Step 1: Baseline 审计

```bash
scripts/litellm-reprice-audit.sh > /tmp/reprice-before.txt
cat /tmp/reprice-before.txt
```

输出表格：所有 model_name → in/out/cache_read/cache_create per 1M USD。另外会列出 chatgpt-\* 中**无显式 cost** 的 deployment（fallback 风险）。

### Step 2: 写 spec.yaml

参考 `scripts/litellm-reprice-spec.example.yaml`（reproduce 上次 commit 2ef1ae6 的改动）。

两种 op：

| op | 用途 | 示例 |
|----|------|------|
| `multiply` | 已有 cost 字段乘倍数 | Claude ×2 |
| `add` | 给没有 cost 字段的 model 新增 | chatgpt-* 首次标价 |

字段：
- `match`: 正则匹配 model_name（`(?i)` 不区分大小写；锚定用 `^...$`）
- `factor` (multiply): 倍数（2.0 = ×2，0.5 = ÷2）
- `fields` (multiply, 可省): 字段白名单，默认 4 个 cost 字段全动
- `values` (add): 新字段名→值的字符串映射
- `insert_after` (add, 可省): anchor 行字段名，默认 `api_key`（chatgpt-* 块的最后一行 litellm_params）

**add op 不查重**：跑两次会插两遍。第一次新增完，后续调整应用 multiply 而不是再 add。

### Step 3: dry-run

```bash
python3 scripts/litellm-reprice.py spec.yaml --dry-run
```

输出 multiply / add 命中条数 + 每条规则的命中数。规则没匹配上的会显示 0——通常意味着正则写错。

### Step 4: 实际改 + diff 审

```bash
python3 scripts/litellm-reprice.py spec.yaml
git diff k8s/litellm-proxy.yaml | less
```

人工检查：
- 预期 model 的 cost 行**都**变了（4 字段全在）
- **非**预期 model 完全未动（重点看跟 Claude 共享数值的 gpt-5.4/5.5 wangsu/快汇 行）
- chatgpt-* add 后 yaml 缩进对齐 `api_key` 同列

### Step 5: Apply + rollout

```bash
scripts/litellm-reprice-deploy.sh
```

包含：syntax check → scp k8s-work-226 → md5 verify → `kubectl diff` → `kubectl apply` → `rollout restart` → wait status (300s) → list pods。

300s 通常够双副本零中断 rollout 完成（first replica ~80s ready，second ~150s 老 pod terminate）。timeout 后再手动 `kubectl rollout status` 确认即可，不算失败。

### Step 6: SpendLogs 实证验证

```bash
# 给等 2-3 分钟让真实流量过几条，再跑
scripts/litellm-reprice-verify.sh '%claude-sonnet%'
scripts/litellm-reprice-verify.sh 'openai/chatgpt-gpt-5.5'
```

输出 `before` / `after` 两行，重点对比 `per_M_in` 列——比值应该等于你预期的 factor（multiply）或 expected/old fallback 比（add）。

**没命中**的原因通常：
- 这个 model 在 ±30 分钟窗口内没真实流量 → 等更久 / 换个高频 model
- pattern 写错了 → 跑 `SELECT DISTINCT model FROM "LiteLLM_SpendLogs"` 看真实 model 名再调

### Step 7: Commit + memory 沉淀

参考 commit 2ef1ae6 的消息结构：

```
feat(litellm): reprice <what> by <factor>

<why business-level>

<which model groups / channels touched, with counts>

Verified post-rollout via SpendLogs: <model> per-token spend dropped/
rose to ~<ratio>x of pre-rollout.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

更新或新增 `project_litellm_pricing_*.md` memory 记录当前价格策略（覆盖式，不要堆历史）。

## 回滚（≤2 分钟）

```bash
git revert <commit>
scripts/litellm-reprice-deploy.sh
```

ConfigMap 是数据，回滚不会丢任何 K8s 资源，只是把 cost 字段还原。已经发生的 SpendLogs 历史不受影响（spend 是请求当时算的，不追溯）。

## 实战 baseline（2026-05-21, commit 2ef1ae6）

| Spec rule | hits |
|-----------|------|
| Claude (Opus/Sonnet/Haiku) ×2 across all channels | 124 cost lines |
| chatgpt-gpt-5.5 add ($1/$6 per 1M) | 5 deployments × 2 fields = 10 lines |
| chatgpt-gpt-5.4 add ($0.5/$3) | 10 lines |
| chatgpt-gpt-5.3-codex add ($0.6/$3) | 10 lines |
| chatgpt-gpt-5.3-codex-spark add ($0.6/$3) | 10 lines |

post-rollout SpendLogs 实测：
- `openai/chatgpt-gpt-5.5`: per_M_in 4.21 → 0.86 = **0.20x** ✓
- `anthropic/anthropic.claude-sonnet-4-6`: 反推 ×2 后 spend formula 匹配实际 ±5% ✓

业务背景：ChatGPT Pro 池后端是 5 个固定 $200/月订阅账号，token 计费是虚拟的；这次调价是为了在 budget 维度让 chatgpt 比 Claude 便宜 10×，推动用户撞顶后迁到订阅池（节省真实美元成本）。max_budget 不动，靠撞顶自然推动迁移。

## 关键文件

- `scripts/litellm-reprice.py` — spec → yaml 改写引擎
- `scripts/litellm-reprice-audit.sh` — 价格表 baseline
- `scripts/litellm-reprice-verify.sh` — SpendLogs pre/post 比对
- `scripts/litellm-reprice-deploy.sh` — scp + apply + rollout
- `scripts/litellm-reprice-spec.example.yaml` — 上次实战的完整 spec（runnable reference）
- 相关 memory: `feedback_spendlogs_model_has_provider_prefix.md`、`project_litellm_pricing_2026_05_21.md`
- 相关 skill: `litellm-ops`（proxy 运维总入口，本 skill 是它的"价格"子流程）
