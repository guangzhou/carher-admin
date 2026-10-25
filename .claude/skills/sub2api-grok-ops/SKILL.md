---
name: sub2api-grok-ops
description: >-
  198 LiteLLM 上 grok 的运维：**现存三条互相独立的 grok 路径**的分诊(修好一条不等于
  grok 好了；路 A grok-proxy 已于 2026-09-08 彻底删除，报错是预期别去修)、
  sub2api 内部虚拟余额充值、**加号=§C 顶部 `grok-warp-onboard.py` 一条命令（225 WARP 抓+198 建号；10-13 起 approve 走 225 浏览器，纯 HTTP 恒 403 castle；只有邮箱+x.ai密码没 sso 加 `--password-login`）**、
  **判档位（heavy/SuperGrok）只认 grok.com `dominantPlan`+JWT `tier`(5/1)，供应商说法和 53M 头都不算（§C 10-19）**、`sa-grok-4.6` 作为 cursor gpt 系 fallback、两层回归。
  同一台 sub2api 上还挂着 Kimi Allegro（`sa-kimi-k3` / `sa-kimi-code*`，group 8/9），
  动它前先看影响面。**升级 sub2api 本体也在这里（§G，一条命令 `sub2api-upgrade.py go`；生产带本地补丁，`go` 会从源码重打）**。
  **「grok 又被全部停下了 / 重新认证下」= §⚡ 最前面那一节：先确认巡检还在跑（`rescue`
  单跑撑不过两分钟，一条 403 park 的是整池），而且九成不该跑 reauth（腿通常是健康的，
  被 sub2api 自己 park 了）。**
  **grok 的 24 个名字里有 22 个走 model_list，2 个 video 走 pass-through（§H）——
  LiteLLM 的 `/v1/videos` 结构性不兼容 sub2api 且升级永远修不好。**
  **查/兑官方重置卡(rate-limit reset token)⇒ skill `grok-reset-card`，不在这里。**
  Use when 用户说"grok 不能用了"/"我的 grok 又被停了/全停了"/"grok 重新认证下"/
  "重新授权 grok"/"sub2api 403"/"grok 报余额不足"/
  "再加一个 grok 账号"/"加 N 个 grok 号(邮箱----密码----ai密码)"/"这号是什么类型/是不是 heavy/订阅什么时候到期"/"sa-grok-*/grok-4.5 打不通"/"kimi 不能用了"/"sa-kimi-* 报错"/
  "sub2api 有新版本了升级下"/"grok 出图能用出视频不能用"/"视频模型打不通"。
---

> **别走错：** Heavy 号 SSO 要 `grok-chat-heavy`（grok.com Web 通道）⇒ skill `grok2api-heavy-ops`（ns `grok2api`，另一套网关）；
> 问「Grok Bot 能不能用 Opus」⇒ skill `grokbot-opus-check`。本 skill 只管 sub2api。

## ⚡ 面板一堆 `grok-4.7` 502(上游 422)、5 腿全 422 —— 先当「请求形状」查，别查号（10-14）

10-14 真因：Codex Desktop 0.162 的 `agent_message` item，x.ai 每个号都拒；sub2api 换完 5 腿回 502，LiteLLM 再重试 6 次
⇒ 1 个坏请求 = 同一 LiteLLM pod IP、约 30s 内 6 条 502（间隔 3→10s 递增）。已上 callback `grok_agent_message_flatten`
（三车道）+ grok 组 `model_group_retry_policy` DefaultRetries=1，修后真实 422 链 0。

1. 聚类判放大：按 `client_ip` + 25s 间隔分簇，簇大小=6 ⇒ 是重试放大，真实失败数 = 簇数。
2. sub2api 不存 422 响应体（v0.2.16 grok responses 路径无 `Detail:`）⇒ 去 LiteLLM `LiteLLM_SpendLogs` 找
   `model_group='sa-grok-4.7' and model ilike '%deepseek%'`（被兜底的就是被拒的），比 input item type 集合。
3. 定因只认 A/B：`scripts/grok-422/probe_agent_message.py`（pod 内跑，关重试关兜底；第一格阳性对照不 200 整轮作废；
   ⛔ 别带 `tags`，tag 过滤会直接 500）。新出现别的 item type 就照 callback 的写法加分支。

## ⚡「我的 grok 又被全部停下了」—— 先看巡检，而且它**不是** reauth

🔴 **2026-09-20 起 `rescue` 单跑已经不成立了：一个坏请求会沿 failover 走遍整个池子，
10 条腿在 6 秒内逐个吃同一个 403、逐个被 park。** 所以「过几分钟又不可用」的第一步是**数
failover 链**，不是查账号健康度、也不是再手跑一遍 rescue：

```bash
PG=sub2api-postgres-bbd7d9995-l76qh   # ⚠️ 不是 StatefulSet，没有 -0 后缀
sudo kubectl -n litellm-dev exec $PG -- psql -U sub2api -d sub2api -c "
select request_id, count(*) hops, count(distinct extra->>'account_id') legs,
       string_agg(distinct extra->>'upstream_status',',') st,
       min(created_at), max(created_at)
from ops_system_logs
where created_at > now() - interval '2 hours'
  and message like '%upstream_failover_switching%'
group by 1 having count(*) >= 4 order by 2 desc limit 15;"
```

**一条链 hops=10 且 legs=10 ⇒ 找到了。** 09-20 13:10~13:35 每 1~2 分钟就有一条这样的链
（13 点那小时 39 条，`max_switches=10` 打满）。

🔴 **403 是那个请求的属性，不是账号的属性。** 判据：leg 30 在 13:35:25 吃 403，
13:35:27 / 13:35:45 就有成功记录；13:35:07 那条走遍全池的链之后 10 条腿全部在 20 秒内
恢复成交（18~232 次）。**腿是好的，是那个 payload 让 x.ai 拒。** 所以：

- ⛔ 别去查账号健康度、别 reauth、别充钱做厚池子 —— **池子越大，一条坏请求扫掉的腿越多**
  （`max_switches=10` 是它的上限，不是池子的）。
- 该查的是那个 caller 的 payload。09-20 的形状：`user_agent=AsyncOpenAI/Python 2.33.0`、
  `api_key_prefix=sk-1e13d`、`user_id=1`、`model=grok-4.6`、`stream=t`，
  `/v1/chat/completions` 和 `/v1/responses` 都有。

🔴 **两个把我带偏过的坏尺子，别再踩：**

1. **failover 的中间跳只写 `ops_system_logs`**（`extra->>'account_id'`），只有最后一跳才落
   `ops_error_logs`。只查后者会得出"10 条被 park 的腿里 7 条一条 403 都没吃过"这种假结论，
   进而编出"一条 403 停整池"的假机制。**判「这条腿吃过什么」两张表都要查。**
2. **先读 `temp_unschedulable_reason`。** 它写的是 `grok stream idle timeout` /
   `grok upstream temporary error` —— 字段里就有分类结果，我没读它就先讲成了 403 规则。

然后确认巡检在跑（止血，不治本）：

```bash
# 198 上
sudo tail -20 /var/log/grok-park-patrol.log     # 每分钟应有 freed=[...] ok=N still_held=0
sudo crontab -l | grep -A2 'BEGIN grok-park-patrol'
```

巡检 = `/home/cltx/grok-onboard/grok-park-patrol.sh`（repo 里在 `scripts/grok-onboard/`），
sudo cron 每分钟起一次、脚本内自己跑 4 轮 × 15s，只放探针实测 `200 usable` 的腿。
⛔ **改 cron 间隔必须同时改脚本里的 `SWEEPS`**，否则两轮叠在一起被 `flock` 挡掉，
表现是"巡检不跑了"。

**它只是止血：15 秒内把腿捞回来，不阻止 park 发生。** 每分钟 1 轮时池子在 11 条 ↔ 1 条之间跳、
503 每 30 秒几十条；4 轮 × 15s 之后 13:38 起 0 parked / 10 open、503 归零、成交 72~91/分。
⚠️ **但那个绿不是巡检赢了，是坏请求 13:36 自己停了** —— 同期 failover 链从每分钟一条掉到零。
别把 503 归零当成"已修复"，判是否真好了要看链数。

**真解顺序**：① 找到发坏 payload 的 caller（上面那组特征）；② 让一个请求别连着换 10 条腿
（`max_switches`）；③ 升级 sub2api 本体（§G）。⛔ 充钱做厚池子不在这条链上 ——
余额死的那 11 条（7/8/9/18/20/27/28/29/33/34/35）该充是另一笔账，但它治不了这个症状。

手动单跑（巡检没装、或要看一眼分类结果时）：

```bash
cd ~/grok-onboard      # 198 上；脚本已在，不用再传
sudo kubectl -n litellm-dev get secret sub2api-secrets \
  -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d | sudo tee /run/.s2apw >/dev/null
sudo chmod 600 /run/.s2apw

sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py rescue --dry-run  # 先看
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py rescue            # 再放
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py regress --minutes 15
sudo rm -f /run/.s2apw
```

`rescue` 做的事：读全五把闸 → **拿每条腿的 token 真打 `api.x.ai/v1/models`** →
只对 `200 usable` 且被扣住的腿 `clear-error` + `bulk-update` → 读 postgres 回核 →
把窗口内的 admin 审计行打出来（**第二只手**）。

🔴 **用户说"重新认证下"时，九成不该跑 `reauth`。** 2026-09-20 一上午停了三回，三回都是
**健康的腿被 sub2api 自己扣住** —— 八条腿直打 x.ai 全 `200 usable`，reauth 是把一个 xAI
已经认的 token 重写一遍，纯 no-op。`reauth` 只在**手里有新凭据行**且探针说存的 token 死了时才跑。

| 症状 | 该跑 | 不该跑 |
|---|---|---|
| 好的腿过几分钟又变停 / 全池反复 503 | **数 failover 链**（上面那条 SQL），找发坏 payload 的 caller | ⛔ 查账号健康度、⛔ reauth、⛔ 反复手跑 rescue、⛔ 靠充钱做厚池子 |
| 全池 503 / 腿都停了，探针 `200 usable` | 查巡检日志；没装就装巡检（`rescue` 单跑撑不过两分钟） | ⛔ reauth（no-op） |
| 腿上 `rate_limited_at` 非空 | 当**余额死**处理，只能充钱 | ⛔ 查限流、⛔ 等窗口过期（这个标志永不自清） |
| 探针 `403 ...bad-credentials`，且手里有新 SSO 行 | `reauth <creds>` | — |
| 探针 `403 ...spending-limit` | **只能充钱** | ⛔ 两个都是 no-op |

⛔ **`rescue` 不放 `spending-limit` 的腿**，这是故意的：放出来只是多一条腿吃 failover 再 403，
09-20 就是这么把一个本来就薄的池子推成全池 503 的。

⚠️ **`rate_limited_at` 不是在 403 那一刻打的，是在"被放回池子后第一次真的出活"那一刻打的。**
leg 7/8/9 三条的 `rate_limited_at` 同为 `09-17 11:00:03`，而 `audit_logs` 里 `10:59:54`
有人 `bulk-update {"account_ids":[7,8,9,18],"schedulable":true}` —— 9 秒之差。
⇒ 查这个标志的来源必须把 `audit_logs` 排进去，**只按 ±5 秒去配 403 会只匹配到 2/11 条，
得出"对不上"的假结论**。

### 这一节踩过的坑（前三个都写进脚本了，别再手搓这套命令）

1. **`reset-quota` 不碰 `temp_unschedulable_until`** ⇒ 跑完整套 reauth 腿仍然是 park 状态，
   503 一条不少。09-20 09:4x reauth 完 36/37，直到单独 `clear-error` 才停。
   **已修**：`reauth` 第 5 步现在自己打 `clear-error`。
2. **`bulk-update` 返 `success:8 failed:0` 之后立刻读库，读到的是旧值。** 09-20 12:18 八条腿
   `updated_at` 已经是 12:18:22、`schedulable` 已经是 `t`，而单次 readback 全读成 `f`。
   **已修**：readback 重试 4 次 × 3s 收敛，不是 sleep 一次赌延迟。
3. 🔴 **`psql -t -A` 的布尔有两种拼法**：裸列印 `t`/`f`，`boolean::text` 印 `true`/`false`。
   我在新查询里加了 `::text`，`== "t"` 的解析当场把八条 `true` 全判成 `False` ⇒
   **八个 "STILL HELD" 假红，形状和"写没落"一模一样**，害我去查 group、查库名、查竞态。
   **已修**：统一走 `pgbool()`，且 SQL 侧不再 cast 布尔。判「写没落」之前先确认读数器没坏。
4. 🔴 **但 13:23 那批 10 个 "STILL HELD" 是真的，不是第 3 条那个假红。** 判据：
   `updated_at=13:13:41 → until=13:43:41`，而我的 `clear-error` 是 13:13:40 打的 ——
   写落了又被立刻改写。**假红和真红的区别只在时间戳**，回读读成 held 时先看
   `updated_at` 是不是比你的写更新。
5. ⛔ **`rescue --no-probe` 永远放不出腿。** `_rescue_classify` 里 `verdict` 被置成
   `"not probed"`，`.startswith("200")` 为假 ⇒ 每条腿都进 `deadtok`，`free` 恒空，
   而且不报错。**未修**，别用这个 flag。

### 第二只手（09-20 实测，会让状态来回翻）

`sub2api` 自己写 park **不留审计行**，所以 `audit_logs` 里每一行都是人或另一个会话。
⛔ `actor_email` 区分不了：**大家共用 `admin@sub2api.local`，脚本自己也是它**，只能按时间分。

09-20 当天：10:51 有人用 `sso-to-oauth` 建了 acct 38/39（不是我建的）；11:01 和 11:31
两次把腿 `POST /accounts/:id/schedulable {"schedulable":false}` 关掉，**11:31 那次就在我
11:28 放开之后 34 秒**；12:18 之前整池 19 条全被关成 `sched=f`，包括三条正在跑
163/184/200 req/15min 的。所以「放开又被停」先看审计行，别默认是 sub2api 干的。

## 🚦 第 0 步（2026-09-17 起强制）：`health` 先拿基线，再动任何东西

```bash
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py health
```

**加号前跑一次、收尾再跑一次。** 09-17 加两个号时，动手那一刻池子里
**7 条腿有 6 条在空转**、正在对外吐 503，而我是在建号之后的回归里才看见失败率突变，
第一反应差点变成"我把它搞坏了"。基线读数是把「既存故障」和「我的改动」分开的唯一办法。

它同时挡掉两个此前没有量具的坑，两个都在 §A0 展开：

| 坑 | 形状 |
|---|---|
| **`schedulable=t` 不等于这条腿在服务** | 09-17 有人 11:00 把 6/7/8/9/18/20 逐个 flip 回 `t`，14 分钟后仍是**零 usage_logs、零 upstream 尝试**；而且到 11:20 已被 sub2api **自己翻回 `f`**，手动 flip 根本不成立 |
| **503 有两种，只有一种能靠加号修** | `error_phase=routing` + `account_id NULL` = 没选到腿、请求没出网；`error_phase=upstream` + 真 account_id = 选到了腿、x.ai 拒了 |

⚠️ **反过来也要小心：刚建的腿零 `usage_logs` 有一个良性形状，别当故障。** 见 §C4 ——
新腿在被首选（`switch_count=1`）之前，只会作为 failover 备胎出现，
而备胎接到的是别人已经失败的那个请求，必然一起失败 ⇒ 零 `usage_logs`。
判据是 **`switch_count` 有没有出现过 1**，不是"有没有 200"。

# sub2api / grok 运维

## ⚠️ 第一节：grok 在 198 上原有**四条互不相干的路**，2026-09-08 起只剩三条

修好一条 ≠ grok 好了。09-04 我就栽在这：修完 sub2api 报"grok 恢复"，
一梳理才发现同事日常用的那 4 个名字**当时全是坏的**。

```bash
# 报"grok 不能用"时，第一件事是把现存 6 条 entry 全打一遍，而不是只看用户提到的那条
for m in sa-grok-4.5 sa-grok-4.6 sa-grok-4.20 \
         cursor-grok-4.5 cursor-grok-4.6 openrouter-grok-4.5; do ... done
```

| 路 | entry | 后端 | 典型故障 | 修法 |
|---|---|---|---|---|
| ~~**A 订阅直连**~~ | ~~`grok-4.5` `grok-4.6` `claude-grok-4.5` `claude-grok-4.6`~~ | ~~grok-proxy(225) → api.x.ai~~ | **09-08 已彻底删除**，见 §E | — |
| **B sub2api** | `sa-grok-4.5` `sa-grok-4.6` `sa-grok-4.20` | sub2api(198) → api.x.ai | **403** 余额不足 | §B 充值 |
| **C cursor shim** | `cursor-grok-4.5` `cursor-grok-4.6` | cursor-agents-shim(225) | — | 见 cursor 线 |
| **D openrouter** | `openrouter-grok-4.5` | 188 bridge :4130 | — | — |

**路 A 已于 2026-09-08 按用户指令彻底删除**（LiteLLM entry + `grok-proxy` pod/svc/secret 全清）。
它的 OAuth refresh 链早就断了、7 天窗 387 请求 386 失败，删除**不是**回归。
现在看到 `grok-4.5/4.6`、`claude-grok-4.5/4.6` 报错 = **预期**，别去"修"它，
更别照 §E 重抓 token 把它救活 —— 那是复活一条用户已经判死刑的路。
仍有 596/572 把同事 key 的 `models` 白名单里留着这 4 个名字（**白名单留着无害**，
只是打了会失败）。详见 [[project_grok_path_a_deleted_2026_09_08]]。

## 拓扑（别再混淆）

有两套 sub2api：**188 docker 那套是空壳，永远别去动**。现役的在
**198 k3s ns `litellm-dev`**：`sub2api` + `sub2api-postgres` + `sub2api-redis`。

```
Cursor/客户端 → litellm-product :4000  (entry sa-grok-4.5 / sa-grok-4.6 / sa-grok-4.20)
              → http://sub2api.litellm-dev.svc.cluster.local:8080/v1   (NodePort :31880)
              → sub2api 内部虚拟账本闸  ←← 403 通常死在这里，还没出网
              → https://api.x.ai/v1     (grok 订阅号 OAuth token)
```

| 东西 | 值 |
|---|---|
| admin | `admin@sub2api.local`，密码在 `litellm-dev`/`sub2api-secrets` 的 `ADMIN_PASSWORD` |
| 登录 | `POST /api/v1/auth/login` → token 在 `data.access_token` |
| keys | `/api/v1/keys`（**不是** `/admin/api-keys`） |
| accounts / groups | `/api/v1/admin/*` |
| 生产 key | `grok-litellm` id=5，绑 group 7「grok账号分组」 |
| grok 账号 | **59 条腿**（10-14 21:36 加完 acct 79/80），同时刻只 **3 条** 在服务（10-14 实测 3/59）、其余多为 `spending-limit`；逐号历史见 memory `topic_grok_index`。⚠️ **别拿腿总数当容量**，跑 `health` 拿真数 |
| DB | postgres pod 里 `psql -U sub2api` |
| helper | `scripts/grok-onboard/sub2api_admin.py` |

198 上 kubectl 一律要 `sudo`（`/etc/rancher/k3s/k3s.yaml` 权限），输出过滤 `^\[sudo\]`。

## A0. 报 **503** —— 池子选不出腿（2026-09-17 实测，和 403/并发闸都不是一回事）

⚠️ **先分清三种，别一律当"grok 挂了"**：

| 状态码 | `error_phase` | `account_id` | 含义 | 修法 |
|---|---|---|---|---|
| **503** | `routing` | **NULL** | 池子里**没有一条可用腿**，请求没出网 | 加号 / 等退避到期（本节）|
| 403 | `request` | NULL | sub2api 内部虚拟余额闸 | §B 充值 |
| 500 | — | — | `Concurrency limit exceeded` 并发闸 | skill `sub2api-concurrency-gate` |
| 502/400 | `upstream` | **真 id** | 选到腿了，x.ai 拒了 | 查 x.ai 侧，**加号无用** |

`health` 直接把这张表读出来。判据 SQL（`ops_error_logs` 里**没有** `upstream_status`
这一列，是 `upstream_status_code`，照旧文档写会报 column does not exist）：

```sql
SELECT error_phase, coalesce(account_id::text,'NULL'), count(*)
FROM ops_error_logs WHERE created_at > now() - interval '30 minutes'
  AND platform='grok' GROUP BY 1,2 ORDER BY 3 DESC;
```

### 🔴 2026-10-21：池里只剩一条腿时，**一个小众模型的 429 会把整号冻 2 分钟 ⇒ 全模型 503**

用户说「163 怎么了，为什么限流」。号**没被封、没烧完**：`status=active`、
`schedulable=t`、`rate_limited_at` 空、grok-4.6 的头还是满格 8300/53M、周额度 47%。
真因是 sub2api 自己的冷却粒度：

| 三段式 | |
|---|---|
| 假设 | `grok-4.20-multi-agent` 的 429 → sub2api 冷却**整个号** → 单腿池全模型 `pool=0` → 503 |
| 证伪条件 | 若无关，503 的分钟分布应与 429 的时刻无关；或冷却期里别的模型仍能选到腿 |
| 数据 | 36 条 429 里 **35 条是 `grok-4.20-multi-agent*`**；每条 429 之后**所有**模型立刻报 `no available Grok accounts ...(pool=0)`，持续 ~2min（22:48:24 → 22:50:22），与源码 `grokRateLimitFallbackCooldown=2m` 逐秒吻合；429 约每 6min 一发 ⇒ 约 **1/3 时间整池断**，断的那几分钟 no_leg 65~281/min 而 163 自己 ok=0 |

