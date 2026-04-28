# Her 记忆与灵魂统一管理 — 落地计划

> 目标：把 K8s 上 ~200 个 her 实例的人格（SOUL/IDENTITY）、人格记忆（MEMORY.md / USER.md）、
> 语义记忆库（memory/main.sqlite）纳入**可观察、可备份、可批量下发、可共享**的统一管理体系。

## 1. 现状梳理

### 1.1 数据物理布局

- 每个 her 独立 PVC：`carher-{uid}-data`（RWX, StorageClass=`alibabacloud-cnfs-nas`）
- ~200 个 PVC **共用同一个阿里云 CNFS NAS**；在 NAS 控制面看就是 200 个子目录
- Operator 已经用过"共享 PVC"模式：`carher-shared-skills`（RO）、`carher-dept-skills`（RO）、
  `carher-shared-sessions`（RW, subPath by uid）

### 1.2 每个 PVC 的内容分层

| 类别 | 路径 | 当前管理方式 | 跨 her 统一管理价值 |
|---|---|---|---|
| **人格/记忆（私有）** | `workspace/MEMORY.md`、`SOUL.md`、`USER.md`、`IDENTITY.md` | 各自 PVC 独立 | 高：SOUL/IDENTITY 可模板化；MEMORY/USER 可观察、可备份 |
| **语义记忆** | `memory/main.sqlite`（+ `-wal`, `-shm`） | 各自 PVC 独立 | 中：可聚合只读查询；运行时写严禁共享 |
| **对话历史** | `agents/` | 各自 PVC 独立 | 低：量大、隐私，仅备份 |
| **运行时** | `browser/`、`media/`、`feishu-*-cache.json`、`logs/` | 各自 PVC 独立 | 无 |
| **共享只读资源** | `skills/`（shared-skills PVC）、`shared-config.json5`（ConfigMap） | 已统一 | — |
| **共享读写** | `sessions/`（shared-sessions PVC, subPath by uid） | 已统一 | — |

### 1.3 配置层的统一管理已经成熟

`k8s/base-config.yaml` → `carher-base-config` ConfigMap → 所有 her 共享；per-instance override 走
`carher-{uid}-user-config` ConfigMap（operator 生成），深合并，走 `config-reloader` sidecar 热更新。
**本方案参考这个模式，把"数据层"也做成 共享 + 私有 + 可热更新 的三段式。**

## 2. 分层设计

| Phase | 能力 | 改动范围 | 工作量 |
|---|---|---|---|
| **P1** | 每日全量快照 workspace + memory 到 NAS 备份目录；N 天回滚 | 新增 CronJob | 1-2 天 |
| **P2** | admin 挂 NAS 根只读，新增 Her Mind 页签，可视化查看所有 her 的 MEMORY/SOUL/USER/IDENTITY；跨实例 grep 搜索 | admin 后端 + 前端 | 2-3 天 |
| **P3** | SOUL/IDENTITY 模板仓 + 单实例/批量应用 + 自动 pre-apply 快照 | admin 写入通道 + UI | 3-5 天 |
| **P4** | `carher-shared-memory` 共享记忆池，公司级知识注入所有 her（不污染个人 MEMORY） | operator 加共享卷 + bot 侧 memorySearch 支持 `shared_memory` source | 1 周 |
| **P5**（选做） | 跨实例 SQLite 只读聚合查询 | admin 侧 SQL 聚合接口 | 2-3 天 |

## 3. Phase 1：每日快照 CronJob（最小代价兜底）

### 3.1 目标

- 每天凌晨 3 点 rsync 所有 `carher-*-data/workspace/` + `memory/` 到 `/nas-backup/her-snapshots/YYYY-MM-DD/{uid}/`
- 保留 30 天；滚动删除
- 单次全量约几 GB，rsync 增量很快；对运行中 bot 零影响（SQLite 加 `--ignore-errors` 兜底）

### 3.2 前置：NAS 根 PVC

在 `k8s/` 下新增一个 `PersistentVolume` 直接指向 NAS 根（或至少 `/carher-data/` 父目录），绑定到一个
`carher-nas-root` PVC。`StorageClass` 留空手动 bind，避免 dynamic provisioner 创建子目录。

