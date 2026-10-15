---
name: litellm-198-fallback-chain
description: >-
  在 198（ns litellm-product）改**全局 `router_settings.fallbacks` 某一行**的兜底链：
  插链首 / 换目标 / 回滚，并按「配置→装载→兜住」三层分别验收。含为什么全局一行能覆盖
  665 把 cursor key（per-key alias 用改写后的名字）、为什么 per-key fallbacks 反而覆盖不了
  （整表替换不是合并）、`router_settings` 每 30s 热加载（旧「必须 rollout」已作废）、
  以及十四个会让你把「兜底存在」误读成「兜住了」、或者把「尺子坏了」误读成
  「没装载」的坏尺子（含容器里没 curl、后台 exit 0 不等于跑完、
  `/key/info?key_alias=` 返 200 全 null、全局表被读成 0 行）。
  **减法在 §8**：摘掉单把 key 单个组的整条兜底链（「把某人某个模型的兜底去掉、
  别影响其他 key」），含判据必须看「这把 key 读的那张表」而不是全局表。
  Use when the user mentions 兜底/fallback 链、"X 挂了兜到 Y"、改 fallbacks、
  兜底没生效、兜底能不能兜住、fallback 回归验证、sa-grok-4.6、
  要给某个模型组换/加备用腿，或**去掉/摘掉/取消某人某把 key 某个模型的兜底**。
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
| `scripts/litellm-198-key-fallback.py drop` | **减法**：摘掉**单把 key 单个组**的整条链 | 「把某人某个模型的兜底去掉」见 §8 |
| `scripts/litellm-198-fallback-set-rows.py` | **整行增/改**：spec 文件里每个组的链设成给定列表（没有就加、有就换），一次写入 | 一批组大多还没有行、或要整条替换时，见 §9 |

前三把都是 `--apply` 需 `--backup`、`--restore` 回滚、默认 dry-run（`set-rows` 同）。
⚠️ **`gpt-fallback-append.py` 的 pod selector 写死成 `app=litellm-proxy`**，见 §0.5 ——
它现在会验到不在服务的车道，用它之前先改 selector。

🔴 **本篇前七节全是「加/换」。「去掉某人某个模型的兜底」是减法，走 §8，别拿
`prepend`/`insert-before` 凑** —— 它们改全局表，会动到所有调这个组的人。

### 0.0 兜底目标先用真实客户端载荷打一发（10-14 踩过）

🔴 **`sa-grok-4.20-multi-agent` 不能当兜底**：x.ai 对带任何函数工具的请求回 400
`Client-side tools for multi-agent models require beta access`（我们的号没有 beta），
而 Codex / Cursor 的请求都带函数工具 ⇒ 那一跳 0.5s 必挂，实际全部落到下一跳。
那次用 `scripts/litellm-198-fallback-set-rows.py` 换上后又 `--restore` 回滚了。

🔴 **直打目标组看到 200 不算数**：目标组自己也有兜底链。10-14 我直打 multi-agent 看到能正常调工具，
其实是它自己的兜底 `openrouter-deepseek-v4.1-flash` 接住的。判据必须看 SpendLogs 的 `model_id`，
或者带 `disable_fallbacks: true` 去打。
搜索相关的兜底现状见 [[litellm-198-web-search]] §4。

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

### 0.1 「给所有 key 加兜底」先问清是哪个组（10-04 踩过）

兜底是**按源组**配的，不是按 key 配的 —— 一行就覆盖调这个组的所有 key，
**不需要、也不应该**「给每把 key 加」。用户说「给所有 cursor/claude/her key 加 X 兜底」时，
先确认**是哪个/哪几个组**，通常就是本轮刚上线的那一个。
10-04 我把这句话理解成「key 能调的所有组」，算出 170 条、又想到通配，
**用户要的只是新上的 `kiro-claude-sonnet-5-5` 一行**。

LiteLLM 支持通配行 `{"*": [...]}`（`fallback_event_handlers.py:369`，有专属链的组优先，
没有的才走它），但它会让出图/向量组也兜到聊天模型、而且影响全站 —— **用户没点名就别加**。
`fallback-add-row.py` 故意不认 `--group "*"`（它要求 group 是真组）。