机制在源码里（`~/codes/sub2api`）：`grok-4.20-multi-agent` 的 429 body 是
`Upstream rate limit exceeded`，**不带 Retry-After / reset 头** ⇒
`grokRateLimitResetAt()` 落到 `grokRateLimitFallbackCooldown = 2 * time.Minute`，
`persistGrokRateLimit()` 写的是 **account 级** `rate_limited_at/rate_limit_reset_at`，
不是 model 级。重复 429 还会自适应加长（2min → 10min → 30min → 1h，
`grokRateLimitRepeatCooldown` 一族）。

**判这个形状的两步**（别只看 `health` 的服务腿数，它会显示"有一条腿在服务"）：

```sql
-- ① 429 是不是集中在一个模型
select coalesce(requested_model,model), count(*) from ops_error_logs
 where account_id=<id> and status_code=429 group by 1 order by 2 desc;
-- ② 503 是不是跟在 429 后面（分钟级对齐，三列一起看才看得出因果）
with m as (select generate_series(date_trunc('minute',now()-interval '50 minute'),
                                  date_trunc('minute',now()), interval '1 minute') t)
select to_char(m.t at time zone 'Asia/Shanghai','HH24:MI'),
 (select count(*) from usage_logs u where u.account_id=<id>
    and u.created_at>=m.t and u.created_at<m.t+interval '1 minute') ok,
 (select count(*) from ops_error_logs e where e.account_id=<id> and e.status_code=429
    and e.created_at>=m.t and e.created_at<m.t+interval '1 minute') r429,
 (select count(*) from ops_error_logs e where e.platform='grok' and e.error_phase='routing'
    and e.created_at>=m.t and e.created_at<m.t+interval '1 minute') no_leg
from m order by 1;
```

🛠 **别手搓这两条查询：`diag/grok-pool-watch.sh`** 每分钟采一行 CSV
（`served,no_leg,r429,r429_ma,<每条腿>,keys`），`no_leg` 超阈值的那分钟连同**前 3 分钟的 429 明细**
写进事件文件，因果同行。改前改后各跑一份直接对比：

```bash
cd <repo>/scripts/grok-onboard/diag
OUT=before END_HHMM=1830 ./grok-pool-watch.sh     # 改前
OUT=after  END_HHMM=2330 ./grok-pool-watch.sh     # 改后
awk -F, 'NR>1&&NF>5{n++;s+=$2;nl+=$3;if($3>=20){b++;if(!o)st++;o=1}else o=0}
  END{printf "minutes=%d served=%d outages=%d outage_min=%d unserved=%d\n",n,s,st,b,nl}' before.csv
```

🔴 腿的列是**现量**的（最近 2h 真发过请求的 account_id），别写死号 —— 写死 163/164 这种，
换了号就恒读 0，长得像「这条腿没流量」的假绿。
🔴 判「改好了没」**必须带改前那份**：10-22 晚上改后 0 次断流，但晚间流量只有下午的 1/4
（46 → 12 req/min），单看「改后 0 次」不足以下结论，真正的证据是 22:15 那次
**同形状的 429 没有导致整池断**（同 30s 内该腿照接 18 个模型 33 个请求）。
⇒ [[feedback_quiet_window_is_not_proof_the_fix_works]]

pod 日志里的直接判据：`openai.account_select_failed` 带
`no available Grok accounts supporting model: X (pool=0)`。
`pool=N, filtered: <原因>=n` 那个括号是 sub2api 自己给的排除统计
（`openAISelectionFilterStats.summary`），**有 `filtered:` 就照它说的原因去查**，
`pool=0` 则是连候选都没进来（`listSchedulableAccounts` 就空了）。

⚠️ 没验证的一格：冷却那 2 分钟里 x.ai 是否真的也拒别的模型 ——
sub2api 在冷却期**根本不发请求**，从外面测不到，别写成"x.ai 只限了这一个模型"。

修法三选（10-21 未动，待用户定）：①继续加号（只稀释，单号仍会被冻）
②在 LiteLLM 给 `multi-agent` 限并发 / 把它挪出这个池（去触发源）
③改 sub2api 成模型级冷却（最对症，要改源码+发版）。
→ [[project_sub2api_grok_multiagent_429_freezes_single_leg_2026_10_21]]

### 09-17 那次的完整形状（下次对号入座）

用户面 `sa-grok-4.6` 从 `ok=59/fail=0`（10:38）塌到 `ok=2/fail=17`（10:44），
持续到 10:56；`ops_error_logs` 同窗 **6443 条 503 / `routing` / `account_id` 全 NULL**。
`usage_logs` 显示 acct 20 最后一发 **10:41**、acct 18 最后一发 **10:25**，此后无任何腿服务。
`accounts.rate_limited_at` 上 acct 20 是 **10:39:19**，与失败起点**秒级吻合** ——
老腿被 x.ai 侧限流打到集体退避，池子空转 17 分钟。10:57 建的两个新号一上线，
那一分钟失败就从 33 塌到 4，下一分钟 0。同窗 kimi 也有 10~21 条/分的 503 routing（旁证同一台）。

### 四个坏尺子（都栽过）—— 外加下面那把 `temp_unschedulable_until`，四把全绿它也能 503

0. ⛔ **上面三个尺子全绿/全红都区分不了"token 死"和"xAI 余额死"**，而这两个的处置
   完全相反（重新授权 vs 只能充钱）。分开它俩的唯一办法是**拿新 token 直打
   `api.x.ai/v1/models` 看 body**：`403 personal-team-blocked:spending-limit` = 余额没了。
   `health --xai` 就是这把尺子，细节见 §C5 红线①。


1. ⛔ **`schedulable` 不是"这条腿在服务"的判据。** 上面那 6 条腿 11:00 被人工 flip 回 `t`，
   14 分钟零流量零 upstream 尝试，随后被 sub2api 自己翻回 `f`。
   **唯一判据是 `usage_logs` 有行 / `ops_error_logs.account_id` 非 NULL。**
2. ⛔ **`quota` 端点判不了死活。** 09-17 老 7 条全返 **502**
   `GROK_QUOTA_TOKEN_UNAVAILABLE` / `oauth refresh account state changed`
   （09-09 是 200+`snapshot=null`，**同一个量具缺口换了个形状**），新号 27/28 返 200 满读数。
   `NO LIVE PROBE` 和 502 都只说明尺子没读数。
3. ⛔ **`rate_limit_reset_at` 有未来时刻 ≠ 已证明调度器读这一列。** 09-17 那 6 条腿的
   reset 指向 09-18～10-01，与"零流量"强相关，但我**没有**证据证明选腿逻辑读的是它
   （相关不是机制）。要下这个结论得去读 sub2api 选腿代码或拿到调度日志。

### 🔴 第五把尺子：`temp_unschedulable_until`（2026-09-20 新增，前四把全绿它也能让整池 503）

**09-20 全池 503 的真因就是它，而上面四把尺子当时全绿。** 形状：

```
schedulable = t          ← 绿
rate_limited_at = NULL   ← 绿
新 token 直打 api.x.ai   ← 200 usable（连红线①都绿）
temp_unschedulable_until = now()+30min   ← 真正的闸在这里
temp_unschedulable_reason = 'grok access or entitlement denied'
```

09-20 09:35:45 **那一秒**，4 条正在服务的腿（30/31/35/37）被同时 park 到 30 分钟后，
`routing`/`account_id=NULL` 立刻起到 300~440 条/分，用户面 ok 从 79/min 塌到 2~9/min、
fail 36~50/min，持续 9 分钟。**触发它的只是 acct 30 的一条上游 403**
（`upstream_status_code=403` "Upstream access forbidden"）—— sub2api 把单条 403
放大成整腿停 30 分钟。腿本身是好的，所以**加号和 reauth 对它都是纯 no-op**。

⛔ **别把 `reason` 里的 `entitlement denied` 当成"这号没订阅"**：那是 sub2api 自己贴的标签，
不是 xAI 的判决。xAI 的判决在红线①那个 body 里，当时是 200。

```sql
-- 判"全池 503 但腿看着都好"必查这一条
SELECT id,name,schedulable,rate_limited_at,temp_unschedulable_until,temp_unschedulable_reason
FROM accounts WHERE platform='grok' AND deleted_at IS NULL
  AND temp_unschedulable_until > now() ORDER BY id;
```

**清它只有一个端点：`POST /api/v1/admin/accounts/{id}/clear-error`。**
09-20 拿 acct 36 试、acct 37 当阴性对照逐个分辨过：

| 端点 | 对 `temp_unschedulable_until` | 备注 |
|---|---|---|
| `POST /accounts/{id}/clear-error` | ✅ 清成 NULL | 唯一有效 |
| `POST /accounts/{id}/clear-rate-limit` | ⛔ 一位没动 | 返 200 |
| `POST /accounts/{id}/recover-state` | ⛔ 一位没动 | 返 200 |
| `POST /accounts/{id}/reset-quota` | ⛔ 一位没动 | 它清的是 `rate_limited_at` |

⚠️ **reset-quota 是 `reauth` 的第 4 步**，所以**跑完整套 reauth，腿仍然是 park 状态** ——
09-20 我 reauth 完 36/37 两条腿，503 一条没少，直到 `clear-error` 才停。

放腿**别再手搓这个 for 循环**（漏掉 `bulk-update` 和回核，第一版就漏了）——
`rescue` 把分类、放腿、回核、查第二只手四件事一起做了，见本文件最前面的 §⚡。

```bash
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py rescue --dry-run
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py rescue
```

**验收看逐分钟塌不塌，别信端点返 200。** 09-20：`routing` 09:44=437 → 09:45=71 →
09:46/47=**0**，`usage_logs` 回到 75/86/50 每分钟，`regress --minutes 30` EXIT=0。

📐 **底层原因是池子没有冗余，而且 09-20 一天内三次复发。**

⛔ **别再抄"11 条 token 死、可 reauth"这个数** —— 那是**坏尺子读出来的**。池子里存的是几天前的
token，过期后直打 x.ai 报 `403 unauthenticated:bad-credentials`，**把底下真正的
`spending-limit` 盖住了**。当天下午拿新 SSO 换过一轮再打，`bad-credentials` 一条不剩：
19 条腿里 **11 条 `spending-limit`（真没钱，只能充）、8 条 `200 usable`、token 死 0 条**。

🔑 **所以「token 死还是余额死」只有拿刚换的新 token 打才算数**；用池子里存的旧 token 打，
读数会系统性偏向"token 死"，而 token 死是能 reauth 的那一类 ⇒ 会让人一轮轮白跑 reauth。

8 条活腿撑全部流量，任何一条上游 403 都会 park 掉一条 30 分钟。**修完这次还是 8 条**，
下一条 403 会重演 —— 要真止住，只有给那 11 条充钱，或者补新号。

### 什么时候该说"加号解决不了"

`upstream` 相位占多数（`account_id` 是真 id）时加号没用。09-17 加完号之后残留的
就是这种：1~2 条/分 `upstream`，属常态噪声，不是故障。

## A-1. 🔴「这个号还是会员吗 / 掉订阅了吗」—— 订阅真源只在 grok.com，要 sso，且**走 188**

2026-10-18 实跑（76/155/156 三条腿定性 + park）。**x.ai 侧的 token 看不到订阅**，
判订阅只能打 `grok.com/rest/subscriptions`（要 sso cookie），库里 `credentials` **不存 sso**，
只有 `refresh_token` ⇒ 没 sso 就只能退一步用「上游判词 + token claim」旁证。

| 量具 | 怎么读 | 坑 |
|---|---|---|
| `grok.com/rest/subscriptions` | 唯一真源：`productId` + `status` + `expiryTime` | 要 sso；🔴 **本地 Mac 打不通，走 188**（[[feedback_github_chatgpt_via_188]]）。401 `Failed to look up session ID` = **cookie 失效，不是出口问题** —— 判据是同机同分钟另一把 sso 回 200（阳性对照） |
| 新 token 直打 `/v1/chat/completions` | `spending-limit` / `resource-exhausted 0/0` / 200 | 🔴 **必须先用 `refresh_token` 换新 token**：存量 token 过期会回 `bad-credentials` **盖住**底下的 `spending-limit` |
| access_token 的 `tier` claim | 活号有（heavy=5、**SuperGrok=1**，10-19），**掉订阅的号连这个 claim 都没有** | 判档可用，判活不行 |
| ⛔ `/v1/models` | **不能判活** | 死腿照样 **200 + 列全部模型**；也**不带** rate-limit 头 |

**10-18 这轮结果**：76 `ultra INACTIVE / 10-16 04:20Z 过期` + 上游 `spending-limit`（两量具同向）；
155 `spending-limit`；156 限流 **0/0**（配额档位归零，不是打太多）；157 `ultra ACTIVE 到 11-12` + `200 PONG`（阳性对照）；
75 的 sso 已 401 ⇒ **订阅状态无数据，别下结论**，要查得让用户重新登录拿新 sso。
三条死腿已 park（`bulk-update`，postgres 回读 `f`），`health` = **2 of 60 served**（157+72），零错误行。

🔴 **155/156 的额度是前身 79/80 烧光的**（10-14~15 共 3152+5160 req / ~5500 万 tok）：
**重建一条 sub2api 腿不会带来新额度** —— xAI 的余额跟 xAI 账号走，不跟库里的行走
（155/156 与 79/80 `credentials->>'sub'` 逐字相等，生涯请求数 **0**）。

⚠️ **我说错过一句，别再传**：「一条 403 会 park 整池」是**错的**。真相见
[[feedback_sub2api_grok_park_is_pool_wide_not_per_leg]]：一个坏请求沿 failover 打 10 条腿、逐个被 park，
**403 是请求的属性不是账号的**。park 死腿的正当理由是：订阅没了的腿留在池里只会吃 failover 再失败、拉长重试链。

## A. 路 B(sub2api)报 403 —— 第一刀分诊

⚠️ **先分清是 403（余额闸）还是 500 + `Concurrency limit exceeded for user`（并发闸）。**
后者是完全不同的故障：sub2api 有两道并发闸，`users.concurrency`（全平台共享，先满的是它）
和 `accounts.concurrency`（每个上游号）。**前端改账号那道对它零效果**（09-09 实测）。
并发闸的识别/操作/验收全在 skill **`sub2api-concurrency-gate`** +
`scripts/sub2api-concurrency.sh`，本节只管 403。

**别先猜订阅到期/账号被封/上游限流。** 先用一条 SQL 把内部闸和上游问题分开：

```sql
-- postgres pod 内
SELECT status_code, error_phase, upstream_status IS NULL AS no_upstream,
       count(*), min(created_at), max(created_at)
FROM ops_error_logs WHERE created_at > now() - interval '2 days'
GROUP BY 1,2,3 ORDER BY 4 DESC;
```

判据（详见 [[feedback_sub2api_403_balance_is_internal_ledger_not_upstream]]）：

| 形状 | 结论 | 下一步 |
|---|---|---|
| `403` + `error_phase=request` + `upstream_*` **全 NULL** | 请求根本没出网，是 **sub2api 内部虚拟余额**闸的 | 走 §B 充值 |
| 有 `upstream_status` / `upstream_body` | 才轮到上游 / 账号 / 限流 | 查 x.ai 侧 |

坐实内部闸再补两枪：

```sql
SELECT id,email,balance,total_recharged,updated_at FROM users WHERE id=1;
SELECT id,name,platform,status,schedulable FROM accounts WHERE platform='grok';
```

`balance` 为负、且 `updated_at` 与第一条 403 的时间戳**秒级吻合** = 闭环。
同时确认 `status=active` / `schedulable=t` 以排除混因。
（09-04 实例：balance `-0.01314125` @ `19:11:19.012`，首条 403 @ `19:11:19.604`。）

## B. 充值（内部账本）

```bash
# 只认这个端点
python3 sub2api_admin.py POST /api/v1/admin/users/1/balance '{"balance":100000,"operation":"set"}'
# 写完必须 GET 回读
python3 sub2api_admin.py GET /api/v1/admin/users/1
```

⚠️ `PUT /api/v1/admin/users/{id}` 里塞 `balance` 会返 **200 但静默丢弃** ——
[[feedback_api_silently_ignores_unknown_field_returns_200]] 的又一例，不回读就是假绿。

余额是**会耗尽的量，不是永久开关**：按 token 记账，08-21→09-04 烧了 ≈$594。
下次再全线 403，第一刀仍是查 `users.balance` 是否转负。

## C. 加一个新 grok 订阅号

### ⚡⚡⚡ 2026-10-23：用户说「加一个 grok 到 sub2api + 换密码 + 登记多维表格」= 三条命令，别拆开手搓

粘来的是 `email----Gx!…----eyJ…`（中间段 = x.ai 密码，§C6）。凭据放 `.work/grok-<MMDD>/creds.txt`（0600）：

```bash
cd <repo>/scripts/grok-onboard
python3 grok-warp-onboard.py ../../.work/grok-<MMDD>/creds.txt --dry-run   # 看基线「N of M legs served」
python3 grok-warp-onboard.py ../../.work/grok-<MMDD>/creds.txt             # 建号 + 回归 + 第5步登记表
python3 grok-pw-rotate.py --creds ../../.work/grok-<MMDD>/creds.txt --dry-run
python3 grok-pw-rotate.py --creds ../../.work/grok-<MMDD>/creds.txt        # 先写表→改→两发探针→补ID列
python3 grok_bitable.py sync                                               # 自查：这个号不该出现在输出里
rm -P ../../.work/grok-<MMDD>/creds.txt
```

- **顺序：先建号再改密。** 建号用的是粘来的 sso，改密不吊销 sso（§C6 ④），反过来也行但没必要
- 10-23 acct 165 实测全程约 15 分钟一把过；改密后复量服务腿不变
- **表的读写只走 `grok_bitable.py`**（两个脚本都 import 它）。规则：**只填空不覆盖**，已有值≠sub2api ⇒ 报 `MISMATCH` 由人判
- 🔴 10-23 之前建号脚本**从不写表**：`sync` 当天量出 sub2api 68 个 grok 号、表 36 行、**56 处缺**（20+ 个号没行，含 164）。
  `sync --apply` 会批量写共享表 ⇒ **先问用户**；新建的行只有账号/ID，sso/密码不在 sub2api 里补不出来
- 阳性对照：`sync` 的输出里不该有刚登记好的那个号（10-23：163/165 不在列表里，164 在 NO ROW 里）

### ⚡⚡ 2026-09-29 起的现行路径：**一条命令 `grok-warp-onboard.py`**（先看这里）

用户粘来 `email----pw----sso`（或 8 段长行）⇒ 凭据写到 **repo 外**的 0600 文件，然后：

```bash
cd <repo>/scripts/grok-onboard
python3 grok-warp-onboard.py ~/x/creds.txt --dry-run   # 可选：parse + 基线 + 两端 sha 核对，不抓不写
python3 grok-warp-onboard.py ~/x/creds.txt             # 真跑，约 8 分钟（含 --wait 360）
shred -u ~/x/creds.txt
```

它只编排、不重写任何一步：本地 parse → `health` 基线（打印「N of M legs served」+ 既存
`routing`）→ 225 mktemp 目录跑 `xai_sso_device_oauth.py`（WARP）→ **只把抓取器判
`OK` 的 bundle** `scp -3` 到 198 mktemp 目录 → `create-grok-oauth-account.py`（模板默认
acct 61）→ 等 `--wait` 秒 → `regress` + `health`，打印服务腿数前后对比 →
**（10-23 起）第 5 步登记飞书 grok 账号表**（账号/Grok SSO/Grok密码 + 从 sub2api 回读的
Sub2API账户ID/Grok UserID，只填空；失败不影响建号结果，提示跑 `grok_bitable.py sync --apply`；`--no-bitable` 跳过）。
两端临时目录 finally 里 shred+rm；远端副本先比 sha256 与 repo 逐字相等才跑。

| 为什么不再用下面 §C0 / 09-21 那条 `sub2api-grok-onboard.py onboard` | |
|---|---|
| 它第 2 步 `verify` 走 sub2api 自带 SSO 端点 | 09-25 起从 pod 出口**恒 403**（`accounts.x.ai` 挡机房，§C1b），整条中止 |
| 它从 Mac 跑会 `delegate()` 覆盖 `/home/cltx/grok-onboard/` | 那份是 **park 巡检 cron 每分钟跑的代码**；本脚本用 `S2A_REMOTE_DIR=<mktemp>` 绕开，常驻副本不动 |

### ⚡⚡ 2026-10-22：approve 报 `Request could not be verified`（Castle 403）**脚本已自动兜底**

默认 `--approve browser` 撞到 Castle 403 会**当场切到 Authorization Code 授权**，不用加参数、不用转 WARP、
不用找人手点。日志里看到 `device approve hit Castle ... -> falling back to authorization-code grant`
接着 `[authcode] code N chars, state ok` 就是兜住了。也可以 `--approve authcode` 直接跳过 device flow。
原理、三个坑、测试 ⇒ 下面「approve 必须走浏览器」那节末尾的 10-22 小节。

### ⚡ 2026-10-19：凭据**只有「邮箱----邮箱密码----x.ai密码」没有 sso** ⇒ 同一条命令加 `--password-login`

先判形状：第 3 段是 `eyJ…` = sso ⇒ 照上面跑；是短密码（如 `aa112233@`）= x.ai 密码 ⇒：

```bash
# 凭据放 repo 内 .work/<任务>/（已在 .git/info/exclude），0600；跑完 rm -P
python3 grok-warp-onboard.py --password-login .work/grok-<MMDD>/pw-creds.txt --dry-run
python3 grok-warp-onboard.py --password-login .work/grok-<MMDD>/pw-creds.txt
```

