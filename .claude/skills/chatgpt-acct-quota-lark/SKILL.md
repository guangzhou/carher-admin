---
name: chatgpt-acct-quota-lark
version: 5.2.0
description: "查看 198 + 阿里云 chatgpt-acct 上游配额 + zerokey 累计消耗并写入飞书多维表格。入口 `scripts/chatgpt-acct-quota-refresh-lark.sh`。用户说"查看配额""写入飞书""更新配额表格""表上 acct-N offline 了"时使用。「表上没有 acct-N」用 `--find N`(纯读,多半是视图滤掉的不是没写)；判 pro/free 只认实探,`plan` 列空=探不到≠free。"
---

> 真身在仓内 `.claude/skills/chatgpt-acct-quota-lark/`（2026-10-07 从 `~/.claude/skills/` 同名独立副本迁入，
> 迁入时两份 sha256 相同；全局那份按 CLAUDE.md 不动，此后会漂，以仓内为准）。

## ⛔ 判 acct 档位（pro/free）：只认实探，`plan` 列空 = 探不到，**不是 free**

2026-10-10 我把 acct-227 报成「已掉 free」。依据是 pod 内 id_token 的
`chatgpt_plan_type=free` —— **那是 10-05 签发时刻的冻结快照**，该号 token 已过期、
实探拿不到档位，于是快照成了唯一可见的"档位"。用户纠正「227 是 pro」。
10-11 实跑 refresh grant：**200 / `plan=pro` / `sub_plan_live=chatgptpro` /
`has_active=true` / 订阅到 11-09 / 7d 配额 0%** —— 账号全健康，只是 token 过期。

**合法判据只有一条**：in-pod refresh grant 拿新 token → 实探 `/codex/usage` 读
`plan_type`。三个诱人的替代判据**全部实测证伪**：

| 替代判据 | 为什么错 |
|---|---|
| id_token / access_token 的 `chatgpt_plan_type` | 签发时刻快照，token 一过期就永久冻结在那个值 |
| SpendLogs 里 pro-only 模型（gpt-6 族）有 success ⇒ pro | **实探确认 free 的 acct-120 也有 591 次 gpt-6 success**。只能当排序信号 |
| pod 里 `.bak-*` 旧快照是 pro、新换的那份是 free ⇒ 真掉档 | 227 正是这形状，实探却是 pro。**换新凭据后仍显示 free ≠ 真的 free** |

另两个坑：统计 pro-only 流量要按 **`model`（真正服务的模型）** 而非 `model_id`（腿名）
—— 腿 `...-gpt-6-sol` 的行里 `model` 可能是 `chatgpt-gpt-5.6-luna`。表上
`sub_left=expired` 若 `sub_src=401:*`，那也是冻结快照，不是真到期。

批量找同形号（**只读**，不打 grant）：
```bash
python3 scripts/chatgpt-acct-plan-misverdict-audit.py        # 或 --json
```
2026-10-11 全量扫出 **35 个**与 227 同形（JWT 说 free + 实探 401）。
救号走 skill `chatgpt-acct-token-expiry-triage`——RT 成功即轮转**必须同轮写回**，
没有只读版本，属凭据写入，**先问用户**。

## ⛔ 「表上没有 acct-N」先用 `--find`，别重跑刷新

2026-10-10 用户问「为什么没有 acct-227，它明明在循环池里」。真相：**全表 190 行里
它有两行**（198 + aliyun），是**视图**滤掉的 ——
- `online` 视图 filter `status==ONLINE AND sub_left!=expired` ⇒ 227 的 198 行
  `status=ONLINE` 但 `sub_left=expired`（且那个 expired 来自冻结快照）
- `预下线` 视图 filter `7d% > 90` ⇒ 227 的 `7d%` 为**空**，而**空格子不满足任何
  数值比较**（10-10 实测 164 行因此不可见）

**视图不是表，空格子在视图里和「不存在」同形。** 重跑刷新只会写回同样的行，答不了
这个问题。

```bash
./scripts/chatgpt-acct-quota-refresh-lark.sh --find 227   # 纯读，不取数/不动表/不抢锁
```
印：全表命中几行、每行关键列、**被哪个视图滤掉 + 具体哪条条件不满足**。
每轮刷新也会印 `[views]` 段（各视图藏了多少行、哪些是因格子为空被藏）。

## ⛔ 用户说「acct-N offline 了」→ 先看是哪一行

同一个编号在表上可能**两行**：`site=198` 和 `site=aliyun`。迁到 198 的号，阿里云那行是有意 scale=0 的旧副本。
2026-10-07 用户说「198 上 acct-229 offline 了」，实际 198 那行 ONLINE（10h 1018 次全 success），
OFFLINE 的是阿里云那行。现在这类阿里云行 `verdict` 写 **`OFFLINE(198在服)`**（`to_lark.mark_migrated_to_198`），
stderr 单列一段「已迁 198」。

按编号拉全部行再下结论：
```bash
lark-cli api POST /open-apis/bitable/v1/apps/YdrZb5xaNaziDHssawGcDpd1nqf/tables/tblsw3E1zZ2IuBbW/records/search \
  --data '{"filter":{"conjunction":"and","conditions":[{"field_name":"acct","operator":"is","value":["acct-229"]}]}}'
```
198 那行真有问题，才进诊断（pod / 叶子实打 / 腿 / SpendLogs，见 skill `chatgpt-pool-198-add-acct` 第 1、7 段）。

# chatgpt-acct-quota-lark

查两个集群的 chatgpt-acct 上游配额消耗 + zerokey 累计消耗，写入同一张飞书多维表格。

**两侧现在都是 in-pod 实探 `/codex/usage`**（198 于 2026-08-05 从"读引擎快照"改过来）。

- **198**（`litellm-product` ns）—— 有 quota-rebalance 引擎，但配额列**不再读它的
  state.json 快照**；引擎那份挪到 `state_tier` 列并排放，供对照
- **阿里云**（ACK `carher` ns）—— 无引擎，一直是实探

两侧同表，靠 `site` 列区分。

## ⛔ 用户手动问配额 → 必须实探，不许交快照

2026-08-05 被当场指出「你是不是读的缓存」。当时 198 侧的 `upstream_tier`/`7d%`/
`reset` 全部来自 `state.json`，`probe_age` 列实测有 `4h46m` 这种。**跑完脚本 ≠
看过官方。** 现在默认路径就是实探，但两件事仍要守：

1. `live_probe` 列若是 `FETCH_FAIL`，说明实探链路挂了、该行退回了快照 —— 汇报时
   必须说这一句，不能当实探值讲
2. `/codex/usage` 是**只读**探针（引擎自己每 5min 就在打），不属于「在真号上做有
   代价的探测」；有代价的是实跑 refresh-grant / OAuth

## 何时使用

- 用户要查 acct 配额并写入飞书
- 用户要更新飞书配额表格
- 用户说"配额写到飞书""更新配额多维表格"

## 行宇宙 = state.json ∪ deploy（两侧同口径）

2026-08-05 改。原来 198 侧只列 `state.json` 的 key，于是**引擎不认识的号在表上根本
不存在** —— 实测 198 有 **74 个 deployment** 而 state 只有 60 个 key。差的 14 个
（`acct-1/3/4/5/6/13/14/16/17/22/23/24` scale=0，**`acct-2`/`acct-15` spec=1 正跑着**）
里，acct-2 官方回 401 `could_not_parse`、acct-15 的 auth.json 里根本没有
`access_token`。这两个是「pod 绿着、官方打不通、引擎看不见」的三重黑洞。