🔴 **被用户拒掉的 `--apply` 可能已经落库。** 10-04 一次 `fallback-add-row.py --apply`
在用户点「拒绝」时**已经执行完**（198 上留了 04:14 的 backup 文件，表 106→107 行），
那行在线上待了 ~50 分钟。⇒ 任何写操作被拒/被打断后，**先回读表行数并找本轮 backup 文件**，
有就 `--restore` 那份快照，再继续。

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

🔴 **两份可以完全矛盾，而只有 DB 那份生效。** 2026-09-29 实测：DB 里 astra 是
五个目标，`config.yaml` 里同一个组只有一个目标（旧残留）。源码
`ProxyConfig._add_router_settings_from_db_config` 是**整键覆盖**，
只有 DB 值为**空列表**时才让位给 yaml ⇒ 「DB 里有非空 fallbacks」
= 「yaml 那份完全不参与运算」。
两个方向都危险：只读 yaml 会把五层链判成一层，然后去改根本不生效的那份
（多换一个 CM、多一份风险）⇒ [[feedback_db_router_settings_overlays_config_yaml]]。

**链子要几层就在同一行写几个目标**（一行五目标），
⛔ 别拆成"每组指向下一组"的链式单跳 —— 那在 `attempted_targets` 去重表下
会退化成只重试 1 次 ⇒ [[litellm-midstream-chain-descent]] §2。

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

## 4. 十四个坏尺子（每一个我都实际踩过）

> 前 8 个在本节，第 9 个在 §7.1，第 10/11 个在 §4.1，
> 第 12 个（脚本自己的装载尺子写死组名）在 §4.2，
> 第 13/14 个（`/key/info?key_alias=` 返 200 全 null、全局表被读成 0 行）在 §8.4。

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
   **10-04 升级后复测仍是 400**，原文已拿到：`MockTestingParamsDisabledError`，
   开关是 `general_settings.dangerously_allow_mock_testing_request_params: true`，
   **只能改 config.yaml，Admin UI/API 改不了**。客户端看到的是被 sanitize 过的
   `API 异常 (req: <id>)`，拿 req id 去 proxy 日志 grep `error_sanitize` 才看得到真因。
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

### 4.1 第 10、11 个坏尺子（2026-09-26，手搓 layer2 时踩的）

10. 🔴 **手搓 layer2 探针用 `curl` ⇒ 4 个 pod 全 `READ-FAIL`，和「这行没装载」完全同形。**
    `litellm` 容器里**没有 curl**（`command -v curl` 返空，镜像自带 python3），
    见 [[feedback_litellm_image_no_curl_smoke_use_python]]。优先用脚本的 `--verify`；
    非要手搓就用 `python3 -c` + `urllib`，**且必须带一个已知存在的组做阳性对照**：

    ```bash
    # 容器里没 curl，走 urllib；ctl 是阳性对照，它有值才说明尺子有分辨力
    for p in $(sudo kubectl -n litellm-product get pods \
                 -l carher.net/litellm-production-route=enabled -o name | sed 's|pod/||'); do
      sudo kubectl -n litellm-product exec "$p" -c litellm -- \
        env MK="$LITELLM_MASTER_KEY" python3 -c '
    import json,os,urllib.request
    q=urllib.request.Request("http://127.0.0.1:4000/get/config/callbacks",
                             headers={"Authorization":"Bearer "+os.environ["MK"]})
    fb=json.load(urllib.request.urlopen(q,timeout=20))["router_settings"]["fallbacks"]
    hit=[v for r in fb for k,v in r.items() if k=="<被测组>"]
    ctl=[v for r in fb for k,v in r.items() if k=="<已知存在的组>"]
    print("rows=%d hit=%s [ctl=%s]"%(len(fb), hit or "MISSING", ctl or "MISSING"))'
    done
    ```

    ⚠️ `/get/config/callbacks` 的 `router_settings` 是 **dict 不是 list** ——
    按 list 解析会 `AttributeError: 'str' object has no attribute 'get'`，
    而这个报错同样会被读成「读不到 ⇒ 没装载」。**先把尺子跑通，再读结论。**

