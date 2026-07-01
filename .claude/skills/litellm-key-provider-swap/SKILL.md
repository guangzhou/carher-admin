---
name: litellm-key-provider-swap
description: >-
  对单个 LiteLLM virtual key 改路由：对调首选/备选供应商（Wangsu Direct ↔ OpenRouter），
  或让某 key 首选某模型组/池（如 cursor key 首选 zerokey-pool，挂了回退账户池/网宿），
  不影响其他用户。Use when the user mentions "换供应商" / "对调首选备选" / "swap
  provider" / "改路由" / "首选 zerokey-pool" / "首选某模型" / "只对某人的 key 生效"
  + 某人名字或 key alias，或想让某个 claude-code / cursor key 的主供应商/主模型从 A 切到 B。
  覆盖 carher（命名空间 carher）与 litellm-product（198·NodePort 30402）两套环境。
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

## 场景：让单 key「首选某模型组 + 兜底链」（litellm-product / 198·30402 实战）

> 案例：让 `cursor-liuguoxian` 这把 key 首选 `zerokey-pool`，挂了再回退账户池 / 网宿，
> 其它 key 与全局路由零影响。2026-06-23 实测定型。脚本：`scripts/prod-patch-key-primary-zerokey.py`。

目标链路（仅该 key）：

```
客户端发 gpt-5.5 / chatgpt-gpt-5.5
  → zerokey-pool        (per-key alias，首选)
  →(挂) chatgpt-gpt-5.5  (ChatGPT 账户池，全局 fallback)
  →(再挂) wangsu-gpt-5.5 (全局 fallback)
```

### ⚠️ 关键认知（实测，别踩）

| # | 认知 | 证据 |
|---|---|---|
| 1 | **「首选」用 per-key `aliases`**：优先级高于全局 `model_group_alias`，只影响该 key | 带 alias 的 throwaway key 调 `gpt-5.5` → 落 zerokey deployment；master 调 `gpt-5.5` → 落 `chatgpt-acct-*`（隔离成立） |
| 2 | **「兜底链」必须写全局 `router_settings.fallbacks`**：litellm-product **不认 per-key fallback** | per-key `router_settings.fallbacks` 兜底实测**未生效**（2026-06-23，alias 到不存在组 + per-key fallback→wangsu，请求 25s 超时无 200）。⚠️ 与本文档上方 anthropic（carher 命名空间）的描述不同——**不同 LiteLLM 部署行为可能不同，以实测为准** |
| 3 | 全局给 `zerokey-pool` 加 fallback **不影响其他 key**：别人根本不调 `zerokey-pool`，只有设了 alias 的 key 会触发该条目 | buyitian 等默认 key 仍 `gpt-5.5 →alias→ chatgpt-gpt-5.5 →fallback→ wangsu-gpt-5.5` |
| 4 | **防 manifest 漂移**：直接 `kubectl apply` live cm 会让源文件 `/root/litellm-product-manifests/30-cm-litellm-config.yaml` 落后；下次谁重 apply 该文件会冲掉改动 | 必须把 cm 改动同步回写该 manifest（脚本已处理） |

### 全局路由配置文件（"每个 key 的路由"其实在这里）

- prod 没有"每 key 一个路由文件"；路由 = **全局** `litellm-config`（源文件 `/root/litellm-product-manifests/30-cm-litellm-config.yaml`） + 每 key 的 `models` allowlist / `aliases`。
- 账户池 `chatgpt-gpt-5.5`（多个 `chatgpt-acct-*`）是 **DB-managed**（`/model/new` 动态加）。
  **更正 2026-06-25：`zerokey-pool` 在 prod (198 litellm-product) 是 *config-managed*——14 个成员
  在 `litellm-config` ConfigMap 的 `config.yaml` `model_list` 里，DB `LiteLLM_ProxyModelTable`
  无 zerokey 行**（实测 + `scripts/prod-add-zerokey-accounts.py` docstring 确认）。改成员（加端口 /
  设 `model_info.id=acct-N`）要改 CM + `kubectl rollout restart deployment/litellm-proxy`。
  无论如何，查真实 deployment 都用 `GET /v1/model/info`，别只看 config 文件。

### 一键执行（推荐）

```bash
# 预览
python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian
# 执行：补 per-key alias(gpt-5.5/chatgpt-gpt-5.5→zerokey-pool) + 确保全局 fallback + 同步 manifest
python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian --apply
# 回滚（清 alias，回全局默认链）
python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian --rollback --apply
```

幂等：全局 fallback 已存在则不重启 proxy；只有 cm 真变了才滚动。

### 手动版（litellm-product master key 在 `litellm-secrets -n litellm-product`，NodePort 30402）

```bash
MKP=$(kubectl get secret litellm-secrets -n litellm-product -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
# 1) per-key alias（aliases 是完整替换）
curl -s -X POST localhost:30402/key/update -H "Authorization: Bearer $MKP" -H 'Content-Type: application/json' \
  -d '{"key":"<TOKEN>","models":[...,"zerokey-pool"],"aliases":{"gpt-5.5":"zerokey-pool","chatgpt-gpt-5.5":"zerokey-pool"}}'
# 2) 全局 fallback：在 router_settings.fallbacks 加 {"zerokey-pool":["chatgpt-gpt-5.5","wangsu-gpt-5.5"]}
#    改 live cm 后必须同步回写 manifest 源文件，再 rollout（仅当之前没有该条目时）
```

## 注意事项

- **aliases 只影响单个 key**，是最安全的路由切换方式
- **不要改全局 `model_group_alias`** 来实现单用户切换（那会动所有人）；但**兜底链 `fallbacks` 只能是全局**（per-key fallback 在 litellm-product 不可靠），靠"只有该 key 调该组"来保证隔离
- 如果目标模型组不在 key 的 `models` allowlist 里，需要先用 `/key/update` 加上
- per-key `router_settings.fallbacks` 和 `aliases` 是独立字段，更新一个不会清另一个（但前者在 litellm-product 实测不被路由采用，别依赖）
- 改 live configmap 后**务必同步回写 `/root/litellm-product-manifests/30-cm-litellm-config.yaml`**，否则漂移