225 同一个私有 Xvfb 里先跑 `xai_pw_login_sso.py`（登录**复用** `xai_device_capture.do_login()`，
只多取 `sso` cookie）→ 写 `creds.txt` → 接原第 2 步。只有拿到 sso 的号往下走；`STOP <email>: …` 的号原因打在输出里。
10-19 首跑（当时手动两段）**4/4** 拿到 sso（Turnstile INTERACTIVE 点一次即 PASS、无 OTP）→ EXIT=0 建出 **158~161**，
服务腿 2/60 → 6/64。合进编排器后 `--dry-run` 已在两端实跑过；真跑合并版还没跑过，下次盯 `[password-login]` 那几行。

### 🔴 2026-10-19：**供应商说 heavy ≠ heavy** —— 判档以 grok.com `dominantPlan` + JWT `tier` 为准（已焊进脚本）

158~161 供应商报 heavy，实为 **SuperGrok（$30/月）**：

| 量具 | 已知 heavy 72/157 | 158~161 | 分得开？ |
|---|---|---|---|
| grok.com `/rest/subscriptions` `dominantPlan.plan` / `tier` | heavy | **`supergrok` / `SUBSCRIPTION_TIER_GROK_PRO`**，`SURFACE_JWT=GROK_PRO` | ✅ |
| access_token JWT `tier` | **5** | **1** | ✅ |
| productId / offer | `grok.ultra` | `grok.pro.monthly.30`（apple，winback 付1送2，offer id `sg_…`） | ✅ |
| x.ai chat 头 `limit-tokens/requests` | 53M/8300 | **53M/8300**（158 有一次快照 10M/480 = 老 supergrok 形状） | ⛔ 会翻 |
| sub2api `plan_from_45_responses` | heavy | **heavy** | ⛔ 看来是从上面的头推的（没读源码确认） |

⇒ **头和 `plan_from_45` 只能判活，不能判档**；下面 10-18 那条「三个量具互证」里的这两把作废。
已修 `xai_sso_device_oauth.py`：`subscription_of()` 带出 `dominantPlan.plan` / `tier` 枚举 / apple 的
`billingPeriodEnd`（apple 条目没有 `expiryTime`）/ `autoRenewOn` / offer；`tier_of()` **先认 plan 再认 productId**
（两者打架时用 plan 并告警），`PRODUCT_TO_TIER` 补 `grok.pro.monthly.30→supergrok`；建号前打一行
`TIER … jwt_tier=…`，订阅档与 JWT 档对不上就打 `🔴 TIER MISMATCH`。对照 `test_subscription_tier_pick.py` **17 case 全绿**
（含阴性对照：plan=heavy 仍读 heavy、未知 plan+SKU 留空）。158~161 库里已回填 `supergrok`（备份
`/home/cltx/grok-onboard/acct158-161-tier-before-20261019T095942Z.txt`，psql + admin API 两处回读）。
⚠️ `JWT_TIER` 只登记了见过的 1/5；`PLAN_TO_TIER` 里只有 `supergrok` 是见过的词。
⚠️ 198 宿主机直打 api.x.ai 会 connection reset ⇒ 实探头要从 **225** 打（token 从 198 用管道送过去，不落盘）。
⚠️ 池里老 supergrok 46~50 现在都 402，**没有活的 supergrok 阳性对照**；「tier=1=SuperGrok」靠的是 grok.com 自报。

### 🔴 2026-10-18：UI「平台/类型」那列显示 `grok.lite` = **建号时自己写错的快照**，不是真档位（已修）

acct 157 在 `http://10.68.13.198:31880/admin/accounts` 上显示 lite、别的号显示 heavy。
UI 渲染的是 `credentials.subscription_tier`，admin API 回包里确实是 `grok.lite`，
**这一列是建号时抓的快照、不参与调度**（157 当时正常收流量）。真档位是 heavy。

| 真因 | 数据 |
|---|---|
| `subscription_of()` 取 `/rest/subscriptions` **第一条**带 productId 的，不看 status | 157 名下三条：① `grok.lite` INACTIVE(10-12 过期) ② `grok.ultra` INACTIVE ③ **`grok.ultra` ACTIVE**(11-12) ⇒ 挑中 ① |
| 映射 `"supergrok_heavy" if tier=="grok.ultra" else tier` 对未知值**原样穿过** | `grok.lite` 不等于 `grok.ultra` ⇒ 商店商品名直接落进档位列 |
| 为什么以前没发现 | 156 及之前每个号名下**只有一条**订阅，第一条就是对的；157 是池里第一个带「历史订阅堆叠」的号 |

~~⇒ 判真档位三个量具互证：`plan_from_45_responses`、JWT `tier`、标称头 `53000000`/`8300`~~
⛔ **10-19 修正**：`plan_from_45` 和 53M/8300 头在 SuperGrok 号上同样读成 heavy（上一节），
判档只认 **grok.com `dominantPlan.plan` + JWT `tier`（heavy=5 / supergrok=1）**。157 这两项都指 heavy，结论不变。
`productId` 是商店 SKU（Google/Apple 各一套），只当次要依据；
`tier` 枚举要整串读：`SUBSCRIPTION_TIER_SUPER_GROK_PRO`（157，heavy）≠ `SUBSCRIPTION_TIER_GROK_PRO`（158~161，SuperGrok），只差 `SUPER_`。

**已修**（`xai_sso_device_oauth.py`）：`subscription_of()` 优先挑 `ACTIVE`（排除 `INACTIVE` 的子串误匹配），
无 ACTIVE 则回退首条并在 `picked` 里打 `first-of-N-none-active` + 告警；
新增 `PRODUCT_TO_TIER` 显式映射表 + `tier_of()`，**未知 productId 留空并告警，绝不原样穿过**
（空值看得出缺，错值看不出）。对照 `test_subscription_tier_pick.py` **11 case 全绿**，
含阴性对照：真 lite 号仍须读成 lite（case 3，防止把尺子改成「一律答 heavy」）、
未知商品留空（case 6）、`INACTIVE` 不许被当 `ACTIVE`（case 8）。
⚠️ 踩到的坑：`def tier_of(sub, log=log)` 这种**默认参数在 def 期绑定**，
测试换掉 `m.log` 收不到告警（两条 case 假红）⇒ 改成 `log or globals()["log"]` 调用期解析。

### 🔴 同一列上的**第二个**病（10-18 同日）：探测整发失败 ⇒ 档位列**空**，且会躲过回扫

🔴 **我第一版回扫查询是坏尺子**：它比对 `stored` vs 实探，**两边都空就算"相等"**，
于是「整列缺失」这一类被整类滤掉，我据此说了「只有 157 一行」——错的。
正确说法是「只有 157 一行**值写错**，另有两行**根本没写**」。
⇒ 回扫必须把空值显式折成一个哨兵值（`coalesce(nullif(x,''),'(EMPTY)')`）再比，
并单独跑一条「哪些 live 号档位为空」。同族：[[feedback_all_metrics_zero_at_once_is_the_ruler_not_the_fleet]]。

| | 病 | 列里的值 |
|---|---|---|
| acct 157 | 挑了第一条（过期 lite），不看 status | 写**错**成 `grok.lite` |
| acct 155/156 | `/rest/subscriptions` 整发失败 ⇒ `subscription_of()` 返 `None` ⇒ 调用方 `or ""` | **根本没写**（空） |

**155/156 的定因证据**（不是推测）：它们自己的 `admin.accounts.create` 请求体里写着
`"_probe":{"subscription":null,"xai_models":"200 usable (14 models)"}` —— 订阅探测是 null，
而同一轮 x.ai 判词 200 可用。查 `audit_logs`（⛔ 没有 `details` 列，是 `request_body`；
`\d audit_logs` 先看列名）。

**真档位怎么定的**（四把尺子全不可用时的出路）：155/156 当时 `plan_from_45` 无、
token 没有 `tier` claim、`/quota` 返 `source=billing_probe`+`snapshot=null`（**连已知 heavy 的 157 都读不出**，
记忆里记过这个坏形状）、直打 `/v1/chat/completions` 155 回 **402**、156 回 **429**。
⇒ 决定性证据来自库本身：**155/156 是 79/80 的重建版**（10-16 重建），
`credentials->>'sub'`（xAI userId）**逐字相等**（`832d9b04…` / `1d03a3e9…`），
而 79/80 在 10-14 那次建号探测出 `supergrok_heavy`。同一个 xAI 账号 ⇒ 同一档位。
⚠️ 只比邮箱同名不够（可能重名），必须比 `sub`。
⛔ `/v1/models` **不带** rate-limit 头，别拿它当档位尺子；要打 `/v1/chat/completions`。

**库里回扫**（空值折成哨兵后重跑）：157 写错 + 155/156 为空，三行已全部改成 `supergrok_heavy`
（157：where 钉 id+旧值+实探值，`UPDATE 1`；155/156：where 钉 `exists(同 sub 的前身号 = supergrok_heavy)`，
`UPDATE 2`；旧值先存 198 `/tmp/acct157-tier-before-<UTC>.txt`、`/tmp/acct155-156-tier-before-<UTC>.txt`），
postgres 与 admin API 两处回读一致。157 改后仍收流量；155/156 零流量**是 402/429 的既存状态**（改的是展示字段，不参与调度）。
收尾全池：**heavy 54 / `supergrok` 5 / plus 1，零空值**。
⚠️ 那 5 个 `supergrok` 是**实探自己回的词**、stored 与实探一致，不是写错的，别动。
⚠️ 剩 **acct 7** `supergrok_plus` vs 实探 heavy 是 09-08 就记过的另一回事
（[[feedback_grok_ratelimit_remaining_is_not_a_balance]]：credentials 陈旧后升级过，无数据分辨），不在这轮范围。

**也已修**：`subscription_of()` 对非 200 **重试 3 次**（CF 403 先 `rotate_warp()`），
始终不通就打 `🔴 … subscription_tier will be EMPTY; set it from the live plan probe after creating`，
不再静默留空。对照 case 10/11。

### 🔴 2026-10-18：`SSO DEAD: get-user http=403` + Cloudflare 拦截页 = **出口被挡，不是凭据死**（已焊进 `check_sso`）

加 acct 157 第一跑，抓取器一发就判 `SSO DEAD: get-user http=403 <!DOCTYPE html>…`，
一个号都没抓到。**三组对照把它翻了面：**

| 对照 | 结果 | 结论 |
|---|---|---|
| 225 WARP，真 sso ×3 | 403 `<title>Attention Required! \| Cloudflare</title>` | — |
| 225 WARP，**假 sso** ×3 | **同一个 403 页** | 回包与 cookie 无关 |
| 225 WARP，**完全不带 cookie** ×3 | **同一个 403 页** | ⇒ 是出口被 grok.com 的 CF 挡 |
| Mac 直连，真 sso ×2 | **200**，`userId` 与粘来的 grok id 逐字相等 | sso 本来就是活的 |
| Mac 直连，不带 cookie | **401** JSON `no-credentials` | 出口通时的形状 |

⇒ **判出口通不通的尺子：不带 cookie 打 `grok.com/rest/auth/get-user`，`401 JSON` = 通，`403 Cloudflare 页` = 挡。**
WARP 连转 5 次实测 `403/401/401/401/403` —— 封锁**跟着 IPv6 /64 段走、按次随机**，
同一把 sso 在不同出口上一会儿「死」一会儿活。⛔ 别拿这个 403 去要新凭据、也别改 jar。

🔴 两个 403 必须按**包体**分，不能按状态码分：
- `<title>Attention Required! | Cloudflare</title>` ⇒ 出口（本节）
- `<title>Request could not be verified</title>` ⇒ castle/csrf（下一节，要浏览器）
- JSON（`{"code":16,…}`）⇒ 真·grok.com 对 cookie 的判词，**一发即死，不许重试**

已改 `xai_sso_device_oauth.py`：`_cf_block()` 认 CF 拦截页，`_get_user_transient()` 把它算瞬时，
`check_sso()` 碰到它**先 `rotate_warp()`（`sudo -n warp-cli disconnect/connect`，换一段出口 IP）再重试**，
而不是在同一个被封的出口上干等 —— 封锁跟着 IP，原地重试永远不清。
对照 `test_xai_sso_check_retry.py` 现 **11 case 全绿**，含两条阴性对照：
JSON 403（case 6）和 401（case 5）仍必须一发判死（只测翻面会放过一个「从不拒绝任何号」的坏尺子）。

### 🔴 2026-10-13 起 approve 必须走浏览器（已焊进脚本，默认 `--approve browser`）

acct 75（`ziuojqpanh@outlook.com`）一轮踩出两个抓取器的病，**都已修**：

| 现象 | 真相 | 判据 | 现在脚本怎么做 |
|---|---|---|---|
| `SSO DEAD: get-user http=499 Timeout expired` | grok.com 自己超时，**不是 sso 死** | **假 sso 也回同一包 499**（阴性对照），225 上 6 发里第 5 发起 200 | `check_sso()` 对 499/5xx/curl 失败重试 6×10s；只有 401/403 等真答复才判 DEAD；一直不清的打 `SSO UNCHECKED … TRANSIENT`，⛔ 别据此要新凭据 |
| `device/approve http=403 <title>Request could not be verified</title>` 连 4 次 | consent 表单多了**空的 `castle_request_token`**（Castle 反机器人，只有页面 JS 会填），纯 HTTP 提交恒拒；页面正文 "We could not verify this browser. Allow scripts…" | jar 里 `__cf_bm`/`gb_anon_id` **都在** ⇒ 不是 09-28 那个 jar 没串；同 sso 换浏览器一把过 | `browser_approve()`：225 patchright 经同一个 WARP，种 `sso`/`sso-rw` 到 `.x.ai`，点 Continue→Allow，**只认落到 `/oauth2/device/done`**；consent 页见到空 castle 字段会先喊一句 |

⛔ 那条 403 的 `<title>` 仍是 `csrf` 那一类，脚本旧提示「jar 没带 cookie，换机器没用」**对一半**：
换机器确实没用，但调 jar 也没用 —— 要的是浏览器。看到 csrf 403 先看 consent 表单有没有空的 JS 字段。

🔴 **10-13 晚用 ~8 次失败换来的定律：⛔ 不要等 `castle_request_token` 填上再点 Allow。**
当天加第二个号（venetiaindy9343）时给 `browser_approve()` 加了「等 castle 字段 40s 再点」，
连挂 6+ 次（token 始终空 ⇒ 空 token 提交 ⇒ 403）；而**两次成功**（acct 75 14:32、acct 76 23:39）
用的都是同一版「按钮一出现（~4-8s、字段还空着）就点」的原版逻辑，都落到 `/device/done`。
干等 40 秒再点的形状更像机器人；Castle 的 token 明显是**搭提交的车**，不是按钟点发。
同一批失败还多盖了一层 `user_agent=UA` 覆盖 —— 两次成功都没盖（patchright 原生 UA），
两个都别再加。诊断岔路上的死胡同，别再走：40s 采样 token 恒空 ≠ 时机不对（不是早快点就行）；
WebGL/SwiftShader 修好照样挂；WARP 出口换 5 次全落同段 IPv6。（未完全隔离：成功那轮 WARP IP
也正好换过，A/B 没跑；下次真再挂 403，先重转 WARP 再重试，但**别把等 castle 加回来**。）

#### 🔴 2026-10-21 修正上面那句「先重转 WARP」：**先打阳性对照，再决定要不要转**

转 WARP 只在**出口真被挡**时有用，而出口有独立尺子。加 `xsohltwqbz@outlook.com`
连挂 3 次（种 sso ×2 + `--password-login` ×1 —— 后者**登录成功、拿到全新 sso**，
approve 照样 403，所以也不是"sso 是种进去的、浏览器没真登过"），四组数据：

| 量 | 结果 | 结论 |
|---|---|---|
| 不带 cookie 打 `grok.com/rest/auth/get-user` | **401 JSON** | 出口**通**（403 CF 页才是被挡） |
| `accounts.x.ai/sign-in` | 200 | 同上 |
| WARP 换段 `2a09:bac5:42f4…` → `2a09:bac1:3b00…` | 两跑都挂 | 换出口**无效** |
| **阳性对照：acct 163 的 sso，同出口 / 同代码 / 同样空 castle** | **一发落 `/device/done`** | ⇒ 出口、代码、空 castle **全都不是原因**，是**这个号**过不了 |

⇒ `castle_request_token` 在**成功和失败两边都是 `value_len=0`** ⇒ 这个字段**零判别力**，
别再围着它改。⛔ 别再转 WARP、别改 jar、别盖 UA。
→ [[feedback_castle_403_needs_a_positive_control_before_rotating_warp]]

#### ✅ 2026-10-22 解法：**换授权方式**，不是换出口 —— 已焊进脚本，默认自动兜底

这个号后来连**人在真实浏览器里点 Allow** 都 403 了两次（另有一次人点成功但我的脚本 bug 丢了 token）。
所以不是"自动化浏览器的指纹差"那么简单：**device flow 的 approve 端点对这个号就是过不去。**

真解：**不走 device flow，改走 Authorization Code + PKCE。** `auth.x.ai/oauth2/authorize`
落到的是**另一个** consent 页（`/oauth2/consent`，标题 "Authorize Grok Build"），
它的 Allow 按钮**不受 Castle 守**。点了之后跳 loopback `redirect_uri` 带 `?code=`，
拿 code + PKCE verifier 去 `/oauth2/token` 换 token。上游同病同解：grok2api issue #1078 / PR #1082
（PR 走纯协议 Next-Action，10-22 **仍未合并**；我们用浏览器点，不依赖会变的 actionID）。

```bash
# 默认就会自动兜底：device approve 撞 castle 403 ⇒ 当场切 authcode，不用加参数
python3 grok-warp-onboard.py ~/x/creds.txt
# 想直接跳过 device flow：
python3 grok-warp-onboard.py ~/x/creds.txt --approve authcode
```

10-22 用 `xsohltwqbz` 实测：`--approve authcode` 整条一把过（code 86 字符、state 对、
token 850 字符、`200 usable (14 models)`、`jwt_tier=5`），建出 **acct 164**。

🔴 这一节的三个坑（都在代码注释里）：
1. **必须浏览器，不能 curl**：`/oauth2/authorize` 只带 sso cookie 会被弹回 `/sign-in`，
   它还要 `cf_clearance`（只有浏览器拿得到）⇒ 先访问一次 `accounts.x.ai/account` 种上。
2. **`127.0.0.1:56121` 上没人监听**，跳过去的那次导航**本来就会失败**，这是正常的 ——
   code 从**请求 URL** 上读（`page.on("request")`），不是从响应里读。
3. **`state` 必须比对**，不一致就拒收 code。
对照 `test_authcode_fallback.py` **6 case 全绿**，含两条阴性对照：
非 castle 的失败**不许**兜底（case 2，防止兜底吞掉所有错误）、兜底本身失败要原样报出来（case 5）。

另：x.ai 那句 "Request could not be verified / could not verify this browser" 对**过期的设备码**
也会出（10-22 一个码隔了 7 小时才点）⇒ 先看码过没过期，再判是不是 Castle。

只读诊断脚本 `diag/consent_recon.py`（`CLICK_ALLOW=1` 才点 Allow，
打出 consent 页的 castle 字段长度 + 失败请求清单）。

#### 🔴 2026-10-22 两轮带仪表：**Castle 在 225 上根本不发请求** ⇒ 别再调浏览器

`diag/castle_diag.py` / `castle_diag2.py`（记 console、失败请求、castle 相关网络、
consent 页脚本清单 + castle 全局、60s 采样 token）量到：

| 量 | 结果 |
|---|---|
| consent 页 castle 配置 | `enable_castle: true`、`castlePk` 在，SDK **已打进页面** |
| `cdn.castle.io` 请求 | **一次都没发过**（两轮都没有） |
| castle 全局对象 / `castle_request_token` 60s | 没有 / **恒空** |
| 轮 1 console | 刷 `Automatic fallback to software WebGL has been deprecated` |
| 轮 2：加 `--enable-unsafe-swiftshader` 修好 WebGL + 点掉 OneTrust | **行为一点没变** |

⇒ 轮 1 很容易推成「WebGL 先失败 ⇒ Castle SDK 没起来」，**轮 2 把它证伪了**。
这是**指纹级拒绝**（数据中心出口 + Xvfb 浏览器），不是哪个开关没开 ⇒
⛔ 别调浏览器参数 / 别等 castle 字段 / 别盖 UA / 别转 WARP，**直接让脚本兜底到 authcode**。
真要人工过一次：`diag/mac_approve.py`（本机真 Chrome，`ssh -L 40000:127.0.0.1:40000 cltx@10.68.13.225 -N -f`
借 225 的 WARP 出口；只换 approve 那跳 + api.x.ai 传输，其余检查全复用 `xai_sso_device_oauth.py`）。
→ [[feedback_castle_never_fires_on_225_is_fingerprint_not_webgl]]

编排器在 225 上**自起私有 Xvfb**（`:80~:89` 里第一个空的，跑完 kill；⛔ 别碰 `:91/:95~:99`，
那是别的抓取任务的），`PYTHONPATH=/Data/grok-onboard/pyenv/...` +
`PLAYWRIGHT_BROWSERS_PATH=/Data/grok-onboard/browsers`。截图落 `bundles/shots-<tag>/`，
随临时目录一起 shred。`--approve http` 保留旧四跳，只用于诊断表单是否又变了。
对照：`test_xai_sso_check_retry.py`（9 case：499×4→200 绿 / 401·403 一发即死不重试 /
499 永不清=TRANSIENT / castle 空字段喊、填了不喊）+ 真机阴性对照（假 sso 在 225 一发 401 判死）。
⚠️ 浏览器路径 10-13 晚已随「别等 castle」的修正回到制胜版。✅ **10-14 acct 78 编排器整条真跑首次端到端一把过**
（browser approve 一把过 → bundle OK → create → regress EXIT=0）；acct 75/76 当时都是「独立脚本抓 + 手工建号」。
加号盯 `[browser] -> clicked Allow` 和 `after: …/device/done` 两行。

