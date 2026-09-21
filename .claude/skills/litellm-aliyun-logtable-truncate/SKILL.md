---
name: litellm-aliyun-logtable-truncate
description: 阿里云(ACK ns carher)LiteLLM 日志表清空与保留期治理。含 online-only TRUNCATE 脚本、与 198 应急锤子的五维差异对照(别互套心智模型)、2026-09-21 实测的 ToolIndex:SpendLogs = 19:1 增长几何、retention CronJob 漏掉 ToolIndex 这个真根因,以及"NAS 的 df 恒显示 10P 量不出水位"这把坏尺子。
---

# 阿里云 LiteLLM 日志表清空 / 保留期治理

> 作用域:**阿里云 ACK,ns `carher`,`litellm-db-0`**。
> 198(ns `litellm-product`)是完全另一套,见 [[litellm-198-diskpressure-502]]。
> **两边的心智模型不许互套** —— 差异见第 1 节,那张表就是这份 skill 存在的主要理由。

## 0. 一句话

```bash
DRY_RUN=1 bash scripts/litellm-aliyun-logtable-truncate.sh   # 先体检
bash scripts/litellm-aliyun-logtable-truncate.sh             # 交互确认后清
```

默认清 `LiteLLM_SpendLogToolIndex` + `LiteLLM_SpendLogs` 两张。Daily* 汇总表不动(按天预聚合,报表仍可用)。

## 1. ★ 与 198 的五维差异(最容易踩的坑)

| 维度 | 198 (`litellm-product`) | 阿里云 (`carher`) |
|------|------------------------|-------------------|
| 存储 | hostPath `/Data` 492G,**会真满** | CNFS NAS,PVC 配额 600Gi |
| 触发 | disk-pressure→驱逐→全站 502 死锁 | **无盘压**,配额水位高时主动清 |
| db pod | **常态是已被驱逐** ⇒ 必须 offline 单用户模式 | 一直 Running ⇒ **只有 online** |
| 连接 | sshpass ssh + kubectl exec(两层) | 本机 kubectl 直连 |
| 收尾 | 等 kubelet 撤 taint(实测 5~41min) | **没有这一步**,清完即完 |

**⚠️ NAS 配额满不会驱逐 db pod** —— 它只让写失败。所以阿里云这边
`litellm-db-0` 不 Running 时**是别的病**,不该用这把锤子(脚本会 fail-fast 挡住)。
反过来,198 那套 offline 单用户模式在这边既不需要也跑不了(没有 hostPath PGDATA)。

## 2. 🔴 坏尺子:NAS 的 `df` 恒显示 10P

```
$ kubectl -n carher exec litellm-db-0 -- df -h /var/lib/postgresql/data
...nas-7c9a4e6a...  10.0P  1.5T  10.0P  0% /var/lib/postgresql/data
```

`0%` 是假绿,`10P` 是 CNFS 的虚拟容量,**量不出 600Gi 配额用了多少**。
198 那边 `df -h /Data` 是真尺子,照抄过来就会把「快撑爆」读成「0% 很健康」。

**判阿里云容量水位只认**:
```sql
SELECT pg_size_pretty(pg_database_size('litellm'));
SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)) FROM pg_class c
  JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relkind='r'
 ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 8;
```

## 3. ★ 真根因:retention 漏了 ToolIndex

阿里云**有** retention CronJob 且一直在跑(`litellm-log-retention`,109d,每天 05:00 Asia/Shanghai,
`RETENTION_INTERVAL=14 days`)。2026-09-21 我第一次判断时说成"阿里云没有任何 CronJob 覆盖",
**是错的** —— 没查 `kubectl get cronjob` 就下了结论。

它覆盖 3 张表,**唯独漏了 ToolIndex**:

| 表 | 被 retention 覆盖 | 2026-09-21 清理前 |
|----|------------------|------------------|
| `LiteLLM_SpendLogs` | ✅ 14 天 | 39 GB / 30.4 万行 |
| `LiteLLM_ErrorLogs` | ✅ 14 天 | 0 行 |
| `LiteLLM_AuditLog` | ✅ 14 天 | 0 行 |
| `LiteLLM_SpendLogToolIndex` | ❌ **无覆盖** | **117 GB / 1.53 亿行** |

