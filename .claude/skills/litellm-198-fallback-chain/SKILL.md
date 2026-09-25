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

脚本（三把，语义不同，别拿错）：

| 脚本 | 语义 | 何时用 |
|---|---|---|
| `scripts/litellm-198-fallback-prepend.py` | 把 target 插到**某一个** group 的链首 | 换某条链的首选腿 |
| `scripts/litellm-198-fallback-insert-before.py` | 在**点名的一组** group 里，把 target 插到 **anchor 之前** | 升级某条既有腿（新腿在前、老腿保留为下一跳） |
| `scripts/litellm-198-gpt-fallback-append.py` | 全表扫「以 anchor **结尾**」的链，往后追加 | 给一大批链加统一的末端兜底 |

三把都是 `--apply` 需 `--backup`、`--restore` 回滚、默认 dry-run。
⚠️ **`gpt-fallback-append.py` 的 pod selector 写死成 `app=litellm-proxy`**，见 §0.5 ——
它现在会验到不在服务的车道，用它之前先改 selector。

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

**cursor 装机包 gpt 段要改哪几行 —— 不是 5 行，是 6 行。**
菜单 gpt 段就 5 个名（`cursor_team_setup.py::DEFAULT_MODELS` 第 ② 段），
但它们**不是同一种触达方式**，按 per-key `aliases` 分成两类：

| 菜单名 | 有 per-key alias 吗 | fallback 查表用的名 |
|---|---|---|
| `gpt-5.5` / `gpt-5.6-sol` / `gpt-5.6-luna` / `gpt-5.6-terra` | ❌ 665 把里只有 1 把有 | **同名行**（`gpt-5.5` …） |
| `gpt-6-astra` | ✅ **644 把**改写成 `chatgpt-gpt-6-astra` | **`chatgpt-gpt-6-astra`** |

所以给 gpt 段改兜底，`gpt-6-astra` 和 `chatgpt-gpt-6-astra` **两行都得改** ——
只改前者，那 644 把 key 一条都吃不到（而且是零症状：链在、日志正常、就是没生效）。
查这张表的命令（别靠猜）：

```sql
select count(*), a.key, a.value
  from "LiteLLM_VerificationToken" t, lateral jsonb_each_text(t.aliases) a
 where t.key_alias like 'cursor-%' group by a.key, a.value order by a.key;
```

**全局一行的代价必须先量再报**：组名不等于「某个产品专属」。
`sa-grok-4.6` 14d 落点里 cursor 占 74%，但 `aliyun-carher-pro198` 有 51.0k、
`carher-75` 1.3k —— 它们**也会拿到这条新腿**。改之前用落点分布把这件事摆出来给用户拍板，
不要事后才说。

## 0.5 改哪条车道 —— 标签在 **Pod** 上，不在 Deployment 上

生产是**带路由标签的那个 Deployment**，不是名字叫 `litellm-proxy` 的那个。
权威判据是 **Service `litellm-proxy-nodeport`（nodePort 30402）的 selector**：

```bash
kubectl -n litellm-product get svc litellm-proxy-nodeport -o jsonpath='{.spec.selector}'
# -> {"carher.net/litellm-production-route":"enabled"}
kubectl -n litellm-product get pods -l carher.net/litellm-production-route=enabled
```

🔴 **那个标签在 Pod 上（podTemplate），不在 Deployment 对象上。**
`kubectl get deploy -l carher.net/litellm-production-route=enabled` 返回
**"No resources found"** —— 这是**假红**，不代表标签不存在、更不代表没有生产车道。
我 2026-09-21 就是这么读了一次，差点据此断言"路由标签已经没人带了"。
选 pod 一律 `get pods -l`，别 `get deploy -l`。