11. 🔴 **后台批量 ssh 跑 N 条：`exit 0` + 最后一条只打了标题 ≠ 它跑过了。**
    09-26 那轮 7 条 `kiro-claude-*`，第 7 条（haiku）日志在 `=== [7/7]` 处截断，
    shell 仍 `exit 0`。唯一合法判据是**回读真实状态**：`fallbacks` 行数 96 → 103、
    且逐 pod 回读到那一行。这是
    [[feedback_kubectl_exec_heredoc_stdout_lost]] 的同一个形状，别靠 exit code。

### 4.2 第 12 个坏尺子：**`prepend.py` 的「装载」尺子曾写死默认组**（2026-09-29，已修）

12. 🔴 **`pod_live()` 读 `rows_of(fb).get(DEFAULT_GROUP)`，而调用方传的是 `--group`。**
    给 `kiro-claude-opus-5-5` 加腿那轮实测：四个副本全打 `NO`，并且把
    **`sa-grok-4.6` 的链子挂在 `kiro-claude-opus-5-5` 这个标签下**打出来 ——
    不是「判错」，是**错的值配着对的标签**，比返空更难发现。
    `--verify` 与 `--apply` 的轮询共用这个函数，所以它会在热重载**早已生效**的
    情况下报 4/4 落后，一路劝到生产滚动重启（§2 说过重启本来不需要）。

    这是 [[topic_ruler_failure_shapes]] 里「尺子里跟名字绑的硬编码表 ⇒
    写它之后新增的每一组都是永久盲区」那一条。已在 `79d79bf` 修掉
    （`pod_live(ip, mk, group)`，两个调用点都传 `a.group`）。

    **判「是脚本坏了」而不是「这行没装载」的办法 = 同一份未改状态上跑两把尺子**：

    ```bash
    # 写入之前跑，此时正确答案必须是 NO（行还没加）但值要是这个组自己的链
    python3 fb-prepend-旧.py  --group <新组> --target <腿> --verify   # 打出别的组的值 ⇒ 尺子坏
    python3 fb-prepend-新.py  --group <新组> --target <腿> --verify   # 打出本组的值、NO
    python3 fb-prepend-新.py  --verify                                # 阳性对照：默认组必 4/4 OK
    ```

    没有第三行那个阳性对照，「4/4 NO」既可能是尺子瞎了也可能是真没装载，两者同形。

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

- **跨协议腿（2026-09-29 加）：主组和腿不在同一个 provider 家族时，要拿
  「客户端真实用的那个端点」实打，不是 `/chat/completions`。**
  给 `kiro-claude-opus-5-5`（anthropic 家）挂 `sa-grok-4.20`（`openai` 家）时，
  8 条 `kiro-claude-*` 链**零先例**全都只兜 anthropic 家 —— 但这是**可测的，
  不是只能推测的**：车队客户端（Claude Code / cursor）发的是 Anthropic 格式
  `/v1/messages`，所以就打这个端点。实测 `sa-grok-4.20` 经 `/v1/messages`
  返 **200、0.72s、nonce 原样返回**，`/chat/completions` 做阳性对照也 200 ⇒
  `openai` 模式的组**可以**当 claude 组的兜底腿。
  ⚠️ 只打 `/chat/completions` 就通过，等于没测协议转换那一层。
- 🔴 **跨模型家族的腿，单轮探针是假绿（2026-10-12）**：要验「它能**接管别家写到一半的对话**」，
  不是「它能答一句新话」。DeepSeek 官方两组（`deepseek-v4.1-flash` / `-responses`）
  单轮 chat/responses/messages 三端点全 200，挂到 `sa-grok-4.7` 后真流量 **5/5 失败** ——
  Cursor 多轮历史里有 Grok 产生的推理/空 assistant 消息，官方 thinking 模式拒收。详见 §9。

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
- [[project_198_kiro_opus55_lane_and_fleet_2026_09_29]] — prepend 用在 `kiro-claude-*` 上：
  **第一条跨协议腿**（claude 组兜到 `openai` 家的 grok）+ 修掉 §4.2 那把坏尺子
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

## 8. 减法：摘掉**单把 key 单个组**的兜底（2026-10-10 加）