`ToolIndex` **有 `start_time` 列**(schema 见下),技术上完全可以被覆盖,就是没写进去。

```
Table "public.LiteLLM_SpendLogToolIndex"
 request_id | text                           | not null
 tool_name  | text                           | not null
 start_time | timestamp(3) without time zone | not null
PK: (request_id, tool_name)
```

### 增长几何(2026-09-21 清空后 43.3 分钟实测)

| 表 | 新增行 | 比值 |
|----|-------|------|
| `LiteLLM_SpendLogToolIndex` | 8,749 | **19.2 ×** |
| `LiteLLM_SpendLogs` | 455 | 1 |

**一次请求平均写 19 行 ToolIndex**。所以"只清 SpendLogs"只处理了约 25% 的体积、
**0.5% 的行数** —— 这就是为什么脚本默认两张都清。

### 补齐的方法(**未实施**,等你拍板)

在 `k8s/litellm-log-retention-cronjob.yaml` 的调用段加一行:
```bash
delete_by_ctid_batches LiteLLM_SpendLogToolIndex start_time
vacuum_table LiteLLM_SpendLogToolIndex
```
⚠️ 现成的 `delete_by_ctid_batches()` 里 `ORDER BY "${time_col}", request_id` 写死了 `request_id` ——
ToolIndex 恰好有这一列所以能直接复用;它主键是复合键 `(request_id, tool_name)`,
排序列不唯一**不影响 ctid 批删的正确性**,只影响批次边界的稳定性。

## 4. 执行纪律(继承自 198 脚本两次实战事故)

1. **不许加 `2>/dev/null`** —— 2026-09-08 在 198 上吞掉 `ctr: image not found`,
   命令静默不执行,差点把「没清掉」读成「清掉了」。
2. **管道尾不许用 `grep -v`** —— 2026-09-13 实测:`psql -Atq` 执行 TRUNCATE 输出 0 行,
   `grep -v` 拿空输入返回 1,叠 `pipefail`+`set -e` ⇒ 循环在**第一张表成功之后**静默中止,
   第二张表根本没跑,屏幕上一个错字都没有。要过滤用 `sed`(删没删到都返回 0)。
3. **逐表打回执** —— `psql -Atq` 对 TRUNCATE 不输出任何东西,不自己 `echo` 一行
   就完全看不出循环走到第几张表。

## 5. 验收判据

| 项 | 判据 | 2026-09-21 实测 |
|----|------|----------------|
| 清掉了 | `pg_database_size` 断崖 | 156 GB+ → **380 MB** |
| 写入没断 | 清完几秒后 `count(*)` **非 0** | SpendLogs 3 / ToolIndex 146 |
| 没误伤 proxy | `RESTARTS` 不变、`AGE` 不重置 | 2 pod 均 1/1,RESTARTS 0,AGE 2d12h |
| 汇总报表还在 | Daily* 表体积不变 | `DailyTagSpend` 132 MB 原样 |

**清完行数非 0 是正常的**,那是 TRUNCATE 之后这几秒的新流量 —— 它恰好证明写入链路没断。
若清完**恒为 0**,反而要查 proxy 是不是不写库了。

## 6. 相关

- [[litellm-198-diskpressure-502]] — 198 盘满 502 死锁(offline 破窗锤在那边)
- [[litellm-ops]] — litellm-db PVC 在线扩容 / NAS 配额告警
- `scripts/litellm-aliyun-logtable-truncate.sh` — 本 skill 的脚本
- `scripts/litellm-198-spendlog-emergency-truncate.sh` — 198 那把,**别混用**
- `k8s/litellm-log-retention-cronjob.yaml` — 阿里云 retention(缺 ToolIndex)
- `k8s/litellm-198-spendlog-retention-cronjob.yaml` — 198 retention(只覆盖 SpendLogs)
