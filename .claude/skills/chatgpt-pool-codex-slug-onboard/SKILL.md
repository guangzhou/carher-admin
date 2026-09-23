---
name: chatgpt-pool-codex-slug-onboard
description: >-
  把一个新的 codex 后端 slug（gpt-6-sol / gpt-6-luna / 下一代 gpt-6-* 等）在 198
  （ns litellm-product）的 ChatGPT acct 池上端到端开出来：可用性直打摸底（带阳性+阴性
  对照）→ 建组（每号一行 DB deployment，不重启任何 pod）→ 兜底链 → key 白名单+alias
  → 三层验收。Use when 用户说"把 gpt-6-X 加上去"/"像 luna 一样把 Y 也开了"/"给 198
  的 cursor key 加上某个 chatgpt 池模型"/"新 codex 模型发布了接进来"。内含被实测推翻
  的旧结论（"禁止 DB 注册"）、可用性会翻面这一课、以及 acct-85 那种"是号不是模型"的分辨法。
---

# 在 198 acct 池上开一个新 codex slug

> 作用域：**198，ns `litellm-product`**。阿里云（ns `carher`）是另一套流水线，
> 要把这个名字再接到 her/carher bot 上走 [[add-litellm-model]]。
> 本篇只管「让 198 上的人（cursor/Codex 客户端）能调到它」。

## 🔴 先读：一条被推翻的旧结论

skill `chatgpt-pool-model-variant`（⚠️ **只存在于 `~/.claude/skills/`，不在本仓 git 里** ——
能加载，但不受本仓版本控制、review 不到、换机器就没有）的头条约束写着：

> 「必须用 CM 条目，禁止用 DB 注册 —— `db_model: True` + `mode: responses`
> 会触发 `No connected db.`」

**2026-09-23 实测推翻。**

| | |
|---|---|
| 假设 | `db_model:True` + `mode:responses` ⇒ `No connected db.` |
| 证伪条件 | 应当找不到任何一个「既 `db_model:True` 又 `mode:responses` 又在服务」的组 |
| 数据 | `/model/info`：`chatgpt-gpt-6-sol` 35 行、`gpt-6-luna` 36 行、`gpt-5.6-sol` 39 行、`gpt-6-astra` 78 行 —— **188 行全部 `db_model=True` + `mode=responses`**，且 sol/luna 当天有 `completion_tokens>0` 的成功 SpendLogs 行 |

真因是**漏了 `litellm_params.api_key`**（secret `chatgpt-pool-master-key`）：acct pod 收到一把
认不出的 key，想去 DB 查，而 acct pod 没接 DB ⇒ 它抛 `No connected db.`。
这句话**不是**「198 的库挂了」，也**不是**「DB 注册这条路不通」。
用户面看到的是打码后的 `API 异常 (req: xxxx)`，真话只在生产车道 proxy pod 日志里
`grep 'masked req=<id>'`。见 [[feedback_acct_pool_db_row_needs_api_key_else_no_connected_db]]。

⇒ **本篇走 DB 注册（`/model/new`），这是当前 71 行 sol+luna 实际在跑的路。**

## ⛔ 与 `scripts/chatgpt-pool-model/fanout-pool-model.sh` 的区别

那份脚本会对**每个 acct `rollout restart`**。**重启在服务的号可能永久打死它，且回退救不回**
（acct-237，[[feedback_restarting_a_serving_acct_can_kill_it_permanently]]）。

本篇的建组脚本**一个 pod 都不碰**。分工：

| 事 | 谁做 | 要不要重启 |
|---|---|---|
| acct pod 的 `/app/config.yaml` 里有这个 slug | CM 那一步（`chatgpt-pool-config`） | **要**，`config.yaml` 是 subPath 挂载，改 CM 不进容器 |
| 198 外层 router 有这个组 | 本篇建组脚本 | **不要** |

所以顺序是：**slug 已在 CM 里** → 本篇。CM 里没有就先去做 CM + 受控分波 rollout，
**别让建组脚本顺手帮你重启整池**。

🔴 **acct 池不止一份 config**：`acct-168` 挂 `chatgpt-pool-config-168`（ns 里还有 `-135`）。
patch 主 CM 是「覆盖大多数」不是「覆盖全池」，判据是 pod 内 `grep -c model_name /app/config.yaml`
对不对得上，见 [[feedback_acct_pool_cm_is_not_the_only_config_source]]。

---

## Step 0 — 可用性摸底：这一步不能省，也不能缓存

```bash
scp scripts/chatgpt-pool-codex-slug-survey.py cltx@10.68.13.198:~/slug-survey-<sid>.py
ssh cltx@10.68.13.198 'python3 ~/slug-survey-<sid>.py --slug gpt-6-sol --out ~/sol-targets.txt'
```

