---
name: litellm-owui-user-row-reconcile
description: >-
  把「有 LiteLLM key 但 LiteLLM_UserTable 没人行」的用户从飞书表补齐,
  以解决新同事拿到 key 却被 key-swap-proxy 门禁 401 / OWUI 报
  `Model '' was not found` 的问题。含对账脚本 backfill.py (默认 dry-run)、
  10 分钟 cron、以及「门禁查的是人行不是钥匙」这个根因。
  Use when 用户提到 "新人登不上 OWUI" / "有 key 却被拒" / "no_litellm_key 但 key 明明在" /
  "补人行" / "LiteLLM_UserTable 缺行" / "孤儿 key" / "对账" / "飞书那边更新了要马上同步" /
  "Model '' was not found"。
---

# LiteLLM 人行对账器 (OWUI 门禁的粮草)

**一句话**:建 key 和建人行是**两条独立的路**,门禁只认人行,所以夹在两次飞书同步
之间入职的人 key 能用但进不去 OWUI。这个 skill 就是把两条路对上账。

## 1. 根因 (2026-09-15 量出来的,不是推测)

```
申请页建 key  → 只写 LiteLLM_VerificationToken,metadata 仅 {purpose, owner_name}
飞书批量同步  → 才写 LiteLLM_UserTable 人行 / TeamMembership / OrganizationMembership
key-swap-proxy 门禁 → 查 /user/info?user_id=cursor-{local};404 就 401
```

**数据**:1272 对 key↔人行里 **1257 对人行晚于 key**,最短滞后 1 天,最长 57 天。
历史上人行同步只跑过 **6 个日期**(04-14 / 06-12 / 06-18 / 08-03 / 08-25 / 09-04)。

⛔ **反代源码 `/app/main.py` 里那句注释是假的**:
> "A user record is auto-created by LiteLLM whenever a key is issued for it,
> so this is a reliable allowlist signal."

1257 对全部证伪。见 [[feedback_litellm_user_row_is_not_created_with_key]]。

## 2. 为什么是对账器,不是 trigger / 不是实时查飞书

| 方案 | 否决理由 (都有实测) |
|------|--------------------|
| DB trigger | `litellm-db-0` 里**只有 psql**,python3/curl/jq 全 MISSING;而且 trigger 挂在建 key 的必经路上,一报错就建不出 key |
| 门禁实时查飞书 | 飞书查询实测 **1.07s / 0.91s / 0.92s**,流量 96793 spend logs/24h = **67.2 req/min** ⇒ 每次门禁都加 1 秒,且飞书变成整个 OWUI 的单点 |
| 门禁 miss 再回填 | 反代 pod **没有任何 DB driver**(asyncpg/psycopg2/psycopg/sqlalchemy/prisma 全 MISSING),要做得给反代开数据库写权限 |
| 调 `/user/new` 建行 | API 产出的行**形状跟存量 1287 行不一致**:organization_id 不写、teams 数组被填(存量是空的)、TeamMembership 行不建、budget_id 不带 |

**人行不是飞书的缓存,是一本账**:1339 行里 **752 行 spend > 0,最大 206511.94**;
1315 条 team membership 里 484 条有 spend。飞书**没有这些数**,所以它不可能实时算出来。

## 3. 用法

脚本在 `scripts/`,线上副本 `198:/Data/litellm-user-backfill/`。

```bash
# 探测 (默认就是 dry-run,不写库不重启)
python3 backfill.py

# 只看某几个人
python3 backfill.py --only cursor-zhangsan,cursor-lisi

# 真写 (事务 + on conflict do nothing,重复跑安全)
python3 backfill.py --apply

# 写库但不重启反代 (自己稍后手动清缓存)
python3 backfill.py --apply --no-restart

# 把 SQL 吐出来人肉审
python3 backfill.py --sql-out /tmp/plan.sql
```

| flag | 作用 |
|------|------|
| `--apply` | 不加就是探测器 |
| `--only` | 逗号分隔 user_id |
| `--limit` | 只处理前 N 个 |
| `--no-restart` | 不 rollout restart key-swap-proxy |
| `--sql-out` | 把生成的 SQL 写到文件 |
| `--include-unknown` | 飞书搜不到的也补(补出来是空壳,**默认跳过**) |

**默认跳过飞书搜不到的**:飞书表是这批人的权威源,搜不到 `key_alias` 就一个真实字段
都填不出来,补出来只会是个连姓名都没有的壳(`cursor-debug4` 这类测试号)。

## 4. cron (现役)

```
CRON_TZ=Asia/Shanghai
*/10 * * * * flock -n /tmp/litellm-user-reconcile.lock bash /Data/litellm-user-backfill/reconcile-cron.sh
```

- 老 crontab 备份在 `198:/root/crontab.backup-20260915T120000`
- 日志 `/var/log/litellm-user-reconcile.log`,脚本自己 20MB 截到 5MB
  (198 有过盘满事故,见 [[project_198_diskpressure_502_recurrence_2026_09_08]])