实跑：09-29 **68/69 一把过**（当时手搓 7 步，脚本是事后泛化的）。
**10-13 acct 75**：编排器 6 次全挂在抓取（2×499 误判 + 4×castle 403），手跑浏览器 approve
→ `create-grok-oauth-account.py` 建出 75，真服务腿 **4/54 → 5/55**，75 首个 15min 窗口
22 req / picked 24 / first-pick 1；四条 `sa-grok-*` nonce 全对；窗口内 upstream 错误在
27/29/30 且 30 的首个早于建号 ⇒ 既存。
**10-13 晚 acct 76**（venetiaindy9343@hotmail.com，grok.ultra **2026-10-16 04:20Z 到期**）：
编排器两轮挂在 castle-wait 上（上面那条定律的由来），改跑 14:32 制胜版独立脚本一把过
（点 Allow → `/device/done` → token 818/86 → `200 usable (14 models)`）→ 手工建号
**acct 76**（group 7 / prio 1 / conc 200，mapping 1080B `matches_template=t`），
真服务腿 **4/55 → 5/56**，76 首个 15min 窗口 2 req / picked 24 / first-pick 1（SERVING），
窗口内 fail=0；upstream 错误在 27/29/30 且首个（23:34:08）早于建号（23:41:32）⇒ 既存。
**10-14 acct 78**（htuwkjdckr@outlook.com，x.subscription.ultra **2026-11-13 17:43Z 到期**）：
编排器 `grok-warp-onboard.py` **一把过**（dry-run → 真跑 EXIT=0），浏览器 approve 一发成功
（clicked Continue → clicked Allow → `/device/done`），token 818/86 → `200 usable (14 models)`
→ acct 78（group 7 / prio 1 / conc 200，mapping 1080B `matches_template=t`），
加号时整池 **0/56 outage**（全部 routing 503），78 上线后独扛全部流量，
真服务腿 **0/56 → 1/57**，78 首个 15min 窗口 246 req / picked 1 / first-pick 1；
四条 `sa-grok-*` D2 nonce 全对；upstream 错误 5 条在 acct 78 属正常噪声。
**10-14 acct 79/80**（wtjhlryulce@outlook.com / qzxouodwwrl@outlook.com，x.subscription.ultra **2026-11-13** 到期）：
编排器 `grok-warp-onboard.py` **两个号都一把过**（browser approve 各一发成功），
token 818/86 → `200 usable (14 models)` → acct 79/80（group 7 / prio 1 / conc 200，
mapping 1080B `matches_template=t`），加号时整池 1/57（既存 routing outage），
真服务腿 **1/57 → 3/59**；79 窗口 18 req picked 1 / 80 窗口 15 req picked 1 first-pick 1；
D2 四条 nonce 全对；EXIT=0。**编排器首次两号批量端到端一把过。**
**10-18 acct 157**（ShanonCassinxceaa@hotmail.com，`grok.ultra` / `SUPER_GROK_PRO` **ACTIVE，2026-11-12 07:13Z 到期**；
同号另有两条 INACTIVE 的 lite/ultra 旧订阅，读档位要挑 `SUBSCRIPTION_STATUS_ACTIVE` 那条）：
第一跑挂在上面那条 CF-403 尺子病上（0 个 bundle），修完 `check_sso` 后**第二跑一把过**，
`EXIT=0`、mapping 1080B `matches_template=t`、group 7 / prio 1。
真服务腿 **1/59 → 2/60**（基线既存 routing outage 233 条），
157 首个 15min 窗口 **50 req**、D2 四条 `sa-grok-*` nonce 全对、window 内 every-leg-422=0；
`phase=upstream acct=156` 那 1 条（23:54:51）早于建号（23:56:13）⇒ 既存。
🔴 **粘来的 11 段长行要按 SHORT 形状落盘**：表头是
`账号----邮箱密码----辅邮----辅邮密码----Cursor密码----Token----手机号----取件URL----grok sso----grok id`，
而 `parse_creds` 对 ≥8 段的行按**旧 LONG 布局**取第 8 段当 sso、第 7 段当 userid ——
照原样喂进去 sso 会取到取件 URL。⇒ 自己切出 `email----pw----<grok sso>` 三段再喂。

**10-01 这个编排器第一次完整真跑**（70/71）：`--dry-run` 绿 → 真跑建出 **acct 70**，
腿 49→50、真服务腿 **7/49 → 8/50**，70 在 15 分钟窗口 41 req；四条 `sa-grok-*` nonce 全对；
用户面 16 分钟 457 ok / 2 fail；错误面首个时间戳 10:11:58 **早于** 建号 10:20:33 ⇒ 既存。
`EXIT=1` 来自抓取判 BAD 的那一个（见下一条），**建号与回归本身都绿**。

🔴 **10-01 发现的尺子病：抓取器的 x.ai 判据原来只打一发，瞬时 403 会把好号判死。**
`ekobqjdskw@outlook.com` 被判 `403 permission-denied` 丢掉，**同一把 token、同一台 225、
3 分钟后连打 3 发全是 200（13 models）**，手工用同一个 `create-grok-oauth-account.py`
建成 **acct 71**（group 7、mapping 1080B 与模板 acct 61 逐字节相等）。
⇒ `xai_models_verdict()` 已改成**非 200 重试 3 次（间隔 20s），只有每次都非 200 才判死**，
绿但曾翻面的会在 `_probe` 里留 `[flapped: …]` 痕迹。对照 8 case 在
`scripts/grok-onboard/test_xai_verdict_retry.py`（**含阴性对照：3×403 仍必须被拒** ——
只测翻面会放过一个"从不拒绝任何号"的坏尺子）。
⛔ `403 permission-denied` **不是** skill 里记的 `403 personal-team-blocked:spending-limit`
（那条才是余额死）；它这次的成因没查，只知道重试即消失，**别拿它当余额判据**。
🔴 判断一个被抓取器判 BAD 的号要不要重来：**直接拿 bundle 里的 token 再打几发
`api.x.ai/v1/models`**，别信那一发。bundle 即使 verdict 非 200 也会写盘（带探针），
但编排器 finally 会 shred 临时目录 ⇒ 要留先 `cp` 到另一个 mktemp 目录。

🔴 **建号成功 ≠ 加了长期容量**：09-22~09-28 加的 52~67 **全部**已 `spending-limit`
（`rescue --dry-run` 09-29 实测 43/49 条 PAY）。首日流量下每号约 **25~30h** 烧完
（56~67 实测 25.4~30.0h、每号 ~90~145M tok），52~55 较早一批撑了 159~168h。
⇒ 汇报时说「新增 N 条、预计 1~2 天内耗尽」，别说「池子修好了」。
收尾基线看 `health`「N of M legs served」，**不是**腿总数。

### C0. ~~首选~~（⛔ 09-25 起 pod 出口恒 403，见 §C1b；仅作历史/端点参考）：sub2api 自带的 SSO→OAuth

09-08 实测：sub2api 有原生端点，**根本不用起 Playwright、不用 225 WARP**（沿 sub2api
pod 自己的出口打 auth.x.ai，实测通）。**09-25 起这条路不通**，现行路径见上面 ⚡⚡。

| 端点 | 用途 |
|---|---|
| `POST /api/v1/admin/grok/oauth/sso-token` `{"sso_token":"<eyJ...>"}` | **只换 token 不建号**，用来先验：返 `data.subscription_tier` / `sub` / `team_id` |
| `POST /api/v1/admin/grok/sso-to-oauth` | 换 token **并建号**，`{"sso_tokens":[...],"name":"<email>","group_ids":[7],"credentials":{"base_url":"https://api.x.ai/v1"},"concurrency":200,"priority":1,"rate_multiplier":1,"auto_pause_on_expired":true}`；返 `data.created[]` / `data.failed[]` |
| `GET /api/v1/admin/grok/accounts/{id}/quota` | **per-account 实探**（绕开池子）。⚠️ 只有 `source=hybrid_probe` 的号才有活的 x.ai rate-limit 头；6/7/8/9 返 `source=billing_probe` + `snapshot=null` + `headers_observed=false`，**读出来一排 None，和死腿长得一模一样**（09-09 实测，它们当时正扛几百个真请求）。**09-17 这把尺子又换了个坏形状：老 7 条全返 502 `GROK_QUOTA_TOKEN_UNAVAILABLE` / `oauth refresh account state changed`，新号 27/28 返 200 满读数** —— 502 同样只说明尺子没读数，判死活只认 `usage_logs`。档位在 `snapshot.plan_from_45_responses`，**不在** `plan` |

🛠 **别再手搓：`scripts/grok-onboard/sub2api-grok-onboard.py`**（09-09 固化，
09-17 加 `health` 子命令 + first-pick 列 + `regress` 按 D2 定退出码；
六个子命令都在 198 实跑验过）

### ⚡ 09-21 起：加号只要一条命令 —— **在自己 Mac 上，只传路径**

⛔ **09-25 起 `onboard` 在第 2 步 `verify` 就中止**（SSO 端点恒 403）⇒ 加号改用上面 ⚡⚡ 的 `grok-warp-onboard.py`。本节的 `health`/`regress`/`rescue`/`reauth` 子命令照旧有效；⚠️ 从 Mac 跑任何子命令都会覆盖 198 常驻副本（巡检用的那份），09-29 起 `delegate()` 在 sha 变化时会打印 `⚠️ replacing …`。

```bash
cd <repo>/scripts/grok-onboard
python3 sub2api-grok-onboard.py ~/Downloads/grok-mima/卡密导出.txt
```

就这一行，**不用 ssh 上去、不用本地 sudo、不用先捞密码、不用 `S2A_PW_FILE=`、不用写子命令**：

- **不在 198 上时脚本自己 ssh 过去**：判据是 `/etc/rancher/k3s/k3s.yaml` 在不在
  （⛔ **不能用 `which kubectl` 当尺子** —— Mac 上也装了，两边都答"是"，
  于是脚本会留在本地跑、死在第一条 `kubectl -n litellm-dev`）。
  它把**脚本 + `sub2api_admin.py` + 凭据文件**一起 scp 到 `/home/cltx/grok-onboard/`，
  凭据那份 0600、**跑完无论成败都 shred**，远端退出码原样带回来。
  ⚠️ 远端用 `sudo -n`（永不弹密码提示）：`ssh -n` 下的提示会永远挂住，
  形状看起来像"脚本在一个会写库的步骤中间卡死了"。
  `S2A_HOST` / `S2A_REMOTE_DIR` 可改目标；`S2A_LOCAL=1` 强制本地跑。
- ⚠️ **本地那些 `print` 必须 `flush=True`**：stdout 非 tty 时是块缓冲，而 ssh
  直接写继承来的 fd ⇒ 不 flush 的话"正在发往 198"这行会打在远端输出**后面**，
  读起来像是先跑完才决定去哪跑。

- 密码脚本自己从 `litellm-dev/sub2api-secrets` 的 `ADMIN_PASSWORD` 取，落 0600 临时文件，
  `atexit` 删。⚠️ **显式的 `S2A_PW_FILE` 仍然优先**，所以每分钟的 park 巡检
  （自带 `/run/.s2apw-patrol`）行为不变、也不会多一次 kubectl。
  🔴 以前漏掉这个环境变量，是在 helper 的 **import 期** `assert _st == 200` 炸，
  报成**登录失败**，看不出是变量丢了 —— 会去查密码、查端点，方向全错。
- 第一个参数是**存在的文件**时隐含 `onboard`；子命令写错仍走 argparse 报错，不会被当路径。

也可以显式写：`... onboard .creds.txt --minutes 20`。

**它按 skill 规定的顺序把五步一次跑完**：`health` 基线 → `verify`（不建号）→ `add`
→ **只对新腿**打 x.ai 判词 → `regress`。退出码 = 「任一新腿不是 `200 usable`」∨
「D2 丢 nonce」；**既存腿余额死不算红**（那是基线不是回归，同
`sub2api-upgrade.py` 的 delta 判法）。09-21 两次实跑 `EXIT=0`。

🔑 **第 4 步是这条命令存在的理由**：没有它，一轮会以「已添加 2 个账号」收尾，
而实际新增可用容量为 **0** —— 09-21 池子 25 条腿里 19 条正是
`200 + tier=supergrok_heavy` 换出来、被 x.ai 回 `spending-limit`。
它只打**新建的那几条**，不重报早就死了的腿、也不浪费每腿一发真请求。

**凭据文件现在吃两种形状**（09-21 起）：

| 形状 | 样子 | 第 7 段 |
|---|---|---|
| LONG（表格导出，8+ 段） | `email----mailpw----pw----CURSOR_tok----phone----sms----grok_userid----sso` | 有,`sub==userid` 是**独立**交叉校验 |
| SHORT（聊天里粘的 / 卡密导出，2~7 段） | `email----mailpw----sso` | 无 ⇒ 自动去 `sso-token` 端点取 `sub` 补上 |

🔴 **SHORT 行的 `sub == userid` 是同义反复,不是交叉校验** —— 答案来自被校验的同一个端点。
脚本会把它打成 `tautological (short line)` 而不是一个说谎的绿 `True`。
要独立校验就得用 LONG 行。末段一律按 JWT（`eyJ` 开头）校验，不像就**大声退出**不静默跳过。

### 🔴 真实「卡密导出.txt」与聊天里粘的那种**不是一个格式**，三处全是硬失败

09-21 拿 `~/Downloads/grok-mima/卡密导出.txt` 对过，一个都不能少：

| 坑 | 不处理会怎样 |
|---|---|
| **UTF-8 BOM** | 第一个字段变成 `'﻿<email>'`，账号名带不可见前缀进库 |
| 抬头行 `卡密导出` + 空行 | 老版本直接 `sys.exit`，整个文件用不了 |
| 🔴 **分隔符是「五个」短横不是四个** | 按字面 `"----"` 切，第 5 个短横粘在后面每个字段头上：密码变 `-N5...`、sso 变 `-eyJ...`。**sso 那个被 `eyJ` 检查抓到，密码那个会静默存错** |

⇒ 脚本已改成 `utf-8-sig` 读 + 按 `-{4,}` 正则切一整段短横 + 跳过无分隔符的行。
**但带 `@` 的行永不静默跳过**（分隔符坏掉的账号行必须报出来，不能当抬头吞掉）；
同名邮箱重复直接拒。离线 6 例回归：真导出 / 旧 4 横粘贴 / CRLF+BOM+注释 /
坏分隔符邮箱行拒 / 重复拒 / 非 JWT 拒。

⚠️ 脚本**不删凭据文件**（故意的，它可能还要重跑）；收尾自己 `shred -u`。

⚠️ **198 上有常驻副本 `/home/cltx/grok-onboard/`**（09-17 那句「没有常驻副本」已过期；
`/root` 和 `/Data/grok-onboard` 至今确实没有）。`sub2api_admin.py` 必须和它放在一起 ——
脚本用 `exec` 引它，缺了直接崩。sha256 应与 repo 逐字相等（当前 `c0ac7479…`）；
⚠️ 这份**正被每分钟的 park 巡检 cron 调用**（`rescue --minutes 5`），
覆盖它就等于同时换掉巡检跑的代码，改完必须立刻核 `tail /var/log/grok-park-patrol.log`，
或者直接按巡检的原样跑一次 `rescue --minutes 5 --dry-run` 看退出码。

🔴 **中断一条已经开始跑的 `ssh ... add`，远端会照样把号建完。** 09-21 实例：
被中断的 `add` 在 18:09:15/18:09:23 建出 acct 44/45，重跑只看到
`SKIP already an account`，而中断前的 `health` 基线里没有这两个邮箱 ——
**形状与「池子有第二只手」一模一样**，我据此误判过一次「`sso-token` 会建号」。
判建号是谁干的只认 `audit_logs.action`：`admin.grok.sso_to_oauth.create` 才是建号，
`admin.grok.oauth.sso_token.create` 只是换 token。
⛔ `audit_logs` **没有 `resource_type` 列**（列名先 `\d audit_logs`），
且 `actor_email` 全是 `admin@sub2api.local`，区分不了人。


```bash
# 0. 传脚本（两个都要）+ 凭据，700/600
ssh cltx@10.68.13.198 'mkdir -p ~/grok-onboard && chmod 700 ~/grok-onboard'
scp sub2api-grok-onboard.py sub2api_admin.py cltx@10.68.13.198:~/grok-onboard/
scp .creds.txt cltx@10.68.13.198:~/grok-onboard/ && ssh ... 'chmod 600 ~/grok-onboard/.creds.txt'

# 1. 密码进 root-only 文件（每 session 一次）
sudo kubectl -n litellm-dev get secret sub2api-secrets \
  -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d | sudo tee /run/.s2apw >/dev/null
sudo chmod 600 /run/.s2apw

# 2~7（全部带 sudo S2A_PW_FILE=/run/.s2apw）
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py health          # 基线，先跑
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py health --xai    # 顺带问 x.ai：token 死 or 余额死（每腿一个真请求）
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py parse  .creds.txt  # 零网络
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py verify .creds.txt  # 验 sso，不建号
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py add    .creds.txt --concurrency 200
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py reauth .creds.txt   # 号已存在、只换 token 时走这条（§C5）
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py quota
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py regress --minutes 60
sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py health          # 收尾再比一次
# 凭据文件自己删，脚本不碰它；本地 /tmp 那份也要删
```

⚠️ **`S2A_PW_FILE` 必须显式带上**：`sudo` 不继承环境变量，漏了它 `sub2api_admin.py`
的模块级 `assert _st == 200` 会在 import 期就炸，报的是登录失败、看不出是环境变量丢了。

### 三个 SQL/时区坑（09-17 现场踩的）

1. **`psql -c` 里的字符串字面量一律 `$$...$$`。** 走 `ssh → kubectl exec → sh` 三层，
   `\x27` 会原样落到 SQL 里报 `syntax error at or near "\"`。
   ⚠️ **`string_agg(x, ',')` 的分隔符也是字面量**，写 `","` 会被当列名报
   `column "," does not exist`，必须 `$$,$$`。
2. **两个库时区不同。** `litellm-db-0` 是 `Etc/UTC`（所以 `+ interval '8 hours'` 对），
   **sub2api 的 postgres 已经是 `Asia/Shanghai`** —— 对它的 `created_at` 再 +8h
   会造出一个 8 小时后的未来时刻。我就是这么把 10:57 建的号读成 18:57，
   差点据此推翻"故障早于建号"这个正确结论。
3. **`kubectl get pod -o jsonpath='{.items[0]...}'` 会选到 `Completed` 的僵尸 pod**，
   这个 ns 里 postgres/sub2api 各有 2~3 个历史 pod。必须带
   `--field-selector=status.phase=Running`（脚本已固化）。
4. ⛔ **临时写的轮询循环禁 `2>/dev/null`。** 09-17 我写了个等新腿上量的轮询，
   SQL 是 `SELECT account_id||$$:$$||count(*) … GROUP BY 1`，postgres 报
   `aggregate functions are not allowed in GROUP BY`，被 `2>/dev/null` 吞掉，
   三轮全读成"零流量"——而同一刻直查是有行的。**假红，方向与真相相反。**
   要拼这种字符串得套子查询：`SELECT string_agg(account_id||$$:$$||c, $$ $$) FROM
   (SELECT account_id, count(*) c FROM … GROUP BY 1 ORDER BY 1) t;`
   → [[feedback_helper_2devnull_hides_the_error_and_fakes_success]]
   ⚠️ 同一条规矩栽第二次：`health` 读 failover 日志那段最初写的是
   `logs … 2>/dev/null | grep upstream_failover_switching || true`，
   pod 选不中时会读成"没有 failover 事件"⇒ 假绿。**09-17 已改成不吞 stderr、
   在 Python 里过滤**，命令真失败就大声退出。

它把下面三条⚠️ 全固化了（含 tier 改从 DB 读、无活探的行显式打 `NO LIVE PROBE`、
`--field-selector=status.phase=Running` 防选到 Succeeded 的 postgres pod）。

⚠️ **`concurrency` 别照旧文档写 1**：线上 6/7/8/9 是 199、09-09 新增的 18/19/20 是 200。
但 account 那道闸**不是**用户报"并发超限"时该动的闸，见 skill `sub2api-concurrency-gate`。

⚠️ **`sso-to-oauth` 返回的 `data.created[]` 里没有 `id` 字段（09-09 实测是 `[null]`）**
—— 它给不了自己的证据。建完必须回读 postgres：
`SELECT id,name,platform,status,schedulable,concurrency FROM accounts WHERE platform='grok';`
+ `SELECT * FROM account_groups WHERE account_id IN (...)` 确认绑到 group 7。

📋 **凭据行是 `----` 分隔的 8 段，第 4 段是诱饵**：
`email----邮箱密码串----密码----cursor_session_token----手机号----收码URL----grok_userid(uuid)----grok_sso(JWT)`。
**要的是第 7、8 段**；第 4 段是 **Cursor** 的 token（`iss=authentication.cursor.sh`），
拿它去换 grok 必失败。免费交叉校验：`sso-token` 端点返的 `data.sub` 必须与第 7 段逐字相等。

档位的**权威自报**是 grok.com `/rest/subscriptions` 的 `dominantPlan.plan` + access_token JWT 里的 `tier`
（heavy=5 / supergrok=1）。⛔ `plan_from_45_responses` 10-19 被证伪（SuperGrok 号也读 heavy，见 §C 10-19）。
⚠️ **`53000000`/`8300` 这两个数不能分档**：acct-7 的 `credentials.subscription_tier` 是
`supergrok_plus`，头里照样是 53000000/8300（也可能是它 credentials 陈旧、后来升级了，
我没数据分辨）。所以见到这两个数只能说"没对不上"，不能说"是 heavy"。
但**量具用上面那个 quota 端点**，别在 198 宿主机上打 api.x.ai —— 宿主出口对 x.ai
恒 `SSL: UNEXPECTED_EOF`，得到的红是宿主的事不是账号的事。
`data.credentials.sub` 应与手里的 grok userid 逐字相等，是一条免费的对错交叉校验。

