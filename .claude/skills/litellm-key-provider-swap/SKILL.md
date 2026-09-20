---
name: litellm-key-provider-swap
description: >-
  对单个 LiteLLM virtual key 改路由：对调首选/备选供应商（Wangsu Direct ↔ OpenRouter），
  或让某 key 首选某模型组/池（如 cursor key 首选 zerokey-pool，挂了回退账户池/网宿），
  或把某把 her key(carher-N) 的某个模型/模型族改打另一个上游（如「carher-1 的 opus
  全部打网宿」），不影响其他用户。Use when the user mentions "换供应商" / "对调首选备选" / "swap
  provider" / "改路由" / "首选 zerokey-pool" / "首选某模型" / "只对某人的 key 生效" /
  "把 X 的某模型映射打 Y" + 某人名字或 key alias，或想让某个 claude-code / cursor / carher
  key 的主供应商/主模型从 A 切到 B。
  覆盖 carher（阿里云·命名空间 carher·脚本 scripts/litellm-aliyun-key-repoint.py）
  与 litellm-product（198·NodePort 30402）两套环境。
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

## 场景：把 key B 的路由配置克隆成 key A 同款（litellm-product / 198·30402 实战）

> 案例：「把王忠伟的 claude key 改成和刘国现的 claude key 配置一样」。2026-06-25 实测定型。

### 该克隆什么 / 不该克隆什么

| 字段 | 是否克隆 | 说明 |
|---|---|---|
| `models` (allowlist) | ✅ | 决定可访问模型集 |
| `aliases` | ✅ | 决定首选 provider |
| `router_settings.fallbacks` | ✅ | 决定备选链 |
| `max_budget` / `budget_duration` | ❌ | 用户额度，跨人复制 = 越权扩 budget，单独问 |
| `metadata` / `email` / `user_id` / `team_id` / `display_name` / `organization_id` | ❌ | 身份字段 |
| `spend` / `created_at` / `key_name` | ❌ | 服务端字段 |

### 一键步骤

```bash
MKP=$(jms ssh AIYJY-litellm 'kubectl get secret litellm-secrets -n litellm-product -o jsonpath="{.data.LITELLM_MASTER_KEY}" | base64 -d')

# 1. 拿到 A、B 两 key 的 token（按人名搜，/spend/keys 比 /key/aliases 信息全）
jms ssh AIYJY-litellm "MKP='$MKP'; curl -s 'http://127.0.0.1:30402/spend/keys?limit=2000' -H \"Authorization: Bearer \$MKP\" | python3 -c '
import sys, json
for r in json.load(sys.stdin):
    ka=(r.get(\"key_alias\") or \"\").lower()
    if \"<NAME_A>\" in ka or \"<NAME_B>\" in ka:
        print(r[\"key_alias\"], r[\"token\"])'"

# 2. 拿 A 的 /key/info，提取 models + aliases + router_settings 三字段
#    （/spend/keys 不返 router_settings.fallbacks，必须走 /key/info）
jms ssh AIYJY-litellm "MKP='$MKP'; curl -s 'http://127.0.0.1:30402/key/info?key=<TOKEN_A>' -H \"Authorization: Bearer \$MKP\"" \
  | jq '.info | {models, aliases, router_settings}' > /tmp/key-a-config.json

# 3. 把 A 的三字段套到 B（注意 router_settings 整体替换，不要只传 fallbacks）
jms ssh AIYJY-litellm "MKP='$MKP'; curl -s -X POST 'http://127.0.0.1:30402/key/update' \
  -H \"Authorization: Bearer \$MKP\" -H 'Content-Type: application/json' \
  -d '{\"key\":\"<TOKEN_B>\", $(cat /tmp/key-a-config.json | jq -r 'to_entries | map(\"\\\"\\(.key)\\\":\\(.value|tojson)\") | join(\",\")')}'"

# 4. 验证：sha256 算 A/B 两 key 的「models+aliases+fallbacks」摘要，必须相等
jms ssh AIYJY-litellm "MKP='$MKP'
for T in <TOKEN_A> <TOKEN_B>; do
  curl -s \"http://127.0.0.1:30402/key/info?key=\$T\" -H \"Authorization: Bearer \$MKP\" | python3 -c '
import sys, json, hashlib
d=json.load(sys.stdin)[\"info\"]
sub={k:d[k] for k in [\"models\",\"aliases\",\"router_settings\"]}
sub[\"models\"]=sorted(sub[\"models\"])
sub[\"router_settings\"][\"fallbacks\"]=sorted(sub[\"router_settings\"][\"fallbacks\"], key=lambda x:list(x.keys())[0])
print(d[\"key_alias\"], hashlib.sha256(json.dumps(sub,sort_keys=True).encode()).hexdigest()[:16])'
done"
```

