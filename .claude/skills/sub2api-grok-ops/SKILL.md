---
name: sub2api-grok-ops
description: >-
  198 LiteLLM 上 grok 的运维：**现存三条互相独立的 grok 路径**的分诊(修好一条不等于
  grok 好了；路 A grok-proxy 已于 2026-09-08 彻底删除，报错是预期别去修)、
  sub2api 内部虚拟余额充值、SSO→OAuth 建号、`sa-grok-4.6` 作为 cursor gpt 系 fallback、两层回归。
  同一台 sub2api 上还挂着 Kimi Allegro（`sa-kimi-k3` / `sa-kimi-code*`，group 8/9），
  动它前先看影响面。**升级 sub2api 本体也在这里（§G，一条命令 `sub2api-upgrade.py go`）**。
  **「grok 又被全部停下了 / 重新认证下」= §⚡ 最前面那一节：先确认巡检还在跑（`rescue`
  单跑撑不过两分钟，一条 403 park 的是整池），而且九成不该跑 reauth（腿通常是健康的，
  被 sub2api 自己 park 了）。**
  **grok 的 24 个名字里有 22 个走 model_list，2 个 video 走 pass-through（§H）——
  LiteLLM 的 `/v1/videos` 结构性不兼容 sub2api 且升级永远修不好。**
  Use when 用户说"grok 不能用了"/"我的 grok 又被停了/全停了"/"grok 重新认证下"/
  "重新授权 grok"/"sub2api 403"/"grok 报余额不足"/
  "再加一个 grok 账号"/"sa-grok-*/grok-4.5 打不通"/"kimi 不能用了"/"sa-kimi-* 报错"/
  "sub2api 有新版本了升级下"/"grok 出图能用出视频不能用"/"视频模型打不通"。
---

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
| grok 账号 | **12 条腿**（09-17 12:31 起），但**实际服务只有 6 条**：id=6/7/8/9/18/20 长期退避空转（`sched=f`），干活的是 19 + 09-17 两批新增：上午 **27 `LandenBeahanhes@hotmail.com`、28 `LarryHowedbf@outlook.com`**，中午 **29 `LarryWhitepgd@outlook.com`、30 `LarueLarkinjkt@outlook.com`、31 `LarueDickiheb@outlook.com`**（全部 `supergrok_heavy`、`concurrency=200`、group 7）。⚠️ **别拿"12 条腿"当容量**，跑 `health` 拿真数 |
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

### C0. ✅ 首选：手里有 grok.com `sso` cookie 时，**用 sub2api 自带的 SSO→OAuth**

09-08 实测：sub2api 有原生端点，**根本不用起 Playwright、不用 225 WARP**（沿 sub2api
pod 自己的出口打 auth.x.ai，实测通）。有 sso 就走这条，C1 只在**只有邮箱密码**时才用。

| 端点 | 用途 |
|---|---|
| `POST /api/v1/admin/grok/oauth/sso-token` `{"sso_token":"<eyJ...>"}` | **只换 token 不建号**，用来先验：返 `data.subscription_tier` / `sub` / `team_id` |
| `POST /api/v1/admin/grok/sso-to-oauth` | 换 token **并建号**，`{"sso_tokens":[...],"name":"<email>","group_ids":[7],"credentials":{"base_url":"https://api.x.ai/v1"},"concurrency":200,"priority":1,"rate_multiplier":1,"auto_pause_on_expired":true}`；返 `data.created[]` / `data.failed[]` |
| `GET /api/v1/admin/grok/accounts/{id}/quota` | **per-account 实探**（绕开池子）。⚠️ 只有 `source=hybrid_probe` 的号才有活的 x.ai rate-limit 头；6/7/8/9 返 `source=billing_probe` + `snapshot=null` + `headers_observed=false`，**读出来一排 None，和死腿长得一模一样**（09-09 实测，它们当时正扛几百个真请求）。**09-17 这把尺子又换了个坏形状：老 7 条全返 502 `GROK_QUOTA_TOKEN_UNAVAILABLE` / `oauth refresh account state changed`，新号 27/28 返 200 满读数** —— 502 同样只说明尺子没读数，判死活只认 `usage_logs`。档位在 `snapshot.plan_from_45_responses`，**不在** `plan` |

🛠 **别再手搓：`scripts/grok-onboard/sub2api-grok-onboard.py`**（09-09 固化，
09-17 加 `health` 子命令 + first-pick 列 + `regress` 按 D2 定退出码；
六个子命令都在 198 实跑验过）

