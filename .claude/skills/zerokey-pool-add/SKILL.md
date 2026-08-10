---
name: zerokey-pool-add
description: >-
  管理独立的 zerokey-pool（与 acct-pool chatgpt-gpt-* 完全隔离）：
  注册/摘除 zero-N pods、批量切换 cursor key 到 zerokey-pool 路由、
  把全量 cursor key 对齐到某个参照 key 的基线（align）。
  Use when 用户说"zerokey-pool"/"zerokey 加入池"/"切 zerokey"/"先锋 key 切池"/
  "所有 cursor key 改成和 X 一样"。
---

# zerokey-pool 管理

## ⚠️ 新号进路由必装 agent 契约（2026-08-10 硬规则）

任何 zero-N 账号**加入 zk 路由**（或死号复活重新入路由）后，必须给它装账号级
agent 契约，否则该号的网页模型没有说明书，上来就是"我没有权限/请提供任务"
的旧行为（实测无契约拒答 5/5）：

```bash
scp scripts/chatgpt-onboard/zerokey-codex/engine/instructions.md \
    cltx@10.68.13.198:/tmp/upstream_instructions.md
scp scripts/zerokey-agent-contract-rollout.py cltx@10.68.13.198:/tmp/
ssh cltx@10.68.13.198 'sudo python3 /tmp/zerokey-agent-contract-rollout.py rollout'
# rollout 自动只覆盖"路由里在用的号"，重复执行幂等；status 子命令可核对
```

契约正文（唯一真源）：`scripts/chatgpt-onboard/zerokey-codex/engine/instructions.md`。
改契约措辞 = 改这个文件 + 重跑 rollout；别直接在 198 /tmp 上改（会被清）。
范围事实（2026-08-10 实查）：集群 68 个 zero deployment / 62 在跑，但接
zerokey 网页流量的只有 zk 路由里的 ~18 个号 —— 其余是 acct 桥（契约无效，
不用装）和停用号（无流量）。

## 架构概览

198 prod LiteLLM 有两套独立的 ChatGPT 路由池，**互不 fallback**：

| 池 | model_name 前缀 | 后端 | 管理脚本 |
|---|---|---|---|
| acct-pool | `chatgpt-gpt-*` | chatgpt-acct-* 子代理 (198) | quota-rebalance.py |
| zerokey-pool | `zerokey-pool-gpt-*` | zero-* codex bridge (225) | zerokey-pool-register.py |

## Model Groups（6 组，对齐 acct-pool）

| zerokey-pool model_name | litellm model slug | bridge 解析 |
|---|---|---|
| zerokey-pool-gpt-5.5 | openai/gpt-5-5 | WEB_MODELS 直匹配 |
| zerokey-pool-gpt-5.4 | openai/gpt-5.4 | ALIASES → gpt-5-4-thinking |
| zerokey-pool-gpt-5.3-codex | openai/gpt-5-3 | WEB_MODELS 直匹配 |
| zerokey-pool-gpt-5.6-sol | openai/gpt-5.6-sol | passthrough |
| zerokey-pool-gpt-5.6-terra | openai/gpt-5.6-terra | passthrough |
| zerokey-pool-gpt-5.6-luna | openai/gpt-5.6-luna | passthrough |

## Entry 规格

- ID 格式：`zk-{N}-{model-suffix}`（如 `zk-18-gpt-5.5`、`zk-18-gpt-5.6-sol`）
- api_base：`http://zero-{N}.litellm-product.svc.cluster.local:8200/v1`
- api_key：`raw`（bridge 不做 auth）
- mode：`responses`
- rpm：30

## 注册脚本

```bash
# 批量注册所有健康 zero-N pods（27 pods × 6 models = 162 条）
python3 scripts/chatgpt-onboard/zerokey-codex/ops-prod/zerokey-pool-register.py

# 确认注册结果
curl -s http://10.68.13.198:30402/pro/model/info \
  -H "Authorization: Bearer sk-pro-litellm-ce077e2b0721bb419a633e4d" | \
  python3 -c "
import json, sys
from collections import Counter
d = json.load(sys.stdin)
c = Counter(m['model_name'] for m in d.get('data',[]) if m['model_name'].startswith('zerokey-pool'))
for k in sorted(c): print(f'  {k}: {c[k]}')
"
```

## 手动注册单个 pod（一个 pod 注册 6 条）