这些行的 `status` 是 **`UNTRACKED`**，与 `ZOMBIE` 刻意分开：ZOMBIE 是引擎清理后的
残留（建议清行），UNTRACKED 是引擎压根没纳管的活号（**撞顶不会被摘、401 不会被修**），
处置方向相反。`take` 印 `?` 而不是 `-` —— 引擎答不了这个问题。

好处之一：新加的号自动进表，不用改代码（2026-08-05 的 135-139 就是这样进来的）。

## tier 词表（两个 view 已对齐）

| 取值 | 判据 | 含义 |
|---|---|---|
| `7D_CAP` | `allowed=false` 或 `limit_reached=true` | 官方**已拒新请求** |
| `7D_BRIM` | `used_pct>=100` 但官方仍放行 | 用满了**还能服务** |
| `7D_HIGH` | `used_pct>=90` | 快满 |
| `HEALTHY` | 其余 | 有余量 |
| `401 需re-OAuth` | 探针 401 | 字面值不许改（见下） |
| `SCALED_DOWN` / `NO_POD` / `PROBE_ERR` | — | 三种"没数值"的原因各自可区分 |

⚠ **`used_pct=100` 与 `allowed=false` 是两个不同的事实。** 2026-08-05 实测 40 个探通
的号里 35 个 `used_pct=100`，其中 **7 个（78/90/92/115/118/119/121）仍
`allowed=true`** —— 官方还在放行。压成一个"撞顶"标签会导致对这 7 个做下线动作，净损失。

⚠ `401 需re-OAuth` 这个**字面字符串**不许改：`auth_plane_diagnostics()` 按它分桶
（认证没进 PVC / repair_frozen / 需实跑 OAuth）。改了整段诊断会**静默失效**——不报错，
只是永远空。

## ⚠ 字段是 `rate_limit`（单数）

2026-08-05 我临时写探测脚本时抄成 `rate_limits`（复数）→ 40 个号 `used_percent` 全
取到 `None`，差点报成「官方返回空，表上的 100% 是错的」。**抓了原始响应才发现是自己
的 bug，而表上那个 100% 被实探证实是对的。**

加字段前先 dump 一份原文：

```bash
# 结构：rate_limit.primary_window.used_percent / .allowed / .limit_reached
#      additional_rate_limits[0] = GPT-5.3-Codex-Spark（独立 7d 子窗口 → spark% 列）
#      rate_limit_upsell 只在 limit_reached 时出现
```

## 步骤

### 一条命令：两侧并发取数 + 写表

```bash
./scripts/chatgpt-acct-quota-refresh-lark.sh             # 取数 + 写表
./scripts/chatgpt-acct-quota-refresh-lark.sh --dry-run   # 取数 + 自检，不动表（参数原样透传给 to_lark.py）
```

wrapper 比直接跑 `to_lark.py --run-quota --run-quota-aliyun` 多做的事：

| 做什么 | 为什么 |
|---|---|
| 强制两侧都取数 | 漏 `--run-quota-aliyun` ⇒ delete-all 后只写回 198，阿里云行静默消失 |
| 先删 `/tmp/chatgpt-acct-quota*-rows.json` | 取数失败时不拿上一轮的陈旧 json 写表 |
| **互斥锁** `/tmp/chatgpt-acct-quota-refresh-lark.lock`（mkdir+PID 判活） | 两轮重叠各自 delete-all/recreate ⇒ 表里每个号两行；撞锁 `rc=3` 退出，持锁进程死了自动接管 |
| 预检 python3/ssh/lark-cli/`~/.config/jms/key.json` | 缺依赖 `rc=2`，不白等 90s |
| 整轮输出存 `/tmp/chatgpt-acct-quota-refresh-lark.<ts>.log` | `[sub]/[auth]/[aliyun]` 自检要回头看 |
| 打 wall time + 基线 | 基线 **93~100s**（2026-10-07，190 行 = 198:171 + aliyun:19）；>200s = 有腿在空等超时 |

两侧是不同主机（198 走 `ssh JSZX-AI-03` → 再 `ssh cltx@10.68.13.198` 才有 kubectl，
阿里云走 `jms → k8s-work-226`），脚本里已经并发跑，wall-clock 取决于慢的那侧。

**只要 198**（阿里云链路抽风时的退路）：去掉 `--run-quota-aliyun` 即可，
脚本会明确打印「本次只写 198 行」而不是静默少写。

### 分步（调试用）

```bash
bash scripts/chatgpt-acct-quota.sh --json-rows --quiet          # → /tmp/chatgpt-acct-quota-rows.json
bash scripts/chatgpt-acct-quota-aliyun.sh --json-rows           # → /tmp/chatgpt-acct-quota-aliyun-rows.json
python3 scripts/chatgpt_acct_quota_to_lark.py \
  --aliyun-rows /tmp/chatgpt-acct-quota-aliyun-rows.json
```

⚠ 分步时注意 rows JSON 是**上一次**的残留文件。两侧的 `generated_at` 会打在
`[parse]` 行里，写表前扫一眼，别把昨天的阿里云数据配今天的 198 数据。

### 人看的文本表（贴回群里）

```bash
pkill -f "jms proxy" 2>/dev/null                    # 清残留会话，占着 JumpServer 席位
bash scripts/chatgpt-acct-quota.sh --summary --raw --quiet     # 198
bash scripts/chatgpt-acct-quota-aliyun.sh --summary            # 阿里云
```

198 那份副本落 `/tmp/chatgpt-acct-quota-last.txt`，自带 BEGIN/END frame + rows
自检，原样贴回不要追加 markdown。

## ⏱ 成本模型：慢的不是干活，是 jms 链路

2026-08-04 实测（远端分阶段计时）：

| 阶段 | 耗时 |
|---|---|
| list_deploys | 1.5s |
| attach_pods | 0.1s |
| sample_serving | 23.2s（其中 20s 是**刻意**的采样间隔）|
| gather_auth | 4.0s |
| spend_window | 5.4s |
| **远端合计** | **34.2s** |

而整轮曾经要 12–15min —— **差额全部是 jms 会话开销**，且这条链路吞吐会塌
（同一条命令可能秒回、也可能爬 20min）。所以：

- **每开一个 jms 会话就是抽一次奖**，常规路径已压到 **1 个会话**
  （同一条命令里 校 sha → 跑 view → 打包吐出）
- view 缓存在 226 上，sha 一致就跳过上传；**改了 view 才会多花 ~7 个会话**
- 曾经那版「setsid 脱离 + 每 45s 轮询」把会话数抬到 ~20 个 = 抽 20 次奖，
  比单会话更慢也更脆，**别再改回去**
- 慢的时候先看 stderr 里的 `[t] xxx=Ns` 行判断是远端在重试还是链路在爬，
  别凭感觉优化

## 已见失败形态（2026-08-20~25 十几轮实跑的分布，都重跑即愈）

任一侧取数失败都会 fail-safe **不动表**（先取数后删表的顺序保证），所以下面这些
都只需重跑，不用救数据：

| 形态 | 特征输出 | 处置 |
|---|---|---|
| jms 链路超时/中断 | `rc=241` / `rc=255 Operation timed out` / aliyun `rc=8 远端无 RC 标记` | 重跑 |
| JMS 认证抖动 | `Permission denied (password,publickey)` | 重跑 |
| **jms cache 目录缺失** | `FileNotFoundError: ~/.config/jms/cache.tmp → cache.json` | `mkdir -p ~/.config/jms` 后重跑（已白跑过两轮各 15min，先建目录再跑） |
| aliyun view 上传校验失败 | `上传校验失败(静默截断?) want=<sha> got=` | 重跑（sha 不符会自动重传） |

