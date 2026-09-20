---
name: litellm-budget-mgmt
description: >-
  Manage daily/periodic budget limits for LiteLLM virtual keys (claude-code-*,
  cursor-*, carher-* etc). Use when the user mentions "限额" / "预算" / "budget"
  / "每日额度" / "max_budget" / "budget_duration", or wants to set, modify,
  remove, or query spending limits on LiteLLM keys. Also covers the per-series
  daily bucket ("系列日额度" / "main 系列" / budget_family_overrides /
  gpt53 vs other family), the cursor-* invariant (other == max_budget) with its
  audit script, codex-client 429 stop behavior (usage_limit_reached /
  x-codex-promo-message / BUDGET_CODEX_STOP_DISABLED), and WHEN budgets reset
  ("重置时间" / "重置时区" / "北京时间零点" / budget_reset_at /
  litellm_settings.timezone).
---

# LiteLLM Key 预算管理

通过 LiteLLM `/key/update` API 热更新 key 的预算限制，**无需重启服务**；批量/排除式调整也支持直接 SQL UPDATE（DB 改完 60s 内 router cache 失效自动生效，无需重启 proxy）。

## 适用环境矩阵

本 skill 三套环境共用一套语义，但**入口和操作方式不一样**：

| 环境 | namespace / 入口 | 用户 | 操作 推荐方式 |
|------|-----------------|------|--------------|
| **aliyun bot** | `carher` (litellm.carher.net) | `carher-*` Her bot keys | `/key/update` API（见下方"操作流程"）|
| **198 prod** | `litellm-product` (cc.auto-link.com.cn/pro) | `claude-code-*` + `cursor-*` 内部 IDE 用户 | **直 SQL UPDATE**（见"198 prod 批量 SQL 速查"）|
| **198 dev** | `litellm-dev` (cc.auto-link.com.cn/dev) | dev 测试 key | 同 198 prod |

**为什么 198 不用 /key/update**：批量 + 白名单排除 + "max_budget > N AND key_alias NOT IN (...)" 这种范围 update，SQL 一条搞定；走 API 要先列 key 再逐个 POST，N+1 慢且没事务。**只调几个特定 key** 才走 API。

## 198 prod 批量 SQL 速查（2026-05-18 实战路径）

**场景**：调整 prod claude-code-\* 或 cursor-\* key 的每日额度，可能要"全员降到 X，但白名单 N 人保留原值"。

```bash
scripts/jms ssh AIYJY-litellm '
DB_POD=litellm-db-0
DB_URL=$(kubectl -n litellm-product exec deploy/litellm-proxy -- env 2>/dev/null | grep DATABASE_URL)
PG_PW=$(echo "$DB_URL" | sed -E "s|.*://[^:]+:([^@]+)@.*|\1|")

# STEP 1: 预览（必做）—— SQL 单引号一定要转义对！见下方坑 1
kubectl -n litellm-product exec $DB_POD -- env PGPASSWORD="$PG_PW" \
  psql -U litellm -d litellm -h localhost -c "
SELECT key_alias, max_budget, budget_duration, ROUND(spend::numeric,2) AS spend
  FROM \"LiteLLM_VerificationToken\"
 WHERE key_alias LIKE '"'"'claude-code-%'"'"'
   AND max_budget > 70
   AND key_alias NOT IN ('"'"'claude-code-buyitian'"'"', '"'"'claude-code-biancaoming-x36t'"'"')
 ORDER BY max_budget DESC;"

# STEP 2: UPDATE（RETURNING 让你看到改了什么）
kubectl -n litellm-product exec $DB_POD -- env PGPASSWORD="$PG_PW" \
  psql -U litellm -d litellm -h localhost -c "
UPDATE \"LiteLLM_VerificationToken\"
   SET max_budget = 70, updated_at = NOW()
 WHERE key_alias LIKE '"'"'claude-code-%'"'"'
   AND max_budget > 70
   AND key_alias NOT IN ('"'"'claude-code-buyitian'"'"', '"'"'claude-code-biancaoming-x36t'"'"')
RETURNING key_alias, max_budget, ROUND(spend::numeric,2) AS spend;"

# STEP 3: 终态分布
kubectl -n litellm-product exec $DB_POD -- env PGPASSWORD="$PG_PW" \
  psql -U litellm -d litellm -h localhost -c "
SELECT max_budget, COUNT(*) FROM \"LiteLLM_VerificationToken\"
 WHERE key_alias LIKE '"'"'claude-code-%'"'"' GROUP BY max_budget ORDER BY max_budget DESC;"'
```

