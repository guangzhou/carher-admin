---
name: hot-grayscale
description: >-
  Zero-downtime grayscale (canary) deployment for CarHer K8s instances.
  Covers hot-reload for config-only changes and ReadinessGate-based rolling
  updates for pod-spec changes. Use when performing grayscale rollouts,
  canary deployments, batch config updates, or any change that must be
  user-imperceptible (zero WebSocket disconnection).
---

# Hot Grayscale Deployment

Zero-downtime grayscale rollout for CarHer instances on Kubernetes. The core
principle: **users must never perceive a network disconnection** during any
configuration or image update.

## Architecture Overview

Changes are classified into two tiers based on what they affect:

| Change Type | Examples | Mechanism | Pod Restart? |
|---|---|---|---|
| **Config-only** | model, provider, owner, bot name, litellmKey | Hot-reload via sidecar | No |
| **Pod-spec** | image, prefix, appSecretRef, deployGroup | Rolling update + ReadinessGate | Yes (zero-downtime) |

The operator determines the tier by comparing two keys on the Deployment:

- `carher.io/pod-spec-key` — raw concatenation `image|prefix|secretName|deployGroup` (NOT a hash)
- `carher.io/live-config-hash` — MD5 of ConfigMap content (first 12 hex chars)

## Tier 1: Config-Only Hot Reload (No Pod Restart)

Flow:

1. CRD spec updated (e.g., `model: opus`, `provider: wangsu`)
2. Operator detects `pod-spec-key` unchanged, `live-config-hash` changed
3. Operator updates the ConfigMap — skips Deployment rollout
4. K8s propagates ConfigMap change to volume mount (~60s)
5. `config-reloader` sidecar detects change, injects secret, writes merged config
6. Main container picks up new config — **zero WebSocket disruption**

### Batch Config Update via kubectl

```bash
# Update all instances to wangsu/opus
kubectl get her -n carher --no-headers -o custom-columns='NAME:.metadata.name' \
  | xargs -I{} kubectl patch her {} -n carher --type merge \
    -p '{"spec":{"model":"opus","provider":"wangsu"}}'
```

### Canary: Update a Subset

```bash
# First N instances get the new config, rest stay on old
kubectl get her -n carher --no-headers -o custom-columns='NAME:.metadata.name' \
  | sort -t- -k2 -n | head -20 \
  | xargs -I{} kubectl patch her {} -n carher --type merge \
    -p '{"spec":{"model":"opus","provider":"wangsu"}}'
```

### Rollback

```bash
# Revert canary instances back to previous config (adjust model/provider as needed)
# Current defaults for NEW instances: provider=wangsu, model=opus
kubectl get her -n carher --no-headers -o custom-columns='NAME:.metadata.name' \
  | sort -t- -k2 -n | head -20 \
  | xargs -I{} kubectl patch her {} -n carher --type merge \
    -p '{"spec":{"model":"gpt","provider":"openrouter"}}'
```

### Verify

```bash
# Check config distribution
kubectl get her -n carher \
  -o custom-columns='MODEL:.spec.model,PROVIDER:.spec.provider' --no-headers \
  | sort | uniq -c | sort -rn

# Confirm hot-reload in operator logs (no pod restarts)
kubectl logs -n carher deploy/carher-operator --tail=30 --since=2m \
  | grep "Hot config reload"
```

## Tier 2: Pod-Spec Rolling Update (Zero-Downtime)

For changes requiring a new pod (image upgrade, prefix change, secret rotation):

1. Operator detects `pod-spec-key` changed → triggers Deployment rollout
2. Strategy: `MaxSurge=1, MaxUnavailable=0` — new pod starts before old terminates
3. New pod has `ReadinessGate: carher.io/feishu-ws-ready`
4. Health checker polls `/healthz` on new pod, waits for Feishu WS connection
5. Once connected, sets ReadinessGate to `True`
6. **Only then** does K8s terminate the old pod (with `preStop: sleep 15`)

### Image Canary Example

```bash
# Canary 20 instances to new image
kubectl get her -n carher --no-headers -o custom-columns='NAME:.metadata.name' \
  | sort -t- -k2 -n | head -20 \
  | xargs -I{} kubectl patch her {} -n carher --type merge \
    -p '{"spec":{"image":"v20260402"}}'
```

## Scripted Canary via Admin API

For more controlled canary rollouts with deploy group tracking, use
`scripts/canary-wangsu-opus.sh`:

```bash
ADMIN_API_KEY=xxx CANARY_COUNT=20 ./scripts/canary-wangsu-opus.sh
```

This tags instances with `deploy_group=canary` for monitoring and rollback.

## Key Implementation Details

### config-reloader Sidecar

- Polls ConfigMap volume every 5s for changes
- Injects `FEISHU_APP_SECRET` from K8s Secret into the merged config
- **Must use `writeFileSync` (not rename)** — SubPath bind mounts don't follow
  inode changes from `fs.renameSync()`

### ReadinessGate Health Check

- Health checker runs every **30s** with **50 concurrent workers**
- Queries `http://{podIP}:18789/healthz` for real WS status
- Fallback: container ready + 15s uptime (WS typically connects in 5-15s)
- During rolling updates, checks **all** pods for a UID (not just one),
  preventing rollout stalls when old pod shadows new pod in the pod map
- Worst-case ReadinessGate delay: ~30s (one health check cycle)

### Graceful Shutdown

- `preStop: sleep 15` — allows in-flight requests to complete
- `terminationGracePeriodSeconds: 30` — hard limit

## Pitfalls & Gotchas

For detailed technical pitfalls discovered during implementation, see
[pitfalls.md](pitfalls.md).

## Quick Decision Tree

```
CRD spec changed?
├─ model/provider/owner/name/litellmKey changed → Tier 1 hot-reload (no restart)
├─ image/prefix/appSecretRef/deployGroup changed → Tier 2 rolling update (zero-downtime)
└─ nothing changed, replicas=0                  → Scale up (unpause)
```

### Provider 切换到 litellm 的副作用

将实例的 `provider` 从 `wangsu/openrouter/anthropic` 切换为 `litellm` 时（当前路由：OpenRouter 主 + 网宿备），
Admin API 会自动生成一个 per-instance LiteLLM 虚拟 key 并写入 CRD `spec.litellmKey`。
反向切换（从 `litellm` 切走）会删除该 key 并清空 `spec.litellmKey`。

这属于 Tier 1 config-only 变更，不会触发 Pod 重启。但需注意：
- 批量切换时，每个实例都会产生一次 LiteLLM key API 调用
- 切走后对应 key 在 LiteLLM 侧被删除，历史 spend 数据仍保留
- Operator 会向 Pod 注入 `LITELLM_API_KEY` env（per-instance virtual key），覆盖共享 Secret 中的 master key
- Key 命名统一为 `carher-{uid}`（`key_alias` 和 `user_id` 一致）