诉求形状：「把 X 的 cursor key 的 gpt-6-astra 兜底去掉，不要影响别人、只改这一把」。

```bash
S=~/litellm-198-key-fallback-<sid>.py   # 198 多会话并行，文件名带会话号
export LITELLM_MASTER_KEY=$(sudo kubectl -n litellm-product get secret litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

python3 $S drop --key cursor-<name>-<sfx> --group gpt-6-astra              # ① dry-run
python3 $S drop --key cursor-<name>-<sfx> --group gpt-6-astra \
    --apply --backup ~/<name>-astra-nofb-$(date -u +%Y%m%dT%H%M%SZ).json   # ②
python3 $S drop-check --key cursor-<name>-<sfx> --group gpt-6-astra        # ③ 验收
```

### 8.1 为什么「删一行」必须写成「照抄 22 行、唯独不含那一行」

per-key `router_settings.fallbacks` **整表替换全局表，不是合并**。在生产镜像里核过
（别靠记忆，这条我自己推过两次才站住）：

```
proxy/route_llm_request.py:511  elif "router_settings_override" in data:
proxy/route_llm_request.py:515      override_settings = data.pop(...)   → data["fallbacks"]
router.py:7421                  fallbacks = kwargs.get("fallbacks", self.fallbacks)
```

⇒ 没有「删除某一行」这种操作。要让一把 key 少一条链，只能**把它够得着的其余链
全部照抄进 per-key 表**，然后不写那一行。10-10 实测：cursor key 照抄 22 行。

**代价必须在报告里写出来：这张表是快照。** 以后全局表加腿/换腿，这把 key 不跟着变。
`drop-check` 会把快照与当前全局表**逐行 diff**，漂移会报出来——这是故意的，
让「不跟着变」可见，而不是静默。

### 8.2 🔴 判「要不要改」的不是全局表，是**这把 key 读的那张表**

这是本节最容易做错的一步，我 10-10 第一版就做错了：no-op 判据写成「全局表里有没有
这个组的行」。**活着的组永远有全局行，所以这个判据没有分辨力** —— 它会把「已经改完的
key」和「本来就没兜底的 key」一起放过去，然后原样重写一遍、报成功、什么也没变，
还白白给这把 key 加一层快照冻结。

正确判据按 per-key 表空不空分两种：

| per-key 表 | key 实际读哪张 | 判「有没有兜底」看哪 |
|---|---|---|
| **空** | 全局表 | 全局表里有没有这组的行 |
| **非空** | **只读 per-key 这张**（整表替换） | **per-key 表里有没有这组的行** |

第二行就是 10-10 的 claude key：31 行表里**没有** opus-5.5 的行 ⇒ 它**本来就没有兜底**，
全局那行 `kiro-claude-opus-5-5 → [sa-grok-4.20, anthropic.wangsu5.claude-opus-5]`
对它压根不生效 ⇒ **什么都不用做**。用户说「照样处理一遍」时，正确回应是把这个讲清楚，
不是照做一遍写个零变化。脚本现在会直接拒跑并打出判据。

### 8.3 🔴 一个组有几个名字，就得摘几行

`fallback_lookup_groups()`（`router_utils/fallback_event_handlers.py`）一个请求会按
**多个名字**查链：pre-routing 选的 tier、metadata 里的 `model_group`、再到请求名。
名字的第二来源有三处：

| 来源 | 10-10 的实例 |
|---|---|
| 全局 `model_group_alias` | `gpt-6-astra` → `chatgpt-gpt-6-astra`，**全局表里两行都在** |
| per-key `aliases` | 改写发生在 router **之前**，所以表按**改写后**的名字建 |
| 裸名 | 没有任何 alias 的 key 用原名到达 router |

⇒ 只摘用户说的那个名字 = **链在另一个拼法下活着，零症状**。
`lookup_names()` 把三处并起来，`drop-check` 逐名断言缺失。

⚠️ 两条 alias 规则方向相反（§0 已记）：router 级 `model_group_alias` 查**改写前**，
per-key `aliases` 查**改写后**。拿不准读运行 pod 的源码。

### 8.4 两个坏尺子（第 13、14 个，都是 10-10 实测，都已修）

