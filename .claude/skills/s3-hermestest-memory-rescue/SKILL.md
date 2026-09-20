---
name: s3-hermestest-memory-rescue
version: 1.1.0
description: >-
  S1 / S2 / S3 (JSZX-AI-0{1,2,3}) 上的老 carher Docker 实例慢 / "@-不回" /
  "model idle timeout" / session lock 超时 的三维度（memory sqlite bloat +
  FTS5 索引 corrupt + session jsonl bloat）救援流程：
  integrity_check → FTS5 rebuild → sessions 归档 → embedding_cache GC →
  VACUUM → WAL truncate → docker restart。
  Use when 用户报 hermestest-N / carher-N "慢"、"有的群艾特他没反应"、"半天不回复"、
  "model idle timeout"、"Something went wrong"，或要做集群级健康巡检。
  与 K8s 的 [[her-memory-reindex-rescue]] 平行（那个针对 carher-N pod / NAS PVC，
  本 skill 针对自建机房 Docker / 本地 ext4）。
  **不要被 skill 名 `s3-...` 误导——S1/S2/S3 同一布局适用**。
metadata:
  requires:
    bins: ["scripts/jms"]
---

# S1 / S2 / S3 hermestest 内存 / 会话 救援

## 节点速查（先确认实例在哪台）

| 节点 | JMS asset | 内网 host | 容器命名 | 典型实例 |
|---|---|---|---|---|
| S1 | `JSZX-AI-01` | `jszx-ai-186` | `hermestest-{N}` | hermestest-13/199-dual/200 |
| S2 | `JSZX-AI-02` | `jszx-ai-187` | **`carher-{N}`**（裸名，不是 hermestest 前缀）| carher-221 (T1V) |
| S3 | `JSZX-AI-03` | `jszx-ai-188` | `hermestest-{N}` | hermestest-14/75 |

**S2 容器命名跟 S1/S3 不一样**——`docker ps` 名字是 `carher-{N}` 而非 `hermestest-{N}`。其他文件布局（`/data/.openclaw/memory/`、env_file 路径）三台一致。下面所有命令把 `hermestest-{N}` 换成 S2 上的 `carher-{N}` 即可。

## 背景

自建机房 JSZX-AI-0{1,2,3} 跑老 carher 用户的 Docker 容器，每个对应一个 carher 用户。数据卷路径仍是 `carher-{N}-data`。所有访问走 `scripts/jms ssh JSZX-AI-0X` 堡垒机。

跟 K8s 的差异（不要照搬 [[her-memory-reindex-rescue]] 的命令）：

| 维度 | K8s carher | S3 hermestest |
|------|-----------|---------------|
| 容器/Pod 入口 | `kubectl exec -n carher carher-{N}-xxx -c carher --` | `docker exec hermestest-{N}` |
| 集群入口 | kubectl tunnel | `scripts/jms ssh JSZX-AI-03` |
| sqlite/sessions 路径 | `/data/.openclaw/memory/`, `/data/.openclaw/agents/main/sessions/` | 同左（容器内 mount 是一致的）|
| 宿主机 mount | NAS PVC (NFS, vers=3) | local ext4 at `/Data/carher-runtime/deploy/carher-{N}/data-home/` |
| sqlite Python 解释器 | node:sqlite (carher 容器自带) | python3 stdlib `sqlite3` |
| Reindex 死循环（模式 A）| 常见，因 NFS 同步调用 block event loop | **极少**，本地 fs 快很多 |
| Cache bloat（模式 B）| 常见 | 也会出现但增长更慢 |
| **Session jsonl bloat（模式 D）** | 较少（K8s 会更频繁 churn） | **常见且更致命** —— 单 session jsonl 涨到 20-50 MB，session-resource-loader 单次加载 ≥18 秒 + SessionWriteLockTimeoutError 60 秒 |
| **FTS5 索引 corrupt（模式 E）** | 见过 1 次 | **2026-05-24 carher-221 (T1V) 首例** —— `PRAGMA integrity_check` 返回 `malformed inverted index for FTS5 table main.chunks_fts`；prework cold scan 撞 corruption 时 block event_loop；用户消息 dispatch 后无响应直到 expired |

