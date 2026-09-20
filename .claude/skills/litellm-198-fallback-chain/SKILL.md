---
name: litellm-198-fallback-chain
description: >-
  在 198（ns litellm-product）改**全局 `router_settings.fallbacks` 某一行**的兜底链：
  插链首 / 换目标 / 回滚，并按「配置→装载→兜住」三层分别验收。含为什么全局一行能覆盖
  665 把 cursor key（per-key alias 用改写后的名字）、为什么 per-key fallbacks 反而覆盖不了
  （整表替换不是合并）、`router_settings` 每 30s 热加载（旧「必须 rollout」已作废）、
  以及六个会让你把「兜底存在」误读成「兜住了」的坏尺子。
  Use when the user mentions 兜底/fallback 链、"X 挂了兜到 Y"、改 fallbacks、
  兜底没生效、兜底能不能兜住、fallback 回归验证、sa-grok-4.6、
  或要给某个模型组换/加备用腿。
---

# 198 全局兜底链改法与三层验收

> 作用域：**198，ns `litellm-product`**。阿里云（ns `carher`）是另一套，
> 别把这里的 DB 写法搬过去。改**单把 key 的 alias/映射**不看这篇，看
> [[litellm-key-provider-swap]]；装 **Router 猴补丁**改重试决策看
> [[litellm-198-router-patch]]。

脚本：`scripts/litellm-198-fallback-prepend.py`
（`--verify` / `--regress` 只读，`--apply` 需 `--backup`，`--restore` 回滚）

## 0. 先决定粒度：全局一行 vs per-key

**几乎总是全局一行。** 因为 per-key 的 `router_settings.fallbacks`
**整表替换全局表，不是合并**（`common_request_processing.py::_resolve_fallback_models`，
`router.py` 里 `kwargs.pop("fallbacks", self.fallbacks)`）——
想给 676 把 key 各写一份，等于要替每把 key 重写全部 ~59 组的链，约 4 万行，
且任何一把漏了就静默失去所有其他兜底。

**全局一行为什么能覆盖到 cursor：** cursor key 是靠 per-key `aliases`
（`grok-4.6`/`gpt-5.2`/`gpt-5.4` → `sa-grok-4.6`）触达的。alias 改写发生在 router **之前**
（`litellm_pre_call_utils._update_model_if_key_alias_exists` 直接 mutate `data["model"]`），
所以 fallback 查表看到的是**改写后**的组名 ⇒ 一行 `sa-grok-4.6` 命中全部 665 把。

⚠️ **两条 alias 规则方向相反，别混**：

| | 查 fallback 用哪个名 |
|---|---|
| router 级 `model_group_alias` | **改写前** |
| per-key `aliases` | **改写后** |

拿不准就读运行 pod 里的源码，别靠记忆挑一个（这两条我自己就记反过一次）。

**全局一行的代价必须先量再报**：组名不等于「某个产品专属」。
`sa-grok-4.6` 14d 落点里 cursor 占 74%，但 `aliyun-carher-pro198` 有 51.0k、
`carher-75` 1.3k —— 它们**也会拿到这条新腿**。改之前用落点分布把这件事摆出来给用户拍板，
不要事后才说。

## 1. 写法：只准 `jsonb_set` 一个键

`STORE_MODEL_IN_DB=True` ⇒ 权威源是 Postgres `LiteLLM_Config` 里
`param_name='router_settings'` 那一行。**改 ConfigMap 是 no-op。**

```sql
update "LiteLLM_Config"
   set param_value = jsonb_set(param_value, '{fallbacks}', $$[...]$$::jsonb)
 where param_name='router_settings';
```

三条禁令：

- ⛔ **`POST /config/update`**：传部分 `router_settings` 会**静默抹掉
  `model_group_alias`**（24 → `{}`），下次重启才引爆。
- ⛔ **`kubectl apply`**：仓库 manifest 陈旧，会回退 image + 内嵌 CM
  （[[feedback_manifest_prod_drift_apply_overwrites]]）。