⚠️ 用模块方式引 helper 时是 `read().rsplit("if __name__",1)[0]` ——
docstring 里也有 `if __name__` 字面量，用 `split(...)[0]` 会把文件切在 docstring 中间。

### C1. 抓 OAuth token（只有邮箱密码时）—— 只能走 225 的 WARP

x.ai 对机房出口硬封（"Blocked due to abusive traffic patterns"）。阿里云节点 EIP
实测被拒，**别再在阿里云起 capture Job**；而且在共享 NAT 上跑浏览器会污染线上
codex acct 的生产出口 IP。唯一可用出口 = **225 上 WARP socks5**（出去是 Cloudflare Osaka）。

```bash
# host 225，照抄 mail135 原版，只换 email / pw 文件 / DISPLAY / 输出 / TAG
/Data/grok-onboard/run_warp_capture.sh            # mail135 原版（参照，勿改）
/Data/grok-onboard/run_warp_capture_mail125.sh    # 09-04 克隆版
```

要点：独立 Xvfb display、`PROXY="socks5://127.0.0.1:40000"`、
`PYTHONPATH=/Data/grok-onboard/pyenv/...`、`PLAYWRIGHT_BROWSERS_PATH=/Data/grok-onboard/browsers`、
密码走 `XAI_PW_FILE`（chmod 600，base64 传输，**不进 argv**）。
捕获器输出 `OAUTH_CAPTURED` / `OAUTH_FAILED` / `STOP: <reason>`。

🔑 **密码是完整整串。** 表里写 `vmendoza1808@mail.com Mail-135-h7auMEre`，密码就是
`Mail-135-h7auMEre`（17 字节），**不是后缀** `h7auMEre`。按后缀试直接 BADPW。
判据：线上能用的 `.xpw` 正好 17 字节。详见 [[reference_xai_oauth_capture_via_warp_225]]。

### C2. 先验 token，再建 account

```bash
curl -s https://api.x.ai/v1/models -H "Authorization: Bearer $TOK" | jq '.data|length'   # 期望 12
# 看响应头确认档位
curl -sD- -o/dev/null https://api.x.ai/v1/chat/completions -H "Authorization: Bearer $TOK" ... \
  | grep -i x-ratelimit-limit
```
supergrok_heavy 档**观测到**的是 `x-ratelimit-limit-tokens: 53000000`、
`x-ratelimit-limit-requests: 8300` —— 但见 §C0 那条⚠️：这两个数**分不出 heavy / plus**，
只能当"没对不上"用，判档看 `subscription_tier` / JWT `tier`。

⚠️ **`x-ratelimit-remaining-tokens` 不是余额表，别拿它答"还剩多少"。** 09-08 实测：
acct-6 一小时内真烧了 424,416 tokens（40 请求）、acct-7 烧了 754,726（36 请求），
两个号在**流量之后**取的快照 remaining 仍是 `53000000` 整数，一位没掉；
响应头里**没有任何 reset / window 字段**，x.ai 不告诉你窗口多长。
所以 53M 是个"标称上限"，用量要查 `usage_logs` 的 `input_tokens+output_tokens`。
`extra.grok_billing_snapshot` 那个 `period_type=weekly`（+ 自然月 `billing_period_*`）
是 grok.com **订阅计费**窗口，和这个 rate-limit 窗口是两码事，别混。

### 🔴 C1b. 2026-09-25：`sso-token` / `sso-to-oauth` 整条 SSO 路对**任何** token 都返 403

那天两个新号无论怎么试都是
`xAI SSO conversion failed: approve xAI device code: xAI OAuth HTTP 403` /
`GROK_SSO_UPSTREAM_FAILED`，3 次独立运行同形。**别据此判用户给的 token 坏**：

**阳性对照**（同一分钟、同一个 pod、token 从 0600 文件读、不进 argv）：拿 acct 61
在 09-23 **成功换过 token** 的那一行 SSO 重打 —— **一样 403**。
⇒ 403 不可归因于新 token。⚠️ 对照本身已 stale 2 天，所以它**也不证明**这条路坏了。

旁证：`audit_logs` 里 `admin.grok.oauth.sso_token.create` 从 09-08 到 09-23 23:00 共
157 次**全 200**，403 只出现在 09-25；最后一次成功 `sso_to_oauth` 是 09-23 23:48。
sub2api 09-24 14:27 切到 v0.2.8 是**候选**原因，但升级后到 09-25 之间**零次 SSO 尝试**
⇒ 零观测，**不是结论**；`sub2api-upgrade.py check` 说 0.2.8 已是 hub 最新，没有上游修复可等。

#### ✅ 2026-09-28 定论：**根因是 `accounts.x.ai` 按出口 IP 挡机房**，与 sub2api / v0.2.8 无关

- **假设**：403 是 Cloudflare 按 egress 挡 `accounts.x.ai`，不是凭据坏、也不是 v0.2.8 回归。
- **证伪条件**：若是凭据坏，**换出口也该 403**；若是 sub2api/v0.2.8，`auth.x.ai` 同样该坏。
- **数据**（同一个 sso、同一分钟）：
  | 出口 | `accounts.x.ai/api/auth/session` | `auth.x.ai/oauth2/device/code` |
  |---|---|---|
  | sub2api pod `172.235.204.67` | **403 + Cloudflare HTML** | **200** |
  | 225 WARP `104.28.238.115` | **200 `{"status":"authenticated"}`** | 200 |
  两个变量只差出口 ⇒ v0.2.8 那条"候选原因"**作废**，别再写进下一轮诊断。
  附带：`grok.com/rest/auth/get-user` 从 198 宿主机直打也 200（机房 IP 不挡它）。

⇒ **凡要过 `accounts.x.ai` 的步骤（`/oauth2/device`、`/verify`、`/consent`、`/approve`）
只能从 225 WARP 出**；`auth.x.ai` / `api.x.ai` / `grok.com/rest/*` 哪儿都行。

#### 🔴 HTML 403 有两种，**只能靠 `<title>` 分**，混了会把 cookie 问题当成出口问题

| `<title>` | 病 | 解 |
|---|---|---|
| `Request could not be verified` | CSRF / cookie 没带全（含 `__cf_bm`） | 全程串一个 cookie jar |
| Cloudflare 拦截页 | 出口不对 | 换 225 WARP |

09-28 先撞的是**前者**：每跳只发 `sso` 头，`/oauth2/device`、`/verify`、`/consent`
下发的 cookie 一个都没回传给 `/approve` ⇒ 4 个号齐刷刷 403，形状和出口被挡一模一样。
修法四件套（缺一个就还是 403）：**每号一个 0600 cookie jar 全程 `-b`/`-c`**、
先 GET `accounts.x.ai/oauth2/device?user_code=` **落地种 jar**、POST 带
`Referer`=上一页 + `Origin: https://accounts.x.ai`、报错时把 `<title>` 抠出来。
修完 `device/approve http=303`，4/4 拿到 token。

**绕法 = 走 §C1 的 225 WARP 设备码抓取，再用新脚本直接写库**（09-25 两个号实跑通）：

```bash
# 225：克隆 runner，只换 DISPLAY/邮箱/.xpw/TAG/OUTPUT，跑出 grok_oauth-<tag>.json
# 198：把 payload 传过来后建号（自己回读 accounts + account_groups）
python3 create-grok-oauth-account.py .newacct-<tag>.json ...
```

#### ⚡ 手里有 `sso` 时的首选：`xai_sso_device_oauth.py`（09-28 新增，4/4 一把过）

~~不用 Playwright、不用 DISPLAY~~ ⛔ **10-13 起 approve 那一跳必须浏览器**（castle_request_token，
见 §C 顶部 10-13 那张表）；其余跳仍是纯 HTTP，`sso` cookie 本身就是登录态，不碰 Turnstile。
`scripts/grok-onboard/xai_sso_device_oauth.py`（已进 git；⚠️ 225 `/tmp/` 下那份是旧版，别直接用，`grok-warp-onboard.py` 每次现传 repo 版、自起 Xvfb）。
单跑时（一般别单跑，用编排器）：

```bash
# 在 225 上（必须，见上面出口表）；要 Xvfb + 225 pyenv
Xvfb :80 -screen 0 1440x1000x24 -nolisten tcp & XP=$!
DISPLAY=:80 PYTHONPATH=/Data/grok-onboard/pyenv/local/lib/python3.14/dist-packages \
  PLAYWRIGHT_BROWSERS_PATH=/Data/grok-onboard/browsers \
  python3 xai_sso_device_oauth.py --creds <0600 creds> --out-dir <mktemp>/b   # --approve browser 是默认
kill $XP
# 逐号打印 [browser] … device authorized / token ok / x.ai verdict，输出 bundle-<局部>.json（0600）
```

每号自带三道对照，**任何一道不过就别往库里写**：
1. `grok.com/rest/auth/get-user` 回的 email **必须等于**那行的 email（防串号）。
2. `grok.com/rest/subscriptions` 的 `productId` —— `grok.ultra` 才是 heavy。
3. 拿**刚换到的**新 token 打 `api.x.ai/v1/models`：`200 usable (13 models)` 才算活。
   这一步专门把"token 坏"和"xAI 余额烧光"分开 —— 后者是
   `403 personal-team-blocked:spending-limit`，只有充钱能修，建号无用。

bundle 的字段形状就是 `create-grok-oauth-account.py` 直接吃的那套
（`group_ids:[7]`、`concurrency:200`、`priority:1`、`subscription_tier` 由 `grok.ultra`
翻成 `supergrok_heavy`），所以 225→198 传完直接建号，中间不用手改。

#### ✅ 09-28 起这几条**已焊进代码**，不再靠人记（配 3 个脚本一起看）

| 坑 | 焊在哪 | 形状 |
|---|---|---|
| 两种 HTML 403 混判 | `xai_sso_device_oauth.py` `html_403_kind()` + `_die_on_html_403()` | 按 `<title>` 返 `csrf`/`egress`/`unknown`，四个 hop 全走它；**认不出的第三种明说 unknown，不硬塞进两类** |
| `fs.protected_regular` | `create-grok-oauth-account.py` | 密码文件默认落**脚本同目录**，不再进 `/tmp` |
| 拿 acct 18 当模板 | 同上，写库**前**断言 | `jsonb_typeof='object'` 且 `length>0`，否则直接 exit 并提示 `MODEL_MAP_FROM=<id>` |
| 以为 POST 200 就带上了 mapping | 同上，写库**后**回读 | 逐腿比 `length(...)==模板长度`，不等即 exit 1；group 行数也数 |
| SSO 403 被读成"用户凭据坏了" | `sub2api-grok-onboard.py` `_sso_403_note()`（`verify` 和 `add` 都挂） | 打印阳性对照 + WARP 绕法命令，并明写 **⛔ 别怪 v0.2.8、别先去要新凭据** |

`html_403_kind()` 配了 6 个 case 的对照（两种真形状 + JSON 403 + 未知 title + 200 阴性对照），
模板守卫 5 个 case，**都在真库上核过**（acct 18 = `absent|0` 触发，61 = `object|1080` 放行）。
⚠️ 改这几处后**必须重跑这两组对照**——它们是唯一能说明"尺子自己没坏"的东西。

🔴 建完仍**不等于**在服务：脚本结尾那句提示写死了下一步（`regress --minutes 15` → `health`
→ 跟**动手前**那个「N of M legs actually served traffic」比），因为 `schedulable=t` 是坏尺子。

`scripts/grok-onboard/create-grok-oauth-account.py`：拒同名重复、**从健康腿抄
`model_mapping`**（缺它新腿认不出 grok-4.6 这些名）、POST、psql 回读两张表、不打印 token。

🔴 **它默认把 admin 密码写 `/tmp/.s2apw-create`，198 上会 `Permission denied` —— 不是 sudo 没生效。**
198 有 `fs.protected_regular = 2`：root 在 sticky 目录（`/tmp`）里用 `O_CREAT`
打开**别人（cltx）已存在**的文件会被内核拒。阳性对照：同一条 root shell 写一个
**新** `/tmp` 文件成功 ⇒ 排除"权限/sudo 坏"这个方向。绕法是**别碰那个旧文件**：

```bash
cd /home/cltx/grok-onboard
sudo -n sh -c 'umask 077; kubectl -n litellm-dev get secret sub2api-secrets \
  -o jsonpath={.data.ADMIN_PASSWORD} | base64 -d > ./.pw'
sudo -n env MODEL_MAP_FROM=61 S2A_PW_FILE=$PWD/.pw python3 create-grok-oauth-account.py bundle-*.json
# 完事 shred -u ./.pw
```

模板腿别随手挑：**acct 18 没有 `model_mapping`**，用它当模板等于给新腿留一个静默盲区。
判据是 `jsonb_typeof(credentials->'model_mapping')='object'`
（⛔ 不是 `jsonb_array_length`，它是 object，会 `cannot get array length of a non-array`）。
09-28 复核：8 / 19 / 61 / 62 / 63 都是 1080 字节 object，默认的 **61 可用**。

🔴 **`xai_device_capture.py` 的 `NEEDS_OTP` 判据栽过两次假红，两次原因不同：**

1. 拿散文当锚点（`"enter the code"`）——**设备码页自己就写着**
   "Enter the code shown in your terminal"，于是**登录成功**被判成要 OTP。
   而且 `bt` 是在 `dump()` 之后重读的，dump 打出来的文本里根本没有那句
   ⇒ **日志里的证据和判据读的不是同一个页面状态**，这种错只能靠截图定性。
2. 改成找 `input[autocomplete='one-time-code']` **照样假红** —— 设备码页那 8 个
   `user_code` 格子用的就是这个 autocomplete。

⇒ 现在只认**正面证据**：`logged_in()` 查 `Sign out` 按钮 / URL 落到 `/oauth2/device`；
只有仍停在 `sign-in` 页且有可见 code 输入框才算真 OTP。
配套还修了 x.ai 登录后的 cookie-chain 重定向：`settle()` 等跳转停、`dump()` 整体
try 包住（观测代码不许终止抓取）、登录后**必须显式再开一次 `verification_uri_complete`**
（它把你甩到 accounts.x.ai 账号页，不会自己回授权页 ⇒ 不重开则 Approve 永远点不到、
`poll_token` 恒 `authorization_pending`）。

### C3. 建 account + 绑 group，回读

`POST /api/v1/admin/accounts`（platform=grok, oauth），然后绑 group 7，
**GET 回读 `account_groups` 确认出现 `<acct>|7|1` 那一行**。

⛔ **下面这段 10-19 已被推翻**：设备码 token 的 JWT `tier` **能**判档（heavy=5、SuperGrok=1，有 grok.com 自报对上），
`plan_from_45` 反而**不能**（SuperGrok 号也读 heavy）。只有 SSO 换来的 token 没有这个 claim，那时才退回 grok.com 订阅接口。

~~📐 建完判档别用 JWT 的 `tier` 数字。~~ 设备码流程的 access_token 有 `tier: 5`，
而池子里 SSO 换来的 token **根本没有 `tier` 这个 claim**（acct 54/59/61 实测全 None）
⇒ 手里没有 5→`supergrok_heavy` 的映射表，照数字填就是编。
**真尺子是 x.ai 自己回的** `extra.grok_usage_snapshot.plan_from_45_responses`：
打一次 `GET /admin/grok/accounts/{id}/quota` 就落库，09-25 两条新腿都读到
`supergrok_heavy` + 53M tokens + `headers_observed=true`，坐实了先前那个假设。

### C4. 建完 5~10 分钟内新腿零 `usage_logs` —— 先看 `switch_count`，别急着判它坏

09-17 第二批（29/30/31）建完 10 分钟仍是零 `usage_logs`，而同天第一批（27/28）
只隔 **7~9 秒**就吃到流量。差别不是号的质量，是**当时池子饿不饿**：
第一批建号时池子空转吐 503，新腿一上线立刻被首选；第二批建号时 19/27/28 正常服务，
新腿排在后面。历史区间：池子健康时 26 秒 ~ 2 分钟（18/19/20），本次约 **4 分半**。

**判据是日志里 `openai.upstream_failover_switching` 的 `switch_count`。**
🛠 `health` 和 `regress` 都已自带 first-pick 列，正常情况下**别手搓**：

```
  acct 29   LarryWhitepgd@outlook.com   0 req / last 15min  (picked 12, first-pick 0)
                    ↑ 零 usage_logs      ↑ 但被选过 12 次，全是备胎 ⇒ 良性
```

三种零流量必须分开读（脚本会把这三句打出来）：

| first-pick | picked | 含义 |
|---|---|---|
| **>0** | >0 | **被首选过还是零行 ⇒ 真故障**，查这条腿 |
| **0** | >0 | 只当备胎 ⇒ 零行是**预期**，等下一轮 |
| 0 | **0** | 压根没被派过活 —— 和"窗口本来就静"长得**一模一样**，换个忙窗口重跑 |

要手查时（脚本挂了/要看别的字段）：

```bash
sudo kubectl -n litellm-dev logs \
  $(sudo kubectl -n litellm-dev get pod -l app=sub2api \
    --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}') \
  --since=5m > /tmp/s2a.log            # ⛔ 别加 2>/dev/null，见 §C0 坑 4
grep upstream_failover_switching /tmp/s2a.log | grep -c '"switch_count": 1'
grep upstream_failover_switching /tmp/s2a.log | grep -o '"account_id": [0-9]*' | sort | uniq -c
```

⚠️ **别写一条横跨三个 key 的正则**（旧版脚本那样 `"account_id": … "upstream_status": … "switch_count": …`）：
emitter 换个 key 顺序或空格就静默匹配 0 条，读出来是"没有 failover 事件"= 假绿。
脚本现在每个 key 各自 `re.search`，且 `upstream_status` / `upstream_status_code`
两种拼法都收（日志用前者、`ops_error_logs` 用后者）。

09-17 实测：29/30/31 的 switch_count **全部 ≥2（12/12/11 次）、零次 =1**，
而 27 有 20 次 =1。⇒ 新腿只在别人失败后作为**备胎**被调度，而备胎接手的是
**同一个已经失败的请求**，必然一起失败，所以 `usage_logs`（只记成功）为零。
**零 usage_logs 在这个形状下不是"腿坏了"，是"还没轮到它当首选"。**

📐 **配套的一条尺子差异**：access log 的 `status_code: 200` 比 `usage_logs` 行**先出现**
（DeferredService `BatchUpdateLastUsed` 批量落库）。本次 29 在 access log 已有 200 时
`usage_logs` 还是空的。要"最快知道新腿活了"就看 access log，要"可复现的判据"用 `usage_logs`。

⚠️ 同一窗口另有一种**与新腿无关**的失败，别混进来：43 个请求里**每条腿都返 422**，
链尾是 `openai.account_select_failed` +
`no available Grok accounts supporting model: grok-4.6 (pool=6, filtered: excluded=6)` → 502。
这些请求 `body_bytes` 达 **7.7MB / 700KB**，是请求本身被 x.ai 拒，加号不修它。
判别法：**同一个 `request_id` 的整条链**都 422 才算（`regress` 已按 request_id 成链，
见 §D1）。⛔ 别退化成"窗口内每条腿都出现过 422"—— 那个条件在真流量下恒真，
09-17 实测 1478 成功 / 3 失败的健康窗口照样满足它。

### C5. 🔑 给**已存在**的腿换新 token —— `reauth`，不是 `add`、更不是 `sso-to-oauth`

🔴 **先读这一句：用户说"重新认证下"时，九成要跑的是 `rescue`（本文件最前面 §⚡），不是这一节。**
`reauth` 重写凭据，而停掉的腿通常凭据是好的（探针 `200 usable`），被 sub2api 自己 park 了 ——
09-20 三次全池 503，三次都是这个形状，reauth 对它是纯 no-op。
**跑 `reauth` 的唯一前提：手里有新的凭据行，并且探针说存的 token 真的死了。**

🔴 **2026-09-21 全量复现：拿飞书表 21 行**最新** SSO 对全池 reauth，救回 0 条。**
21 行 `verify` 全 `200 + sub==userid:True`（SSO 确实新鲜），但每一条的**新 token 直打
x.ai 仍是 `403 personal-team-blocked:spending-limit`** ⇒ 23 条腿里 17 条是余额死，
收尾 `sched=t` 只剩 19/30/31/38/39/43。⚠️ `tier` 在这里**没有判别力**：报
`supergrok_heavy` 的（8/9/18/20/27/28/29/40）和报 `tier=None` 的（33~37/41/42）
吃的是同一个 403。表里 `Grok UserID` 列缺值时用 DB `credentials->>'sub'` 补第 7 段
（acct 8 两边逐字相等，验过这个补法）。**下次遇到同样形状别再跑一遍 reauth，直接报"只能充钱"。**
⚠️ 判"这条腿还活着"别只看 `health` 的 SERVING 列：40/41/42 在 17:30~17:34 之间
逐个被打上 `rate_limited_at` 停产，而 30min 窗口里它们仍显示几百 req —— 要配
`select account_id,max(created_at) from usage_logs` 一起读。
详见 [[project_sub2api_grok_pool_credit_death_2026_09_21]]。

2026-09-18 实测。号早就在池子里、只是 token 过期时，前面两条路都不能用：

- `add` 会看到同名号直接跳过；
- `POST /grok/sso-to-oauth` 带 `update_existing: true` **不原地更新**，它**新建了一个
  同名号**（id=32）并把原号 18 完全没动 —— 这是个真会污染池子的坑，收尾必须
  `PUT {"schedulable":false}` + `DELETE /api/v1/admin/accounts/{新id}`（注意是**软删**，
  见下面 `deleted_at`）。

🛠 现在是脚本子命令：`sudo S2A_PW_FILE=/run/.s2apw python3 sub2api-grok-onboard.py reauth .creds.txt`