```bash
for model_suffix in gpt-5.5 gpt-5.4 gpt-5.3-codex gpt-5.6-sol gpt-5.6-terra gpt-5.6-luna; do
  # model slug mapping
  case $model_suffix in
    gpt-5.5)       slug="openai/gpt-5-5"; ic=5e-6; oc=3e-5 ;;
    gpt-5.4)       slug="openai/gpt-5.4"; ic=2e-6; oc=8e-6 ;;
    gpt-5.3-codex) slug="openai/gpt-5-3"; ic=1e-6; oc=4e-6 ;;
    gpt-5.6-sol)   slug="openai/gpt-5.6-sol"; ic=5e-6; oc=3e-5 ;;
    gpt-5.6-terra) slug="openai/gpt-5.6-terra"; ic=2.5e-6; oc=1.5e-5 ;;
    gpt-5.6-luna)  slug="openai/gpt-5.6-luna"; ic=1e-6; oc=6e-6 ;;
  esac
  curl -s -X POST http://10.68.13.198:30402/pro/model/new \
    -H "Authorization: Bearer sk-pro-litellm-ce077e2b0721bb419a633e4d" \
    -H "Content-Type: application/json" \
    -d "{
      \"model_name\": \"zerokey-pool-${model_suffix}\",
      \"litellm_params\": {
        \"model\": \"${slug}\",
        \"api_base\": \"http://zero-N.litellm-product.svc.cluster.local:8200/v1\",
        \"api_key\": \"raw\", \"rpm\": 30,
        \"input_cost_per_token\": ${ic}, \"output_cost_per_token\": ${oc}
      },
      \"model_info\": {\"id\": \"zk-N-${model_suffix}\", \"mode\": \"responses\"}
    }"
done
```

## 摘除单个 pod（删 6 条）

```bash
for suffix in gpt-5.5 gpt-5.4 gpt-5.3-codex gpt-5.6-sol gpt-5.6-terra gpt-5.6-luna; do
  curl -s -X POST http://10.68.13.198:30402/pro/model/delete \
    -H "Authorization: Bearer sk-pro-litellm-ce077e2b0721bb419a633e4d" \
    -H "Content-Type: application/json" \
    -d "{\"id\": \"zk-N-${suffix}\"}"
done
```

## 切换 Cursor Key 到 zerokey-pool（当前范式，2026-07-23 定型）

**首选脚本（本机即可跑，纯管理 API，不碰 DB / 不 rollout）：**

```bash
# 只读盘点：飞书先锋=zerokey 名单里哪些已切/待切
python3 scripts/zerokey-pioneer-key-sync.py status

# 批量切换所有待切户（幂等，已切跳过；--only 限定，--dry-run 预览）
python3 scripts/zerokey-pioneer-key-sync.py switch

# 回滚：把误切户恢复到与某参照 key 完全一致（拷贝其 aliases+models）
python3 scripts/zerokey-pioneer-key-sync.py rollback --ref cursor-biancaoming \
    --only cursor-guran,cursor-linsen

# 冒烟：发流式请求验响应头落 zk-*
python3 scripts/zerokey-pioneer-key-sync.py smoke --only cursor-foo
```

## 全量对齐基线：`align`（2026-07-27 加）

**"把所有 cursor key 的模型配置改成和 `<某 key>` 一样"用这个，不要用 rollback。**

```bash
# 先 dry-run 看范围（必做）
python3 scripts/zerokey-pioneer-key-sync.py align --ref-alias cursor-linsen-03gc \
    --exclude cursor-liuguoxian-l08v --dry-run

# 执行（非 dry-run 强制要 --backup，否则拒跑）
python3 scripts/zerokey-pioneer-key-sync.py align --ref-alias cursor-linsen-03gc \
    --exclude cursor-liuguoxian-l08v --backup ~/cursor-align-$(date +%Y%m%d).json
```

跑完自动重拉 `/key/list` 复查，并报「仍不一致的 key」+「仍带 zerokey alias 的 key」。

### align vs rollback：范围来源不同，别混

| | 名单来源 | 适用 |
|---|---|---|
| `switch`/`status`/`rollback` | **飞书先锋列**（120 条）| 先锋子集操作，靠 `--only` 点名 |
| `align` | **LiteLLM `/key/list` 全量**（530 把 `cursor-*`）| "所有 cursor key 都改成 X 那样" |

⚠️ **"所有 cursor key" ≠ 飞书先锋名单**。飞书只有 120 条先锋，LiteLLM 里有 530 把 cursor key。用 rollback 做全量会漏 410 把。

⚠️ **`--ref` 按飞书 key_alias 查会撞重名**：飞书有**两行** `cursor-liuguoxian`，`fetch_key_token()` 取第一个匹配，可能拿到 `-alie`（0 aliases/25 models 空壳）而非 `-l08v`。`align --ref-alias` 走 LiteLLM 全名（带 `-{4char}` 后缀）所以无歧义——**参照 key 一律用带后缀的 LiteLLM key_alias**。

### `/key/list` 的两个坑（2026-07-27 实证）

