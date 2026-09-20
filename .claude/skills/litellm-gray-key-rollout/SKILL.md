---
name: litellm-gray-key-rollout
description: 198 LiteLLM 网关按 key 名单灰度切流到新版本（force-gray 路线）。含选人、库对账、批量下发、双尺子验证、回滚边界、容量判据。适用于「把某些人/某个比例的 key 切到新版本」这类请求。
---

# LiteLLM 198 按名单灰度切流

**这个 skill 解决的问题**：把指定的一批虚拟 key 从旧版本切到灰度新版本，且切完能证明真的切了。

**不是这个 skill**：
- CarHer 实例灰度（改 ConfigMap / 换镜像）→ `hot-grayscale`
- 灰度环境本身的搭建、桥接、收敛、提交 → `litellm-gray-rollout/` 的 13 个脚本
- 按哈希均匀抽比例 → `gray-split-update.sh`（见下方「两条路怎么选」）

## 本 skill 自带的四个脚本（`scripts/`）

按顺序跑，一步一个脚本，别再现场拼：

| 脚本 | 跑在哪 | 干什么 | 对应章节 |
|---|---|---|---|
| `select-keys.py` | Mac | 从飞书导出的 ndjson 选人，出 `.keys`(0600明文) / `.b64` / `_expect.json`(无明文) | §2 |
| `reconcile-keys.py` | Mac | 读 `_expect.json` 生成对账 SQL（按 sha256，明文不进 SQL） | §3 |
| `apply-batch.sh` | 198 | 串行下发，隐藏 stdin，第一把失败就停并告诉你从第几行续 | §5 |
| `verify-rollout.sh` | 198 | 双尺子 + 容量体检，已内置排掉 30403 长连接 | §6 §4 |

阳性对照记录：`select-keys.py --pct 50` 在 2026-09-15 的数据上复现出与实际下发
**完全相同的 654 把**（sid 集合逐条相等）；`reconcile-keys.py` 生成的 196KB SQL
内 `grep -c 'sk-'` = **0**。

### 仓内配套脚本（不在本 skill 目录，但属于同一条流水线）

| 脚本 | 什么时候跑 | 判什么 |
|---|---|---|
| `k8s/monitoring/observability-preflight.py` | **第 −1 天，切流之前** | 尺子自己活不活。五条腿 LIVE/RED-ABLE/DELIVER/FAIL-RED/CADENCE，不过 exit 1。见 [[litellm-198-monitoring-ops]] |
| `litellm-gray-rollout/scripts/gray-monitor-cycle.sh` | 切流后每个观察周期 | 单周期采样 + 门禁判定（`gray-monitor-loop.sh` 是它的循环外壳） |
| `litellm-gray-rollout/scripts/collect-metrics.py` | 同上，被 cycle 调 | 从 Prometheus 取门禁那几条腿的原始数 |
| `litellm-gray-rollout/scripts/collect-spend-reconciliation.py` | 收尾对账 | SpendLogs 与门禁读数对齐。⚠️ 落库是**双峰刷盘**（98.4% 在 30s 内，尾巴 6069s），滞后只出 alert、缺行先 `pending` 跨 cycle 携带，**不许当回滚信号** |
| `litellm-gray-rollout/scripts/check-monitor-continuity.py` | **每次 `gray-split-update.sh <N>`（N>0）之前** | 上一段观察窗口「连续」且「够长」。缺口 + **周期数 < `--min-cycles`（默认 4 = `metrics.py` 的 `ABS_SUSTAIN_WINDOWS`）** 都出 `FAIL` |
| `litellm-gray-rollout/scripts/check-split-capacity.py` | **每次 `gray-split-update.sh <N>`（N≥50）之前** | 车道装不装得下目标比例。尺子 = **upstream-秒/秒/ready 容器**（= 并发，Little 定律），**不是请求数**：09-18 那窗 canary 承 21.4% 请求但 44.9% 工作量。`--per-container-concurrency` **没有默认值**（写死就是又一个 `llm-stab-scrape-down` 的字面量 5）；这道门禁 09-20 之前**根本没有生产者**，证据是手写的 |
| `litellm-gray-rollout/scripts/gray-workload-pin.sh` | **preflight 一次；每次给活 release 打补丁再跑一次；第 7 节 `helm upgrade litellm-product-proxy` 之后必跑一次**（09-21 起 `prod_verified` 和 `gray-convergence-commit.sh` 都要求 prod release 已被钉，`require_pinned_release`）。⚠️ **一次 pin 记的是整张表不是追加**，那一刻 gray 还扛 100% 流量 ⇒ 同一条命令里必须把 gray 那三项一起重报，否则服务中 build 的身份从记录里消失而 `require_workload_binding` 仍然绿（它只问"有没有钉住东西"） | 跑着的 build 到底是哪一个。把 chart `.tgz` / values / image digest 的摘要写进 `workload.env` 并摘进 `config_checksum`。09-21 之前这三样**只靠执行单的文字冻结**，代码一处都没摘 ⇒ `helm upgrade` 打完补丁 generation 不轮转、`verify_generation` 照过、**打补丁前批准的每一份 evidence 对打补丁后的负载依然有效**，sustain 连续计数继续替新 build 累加。钉一次就轮转 generation ⇒ 一步作废所有 gate 证据（放量重新挣 `split_sample`/`continuity`，≥50% 再挣 `split_capacity`，sustain 归零）。摘要没变时是 no-op，不会白轮转。⛔ chart 摘要只能取当天在 198 上 `helm package` 出的那个 `.tgz`，**"再打一次包比一比"在 198 上必然假红**（helm 3 把打包时刻写进内层 tar） |
| `litellm-gray-rollout/scripts/gray-progress.py` | **随时**（只读、恒 exit 0、不碰路由） | 现在第几/共几环、走过哪些、跳过哪些、每档观察了几个周期 vs 要求、已耗时、剩余（机器时间与等人签**分开报**） |
| `litellm-gray-rollout/scripts/replay-gate-negative-control.py` | **改了任何一条门禁腿之后**（本地、不碰集群） | 这条腿在**健康**流量上会不会乱响。把同一批请求同时标 `canary` 和 `stable`（构造上没有版本差 ⇒ 任何红都是假阳），过真 `metrics.py` 和真 sustain 状态携带。⛔ 全绿只证明它安静，**不证明它能响** —— 必须配 `--inject` 跑阳性对照，两个都跑完才能信任一条腿 |
| `scripts/litellm-set-live-weight.py` | 调权重 | 改的是承接生产那条 lane，属生产变更，先确认再动 |