### 坑 1：bash + kubectl exec + psql 嵌套引号 — `"..."` 在 SQL 里是**列名引用** ⚠️

PostgreSQL 用双引号引列名（`"LiteLLM_VerificationToken"`），用**单引号**引字符串（`'claude-code-buyitian'`）。把字符串写成双引号 → PG 解析为列名 → `ERROR: column "..." does not exist` → **UPDATE 完全没生效但脚本不报错**（kubectl exec 还回 exit 0，因为打印的 ERROR 走 stdout）。

bash 单层包裹 `'...'` 内不能直接写单引号，必须用 `'"'"'`（关、加双引号转义的单引号、重开）四字符序列。完整示例：

```
key_alias NOT IN ('"'"'name1'"'"', '"'"'name2'"'"')
       ↑     ↑     ↑      ↑     ↑     ↑      ↑     ↑
       SQL 单引号开始    SQL 单引号结束（中间用 bash 转义序列）
```

**检测方法**：UPDATE 后用 SELECT 验证 max_budget 确实变了。WHERE 条件如果用列名当字符串，SELECT 会出意外结果（如全表命中或全空）。

### 坑 2：SQL UPDATE 不触发 LiteLLM router 缓存失效

直 SQL 写 DB 后，LiteLLM proxy 内存里的 key 对象**60s 内还是旧值**（router 缓存）。生产可接受（用户 retry 一下就好），但要立即生效得 `kubectl rollout restart deployment/litellm-proxy` 或调用 `/cache/flushall`。**预算上调不急**（用户暂时无感）、**预算下调要等 60s 才严格生效**（窗口期可能放过几个超额请求）。

### 坑 3：DELETE / TRUNCATE 之类的写操作要先 BEGIN

PG psql 命令行 default autocommit，单条 UPDATE 就是单条事务。对 ≥ 1000 行的批量 UPDATE，**先 EXPLAIN 看影响行数**再执行，避免 WHERE 写错把全表干了。

## 关键概念

| 字段 | 含义 | 示例值 |
|------|------|--------|
| `max_budget` | 单周期预算上限（美元） | `100.0` |
| `budget_duration` | 预算重置周期 | `1d`（每天）、`7d`（每周）、`30d`（每月） |
| `budget_reset_at` | 下次重置时间（LiteLLM 自算，**DB 里是 `timestamp without time zone` 裸 UTC**） | `2026-08-07 16:00:00` |
| `spend` | 当前周期已消费金额 | `42.5` |

- `max_budget` + `budget_duration` 配合 = **周期性预算**（如每天 $100）
- 只设 `max_budget` 不设 `budget_duration` = **总预算**（用完即止，不重置）
- 超额后 LiteLLM 自动拒绝请求，到 `budget_reset_at` 时 spend 归零自动恢复

## 超额后用户看到什么（198 prod 2026-08-21 起；阿里云 carher 同日仅①②）

用户报"额度用完了但提示看不懂 / 一直 429"时先对照这张表——**行为按 key 前缀分流**：

| 环境 / key | 超额行为 | 载体 |
|---|---|---|
| 198 `claude-code-*` / `cursor-*`（gated）| **HTTP 200** 助手消息「🚫 你的 key 今日额度已用完：已消费 $X / 限额 $Y。北京时间明日 00:00 自动重置…」| budget_notice.py ③ 软拦截（auth 层 monkey-patch 吞 BudgetExceededError → pre-call 出 mock，零上游零计费）|
| 198 其余 key | HTTP 429 + 友好中文 body + `type=budget_exceeded` | error_sanitize.py 的 BudgetExceededError 分支 |
| **阿里云 `carher-*`（her bot）**| HTTP 429 原生（**③ 用 `BUDGET_FRIENDLY_MOCK_DISABLED=1` 关着**，bot 侧行为不变；①查余额②90%预警已开，gate `BUDGET_NOTICE_KEY_PREFIXES=carher-`。要开③删该 env 即可）| 同一份 budget_notice.py（三拷贝 md5 全等），repo 清单 `k8s/litellm-proxy.yaml` |