两边 sha256 完全相等才算克隆成功。

## 场景：把 key 收窄到模型子集（"只保留这 4 个，其他全删"）

把 `models` allowlist 收窄时，**必须同步清理 `aliases` 和 per-key `fallbacks` 里指向已删模型的条目**，否则：
- alias 源模型已不在 allowlist → alias 形同虚设但占字段
- fallback 条目里指向 wangsu-* / 网宿 target 不再相关 → 后续 audit 误导

```bash
# 收窄到 4 个国产模型（含 4 条 alias），清空 per-key fallbacks
curl -s -X POST "http://127.0.0.1:30402/key/update" \
  -H "Authorization: Bearer $MKP" -H "Content-Type: application/json" \
  -d '{
    "key":"<TOKEN>",
    "models":["claude-deepseek-v4-pro","claude-kimi-k2.7-code","claude-minimax-m3","claude-qwen3.7-plus"],
    "aliases":{
      "claude-deepseek-v4-pro":"deepseek-v4-pro",
      "claude-kimi-k2.7-code":"wangsu-claude-kimi-k2.7-code",
      "claude-minimax-m3":"minimax-m3",
      "claude-qwen3.7-plus":"wangsu-claude-qwen3.7-plus"
    },
    "router_settings":{"fallbacks":[]}
  }'
```

⚠️ `models` / `aliases` / `router_settings` 都是**整体替换**不是增量 merge。少传字段 = 该字段保持原值。

## 场景：改 key 的每日预算

```bash
curl -s -X POST "http://127.0.0.1:30402/key/update" \
  -H "Authorization: Bearer $MKP" -H "Content-Type: application/json" \
  -d '{"key":"<TOKEN>","max_budget":30,"budget_duration":"1d"}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print({k:d[k] for k in ["max_budget","budget_duration","budget_reset_at","spend"]})'
```

要点：
- `max_budget` 单位是 **USD**；`budget_duration` 走 `1d` / `7d` / `30d`，不是 cron 表达式
- 改 budget **不会清 `spend`**——当前 spend 已超新 budget 时，仍被挡到下次 `budget_reset_at`
- 想立即放行：单独 `POST /key/update` 带 `{"spend":0}` 重置，或等 reset 时间

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

## 198 Cursor DeepSeek 官方直连：双 alias 全量同步（2026-08-12）

用户说「198 上所有 cursor key 的 DeepSeek v4-pro 改官方 flash」时，不能只改裸名。
Cursor 实际会发两种产品名：

```json
{
  "deepseek-v4-pro": "deepseek-v4-flash",
  "claude-deepseek-v4-pro": "deepseek-v4-flash"
}
```

漏掉第二条时，日志会显示 `model=custom_openai/deepseek-v4-pro`，但
`model_group=claude-deepseek-v4-pro`、`model_id=claude/deepseek-v4-pro`，实际仍走网宿。
响应体中的 `model` 可以保留客户端请求名，不能作为 deployment 判据；必须看
`x-litellm-model-id`，官方应为 `deepseek-official/deepseek-v4-flash`。