两个**写表侧**的已见形态（均未定根因，先记特征别瞎归因）：

- `[delete]` 计数偶发翻倍（165→330 / 175→350），最终 `[verify] OK` 行数正确 ——
  已排除第二写手（本机/JSZX-AI-03 crontab、repo 全 grep 都没有别的 Lark 写入方）；
  剩余假设是 record-list 删后读一致性延迟导致同批 ID 被二次删除。**无害**，
  verify 过了就不用管；要钉根因得 diff 两轮 list 的 ID。
- 一次 `[verify] MISMATCH: 0 records in table, expected 175`（写入报 created=175
  但读回 0）—— 重跑即好。读回也走 record-list，同样疑似读一致性延迟。
  MISMATCH 时**表内容不可信，必须重跑**，不能当成功。

## 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--run-quota` | false | 先取 198 |
| `--run-quota-aliyun` | false | 先取阿里云（送 view 到 226 上跑）|
| `--aliyun-rows` | "" | 直接吃现成的阿里云 rows JSON |
| `--dry-run` | false | 只取数 + 自检，不建列不删不写 |
| `--find <N>` | "" | **纯读**：`acct-N` 在不在表上、被哪个视图滤掉（不取数/不动表/不抢锁；`227` 或 `acct-227` 都行）|
| `--base-token` | `YdrZb5xaNaziDHssawGcDpd1nqf` | 飞书 Base token |
| `--table-id` | `tblsw3E1zZ2IuBbW` | 目标数据表 ID |
| `--skip-delete` | false | 跳过删除旧记录（追加写入） |
| `--cards-json` | "" | reset 卡数据（`chatgpt-acct-reset-cards.py` 输出，仅 198）|
| `--prune-fields` | false | 删改名后留下的僵尸列 ——**会连数据一起删** |

`chatgpt-acct-quota.sh` 的 `--json` 是透传 state.json 原文，`--json-rows` 才是
结构化行。两者语义不同，别混。

## ⛔ 新增枚举值必须先补单选列 options，否则清空整张表

2026-08-05 事故：给 `status` 加取值 `UNTRACKED`、给 `take` 加 `?`，而这两列在飞书是
**单选**，options 只有 `ONLINE/OFFLINE/PAUSED/SLOW` 和 `yes/-`。流程是
建列 → **删全表** → 批量写，于是：

```
[delete] total deleted: 70
[create] ERROR ... code 800030005 "not_found"
[create] total created: 0
[WARN] created 0 != parsed 84        ← 事后才报,救不回数据
```

**表被清空。** 已加 `ensure_select_options()`，在 `ensure_fields` 之后、
`delete_all_records` **之前**跑：把本次要写的值求差集补进 options，补不上就
`exit 6` 不动表。

- `+field-update` 是 **PUT 全量语义**，必须带上原有 options（否则等于删掉它们）
- hue 只接受 11 个：`Red Orange Yellow Lime Green Turquoise Wathet Blue Carmine
  Purple Gray`。实测 `Violet` 被拒 `Invalid enum value`
- `ensure_fields()` 只保证列**存在**，不保证列**接受这些值** —— 两件事
- 当前单选列：`status`(ONLINE/OFFLINE/PAUSED/SLOW/QUOTA/PROBE_ERR/ZOMBIE/UNTRACKED)、
  `take`(yes/-/?)

**通用教训：任何"先破坏后重建"的流程，schema 兼容性检查必须挪到破坏之前。**
事后比 `created==parsed` 是发现问题，不是防止问题。

## 写表是「先删全表再重建」→ 取数必须先全部落地

`delete_all_records` 无条件清空整张表。若阿里云取数失败发生在删除**之后**，
结果是全表被清、只写回 198，那些行**静默消失**，而脚本还会报
`created==parsed` 的 OK。所以两侧取数都在动表之前完成，任一侧失败就直接非零
退出、不碰表。2026-08-04 阿里云那次 `Killed: 9` 就是靠这条兜住的。

## 表格字段（45 列）

**site**, acct, email, take, status, verdict, ready, refresh_err, device_code,
exp_gap, **upstream_tier, plan, live_probe, state_tier**, probe_age, auth_sync, pod_cred,
7d%, **state_7d%**, **spark%**, reset, main_n, main$, **main_n7, main$7**, codex_n, codex$,
**codex_n7, codex$7**, zk_n, zk$, zk_n7, zk$7,
zk_lat_avg, zk_lat_p95, zk_empty%, next_reset, restore, sub_until, sub_left,
**sub_src, will_renew**, reset_cards, cause, snapshot_at

- 2026-07-13 OpenAI 合并为单 7d 窗口 → 17 列迁至 16 列
- 2026-07-26 新增 zerokey 7 列 → 24 列
- 2026-07-27 新增 `snapshot_at` → 25 列
- 2026-08-01 新增 `state_age` / `auth_sync` / `pod_cred` → 28 列
- 2026-08-02 **加服务平面** `verdict`/`ready`/`refresh_err`/`device_code`/`exp_gap`；
  `tier`→`upstream_tier`、`state_age`→`probe_age` → 33 列。
  僵尸列用 `--prune-fields` 清（改名后飞书旧列不会自己消失，空壳并排显示最误导人）
- 2026-08-03 **接入阿里云**，新增 `site` 列并放在第一列 → 34 列
- 2026-08-05 **198 侧改实探**：新增 `live_probe`（实探结果）/ `state_tier`（引擎快照，
  供对照）/ `spark%`（GPT-5.3-Codex-Spark 独立子窗口）→ 37 列。
  `upstream_tier`/`7d%`/`reset` 语义由"引擎快照"变为"实探值"，`probe_age` 实探成功恒 `live`
- 2026-08-20 **订阅日期改实探**：`sub_until`/`sub_left` 从"冻结的 JWT claim
  `chatgpt_subscription_active_until`（签发时刻的到期日，续订后不变）"改为
  **in-pod 实探 `/backend-api/accounts/check/v4-2023-04-27`**（同 pod、同 codex
  headers，与 `/codex/usage` 同一次 exec），取 `entitlement.expires_at`。新增
  `sub_src`（该行订阅日期的来源标记，语义同 `live_probe`）+ `will_renew`（yes/no
  续订意向）→ 39 列。两列都是 **text**（非单选，无清表风险）。
  - 起因：acct-84 表上显示 `expired`，实况已续订到 2026-09-20、`will_renew=true`
    —— 旧口径读 JWT claim，**续订对表完全不可见**，得靠人工告知才更新。
  - acct-237 情形：`has_active=true` 但 `will_renew=false`（快到期不续），
    旧口径看不出"不续"，现在 `will_renew` 列如实呈现。
- 2026-08-25 **spend 双窗口 + 订阅专项报告**：新增 `main_n7`/`main$7`/`codex_n7`/
  `codex$7`（最近 7 天，与上游 7d 配额窗口同尺度）→ **43 列**；原
  `main_n`/`main$`/`codex_n`/`codex$` **保持 24h 口径不变**（近况信号，`idle_24h`
  等判据依赖它）。198 侧同一条 SQL 用 `FILTER` 双聚合（扫描仍只有 7d 一遍），
  阿里云 `spend_window()` 调两次。命名跟 `zk_n7`/`zk$7` 先例。
  同日 to_lark 加 **`[sub]` 订阅到期专项报告**（写表前必印，两个集群一起看）：
  已过期 / 7 天内到期（<7d，边界与 aliyun `sub<7d` 一致）/ `will_renew=no` 三桶
  逐行点名（含到期日+sub_src），另点名 sub_src≠live 的行数——那些行的日期是冻结
  快照，不反映续订。纯函数 `subscription_buckets()` 有单测钉边界。