它跑的顺序（每一步都是因为"显然的那步"是 no-op 才存在的）：

1. `POST /grok/oauth/sso-token` 换 token，并**断言 `data.sub == 凭据第 7 段**（防止把
   别人的 token 写进这条腿）；
2. **拿新 token 直打 `https://api.x.ai/v1/models`** —— 见下面那条红线；
3. `PUT /accounts/{id} {"credentials": 合并后的}`，`_token_version` 自己顶成新的毫秒
   时间戳（不顶，reloader 侧可能不认为变了）；
4. `POST /accounts/{id}/reset-quota` —— 清 `rate_limited_at` / `rate_limit_reset_at` 的
   就是这一个端点；
5. `POST /accounts/{id}/clear-error`（**2026-09-20 补的第 5 步**）—— 第 4 步**不碰**
   `temp_unschedulable_until`，少这一步的话凭据换新了腿仍然是 park 状态、503 一条不少；
6. `POST /accounts/bulk-update {"account_ids":[id],"schedulable":true}`；
7. 逐字段回读，**park 那一列从 postgres 读**（admin GET 不暴露它）。

#### 🔴 这一节的三条硬红线（都是当天被咬出来的）

**① 判"该不该 reauth"的唯一尺子 = 拿新 token 直打 x.ai，看 body。**
`403 {"code":"personal-team-blocked:spending-limit"}` = **xAI 那边余额/订阅没了，
重新授权是纯 no-op，只有充钱能修**。而这个号的 SSO 换 token 一样返 **200 且
`subscription_tier=supergrok_heavy`** —— ⛔ **token 里的档位不是余额的证据**。
09-18 七个号里 4 个是这个形状（18 / 20 / 27 / 28），3 个真是 token 过期（19 / 29 / 30）。
从 sub2api 内部看这两类**长得完全一样**（`schedulable=f`、`quota` 端点 502、零
`usage_logs`），只有 x.ai 的 body 能分开。
⚠️ 必须在 **198 host** 上打：pod 里只有 BusyBox `wget`，它**吐不出 403 的 body**，
而整个判据就在 body 里。脚本的 `health --xai` / `reauth` 都已内建这一步。

**② `PUT /accounts/{id} {"schedulable": true}` 返 200 但静默丢弃这个字段**（对着
postgres 验过，值还是 `f`）。能写进去的是
`POST /api/v1/admin/accounts/bulk-update {"account_ids":[...],"schedulable":true}`
（返 `{"success":N}`）。⛔ 顺带记：`{"updates":{...}}` 那种形状返 400 "No updates provided"。

**③ admin API 在 GET 时把 `access_token`/`refresh_token`/`id_token` 全掩成空串**，
所以"写完对比 token 有没有变"永远得到 **False**，这是个**假红**。判写没写进去只能看
`credentials.expires_at` / `_token_version`，或者直接查 postgres 取真长度。

#### `deleted_at`：sub2api 是软删

`DELETE /accounts/{id}` 之后 API 返 404，但**表里那行还在**，只是 `deleted_at` 有值。
所以**所有 `FROM accounts` 的查询都必须带 `AND deleted_at IS NULL`**，否则：
`health` 会把已删的号列成一条腿、`add` 会因为"名字已存在"跳过一个其实已经不存在的号。
脚本里五处查询 09-18 已全部补上。回滚点：把 `deleted_at` 清成 NULL 就能把号捞回来。

### C6. 🔑 换 x.ai **登录密码** —— 🛠 `grok-pw-rotate.py`（2026-10-21 新增）

用户说「改下这个号的密码」时先分清是哪一个：飞书 grok 账号表里
**`Grok密码` 列 = x.ai 登录密码**（本节），`邮箱密码` 列才是 Outlook 的
（34 行里只有 1 行有值，基本没在维护）。粘来的 `email----Gx!…----eyJ…`
中间那段就是 `Grok密码` 这一列的东西 —— 同一批号 23 行都是这个形状。

```bash
cd <repo>/scripts/grok-onboard
python3 grok-pw-rotate.py --creds ~/x/one.txt --dry-run   # 开弹窗 dump，不写表不提交
python3 grok-pw-rotate.py --creds ~/x/one.txt             # 生成→写表→改→两发探针
rm -P ~/x/one.txt
```

**顺序是硬的：先把新密码写进飞书表（逐字回读 3/3 相等），再去改。**
改密码不幂等，先改后写中途挂一下 = 新密码只活在一个已经退出的进程里，号变砖。
脚本里 `COL_NEW="新Grok密码"` 这一列 10-21 新建；没有这一列时自己补，
有就复用，`Grok密码` 列**保留旧值**不动（输入列留旧、产出列写新，
同 [[feedback_bitable_input_col_keeps_old_value]]）。
两发探针都过后（10-23 起）顺手调 `grok_bitable.fill_ids()` 补空的 `Sub2API账户ID`/`Grok UserID`
（165 那次是手补的）；这个号还没进 sub2api 时只喊不写。

#### 🔴 这一节的四条，前两条是「会点错东西」的 UI 坑

**① `accounts.x.ai/account#security` 上有三个 `Change` 按钮**（改名 / 改邮箱 / 改密码）。
`button:has-text('Change')` 取第一个 ⇒ 开出来是 **Edit name** 弹窗，截图一看就知道点错了。
判据：从 `Manage the password` 那段文字往上找**只含一个** Change 按钮的最小祖先
（往上找太高会把前两个也圈进来）。已焊进 `xai_change_pw.py: password_change_button()`。

**② OneTrust cookie 横幅本身是 `[role=dialog]`** ⇒ `query_selector("[role=dialog]")`
抓到它，于是「可见 password input = 0」，形状像「x.ai 改版了 / 这个号不让改密码」。
真因只是选错了元素。判据：挑 dialog 要排掉 `privacy preference`，并断言弹窗文字含
`password`。弹窗真身：`[data-testid=oldPassword]` + `[data-testid=password]` +
`button[type=submit]:has-text('Save password')`，**无 OTP、无二次验证**。

**③ 回验必须两发，少一发等于没验：**
新密码探针 `state=LOGGED_IN sso_cookie=True` + **旧密码探针 `state=BADPW`（阴性对照）**。
只测新密码能登会放过「其实没改成、两把都能登」这一类
（同族 [[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]]）。
工具 `xai_pw_probe.py MODE=new|old`，全新浏览器、不带 sso，所以对正在服务的腿零影响。

**④ 🔴 x.ai 改密码不吊销 sso / OAuth refresh_token** —— 10-21 acct 163 实测：
改完旧 sso 打 `get-user` **3/3 200**、sub2api `accounts action 163 refresh` 正常
（`_token_version` 顶新、0 条 401/403）、腿改前改后都在收流量。
⇒ **不需要停腿、不需要先 reauth、不需要挑低峰**。
我改前判断过「改完 token 可能全失效、而 163 是唯一在服务的腿」——
这个担心实测不成立，别再据此拖着不改。⚠️ 这条只对 x.ai 成立；
Kiro 那边反过来（改密码立刻 `TooManyRefreshFailures`，
[[project_kiro_cred_rotation_sop_2026_10_19]]），别互相套。

⚠️ `xai_device_capture.py` **import 期**就 `os.makedirs("/work/ss-xai")` ⇒
cltx 用户下 PermissionError，形状像「脚本坏了」。任何复用它的脚本都必须先给
`SCREENSHOT_DIR` / `OAUTH_OUTPUT`（`grok-pw-rotate.py` 的远端命令里已带）。

## D. 回归（两层，缺一不可）

**两层都在 `sub2api-grok-onboard.py regress` 里**，下面是它跑的东西。

⛔ **判"用户有没有受影响"永远别用 sub2api 的 `ops_error_logs`**：那张表数的是
**上游尝试**，换腿重试在到达用户前就把失败吞掉了。09-09 它一小时 480 条 x.ai 422，
同小时用户面（`LiteLLM_SpendLogs`）是 **941 成功 / 1 失败 / 117 个 key / 25.9s**，
根本没有异常 —— 我据此报过一次假事故。用户面要同时看
`count(distinct api_key)`（没人用的零失败是假绿）和平均耗时（重试的代价跑在耗时上）。
→ [[feedback_retry_layer_error_count_is_not_user_failure_rate]]

⛔ **回归窗口里出现的失败，先证明它是不是本来就有的。** 加号动作到 `regress` 只隔几分钟，
一段**既存**故障必然落在同一个窗口里，形状和"我把它搞坏了"一模一样。三段式：
拿 `ops_error_logs` 该相位的 `min(created_at)` 与 `accounts.created_at` 比
（**sub2api 那库已是北京时间，别再 +8h**）—— 早于建号就是既存。
09-17 实测失败起于 10:39、建号在 10:57，早 18 分钟，且**建号那一分钟失败就塌了**，
方向相反。`regress` 现在自己把这两个时刻打出来。

**D1. sub2api 层 —— 必须证明新腿真的被选中**，不是"都成功"就算数
（[[feedback_failover_drill_must_prove_bad_lane_was_actually_picked]]）：

```bash
# 用生产 key 打 10 发，然后查调度分布
psql -U sub2api -c "SELECT account_id,count(*) FROM usage_logs
  WHERE created_at > now() - interval '15 min'
    AND account_id IN (<grok 的 id 列表>) GROUP BY 1 ORDER BY 1;"
```
⚠️ **`account_id IN (…)` 不能省**：同一台 sub2api 上还有 kimi（group 8/9）和
antigravity 的行，不过滤会把别的平台的 account_id 混进这张表读成"grok 的腿"。
脚本已按 `accounts.platform='grok'` 自动拼这个列表。

期望每条腿都有非零计数（09-04 实测 `6|5`、`7|8`）。**零计数不许直接判故障** ——
先读 first-pick 列分上面 §C4 那三种。脚本的收尾断言现在写的是
「every grok leg in the accounts table took real traffic」，不是旧的
"all schedulable legs…"（那句话在查询早就不按 `schedulable` 过滤之后还留着，
属 [[feedback_hardcoded_log_string_reports_stale_truth]] 那个形状）。

🔀 **全腿 422 的混淆项脚本也判了，但判据是「按 `request_id` 成链」不是「按窗口」**：

```
  failover chains in window: 52; chains where EVERY tried leg returned 422: 41
        06e6c225-5c6c-4a4e-9b63-7397b0b07db3  legs=28,27,19
```

⛔ **我第一版写成「窗口内每条腿都出现过 422」，当场就是假红**：09-17 13:40 实测
六条腿在 30 分钟里各自都收过 422，而同窗用户面是 **1478 成功 / 3 失败**。
真流量下每条腿都会捡到零星 422，这个条件恒真。**必须同一个 `request_id` 的整条
failover 链全 422** 才是"请求被拒"那个形状（日志行里就带 `request_id`）。
→ [[feedback_threshold_must_be_falsified_against_data_at_hand]]

⚠️ **`picked=0` 配非零流量不是矛盾**：首选就成功的腿压根不产生 failover 行，
脚本会标 `<-- served on first pick, never had to fail over`。

🚦 **`regress` 的退出码只由 D2 决定**（D2 探针任一 entry 没回 nonce ⇒ exit 1），
D1 的空闲腿**不参与**退出码：空闲天生有三义（§C4），
用一个有歧义的信号去卡门禁只会制造假红。→ [[feedback_deploy_must_gate_on_test_exit_code]]

**D2. LiteLLM 层 —— 用户面真正走的路**（643 把 cursor-* key 都打这里）：

```bash
ssh cltx@10.68.13.198 'MK=$(sudo kubectl -n litellm-product get secret litellm-secrets \
  -o jsonpath="{.data.LITELLM_MASTER_KEY}" | base64 -d)
for m in sa-grok-4.5 sa-grok-4.6 sa-grok-4.20; do ... done'
```
litellm 容器里**没有 curl**，用 `python3 -c` + urllib 打 `127.0.0.1:4000`。
三条 entry 都要 200 才算完（09-04 三条全 `PONG`）。

⚠️ 动 litellm-proxy **禁 `kubectl apply`**（manifest 陈旧会回退 image + 内嵌 CM），
只用 `set image` / `patch` —— [[feedback_manifest_prod_drift_apply_overwrites]]。

## E. ~~路 A（grok-proxy）502 —— OAuth 续期~~ ⛔ 2026-09-08 路 A 已彻底删除

**这一节整节作废，作为档案保留，不要执行。** 用户 09-08 的原话是"彻底删除"，
`grok-proxy` 的 deploy/svc/secret 和 LiteLLM 的 4 条 entry 都已清掉。
再走下面的续期流程 = 复活一条被判死刑的路。

### E0. 删除做了什么（可回滚点）

| 动作 | 判据 |
|---|---|
| CM `litellm-config` merge-patch | `model_list` 84→80，`grok-proxy` 字面量 0 次，另外 3 个 js 键字节数不变 |
| 逐 pod `/v1/models` | 4/4 pod `total=278`，路 A 残留 0，B/C/D 齐全 |
| 删 k8s 对象 | `deploy/grok-proxy` `svc/grok-proxy` `secret/grok-proxy-auth` `secret/grok-proxy-secret` |
| 备份 | `/tmp/llcfg-bak/grokproxy-*.20260908-212931.yaml`（4 份，已验 yaml 可解析、kind/name 正确）+ `config.20260908-212931.yaml` sha256 `bc7887d0…6223f93` |

**动手前必查的一步**（否则删了 CM 也删不干净）：路 A 那 4 个名字**只在 CM，不在 DB**。
判据是按**明文 `model_name`** 查 `LiteLLM_ProxyModelTable`（638 行）返 0 行 ——
⛔ **不能用 `litellm_params::text ILIKE '%grok%'`**，那一列是**加密**的，
返 0 行什么都证明不了（09-08 我就先这么误报了一次）。

```sql
SELECT model_name FROM "LiteLLM_ProxyModelTable" WHERE model_name ILIKE '%grok%';
-- 09-08 实测只有 cursor-grok-4.5/4.6、openrouter-grok-4.5、sa-grok-4.5/4.6/4.20
```

### E1. 原续期流程（历史档案）

`grok-proxy` deploy 曾在 `litellm-product` ns、跑在 225 节点，token 来自
**secret `grok-proxy-auth`** 的 `.grok_oauth.json`（挂 `/data/.oauth-proxy/`，
env `GROK_AUTH_FILE`）。它把上游失败一律翻成 `502 {"message":"API 异常 (req: …)"}`，
uvicorn access log 只有状态码看不出原因 —— **必须进 pod 验凭据**：

```bash
sudo kubectl -n litellm-product exec $POD -- python3 -c '
import json,urllib.request,urllib.error,urllib.parse
d=json.load(open("/data/.oauth-proxy/.grok_oauth.json"))
# ① 网络通不通（不带凭据，401/405 都算通）
# ② access_token 直打 api.x.ai/v1/models
# ③ refresh_token 换新 auth.x.ai/oauth2/token
'
```

09-04 实测三段结果，照此对号入座：

| 现象 | 读法 |
|---|---|
| 不带凭据打 `api.x.ai` 得 401、`auth.x.ai` 得 405 | **网络通**，别去查防火墙/出口封锁 |
| access_token → `403 unauthenticated:bad-credentials` | token 过期（`expires_in` 仅 **21600s=6h**） |
| refresh_token → `400 invalid_grant Invalid or unknown refresh token` | **refresh 链已断，proxy 自己救不回来**，只能重抓 |

⚠️ `RESTARTS` 高（09-04 是 174）不是根因线索，proxy 崩溃重启不影响这个判断。
⚠️ 在 **198 宿主机**上打 `auth.x.ai` 会得 `SSL: UNEXPECTED_EOF` —— 那是宿主机出口的事，
**不能拿它推断 pod 网络**（pod 里实测是通的）。判据必须取自 grok-proxy 所在的 pod。

~~**修 = 重抓一份 bundle（§C1 走 225 WARP）→ 写回 secret `grok-proxy-auth` → 重启 deploy。**~~
（路 A 已删，secret 已不存在，此修法无对象。）

🟢 **09-08 起这条副作用风险随路 A 一起消失了**（原文保留供理解 OAuth 轮换机制）：
~~同一个订阅号在 A（grok-proxy secret）和 B（sub2api DB）各存一份 token。OAuth refresh
通常**轮换** refresh_token，一方成功 refresh 可能作废另一方那份~~ —— 这曾是 A 的 refresh
链为何会断的候选解释之一（**始终没有数据坐实**，现在也不会有了）。
现在 `vmendoza1808@mail.com` 的 token 只有 sub2api 一个持有者，**不再存在双持轮换互踩**。
新增订阅号时若又要给某条新路单独持 token，这个坑会原样回来 —— 届时用不同订阅号。

## F. `sa-grok-4.6` 已是 cursor gpt 系的 fallback 一层（09-08 起）

198 `router_settings.fallbacks` 里 **19 条** entry 带 `sa-grok-4.6`：17 条 cursor 系
gpt 组插在两个 `deepseek-*-responses` **之前**，`chatgpt-gpt-6-astra` / `gpt-6-astra`
两条**追加在末尾**（它俩链里本来就没有 deepseek，只有 `chatgpt-gpt-5.6-sol`）。
zerokey-pool-\* / claude-gpt-\* / claude-zerokey-\* / openrouter-\* / ua-split-\* 那
**20 条同样含 deepseek 的 entry 是有意不动的**，别顺手补齐。

⇒ **sub2api 挂掉不再只影响 `sa-*` 三条窄面**：cursor 主力组（`gpt-5.6-sol` 单组
680 req/15min）的降级路上现在有 grok 一层。排查 cursor 降级异常时要把 sub2api
的内部余额闸（§A/§B）也算进嫌疑人。

改法照 skill `litellm-router-settings-update`：`jsonb_set` 单键直写 + 确认 `UPDATE 1`
+ 回读逐字段比对（顶层 13 key 不变、非目标键逐字节相同）+ `rollout restart`
+ 逐 pod `GET /router/settings`。**禁 `/config/update`**（会把 `model_group_alias` 清空）。

⚠️ `sa-grok-4.6` 注册的是 `mode: chat`，而 cursor 流量 83% 是 `aresponses`/`responses`。
09-08 实测 **LiteLLM 会自己翻译**：打 `/v1/responses` 拿到 `status=completed` +
`output_text` 原样回显 nonce。所以 mode 不匹配**不构成**空转 —— 但这条是实测出来的，
换别的组当 fallback 前要重测一遍，别默认成立。

⚠️ 「真的被 fallback 打中」目前**没有实证**：mock 触发不了 fallback（见
`litellm-router-settings-update` §5.3），只能等主组自然进 cooldown。
配置层已坐实（4/4 pod 加载），命中层是待观测。

## G. 升级 sub2api 本体 —— 🛠 `sub2api-upgrade.py`，别再手搓

### 🔴🔴 2026-10-22 起生产带一个**本地补丁**，升级时必须重打，否则 grok 每几分钟整池 503

| | |
|---|---|
| 病 | x.ai multi-agent 限 **每号每模型每秒 2 个**（429 `resource-exhausted` … `Requests per Second (actual/limit): 2/2`）。原版 sub2api 当普通限流 ⇒ **冻整个号 2min、所有模型一起停**；单腿时整池 503。10-22 下午 5h 断 25 次、13700 请求没接住 |
| 补丁 | `grok_upstream_failure.go`：`isGrokModelCapacityText` 加认 `requests per second` ⇒ 走 capacity 分支（同号 0.5s 重试 1 次、不冻号）。每分钟/token 限流照旧冻号（阴性对照测试守着） |
| 源 | 🔴 **以 repo `scripts/grok-onboard/patches/sub2api-grok-per-second-429-as-capacity.patch` 为准**（sha256 `56995d09…`，2 文件 57 行，**0 migration**）。`~/codes/sub2api` 那个分支上**零提交**，补丁只是工作区未提交改动（10-24 才发现），别指望 cherry-pick |
| 线上镜像 | `127.0.0.1:5000/sub2api:20261024-v0.2.18-ps429b`（10-24 16:01 切；= 0.2.17 prod 镜像为 base，换 `/app/sub2api` + `/app/resources`，label 改 0.2.18）。前一版 `20261022-v0.2.17-ps429b` |
| 回滚 | `kubectl -n litellm-dev set image deploy/sub2api sub2api=127.0.0.1:5000/sub2api:20261022-v0.2.17-ps429b`（0.2.18 只加迁移 246=把 gemini/antigravity 分组生图开关置 true，旧版不读这列的语义差，回退不用 pg_restore；dump `/root/sub2api-backup/sub2api-20261024-1534.dump`） |
| 闸门 | `sub2api-upgrade.py cutover` 读目标镜像二进制里有没有 `LOCAL_PATCH_MARKERS`，**没有就拒绝**；上游修了才用 `--drop-local-patches` |

**升级 = 一条 `go`，补丁由脚本重打**（10-24 把手跑的七步收进脚本 `build` / `patch-regress`，同日在 198 实跑 `build` 一遍：约 12min，补丁标记 present、自报 0.2.18）：

```bash
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py go      # check → probe pre → backup → build → [窗口闸] → cutover → verify → probe post → patch-regress
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py build --version 0.2.19   # 单跑：只出镜像，不碰线上
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py patch-regress            # 单跑：专打补丁那条
```