- ⛔ **整块重写 `param_value`**：13 个顶层键，少一个就是事故。只动 `{fallbacks}`。

**改哪条车道**：生产是**带路由标签的那个 Deployment**，不是名字叫 `litellm-proxy` 的那个。
判据 `-l carher.net/litellm-production-route=enabled`（2026-09-20 是
`litellm-proxy-gray` 4 副本吃 30402；`litellm-proxy` 1 副本闲置）。
脚本靠标签选，不写死名字——**永远别写死 Deployment 名**。

## 2. `router_settings` 每 30s 热加载 —— 旧「必须 rollout」已作废

2026-09-20 实测：`jsonb_set` 写完，4 副本 **~40s 内全部收敛**，
`restartCount=0`、`startTime` 还是 12 小时前 ⇒ **没有发生任何 rollout**。

机制在镜像里，不是猜的：

```
constants.py:1607   PROXY_CONFIG_RELOAD_INTERVAL_SECONDS = get_env_int(..., 30)
proxy_server.py:6232  → _add_router_settings_from_db_config(...)
proxy_server.py:6483    → llm_router.update_settings(**combined_router_settings)
```

boot 不是唯一读取点，而且它是 **overlay 合并**（`_update_dictionary`，空列表让位给 config），
这也是只改一个键安全的原因。⚠️ 这是**当前 gray 镜像**的性质，换镜像/降级后重测，
别当跨版本常量（[[feedback_router_settings_hot_reloads_every_30s_no_restart]]）。

推论：**不要因为"一测没中"就去滚 4 副本生产车道**。先等一个 30s 周期。
脚本 `--apply` 自己轮询 150s，只有真落后才打印 restart 命令。

## 3. 三层验收 —— 这是本篇的核心，三层都不能互相顶替

| 层 | 命题 | 判据 | 脚本 |
|---|---|---|---|
| 1 配置 | DB 那行写的是我要的 | 回读断言：顶层键逐字节、行数不变、**恰好一行有 delta** | `--apply` 内置 |
| 2 装载 | 每个在服务的 pod 内存里有它 | **逐 pod** 打 `podIP:4000/get/config/callbacks` | `--verify` |
| 3 **兜住** | 落到这条腿的请求**拿到了回答** | **按 status 分组的 SpendLogs** | `--regress` |

**第 3 层是最容易被跳过、也最重要的一层。**「兜底被走到了」≠「兜住了」。
2026-09-20 被它揪出来的事实：这次换掉的那条老腿
`openai/deepseek-v4-flash`，改动前 7 天 **983 failure / 959 success = 49%**，
最后 12 小时恶化到 **409/38 = 8.5%**，且**983 条失败全部 `completion_tokens=0`**。
也就是说：链上有这条腿、请求也确实落上去了、`--verify` 会全绿 ——
**但 grok 挂了用户依然没救**。只有按 status 分组才看得见。

```bash
# layer 1+2
python3 scripts/litellm-198-fallback-prepend.py --verify
# layer 3（--since 用写入时刻，UTC）
python3 scripts/litellm-198-fallback-prepend.py --regress --since '2026-09-20 01:59:00'
```

`--regress` 按 **cursor / 非 cursor 分组**输出——全局行不是 cursor 专属，
混在一起看会分不清失败属于哪个人群。2026-09-20 实测 6h：
cursor **380 success / 1 failure（99.7%）**，3 条失败全在非 cursor 那把 key 上。

## 4. 六个坏尺子（每一个我都实际踩过）

1. 🔴 **`metadata->>'model_group'` 必超时。** `LiteLLM_SpendLogs` 有**真列
   `model_group`**，走 JSONB 提取用不上索引 —— 我两次查询都 120s 超时进后台且永不返回。
   列语义见 [[feedback_spendlogs_model_group_is_request_name_model_is_landing]]：
   `model_group`=请求名、`model`=落点、`model_id`=部署。