13. 🔴 **`/key/info?key_alias=<alias>` 返 HTTP 200 + 每个字段都是 `null`。**
    `key_alias`/`token`/`models`/`aliases`/`router_settings` 全空。**没有任何异常**，
    因为它是 200。后果链条：`models or []` 把 null 吞成空列表 ⇒ 算出的表 **0 行** ⇒
    `lost_global_rows()`（本来就是防这个的闸门）**也从同一份空 info 算**，于是返空、
    放行 ⇒ 发出 `{"key_alias": null, "fallbacks": []}`。
    而**空表不等于没兜底**——空表让 key 落回全局表。所以症状是
    **「脚本说成功、兜底一行没改」**。

    三个端点实测：

    | 端点 | 回什么 |
    |---|---|
    | `/key/info?key_alias=` | 200，**全 null** ⛔ |
    | `/key/info?key=<token sha256>` | 有 models/aliases，但 `token` 仍 null |
    | `/key/list?...&return_full_object=true` | ✅ 完整行，**用这个** |

    还有四个脚本在用这个坏端点，待排查：`litellm-198-key-behavior-regress.py`、
    `litellm-her-key-from-template.py`、`litellm-198-cursor-newnames-probe.py`，
    以及 skill `litellm-198-key-allowlist` / `litellm-her-key-from-template` 的 SOP 文本。

14. 🔴 **`global_fallbacks()` 把 109 行的全局表读成 0 行，然后退回读 `config.yaml`。**
    `/get/config/callbacks` 的 `router_settings` 是 **dict**，老代码按
    「`{field_name, field_value}` 的 list」解析 —— 遍历 dict 得到的是**字符串 key**，
    `str.get` 抛 `AttributeError`，被 `except` 吞掉，表返空；接着 fall through 到
    `/config/yaml`，而那份在 `STORE_MODEL_IN_DB=True` 下**是权威错的**（§1）。
    这正是 §4.1 第 10 条记过的「dict 不是 list」的同一个形状，在另一个函数里又犯一次。

    现在三条闸门：非 dict 直接拒、空表直接拒（"空表几乎一定是读坏了，
    每个调用方都会把它当成『所有组都没兜底』"）、重复行拒。
    **删掉 yaml 退路** —— 唯一保证与线上不一致的源，不该当兜底。

**共同形状**：两个坏尺子都是**读失败伪装成「这里本来就是空的」**，而「空」在本节的
语义里恰好是**合法输入**（空表 = 吃全局表；没有行 = 没有兜底）。
⇒ **凡是「空」既可能是答案又可能是读坏了的地方，必须拒跑而不是继续算。**

### 8.5 验收：这一节的三层长什么样

| 层 | 判据 | 怎么量 |
|---|---|---|
| 1 配置 | 目标组在 key 表里**按每个名字**都查不到；其余行**逐字节**等于全局表 | `drop-check`（planned=written 且 0 problem） |
| 2 隔离 | 全局表行数/astra 行/`model_group_alias`/`context_window_fallbacks` 全不变；同族其他 key 不变 | 见下面的 SQL |
| 3 行为 | 主路挂时真的不再兜底 | **只能等真实故障**（§4 第 3 条：`mock_testing_fallbacks` 返 400，造不出来） |

⛔ **`updated_at` 不能当「我改过谁」的尺子。** spend 记账也会把它顶起来 ——
10-10 我查「近 3 小时改过的 key」得到 **92 行**，其中只有 1 行是我写的。
隔离性要按**内容**查，别按时间戳：

```sql
-- 分母必须是「能调这个组的 key」，不是「所有带 per-key 表的 key」
-- （第一版我写成后者：768 把里全是摸不到 astra 的 claude-code-*，分母没意义）
WITH a AS (
  SELECT key_alias,
         (router_settings IS NULL OR router_settings::text IN ('{}','null')) AS on_global,
         EXISTS (SELECT 1 FROM jsonb_array_elements(
                   coalesce(router_settings->'fallbacks','[]'::jsonb)) e
                  WHERE e ? 'gpt-6-astra' OR e ? 'chatgpt-gpt-6-astra') AS own_row
    FROM "LiteLLM_VerificationToken"
   WHERE 'gpt-6-astra' = ANY(models) OR aliases::text LIKE '%gpt-6-astra%')
SELECT count(*) AS astra_capable,
       count(*) FILTER (WHERE on_global) AS still_on_global_chain,
       count(*) FILTER (WHERE NOT on_global AND NOT own_row) AS no_astra_fb
  FROM a;
```