**本 skill 的核心新增**：模式 D（session bloat）+ 模式 E（FTS5 corrupt）—— [[her-memory-reindex-rescue]] 几乎没覆盖的失败模式。

## 症状速览

用户报：
- "hermestest-N 慢" / "@ 它半天没反应" / "有的群艾特他没反应但有的群能回"
- 飞书里看到 `⚠️ Something went wrong while processing your request`
- 或者 `The model did not produce a response before the model idle timeout`

容器层看：`docker ps` 显示 `Up X hours (healthy)`，CPU/Mem 都正常，**没有 OOMKilled，没有 restart**。所以监控会说"一切正常"，只有用户体感是坏的。

## 三段式归因（开工前先验）

**假设**：memory sqlite + session jsonl 文件膨胀 → cold scan / 加锁 block event loop → 把 LLM 请求从 timer 上 abort 掉 → "@ 不回"。**或** FTS5 倒排索引 corrupt → prework cold scan 撞 corruption 时 block event_loop（模式 E）。

**证伪条件**：若假设错，应见 (a) sqlite < 500 MB (b) session 单文件 < 1 MB (c) `eventLoopDelayMaxMs` < 100 ms (d) timeout 来源是上游而非 client abort (e) `PRAGMA integrity_check` 返回 `ok`。

**实测数据落点**（一行命令出全部）：

```bash
N=14   # 实例编号
bash scripts/diag.sh "$N"
```

正常 baseline vs 故障 baseline：

| 指标 | 健康 | 故障 |
|------|------|------|
| `main.sqlite` | < 300 MB | ≥ 1 GB |
| `sessions/` 总占用 | < 500 MB | ≥ 1.3 GB |
| 单 session jsonl 最大 | < 1 MB | ≥ 20 MB |
| `eventLoopDelayMaxMs` 24h 峰值 | < 500 ms | ≥ 4000 ms |
| `session-resource-loader` 单次 | < 500 ms | ≥ 5000 ms |
| `SessionWriteLockTimeoutError` 24h | 0 | ≥ 1 |
| `lane task error: durationMs=630000` | 0 | ≥ 1 |
| `All models failed (timeout)` | 0 | ≥ 1 |
| `PRAGMA integrity_check` | `ok` | `malformed inverted index for FTS5 table chunks_fts`（模式 E）|
| dispatch 后日志静默时长 | <10s | ≥ 2 min + 后续消息 `expired, discarding` |

## 诊断决策树

```
docker ps + docker stats        →  容器在跑、低 CPU/Mem
                                  （否则进 K8s/Docker 通用排查，不在本 skill 范围）
            ↓
docker logs 最近 24h grep:
   "All models failed (timeout)"     ──┐
   "SessionWriteLockTimeoutError"     ─┤→ 命中其中 ≥1 → 进入救援
   "lane task error: durationMs=630000"┤
   "model idle timeout"               ─┤
   "dispatching to agent" 后无下文     ─┤  ← 模式 E (FTS corrupt) 的 dispatch 静默
   liveness eventLoopDelayMaxMs ≥4000ms┘
            ↓
跑 scripts/diag.sh <N>
            ↓
   PRAGMA integrity_check                                    ← 必跑（模式 E）
     - 'ok'  → 跳到 size 判断
     - 'malformed inverted index for FTS5 table chunks_fts'  → Phase B0 (FTS rebuild) 必做
     - 其他 corrupt → 升级处理（drop+rebuild from chunks，或 reset 实例）
   main.sqlite ≥ 1 GB?  → Phase B (sqlite GC + VACUUM) 必做
   sessions/ ≥ 1.3 GB?  → Phase A (sessions GC) 必做
   docker restart       → Phase C 总是做（清 in-memory 锁 + 重置 event loop）
            ↓
跑 scripts/rescue.sh <N>  （integrity → A + B0 + B + C 串行 ~3-4 分钟）
            ↓
跑 scripts/diag.sh <N>     再确认指标回到 baseline
```

## 救援脚本（一键 integrity → A → B0 → B → C）

```bash
bash scripts/rescue.sh <N>
```

