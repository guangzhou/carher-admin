---
name: litellm-198-diskpressure-502
description: 198 LiteLLM 盘满→disk-pressure→db/redis 被驱逐→全站 502 死锁的应急 runbook。含 60 秒定因三腿（先证伪"是我改的"，再查并行 session —— "不是我"≠"没人干"）、db pod 已被驱逐时的 offline 单用户 TRUNCATE 破窗锤、eviction 转换期的等待纪律（09-08 实测 41 分钟，且要读 kubelet 自己的四个信号而非 df）、事后用 relfilenode/n_tup_del 分辨 TRUNCATE vs DELETE 的取证法与幸存表清单，以及 2026-08-30/09-08 两次复发暴露的增长几何（20~30GB/天 × 10 天保留，plain VACUUM 不回收空间）。
---

# 198 LiteLLM 盘满 502 死锁 runbook

> 实证：**2026-08-30 第一次**（SpendLogs 336G 撑爆 /Data，事后建了 retention CronJob）；
> **2026-09-08 原样复发**（345G，/Data 413G/492G）。两次形状一模一样 ⇒ 第一次的防线不够，
> 这份 runbook 既是止血流程，也是"为什么会复发"的档案。
>
> 直连凭据见 memory `reference_198_direct_ssh`（**直接 ssh，不走 jms**）。

## 0. 故障形状（怎么认出是这一类）

```
/Data 用量 > 90%
  → kubelet nodefs.available<10% → 给 node 打 node.kubernetes.io/disk-pressure:NoSchedule taint
  → 驱逐管理器按 priority 排序，从低到高逐个 evict（198 上有 ~70 个 pod 要啃）
  → 最终轮到 litellm-db-0 / litellm-redis-0，照样被 Evicted
  → StatefulSet 重建的替身在 kubelet 准入处被拒（Failed to admit, nodeCondition=["DiskPressure"]）
    → 建→拒→删 热循环，pod 对象只存在毫秒级
  → litellm-proxy 连不上 db → 0/1 not ready → nginx 上游全挂 → 用户面 502
```

**鸡生蛋死锁**：想清盘得进数据库，可数据库已经被盘满赶走了。这就是本 runbook 存在的理由。

### ★ 为什么 `kubectl get pod litellm-db-0` 报 NotFound 而不是 Pending

因为它不是"调度不上"，是**准入被拒后立刻被删、然后 StatefulSet 再建**的热循环。
2026-09-08 实测 `kubectl -n litellm-product get events` 里同一秒内反复出现
`Scheduled` → `Evicted: The node had condition: [DiskPressure]`。
所以任何"先 `kubectl exec` 进 db"的方案在这个窗口里都不可能成功 —— 直接走 §2.1 offline。

### ★ 08-30 的"防死锁几何"为什么没兜住（2026-09-08 定因）

08-30 的修复给 db/redis 加了 `priorityClassName: litellm-infra-critical`（value **1,000,000**）
+ disk-pressure toleration，当时记的结论是"盘再满也不被驱逐"。**这条结论是错的**，09-08 现场数据：

| 观察 | 数据 |
|---|---|
| 几何还在吗 | 在。两个 sts 的 prio + toleration 都 live，PriorityClass=1000000 也在 |
| 排序生效了吗 | 生效。kubelet `pods ranked for eviction` 里 prod 的 db/redis 排在**最末**（~70 个之后） |
| 但还是被驱逐了吗 | 是。`21:24:51 Evicted ... litellm-product/litellm-db-0` |
| 替身能起来吗 | 不能。`Failed to admit pod to node nodeCondition=["DiskPressure"]` 刷屏 |

**机制**：kubelet 的 DiskPressure 准入豁免只认 `IsCriticalPod`，门槛是 priority ≥ **2,000,000,000**
（内置 `system-cluster-critical`/`system-node-critical`）。**toleration 管的是调度器的 taint，
管不了 kubelet 的准入检查**；自定义 PriorityClass 只改**驱逐顺序**，不给**准入豁免**。

两条候选修法当场 server-dry-run 验过（不落地）：

```
A) 自定义 PriorityClass 提到 2e9
   → 被 apiserver 直接否决：
     Forbidden: maximum allowed value of a user defined priority is 1000000000
   ⇒ 自定义类**永远**够不到 critical 门槛，此路不通。

B) litellm-product 里直接用内置 system-node-critical
   → pod/probe-snc created (server dry run)
   ⇒ 本集群没有 namespace 限制，**可行，这是唯一能真正豁免的做法**。
```

