---
name: litellm-her-key-from-template
description: >-
  给一个 her 实例发 LiteLLM virtual key，做法是**当场现读参照实例**（杭州 gw =
  `bot-1000`，新加坡 = `carher-1000`）的活配置再原样复制，**绝不**用写死的模型清单。
  含：两套网关形状完全不同（杭州把权限放在 team 靠 `all-team-models` 继承 / 新加坡在 key 上
  逐个列举 + per-key aliases）、打错网关的自动守门、fail-closed（模板读不到就停，不退回默认值）、
  建完的三层验收（字段比对 → 真打推理 → 阴性对照）、删除的双重证明、漂移体检。
  Use when 用户说"给某个 her / 实例发一把 key"/"新开实例要 key"/"这把 key 配置对不对"/
  "杭州 gw 上怎么建 key"/"key 跟参照实例不一致了"。
  ⛔ 先确认打哪套网关——杭州 `gw.carher.net` 与新加坡 `litellm-proxy.carher.svc` 是两套，
  搞反了会建出一把在本集群根本不能用的 key。
---

# 按活模板发 her key

## 这个 skill 存在的理由

`backend/litellm_ops.py::generate_key()` 用写死的 `ALL_MODELS` 常量发 key。
写死的清单有个**零症状**的失效方式：参照实例哪天被改了（加模型、换 team、改映射），
之前建的 key 不会跟着变，之后建的 key 也不会——但两边都「建成功了」。
等有人发现不一致，已经隔了几十把 key。

所以本流程的硬规则只有一条：

> **每建一把 key，都当场重读一次参照实例，复制它当时的配置。读不到就停，不许退回默认值。**

用一份猜的配置建出来的 key，比建不出来更糟，因为它看着是成功的。

## 先分清你在哪套网关上（最大的坑）

| | **杭州 gw** | **新加坡舰队** | 198 |
|---|---|---|---|
| 地址 | `gw.carher.net` / 堡垒机资产 `vllm` 47.96.23.21，`/opt/llm-gateway` compose | `litellm-proxy.carher.svc:4000` ns=`carher`，K8s 2 副本 | 另见 `litellm-198-*` skills |
| 参照实例 | `bot-1000` | `carher-1000` | 按族 |
| `key_alias` | `bot-<uid>` | `carher-<uid>` | 按族前缀 |
| `models` | `["all-team-models"]` 一个词 | 逐个列举约 20 个名 | 按 allowlist |
| `aliases` | `{}`，**全库 358 把无一例外** | 约 15 条，缺了纯 alias 名一律 400 | 大量 |
| `team_id` | 有，`carher-bot` | 不用 | 不用 |
| 预算 | 不限（352 把全空） | 默认 100 | 按族 |

⛔ **禁止跨网关搬配置。**两套的 `models` 形状是相反的：杭州是「一个词，权限甩给 team」，
新加坡是「二十个名，权限在 key 上」。把新加坡那份清单写到杭州、或反过来，都会建出废 key。

脚本用 `--profile` 区分（默认 `hangzhou`），并且在模板读不到时**反查另一套的模板在不在**，
命中就直接报「你打错网关了」退出 3。这个守门是 2026-09-24 搞反过一次之后加的。

### 杭州的权限是三层继承的，key 自己不持有模型清单

| 层 | 值（2026-09-24 实测） | 含义 |
|---|---|---|
| key | `models=["all-team-models"]`、`aliases={}` | 不列举，甩给 team |
| team | `carher-bot`，`models=["all-proxy-models"]` | 再甩给整个 proxy，无预算无限速 |
| proxy | `/v1/models` 返回 **31** 个 | 真正的菜单 |

所以在杭州，「复制模型配置」= 复制 `models` + **`team_id`**。

⚠️ **`team_id` 不填不会坏，但要填。**实测一把无 `team_id` 的 `all-team-models` key
照样看得到全部 31 个模型、推理正常返回——这版 LiteLLM 里「没有 team」是落到**不限制**，
不是落到零。代价是它**逃掉 team 级预算/限速**（今天 team 上没设闸所以无感）。
现网 `bot-313`、`bot-313-migration-poc` 两把正处在这个状态。
见 [[feedback_team_id_absent_falls_through_to_unrestricted]]。

## 脚本

`scripts/litellm-her-key-from-template.py`（机上副本 `/opt/llm-gateway/` 同名文件，
sha256 要与仓库一致，那台**不是 git 仓**，改了没有版本记录）。

```
show    现读模板并打印，不写任何东西
plan    打印将要 POST 的完整 payload，不写
apply   建 key，然后读回来跟**本轮同一次**读到的模板逐项比对
verify  只读，拿一把已有 key 跟模板比对（体检漂移用）
```

退出码：`0` 一致 / `1` 建出来了但不合格 / `2` REFUSE 已存在 或 NOT FOUND / `3` ABORT fail-closed。

### 杭州跑法（在机上 source，master key 不外带）

```bash
scripts/jms ssh vllm
cd /opt/llm-gateway && set -a && . ./.env && set +a
./litellm-her-key-from-template.py show
./litellm-her-key-from-template.py plan  --uid 1234
./litellm-her-key-from-template.py apply --uid 1234
```

**判据**：`echo ${#LITELLM_MASTER_KEY}` 非 0；`show` 末行出现
`all-team-models expands to 31 live models`。

### 新加坡跑法（先打隧道）

```bash
kubectl port-forward -n carher svc/litellm-proxy 4000:4000
export LITELLM_MASTER_KEY=$(kubectl get secret -n carher litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
./scripts/litellm-her-key-from-template.py --profile singapore show
```

