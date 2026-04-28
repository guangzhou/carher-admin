---
name: her-memory-reindex-rescue
description: >-
  排查并救援 CarHer her 实例因 memory reindex 死循环 / 上游 embedding fail 引发的
  OOM / 孤儿 tmp 文件 / 卡死实例 / event-loop 卡顿连锁反应。Use when the user mentions
  "main.sqlite.tmp-*", "孤儿 tmp", "reindex 循环", "needsFullReindex",
  "卡死的 her" + "main.sqlite 多天不更新"，或描述了
  "embedding 卡 → ws 断 → 错过消息 → 重连 → 又卡" / "[memory] embeddings rate limited" /
  "[memory] sync failed: memory embeddings batch timed out after 120s" /
  "bot-registry re-registered after key expiry" / "[ws] handshake timeout" /
  "litellm cooldown" 等 event-loop 被 reindex 同步循环 block 100-260s 或上游
  embedding 持续 429/超时 引发的连锁症状，or wants to scan/clean orphan tmp files
  cluster-wide. **本 skill 仅处理 reindex 死循环这一种 OOM/卡顿场景**——
  泛 OOM / 阿里云 ACK 阈值告警 / compaction archive 累积 / active session
  巨大 等场景请先用 `her-oom-alert-triage` 做分诊；命中上游 LiteLLM fail
  时跳到 `litellm-ops`。本 skill 提供从「7 个判断 → 全集群扫描 → 6 档修复方案
  (A 原地清孤儿 / B 等自愈 / C 删 pod / D paused-toggle / E 修上游 / F 切 fts-only)」
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

## 修复方案决策树（按副作用从小到大）

```
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
```

| 方案 | 适用 | 副作用 | 脚本 / 命令 |
|---|---|---|---|
| **A 原地清孤儿** | TMP_ACTIVE=0 | **零下线零重启**，仅 PVC 上 `rm` 文件 | `inplace_clean_orphans.sh <HID>` |
| **B 让它跑完** | TMP_ACTIVE≥1 + 上游健康 + size 涨速 ≥5 MB/min | 0 下线但持续 100-260s/周期卡顿 | 不需要脚本，等待即可（一般 30-60min） |
| **C 删 pod 重建** | TMP_ACTIVE≥1 + meta 11 字段全一致 + 上游健康 + main 僵死 | 30-60s 下线一次（飞书 SDK 自动重连，消息有 retry） | `kubectl delete pod -n carher <pod>` |
| **D paused-toggle SOP** | meta 11 字段不一致 OR 历史多次 OOM 想换 5Gi 缓冲 | 30-60s × 2 次下线 | `sop_phase_a/c.sh` |
| **E 修上游** | 上游 LiteLLM 仍在 fail | 不影响 carher pod | `litellm-ops` skill |
| **F 临时关 embedding** | 上游短时间内修不好且业务等不起 | 受影响实例失去向量记忆能力（只剩 fts），用户能感知 | `carher-instance-config-override` skill 加 memorySearch override |

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

- 一次完整跑通本 skill 的会话：清理 18 个 pod 的孤儿 tmp（10.82 GB）+ 救 9 个 B 类卡死实例（74/54/66/40/73/8/67/170/188）+ 总释放 ~13.5 GB + 操作总用时约 1 小时
- 经验：**B 类清完后通常不再触发 reindex**，5Gi 是给意外 reindex 留的缓冲，绝大多数情况都用不上
- **inplace 方案首跑**（继上次 4Gi 集群升级 5.5h 后引爆 7 个 CRITICAL）：
  - 4 个 ORPHAN-only (her-26/161/68/31) → Phase 0 inplace rm，**全程零下线零重启**，释放 1.34 GB，单实例 16s
  - 3 个含 ACTIVE (her-8/74/2) → Phase 0 先清孤儿（释放 1.93 GB），ACTIVE 部分**等自愈**（不重启）
  - 关键诊断证据：sqlite readonly probe 在 her-8 卡 ≥2.5min（[判断 3](#判断-3sqlite-是否被-reindex-锁住判断主线程是否被殃及)）+ her-74 日志 10min 内 3 次 `bot-registry re-registered`（[判断 5](#判断-5从日志找-event-loop-被-block-的间接证据)）
  - 集群级触发因子识别（[判断 6](#判断-6集群级触发因子识别避免一个一个修反复出-case)）：6/7 实例 `TMP_OLDEST_AGE_H` 集中在 5.3-5.4h，对齐 RS AGE 5h29m → 锁定上次集群升 4Gi 部署同时引爆，4% 失败率

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

- **`her-oom-alert-triage`**：OOM / 内存告警的入口分诊。**先用它确认是不是 reindex 死循环**，再决定是否走本 skill。压制 compaction archive / 集群批量升 limit / 阿里云 ACK 阈值告警 都属于那个 skill 的范围
- **`litellm-ops`**：LiteLLM proxy 状态、cooldown、429 排查。**[判断 7] 命中"上游 fail"时直接跳到这个 skill**——上游 fail 期间任何对 carher pod 的操作都是反作用
- `check-instance-status`：单实例日志/状态/重启历史
- `carher-memorysearch-config`：embedding 配置链路（providerKey / scopeHash / sources 变化的源头）
- `carher-instance-config-override`：临时给单实例打 override（方案 F 切 fts-only 时用）
- `hot-grayscale`：零下线重启（本 skill 用的是 paused 方式重启；`her-oom-alert-triage` 用的是 patch deployment 自动 rolling）