⚠️ `models` 是 **`text[]` 不是 jsonb** ⇒ 用 `= ANY(models)`，写 `@>` 会报
`operator does not exist: text[] <@ jsonb`。

10-10 收盘读数：astra_capable **780**，仍吃全局链 **559**，无 astra 兜底 **218**
—— 那 218 里绝大多数是 `carher-*`（早先 per-key 作业留下的，本来就没有 astra 行），
**不是本轮动的**。cursor 族带 per-key 表的只有 4 把，报告里要点名到具体 key，
别只报一个总数。

### 8.6 改完必须报的一句话：这些兜底次数会变成失败

摘兜底前先查这把 key 该组 14 天的**兜底落点**（`model` ≠ 主落点的那些行）：

```sql
SELECT model_group, model, status, count(*) AS n,
       sum(case when completion_tokens>0 then 1 else 0 end) AS ct_pos
  FROM "LiteLLM_SpendLogs"
 WHERE api_key = '<token sha256>' AND "startTime" > now() - interval '14 days'
 GROUP BY 1,2,3 ORDER BY n DESC;
```

🔴 **按有索引的 `api_key`（= token sha256）查，别按 `metadata->>'user_api_key_alias'`**
（JSONB 提取用不上索引，必超时 ⇒ [[feedback_spendlogs_api_key_not_jsonb_alias]]）。

10-10 两把 key 的读数：边朝明 astra 主路 955 次成功、**兜底 11 次**（sol 5 / luna 5 /
grok-4.20 1，全部出字）；傅天麒主路 189 次成功、**兜底 4 次**。
⇒ 改完这 11 次和 4 次会变成**直接报错**。这是用户要的效果，但**数必须摆出来**，
不能等他自己发现。

- [[project_198_astra_fallback_dropped_per_key_2026_10_10]] — 本节首次使用全过程
- [[feedback_key_info_by_alias_returns_all_null]] / [[feedback_empty_read_is_not_an_empty_table]]
- [[litellm-key-provider-swap]] — 改单把 key 的 alias/映射（不是兜底）走那篇

## 9. 整行增/改：`fallback-set-rows.py`（2026-10-12 加）

诉求形状：「grok 全系（含 composer，除出图）兜到 X；出图 grok ↔ gpt 互兜」。
12 个组里 10 个**还没有行**，`prepend`/`insert-before` 都要求行已存在 ⇒ 用这把。

```bash
S=~/fb-setrows-<sid>.py                       # 198 多会话并行，文件名带会话号
# spec：{"<组>": ["<腿>", ...], ...}，每个组的行整条设成这个列表
sudo python3 $S --spec spec.json                                   # ① dry-run，逐行 ADD/REPLACE
sudo python3 $S --spec spec.json --apply --backup ~/rs-bak/router_settings.pre-<what>-<utc>.json
sudo python3 $S --spec spec.json --verify                          # 层 1+2：DB + 逐 pod
sudo python3 $S --restore ~/rs-bak/<file>.json --apply             # 整表回滚
```

脚本自带的闸门：组和腿**都必须是运行 router 上的活组**；回读断言 13 个顶层键逐字节、
**未点名的行逐字节不变**、行数 = 旧 + 新增数、点名行精确；全局表读成空/重复行直接拒跑
（§8.4 第 14 条）。容器无 curl，逐 pod 读用 python3 urllib。单测
`backend/tests/test_litellm_198_fallback_set_rows.py`。

### 9.1 现行配置（10-12 终态，120 行）

| 组 | 兜底 |
|---|---|
| `sa-grok-4.6` / `sa-grok-4.7` | `openrouter-deepseek-v4.1-flash` → `deepseek-v4-flash-responses` |
| `sa-grok-4.20` / `-4.20-reasoning` / `-4.20-multi-agent` / `-4.3` / `-4.5` / `-build-0.1` / `sa-composer-2.5-fast` | `openrouter-deepseek-v4.1-flash` |
| `gpt-image-2` | `sa-grok-imagine-image-2.0` |
| `sa-grok-imagine-image-2.0` / `sa-grok-imagine` / `sa-grok-imagine-image-quality` | `gpt-image-2` |