脚本做的事（按顺序）：
1. **预检 integrity** — `PRAGMA integrity_check`。**不 ok 时不能直接 VACUUM INTO backup**——backup 会复现 corruption（VACUUM INTO 是逻辑拷贝带 schema 含坏索引）。必须先 Phase B0 修
2. **A. Sessions GC** — 把 `*.trajectory.jsonl` 和 `*.trajectory-path.json` 中 `mtime > 7 天` 的文件 rename 成 `*.archive.<TS>`（不删，保 7 天兜底）；同时清掉 `mtime > 60 分钟` 的孤儿 `.lock` 文件
3. **B0. FTS5 rebuild（仅当 integrity 报 malformed inverted index）** — `INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')`；再 `PRAGMA integrity_check` 确认 'ok'。**先做 raw `cp` 而不是 `VACUUM INTO` 备份**，避免 backup 也带坏索引——`cp` 是 byte-for-byte，能保留原始 corrupt 状态做 forensic
4. **B1. Backup** — 若 integrity 已经 ok，用 `VACUUM INTO` 一致快照到 `main.sqlite.bak-rescue-<TS>`；若 B0 走过了，raw `cp` 已经做了备份
5. **B2. Integrity check on backup** — backup 跑 `PRAGMA integrity_check`，行数对照源库；不 ok 则 abort
6. **B3. GC embedding_cache** — `DELETE FROM embedding_cache WHERE updated_at < (now - 7d) * 1000`（**updated_at 是 ms 不是 s**，踩过这个坑）
7. **B4. VACUUM main** — 整理碎片回收 freelist
8. **B5. ls -lh** 报告最终大小
9. **C. docker restart** — 30s 内 healthy（**S2 容器名是 `carher-{N}` 不是 `hermestest-{N}`**）
10. **C2. WAL truncate** — restart 后 wal 可能仍是几百 MB，必须 `PRAGMA wal_checkpoint(TRUNCATE)` 手工清。**docker restart 本身不会自动 truncate wal**（实测踩过）

> ⚠️ **VACUUM 期间 sqlite 持 EXCLUSIVE 锁**。脚本在容器运行时跑，主程序 memory.sync 会排队 1-3 分钟，期间用户消息回复体感更慢——结束后立刻恢复。本地 ext4 上 VACUUM 8 秒搞定，比 K8s NFS 的 60-180 秒快得多。

## 验证（救援后 10h 健康基线）

`scripts/diag.sh <N>` 应该看到全部归零或显著下降：

| 指标 | 救援前 24h | 救援后 10h | 健康判定 |
|------|-----------|-----------|---------|
| `surface_error` / `All models failed` | ≥ 多次 | 0 或 1（auto-compaction 自愈不算）| ✓ |
| `SessionWriteLockTimeoutError` | ≥ 1/h | 0 或 ≤ 0.5/h | ✓ |
| `lane task error: durationMs=630000` | ≥ 1/h | 0 | ✓ |
| `[ws] reconnect` | ≥ 1/h | 0 | ✓ |
| `liveness eventLoopDelayMaxMs` | ≥ 5000ms | ≤ 5000ms | ✓ |
| `liveness warning` 频率 | ≥ 几乎不停 | ≤ 6/h | ✓ |

**重要**：context overflow auto-compaction 走得通就不算故障。看到 `context overflow detected ... auto-compaction succeeded ... retrying prompt` 三连就是健康自愈路径；只有 `All models failed (timeout)` 才是真 fail。

## 已知坑（实战累积）

1. **嵌套 shell 引号地狱**：`scripts/jms ssh JSZX-AI-03 'docker exec hermestest-N python3 -c "SELECT ..."'` 三层引号穿越很难写对。**对策**：把 Python 脚本写到本地 `/tmp/x.py` → `scripts/jms scp /tmp/x.py JSZX-AI-03:/tmp/x.py` → `docker cp /tmp/x.py hermestest-N:/tmp/x.py` → `docker exec hermestest-N python3 /tmp/x.py <args>`。本 skill 的 `scripts/rescue.sh` 已经这样做。

2. **embedding_cache.updated_at 是毫秒，不是秒**。`SELECT MIN(updated_at), MAX(updated_at)` 看到 13 位数就是 ms。GC 写 `(now - 7d) * 1000`，否则 `(now - 7d)` 当 ms 用永远 < min(updated_at)，删 0 行还以为没 bloat。