### 固化脚本（198 / litellm-product）

脚本范围来自 LiteLLM `/key/list` 的全量 `cursor-*`，不是飞书先锋名单：

```bash
# 默认只读预览
python3 scripts/litellm-cursor-deepseek-official.py

# 单 key 灰度（先选低消费 key），非 dry-run 必须备份
python3 scripts/litellm-cursor-deepseek-official.py \
  --only cursor-example-xxxx \
  --backup ~/cursor-deepseek-canary-$(date +%Y%m%dT%H%M%S).json --apply

# 全量同步
python3 scripts/litellm-cursor-deepseek-official.py \
  --backup ~/cursor-deepseek-official-$(date +%Y%m%dT%H%M%S).json --apply

# 从备份恢复 aliases + models
python3 scripts/litellm-cursor-deepseek-official.py \
  --restore ~/cursor-deepseek-official-<timestamp>.json --apply
```

脚本必须 caller-side merge `aliases` / `models`，因为 `/key/update` 是整字段替换；保留
所有无关 alias、原有 allowlist、token、预算和身份字段。`models==[]` 表示不限制模型，
禁止写成窄 allowlist。执行前拒绝重复 alias/token；连续两次写失败立即停止；写后重新拉取
全量回读，双 alias、`deepseek-v4-flash` allowlist 及无关字段逐项校验。

### 证据与时间

- 生产 API 的时间字段是 UTC；UI 展示的「北京时间」先减 8 小时再与 `startTime` 比较。
- 看到旧网宿记录，先查该请求是否早于 key 的 `updated_at`，再讨论缓存传播；超过更新时间仍旧路由，
  必须逐 Pod `/key/info` 回读并做裸名/前缀名对照 smoke。
- 不需要 rollout：per-key `/key/update` 立即更新管理配置；若 Pod 间结果不一致，才进一步查缓存传播。

## 阿里云 her key：deepseek-v4-flash → 自建盒（2026-08-19/20 实战）

> 🔴 **结论已再次反转，本节保留只为方法论。** 2026-09-10 起 her 的
> `deepseek-v4-flash` **走官网**：`carher-1` 实测 alias 为
> `deepseek-v4-flash → official-deepseek-v4-flash`
> （`deepseek-official/deepseek-v4-flash-fallback`, base `https://api.deepseek.com/v1`），
> CM 里盒子那个组原样留着但零流量。⛔ 别再说"her 在用盒子"。
> 详见 memory `project_aliyun_her_dsflash_official_2026_09_10`。
> 本节下面那些「切盒子」的操作细节**不要照着执行**，只看它的方法论：
> 组名不是判据、切上游必配 fallback + clamp、验收探针要复刻 key 形状。

**平台归属先搞对**：her(carher-*)key **只在阿里云 litellm(ns `carher`,经
`jms ssh k8s-work-226`)有效**;198 上也有一批 `carher-*` key 但不被使用——
"改 her 的模型映射"打到 198 是整轮白跑。同名组两边含义也曾相反
(`local-deepseek-v4-flash`:应=自建 GPU 盒 36.151.241.10:8000,
官方直连降为 `official-deepseek-v4-flash` fallback)。

> ⚠️ **组名不是判据,`api_base` 才是。** 2026-09-09 复查发现 08-19 的组改动
> **已不在 live CM**:`local-deepseek-v4-flash` 又指回 `api_base:
> https://api.deepseek.com/v1`、`id: deepseek-official/deepseek-v4-flash`,
> `official-deepseek-v4-flash` 组和两条 deepseek fallback **整段消失**,
> 而 clamp callback 还活着(所以探针只测 clamp 会绿)。**谁在什么时候改掉的没有数据**
> (`updated_at` 类时间戳答不了 provenance),已 09-09 重切。
> ⇒ 任何时候声称"her 在用盒子"之前先跑一句:
> `kubectl -n carher get cm litellm-config -o jsonpath='{.data.config\.yaml}' | grep -n 36.151.241.10`
> 零命中就是没在用。定期核这一条,别信记忆里的"已上线"。