### 3.3 CronJob 清单（路径：`k8s/her-memory-backup.yaml`）

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: her-memory-backup
  namespace: carher
spec:
  schedule: "0 3 * * *"
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        spec:
          restartPolicy: OnFailure
          containers:
          - name: rsync
            image: alpine:3.20
            command:
            - sh
            - -c
            - |
              set -eu
              apk add --no-cache rsync
              DATE=$(date +%F)
              DEST=/nas-backup/her-snapshots/$DATE
              mkdir -p $DEST
              for d in /nas/carher-*-data; do
                [ -d "$d/workspace" ] || continue
                uid=$(basename $d | sed 's/carher-//;s/-data//')
                mkdir -p $DEST/$uid
                rsync -a --ignore-errors $d/workspace/  $DEST/$uid/workspace/
                rsync -a --ignore-errors $d/memory/     $DEST/$uid/memory/ || true
              done
              find /nas-backup/her-snapshots -maxdepth 1 -type d \
                -mtime +30 -exec rm -rf {} +
            volumeMounts:
            - name: nas-root
              mountPath: /nas
              readOnly: true
            - name: nas-backup
              mountPath: /nas-backup
          volumes:
          - name: nas-root
            persistentVolumeClaim: { claimName: carher-nas-root }
          - name: nas-backup
            persistentVolumeClaim: { claimName: carher-nas-backup }
```

### 3.4 回滚 SOP

```bash
# 回滚 uid=14 的 MEMORY.md 到 2026-04-23 快照
kubectl exec -n carher deploy/carher-admin -- \
  cp -a /nas-backup/her-snapshots/2026-04-23/14/workspace/MEMORY.md \
        /nas/carher-14-data/workspace/MEMORY.md
curl -X POST -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/14/restart
```

### 3.5 验收

- 第二天 `kubectl exec -n carher -it <pod> -- ls /nas-backup/her-snapshots/`
- 随机抽 3 个 uid 验证 `workspace/MEMORY.md` 大小与原 PVC 一致

---

## 4. Phase 2：Admin 挂 NAS + Her Mind 只读面板

### 4.1 改动：给 `carher-admin` Deployment 加 NAS 根卷

```yaml
# k8s/carher-admin.yaml 片段
spec:
  template:
    spec:
      containers:
      - name: carher-admin
        volumeMounts:
        - name: nas-root
          mountPath: /nas
          readOnly: true   # P2 只读；P3 再开 RW
      volumes:
      - name: nas-root
        persistentVolumeClaim: { claimName: carher-nas-root-ro }