**为什么 gated 是 200 不是 429**：Cursor 收到 429 完全不渲染 body，只显示自家
"exceeded retry limit" 串——200 助手消息是唯一用户可见通道（2026-08-21 活体
探针实证）。已知 tradeoff：Cursor agent 循环会反复收到同一条文案（零计费）。

**例外——codex 客户端超额回 429 停机（198 prod 2026-08-22 起）**：codex agent/goal
模式（UA `codex_exec/*` 或 header `originator:codex_exec`）把网关的 200 友好文案
**当成功续发 → 无限刷屏**。所以对 codex 客户端在 ③ 总额超限 与 ④ 系列超限两处
都**改回 HTTP 429 + body `error.type=usage_limit_reached` + 响应头
`x-codex-promo-message`**（ASCII-only，含用量数字/reset 时间/切 gpt-5.3 提示；codex
只认这个头、忽略 body）。codex 二进制把该 429 映射为非重试 `UsageLimitReached` →
退出 goal 循环。**非 codex UA 仍走 200 软拦截，不受影响**。逃生门
`BUDGET_CODEX_STOP_DISABLED=1`（退回 200 mock）。长稳观察脚本
`scripts/litellm-198-codex-stop-observe.sh`（cron 每 4h，输出 VERDICT: OK/ALERT）。
判据/实现全记录见 [[project_198_budget_notice_suite_2026_08_19]]。

**⚠️ codex CLI 与 codex IDE 扩展渲染不同(2026-08-24 实证,别再排查"头没发到")**:
`x-codex-promo-message` 头**只有 codex CLI(rust 二进制)读**,渲染成
"You've hit your usage limit. {promo}, or try again later."。**codex IDE 扩展
(`openai.chatgpt` VS Code/Cursor 插件)是另一套代码库**,5 个已装版本 grep
`codex-promo-message` **全 0 命中**,且插件自带中文本地化 → 撞额度时只显示它
硬编码的 zh-CN 串「你已达到使用上限。请稍后再试。」(webview `zh-CN-*.js` 里有
逐字原串)。**我们的 429 依旧让 IDE 退出循环(停机生效)**,但**促销文案在 IDE 上
不可控**——用户报"提示看不懂/没收到我们的文案"是假象:发了、到了、客户端不渲染。
CLI 用户才看得到我们的英文 promo。要给 IDE 用户可读文案没有网关侧办法(插件不认
第三方头)。三段式证据链见 [[feedback_codex_ide_ignores_promo_header_renders_own_zh]]。

配套能力（同一个 hook，`BUDGET_NOTICE_KEY_PREFIXES=claude-code-,cursor-` 门控）：
- 用户发 `/查余额`（Cursor 用户须发裸词 `查余额`，`/` 开头会被 Cursor 命令面板拦截）→ 200 返回今日用量，零计费
- 已用 ≥90% → 响应流末尾注入一次性预警（每 key 每北京日一次）

止血 env（改 deploy litellm-proxy env 即生效）：`BUDGET_NOTICE_DISABLED=1`
全关；`BUDGET_FRIENDLY_MOCK_DISABLED=1` 只关 200 软拦截退回友好 429；
`BUDGET_FRIENDLY_429_DISABLED=1` 连友好 429 文案也关。
排障日志判据：`kubectl -n litellm-product logs -l app=litellm-proxy --tail=-1 | grep "budget_notice: soft-block"`。
prod 冒烟：`scripts/litellm-198-budget-notice-smoke.sh`（198 上 sudo 跑，8 检查项）。
实现/上线全记录见 [[project_198_budget_notice_suite_2026_08_19]] 与
`.cursor/skills/litellm-hook-dev/SKILL.md` 的 budget_notice 案例。