`build` 做的事（每步失败都停，不会出半成品）：
① **上游修了没**：拉新 tag 下补丁动过的那个文件，查 marker 字面量（`requests per second`）；在 ⇒ 停下让人读上游改法，读不到 ⇒ 判 UNKNOWN 不判「已修」；
② 198 直接 `codeload.github.com` 下源码包（2s，不用经 Mac 传）→ `patch -p1 --forward` 打 `PATCHES` 列表里每个补丁；
③ 去 BuildKit 语法存 `Dockerfile.198`（注释行不算；有不认识的语法就停）；
④ golang 容器跑每个补丁登记的 `go test -tags=unit -run …`（模块缓存 `/Data/sub2api-gomod-cache`）；
⑤ classic build（~10min，日志 `/Data/sub2api-build-<ver>/build.log`）；
⑥ 以**现役镜像**为 base 换 `/app/sub2api` + `/app/resources`、改 label；顺手比 `docker-entrypoint.sh`，上游改了会喊；
⑦ push + k3s 拉得到 + 二进制 `--version` / label / 补丁标记三样都对上才给 `next: cutover --image …`。
**新增补丁** = patch 文件放 `scripts/grok-onboard/patches/` + 在脚本 `PATCHES` 加一行（file / marker / test）——`cutover` 闸门的 `LOCAL_PATCH_MARKERS` 由它派生，不用另改。

`patch-regress` 判据与边界（10-24 第二次实跑踩出来的）：
- 先数**能服务的 grok 腿**（active+schedulable+没在冷却），**<3 条默认跳过**：它是打在生产池上的真流量。10-24 16:30 只剩 156/165 两条腿时跑了一次，5/5 成功，但同一分钟 165 被冻 2min、真实调用方 sa-grok 丢了 9 条；
- 那次冻 165 的是 **`grok-4.5` 的 429**（不是压测的 multi-agent），而 `ops_error_logs` 只存 sub2api 改写过的 `Upstream rate limit exceeded`，**x.ai 原始 body 不落库** ⇒ 是每秒（补丁该接）还是每分钟/额度（本就该冻）**从库里判不了**，脚本把这类 429 行列出来、标 `not the burst model`，FAIL 文案写明「不是对补丁的判决」；
- 没抽到 429 ⇒ INCONCLUSIVE（补丁那条分支根本没跑），不算 PASS。

🔴 `pull`（拉上游原版镜像）在 198 上**推不进 registry**：`does not provide any platform`，加 `--platform linux/amd64` 也一样（containerd 同一病）。生产带补丁后它只在 `--drop-local-patches` 时才有用，`go` 已不走它。
🔴 本机给 198 跑多行脚本别用 `ssh -n … <<EOF`：`-n` 把 stdin 接到 /dev/null，heredoc 整段被吞，**rc=1 且零输出**（10-24 repack 脚本一行没跑）⇒ scp 脚本过去再 `sudo -n bash`。

**上线后怎么判修好了**：sub2api 日志出现 `openai.pool_mode_same_account_retry … upstream_status 429 … retry_delay 500`
（= 走了 capacity 同号重试），且同一分钟 `ops_error_logs` routing no_leg = 0。

198 上常驻 `/Data/sub2api-ops/sub2api-upgrade.py`（源在 repo
`scripts/grok-onboard/sub2api-upgrade.py`，两边 sha256 应逐字相等，改完必同步）。
09-08（v0.1.179→v0.2.3）、09-10（v0.2.3→v0.2.4）、09-20（v0.2.4→v0.2.7）、
09-24（v0.2.7→v0.2.8）、**10-01（v0.2.8→v0.2.11，跨三个版本）**、10-02 23:31（→v0.2.13）、
10-12 16:39（v0.2.13→v0.2.15）、10-13（→v0.2.16）、10-16（→v0.2.17）、10-22（补丁 ps429b）、10-24（→v0.2.18-ps429b，首次带补丁升级）共十一次，每次踩到的坑都已固化进脚本。
**当前线上 v0.2.18-ps429b**（10-24 16:01 切，迁移 +1→297，用户面切换那分钟 ok=39/fail=1，0 ERROR，165 号/153 active 零翻转，post 8/8 绿）。⚠️ 这行会过时 ⇒ 现量命令见 skill
`cli-anything-sub2api`「版本现状」。升级用的那份**脚本**现 sha256 `68bbfc55…`（10-24 加 `build`/`patch-regress`、`go` 改走 build；198 旧版留 `.bak-20261024`=`65a369c8…`）。更早 `08a40bd2…`（10-16 改：Hub API 不通回退 GitHub releases；user plane 只数 sub2api 腿——旧版按名字含 grok/kimi 数，把 router9 `cursor-fc-grok-*` 504/openrouter 402/网宿黑名单 37 条切换前失败算成「Recreate 空档」；198 旧版留 `.bak-20261016`=`268ce0e4…`）
（10-01 改了 `verify` 的三处 + `check` 的暴露面，见下；198 上旧版留作
`sub2api-upgrade.py.bak-20261001` = `aea83379…`，更早的 `.bak-20260924`/`.bak-20260920` 也还在）。

🔴 **「两侧 sha 逐字相等」只证明 repo 工作区 == 198，不证明 git 里有这份。**
10-01 核完 `aea83379…` 两边相等就开跑，事后 `git diff` 才发现：**那 326 行差异全是未提交改动**，
`git HEAD` 里那份是 **09-24 修复之前**的旧版 —— `creationTimestamp` 锚点、probe delta、
3 小时基线统统不在版本控制里（`grep -c creationTimestamp` 在 HEAD 版是 **0**，工作区是 2）。
于是「从 git 重新 clone 一份来跑」会拿到一个会把切换前 18 分钟算进升级账的尺子。
⇒ **三方核对，不是两方**：`git show HEAD:<path> | shasum -a 256`、工作区、198 三个值
都要对上；只对后两个等于没核。这是
[[feedback_sop_referenced_scripts_were_never_in_git]] 的第五次复现。

🔴 **`verify` 那段错误统计前后修过四轮（09-24 两轮 + 10-01 两轮），形状变了：**

- **旧版写着 `since cutover`，实际锚在 pre-state（备份）时刻**，中间隔着 probe/backup/pull
  三步的十几分钟。09-24 它把切换前就停了的 30 条 grok 502 算进升级账上。
  **已修**：锚点改成 `kubectl get pod` 的 **`metadata.creationTimestamp`**
  （`Recreate` 在切换时造新 pod；⛔ 别用 `containerStatuses[].state.running.startedAt`，
  容器后来重启它会漂）。拿不到 pod 时会退回 pre-state 并**在行里喊出来自己退化了**。
- **第一版修法用等长 pre 窗口，当场又造了一次假红**：切完 7 分钟时报
  `grok 400 pre=0 post=4 <-- NEW`。真相是 grok-4.7 的 400 已经烧了 22 小时（775 条，
  同期 4.7 成功 33310 条），只是没落进那 7 分钟。**基线窗口比被测对象的突发周期还短，
  就不是基线。** **已修**：基线固定**至少 3 小时**，两边都换算成 `/min` 速率，
  且只在「post ≥ 5 条 **且** 速率 ≥ 3× 基线」时才喊 CHECK；post 窗口不足 10 分钟会
  明写「太薄，清不了任何东西，一小时后重跑」。
- 🔴 **10-01 第三轮：pre 腿与 post 腿横跨昼夜分界，而 pre 腿干脆是空的。**
  11:21 切换 ⇒ pre 窗口 = 北京 05:21~11:21，这张表在那 6 小时**一行都没有**；
  原版照样拿它当分母，算出 `grok 502 pre=0.17/min post=0.88/min` 喊 5×。
  实际 10-01 全天 grok-4.7 的 502 只有 337 条，**低于近一周日均**（09-30 974 / 09-29 500 /
  09-28 442）。**零基线不是低基线，它根本不是基线。** **已修**：加**第三条腿 `yday`
  = 24 小时前同一个时钟窗口**（它带昼夜形状，pre 腿带不了），只在 post **同时**超 pre 与
  yday 3× 时才喊；pre 腿为空时整段明写「pre leg is EMPTY，只认 yday」并让 pre 腿**不投票**
  （否则安静夜切换时真回归会被放过去）。
- 🔴 **10-01 第四轮，同一轮里 `status_code` 把两种故障合成一格。**
  那 319 条「502」里 **306 条的 `upstream_status_code` 是 422** —— `status_code` 是
  **sub2api 回给调用方的码**，它把 x.ai 的 422 包成 502。只按它分组，96% 的既存上游拒绝
  会把 13 条真网关 502 盖掉。**已修**：分组键加 `upstream_status_code`，表头打
  `code = 调用方拿到的 / up = 上游真给的`。
- 🔴 **阳性对照抓出的第五个病：地板写成速率就会随窗口放大。**
  `max(ryday, 0.05)` 这个 `0.05/min` 在 371 分钟窗口下等于 **18 条**，于是
  「两边都没先例的全新故障」（pre=0 yday=0 post=50）被判 quiet —— 原版同样有这病，
  只是被 pre 腿掩着。**已修**：地板回到**行数**（`post>=5`），比例只在基线非零时要求；
  `yday==0` 且 post≥5 直接喊 `NO PRECEDENT on either leg (new fault shape)`。
  ⚠️ **改完这段必须重跑那 10 case 的阳性对照**（两个方向都要分得开，只会说「没事」的
  尺子和坏尺子同形）：见 §G 末尾那段。

⇒ **这张表永远只能读 delta，不能读绝对值**：这台机 24h 内 grok 常态背景就是
502 x556 / 400 x957 / 422 x916，一条事故都没有。

```bash
ssh cltx@10.68.13.198
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py check   # 当前版本 vs hub latest + release notes
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py go      # 全流程（含 build 重打补丁 + patch-regress）
# 或者分步：probe --tag pre / backup / build / cutover --image <tag> / verify / probe --tag post / patch-regress
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py rollback-plan
```

**`go` 白天跑是安全的（09-20 起）。** 它把 check/probe/backup/build 四步**无条件**跑完
（这四步不碰线上），然后在 `cutover` 前撞窗口门禁停住，打印「dump 和镜像已就位，
只剩一条命令」。**这个顺序是刻意的**：要跟人确认断流时，该确认的只剩一刀，
而不是带着一个什么都没准备的问题去问。想在窗口外继续 = `go --force` 或单跑 `cutover --yes`。

⛔ **别想把断流「优化」掉成 RollingUpdate**：迁移在 pod 启动时跑，
两个版本同时对一个库写 schema 比断一分钟 503 严重得多。`Recreate` 是对的。

**⚠️ 全程用同一个身份跑**（推荐一路 `sudo`）。备份目录取 `$HOME`，`sudo` 下是
`/root/sub2api-backup`、不 sudo 是 `/home/cltx/...`；混着跑会让 `rollback-plan`
指向另一个时刻的 dump —— 09-10 我就制造过一次「指向升级**后**快照的回滚点」。
dump 里含 `accounts` 表的 grok/kimi OAuth token，**是凭据文件**，脚本已 chmod 600。

### 十条这脚本替你挡掉的坑