2026-09-24 现状：生产车道是 `litellm-proxy`（4 副本吃 30402）；`litellm-proxy-gray`
已排空到 0 副本、留作回滚；`litellm-proxy-guarded-old` 0 副本，老版本已退役。
（09-24 前带路由标签的是 `litellm-proxy-gray`，车道搬过家了。）**永远别写死 Deployment 名**——
车道搬过家，还会再搬。`litellm-198-fallback-insert-before.py` 按标签选，
并有一条测试钉住这件事（`test_production_lane_is_selected_by_route_label_...`，
实测把 selector 改回 `app=litellm-proxy` 它会真的红）。

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

**改哪条车道**：见 §0.5（判据 = Service `litellm-proxy-nodeport` 的 selector，
且必须 `get pods -l`，不是 `get deploy -l`）。脚本靠标签选，不写死名字。

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

### 3.1 「能不能兜住」的回归 = 两条腿，缺一条都不算

| 腿 | 量什么 | 量不到什么 |
|---|---|---|
| **A 真流量窗口** | 链**真的走到**新腿 + 新腿**真出字**（按 status + ct>0 分组） | 主路没挂的那些组（走不到就没读数） |
| **B cursor 线型探针** | 新腿**接得住 cursor 形状**（`stream:true` + `tools` + 唯一 nonce） | fallback 本身（探针打 gpt-* 走的是**主路**） |

A 用 SQL（写入时刻起，`model like '%<新腿落点>%'`，按 `model_group/status/who` 分组；
同时查**老腿在同窗口是否归零** —— 归零才证明插入位置生效）。
B 用 `scripts/litellm-198-cursor-newnames-probe.py --clone-file <真key形状> --names <新腿>,<5个gpt名>`，
脚本会把 `--names` 里不在 models 的自动加进临时 key（兜底目标不在 cursor 的 allowlist 里是预期的，
这里加进去是为了量"它吃不吃这种 payload"，不是在量授权）。

⛔ **B 绿 ≠ 兜住**：探针打 `gpt-5.6-sol` 200 出字只说明主路活着。
⛔ **A 里没读数 ≠ 坏**：2026-09-21 窗口 23.5min 里 `gpt-5.6-sol/luna/terra` 三个名
**一条都没落到新腿** —— 不是坏了，是它们链里新腿前面还有两跳互兜，中间跳接住了。
这三个名当时只有 1、2 层绿，第 3 层是空的，**报告里必须写成空的**，不许拿 B 的绿顶上。

2026-09-21 实测（`sa-grok-4.20` 插在 `sa-grok-4.6` 之前，6 行）：
写入 → 4 pod 收敛 <60s；23.5min 窗口新腿 **25 success / 1 failure，success 全 ct>0**；
老腿 `grok-4.6` 同窗口 **0 条**；探针 6/6 绿、阴性对照 403。
触发兜底的是主路真实故障（`MidStreamFallbackError: transient capacity` 一分钟十几条），
不是我造的载具。

### 3.2 那 1 条失败怎么定性：先查它是不是**请求属性**

`Invalid 'input[271].id': string too long. Expected max 64, got 84`（`/v1/responses` 端点）。
判据不是"看起来像"，是**同形状错误在改动前有没有出现在别的腿上**：

```sql
select model, count(*), min("startTime"), max("startTime") from "LiteLLM_SpendLogs"
 where "startTime" > now() - interval '7 days' and status='failure'
   and metadata->>'error_information' like '%string too long%' group by 1;
-- openai/deepseek-v4-pro | 11 | 09-20 14:58 | 09-21 05:45   ← 全在改动之前
```

⇒ 既存缺口：chatgpt 系的腿吃 84 长度的 id，**非 chatgpt 系的兜底腿**（deepseek-v4-pro、
grok-4.20）不吃。不归本次改动，但新腿排前了会**更早**撞上它。
⚠️ 400 是不可重试错误，链在这跳**停住**（该 request_id 只有一行，没继续兜到 4.6）——
那一发用户是真失败。要不要治是另一件事，别混进"兜住没有"的结论里。

## 4. 八个坏尺子（每一个我都实际踩过）

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
   **2026-09-21 复测仍是 400** —— 别每次改完都重新去试一遍，它没变。
   推论：**「兜住没有」这件事只能等真实流量，不能合成。**