**为什么 preflight 排在最前面**：09-13→09-20 那 38 个提交里 **11 个是在修门禁和监控
自己**，不是在修被测对象；根因是 Prometheus 六天没抓过生产而门禁一直在读它出的数。
先验尺子再谈阈值，顺序颠倒就会把返工重做一遍。

## 0. 两条路怎么选（先做这个判断）

| | `gray-split-update.sh <pct>` | `gray-key-route.sh force-gray`（本 skill） |
|---|---|---|
| 选人方式 | 按 key 哈希均匀抽 | 显式名单，一把把点 |
| 可接受比例 | 只有 `0/1/5/10/50/100` | 任意 |
| 门禁 | 先要 `workload_checksum != unbound`（没钉住 workload 的 run 比例上不去），再要 `split_sample` + `monitor_continuity` 证据，≥50% 再要**量出来的** `split_capacity` | `workload_checksum != unbound` + `key_pilot_entry`（09-21 起） |
| 自动止损 | 有（门禁背后的监控周期） | **没有** |
| 要明文 key | 不要 | **要**（要算 sha256 指纹） |
| 适合 | 常规按比例放量 | 指定人群 / 表里选特定行 / 门禁证据不存在 |

🔴 **`split=0` 不等于"还没有真实流量"。** `route_model.py` 的 `evaluate()` 里
`force_gray` 命中**排在** `bucket_for(key, split)` **前面**，所以这条路送过去的人
就是新 build 上的第一批真实生产用户（上一轮这一环跑了 84.8h）。而
`require_workload_binding` 过去只守 `split > 0`，于是**整条流水线里唯一没有任何东西
检查"跑的是哪个 build"的一环，正是这一环**。09-21 起 `force-gray` 要
`require_workload_binding` + `require_gate_evidence key_pilot_entry`；
`remove` 在 `split≠0` 时也要（摘掉 `protected-prod` 之后这个 key 会落回
`bucket_for()`，可能就上了 gray）。**`force-prod` / `protect-prod` 永不设门禁**——
把用户拉回旧 build 这条路必须在任何状态下都能走。