- 2026-10-10 **`plan` + `state_7d%` 两列**（→ **45 列**，都是 text/number 非单选）。
  两列都在补同一个洞：**view 早就取到了数据，但没有列承载 ⇒ 表上看不到 ⇒ 读者去读
  更坏的替代品**。
  - **`plan`**（源 `plan_type`，实探 `/codex/usage`）：此前整张表**没有任何档位列**。
    于是 acct-227 实探 401 时，唯一可见的"档位"是 pod 内 id_token 里冻结的
    `chatgpt_plan_type=free` —— 我据此报「227 已掉 free」，用户纠正，10-11 实跑
    refresh grant 得 `plan=pro`、订阅到 11-09、7d 0%。详见下节「⛔ 判档位」。
    `state.json` 全 170 条**都没有 plan 字段**（实测），所以这列有值必来自实探。
  - **`state_7d%`**（源 `state_pct7d`，引擎快照）：`7d%` 只在实探成功时有值，实探
    401 时**刻意置空**（三分支逻辑）。于是「快照说 100% 但探不到」的行两列全空，
    与「真没量」同形，且**对 `7d% > 90` 这类视图结构性不可见**（10-10 实测 164 行）。

### `sub_src` 取值（语义同 `live_probe`）

| 值 | 含义 |
|---|---|
| `live` | 实探 `/accounts/check/v4` 拿到了到期日，`sub_until` 是官方现值 |
| `state` | 实探 200 但订阅字段缺失 → 退回 JWT 快照（旧口径值） |
| `off` / `no-pod` / `401:<kind>` / `ERR:*` / `FETCH_FAIL` | 探不到 → 退回 JWT 快照，此值标明为何探不到 |

`live` 之外的所有值都意味着 `sub_until` 可能陈旧（续订不可见）。**探订阅是只读 GET，
与 `/codex/usage` 同级别**，不属于「在真号上做有代价的探测」（那指实跑 refresh-grant
/ OAuth）—— **不触碰** `needs_live_probe` 桶（86/87/92）的 OAuth。

### `live_probe` 六个取值，语义互不重叠

| 值 | 含义 |
|---|---|
| `live` | 本次实探成功，7d%/reset 是官方现值 |
| `off` | `spec.replicas=0`，没 pod 可探 |
| `no-pod` | spec>0 但没有 Running pod |
| `401:<kind>` | 官方拒了，kind ∈ token_expired / invalidated / could_not_parse / no_access_token |
| `ERR:<code>` | 探针其他失败 |
| `FETCH_FAIL` | **整条实探链路挂了 → 本行退回引擎快照** |

`FETCH_FAIL` 是整张表可信度的分界。留空会被读成"这号没问题"—— 正是 2026-08-02
acct-121 那次的失效形态。

### `ERR:403` = CF 边缘随机拦截，探针已重试 + 退到 `wham/usage`（2026-10-07）

同一个 pod 连打 6 轮（acct-206/224/229）：`codex/usage` 与 `accounts/check` **各约 1/3 随机 CF 403 html**，
`wham/usage` 18/18 全 200。旧探针一次 403 就判死 ⇒ 13 个在服务的号被印成 `ERR:403 → UPSTREAM:PROBE_ERR`，
订阅日期退回 JWT 快照（229 显示 `sub_until=09-18 expired`，live 实为 11-06）。

`LIVE_USAGE_PROBE` 现在：
- `_get_retry`：**只对 403** 重试，最多 4 次、退避 1.5s×n；401 不重试（那是凭据结论）
- `codex/usage` 重试完仍 403 ⇒ 打 `wham/usage`（同一份 `rate_limit` 结构）
- `accounts/check` 同样走 `_get_retry`

效果：ERR:403 **13 → 0**，`sub_src=live` 23 → 39。**附带现形**：82/93/106 以前显示 ERR:403，
重试后是 `401:invalidated` —— 403 曾经盖住了真 401。⇒ 再见到 ERR:403 是重试 4 次都没过，
仍属"状态未知"（不是号坏），但已经罕见；成片出现时先怀疑出口 IP 被盘查。
回归测试 `test_live_usage_probe_retries_cf_403_and_falls_back_to_wham`。

## 四个平面各自独立，禁止交叉当对照组

| 平面 | 列 | 数据源 |
|------|-----|--------|
| 上游配额 | `upstream_tier` / `7d%` / `spark%` / `reset` | **in-pod 实探 /codex/usage** |
| 订阅 | `sub_until` / `sub_left` / `will_renew` | **in-pod 实探 /accounts/check/v4**（探不到退 JWT 快照，`sub_src` 标明）|
| 引擎快照 | `state_tier` / `status` / `cause` / `restore` | state.json |
| 凭证文件 | `auth_sync` / `pod_cred` / `device_code` / `exp_gap` | pod:/chatgpt-auth/auth.json |
| 服务 | `ready` / `refresh_err` | deploy `readyReplicas` + pod 日志 |

`verdict` 是合成的唯一总判定，优先级：
`?`(取数失败) > `OFFLINE`(spec=0) > `SERVING_DEAD`(ready=0/N) >
`DEVICE_CODE_HELL`(dcr 存在) > `REFRESH_DYING`(rfx>0) > `FLAPPING`(0<ready<N) >
`UPSTREAM:*` > `OK`。

服务平面刻意盖过上游：撞顶会被引擎 `scale=0` 摘掉，**服务平面坏掉却一直留在池里当
黑洞**（`Ready=False` 比 `scale=0` 更毒，它绕过 preflight）。

### `live_vs_state` 不能按 tier 字符串比

引擎写 `OFFLINE-7D`、实探写 `7D_CAP`/`7D_BRIM` —— **同一个事实的两种说法**（我换了
词表）。第一版直接比字符串，74 行报出 35 个"不一致"，全是改名噪声，真分歧被埋了。
现在先归粗类（FULL/HIGH/OK/DEAD/OFF）再比，同类内另要求 pct 差 ≥10 才算数值分歧。

## 阿里云侧：哪些列在该集群"无定义"

| 平面 | 198 | 阿里云 |
|---|---|---|
| 上游配额 | **pod 内实探 `/codex/usage`**（2026-08-05 起）| 同（一直如此）|
| 引擎快照 | `state_tier` 有值 | `state_tier` 恒 `no-engine` |
| 服务 | deploy `readyReplicas` 采样 + 日志 refresh-401 | 同手法 |
| 凭证 | pod auth.json + `/Data` 对照 | 只有 pod auth.json |
| spend | litellm-db SpendLogs | 同库（注意 model_id 分隔符不同，见下）|

阿里云**真正没有**的只有三项，已显式标注而非留空：

- `auth_sync` 恒 `no-local` —— 无 `/Data/chatgpt-auth` 镜像可比方向，不是"一致"
- `restore` 恒 `-` —— auto-revive 是 quota 引擎概念，阿里云无引擎
- `state_tier` 恒 `no-engine` —— **不存在**可对照的快照，与"取不到快照"是两件事

