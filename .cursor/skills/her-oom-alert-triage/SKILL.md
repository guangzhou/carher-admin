---
name: her-oom-alert-triage
description: >-
  CarHer her 实例 OOM / 内存告警的分诊与处置：辨别"阿里云 ACK 阈值告警"
  vs "K8s 真 OOMKilled" vs "reindex 死循环" vs "compaction archive 内存爬升"
  vs "active session 巨大"，给单实例临时加 limit、集群批量升 4Gi、清理老
  session archive。Use when the user mentions "OOM" + "carher" / "her",
  "内存告警" / "memory alert" / "ACK 告警" / "阿里云 监控", "反复 OOM",
  "内存利用率高" / "memory utilization", or wants to scan / patch /
  rescue OOM-prone instances cluster-wide. **本 skill 不处理 reindex 死循环
  专属流程**——若已确认 main.sqlite.tmp-* 堆积，请改用 `her-memory-reindex-rescue`。
---

# Her OOM / 内存告警 分诊与处置

## 入口：用户报"OOM"时先做的事

收到"实例 OOM"/"内存告警"时，**先不要急着重启**，先分诊：告警源是什么？是真 OOM 还是阈值告警？

```
┌──────────────────────────────────────────────────┐
│ 用户说："her-X 又 OOM 了" / "收到 OOM 告警"          │
└──────────────────────────────────────────────────┘
                       │
                       ▼
       ┌───────────────────────────────────┐
       │ Step 0: 告警源在哪里？              │
       └───────────────────────────────────┘
        │                              │
   飞书 ACK 邮件                  K8s lastState
        │                              │
        ▼                              ▼
  阈值告警？                      真 OOMKilled？
  (不是真 OOM)                   (event=PodOOMKilling)
        │                              │
   集群 4Gi 升级                  分诊根因（Step 2）
   清理老 archive
```

## 三种"OOM 告警"的辨别

| 告警源 | 形式 | 真假 | 关键词 |
|---|---|---|---|
| 阿里云 ACK 监控 | 飞书邮件 / 飞书消息，发件人 `monitor@monitor.aliyun.com` | **常常不是真 OOM**——通常是 `container_memory_working_set_bytes / memory_limit > 60%` 阈值告警 | "工作集"、"利用率"、"超过阈值" |
| K8s `PodOOMKilling` 事件 | `kubectl get events --field-selector reason=PodOOMKilling` | **真 OOM**——内核 cgroup OOM killer 触发 | "OOM killed" |
| pod `lastState.terminated` | `kubectl get pod -o json` 里 `reason=OOMKilled exit=137` | **真 OOM**——但 pod 被替换后会丢，要尽早抓 | reason=OOMKilled |

K8s events 默认 1h TTL，pod replaced 后 lastState 也会丢——历史 OOM 容易"消失"。**5 信号扫描**（见 Step 1）能尽量补全。

## 根因家族（按出现频率排）