🔴 **「门禁在跑」≠「门禁有量具」**：脚本里 22 处 `require_gate_evidence`，
**只有 2 道有生产者**（`split_monitor_continuity`、`split_capacity`）。剩下 20 道里
有一批本来就是人签（bypass 处置、阶段跃迁、不可逆动作确认这些没有量具），但**谁是哪种
过去没写在任何地方**，读到 `require_` 的人会默认它已经在管。判一道门禁有没有用只看
「这份证据是**谁量**出来的」；分类表在手册 6.2.3，有测试盯着它跟脚本同步——
包括手册里那句 "一共 N 处" 的数字本身（它已经错过两次）。

两类证据现在形状不同：量出来的那两道必须带 `tool` + `schema_version=1`，而且
`result_sha256` **会被重算**（工具跑完之后改过任何字段就红）；人签那 20 道不许带
`tool`，必须带 **`signer`** 真名——`FILL_ME`/`TBD`/`operator`/`root` 全拒收
（sudo 之下所有人的 `$USER` 都是 root，那不指向任何人）。两类都加了一条：
`status=PASS` 但 `errors` 非空 → 红。

**默认应该走 split。** 只有下面这些情况才走 force-gray：
- 用户点名了具体的人（"把 linsen 的切过去"）
- 用户按业务字段选人（"编号最小的 20%"、"某部门的"）
- split 的门禁证据不存在且不该现造（真出事时不会想现场造证据）

**走 force-gray 必须当场告诉用户：这条路没有自动止损。** 新版本出问题不会有任何东西把这批人自动切回去，只能靠人发现。这句话不许省。

## 1. 环境（不 source 就全盘失败）

```bash
RUN=/root/litellm-gray-run/<run_id>          # 例 litellm-198-v195-20260914
set -a; . $RUN/nginx/frozen-env.sh; set +a   # ← 忘了这句，下面全报错
S=$RUN/src-3914155/litellm-gray-rollout/scripts/gray-key-route.sh
bash $S list                                  # 冒烟：能列出 sid 才算环境对
```

⚠️ **`list` 报 `invalid active generation: [Errno 2] ... '/var/lib/litellm-gray-rollout/active'` 不是状态坏了，是 `GRAY_ROOT` 没设**，即 `frozen-env.sh` 没 source。这个报错极具误导性，别顺着它去查状态文件。

## 2. 选人：明文从哪来

权威源是飞书多维表格：
- base `DlT9bsrwMad12VsogEpcK9Ptncc` / table `tblJT2s6Y6xjYj5A`（1311 行 / 28 字段）
- **明文全在 `API Key` 这一列**。`Cursor Key` / `Claude Code Key` 两列实际是空的，别读
- `编号` 字段返回的是**字符串**，不是数字。`isinstance(n, int)` 会把 1309 行全过滤掉

拉数据（一次拉完，不要分页循环）：

```bash
lark-cli base +record-list --base-token DlT9bsrwMad12VsogEpcK9Ptncc \
  --table-id tblJT2s6Y6xjYj5A \
  --field-id '编号' --field-id key_alias --field-id '邮箱前缀' \
  --field-id '状态' --field-id '是否在职' --field-id '账户类型' --field-id 'API Key' \
  --format ndjson --output ./all.ndjson --overwrite
```

- `--format ndjson` 默认 limit 2000，1311 行一次到底，**不用分页**
- 手写分页循环会撞 `JSONDecodeError`；没有 `--page-token`，翻页参数是 `--offset`/`--limit`（markdown/json 上限 200）
- `record-list` 是纯读接口。删记录要 `record-delete`/`record-batch-delete`——**永远不要调**
- 拉完之后所有筛选都在本地 ndjson 上做，不要反复打表

## 3. 库对账（下发前必做，不许跳）

表里的明文可能是陈旧的：**key 早被删了，表里那行还在**。这种 key 切过去会 401，而且症状跟"灰度切坏了"一模一样，会把下一轮诊断带偏。

用 CTE 比对，**明文永远不进 SQL**：

```sql
WITH t(n,h) AS (VALUES ('1','<sha256>'), ('2','<sha256>'), ...)
SELECT count(*) AS 送检, count(v.token) AS 库里有行,
       count(*)-count(v.token) AS 无行,
       count(*) FILTER (WHERE v.blocked) AS blocked,
       count(*) FILTER (WHERE v.expires IS NOT NULL AND v.expires < now()) AS 已过期
FROM t LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = t.h;
```