## ⚠️ 重置额度 = 两套记账都要清（否则"重置了还是不可用"）

198 上有**两套独立记账**：① 总额度（DB `spend`/`max_budget`）② 系列日额度 ④（redis
`budget_notice:fam:*` 桶）。只重置总额度**碰不到 redis 系列桶**，key 照样被 ④ 拦
（实证 cursor-zhuge-zlcb：总 spend $18/$500 却因 other 桶 $544/$500 被软拦截）。

**一键全部重置**：`scripts/litellm-key-budget-reset.py <alias> --apply`（198 host 上跑，
默认 dry-run；先打印两套现值再动）。**全量 cursor 一次清**用集合运算版
`scripts/litellm-198-cursor-reset-all.sh --apply`（单条 SQL UPDATE + token 过滤批量 redis DEL,
秒级,替代逐把 585 次 API）。完整判据/手动兜底见专门 skill **`litellm-budget-full-reset`**。

## 模型系列日额度（④，198 prod 2026-08-22 起）

每 key 每北京日按**系列**限额（budget_notice.py 自建；litellm 原生
`model_max_budget` 做不到：精确匹配无系列桶、每模型独立记账、滚动 24h 窗、
超限裸 429）：

| 系列 | 匹配规则（model_group 名） | 默认限额 |
|---|---|---|
| GPT-5.3 系列 | 含 `gpt` 且含 `5.3`（8 个入口名全中） | **$200/天** |
| 其他所有模型 | 兜底匹配一切非 5.3（gpt/claude/deepseek/glm…共用一桶，fkey=`other`） | **$500/天** |

- 超限行为：200 助手消息「🚫 该系列今日额度已用完…其他系列不受影响」，零上游零计费；`/查余额` 显示各系列已用/限额
- 记账：redis `budget_notice:fam:{family}:{token}:{北京日}`（INCRBYFLOAT，键含日期天然北京 0 点换桶）；**只累计 response_cost>0 的请求**——免费入口（如 gpt-5.4-mini 计费 0）不占系列额度
- 调整：env `BUDGET_FAMILY_GPT53_USD` / `BUDGET_FAMILY_OTHER_USD`（全局）；按人覆盖走 key `metadata.budget_family_overrides: {"gpt53": 300}`（/key/update 热改，auth 缓存 ~60s 生效）
- 开关：`BUDGET_FAMILY_ENABLED=1`（仅 198 设了；阿里云未设=完全不生效）
- 与 key 总额度叠加判定：先系列后总额，两者独立；总额 $70 的普通 key 通常先触总额，系列限额主要约束白名单高额 key
- ⚠️ 动预算拦截逻辑必看 [[feedback_litellm_budget_rejection_has_three_gates]]——预算 429 有**三道独立闸门**（auth check / MaxBudgetLimiter hook / capacity 预留层），只 patch 一处 = 假修，零价格 mock 的 T0 测不出来

### cursor-\* 不变量：main 系列日限额 == 每天总限额（2026-08-23 约定）

每个 `cursor-*` key 的 `metadata.budget_family_overrides.other` **必须 == 该 key 自己的
`max_budget`**（"其他/main 系列"日桶 = 每天总额度）。**onboarding 新造的 cursor key 常
漏设 `other`** → 退回 env 默认 $500，与总额度脱节，需定期回归审计。gpt53 系列（$200）
不在此不变量内。

```bash
# 审计(dry-run)/对齐一把梭:
scripts/litellm-198-cursor-family-budget-audit.sh              # 只审计,列漏网 + 异常
scripts/litellm-198-cursor-family-budget-audit.sh --apply      # 对齐"干净"者(有每日周期)
scripts/litellm-198-cursor-family-budget-audit.sh --exclude canary   # 排除灰度残留测试 key
```

- 判据 SQL（漏网 = other 覆盖值缺省视作 500 后 <> max_budget）：
  `coalesce((metadata->'budget_family_overrides'->>'other')::float,500) <> max_budget` 过滤 `cursor-%`
- 脚本只自动对齐**干净 key**（`max_budget` 非空 + `budget_duration` 非空，走 SQL `jsonb_set`
  合并进 metadata 不动其它字段）；**异常另列不改**交人判：`budget_duration` 为空 =
  终身额度没有"每天总额"语义（如 `cursor-guran-v2sb`）、canary 灰度残留 key