`live_probe` 在阿里云由 `to_lark.normalize_aliyun_live_fields()` 如实推导
（`off`/`live`/`ERR:probe`/`no-pod`）—— **不能因为字段是 198 新加的就留白**，
留白与 `live` 在表上同形，会让读者以为阿里云那半边没实探过，而真相恰好相反。

另有两组本期未接，脚本每次都会打 stderr 说明：`zk_*`（聚合是新工作）、
`codex_*`（阿里云只跑 gpt-5.5 一档）。**空白不代表 0**，这两者在表上同形。

`probe_age` 在阿里云恒为 `live`（本次实探）。留空会被读成"取不到"，
而真相是比 198 那侧还新。

### 阿里云 `verdict=OFFLINE(198在服)`：迁走的号

判据（`mark_migrated_to_198`，纯函数有单测）：阿里云行 `upstream_tier=SCALED_DOWN` **且** 198 同编号
`status=ONLINE` 且 `ready` 不是 off。只改 `verdict`（**text 列**，不碰单选 options）；`status` 仍是 `OFFLINE`
—— 往单选列加新值要先补 options，否则 delete-all 后写入全失败清空整表。
按**编号** join（scale=0 的阿里云行没 email）：编号在两侧漂过（210 曾是两个身份），所以这个值只表示
「198 同编号在服」，**不证明是同一个号**。198 侧也停着的（211-213）保持 `OFFLINE`，阿里云在跑的行（227）不改。
2026-10-07 命中 209/210/226/228/229。

### 行宇宙必须从 deployment 取，不是 pod

`scale=0` 的号（实测 acct-7/11/123）**没有 pod**，走 pod 列表会让它们整行从表上
消失 —— 而"该退役还是该救"正是最需要被记录的状态。改列 deployment 后
`spec.replicas==0` → `ready="off"` / `SCALED_DOWN`，与 198 侧 88/94/107 同口径。
好处之一：新加的号（如 2026-08-04 的 132/133/134）**自动进表**，不用改代码。

### `model_id` 分隔符两个集群不同

198 是 `chatgpt-acct-N-<model>`（`-` 分隔），阿里云是 `chatgpt-acct-N/<model>`
（`/` 分隔）。直接照抄 198 的 `split_part(model_id,'-gpt-',1)` 会全部取空。
`scripts/chatgpt-acct-spend.sh` 里有写好的兼容 `CASE` 表达式，别重写。

## 阿里云传输：整份 view 送到 226 上跑

**本地 kubectl + `jms proxy` 隧道已死**：2026-08-03 实测
`jms proxy laoyang 16443 → 172.16.1.163:6443` 起得来但 **TLS handshake timeout**，
kubectl 20s 超时后整脚本 traceback。且旧默认 hop `laoyang` 的 KoKo exec channel
自 2026-07-12 起已死。

现在走 `jms ssh --tty k8s-work-226`（226 自带 kubectl + 集群凭证），
view 用 gzip+base64 分块下发、结果 fold 成短行回传，**两头都校 sha256**。

### 三个已实测的坑（都会伪装成"成功"）

1. **`jms ssh` 退出码不可信** —— 正常完成也返非零。配 `set -e` 直接杀脚本。
   一律 `|| true`，成败只看产物（sha / `RC=` 标记）。
2. **`--tty` 会在行首注入 ANSI 转义（`\033\r`）** —— `grep -oE '^...$'` 锚定必
   失配，再被 `pipefail` 放大成致命退出。远端一律输出 `KEY=<value>`，
   本地按前缀提取。
3. **`cut -d= -f2` 会吃掉 base64 尾部的 `==` 填充** —— 远端 `...kAAA==` 到本地
   变成 `...kAAA`，解码报 padding 错。只能用 `sed 's/^[^=]*=//'` 剥第一个 `=`
   之前的前缀。

### 完整性判据取 b64 文本自身，不要拿 gzip 结果比

踩过：用收到的 b64 去跟"远端再 gzip 一次"的输出比长度 —— 两次 gzip 输出不保证
逐字节相同，**这个比较本身就是错的**，查不出真相。现在远端对同一份 `out.b64`
输出 `BLEN`/`BSHA`，本地先比长度（并印出差了多少字符）再比摘要，最后才解码。

这层校验实测挡下过两次"看着像成功"的半份数据（`==` 被吃、会话被 `Killed: 9`
截断）。**别为了省事去掉。**


## ⚠ 只看 `verdict` 一列。`upstream_tier` 不回答"这号好不好用"

三个平面各自独立，**任何一个平面的列都不能当总判定**：

| 平面 | 列 | 数据源 |
|------|-----|--------|
| 上游配额 | `upstream_tier` / `7d%` / `reset` | **in-pod 实探 /codex/usage** |
| 订阅 | `sub_until` / `sub_left` / `will_renew` | **in-pod 实探 /accounts/check/v4**（探不到退 JWT 快照，`sub_src` 标明）|
| 凭证文件 | `auth_sync` / `pod_cred` / `device_code` / `exp_gap` | pod:/chatgpt-auth/auth.json |
| 服务 | `ready` / `refresh_err` | deploy `readyReplicas` + pod 日志 |

`verdict` 是三平面合成的唯一总判定（阿里云另有 `OFFLINE(198在服)`，见「阿里云侧」），优先级：
`?`(取数失败) > `OFFLINE`(spec=0) > `SERVING_DEAD`(ready=0/N) >
`DEVICE_CODE_HELL`(dcr 存在) > `REFRESH_DYING`(rfx>0) > `FLAPPING`(0<ready<N) >
`UPSTREAM:*` > `OK`。

服务平面刻意盖过上游：撞顶会被引擎 `scale=0` 摘掉，**服务平面坏掉却一直留在池里当
黑洞**（`Ready=False` 比 `scale=0` 更毒，它绕过 preflight）。

### 为什么加这个平面：acct-121 事件（2026-08-02）

用户问「acct-121 明明有问题你为什么检查不出来」。当时表上：
`tier=HEALTHY / 7d=3% / 订阅到 8-25 / state_age=0h09m`，四列全绿。
同一时刻 198 上 `deployment/chatgpt-acct-121` 是 `0/1 READY, AVAILABLE=0`，
pod 每 5min 报 `refresh token failed: 401 auth.openai.com/oauth/token` 并打印设备码。

表不是算错了，是**正确地印了一个不完整的事实** —— 28 列里没有一列覆盖服务平面，
所以「上游有余量但一个请求都服务不了」和「真健康」在表上完全同形。

引擎也看不见：它拿 access_token 打 `/codex/usage`（JWT 还有效 → 200 → HEALTHY），
**从不走 refresh 路径**。所以 `upstream_tier` 对这一类结构性失明。

### 四个判据互补，缺一漏一半（别只留一套）

2026-08-02 同一次采样的实测分布，就是"不能只留一套"的硬证据：

| acct | verdict | `device_code` | `exp_gap` | `refresh_err` | `ready` |
|------|---------|-----|---------|-----|-------|
| acct-114 | DEVICE_CODE_HELL | **yes** | **3.0** | 0 | **2/2** |
| acct-117 | SERVING_DEAD | yes | 3.0 | 1 | 0/2 |
| acct-120 | SERVING_DEAD | yes | 3.0 | 1 | 0/2 |
| acct-115 | SERVING_DEAD（另一轮）| **no** | **0.0** | 2 | 0/2 |

