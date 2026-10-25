---
name: cli-anything-litellm
description: >-
  只读查 198 上跑着的 LiteLLM：用户报错的真实原因（`db req`）、某组失败率（`db failure-rate`）、线上版本源码（`src`）、
  每个补丁接管了哪个上游函数（`patches`）、生产库（`db`）。入口 `cli-anything-litellm`，真身 `tools/cli-anything-litellm/`。
  `references/` 里是读 1.105.1 源码得出的工作原理和逐行深读（带 file:line，标了哪些在生产上实测过）。
  Use when 用户说「用户贴了 req:xxxx / API 异常 / rate limit exceeded 是什么原因 / 某组失败率多少 / 过载被兜住没有 /
  看 litellm 源码 / 这个函数在哪 / 哪个补丁改了 X / 补丁到底生没生效 / 三条车道补丁一样吗 / router_settings 改了不生效 /
  fallbacks 在 DB 还是 yaml / 查 litellm 库 / SpendLogs 少行 / 明细和日表对不上 / 熟悉 litellm 代码」。
  线上补丁分几层、各在哪见 [[litellm-198-patch-layer-map]]；装/换补丁见 [[litellm-198-router-patch]]；
  升级见 [[litellm-198-version-upgrade]]。
---

# cli-anything-litellm：只读查 198 的 LiteLLM

线上 litellm = fork 的 tag + 我们四层补丁 + 一个管着大半路由配置的 DB。三样都只认现场：源码读**线上版本那个 tag**，
补丁看**在线 pod 里函数对象被谁换了**，配置看 **DB 行和 yaml 谁覆盖谁**。

```bash
uv tool install --editable tools/cli-anything-litellm     # 已装就跳过；editable，改代码即生效
cli-anything-litellm db req 2d910055                       # 最常用：用户贴的报错，真实原因是什么
```

全部只读：ssh key + `sudo -n` 连 `cltx@10.68.13.198`；psql 开只读事务 + 语句超时（写会被 Postgres 拒，有测试）。
不读也不需要任何密码。加 `--json` 给脚本用。

## 先看这里：用户报错

🔴 **用户贴「API 异常 (req: xxxxxxxx)」或「rate limit exceeded」，第一步 `db req <8位前缀>`。**
客户端看到的报错被 `error_sanitize` 打过码，原文只在 SpendLogs。10-20 那句「rate limit」其实是上游
`servers are currently overloaded`（`streaming_output_backfill` 为了触发换号把它改写成 RateLimitError），
照字面去查 key 预算 / 7 天配额就全查错了。查不到时会列三种可能（超出 6 天保留期 / 被 SpendLogs 丢行 / 前缀打错）。

接着看兜住没有：`db failure-rate <组...> --minutes 60`。按客户端请求的组（`original_model_group`）归类，
被兜底救到别的组算原组成功（`rescued` 列）。这两列没索引，**窗口上限 90 分钟**（3 小时、5 小时实测超时）。

## 命令

| 想知道 | 命令 |
|---|---|
| 用户报错的真实原因 | `db req 2d910055 [--hours 48]`（8 位前缀或完整 id） |
| 某几个组现在失败率、多少是过载、多少被兜住 | `db failure-rate chatgpt-gpt-5.6-luna gpt-6-astra [--minutes 60 --bucket 10 --needle overloaded]` |
| 线上版本里某个函数长什么样 | `src find Router.should_retry_this_error --path litellm/router.py` → `src show litellm/router.py:7915` |
| 换个版本看（升级前对比） | `src find _is_cooldown_required --tag v1.106.0` |
| 一个请求经过哪些步骤、每步挂了哪些补丁 | `src path` |
| 哪个补丁接管了哪个上游函数 | `patches targets --lane prod`（28 个符号；多层叠的列整条链） |
| 三条车道补丁一样吗 | `patches compare` |
| 某个补丁是干嘛的 | `patches purpose dead_deployment_retry`（说明文字，**不是判据**） |
| 我连的是生产库吗 | `db anchor`（prod 约 523 模型行 + 129 条 DB fallbacks；dev 只有 3 行、没 fallbacks） |
| 某个路由配置到底谁说了算 | `db config-owner`（yaml 写了的键盖 DB；见下） |
| 库里有什么、和 schema 对得上吗 | `db catalog` |
| SpendLogs 有没有少行 | `db spend-reconcile 2026-10-20 [--model-group 'kiro-claude-*']` |
| 明细能查多远 | `db retention`（SpendLogs 约 6 天；日表从 05-02 起） |