处置：
- **无行 → 必须剔除**。这是废 key，切过去就是 401
- **blocked → 可以切**。blocked 是 LiteLLM 层拒绝，跟走哪个池子无关，切不切都用不了。但要报给用户
- **已过期 → 可以切**，同上，要报
- 离职员工的 key → 问用户，别自己决定

SQL 必须在 `litellm-db-0` 里跑，proxy pod 没有 `psql` 也没有任何 DB driver：

```bash
base64 < q.sql | tr -d '\n' > q.b64      # 本地
# 198 上：
echo <b64> | base64 -d > /root/l3/q.sql
kubectl -n litellm-product exec -i litellm-db-0 -- sh -c 'cat > /tmp/q.sql' < /root/l3/q.sql
kubectl -n litellm-product exec litellm-db-0 -- psql -U litellm -d litellm -f /tmp/q.sql
```

⚠️ 双引号标识符在 `psql -c` 里会被当成列名解析（`ERROR: column "public" does not exist`）。写进 `.sql` 文件，字面量用 `$$...$$`。

## 4. 容量：下发前先量，不要事后解释

**判据不是「gray 有几个副本」，是「单副本要扛多少」。** 本轮实测的正确算法：

```
stable 侧：4 副本共 831m CPU 扛 485 请求/15min
gray  侧：1 副本      652m CPU 扛 406 请求/15min
→ gray 单副本已经比 4 个 stable 副本加起来还忙
→ 全量（100%）线性外推约 1.9 核，2 副本各摊 <1 核，limit 4 核 → 够
```

必查项：

```bash
kubectl -n litellm-product get deploy litellm-proxy litellm-proxy-gray \
  -o custom-columns=N:.metadata.name,REP:.spec.replicas,CPULIM:'.spec.template.spec.containers[0].resources.limits.cpu'
kubectl -n litellm-product top pod -l app=litellm-proxy-gray --no-headers
kubectl top nodes
kubectl describe nodes | grep -A5 'Allocated resources'   # 看 requests 不是看 limits
```

- **看 requests 不看 limits**。limit 超卖到 500%+ 是 K8s 常态，不是告警。requests 才是真占用
- **`MemoryPressure=False` 才算没压力**，光看百分比会误判
- gray 被 `nodeSelector: kubernetes.io/hostname` 硬钉在 standby 上。副本再多也只在那一台，**整台机器挂了新版本就全没**
- 198 那台（`aiyjy-litellm`）内存常年 75%、swap 已动，**永远不要往它上面加东西**

`map_hash_sizing()` 会自动扩（`render-production-nginx.py`：bucket 从 64 翻倍到覆盖最长 key，max_size 从 2048 翻倍到 `entries*4`），几百个 key 不用担心 map 溢出。

## 5. 下发

**一把 key 一次调用，一次 nginx reload，没有批量模式**（`grep -rn "batch|while read|--file|xargs"` 在脚本里查无此物）。所以：

**下发前先把 `key_pilot_entry` 证据备好**，否则第一把 key 就会被门禁挡下：

```bash
# 人签证据。signer 写真名，不是 root / operator / FILL_ME。
# run_id / generation / config_checksum 从当前状态读，不要手抄。
eval "$(bash $RUN/src-*/litellm-gray-rollout/scripts/gray-phase.sh current | sed 's/^/S_/')"
CS=$(sudo awk -F= '$1=="config_checksum"{print $2}' /etc/nginx/gray-route/active/state.env)
umask 077
sudo tee /root/litellm-gray-run/evidence/key_pilot_entry.json >/dev/null <<EOF
{"gate":"key_pilot_entry","status":"PASS","signer":"刘国现",
 "run_id":"$S_run_id","generation":"$S_generation","config_checksum":"$CS",
 "captured_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
sudo chmod 600 /root/litellm-gray-run/evidence/key_pilot_entry.json
```

证据吃 `run_id`+`generation`+`config_checksum`+24h，所以**中途 pin 过 workload
就要重开一份**（generation 轮转了）。这不是麻烦，这就是机制本身。

