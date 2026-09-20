---
name: litellm-budget-full-reset
description: >-
  198 prod LiteLLM key「全部重置」额度的正确姿势。当用户说"重置了额度还是不可用 /
  重置限额 / 额度重置 / reset budget / 还是提示额度用完 / 还是被拦"时用。核心：198
  上有**两套独立记账**——① 总额度(DB spend/max_budget)② 系列日额度(redis
  budget_notice:fam 桶,gpt5.3=$200/其他=$500 每北京日)。只重置总额度碰不到 redis
  系列桶,key 照样被拦。重置必须两套都清:单把/几把用 scripts/litellm-key-budget-reset.py,
  全量 cursor 用 scripts/litellm-198-cursor-reset-all.sh(集合运算,秒级)。
---

# LiteLLM Key 额度「全部重置」

> 198 prod（`litellm-product` ns）。2026-08-22 起线上有 **两套独立的额度记账**，
> 超限提示几乎一样，但**存储位置和清法不同**。只清一套 = "重置了还是不可用"。

## 铁律

**重置一把 key 的额度 = 两套记账都要清。** 缺一套就复发。

## 两套记账（判据 + 清法）

| # | 记账 | 存储 | 判据（怎么确认是它拦的） | 清法 |
|---|---|---|---|---|
| ① | **总额度** | DB `LiteLLM_VerificationToken`：`spend` / `max_budget` | `spend >= max_budget` | 改 `spend`/`max_budget`（`/key/update` 或 SQL） |
| ② | **系列日额度 ④** | redis `budget_notice:fam:{gpt53\|other}:{token}:{北京日}` | 该桶值 ≥ 系列限额（gpt5.3=$200 / 其他=$500，或 key `metadata.budget_family_overrides`） | `redis-cli DEL` 那个桶，或提高 override |

> `token` = key 的 sha256（DB `token` 列，redis 桶里就是这个）。北京日 = `TZ=Asia/Shanghai date +%Y%m%d`。
>
> **实证坑**（cursor-zhuge-zlcb，2026-08-21）：总 spend **$18/$500** 远没到，用户
> "重置总限额"没用；真凶是 redis `budget_notice:fam:other:…` = **$544/$500** 超限
> → 200 软拦截「该系列今日额度已用完」。重置总额度**碰不到** redis 桶，所以照样拦。

## 一键全部重置（首选）

`scripts/litellm-key-budget-reset.py` —— 一条命令清两套（`spend→0` + DEL 所有系列桶），
默认 dry-run，必须 `--apply`。本机首选包装脚本直接 SSH 到 198，并通过标准输入执行，
不再逐次上传 `/tmp` 文件；直连 SSH 不可达时包装脚本才回退 JumpServer。

```bash
# 单把精确重置：先 dry-run，再 apply
./scripts/litellm-198-key-budget-reset.sh cursor-zhuge-zlcb
./scripts/litellm-198-key-budget-reset.sh cursor-zhuge-zlcb --apply

# 只知道人名时允许模糊查询；先看 dry-run 命中了哪些 key
./scripts/litellm-198-key-budget-reset.sh liujinling --like
./scripts/litellm-198-key-budget-reset.sh liujinling --like --apply
```

常用参数：
```bash
litellm-198-key-budget-reset.sh cursor-zhuge-zlcb --apply
litellm-198-key-budget-reset.sh cursor-zhuge-zlcb --max-budget 500 --apply
litellm-198-key-budget-reset.sh cursor-zhuge --like --apply
litellm-198-key-budget-reset.sh cursor-zhuge-zlcb --keep-spend --apply
litellm-198-key-budget-reset.sh cursor-zhuge-zlcb --today-only --apply
```

脚本会先打印每把 key 的 **总额度 + 系列桶现值**，dry-run 列出将执行的动作。

## 全量批量重置所有 cursor key（`litellm-198-cursor-reset-all.sh`，2026-08-24）

场景：用户说"把**所有人** cursor 的余额全部重置"。**别用 `litellm-key-budget-reset.py cursor- --like`**
——它逐把 key 打 `/key/update` HTTP + 逐把 redis `--scan`,585 把串行几分钟。改用**集合运算**版:

```bash
# 本机跑,脚本内部 SSH 进 198(不用先 scp)
./scripts/litellm-198-cursor-reset-all.sh            # dry-run,只报范围(匹配 key 数 / spend>0 数 / cursor 系列桶数)
./scripts/litellm-198-cursor-reset-all.sh --apply    # 执行
./scripts/litellm-198-cursor-reset-all.sh --pattern 'cursor-%' --apply   # 自定义 LIKE
```