- 首轮实战（08-23）：585 把 cursor-\*，569 已对齐，16 漏网 → 14 真实用户 key 对齐完成，
  2 个 `cursor-canary-bn-warn-*` 待删、1 个 guran 终身额异常

### ⚠️ 验 prod budget_notice callback 是否真生效 —— 别造新 key（假阴性）

见 [[feedback_fresh_key_not_gated_in_pre_call_hook]]：`/key/generate` 新造的测试 key 在
`async_pre_call_hook` 里 `user_api_key_dict.key_alias` **拿不到**（DB 行/`/key/info` 都有，
但 hook 收到空，等 95s、pin 单 pod 都无效）→ `_gated()` 返 False → **新 key 一律不被拦**。
验 prod callback（gate/系列/codex-stop 是否 live）**直接 grep 全 pod 日志里真实老 key 的
`budget_notice:` 行**（或全链 T0 `scripts/litellm-198-t0-fullchain.sh`，那里 key 已预热），
不要拿冒烟新 key 下"功能没生效"的结论。

## 重置时刻在哪个时区 —— 只看 `litellm_settings.timezone`

**重置发生在几点，跟主机 TZ / 容器 TZ / DB TZ 全无关**（2026-08-06 在 198 实测，litellm 1.90.2）：

```
reset_budget_job._reset_budget_common
  → proxy/common_utils/timezone_utils.get_budget_reset_time()
  → get_budget_reset_timezone() = getattr(litellm, "timezone", None) or "UTC"
  → litellm_core_utils/duration_parser.get_next_standardized_reset_time()
```

- `litellm_settings` 里的未知 key 走 proxy_server.py 的 `else: setattr(litellm, key, value)` → 写 `timezone: Asia/Shanghai` 即生效，**不用改代码**。全仓只有 `get_budget_reset_timezone` 读它，爆炸半径 = 预算重置锚点 + 新建 key 的初始 `reset_at`，**不影响日志/计费时间戳**。
- 不配 = UTC 午夜 = **北京早上 8 点**重置。
- 对齐规则：`1d` → 该时区次日 00:00；`7d` → 下个周一 00:00；`30d` → 下月 1 日 00:00；`24h` 走 `_handle_hour_reset`（`current_hour % 24 == current_hour`）**结果同样是次日午夜，与 `1d` 等价，不必改成 `1d`**。
- reset job 节奏：`PROXY_BUDGET_RESCHEDULER_MIN_TIME=597 / MAX=605`（**约 10 分钟一次 tick**），每个 proxy 副本从自己启动时刻起算。`general_settings.disable_reset_budget=true` 会整个关掉。

### 改时区锚点的三步（198 prod 实操，2026-08-06 走通）

```bash
# 0) 198 可直连，不必走 jms（见 [[198-direct-ssh]]）
SSH() { sshpass -p 'Hn8#mKLp3QxZ' ssh -o StrictHostKeyChecking=no cltx@10.68.13.198 "$@"; }
```

**STEP 1 — 改 CM + 重启**。CM `litellm-config` 有 4 个 data key（`config.yaml` / `raw.js` / `responses.js` / `web-tools.js`），**只能 patch `config.yaml` 那一个**，不要 `kubectl create cm --dry-run | apply` 重建（会掉另外 3 个 key）：

```bash
# 本地取 config.yaml → python 插入一行 → yaml 校验不变量 → patch-file 回写
#   assert 顶层 key 顺序不变 / len(model_list) 不变 / 其余 litellm_settings 逐字段相等
#   diff 必须只有 "+  timezone: Asia/Shanghai" 一行
kubectl -n litellm-product patch cm litellm-config --type=merge --patch-file=/tmp/cm-patch.json
kubectl -n litellm-product rollout restart deploy/litellm-proxy   # 4 副本 maxSurge=0/maxUnavailable=1，约 5 分钟
kubectl -n litellm-product rollout status  deploy/litellm-proxy
```