```bash
# 明文落地：base64 → 0600 文件，永不进 argv
umask 077
echo <b64> | base64 -d > /root/l3/batch.keys
chmod 600 /root/l3/batch.keys
awk '{if(length($0)<20||substr($0,1,3)!="sk-")bad++}END{print "格式异常:", bad+0}' /root/l3/batch.keys

# 串行下发，第一个失败就停
LOG=/root/l3/batch.applylog; : > $LOG; chmod 600 $LOG
n=0; ok=0; fail=0
while IFS= read -r k; do
  n=$((n+1))
  if printf "%s" "$k" | bash $S force-gray >>$LOG 2>&1; then ok=$((ok+1));
  else fail=$((fail+1)); echo "FAIL at line $n" >>$LOG; break; fi
  [ $((n % 50)) -eq 0 ] && echo "progress: $n ok=$ok fail=$fail"
done < /root/l3/batch.keys
echo "done: total=$n ok=$ok fail=$fail"
```

- `printf | bash` 走**隐藏 stdin**，key 不进命令行、不进 history、不进日志
- 速度约 1 把/秒。300 把以上必然超前台超时限制，**用后台任务**
- `break` 是故意的：失败即停，好定位。**恢复时从 `FAIL at line N` 的 N+1 续跑，不要整个文件重跑**（重跑虽然无害——已存在的 key 返回 unchanged——但会掩盖它停在哪）
- 事务模型是 `stage_from_active` → 改 stage 目录 → `render_fragments` → `nginx -t` → `commit_stage`。每次 reload 前都有 `nginx -t`，配置错了不会上线

## 6. 验证：两把尺子，缺一不可

### 尺子一：映射表对账

```bash
bash $S list | sed -n 's/.*sid=\([0-9a-f]*\).*/\1/p' | sort > live.sids
# 跟事先存的 expect.json（只含 n/alias/sid，无明文）逐条比
```

**sid 总数不等于「原有 + 本批」**，因为跟前几批重叠的 key 重复下发返回 unchanged 不新增。所以：
- ✅ 判据 = `expect.json` 里每个 sid 都在 `live.sids` 里
- ❌ 判据 = `sids == 原有 + 本批`（本轮我拿这个当 monitor 的 break 条件，永远不触发）

### 尺子二：真流量

```bash
awk -v cut="$(date -d '10 minutes ago' +%Y-%m-%dT%H:%M:%S)" '
  {split($1,a,"="); if(a[2]<cut) next
   if($0 ~ /upstream=127.0.0.1:30403/) next          # ← 必须排除，见下
   p="-"; for(i=1;i<=NF;i++) if($i ~ /^pool=/){split($i,x,"="); p=x[2]}
   c[p]++; if($3 !~ /^2/) e[p" "$3]++ }
  END{t=0; for(k in c) t+=c[k]
      for(k in c) printf "pool %-10s %5d (%.1f%%)\n",k,c[k],100*c[k]/t
      for(k in e) printf "  非2xx %s = %d\n",k,e[k]}' \
  /var/log/nginx/cc-auto-link.gray.log
```

**流量占比不等于 key 占比。** 本轮实测：切 20% 的 key 吃到 35% 流量（按编号取选中的是最早注册那批重度用户），切 50% 吃到 50.8%（样本大了偏差摊平）。所以按业务字段选人时，要提前告诉用户「流量占比会偏离 key 占比」。

## 7. 日志：两个文件，只有一个有 pool

| 文件 | format | 有 pool 吗 |
|---|---|---|
| `/var/log/nginx/zkreq.log` | `zkreq`（nginx.conf:39） | **没有** |
| `/var/log/nginx/cc-auto-link.gray.log` | `litellm_gray`（nginx.conf:90） | 有 |

格式：`ts=$time_iso8601 $remote_addr $status $request_time pool=$pool_label upstream=$upstream_addr rt=$upstream_response_time uri_class=$uri_class sid=$key_sid`

## 8. 三个已被证伪的坏尺子（本轮全踩过）

### ❌ 用 nginx 日志判「某人是否还在旧版本」

**所有 `pool=stable` 的行 `sid` 都是 `-`**，因为 `key-sid.map` 只给灰度名单里的 key 分配 sid。个体池子归属只有 `LiteLLM_SpendLogs` 按 `api_key = <sha256>` 能答。

### ❌ 用源 IP 认人

`10.68.13.97` 是站点 NAT 出口，一天 74 万请求，不是任何个人的机器。（同类：198/188 共用出口 `58.241.5.230`。）

### ❌ 用窄时间窗判「新版本独有的异常」

本轮我截 15 分钟窗口，看到 canary 10 个 101 / stable 0 个，就说"新版本独有异常"。放到全天：**stable 9155 个、canary 25 个，老版本多 366 倍**。而且全部 9669 个都落在 `upstream=127.0.0.1:30403`（ws-ingress），101 就是 WebSocket 握手成功的正常状态码。

