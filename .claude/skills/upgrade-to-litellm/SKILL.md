---
name: upgrade-to-litellm
description: >-
  Upgrade existing CarHer bot instances from wangsu/openrouter to litellm provider.
  Use when switching a batch of running instances to route through LiteLLM proxy,
  or when the user mentions "升级/切换/迁移 litellm"、"走 litellm"、"切到 litellm".
---

# 批量升级实例到 LiteLLM

将已运行的 her 实例从 `wangsu` 或 `openrouter` 切换为 `litellm` provider，实现统一的 token 消费追踪和供应商路由。

> ⚠️ **重要：跨 provider 切换不是零中断**
>
> Operator 当前在跨 provider 切换时**只会热加载 ConfigMap**（更新 `primary` 模型别名），
> 但**不会同步 Deployment 的 `LITELLM_API_KEY` env**——这意味着新 key 不会注入到 pod，
> pod 会继续用旧 env（如果之前没有 LITELLM_API_KEY 则是空，如果之前曾经在 litellm 上过
> 则是某个早已 revoke 的旧 key），结果就是 **HTTP 401 Authentication Error**，her 完全无法应答。
>
> **必须执行下面的 Step 3.5 手动 sync deploy env + 触发 rollout**，会有一次 ~30s 的 pod 重启
> （飞书 WS 会短暂断开重连，期间消息进 redis-broadcast 缓冲，不丢但有延迟）。
>
> "零重启" 只适用于 **同 provider 内的纯 ConfigMap 变更**（如 litellm 内部换 model 别名、
> plugin enable/disable）。跨 provider / 换 LITELLM_API_KEY 永远要走 rollout。
>
> 见文末"故障排查 / Operator hot-reload 陷阱"详细说明。

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
        'key_alias': 'carher-$i',
        'metadata': {'instance': 'carher-$i', 'owner_name': '$NAME'},
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
| `key_alias` | LiteLLM key 别名，格式 `carher-{ID}`，与 user_id 保持一致 |
| `max_budget` | 可选消费上限（None = 不限） |
| `metadata.instance` | 实例标识 `carher-{ID}` |
| `metadata.owner_name` | 附加元数据，便于在 LiteLLM 侧关联用户名 |

## Step 3.5：强制 Sync Deploy Env（**跨 provider 切换必做**）

> ⚠️ Operator 跨 provider 切换时不会自动同步 Deployment env。即使 CRD `spec.litellmKey`
> 已经是新 key，pod 里的 `LITELLM_API_KEY` 还是旧值（甚至是 LiteLLM 已经 revoke 的死 key）。
> 必须手动 `kubectl set env` 触发 rollout。

```bash
for i in $(seq <START> <END>); do
  kubectl get herinstance her-$i -n carher >/dev/null 2>&1 || continue

  KEY=$(kubectl get herinstance her-$i -n carher -o jsonpath='{.spec.litellmKey}')
  CURRENT_ENV=$(kubectl get deploy carher-$i -n carher \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="carher")].env[?(@.name=="LITELLM_API_KEY")].value}')

  if [[ -z "$KEY" ]]; then
    echo "her-$i: spec.litellmKey is empty, SKIP (Step 3 failed?)"
    continue
  fi

  if [[ "$KEY" == "$CURRENT_ENV" ]]; then
    echo "her-$i: env already in sync ✓ (${KEY:0:8}...)"
    continue
  fi

  echo "her-$i: env stale (deploy=${CURRENT_ENV:0:8}... vs spec=${KEY:0:8}...) -> syncing"
  kubectl set env deploy/carher-$i -n carher LITELLM_API_KEY=$KEY -c carher
done

echo ""
echo "=== 等待所有 deploy rollout 完成 ==="
for i in $(seq <START> <END>); do
  kubectl get deploy carher-$i -n carher >/dev/null 2>&1 || continue
  kubectl rollout status deploy/carher-$i -n carher --timeout=120s
done
```

> 这一步会让每个被升级的 pod 重启一次（~30s rolling）。飞书 WS 会断开重连，
> 期间用户消息进 redis-broadcast 缓冲，**不丢但有延迟**。生产时段升级请控制并发。

