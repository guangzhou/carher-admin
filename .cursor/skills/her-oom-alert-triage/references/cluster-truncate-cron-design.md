# 集群 active session 定期 truncate cron — 设计文档（未部署）

> 状态：**设计中，等手动跑 her-40 一周观察期通过后再决定是否部署**。
>
> 触发：每天凌晨 03:00（北京时间，集群低峰）。

## 目标

把 her 实例 active session jsonl 的"条数累积型膨胀"自动收掉，避免：
- pod 重启时 `fs.readFile` 全量加载导致 OOM
- compaction 越积越慢
- PVC 长期被无效内容占满

**不目标**：
- 不处理"单条爆炸型"（单行 ≥ 50MB 的 toolResult），那种是数据问题，不是膨胀问题
- 不处理 reindex 死循环（参见 `her-memory-reindex-rescue`）
- 不处理 archive 文件清理（已有 `clean_session_archives.sh`，独立 cron）

## 触发条件

满足**全部**才动手 truncate：

| 条件 | 阈值 | 来源 |
|---|---|---|
| 文件大小 | ≥ 100 MB | `stat` |
| entries 总数 | ≥ 1500 | `wc -l` |
| compaction 次数 | ≥ 20 | grep `"type":"compaction"` |
| 单行平均 size | < 1 MB | size / entries |
| mtime 静止 | ≥ 5 分钟 | `--idle-secs 300` |
| 文件最后一行可解析 | true | 取尾行 `json.loads` |

任何一项不满足就跳过，不试图"修"。

## 实施

### Step 1: 节制版扫描脚本

`scripts/cron_truncate_scan.sh`（待写）—— 列出所有候选实例：

```text
HID    SESSION_FILE                      SIZE    ENTRIES  COMPACTIONS  AVG_LINE  ACTION
40     f735275e-...jsonl                 271M    2275     47           120K      TRUNCATE
56     a1b2c3d4-...jsonl                 153M    1890     38           80K       TRUNCATE
166    7f8e9d-...jsonl                   45M     900      12           50K       SKIP (compactions < 20)
190    abc-...jsonl                      150M    50       1            3M        SKIP (single-row 巨型)
...
```

### Step 2: 节制版 apply 脚本

`scripts/cron_truncate_apply.sh`（待写）：

```bash
#!/bin/bash
set -euo pipefail
SCAN_OUTPUT=$1
DRY_RUN=${2:-yes}   # 默认 dry-run

awk '$NF=="TRUNCATE" {print $1}' "$SCAN_OUTPUT" | while read HID; do
  POD=$(kubectl get pod -n carher --no-headers | grep "^carher-${HID}-" | awk '{print $1}')
  [ -z "$POD" ] && continue
  
  # 拷脚本（每次都拷，保证版本一致）
  kubectl cp scripts/truncate_session_jsonl.py carher/$POD:/tmp/truncate.py -c carher
  
  # 找 active session
  SESSION=$(kubectl exec -n carher "$POD" -c carher -- sh -c '
    cd /data/.openclaw/agents/main/sessions
    ls -1S *.jsonl 2>/dev/null | head -1
  ')
  [ -z "$SESSION" ] && continue
  
  # dry-run 永远跑
  echo "===== her-$HID dry-run ====="
  kubectl exec -n carher "$POD" -c carher -- python3 /tmp/truncate.py --dry-run \
    "/data/.openclaw/agents/main/sessions/$SESSION" 2>&1
  
  if [ "$DRY_RUN" = "yes" ]; then
    continue
  fi
  
  echo "===== her-$HID apply ====="
  kubectl exec -n carher "$POD" -c carher -- python3 /tmp/truncate.py --apply \
    --idle-secs 300 \
    "/data/.openclaw/agents/main/sessions/$SESSION" 2>&1 || {
      echo "[FAIL] her-$HID truncate failed, skip"
      continue
    }
  
  # 验证 ws 仍 ready
  WS_READY=$(kubectl get pod -n carher "$POD" \
    -o jsonpath='{.status.conditions[?(@.type=="carher.io/feishu-ws-ready")].status}')
  if [ "$WS_READY" != "True" ]; then
    echo "[ALERT] her-$HID ws_ready=$WS_READY after truncate, manual check required"
  fi
  
  sleep 5
done
```

