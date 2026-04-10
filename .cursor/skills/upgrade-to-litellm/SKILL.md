---
name: upgrade-to-litellm
description: >-
  Upgrade existing CarHer bot instances from wangsu/openrouter to litellm provider.
  Use when switching a batch of running instances to route through LiteLLM proxy,
  or when the user mentions "升级/切换/迁移 litellm"、"走 litellm"、"切到 litellm".
---

# 批量升级实例到 LiteLLM

将已运行的 her 实例从 `wangsu` 或 `openrouter` 切换为 `litellm` provider，实现统一的 token 消费追踪和供应商路由。

全程零重启、零中断——operator 仅更新 ConfigMap，config-reloader sidecar 热加载，飞书 WS 不断连。

## 前置条件

```bash
# 1. kubectl 连通
kubectl get nodes

# 2. 获取 LiteLLM Master Key
MASTER_KEY=$(kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

# 3. 确认 Admin Pod 名称（用于在集群内调用 LiteLLM API）
ADMIN_POD=$(kubectl get pods -n carher -l app=carher-admin \
  -o jsonpath='{.items[0].metadata.name}')

# 4. LiteLLM 集群内地址
LITELLM_URL="http://litellm-proxy.carher.svc.cluster.local:4000"
```

## Step 1：选择参考实例

找一个已经在 litellm 上正常运行的实例作为配置参考（如 her-1000）：

```bash
kubectl get herinstance her-1000 -n carher \
  -o jsonpath='provider={.spec.provider} model={.spec.model} litellmKey={.spec.litellmKey}'
```

确认输出 `provider=litellm` 且有 `litellmKey`。

## Step 2：确认待升级实例现状

```bash
for i in $(seq <START> <END>); do
  echo -n "her-$i: "
  kubectl get herinstance her-$i -n carher \
    -o jsonpath='provider={.spec.provider} model={.spec.model} litellmKey={.spec.litellmKey}' 2>&1
  echo ""
done
```

确认它们当前是 `provider=wangsu` 或 `provider=openrouter`，且 `litellmKey` 为空。

如果某些实例不存在（NotFound），从列表中排除。

## Step 3：批量生成 LiteLLM Key + Patch CRD

核心脚本——为每个实例生成独立的 LiteLLM virtual key 并 patch CRD：

```bash
MASTER_KEY=$(kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
ADMIN_POD=$(kubectl get pods -n carher -l app=carher-admin \
  -o jsonpath='{.items[0].metadata.name}')
LITELLM_URL="http://litellm-proxy.carher.svc.cluster.local:4000"

for i in $(seq <START> <END>); do
  # 跳过不存在的实例
  kubectl get herinstance her-$i -n carher >/dev/null 2>&1 || continue

  NAME=$(kubectl get herinstance her-$i -n carher -o jsonpath='{.spec.name}')
  echo -n "Generating key for her-$i ($NAME)... "

  KEY=$(kubectl exec $ADMIN_POD -n carher -- \
    python3 -c "
import urllib.request, json
req = urllib.request.Request(
    '${LITELLM_URL}/key/generate',
    data=json.dumps({
        'user_id': 'carher-$i',
        'key_alias': 'her-$i',
        'metadata': {'instance_name': '$NAME'},
        'max_budget': None
    }).encode(),
    headers={
        'Authorization': 'Bearer ${MASTER_KEY}',
        'Content-Type': 'application/json'
    }
)
resp = urllib.request.urlopen(req)
data = json.loads(resp.read())
print(data.get('key', 'ERROR'))
" 2>&1)

  echo "$KEY"

  if [[ "$KEY" == sk-* ]]; then
    kubectl patch herinstance her-$i -n carher --type merge \
      -p "{\"spec\":{\"provider\":\"litellm\",\"litellmKey\":\"$KEY\"}}"
    echo "  -> Patched her-$i to litellm"
  else
    echo "  -> FAILED: $KEY"
  fi
done
```

### 关键参数说明

| 参数 | 用途 |
|------|------|
| `user_id` | LiteLLM 用户标识，格式 `carher-{ID}`，用于 spend 聚合 |
| `key_alias` | LiteLLM key 别名，格式 `her-{ID}`，方便在 UI 中识别 |
| `max_budget` | 可选消费上限（None = 不限） |
| `metadata.instance_name` | 附加元数据，便于在 LiteLLM 侧关联用户名 |