```

**安全策略**：P2 阶段 admin 容器层面 `readOnly: true`；即使后端代码有 bug 也写不坏数据。

### 4.2 后端新增接口（路径：`backend/app/api/her_mind.py`）

```
GET  /api/her-mind/{uid}/files               # 列 workspace 下所有 .md（大小、mtime）
GET  /api/her-mind/{uid}/files/{name}        # 读单文件（限 .md，白名单 MEMORY/SOUL/USER/IDENTITY）
GET  /api/her-mind/search?q=xxx&scope=soul   # grep 跨所有 her 的 workspace/*.md
GET  /api/her-mind/diff?uid=14&a=current&b=2026-04-23  # 和某日快照 diff
```

实现要点：
- 所有路径在代码里限死 `/nas/carher-{uid}-data/workspace/` + 固定文件名白名单，避免路径穿越
- `search` 用 subprocess 调 `grep -rIl --include='*.md'`，超时 5s，结果限 50 条
- 文件大小超 1MB 截断，防 OOM

### 4.3 前端：新增 Her Mind 页签

页面结构（React）：
- 左侧：实例列表（复用现有 instances table 的搜索/筛选）
- 右侧：tab 切换 MEMORY / SOUL / USER / IDENTITY，markdown 渲染
- 顶栏：全局搜索框（走 `/api/her-mind/search`，点结果跳对应实例）
- 右上角按钮：**"查看历史快照"** → 弹出日期选择 → diff 视图

### 4.4 验收

- 随机点 5 个实例能正确渲染四件套
- 搜索 "飞书" 能命中多个实例的 MEMORY.md 片段并跳转
- 尝试在 admin 容器里 `echo x > /nas/carher-14-data/workspace/MEMORY.md` 应该报 RO

---

## 5. Phase 3：SOUL/IDENTITY 模板仓 + 批量下发

### 5.1 模板模型

admin SQLite 新增表：

```sql
CREATE TABLE soul_templates (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,      -- e.g. "default-soul-v2", "vip-soul"
  target TEXT NOT NULL,           -- "SOUL" | "IDENTITY"
  content TEXT NOT NULL,          -- 可含 {{name}} {{owner}} {{prefix}} 占位
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE soul_template_applies (
  id INTEGER PRIMARY KEY,
  uid INTEGER NOT NULL,
  template_id INTEGER NOT NULL,
  snapshot_path TEXT,             -- pre-apply 快照绝对路径（便于回滚）
  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  applied_by TEXT
);
```

### 5.2 Admin Deployment 调整

需要写入时临时把 NAS 卷切 RW，方案二选一：

**A. 独立 writer Deployment（推荐）** —— 新建 `carher-admin-writer` Deployment，挂 NAS 根 RW，
   承载所有写接口；`carher-admin` 主实例仍保持 RO。Ingress 根据 path 前缀 `/api/her-mind/write/*`
   路由到 writer。这样生产 admin 炸了不会批量写坏所有 her。

**B. 单进程双挂载** —— admin Pod 同一个 NAS 根挂两次：`/nas` RO + `/nas-rw` RW；代码里写死读走
   `/nas`、写走 `/nas-rw`。省一个 Deployment，但更依赖代码纪律。

### 5.3 接口

```
# 模板 CRUD
GET/POST/PUT/DELETE /api/her-mind/templates

# 渲染预览（占位替换后长什么样）
POST /api/her-mind/templates/{id}/render { uid: 14 }

# 单实例应用（自动 pre-apply 快照 + 应用 + 记录 apply_log）
POST /api/her-mind/{uid}/apply-template { template_id, vars: {...} }

# 批量应用
POST /api/her-mind/batch-apply-template
     { template_id, uids: [14,25,30], restart: true, vars: {...} }

# 回滚到上一次 apply 前
POST /api/her-mind/{uid}/rollback-template
```

### 5.4 应用流程（单实例）

```
1. admin 读模板 content，渲染占位符
2. 对目标文件（SOUL.md / IDENTITY.md）做快照：
   cp -a /nas-rw/carher-14-data/workspace/SOUL.md \
         /nas-backup/pre-apply/14/20260424-153012/SOUL.md
3. 写入新内容
4. 记录 soul_template_applies
5. 如果 restart=true，POST /api/instances/14/restart
```

**关键：`SOUL.md` / `IDENTITY.md` 大概率是 bot 启动时读一次**（不像 openclaw.json 有 reloader），
所以批量应用默认应该触发 `/restart`。先在 1 个实例验证 bot 侧生效时机再决定默认值。

### 5.5 灰度流程（沿用 carher-instance-config-override skill 的三段式）

1. **Phase 0**：准备模板，在 admin 模板仓新建但不应用
2. **Phase 1**：挑 1 个自有实例（如 carher-1000）应用 + restart + 自测
3. **Phase 2**：按 deploy_group 分批批量应用（canary → early → stable），每批观察 5-10 min
4. **Phase 3**：全量完成后不删除模板（便于后续新 her 应用和一致性审计）

---

## 6. Phase 4：共享记忆池（carher-shared-memory）

### 6.1 架构

参考 `shared-skills` 的做法：

```
新建 PVC: carher-shared-memory  (RWX, NAS, ~20Gi)

operator/reconciler.go 的 volumes 增加：
  {Name: "shared-memory",
   VolumeSource: PVC{ClaimName:"carher-shared-memory", ReadOnly:true}}

container volumeMounts 增加：
  {Name: "shared-memory", MountPath: "/data/.openclaw/shared-memory", ReadOnly: true}

base-config shared-config.json5 调整 memorySearch.sources:
  sources: ["memory", "shared_memory", "sessions"]
```

### 6.2 Bot 侧改动（需要 carher 主程序配合）

memorySearch 实现按 source 分检索：`shared_memory` source 从
`/data/.openclaw/shared-memory/main.sqlite`（或同构的向量索引）读取。

### 6.3 公司级知识写入通道

admin 新增"公共知识库"编辑器：
- Markdown 文本 / 链接 / 片段
- 后台走 embedding 服务（已有 LiteLLM bge-m3）写入 `carher-shared-memory/main.sqlite`
- 所有 her 下一次 memorySearch 自动能检索到
- **不触发任何 her restart**，也不污染 197 个 MEMORY.md

### 6.4 风险

- 需要 carher 主程序同步改造（跨仓库改动）
- embedding 模型必须和 bot 侧 `memory/main.sqlite` 一致（都是 bge-m3），否则向量不可比
- 共享记忆的访问控制：目前设计是"全公司可见"，如果要按 deploy_group 隔离需要多个 shared-memory PVC

---

## 7. Phase 5（选做）：跨实例 SQLite 只读聚合

场景：运营问"哪些 her 的 memory 里出现过关键词 X"、调试"她到底记得什么"。

实现：admin 挂 NAS 根（已在 P2 完成），后端开 `/api/her-mind/search-memory` 接口：

```python
async def search_memory(q: str, uids: list[int]):
    results = []
    for uid in uids:
        db = f"/nas/carher-{uid}-data/memory/main.sqlite"
        if not os.path.exists(db): continue
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT content, score FROM memory WHERE content LIKE ? LIMIT 5",
                (f"%{q}%",)
            ).fetchall()
            results.append({"uid": uid, "rows": rows})
        finally:
            conn.close()
    return results
```

⚠️ 必须 `mode=ro`，bot 在跑时持 WAL；运维查询严禁写入。

---

## 8. 权限与审计

| 接口 | 权限 | 审计 |
|---|---|---|
| `GET /her-mind/*/files*` | admin 登录 或 API Key | 不记 |
| `GET /her-mind/search*` | admin 登录 或 API Key | 不记 |
| `POST /her-mind/*/apply-template` | admin 登录（不允许 API Key 批量写） | 全量入 `audit_log`：uid, template_id, applied_by, ts |
| `POST /her-mind/batch-apply-template` | 需 admin 登录 + 二次确认弹窗 | 全量入 `audit_log` |
| `POST /her-mind/*/rollback-template` | admin 登录 | 入 `audit_log` |

## 9. 与现有 skill 的配合

| 场景 | 配合 skill |
|---|---|
| 应用模板后批量 restart 不能断消息 | `carher-k8s-zero-downtime-rollout` |
| 批量应用的灰度节奏 | `carher-instance-config-override` |
| 备份失败排查、临时 Pod 拷贝 | `clone-instance-memory` |
| admin 后端/前端改动的部署 | `carher-admin-deploy`（不触发 bot 实例重启） |

## 10. 里程碑与建议节奏

| 周 | 产出 |
|---|---|
| W1 | P1 上线：CronJob + 回滚 SOP + 飞书告警备份失败 |
| W2 | P2 上线：admin Her Mind 只读面板 + 跨实例搜索 |
| W3 | P3 上线：SOUL/IDENTITY 模板仓；先挑 1 个实例验证 bot 侧生效时机，再决定 batch 默认策略 |
| W4-W5 | P4：评估 bot 改造成本；若 carher 主程序方便改就做 shared_memory，否则先跳过 |
| W5+ | P5 按需做，运营提需求再做 |

## 11. 立刻可做的先行动作（无代码改动）

1. 在 NAS 控制台建 `/nas-backup/her-snapshots/` 目录（给 P1 用）
2. 在 NAS 控制台验证 admin 是否已有 NAS 根可见权限（如果和 user PVC 在同一个文件系统，应该天然可见）
3. 先手动 rsync 一次 `carher-1000-data/workspace/` 到 `/nas-backup/`，验证路径和权限
4. 写一个简单脚本 `scripts/her-mind-dump.sh`，挂 NAS 根的临时 Pod 跑，观察 197 个 MEMORY.md 大小分布、是否有异常实例（空/超大/损坏）— 这个信息对后续 UI 设计有用