退出码：0 正常；1 后端出错或输入被拒（如 `--lane` 和 `--tag` 对不上、callback import 失败）；2 用法错。

## 最容易踩的三条

1. **路由配置：yaml 写了的键盖 DB，yaml 没写的归 DB。** 现在 `fallbacks`(129)、`model_group_alias`、`num_retries`、`timeout`
   都归 DB。🔴 往 yaml 的 `router_settings` 加 `fallbacks` = 把 DB 那 129 条整个盖掉，rollout 全绿看不出来。
2. **算请求量和花费以 `LiteLLM_DailyUserSpend` 为准**，别数 SpendLogs：10-25 17:10 前的明细丢过行（见下，已修），日表另走一条路不受影响。
3. **补丁装没装，看 `patches targets`**，别信 import 成功、文件 sha、CM 名：指纹守卫拒装时这三样全绿。

## 读源码得出的结论（10-20，按影响排序）

工作原理（配置优先级、挑腿顺序、重试/冷却/兜底、计费写库、库的样子）⇒ `references/how-litellm-works.md`。
六个子系统的逐行深读 ⇒ `references/v1.105.1/01~06-*.md`；**哪些在生产上量过 ⇒ `references/v1.105.1/00-verified-on-prod.md`**；
离线跑真 Router 的实验 ⇒ `references/v1.105.1/router-experiments/`。

| # | 问题 | 证据 | 现状 |
|---|---|---|---|
| 1 | **SpendLogs 每天少 1~2% 行 = 我们自己的两层清洗补丁**（详见下一节） | 实测 | ✅ 10-25 已修上线（`87515f0`，prod+gray） |
| 2 | **`/v1/responses` 流中途失败，出错的号不进冷却**：`streaming_output_backfill` 换掉了 `_handle_failure`，只跑 async 失败处理，冷却挂在 sync 那条上。它防的事件循环卡死，上游 1.105.1 已自己修了 | 实测 + 离线实验 | 🔴 未修。删那段之前先在 gray 用死腿做「流中失败 → 该腿被冷却」阳性对照 |
| 3 | **`chatgpt_responses_normalize` 的 deployment hook 永不触发**：传进来的是 `CallTypes` 枚举，`str()` 得 `'CallTypes.aresponses'`，门控 `str(call_type) in (...)` 恒假。`deepseek_responses_adapt` 08-03 记过同一个坑并改了，这个文件没改 | 实测 | 🔴 未修。改成取 `.value` |
| 4 | callback 上的 `async_pre_routing_hook` 是死代码：Router 只调自己的方法，不遍历 callbacks | 读码 | 别再往这个钩子里写东西 |
| 5 | **一个请求打上游的总次数没有上限**，同组换号和跨组兜底共用 `max_fallbacks`。离线：40/20/10 腿全 500 = 84 次 | 离线实验 + 10-20 事故 | ✅ prod/gray 已上 `same_group_failover_cap`（同组≤3）+ `ROUTER_MAX_FALLBACKS=10`，同形状 84→60 次。`num_retries_per_request` 仍未设 |
| 6 | **per-key `router_settings.fallbacks` 整体替换全局兜底**，不是合并（764 把 key 有）；这条路还跳过「Model is blocked」的 403 | 读码（两份独立确认） | 改 key 兜底时把要保留的全局兜底一起写进去 |
| 7 | `fallbacks` / retry policy 的 key 必须是客户端发来的名字（alias）；配了 `context_window_fallbacks` 后没列的组超长直接失败 | 读码 + 离线实验 | 24 个 alias 未逐条核 |
| 8 | **几个补丁已是空操作**：`encrypted_content_degrade_strip` · `budget_notice` 里的 max_budget_limiter patch · `streaming_output_backfill` 的空流升格和 failed_deployment 盖章 · anthropic logging 覆盖件 | 实测（第一个）/ 读码 | ✅ `anthropic_passthrough_pingfix`、`log_truncate` 已从 prod/gray 摘掉；其余下次升级删 |
| 9 | `responses_aclose` 还要留（上游迭代器仍没有 `aclose`）；但 chat→responses 桥接路径的内层连接没人关 | 读码 | 桥接路径另查 |
| 10 | 原生 `/v1/messages`（kiro）不检测截断流：上游断了客户端收到 200 + 半截消息 | 读码 | — |
| 11 | kiro 计费 = 全价 input × 1.3；6 小时 15257 行 0 行报了 cache_read | 实测 | 看 kiro 上游有没有真缓存 |
| 12 | `#43049` 回退的代价：鉴权只看请求名在不在 `key.models`，alias 指向哪不查；key 所有者能改 alias | 读码 | 列入 #43049 长期解；198 上谁能改未核 |