**全 fleet 已 alias 到同一组名时,换上游改组本身,不要碰 N 把 key**:
347 把 key 的 `deepseek-v4-flash => local-deepseek-v4-flash` 已就位,把该组的
`api_base` 从官方切到盒子 = 一次 CM 变更全量生效,key 零改动、零回读负担。

**阿里云 litellm 配置纪律(与 198 相反)**:init 容器 `wipe-db-config-rows`
重启即清 DB config 行 → 配置正统在 CM `litellm-config`。改组/加组/改 fallback
一律**编辑 CM + rollout restart**(2 副本滚动零中断),禁用 `/model/new`、
`/config/update`。rollout 卡 Pending 数分钟=hostPort 被 Terminating 旧 pod
占着,等即可,不是配置错。

**切自建盒必配套两件事**(否则流量落不到盒子,白切):
1. fallback:`fallbacks` + `context_window_fallbacks` 都加
   `local-deepseek-v4-flash → [official-deepseek-v4-flash]`(单上游无兜底=单点)
2. max_tokens 钳制 hook `local_gpu_max_tokens_clamp.py`:her catalog 发
   `max_tokens=384000`,盒子 393216 且输入+输出合算 → 不钳则真实会话全 400
   回落官方。`litellm_params.max_tokens` 是 default 压不过客户端值,只能 hook。

### 固化脚本(阿里云 / ns carher,pod 内执行)

```bash
# 投递方式见脚本 docstring(base64 → kubectl cp → exec)
python3 /tmp/dsbox.py summary   # 只读:全量 carher-* 映射覆盖率(新 key 漂移检查)
python3 /tmp/dsbox.py backup    # 只读:缺口 key 现状存档
python3 /tmp/dsbox.py apply     # 补 alias+裸名;连续2败停;--only=carher-N 金丝雀
python3 /tmp/dsbox.py verify    # 全量回读,期望 unmapped=0
python3 /tmp/dsbox.py probe     # 判别式验收:20k输入+mt=384000 必须落 local-gpu
                                # (小输入+384000 是假证明:20+384000<393216 盒子本来就收)
```

源文件 `scripts/litellm-aliyun-her-dsflash-local-box.py`。
**新 key 漂移已修源头**:`backend/litellm_ops.py` `DEFAULT_KEY_ALIASES`
(旧模板 `aliases:{}` 造成 carher-351~354 漂移;admin 镜像重建生效前,
新实例上线后跑一次 `summary`/`apply` 兜底)。
全记录:memory `project_aliyun_her_dsflash_to_local_gpu_box_2026_08_19`。

### 2026-09-09 重切:完整 SOP + 四条实测认知

用户约束是「**不要改 her 的配置,只改 litellm 端 her 的 key 的配置**」——
即不碰 user-config / base-config / CRD、不重启 her pod。落地=只动 CM 组上游 +
router fallbacks，**372 把 key 一把没写**。

**动手前先摸盒子**(顺序不能反):盒子引擎从 09-06 17:24 就死着、daynight cron
自 08-11 全 PAUSED——**切过去等于把 her 打进黑洞**。必查:
`docker ps | grep dspark`、`curl :8767/v1/models`、`crontab -l`。

**CM patcher 的四条断言**(外科式文本编辑,不重排整份 YAML,写完 `yaml.safe_load` 校验):
组数 163→165、`added == {official-deepseek-v4-flash, ...}`、`removed == {}`、
**除目标组外每个组逐字节相同**(`ib[name] == ia[name]`)。
router_settings 整块替换必须带**原文断言**(`new.count(ROUTER_OLD) == 1`)。
备份 `/root/litellm-config-backup-<ts>.yaml`,回滚 = 恢复该文件 + rollout。