## Step 4：验证

operator 在 Step 3 已通过 hot reload 更新了 ConfigMap（primary 模型别名），
Step 3.5 已通过 deploy rollout 注入新 env。这一步全面校验。

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

应输出 `primary: litellm/gpt-5.4`（或对应实例 `spec.model` 的映射）。

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

- `alias_count: 7+`（基础 7 个，可能有 `opus4.7` 等扩展，最新值以参考实例 her-1000 为准）
- `providers: ['litellm']`
- `provider_model_count` 与 `alias_count` 一致
- aliases 至少包含 `opus / sonnet / gpt / gemini / minimax / glm / codex`
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

### 4e. **关键：验证 deploy env 与 CRD 一致**（防 Step 3.5 遗漏）

```bash
for i in $(seq <START> <END>); do
  kubectl get herinstance her-$i -n carher >/dev/null 2>&1 || continue

  SPEC_KEY=$(kubectl get herinstance her-$i -n carher -o jsonpath='{.spec.litellmKey}')
  DEPLOY_ENV=$(kubectl get deploy carher-$i -n carher \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="carher")].env[?(@.name=="LITELLM_API_KEY")].value}')
  POD=$(kubectl get pod -n carher -l user-id=$i --field-selector status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  POD_ENV=$(kubectl exec $POD -n carher -c carher -- printenv LITELLM_API_KEY 2>/dev/null)

  if [[ "$SPEC_KEY" == "$DEPLOY_ENV" && "$DEPLOY_ENV" == "$POD_ENV" ]]; then
    echo "her-$i: ✓ all 3 in sync (${SPEC_KEY:0:8}...)"
  else
    echo "her-$i: ✗ MISMATCH spec=${SPEC_KEY:0:8} deploy=${DEPLOY_ENV:0:8} pod=${POD_ENV:0:8}"
  fi
done
```

期望：所有实例 ✓。任何 ✗ 都说明 Step 3.5 没生效，回头 `kubectl set env` + 等 rollout。

### 4f. **关键：用新 key 真实调一次 LLM**（端到端验证）

仅在 ConfigMap 和 env 都对齐还不够——上游 LiteLLM proxy 可能 reject。抽样调一次：

```bash
ADMIN_POD=$(kubectl get pods -n carher -l app=carher-admin -o jsonpath='{.items[0].metadata.name}')

for i in <SAMPLE_IDS>; do
  KEY=$(kubectl get herinstance her-$i -n carher -o jsonpath='{.spec.litellmKey}')
  echo -n "her-$i: "
  kubectl exec $ADMIN_POD -n carher -- python3 -c "
import urllib.request, json
req = urllib.request.Request(
    'http://litellm-proxy.carher.svc.cluster.local:4000/chat/completions',
    data=json.dumps({
        'model': 'claude-opus-4-6',
        'messages': [{'role':'user','content':'回复一个字: hi'}],
        'max_tokens': 5, 'stream': False
    }).encode(),
    headers={'Authorization': 'Bearer $KEY', 'Content-Type': 'application/json'}
)
try:
    r = urllib.request.urlopen(req, timeout=30)
    d = json.loads(r.read())
    print('200 reply=' + d['choices'][0]['message']['content'][:20])
except urllib.error.HTTPError as e:
    print(f'HTTP {e.code}: ' + e.read().decode()[:200])
"
done
```

期望：`200 reply=...`。任何 401/403 都说明 key 没生效或 proxy 侧路由问题。

### 4g. 抽查 her 主进程日志确认无 401

```bash
for i in <SAMPLE_IDS>; do
  POD=$(kubectl get pod -n carher -l user-id=$i --field-selector status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}')
  echo "=== her-$i ($POD) recent 401/auth errors ==="
  kubectl logs $POD -n carher -c carher --since=5m 2>&1 \
    | grep -iE "401|Authentication|Invalid proxy server token" | tail -5
  echo "(empty = OK)"
done
```

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