| 坑 | 形状 | 脚本怎么挡 |
|---|---|---|
| 镜像站 525 | `docker pull weishaw/sub2api:<版本号>` 被 `registry.dockermirror.com` 挡，`:latest` 能拉 | 拉 `:latest`，用 label `org.opencontainers.image.version` **反证**是不是目标版本，不符**拒绝 push** |
| `schema_migrations` 没有 `version` 列 | 主键是 **`filename`**，照通用 runbook 写必报 column does not exist | 查询已按 `filename`/`applied_at` |
| **迁移序号不唯一** | 09-20 一轮来了**两条都叫 238**（`238_opencode_go_platform` + `238_purge_unlimited_user_platform_quotas`），拿序号当主键/排序键会少算一条 | `verify` 检测同前缀并喊出来：数行数、只认 `filename` |
| Recreate 空档 | replicas=1 + `Recreate`，有 30~90s 断流，**同时打掉 grok + kimi + cursor gpt 组的 fallback 链** | 门禁挪到 `cutover` 正前方，前四步无条件跑完；`cutover` 打印影响面并要求 `yes` |
| 静默改数 | 迁移可能悄悄动账号/余额，几天后被用户发现 | `backup` 存 pre-state（镜像/迁移数/账号数/active/balance/**逐账号 status**），`verify` 逐项 diff |
| **`active` 变了但不知道是谁、也不知道是不是我干的** | 09-20 `active 39→33`，翻转时刻 == 重启时刻，**和「升级弄坏了凭据」完全同形**；我手搓了五轮 SQL 才定因 | `verify` 自己展开：逐个翻转账号打 `expires_at` + 末次成功 `usage_logs` + 影响面提示，并写明 `updated_at` 是**被发现的时刻**不是死亡时刻 |
| `"error": null` 读成红 | `/v1/responses` 正常也带 `"error": null`，`if "error" in d` 把全绿读成全红（09-08 栽过） | 判据是**唯一 nonce 原样回显**，不看 error 键 |
| **既存故障把 post 判成红** | 09-20 `sa-grok-4.20` 升级前后都是 503（半数腿被 x.ai 限流 park 到 09-22+）。判绝对全绿 ⇒ 只要有一条既存故障 `go` 就永久不可用，还得人眼 diff 两屏 | `probe` 落盘逐条判词，post 打 **delta**（fixed / pre-existing / **NEWLY BROKEN**），**只在 newly-broken 上返非零**；pre 红不再阻塞（它是基线）；两边跑的集合不一致会喊 `NOT RE-RUN` |
| ERROR 计数假阳性 | `grep -i '\bERROR\b'` 会命中健康 WARN 行里 JSON 的 `"error":` 键（09-10 实测 11 条全假） | 大小写敏感 + 锚在制表符分隔的日志级别字段 |
| **错误窗口锚错 + 基线太薄** | 09-24 两连坑：① 标题写 `since cutover` 实际锚在备份时刻，把切换前 18 分钟算进升级账；② 改成等长窗口后，切完 7 分钟报 `grok 400 pre=0 post=4 NEW`，而那是烧了 22 小时的背景 | 锚点=运行中 pod 的 `creationTimestamp`；基线**至少 3 小时**、两边换算 `/min` 速率；只在 post≥5 且 ≥3× 基线时喊；post 窗口 <10min 明写「太薄，一小时后重跑」 |
| 🔴 **基线横跨昼夜 / pre 腿是空的** | 10-01：11:21 切换 ⇒ pre 窗口落在凌晨零行，拿 0 当分母喊出 5×，而当天 502 实际低于一周日均 | 加第三条腿 **`yday`=24h 前同一时钟窗口**，须同时超 pre 与 yday 3× 才喊；pre 为空时明写 `pre leg is EMPTY` 且不让它投票 |
| 🔴 **`status_code` 把上游拒绝和网关故障合成一格** | 10-01：319 条「502」里 306 条的 `upstream_status_code` 其实是 422（sub2api 把 x.ai 的 422 包成 502），13 条真 502 被盖掉 | 分组键加 `upstream_status_code`，表头标明 `code`=调用方拿到的 / `up`=上游真给的 |
| 🔴 **地板写成速率 ⇒ 窗口越长越吞新故障** | 阳性对照抓到：`0.05/min` 在 371min 窗口下 = 18 条，于是 pre=0 yday=0 post=50 的全新故障被判 quiet（原版同病） | 地板回到**行数** `post>=5`；比例只在基线非零时要求；`yday==0` 直接喊 `NO PRECEDENT on either leg` |
| 🔴 **整段 `verify` 从不量用户面** | 10-01 真正给出结论的两个数字（切换那 1 秒 5 条 `APIConnectionError`、之后 100/0）全靠事后手搓 `SpendLogs` | 新增 `=== user plane ===` 段：切换前 3min~后 10min **逐分钟** ok/fail + 「切换+1min 至今」总计，直接读 `LiteLLM_SpendLogs` |
| 🔴 **release notes 的「默认开启」不等于与我们无关** | 0.2.11 带 key 创建上限 200/60 与余额在途预占两条默认开启项，是我手查 `api_keys` / balance 才敢说碰不到 | `check` 检出「破坏性/BREAKING」小节时**自己量暴露面**：逐 user 的 active key 数、user 1 的 balance，量不到时明写「别读成不受影响」 |

### 回归的边界（脚本会自己喊出来，别替它下结论）

- **`ops_error_logs` 必须按 `platform` 拆**。这台机器还跑 openai 图片模型和
  antigravity：09-10 升级后那几条错误**全是 `openai / gpt-image-*`**（还有人在拿
  `NOPE-fake-999` 探端点），读总数就会凭空造出一起 grok/kimi 事故。
  而且这张表数的是**上游尝试**，空也不是用户面的绿。
- **D1「每条腿真被选中」在夜间切换后不可判**：窗口里只有探针自己的流量，
  安静窗口和「腿从没被选中」形状一模一样。要判得白天重跑
  `sub2api-grok-onboard.py regress`。
- **kimi 探针是花钱的**：全公司共用一个会员，100 次/5h 且 100 次/周。
  所以 `probe --tag pre` 默认**不打 kimi**（阳性对照用 grok 那 4 发就够），
  `--tag post` 才打全 4 条协议面；要在 pre 也打就显式加 `--kimi`。
  **反过来，重跑 post 要显式 `--no-kimi`**（09-20 加的）—— 修完东西重跑一次很正常，
  每跑一次再吃 4 发**周额度**，跑几轮就悄悄啃掉全公司一块。
- **升级是为了某个具体修复才升的**，回归要**专门打那一条**。脚本收尾会提醒，
  但它不知道你这次为什么升。
- 🔴 **`verify` 喊出 `<-- CHANGED` 是提问不是结论。** 它现在会把逐账号证据打全
  （`expires_at` / 末次 usage / 影响面），但**下结论仍然是人的事**：
  判据是「这 token 一小时前还活着吗」。几天前就过期 ⇒ 是重启的刷新周期
  把旧账揭出来了，不是这次升级。见 [[feedback_restart_token_refresher_reveals_pre_dead_tokens]]。
- 🔴 **`verify` 里 `<-- CHECK` 那几行同样是提问。** 10-01 改完门禁后它喊出三条，
  逐条查完**全不是升级造成的**：`400 ×14` = acct 70（当天 10:20 刚建的号）16:21~16:25
  四分钟内 x.ai 回的 400；`502/internal ×13` = acct 68 在 13:29~13:39 的
  `The model is currently at capacity due to high demand`（上游容量，原文就写着）；
  `503/account_auth ×12` = `No healthy Grok OAuth account is currently available`，
  分散 14:09~17:08 涉及 acct 18/19/20/27/30，是「新号 25~30h 烧成 spending-limit」
  的日常形状。**三条最早的也在切换后 2h48m** ⇒ 门禁该喊的是「这格偏离了昨日同时段」，
  不是「升级弄坏了」。
- 🔴 **改 `verify` 那段门禁 = 改尺子本身，必须拿阳性对照验两个方向。**
  只会说「没事」的尺子和坏尺子同形：10-01 第一版改完对真数据一条不喊，看着很干净，
  阳性对照一跑就露出「全新故障被速率地板吞掉」。现成 harness（判定逻辑逐字照抄
  `cmd_verify`，10 个 case 含今天 4 条真实行 + 安静夜真回归 + 无先例新故障 + 09-24 那次假红）
  在 [[project_sub2api_198_upgrade_v023_2026_09_08]] 的 10-01 段落里，改完照抄重跑，
  **10/10 才算过**。

### 升级的实际数值（下次对照用）

10-02 v0.2.13 / 10-12 v0.2.15 两次只留下这些可复核的数(10-13 从 198 现读):迁移 **+2 → `241_add_typesafe_platform.sql`**、
**+1 → 292 / `242_drop_platform_check_constraints.sql`**;10-12 的 `probe-pre` 4/4 ok、`probe-post` 8/8 ok
(`/root/sub2api-backup/`,dump `sub2api-20261012-1637.dump`,升级前镜像 `20261002-v0.2.13`、74 号/64 active);
现 pod `restartCount=0`。rollout 时长、ERROR 数、verify 结果**没留记录**,别补编。

| | 09-08 v0.2.3 | 09-10 v0.2.4 | 09-20 v0.2.7 | 09-24 v0.2.8 | **10-01 v0.2.11** |
|---|---|---|---|---|---|
| 迁移 | 8 条 → `236_…` | +1 → 284 | +2 → 286（两条都叫 238） | +3 → 289（含 `238b_…`，**b 后缀**，又一次证明只能认 `filename`） | **+0 → 289**（跨三个版本却零 schema 变更；head 仍 `240_affiliate_ledger_operation_id.sql`。脚本明写「没迁移也正常」，⛔ 别据此怀疑镜像没换 —— 判据是 label） |
| rollout | — | ~30s | 31s，`restartCount=0` | 31s，`restartCount=0` | **30s**，`restartCount=0` |
| 真 ERROR | 0 | 0 | 0 | 0 | **0** |
| 账号 | — | 20 / 18 active | 41 / 39→33（6 个 antigravity，非升级所致） | 61 / 53→53，逐账号零翻转 | **71 / 62→62，逐账号零翻转** |
| 回归 | — | 8/8 | 7/8，delta 判定 0 regression | 4/4 pre 全绿 → 4/4 post 全绿，0 NEWLY BROKEN | **4/4 pre → 8/8 post（含 4 条 kimi）全绿，0 NEWLY BROKEN** |
| 备份 | 2.9MB | 6.6MB | `sub2api-20260920-1324.dump`，sha256 `2f1daff4…` | `sub2api-20260924-1409.dump`，sha256 `f1fd9c0b…` | `sub2api-20261001-1058.dump`，sha256 `e7d96ae2…` |

**09-20 那次是白天高峰切的**（用户拍板接受 30~90s 断流），不是夜间窗口 ——
我先把 probe pre / backup / pull 三步做完才去问，问的时候只剩一刀。
0.2.7 与我们相关的修复：Kimi 国内 Coding Plan 配额耗尽 403 不再误判为永久禁用（改限时暂停）、
Grok Responses `sequence_number` 未始终写出、Grok 媒体槽位泄漏、
Codex 根级联合 schema 致 `/v1/responses`→`/v1/messages` 400。
全记录 [[project_sub2api_198_upgrade_v023_2026_09_08]]。

**09-24 v0.2.8 同样是白天切的**（14:27，同一套「四步先做完再问一刀」），切完 80 秒内
72 条成功 usage_logs、0 条 ops_error_logs，0 parked。与我们相关的修复：
**「临时不可调度状态限定于当前账号」**（正对 §⚡ 那个「一条坏请求沿 failover 把 10 条腿
逐个 park」的症状 —— ⚠️ **这是 release notes 的说法，不是我实测的**，下次再出全池 park
先照 §⚡ 数 failover 链验它）、Grok 冷却期间允许配额查询、工具 Schema 清洗（null required /
prefixItems）避免上游 400、原生 Grok 4.7 / GPT-6 Sol·Luna / Opus 5.5 支持。

**10-01 v0.2.11 也是白天切的**（11:21，同一套「四步先做完再问一刀」，用户当场拍板"现在就切"）。
跨 0.2.9/0.2.10/0.2.11 三个版本一跳。**断流的代价这次量到了具体数字**：LiteLLM `SpendLogs` 里
grok 恰好 **5 条 `APIConnectionError` 全落在 11:21:28~29 这两秒**（= 新 pod `creationTimestamp`），
之后 4 分钟 **100 成功 / 0 失败**。⇒ 以后说「断 30~90s」要补一句：用户面真正吃到的是
**切换那一两秒内在飞的请求**，不是整段 rollout。

🔴 **0.2.11 有两条「破坏性变更」默认开启，升之前先算自己碰不碰得到**（10-01 实测我们碰不到，
但这是**量过**才敢说的，别下次照抄结论）：

| 新默认 | 怎么量我们受不受影响 | 10-01 实测 |
|---|---|---|
| API Key 创建限制（单用户 200 把有效 / 每小时 60 次） | `select user_id, status, count(*) from api_keys group by 1,2` | **1 个 user、9 把 active** ⇒ 离 200 极远 |
| 余额在途预占（按预估成本预占，余额低的并发会更早被拒） | pre-state 里的 `balance` | **1.0e9（内部虚拟账本）** ⇒ 预占吃不穿 |

要关是 `api_key_create.max_active_per_user` / `max_per_user_per_hour` / `billing.inflight_reservation`
设 0 或关闭 —— 这次**没动**。
⚠️ `api_keys.status` 是 **varchar**（值 `'active'`），`status=1` 会报
`operator does not exist: character varying = integer`。

0.2.9~0.2.11 与我们相关的修复里值得专门回归的：**0.2.9「客户端断开统一标记为 499，
不再被误记为 502/200 上游错误」** —— 它直接改掉我们那张「常态背景 502 x556」表的读法，
下次拿 `ops_error_logs` 的 502 当判据前先确认这批 499 去哪了；0.2.10 修「复合（composite）分组
在旧版调度与 WebSocket 别名下的模型归属与路由」、「账号模型白名单与模型映射冲突」。
另外 0.2.11 自带 GPT-6.1 Sol 与 Astra Ultrafast 的识别/计费，和我们 09-30 在 198 上手接的那套平行。

🔴 **`verify` 那段 `ops_error_logs since cutover` 的窗口比字面宽，会把切换前的行算进来。**
09-24 它报 grok `502 x30 / 422 x6 / 400 x1`，而按分钟拆开后**切换时刻（14:27）之后一条都没有**，
那 30 条 502 全落在 13:58 及更早。这台机器 24 小时内 grok 常态就有 502 x556 / 400 x957 /
422 x916 的背景噪声 ⇒ **别拿 `verify` 这一段判「升级弄坏了」，要自己按 `created_at` 卡切换时刻重数。**

## H0. 🔴 sub2api 认得哪些 model，**写在库里不在二进制里** —— `grok-4.7` 接入记（2026-09-22）

✅ **结论先行：`sa-grok-4.7` 已于 09-22 接进 198，能用。** 下面是**我在同一天先判错一次**
的全过程，因为那个错法会重演。

### 🔴 判定源是 `accounts.extra.grok_observed_models`，**每条腿各有一份缓存快照**

sub2api 拒绝一个 model 时说的是
`Model "grok-4.7" is not supported by any configured account in this group` ——
**"any configured account" 是字面意思**：它逐个账号查那份快照，不是查一张全局表。

```sql
-- 哪些腿认得 grok-4.7（这才是 /v1/models 和 404 的真来源）
select case when (extra::jsonb->'grok_observed_models'->'models') @> '["grok-4.7"]'
            then 'has_47' else 'no_47' end, count(*), string_agg(id::text,',' order by id)
from accounts where platform='grok' and deleted_at is null group by 1;
```

快照里带 `"source":"upstream_v1_models"` + `fetched_at`，是**建号时/使用时**从
`api.x.ai/v1/models` 抓下来存的。⇒ **老腿的快照会过期**：09-22 09:38 全池 34 条腿只有
4 条认得 4.7（30/52/53/54，全是当天刷新过的），两分钟后变 8 条（30/31/44/45/51/52/53/54）——
**正在出活的腿会自己刷新**，不用人去捅。`quota` 子命令**不刷**这份快照（09-22 实测
acct 44 跑完 `fetched_at` 一秒没动），别拿它当刷新手段。

⇒ **新模型进 sub2api 的正常路径就是"加一个新号"**：新腿建号时抓的是当天的上游目录。
09-22 这三条 heavy（52/53/54）就是这么把 `grok-4.7` 带进池子的。

### ⛔ 我判错的那一步：拿 `grep /app/sub2api` 当判定源

我在容器里 `grep -c` 二进制，`grok-4.6` 命中 4 次、`grok-4.7` 命中 0 次，就写下了
"名单编译进二进制、0.2.7 已是 hub latest ⇒ 永远加不了、脚本不用改"。
**用户在 sub2api 管理界面上用 acct `spggfyvwtsc` 实测 grok-4.7 直接答出来了**，结论当场作废。

三段式里我缺的正是"证伪条件"：二进制里有某个字符串 ⇒ 说明不了它是**唯一**判定源；
`grok-4.6` 那 4 次命中只是别处的默认值。**该查的是"这个 404 是谁抛的"，
判据在 `accounts` / `groups` 两张表**，我当时只 `\d accounts` 看了列名就说"没有白名单列"，
而真名单藏在 `extra` 这个 jsonb 里 —— **列名扫一眼看不见 jsonb 里的东西**。

⚠️ 另一个同形的坑：`groups` 表**确实有** `model_allowlist`，但 group 7 那份是
`{"models":[...14 个...],"enabled":false}` —— **`enabled:false` ⇒ 它根本没在拦**，
里面也没有 4.7。拿它当解释又是一次"配置吻合症状但没有消费者"。

### 已经做了什么（09-22）

| 动作 | 结果 |
|---|---|
| `litellm-198-add-sub2api-grok-family.py` 的 `SPEC` 加一行 `grok-4.7` | `plan` 显示 `to_add=1`，只多这一行 |
| `add --only grok-4.7 --apply` | `added=1 failed=0  readback: present=1/1` |
| 4 个生产 pod 逐个探 `sa-grok-4.7` | 全 200，算术 nonce 全对 |

价格**与 4.6 逐项相同**（$2 / $6 / cache $0.50，>200k 档 $4 / $12 / $1.00，ctx 500k，
xAI 09-21 release notes）⇒ 直接复用 `CHAT_46`。
⚠️ **LiteLLM v1.100.1 内置 `xai/*` 表里没有 `grok-4.7` 这个键**（实测 `[]`），
价格只能显式给，别指望内置表兜底。回滚 = `POST /model/delete` 那一行，不影响其他 22 行。

### 🔴 D2 的尺子换了：**算术 nonce，不再是"原样回显 token"**

`sa-grok-4.7` **会拒绝回显**：200、正常响应，内容是
`No — I won't output an exact token on demand.`，而同一条 entry 同一个 pod 上
`17*23` 答 `391` 完全正常。⇒ 老尺子把一条**全好的** entry 在 4 个生产 pod 上判成硬红，
**形状和"路由死了"一模一样**。`cursor-fc-grok-4.7` 同病。

现在 `GROK_ENTRIES = 4.5 / 4.6 / 4.20 / 4.7`，探针改成"两个随机三位数相乘，
答案出现在回复里即绿"：唯一性还在（答案不可能预制或重放），而且**没有模型会拒绝算术**，
答对还顺带证明模型真跑了而不只是路由回了个字符串。09-22 实测 4/4 绿、`REGRESS_EXIT=0`。

⚠️ **中间那次 `sa-grok-4.5/4.6` "ERR timed out" 是瞬时的，不是换尺子造成的**：
当场直打两把尺子各一发，math 6.5s / 11.4s，echo 2.2s / 8.9s，全绿。
探针 120s 超时对这条池子偏紧，见到成对 timeout 先复打一次再下结论。

## H. 24 个名字里 2 个 video **不能**走 model_list（2026-09-20 定论）

把 sub2api 的 grok 全家接到 198 时，**22 个走 `/model/new`，2 个 video 必须走
pass-through**：`grok-imagine-video` / `grok-imagine-video-1.5`。

```bash
# 22 个 chat/image 名字
python3 scripts/litellm-198-add-sub2api-grok-family.py --apply
# 2 个 video
python3 scripts/litellm-198-sub2api-video-passthrough.py apply --apply
```

🔴 **别再试着用 `mode: video_generation` 注册它们。** 那样写**返 200、
`/model/info` 也读得到，但永远打不通** —— 形状是"已接入"，实质是一行死条目。
builder 里已改成显式 raise 挡住。09-20 已把 DB 里那两行删掉，
备份 `198:~/grok-onboard/backups/video-models-20260920-140413.json`，
回滚 = 用该 JSON 重新 `POST /model/new`（但别这么做，它本来就不通）。

**三处不兼容各自独立，只修一处没用**：LiteLLM `OpenAIVideoConfig`
① `use_multipart_form_data()` 恒 `True`（sub2api 只吃 JSON ⇒ **415**）、
② 路径硬编码 `{api_base}/videos`（真身 `/v1/videos/generations`）、
③ 响应过 `VideoObject.model_validate` 强制 `id`+`object`+`status`
（sub2api 返 `{"request_id": …}`）。

⛔ **"等升级"是永久等待**：upstream **PR #38104（2026-08-24 合入，早于我们的
v1.100.1）故意**把 `/v1/videos` 改成无条件 multipart，去对齐官方 OpenAI SDK。
issue #36493 仍 open 追这一类缺口。

⚠️ **代价：pass-through 不进 SpendLogs。** video 按秒计费而
`cost_per_request` 只有定额一种形状 ⇒ 对账去 sub2api 自己的 `usage_logs`
和响应里的 `usage.cost_in_usd_ticks`（`1e10 ticks = $1`，8s 片子实测 $0.40~$0.64）。

诊断矩阵、`include_subpath`/`auth` 两个不变量、三段验收梯子、全部坑位
⇒ skill **`litellm-passthrough-endpoint`**。

⚠️ 清点 `sa-*` 条目时**别用 `startswith("sa-grok")`** —— `sa-composer-2.5`
没有 grok 前缀，会被漏掉读成"少了一个"。**09-20 又多了一个**
`sa-composer-2.5-fast`（见下），这条更容易踩了。

### 组名改过一次：`sa-grok-composer-2.5-fast` → `sa-composer-2.5-fast`（09-20）

用户点名去掉 `grok-` 前缀，和既有的 `sa-composer-2.5` 一族对齐。**上游名没变**
（仍是 `openai/grok-composer-2.5-fast`），只有对外组名和 `model_info.id` 变了。

脚本里编码成 SPEC 的 `public` 覆盖字段，`model_info.id` 跟着**对外名**走：

```python
dict(name="grok-composer-2.5-fast", public="sa-composer-2.5-fast",
     mode="chat", price=CHAT_45, lands="grok-4.5"),
```

按 add→verify→remove 做的（改名是加法）：`/model/new` 新名 → 带 nonce 实打 200
且落点与老行一致 → 才 `/model/delete` 老行 `sa/grok-composer-2.5-fast`。
终态 DB `%grok-composer%` **0 行**、`LiteLLM_Config` 零引用、`sa-*` 共 **22** 行不变。
⚠️ 备份 `198:~/grok-onboard/backups/sa-grok-composer-rename-20260920-150154.json`
**不能回放**（`model`/`api_base` 库里是密文），回滚 = 去掉 `public` 那行重跑脚本。
详细纪律见 skill `add-litellm-model` 的「改一个已存在的组名」。

### 六个名已铺进全部 cursor key 白名单（09-20）

`sa-composer-2.5-fast` / `sa-grok-4.20-0309-reasoning` / `sa-grok-4.6-latest` /
`sa-grok-4.5-latest` / `sa-grok-imagine` / `sa-grok-imagine-image-2.0`
⇒ cursor **677/677**（含 19 把 blocked）。剩下 16 个名字和
claude-/carher-/其他 398 把**没动**，要铺按同一条路径走。
验收脚本 `scripts/litellm-198-grok6-cursor-probe.py`，SOP = skill
`litellm-198-key-allowlist`。⚠️ 那两个 image 名必须走 `/v1/images/generations`。

⚠️ **`-latest` 后缀模型名 x.ai 不认（10-15 修）**：`sa-grok-4.6-latest` / `sa-grok-4.5-latest` / `sa-grok-4.3-latest`
的上游 model 原本配成 `openai/grok-X.Y-latest`，x.ai 返 404，sub2api 按 `upstream_404_model_not_found` 触发
30 分钟模型级冷却。pool 只含 ultra 号（credentials.model_mapping 有 `-latest` 条目的 3 个），全 404 后整个模型 429。
已改为 `openai/grok-4.6` / `openai/grok-4.5` / `openai/grok-4.3`。
剩余 3 个（`sa-grok-4.20-multi-agent-latest` / `sa-grok-latest` / `sa-grok-build-latest`）base name 不在 observed_models，暂未动。

## 同一个 sub2api 上还挂着 kimi（2026-09-08 起）

**动这台 sub2api 不只影响 grok。** 它同时承载 Kimi Allegro 会员，
排查/重启/升级前先把这条腿也算进影响面（升级走 §G，那条路已经把影响面写进确认提示）：

| | grok | kimi |
|---|---|---|
| group / account | group 7 / acct 6,7,8,9,**18,19,20** | group 8 `kimi-allegro-cc` + acct 10（`chat_completions`）<br>group 9 `kimi-allegro-anthropic` + acct 11（`anthropic`）|
| LiteLLM entry | `sa-grok-4.5/4.6/4.20` | `sa-kimi-k3`、`sa-kimi-k3-responses`、`sa-kimi-code`、`sa-kimi-code-anthropic`、`sa-kimi-code-responses` |
| 上游 | api.x.ai | api.kimi.com/coding（**不是** api.moonshot.cn，两套鉴权不通用）|
| 额度量具 | 内部虚拟余额（§B）| `GET /api/v1/admin/cn-providers/accounts/{10,11}/quota`<br>⚠️ 09-08 实测 `used_percent` 恒读 0、`used`/`limit` 全 `None`，**这把尺子疑似已坏**，别拿它当"没消耗"的证据 |
| 额度 | 见 §B | **100 次/5h 且 100 次/周**，全公司共用一个会员 |

三个要点：

1. **`sa-kimi-k3` 一条 entry 吃三面**（chat + `/v1/messages` + `/v1/responses`，
   09-08 实测 nonce 全回显）。写法是 `custom_openai/k3` + `model_info.mode: chat`。
   ⚠️ **但这只对简单 payload 成立**。`mode: chat` 会让 LiteLLM 把 Responses 请求
   **翻译成 chat** 再发上游；Codex 0.153.4 的 `tools[]` 里带
   `{"type":"namespace","name":"functions","tools":[…]}`，这形状 chat 协议没有，
   翻译后上游拒收 → `codex exec` 恒 400 `invalid_request_error`。
   **Codex / 带 Responses 专有工具的客户端一律用 `sa-kimi-k3-responses`**
   （`openai/k3` + `mode: responses`，原生直通）。

   排查这类 400 时的第一反射**不是**去查 sub2api：09-08 实测同一个 body
   直打 sub2api `/v1/responses` 是 **200 completed**，网关无辜。判据是拿客户端
   **原始 body 逐字重放**（从 `LiteLLM_SpendLogs.proxy_server_request` 取）再单变量消融——
   自己拼的"最小复现"复现不出来。详见
   [[feedback_responses_to_chat_translation_drops_namespace_tools]]。
2. **两条 k3 entry 都已铺进全部 1258 把 cursor/claude key 白名单，但永不进 fallback**——
   100 次/周的池子被主力流量自动打一次就干了。见 skill `litellm-198-key-allowlist`。
3. sub2api 对 kimi 的 `TestCredentials` **直接 return nil**，"凭据测试通过"是**假绿**；
   判活只能拿 pool key 打真实推理。

全记录：[[project_kimi_allegro_sub2api_198_2026_09_08]]、
升级史与 SOP（`created_at` 修复出自 v0.2.3；**当前线上 v0.2.18-ps429b**，10-24 16:01 切的）：见 **§G** +
[[project_sub2api_198_upgrade_v023_2026_09_08]]。

## 同一个 sub2api 上还挂着 antigravity（09-10 补到 6 条，09-16 又被加到 9 条）

group 10 `ag-gemini-probe-s48` + account **15/16/17（09-09 建）、21/22/23（09-10 建）、
24/25/26（09-16 建，不是我这条线加的）= 九个** Google AI Pro 号（09-20 实测，
旧文档写"六个 15/16/17/21/22/23"已过期）。
**目前只有一把 probe key `s48-ag-probe`，零生产流量** —— 生产的 Antigravity 走的是
另一条路（`cli-proxy-api` → LiteLLM 的 12 条 `ag-*` entry），不是这台。
所以重启/升级 sub2api **不会**影响同事的 gemini。

⚠️ **但凭据是两份拷贝**：同一批 refresh_token 既在 k8s Secret `cliproxy-secrets`、
又在这台的 Postgres 里，两边各自刷。**加号/撤号要两边都动**，只动一边会留僵尸腿。

🔴 **acct 21-26 这六条腿已经死了（09-20 实测）**：`invalid_grant`，
`credentials.expires_at` 全冻在 **09-16 19:33~20:18**，末次成功 `usage_logs` 是 09-10。

⚠️ **`expires_at` 是 unix epoch，而且它是 access token 的到期时刻 ⇒ 实际读法是
「末次成功刷新的水位线」，不是「死亡日期」。** 活着的腿这个值永远在**未来**
（15/16/17 实测是当天 14:03~14:08，每 5min 往前滚）；死掉的腿它**冻在最后一次刷新成功的那刻**。
所以"冻在 09-16"= refresh_token 从 09-16 起就被 Google 拒了，**比"过期了"是更硬的证据**。
⛔ 别忘了 `to_timestamp(...::bigint)`，裸读是个十位数字。
是 09-20 升级时新 pod 的 token 刷新周期把它揭出来的，**不是升级弄坏的**
（[[feedback_restart_token_refresher_reveals_pre_dead_tokens]]）。acct 15/16/17 活着。
修它要重新走 OAuth 拿新 refresh_token，**两边都要写**。

⚠️ **别拿 `status` / `error_message` 判它死活，这两列每 5min 被刷新周期覆写、会来回翻。**
09-20 实测：13:26 写成 `status=error`，13:36:26 又失败一轮，**13:36:53 却全翻回 `active`
并清空 `error_message`**，而 `expires_at` 一直是 09-16、`schedulable` 一直 `f`。
`audit_logs` 45 分钟零行 ⇒ 是 sub2api 自己翻的。**只认 `expires_at` + 末次 usage。**

✅ **09-10 留的「双刷互踢」猜想已被 09-20 的数据证伪，别再提它。** 判据：
`cliproxy-secrets`（ns `litellm-dev`，唯一消费者是 deploy `cli-proxy-api`）里 8 个
`antigravity-<email>.json` 的 `refresh_token` 与 DB 对应行 **md5 逐个相同** ——
而这 8 个里 `samuelsmart341`(=acct 15)、`ikmedose`(=acct 16) **是活着的**。
「两边各有一份同一个 token」在活腿和死腿身上同时成立 ⇒ **它不是区分量**。
（acct 17 `1632004@gmail.com` 反过来只在 DB、Secret 里没有，也活着。）

⚠️ Secret 那边是**静态种子、从不回写**：`timestamp=0`、`expired=2020-01-01T00:00:00`、
`disabled=false`，八个文件全一样 ⇒ **别拿 Secret 里的字段判死活**，它只是建号时塞进去的原件。
（读它做比对时只打 `md5(refresh_token)`，不要把 token 本体打到终端/日志里。）

剩下的真问题仍是「21-26 为什么在 09-16 一起被拒」，**目前没有能定因的数据**：
21/22/23 是 09-10 建的活到 09-16，24/25/26 是 **09-16 建的当天就冻住**。
本机这边已经查完、**两个候选量都不是区分量，别再查第三遍**：
`project_id` 八个全是 `aicode-consumers`、`user_agent` 全是 `antigravity/1.0.0 windows/amd64`，
活腿死腿一样。要定因只剩 Google 侧（那批 Gmail 是否被判滥用/风控），本机拿不到证据。

加号、判活、回滚、六个坑（`batch-refresh` 参数名、`privacy_set_failed`、
`error_message` 是历史残留、逐腿只能看 `usage_logs.account_id`…）
一律走 skill **`cliproxy-antigravity-ops` §两条路** +
`scripts/sub2api-antigravity-add-account.py`，别在这里另起一套。

## 相关

- video 走 pass-through 的全套（诊断矩阵 / 不变量 / 验收梯子）：skill **`litellm-passthrough-endpoint`**
- 部署与接入原始记录：[[project_198_sub2api_grok_litellm_2026_08_21]]
- 09-04 定因 + 加号全过程：[[project_sub2api_grok_balance_gate_and_acct7_2026_09_04]]
- 188 空壳那套的历史：[[project_grok_routing_reality_and_sub2api_empty_2026_08_13]]