1. **fallback 目标不受 key 白名单限制**(实测,别再猜)。造一个死端口组
   `zz-dsprobe-dead` + 临时 key(`models` 里**故意不含**官方组名)→ 200 落
   `deepseek-official/...-fallback`。⇒ 加 fallback 组**不需要**给 375 把 key 扩白名单。
   临时探针组验完必须 `cleanup` 摘掉(脚本自带,断言零残留)。
2. **`context_window_fallbacks` 用的是路由器自算的 token 数,不是真实 prompt_tokens**——
   见 [[feedback_litellm_context_window_check_uses_router_estimate]]。
   `max_input_tokens: 320000` 实际在真实输入 ~278k 就开始回落官方(路由器数成 329k)。
   要让长会话留在盒子就提这个值;提高的下行风险只是多一次往返
   (盒子 400 → 普通 `fallbacks` 仍兜到官方),不会打到用户。
3. **验收探针必须复刻 her key 的形状**:临时 key `models=["deepseek-v4-flash",
   "deepseek-v4-pro"]` + `aliases` 指目标组 + 带 tools。四道闸:tools 流式/非流式落盒子、
   22k 输入 + `max_tokens=384000` 仍落盒子(证明 clamp 活着)、~330k 输入落官方。
   **两个 pod 都要各跑一遍**;超长那一枪的字符串长度自己先算准
   (一次算成 761k token,超过官方腿 393216 → 400,读起来像"窗口 fallback 坏了",
   是量具坏不是配置坏)。
4. **漂移 key 只补这次要的那两条,别整体照抄参照 key**。09-09 发现 `carher-422`/
   `carher-378` 只有 `{"grok":"grok-4.6"}`,缺 deepseek 两条 → 走网宿。参照 key
   `carher-333` 有 17 models/13 aliases,整体 merge 会连 claude/gemini/qwen 的映射
   一起改 = 超范围。只 union `deepseek-v4-flash`/`deepseek-v4-pro`,并在写前断言
   `set(cur) <= set(new)` 零删除(`/key/update` 是整字段替换,必须 caller-side merge)。

**判"盒子接到流量了吗"只认盒子侧**(litellm 的 SpendLogs 只能证明它自己认为发出去了):
`journalctl -u deepseek-v4-flash-public-8000.service` 看源 IP 是不是 K8s 出口
`47.84.112.136` + 200;`docker logs deepseek-v4-dspark` 看 `#running-req` / gen throughput。
⚠️ `nvidia-smi utilization.gpu=0%` **不是没流量**——见
[[feedback_gpu_util_snapshot_and_silent_watchdog_are_bad_liveness_rulers]]。

**盒子侧必须配 watchdog,且窗口要开成 24h**:`watchdog_infer.sh` 默认只在
`08:50–23:25` 自愈(为配合 23:30 腾卡给训练)。不恢复夜间停机时必须在 cron 行上覆盖:
`*/2 * * * * WATCHDOG_INFER_START_HM=0 WATCHDOG_INFER_END_HM=2359 .../watchdog_infer.sh`
否则夜里引擎挂了没人拉。全记录:
memory `project_aliyun_her_dsflash_box_recut_2026_09_09`。

## 场景：单把 her key 的某个模型改打别的上游（阿里云 / ns `carher`，2026-09-10 定型）

> 案例：「把阿里云上 `carher-1` 的 opus 系列全部映射打网宿」。用户补了一句
> 「**改 litellm 不要改其他**」＝只动 key，不碰 CM、不 rollout、不碰 her 配置 / HerInstance。
> 固化脚本：`scripts/litellm-aliyun-key-repoint.py`（groups / inspect / plan / apply / probe / rollback）。

### ⛔ 组名会撒谎——判 provider 只认 `api_base` + `model` + `model_info.id`