> ⚠️ 回滚同样有 deploy env 同步问题：CRD 改回 wangsu 后，deploy 里的 `LITELLM_API_KEY`
> env 不会自动清理（残留的死 key），下次再切回 litellm 时就是 her-11 这种事故的源头。
> 完整回滚需要：

```bash
for i in $(seq <START> <END>); do
  kubectl set env deploy/carher-$i -n carher LITELLM_API_KEY- -c carher 2>/dev/null
  kubectl rollout status deploy/carher-$i -n carher --timeout=120s
done
```

## 升级前后对比

| 维度 | wangsu (升级前) | litellm (升级后) |
|------|----------------|-----------------|
| 路由 | 直连网宿 API | gpt/sonnet/opus/gemini → OpenRouter(主) + 网宿(备)；minimax/glm/codex → OpenRouter only |
| 可用模型 | 4 个（gpt/sonnet/opus/gemini） | 7+ 个（+minimax/glm/codex/opus4.7...） |
| Token 追踪 | 无 | per-instance virtual key（`carher-{uid}`），按 user_id 聚合 |
| 故障转移 | 无 | 4 个主模型自动 fallback 到网宿 |
| 消费监控 | 无 | LiteLLM Dashboard / Admin API `/api/litellm/spend` |
| Env 注入 | — | Operator 注入 `LITELLM_API_KEY` 覆盖共享 master key |
| 延迟 | 直连 | +1 hop（集群内 <1ms） |

## 注意事项

- LiteLLM key 生成通过 admin pod 内 `urllib` 调用 cluster-internal API，不走外网
- LiteLLM virtual key 允许模型集合需与 proxy/config_gen 同步；当前应包含 7+ 个 chat 模型（含 `opus4.7`）
- 每个 key 的 `user_id` / `key_alias` 统一为 `carher-{uid}`，保证唯一
- **跨 provider 切换会导致一次 pod rolling restart（~30s），飞书 WS 短暂断连**——
  仅 ConfigMap 内的纯配置变更（同 provider 换 model 别名等）才能完全热加载
- pure LiteLLM 形态下，运行时只暴露 `litellm/*` 别名；不再保留 `ws-*` / `or-*`
- 如果目标实例已是 `provider=litellm`，脚本中 `kubectl patch` 为幂等操作；
  但 Step 3.5 的 `kubectl set env` **会真的触发 rollout**，已在 sync 状态时脚本会跳过
- 建议先在小范围（2-3 个实例）走完 Step 1-4 全流程验证，再批量推广
- **不要跳过 Step 4e 和 4f**——只看 4a/4b/4c/4d 通过会漏掉 her-11 这种事故

## 故障排查

### Operator hot-reload 陷阱（her-11 事故根因）

**症状**：跨 provider 切换（如 openrouter → litellm）后，pod 调用 LiteLLM 报：

```
HTTP 401: Authentication Error, Invalid proxy server token passed.
Received API Key = *** Key Hash (Token) = <hash>.
Unable to find token in cache or `LiteLLM_VerificationTokenTable`
```

her 主进程日志会出现 `embedded run agent end: ... isError=true ... error=HTTP 401: Authentication Error`，
飞书侧表现为 her **完全无法应答**（每条消息都返回 "HTTP 401: Authentication Error..." 文字）。

**根因**：

Operator 的 reconcile 逻辑在 spec 变化时优先尝试 hot reload，日志体现为：

```
{"level":"info","msg":"Hot config reload via sidecar (no pod restart)",
 "controller":"herinstance","HerInstance":{"name":"her-X"},"configHash":"xxxx"}
```

热加载只重写 ConfigMap（更新 `agents.defaults.model.primary` 别名），
**不会更新 Deployment template 的 env 字段**。结果：

| 维度 | 状态 |
|------|------|
| `spec.litellmKey` | 新 key（`sk-xxxNew...`） ✓ |
| ConfigMap `primary` | 新值（`litellm/claude-opus-4-6`） ✓ |
| Deployment env `LITELLM_API_KEY` | **旧值或空** ✗ |
| Pod 实际 env `LITELLM_API_KEY` | **旧值或空** ✗ |