⛔ **master key 绝不下发给 bot 实例**，实例只拿本流程产出的 virtual key。
也不要把它复制到本地文件——在机上 `source`，用完随 shell 散掉。

## 验收是三层，缺一层都不算数

1. **字段层** —— `apply` 末尾 `VERIFY OK: matches <模板> exactly`，退出码 0。
   比对的是**本轮同一次**读到的模板，不是快照。
2. **可用层** —— 必须**真打一次推理**。`/key/list` 读得回来不证明能调。

   ```bash
   curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
     -H "Authorization: Bearer <新key>" -H "Content-Type: application/json" \
     -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"reply with exactly: pong"}],"max_tokens":512}'
   ```

   🔴 **`max_tokens` 给小了会造出假红**：菜单里多是推理模型，预算先被 reasoning token
   吃光，结果是 HTTP 200 但 `content` 为**空字符串**，看着像 key 没权限。
   **判据先看 `usage.completion_tokens_details.reasoning_tokens`**——它约等于
   `completion_tokens` 就是探针自己饿死的，加大重打。实测 `32` 假红、`512` 正常。
   见 [[feedback_reasoning_model_probe_needs_headroom_for_reasoning_tokens]]。
3. **阴性对照** —— 拿一个不存在的模型名打，应返回 400 `Invalid model name`。
   它要是也「成功」，说明你打的根本不是这台机。

新加坡额外一条：proxy 2 副本，另一个 pod 约 **2 分钟**后才认新 key，
刚建完探针失败别急着判故障，两个 pod 都打过再下结论
（[[feedback_litellm_key_write_lags_2min_on_the_other_proxy_pod]]）。

## 读 key 的正确姿势

```bash
curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  "http://127.0.0.1:4000/key/list?key_alias=bot-1234&return_full_object=true&size=5"
```

🔴 **不要用 `/key/info?key_alias=`**——**两套网关都对它返 404**（2026-09-24 实测），
看着像「这把 key 不存在」。能按 alias 读出完整行的只有 `/key/list` 加
`return_full_object=true`。
⚠️ `/key/list` 默认分页会静默只回一页，做「某 alias 不存在」这种**否定结论**
必须显式带 `key_alias` 过滤或直接走 DB（[[feedback_paged_api_silently_caps_at_ten_rows]]）。

## 异常与停止

| 现象 | 码 | 处置 |
|---|---|---|
| `ABORT: template bot-1000 not found ... Found carher-1000 instead` | 3 | **你打到另一套去了**。停，改 profile 或 base-url，不要跨网关 |
| `ABORT: ... has no team_id` / 模板返回空清单 | 3 | **模板自己漂了**。查参照实例是否被改或删，修好再建。不许用默认值顶上 |
| `ABORT: ... unreachable` | 3 | proxy 没起或端口不对。杭州容器每 1~2 小时自重启（设计行为），撞窗口稍等重试 |
| `REFUSE: <alias> already exists` | 2 | 停。确认是否重复开号；改配置用 `/key/update`，**不覆盖** |
| `VERIFY FAILED` 并列出差异 | 1 | key 已建出但不合格，`/key/delete` 删掉重来，**不要留着** |
| 推理 200 但 `content` 为空 | — | 先看 `usage`，八成是 `max_tokens` 被 reasoning 吃光，不是故障 |

## 回滚 = 删掉那把 key，但要两重证明

本流程唯一的写动作就是建一把 key。

```bash
curl -s -X POST http://127.0.0.1:4000/key/delete \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"key_aliases":["bot-1234"]}'
```

**判据（两条都要）**：① `verify --uid 1234` 返回 `NOT FOUND` 退出码 2，
**且同时跑一条阳性对照** `verify --uid 1000` 返回 OK 退出码 0；② DB 里该 alias 计数为 0。

⛔ 只看 delete 接口回执不算数。⛔ 只看 `verify` 返回 2 也不算数——
**缺 master-key 的报错恰好也是 2**，2026-09-24 就撞到过这个假绿，
所以必须配阳性对照 + 读那一行文字。

## 核对 key 总数时先排除管理台自建的

杭州库里有一类 `key_alias` 为空、挂 team `litellm-dashboard`、`max_budget=1` 的行，
约**每天一把**，来自有人登录 Web 管理台。做「总数对不对 / 试跑残留删干净没」这种核对
必须把它们排除，否则会误判成有人偷偷建号或自己没删净。

成功登录**不写日志**、`LiteLLM_AuditLog` 未开启——**日志里查不到不能当证据**。
排除自己的正确做法是受控实验：把本轮用过的读端点重打一遍看总数是否零增量。

## 已知缺口

- **`backend/litellm_ops.py` 不走这条路，也够不到杭州**：`generate_key()` 用写死的
  `ALL_MODELS`，`LITELLM_PROXY_URL` 默认指向新加坡的 `litellm-proxy.carher.svc`。
  凡是走 Admin 后端自动建的 key 既不受本 SOP 约束、也不落在杭州。**[待 owner 决定是否改造]**
- `/opt/llm-gateway` **不是 git 仓**，机上脚本只有一份。权威副本在本仓 `scripts/`，
  两边改动要自己对齐（判据 = `sha256sum` 逐字相同）。
- 杭州库里时间**全是 UTC**，机器本地 CST(+8)。直接当本地时间读会把「刚刚」读成「8 小时前没流量」。

## 配套

- 裸接口口径（脚本不可用时兜底）：`docs/hangzhou-gw-carher-key-api.md`
- 飞书 SOP（含判据、退出码、回滚、试跑记录）：
  <https://t83dfrspj4.feishu.cn/docx/BvtXdtgbSoBRVCxpqaEcwe3mngd>