阿里云 CM 里组名与真实上游**大面积不一致**，按名字猜必然选错 target：

| 组名 | 名字暗示 | 实际上游（实测 2026-09-10） |
|---|---|---|
| `claude-opus-4-8` | 中性 | **网宿** `wangsu-direct5/claude-opus-4-8` |
| `openrouter-claude-opus-4-8` | OpenRouter | **网宿**，同一个 `wangsu-direct5/claude-opus-4-8` |
| `openrouter-claude-opus-4-8-fast` | OpenRouter | **网宿**，同上 |
| `anthropic.claude-opus-4-7` | 官方/网宿 | **快汇** `kuaihuiai.com` |
| `anthropic.claude-opus-4-8` | 官方/网宿 | 网宿（这个倒是对的） |
| `wangsu-gemini-3.1-pro-preview` | gemini 3.1 pro | 真实 model = **`gemini-3.5-flash`** |
| `wangsu-glm-5.1` | glm 5.1 | 真实 model = **`glm-5.2`** |
| `claude-opus-4-6` vs `anthropic.claude-opus-4-6` | 同源 | **不同网关租户**（`yqhhclqf` vs `sqix2pnh`） |

动手前先跑真相表，别翻记忆：

```bash
python3 /tmp/repoint.py groups --grep opus   # 组名 → id / model / api_base
```

### 目标组本身就是想要的上游时，正解是「摘掉 alias」不是写新 alias

`carher-1` 原本 `claude-opus-4-8 → chatgpt-gpt-5.6-sol`（劫到 ChatGPT 账户池）。
而裸组 `claude-opus-4-8` 本身就是网宿。所以切网宿 = **删掉那条 alias**，让裸名落回同名真实组：

```bash
python3 /tmp/repoint.py apply --key carher-1 --map 'claude-opus-4-8='   # TARGET 留空 = 摘除
```

比写 `{"claude-opus-4-8": "anthropic.claude-opus-4-8"}` 干净：少一层间接、不用担心自指。
**前提是先确认 CM 里 `model_group_alias` 为空**——它会无条件压过同名真实组
（见 [[feedback_litellm_alias_fallback_pre_rewrite_lookup]]）。阿里云这份实测为 `None`，脚本每次 plan 都会复查并打印。

### 「某某系列全部」先读 allowlist 再谈"全部"

用户说的是「opus **系列全部**」，但 `carher-1` 的 `models` 里 opus 只有 `claude-opus-4-8` 一个，
`claude-opus-4-6/4-7` 根本不在白名单里 —— 按模型家族想当然去铺，会写出一堆死条目。
`inspect` 一把看清每个请求名现在打谁，再决定"全部"到底是几条：

```bash
python3 /tmp/repoint.py inspect --key carher-1
```

要真扩到 4-6/4-7 那是**扩能力**不是改路由，得单独确认（脚本 `--allow a,b` 只增不删）。

### 拿不到 key 明文时怎么做真流量验收

库里只有 token hash，`carher-N` 也**不保证有对应 HerInstance**（CRD 名是 `her-N`，
`her-1` 实测 NotFound），别指望从 CRD 掏 key。做法是造一把**复刻目标 key 当前形状**
（同 `models` + 同 `aliases` + `duration`）的临时 key，对**每一个** proxy pod 各打一发：

```bash
python3 /tmp/repoint.py probe --key carher-1 --model claude-opus-4-8 \
  --expect wangsu-direct5/claude-opus-4-8
```

判据两条缺一不可：响应头 `x-litellm-model-id` 等于期望 deployment（**响应体的 `model` 会
原样回显客户端请求名，不是落点判据**），且唯一 nonce 真的被吐回来。跑完自动删临时 key
并断言 `zz-probe*` 零残留。

### 隔离对照：改前改后数 fleet 分布

```
改前 372 把 carher-* 是 {"claude-opus-4-8": "chatgpt-gpt-5.6-sol"}
改后 371 把         ← 只少了目标那一把，其余零误伤
```