🔴 **目录声明不是事实。** `/backend-api/codex/models` 里 `visibility: list` +
`supported_in_api: true` 的 slug 直打照样 404。而且 `gpt-6-sol` **同一天内翻了三次面**：

| 时刻 | 404 的号数 |
|---|---|
| 10:24 | 33 / 36（只有 178/184/201 返 200） |
| 10:40 | 36 / 36 |
| 15:15 | **0 / 35** |

⇒ **可用性是时间函数，不是账号属性。** 「哪几个号有」这种名单**禁缓存到下一轮**，
每次开组前重扫。见 [[feedback_gpt6_sol_availability_flaps_not_entitlement]]。

脚本自带三条纪律，**换尺子时别丢掉**：

1. **阴性对照（编造名）必须全被拒**，否则本轮读数作废（脚本 exit 3）。
   2026-09-23 有一条 kubectl websocket 警告混进 stdout，被读成「编造名返 200」——
   所以解析**只认行首 `RESULT\t` 锚点**，禁用子串匹配。
2. **阳性对照**（一个此刻确定能用的 slug，默认 `gpt-5.6-sol`）大面积失败 ⇒
   坏的是池子或尺子，**不是目标 slug**，脚本拒绝出结论。
3. **`200 空流` 必须复打**。HTTP 200 + 空 SSE + `usage=None` 是瞬态节流，
   阳性对照同频率中招，复打即回显。不复打会把它算成不可用。

### 三桶分类：`acct-85` 那一课

脚本把失败分成两桶，**这个区分决定了 targets 是 35 还是 36**：

| 桶 | 判据 | 含义 |
|---|---|---|
| 模型缺口 | 目标失败、**阳性对照成功** | 真的是这个 slug 在这个号上没有 |
| 号本身的问题 | 目标失败、**阳性对照也失败** | 是号（free 档 / token 死），**不是模型** |

`acct-85` 在 `gpt-6-sol` 和 `gpt-5.6-sol` 上**都**返 400 ⇒ 第二桶，排除。
少了阳性对照那一列，它会被读成「sol 还没全放开」，整轮结论跟着跑偏。

两种 4xx 分开读，禁合并：
- `404 model_not_found` —— 付费 claim 的号走这条
- `400 The '<slug>' model is not supported when using Codex with a ChatGPT account.`
  —— free 档走这条。⚠️ 这句 400 **不再唯一指向「模型已退役」**（`gpt-6-sol` 是当天在架的
  新模型，照样吐这句），见 [[feedback_dead_model_name_400_is_retirement_not_a_limit]]。

**0 个可用就不要建组**（脚本 exit 4）。建了就是对用户稳定返 404。

---

## Step 1 — 建组：每个可用号一行

```bash
scp scripts/chatgpt-pool-add-codex-model.py cltx@10.68.13.198:~/pool-add-<sid>.py
ssh cltx@10.68.13.198 'python3 ~/pool-add-<sid>.py --slug gpt-6-sol \
    --targets ~/sol-targets.txt --in-cost 2e-06 --out-cost 1e-05'        # dry-run
ssh cltx@10.68.13.198 'python3 ~/pool-add-<sid>.py --slug gpt-6-sol \
    --targets ~/sol-targets.txt --in-cost 2e-06 --out-cost 1e-05 --apply'
```

组名默认 `chatgpt-<slug>`，行 id `chatgpt-acct-<N>-<slug>`。四个字段一个都不能漏：

| 字段 | 漏了会怎样 |
|---|---|
| `litellm_params.api_key` | acct pod 返 `No connected db.`，被打码成 `API 异常 (req:)` |
| `input/output_cost_per_token` | 按 0 计费，账单上这个组不存在 |
| `model_info.max_input_tokens` | ⛔ 更危险的是**填 10000000** = 把闸门关掉，不是能力声明。填上游 `max_context_window` |
| `model_info.mode: responses` | 走错端点 |

🔴 **`litellm_params.model` 是 `openai/<组名>`，不是 `openai/<slug>`。** acct pod 内部才把
组名翻成 `chatgpt/<slug>`。写成 slug 会在 acct pod 侧报 `Invalid model name`。

🔴 **窗口是两个数**：`context_window=272000`（默认档）和 `max_context_window=872000`（天花板）。
我们只有一个 `max_input_tokens` 字段 = 把两个数压成一个，填哪个都要先说清口径。
⛔ **922,000 是我们自己算出来的，官方只有 272k / 872k**，见 [[feedback_922000_was_never_the_real_ceiling]]。