同一缺陷也打在 `spendlog-retention` CronJob 上：它一样只有 toleration + 1e6 优先级，
所以"盘满时清理任务自己能调度"的自愈设计**同样不成立**（未在现场取证，见 §5 未定因）。

## 1. 先证伪"是我刚才那个改动引起的"（60 秒，三条独立腿）

出事时最省事也最容易带偏全局的结论，就是"刚动过 X ⇒ X 干的"。按 CLAUDE.md 三段式，
说出口前必须先花一次查询打它的证伪腿。这三条腿彼此独立，任一条成立即可排除：

| 腿 | 查询 | 排除判据 |
|---|---|---|
| 时间 | `kubectl get node <n> -o jsonpath='{.status.conditions[?(@.type=="DiskPressure")].lastTransitionTime}'` | taint 时间**早于**我的改动 ⇒ 不是我 |
| 空间 | 挂掉的 pod 落在哪些节点 | 我只改了 A 节点，B 节点的 pod 也挂 ⇒ 不是我 |
| 形状 | SpendLogs `metadata->'error_information'->>'error_class'` / 现场 curl | 错误是 **TCP connection refused**（有 IP、连不上）而非 **name resolution failure** ⇒ 不是 DNS 类改动 |

2026-09-08 实战：我 14 分钟前刚改过 host DNS，三条腿全部指向"不是我"，真因是磁盘。
**先打这三腿再动手**，否则会花掉宝贵的分钟去回滚一个无关改动。

### 1.1 ★ 排除掉自己之后，下一句**不是**"未定因" —— 先查并行 session

198 是多人 + **多个 Claude session 并行**作业的生产环境（`ListAgents` 09-08 实测同机 7 个 peer）。
"我这条会话的操作记录里没有" ⇒ 只排除了我，**不等于没人干**。09-08 我就在这里栽了一次：
查完 `pg_class` 坐实 SpendLogs 被 TRUNCATE、`n_tup_del=0`、db 上一个容器日志已随 Evicted pod
丢失、两台机器 `bash_history` 无匹配，于是报了"谁干的没有证据，未定因"——
**而真相是并行 session 正按本 runbook §2.1 止血**，它同时还在往仓库里写这份 skill。

排除自己之后按顺序打这三枪，再谈"未定因"：

```bash
# ① 同机并行 session（最容易命中，最便宜）
#    ListAgents；必要时 SendMessage 直接问"是不是你清的表"
# ② 仓库里刚落地的未提交文件 —— 别人的止血往往先变成 skill/script
git status --short && git log --oneline -3
ls -lt --time-style=full-iso .claude/skills/*/SKILL.md scripts/*.sh | head
# ③ 集群侧留痕
kubectl -n litellm-product get job --sort-by=.metadata.creationTimestamp | tail
kubectl -n litellm-product get events --sort-by=.lastTimestamp | tail -40
```

②的判据很硬：**文件 mtime 落在故障窗口内、且正文自述"我 truncate 了"** —— 这是物证不是猜测。
详见 memory `feedback_not_me_is_not_nobody_check_parallel_sessions`。

## 2. 止血：清表（唯一能立刻回收空间的手段）

盘已满时 `DELETE` + `VACUUM` **没用**：DELETE 需要临时空间和长锁，plain VACUUM 只把页
标成可复用、**不把空间还给文件系统**。只有 `TRUNCATE`（unlink 关系文件）当场释放。

```bash
# auto 模式会自己判 online/offline
FORCE=1 bash scripts/litellm-198-spendlog-emergency-truncate.sh
# 连没人管的 ToolIndex 一起清
TABLES='LiteLLM_SpendLogs LiteLLM_SpendLogToolIndex' FORCE=1 bash scripts/litellm-198-spendlog-emergency-truncate.sh
```

### 2.1 ★ db pod 已被驱逐 = 盘满时的常态，`kubectl exec` 路径必死

旧版脚本只有 online 路径，2026-09-08 一跑就是
`Error from server (NotFound): pods "litellm-db-0" not found` ——
**它写来救的那个场景，恰恰是它跑不了的场景**。offline 破窗锤（已固化进脚本）：