- `acct-114` 只有 `device_code` 抓得到 —— `ready=2/2`、`rfx=0`，服务平面两个判据全绿
- `acct-115` 只有 `ready`+`rfx` 抓得到 —— refresh 已成功过（`expires_at` 已推到未来、
  `exp_gap=0`），凭证轴自愈了，但 readiness 还在事故尾巴上

**机制**：LiteLLM 信 `expires_at` 不信 JWT `exp`。越过后每个请求都先试 refresh →
`refresh_token` 401 → 掉进 device-code 流程，而 `_poll_for_authorization_code()` 是
**同步阻塞**轮询、容器单进程 → event loop 被堵 6~17min → `/health/readiness` 一起哑
→ kubelet 摘 endpoints → proxy 报 `Cannot connect to host`。
**表面像网络故障，实为凭证。**

`exp_gap>0`（JWT exp 比 expires_at 晚）= 从上线起从未成功 refresh 过一次，
根因是 onboard 写死 `expires_at=issue+7d` 而真 token 活 10d；刷成功一次两字段会被
一起改成一致，永久自愈。

## `tier` 是快照，不是现状 —— 401 最多陈旧 6h

2026-08-01 用户说「好多我已经认证了怎么还是 401」。我原样搬了 state.json 的
`tier` 列当结论讲，**没有验证任何一个号**。真因是机制层面的：

引擎对 `manual_offline` 的号**每 6h 才重探一次**，其余每 5min 的 cron tick 全是
`SKIP (manual_offline, retry in Nmin)`（`MANUAL_OFFLINE_RETRY_INTERVAL=6*3600`）。
所以重认证之后，表里的 `token_dead_401` 会继续挂最多 6 小时。

硬证据：`acct-106` 在快照（17:05Z）时是 `401 需re-OAuth`，pod 凭证 17:10 换新，
引擎 17:11 就转 HEALTHY —— 我贴出去的表 5 分钟就有一行过期了。

→ 这就是 `probe_age` 列存在的理由（= `now - state.ts`，本行距上次**真探测**多久）。
2026-08-02 改名纠正：原名 `state_age` + 注释写"距上次写入"是错的。实证 `state.ts`
只在真探测时更新（acct-121 `ts`=01:43:59 与 cron.log 最后一条 HEALTHY 完全对齐，
之后的 tick 全是 `SKIP (7d=3%, Nmin ago)`，那个 Nmin 正是从 `ts` 算的）。
没有它，「刚探过确实死」与「你已重认证但还没被探到」在表上长得一模一样。

## 引擎只读 pod 内凭证 —— 在 /Data 上认证不生效

两份 auth.json，**语义完全不同**：

| 路径 | 谁写 | 引擎探测是否读 |
|------|------|--------------|
| `/Data/chatgpt-auth/acct-N/auth.json`（JSZX-AI-03）| `re-oauth.sh` / 手工认证 | **不读** |
| pod `:/chatgpt-auth/auth.json`（198 PVC）| `attempt_repair_198` 的 `kubectl cp` | **只读这份** |

`quota-rebalance.py` 的 `read_pod_auth()` 是 `ssh cltx@10.68.13.198 → kubectl exec
$POD -- cat /chatgpt-auth/auth.json`。两份之间**没有任何自动同步**。

所以「认证成功了但表上还是 401」的第二种真因：认证落在 /Data，从没进 PVC。
2026-08-01 实测 acct-33/35 就是这样 —— /Data 那份 07-25 认证、窗口到 08-04 仍有效，
pod 里还是 07-14 的旧凭证，refresh_token 指纹完全不同。而这俩 `repair_frozen=True`
（`repair_attempts=5/5` 撞 `MAX_REPAIR_ATTEMPTS`），引擎也不会再自己去 cp
→ **认证多少次都不会变绿**，必须手动 `kubectl cp` + `rollout restart` + reset
`repair_frozen`。

`auth_sync` 列的方向决定严重性，别只看"不一致"：

- `local↑` — /Data 比 pod 新 → **真故障**，认证没进 PVC
- `pod↑` — pod 比 /Data 新 → 无害，引擎读的就是新的
- `same` — 两份指纹一致（2026-08-02 由 `ok` 改名：`ok` 让"两份都是死 RT"看起来是绿的）
- `both_dead` — 一致**且** pod 正在报 refresh 401 → 一致但都是死的
- `no-pod` / `no-local`

方向靠 `expires_at` 比（= 签发时刻 + 固定窗口，内容派生、与时钟无关），
**不用文件 mtime** —— pod 侧 mtime 是 `kubectl cp` 时刻，与凭证新旧无关。

## 一个 `401 需re-OAuth` 标签下面是五种病，处置动作互不相同

把它们并成一句"12 个需 re-OAuth"，会让人对其中四组做同一个无效动作。
`auth_plane_diagnostics()` 按以下顺序分桶（2026-08-01 12 个 401 的实测分布）：

| 桶 | 判据 | 动作 |
|----|------|------|
| `auth_not_in_pvc` | `auth_sync == local↑` | 手动 `kubectl cp` + restart（33/35）|
| `repair_frozen` | `repair_frozen=True` | 人工介入 + reset flag（33/35/66/74/76）|
| `verdict_predates_cred` | pod 凭证 mtime > `state.ts` | 不用再认证，等下次探测重测（66/105/107/110）|
| `no_pod_auth` | pod 内读不到 auth.json | 先查 deploy 是否 scale=0（74/76）|
| `needs_live_probe` | 以上全部排除后剩下的 | 实跑 refresh-grant 判 `ACCOUNT_DEACTIVATED`（88/94/98）|

`needs_live_probe` 是**排除法**得到的，不是靠"凭证还没过期"正面判定 —— 见下一节。
前四桶必须先从它里面扣掉，否则「pod 凭证刚换新、判定是对旧凭证下的」这类号会被推去
做**有代价的真号探测**，而真号上探测会把号探死。

`verdict_predates_cred` 只能声称「判定会被重测」，**不能声称「会翻绿」**。
反例：acct-107 于 2026-08-01 re-OAuth 明确返回 `ACCOUNT_DEACTIVATED`（救不回），
其 pod 凭证事后仍被刷新过，照样落进这个桶。凭证新 ≠ 账号活。

下判活结论前先查 memory —— 88/94/107 在 2026-08-01 已实测 `ACCOUNT_DEACTIVATED`，
再对它们实跑 OAuth 是重复劳动且无谓消耗真号。

pod 凭证落地时刻取自 `stat -c %Y`，与 `cat` 合并进同一次 `kubectl exec`
（`sh -c 'stat; cat'`），不额外增加 exec 次数，也不用靠"窗口恒为 10d"反推。

## auth.json 的 `expires_at` 不是活性信号

第一版我加了个 `pod_auth` 列显示"凭证窗口剩余"，`expires_at` 已过就渲染 `expired`。
跑完立刻被数据证伪：**acct-112/113/114/115/117/118/119/120/121 共 9 个号
`expires_at` 已过，却全是 HEALTHY 且 24h 各跑 400~550 calls。**
pod 内部刷新 access_token 不回写 `/chatgpt-auth/auth.json`。

所以 `expires_at` 只有一个合法用途：**比较两份文件的先后**（内容派生、跨主机
无时钟问题）—— 这正是 `auth_sync` 判方向用它的原因。它不能回答"这份凭证还能用吗"，
那个问题只有实跑 refresh-grant 能回答。

列已改名 `pod_cred` = `now - mtime`（引擎读的那份多久没更新），纯描述、不做活性断言。

## 回归测试：改 verdict 逻辑前先跑