**STEP 2 — 探针验证生效**。`setting litellm.timezone=` 是 debug 级日志，prod 日志级别**看不到**，别指望 grep 日志。建一把临时 key 看 `/key/info`，看完立刻删（镜像无 curl，用 python urllib）：

```python
r  = call("/key/generate", {"key_alias":"tzprobe-tmp","max_budget":0.01,
                            "budget_duration":"1d","models":["no-such-model-tzprobe"]})
info = call("/key/info?key="+r["key"], method="GET")["info"]   # 期望 budget_reset_at = 目标时区次日 00:00
call("/key/delete", {"keys":[r["key"]]})
```

**STEP 3 — 存量 key 重锚，让 job 自己收尾**。把 `budget_reset_at` 拨到**刚过去的那个目标时区午夜**，下一个 tick job 会清 spend + 重算下次 reset_at —— **不用手写 `spend=0`**，job 顺带 invalidate spend counter 缓存，这一步同时就是端到端验证：

```sql
UPDATE "LiteLLM_VerificationToken"
   SET budget_reset_at = TIMESTAMPTZ '2026-08-06 16:00:00+00',   -- 北京 08-07 00:00
       updated_at = NOW()
 WHERE budget_duration IS NOT NULL;
```

### 时区相关的三个坑

1. **`budget_reset_at` 是裸 UTC 的 `timestamp`，不是 timestamptz**。查北京时间必须**两层**：`(budget_reset_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Shanghai'`。只写一层 `AT TIME ZONE 'Asia/Shanghai'` 会把裸值**当北京时间**解释，结果差 16 小时（2026-08-06 我第一次就这么看错，差点误判成没生效）。
2. **别 FLUSHDB**。198 的 redis 还存 weighted_affinity / deployment_affinity 粘性，冲了会打断 Codex 长会话。job 自带缓存失效，不需要冲。
3. **过期 key 不会被 job 捞**（`get_data(expires=now, reset_at=now)` 带 `expires` 过滤），它们的 `reset_at` 会永久停在过去值 —— 属正常残留，不是没生效。用 `WHERE expires < NOW() OR blocked` 把这类挑出来单独看。

### 时区 / 主机基线（2026-08-06 实测）

| 位置 | 时区 |
|---|---|
| 198 host `AIYJY-litellm` | **Asia/Shanghai (CST +0800)** |
| 225 host `AIYJY-litellm-standby` | **Etc/UTC**（跟 198 不一致，与预算无关） |
| litellm-proxy / litellm-db-0 容器内 | UTC（`TZ` 未设） |
| PostgreSQL `SHOW timezone` | Etc/UTC |

## Key 别名命名规则与默认限额

| 前缀 | 用途 | 默认每日限额 |
|------|------|-------------|
| `carher-*` | Her bot 实例（aliyun carher namespace） | **$100/天**（2026-05-23 确立为强制默认） |
| `claude-code-*` | Claude Code / CLI 开发者账户 | $100/天（prod 2026-05-18 起降为 $70/天） |
| `cursor-*` | Cursor IDE 开发者账户 | $100/天 |

> 🟢 **2026-08-21 — gated key 超额从 429 改为 200 软拦截**：见上方「超额后用户
> 看到什么」。此前 08-19 上线的友好 429 body 在 Cursor 里不可见（客户端吞 body），
> 是"用户一脸懵逼"的第二层根因（第一层是 error_sanitize 整体置换文案，08-19 已修）。
>
> 🟢 **2026-08-06 — 198 prod 重置锚点从 UTC 午夜改成北京午夜**：CM 加 `litellm_settings.timezone: Asia/Shanghai`，存量 **1313 把**周期预算 key（1306 个 `1d` + 7 个 `24h`：`cursor-*` 546 / `claude-code-*` 539 / `carher-*` 225 / 其他 3）重锚，reset job 一个 tick 内清零 $24,387 spend 并落到 `08-07 16:00Z = 北京 08-08 00:00`。残留两处：`claude-code-yuelin-ssc5`（已过期+blocked，job 不捞）；**245 把 `budget_duration IS NULL` 的终身额度 key 没动**（193 把带 max_budget，含 `canary-*` $3503 / `ceshi-*` $1909 / `claude-*`×5 / `cursor-*`×2 + 178 把无 alias 零消费），给它们配 duration = 终身上限变日上限，属独立决策不要顺手做。详见 [[198-litellm-budget-reset-beijing-midnight-2026-08-06]]。