```bash
NS=litellm-product
# 1) 保证没有第二个 postmaster（单用户模式的硬前提）
kubectl -n $NS scale sts litellm-db --replicas=0     # 等 pod 真的消失再往下

# 2) 绕开 k8s，直接对 hostPath PGDATA 开单用户模式
PVCDIR=$(ls -d /Data/rancher/storage/*_${NS}_data-litellm-db-0)
docker run --rm --user 999:0 -v $PVCDIR:/var/lib/postgresql/data \
  127.0.0.1:5000/postgres:16-prod-71e27bf \
  sh -c 'echo "TRUNCATE TABLE \"LiteLLM_SpendLogs\";" | postgres --single -D /var/lib/postgresql/data/pgdata litellm'

# 3) 拉回来
kubectl -n $NS scale sts litellm-db --replicas=1
```

**镜像在 docker 里，不在 containerd**：`k3s ctr run` 会报
`image "127.0.0.1:5000/postgres:16-prod-71e27bf": not found`（那是 containerd 的 `k8s.io`
namespace，sts 声明的 `postgres:16` 也未必在）。offline 一律走 `docker`，并从
`docker images | grep postgres` 里挑现成的。

**helper 函数里绝不许写 `2>/dev/null`**：2026-09-08 我的临时 helper 吞掉 stderr，
`ctr` 的 image-not-found 一个字没打出来，命令静默没执行，而我差点把「没清掉」读成
「清掉了 7GB」。真相是 `base/` 还是 345G。详见 memory
`feedback_helper_2devnull_hides_the_error_and_fakes_success`。

### 2.2 ★ 多表清理曾静默只清第一张（2026-09-13 已修）

`TABLES='A B'` 跑下来只有 A 被清、B 原封不动，脚本 `exited with code 1` 且**不打任何错误**。
不是 FK（两表间无 FK，查过），也不是锁（B 单独重跑 3.8 s 就过）。真因在 helper：

```
psql -Atq 执行 TRUNCATE 输出 0 行
  → 管道尾的 `grep -v '^\[sudo'` 拿到空输入 ⇒ 退出码 1
  → `pipefail` 把整个 ssh198 变成 1
  → `set -e` 在第一张表成功之后立刻中止循环
```

即**「命令成功」被 grep 翻译成了「失败」**。已改成 `sed '/^\[sudo/d'`（无论删没删到行都返回 0，
pipefail 于是透出 ssh/psql 的真实退出码），并给循环加了逐表 `[ok]` 回执。

> 通用教训：`grep` 放在管道尾部就是一把会把空输出读成红的坏尺子。过滤器要用 `sed`/`awk`。

**判据仍然是 relfilenode，不是退出码**：清完必须按 §3bis 逐表查
`oid <> relfilenode`，别拿脚本 rc 当交付。

### 2.3 不要浪费时间的两件事

- **journald / 系统日志**：`journalctl --vacuum` 只清 `/`，而 DiskPressure 是按 **`/Data`**
  判的。2026-09-08 实测释放 3GB（42G→39G），对故障零贡献。
- **`crictl rmi --prune`**：同理，释放量与 345G 的表不在一个量级。
- 盘满时**别跑全盘 `du`**：慢且会被反复打断；直接查 `pg_total_relation_size` 排序定位。

## 3. 清完不会立刻好：5 分钟转换期是预期，不是失败

kubelet 的 **`eviction-pressure-transition-period` 默认 5 分钟**：盘已经空了，node 的
DiskPressure 也要熬满这段才翻回 False、taint 才摘、db/redis 才能被调度。

**这段时间内看到"还是 502"是正常的**。别去 force-delete，别反复 scale，别提前手动摘 taint。
熬过转换期还带 taint 才手动摘：

```bash
kubectl taint node aiyjy-litellm node.kubernetes.io/disk-pressure-
```

### 3.1 ★ "5 分钟"是**阈值满足之后**才起算 —— 09-08 实测熬了 41 分钟

2026-09-08 现场：`21:07:34` 起 DiskPressure=True，`21:48:12` 才翻回 False，**跨度 41 分钟**，
远超"5 分钟转换期"的直觉预期。原因是转换期从**信号降到阈值以下的那一刻**才开始计时，
而 truncate 释放空间到 kubelet 下一轮 housekeeping 采样到、再熬满 5 分钟，中间还叠了
`/Data` 因 ~70 个 pod 重建而短暂回涨。**不要因为"超过 5 分钟还没好"就判定止血失败去加码操作。**

判"到底还差什么"用 kubelet 自己的读数，别用 `df`（两者口径可能不同）：