**为什么快 & 准（设计要点)**:
- **① DB**:单条 `UPDATE spend=0 WHERE key_alias LIKE 'cursor-%' AND spend>0`(1 次,不是 585 次)。
  用 `WITH u AS (UPDATE ... RETURNING 1) SELECT count(*) FROM u` 取更新行数(裸 UPDATE 的 `UPDATE n`
  状态标签被 psql `-q` 吞掉)。
- **② redis**:全表一遍 `--scan budget_notice:fam:*` → 按 **cursor token 集**过滤(fam 键冒号第 4 段
  = token,与 DB `token` 列全等)→ 分批 `xargs -n 400 ... redis-cli DEL`。**绝不误删** claude-code-\* 等
  非 cursor 桶。
- **正确性依据**(与 audit 脚本同款直写 DB):litellm 落账是 `UPDATE SET spend = spend + delta`(相对
  增量),直接置 0 不会被后续 flush 冲掉,只是从 0 重累加;各 proxy in-memory auth 缓存 **≤60s** 刷新;
  redis DEL 立即生效。复核只看 `spend>=max_budget`(期望 0;>0 多半是重置后瞬时新流量到顶)。

> 与 `litellm-key-budget-reset.py` 分工:**单把/几把** → py 脚本(带各系列桶明细、可 `--max-budget`);
> **全量 cursor** → 本 sh 脚本(秒级)。两者清的是**同两套记账**,语义一致。

## 手动兜底（脚本不可用时）

先拿 token：
```bash
kubectl -n litellm-product exec litellm-db-0 -- env PGPASSWORD=<pw> psql -U litellm -d litellm -h localhost -c \
 "SELECT key_alias, token, ROUND(spend::numeric,2), max_budget FROM \"LiteLLM_VerificationToken\" WHERE key_alias ILIKE '%zhuge%';"
```
① 总额度（热更新，比 SQL 立即生效）：
```bash
# /key/update {"key":"<token>","spend":0}  （可带 "max_budget":500）
```
② 系列桶（立即生效）：
```bash
kubectl -n litellm-product exec litellm-redis-0 -- redis-cli --scan --pattern 'budget_notice:fam:*:<token>:*'
kubectl -n litellm-product exec litellm-redis-0 -- redis-cli DEL '<那些 key>'
```

## 生效时延

- **系列桶 DEL**：立即（budget_notice hook 每次请求直读 redis）。
- **总额度 spend**：走 `/key/update` 是热更新，但各 proxy 副本 in-memory auth 缓存 **≤60s** 才全刷；直写 SQL 同样 ≤60s。下调额度/清 spend 后用户最多等 1 分钟。

## 别做

- ❌ 别 `FLUSHDB`：198 redis 还存 weighted_affinity 粘性，冲了打断 Codex/Cursor 长会话。只 DEL 目标桶。
- ❌ 别只改 max_budget 就以为"重置完了"——系列桶还在 redis 里拦人。

## 连接与执行优化（2026-09-07）

- 已验证本机可免密直连 `cltx@10.68.13.198`，并可使用 `sudo -n kubectl`。
- 单把/几把 key 使用 `scripts/litellm-198-key-budget-reset.sh`：直接 SSH，把 Python 脚本从 stdin 执行，不落远端临时文件。
- 只有 SSH 返回连接级错误（exit 255）时才回退 `scripts/jms ssh AIYJY-litellm`。
- 不要为日常重置先 `scp` 到固定 `/tmp/litellm-key-budget-reset.py`；旧文件属主可能导致 `Permission denied`，并增加一次连接。

## 本次执行记录与优化记忆（2026-08-26）

- 全量脚本 dry-run 先确认范围，再 `--apply`；本次匹配 `cursor-%` 共 592 把 key。
- 执行时 DB `spend>0` 为 130 把，Redis 对应系列桶 132 个，最终 DB 置零 130 行、Redis 删除命中 132/132，复核被总额度拦截为 0。
- 全量场景必须使用集合运算脚本：DB 单条 `UPDATE`，Redis 单次全表 `SCAN` 后按 DB token 集过滤，再按 400 个键分批 `DEL`；不要对 592 把 key 逐把调用 API/SCAN。
- 统计数字可能因实时流量在 dry-run 与 apply 间变化；以 apply 阶段的更新行数、DEL 命中数和最终复核为准。
- 重置后仍不可用时，按“总额度 DB → 系列日桶 Redis → proxy 缓存（最多约 60 秒）”顺序复核，不要扩大清理范围。

相关：`litellm-budget-mgmt`（完整预算管理）、[[feedback_litellm_budget_rejection_has_three_gates]]、
[[project_198_budget_notice_suite_2026_08_19]]、[[198-direct-ssh]]。