- **稳态下只跑一条 psql 查询就退出**,不写库、不重启任何东西
- 只在**真补了人**的那一轮才 rollout restart 反代清 600s 拒绝缓存

## 5. 脚本里三个不许拆的设计

### 5.1 SQL 走 `kubectl cp` + 两头对 md5,不用 heredoc

```python
run(["kubectl", "-n", NS_DB, "cp", local, f"{DB_POD}:{remote}"], timeout=120)
got = run(["kubectl", "-n", NS_DB, "exec", DB_POD, "--", "md5sum", remote]).split()[0]
if got != md5_local:
    raise Fail(f"md5 不一致：本地 {md5_local} pod 内 {got} —— 传输坏了，不执行")
```

⛔ **绝不用 `kubectl exec ... <<EOF` 喂 SQL**:没 `-i` 时 psql 读到空 stdin,
**静默 exit 0**,看起来成功但一个字都没执行。2026-09-15 在生产上踩过一次。
见 [[kubectl_exec_i_eats_heredoc_stdin]]。

### 5.2 `if not plans: return 0` —— cron 的安全阀

```python
if not plans:
    print(f"有 {len(skipped)} 个孤儿身份在飞书表里搜不到，没有可补的字段，本轮不写库也不重启。")
    return 0
```

没这一句,`cursor-debug4` / `cursor-quanshaoying` 这种**永远解析不出来**的孤儿会让
cron **每 10 分钟空转一个事务 + 白白滚动重启一遍反代**,无限循环。
验证方式:`--apply --only cursor-debug4` 应该打出"本轮不写库也不重启"。

### 5.3 `team_id` 为空要分两种成因报

```python
no_dept = [p for p in plans if not p["team_id"] and not p["department"]]   # 飞书没填部门
no_team = [p for p in plans if not p["team_id"] and p["department"]]       # 部门在但没建 team
```

混在一起说会把「部门没建 team」误报成「飞书没填部门」,让人去改错的地方。

## 6. team_alias 撞名的取舍

`LiteLLM_TeamTable` 里 **71 组 team_alias 重复**(06-12 建的老壳 + 09-04 同步建的主力)。
规则:**同别名取 `created_at` 最新**。实测 92 个别名 → 79 个直接命中有人的那个,
落选 6 个都是老壳(各 1~4 人),剩 13 个是压根没人的空 team。

## 7. 2026-09-15 那一轮的实际结果

```
dry-run:  45 个孤儿身份 / 23 个人
回滚测试: commit→rollback,112 INSERT / 28 UPDATE / 1 SELECT 13 / ROLLBACK,零残留
--apply:  42 人行 + 42 org 成员 + 28 team 成员 + 13 team 备份行
孤儿:     45 → 3 (剩下 3 个是飞书搜不到的测试号)
门禁探针: 42/42 绿
```

## 8. ⚠️ 门禁查的是错册子 —— 一个**未拍板**的洞

门禁判据是「人行存在」,不是「钥匙有效」。后果:

```
has_live_key=false / has_user_row=true → 39 个身份 = 20 个人
```

**这 20 个人钥匙早就撤了,但今天照样能进 OWUI**(抽 4 个端到端实测,全 200)。
抽样 8 人近 30 天 OWUI 用量为 0。

正确的实时判据应该是**查钥匙表**:
```
cursor-debug4      → /user/info = 404  但 /key/list = 200 keys=1
cursor-nosuchperson → /key/list = 200 keys=0
```
`/key/list` 在 key 建出来的那一刻就有,**没有滞后**,所以改成查它连对账器都不用了。

两个选项(**用户尚未选择,不要擅自动手**):
- **A** `user_row OR live_key` —— 新人当场可用,零附带损伤,洞留着
- **B** 只认 `live_key` —— 洞堵上,但把这 20 个人关在门外

**这是业务判断**,不是技术判断。

## 9. 探针必须用对的 header

```bash
# ✅ 反代读的是这个
-H 'X-OpenWebUI-User-Email: someone@auto-link.com.cn'
# ❌ 这个一律 401,包括阳性对照
-H 'X-User-Email: ...'
```

2026-09-15 我用错 header 时**阳性对照也红了** —— 判据是「已知好用的同事也红」
⇒ 坏的是量具不是被测对象。见 [[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]]。

反代 pod 里**没有 curl**,探针用 `python3` + `urllib`。Service 端口 **8081**。

## 10. 相关

- [[owui-key-swap-proxy]] —— 门禁代码本身 / 模型白名单
- [[owui-ops]] —— OWUI 端
- [[litellm-key-mapping]] —— 看 cursor-* / claude-code-* key 分布
- [[feedback_198_prod_db_is_litellm_product_not_dev]] —— 查生产必须 `-n litellm-product`
