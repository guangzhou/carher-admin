---
name: carher-instance-config-override
description: >-
  针对 carher her 实例做单实例配置覆盖 / 灰度验证 / 批量推进 / 清理收尾的完整
  workflow。Use when the user wants to override a single field per-instance
  (e.g. memorySearch baseUrl, plugin config, model alias), roll it out to
  all ~200 instances gradually, or clean up overrides back to base-config
  only. Also covers "${ENV}" placeholder gotchas and hot-reload vs
  pod-restart-scope distinctions.
---

# Carher 实例配置覆盖 & 批量灰度

## 三层配置合并关系

```
carher-base-config (ConfigMap, 全局)
  ├── shared-config.json5        ← memorySearch / tools / plugins 等默认值
  └── carher-config.json         ← $include shared-config.json5
            ↑ 引用
carher-<N>-user-config (ConfigMap, per-instance, 由 operator 生成)
  └── openclaw.json              ← $include carher-config.json
                                 ← 上面加 per-instance override（agents/models/channels 等）
```

**深合并语义**：user-config 的某字段覆盖 base-config 同路径的字段；其它字段继承。例如：

```json
{"$include": "./carher-config.json",
 "agents": {"defaults": {
   "memorySearch": {"remote": {"baseUrl": "http://litellm-proxy...", "apiKey": "sk-..."}}
 }}}
```

只覆盖 `memorySearch.remote.{baseUrl,apiKey}`；`memorySearch.model / sources / query / experimental` 继续从 base-config 继承。

## 两种生效方式（关键差异）

| 字段位置 | 挂载方式 | 变更生效 |
|---|---|---|
| **user-config**（`carher-<N>-user-config`）| operator 通过 init-container + sidecar `config-reloader` 把 ConfigMap → emptyDir `/data/.openclaw/openclaw.json` | ✅ **热 reload**，~60-120s 自动生效，**不用重启 pod** |
| **base-config**（`carher-base-config`）| 直接 subPath 挂载到 pod 里的 `shared-config.json5` / `carher-config.json` | ❌ **subPath 限制**：改了 ConfigMap pod 看不到新内容，**必须 rollout restart 才生效** |

详见 [k8s-configmap-mount-debug](../k8s-configmap-mount-debug/SKILL.md) 里的 subPath 陷阱详解。

## `${ENV_VAR}` 占位符的坑

- **Base-config 里**：bot 会在运行时把 `"apiKey": "${LITELLM_API_KEY}"` 替换成 pod env 里 `LITELLM_API_KEY` 的实际值
- **User-config 里**：bot **不做** env 替换，`${LITELLM_API_KEY}` 会**原样保留** → 调用上游时报 401
- **结论**：user-config override 必须写**字面 key**（从 `spec.litellmKey` 取），不能用 env 占位

```bash
# 取某实例的 litellmKey
KEY=$(kubectl get her her-${ID} -n carher -o jsonpath='{.spec.litellmKey}')
```

## 灰度三段式

### Phase 0：预验证（纯读，0 影响）

```bash
# 1. 确认目标 key env 被 operator 注入到 pod
kubectl exec <some-pod> -n carher -c carher -- env | grep LITELLM_API_KEY
# 2. 挑一个样本实例，用其 key 直打目标 upstream 确认 200
KEY=$(kubectl get her her-10001 -n carher -o jsonpath='{.spec.litellmKey}')
kubectl run ck --image=curlimages/curl:latest --restart=Never -n carher --quiet --rm -i --command -- \
  curl -sS -w "\n%{http_code}\n" -X POST <url> \
  -H "Authorization: Bearer ${KEY}" -H "Content-Type: application/json" -d '<payload>'
# 3. 评估 upstream 容量：预估全量流量峰值，LiteLLM/依赖服务资源是否扛得住
kubectl top pod -n carher -l app=litellm-proxy
```

### Phase 1：单实例灰度（建议挑自己常用的，如 carher-1000）

```bash
# 备份当前 user-config
kubectl get cm carher-1000-user-config -n carher -o yaml > /tmp/carher-1000-user-config.bak.yaml

# 读当前 openclaw.json
kubectl get cm carher-1000-user-config -n carher -o jsonpath='{.data.openclaw\.json}' > /tmp/c.json

# 用 python 注入 override
# 注意 heredoc 用 'PY'（带单引号）避免 bash 展开 $ 变量
python3 <<'PY'
import json
cfg = json.load(open('/tmp/c.json'))
cfg.setdefault('agents', {}).setdefault('defaults', {})['<FIELD>'] = { ... }  # ← 替换 <FIELD>
json.dump(cfg, open('/tmp/c-new.json','w'), indent=2, ensure_ascii=False)
PY

# apply
kubectl create cm carher-1000-user-config -n carher \
  --from-file=openclaw.json=/tmp/c-new.json \
  --dry-run=client -o yaml | kubectl apply -f -

# 等 sidecar reload（kubelet 同步 + reloader 5s 轮询，总计 ~60-120s）
sleep 120

# 验证 pod 里真的生效了
POD=$(kubectl get pod -n carher --no-headers | grep "^carher-1000-" | awk '{print $1}' | head -1)
kubectl exec $POD -n carher -c carher -- python3 -c "
import json; d=json.load(open('/data/.openclaw/openclaw.json'))
print(d.get('agents',{}).get('defaults',{}).get('<FIELD>'))
"
```

### Phase 2-3：批量推进（灵活节奏）

**一次性拉数据（快）+ 并行 apply（快）**，避免 per-instance kubectl get 慢：