```bash
python3 -m pytest backend/tests/test_chatgpt_acct_quota_view.py \
                  backend/tests/test_chatgpt_acct_quota_aliyun_view.py \
                  backend/tests/test_chatgpt_acct_quota_to_lark_options.py -q  # 90 passed
```

用例把 2026-08-02 的真实数据钉成基准，防止 verdict 退回「上游绿就印 OK」：
- `test_acct121_regression_upstream_healthy_but_not_ready` — 上游 HEALTHY +
  `readyReplicas=0` 必须是 `SERVING_DEAD`
- `test_device_code_absent_but_serving_dead_still_caught` — 只用 `dcr` 判据会漏 115/118
- `test_exp_gap_normalizes_millisecond_expires_at` — 见下

2026-08-05 新增 17 例钉实探平面（改 live_tier / 行宇宙前先跑）：
- `test_live_tier_cap_and_brim_are_not_the_same_bucket` — 100% 但 allowed=true 的
  7 个号不许与真封顶的 28 个同标签
- `test_live_tier_401_keeps_literal_string_for_auth_diagnostics` — 改字符串会让
  auth 诊断静默失效
- `test_build_rows_universe_is_state_union_deploy` / `test_untracked_is_not_zombie`
- `test_live_fetched_defaults_to_whether_live_data_exists` — 不传 live 必须退回快照，
  **不能**默认 True（那会把"没探"渲染成"已下线"）
- `test_probe_failure_leaves_pct_blank_not_zero` — 探不通是空白不是 0
- `test_live_vs_state_ignores_pure_rename_noise` — 改名差异不算分歧

2026-08-20 新增 8 例钉订阅实探平面（改 sub 探针 / sub_src 前先跑）：
- 198：`test_live_subscription_overrides_stale_state_snapshot`（acct-84：JWT 快照
  过期但实探续订到未来 → sub_src=live、不报 expired）、
  `test_missing_live_subscription_falls_back_to_state_snapshot`（实探缺订阅字段退
  快照 sub_src=state）、`test_sub_src_marks_fetch_fail_when_whole_link_down`（整链
  挂了 sub_src=FETCH_FAIL、不冒充实探）、`test_sub_src_carries_401_kind_when_probe_rejected`
- 阿里云：`test_live_subscription_overrides_stale_jwt_claim`、
  `test_missing_live_subscription_falls_back_to_jwt_and_marks_state`、
  `test_active_but_will_not_renew_is_shown`（acct-237：active 但 will_renew=no）、
  `test_scaled_down_sub_src_is_off_not_state`

阿里云那套（24 用例）另外钉了：探针失败必须 `PROBE_ERR` 且 verdict 不得为 `OK`
（fail-closed）、`replicas=0` → `off`/`OFFLINE` 而非"取不到"、
`auth_sync` 无 `/Data` 时是 `no-local` 而非 `same`。

⚠ 两个 view 的纯函数是**各自一份**（`serving_verdict` 等同名但不共用模块）。
不是疏忽 —— 两个 view 都是**整份文件被送到远端执行**的自包含脚本
（198 走 `ssh JSZX-AI-03 python3 -` 喂 stdin，阿里云走 gzip+base64 落盘到 226），
远端没有这个 repo，`import` 任何本地模块都会 ImportError。**改判定逻辑时两边都要改。**

注：`backend/tests/test_fusion_diagnosis.py::test_demo_route_rejects_other_uids`
是**预存失败**（`ModuleNotFoundError`），与本套无关，别去修。

## 两个窗口/口径坑（都实测过，别再踩）

**1. `kubectl logs --tail=N` 是错的窗口原语，必须用 `--since=`**

同一时刻同一 acct：

| acct | tail80 | tail200 | tail500 | since15m | 15min 内总行数 |
|------|--------|---------|---------|----------|--------------|
| acct-121 | 0 | 0 | **4** | **0** | 10 |
| acct-118 | 1 | 3 | 5 | 1 | 10 |

行数窗把「正在失败」和「保留日志里曾经失败过」混成一个数：`tail500` 那 4 条已陈旧
（`since15m=0`），会让 verdict 永远卡 `REFRESH_DYING`；`tail80` 又在日志变啰嗦时漏判。

**2. `expires_at` 有毫秒口径的，必须过 `_exp_seconds()` 归一化**

acct-66 的 `expires_at` 是毫秒、JWT `exp` 是秒。没归一化时 `exp_gap=-20656055.3`
（≈-5.6 万年），而 `never_refreshed` 的判据是 `>0` → **这个垃圾值没触发任何告警、
静默混在表里**。现已归一化 + `|gap|>60d` 置 None 并打 stderr。

## `ready` 的分母不是装饰，`2/2` 也不等于稳定

`ready` 恒为 `n/N` 形式（采样 N 次、间隔 20s）。**单点 ready 是噪声** ——
2026-08-02 deploy 级 `readyReplicas` 与 pod 级 `containerStatuses[0].ready` 两次单点
采样结论互斥（112/121 一次 `0/1` 一次 `true`，115 反向），我据此写了一段错的分析。

口径已固定为 deploy `readyReplicas`（K8s 自己的聚合），**不用**
`pod.containerStatuses[0].ready` —— 多 pod / 滚动时 `items[0]` 指向不确定。

局限必须说出来：几十秒间隔只抓得住**秒级**抖动。当晚观察到的是分钟级慢抖
（02:10 测 `0/1`、02:14 测 `true`、02:33 测 `2/2`），这种会以 `2/2` 或 `0/2` 出现。
所以 **`ready` 只用来定位，定性靠 `device_code` / `exp_gap` / `refresh_err`**。

不 Ready 的号在几次采样间会**换人**（02:10 是 107/112/113/114/118/119/120/121，
02:38 是 115/118/120）—— 这几列抓的是这**一类**故障，不是钉死某个编号。

## ⛔ 禁止用 `upstream_tier` 当任何服务平面信号的对照组

2026-08-02 我手里已经有 `pod_cred 过期` 这个信号（13 个），却用「9 个
`tier=HEALTHY` 的号也过期」把它判成噪声。错在 `tier` 只覆盖上游配额平面，
**它对服务平面结构性失明** —— 我拿一个瞎的指标去否证一个关于它盲区的信号。

修正后交叉表：8 个 `readyReplicas=0` 的号 **8/8** 落在过期集合内（召回 100%、
精度 8/13）。**用被质疑的指标当对照组，结论恒为"信号是噪声"。**

要否证服务平面的信号，对照组必须取自服务平面（`ready` / `refresh_err` / 实跑请求）。

## verify 必须逐格比对，行主键是 (site, acct)

原 `verify_records()` 只比 `len(record_id_list) == expected` —— 64 行全写成空格子
也报 `[verify] OK`。已改为读回全部行 × 全部列（`len(COLUMNS)`，当前 45）逐格 diff，
不符则列前 15 处并 `return 4`。
坑：`+record-list` 列式返回、select 列回读为 list、表格列顺序与脚本 `COLUMNS` 无关
（必须按列名取）。

### 🔴 分页只能用 `--offset`，`+record-list` 没有 `--page-token`

2026-10-10 修。原 verify 的翻页循环传 `--page-token`，而该子命令**没这个 flag**
（实测 `unknown flag "--page-token"`，lark-cli 1.0.93）⇒ 第一页之后必然拿 validation
error、`data` 为空 ⇒ break。表只有 190 行（<200 一页装得下）时这条路径**从没走到过**，
所以一直没暴露；**一过 200 行，每一轮成功写入都会被误判成失败**（`200 records in
table, expected N` → rc=4）。池子现在 190 行，离这个坑一步之遥。
已抽成 `read_all_records(base_token, table_id, fields=None)`：`--offset` 翻页、
`fields` 可投影、空页也 break（`has_more=True` + 空页会无限循环）。