- 分页参数是 **`size`**，传 `page_size` 直接 **422**
- 不加 **`return_full_object=true`** 时 `keys` 是 **str 列表**（只有 token），拿不到 aliases/models
- 返回的 `token` 是 **hash 不是 `sk-` 明文**，但 **`/key/update` 的 `key` 字段接受 hash** —— 所以批量对齐**无需回飞书查明文 token**，这是 align 能脱离飞书跑全量的前提

**机制（脚本封装的核心，手动做也照此）——逐 key 调 `/pro/key/update` 的 caller 端 merge：**

1. GET `/pro/key/info?key=<sk-token>` 读现有 aliases/models
2. aliases **合并追加 10 条**（5 个 GPT 产品名 × {裸名, `chatgpt-` 前缀} → `zerokey-pool-*`）：
   `gpt-5.5 / gpt-5.4 / gpt-5.6-sol / gpt-5.6-terra / gpt-5.6-luna`
3. models allowlist **追加 5 个** `zerokey-pool-gpt-5.5/5.4/5.6-sol/5.6-terra/5.6-luna`
4. **保留**各 key 已有的 `glm/kimi/deepseek` 经济路由 aliases 与原 models（**不覆盖**）
5. POST `/pro/key/update`，缓存自动刷新，**不需要 rollout**

master key / base：`sk-pro-litellm-ce077e2b0721bb419a633e4d` @ `http://10.68.13.198:30402/pro`（脚本内可用 env `LITELLM_MASTER_KEY`/`LITELLM_BASE` 覆盖）。

### ⚠️ 不切 gpt-5.3-codex

codex 后端对 `gpt-5.3-codex` slug 返 400，切到 zerokey-pool 只会 fail→fallback 拖慢。已切户（cursor-zhangkairui 等）也不含 5.3-codex 的 zk alias。留在 acct-pool。`gpt-5.2`、`gpt-5.4-mini` 无 zerokey-pool 对应组，同样不动。

### 回滚到 acct-pool 基线

- **子集回滚**（几把误切户）：`rollback --ref <非先锋基线 key> --only a,b`
- **全量回滚**（所有 cursor key）：用上面的 **`align`**，别用 rollback

acct-pool 基线参照 key 形态（`cursor-linsen-03gc`，2026-07-27 实测）：**6 条 alias 全是经济路由**（glm×4 / kimi / deepseek），**无任何 GPT alias**——GPT 走 router 的 `model_group_alias` → `chatgpt-gpt-5.x` 账号池，26 models，不含 `zerokey-pool-*` / `zerokey-codex-*`。

**⚠️ 回滚后飞书先锋列会与 LiteLLM 不一致**：飞书还标着 `先锋=zerokey`，LiteLLM 已撤回账号池。下次谁跑 `switch` 会**按飞书名单重新切回 zerokey**（飞书先锋列是 switch 的唯一真源）。长期回滚必须同步清飞书先锋列；临时切换可以先不管，但要明确告知用户这个状态不一致。

### 2026-07-27 全量撤回实录

用户要求「除 liuguoxian 外，所有 cursor key 的模型配置改为和 `cursor-linsen-03gc` 一样」：

- 530 把 `cursor-*`，441 把本来已一致，**实改 88 把**（`ok=88 fail=0`）
- 净效果：**75 把从 zerokey 撤回账号池**（71 把 `zerokey-pool-*` + 4 把 `zerokey-codex-*`），等于把 07-21~23 那两次先锋切换（24→81 把）在 LiteLLM 侧归零
- 复查：529/530 一致，残余 zerokey alias 仅 `cursor-liuguoxian-l08v`（有意排除）
- 冒烟：GPT 落 `chatgpt-acct-*`（1.7-5.1s，比 bridge 的 11-15s 快），glm 落 `openrouter/z-ai/glm-5.2`
- 备份 `~/cursor-mass-backup-20260727.json`

**先做金丝雀**：批量前先拿一把 `$0` 消费的 key（如 `cursor-liuguoxian-alie` 这种空壳）单独跑一次并回读校验，确认 hash token 能被 `/key/update` 接受，再铺开。

### 旧写法已废弃（勿用）

- ~~直接改 DB 全量替换 aliases（含 5.3-codex）+ kubectl cp/psql~~：会抹掉经济路由 aliases，且切了 5.3-codex。
- `scripts/prod-patch-key-primary-zerokey.py`：单 key + kubectl 改 configmap + 全局 fallback 链，是 codex-pool 之前的旧架构，勿与本脚本混用。

### 飞书 bitable 坐标

base_token=`DlT9bsrwMad12VsogEpcK9Ptncc`，table_id=`tblJT2s6Y6xjYj5A`，先锋字段=`fldBPzCQDz`（single-select，值 `zerokey`/`ccmax`/空），key_alias 字段=`fldUgZxOOt`，`API Key` 字段含真实 sk-token。飞书 `cursor-xxx` ↔ DB `cursor-xxx-{4char}` 后缀；脚本用 sk-token 直连，无需做后缀匹配。
