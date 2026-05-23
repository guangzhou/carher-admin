---
name: her-memory-reindex-rescue
description: >-
  排查并救援 CarHer her 实例因 **memory pipeline（reindex / cache bloat）** 引发的
  OOM / 孤儿 tmp 文件 / 卡死实例 / event-loop 卡顿 / **客户端 turn timeout（"model idle timeout"）** 连锁反应。
  本 skill 覆盖两类失败模式（共享同一物理层：NFS NAS 上的 sqlite 同步调用 block event loop）：

  **模式 A：reindex 死循环**。Use when the user mentions "main.sqlite.tmp-*", "孤儿 tmp",
  "reindex 循环", "needsFullReindex", "卡死的 her" + "main.sqlite 多天不更新"，或
  "embedding 卡 → ws 断 → 错过消息 → 重连 → 又卡" / "[memory] embeddings rate limited" /
  "[memory] sync failed: memory embeddings batch timed out after 120s" /
  "bot-registry re-registered after key expiry" / "[ws] handshake timeout" /
  "litellm cooldown" 等 event-loop 被 reindex 同步循环 block 100-260s 或上游
  embedding 持续 429/超时 引发的连锁症状，or wants to scan/clean orphan tmp files
  cluster-wide.

  **模式 B：embedding_cache / chunks 过大导致 prework cold scan stall**。Use when 用户报
  "model idle timeout / Please try again / increase models.providers.<id>.timeoutSeconds"
  这种 OpenClaw user-facing 错误文案；或 her 回复超时（120-130s 后才 surface_error）；或
  LiteLLM SpendLogs 在 timeout 窗口内 **0 条** her 请求记录（请求根本没发出去）；或
  main.sqlite 体积异常大（>500 MB，embedding_cache 主导）。

  **本 skill 仅处理 memory pipeline 相关的 OOM / 卡顿 / timeout 场景**——
  泛 OOM / 阿里云 ACK 阈值告警 / compaction archive 累积 / active session 巨大
  请先用 `her-oom-alert-triage` 分诊；命中上游 LiteLLM fail 跳 `litellm-ops`；
  纯 reply 失败分诊用 `carher-her-reply-failure-triage`。

  本 skill 提供从「8 个判断 → 全集群扫描 → 模式 A 的 6 档修复（A 原地清孤儿 / B 等自愈 /
  C 删 pod / D paused-toggle / E 修上游 / F 切 fts-only）+ 模式 B 的 GC + VACUUM 修复」
  的完整决策树；做运维层治标，不修后端代码根因。
---

# Her Memory Reindex 死循环 排查与救援

## 适用场景判定（开工前先做）

只有同时满足下面两条才用本 skill：

1. `/data/.openclaw/memory/main.sqlite.tmp-*` 孤儿 tmp 堆积
2. `/data/.openclaw/memory/main.sqlite` mtime 多天前（说明 reindex 一直没成功 swap）

**如果用户只是说"OOM"或"内存告警"，先用 `her-oom-alert-triage` 分诊**——大多数"OOM 告警"实际是阿里云 ACK 阈值告警 / compaction archive 累积导致的内存爬升，不是 reindex 死循环。错误地走本 skill 会做不必要的 paused-toggle 重启。


## 现象速览

her 实例反复 OOMKilled / restartCount 持续增长 / `/data/.openclaw/memory/` 里堆积大量 `main.sqlite.tmp-*` 文件 / `main.sqlite` 长期不更新（mtime 几天前）。表现为 her 偶尔下线、日志里高频出现 reindex 启动但很少看到 reindex 完成。

## 根因（必读，不超过 200 字）

`MemoryManager.runSafeReindex` 把整个 memory 数据库重建到 `main.sqlite.tmp-<uuid>`（流式拉 embedding 写入），跑完后 swap 成新的 `main.sqlite` 并 `writeMeta`。

死循环路径：

```
（embedding 上游切换、base-config 改 baseUrl、或个别实例做了 memorySearch override
 → providerKey 变了，受影响实例的 needsFullReindex=true）
         ↓
needsFullReindex = true → 创建 tmp 开始重建
         ↓
重建到一半，单 pod 内存峰值 4-5 GB > 3Gi limit
         ↓
OOMKilled → tmp 文件留在 PVC（fd 没释放 = 等 pod 真正退出后变孤儿）
         ↓
新 pod 起来 → 看到旧 tmp + meta 仍然不匹配 → 又触发 reindex
         ↓
循环
```

**关键点**：清孤儿 + 干净重启后，`needsFullReindex` 通常会变成 `false`（因为 main.sqlite 的 meta 已经是当前 embedding 上游算出的 `providerKey`），pod 不再触发 reindex 直接进入正常工作模式——只要打破 tmp 累积循环就够了。

> ⚠️ **集群可能并存多套 embedding 上游**：例如一部分实例在 user-config 里 override 了 `memorySearch.remote.baseUrl` 走 OpenRouter 直连，另一部分走 base-config 默认的 LiteLLM proxy（`http://litellm-proxy.carher.svc:4000`）。每套上游算出的 providerKey 不同。判断"meta 是否一致"时**不要硬编码对照 hash**，要去同集群健康的、同上游配置的兄弟实例（比如 `carher-2` vs `carher-188`）实测对照。判断方法见下面的"快速比对"片段。

## 诊断方法论：7 个关键判断（先做这些再动手）

每个判断都给出"一行命令 + 怎么读结果"。这套判断的意义：**避免误诊**（不是 reindex 问题不要走本 skill；也不要把上游 fail 误诊成内部死循环）+ **选对修复方案**（决定 inplace rm / 等自愈 / 重启 / 修上游）。

**判断 1-3** 看症状现场（fd / size 漂移 / 锁），**判断 4-6** 看 carher pod 内部（meta / 日志 / 集群级触发因子），**判断 7** 看上游（LiteLLM 健康）—— 必须把 4 和 7 都做完才能下"重启大概率有用"的结论。

### 判断 1：tmp 是孤儿还是 active？（决定能不能在活 pod 里直接 rm）

```bash
kubectl exec -n carher "$POD" -c carher --request-timeout=15s -- sh -c '
cd /data/.openclaw/memory
for f in main.sqlite.tmp-*; do
  [ -e "$f" ] || continue
  fd=$(ls -la /proc/*/fd/ 2>/dev/null | grep -c "$f" || true)
  echo "$f fd=$fd $([ "${fd:-0}" = "0" ] && echo ORPHAN || echo ACTIVE)"
done'
```

- `fd=0` ORPHAN：上一个 pod 留下的死文件，**当前进程不持有** → 可在活 pod 内 `rm` 安全释放，不影响主程序，**零下线**。
- `fd>=1` ACTIVE：当前 pod 的 reindex worker 还在写它，**rm 会让进度白丢**（unlink + open fd 在 Linux 合法但于事无补）。

### 判断 2：reindex 是真在工作还是僵死？（决定要不要打断）

拍两次快照对比 size + mtime：

```bash
# T0
kubectl exec ... -- stat -c '%n size=%s mtime=%Y' main.sqlite.tmp-*
sleep 180
# T1：再拍一次
```

- size 增长 + mtime 持续变 → **真在写**，让它跑完是对的（缓慢但前进）
- size 不变 / mtime 凝固 → **僵死**，重启 pod 才能脱困
- 经验值：~5 MB/min 是 her-68 案例下的"在跑但极慢"基线；低于 1 MB/min 视为僵死

### 判断 3：sqlite 是否被 reindex 锁住？（判断主线程是否被殃及）

```bash
# 在活 pod 内 readonly open 试一下
kubectl exec ... --request-timeout=30s -- node -e '
const sqlite = require("node:sqlite");
const db = new sqlite.DatabaseSync("/data/.openclaw/memory/main.sqlite", {readOnly:true});
console.log(db.prepare("SELECT count(*) c FROM chunks").get());
db.close();
'
```

- 立刻返回 → 没事
- **卡 ≥30s 不返回** → reindex worker 持有写锁，**主线程的 memorySearch / 记忆读写也在排队**，每次至少 100-260s（her-68 实测值）。这是 event-loop 被 block 最直接的现场证据。

> 💡 这个探测**本身**就是诊断价值，不需要拿到具体数据。卡死即结论。

### 判断 4：重启会不会重新触发 reindex？（决定能不能用最便宜的"删 pod + 自动重建"）

代码里 `needsFullReindex` 有 **11 个触发因子**（manager-*.js line ~1305）：