**规则：判「A 有 B 没有」这类差异，样本窗口必须覆盖到两边都有足够量，并且要看落点（upstream）是不是同一个组件。**

## 9. 判时延要有对照组

「新版本慢」这个结论极易假成立。正确的尺子是**同一批人、同一 model_group、迁移前后自比，且带未迁移的人做对照组**：

```sql
WITH s AS (
  SELECT CASE WHEN left(api_key,12) IN (SELECT sid FROM mig) THEN 'migrated' ELSE 'control' END AS grp,
         CASE WHEN "startTime" >= '<切换时刻>' THEN 'post' ELSE 'pre' END AS win,
         "model_group" AS mg,
         EXTRACT(EPOCH FROM ("endTime"-"startTime")) AS dur
  FROM "LiteLLM_SpendLogs"
  WHERE "startTime" >= '<切换前若干小时>' AND "endTime" IS NOT NULL
)
SELECT mg, grp, win, count(*) n,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY dur)::numeric,2) p50
FROM s WHERE mg IN (SELECT mg FROM s GROUP BY mg HAVING count(*)>80)
GROUP BY mg,grp,win ORDER BY mg,grp,win;
```

本轮结论：canary p50 17.7s vs stable 0.42s，看着差 42 倍。但迁移组**切换之前**在旧版本上 p50 就已经 14.25s——差距是用户结构（切的是重度用户），不是版本。逐模型看切前后基本原地，`gpt-5.6-sol` 慢 4s 而对照组同期也在涨，即上游劣化。

**没有对照组就不许说"新版本慢"。**

## 10. 回滚：能回，但有一道单程门

| 动作 | 命令 | 可逆？ |
|---|---|---|
| 单把回旧版 | `printf "%s" "$k" \| bash $S force-prod` | ✅ 随时来回切，不动 phase |
| 单把移出名单 | `printf "%s" "$k" \| bash $S remove` | ✅ |
| **全体回旧版** | `gray-global-rollback.sh` | ❌ **单程票** |

`gray-global-rollback.sh` 把 phase 写成 `rolled_back`、frozen 置 1。而 `_lib.sh:1037` 的状态机转移表里**只有 `normal_gray:rolled_back`，没有反向**；`gray-key-route.sh` 和 `gray-split-update.sh` 都要求 `normal_gray && frozen=0`。所以**按完这个 run 就死了**，想再上新版本只能 `gray-run-close.sh` 关掉、重开 run 走全套 preflight。

而且它要 `global_rollback` 门禁证据（`GRAY_PROD_HEALTHY`）。**证据文件不存在时这个按钮按不下去**——真出事的时候不会想现场造证据。走 force-gray 路线前应该提醒用户预先备好。

**日常回滚就用逐把 `force-prod`。** 它不改 phase、随时可用、可来回切。

## 11. 交付话术模板（删除/覆盖类操作必须带）

```
动了啥：只往 /etc/nginx/gray-route/{force-gray.map,key-sid.map} 加了 N 行（纯加法），
        N 次 nginx reload，每次前都有 nginx -t。
        没碰生产 Deployment、没碰服务中的 Pod、没改生产库任何一行。
证据在哪：/etc/nginx/gray-route/generations/<gen>/render-attestation.json（带 sha256）
怎么回滚：逐把 gray-key-route.sh force-prod（推荐，可来回切）
          或 gray-global-rollback.sh（单程票，且需要门禁证据）
```

## 12. 收尾清理（明文不许留）

```bash
shred -u /root/l3/batch.keys          # 198 上的明文
shred -u ./batch.json ./batch.b64     # 本地的明文
# expect.json（只有 n/alias/sid）可以留，没有明文
```

⚠️ 从飞书拉的 `all.ndjson` 含 1309 把明文，用完 shred。

⚠️ 打印 key 只允许 `len=N head=sk- tail=XXXX sha256head=<12hex>` 这种形式，全值永不出现。

⚠️ 杀进程按显式 PID + `ps -p` 守卫。`pkill -f <脚本名>` 是宽选择器，禁用。

## 13. 本轮实测记录（run_id litellm-198-v195-20260914）

