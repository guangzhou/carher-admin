# bge-m3：OpenRouter → 硅基流动 Pro（第 2 步执行单）

第 1 步（`bge-m3-siliconflow-step1-provider.sh`）必须已 PASS 才能进这里。
第 1 步只加凭据不改路由；本步改的是真实流量落点。

## 已核实的前提（2026-10-08，198 pod 内实测）

| 项 | 数据 | 结论 |
|---|---|---|
| 向量一致性 | 同文本跨厂商 cos **0.999927~0.999958**，L2 范数两边都 1.0000 | **索引不用重建**，同权重同归一化 |
| 内部结构偏差 | 10 组 pair 最大 diff **0.0008** | `minScore: 0.25` 阈值上无影响 |
| Pro 档容量 | 60 并发 **184 QPS**，p50 116ms，零 429 | 生产峰值 1.2 QPS（4276/h），余量 150 倍 |
| 维度 | 1024（Pro 与免费档一致） | 与 OpenRouter 相同 |
| 长文本 | 12000 字 / 7581 tokens → 200，1318ms | 覆盖生产 max_tok 4289 |
| `encoding_format` | `base64` 与 `float` 均 200 | LiteLLM 会传 base64 |
| lone surrogate | siliconflow 返回 400 | 已有 `embedding_sanitize.py` pre-call hook 剥除，非新风险 |

## 改动面

**240 把 key 一把都不动。** key 的 `models` 白名单里只有模型名，上游在 model_list。

两个组要改（两组都在用，不能只改一个）：

| 组名 | 在用 key | 定义位置 |
|---|---|---|
| `BAAI/bge-m3` | 218 把 | ConfigMap `config.yaml` |
| `bge-m3` | 22 把 | DB `LiteLLM_ProxyModelTable` 行 `converge/bge-m3` |

### 目标形态（两组都是）

```yaml
# 首选：硅基流动 Pro
- model_name: BAAI/bge-m3
  litellm_params:
    model: openai/Pro/BAAI/bge-m3
    api_key: os.environ/SILICONFLOW_API_KEY
    api_base: https://api.siliconflow.cn/v1
    input_cost_per_token: 0.0000000104   # ¥0.07/M ÷ 6.71
    output_cost_per_token: 0
  model_info:
    id: siliconflow/bge-m3-pro
    mode: embedding
    input_cost_per_token: 0.0000000104
    output_cost_per_token: 0

# 兜底：保留现状那条 OpenRouter（原样不动，仅作为第二 deployment 存在）
- model_name: BAAI/bge-m3
  litellm_params:
    model: openrouter/BAAI/bge-m3
    api_key: os.environ/OPENROUTER_API_KEY
    api_base: https://openrouter.ai/api/v1
    input_cost_per_token: 0.00000001
    output_cost_per_token: 0
  model_info:
    id: openrouter/bge-m3
    mode: embedding
    input_cost_per_token: 0.00000001
    output_cost_per_token: 0
```

顺带补上 198 侧**现在完全没有的 embedding 兜底**：今天 OpenRouter 一挂就是
`404 No fallback model group found for BAAI/bge-m3`，一挂到底。

> 兜底落点与首选是同权重同归一化（cos 0.9999），所以这条兜底链
> **不会**像换成 `text-embedding-v3` 那样污染向量空间。这是选 bge-m3 同族
> 而不是选网宿 `text-embedding-v3` 的理由。

## 🔴 CM 是 immutable + Helm 管理，不能 kubectl edit

```
litellm-product-proxy-config-ffb212f72856   immutable: true
  ← helm release litellm-product-proxy
  ← chart _helpers.tpl: configName = <release>-config-<sha256sum(config.data)|trunc 12>
```

改法只能是：改 config 内容 → 派生新名字的新 CM → `helm upgrade` → Deployment
挂到新 CM → 滚动。与 §7 升级同一条路，但**只改 config 一个维度，不碰 image**。

⚠️ 派生 CM 的名字**不可当内容校验**（见 memory
`feedback_derived_cm_name_is_not_content_sha_after_upgrade`）。
验收一律回读 CM 实际字节 + `wc -c`，不认名字。

## 执行顺序

### 2.1 备份（落 `~/bge-sf-step2-<ts>/`）