如果实例**从未在 litellm 上过**，env 直接没有 LITELLM_API_KEY，pod 会用 master key 或报 missing。
如果实例**曾经在 litellm 上过然后被切走**（如先 litellm → openrouter → litellm），
env 会保留上一次的死 key（很可能在 LiteLLM 的 `LiteLLM_VerificationTokenTable` 已被 revoke / 删除），
就出现完整的 401 复现。

**诊断三联**：

```bash
# 1. 三层一致性对比
SPEC_KEY=$(kubectl get herinstance her-<ID> -n carher -o jsonpath='{.spec.litellmKey}')
DEPLOY_ENV=$(kubectl get deploy carher-<ID> -n carher \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="carher")].env[?(@.name=="LITELLM_API_KEY")].value}')
POD=$(kubectl get pod -n carher -l user-id=<ID> --field-selector status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')
POD_ENV=$(kubectl exec $POD -n carher -c carher -- printenv LITELLM_API_KEY)
echo "spec=$SPEC_KEY"
echo "deploy=$DEPLOY_ENV"
echo "pod=$POD_ENV"

# 2. operator 是不是走了 hot reload 路径
OPPOD=$(kubectl get pod -n carher -l app=carher-operator -o jsonpath='{.items[0].metadata.name}')
# 注意拿 leader 那个 pod 的日志（看 lease）：
kubectl get lease carher-operator-leader -n carher -o jsonpath='{.spec.holderIdentity}'
kubectl logs <leader-pod> -n carher --tail=200 | grep -iE "her-<ID>|carher-<ID>"
# 看到 "Hot config reload via sidecar (no pod restart)" 即命中本陷阱

# 3. 旧 deploy env 在 LiteLLM 是不是已 revoke
MASTER_KEY=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
ADMIN_POD=$(kubectl get pods -n carher -l app=carher-admin -o jsonpath='{.items[0].metadata.name}')
kubectl exec $ADMIN_POD -n carher -- python3 -c "
import urllib.request, json
req = urllib.request.Request(
    'http://litellm-proxy.carher.svc.cluster.local:4000/key/info?key=$DEPLOY_ENV',
    headers={'Authorization': 'Bearer $MASTER_KEY'})
try:
    print(json.loads(urllib.request.urlopen(req).read()))
except urllib.error.HTTPError as e:
    print(f'HTTP {e.code}')  # 404 = 死 key
"
```

**修复**（即时止血，~30s）：

```bash
NEW_KEY=$(kubectl get herinstance her-<ID> -n carher -o jsonpath='{.spec.litellmKey}')
kubectl set env deploy/carher-<ID> -n carher LITELLM_API_KEY=$NEW_KEY -c carher
kubectl rollout status deploy/carher-<ID> -n carher --timeout=120s
```

**根治**（待 carher-operator 修复）：

operator 的 `shouldHotReload(...)` 判定应该把 env 字段差异也算进去——
当 `spec.litellmKey` 变化时，必须走 deploy template update 路径，而不是 sidecar hot reload。
在 operator 修复之前，本 skill 的 Step 3.5 是必经之路。

### LiteLLM key 已生成但 user_id 查不到

```bash
# /key/list?user_id=carher-X 返回空，但 /key/info?key=sk-xxx 能查到 key 信息
```

是 LiteLLM `/key/list` 的过滤行为问题（不是事故）。直接用 `/key/info?key=...` 校验
key 存在性 + user_id 字段，更可靠。

### Pod 重启时间和 patch 时间对不上

可能场景：carher 主进程在 hot reload 触发时检测到某些 config 变更后**自己 graceful exit (exit 0)**，
K8s 按 deploy template 重新拉起 pod——但拉起时还是用旧 deploy template 的 env。
这种"重启了但 env 没变"是最迷惑的现象，必须同时看 `kubectl rollout history deploy/carher-<ID>`
确认 deploy generation 是不是真的 bump 了（没 bump = 没走 rollout，env 一定没变）。
