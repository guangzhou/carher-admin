---
name: litellm-key-provider-swap
description: >-
  对单个 LiteLLM virtual key 对调首选/备选供应商（如 Wangsu Direct ↔ OpenRouter），
  不影响其他用户。Use when the user mentions "换供应商" / "对调首选备选" / "swap
  provider" / "改路由" + 某人名字或 key alias，或想让某个 claude-code / cursor key
  的主供应商从 A 切到 B。
---

# LiteLLM 单 Key 供应商对调

## 原理

LiteLLM virtual key 支持 per-key `aliases`（JSON 对象）。当请求到达时：

1. 客户端发送 `model: X`
2. LiteLLM 在请求入口检查 key 的 `aliases`：若有 `X → Y`，则将请求重路由到模型组 Y
3. 若 Y 失败，走 per-key 或 global `fallbacks` 中 `Y → Z` 的链
4. aliases 仅影响该 key，不改全局路由

## 前置

```bash
# kubectl 隧道（按 k8s-via-bastion skill）
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes

# Master key
MK=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
```

## 步骤 1：找到目标 key

```bash
# 按人名模糊搜
curl -s "http://127.0.0.1:4000/key/aliases?page=1&size=100&search=<NAME>" \
  -H "Authorization: Bearer $MK" | jq

# 拿详情（含 token hash、当前 aliases、models allowlist、per-key fallbacks）
curl -s "http://127.0.0.1:4000/spend/keys?limit=600" \
  -H "Authorization: Bearer $MK" | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    if '<NAME>' in (r.get('key_alias') or '').lower():
        print(json.dumps({k: r[k] for k in
          ['token','key_alias','aliases','models','router_settings']}, indent=2, default=str))
"
```

记下 `token`（64 位 hex hash）。

## 步骤 2：确认当前路由

查 7 天内实际命中的模型分布，确认哪个是首选、哪个是备选：

```bash
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -c "
SELECT sl.model, count(*) AS cnt,
       round(sum(sl.spend)::numeric, 3) AS spend
FROM \"LiteLLM_SpendLogs\" sl
JOIN \"LiteLLM_VerificationToken\" vt ON sl.api_key = vt.token
WHERE vt.key_alias = '<KEY_ALIAS>'
  AND sl.\"startTime\" > NOW() - INTERVAL '7 days'
GROUP BY sl.model ORDER BY cnt DESC;"
```

典型输出：
- `anthropic/anthropic.claude-opus-4-7` (Wangsu Direct) = 首选（大量）
- `anthropic/anthropic/claude-opus-4.7` (OpenRouter) = 备选（少量 fallback 命中）

## 步骤 3：设置 per-key aliases 对调

以 Wangsu Direct → OpenRouter 对调为例：

```bash
curl -s -X POST "http://127.0.0.1:4000/key/update" \
  -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "<TOKEN_HASH>",
    "aliases": {
      "anthropic.claude-opus-4-7": "anthropic.openrouter.claude-opus-4-7",
      "anthropic.claude-opus-4-6": "anthropic.openrouter.claude-opus-4-6",
      "anthropic.claude-sonnet-4-6": "anthropic.openrouter.claude-sonnet-4-6"
    }
  }' | jq '{key_alias, aliases}'
```

**要点**：
- `aliases` 是**完整替换**，不是增量 merge；传入的就是最终值
- 只映射需要对调的模型；`anthropic.claude-haiku-4-5` 无 OpenRouter 对应组则不映射
- per-key `router_settings.fallbacks` 已有反向链（`anthropic.openrouter.* → anthropic.claude-*`），所以 OpenRouter 挂了自动回落 Wangsu Direct
- 立即生效，无需重启 proxy

## 步骤 4：验证

```bash
# 1. 确认 aliases 已写入
curl -s "http://127.0.0.1:4000/key/info?key=<TOKEN_HASH>" \
  -H "Authorization: Bearer $MK" | jq '.info.aliases'

# 2. 抽查其他用户的 key 没被改动
curl -s "http://127.0.0.1:4000/spend/keys?limit=600" \
  -H "Authorization: Bearer $MK" | python3 -c "
import sys, json
rows = json.load(sys.stdin)
with_a = [r['key_alias'] for r in rows if r.get('key_alias','').startswith('claude-code-') and r.get('aliases')]
print(f'有 aliases 的 claude-code keys ({len(with_a)}):', with_a)
"
```

## 回滚

清空 aliases 即恢复原始路由（客户端发什么模型名就打什么模型组）：

```bash
curl -s -X POST "http://127.0.0.1:4000/key/update" \
  -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{"key": "<TOKEN_HASH>", "aliases": {}}' | jq '{key_alias, aliases}'
```

## 常见对调场景

| 场景 | aliases 内容 |
|------|-------------|
| Claude Code: Wangsu → OpenRouter | `{"anthropic.claude-opus-4-7": "anthropic.openrouter.claude-opus-4-7", "anthropic.claude-opus-4-6": "anthropic.openrouter.claude-opus-4-6", "anthropic.claude-sonnet-4-6": "anthropic.openrouter.claude-sonnet-4-6"}` |
| Claude Code: OpenRouter → Wangsu | `{"anthropic.openrouter.claude-opus-4-7": "anthropic.claude-opus-4-7", "anthropic.openrouter.claude-opus-4-6": "anthropic.claude-opus-4-6", "anthropic.openrouter.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6"}` |
| Carher bot: Wangsu → OpenRouter | `{"claude-opus-4-6": "openrouter-claude-opus-4-6", "claude-sonnet-4-6": "openrouter-claude-sonnet-4-6"}` |
| 恢复默认 | `{}` |

## 注意事项

- **aliases 只影响单个 key**，是最安全的路由切换方式
- **不要改全局 YAML** 的 `model_group_alias`/`fallbacks` 来实现单用户切换
- 如果目标模型组不在 key 的 `models` allowlist 里，需要先用 `/key/update` 加上
- per-key `router_settings.fallbacks` 和 `aliases` 是独立字段，更新一个不会清另一个