2. 🔴 **拿组名去查 SpendLogs `model` 恒 0 行。** 组名是
   `openrouter-deepseek-v4.1-flash`，落点是 `openrouter/deepseek/deepseek-v4.1-flash`。
   0 行会被读成「没流量」，其实是**查错了列值**。
   而这个映射**两个元数据源都给不了**：`/model_group/info` 没有 `litellm_params`
   （只有 `providers`），`LiteLLM_ProxyModelTable.litellm_params->>'model'` 是**加密的**。
   只能从实际落点集合里反推（脚本 `landing_of()` 这么做，且**歧义时拒绝猜**）。
3. ⛔ **`mock_testing_fallbacks` 在本镜像返硬 400**，当不了"强制触发"的尺子。
   （旧记忆写的是"静默丢弃"，实测是 400，形状不同。）
4. ⛔ **想造"100% 失败的组"当载具基本走不通**：我挑的候选（`zk-116-gpt-5.4` 等
   24h 全挂）**压根没有 fallback 行**（读回 `None`）。
   正解是**别造载具，去查历史真实流量** —— 答案本来就在 SpendLogs 里。
5. ⛔ **`status='success'` 但 `completion_tokens=0` 是空壳**，等于没兜住，要单独挑出来。
6. ⛔ **一个窄窗口里的几条失败不许直接定性。** 那 3 条非 cursor 失败全是
   `APIConnectionError / Timeout on reading data from socket`，挤在 **6 秒内**、同一把 key。
   判「瞬时抖动」的判据是**再等一段时间证明它不复现**（实测 12min 和 6h 后都没有），
   不是"看起来像抖动"。

## 5. 目标组上线前要核的两件事

- **它是运行 router 上的真组**（`/model_group/info` 有这个 `model_group`），
  不是 config 里写了就算（脚本 `--apply` 前会挡一次）。
  ⚠️ 附带发现：原链上的 `deepseek-v4-flash-responses` 在
  `LiteLLM_ProxyModelTable` 里**查不到行**，但落点是 `openai/deepseek-v4-flash` ——
  说明它靠 alias/config 定义，不在 DB 模型表里。要收拾它得先知道这点。
- **它的 `max_input_tokens` 吃得下主路的上下文**。cursor 常打 20 万 token；
  新目标 `1,048,576` 撑得住。不核这条会换来一条"一上大上下文就 400"的腿。

**兜底目标不受 key 的 `models` allowlist 限制** —— 用 cursor key 直打目标组拿 403
是**预期行为**，不是故障。别为此去加白名单（我为此误判过一次）。
探针要用 master key，且⛔**不能拿 DB 里的 `token` 当 Bearer**（那是 sha256，
不存在的名字也 401 ⇒ 阴性对照失效）。

## 6. 回滚

```bash
python3 scripts/litellm-198-fallback-prepend.py \
    --restore /root/rs-bak/<pre-change>.json --apply
```

`--apply` 会先把改前的整个 `router_settings` 存到 `--backup`（必填）。
回滚后 ~30s 自动生效，用 `--verify` 确认，**不需要 restart**。

⚠️ 回滚前先问一句「回滚到的那条腿现在是什么成功率」。2026-09-20 这次，
回滚等于把兜底退回 8.5% 成功率那条腿上 —— 技术上可回滚，实际上是把用户推回坑里。

- [[project_198_sa_grok_fallback_to_openrouter_deepseek_2026_09_20]] — 首次使用全过程
- [[feedback_router_settings_hot_reloads_every_30s_no_restart]]
- [[feedback_litellm_alias_fallback_pre_rewrite_lookup]] — 两条 alias 规则方向
- [[feedback_config_update_silently_drops_unknown_router_settings]]
- [[feedback_litellm_group_name_lies_judge_by_api_base]]
- [[litellm-key-provider-swap]] / [[litellm-198-router-patch]] / [[topic_litellm_ops_index]]