4. ⛔ **想造"100% 失败的组"当载具基本走不通**：我挑的候选（`zk-116-gpt-5.4` 等
   24h 全挂）**压根没有 fallback 行**（读回 `None`）。
   正解是**别造载具，去查历史真实流量** —— 答案本来就在 SpendLogs 里。
5. ⛔ **`status='success'` 但 `completion_tokens=0` 是空壳**，等于没兜住，要单独挑出来。
6. ⛔ **一个窄窗口里的几条失败不许直接定性。** 那 3 条非 cursor 失败全是
   `APIConnectionError / Timeout on reading data from socket`，挤在 **6 秒内**、同一把 key。
   判「瞬时抖动」的判据是**再等一段时间证明它不复现**（实测 12min 和 6h 后都没有），
   不是"看起来像抖动"。
7. 🔴 **`get deploy -l <路由标签>` 返 "No resources found" 是假红** ——
   标签在 Pod 上。见 §0.5。我据它差点断言"生产车道没人带路由标签了"。
8. 🔴 **"某个组没有 fallback"这个否定结论，必须**在 91 行里按名字查过**再说。**
   2026-09-21 用户报"cursor 的 gpt 系列没有 fallback"，实际 5 个名**全都有**，
   而且近 3 天已真兜住 **1141 次**（全 ct>0）。真实诉求是"把 grok 那一跳升级"。
   **先证伪用户给的前提，再动手** —— 否则会去"补"一条本来就在的链，
   顺手把已经在兜的腿改坏。

## 5. 目标组上线前要核的两件事

- **它是运行 router 上的真组**（`/model_group/info` 有这个 `model_group`），
  不是 config 里写了就算（脚本 `--apply` 前会挡一次）。
  ⚠️ 附带发现：原链上的 `deepseek-v4-flash-responses` 在
  `LiteLLM_ProxyModelTable` 里**查不到行**，但落点是 `openai/deepseek-v4-flash` ——
  说明它靠 alias/config 定义，不在 DB 模型表里。要收拾它得先知道这点。
- **它的 `max_input_tokens` 吃得下主路的上下文**。cursor 常打 20 万 token；
  2026-09-20 的新目标 `1,048,576` 撑得住；2026-09-21 的 `sa-grok-4.20` 是
  **1,000,000**（老腿 `sa-grok-4.6` 只有 500,000）。不核这条会换来一条
  "一上大上下文就 400"的腿。读法：`/model_group/info` 里该组的 `max_input_tokens`
  （⚠️ 这个数是**组内最大值**，不是闸门本身，见
  [[feedback_model_group_info_cap_is_group_max_not_gate]]）。
- **拿 master key 带唯一 nonce 实打一次，确认它真出字。** 组在表里 ≠ 能用。
  2026-09-21 `sa-grok-4.20` 只有 7 天 24 条历史流量 —— **"它是真组"和"它扛得住
  承接量"是两个命题**，前者能证，后者上线前证不了，要在报告里说出来。

**兜底目标不受 key 的 `models` allowlist 限制** —— 用 cursor key 直打目标组拿 403
是**预期行为**，不是故障。别为此去加白名单（我为此误判过一次）。
探针要用 master key，且⛔**不能拿 DB 里的 `token` 当 Bearer**（那是 sha256，
不存在的名字也 401 ⇒ 阴性对照失效）。

## 6. 回滚

```bash
# 用哪把脚本改的，就用哪把回滚（三把的 --restore 语义一致：整表写回 fallbacks）
python3 scripts/litellm-198-fallback-insert-before.py \
    --restore /tmp/rs-pre-<change>.json --apply
python3 scripts/litellm-198-fallback-prepend.py \
    --restore /root/rs-bak/<pre-change>.json --apply
```

`--apply` 会先把改前的整个 `router_settings` 存到 `--backup`（必填）。
回滚后 ~30s 自动生效，用 `--verify` 确认，**不需要 restart**。