3. **`PRAGMA wal_checkpoint(TRUNCATE)` 必须手工跑**：docker restart 后 carher 进程刚启动，还没触发自动 checkpoint；只要 wal_autocheckpoint 没达到，wal 就一直占几百 MB 磁盘。脚本里强制跑一次 truncate。

4. **`du -sh sessions/` 不能反映 GC 效果**：因为脚本 rename 成 `*.archive.<TS>` 而不是 rm（保 7 天兜底）。真正的指标是 "active scannable files"：`find sessions -maxdepth 1 \( -name "*.jsonl" -o -name "*.json" \) ! -name "*.archive.*"` 的总字节数。

5. **`-mtime +7` 保留 5/15 文件（今天 5/22）= 7 天前**：边界包含与否取决于 find 实现。如果你想确保更激进，用 `-mtime +6` 或显式日期。**不要为了清更多去把活跃 session（mtime 最近 24h）也清掉** —— 那是用户当前对话历史，删了用户会觉得 bot 失忆。

6. **chunks 表是记忆本体不能删**。Bloat 的"安全"目标只有：`embedding_cache`（cache，删了下次 LLM 重算）和老 `trajectory.jsonl`（write-only 审计日志）。**不要 DELETE FROM chunks** —— 删了用户的所有记忆会消失。

7. **sqlite3 CLI 容器内不可用**，但 python3 stdlib `sqlite3` 可以。`from node:sqlite` 是 K8s 那边 carher 镜像的特性，S3 hermestest 没有，全部用 `python3 -c "import sqlite3"`。

8. **chunks_vec 表 query 会报 `no such module: vec0`**：因为 stdlib sqlite3 没加载 sqlite-vec 扩展。对于 inspect 类查询无所谓（不查 chunks_vec 就行）；如果一定要查 vec，需要 `c.enable_load_extension(True)` + 找到容器内 sqlite-vec.so 的路径（一般在 `/opt/openclaw/...`）。

9. **JSZX-AI-03 cltx 用户无需 sudo 跑 docker**（不像别的机器）。直接 `docker exec` / `docker ps` / `docker stats`。

10. **不要在 docker 直连 `cltx@10.68.13.188`**，必须走 `scripts/jms ssh JSZX-AI-03`（JumpServer KoKo 网关）。

11. **`PRAGMA integrity_check` 必须在 GC 之前跑** — 否则 corruption 跟着 GC + VACUUM 沉淀进新 main.sqlite，restart 后 prework cold scan 还是撞坏 FTS index 卡死。dispatch 后 2 分钟静默 + 后续消息 `expired, discarding` 不是 cache bloat 是 FTS 损坏（2026-05-24 carher-221 (T1V) 首例）。**`VACUUM INTO backup` 不能修 corruption**——backup 也是坏的。先 `INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')` 修索引，再做后续 GC。

12. **FTS5 rebuild 之前别 VACUUM INTO** — VACUUM INTO 是逻辑拷贝（生成 schema + 重写所有行），坏的 FTS index 在 schema 里，**backup 也会复现 corruption** 让你以为是 source 又坏一次。改用 raw `shutil.copy2(src, bak)` 或 `cp` byte-for-byte 备份，留住原始 corrupt 状态做 forensic，再原地 rebuild。

13. **S2 容器名是 `carher-{N}` 不是 `hermestest-{N}`** — 凡是 `docker exec hermestest-{N}` 的命令 S2 上要换成 `docker exec carher-{N}`。文件路径布局 S1/S2/S3 一致。

## Edge Cases

- **容器不 healthy**：先 `docker logs --tail 100 hermestest-N` 看 startup 失败原因，不在本 skill 范围（参考 LiteLLM/Codex 配置 skill）
- **OOMKilled**：本地 ext4 不会因 IO block 触发 OOM；如果真见到 OOMKilled，宿主机内存压力或别的进程问题，跟本 skill 无关
- **`main.sqlite.bak.<旧日期>` 一直在**：上次 rescue 留的兜底备份。skill 建议 **保留 7 天** —— 救援后第 8 天可以删
- **多个 hermestest-N 同时出问题**：可以 for 循环串行救（不要并行，VACUUM 期间宿主机 disk IO 集中可能影响别的容器）
- **救援后症状立刻复发**：检查 `top 5 active sessions by size` 是否仍有 20+ MB 文件且 mtime 是当天 —— 说明有用户在大 session 内持续对话；治本是让该用户在该群 `/new`，否则 7 天后这个文件会自然过 cutoff 被本 skill 下一轮归档