```bash
mkdir -p /tmp/rollout && cd /tmp/rollout
kubectl get her -n carher -o json > all_hers.json
kubectl get cm -n carher -o json > all_cms.json

python3 <<'PY'
import json
hers = json.load(open('all_hers.json'))['items']
cms  = json.load(open('all_cms.json'))['items']
id_to_key = {str(h['spec'].get('userId')): h['spec'].get('litellmKey','')
             for h in hers if h['spec'].get('litellmKey')}
cm_map = {cm['metadata']['name']: cm['data'].get('openclaw.json','')
          for cm in cms if cm['metadata']['name'].endswith('-user-config')}

# 选目标 ID 集合：奇数、特定模型、特定 group、或全量
target_ids = sorted([uid for uid in id_to_key if <CONDITION>], key=int)
for uid in target_ids:
    cfg = json.loads(cm_map.get(f'carher-{uid}-user-config',''))
    cfg.setdefault('agents',{}).setdefault('defaults',{})['<FIELD>'] = { ... }
    json.dump(cfg, open(f'new-{uid}.json','w'), indent=2, ensure_ascii=False)
with open('ids.txt','w') as f: f.write('\n'.join(target_ids)+'\n')
PY

# 并行 apply（每秒 ~10 个）
cat ids.txt | xargs -P 8 -I {} bash -c '
  ID=$1
  kubectl create cm carher-${ID}-user-config -n carher \
    --from-file=openclaw.json=/tmp/rollout/new-${ID}.json \
    --dry-run=client -o yaml 2>/dev/null | kubectl apply -f - >/dev/null 2>&1 \
    && echo "OK $ID" || echo "FAIL $ID"
' _ {} | tee apply.log
grep -c "^OK " apply.log
```

**节奏选择**（按风险偏好）：

| 方案 | 批量大小 | 间隔 | 总时长（~200 台）|
|---|---|---|---|
| 保守 | 1 台串行 | 每台验证后再下一台 | 3-4 小时 |
| 稳健 | 10 台/批 | 批间 60s | ~40 分钟 |
| 激进 | 20 台/批 | 批间 60s | ~18 分钟（今天实测）|

user-config 热 reload 不走 rollout，所以严格说**不受 K8s 控制面压力影响**；但若 hot-reload 后下游（如 LiteLLM）流量激增，仍建议分批观察。

## 清理回退（从 override 回到 base-config 唯一源）

**前置条件**：base-config 必须已经包含正确的最终配置（否则删掉 override 后 pod 热 reload 会回退到 base-config 的旧值）。

如果 base-config 也要改，注意它 **subPath 挂载不热 reload**，必须 **rollout restart** 新 pod 才能读到新 base-config。

**正确时序**：

```
1. 改 base-config ConfigMap + apply（pod 看不到，但 kubelet 已同步）
2. rollout restart 目标 deployment（分批）
3. 等新 pod Ready，确认新 pod 里 shared-config.json5 已是新内容：
     kubectl exec <new-pod> -c carher -- grep baseUrl /data/.openclaw/shared-config.json5
4. 从 user-config 删除 override 字段 + apply
5. sidecar ~60s 热 reload，bot 从 base-config 继承（已是新内容）→ 稳态
```

**反向时序会 revert**：先删 override 再 rollout，drainage 时旧 pod 瞬间从 override 退到 base-config 的**旧**内容。

## 批量清理 override 脚本模板

```bash
# 生成 "删除 override 字段" 版的 new openclaw.json
python3 <<'PY'
import json
cms = json.load(open('/tmp/rollout/all_cms.json'))['items']
cm_map = {cm['metadata']['name']: cm['data'].get('openclaw.json','')
          for cm in cms if cm['metadata']['name'].endswith('-user-config')}
for name, raw in cm_map.items():
    if not raw: continue
    d = json.loads(raw)
    ad = d.get('agents', {}).get('defaults', {})
    if '<FIELD>' in ad: del ad['<FIELD>']
    uid = name.replace('carher-','').replace('-user-config','')
    json.dump(d, open(f'/tmp/rollout/clean-{uid}.json','w'), indent=2, ensure_ascii=False)
PY
# 分批 apply（和 Phase 2-3 同样的 xargs 模板）
```

## 案例：memorySearch 全量切换到 LiteLLM (2026-04-21)

- **目标**：把所有 her 实例的 memorySearch 从"直连 OpenRouter"切到"走 LiteLLM"（为了 spend 统计）
- **Phase 1**：carher-1000 单实例 override，约 2 分钟
- **Phase 2**（激进）：98 个奇数 ID 并行 8 路 apply，**10 秒**完成；等 sidecar reload 120s
- **Phase 3**（激进）：98 个偶数 ID（排除 1000），**18 秒**完成
- **Phase 4**：全量 197 rollout restart（10 批 × 20）+ 清理 override，**18 分钟**
- **结果**：0 次 5xx（除滚动窗口的 3 次 rollout 抖动），用户 0 感知
- **commit**：`2aefc16 feat(base-config): route memorySearch through LiteLLM proxy by default`

## 相关 skill

- 配置挂载细节 → [k8s-configmap-mount-debug](../k8s-configmap-mount-debug/SKILL.md)
- rollout 机制 → [carher-k8s-zero-downtime-rollout](../carher-k8s-zero-downtime-rollout/SKILL.md)
- memorySearch 特定路径 → [carher-memorysearch-config](../carher-memorysearch-config/SKILL.md)