### 其它实测认知（2026-09-10）

- **alias 的 target 不需要在 `models` allowlist 里**：`carher-1` 的
  `chatgpt-gpt-5.6-terra` 不在白名单却正常落地。（与"白名单缺 alias 的裸名会 400"是两回事）
- **per-key 改动不受 CM rollout 影响**：本轮期间阿里云 litellm 恰好滚了一轮
  （pod 从 `586dff48d6-*` 变 `5ddb69d588-*`），opus 改动照样活着——key 配置在 DB 不在 CM。
  所以 probe 必须按 label 现查 pod，别写死 pod 名/IP。
- **阿里云 carher-* 也会有并发批量写**：同一天 `carher-1` 的 `models` 被别的会话从 18 改到 21。
  `/key/update` 是整字段替换 ⇒ 一律 read-merge-write，写前断言"除点名项外逐条相同、零删除"。
- **这条网宿腿没有兜底**：CM 全局 `fallbacks` / `context_window_fallbacks` 里一条 opus 都没有，
  该 key 的 `router_settings` 是 `{}`。改前打 ChatGPT 池同样没兜底 ⇒ 不算回退，但要跟用户讲明是单上游；
  真要加兜底只能改全局 CM + rollout，那就超出"只改 litellm key"的范围了。

## 场景：单 key 改映射 **并且**要兜底（198 / ns `litellm-product`，2026-09-17 定型）

> 案例：「把 198 上 `carher-1` 的 fable-5.1 映射到 9router 的 fable-5.1 并 fallback 到
> wangsu 的 opus-5，opus-5 映射到 9router 的 opus-5 fallback 到 wangsu 的 opus-5」。
> 固化脚本：**`scripts/litellm-198-key-alias-fallback-pair.py`**
> （预览 / `--apply` / `--probe` / `--restart` / `--rollback --from`）。

```bash
# 预览（只读，会打真相表 + 列出要 append 的 fallback 行）
python3 scripts/litellm-198-key-alias-fallback-pair.py --key carher-1 \
  --map claude-fable-5.1=cursor-fc-fable-5.1 --map claude-opus-5=cursor-fc-opus-5 \
  --fallback-to anthropic.wangsu5.claude-opus-5
# 真做（alias 立即生效，fallback 落库但**未加载**）
... --apply
# 验收（克隆 key 形状打真实推理 + 阴性对照 + SpendLogs）
... --probe
```

### ★ alias 的**源名和目标名**都要有自己的 fallback 行

router 解析顺序是
`specific_deployment → has_model_id → _get_model_from_alias 改写 → _get_all_deployments`，
但 **fallback 表是拿改写前的名字去查的**。所以 key 上 `A → B` 的 alias，
`A` 和 `B` **各自**都要一行、且目标列表相同。只写 B 那行，等 B 的上游真挂了会直接硬 5xx：
`No fallback model group found for original model_group=A` —— 兜底等于从来没存在过。
见 [[feedback_litellm_alias_fallback_pre_rewrite_lookup]]。本例四行一次写齐：

```
{"cursor-fc-fable-5.1": ["anthropic.wangsu5.claude-opus-5"]}
{"cursor-fc-opus-5":    ["anthropic.wangsu5.claude-opus-5"]}
{"claude-fable-5.1":    ["anthropic.wangsu5.claude-opus-5"]}
{"claude-opus-5":       ["anthropic.wangsu5.claude-opus-5"]}
```

### 198 的 fallback 在 **DB** 不在 CM，且**不热加载**

`STORE_MODEL_IN_DB=True` ⇒ 正统是 `LiteLLM_Config.param_name='router_settings'`。
只 patch CM 是纯 no-op。写法必须是 `jsonb_set` **只动 `{fallbacks}` 这一格的追加**，
⛔ 禁整字段覆盖、⛔ 禁 `POST /config/update`（实测会静默把兄弟项 `model_group_alias`
从 10 条抹成 `{}`，而且要等下次重启才炸）。断言：前 N 条逐字节不变 + 新增恰好是那几行。

