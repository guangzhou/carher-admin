---
name: sub2api-concurrency-gate
description: >-
  198 sub2api 的**两道并发闸**：`users.concurrency`（user 级，全平台共享）与
  `accounts.concurrency`（account 级，每个上游号）。症状是上游回
  `Concurrency limit exceeded for user, please retry later`、LiteLLM 侧变成
  APIConnectionError→重试打光→500 打到用户脸上（**不是 429，所以没有 cooldown、
  没有退避**）。⚠️ 前端把两道闸放在两个页面，**改 account 那道对本症状零效果** ——
  2026-09-09 实测账号 10→199 后失败率一点没动，真闸是 user 的 5。
  Use when 用户说"grok/kimi 并发不够"/"grok 报并发超限"/"把并发改到 N"/
  "sa-grok-* 大面积 500 但账号是好的"/"前端改了并发没生效"。
  工具：`scripts/sub2api-concurrency.sh`（show / set-user / set-account / judge）。
---

# sub2api 并发闸

## 0. 一句话拓扑：为什么 user 那道闸先满

```
Cursor/客户端 → litellm-product :4000  (sa-grok-4.6 / sa-kimi-* / antigravity)
              → sub2api  用 同一把 key `grok-litellm` (id=5, user_id=1)
                 ├─ 闸①  users.concurrency          ← 全平台共享，先满的是这个
                 └─ 闸②  accounts.concurrency       ← 每个上游号各自的
              → api.x.ai / kimi / …
```

**198 上所有经 LiteLLM 打到 sub2api 的流量都是同一个 sub2api user（id=1）。**
grok、kimi、antigravity 共用它那一份槽。所以：

| 你想解决的 | 该动哪道闸 |
|---|---|
| "同时用的人多了，排队超时" | **闸① user**（`set-user`） |
| "某个上游号被压太狠 / 想给某号限流" | 闸② account（`set-account`） |
| "这条路断了 / 报 403 余额" | 都不是并发问题，回 skill `sub2api-grok-ops` |

## 1. ⚠️ 09-09 踩过的坑：改错闸 + 前端把它藏在别的页面

用户在前端把 4 个 grok 账号的并发 10→199（**落库了、没写错**），
但失败率**一点没动**：改后 4 分钟窗口仍有 620 条失败，**全部是 user 级**、
account 级 0 条。真闸是 `users.concurrency = 5`。

⇒ **"我在前端改了"不等于"改到了那道闸"。** 判据只有一条：
sub2api 日志里失败事件的名字是 `user_slot_acquire_failed` 还是 `account_*`。
（[[feedback_config_knob_that_matches_symptom_must_prove_a_consumer]] 的同族：
一个数值看着像瓶颈 ≠ 它是那个被读到的瓶颈。）

## 2. 识别：怎么确认"就是并发闸"，而不是余额/上游/账号

| 观察点 | 并发闸 | 不是并发闸 |
|---|---|---|
| sub2api 日志事件 | `openai.user_slot_acquire_failed` + `error: "timeout waiting for user concurrency slot"` | 有 `upstream_status`/`upstream_body` ⇒ 上游/账号；`error_phase=request` 且 upstream 全 NULL 且 `users.balance` 转负 ⇒ 内部余额闸 |
| LiteLLM 报文 | `APIConnectionError: OpenAIException - Concurrency limit exceeded for user, please retry later` | 403 / 401 / capacity 原文 |
| RPM | **不涨也会炸**（长流各占槽几十秒） | — |
| 同期并发人数 | `count(distinct api_key)` 明显上台阶 | — |

那句英文原文是 sub2api 自己的，binary 里就有格式串
（实证：`grep -a "Concurrency limit exceeded" /app/sub2api` →
`Concurrency limit exceeded for %s, please retry later`）。
**别拿它去查 x.ai 文档**，跟上游无关。

⚠️ **LiteLLM 收到的是 `APIConnectionError` 不是 `RateLimitError`** ⇒
腿不进 cooldown、没有退避，`num_retries` 只是把同一个满队列再撞三次，
终局是 500。所以并发闸满的表现是**硬报错**，不是变慢。

## 3. 操作：`scripts/sub2api-concurrency.sh`

```bash
# 在 198 上（脚本要和 grok-onboard/sub2api_admin.py 保持相对路径）
scp scripts/sub2api-concurrency.sh cltx@10.68.13.198:~/sc-<sid>/
scp scripts/grok-onboard/sub2api_admin.py cltx@10.68.13.198:~/sc-<sid>/grok-onboard/

bash ~/sc-<sid>/sub2api-concurrency.sh show                  # 两道闸 + headroom
bash ~/sc-<sid>/sub2api-concurrency.sh set-user 1 1000       # 动闸①
bash ~/sc-<sid>/sub2api-concurrency.sh set-account 6 200     # 动闸②
bash ~/sc-<sid>/sub2api-concurrency.sh judge 5 sa-grok-4.6   # 判生效
```