```bash
k3s kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary \
 | python3 -c 'import json,sys;d=json.load(sys.stdin)["node"]
for lbl,f in (("nodeFs",d.get("fs")),("imageFs",d.get("runtime",{}).get("imageFs"))):
    if f: print("%-8s avail%%=%.2f inodesFree%%=%.2f"%(lbl,100*f["availableBytes"]/f["capacityBytes"],100*f["inodesFree"]/f["inodes"]))'
```

四个信号（nodefs.available / nodefs.inodesFree / imagefs.available / imagefs.inodesFree）
**全部**健康、node 却仍报 True ⇒ 就是在熬转换期，唯一正确的动作是**等**。
配套看 `journalctl -u k3s | grep 'Failed to admit'`：这行**停止刷屏**的时刻 ≈ 转换期结束。

## 3bis. 事后如何分辨"数据是被 TRUNCATE 的"还是"被 DELETE 的"

止血用的 TRUNCATE 会留下可验的指纹。事故复盘时（尤其**别人/并行 session 止的血**，
你自己会话里查不到痕迹）用这三列一次判定：

```sql
SELECT relname, oid, relfilenode, (oid<>relfilenode) AS rewritten FROM pg_class
 WHERE relname='LiteLLM_SpendLogs';
SELECT n_tup_ins, n_tup_del, n_live_tup FROM pg_stat_user_tables
 WHERE relname='LiteLLM_SpendLogs';
```

| 指纹 | TRUNCATE | DELETE（retention CronJob） |
|---|---|---|
| `relfilenode`（表 + **每个索引**都换）| ≠ `oid` | == 原值，不变 |
| `n_tup_del` | **0** | > 0 |
| 幸存范围 | 整表清空，最早行 == db 重启时刻 | 只砍 cutoff 之前 |

⛔ **`pg_stat_database.stats_reset` 为空不代表没发生过 TRUNCATE** —— TRUNCATE 不重置统计。
⛔ **db pod 被 Evicted 后 `kubectl logs --previous` 必然 `container not found`**，
postgres 侧的证据链就断了；`pg_class` 那三列是唯一还在的物证。

09-08 实测（本次）：`relfilenode 23460466 ≠ oid 16508`、6 个索引全换、`n_tup_del=0`、
存活 257 行且最早行 == db 启动后 20 秒 ⇒ TRUNCATE 坐实。

**幸存/丢失清单**（决定复盘还能不能做，先查这个再决定要不要慌）：

| 表 | 09-08 事后状态 |
|---|---|
| `LiteLLM_SpendLogs` | **清空**，逐请求原始日志丢失 |
| `LiteLLM_DailyTagSpend` | 完好，174,027 行，`2026-05-03`→今天 |
| `LiteLLM_SpendLogToolIndex` | 完好，22,649,728 行，`2026-05-22`→今天（`oid==relfilenode`）|

⇒ **日聚合口径的历史都在**，按天/按 tag 的用量复盘照做；丢的只有逐请求明细。
报"数据丢了"之前必须把这三行查完，否则会把"明细丢失"夸大成"账没了"。

## 4. 恢复判据（pod Ready 不算好）

```bash
kubectl -n litellm-product get pod -l app=litellm-proxy   # 期望 4/4 都是 1/1 Running
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1/health/liveliness   # 期望 200
df -h /Data
```

## 5. 为什么会复发 —— 增长几何（2026-09-08 实测）

清表后表是空的，正好当量具。**truncate 后 18 分钟涨到 291MB ⇒ ≈23GB/天**。

**第二把独立量具（09-08 22:00 另一条会话实测，交叉验证）**：恢复后对 `/Data` 做 60s 两点采样，
`delta = 20 MB/min ⇒ ≈28.8GB/天`。两把尺子（表级 291MB/18min vs 盘级 20MB/min）
**同一量级、相差 25%**，差额来自 ToolIndex/WAL/其它 pod 也在写盘 —— 结论稳：**20~30GB/天**。
```bash
S1=$(df --output=used -k /Data | tail -1); sleep 60; S2=$(df --output=used -k /Data | tail -1)
echo "delta=$(( (S2-S1)/1024 )) MB/min"
```
增长的直接来源是 `STORE_PROMPTS_IN_SPEND_LOGS=True`（deploy env + CM `general_settings` 各一处，
09-08 实测两处都是 true）：**整段 prompt 进 SpendLogs**。关掉它是把增速砍一个数量级的最短路径，
代价是丢 prompt 级审计 —— 属于 §6 要用户拍板的范畴，别擅自改。

