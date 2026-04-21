---
name: carher-memorysearch-config
description: >-
  Carher bot 的 memorySearch（向量记忆检索）配置链路、触发场景、问题排查。Use
  when debugging embeddings not being called, embeddings失败, 切换 provider /
  切换 embedding model, 或用户问 "memorySearch 没触发"、"bge-m3 没被用"、
  "为什么 N 个实例的 embedding 路由不一样". 本 skill 不涉及 memory 本身的向量
  库/存储实现，仅处理 API 调用的路由和配置。
---

# Carher memorySearch 配置与排查

## 配置链路（3 层 $include）

```
k8s/base-config.yaml → carher-base-config ConfigMap
  └── shared-config.json5
        agents.defaults.memorySearch.*        ← 默认值，所有 her 共享
        ├── provider: "openai"                ← 协议（OpenAI 兼容）
        ├── remote:
        │     baseUrl: http://litellm-proxy.carher.svc:4000
        │     apiKey:  ${LITELLM_API_KEY}     ← env 占位，bot 运行时替换
        ├── model: "BAAI/bge-m3"              ← embedding 模型
        ├── sources: ["memory", "sessions"]   ← 检索范围
        ├── experimental: { sessionMemory: true }
        └── query:
              minScore: 0.25
              maxResults: 8
              hybrid:
                vectorWeight: 0.7
                textWeight:   0.3
                mmr: { enabled: true, lambda: 0.4 }
        ↑
carher-<N>-user-config 里的 openclaw.json（每实例）
  └── agents.defaults.memorySearch.remote     ← 如有，覆盖 base-config 同字段
```

**深合并**：user-config 的 `memorySearch.remote`（如有）覆盖 base-config 的 `remote`；其它字段（model / sources / query）继续从 base-config 继承。

## 当前生产路由

- **所有 197 个 her 实例** 通过 base-config 走 `http://litellm-proxy.carher.svc:4000`
- LiteLLM 把 `BAAI/bge-m3` 转发到 `openrouter/BAAI/bge-m3`
- OpenRouter 背后是 DeepInfra 的 bge-m3 实例（1024 维输出）
- 定价：$0.01 / 1M tokens（spend 会记录在 LiteLLM_SpendLogs）

## memorySearch 什么时候真的被触发？

**误区**：不是每条用户消息都会触发 embedding。

**实际触发场景**（需要语义检索的）：
- 用户说 "之前是不是跟你提过 X？" / "找找我说过关于 Y 的" / "搜一下我们聊过 Z 的那次"
- 某些 tool call（如 agent 主动调用 `search_memory(query)`）

**不触发的场景**：
- "最近一个月聊天汇总" —— 走 feishu 消息归档按时间范围查询
- "今天吃什么" —— 日常对话，走 chat completion 直接回复
- bot 启动时的 warmup（有时会发 1-2 次作为 memory 索引 warmup）

**现象**：日活 ~10 个用户的情况下，日均 embedding 调用几十到几百次，不是每条消息都调。

## 排查命令

### 1) 看某实例 pod 里实际生效的路由

```bash
POD=$(kubectl get pod -n carher --no-headers | grep "^carher-<ID>-" | awk '{print $1}' | head -1)

# 先看 user-config 层（openclaw.json）
kubectl exec $POD -n carher -c carher -- python3 -c "
import json
d = json.load(open('/data/.openclaw/openclaw.json'))
ms = d.get('agents',{}).get('defaults',{}).get('memorySearch', {})
print('USER override:', json.dumps(ms.get('remote',{}) if ms else {}, indent=2))
"

# 再看 base-config 层（shared-config.json5）
kubectl exec $POD -n carher -c carher -- grep -A5 "memorySearch:" /data/.openclaw/shared-config.json5 | head -12
```

**最终 bot 用的是哪个？** user-config 如果定义了 `memorySearch.remote` 就用 user-config；否则用 base-config。注意 base-config 走 subPath 挂载，ConfigMap 更新后 pod 看到的可能是旧内容，见 [k8s-configmap-mount-debug](../k8s-configmap-mount-debug/SKILL.md)。

### 2) 看 LiteLLM 侧的 embedding 流量