> 🟢 **2026-05-23 — carher-\* 默认 $100/天 确立为强制策略**：新建 her 实例（Admin API batch-import 自动生成 `carher-{uid}` virtual key）和**任何手动新建 carher-\* key 都必须立刻设 $100/day + budget_duration=1d**——admin API 当前不会自动 set，要在 [[add-instances]] 创建后调用本 skill 的"操作流程"或 `scripts/litellm-key-budget.py`。3 个特批高额度保留不动：carher-2 ($300), carher-11 ($200), carher-94 ($150)。当时 234 个 carher-\* key 6 个无限额已补齐。

> 🟡 **2026-05-18**：prod 把 claude-code-\* 默认从 $100/天 → **$70/天**（337 个 key 批量降）。4 个白名单保留高额度：buyitian ($1200), biancaoming-x36t ($600), linsen-rg9t ($500), liuguoxian-50gj ($500)。cursor-\* 保持 $100/天。当前 prod 上限合计 ~$26k/天。

## 前置

1. 集群连通性：`kubectl get nodes`。若 `connection refused`，按
   `k8s-via-bastion` skill 启动 kubectl 隧道：

```bash
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

2. Port-forward LiteLLM：

```bash
kubectl port-forward -n carher svc/litellm-proxy 4000:4000 &
```

3. 获取 Master Key：

```bash
MASTER_KEY=$(kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
```

## 操作流程

### 1. 查询当前 key 的预算状态

拉取所有 key 的 spend 信息并按前缀过滤：

```bash
curl -s "http://127.0.0.1:4000/spend/keys?limit=600" \
  -H "Authorization: Bearer $MASTER_KEY" -o /tmp/spend_keys.json

python3 << 'PY'
import json
with open("/tmp/spend_keys.json") as f:
    rows = json.load(f)
for r in rows:
    a = r.get("key_alias") or ""
    if a.startswith("claude-code-") or a.startswith("cursor-"):
        print(f'{a}: spend={r.get("spend",0)}, max_budget={r.get("max_budget")}, budget_duration={r.get("budget_duration")}')
PY
```

查看单个 key 详情：

```bash
curl -s "http://127.0.0.1:4000/key/info?key=<token>" \
  -H "Authorization: Bearer $MASTER_KEY" | jq '.info | {key_alias, max_budget, budget_duration, spend, budget_reset_at}'
```

### 2. 批量设置每日预算

**aliyun carher-\* 默认场景优先用脚本** `scripts/litellm-key-budget.py`（自动 port-forward + 拿 master key + idempotent，2026-05-23 起 carher-\* 默认 $100/天的强制 enforcer）：

```bash
# 给所有"无限额"的 carher-* 补 $100/day，已有限额的不动（适合新建 her 后做 enforcer）
scripts/litellm-key-budget.py --apply

# 精确指定（如 batch-import 刚创建的 ID）
scripts/litellm-key-budget.py --apply --key carher-234 --key carher-235

# 强制覆盖现有限额（不会动 3 个特批白名单：carher-2/11/94）
scripts/litellm-key-budget.py --apply --force

# 自定义额度（如某 her 临时调到 $200）
scripts/litellm-key-budget.py --apply --force --key carher-99 --budget 200

# 只查看当前状态
scripts/litellm-key-budget.py --inspect
```

**手动批量场景**（cursor-\*、claude-code-\*、或非 aliyun 环境）用 Python 脚本通过 `/key/update` 逐个更新。`/key/update` 是热更新，不需要重启（auth 缓存约 60s 后严格生效，见"注意事项"）。

```bash
python3 << 'PYEOF'
import json, urllib.request

MASTER_KEY = "<master_key>"
BASE_URL = "http://127.0.0.1:4000"
TARGET_PREFIXES = ("claude-code-", "cursor-")  # 按需修改
MAX_BUDGET = 100.0      # 美元
BUDGET_DURATION = "1d"   # 每天重置

with open("/tmp/spend_keys.json") as f:
    rows = json.load(f)

targets = [r for r in rows if (r.get("key_alias") or "").startswith(TARGET_PREFIXES)]
print(f"Updating {len(targets)} keys: max_budget={MAX_BUDGET}, budget_duration={BUDGET_DURATION}")

success = failed = 0
for i, r in enumerate(targets):
    payload = json.dumps({
        "key": r["token"],
        "max_budget": MAX_BUDGET,
        "budget_duration": BUDGET_DURATION
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/key/update", data=payload,
        headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        success += 1
    except Exception as e:
        failed += 1
        print(f"  FAIL: {r.get('key_alias')}: {e}")
    if (i+1) % 50 == 0:
        print(f"  Progress: {i+1}/{len(targets)}")

print(f"Done: {success} success, {failed} failed")
PYEOF
```

### 3. 重置所有目标 key 的 spend（关键！）

设置 `budget_duration` 后，LiteLLM 拿当前 `spend` 与 `max_budget` 比较。`spend` 是**历史累计值**，不会因为新设 `budget_duration` 而自动归零。

**必须重置所有目标 key 的 spend 为 0**，否则历史消费会算入新周期，导致：
- 累计 $89 的 key 只能再用 $11 就触发 $100 限额（实际今天才用了 $11）
- 用户看到 "Budget exceeded" 但完全不理解为什么

```bash
python3 << 'PY'
import json, urllib.request

MASTER_KEY = "<master_key>"
BASE_URL = "http://127.0.0.1:4000"

with open("/tmp/spend_keys.json") as f:
    rows = json.load(f)

targets = [r for r in rows
           if (r.get("key_alias") or "").startswith(("claude-code-","cursor-"))
           and (r.get("spend") or 0) > 0]

print(f"Resetting spend for {len(targets)} keys...")
for alias, token in [(r["key_alias"], r["token"]) for r in targets]:
    payload = json.dumps({"key": token, "spend": 0.0}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/key/update", data=payload,
        headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"  FAIL: {alias}: {e}")
print("Done")
PY
```

### 4. 验证

```bash
python3 << 'PY'
import json, urllib.request

MASTER_KEY = "<master_key>"
req = urllib.request.Request(
    "http://127.0.0.1:4000/spend/keys?limit=600",
    headers={"Authorization": f"Bearer {MASTER_KEY}"}
)
rows = json.loads(urllib.request.urlopen(req, timeout=30).read())

correct = blocked = total = 0
for r in rows:
    a = r.get("key_alias") or ""
    if a.startswith("claude-code-") or a.startswith("cursor-"):
        total += 1
        if r.get("max_budget") == 100.0 and r.get("budget_duration") == "1d":
            correct += 1
        if (r.get("spend") or 0) > (r.get("max_budget") or float("inf")):
            blocked += 1

print(f"Total: {total}, Correct: {correct}, Currently blocked: {blocked}")
PY
```

### 5. 移除预算限制

恢复为不限额度：

```python
payload = json.dumps({
    "key": "<token>",
    "max_budget": None,
    "budget_duration": None
}).encode()
```

批量移除参照步骤 2 的脚本结构，将 `MAX_BUDGET` 和 `BUDGET_DURATION` 改为 `None`。

## 注意事项

- `/key/update` 是**热更新**，无需重启 LiteLLM proxy；但 **auth 层 key 缓存约 60s 才失效**
  （2026-08-21 阿里云实测：改 spend 后立刻发请求，预算门/90% 预警门读到的还是旧 spend）。
  写完脚本化验证/冒烟必须 sleep ≥70s 再断言；生产场景用户无感（下一分钟自然生效）
- 首次设置预算后**必须重置所有目标 key 的 spend 为 0**（步骤 3），否则历史累计消费会算入新周期
- `carher-*` key 关联 bot 实例，修改前确认业务影响
- 脚本中 `MASTER_KEY` 不要硬编码到日志或聊天中，运行时从 Secret 获取
- 每次操作约 500ms/key，556 个 key 约需 4-5 分钟