## 历史结果参考

**2026-05-21 hermestest-14 + hermestest-75 首例**：

| | hermestest-14 救援前 24h | 救援后 10h | 救援后 48h (5/23) |
|--|--|--|--|
| surface_error / All models failed | 多次 | 5 (全 auto-compaction 成功) | 1 |
| SessionWriteLockTimeout | 频繁 | 5 | **0** |
| ws reconnect | 多次 | 0 | 0 |
| worst event_loop max | 4.3s | 5.2s | **3.3s** |

| | hermestest-75 救援前 24h | 救援后 10h | 救援后 48h |
|--|--|--|--|
| lane task error 604s | 频繁 | 5 | 2 |
| liveness warning 频率 | 几乎不停 | 6/h | **2.8/h** |
| worst event_loop max | 12.4s | 8.5s | **4.7s** |

GC 命中数据：
- hermestest-14: sessions 归档 511 文件 / 369 MB；sqlite 1.05 GB → 971 MB；embedding_cache 4344 → 2994
- hermestest-75: sessions 归档 724 文件 / 439 MB；sqlite 1.18 GB → 1.1 GB；embedding_cache 3651（24h 内全部，无可 GC）

**关键发现**：hermestest-75 的 embedding_cache 全是 24h 内的，模式 B（cache bloat）几乎不命中；它的"@-不回"症状 90% 来自模式 D（session jsonl bloat → SessionWriteLockTimeoutError → event loop block）。所以**别只看 sqlite 大小**，要同时查 session 文件大小。

---

**2026-05-24 carher-221 (T1V, S2) 模式 E 首例**：

| | 救援前 | 救援后 |
|--|--|--|
| 症状 | 群里 @T1V 半天不回，dispatch 后日志静默 2 分钟，后续消息全 `expired, discarding` | dispatch 完成、reply 正常 |
| `PRAGMA integrity_check` | `malformed inverted index for FTS5 table main.chunks_fts` | **ok** ✓ |
| main.sqlite 大小 | 704 MB | **187 MB** |
| embedding_cache 行数 | 19288（96% > 7 天，老化严重）| 778 |
| `liveness eventLoopDelayMaxMs` | 2871 ms 持续报警 | 60s 内 0 次 |

**关键发现**：T1V 的"半天不回"**首要根因是 FTS5 索引 corrupt**，不是 cache bloat。如果一上来就 `VACUUM INTO backup` + GC + VACUUM，backup 也会复现 corruption（VACUUM INTO 是逻辑拷贝带 schema 含坏索引），restart 后 prework 还是卡。正确顺序：

1. **先 `PRAGMA integrity_check`** —— 没有 integrity_check 就动手是大坑
2. **若 FTS 坏**：先 raw `cp` 备份（保留 forensic）→ `INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')` 修索引 → 再次 integrity_check 确认 ok
3. **再走标准 GC**：DELETE stale embedding_cache → VACUUM → WAL truncate → restart

T1V 的 FTS rebuild 只花了 0.3s（2633 行），相比下 VACUUM (1.5s) 和 raw copy (15.2s) 都更快——但**没修就直接做后续步骤的话，整套 rescue 等于白做**。

## 相关 skill

- **[[her-memory-reindex-rescue]]**：K8s 版本，覆盖模式 A (reindex 死循环) + B (cache bloat) + C (stream consumer hang)。本 skill 是 S3 docker 的 sibling，强调模式 D (session bloat)
- **[[s3-hermestest-litellm-config]]**：S3 hermestest 的 LiteLLM 路由配置，跟本 skill 平行（一个管模型，一个管内存/会话）
- **[[migrate-s3-to-k8s]]**：S3 → K8s 迁移，根治"S3 老实例反复 rescue"的长期方案
- **[[carher-her-reply-failure-triage]]**：K8s 上"reply 失败"决策树，本 skill 是 S3 上对应的"慢 / @-不回"决策树
- **[[k8s-via-bastion]]**：`scripts/jms` 包装器与堡垒机访问规则