| 轮次 | key 数 | 编号区间 | 实际流量占比 | 失败 |
|---|---|---|---|---|
| 点名（linsen 等 3 人） | 5 | — | — | 0 |
| liuguoxian* | 8 | — | — | 0 |
| 20% | 261（剔 1 废） | 1~275 | **35%** | 0 |
| 50% | 392 新增（累计 653） | 1~714 | **50.8%** | 0 |

- 剔除的废 key 恒定是编号 130 `claude-code-heweiwei`（库里无行）
- gray 从 1 副本扩到 2 副本用 `kubectl scale`（**禁 `apply`**），原 pod 不重启
- 50% 时两个 gray pod 共 688m CPU / 7.4Gi，standby 节点 CPU 17% / 内存 39%，余量宽
- 无法迁移（表里无明文）：`cursor-linsen-03gc`、`cursor-zhuyida-3pfs`、`ceshi-zhuyida-3sth`

### 13.1 这一轮整体耗时的实测拆解（`gray-progress.py` 从 675 个 generation 反推）

| 环节 | 停留 | 备注 |
|---|---|---|
| ① 预检 | 17h58min | |
| ② guarded-old 保险丝 | 4h32min | |
| ③ 进 normal_gray（0%） | 5h54min | |
| ④ 指名 key 试点 | **3d06h53min** | 其中 **79 小时零提交零换代**，纯空转 |
| ⑤.1~⑤.5 放量 1→100% | **83min** | 整条放量梯子 |
| ⑥~⑨ 收敛到提交 | 19min | |
| **合计** | **126.0h** | |

**结论：放量本身不慢（83 分钟），慢在两处。** 一是那 79 小时空转，紧接着爆出六个
连续的"门禁自己坏了"修复提交 —— 时间花在发现尺子是错的。二是 ③→④ 那 662 个
generation 是在一天里逐个手工加 key。

判"现在到哪了"不要凭印象，跑 `gray-progress.py`。它只读、恒 exit 0、可在放量中途跑。

⚠️ **放量档位停留不足 sustain 深度 ⇒ 止损腿没武装而门禁全绿**：这一轮 5%/10%/50%
各只跑了 2 个监控周期，而绝对 5xx 止损腿要连续 4 个窗口（`ABS_SUSTAIN_WINDOWS=4`）。
五档里三档的止损腿是熄灯的。现在 `check-monitor-continuity.py` 会以
`INSUFFICIENT_CYCLES` 拦住，台账也会写明"没有武装过"。下一轮**每档至少停 4 个周期
（300s 间隔 ⇒ 20 分钟）**，机器时间下限总计 ≈ 1h40min，别再压到 9 分钟。

### 13.2 新写一个门禁脚本的验收清单（09-20 这轮踩全了）

写完一个像 `check-split-capacity.py` 这样的量具，四步一步都不能省：

1. **两个对照都进测试套件，不是手跑一次就算。** 阴性（健康流量上它安静）+ 阳性（真有故障时它会红）。只有阴性 = 只证明它不吵，不证明它有用。
2. **⛔ 阳性对照「过」了是信息，不是无事发生。** 09-20 那次改 fixture 里 `"lane"` 的名字，预期红结果绿——原因是 fixture 自己的 `data` 同时喂给了两边，改名两边一起变、互相抵消。不许把这种绿当通过：要么改错了地方，要么测试的覆盖比它 docstring 承诺的窄，**后者必须把 docstring 改窄**，否则下一个人读到的是一个不存在的保证。
3. **`chmod +x`，并且**别指望"按名单校验模式位"的测试帮你发现忘了。带 `#!` 却没 `+x` 的文件，**恰好在文档写着那条命令的地方**报 permission denied。09-20 有 5 个文件是这个状态，包括当天刚写的 `check-split-capacity.py` 本身，而当时的测试吃的是手维护名单 ⇒ 名单外的看不见。这类检查一律**扫目录认 shebang**。
4. **顺手核一遍文档里写的调用形状是真的**（flag 名、位置参数、输出文件名）。凭印象写进 SOP 的命令，换台机器照着走就是一条死路。

## 14. 全量（100%）不该走这条路

全量**不需要明文、不需要选人**——直接把默认路由从旧版切到新版即可（`nginx.conf` 里 `$normal_inference_upstream` 的 default 分支 / `whole_product_override`），它不认 key 只管流量。

走全量前先确认：
1. gray 副本数 ≥ 2（单副本挂了就是 100% 断）
2. 全局回滚门禁证据已备好
3. 已有一轮 50% 级别的真流量观察，无 5xx 抬升