**核过、不成立的**（别再当风险报）：30s 重载往 callbacks 里追加字符串（实测 40s 前后长度不变）·
预算重置并发导致负 spend（实测只有 3 把 -5.7e-14 的浮点尘）· keepalive 让每个 chunk 丢 contextvar（实测生产 keepalive 关着）。

## SpendLogs 少行 = 我们自己的清洗补丁（✅ 10-25 已修）

**10-25 修法**（`87515f0`）：两层的 `_scrub_str` 改成按 JSON 词法从左往右扫的一条正则 —— `\\` 整体保留、成对代理转义（emoji）保留，只删 `\u0000` 和孤立代理转义。测试 `k8s/litellm-callbacks/tests/test_spendlog_scrub.py`（旧实现照抄做阳性对照必须红）。上线验收：同一条带字面 `\u0000` 的请求，上线前 prod 丢、gray 落；上线后 prod/gray 4/4 落；prod 4 pod 上线后 `dropping spend log row` = 0。**10-25 17:10 北京时间以前的明细仍然缺**（丢掉的行补不回来），对那段时间的账还是以日表为准。
⚠️ `litellm-198-callback-update.py verify` 验 `sitecustomize.py` 恒报「sha 读不到」：它挂在 site-packages 不在 `/app/`，那是量具的假红，要自己 `sha256sum /app/.venv/lib/python3.13/site-packages/sitecustomize.py`。

**「是不是被别人反复覆盖」10-25 核过 —— 不是**：198 上留存的 118 代车道 CM 里这两个文件从来只有旧版，正确版本今天之前一次都没上过；
10-19~10-24 每天都丢 0.5~1.4%（明细保留 6 天，再早量不到）。每次「修 SpendLogs」修的都是别的环节（04-27 NUL、07-24 每 worker 生效、09-19 误判传输错误），
同一个删法一直在。往后的风险是旧拷贝被拿去发版 ⇒ 三处指纹闸门（见 [[litellm-198-version-upgrade]] §6）。

**每日对账告警（10-25 上线）**：198 root cron 每天 09:30（北京）跑 `scripts/litellm-198-spendlog-reconcile-alert.py`
（装在 `/usr/local/sbin/`，日志 `/var/log/litellm-spendlog-reconcile.log`），对昨天 UTC：日表请求数 vs SpendLogs 行数，
全库 >0.3% 或单 key >2% 且 ≥50 行 ⇒ 飞书卡片给刘国现；明细被清表 / 脚本出错也报；顺带核三车道 + 共享 CM 清洗代码是不是旧版
（兜 `helm rollback` / 手工切旧 CM）。改阈值先 `--date <过去某天> --dry-run` 对过去几天量一遍。