| `ag-gemini-3.8-flash` | `web-gemini-3.8-flash`（10-14 加，全局，1468 把 key 共用） |

⚠️ `web-gemini-3.8-flash` 是 Gemini 网页版（`litellm-dev/gemini-web` 单副本、小号池、约 5h/2400 credits、
240k 上下文、三客户端工具实测 5/9）。用户知情选了全局一行：antigravity 整体故障时全员会压到它、
几分钟可能耗尽 —— 届时现象是兜底腿也报限额，不是这行没生效。备份
`~/rs-bak/router_settings.pre-gemini38-ag-to-web-20261014T*.json`。

出图互兜成环是安全的：`fallback_event_handlers.py` 的 `attempted_targets` 跨层共享去重，
同一目标一次请求只试一次，外加 `max_fallbacks` 深度上限（读运行 pod 源码核过）。

### 9.2 🔴 为什么不是 DeepSeek 官方：写入 3 分钟就回滚的那一版

第一版按「Responses 请求要原生组」的推理写了
`[deepseek-v4.1-flash-responses, deepseek-v4.1-flash]`。上线前单轮 nonce 三端点全 200。
写入后 `sa-grok-4.7` 真流量兜底 **5/5 失败**，两段原文：

- `-responses`：``The `reasoning_text` in the thinking mode must be passed back``
- chat 组：`Invalid assistant message: content or tool_calls must be set`

Cursor 的多轮历史里带着 **Grok 产生的推理与空 assistant 消息**，官方 thinking 模式接管
别家写到一半的对话会拒；入口的 `_RESPONSES_GROUP_REWRITE` 只认请求原名，兜底跳转时不触发。
`openrouter-deepseek-v4.1-flash` 不卡这一点：改前 24h 230 次 success，回滚后 10min 6/6。

⇒ **跨家族兜底腿的验收**：单轮探针只证「这个组活着」。要么用带对方推理/工具历史的多轮
payload 打，要么写入后**立刻盯真流量 SpendLogs 的 status**（`api_key` 列，按 `model` 落点分组），
有失败就回滚 —— 这次就是靠后者 3 分钟内抓到的。

### 9.3 ⚠️ `sa-grok-4.20-multi-agent` 带 tools 必兜底（上游属性，不是故障）

不带 tools 200；带 tools（Cursor 每发都带）上游回 `Upstream rejected the request`，
请求落到 `openrouter-deepseek-v4.1-flash`（`x-litellm-attempted-fallbacks: 1`）。
⇒ 在 Cursor 里选这个名，**实际作答的是 deepseek**。
官方文档 Limitations 明写三条：**不支持 client-side/custom tools（function calling）**、
**不支持 Chat Completions**（只 Responses）、**不支持 `max_tokens`**；定位是 4/16 子智能体的
联网深度调研（beta），所有子智能体 token 都计费 —— 实测同题 effort=low 约 20×、high 约 250× token。
⇒ 对 Claude Code / Codex / Cursor **没有价值**，10-12 已从 `cursor-liuguoxian-std` 摘掉（兜底行保留）。

⚠️ `sa-grok-4.20` 实落上游是 **`grok-4.20-non-reasoning`**（3 天 1349/1349），不会思考。
要推理版用 `sa-grok-4.20-reasoning`（实测 reasoning_tokens>0，三客户端线型 9/9 工具调用）；
10-12 起 std 的 `grok-4.20` 已映射到它，并补了兜底行。

备份：`198:~/rs-bak/router_settings.pre-grok-ds41-image-20261012T093908Z.json`（本轮之前）、
`...pre-rollback-grok46-47-20261012T094308Z.json`、`...pre-grok6-openrouter-20261012T101722Z.json`、`...pre-grok420-reasoning-20261012T133647Z.json`。

- [[feedback_deepseek_official_cannot_take_over_midstream_grok_fallback]]