⚠️ 回滚前先问一句「回滚到的那条腿现在是什么成功率」。2026-09-20 这次，
回滚等于把兜底退回 8.5% 成功率那条腿上 —— 技术上可回滚，实际上是把用户推回坑里。

- [[project_198_sa_grok_fallback_to_openrouter_deepseek_2026_09_20]] — 首次使用全过程
- [[project_198_cursor_gpt_fallback_grok420_2026_09_21]] — insert-before 首次使用 + 两腿回归
- [[feedback_router_settings_hot_reloads_every_30s_no_restart]]
- [[feedback_litellm_alias_fallback_pre_rewrite_lookup]] — 两条 alias 规则方向
- [[feedback_config_update_silently_drops_unknown_router_settings]]
- [[feedback_litellm_group_name_lies_judge_by_api_base]]
- [[litellm-key-provider-swap]] / [[litellm-198-router-patch]] / [[topic_litellm_ops_index]]

## 7. 点名模式（2026-09-25 加）—— 「全部 X 系列末端统一成 Y」用这个，不是 anchor

`litellm-198-gpt-fallback-append.py --groups-file <清单>`：把 target 做成点名
那些组的**最后一跳**，`--anchor` 整个忽略。

**为什么 anchor 模式接不住这个诉求**：53 条在范围内的 gpt 链末端有 **7 种**
不同的腿，按 anchor 扫会连带改到共享同一末端的**非 gpt** 链（光
`deepseek-v4-pro-responses` 结尾的就有 36 条，不全是 gpt）。

两条语义是刻意的：

- target 已在链**中段** ⇒ **搬到末尾**，不是再追加一份（`gpt-5.6-sol` 正是
  这形状，追加会让同一条腿在链里出现两次）。
- 点名的组在表里**没有** fallback 行 ⇒ 报出来并**跳过，不新建**。凭空造一条
  链是一个新的**路由决定**，不是一次编辑。

**范围别只按名字子串**：`codex-auto-review` 是 gpt 系，名字里没有 `gpt`。
（`chatgpt-codex-auto-review` 反而被 `gpt` 子串捞到了。）先把全表 95 条
dump 出来人眼扫一遍非 gpt 的那 42 条，再定清单。

### 7.1 第 9 个坏尺子：拿组名当 `model` 查出来的「全挂」

判 `openrouter-deepseek-v4.1-flash` 健康度，第一版 SQL 同时 `OR` 了组名和落点，
得到 **0 成功 / 92 失败**，差点据此断言这条腿不能用。

**识破它的信号**：一屏里十几条**不同的**腿全都恰好 90~92 bad / 0 ok，
且 `last_seen` **同为 `09-23 01:39` 这一分钟** ⇒ 机器指纹，是某个扫描器把全表
打了一遍留下的行，不是真实流量（[[feedback_fixed_cadence_is_a_machine_fingerprint]]）。
只按**落点** `openrouter/deepseek/deepseek-v4.1-flash` 查，真相是
**9292 success（ct>0）/ 18 failure**。

⇒ §4.2 那条「拿组名查 `model` 恒 0 行」还有个更坏的变体：**不是 0 行，是一堆
探针留下的失败行**，看起来像证据。

### 7.2 append 还是 replace：先分清 ct=0 落在 success 还是 failure 上

§3 记的「983 条 ct=0」曾让我以为现有末端 `openai/deepseek-v4-*` 会返
**success 空壳**、追加在它后面永远走不到。重查按 status 分组：

| 落点 | success | 其中 ct=0 | failure |
|---|---|---|---|
| `openai/deepseek-v4-flash` | 2118 | **0** | 655（全 ct=0） |
| `openai/deepseek-v4-pro` | 85 | **0** | 14 |

ct=0 全在 **failure 行**上（那是正常的），success 行**全部 ct>0**。
⇒ 失败就是真失败、会继续往下跳 ⇒ **append 可达**，不需要 replace
（replace 要摘掉一条 7 天出字 2118 次的腿）。

**判据**：不是「有多少 ct=0」，是「ct=0 落在哪个 status 上」。
混在一起算会把一条健康的腿误判成空壳。