| 根因 | 触发场景 | 识别信号 | 处置 skill |
|---|---|---|---|
| **阈值告警**（不是真 OOM） | 单实例 mem 长期 > 60% limit，触发监控阈值 | K8s 无 OOM 事件，但 ACK 有告警 | 本 skill：升 limit |
| **compaction archive 累积** | session 增长到 10万+ token 触发 compaction，老 archive 留在 PVC，hook 反复加载 36MB+ jsonl | sessions/*.reset.* 总量大、单文件 ≥ 30MB | 本 skill：archive 体积扫描 + 清理 |
| **active session 巨大（单条超大）** | 老 image 时代某次 toolResult 返回几十-几百 MB（飞书文档/jira/cloud listing 等），写入 jsonl 一行就是巨型——guard 没装时直接进文件 | active *.jsonl 单文件 ≥ 30MB **且单行 size 巨大** | 本 skill：归档死会话 / 升 limit / 等 truncateOversizedToolResultsInSession |
| **active session 巨大（条数累积）** | 长跑会话（数千条），单条不大但累积 100-300MB；compaction 已跑 N 次但**老 message entry 没物理删除** | active *.jsonl ≥ 100MB **且** compaction count 数十次 **且** 单行平均 < 1MB | 本 skill：**手动 streaming truncate** (Step 3.4) |
| **reindex 死循环** | embedding 上游切换 → providerKey 变化 → main.sqlite.tmp-* 堆积 + main.sqlite mtime 多天前 | TMP_COUNT > 0 且 MAIN_AGE_H > 24 | **改用 `her-memory-reindex-rescue`** |
| **大上下文 + 长流式** | 单次大消息（含群历史注入）+ LLM 长流式输出，瞬时峰值超 limit | 没有 archive / tmp 信号，OOM 时机随机 | 本 skill：升 limit |

> **常见误判**：用户报"OOM"，先看 K8s 真事件——大概率是阈值告警，不是真 OOM。**不要直接执行 paused-toggle 重启**——重启会让历史 lastState 丢失，反而失去诊断证据。

## kubectl / 集群关键事实（从历史救援沉淀）

- her CRD `spec.paused` 切换不会让 operator 重做 deployment（仅 scale 0/1）→ 直接 `kubectl patch deployment` 改 memory limit **不会被 operator 调和回去**
- 但 `kubectl patch deployment` 改 `resources.limits.memory` **会被 K8s 自身识别为 Pod template 变更，立即触发 rolling update**（不是热生效）
- 单副本 Her 用 readinessGate `carher.io/feishu-ws-ready`：rolling update 期间老 pod 一直 Ready，新 pod 拉好 + ready 后才切流——零 ws 断开
- 集群 `kubectl get events` TTL 默认 1h；`describe pod` 也只看到最近 1h；要追历史 OOM 必须靠多信号叠加（lastState、events PodOOMKilling、events Killing、container restartCount、recent created pod）
- `cgroup memory.current` 包含 page cache（Chrome user-data 1.6G 也算）；看真实工作集要 `kubectl top pod`

## Workflow

```
[ ] Step 0: 抓告警源 (飞书 / kubectl events)
[ ] Step 1: 5-signal 扫描 (确认是真 OOM 还是阈值告警)
[ ] Step 2: 根因分诊 (mem 利用率 / archive 体积 / active session)
[ ] Step 3: 处置 (单实例 patch / 集群 4Gi 升级 / archive 清理)
[ ] Step 4: 复扫验证
```

## Step 0: 抓告警源

### 0.1 K8s 真 OOM 事件（5 信号）

```bash
SKILL_DIR=.cursor/skills/her-oom-alert-triage
mkdir -p /tmp/her-triage

# 5-signal 扫描：lastState OOM / events PodOOMKilling / events Killing /
# 任何 restart / 最近创建的 pod
$SKILL_DIR/scripts/scan_oom_signals.sh 180   # 180 = 最近 180 分钟
```

输出会按信号分组列出每个 her id 的最早异常迹象。**0 个信号 = 无真 OOM**（即便用户收到了告警，也是阈值类）。

### 0.2 阿里云 ACK 告警（飞书）

```bash
# 用 lark-cli 找最近 ACK 告警邮件
lark-cli mail +search --query "ACK 告警" --max 20
# 或 im 群消息
lark-cli im +chat-search --query "OOM" --max 20
```

发件人是 `monitor@monitor.aliyun.com` 且邮件正文有"工作集"、"内存利用率"等字样的 → **几乎都是阈值告警，不是真 OOM**。

## Step 1: 内存利用率扫描

```bash
$SKILL_DIR/scripts/scan_mem_usage.sh
```

输出按 `mem_used / mem_limit` 利用率倒排，TOP 15 的实例就是阈值告警的常客。

判定：
- 利用率 > 80% **且** limit = 3Gi → **强烈建议升 4Gi**
- 利用率 > 60% **且** limit = 4Gi → 看是否 active session 巨大；考虑升 5Gi 单点
- 利用率 < 50% → 当前 limit 足够，告警是抖动 / page cache 误报

## Step 2: Session archive 体积扫描

`compaction-archive` 内存爬升是真 OOM 的常见原因（不是 reindex）。扫描全集群每个 pod 的 `/data/.openclaw/agents/main/sessions/` 下：

- `*.reset.*` / `*.deleted.*` / `*.bak-*` = archive 文件（compaction 留下的历史快照）
- `*.jsonl` 不带后缀 = active session（当前会话）

```bash
$SKILL_DIR/scripts/scan_session_archive.sh
```

输出风险表：

| 信号 | 阈值 | 含义 |
|---|---|---|
| `archive_total ≥ 100M` | HIGH | 老归档堆积，hook 反复加载会爆 |
| `single archive ≥ 30M` | HIGH | 一次 hook 加载就吃 80-150MB JS 对象 |
| `active session ≥ 30M` | ACTIVE_BIG | 启动 `repairSessionFileIfNeeded` 全量读 |
| `active session ≥ 100M` | ACTIVE_CRITICAL | **极危**——重启即 OOM 风险 |

**ACTIVE_CRITICAL 的实例不能贸然重启**——必须先升 limit 到 4Gi 或 5Gi 才能安全重启。

## Step 3: 处置

### 3.1 单实例临时升 limit（5-15 分钟内生效）

```bash
$SKILL_DIR/scripts/resize_her.sh 166 4Gi   # her-166 升到 4Gi
$SKILL_DIR/scripts/resize_her.sh 40 5Gi    # her-40 升到 5Gi (active 271MB)
```

脚本做：
1. `kubectl patch deployment carher-$HID` 改 memory limit（索引 0 的 carher 容器）
2. **K8s 检测到 Pod template 变更，立即触发 rolling update**
3. ReadinessGate 保证零 ws 断开
4. 验证最终 pod limit 与 ws ready

不需要 paused-toggle——`patch deployment` 已经天然触发滚动。

### 3.2 集群批量 4Gi 升级（处理阈值告警）

如果 `scan_mem_usage.sh` 显示集群整体已逼近 3Gi 上限（>10% 实例 mem%>80），就批量升级到 4Gi：

```bash
$SKILL_DIR/scripts/patch_cluster_mem.sh 4Gi   # 把所有 limit=3Gi 的 deploy 改 4Gi
```

脚本做：
1. 列出 `.spec.template.spec.containers[?(@.name=="carher")].resources.limits.memory == "3Gi"` 的所有 deployment
2. 逐个 `kubectl patch` —— 每个触发自己的 rolling update（ReadinessGate 平滑切换）
3. 不并行（API server 节流），200 个实例约 30 分钟跑完

> ⚠️ 这个脚本会触发**集群范围 rolling update**——所有 her pod 在 30 分钟内会被替换一遍。
> 因为有 ReadinessGate，单个用户感知是 0；但建议在低峰期跑。

升级完不要忘了**改 operator 默认值**：

```
operator-go/internal/controller/reconciler.go
```

里 `corev1.ResourceMemory: resource.MustParse("3Gi")` 改成 `4Gi`，否则下次新建实例又是 3Gi。

### 3.3 老 archive 清理（compaction 类 OOM 的根本治理）

⚠️ **要小心**：`loadBeforeResetTranscript` 在某些 reset hook 里会读最新的 `.reset.*` 文件——直接全删可能丢 hook 数据。

策略：保留**最近 2 份**reset、删 7 天前的：

```bash
$SKILL_DIR/scripts/clean_session_archives.sh 166      # dry-run，列出会删什么
$SKILL_DIR/scripts/clean_session_archives.sh 166 yes  # 真删
```

脚本做：
1. 列出 `*.reset.*` / `*.deleted.*` / `*.bak-*`，按 mtime 倒排
2. 保留最近 2 份 reset
3. 删 mtime ≥ 7 天的 reset / deleted / bak

清理是在线的（不重启 pod），但建议**重启一次让进程释放任何可能 cache 的旧 archive 引用**。

### 3.4 单实例 active session 手动 streaming truncate（条数累积型 OOM 的根本治理）

**适用场景**：`scan_session_archive.sh` 报 `ACTIVE_CRITICAL`/`ACTIVE_BIG`，**且** 进 pod 看 `wc -l` 显示几千条 entry、单行平均 < 1MB（不是单条爆炸型）。典型现场：

| 指标 | 临界值 | her-40 实例 |
|---|---|---|
| 文件大小 | ≥ 100MB | 271MB |
| entries 总数 | ≥ 1500 | 2275 |
| compaction 次数 | ≥ 30 | 47 |
| 单行平均 size | < 1MB | ~120KB |

**根因**：carher 主程序在 compaction 时**只逻辑标记，不物理删除老 message**——除非配置 `compaction.truncateAfterCompaction: true`。但这个开关：
- 在 `base-config.yaml` 改了**不会立即生效**（`shared-config.json5` 是 subPath 挂载，K8s 不自动同步）
- 即便代码生效了，也是"下次 compaction 才修剪"——已经累积到 271MB 的不会自动瘦身

所以需要**离线手动 truncate**。本 skill 提供脚本 `truncate_session_jsonl.py`，**完全镜像** carher 内部 `truncateSessionAfterCompaction` 的语义（pi-embedded.js:30002-30117），但流式读写、不依赖代码生效、自动备份。

**安全保证**（重要）：
- 只删**最后一次 compaction 的 firstKeptEntryId 之前**的 message entry —— 这些 entry 已经被摘要进 compaction summary，agent 逻辑上**已经不再读它们**
- 保留全部 47 个 compaction summary、所有 model_change/thinking_level_change/custom 等状态 entry、以及 firstKeptEntryId 之后的所有内容
- 自动 hardlink 备份原文件到 `<file>.pre-truncate-backup-<TS>`（同 inode，不占额外空间，立刻可回滚）
- 原子 rename `.truncate-tmp` → 原文件名（POSIX 保证 reader 看到完整旧或完整新，不会看半个）
- carher 内存里 sessionManager **不感知**外部改动 —— 但因为它用 in-memory state 而不是 file offset，新对话仍正常 append；下次 pod 重启时从新 jsonl rebuild，**没有错位风险**

**workflow**：

```bash
# 0) 找目标 pod 和 jsonl
HID=40
POD=$(kubectl get pod -n carher --no-headers | grep "^carher-${HID}-" | awk '{print $1}')

# 哪个 jsonl 最大？
kubectl exec -n carher "$POD" -c carher -- ls -lhS /data/.openclaw/agents/main/sessions/ | grep -v "\.reset\.\|\.deleted\.\|\.bak-\|backup-" | head -5
SESSION=/data/.openclaw/agents/main/sessions/<那个最大的 .jsonl>

# 1) 拷贝脚本到 pod
kubectl cp $SKILL_DIR/scripts/truncate_session_jsonl.py carher/$POD:/tmp/truncate.py -c carher

# 2) dry-run（必做）
kubectl exec -n carher "$POD" -c carher -- python3 /tmp/truncate.py --dry-run "$SESSION"
# 期望输出：to drop X 条 (~Y MB)，估算 reduction 90%+

# 3) 真跑（idle-secs 默认 30，要求文件 30s 内没被改）
kubectl exec -n carher "$POD" -c carher -- python3 /tmp/truncate.py --apply "$SESSION"

# 4) 验证
kubectl exec -n carher "$POD" -c carher -- ls -lh "${SESSION}"*    # 看新文件 + 备份
kubectl get pod -n carher "$POD" -o jsonpath='{.status.conditions[?(@.type=="carher.io/feishu-ws-ready")].status}'
# 应仍为 True
```

**her-40 实战收益**（2026-04-27）：

| 指标 | 之前 | 之后 |
|---|---|---|
| jsonl size | 271.4 MB | 961 KB（**-99.7%**） |
| entries | 2275 | 75 |
| 重启时 fs.readFile 内存峰值 | 估 ~700MB | < 5MB |
| pod 重启代价 | 高（压力极大） | 低 |
| ws 中断 | 0（操作期间未重启） | 0 |

**回滚**：

```bash
kubectl exec -n carher "$POD" -c carher -- mv \
  "${SESSION}.pre-truncate-backup-<TS>" "$SESSION"
# 因为是 hardlink，立刻可逆，无需重启
```

**何时不能用此脚本**：
- jsonl 中存在**单行超大**（> 50MB 单行 toolResult）—— 这是"单条爆炸型"，应该按 3.3 + 升 limit 处理，不是本路径
- jsonl 完全没 compaction entry —— 没有 firstKeptEntryId 就不知道边界，脚本会 exit 1
- 文件 30s 内还在被 carher 写入 —— 用 `--idle-secs 60` 或等会话停下；强制覆盖加 `--idle-secs 0`（不推荐）

### 3.5 集群批量定期 truncate（cron 设计，未部署）

参见 `references/cluster-truncate-cron-design.md`。核心思路：

- 每天凌晨低峰跑 `scan_session_archive.sh`
- 对 `ACTIVE_CRITICAL` / `active ≥ 100MB && compaction ≥ 20` 的实例自动调用 `truncate_session_jsonl.py --apply`
- 强制 `--idle-secs 300`（5 分钟无写入才动手）
- 备份 7 天后自动清理（独立 cron）
- 每次跑完发飞书摘要

**当前未部署**——优先观察手动跑 her-40 一周的稳定性，再决定是否上 cron。

## Step 4: 复扫验证

```bash
$SKILL_DIR/scripts/scan_mem_usage.sh        # 利用率应回落到 < 30%
$SKILL_DIR/scripts/scan_session_archive.sh  # archive total 应大幅下降
$SKILL_DIR/scripts/scan_oom_signals.sh 30   # 30min 内不应再有 OOM 信号
```

## 已知坑（从历史救援沉淀）

- **不要把"收到 OOM 告警"等价于"真 OOM"**：阿里云 ACK 阈值告警和 K8s OOMKilled 是两回事，处置策略也完全不同
- **K8s events 1h TTL**：超过 1h 的 OOM 在 events 里查不到，必须靠 pod lastState + restartCount 累加 + recent created pod 等多信号才能补全
- **kubectl exec + python heredoc + xargs 组合**容易 timeout：脚本里如果有 `python3 - <<'PY'` 嵌入，要把 stdin 让出来；用临时文件中转更稳
- **xargs -P 高并发** 经过 kubectl exec 会被 API server / kubelet 限流：建议 `-P 5`，单 exec timeout ≥ 60s
- **patch deployment memory 触发 rolling update**：不是热生效，pod 会被替换。但 ReadinessGate 保证用户无感
- **operator 不会回滚 memory 变更**（不在 pod-spec-key 里），但下次新建实例 / 大变更时会回到默认 3Gi
- **`grep -c` 在无匹配时退出码非零** + 输出可能含换行：脚本要 `|| echo 0` 兜底再 `tr -d '\n'`
- **page cache 算在 cgroup memory.current**：Chrome user-data 1.6G 也会被算进 memory utilization 触发告警；看真实 RSS 要 `kubectl top` 不是 cgroup
- **`ls -1` 后接 `wc -l` 在 zero match 时返回 1（空行）**：先判 `[ -n "$VAR" ] || VAR=""`，再 wc
- **`shared-config.json5` 改 ConfigMap 不会热生效**：是 subPath 挂载，pod 内文件是 K8s 创建时的快照；改 base-config.yaml 后必须 `kubectl rollout restart`（或下次 pod 替换）才进 pod。`config-reloader` sidecar 只 watch `openclaw.json`
- **carher 内存里 sessionManager 不感知外部 jsonl 改动**：truncate 后老进程仍用 in-memory state，新对话 append 到新文件末尾（`O_APPEND` 多进程安全），下次重启 rebuild from new file —— **不会错位**，但 truncate 操作前避免 `kubectl rollout restart`
- **truncate 脚本备份用 hardlink**：同 inode，不占空间，原 inode 在 backup 引用着；rename 把 jsonl 名字指向新 inode，老 inode 仍存活在 backup 路径，立刻可回滚
- **f-string 在 Python 3.11 之前不能含反斜杠**：`f"{x.get(\"y\")}"` 会语法错；脚本里 `kubectl exec -- python3 -c` 嵌入时要先把转义抽出 `key="y"`，或用 heredoc

## 决策原则

1. **先诊断再处置**：每次都跑 `scan_oom_signals.sh` 确认是真 OOM 还是阈值告警，不要省
2. **批量升 limit 比单点重启更稳**：当 >5 个实例报告告警，直接 `patch_cluster_mem.sh 4Gi` 比逐个 SOP 高效
3. **archive 清理在能下手就下手**：但保留最近 2 份 reset；不要全删（hook 可能用）
4. **active CRITICAL 实例先升 limit 再操作**：active session > 100M 的不能直接重启
5. **不要用 paused-toggle 来"修" OOM**：那个是给 reindex 死循环用的；OOM 该升 limit 就升 limit
6. **条数累积型先 truncate 再考虑加 limit**：active session 大且 compaction 多次的优先走 Step 3.4，体积一缩 99% limit 就够用
7. **truncate 操作要 dry-run + 备份 + 验证三件套，不要图省事**：每个步骤都有自检意义

## 相关 skill

- `her-memory-reindex-rescue`：处理 main.sqlite.tmp-* 堆积 + reindex 死循环（**和本 skill 互补，不要混用**）
- `check-instance-status`：单实例日志 / 状态 / 重启历史
- `lark-im` / `lark-mail`：拉飞书告警邮件、群消息（识别 ACK 告警）
- `hot-grayscale`：零下线重启原理（patch deployment 自动走的就是这条路）

## 历史结果参考

- 一次完整跑通：扫描 201 实例 → 识别 47 HIGH + 40 MED archive 风险 + 10 active session 巨大 → 集群 3Gi → 4Gi 升级（ReadinessGate 零下线）→ utilization 从 99% 降到 9% → 阈值告警全部清零
- 经验：集群整体内存压力主要来自 `compaction archive 累积` 而不是 reindex；compaction archive 不修代码就会持续累积，定期跑 archive 扫描 + 清理是必要的运维动作
- **her-40 单点拯救（2026-04-27）**：271MB / 2275 entries / 47 compactions → streaming truncate → 961KB / 75 entries（**-99.7%**）；ws 0 中断、carher 进程不重启，只需备份 + atomic rename
- **不修代码不会有终结**：本 skill 是周期性运维工具；根本治理需要 carher 主程序仓库做 streaming jsonl parse + archive 自动清理 + reset hook 节流

## 限制

清理 archive + 升 limit + 手动 truncate **不修根因**。代码层根本修复需要在 **carher 主程序仓库**（不是这个 carher-admin）做：

1. **streaming jsonl parse**：`commands-core` 里 `loadBeforeResetTranscript` 改用 `readline.createInterface` 逐行读取，不要 `fs.readFile(sessionFile, "utf-8")` 全量
2. **archive 大小护栏**：readFile 前 stat 检查，> 5MB 时只 tail 最近 N 条 / 完全跳过
3. **archive 自动清理**：compaction 完成后只保留最近 1-2 份归档
4. **reset hook 节流**：短时间内多次 reset 信号合并
5. **`compaction.truncateAfterCompaction` 默认开启**：当前默认 `false` → 老 message 不物理删除 → jsonl 无界增长。把默认改 `true` 是治本动作（已在 `pi-embedded.js:31413` 有调用，只差配置打开）
6. **operator config-reloader 修 subPath 局限**：让 `shared-config.json5` 也能热生效，而不是只 `openclaw.json`；目前 base-config.yaml 改了配置必须 rolling restart 才进 pod

代码修好之前，每隔几天就会有新实例陷入告警——本 skill 是周期性运维工具。