### Step 3: 备份保留策略

backup 命名 `<file>.pre-truncate-backup-<TS>`。独立 cron 每周一清理：

```bash
# 删 7 天前的备份（hardlink，删了也不影响 active）
kubectl exec -n carher "$POD" -c carher -- find /data/.openclaw/agents/main/sessions/ \
  -name "*.pre-truncate-backup-*" \
  -mtime +7 \
  -delete
```

### Step 4: 飞书摘要

每次跑完发飞书群（用 `lark-im`）：

```text
[carher-cron] 集群 truncate 报告 2026-04-28 03:15

候选: 12 个
执行: 10 个 (跳过 2: idle 不够)
节省空间: 2.4 GB
失败: 0

详情:
- her-40: 271M → 1M (-99.7%)
- her-56: 153M → 8M (-94.7%)
- ...
```

### Step 5: 部署形态

**两种选择**：

**A. 集群外 cron**（`carher-admin` 容器跑）：
- ✅ 集中可控，逻辑统一
- ✅ 改脚本不用动 image
- ❌ 依赖 kubectl 连接、集群外网络
- 推荐用 `CronJob` 资源，挂 ServiceAccount

**B. 每个 her pod sidecar**：
- ✅ 解耦，故障隔离
- ❌ 200 个 pod × 1 个 sidecar = 维护成本高
- ❌ 每个 pod 都要时钟、并发控制

**推荐 A**：单个 K8s `CronJob`，`schedule: "0 19 * * *"`（UTC 19:00 = 北京 03:00），权限只够 exec 进 carher pod，结束发飞书。

YAML 草案（未部署）：

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: carher-session-truncate
  namespace: carher-admin
spec:
  schedule: "0 19 * * *"     # UTC, daily 19:00 = 北京 03:00
  concurrencyPolicy: Forbid  # 上一次没跑完就跳过
  successfulJobsHistoryLimit: 7
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          serviceAccountName: carher-truncate-runner
          restartPolicy: Never
          containers:
          - name: runner
            image: carher-admin-truncate:v1   # 自带 kubectl + python3 + 脚本
            command: ["/scripts/cron_truncate.sh"]
            env:
            - name: DRY_RUN
              value: "no"
            - name: LARK_WEBHOOK
              valueFrom:
                secretKeyRef:
                  name: carher-lark
                  key: ops-webhook
```

权限只需 `pods/exec` + `pods/get` on namespace `carher`。

## 失败回滚

每个实例的 backup 都在原 PVC 同目录，hardlink 占 0 字节。回滚一行：

```bash
kubectl exec -n carher "carher-40-xxx" -c carher -- mv \
  /data/.openclaw/agents/main/sessions/<file>.pre-truncate-backup-<TS> \
  /data/.openclaw/agents/main/sessions/<file>
```

如果 carher 进程在 truncate 之后已经 append 了新内容，**回滚会丢这部分新对话**——所以备份要至少留 24h 内的，超过就累积新内容太多回滚得不偿失，应该让代码层修复跟上。

## 上线前要先做的事

- [ ] her-40 手动跑后**观察 7 天**，看 carher 进程会不会出任何 sessionManager 异常
- [ ] 在 staging（如有）跑一次完整流程
- [ ] 写 dry-run-only 模式跑一周 → 看每天会处理多少实例 → 评估爆炸半径
- [ ] 飞书机器人接好告警 channel
- [ ] `carher-truncate-runner` SA + RBAC
- [ ] 本 cron 与 `clean_session_archives.sh` 错开跑（避免互相 lock 同 pod）

## 长期治本

如果 carher 主程序仓库把 `compaction.truncateAfterCompaction` 默认值改 `true`、并把 `shared-config.json5` 接入 config-reloader，那么：
- compaction 完成时**就**物理删除老 message
- 不再有 271MB 这种状态
- 本 cron 可以直接下线

在那之前，本 cron 是兜底运维工具。