### ⚡ 09-21 起：加号只要一条命令 —— **只传路径**

```bash
sudo python3 /home/cltx/grok-onboard/sub2api-grok-onboard.py /abs/path/卡密导出.txt
```

就这一行，**不用先捞密码、不用 `S2A_PW_FILE=`、不用 `cd`、不用写子命令**：

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

档位的**权威自报**是 SSO→OAuth 换回来的 `data.subscription_tier`（xAI 自己说的）+
sub2api 从 grok-4.5 响应里推的 `extra.grok_usage_snapshot.plan_from_45_responses`，
再加 access_token JWT 里的 `tier`（heavy=5）。
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

### C3. 建 account + 绑 group，回读

`POST /api/v1/admin/accounts`（platform=grok, oauth），然后绑 group 7，
**GET 回读 `account_groups` 确认出现 `<acct>|7|1` 那一行**。

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

198 上常驻 `/Data/sub2api-ops/sub2api-upgrade.py`（源在 repo
`scripts/grok-onboard/sub2api-upgrade.py`，两边 sha256 应逐字相等，改完必同步）。
09-08（v0.1.179→v0.2.3）、09-10（v0.2.3→v0.2.4）、09-20（v0.2.4→v0.2.7）三次，
每次踩到的坑都已固化进脚本。**当前线上 v0.2.7**，脚本 sha256 `77a4e06e…`（repo 与 198 `/Data/sub2api-ops/` 同值，改完必核）。

```bash
ssh cltx@10.68.13.198
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py check   # 当前版本 vs hub latest + release notes
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py go      # 全流程
# 或者分步：probe --tag pre / backup / pull / cutover / verify / probe --tag post
sudo python3 /Data/sub2api-ops/sub2api-upgrade.py rollback-plan
```

**`go` 白天跑是安全的（09-20 起）。** 它把 check/probe/backup/pull 四步**无条件**跑完
（这四步不碰线上），然后在 `cutover` 前撞窗口门禁停住，打印「dump 和镜像已就位，
只剩一条命令」。**这个顺序是刻意的**：要跟人确认断流时，该确认的只剩一刀，
而不是带着一个什么都没准备的问题去问。想在窗口外继续 = `go --force` 或单跑 `cutover --yes`。

⛔ **别想把断流「优化」掉成 RollingUpdate**：迁移在 pod 启动时跑，
两个版本同时对一个库写 schema 比断一分钟 503 严重得多。`Recreate` 是对的。

**⚠️ 全程用同一个身份跑**（推荐一路 `sudo`）。备份目录取 `$HOME`，`sudo` 下是
`/root/sub2api-backup`、不 sudo 是 `/home/cltx/...`；混着跑会让 `rollback-plan`
指向另一个时刻的 dump —— 09-10 我就制造过一次「指向升级**后**快照的回滚点」。
dump 里含 `accounts` 表的 grok/kimi OAuth token，**是凭据文件**，脚本已 chmod 600。

### 九条这脚本替你挡掉的坑

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

### 三次升级的实际数值（下次对照用）

| | 09-08 v0.2.3 | 09-10 v0.2.4 | **09-20 v0.2.7** |
|---|---|---|---|
| 迁移 | 8 条 → `236_…` | +1 → 284 | **+2 → 286**（两条都叫 238） |
| rollout | — | ~30s | **31s**，`restartCount=0` |
| 真 ERROR | 0 | 0 | **0** |
| 账号 | — | 20 / 18 active | 41 / **39→33**（6 个 antigravity，非升级所致） |
| 回归 | — | 8/8 | **7/8，delta 判定 0 regression** |
| 备份 | 2.9MB | 6.6MB | `sub2api-20260920-1324.dump`，sha256 `2f1daff4…` |

**09-20 那次是白天高峰切的**（用户拍板接受 30~90s 断流），不是夜间窗口 ——
我先把 probe pre / backup / pull 三步做完才去问，问的时候只剩一刀。
0.2.7 与我们相关的修复：Kimi 国内 Coding Plan 配额耗尽 403 不再误判为永久禁用（改限时暂停）、
Grok Responses `sequence_number` 未始终写出、Grok 媒体槽位泄漏、
Codex 根级联合 schema 致 `/v1/responses`→`/v1/messages` 400。
全记录 [[project_sub2api_198_upgrade_v023_2026_09_08]]。

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
升级史与 SOP（`created_at` 修复出自 v0.2.3；**当前线上 v0.2.7**，09-20 切的）：见 **§G** +
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