```js
needsFullReindex = params?.force
  || !meta                                         // 1. 没 meta（新库）
  || meta.model !== this.provider.model            // 2. embedding model 变
  || meta.provider !== this.provider.id            // 3. provider 变
  || meta.providerKey !== this.providerKey         // 4. baseUrl/apiKey hash 变 ← 大多数人只查这条
  || this.metaSourcesDiffer(meta, configuredSources) // 5. 数据源配置变（baseUrl/apiKey 等内部结构）
  || meta.scopeHash !== configuredScopeHash        // 6. 作用域 hash 变
  || meta.chunkTokens !== this.settings.chunking.tokens   // 7
  || meta.chunkOverlap !== this.settings.chunking.overlap // 8
  || (vectorReady && !meta.vectorDims)             // 9
  || (meta.ftsTokenizer ?? "unicode61") !== this.settings.store.fts.tokenizer; // 10
```

**只查 providerKey 不够**——必须把 11 个字段全 dump 出来跟健康对照组（同上游配置的兄弟实例）逐项比：

```bash
HID=68
POD=$(kubectl get pod -n carher --no-headers | awk '/^carher-'"$HID"'-/{print $1; exit}')
kubectl exec -n carher "$POD" -c carher --request-timeout=20s -- node -e '
const sqlite = require("node:sqlite");
const db = new sqlite.DatabaseSync("/data/.openclaw/memory/main.sqlite", {readOnly:true});
const r = db.prepare("SELECT value FROM meta WHERE key=?").get("memory_index_meta_v1");
if (!r) { console.log("NO_META"); process.exit(0); }
const m = JSON.parse(r.value);
console.log(JSON.stringify({
  provider: m.provider, model: m.model,
  pk: (m.providerKey||"").slice(0,12),
  scopeHash: (m.scopeHash||"").slice(0,12),
  chunkTokens: m.chunkTokens, chunkOverlap: m.chunkOverlap,
  vectorDims: m.vectorDims, ftsTokenizer: m.ftsTokenizer,
  sources: m.sources,
}));'
```

判断结论：