```bash
# 最近 N 分钟 bge-m3 请求成功率
kubectl logs deploy/litellm-proxy -n carher --since=10m 2>/dev/null \
  | grep "aembedding.*bge-m3" | grep -oE "(200 OK|Exception|Error)" | sort | uniq -c

# 最近 N 分钟按 key_alias 的 embedding 调用分布（哪些实例活跃）
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c "
SELECT t.key_alias, COUNT(*) AS calls, SUM(s.total_tokens) AS tokens,
       SUM(s.spend)::numeric(14,8) AS cost_usd
FROM \"LiteLLM_SpendLogs\" s
JOIN \"LiteLLM_VerificationToken\" t ON s.api_key = t.token
WHERE s.model LIKE '%bge%'
  AND s.\"startTime\" > NOW() - INTERVAL '30 minutes'
GROUP BY t.key_alias ORDER BY calls DESC LIMIT 20;
"

# 某实例 bot pod 的出站 embedding 请求是否打到 LiteLLM
IP=$(kubectl get pod -n carher -l user-id=<ID> -o jsonpath='{.items[0].status.podIP}')
kubectl logs deploy/litellm-proxy -n carher --since=5m 2>/dev/null \
  | grep -E "POST /(v1/)?embeddings.*${IP}" | head -5
```

### 3) 直接用某实例 key 测试 LiteLLM 链路

```bash
KEY=$(kubectl get her her-<ID> -n carher -o jsonpath='{.spec.litellmKey}')
kubectl run ck --image=curlimages/curl:latest --restart=Never -n carher --quiet --rm -i --command -- \
  curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST \
    "http://litellm-proxy.carher.svc:4000/v1/embeddings" \
    -H "Authorization: Bearer ${KEY}" \
    -H "Content-Type: application/json" \
    -d '{"model":"BAAI/bge-m3","input":"hello test"}'
# 预期 200
```

## 常见问题

### Q: 某实例 LiteLLM UI 只看到 1 条 bge-m3 记录，是不是没切换成功？

**首先核实**：看那一条记录是不是早先的手工测试，和用户最近是否触发过语义检索的消息。用 Q1 命令看 pod 里真实配置。

**如果配置正确但 UI 稀少**：正常，embedding 不是每条消息都调，参考"什么时候触发"章节。

**如果 LiteLLM 没有该实例 IP 的任何 /embeddings 请求**：bot 侧 memorySearch 可能走了别的路径（如本地 index cache、或 provider 不是 litellm）。

### Q: `Request Failed 404: No fallback model group found for BAAI/bge-m3`

根因：LiteLLM 调 OpenRouter 时 Python httpx 抛 `UnicodeEncodeError: surrogates not allowed`（输入含断裂 emoji），重试 2 次后触发 fallback，但 bge-m3 没配 fallback 所以 404。

已上线的防御：`embedding_sanitize` pre-call hook（`k8s/litellm-callbacks/embedding_sanitize.py`）清洗 lone surrogate 再转发。

根治（TODO）：修 bot 侧字节级切片，按 grapheme 切 emoji 不切断。

### Q: 改 memorySearch.remote 切新 provider，所有实例一次性切还是灰度？

**推荐灰度**。参考 [carher-instance-config-override](../carher-instance-config-override/SKILL.md) 的三段式：
1. Phase 0 预验证新 provider endpoint 能用该 key 打通
2. Phase 1 挑 1 个实例（通常 carher-1000，便于自测）做 per-instance override
3. Phase 2-3 批量推进 per-instance override；确认全通后改 base-config、分批 rollout restart、最后清理 override

**正确时序**（避免瞬间 revert 到旧路由）：
```
改 base-config apply → rollout restart 新 pod（读到新 base-config）→ 删 override
                                                                      ↑
                                               不能反过来：先删 override 会让 bot
                                               热 reload 后从 subPath 里的旧
                                               base-config 继承，暂时 revert
```

详见 [carher-instance-config-override](../carher-instance-config-override/SKILL.md) 的"清理回退"章节。

## 相关文件

- ConfigMap 源：`k8s/base-config.yaml`（`carher-base-config`）
- LiteLLM 路由定义：`k8s/litellm-proxy.yaml` 里 `model_list` 下的 `BAAI/bge-m3` 条目
- Embedding sanitize hook：`k8s/litellm-callbacks/embedding_sanitize.py`

## 历史变更记录

| 日期 | 变更 | commit |
|---|---|---|
| 2026-04-21 | 所有 her 的 memorySearch 从直连 OpenRouter 切到走 LiteLLM | `2aefc16` |
| 2026-04-21 | LiteLLM 加上 bge-m3 定价（$0.01/1M），SpendLogs 开始记录 embedding 花费 | `57960bc` |
| 2026-04-21 | 加 `embedding_sanitize` hook 防御 Node.js lone surrogate | `7f584fc` |

## 相关 skill

- 改配置的灰度 workflow → [carher-instance-config-override](../carher-instance-config-override/SKILL.md)
- LiteLLM 层的 hook 开发 → [litellm-hook-dev](../litellm-hook-dev/SKILL.md)
- LiteLLM 整体路由 / 模型 list / 运维 → [litellm-ops](../litellm-ops/SKILL.md)
- 消费统计 SQL 模板 → [her-spend-stats](../her-spend-stats/SKILL.md)