**写完 ≠ 生效**：router 只在 boot 读 `router_settings`，写后轮询 7 分钟毫无变化。
所以 `--apply` 之后是「主路已通、兜底待装」这个中间态，脚本**只打印** rollout 命令不执行
—— 滚 4 副本生产 proxy 得用户点头。alias 本身是立即生效的，两者不要混为一谈。

⚠️ **fallback 是按组名全局的**，198 没有 per-key 粒度（见本文上面 2026-06-23 实测）。
给 `cursor-fc-*` 加一行，carher-14 和所有别的 cursor-fc 消费者一起获得这条兜底 ——
是**加法不是删除**，但汇报时必须讲明。

### ⛔ 别拿 DB 里的 `token` 当 Bearer 去探针

`LiteLLM_VerificationToken.token` 是 **sha256 哈希**不是明文 `sk-`。拿它打请求，
**任何模型名都 401，包括根本不存在的那个** ⇒ 阴性对照失去分辨力，整轮读数作废
（一次真踩：三发全 401，差点读成"权限没开"）。
正解是**克隆 key 形状**：`/key/generate` 带同样的 `models`+`aliases`+`duration`，
用完在 `finally` 里删掉。见 [[feedback_litellm_key_probe_clones_key_shape]]。

判据三条，缺一不可：
1. HTTP 200 **且唯一 nonce 被回显**（响应缓存是开的，逐字节相同的枪不到上游）。
2. 一个不存在的模型名必须 **403**（阴性对照，没有它上面的绿不算数）。
3. 落点看 **SpendLogs 的 `model_id`**。响应头 `x-litellm-model-id` 在 198 返的是
   内部哈希（如 `e50faa178dff`）不是声明的 id，当不了判据。
4. ⚠️ `x-litellm-attempted-fallbacks: 0` 配 **HTTP 200 是正确读数**——主路直接答了、
   没必要兜底。它只有配 5xx 才是红旗。

⚠️ 写完 key 后**另一个 proxy pod 约 2 分钟才认**，当场 403 是传播延迟不是写失败；
脚本 `--apply --probe` 连用时会先 sleep 120s。

### 顺带两条查法

- **「key X 存在吗 / 带了什么」只能查 DB 表 `LiteLLM_VerificationToken`**：
  `/key/list` 按调用者 scope 截断（master key 调也只返 10 行、没有 carher-1）。
- **`/model/info` 在 198 约 10MB**（1597 个 deployment），经 ssh→kubectl 传出来会
  半路截断成坏 JSON。任何读它的代码**必须在 pod 内先 reduce**（脚本的 `reduce_py`）。
- 嵌套 `ssh→kubectl→psql` 会**吃掉 psql 的双引号**，`"LiteLLM_SpendLogs"` 被小写成
  `relation does not exist`。解法：base64 注入一个 `.sql` 文件再 `psql -f`。
  198 的库角色是 **`litellm`**（不是 `postgres`，那个角色不存在）。

## 注意事项

- **aliases 只影响单个 key**，是最安全的路由切换方式
- **不要改全局 `model_group_alias`** 来实现单用户切换（那会动所有人）；但**兜底链 `fallbacks` 只能是全局**（per-key fallback 在 litellm-product 不可靠），靠"只有该 key 调该组"来保证隔离
- 如果目标模型组不在 key 的 `models` allowlist 里，需要先用 `/key/update` 加上
- per-key `router_settings.fallbacks` 和 `aliases` 是独立字段，更新一个不会清另一个（但前者在 litellm-product 实测不被路由采用，别依赖）
- 改 live configmap 后**务必同步回写 `/root/litellm-product-manifests/30-cm-litellm-config.yaml`**，否则漂移