⚠️ **写完立刻读 `/model/info` 可能只回到一部分**（35 行的写入中途读到 19 行）。
那是传播中的快照**不是漏写**。判行数换尺子直接查 `LiteLLM_ProxyModelTable`，
或等一会儿重读。别据此重跑（会建重复行）。

### 组在表里 ≠ 能用

必须带**唯一 nonce** 实打一次，并确认 SpendLogs 的 `model_id` 落在本组的行上。
⛔ 别复用上一轮的 nonce —— 2026-09-23 用 sed 从 luna 脚本改 sol 探针时没替换到 nonce 常量,
探针拿着旧 nonce 跑了一轮，尺子分不出本轮和上轮。

---

## Step 2 — 兜底链：对会翻面的 slug 是硬要求

```bash
scp scripts/litellm-198-fallback-add-row.py cltx@10.68.13.198:~/fb-add-row-<sid>.py
ssh cltx@10.68.13.198 'python3 ~/fb-add-row-<sid>.py --group chatgpt-gpt-6-sol \
    --chain chatgpt-gpt-5.6-sol,sa-grok-4.20,sa-grok-4.6 \
    --apply --backup ~/fb-sol-pre-$(date -u +%Y%m%dT%H%M%S).json'
```

**为什么必须配**：这类 slug 的可用性会在一天内翻面（见 Step 0 那张表）。翻回 404 时
没有兜底 = 用户直接吃 404。首选兜底挂**同族的稳定 slug**（`chatgpt-gpt-5.6-sol`）。

🔴 **建新组要用 `fallback-add-row.py`，不是 `fallback-prepend.py`。** 后者断言
`len(fallbacks)` 不变，**没法为一个全新组创建行**。

`router_settings` 每 ~30s 热加载，**不需要 rollout**。⛔ 禁 `POST /config/update`
（会静默抹掉 `model_group_alias`），只准 `jsonb_set(param_value,'{fallbacks}',...)`。
完整纪律见 [[litellm-198-fallback-chain]]。

⚠️ **顺手核一下同族别的组有没有漏**：给 sol 配兜底时发现 `chatgpt-gpt-6-luna`
**至今没有 fallback 行**。「这个组有没有兜底」这个否定结论，必须在那 90+ 行里按名字查过再说。

---

## Step 3 — key：白名单和 alias 必须同一条命令写

```bash
scp scripts/litellm-198-key-allowlist.py cltx@10.68.13.198:~/kav-<sid>.py
# 金丝雀
ssh cltx@10.68.13.198 'python3 ~/kav-<sid>.py --prefix cursor- --add-model gpt-6-sol \
   --alias gpt-6-sol=chatgpt-gpt-6-sol --limit 1 --apply --backup ~/sol-canary-<ts>.json'
# 全量
ssh cltx@10.68.13.198 'python3 ~/kav-<sid>.py --prefix cursor- --add-model gpt-6-sol \
   --alias gpt-6-sol=chatgpt-gpt-6-sol --apply --backup ~/sol-full-<ts>.json'
```

🔴 **用户打的是裸名 `gpt-6-sol`，而它不是真实组、也没有全局 alias ⇒ 只写白名单必然 400。**
白名单只发**准入**，不发路由。一个名字要真能调通必须满足其一：它是
`LiteLLM_ProxyModelTable` 里的真实组、或有全局 `model_group_alias`、或有 per-key `aliases`。
所以 `--add-model` 和 `--alias` 要在**同一条命令**里（合成一次 `/key/update`）。

🔴 **`--prefix` 必须显式写。** 默认范围是 `cursor-` + `claude-`，漏写会连带 600+ 把
Claude Code key，而 `planned` 数看着很正常。

范围纪律：加模型时 blocked key **跳过是对的**（它们连认证都过不了）；
**撤模型时必须 `--include-blocked`**，否则解封后权限静默复活。
⚠️ 跳过的 blocked key 要在报告里单列出来说（sol 这轮是 19 把）。

---

## Step 4 — 三层验收

| 层 | 命题 | 判据 |
|---|---|---|
| 1 配置 | DB 里写的是我要的 | 回读 + **对 `--backup` 快照逐 key 比对** |
| 2 装载 | 每个在服务的 pod 内存里有它 | 逐 pod 打 `podIP:4000/get/config/callbacks` |
| 3 **兜住** | 落到兜底腿的请求**拿到了回答** | 按 status 分组的 SpendLogs，`completion_tokens>0` |

### 判「写干净了」只认 backup 快照，不认平均值

sol 这轮平均模型数 41.48 vs 预估 42.25 对不上 —— **跨天的均值不可比，弃用这把尺子**。
换成拿 `--backup` 快照里每一把 key 的 `token` 去对当前库，分四桶：

```
clean_plus1=672  already_had=0  BAD=0  token_gone=0
```