- **每个字段都和健康对照实例一致** → 重启大概率跳过 reindex（her-68 已验证）
- **任一字段不同** → 重启会立刻再开新一轮 reindex
- **所有字段一致但仍在 reindex 循环** → 不是 meta 问题，进 [判断 7](#判断-7上游-embedding-服务健康判断真因是内部还是上游)

> 💡 **本次案例的反例**：her-2/8/74 的 main.sqlite meta 11 字段和 her-68 完全一致（包括 `metaSourcesDiffer` 的 sources 数组也一致），但仍在持续 reindex —— 真因是上游 LiteLLM cooldown，不是 meta 不一致。

### 判断 5：从日志找 event-loop 被 block 的间接证据

不能直接 strace，看高频副作用：

```bash
kubectl logs -n carher "$POD" -c carher --since=10m \
  | grep -iE "bot-registry.*re-registered|ws.*handshake timeout|cooldown|stalled"
```

| 日志关键词 | 含义 | 触发阈值 / 真因侧 |
|---|---|---|
| `[bot-registry] re-registered after key expiry` | Redis bot-registry key TTL=120s 过期 | event loop block ≥120s（症状） |
| `[ws] handshake timeout` | 飞书 WS 心跳错过 | event loop block ≥几十秒（症状） |
| `[memory] embeddings rate limited; retrying in N ms` | 上游 LiteLLM/openrouter 返回 429 | **上游问题**（真因） |
| `[memory] sync failed (...): Error: memory embeddings batch timed out after 120s` | 单批 embedding fetch 超过 120s | **上游慢/cooldown**（真因） |
| `litellm ... cooldown` | LiteLLM router 把某 model 拉黑 | **上游问题** |
| `runSafeReindex` 启动多次而很少看到完成 | 反复触发 | 内部死循环或上游 fail（待 [判断 7](#判断-7上游-embedding-服务健康判断真因是内部还是上游) 区分） |

10 分钟出现 ≥2 次 → 用户已经在持续受影响，应该尽快进入决策树。

> ⚠️ **看到 `embeddings rate limited` / `sync failed ... batch timed out`**：先不要走 inplace/restart，直接进 [判断 7](#判断-7上游-embedding-服务健康判断真因是内部还是上游)。重启 carher pod 改不了 LiteLLM 问题。

### 判断 6：集群级触发因子识别（避免一个一个修反复出 case）

把 deep-scan 出的 CRITICAL 实例的 `TMP_OLDEST_AGE_H` 和 `kubectl get rs -n carher --sort-by=.metadata.creationTimestamp | tail` 的 ReplicaSet AGE 比较：

- **多个实例 TMP_OLDEST_AGE_H 集中在同一个值附近**（误差 <0.5h） → 不是个体问题，是某次集群级动作（rolling upgrade / 改 base-config / 改 image / 升 memory limit）触发了**所有 pod 同时启动 needsFullReindex=true**。
- 一致点 ≈ 最老 ReplicaSet AGE → 那次部署就是触发因子。

**本次案例**：7 个 CRITICAL 中 6 个 `TMP_OLDEST_AGE_H` 在 5.3-5.4h，与所有 RS AGE 5h29m-5h30m 完美对上，确认是「集群批量升 4Gi memory」那次部署同时引爆。

意义：下次做集群级 base-config / memory limit / image rolling 之前，**应优先评估有多少实例处于会触发 reindex 的状态**，否则每次部署都会带出一波 4-5% 失败率。

### 判断 7：上游 embedding 服务健康（判断真因是内部还是上游）

**触发条件**：[判断 4](#判断-4重启会不会重新触发-reindex决定能不能用最便宜的删-pod--自动重建) 显示 meta 11 字段全部一致，但 active tmp UUID 不断翻新（多次失败 abort 重启），或日志里有 `embeddings rate limited` / `sync failed ... batch timed out`。

#### 7.1 看 LiteLLM proxy 当前是否健康

```bash
LITELLM_POD=$(kubectl get pod -n carher --no-headers | awk '/^litellm-proxy.*Running/{print $1; exit}')
kubectl logs -n carher "$LITELLM_POD" --since=5m --tail=500 2>/dev/null \
  | grep -iE "bge-m3|cooldown|429|rate.limit|embedding" | tail -10
```

- 全是 `200 OK` → **上游已健康**（可能 N 小时前曾经抖动）。本轮 reindex 大概率能成功，等自愈最优
- 频繁 `429` / `cooldown` / `> 30s` → **上游在 fail 中**，重启 carher pod 没用，必须先修上游

#### 7.2 区分 carher pod 日志里的"历史 fail"vs"实时 fail"

只看时间戳，不看绝对计数：

```bash
HID=8
POD=$(kubectl get pod -n carher --no-headers | awk '/^carher-'"$HID"'-/{print $1; exit}')
echo "--- last 5 minutes ---"
kubectl logs -n carher "$POD" -c carher --since=5m --tail=2000 2>/dev/null \
  | grep -E "embeddings rate limited|sync failed.*batch timed out" | tail -5
echo "--- last 1 hour ---"
kubectl logs -n carher "$POD" -c carher --since=1h --tail=5000 2>/dev/null \
  | grep -cE "embeddings rate limited|sync failed.*batch timed out"
```

- last 5min 有 → 上游正在 fail，**实时问题**
- last 5min 无、但 1h 有 → 上游已恢复，pod 内只是历史残留，**正在自愈**
- 1h 都无 → 不是 embedding fetch 问题，回到 [判断 4](#判断-4重启会不会重新触发-reindex决定能不能用最便宜的删-pod--自动重建) 重新检查 meta

#### 7.3 active tmp size 增速是判断"上游问题"vs"内部问题"的关键尺子

拍 5 分钟快照（[判断 2](#判断-2reindex-是真在工作还是僵死决定要不要打断)）：

| 增速 | 通常含义 |
|---|---|
| 0 MB/min | 完全僵死，进 Phase D 重启 |
| < 1 MB/min | 上游严重慢 / cooldown，**重启没用，先修上游** |
| 5 MB/min | 上游有压力但能跑（her-68 旧基线） |
| 8+ MB/min | 上游健康，正常速度，等自愈 |

> 💡 **本次案例转折**：第一轮诊断把 her-8/74/2 误判成"内部 reindex 死循环"，差点重启。靠 7.1 + 7.2 看到 LiteLLM 已恢复 200 OK + 5min 内已无新 timeout 才识破——真因是 5.5h 前 LiteLLM cooldown 累积出循环，上游恢复后 pod 内本轮 reindex 已在自愈，**任何重启操作都会丢弃当前 250-618 MB 的进度，反作用**。

### 判断 8：embedding_cache 是否过大（模式 B 入口）

**触发条件**：tmp 文件为 0（不是 reindex 循环）、上游健康（LiteLLM 200 OK）、但用户报"超时 / model idle timeout / 半天不回复 / 回复"模型异常""。

OpenClaw 在客户端 turn timeout（默认 120-130s）后会把 *任何* 卡顿包装成 `surface_error reason=timeout`，user-facing 文案是：

```
The model did not produce a response before the model idle timeout.
Please try again, or increase models.providers.<id>.timeoutSeconds
for slow local or self-hosted providers
```

> ⚠️ **这文案不是 Codex 自己的报错**，是 OpenClaw 借鉴了 Codex 的字段命名做 user-facing 兜底。看到这文案不要去查 Codex 客户端配置，先按本判断走。

#### 8.1 反证：LiteLLM SpendLogs 是否真有这次请求

`carher-30` 案例验证：客户端报 130s timeout 时，LiteLLM SpendLogs 在该时间窗里 **0 条** her-30 记录。

> ⚠️ **重要订正（2026-05-16 晚 carher-30 复现后发现）**：**SpendLogs 0 条 ≠ 请求没发出去**。SpendLogs 在某些 fallback / 异常 / 流式中断路径下不写入（具体路径见 litellm-fork 源码 `_PROXY_track_cost_callback`）。**LiteLLM proxy access log（POST /v1/chat/completions 的 200 OK 行）才是请求是否进 proxy 的 ground truth**——必须用 her pod 的 podIP 在 proxy 容器日志里 grep，不能只看 SpendLogs。
>
> ```bash
> # 必查：用 podIP 在 litellm-proxy access log 里反证
> POD_IP=$(kubectl -n carher get pod "$POD" -o jsonpath='{.status.podIP}')
> kubectl -n carher logs -l app=litellm-proxy --since=2h --tail=-1 \
>   | grep "$POD_IP" | grep -E "POST /(v1/)?chat/completions"
> ```
>
> **三种情况要分开判**：
> - SpendLogs 0 + access log 0 → 请求**真没发出去**，进 8.2 量 DB 体积（模式 B）
> - SpendLogs 0 + access log **有 200 OK 1-5s** → 请求发了 LiteLLM 也正常返回，但 carher 端 stream 没消费完 → **跳模式 C（session trajectory accumulation，见下文）**
> - SpendLogs **有**记录 → 不是模式 B，回 [判断 7] 看上游

```bash
# 进 litellm-db Pod 查（数据库密码在 litellm-proxy 容器 DATABASE_URL env 里）
HID=30
HASH_PREFIX=$(kubectl -n carher exec litellm-db-0 -- bash -c "PGPASSWORD=<pw> psql -U litellm -d litellm -h localhost -tA -c \"
  SELECT LEFT(token,20) FROM \\\"LiteLLM_VerificationToken\\\" WHERE key_alias='carher-$HID';\"" 2>&1)

kubectl -n carher exec litellm-db-0 -- bash -c "PGPASSWORD=<pw> psql -U litellm -d litellm -h localhost -tA -c \"
SELECT to_char(\\\"startTime\\\",'HH24:MI:SS'), model, completion_tokens
  FROM \\\"LiteLLM_SpendLogs\\\"
  WHERE api_key LIKE '${HASH_PREFIX}%'
    AND \\\"startTime\\\" BETWEEN '<timeout-start>' AND '<timeout-end>';\""
```

零行 → 进 8.2 量 DB 体积；有行 → 不是模式 B，回 [判断 7] 看上游。

#### 8.2 量 main.sqlite 体积 + 各表行数

```bash
HID=30
POD=$(kubectl -n carher get pod --no-headers | grep "^carher-${HID}-" | head -1 | awk '{print $1}')

kubectl -n carher exec "$POD" -c carher -- ls -lh /data/.openclaw/memory/main.sqlite
kubectl -n carher exec "$POD" -c carher -- node -e "
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync('/data/.openclaw/memory/main.sqlite', {readOnly: true});
for (const t of ['chunks','chunks_vec','chunks_fts','embedding_cache','files']) {
  try {
    const c = db.prepare('SELECT count(*) AS n FROM \"' + t + '\"').get().n;
    console.log(t + ':', c, 'rows');
  } catch(e) {}
}
console.log('embedding_cache MB:',
  db.prepare(\"SELECT ROUND(sum(length(embedding))/1024.0/1024,1) AS mb FROM embedding_cache\").get().mb);
"
```

| 阈值 | 含义 |
|---|---|
| main.sqlite ≥ 500 MB **且** embedding_cache ≥ 200 MB | **高度可疑**，进 8.3 做冷态 benchmark |
| main.sqlite ≥ 1 GB | **确诊**，跳过 8.3 直接走模式 B 修复 |
| < 500 MB **且** access log 有 200 OK | 跳模式 C（trajectory accumulation） |
| < 500 MB **且** access log 也 0 | 不是模式 B，回 [判断 4] / [判断 7] 重新分诊 |

#### 8.3 实测 cold scan / KNN 耗时（可选，确证用）

```bash
kubectl -n carher exec "$POD" -c carher -- node -e "
const { DatabaseSync } = require('node:sqlite');
const sqliteVec = require('/app/node_modules/sqlite-vec');
const db = new DatabaseSync('/data/.openclaw/memory/main.sqlite', {readOnly: true, allowExtension: true});
db.loadExtension(sqliteVec.getLoadablePath());
const t1 = Date.now();
db.prepare(\"SELECT count(*) FROM embedding_cache WHERE LENGTH(embedding)>0\").get();
console.log('embedding_cache full scan:', Date.now()-t1, 'ms');
const probe = db.prepare('SELECT embedding FROM chunks_vec LIMIT 1').get();
const t2 = Date.now();
db.prepare('SELECT id FROM chunks_vec WHERE embedding MATCH ? AND k=10').all(probe.embedding);
console.log('vec0 KNN k=10 cold:', Date.now()-t2, 'ms');
const t3 = Date.now();
db.prepare('SELECT id FROM chunks_vec WHERE embedding MATCH ? AND k=10').all(probe.embedding);
console.log('vec0 KNN k=10 warm:', Date.now()-t3, 'ms');
"
```

| 指标 | 健康 | 模式 B 命中 |
|---|---|---|
| embedding_cache 全表 LENGTH 扫 | < 2s | 10-40s（`carher-30` 实测 38s） |
| vec0 KNN k=10 冷态 | < 1s | 8-12s |
| vec0 KNN k=10 热态 | < 50ms | < 50ms |

冷热差距 > 100× 是 NFS page cache miss 的强指标 —— sqlite 走 NAS NFS，4KB 随机读全是 RPC round trip。

#### 8.4 物理机制速记（用于解释和教训迁移）

```
NAS NFS（vers=3, rsize=1MB）→ 单页 4KB 读都要 GETATTR + READ round trip
        ↓
main.sqlite 1.1 GB 存在 NAS 上
        ↓
embedding_cache 698 MB / 33787 行（>>chunks 表的 111MB / 5360 行）
        ↓
单次 turn prework: embedding API + vec0 KNN（cold 10s）+ embedding_cache 操作 + chunks JOIN
        ↓
node:sqlite 是同步 API → 整段 prework 期间 event loop 完全 block
        ↓
NFS page cache 4h+ 不活跃后 evict（节点 200 Pod 共享 cache pressure 大）
        ↓
每次 cold path 累计 30-130s → 客户端 turn timeout（默认 120-130s）触发 surface_error
```

关键差异（vs 模式 A reindex 死循环）：

| | 模式 A reindex 循环 | 模式 B cache bloat |
|---|---|---|
| 现象 | OOMKilled 反复 + tmp 堆积 | 偶发"超时不回复"，pod 不一定 restart |
| main.sqlite mtime | 多天前（卡死） | 最近（正常更新） |
| tmp-* 文件 | ≥1 个 | 0 个 |
| 主诉关键词 | 卡死 / OOM / tmp / 重启 | "model idle timeout" / 半天不回 |
| LiteLLM 上是否能看到请求 | 部分能看到 | **0 条**（请求没发出去） |
| 修复方向 | 清孤儿 + 等自愈 / 重启 | **GC embedding_cache + VACUUM** |

> ⚠️ **embedding_cache 是性能缓存，不是记忆本体**。记忆本体在 `chunks` 表（自带 `text` + `embedding` 列）+ `chunks_vec`（vec0 KNN 索引）+ `chunks_fts*`（FTS5 全文索引）—— 这三张表跟 embedding_cache 完全独立。删 cache 唯一影响是：下次相同 hash 的文本要重新调一次 LiteLLM `/embeddings`（bge-m3 ~1-3s + < $0.0001）。**不会丢失任何记忆。**

### 判断 9：模式 C（假说，2026-05-16 引入，待验证）——session trajectory accumulation → stream consumer hang

**触发条件**：
- 文案是"model idle timeout"
- DB 体积 < 500 MB（模式 B 不命中）或 **已经做过 GC + VACUUM 但 timeout 仍复现**
- **LiteLLM proxy access log 看得到 200 OK 1-5s**（即 LiteLLM 正常返回），但 carher 端 stream 消费没完成 / 130s 后 surface_error

**假说**：carher 把 LiteLLM 返回的 SSE chunk 喂进 `processOpenAICompletionsStream` / `sanitizeOpenAISdkSseResponse` / `buildGuardedModelFetch` 这几层包装时，遇到特定 schema 或累积过大的 session trajectory（`agents/main/sessions/<id>.trajectory.jsonl` 5+ MB + 单 turn `context.compiled` 接近 trajectory-event-size-limit=262144 截断阈值）时迭代器卡住不前进，OpenClaw `streamWithIdleTimeout(120s)` 到点报 surface_error。

**carher-30 案例时间线（2026-05-16）**：
- 早上 GC + VACUUM：main.sqlite 1.1 GB → 208 MB（embedding_cache 砍 90%）
- **同日 20:33 同样 timeout 复现** → 仅靠 GC 没修住，cache bloat 不是充分根因
- 该窗口 LiteLLM proxy access log：POST /v1/chat/completions 200 OK，TTFB 3 秒
- 王丽花 07:12 切到 opus 模型，session 0b7c3096 trajectory.jsonl 累积 5.2 MB / 单 turn context.compiled 290 KB

**临时修复（最小侵入）**：archive 老 session 文件 + 删 pod 让它起新 session
```bash
POD=$(kubectl -n carher get pod --no-headers | grep "^carher-${HID}-" | head -1 | awk '{print $1}')
TS=$(date +%Y%m%d-%H%M)
# 找最近修改的 session（要确认是用户实际在用的）
kubectl -n carher exec "$POD" -c carher -- bash -c '
  cd /data/.openclaw/agents/main/sessions
  ls -lt *.jsonl 2>/dev/null | head -5
'
# 把 hot session 改名归档（OpenClaw 起新 session 自然走新 ID）
SID=<above 选中的>
kubectl -n carher exec "$POD" -c carher -- bash -c "
  cd /data/.openclaw/agents/main/sessions
  for f in ${SID}.jsonl ${SID}.trajectory.jsonl ${SID}.trajectory-path.json; do
    [ -f \"\$f\" ] && mv \"\$f\" \"\$f.archive.${TS}\"
  done
  # 同步从 sessions.json 移除 agent:main:main → 该 session 的映射
  node -e \"
    const fs = require('fs');
    const p = 'sessions.json';
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    if (j['agent:main:main'] === '${SID}') { delete j['agent:main:main']; fs.writeFileSync(p, JSON.stringify(j, null, 2)); }
  \"
"
# pod 重启让 in-memory 状态归零
kubectl -n carher delete pod "$POD"
```

**验证状态**：carher-30 fix 已部署，等用户下次自然使用后判断秒回 vs 仍卡。**如果秒回 → 写实模式 C，把"模式 B cache bloat" 的诊断顺序从 8.1 → 8.2 改成"先看 access log 区分 B/C"**。**如果仍卡** → 进一步挖 `processOpenAICompletionsStream` / openai SDK 包装层的具体卡点。

**未确证之前不要把模式 C 当成首选诊断**——这是带证据但未闭环的假说。

## 修复方案决策树（按副作用从小到大）

```
模式 A（有 tmp 文件 / OOM 循环）
                              ┌─ TMP_ACTIVE = 0 ────────────────────────────► A: inplace rm（零下线）
                              │
                              │                              ┌─ 上游健康 + 在前进 ──► B: 等自然完成
                              │                              │
判断 7 上游健康？ ─── 健康 ────┤                              ├─ 上游健康 + main 僵死 ► C: kubectl delete pod
                              │                              │
                              └─ TMP_ACTIVE ≥ 1 ────────────┤                       (但先验证 [判断 4] meta 11 字段一致)
                                                             │
                                                             └─ meta 11 字段不一致 ► D: paused-toggle + 5Gi SOP
                              ┌──────────────────────────────────────────────────►
判断 7 上游健康？ ─── 在 fail ─┤
                              │   ► E: 先修 LiteLLM（不要碰 carher pod）
                              │     • LiteLLM 重启 / 加 router 节流 / 切备用 model
                              │     • 看 litellm-ops skill
                              │
                              └── 实在修不了上游？ ──► F: 把受影响 her 临时切 fts-only
                                                          (config override: memorySearch.disabled=true 或换轻量 embedding)

模式 B（无 tmp / "model idle timeout" / main.sqlite > 500MB）
                              ┌─ 仅 1-2 个实例 ──► G1: 单实例 GC + VACUUM（先做 VACUUM INTO 备份）
判断 8 命中 ──────────────────┤
                              └─ 集群级多实例命中 ► G2: 批量 GC（结合判断 6 找触发因子）
```

| 方案 | 适用 | 副作用 | 脚本 / 命令 |
|---|---|---|---|
| **A 原地清孤儿** | TMP_ACTIVE=0 | **零下线零重启**，仅 PVC 上 `rm` 文件 | `inplace_clean_orphans.sh <HID>` |
| **B 让它跑完** | TMP_ACTIVE≥1 + 上游健康 + size 涨速 ≥5 MB/min | 0 下线但持续 100-260s/周期卡顿 | 不需要脚本，等待即可（一般 30-60min） |
| **C 删 pod 重建** | TMP_ACTIVE≥1 + meta 11 字段全一致 + 上游健康 + main 僵死 | 30-60s 下线一次（飞书 SDK 自动重连，消息有 retry） | `kubectl delete pod -n carher <pod>` |
| **D paused-toggle SOP** | meta 11 字段不一致 OR 历史多次 OOM 想换 5Gi 缓冲 | 30-60s × 2 次下线 | `sop_phase_a/c.sh` |
| **E 修上游** | 上游 LiteLLM 仍在 fail | 不影响 carher pod | `litellm-ops` skill |
| **F 临时关 embedding** | 上游短时间内修不好且业务等不起 | 受影响实例失去向量记忆能力（只剩 fts），用户能感知 | `carher-instance-config-override` skill 加 memorySearch override |
| **G GC embedding_cache + VACUUM** | 判断 8 命中：main.sqlite 异常大、prework cold scan stall | DB 加 sqlite 写锁约 1-3 分钟（VACUUM 期间），无 pod 重启 | 见下文 Phase G |

**核心原则**：

1. **先 [判断 7] 后选方案**——别上来就 inplace rm/重启，先确认是不是上游问题
2. **上游 fail 中绝对不要走 C/D**——重启会丢弃已有进度，下一轮还是会被上游 fail 掉，纯反作用
3. **上游恢复后等自愈**最便宜——本次 her-2/74 在 LiteLLM 恢复 2h 后从 5 MB/min 加速到 8.5 MB/min，自愈 30-60 min
4. **A 永远可以先做**——孤儿 tmp 跟上游无关，清掉只省磁盘不影响其他逻辑

**本次 7 个 CRITICAL 实战分布**：

- her-26/161/68/31（4 个）→ 方案 A，零下线全修复
- her-8/74/2（3 个）→ 先 A 清孤儿（释放 1.93 GB），后续走方案 B 等自愈（**不能走 C/D，否则丢进度**）

## kubectl / k8s 关键事实

- her CRD 名：`her-<uid>`（如 `her-54`），pod 名：`carher-<uid>-<rs>-<pod>`，PVC：`carher-<uid>-data`
- her CRD `spec.paused=true` → operator scale deployment 到 0；`paused=false` 拉回 1
- operator 的 `pod-spec-key` 只包含 image / prefix / secret / deployGroup，**不包含 memory limit**
  → 直接 `kubectl patch deployment` 改 memory **不会被 operator 调和回去**（除非有人改了 image 等触发 ensureDeployment）
- `main.sqlite.tmp-*` 文件分两类：**ACTIVE**（活 pod 进程持有 fd，正在写）和 **ORPHAN**（fd=0 的死文件，上一个 pod 留下的）
- ACTIVE 不能 rm（unlinked + open fd 在 Linux 合法但于事无补，进度仍丢）；ORPHAN 可以**在活 pod 内直接 rm**（见 Phase 0 / `inplace_clean_orphans.sh`）——这是 her-68 案例验证过的零下线路径
- 历史做法（一次性 busybox pod 挂 PVC 删）只在 paused-toggle SOP 已经把 pod 停了的情况下用——pod 完全退出意味着所有 tmp 自然变 ORPHAN，所以 cleaner pod 不需要区分。优先级：**Phase 0 inplace > cleaner pod**（前者无下线）

如果 `kubectl get nodes` 报 connection refused，先建立 SSH 隧道（参考 check-instance-status skill）。

## Workflow

```
[ ] Step 1: 全集群扫描（生成 TSV + Markdown 报告）
[ ] Step 2: 7 个判断逐个过
        ├─ 1 fd 区分 ORPHAN/ACTIVE
        ├─ 2 size+mtime 漂移测进度
        ├─ 3 sqlite probe 卡死=锁现场
        ├─ 4 meta 11 字段（不只 providerKey）
        ├─ 5 carher pod 日志关键词
        ├─ 6 TMP_OLDEST_AGE_H ≈ RS AGE 找集群级触发因子
        └─ 7 上游 LiteLLM 健康（区分内部 vs 上游真因）★ 新增，避免误诊
[ ] Step 3: 按决策树选方案 A/B/C/D/E/F
        ├─ A inplace_clean_orphans.sh（零下线，先做）
        ├─ B 等上游恢复后自愈（5-8 MB/min 增速健康）
        ├─ C kubectl delete pod（meta 一致 + 上游健康 + 僵死时）
        ├─ D paused-toggle + 5Gi/3Gi 完整 SOP（meta 不一致）
        ├─ E 修上游 LiteLLM（litellm-ops skill）
        └─ F 临时切 fts-only（carher-instance-config-override skill）
[ ] Step 4: 复扫确认 + 报告
```

## Step 1: 全集群扫描（推荐：两阶段 deep scan）

**推荐入口**：`run_full_deep_scan.sh` —— 两阶段并发，自动出 Markdown 报告。

```bash
# 默认两阶段（~9 min）：先 scan_one 全集群拿基础信息，对嫌疑实例再做 deep_scan
bash .cursor/skills/her-memory-reindex-rescue/scripts/run_full_deep_scan.sh

# --full 模式（~25-30 min）：所有实例都做 deep_scan，数据最完整
bash .cursor/skills/her-memory-reindex-rescue/scripts/run_full_deep_scan.sh --full

# --quick 模式（~6 min）：只跑 scan_one，不做 sqlite 内部探查
bash .cursor/skills/her-memory-reindex-rescue/scripts/run_full_deep_scan.sh --quick
```

输出：

- `/tmp/her-rescue/scan.tsv` —— Phase 1 基础 TSV（11 列）
- `/tmp/her-rescue/deep-scan.tsv` —— Phase 2 深扫 TSV（22 列，含 sqlite 内部）
- `/tmp/her-rescue/deep-scan-report.md` —— **Markdown 报告（按风险分级）**

### deep_scan_one.sh 字段

```
POD HID RESTARTS LAST_OOM POD_AGE_M WS_READY \
MAIN_MB MAIN_AGE_H \
TMP_COUNT TMP_ACTIVE TMP_MB TMP_OLDEST_AGE_H \
CHUNKS EMB_MB EC_ROWS EC_MB TMP_CHUNKS TMP_HAS_META \
PROVIDER_MODEL PROVIDER_KEY \
MEM_MB STATUS
```

新增字段（相对 `scan_one.sh`）说明：

- `POD_AGE_M` —— pod 启动至今分钟（< 10 表示刚 churn 完）
- `WS_READY` —— `carher.io/feishu-ws-ready` ReadinessGate（YES/NO/UNKNOWN）
- `TMP_OLDEST_AGE_H` —— 最老 tmp 文件年龄（her-68 案例里这个值 ≈ 4.6h，配合 main 78h 锁定问题）
- `CHUNKS / EMB_MB / EC_ROWS / EC_MB` —— 主库 sqlite 内部数据规模，**判断 reindex 工作集大小**（≥150 MB 强烈警告，≥240 MB 是 her-68 危险线）
- `TMP_CHUNKS / TMP_HAS_META` —— 活 tmp 内的 reindex 进度，`TMP_HAS_META=0` 表示**还没写到 swap 步骤**
- `PROVIDER_MODEL / PROVIDER_KEY` —— 用于"快速比对兄弟实例 hash 是否一致"

### 风险分级（classify_risk.py 的内部规则）

参见 `scripts/classify_risk.py` 文件头部 docstring。简化版：

- **CRITICAL**: TMP_COUNT≥1 AND MAIN_AGE_H≥24（her-68 模式：tmp 在 + 主库多天没更新）
- **CRITICAL**: TMP_COUNT≥2（多次失败堆积）
- **CRITICAL**: RESTARTS≥5 AND TMP_COUNT≥1
- **HIGH**: TMP_COUNT≥1 AND MAIN_AGE_H≥12 / TMP_OLDEST_AGE_H≥6 / 近期反复重启
- **MED**: 正在 reindex（TMP_ACTIVE≥1 但 main 还新）/ 近期 OOM
- **OK**: 其他

报告底部会自动生成"一键命令"段，可以直接复制 `run_full_rescue.sh <ids...>` 救援。

### 兼容旧脚本

`run_scan.sh` + `scan_one.sh` 仍然保留，输出基础 11 列 TSV：

```
POD  UID  RESTARTS  LAST_OOM  MAIN_MB  MAIN_AGE_H  TMP_COUNT  TMP_ACTIVE  TMP_MB  MEM_MB  STATUS
```

注意 `UID` 这列在 macOS zsh 里会被覆盖成本机用户 uid（502）；脚本里实际用的是 `HID`，UID 列是 placeholder——分析时**忽略 UID 列**，从 pod 名 `carher-<id>-` 自取真实 id。

## Step 2: 分类

```bash
# 当前还有 tmp 文件的 pod
awk -F'\t' '$11=="OK" && $7+0>0' /tmp/her-rescue/scan.tsv

# 高内存 (>2500 MB，距 3Gi 警戒)
awk -F'\t' '$11=="OK" && $10+0>2500' /tmp/her-rescue/scan.tsv

# 重启过的（含历史 OOM）
awk -F'\t' '$11=="OK" && $3+0>0' /tmp/her-rescue/scan.tsv | sort -t$'\t' -k3 -nr
```

按以下规则分类：

| 类别 | 判定 | 处置 |
|------|------|------|
| **B 类卡死** | TMP_ACTIVE>0 **且** MAIN_AGE_H>24（main.sqlite 多天不更新） | **必做 SOP** |
| **A 类健康周期** | TMP_ACTIVE>0 **且** MAIN_AGE_H<6（mtime 最近还在推进） | 不动（reindex 周期内能成功 swap） |
| **OOM 边缘** | RESTARTS>=5 或 MEM_MB>2700（接近 3Gi） + 无 tmp | 视情况 SOP（重启王主动 5Gi 起一次更安全） |
| **NO_DIR** | 17 个左右，老的 docker 时代用户没 PVC 数据 | 跳过，正常 |

## Step 3: 单实例 SOP

> 顺序阅读：先看 **Phase 0（inplace 快速通道）**，能用就别走完整 SOP。完整 SOP 适合 active reindex + meta 不一致 / 多次 OOM 的情况。

### Phase 0: inplace 清孤儿（零下线，优先尝试）

适用前提：[判断 1](#判断-1tmp-是孤儿还是-active决定能不能在活-pod-里直接-rm) 显示有 ORPHAN（fd=0）的 tmp。

```bash
HID=68
bash .cursor/skills/her-memory-reindex-rescue/scripts/inplace_clean_orphans.sh $HID --dry-run  # 先看
bash .cursor/skills/her-memory-reindex-rescue/scripts/inplace_clean_orphans.sh $HID            # 真删
```

脚本保证：
- 只删 fd=0 的孤儿（`rm` 前再 check 一次 fd，双重保险）
- ACTIVE tmp 自动 SKIP（不打断进行中的 reindex）
- pod 不重启、ws 不断、main.sqlite 不动
- 单实例 ~16s，可并行批量（多 pod 同时跑安全）

退出码：0=成功（含部分清理）、1=前置失败（pod 不 ready）、2=没孤儿可清。

如果 Phase 0 跑完 TMP_COUNT 归零 → **直接结束**，不需要后续 Phase。

如果还有 ACTIVE tmp 留下 → 进入 [判断 2~5](#诊断方法论6-个关键判断先做这些再动手) 决定是 B/C/D。

### Phase C0: kubectl delete pod（meta 一致时的中档方案）

如 [判断 4](#判断-4重启会不会重新触发-reindex决定能不能用最便宜的删-pod--自动重建) 显示当前 pod 的 `main.sqlite.meta.providerKey` 已经是当前 runtime 算出的值，可以直接：

```bash
HID=2
POD=$(kubectl get pod -n carher --no-headers | awk '/^carher-'"$HID"'-/{print $1; exit}')
kubectl delete pod -n carher "$POD"
# 等新 pod ready
kubectl wait --for=condition=ready pod -l carher.io/her-id=$HID -n carher --timeout=120s 2>/dev/null \
  || kubectl get pod -n carher --no-headers | grep "^carher-${HID}-"
```

副作用：30-60s 单次下线，飞书 SDK 自动重连，消息走飞书 retry 不丢。比 Phase A/C 完整 SOP 少一次重启。

如果新 pod 起来后 needsFullReindex 依然 true（kubectl exec 看到 `main.sqlite.tmp-*` 又出现）→ 退回完整 SOP。

### Phase A/B/C: 完整 SOP（适用 meta 不一致 / 多次 OOM 想换 5Gi 缓冲）

以下三个 phase 串行幂等：

```bash
HID=54  # 或 66, 67, 170, 188, 40, 73, 8 等

# Phase A: paused → 等 pod 退出 → 一次性 pod 删孤儿 → patch deploy 5Gi → paused=false → 等 ready
$SKILL_DIR/scripts/sop_phase_a.sh $HID

# Phase B: 观察 5 分钟（看 reindex 是否触发；通常不触发）
$SKILL_DIR/scripts/sop_observe.sh $HID

# Phase C: patch deploy 3Gi → paused=true→false 让新 limit 生效 → verify
$SKILL_DIR/scripts/sop_phase_c.sh $HID
```

每个 phase 60-90 秒。Phase A + B + C 串行约 8 分钟/实例。多个实例可以串行 `for HID in 40 73 8 67 170 188; do ...; done`，6 个约 14 分钟。

**用户感知**：Phase A 和 Phase C 各引发 1 次 pod 重启（共 2 次），每次约 30-60 秒下线。期间消息不丢，飞书自动 retry 回放。

每个 phase 的输出写到 `/tmp/her-rescue/her-$HID.log`，最终 pod 名记录在 `/tmp/her-rescue/her-$HID-pod-final.txt`。

### Phase A 详细步骤（脚本里固化）

1. 记录 BEFORE：当前 pod 的 main.sqlite + tmp + fd
2. `kubectl patch herinstance her-$HID -p '{"spec":{"paused":true}}'`
3. 等 pod 完全消失（最多 90s，preStop 15s + grace 30s）
4. `kubectl apply` 一次性 cleaner pod（busybox + 同 PVC，`rm -fv main.sqlite.tmp-*`）
5. 等 cleaner Succeeded → `kubectl logs` 拿删除清单 → 强制 delete cleaner pod
6. `kubectl patch deployment carher-$HID` 改 memory limit 5Gi（**`/spec/template/spec/containers/0/resources/limits/memory`**——索引 0 是 carher 容器，1 是 config-reloader）
7. `kubectl patch herinstance her-$HID -p '{"spec":{"paused":false}}'`
8. 等新 pod ready（通常 4-8 秒）

### Phase C 步骤

1. patch deployment memory 3Gi
2. paused=true → 等退出 → paused=false → 等 ready
3. 验证 final pod 的 limit + 文件状态

`paused` 触发的重启，operator 不会重新生成 deployment（仅 scale 0/1），所以 step 1 改的 3Gi 会保留。

### Phase G: embedding_cache GC + VACUUM（模式 B 修复，零下线）

适用前提：[判断 8] 命中（main.sqlite > 500MB、tmp=0、上游健康、用户报"超时/不回复"）。

**核心思想**：embedding_cache 是性能缓存，删了不丢记忆。记忆本体在 `chunks` / `chunks_vec` / `chunks_fts*`。

#### G1: 单实例（适合首次验证）

整个流程在活 pod 内执行，**不重启 pod，不动 CRD**。期间 sqlite 短暂写锁（VACUUM 时主程序的 memory.sync 写入会排队 1-3 分钟，event loop 不至于 block 太久因为是后台 thread）。

##### Step G1.1: 备份（强制，否则不要进下一步）

```bash
HID=30
POD=$(kubectl -n carher get pod --no-headers | grep "^carher-${HID}-" | head -1 | awk '{print $1}')
DATE=$(date +%Y%m%d)
kubectl -n carher exec "$POD" -c carher -- node -e "
const { DatabaseSync } = require('node:sqlite');
const fs = require('fs');
const BAK = '/data/.openclaw/memory/main.sqlite.bak.$DATE';
if (fs.existsSync(BAK)) { console.log('ABORT: backup exists:', BAK); process.exit(2); }
const db = new DatabaseSync('/data/.openclaw/memory/main.sqlite');
const t = Date.now();
db.exec(\`VACUUM INTO '\${BAK}'\`);
console.log('VACUUM INTO done', Date.now()-t, 'ms');
"
# carher-30 实测：1.1GB → 903MB backup，~83 秒
```

`VACUUM INTO` 是 sqlite 原生命令，相当于做一个 defragmented 一致快照（即使主程序在并发写也安全 —— 用 backup API）。

##### Step G1.2: 备份完整性校验

```bash
kubectl -n carher exec "$POD" -c carher -- node -e "
const { DatabaseSync } = require('node:sqlite');
const sqliteVec = require('/app/node_modules/sqlite-vec');
const db = new DatabaseSync('/data/.openclaw/memory/main.sqlite.bak.$DATE', {readOnly: true, allowExtension: true});
db.loadExtension(sqliteVec.getLoadablePath());
console.log('integrity_check:', db.prepare('PRAGMA integrity_check').get());
for (const t of ['chunks','chunks_vec','embedding_cache']) {
  console.log(t + ':', db.prepare('SELECT count(*) AS n FROM \"' + t + '\"').get().n);
}
"
# 期望：integrity_check=ok，chunks/chunks_vec/embedding_cache 行数与原库一致
```

如果 `integrity_check` 不是 `ok` 或行数对不上：删 backup 重做，**不要进 Step G1.3**。

##### Step G1.3: GC

激进版（保留 7 天）—— 推荐用于首次验证拿最大信号：

```bash
kubectl -n carher exec "$POD" -c carher -- node -e "
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync('/data/.openclaw/memory/main.sqlite');
const cutoff = Date.now() - 7*86400*1000;
const before = db.prepare('SELECT count(*) AS n, ROUND(sum(length(embedding))/1024.0/1024,1) AS mb FROM embedding_cache').get();
const t = Date.now();
const r = db.prepare('DELETE FROM embedding_cache WHERE updated_at < ?').run(cutoff);
console.log('DELETE done', Date.now()-t, 'ms; rows deleted:', r.changes);
const after = db.prepare('SELECT count(*) AS n, ROUND(sum(length(embedding))/1024.0/1024,1) AS mb FROM embedding_cache').get();
console.log('before:', before, '→ after:', after);
"
```

保守版（保留 30 天）：把 `7*86400*1000` 改成 `30*86400*1000`。

##### Step G1.4: VACUUM 主库（回收磁盘空间 + 重建 b-tree）

```bash
kubectl -n carher exec "$POD" -c carher -- node -e "
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync('/data/.openclaw/memory/main.sqlite');
const t = Date.now();
db.exec('VACUUM');
console.log('VACUUM done', Date.now()-t, 'ms');
"
# 1.1GB 库在 NAS 上 VACUUM 约 60-180 秒
```

> ⚠️ `VACUUM` 期间持 EXCLUSIVE 锁。其他 sqlite 写连接会排队等待（carher 主程序的 memory.sync 写入会被推迟 1-3 分钟）。读连接通常不受影响（WAL 模式下）。**carher 主程序在此期间能继续收消息**（feishu WS 不断），但 memory pipeline 会延迟，期间用户消息回复体感更慢——结束后立刻恢复。

##### Step G1.5: 验证效果

```bash
kubectl -n carher exec "$POD" -c carher -- sh -c 'ls -lh /data/.openclaw/memory/main.sqlite*'
# 再跑一遍 [判断 8.3] 的 benchmark 对比 cold scan 时间
```

期望：

| 指标 | GC 前 | GC 后（7 天保留） |
|---|---|---|
| main.sqlite | 1.1 GB | ~50-100 MB |
| embedding_cache rows | 33787 | 1500-2000 |
| embedding_cache 全表扫 cold | 38s | < 2s |
| vec0 KNN cold | 10s | 5-8s（chunks_vec 不变，但 page cache 友好度更高） |

##### Step G1.6: 回滚预案（仅在 G1.4 后异常时用）

```bash
# 如果 VACUUM 异常 / 用户反映记忆丢失 / 行为退化，立刻回滚：
kubectl -n carher exec "$POD" -c carher -- sh -c '
mv /data/.openclaw/memory/main.sqlite /data/.openclaw/memory/main.sqlite.broken
cp /data/.openclaw/memory/main.sqlite.bak.'$DATE' /data/.openclaw/memory/main.sqlite
'
kubectl -n carher delete pod "$POD"   # 让 carher 重新连库
```

##### Step G1.7: 清理备份（确认稳定 7 天后）

```bash
kubectl -n carher exec "$POD" -c carher -- rm /data/.openclaw/memory/main.sqlite.bak.$DATE
```

#### G2: 集群级巡检 + 批量 GC

适用于 [判断 6] 集群级触发因子识别后发现多实例都是大 main.sqlite。建议先 G1 单实例验证完整收益 → 再批量。

```bash
# 巡检：找所有 main.sqlite > 500MB 的实例
for POD in $(kubectl -n carher get pods --no-headers | awk '/^carher-[0-9]+-/{print $1}'); do
  sz=$(kubectl -n carher exec "$POD" -c carher --request-timeout=10s -- \
    stat -c '%s' /data/.openclaw/memory/main.sqlite 2>/dev/null)
  [ -n "$sz" ] && [ "$sz" -gt 524288000 ] && \
    printf "%s\t%s\n" "$POD" $((sz/1024/1024))MB
done | sort -k2 -hr
```

批量 GC 时**严格串行**（不并发 VACUUM，避免 NAS 同时被多个 pod 大流量读写）。

## Step 4: 复扫验证

重新跑 Step 1 的扫描，确认目标实例 TMP_COUNT=0、main.sqlite mtime 没倒退、MEM_MB 正常。

## 决策原则（重要）

1. **A 类 healthy 不要碰**——周期内能 swap 成功，主动重启反而打断 reindex
2. **B 类必救**——卡死状态下 reindex 永远不会自然完成
3. **高内存 + 无 tmp 不必走 SOP**——很可能不是 reindex 问题，重启反而可能引发触发
4. **重启王（restartCount > 5）即便当前无 tmp 也建议救一次**——给她一次干净的 5Gi 启动机会
5. **NO_DIR pod 跳过**——老 docker 用户，没 PVC

## 已知坑

- `$UID` 是 zsh/bash 内置只读变量，脚本变量必须用 `HID`（her id）
- `grep -c` 没匹配时会非零退出，用 `|| true` 兜底，再 `${var:-0}` 强制单数字
- 删 tmp 时 pod 必须**完全消失**（`kubectl get pod` 返回空，不是 Pending/Failed），否则 fd 还在
- cleaner pod 用过即删（`--grace-period=0 --force`），名字带 `-her-$HID` 防冲突
- 不能并行对同一 pod 操作（kubectl patch 之间没事，但 cleaner pod 的 PVC ReadWriteOnce 时会冲突；现集群是 RWX NAS 所以并行 6 个 phase A 在物理上 OK，但日志混乱不建议）
- **判断 providerKey 是否一致不要硬编码**：集群可能同时存在 OpenRouter 直连组和 LiteLLM proxy 组（每组 hash 不同），需要拉同上游兄弟实例对照。`cgroup memory.current` 也包含 page cache，看真实工作集要用 `kubectl top`，看上限风险才看 `memory.current`/`memory.peak`。
- **OOM 不一定等于 reindex 死循环**：incremental reindex 完成 → 紧接大上下文用户消息（含历史群消息注入 + 长流式输出）也可能把 working set 顶到 3Gi。这种情况扫描时 TMP_COUNT=0、main.sqlite mtime 是最近几分钟内，restartCount 几小时增 1，不需要走 SOP，可考虑给该实例单独 `kubectl patch deployment` 提到 4Gi。
- bash inline `awk '...{print; exit}'` 可能让上游 `kubectl` 收到 SIGPIPE 退出 141，cursor shell wrapper 会因此中断；改 `grep '^carher-...-' | head -n1 | awk '{print $1}'` 更稳。
- **`kubectl patch deployment` 改 memory limit 会立即触发 rolling update**（不仅 operator pod-spec-key 不监管 → 不被回滚，K8s 自身的 deployment controller 已经识别 Pod template hash 变化）。所以本 skill 里 Phase A 的 `patch + paused-toggle` 顺序中，patch 那步本身就会创建新 RS，paused-toggle 实际上是为了"立即"触发 + 拿到稳定 pod，而不是 patch 不生效的兜底。如果不在意 5-10s 的额外重启窗口，可以省掉 paused-toggle 直接等 rolling 完成。
- **`kubectl exec --request-timeout` 在并行场景容易 timeout**：`xargs -P 20` 经过 kubectl exec 会被 API server 节流；建议 `-P 5`，单 exec timeout ≥ 60s。批量扫描后必跑一次 retry 兜底。
- **kubectl heredoc 与 stdin 抢占**：脚本中 `kubectl exec ... -- sh -c '...' < file` 配合外层 `python3 - <<'PY'` 时，python 的 heredoc 会偷掉 stdin。改成先 `kubectl get ... -o json > /tmp/x.json` 再让 python `open()` 读文件最稳。
- **K8s events 默认 1h TTL**：超过 1h 的 OOM 在 `kubectl get events` 里查不到，必须靠 pod lastState + restartCount 累加 + 最近创建 pod 等多信号才能补全。`her-oom-alert-triage/scripts/scan_oom_signals.sh` 用的就是这种 5 信号叠加方法。

### 快速比对：拉一组实例的 providerKey

```bash
for HID in 2 50 74 100 150 188; do
  POD=$(kubectl get pod -n carher 2>/dev/null | grep "^carher-${HID}-" | head -n1 | awk '{print $1}')
  [ -z "$POD" ] && continue
  printf "carher-%-3s  " "$HID"
  kubectl exec -n carher "$POD" -c carher --request-timeout=10s -- node --experimental-sqlite -e '
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/data/.openclaw/memory/main.sqlite", {readonly: true});
const r = db.prepare("SELECT value FROM meta WHERE key=?").get("memory_index_meta_v1");
console.log("pk=" + (r ? JSON.parse(r.value).providerKey.slice(0,16) : "NO_META"));
' 2>&1 | grep '^pk=' | head -1
done
```

输出会自然分群——每组里都是同上游配置的实例，组间 pk 不同属于正常配置差异。

## 限制（这只是治标）

清孤儿 + 重启**不修根因**。代码层根本修复需要在 **carher 主程序仓库**（不是这个 carher-admin 仓库；image 形如 `fix-compact-eb348941` 来自 openclaw / carher 主项目）做：

1. **finally 清理**：`runSafeReindex` 用 `try/finally` 保证 OOM/异常时也能 `unlink` tmp 文件
2. **reindex 节流**：N 分钟内重复触发只跑一次，避免 OOM 立刻又触发新一轮
3. **流式/分批 reindex**：1 GB+ main.sqlite 一次性加载内存的设计本身需要重构

代码修好之前，每隔几小时就会有新实例陷入循环——本 skill 是周期性运维工具，不是一劳永逸方案。可以考虑加一个 cronjob 周期性跑 Step 1+3。

## 历史结果参考

### 模式 A（reindex 死循环）

- 一次完整跑通本 skill 的会话：清理 18 个 pod 的孤儿 tmp（10.82 GB）+ 救 9 个 B 类卡死实例（74/54/66/40/73/8/67/170/188）+ 总释放 ~13.5 GB + 操作总用时约 1 小时
- 经验：**B 类清完后通常不再触发 reindex**，5Gi 是给意外 reindex 留的缓冲，绝大多数情况都用不上
- **inplace 方案首跑**（继上次 4Gi 集群升级 5.5h 后引爆 7 个 CRITICAL）：
  - 4 个 ORPHAN-only (her-26/161/68/31) → Phase 0 inplace rm，**全程零下线零重启**，释放 1.34 GB，单实例 16s
  - 3 个含 ACTIVE (her-8/74/2) → Phase 0 先清孤儿（释放 1.93 GB），ACTIVE 部分**等自愈**（不重启）
  - 关键诊断证据：sqlite readonly probe 在 her-8 卡 ≥2.5min（[判断 3](#判断-3sqlite-是否被-reindex-锁住判断主线程是否被殃及)）+ her-74 日志 10min 内 3 次 `bot-registry re-registered`（[判断 5](#判断-5从日志找-event-loop-被-block-的间接证据)）
  - 集群级触发因子识别（[判断 6](#判断-6集群级触发因子识别避免一个一个修反复出-case)）：6/7 实例 `TMP_OLDEST_AGE_H` 集中在 5.3-5.4h，对齐 RS AGE 5h29m → 锁定上次集群升 4Gi 部署同时引爆，4% 失败率

### 模式 B（cache bloat / "model idle timeout"）

- **carher-30 首例（2026-05-16）** 王丽花实例反复出现"model idle timeout"，3 次/2h（07:15 / 07:46 / 08:27 都是 130s timeout）。

> ⚠️ **FOLLOWUP 2026-05-16 20:33**：上午做完 GC + VACUUM（1.1 GB → 208 MB）后同日 20:33 同样的 130s timeout 复现 → **cache bloat 不是 carher-30 的充分根因**。复盘发现该窗口 LiteLLM proxy access log 有 POST 200 OK / TTFB 3s 的记录（**SpendLogs 0 条但 access log 非 0**），证明请求发出去且 LiteLLM 正常返回，是 carher 端的 stream consumer 卡住。当晚补做 fix：archive 5.2 MB trajectory + 删 pod 重启（**模式 C 假说**，见判断 9）。fix 待王丽花下次使用验证。**别照搬"GC + VACUUM 就够"——必须先用 podIP 在 access log 反证**。

- 关键诊断证据链：
  1. carher logs：`embedded run failover decision: ... reason=timeout from=litellm/claude-sonnet-4-6` `next=none`
  2. LiteLLM SpendLogs：08:00-09:00 期间 her-30 key（`carher-30`）**只有 1 条记录**且是手动 curl 测试时记的，timeout 窗口内 0 条 → 请求**根本没发出去**
  3. LiteLLM proxy logs：同窗口对其他 key 都是 sonnet-4-6 / opus-4-7 200 OK 1-3s 正常 → 不是 LiteLLM 慢
  4. main.sqlite **1.1 GB**，embedding_cache 33787 行 / **698 MB**（远大于 chunks 表 5360 行 / 111 MB）
  5. 冷态实测：`SELECT count(*) FROM embedding_cache WHERE LENGTH(embedding)>0` 跑了 **38 秒**；vec0 KNN cold 9.6s，热 15ms
  6. 物理层：PVC = NFS NAS（`alibabacloud-cnfs-nas`，vers=3）→ sqlite 每页 4KB 都是 NFS RPC round trip + node:sqlite 同步调用 block event loop
- embedding_cache 时间分布：5-08 一天突增 9906 行 / 205 MB（推测当天有过 reindex），其余日子 100-500 行/天
- GC 收益预估（按 updated_at）：
  - 保留 7 天：删 32178 行 / 664 MB（最激进）
  - 保留 30 天：删 15355 行 / 316 MB（保守）
- VACUUM INTO 备份：1.1 GB → 903 MB（17% 自动压缩），耗时 83 秒
- **教训**：用户截图里的 `models.providers.<id>.timeoutSeconds` 文案让人第一直觉去查 Codex / 客户端配置 —— 实际是 OpenClaw 的 surface_error 兜底模板，与 Codex 无关
- **教训**：5-15 该 key 曾触发 budget exceeded（cost=$104 / max=$100，opus-4-6），日志里至今刷屏 `Key is blocked. Update via /key/unblock` —— 但那是**其他 key**的（用 `carher-30` key 的 hash prefix 比对 LiteLLM SpendLogs 反证 her-30 key 现在 `blocked=空 spend=$51.24/$100`，是另一些用户的 key 在反复 retry）。**看到 `Key is blocked` 不能直接归因到当前调查的实例**，必须用 podIP + LITELLM_API_KEY hash 对齐

### ⚠️ 同次会话的诊断转折（**重要教训**）

her-8/74/2 的 ACTIVE reindex **看起来像内部死循环**（meta.providerKey 全部一致、active tmp UUID 不断翻新、main.sqlite 9-11h 不更新），差点用方案 C 重启。后来通过 [判断 7](#判断-7上游-embedding-服务健康判断真因是内部还是上游) 翻盘：

1. dump 完整 meta 11 字段：her-8/74/2 跟健康 her-68 **逐字一致** → 不是 meta 不匹配
2. carher pod 日志一直翻 `[memory] embeddings rate limited` + `sync failed: batch timed out after 120s`，时间戳是 5.5h 内的 → 命中"上游 fail"关键词
3. LiteLLM proxy 当前日志全 200 OK + 5min 内无新 timeout → 上游已恢复
4. 27min 内 size 增速从 5 MB/min 加速到 8.5 MB/min → 印证上游恢复，本轮 reindex 正在自愈

**结论**：真因是 5.5h 前 LiteLLM 短期 cooldown 让每轮 reindex 跑到一半 fetch 超时 → 抛弃 tmp 重开新一轮 → 累积出"看起来像内部死循环"的现象。LiteLLM 恢复后本轮 reindex 健康前进。**任何重启都会丢弃 250-618 MB 已写进度，反作用**。

教训：

- **`needsFullReindex` 11 个触发因子，只查 providerKey 会漏诊**——后来加上 metaSourcesDiffer / scopeHash / chunkTokens / chunkOverlap / vectorDims / ftsTokenizer 全比，才能下"meta 一致"的结论
- **`active tmp UUID 翻新` ≠ 内部死循环**——可能是上游 fail 反复 abort 导致的现象，必须叠加 [判断 7] 的上游健康检查才能定性
- **看到 `embeddings rate limited` / `batch timed out` 关键词 → 必须先看 LiteLLM 当前状态**，不是先动 carher pod
- **历史日志 vs 实时日志**：`--since=5m` 看实时，`--since=1h` 看历史，二者结合判断"上游正在 fail"vs"已恢复仍在自愈"

## 相关 skill

- **`s3-hermestest-memory-rescue`**：S3 (JSZX-AI-03) docker 容器版的 sibling。本 skill 是 K8s 版（NFS NAS + node:sqlite），那个是 S3 版（本地 ext4 + python3 sqlite3）。**S3 集群报"慢 / @-不回 / model idle timeout"必须用那个 skill**，本 skill 的 kubectl 命令完全不适用；同时它强调本 skill 未覆盖的**模式 D：session jsonl bloat → SessionWriteLockTimeoutError → event loop block**
- **`her-oom-alert-triage`**：OOM / 内存告警的入口分诊。**先用它确认是不是 reindex 死循环**，再决定是否走本 skill。压制 compaction archive / 集群批量升 limit / 阿里云 ACK 阈值告警 都属于那个 skill 的范围
- **`litellm-ops`**：LiteLLM proxy 状态、cooldown、429 排查。**[判断 7] 命中"上游 fail"时直接跳到这个 skill**——上游 fail 期间任何对 carher pod 的操作都是反作用
- `check-instance-status`：单实例日志/状态/重启历史
- `carher-memorysearch-config`：embedding 配置链路（providerKey / scopeHash / sources 变化的源头）
- `carher-instance-config-override`：临时给单实例打 override（方案 F 切 fts-only 时用）
- `hot-grayscale`：零下线重启（本 skill 用的是 paused 方式重启；`her-oom-alert-triage` 用的是 patch deployment 自动 rolling）