| 事实 | 数 |
|---|---|
| /Data 总量 | 492G |
| SpendLogs 增速 | ≈23GB/天 |
| `RETENTION_DAYS` | 10（`k8s/litellm-198-spendlog-retention-cronjob.yaml`） |
| ⇒ SpendLogs 稳态 | **≈233GB**，占盘 47% |
| `LiteLLM_SpendLogToolIndex` | 25GB，**没有任何 CronJob 覆盖** |

三个结构性问题，retention CronJob 一个都没解：

1. **保留窗口本身就顶到天花板**。10 天 × 23GB/天 = 233GB，加上 ToolIndex 和非 db 占用，
   基线就在 70% 上下，任何流量抬升直接撞 90% 阈值。
2. **plain VACUUM 是棘轮，不是回收**。CronJob 结尾是 `VACUUM (ANALYZE)`，只有尾部全空页
   才会截断文件。表文件按历史峰值抬高水位后**永不回落** —— 一个突发日会永久抬高地板。
   真正能还空间的只有 `VACUUM FULL` / `pg_repack`（都要等量临时空间，盘满时用不了）
   或者**改成按天分区表 + DROP PARTITION**（unlink，跟 TRUNCATE 同一性质）。
3. **ToolIndex 完全没人管**，25GB 且只涨不落，是下一个爆点。

> ⚠️ 未定因（禁止讲成结论）：事故当时 SpendLogs 345GB ≫ 233GB（10 天理论量）。
> 差额来自「文件高水位棘轮」还是「当时流量确实更高」**分辨不出** —— 表已被我 truncate，
> retention job 的容器日志也已随盘清理丢失（`unable to retrieve container logs`，
> `/var/log/pods/` 下对应目录已空）。
> 证伪路径：从现在起给 CronJob 加日志留存（写进表或外发），并每天记一次
> `pg_total_relation_size` vs `count(*)`；两者比值持续上升 = 棘轮，比值平稳 = 纯流量。
> **两种成因的修法相同**（缩窗口 / 分区表），所以不必等结论就能动手。

## 6. 建议的根治（未经用户批准，勿擅自 apply）

按性价比排序：

0. **把 db/redis 的 `priorityClassName` 换成内置 `system-node-critical`**（已 server-dry-run
   验证本集群可用）。这是唯一能让它们在 DiskPressure 下**免于驱逐、且替身能通过准入**的做法；
   现有的 `litellm-infra-critical`(1e6) 只改排序不给豁免。改完 db/redis 至少能活着，
   §2 就能走 online 路径而不必破窗。`spendlog-retention` CronJob 同改。
1. `RETENTION_DAYS` 10 → 3~5（一行 env，立竿见影，代价是审计窗口变短）。
2. 给 `LiteLLM_SpendLogToolIndex` 加同款 retention CronJob（当前零覆盖）。
3. 加磁盘水位刹车 CronJob：`/Data` > 75% 就触发一次紧急 retention，别等 kubelet 的 90%。
4. SpendLogs 改**按天分区**，retention 变成 `DROP PARTITION` —— 唯一真正把空间还给
   文件系统、且不需要临时空间的做法。改动最大，但这是唯一能终结复发的方案。

> 0 是止损（坏了也能修），1~4 是治本（不让它坏）。两类都要，别只做一类。

## 相关

- `k8s/litellm-198-infra-priority.yaml` —— 防死锁几何（db/redis/retention 的
  `priorityClassName: litellm-infra-critical` + disk-pressure toleration，让清理任务自己能调度）
- `k8s/litellm-198-spendlog-retention-cronjob.yaml` —— 日常保留
- `scripts/litellm-198-spendlog-emergency-truncate.sh` —— 本 runbook 的执行体
- skill `k3s-198-node-expand` —— 如果结论是"盘就是不够"，扩节点/扩盘走那条
- [[litellm-aliyun-logtable-truncate]] —— **阿里云(ns `carher`)是另一套,别把这份 runbook 套过去**:
  那边是 CNFS NAS(`df` 恒显示 10P 量不出水位)、无 disk-pressure、db pod 常驻 Running,
  所以只有 online 模式、也没有等 taint 撤销这一步。两边表结构还不一样:
  阿里云的大头是 `LiteLLM_SpendLogToolIndex`(实测 19 行/请求,是 SpendLogs 的 19 倍)。