以下是 10-20 的原始定案：

10-20 当天日表 236299 次 vs SpendLogs 232778 行，缺约 1.5%，集中在少数长会话 key。**计费不受影响**（日表另走一条路）。

**原因**：`PrismaClient.jsonify_object` 上两层清洗（`sitecustomize.py` 的 spendlog sanitizer、`null_byte_sanitize.py`）都在
**已经序列化好的 JSON 文本**上用正则删 `\u0000` 和孤立代理对。对话里一旦出现**字面的**反斜杠 u（代码里的 `'\u0000'`、
正则 `r'[\ud800-\udfff]'`），序列化后是 `\\u0000`，正则删掉后 6 个字符，剩下一个孤零零的 `\` ⇒ 非法转义 ⇒ prisma 拒收 ⇒
上游把这一行丢掉。会话之后每一轮都带着这段历史 ⇒ 之后全丢。

**证据**：
- gray pod 逐层跑同一列：上游原版合法；加 sitecustomize 就 `Invalid \escape`；再加 null_byte_sanitize 也是 ⇒ **两层都要改**。
- 一个会话：03:01:28Z 第一次出现字面 `\u0000`，SpendLogs 里该会话主线程的行停在 03:00:58；同 key 同组当天日表比 SpendLogs
  多 421，正好是 03:01 之后主线程的消息数 421。
- gray 有流量但 0 次丢行（只有带字面 `\u0000` 的长会话才丢）。
- 生产日志里的 `dropping spend log row Postgres rejected` 点名字段那段被 `MAX_STRING_LENGTH_STDOUT_LOG=4096` 截掉了，光看日志找不到原因。

**修法候选**：在解析后的值上清洗、再序列化；或只删真正的 `\x00` 和代理对码点，不动文本里的 `\\u....`。两层都要改。

## 扩展这个 CLI

- 真身 `tools/cli-anything-litellm/`；设计说明 `LITELLM.md`；测试计划/结果 `cli_anything/litellm/tests/TEST.md`。
- 新的 SpendLogs 查询：没索引的列（model_group / status / metadata）一律带短 `startTime` 窗口并在 CLI 上限死；
  先拿真数据量一次耗时再定上限。SpendLogs 只按索引查：`startTime`、`(api_key, startTime)`、`request_id`、`session_id`、
  `litellm_call_id`、`end_user`。整天 `group by api_key` 会超时，照 `spend-reconcile` 的写法：先从日表挑 key，再逐个点查。
- 新的被 patch 符号：改 `probes/patch_targets.py` 的 `TARGETS`，在 `core/patches.py` 的 `PURPOSE` 补一行；
  prod 的 callback 变了，`test_198_core.py::_LIVE_198_MODULES` 也要改（字面量，故意不从 PURPOSE 推）。
- 判「补丁装没装」的三个坑（探针已处理，自己写探针时注意）：`functools.wraps` 会抄走 `__module__`，别按它判；
  绑定方法每次取都是新对象，比身份要比 `__func__`；sitecustomize 在探针之前就跑了，只能靠哨兵属性 / `__wrapped__` 认。
- 新的 DB 视图：在 `core/db.py` 加函数，用户输入一律过 `_lit()` 白名单；先 `explain` 确认走索引。
- 测试：`CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/litellm/tests -v`（`test_198_e2e.py` 连真 198，连不上就失败）。
- 本机 `backend status` 报的 1.79 是本机的包，不是 198 的版本。
- 🔴 别用 cli-anything 的 `skill_generator.py` 重生成包内 `skills/SKILL.md`，会冲掉手写内容。