## Step 4：等待 Reconcile 并验证

operator 会自动检测 CRD 变更 → 更新 ConfigMap → config-reloader sidecar 热加载（约 10-30s）。

### 4a. 确认 CRD 已更新

```bash
for i in $(seq <START> <END>); do
  kubectl get herinstance her-$i -n carher >/dev/null 2>&1 || continue
  echo -n "her-$i: "
  kubectl get herinstance her-$i -n carher \
    -o jsonpath='provider={.spec.provider} litellmKey={.spec.litellmKey}' \
    | sed 's/\(litellmKey=sk-.....\).*/\1****/'
  echo ""
done
```

### 4b. 确认 ConfigMap 已切换 primary 模型

```bash
kubectl get cm carher-<SAMPLE_ID>-user-config -n carher \
  -o jsonpath='{.data.openclaw\.json}' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
primary = d.get('agents',{}).get('defaults',{}).get('model',{}).get('primary','')
print(f'primary: {primary}')
"
```

应输出 `primary: litellm/claude-opus-4-6`（或对应模型）。

### 4c. 确认 pure LiteLLM alias 集已生效

```bash
kubectl get cm carher-<SAMPLE_ID>-user-config -n carher \
  -o jsonpath='{.data.openclaw\.json}' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
models = d.get('agents',{}).get('defaults',{}).get('models',{})
providers = d.get('models',{}).get('providers',{})
provider_models = providers.get('litellm',{}).get('models',[])
print('aliases:', sorted(v.get('alias','') for v in models.values()))
print('alias_count:', len(models))
print('providers:', list(providers.keys()))
print('provider_model_count:', len(provider_models))
"
```

期望：

- `alias_count: 7`
- `providers: ['litellm']`
- `provider_model_count: 7`
- aliases 为 `opus / sonnet / gpt / gemini / minimax / glm / codex`
- 不再出现 `ws-gpt` / `ws-gemini` / `or-opus` / `or-sonnet`

### 4d. 确认实例健康

```bash
for i in $(seq <START> <END>); do
  kubectl get herinstance her-$i -n carher >/dev/null 2>&1 || continue
  echo -n "her-$i: "
  kubectl get herinstance her-$i -n carher \
    -o jsonpath='phase={.status.phase} ws={.status.feishuWS} restarts={.status.restarts}'
  echo ""
done
```

期望：所有实例 `phase=Running ws=Connected`。

## 回滚

如果升级后某实例异常，可单独回滚到 wangsu：

```bash
kubectl patch herinstance her-<ID> -n carher --type merge \
  -p '{"spec":{"provider":"wangsu","litellmKey":""}}'
```

批量回滚：

```bash
for i in $(seq <START> <END>); do
  kubectl patch herinstance her-$i -n carher --type merge \
    -p '{"spec":{"provider":"wangsu","litellmKey":""}}' 2>/dev/null
done
```

## 升级前后对比

| 维度 | wangsu (升级前) | litellm (升级后) |
|------|----------------|-----------------|
| 路由 | 直连网宿 API | LiteLLM proxy → 网宿(主) + OpenRouter(备) |
| Token 追踪 | 无 | 每个实例独立 virtual key，按 user_id 聚合 |
| 故障转移 | 无 | 自动 fallback 到 OpenRouter |
| 消费监控 | 无 | LiteLLM Dashboard / Admin API `/api/litellm/spend` |
| 延迟 | 直连 | +1 hop（集群内 <1ms） |

## 注意事项

- LiteLLM key 生成通过 admin pod 内 `urllib` 调用 cluster-internal API，不走外网
- LiteLLM virtual key 允许模型集合需与 proxy/config_gen 同步；当前应包含 7 个 chat 模型
- 每个 key 的 `user_id` / `key_alias` 保证唯一，重复生成会创建新 key（旧 key 仍有效）
- 升级过程完全热加载，Pod 不重启，飞书 WebSocket 不断连
- pure LiteLLM 形态下，运行时只暴露 `litellm/*` 别名；不再保留 `ws-*` / `or-*`
- 如果目标实例已是 `provider=litellm`，脚本中 `kubectl patch` 为幂等操作
- 建议先在小范围（2-3 个实例）验证，再批量推广