```bash
K="sudo kubectl"; NS=litellm-product
$K -n $NS get cm litellm-product-proxy-config-ffb212f72856 -o yaml > cm-prod.before.yaml
$K -n $NS get cm litellm-product-gray-config-447a29d8e8b6  -o yaml > cm-gray.before.yaml
$K -n $NS get deploy litellm-proxy      -o yaml > deploy-prod.before.yaml
$K -n $NS get deploy litellm-proxy-gray -o yaml > deploy-gray.before.yaml
$K -n $NS exec litellm-db-0 -- psql -U litellm -d litellm -tAc \
  "SELECT row_to_json(t) FROM (SELECT * FROM \"LiteLLM_ProxyModelTable\"
    WHERE model_id='converge/bge-m3') t;" > db-converge-bge-m3.before.json
sudo helm -n $NS get values litellm-product-proxy > helm-values-prod.before.yaml
wc -c *.before.*   # 记数，验收时对账
```

### 2.2 gray 先行（prod 完全不动）

只对 `litellm-proxy-gray` 做 helm upgrade + rollout。

### 2.3 gray 验收 —— 4 项，缺一不往下走

1. **落点**：探针打 10 次，`LiteLLM_SpendLogs` 里 `model` 字段 =
   `openai/Pro/BAAI/bge-m3`，不是 `openrouter/*`
   （🔴 只看 HTTP 200 是假绿，必须看 SpendLogs 的真实落点）
2. **维度** = 1024
3. **向量一致性**：与 OpenRouter 旧向量 cos > 0.999（用第 1 步那 5 条中英文样本做对照）
4. **兜底真的活**：把 siliconflow 那条的 `api_base` 临时改成不可达，确认
   自动回落 OpenRouter 且仍返回 1024 维；验完改回
   （🔴 `fallback_reached != fallback_held`，见 memory
   `feedback_fallback_reached_is_not_fallback_held`）

### 2.4 prod 滚动

```bash
helm upgrade litellm-product-proxy <chart.tgz> -n litellm-product \
  --values <prod-target-values.yaml> --reset-values --atomic=false
$K -n $NS rollout status deploy/litellm-proxy --timeout=600s
```

4 副本滚动更新，**零 `kubectl delete pod`**。

### 2.5 DB 行（服务 `bge-m3` 组，22 把 key）

`LiteLLM_ProxyModelTable` 的 `model` / `api_base` / `api_key` 三列是**加密存储**的
（见 memory `feedback_proxymodeltable_model_and_api_base_are_encrypted`），
不能直接 UPDATE 明文。走 `/model/update` API，且注意该端点
**会清掉未传字段**（memory `feedback_litellm_model_update_endpoint_clobbers_fields`）
⇒ 必须 read-merge-write，把现有字段全量回传。

写完要 regress：`ProxyModelTable` 写入会导致 deployment 重路由，需确认生效
（memory `feedback_proxymodeltable_write_reroutes_deployment_needs_restart_regress`）。

### 2.6 回读验收

```sql
-- 新落点出现、旧落点归零
SELECT model, count(*), max("startTime") FROM "LiteLLM_SpendLogs"
WHERE (model ILIKE '%bge%' OR model_group ILIKE '%bge%')
  AND "startTime" > NOW() - INTERVAL '20 minutes'
GROUP BY 1 ORDER BY 2 DESC;
```

- `openai/Pro/BAAI/bge-m3` 有量
- `openrouter/BAAI/bge-m3` 归零（或仅剩兜底零星）
- 抽 2 个 her 实例真发一句「之前跟你提过什么」，确认 memorySearch
  **能搜到旧记忆** —— 这是索引兼容性的终检，比 cos 值更硬

## 风险与已知坑

- **滚动期间在飞的 embedding 会断**：表现为某次 memorySearch 失败一次，
  bot 侧那一句搜不到记忆。当前量 207~4000 次/小时。用户已确认「立即滚」。
- **余额查不到**：`/user/info` 返回 410 已废弃。Pro 档余额用尽会怎样
  （拒绝 / 降级 / 转免费档）**无数据**。OpenRouter 兜底在位，所以余额断了
  不至于全挂，但会静默切回 OpenRouter 花钱 —— 上线后看一次 SpendLogs
  落点分布确认没有意外回落。
- **10-05 08:00 起流量断崖 97%**（8.7 万次/天 → 207~4073 次/天），用户确认
  本轮先不查。⚠️ 这让「换完有量了」不能当成换对了的证据 —— 尺子是脏的，
  验收只认 SpendLogs 的 `model` 落点字段，不认调用量变化。
- **那把 key 在会话里明文出现过**，建议上线后在硅基流动后台 revoke 重发。