`token_gone` 这一桶是必须的：同一个 `key_alias` **可能被删掉重建**（新 token、新 created_at），
只比 `cardinality(models)` 会把「被重建」读成「被覆盖」，处置完全不同。

psql 侧（写成 `.sql` 文件 `kubectl cp` 进 `litellm-db-0`，`-U litellm -d litellm`）：

```sql
-- usable = 有名 AND 有 alias；half_state 必须为 0
select count(*) filter (where '<裸名>' = ANY(models))                    has_name,
       count(*) filter (where aliases ? '<裸名>')                       has_alias,
       count(*) filter (where '<裸名>' = ANY(models) and aliases ? '<裸名>') usable,
       count(*) filter (where ('<裸名>' = ANY(models)) <> (aliases ? '<裸名>')) half_state
from "LiteLLM_VerificationToken" where key_alias like 'cursor-%';
-- 范围外误伤必须为 0
select count(*) from "LiteLLM_VerificationToken"
 where '<裸名>' = ANY(models) and key_alias not like 'cursor-%';
```

### 第 3 层只能等真实流量，不能合成

⛔ `mock_testing_fallbacks` 在本镜像返**硬 400**，当不了强制触发的载具。
sol 这轮第 3 层是真的绿：SpendLogs 里 `model_group=chatgpt-gpt-6-sol` 有 3 条落
`model_id=sa/grok-4.20` 的 `success` 行且 `ct>0` ⇒ 是「兜住」不只是「被走到」。
⚠️ **读不到数 ≠ 坏**。窗口里没落到兜底腿只说明主路活着，这时第 3 层是**空的**，
报告里必须写成空的，不许拿探针的绿顶上。

### 最后一道：用**他的 key 形状**打**他发的名字**

我挑名字的探针全绿，他照样可能红 —— 他发什么名只有他的 SpendLogs 知道。

```sql
-- 准入闸门的指纹：status='failure' 且 model_group 为空，model 是客户端原始名
select model, count(*), min("startTime"), max("startTime"),
       metadata->>'user_api_key_alias' who
from "LiteLLM_SpendLogs"
where "startTime" > now() - interval '12 hours' and status='failure'
  and model_group is null and model like '%<裸名>%'
group by 1,5 order by 3;
```

sol 这轮查出**写入前就有两个人在打裸名吃 403**（30 次 + 12 次，全部早于金丝雀）。
写完后**复刻这两把 key 的真实形状**（41 models / 22 aliases，从 `--backup` 快照取）
建临时 key 直打裸名 → 200 回显 nonce + 落点正确 + 阴性编造名仍 403。

⚠️ 这是「**我用他的形状证的**」，不是「他的流量证的」。两者在报告里必须分清 ——
他本人没回来试过之前，不许说「他已经好了」。

---

## 回滚（三段各自独立，写报告时必须都给出）

```bash
python3 ~/pool-add-<sid>.py --slug <slug> --targets ~/<slug>-targets.txt --delete  # 撤组行
python3 ~/fb-add-row-<sid>.py --restore ~/fb-<slug>-pre-<ts>.json --apply          # 撤兜底
python3 ~/kav-<sid>.py --restore ~/<slug>-full-<ts>.json --apply                   # 撤 key
```

`--delete` 只动 targets 文件里那些 id，不会误删别的组 —— **禁用宽选择器**。

## 并发防撞（198 是多会话并行机）

开跑前四步，成本约 2 分钟：`ListAgents` 看有没有 busy 会话 → `ls -lt ~/*.py` 看别人新投的脚本
→ `updated_at` 直方图（**看 `distinct_len` 不看 `n`**，流量刷 spend 也会顶到 60~70/min）
→ `SendMessage` 直接问对方动的是哪些字段和模型名。
收尾**反向核对方的字段还在不在** —— 只核自己那半边等于没证明「我没覆盖别人」。
详见 [[litellm-198-key-allowlist]]。

## 关联

- [[litellm-198-key-allowlist]] — key 白名单/alias 批量编辑的全部纪律
- [[litellm-198-fallback-chain]] — 兜底链改法与三层验收
- [[reference_codex_backend_model_catalog_2026_09_21]] — 上游权威目录（9 个 slug，
  ⛔ 接口按 `client_version` 发货，填 `0.60` 返空列表假红）
- [[project_198_gpt6_luna_fleet_2026_09_23]] — luna + sol 两轮的实际读数与回滚锚点
- [[add-litellm-model]] — 再把这个名字接到阿里云 / her bot 上（另一条流水线）
- ⚠️ `~/.claude/skills/chatgpt-pool-model-variant` — 只在用户全局目录、**不在本仓 git 里**
  （能加载，但换机器就没有、review 不到），且头条约束已被本篇推翻。