**写入走 admin API 而不是直接 UPDATE**（API 会让 sub2api 自己的缓存失效），
**但回读只认 postgres** —— `PUT /api/v1/admin/users/{id}` 有"返 200 却静默丢字段"的
前科（`balance` 就是这样），API 不能给自己作证。脚本已经把这两条都固化了。

⚠️ **`set-account` 是危险的那个**：账号行里带 `credentials`（上游 token）。
部分字段 PUT 万一把它清了，这条腿就真死了。脚本因此做 GET→改→PUT，
并在写后断言 `credentials.sub` 没变，变了就立刻喊。

**生效不是瞬间的：闸值缓存约 30 秒。** 09-09 实测写入 16:43:08 →
最后一条失败 16:43:37（排干队列尾巴）→ 之后归零。

## 4. 验收：三条判据，缺一条都可能是假绿

1. **DB 回读** = 目标值（脚本已断言）。
2. **sub2api 日志**：`slot_acquire_failed` 归零，且**最后一条的时间戳早于写入时刻 + 30s**。
   ⚠️ 这个容器高负载时日志只留 **几分钟**（实测 `--since=8m` 最老一行只到 8 分钟前）。
   "窗口内 0 条"必须先确认**日志真覆盖了那个窗口** —— 脚本会打印最老一行的时间戳，
   就是为了防这个假绿（[[topic_ruler_failure_shapes]]）。
3. **LiteLLM 真流量**：`LiteLLM_SpendLogs` 里目标 `model_group` 逐分钟 `fail` 归零，
   且 `count(distinct api_key)` **没降** —— 人跑光了当然也不失败了，那是假绿
   （[[feedback_every_change_regresses_on_real_user_usage]]）。
   **自己发探针不算验收**：并发闸只在真并发下咬人，单发探针永远绿。

## 5. 定容：两道闸谁是天花板

`show` 最后会打印 headroom。**有效上限 = min(user 闸, 该平台 schedulable 账号 concurrency 之和)。**
account 之和低于 user 闸时，抬 user 是空转 —— 反过来也一样。

判"够不够用"的量具是**同时在线的 key 数 × 单条流的持续时间**，不是 RPM：

```sql
-- 峰值并发人数与流长（换 model_group）
select date_trunc('hour',"startTime") h, count(distinct api_key) keys, count(*) n,
       round(avg(extract(epoch from ("endTime"-"startTime")))::numeric,1) avg_s
  from "LiteLLM_SpendLogs"
 where model_group='sa-grok-4.6' and "startTime" > now() - interval '24 hours'
 group by 1 order by 1;
```

⚠️ **`avg_s` 在闸满时是被污染的**（排队时间算在里面），涨了不代表上游变慢 ——
09-09 那小时 20~30s → 58.6s 就是排队，不是 x.ai 劣化。要判上游快慢，取闸没满的窗口。

## 6. 2026-09-09 的事故与处置（现状基线）

| 时刻(北京) | 事 |
|---|---|
| 16:02 | `sa-grok-4.6` 开始大面积失败；当小时 174 请求 129 失败（74%）。前 6 小时同等 RPM 全 0 失败。onset 前后 distinct key **6 → 26**，成功耗时 20~30s → 58.6s |
| 16:42 | 用户在前端把 grok 账号并发 10→199 —— **落库了但没解**（4 分钟仍 620 条 user 级失败） |
| 16:43 | `users.concurrency` **5 → 500** → 16:43:37 起失败归零，08:44 UTC 那分钟 9 请求 0 失败 |
| 17:01 | 按用户要求再抬到 **1000**；近 3 分钟 0 失败、grok 12 请求全成 |
| 17:10 | 新增 grok 账号 18/19/20（各 200，走 `sub2api-grok-onboard.py add`），grok 账号槽合计 **1396** ⇒ 现在 user 闸 1000 是天花板，**再加账号并发是空转** |
| 17:34~17:49 | 收口：用户面 16 分钟连续 0 失败、每分钟 1~12 个不同 key、18~45s（基线 20~33s，无排队） |

**还挂着没做的一件**（用户 09-09 认为我给的 fallback 方案不好，未采纳）：
`sa-grok-4.6` 在 `router_settings.fallbacks` 里作为 **value 出现 19 次、作为 key 0 次**
⇒ 它是降级链终点，自己出问题就是 500 直接见用户。别自作主张加，等用户拍板。

## 关联

- 同一台 sub2api 的余额闸 / 建号 / 三条 grok 路分诊：skill `sub2api-grok-ops`
- 这台 sub2api 上还挂着 Kimi Allegro 和 antigravity，**它们和 grok 共用 user 闸①** ——
  抬 grok 并发等于给它们一起抬，动之前把影响面说清楚。