## 汇报纪律：state.json 的字段不是"验证过的结论"

跑完脚本 ≠ 验证过。`tier`/`cause`/`status` 都是引擎上次探测的快照，直接当现状讲
就是把陈旧值冒充事实。要下「这些号需要重新认证」这种结论，最少要有：

1. `state_age` —— 判定有多旧
2. `auth_sync` —— 引擎读的那份凭证是不是你认证的那份
3. `pod_cred` —— 那份凭证多久没更新过（**不是**有没有过期）
4. 对照组 —— 同一时段同一循环里有号成功了吗（acct-106 就是这个作用，
   证明"探测链路没坏"，从而把问题定位到单个号而不是系统）

只有 `needs_live_probe` 这一桶才需要实跑 OAuth，而且要先确认再动。

## snapshot_at：没有它，成功的写入无法自证

2026-07-27 用户说「你没有更新多维表格」，实际写入成功（raw API `automatic_fields:true`
读 `created_time` 证明 64 条均为当下创建）。真因：**表里全是配额数值、无采样时刻列**，
而两次采样间上游 7d%/main_n 几乎不动 → 删 64 行重建 64 行后屏幕肉眼完全一样。

值取 payload 的 `generated_at`（**数据采样时刻**，非写入时刻 —— quota 脚本跑 198 要
几分钟，表该反映数据多新而非写得多新）。

要证明记录何时写的：
```bash
lark-cli api POST "/open-apis/bitable/v1/apps/<base>/tables/<tbl>/records/search?page_size=2" \
  --as user --data '{"automatic_fields":true,"field_names":["acct"]}'
# → created_time / last_modified_time（毫秒）。+record-list 不返回这些
```

## zerokey 数据：三个必须记住的坑

**1. `zk_lat_*` 是端到端总时延，不是首 token 时间（TTFT）**

想加 TTFT 列会踩这里。LiteLLM 的 `completionStartTime` 在流式下几乎等于响应结束点：
实测 16116 行中 `endTime - completionStartTime` 中位数 **1ms**、仅 1.8% 超过 1 秒。
对照组 `gpt-5.5` 走真 API 同样 99.4% —— 这是 LiteLLM 对 responses 流式的通用记账
行为，不是 zerokey 特有。`metadata` 无 `first_token` 字段，proxy `/metrics` 返回空。
**DB 里不存在真 TTFT。** 故列名叫 `lat`，用 `request_duration_ms`。要真 TTFT 得改
LiteLLM callback 记首个 SSE chunk 时刻（需动生产镜像）。

**2. zk-N ↔ acct-N 映射（三重实证，别改成猜的）**

1. `zero-N` svc/deploy 编号集合 == acct 池编号集合（无差集）
2. `zk-N-*` 的 `api_base` 恒为 `http://zero-N.litellm-product.svc:8200`（1:1）
3. `zero-N` pod 内 `/app/temp/users.json` → `chatgpt.acctN.username == "acctN"`

**3. 「未归属消耗」是常态，必须报**

zk 聚合按 `zk-N` 分组，但表格行来自 state.json。state 里没有的 `acct-N` 会被丢掉。
2026-07-26 实测差额 **8565 calls / $963.99（20 个编号）**——占全池一半以上。脚本会印
`[zk] 表内合计 / 全池合计 / 未归属`，看表的人不能把表内合计当全池真值。

`zerokey-account-usage.py` 头部注释「zerokey spend 恒为 0」**已过期**：实测 16116 行中
15148 行 spend>0，累计 $1544.88。那脚本是 188 端口平面的老物，与 198 集群内平面无关。

## 诊断输出（13 段）

zerokey 平面（`zk_diagnostics`）：

- **zk 未归属消耗** — state.json 无对应 acct 但有流量的 zk-N
- **zk 孤儿路由** — LiteLLM 注册着 zk-N 但 198 无 `zero-N` svc（请求打到不存在的后端）
- **zk 稳定空返** — `completion_tokens=0` 占比 ≥50% 且 n≥10。依据「zerokey 稳定空返
  = 网页端订阅失效」。阈值有下限是为了不把低样本噪声报成坏号

凭证平面（`auth_plane_diagnostics`，见上文五种病）：

- **认证没进 PVC** / **repair_frozen** / **401 判定已陈旧>6h** / **判定将被重测** /
  **pod 内读不到凭证** / **需实跑 OAuth 判活**

两个平面在 `--json` 的 `diagnostics` 里合并成一个 dict，`to_lark.py` 分别用
`report_zk_coverage()` 和 `report_auth_plane()` 在写表前印到 stderr。

订阅平面（`report_subscription_plane`，两集群一起）：`⛔ 已过期` / `⚠ 7 天内到期` / `⚠ will_renew=no`
**只收 `sub_src ∈ {live, 401:invalidated}`** 的行；其余「快照说 expired 但探不到」的放
`ⓘ stale_expired` 桶，**只印一行编号**（10-07 这桶 126 行逐行打印，把真告警挤出了屏幕）。
读 `[sub]` 时只把前三段当待处置清单。

档位平面（`report_plan_plane`，2026-10-10 加）：按实探结果分 `实探 pro` / `实探 free`
两桶逐个点名，**探不到的行单独一桶**并印明「空不等于 free」+ 判档位的唯一合法路径。
10-11 实测分布：**pro 16 / free 8 / 探不到 166**。
⚠ 这一段**刻意不对探不到的行给档位结论** —— 它存在的理由就是阻止 227 那类误判复发。

视图盲区（`report_view_blind_spots`，2026-10-10 加）：读每个带 filter 的视图，**本地
求值**算出各藏了多少行，并单列「因格子为空而被滤掉」的行。纯读+本地求值，`--dry-run`
也跑。10-10 本地算出的 `预下线 10/190`、`online 15/190` 与线上视图**逐一对上**。
不认的算子（`_cell_matches` 返回 `None`）整段跳过并说明 —— 猜算子会印出一个看着精确
但是编的数字。**本段只报事实，绝不改视图**（视图是用户配的）。

## 稳定写入关键点

- JSON payload 通过 `@file`（当前目录相对路径）传给 lark-cli，避免命令行参数过长
- `+field-list` 响应字段在 `data.fields`
- `+record-batch-create` 记录数在 `data.record_id_list`（非 `data.items`/`data.records`）
- `+record-list` 是**列式**返回：`data.fields` 是表头、`data.data` 是行数组（不是
  每行一个 dict），另有 `data.record_id_list` + `data.has_more` 分页
- 临时文件 `_lark_del_batch.json` / `_lark_create_batch.json` / `_lark_field.json` 用后即删

## 别再用固定宽度反解

`to_lark.py` 曾用 `line[100:107]` 这类字符偏移反解渲染后的文本表。加一列就要重算全部
偏移，且错位是**静默**的（切出半个数字仍能 `float()`）。2026-07-26 已改为消费
`chatgpt_acct_quota_view.py --json`。加列只需改 `COLUMNS` + `NEW_FIELD_SPECS`，
文本表和 JSON 共用 `build_rows()` 这一个事实源。

## 飞书表格链接

https://t83dfrspj4.feishu.cn/base/YdrZb5xaNaziDHssawGcDpd1nqf?table=tblsw3E1zZ2IuBbW
